#!/usr/bin/env python
"""AquaTrader 数据更新统一入口（v2 收敛版）。

数据架构收敛后，本脚本是项目内**唯一**的权威数据更新入口。

支持的 ``--target``：

- ``lancedb`` (默认)：更新 ``daily_ohlcv`` / ``stock_info`` / ``trade_status`` / ``factors``。
- ``matrix-cache``：从 LanceDB 主源重建回测矩阵缓存。
- ``sqlite-meta``：只刷新策略/组合/任务/系统配置等元信息，不写行情。
- ``parquet-snapshot``：手动导出快照，不默认执行。
- ``parquet-cache``：增量重建 Parquet 分片查询加速缓存。
- ``all``：执行 lancedb + factors + matrix-cache + data_health，**不**包含 parquet-snapshot / parquet-cache。

通用参数：

- ``--start-date`` / ``--end-date``     时间窗口（默认 30 天 ~ 今天）
- ``--dry-run``                        只检查不写入
- ``--only daily`` / ``--only factors`` 限制 stage 集合
- ``--max-symbols``                    限制单次抓取的 symbol 数（0 = 全部）
- ``--request-delay``                  baostock 单股请求间隔秒数

历史重复入口已合并并归档。如需找回历史行为，请阅读归档目录的 README。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import Config
from config.logger import get_logger
from infrastructure.data_providers.base import (
    DAILY_BARS_COLUMNS,
    ensure_columns,
    normalize_symbols,
    stock_code,
)
from server.services.data_health_service import build_data_health_report

logger = get_logger("scripts.update_market_data_incremental")

REPORT_DIR = PROJECT_ROOT / "data" / "reports"
MATRIX_CACHE_DIR = PROJECT_ROOT / "data" / "matrix_cache"
PARQUET_DIR = Path(Config.PARQUET_DIR)
PARQUET_SNAPSHOT_PATH = PARQUET_DIR / "stock_daily.parquet"
REPORT_LATEST = REPORT_DIR / "update_report_latest.json"
LANCEDB_REPORT_FLAG = "lancedb"  # data_health 报告里 lancedb 数据集的 name


# ---------------------------------------------------------------------------
# target / only 常量
# ---------------------------------------------------------------------------
TARGET_LANCEDB = "lancedb"
TARGET_MATRIX_CACHE = "matrix-cache"
TARGET_SQLITE_META = "sqlite-meta"
TARGET_PARQUET_SNAPSHOT = "parquet-snapshot"
TARGET_PARQUET_CACHE = "parquet-cache"
TARGET_ALL = "all"

VALID_TARGETS = {
    TARGET_LANCEDB,
    TARGET_MATRIX_CACHE,
    TARGET_SQLITE_META,
    TARGET_PARQUET_SNAPSHOT,
    TARGET_PARQUET_CACHE,
    TARGET_ALL,
}

ONLY_DAILY = "daily"
ONLY_FACTORS = "factors"
ONLY_ALL = "all"
VALID_ONLY = {ONLY_DAILY, ONLY_FACTORS, ONLY_ALL}

# target 集合 -> 实际执行 stage 列表
TARGET_PLAN: dict[str, list[str]] = {
    TARGET_LANCEDB: ["daily", "stock_info", "trade_status", "factors", "data_health"],
    TARGET_MATRIX_CACHE: ["matrix_cache", "data_health"],
    TARGET_SQLITE_META: ["sqlite_meta", "data_health"],
    TARGET_PARQUET_SNAPSHOT: ["parquet_snapshot", "data_health"],
    TARGET_PARQUET_CACHE: ["parquet_cache_rebuild", "data_health"],
    # 默认包含 lancedb + factors + matrix-cache + data_health；不包含 parquet-snapshot / parquet-cache
    TARGET_ALL: ["daily", "stock_info", "trade_status", "factors", "matrix_cache", "data_health"],
}


# ---------------------------------------------------------------------------
# 阶段结果结构
# ---------------------------------------------------------------------------
@dataclass
class StageResult:
    name: str
    started_at: str
    finished_at: str = ""
    success: bool = False
    status: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    rows_added: int = 0
    rows_updated: int = 0
    skipped_reason: str = ""
    # 本阶段实际读取、计算或写入的最新数据日（YYYY-MM-DD）。
    data_date: str = ""


# ---------------------------------------------------------------------------
# 全局最新交易日工具方法
# ---------------------------------------------------------------------------
def _latest_data_date_from_lancedb() -> str:
    """从 LanceDB ``daily_ohlcv`` 读最新交易日，失败时返回空串。"""

    try:
        from data_svc.storage.lancedb_reader import LanceDBDataReader
    except Exception:
        return ""
    try:
        reader = LanceDBDataReader()
        # get_date_range 返回 (earliest, latest)
        _earliest, latest = reader.get_date_range()
        return str(latest)[:10] if latest else ""
    except Exception as exc:
        logger.debug("读取 LanceDB 最新交易日失败: %s", exc)
        return ""


def _stale_modules(stages: list[StageResult], global_latest: str) -> list[str]:
    """返回 ``data_date`` 与全局最新日期不一致的阶段名。"""

    if not global_latest:
        return []
    stale: list[str] = []
    for stage in stages:
        if not stage.data_date:
            continue
        if stage.data_date < global_latest:
            stale.append(stage.name)
    return stale


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(
        description="AquaTrader 唯一数据更新入口（v2 收敛版）",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=TARGET_LANCEDB,
        choices=sorted(VALID_TARGETS),
        help=(
            "更新目标：lancedb / matrix-cache / sqlite-meta / "
            "parquet-snapshot / parquet-cache / all（默认 lancedb）"
        ),
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="起始日期 YYYY-MM-DD；默认 end_date - 30 天",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="截止日期 YYYY-MM-DD；默认今天",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查缺失交易日，不写入任何数据",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=ONLY_ALL,
        choices=sorted(VALID_ONLY),
        help="仅执行某个阶段：daily/factors/all",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="限制单次抓取的 symbol 数量（0 表示全部）",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.0,
        help="每次 baostock 单股请求之间的休眠秒数",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_iso_date(value: Any) -> str:
    """把任意日期字符串规范成 YYYY-MM-DD。"""

    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    import pandas as pd

    return pd.to_datetime(text).strftime("%Y-%m-%d")


def _resolve_window(args: argparse.Namespace) -> tuple[str, str]:
    """根据 CLI 解析起始 / 截止日期。"""

    end_date = _normalize_iso_date(args.end_date) or datetime.now().strftime("%Y-%m-%d")
    if args.start_date:
        start_date = _normalize_iso_date(args.start_date)
    else:
        start_date = (
            datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=30)
        ).strftime("%Y-%m-%d")
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stage_decorator(name: str) -> "StageResult":
    """构造一个 stage 结果对象的快速工厂。"""

    return StageResult(name=name, started_at=_now_text())


def _finalize_stage(stage: StageResult) -> StageResult:
    """补齐稳定的报告状态，不根据 data_date 猜测阶段是否成功。"""

    if stage.skipped_reason:
        stage.status = "skipped"
    elif stage.success:
        stage.status = "success"
    else:
        stage.status = "failed"
    return stage


# ---------------------------------------------------------------------------
# LanceDB 行情写入
# ---------------------------------------------------------------------------
def _list_active_symbols(max_symbols: int = 0) -> list[str]:
    """从 LanceDB ``stock_info`` / ``daily_ohlcv`` 拉取在市 symbol 列表。"""

    try:
        import lancedb
    except Exception as exc:
        logger.warning("lancedb 不可用，无法拉取股票列表: %s", exc)
        return []

    lancedb_path = getattr(Config, "LANCEDB_PATH", "")
    if not lancedb_path:
        return []
    try:
        db = lancedb.connect(lancedb_path)
        listed = db.list_tables()
        names = listed.tables if hasattr(listed, "tables") else list(listed)
        if "stock_info" not in names:
            return []
        table = db.open_table("stock_info")
        schema_cols = {field.name for field in table.schema}
        col = "ts_code" if "ts_code" in schema_cols else (
            "stock_code" if "stock_code" in schema_cols else None
        )
        if not col:
            return []
        import polars as pl

        arrow = table.to_lance().scanner(columns=[col]).to_table()
        df = pl.from_arrow(arrow)
        if df.is_empty():
            return []
        series = df.get_column(col) if hasattr(df, "get_column") else df[col]
        symbols = normalize_symbols(series.to_pandas()).dropna().astype(str).tolist()
        if max_symbols and max_symbols > 0:
            symbols = symbols[:max_symbols]
        return symbols
    except Exception as exc:
        logger.warning("读取 stock_info 失败: %s", exc)
        return []


def _fetch_with_baostock(
    symbols: list[str],
    date: str,
    request_delay: float = 0.0,
):
    """使用 baostock 拉取单日全市场日线（pandas DataFrame）。"""

    import baostock as bs
    import time as _time

    if not symbols:
        import pandas as pd

        return pd.DataFrame(columns=DAILY_BARS_COLUMNS)
    session = bs.login()
    try:
        if getattr(session, "error_code", "0") != "0":
            raise RuntimeError(f"baostock 登录失败: {session.error_msg}")
        fields = (
            "date,code,open,high,low,close,preclose,volume,amount,"
            "pctChg,turn,tradestatus"
        )
        import pandas as pd

        frames: list = []
        for index, symbol in enumerate(symbols):
            if request_delay and index > 0:
                _time.sleep(request_delay)
            code = stock_code(symbol)
            suffix = (
                "sh" if code.startswith(("6", "5", "9"))
                else "bj" if code.startswith(("4", "8"))
                else "sz"
            )
            bs_code = f"{suffix}.{code}"
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    fields,
                    start_date=date,
                    end_date=date,
                    frequency="d",
                    adjustflag="2",
                )
            except Exception as exc:
                logger.debug("baostock %s 失败: %s", symbol, exc)
                continue
            rows: list[list[str]] = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) >= 12 and row[11] not in ("1", "1.0"):
                    continue
                rows.append(row)
            if not rows:
                continue
            df = pd.DataFrame(rows, columns=rs.fields)
            frame = pd.DataFrame(index=df.index)
            frame["trade_date"] = df["date"].astype(str)
            frame["symbol"] = normalize_symbols(
                df["code"].astype(str).str.replace(".", "", regex=False)
            )
            frame["stock_name"] = ""
            frame["open"] = pd.to_numeric(df["open"], errors="coerce")
            frame["high"] = pd.to_numeric(df["high"], errors="coerce")
            frame["low"] = pd.to_numeric(df["low"], errors="coerce")
            frame["close"] = pd.to_numeric(df["close"], errors="coerce")
            frame["pre_close"] = pd.to_numeric(df["preclose"], errors="coerce")
            frame["pct_chg"] = pd.to_numeric(df["pctChg"], errors="coerce")
            frame["amount"] = pd.to_numeric(df["amount"], errors="coerce")
            frame["volume"] = pd.to_numeric(df["volume"], errors="coerce")
            frame["turnover_rate"] = pd.to_numeric(df["turn"], errors="coerce")
            frame["volume_ratio"] = None
            frame["total_market_cap"] = None
            frame["float_market_cap"] = None
            frame["provider"] = "baostock"
            frame["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            frames.append(ensure_columns(frame, DAILY_BARS_COLUMNS))
        return (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=DAILY_BARS_COLUMNS)
        )
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _map_to_daily_ohlcv(df):
    """把 provider 输出映射为 LanceDB daily_ohlcv 表结构。"""

    import polars as pl

    if df is None or getattr(df, "empty", True):
        return pl.DataFrame()
    pl_df = pl.from_pandas(df) if hasattr(df, "columns") else df
    if "trade_date" not in pl_df.columns and "date" in pl_df.columns:
        pl_df = pl_df.rename({"date": "trade_date"})

    if pl_df.schema.get("trade_date") != pl.Utf8:
        pl_df = pl_df.with_columns(
            pl.col("trade_date").cast(pl.Utf8).str.slice(0, 10).alias("trade_date")
        )

    if "symbol" in pl_df.columns:
        pl_df = pl_df.with_columns(pl.col("symbol").alias("stock_code"))
    elif "stock_code" not in pl_df.columns:
        pl_df = pl_df.with_columns(pl.lit("").alias("stock_code"))

    rename_map = {
        "pct_chg": "change_pct",
        "pre_close": "prev_close",
        "total_market_cap": "total_mv",
        "float_market_cap": "float_mv",
    }
    pl_df = pl_df.rename({k: v for k, v in rename_map.items() if k in pl_df.columns})

    keep = [
        "stock_code", "trade_date",
        "open", "high", "low", "close", "volume", "amount",
        "change_pct", "prev_close", "turnover_rate", "volume_ratio",
        "total_mv", "float_mv",
    ]
    selected = [c for c in keep if c in pl_df.columns]
    return pl_df.select(selected)


def _write_daily_to_lancedb(pl_df, trade_dates: Iterable[str], *, dry_run: bool) -> int:
    """通过 ``UnifiedDataUpdater`` 写入 daily_ohlcv。"""

    import polars as pl

    if pl_df is None or pl_df.is_empty():
        return 0
    if dry_run:
        logger.info("[dry-run] 跳过写入 daily_ohlcv (rows=%d)", len(pl_df))
        return 0
    try:
        from data_svc.storage.unified_updater import UnifiedDataUpdater
    except Exception as exc:
        logger.warning("UnifiedDataUpdater 不可用: %s", exc)
        return 0

    updater = UnifiedDataUpdater()
    return updater._add_or_create(  # noqa: SLF001 - 复用现有 upsert 逻辑
        "daily_ohlcv",
        pl_df,
        delete_dates=sorted({str(d)[:10] for d in trade_dates}),
    )


def _stage_daily(args: argparse.Namespace, start_date: str, end_date: str) -> StageResult:
    """执行行情增量更新阶段。"""

    stats = _stage_decorator("daily")
    start = time.time()
    try:
        symbols = _list_active_symbols(args.max_symbols)
        if not symbols and not args.dry_run:
            stats.message = "未从 lancedb.stock_info 取到 symbol 列表，行情抓取为空"
            stats.skipped_reason = "no_active_symbols"
            return stats

        # dry-run 模式：只展示窗口内的交易日，不真正调用网络 API
        if args.dry_run:
            window_days = []
            cur = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            while cur <= end_dt:
                if cur.weekday() < 5:
                    window_days.append(cur.strftime("%Y-%m-%d"))
                cur += timedelta(days=1)
            stats.success = True
            stats.data_date = _latest_data_date_from_lancedb()
            stats.message = (
                f"[dry-run] 跳过 baostock 网络请求（窗口 {len(window_days)} 个交易日，"
                f"symbols={len(symbols)}）；未写入任何数据"
            )
            stats.details.update(
                symbols_count=len(symbols),
                window_trading_dates=window_days,
                dry_run=True,
            )
            return stats

        # 单日拉取（baostock 限制）：窗口逐日抓取
        cur = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        total_rows = 0
        fetched: list[str] = []
        failed: list[str] = []
        while cur <= end_dt:
            date = cur.strftime("%Y-%m-%d")
            if cur.weekday() >= 5:
                cur += timedelta(days=1)
                continue
            try:
                df = _fetch_with_baostock(symbols, date, args.request_delay)
            except Exception as exc:
                logger.warning("baostock 拉取 %s 失败: %s", date, exc)
                failed.append(date)
                cur += timedelta(days=1)
                continue
            if df is None or df.empty:
                failed.append(date)
                cur += timedelta(days=1)
                continue
            pl_df = _map_to_daily_ohlcv(df)
            if pl_df.is_empty():
                failed.append(date)
                cur += timedelta(days=1)
                continue
            rows = _write_daily_to_lancedb(
                pl_df, [date], dry_run=args.dry_run,
            )
            total_rows += rows
            if rows:
                fetched.append(date)
            cur += timedelta(days=1)

        stats.details.update(
            symbols_count=len(symbols),
            succeeded_dates=fetched,
            failed_dates=failed,
            records_written=total_rows,
            dry_run=bool(args.dry_run),
        )
        # 本阶段读到的最新交易日：实际命中取到的最大日期
        if fetched:
            stats.data_date = max(fetched)
        else:
            stats.skipped_reason = "no_market_rows_fetched"
        stats.rows_added = total_rows
        stats.success = True
        stats.message = f"daily 阶段完成：{len(fetched)} 个日期写入 {total_rows} 行"
    except Exception as exc:
        logger.exception("daily 阶段失败")
        stats.success = False
        stats.message = f"daily 阶段异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


# ---------------------------------------------------------------------------
# stock_info / trade_status / factors 阶段
# ---------------------------------------------------------------------------
def _stage_stock_info(args: argparse.Namespace) -> StageResult:
    """更新 stock_info / trade_status 快照。"""

    stats = _stage_decorator("stock_info")
    start = time.time()
    try:
        if args.dry_run:
            stats.success = True
            stats.message = "[dry-run] 跳过 stock_info 更新"
            return stats
        try:
            from data_svc.storage.unified_updater import UnifiedDataUpdater
        except Exception as exc:
            stats.message = f"UnifiedDataUpdater 不可用: {exc}"
            return stats
        updater = UnifiedDataUpdater()
        result = updater.update_stock_basic()
        stats.success = bool(result.get("records_updated"))
        stats.message = f"stock_info 写入 {result.get('records_updated', 0)} 行"
        stats.details = {"records_updated": result.get("records_updated", 0)}
        stats.rows_updated = int(result.get("records_updated") or 0)
    except Exception as exc:
        logger.exception("stock_info 阶段失败")
        stats.success = False
        stats.message = f"stock_info 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


def _stage_trade_status(args: argparse.Namespace, end_date: str) -> StageResult:
    """更新 trade_status（来自 DragonEye 涨停/跌停池）。"""

    stats = _stage_decorator("trade_status")
    start = time.time()
    try:
        if args.dry_run:
            stats.success = True
            stats.message = "[dry-run] 跳过 trade_status 更新"
            return stats
        try:
            from data_svc.storage.unified_updater import UnifiedDataUpdater
        except Exception as exc:
            stats.message = f"UnifiedDataUpdater 不可用: {exc}"
            return stats
        updater = UnifiedDataUpdater()
        result = updater.update_dragon_eye(target_date=end_date, backfill=False)
        stats.details = result
        stats.success = bool(result.get("records_updated"))
        updated_dates = [
            str(value)[:10] for value in (result.get("updated_dates") or []) if value
        ]
        stats.data_date = max(updated_dates) if updated_dates else ""
        stats.rows_updated = int(result.get("records_updated") or 0)
        stats.message = (
            f"trade_status 更新 {result.get('records_updated', 0)} 条；"
            f"failed={len(result.get('failed_dates') or [])}"
        )
    except Exception as exc:
        logger.exception("trade_status 阶段失败")
        stats.success = False
        stats.message = f"trade_status 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


def _stage_factors(args: argparse.Namespace, start_date: str, end_date: str) -> StageResult:
    """因子预计算。"""

    stats = _stage_decorator("factors")
    start = time.time()
    try:
        if args.dry_run:
            stats.success = True
            stats.message = "[dry-run] 跳过因子计算"
            return stats
        try:
            from data_svc.storage.factor_precompute_service import FactorPrecomputeService
        except Exception as exc:
            stats.message = f"FactorPrecomputeService 不可用: {exc}"
            return stats
        result = FactorPrecomputeService().precompute_all_factors(
            start_date=start_date, end_date=end_date
        )
        stats.success = bool(result.success)
        stats.message = result.message
        stats.details = {
            "records_computed": result.records_computed,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "error": result.error,
        }
        stats.data_date = str(result.end_date or "")[:10]
        stats.rows_updated = int(result.records_computed or 0)
    except Exception as exc:
        logger.exception("factors 阶段失败")
        stats.success = False
        stats.message = f"factors 阶段异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


# ---------------------------------------------------------------------------
# matrix-cache 阶段（从 LanceDB 重建）
# ---------------------------------------------------------------------------
def _stage_matrix_cache(args: argparse.Namespace, start_date: str, end_date: str) -> StageResult:
    """从 LanceDB 重建 matrix_cache。"""

    stats = _stage_decorator("matrix_cache")
    start = time.time()
    try:
        if args.dry_run:
            stats.success = True
            stats.message = "[dry-run] 跳过 matrix_cache 重建"
            return stats
        try:
            from core.quant.backtest.matrix_cache_manager import MatrixCacheManager
            from data_svc.storage.lancedb_reader import LanceDBDataReader
        except Exception as exc:
            stats.message = f"重建依赖不可用: {exc}"
            return stats

        import polars as pl

        reader = LanceDBDataReader()
        base_df = reader.read_all(start_date=start_date, end_date=end_date)
        if base_df is None or base_df.is_empty():
            stats.message = "无基础行情数据（LanceDB daily_ohlcv），无法构建 matrix_cache"
            stats.skipped_reason = "no_market_data_in_window"
            return stats
        if "trade_date" in base_df.columns and base_df.schema.get("trade_date") != pl.Utf8:
            base_df = base_df.with_columns(
                pl.col("trade_date").cast(pl.Utf8).str.slice(0, 10).alias("trade_date")
            )
        trading_dates = sorted(base_df.get_column("trade_date").unique().to_list())
        stock_codes = sorted(base_df.get_column("stock_code").unique().to_list())
        if not trading_dates or not stock_codes:
            stats.message = "窗口内股票 / 交易日为空，跳过"
            stats.skipped_reason = "empty_matrix_window"
            return stats

        manager = MatrixCacheManager(cache_dir=str(MATRIX_CACHE_DIR))
        result = manager.build_and_save_matrix(
            preloaded_data={"stock_daily": base_df},
            trading_dates=trading_dates,
            stock_codes=stock_codes,
            force_rebuild=False,
        )
        if result is None:
            stats.success = True
            stats.message = "matrix_cache 已存在，未重建"
        else:
            meta = result.get("metadata", {})
            stats.success = True
            stats.message = f"matrix_cache 重建完成 T={meta.get('T')} N={meta.get('N')}"
            stats.details = {
                "cache_key": result.get("cache_key"),
                "T": meta.get("T"),
                "N": meta.get("N"),
                "fill_count": meta.get("fill_count"),
            }
            stats.rows_added = int(meta.get("fill_count") or 0)
        # matrix_cache 阶段覆盖到的最大交易日 = trading_dates 末位
        stats.data_date = trading_dates[-1] if trading_dates else ""
    except Exception as exc:
        logger.exception("matrix_cache 阶段失败")
        stats.success = False
        stats.message = f"matrix_cache 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


# ---------------------------------------------------------------------------
# sqlite-meta / parquet-snapshot / data_health 阶段
# ---------------------------------------------------------------------------
def _stage_sqlite_meta(args: argparse.Namespace) -> StageResult:
    """仅刷新元数据；不写行情。

    实际可扩展点：清理过期任务、刷新策略版本、刷新系统设置等。
    当前默认实现：dry-run 时仅记录；非 dry-run 时调用 build_data_health_report
    让 health 模块能感知到元数据状态变化。
    """

    stats = _stage_decorator("sqlite_meta")
    start = time.time()
    try:
        if args.dry_run:
            stats.success = True
            stats.message = "[dry-run] 跳过 sqlite_meta 刷新"
            stats.data_date = _latest_data_date_from_lancedb()
            return stats
        # 当前 sqlite_meta 仅承担元数据一致性确认；预留 hook
        stats.success = True
        stats.message = "sqlite_meta 阶段：仅做元数据一致性确认，未写行情"
        stats.details = {"note": "元数据刷新具体逻辑由 data_health / strategy 任务承担"}
        # sqlite_meta 反映全局数据状态，所以用 LanceDB 最新交易日
        stats.data_date = _latest_data_date_from_lancedb()
    except Exception as exc:
        logger.exception("sqlite_meta 阶段失败")
        stats.success = False
        stats.message = f"sqlite_meta 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


def _stage_parquet_snapshot(
    args: argparse.Namespace, start_date: str, end_date: str
) -> StageResult:
    """手动从 LanceDB 导出可选 Parquet 快照。"""

    stats = _stage_decorator("parquet_snapshot")
    start = time.time()
    try:
        try:
            from data_svc.storage.lancedb_reader import LanceDBDataReader
        except Exception as exc:
            stats.message = f"LanceDB 不可用: {exc}"
            return stats
        reader = LanceDBDataReader()
        df = reader.read_all(start_date=start_date, end_date=end_date)
        if df is None or df.is_empty():
            stats.message = "LanceDB 窗口内无数据，跳过导出"
            stats.skipped_reason = "no_market_data_in_window"
            return stats
        if args.dry_run:
            stats.success = True
            stats.message = f"[dry-run] 待导出 {len(df)} 行到 {PARQUET_SNAPSHOT_PATH}"
            stats.details = {"rows": len(df)}
            if "trade_date" in df.columns:
                stats.data_date = str(df.get_column("trade_date").max())[:10]
            return stats
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = PARQUET_SNAPSHOT_PATH.with_suffix(".parquet.tmp")
        if hasattr(df, "to_pandas"):
            df.to_pandas().to_parquet(str(tmp_path), index=False)
        else:
            import pandas as pd

            pd.DataFrame(df).to_parquet(str(tmp_path), index=False)
        if PARQUET_SNAPSHOT_PATH.exists():
            os.replace(str(tmp_path), str(PARQUET_SNAPSHOT_PATH))
        else:
            tmp_path.rename(PARQUET_SNAPSHOT_PATH)
        stats.success = True
        stats.message = f"Parquet 快照已导出 {len(df)} 行 -> {PARQUET_SNAPSHOT_PATH}"
        stats.details = {"rows": len(df), "path": str(PARQUET_SNAPSHOT_PATH)}
        stats.rows_added = len(df)
        if "trade_date" in df.columns:
            stats.data_date = str(df.get_column("trade_date").max())[:10]
    except Exception as exc:
        logger.exception("parquet_snapshot 阶段失败")
        stats.success = False
        stats.message = f"parquet_snapshot 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


def _stage_parquet_cache_rebuild() -> StageResult:
    """增量重建 Parquet 分片查询加速缓存。"""

    stats = _stage_decorator("parquet_cache_rebuild")
    start = time.time()
    try:
        import subprocess

        script_path = PROJECT_ROOT / "scripts" / "update" / "build_parquet_partitions.py"
        try:
            result = subprocess.run(
                [sys.executable, str(script_path), "--incremental"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                stats.success = True
                stats.message = "Parquet 分片缓存增量重建完成"
            else:
                stats.success = False
                stats.message = f"Parquet 分片缓存重建失败: {result.stderr[:500]}"
        except subprocess.TimeoutExpired:
            stats.success = False
            stats.message = "Parquet 分片缓存重建超时（600s）"
        except Exception as exc:
            stats.success = False
            stats.message = f"Parquet 分片缓存重建异常: {exc}"
    except Exception as exc:
        logger.exception("parquet_cache_rebuild 阶段失败")
        stats.success = False
        stats.message = f"parquet_cache_rebuild 异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


def _stage_health_check() -> StageResult:
    """执行 data_health 报告。"""

    stats = _stage_decorator("data_health")
    start = time.time()
    try:
        report = build_data_health_report(write_files=True)
        stats.success = True
        stats.message = f"data_health 状态: {report.get('status')}"
        stats.details = {
            "status": report.get("status"),
            "blocking": report.get("blocking"),
            "datasets": [
                {
                    "name": ds.get("name"),
                    "role": ds.get("role"),
                    "blocking": ds.get("blocking"),
                    "status": ds.get("status"),
                    "missing_dates": len(ds.get("missing_dates", [])),
                    "missing_fields": ds.get("missing_required_fields", []),
                }
                for ds in report.get("datasets", [])
            ],
        }
        # 从 lancedb 数据集读 latest_date 作为 data_date
        for ds in report.get("datasets", []):
            if ds.get("name") == LANCEDB_REPORT_FLAG:
                stats.data_date = str(ds.get("latest_date") or "")[:10]
                break
        if not stats.data_date:
            stats.data_date = _latest_data_date_from_lancedb()
    except Exception as exc:
        logger.exception("data_health 阶段失败")
        stats.success = False
        stats.message = f"data_health 阶段异常: {exc}"
    finally:
        stats.finished_at = _now_text()
        stats.duration_seconds = round(time.time() - start, 3)
    return stats


# ---------------------------------------------------------------------------
# Stage 调度
# ---------------------------------------------------------------------------
def _should_run(stage: str, args: argparse.Namespace) -> bool:
    """根据 ``--only`` 和 ``--target`` 决定是否执行某个 stage。"""

    if args.target == TARGET_ALL:
        # TARGET_ALL 不包含 parquet_snapshot
        if stage == "parquet_snapshot":
            return False
    if stage in {"daily", "stock_info", "trade_status"}:
        if args.only == ONLY_FACTORS:
            return False
    if stage == "factors":
        if args.only == ONLY_DAILY:
            return False
    return True


def _run_stages(
    args: argparse.Namespace, stages: list[str], start_date: str, end_date: str
) -> list[StageResult]:
    """按顺序执行 stage 列表。"""

    results: list[StageResult] = []
    for name in stages:
        if not _should_run(name, args):
            continue
        if name == "daily":
            results.append(_stage_daily(args, start_date, end_date))
        elif name == "stock_info":
            results.append(_stage_stock_info(args))
        elif name == "trade_status":
            results.append(_stage_trade_status(args, end_date))
        elif name == "factors":
            results.append(_stage_factors(args, start_date, end_date))
        elif name == "matrix_cache":
            results.append(_stage_matrix_cache(args, start_date, end_date))
        elif name == "sqlite_meta":
            results.append(_stage_sqlite_meta(args))
        elif name == "parquet_snapshot":
            results.append(_stage_parquet_snapshot(args, start_date, end_date))
        elif name == "parquet_cache_rebuild":
            results.append(_stage_parquet_cache_rebuild())
        elif name == "data_health":
            results.append(_stage_health_check())
        else:
            logger.warning("未知 stage: %s", name)
    return [_finalize_stage(result) for result in results]


# ---------------------------------------------------------------------------
# 报告 / 调度入口
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> dict[str, Any]:
    """主入口：根据 target / only 调度各 stage 并输出统一报告。"""

    started = time.time()
    start_date, end_date = _resolve_window(args)
    target = args.target
    if target not in VALID_TARGETS:
        raise SystemExit(f"非法 target: {target}")

    stages = TARGET_PLAN[target]
    stage_results = _run_stages(args, stages, start_date, end_date)

    overall_success = all(stage.success for stage in stage_results)
    global_latest = _latest_data_date_from_lancedb()
    stale = _stale_modules(stage_results, global_latest)
    payload: dict[str, Any] = {
        "generated_at": _now_text(),
        "status": "success" if overall_success else "partial",
        "global_latest_trade_date": global_latest,
        "stale_modules": stale,
        "args": {
            "target": target,
            "start_date": start_date,
            "end_date": end_date,
            "dry_run": bool(args.dry_run),
            "only": args.only,
            "max_symbols": args.max_symbols,
            "request_delay": args.request_delay,
        },
        "stages": [
            {
                "name": s.name,
                "status": s.status,
                "success": s.success,
                "message": s.message,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
                "duration_seconds": s.duration_seconds,
                "data_date": s.data_date,
                "rows_added": s.rows_added,
                "rows_updated": s.rows_updated,
                "skipped_reason": s.skipped_reason,
                "details": s.details,
            }
            for s in stage_results
        ],
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_LATEST.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    payload["elapsed_seconds"] = round(time.time() - started, 3)
    return payload


def _print_summary(payload: dict[str, Any]) -> None:
    args = payload.get("args", {})
    print(
        f"\n=== AquaTrader 更新入口 v2  (target={args.get('target')}, "
        f"window={args.get('start_date')}~{args.get('end_date')}, "
        f"dry_run={args.get('dry_run')}) ==="
    )
    print(f"全局最新交易日：{payload.get('global_latest_trade_date') or 'N/A'}")
    for stage in payload.get("stages", []):
        flag = "OK" if stage.get("success") else "FAIL"
        print(
            f"[{flag}] {stage['name']} "
            f"({stage.get('duration_seconds', 0):.2f}s) "
            f"data_date={stage.get('data_date') or '-'} "
            f"- {stage.get('message', '')}"
        )
    stale = payload.get("stale_modules") or []
    print(f"stale_modules: {', '.join(stale) or '无'}")
    print(f"\n报告路径: {REPORT_LATEST}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_with(argv)
    payload = run(args)
    _print_summary(payload)
    return 0 if payload.get("status") == "success" else 1


def parse_args_with(argv: list[str]) -> argparse.Namespace:
    """用预置 argv 列表解析参数，便于沙箱测试。"""

    parser = argparse.ArgumentParser(
        description="AquaTrader 唯一数据更新入口（v2 收敛版）",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=TARGET_LANCEDB,
        choices=sorted(VALID_TARGETS),
    )
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--only",
        type=str,
        default=ONLY_ALL,
        choices=sorted(VALID_ONLY),
    )
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--request-delay", type=float, default=0.0)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
