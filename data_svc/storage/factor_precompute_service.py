"""Factor precompute service backed by LanceDB."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import lancedb
import polars as pl
import pyarrow as pa

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config.config import Config
from config.logger import get_logger
from data_svc.storage.lancedb_reader import LanceDBDataReader

logger = get_logger(__name__)


@dataclass
class FactorComputeResult:
    success: bool
    records_computed: int = 0
    message: str = ""
    error: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    table_name: str = "factors"


class FactorPrecomputeService:
    """Computes rolling factors from daily_ohlcv and upserts factors by date."""

    def __init__(self):
        self.lancedb_path = getattr(Config, "LANCEDB_PATH", str(Path(BASE_DIR) / "data" / "lancedb"))
        # Use a fresh reader for every compute job. The process-wide singleton can
        # hold an older Lance dataset snapshot after the updater appends new rows.
        self.reader = LanceDBDataReader(self.lancedb_path)

    def precompute_all_factors(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> FactorComputeResult:
        started = time.time()
        try:
            base_data = self._load_base_data(start_date, end_date)
            if base_data is None or base_data.is_empty():
                return FactorComputeResult(False, message="no base data available")

            factor_df = self._compute_factors(base_data)
            if start_date:
                factor_df = factor_df.filter(pl.col("trade_date") >= pl.lit(start_date).str.to_date())
            if end_date:
                factor_df = factor_df.filter(pl.col("trade_date") <= pl.lit(end_date).str.to_date())
            if factor_df.is_empty():
                return FactorComputeResult(False, message="no factor rows in requested date range")

            self._validate_factor_output(factor_df)
            self._write_to_lancedb(factor_df)
            elapsed = time.time() - started
            return FactorComputeResult(
                True,
                records_computed=len(factor_df),
                message=f"computed and stored {len(factor_df)} factor rows in {elapsed:.1f}s",
                start_date=str(factor_df["trade_date"].min())[:10],
                end_date=str(factor_df["trade_date"].max())[:10],
            )
        except Exception as exc:
            logger.exception("factor precompute failed")
            return FactorComputeResult(False, message=f"factor precompute failed: {exc}", error=str(exc))

    def _load_base_data(self, start_date: Optional[str], end_date: Optional[str]) -> Optional[pl.DataFrame]:
        """Load daily OHLCV data with extra columns needed for factor computation.

        Args:
            start_date: Optional start date filter (YYYY-MM-DD).
            end_date: Optional end date filter (YYYY-MM-DD).

        Returns:
            Sorted Polars DataFrame or None if no data available.
        """
        extended_start = None
        if start_date:
            from datetime import datetime, timedelta

            extended_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=260)).strftime("%Y-%m-%d")
        # 请求 turnover_rate 列；若表中不存在会被 _normalize_fields 静默跳过
        fields = [
            "stock_code", "trade_date",
            "open", "high", "low", "close", "volume", "amount",
            "turnover_rate",
        ]
        df = self.reader.read_all(start_date=extended_start, end_date=end_date, fields=fields)
        if df is None or df.is_empty():
            return None
        if "stock_code" not in df.columns and "ts_code" in df.columns:
            df = df.with_columns(pl.col("ts_code").alias("stock_code"))
        if df.schema.get("trade_date") == pl.Utf8:
            df = df.with_columns(pl.col("trade_date").str.to_date())
        return df.sort(["stock_code", "trade_date"])

    def _compute_factors(self, df: pl.DataFrame) -> pl.DataFrame:
        required = {"stock_code", "trade_date", "close", "high", "low", "volume"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"missing required columns for factor precompute: {sorted(missing)}")

        out = df.with_columns([
            pl.col("close").rolling_mean(5).over("stock_code").alias("ma5"),
            pl.col("close").rolling_mean(10).over("stock_code").alias("ma10"),
            pl.col("close").rolling_mean(20).over("stock_code").alias("ma20"),
            pl.col("close").rolling_mean(30).over("stock_code").alias("ma30"),
            pl.col("close").rolling_mean(60).over("stock_code").alias("ma60"),
            pl.col("close").rolling_mean(120).over("stock_code").alias("ma120"),
            pl.col("volume").rolling_mean(5).over("stock_code").alias("volume_ma5"),
            pl.col("volume").rolling_mean(20).over("stock_code").alias("volume_ma20"),
            pl.col("close").pct_change(1).over("stock_code").alias("ret_1d"),
            # ret_3d: 3日收益率
            pl.col("close").pct_change(3).over("stock_code").alias("ret_3d"),
            # ret_5d: 5日收益率
            pl.col("close").pct_change(5).over("stock_code").alias("ret_5d"),
            (pl.col("close") / pl.col("close").shift(5).over("stock_code") - 1).alias("return_5d"),
            (pl.col("close") / pl.col("close").shift(20).over("stock_code") - 1).alias("return_20d"),
            pl.col("close").pct_change(1).over("stock_code").rolling_std(20).over("stock_code").alias("volatility_20d"),
            ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("intraday_range"),
            pl.col("close").diff().over("stock_code").alias("_price_diff"),
        ])
        out = out.with_columns([
            # volume_ratio: 今日成交量 / 5日均量（依赖 volume_ma5，需在第二步计算）
            (pl.col("volume") / pl.col("volume_ma5")).alias("volume_ratio"),
            pl.when(pl.col("_price_diff") > 0).then(pl.col("_price_diff")).otherwise(0.0).alias("_gain"),
            pl.when(pl.col("_price_diff") < 0).then(-pl.col("_price_diff")).otherwise(0.0).alias("_loss"),
            (
                pl.col("close").ewm_mean(span=12, adjust=False).over("stock_code")
                - pl.col("close").ewm_mean(span=26, adjust=False).over("stock_code")
            ).alias("macd_dif"),
        ])
        out = out.with_columns([
            (
                100 - 100 / (
                    1
                    + pl.col("_gain").ewm_mean(span=6, adjust=False).over("stock_code")
                    / (pl.col("_loss").ewm_mean(span=6, adjust=False).over("stock_code") + 1e-10)
                )
            ).alias("rsi_6"),
            (
                100 - 100 / (
                    1
                    + pl.col("_gain").ewm_mean(span=12, adjust=False).over("stock_code")
                    / (pl.col("_loss").ewm_mean(span=12, adjust=False).over("stock_code") + 1e-10)
                )
            ).alias("rsi_12"),
            (
                100 - 100 / (
                    1
                    + pl.col("_gain").ewm_mean(span=24, adjust=False).over("stock_code")
                    / (pl.col("_loss").ewm_mean(span=24, adjust=False).over("stock_code") + 1e-10)
                )
            ).alias("rsi_24"),
            pl.col("macd_dif").ewm_mean(span=9, adjust=False).over("stock_code").alias("macd_dea"),
        ])
        out = out.with_columns(
            (2 * (pl.col("macd_dif") - pl.col("macd_dea"))).alias("macd_bar")
        )
        cols = [
            "trade_date", "stock_code", "ma5", "ma10", "ma20", "ma30", "ma60", "ma120",
            "rsi_6", "rsi_12", "rsi_24", "macd_dif", "macd_dea", "macd_bar",
            "volume_ma5", "volume_ma20", "ret_1d", "ret_3d", "ret_5d", "volume_ratio",
            "return_5d", "return_20d",
            "volatility_20d", "intraday_range",
        ]
        # turnover_rate 从 daily_ohlcv 透传；若源表无此列则跳过并警告
        if "turnover_rate" in out.columns:
            cols.append("turnover_rate")
        else:
            logger.warning("turnover_rate not found in daily_ohlcv, skipping this factor column")
        return out.select([c for c in cols if c in out.columns])

    def _validate_factor_output(self, df: pl.DataFrame) -> None:
        duplicate_keys = (
            df.group_by(["trade_date", "stock_code"])
            .len()
            .filter(pl.col("len") > 1)
        )
        if not duplicate_keys.is_empty():
            raise ValueError(
                f"factor precompute produced {len(duplicate_keys)} duplicate date/symbol keys"
            )

        required = ["rsi_6", "rsi_12", "rsi_24", "macd_dif", "macd_dea", "macd_bar"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"factor precompute missing required columns: {missing}")
        all_null = [col for col in required if df[col].null_count() == len(df)]
        if all_null:
            raise ValueError(f"factor precompute produced only null values for: {all_null}")
        for column in ("rsi_6", "rsi_12", "rsi_24"):
            invalid = df.filter(
                pl.col(column).is_not_null()
                & ((pl.col(column) < -1e-8) | (pl.col(column) > 100.0000001))
            )
            if not invalid.is_empty():
                raise ValueError(f"factor precompute produced out-of-range values for {column}")

    def _write_to_lancedb(self, df: pl.DataFrame) -> bool:
        Path(self.lancedb_path).mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(self.lancedb_path)
        table_name = "factors"
        listed = db.list_tables()
        tables = listed.tables if hasattr(listed, "tables") else list(listed)
        if table_name in tables:
            table = db.open_table(table_name)
            target_cols = [field.name for field in table.schema]
            new_cols = [col for col in df.columns if col not in target_cols]
            if new_cols:
                fields = []
                for col in new_cols:
                    dtype = df.schema[col]
                    if dtype == pl.Date:
                        arrow_type = pa.date32()
                    elif dtype == pl.Boolean:
                        arrow_type = pa.bool_()
                    elif dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64):
                        arrow_type = pa.int64()
                    elif dtype == pl.Utf8:
                        arrow_type = pa.string()
                    else:
                        arrow_type = pa.float64()
                    fields.append(pa.field(col, arrow_type))
                table.add_columns(fields)
                target_cols = [field.name for field in table.schema]
            writable_cols = [col for col in df.columns if col in target_cols]
            missing_required = [col for col in ("trade_date", "stock_code") if col not in writable_cols]
            if missing_required:
                raise ValueError(f"factor rows missing required columns: {missing_required}")
            dropped_cols = [col for col in df.columns if col not in target_cols]
            if dropped_cols:
                logger.warning("dropping factor columns not present in existing schema: %s", dropped_cols)
            df = df.select(writable_cols)
            dates = df.select("trade_date").unique().to_series().to_list()
            existing_parts = []
            lance_ds = table.to_lance()
            for value in dates:
                date_str = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]
                try:
                    if hasattr(lance_ds, "scanner"):
                        existing_parts.append(
                            pl.from_arrow(
                                lance_ds.scanner(
                                    columns=target_cols,
                                    filter=f"trade_date = date '{date_str}'",
                                ).to_table()
                            )
                        )
                except Exception as exc:
                    logger.warning("read existing factors date %s failed: %s", date_str, exc)

            existing_df = pl.concat(existing_parts, how="vertical") if existing_parts else pl.DataFrame()
            if not existing_df.is_empty():
                incoming_df = df
                update_cols = [col for col in df.columns if col not in ("trade_date", "stock_code")]
                merged = existing_df.join(
                    df,
                    on=["trade_date", "stock_code"],
                    how="left",
                    suffix="__new",
                )
                exprs = []
                for col in target_cols:
                    new_col = f"{col}__new"
                    if col in update_cols and new_col in merged.columns:
                        exprs.append(pl.coalesce([pl.col(new_col), pl.col(col)]).alias(col))
                    elif col in merged.columns:
                        exprs.append(pl.col(col))
                    else:
                        exprs.append(pl.lit(None).alias(col))
                merged_existing = merged.select(exprs)

                new_only = incoming_df.join(
                    existing_df.select(["trade_date", "stock_code"]).unique(),
                    on=["trade_date", "stock_code"],
                    how="anti",
                )
                if not new_only.is_empty():
                    for col in target_cols:
                        if col not in new_only.columns:
                            new_only = new_only.with_columns(pl.lit(None).alias(col))
                    new_only = new_only.select(target_cols)
                    df = pl.concat([merged_existing, new_only], how="vertical_relaxed")
                else:
                    df = merged_existing

            for value in dates:
                date_str = value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]
                try:
                    table.delete(f"trade_date = DATE '{date_str}'")
                except Exception:
                    try:
                        table.delete(f'trade_date = "{date_str}"')
                    except Exception as exc:
                        logger.warning("delete factors date %s failed: %s", date_str, exc)
            table.add(df.to_arrow())
        else:
            table = db.create_table(table_name, df.to_arrow())
            for col in ("trade_date", "stock_code"):
                try:
                    table.create_scalar_index(col)
                except Exception:
                    pass
        return True

    def incremental_update(self, trade_date: str) -> FactorComputeResult:
        return self.precompute_all_factors(start_date=trade_date, end_date=trade_date)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Precompute factors into LanceDB")
    parser.add_argument("--start-date", type=str, help="start date YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, help="end date YYYY-MM-DD")
    args = parser.parse_args()
    result = FactorPrecomputeService().precompute_all_factors(args.start_date, args.end_date)
    print(result.message)
    raise SystemExit(0 if result.success else 1)


if __name__ == "__main__":
    main()
