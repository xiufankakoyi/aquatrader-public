"""Unified LanceDB data updater."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import lancedb
import pandas as pd
import polars as pl
from loguru import logger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config.config import Config


@dataclass
class UpdateResult:
    success: bool
    message: str
    stock_dates_updated: int = 0
    stock_records_added: int = 0
    benchmark_dates_updated: int = 0
    benchmark_records_added: int = 0
    industry_records_updated: int = 0
    factor_records_computed: int = 0
    limit_status_records: int = 0
    errors: list[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class UnifiedDataUpdater:
    """Writes Tushare, DragonEye and factor outputs into LanceDB."""

    REFRESH_LOOKBACK_DAYS = 3

    def __init__(self, progress_callback: Optional[Callable[[str, int, str], None]] = None):
        self.progress_callback = progress_callback
        self.token = Config.TUSHARE_TOKEN
        self.lancedb_path = getattr(Config, "LANCEDB_PATH", str(Path(BASE_DIR) / "data" / "lancedb"))

    def _emit_progress(self, status: str, progress: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(status, progress, message)
        logger.info("[UnifiedUpdater] {} {}% {}", status, progress, message)

    def _get_lancedb(self):
        Path(self.lancedb_path).mkdir(parents=True, exist_ok=True)
        return lancedb.connect(self.lancedb_path)

    def _get_latest_date(self, table_name: str) -> Optional[str]:
        try:
            db = self._get_lancedb()
            tables = self._list_tables(db)
            if table_name not in tables:
                return None
            table = db.open_table(table_name)
            if "trade_date" not in {field.name for field in table.schema}:
                return None
            date_table = table.to_lance().scanner(columns=["trade_date"]).to_table()
            if date_table.num_rows == 0:
                return None
            value = pl.from_arrow(date_table).select(pl.col("trade_date").max()).item()
            if hasattr(value, "strftime"):
                return value.strftime("%Y%m%d")
            return str(value)[:10].replace("-", "")
        except Exception as exc:
            logger.warning("latest date lookup failed for {}: {}", table_name, exc)
            return None

    def _list_tables(self, db) -> list[str]:
        listed = db.list_tables()
        return listed.tables if hasattr(listed, "tables") else list(listed)

    def _get_trade_dates(self, pro, start_date: str, end_date: str) -> list[str]:
        cal_df = pro.trade_cal(exchange="SSE", start_date=start_date, end_date=end_date)
        if cal_df is None or cal_df.empty:
            return []
        return cal_df[cal_df["is_open"] == 1]["cal_date"].astype(str).tolist()

    def _delete_trade_dates(self, table, trade_dates: list[str]) -> None:
        for td in trade_dates:
            date_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(str(td)) == 8 else str(td)[:10]
            try:
                table.delete(f"trade_date = DATE '{date_str}'")
            except Exception:
                try:
                    table.delete(f'trade_date = "{date_str}"')
                except Exception as exc:
                    logger.warning("delete date {} failed: {}", date_str, exc)

    @staticmethod
    def _date_text(value: Any) -> str:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        text = str(value)[:10]
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return text

    def _read_table_dates(self, table, dates: list[str]) -> pl.DataFrame:
        parts = []
        for value in dates:
            date_text = self._date_text(value)
            try:
                parts.append(
                    pl.from_arrow(
                        table.to_lance()
                        .scanner(filter=f"trade_date = date '{date_text}'")
                        .to_table()
                    )
                )
            except Exception as exc:
                logger.warning("read existing date {} failed: {}", date_text, exc)
        return pl.concat(parts, how="vertical_relaxed") if parts else pl.DataFrame()

    def _preserve_existing_values(
        self,
        incoming: pl.DataFrame,
        existing: pl.DataFrame,
        key_columns: list[str],
    ) -> pl.DataFrame:
        if existing.is_empty():
            return incoming

        existing = existing.unique(key_columns, keep="last")
        incoming = incoming.unique(key_columns, keep="last")
        cast_expressions = []
        for column in key_columns:
            if incoming.schema.get(column) != existing.schema.get(column):
                cast_expressions.append(
                    pl.col(column).cast(existing.schema[column], strict=False).alias(column)
                )
        if cast_expressions:
            incoming = incoming.with_columns(cast_expressions)
        merged = incoming.join(existing, on=key_columns, how="left", suffix="__existing")

        expressions = []
        incoming_columns = set(incoming.columns)
        for column in existing.columns:
            if column in key_columns:
                expressions.append(pl.col(column))
                continue
            existing_column = f"{column}__existing"
            if column in incoming_columns and existing_column in merged.columns:
                expressions.append(pl.coalesce([pl.col(column), pl.col(existing_column)]).alias(column))
            elif column in incoming_columns:
                expressions.append(pl.col(column))
            elif existing_column in merged.columns:
                expressions.append(pl.col(existing_column).alias(column))
            elif column in merged.columns:
                expressions.append(pl.col(column))

        extra_columns = [column for column in incoming.columns if column not in existing.columns]
        expressions.extend(pl.col(column) for column in extra_columns)
        return merged.select(expressions)

    def _add_or_create(self, table_name: str, df: pl.DataFrame, delete_dates: Optional[list[str]] = None) -> int:
        if df.is_empty():
            return 0
        db = self._get_lancedb()
        tables = self._list_tables(db)
        if table_name in tables:
            table = db.open_table(table_name)
            if delete_dates:
                existing = self._read_table_dates(table, delete_dates)
                key_columns = [
                    column
                    for column in ("trade_date", "stock_code", "symbol")
                    if column in df.columns and column in {field.name for field in table.schema}
                ]
                if "trade_date" in key_columns and len(key_columns) >= 2:
                    df = self._preserve_existing_values(df, existing, key_columns)

                # Materialize Arrow before deleting old rows so schema errors cannot
                # remove a valid date partition.
                arrow_table = df.to_arrow()
                self._delete_trade_dates(table, delete_dates)
                try:
                    table.add(arrow_table)
                except Exception:
                    if not existing.is_empty():
                        logger.warning("restoring {} after failed write", table_name)
                        table.add(existing.to_arrow())
                    raise
            else:
                table.add(df.to_arrow())
        else:
            table = db.create_table(table_name, df.to_arrow())
            for col in ("trade_date", "stock_code", "symbol"):
                if col in df.columns:
                    try:
                        table.create_scalar_index(col)
                    except Exception:
                        pass
        return len(df)

    @staticmethod
    def _merge_optional_frame(
        base: pd.DataFrame,
        extra: Optional[pd.DataFrame],
        columns: list[str],
    ) -> pd.DataFrame:
        if extra is None or extra.empty:
            return base
        selected = [column for column in columns if column in extra.columns]
        keys = [column for column in ("ts_code", "trade_date") if column in base.columns and column in extra.columns]
        if not keys:
            return base
        for key in keys:
            if key not in selected:
                selected.insert(0, key)
        return base.merge(extra[selected].drop_duplicates(keys), on=keys, how="left")

    def _fetch_stock_daily_date(self, pro, trade_date: str) -> tuple[pd.DataFrame, list[str]]:
        daily = pro.daily(trade_date=trade_date)
        if daily is None or daily.empty:
            return pd.DataFrame(), ["daily"]

        missing_auxiliary = []
        try:
            daily_basic = pro.daily_basic(
                trade_date=trade_date,
                fields=(
                    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,"
                    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
                    "free_share,total_mv,circ_mv"
                ),
            )
        except Exception as exc:
            logger.warning("daily_basic {} failed: {}", trade_date, exc)
            daily_basic = None
        if daily_basic is None or daily_basic.empty:
            missing_auxiliary.append("daily_basic")
        else:
            for column in ("turnover_rate", "turnover_rate_f", "volume_ratio", "total_mv", "circ_mv"):
                if column not in daily_basic.columns or daily_basic[column].isna().all():
                    missing_auxiliary.append(f"daily_basic.{column}")
        daily = self._merge_optional_frame(
            daily,
            daily_basic,
            [
                "ts_code", "trade_date", "turnover_rate", "turnover_rate_f", "volume_ratio",
                "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
                "total_share", "float_share", "free_share", "total_mv", "circ_mv",
            ],
        )

        try:
            adj = pro.adj_factor(trade_date=trade_date)
        except Exception as exc:
            logger.warning("adj_factor {} failed: {}", trade_date, exc)
            adj = None
        if adj is None or adj.empty:
            missing_auxiliary.append("adj_factor")
        elif "adj_factor" not in adj.columns or adj["adj_factor"].isna().all():
            missing_auxiliary.append("adj_factor.adj_factor")
        daily = self._merge_optional_frame(
            daily,
            adj,
            ["ts_code", "trade_date", "adj_factor"],
        )

        try:
            limits = pro.stk_limit(trade_date=trade_date)
        except Exception as exc:
            logger.warning("stk_limit {} failed: {}", trade_date, exc)
            limits = None
        if limits is None or limits.empty:
            missing_auxiliary.append("stk_limit")
        else:
            for column in ("up_limit", "down_limit"):
                if column not in limits.columns or limits[column].isna().all():
                    missing_auxiliary.append(f"stk_limit.{column}")
        daily = self._merge_optional_frame(
            daily,
            limits,
            ["ts_code", "trade_date", "up_limit", "down_limit"],
        )
        return daily, missing_auxiliary

    @staticmethod
    def _validate_stock_daily_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        if df.empty:
            return df, 0
        frame = df.copy()
        frame = frame.drop_duplicates(["ts_code", "trade_date"], keep="last")
        numeric_columns = [
            "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
        ]
        for column in numeric_columns:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        invalid = (
            frame[["ts_code", "trade_date", "open", "high", "low", "close", "vol"]]
            .isna()
            .any(axis=1)
        )
        invalid |= (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
        invalid |= frame["vol"] < 0
        invalid |= frame["high"] < frame[["open", "close", "low"]].max(axis=1)
        invalid |= frame["low"] > frame[["open", "close", "high"]].min(axis=1)

        if {"pre_close", "pct_chg"} <= set(frame.columns):
            comparable = frame["pre_close"].notna() & (frame["pre_close"] > 0) & frame["pct_chg"].notna()
            calculated = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
            invalid |= comparable & ((calculated - frame["pct_chg"]).abs() > 0.05)

        invalid_count = int(invalid.sum())
        return frame.loc[~invalid].copy(), invalid_count

    def update_stock_daily(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        self._emit_progress("UPDATING", 10, "updating stock daily data")
        import tushare as ts

        pro = ts.pro_api(self.token)
        if start_date is None:
            latest = self._get_latest_date("daily_ohlcv")
            start_date = (
                datetime.strptime(latest, "%Y%m%d") - timedelta(days=self.REFRESH_LOOKBACK_DAYS)
            ).strftime("%Y%m%d") if latest else "20200101"
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if start_date > end_date:
            return {"dates_updated": 0, "records_added": 0}

        trade_dates = self._get_trade_dates(pro, start_date, end_date)
        frames: list[pd.DataFrame] = []
        failed_dates: list[str] = []
        missing_auxiliary: Dict[str, list[str]] = {}
        invalid_rows = 0
        for trade_date in trade_dates:
            df, missing = self._fetch_stock_daily_date(pro, trade_date)
            if df.empty:
                failed_dates.append(trade_date)
                continue
            if missing:
                missing_auxiliary[trade_date] = missing
            df, bad_rows = self._validate_stock_daily_rows(df)
            invalid_rows += bad_rows
            if df.empty:
                failed_dates.append(trade_date)
                continue
            df["stock_code"] = df["ts_code"]
            frames.append(df)

        if not frames:
            return {
                "dates_updated": 0,
                "records_added": 0,
                "expected_dates": len(trade_dates),
                "failed_dates": failed_dates,
                "invalid_rows": invalid_rows,
                "missing_auxiliary": missing_auxiliary,
            }
        combined = pd.concat(frames, ignore_index=True)
        df_pl = pl.from_pandas(combined)
        rename_map = {
            "pct_chg": "change_pct",
            "vol": "volume",
            "turnover_rate_f": "turnover_free",
            "pre_close": "prev_close",
            "change": "change_amount",
            "circ_mv": "float_mv",
            "dv_ratio": "dividend_yield",
            "dv_ttm": "dividend_yield_ttm",
            "total_share": "total_shares",
            "float_share": "float_shares",
            "free_share": "free_float_shares",
            "up_limit": "limit_up",
            "down_limit": "limit_down",
        }
        df_pl = df_pl.rename({k: v for k, v in rename_map.items() if k in df_pl.columns})
        cols = [
            "stock_code", "ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "change_pct", "change_amount", "prev_close",
            "turnover_rate", "turnover_free", "volume_ratio", "total_mv", "float_mv",
            "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dividend_yield",
            "dividend_yield_ttm", "total_shares", "float_shares", "free_float_shares",
            "limit_up", "limit_down", "adj_factor",
        ]
        df_pl = df_pl.select([c for c in cols if c in df_pl.columns])
        if "trade_date" not in df_pl.columns:
            raise ValueError("daily_ohlcv update produced no trade_date column")
        df_pl = df_pl.with_columns(pl.col("trade_date").cast(pl.Utf8).str.to_date("%Y%m%d"))
        fetched_dates = sorted(combined["trade_date"].dropna().astype(str).unique().tolist())
        rows = self._add_or_create("daily_ohlcv", df_pl, fetched_dates)
        return {
            "dates_updated": len(fetched_dates),
            "records_added": rows,
            "expected_dates": len(trade_dates),
            "failed_dates": failed_dates,
            "invalid_rows": invalid_rows,
            "missing_auxiliary": missing_auxiliary,
            "first_date": fetched_dates[0] if fetched_dates else None,
            "last_date": fetched_dates[-1] if fetched_dates else None,
        }

    def update_benchmark_daily(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, int]:
        self._emit_progress("UPDATING", 30, "updating benchmark index data")
        import tushare as ts

        pro = ts.pro_api(self.token)
        if start_date is None:
            latest = self._get_latest_date("index_daily")
            start_date = (
                datetime.strptime(latest, "%Y%m%d") - timedelta(days=self.REFRESH_LOOKBACK_DAYS)
            ).strftime("%Y%m%d") if latest else "20200101"
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        indices = {
            "000300.SH": "CSI300",
            "000905.SH": "CSI500",
            "000001.SH": "SSE Composite",
            "399001.SZ": "SZSE Component",
            "000016.SH": "SSE50",
            "399006.SZ": "ChiNext",
        }
        frames = []
        for code, name in indices.items():
            df = pro.index_daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df["index_name"] = name
                frames.append(df)
        if not frames:
            return {"dates_updated": 0, "records_added": 0}
        combined = pd.concat(frames, ignore_index=True)
        df_pl = pl.from_pandas(combined).rename({"ts_code": "symbol", "vol": "volume", "index_name": "name"})
        cols = ["trade_date", "symbol", "name", "open", "high", "low", "close", "volume", "amount"]
        df_pl = df_pl.select([c for c in cols if c in df_pl.columns])
        df_pl = df_pl.with_columns(pl.col("trade_date").cast(pl.Utf8).str.to_date("%Y%m%d"))
        trade_dates = sorted(combined["trade_date"].dropna().astype(str).unique().tolist())
        rows = self._add_or_create("index_daily", df_pl, trade_dates)
        return {"dates_updated": len(trade_dates), "records_added": rows}

    def update_stock_basic(self) -> Dict[str, int]:
        self._emit_progress("UPDATING", 60, "updating stock_info snapshot")
        import tushare as ts

        pro = ts.pro_api(self.token)
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,area,industry,market,list_date")
        if df is None or df.empty:
            return {"records_updated": 0}
        df["stock_code"] = df["ts_code"]
        df_pl = pl.from_pandas(df)
        db = self._get_lancedb()
        if "stock_info" in self._list_tables(db):
            db.drop_table("stock_info")
        db.create_table("stock_info", df_pl.to_arrow())
        return {"records_updated": len(df_pl)}

    def update_factors(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        from data_svc.storage.factor_precompute_service import FactorPrecomputeService

        start_dash = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}" if start_date and len(start_date) == 8 else start_date
        end_dash = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}" if end_date and len(end_date) == 8 else end_date
        result = FactorPrecomputeService().precompute_all_factors(start_date=start_dash, end_date=end_dash)
        return {
            "success": result.success,
            "factors_computed": result.records_computed if result.success else 0,
            "message": result.message,
            "error": result.error,
        }

    def update_dragon_eye(self, target_date: Optional[str] = None, backfill: bool = False) -> Dict[str, Any]:
        self._emit_progress("UPDATING", 85, "updating DragonEye data")
        from data_svc.ingestion.dragon_eye_adapter import DragonEyeAdapter

        target_date = target_date or datetime.now().strftime("%Y-%m-%d")
        adapter = DragonEyeAdapter()
        target_dates = (
            adapter.scan_missing_dates(end_date=target_date)
            if backfill
            else [target_date]
        )
        if not target_dates and backfill:
            return {"records_updated": 0, "dates_updated": 0, "failed_dates": []}

        rows = 0
        updated_dates = []
        failed_dates = []
        for date_text in target_dates:
            if not adapter.crawl(target_date=date_text, backfill=False):
                failed_dates.append(date_text)
                continue
            written = adapter.ingest_to_lancedb(date_text)
            if written > 0:
                rows += written
                updated_dates.append(date_text)
            else:
                failed_dates.append(date_text)
        return {
            "records_updated": rows,
            "dates_updated": len(updated_dates),
            "updated_dates": updated_dates,
            "failed_dates": failed_dates,
        }

    def run_full_update(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        skip_stock: bool = False,
        skip_benchmark: bool = False,
        skip_industry: bool = False,
        skip_factors: bool = False,
        skip_limit_status: bool = True,
        skip_dragon: bool = False,
        dragon_target_date: Optional[str] = None,
        dragon_backfill: bool = False,
    ) -> UpdateResult:
        started = time.time()
        result = UpdateResult(success=False, message="")
        try:
            if not skip_stock:
                stock = self.update_stock_daily(start_date, end_date)
                result.stock_dates_updated = stock.get("dates_updated", 0)
                result.stock_records_added = stock.get("records_added", 0)
                if stock.get("failed_dates"):
                    raise RuntimeError(f"stock daily update failed for dates: {stock['failed_dates']}")
                from data_svc.ingestion.gap_checker import check_and_repair_gaps

                repair_start = start_date
                if repair_start is None:
                    repair_start = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
                repair_end = end_date or datetime.now().strftime("%Y%m%d")
                gap_repair = check_and_repair_gaps(
                    trading_dates=[
                        f"{repair_start[:4]}-{repair_start[4:6]}-{repair_start[6:8]}" if len(repair_start) == 8 else repair_start,
                        f"{repair_end[:4]}-{repair_end[4:6]}-{repair_end[6:8]}" if len(repair_end) == 8 else repair_end,
                    ],
                    auto_repair=True,
                    remove_unexpected_dates=True,
                    precompute_factors=False,
                )
                repaired_rows = sum(item.get("rows", 0) for item in gap_repair.get("repair_results", []))
                if repaired_rows:
                    result.stock_records_added += repaired_rows
                if gap_repair.get("failed_repairs"):
                    raise RuntimeError(f"stock gap repair failed: {gap_repair['failed_repairs']}")
                result.details["stock_update"] = stock
                result.details["gap_repair"] = {
                    "repaired_dates": [item.get("date") for item in gap_repair.get("repair_results", [])],
                    "failed_repairs": gap_repair.get("failed_repairs", []),
                    "removed_unexpected_dates": gap_repair.get("removed_unexpected_dates", []),
                }
            if not skip_benchmark:
                benchmark = self.update_benchmark_daily(start_date, end_date)
                result.benchmark_dates_updated = benchmark.get("dates_updated", 0)
                result.benchmark_records_added = benchmark.get("records_added", 0)
            if not skip_industry:
                basic = self.update_stock_basic()
                result.industry_records_updated = basic.get("records_updated", 0)
            if not skip_dragon:
                dragon_date = dragon_target_date
                if dragon_date is None and end_date:
                    dragon_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}" if len(end_date) == 8 else end_date
                dragon = self.update_dragon_eye(dragon_date, backfill=dragon_backfill)
                result.limit_status_records = dragon.get("records_updated", 0)
                result.details["dragon_eye"] = dragon
            if not skip_factors:
                factor_dates = []
                stock_details = result.details.get("stock_update", {})
                for key in ("first_date", "last_date"):
                    if stock_details.get(key):
                        factor_dates.append(str(stock_details[key]))
                gap_details = result.details.get("gap_repair", {})
                factor_dates.extend(date for date in gap_details.get("repaired_dates", []) if date)
                factor_dates.extend(date for date in gap_details.get("removed_unexpected_dates", []) if date)

                if start_date:
                    factor_dates.append(start_date)
                if end_date:
                    factor_dates.append(end_date)

                if factor_dates:
                    compact_dates = [date.replace("-", "") for date in factor_dates]
                    factors = self.update_factors(min(compact_dates), max(compact_dates))
                    if not factors.get("success"):
                        raise RuntimeError(f"factor precompute failed: {factors.get('message')}")
                    result.factor_records_computed = factors.get("factors_computed", 0)
                    result.details["factor_range"] = {
                        "start_date": min(compact_dates),
                        "end_date": max(compact_dates),
                    }
                else:
                    result.details["factor_range"] = None
            result.success = True
            result.message = f"update completed in {time.time() - started:.1f}s"
        except Exception as exc:
            result.success = False
            result.message = str(exc)
            result.errors.append(str(exc))
            logger.exception("full update failed")
        return result


def run_unified_update(**kwargs) -> UpdateResult:
    return UnifiedDataUpdater().run_full_update(**kwargs)


if __name__ == "__main__":
    update_result = run_unified_update()
    print(update_result)
