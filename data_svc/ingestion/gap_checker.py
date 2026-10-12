"""Actual LanceDB coverage checks and repair helpers.

The old gap check relied on Redis watermarks.  A watermark can say a stock is
current when it has a row on the latest date, even if whole trading dates are
missing in the middle.  This module treats LanceDB as the source of truth and
checks date-by-date table coverage against an external trading calendar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import lancedb
import polars as pl

from config.config import Config
from config.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GapCheckResult:
    """Result returned by data gap checks."""

    has_gaps: bool
    total_missing_stocks: int = 0
    total_missing_dates: int = 0
    gaps: Dict[str, List[str]] = field(default_factory=dict)
    missing_dates: List[str] = field(default_factory=list)
    incomplete_dates: Dict[str, int] = field(default_factory=dict)
    unexpected_dates: Dict[str, int] = field(default_factory=dict)
    date_counts: Dict[str, int] = field(default_factory=dict)
    expected_dates: List[str] = field(default_factory=list)
    check_duration_ms: float = 0.0
    error_message: Optional[str] = None


def _normalize_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value)[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _compact_date(value: str) -> str:
    return value.replace("-", "")


def get_trading_dates(start_date: str, end_date: str, exclude_weekends: bool = True) -> List[str]:
    """Fallback calendar used when Tushare is unavailable."""

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    dates: List[str] = []
    current = start
    while current <= end:
        if not exclude_weekends or current.weekday() < 5:
            dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def get_expected_trading_dates(start_date: str, end_date: str) -> List[str]:
    """Return exchange trading dates without depending on daily_ohlcv itself."""

    token = getattr(Config, "TUSHARE_TOKEN", None)
    if token:
        try:
            import tushare as ts

            pro = ts.pro_api(token)
            cal = pro.trade_cal(
                exchange="SSE",
                start_date=_compact_date(start_date),
                end_date=_compact_date(end_date),
            )
            if cal is not None and not cal.empty:
                dates = (
                    cal[cal["is_open"] == 1]["cal_date"]
                    .astype(str)
                    .map(_normalize_date)
                    .tolist()
                )
                if dates:
                    return sorted(set(dates))
        except Exception as exc:
            logger.warning("[GapCheck] Tushare trading calendar unavailable: %s", exc)

    logger.warning("[GapCheck] using weekday fallback trading calendar")
    return get_trading_dates(start_date, end_date, exclude_weekends=True)


def _table_names(db: Any) -> List[str]:
    listed = db.list_tables()
    return listed.tables if hasattr(listed, "tables") else list(listed)


def _read_date_counts(table_name: str, start_date: str, end_date: str) -> Dict[str, int]:
    db_path = getattr(Config, "LANCEDB_PATH", str(Path("data") / "lancedb"))
    db = lancedb.connect(db_path)
    if table_name not in _table_names(db):
        return {}

    table = db.open_table(table_name)
    schema_cols = {field.name for field in table.schema}
    if "trade_date" not in schema_cols:
        return {}

    dataset = table.to_lance()
    columns = ["trade_date"]
    if "stock_code" in schema_cols:
        columns.append("stock_code")

    scanner = dataset.scanner(
        columns=columns,
        filter=f"trade_date >= date '{start_date}' AND trade_date <= date '{end_date}'",
    )
    df = pl.from_arrow(scanner.to_table())
    if df.is_empty():
        return {}

    if df.schema["trade_date"] != pl.Utf8:
        df = df.with_columns(pl.col("trade_date").cast(pl.Utf8).str.slice(0, 10).alias("trade_date"))

    return dict(
        df.group_by("trade_date")
        .len()
        .sort("trade_date")
        .iter_rows()
    )


def _infer_min_rows(date_counts: Dict[str, int], floor_ratio: float) -> int:
    full_counts = [count for count in date_counts.values() if count > 100]
    if not full_counts:
        return 1
    return max(1, int(sorted(full_counts)[len(full_counts) // 2] * floor_ratio))


def check_table_date_coverage(
    start_date: str,
    end_date: str,
    table_name: str = "daily_ohlcv",
    min_rows: Optional[int] = None,
    min_coverage_ratio: float = 0.8,
) -> GapCheckResult:
    """Check actual LanceDB rows for every expected trading date."""

    started = time.time()
    expected_dates = get_expected_trading_dates(start_date, end_date)
    date_counts = _read_date_counts(table_name, start_date, end_date)
    expected_set = set(expected_dates)
    min_required = min_rows or _infer_min_rows(date_counts, min_coverage_ratio)

    missing_dates = [date for date in expected_dates if date_counts.get(date, 0) == 0]
    incomplete_dates = {
        date: count
        for date, count in sorted(date_counts.items())
        if date in expected_set and 0 < count < min_required
    }
    unexpected_dates = {
        date: count
        for date, count in sorted(date_counts.items())
        if date not in expected_set
    }

    gaps: Dict[str, List[str]] = {}
    if missing_dates:
        gaps["__missing_dates__"] = missing_dates
    if incomplete_dates:
        gaps["__incomplete_dates__"] = list(incomplete_dates)

    duration_ms = (time.time() - started) * 1000
    has_gaps = bool(missing_dates or incomplete_dates)
    logger.info(
        "[GapCheck] %s %s~%s missing=%s incomplete=%s unexpected=%s duration=%.1fms",
        table_name,
        start_date,
        end_date,
        len(missing_dates),
        len(incomplete_dates),
        len(unexpected_dates),
        duration_ms,
    )
    return GapCheckResult(
        has_gaps=has_gaps,
        total_missing_dates=len(missing_dates) + len(incomplete_dates),
        gaps=gaps,
        missing_dates=missing_dates,
        incomplete_dates=incomplete_dates,
        unexpected_dates=unexpected_dates,
        date_counts=date_counts,
        expected_dates=expected_dates,
        check_duration_ms=duration_ms,
    )


def get_trading_dates_from_db() -> List[str]:
    """Compatibility helper: list dates physically present in daily_ohlcv."""

    db_path = getattr(Config, "LANCEDB_PATH", str(Path("data") / "lancedb"))
    db = lancedb.connect(db_path)
    if "daily_ohlcv" not in _table_names(db):
        return []
    counts = _read_date_counts("daily_ohlcv", "1900-01-01", "2099-12-31")
    return sorted(counts)


def check_data_gaps(
    trading_dates: Optional[List[str]] = None,
    stock_codes: Optional[List[str]] = None,
    lookback_days: int = 30,
) -> GapCheckResult:
    """Check for missing or incomplete trading dates in LanceDB.

    ``stock_codes`` is accepted for API compatibility.  The production check is
    date coverage based because whole-market holes were being missed by the
    previous per-stock watermark logic.
    """

    if trading_dates:
        normalized = sorted({_normalize_date(date) for date in trading_dates})
        return check_table_date_coverage(normalized[0], normalized[-1])

    available_dates = get_trading_dates_from_db()
    end_date = available_dates[-1] if available_dates else datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return check_table_date_coverage(start_date, end_date)


def check_and_repair_gaps(
    trading_dates: Optional[List[str]] = None,
    stock_codes: Optional[List[str]] = None,
    max_retries: int = 3,
    auto_repair: bool = True,
    remove_unexpected_dates: bool = False,
    precompute_factors: bool = True,
) -> Dict[str, Any]:
    """Check LanceDB coverage and repair missing dates from Tushare."""

    check_result = check_data_gaps(trading_dates, stock_codes)
    repair_dates = sorted(set(check_result.missing_dates) | set(check_result.incomplete_dates))
    removed_unexpected_dates: List[str] = []

    if remove_unexpected_dates and check_result.unexpected_dates:
        db_path = getattr(Config, "LANCEDB_PATH", str(Path("data") / "lancedb"))
        db = lancedb.connect(db_path)
        tables = _table_names(db)
        for date in sorted(check_result.unexpected_dates):
            date_removed = False
            for table_name in ("daily_ohlcv", "factors"):
                if table_name not in tables:
                    continue
                table = db.open_table(table_name)
                try:
                    table.delete(f"trade_date = DATE '{date}'")
                    date_removed = True
                except Exception:
                    try:
                        table.delete(f'trade_date = "{date}"')
                        date_removed = True
                    except Exception as exc:
                        logger.warning(
                            "[GapCheck] failed to remove unexpected date %s from %s: %s",
                            date,
                            table_name,
                            exc,
                        )
            if date_removed:
                removed_unexpected_dates.append(date)
                logger.warning("[GapCheck] removed non-trading date %s from local tables", date)

    if not repair_dates or not auto_repair:
        return {
            "check_result": check_result,
            "repair_results": [],
            "failed_repairs": [],
            "removed_unexpected_dates": removed_unexpected_dates,
        }

    from data_svc.storage.factor_precompute_service import FactorPrecomputeService
    from data_svc.storage.unified_updater import UnifiedDataUpdater

    updater = UnifiedDataUpdater()
    repair_results: List[Dict[str, Any]] = []
    failed_repairs: List[Dict[str, Any]] = []

    for date in repair_dates:
        start = _compact_date(date)
        success = False
        for attempt in range(1, max_retries + 1):
            result = updater.update_stock_daily(start, start)
            rows = result.get("records_added", 0)
            if rows > 0:
                repair_results.append({"date": date, "rows": rows, "success": True})
                success = True
                break
            logger.warning("[GapCheck] repair %s returned no rows on attempt %s/%s", date, attempt, max_retries)
        if not success:
            failed_repairs.append({"date": date, "error": "no rows fetched", "retries": max_retries})

    if repair_results and precompute_factors:
        start = min(item["date"] for item in repair_results)
        end = max(item["date"] for item in repair_results)
        factor_result = FactorPrecomputeService().precompute_all_factors(start, end)
    else:
        factor_result = None

    return {
        "check_result": check_result,
        "repair_results": repair_results,
        "failed_repairs": failed_repairs,
        "factor_result": factor_result,
        "removed_unexpected_dates": removed_unexpected_dates,
    }


def startup_check_and_reconcile() -> Dict[str, Any]:
    """Run a non-destructive startup coverage check."""

    result = check_data_gaps()
    status = "gaps_found" if result.has_gaps else "ok"
    return {
        "status": status,
        "gap_check": result,
    }
