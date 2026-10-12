#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Optimized LanceDB-backed data service for stock screener routes."""

from __future__ import annotations

import os
import sys
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import lancedb
import polars as pl

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from config.config import Config
from data_svc.storage.lancedb_reader import get_lancedb_reader
from data_svc.query.fast_market_query import get_fast_market_query

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    data: pl.DataFrame
    loaded_at: float
    hit_count: int = 0


class ScreenerDataService:
    """Fast single-date screener dataset loader.

    The hot path reads only the requested date and columns from LanceDB.  It keeps
    data in Polars all the way through filtering and avoids `to_arrow()` full-table
    reads except as a last-resort fallback.
    """

    DEFAULT_STOCK_COLUMNS = {
        "stock_code", "ts_code", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
        "total_mv", "float_mv", "change_pct", "prev_close", "change_amount",
    }

    DEFAULT_FACTOR_COLUMNS = {
        "ma5", "ma10", "ma20", "ma30", "ma60", "ma120",
        "ma3_avg_price", "ma5_avg_price", "ma10_avg_price",
        "rsi_6", "rsi_12", "rsi_24", "macd_dif", "macd_dea", "macd_bar",
        "kdj_k", "kdj_d", "kdj_j", "wr_14", "cci_14",
        "boll_upper", "boll_mid", "boll_lower", "bb_width_20",
        "ema12", "ema26", "ema50", "ema200", "dmi_pdi", "dmi_mdi", "dmi_adx", "trix_12",
        "volume_ma5", "volume_ma20", "vol_ma5", "vol_ma10", "vol_ma20",
        "obv", "vwap_20", "vr_26", "bias_6", "bias_12", "bias_24", "mfi_14", "volume_std_20d",
        "hv_20d", "hv_60d", "atr_14", "atr_ratio_14_50",
        "ret_1d", "return_5d", "return_20d", "return_60d", "ret_5d", "ret_20d", "ret_60d",
        "volatility_20d", "intraday_range",
        "max_drawdown_20d", "max_drawdown_60d", "max_drawdown_250d",
        "sharpe_20d", "sortino_250d", "calmar_250d", "var_95_250d",
        "ma_bull_alignment", "golden_cross", "death_cross", "macd_golden_cross", "macd_death_cross",
        "beta_60d", "beta_120d", "beta_250d", "alpha_60d", "alpha_120d", "alpha_250d",
        "corr_60d", "corr_120d", "corr_250d", "excess_ret_20d", "ir_250d",
    }

    FACTOR_COLUMN_RENAME = {"macdbar": "macd_bar"}

    def __init__(self, cache_ttl: int = 3600):
        self._cache: Dict[str, CacheEntry] = {}
        self._cache_ttl = cache_ttl
        self._lancedb_reader = get_lancedb_reader()
        self._fmq = get_fast_market_query()
        self._db = lancedb.connect(getattr(Config, "LANCEDB_PATH", str(project_root / "data" / "lancedb")))
        logger.info("[ScreenerDataService] initialized with LanceDB")

    def _get_cache_key(self, date: str, columns: Set[str]) -> str:
        return f"{date}:{hash(frozenset(columns))}"

    def _is_cache_valid(self, entry: CacheEntry) -> bool:
        return (time.time() - entry.loaded_at) < self._cache_ttl

    def _table_names(self) -> list[str]:
        listed = self._db.list_tables()
        return listed.tables if hasattr(listed, "tables") else list(listed)

    def _scan_table_for_date(self, table_name: str, date: str, columns: Set[str]) -> pl.DataFrame:
        if table_name not in self._table_names():
            return pl.DataFrame()
        table = self._db.open_table(table_name)
        schema_names = {field.name for field in table.schema}
        selected = [c for c in columns if c in schema_names]
        for key_col in ("trade_date", "stock_code"):
            if key_col in schema_names and key_col not in selected:
                selected.append(key_col)

        filter_expr = f"trade_date = date '{date}'"
        lance_ds = table.to_lance()
        if hasattr(lance_ds, "scanner"):
            kwargs: Dict[str, Any] = {"filter": filter_expr}
            if selected:
                kwargs["columns"] = selected
            return pl.from_arrow(lance_ds.scanner(**kwargs).to_table())

        df = pl.from_arrow(table.to_arrow())
        if "trade_date" in df.columns:
            df = df.filter(pl.col("trade_date") == pl.lit(date).str.to_date())
        if selected:
            df = df.select([c for c in selected if c in df.columns])
        return df

    def _resolve_required_columns(self, fields: Optional[List[str]], conditions: Optional[List[Dict]]) -> Set[str]:
        required = set(fields) if fields else (self.DEFAULT_STOCK_COLUMNS | self.DEFAULT_FACTOR_COLUMNS)
        required.update({"stock_code", "ts_code", "trade_date"})
        for cond in conditions or []:
            field = cond.get("field")
            if field:
                required.add(field)
        return required

    def get_data(self, date: str, fields: Optional[List[str]] = None, conditions: Optional[List[Dict]] = None) -> Optional[pl.DataFrame]:
        start = time.perf_counter()
        required = self._resolve_required_columns(fields, conditions)
        cache_key = self._get_cache_key(date, required)
        entry = self._cache.get(cache_key)
        if entry and self._is_cache_valid(entry):
            entry.hit_count += 1
            return entry.data

        stock_cols = (required & self.DEFAULT_STOCK_COLUMNS) | {"stock_code", "ts_code", "trade_date"}
        factor_cols = required & self.DEFAULT_FACTOR_COLUMNS

        stock_df = self._fmq._read_table("daily_ohlcv", start_date=date, end_date=date, fields=list(stock_cols))
        if stock_df is None or stock_df.is_empty():
            return None

        if "change_pct" in required and "change_pct" in stock_df.columns and stock_df["change_pct"].is_null().any():
            if {"close", "prev_close"}.issubset(stock_df.columns):
                stock_df = stock_df.with_columns(
                    pl.when(pl.col("prev_close").is_not_null() & (pl.col("prev_close") != 0))
                    .then((pl.col("close") - pl.col("prev_close")) / pl.col("prev_close") * 100)
                    .otherwise(pl.col("change_pct"))
                    .alias("change_pct")
                )

        if factor_cols:
            factor_df = self._load_factor_data_optimized(date, set(factor_cols) | {"stock_code", "trade_date"})
            if factor_df is not None and not factor_df.is_empty():
                stock_df = self._merge_data_optimized(stock_df, factor_df)

        stock_df = self._add_stock_names_optimized(stock_df)
        self._cache[cache_key] = CacheEntry(stock_df, time.time(), 1)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("[ScreenerDataService] loaded %s rows, %s cols for %s in %.1fms", len(stock_df), len(stock_df.columns), date, elapsed_ms)
        return stock_df

    def _load_factor_data_optimized(self, date: str, columns: Set[str]) -> Optional[pl.DataFrame]:
        df = self._fmq.get_factor_snapshot(date, fields=list(columns))
        if df.is_empty():
            return None
        rename_map = {k: v for k, v in self.FACTOR_COLUMN_RENAME.items() if k in df.columns}
        if rename_map:
            df = df.rename(rename_map)
        available = [c for c in columns if c in df.columns]
        for key_col in ("trade_date", "stock_code"):
            if key_col in df.columns and key_col not in available:
                available.append(key_col)
        return df.select(available) if available else df

    def _merge_data_optimized(self, stock_df: pl.DataFrame, factor_df: pl.DataFrame) -> pl.DataFrame:
        if "stock_code" not in stock_df.columns or "stock_code" not in factor_df.columns:
            return stock_df
        if "trade_date" in stock_df.columns and "trade_date" in factor_df.columns:
            if stock_df.schema["trade_date"] != factor_df.schema["trade_date"]:
                if stock_df.schema["trade_date"] == pl.Utf8:
                    stock_df = stock_df.with_columns(pl.col("trade_date").str.to_date())
                if factor_df.schema["trade_date"] == pl.Utf8:
                    factor_df = factor_df.with_columns(pl.col("trade_date").str.to_date())
            join_cols = ["stock_code", "trade_date"]
        else:
            join_cols = ["stock_code"]
        duplicate_factor_cols = [
            c for c in factor_df.columns
            if c not in join_cols and c in stock_df.columns
        ]
        if duplicate_factor_cols:
            stock_df = stock_df.drop(duplicate_factor_cols)
        return stock_df.join(factor_df, on=join_cols, how="left")

    def _load_stock_name_map_ts_code(self) -> Optional[pl.DataFrame]:
        df = self._fmq._read_table("stock_info", fields=["ts_code", "name", "stock_name"])
        if df.is_empty() or "ts_code" not in df.columns:
            return None
        name_col = "name" if "name" in df.columns else "stock_name" if "stock_name" in df.columns else None
        if name_col is None:
            return None
        return (
            df.select(["ts_code", name_col])
            .rename({"ts_code": "stock_code", name_col: "name"})
            .unique(subset=["stock_code"])
        )

    def _scan_table_columns(self, table_name: str, columns: Set[str]) -> pl.DataFrame:
        if table_name not in self._table_names():
            return pl.DataFrame()
        table = self._db.open_table(table_name)
        schema_names = {field.name for field in table.schema}
        selected = [c for c in columns if c in schema_names]
        lance_ds = table.to_lance()
        if hasattr(lance_ds, "scanner"):
            return pl.from_arrow(lance_ds.scanner(columns=selected).to_table())
        df = pl.from_arrow(table.to_arrow())
        return df.select(selected) if selected else df

    def _add_stock_names_optimized(self, df: pl.DataFrame) -> pl.DataFrame:
        if "stock_name" in df.columns and df["stock_name"].null_count() < len(df):
            return df
        if "name" in df.columns and "stock_name" not in df.columns:
            return df.rename({"name": "stock_name"})
        if "stock_code" not in df.columns:
            return df

        cache_key = "_stock_name_map_ts_code"
        entry = self._cache.get(cache_key)
        if entry and self._is_cache_valid(entry):
            name_map = entry.data
        else:
            name_map = self._load_stock_name_map_ts_code()
            if name_map is None or name_map.is_empty():
                return df
            self._cache[cache_key] = CacheEntry(name_map, time.time(), 1)
        out = df.join(name_map, on="stock_code", how="left")
        return out.rename({"name": "stock_name"}) if "name" in out.columns else out

    def apply_filter_optimized(self, df: pl.DataFrame, conditions: List[Dict], logic: str = "AND") -> pl.DataFrame:
        if not conditions:
            return df
        filters = []
        missing_fields = []
        invalid_conditions = []
        for cond in conditions:
            field = cond.get("field")
            operator = cond.get("operator") or cond.get("op")
            value = cond.get("value")
            value2 = cond.get("value2")
            if not field or not operator:
                invalid_conditions.append(cond)
                continue
            if field not in df.columns:
                missing_fields.append(field)
                continue
            col = pl.col(field)
            try:
                if operator == ">":
                    filters.append(col > float(value))
                elif operator == "<":
                    filters.append(col < float(value))
                elif operator in ("=", "=="):
                    filters.append(col == float(value))
                elif operator == ">=":
                    filters.append(col >= float(value))
                elif operator == "<=":
                    filters.append(col <= float(value))
                elif operator == "between":
                    filters.append((col >= float(value)) & (col <= float(value2)))
                elif operator == "contains":
                    filters.append(col.cast(pl.Utf8).str.contains(str(value)))
                elif operator == "starts_with":
                    filters.append(col.cast(pl.Utf8).str.starts_with(str(value)))
                else:
                    invalid_conditions.append(cond)
            except (TypeError, ValueError):
                invalid_conditions.append(cond)
        if missing_fields:
            raise ValueError(f"filter fields not available in loaded data: {sorted(set(missing_fields))}")
        if invalid_conditions:
            raise ValueError(f"invalid filter conditions: {invalid_conditions}")
        if not filters:
            return df
        combined = filters[0]
        logic = (logic or "AND").upper()
        for expr in filters[1:]:
            combined = (combined | expr) if logic == "OR" else (combined & expr)
        return df.filter(combined)

    def get_cache_stats(self) -> Dict[str, Any]:
        return {
            "cache_count": len(self._cache),
            "entries": [
                {
                    "key": key,
                    "hit_count": entry.hit_count,
                    "age_seconds": time.time() - entry.loaded_at,
                    "rows": len(entry.data),
                }
                for key, entry in self._cache.items()
            ],
        }

    def clear_cache(self) -> None:
        self._cache.clear()
        logger.info("[ScreenerDataService] cache cleared")


_screener_service: Optional[ScreenerDataService] = None


def get_screener_service() -> ScreenerDataService:
    global _screener_service
    if _screener_service is None:
        _screener_service = ScreenerDataService()
    return _screener_service
