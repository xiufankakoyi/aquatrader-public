"""Seven-stage local research workflow.

The pipeline only uses local structured evidence. It does not place orders and
does not manufacture conclusions when a source is missing.

每个阶段都会输出 ``data_date``，代表本阶段读到的最新交易日。流水线入口会在
所有 stage 跑完之后，把每个 stage 的 ``data_date`` 与全局
``global_latest_trade_date``（从 LanceDB 推算的"今天"）做对比，差异的 stage
会从 ``ok`` 降级为 ``warning``，并在 ``final_brief.data.stale_modules`` 中列出。
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORT_DIR = PROJECT_ROOT / "data" / "reports"
PARQUET_DIR = PROJECT_ROOT / "data" / "parquet_data"
LANCEDB_REPORT_FLAG = "lancedb"  # data_health 报告里 lancedb 数据集的 name


@dataclass
class StageResult:
    stage: str
    status: str
    summary: str
    data: Any
    source: str
    reason: str
    errors: list[str]
    data_date: str = ""
    stale_reason: str = ""
    candidate_count: int | None = None
    signal_count: int | None = None


# 单个 stage 最多参与信号扫描的股票数，避免无界扫描
DEFAULT_SIGNAL_SCAN_LIMIT = 200
# 信号扫描的最小历史回看天数，少于这个天数的股票会被跳过
SIGNAL_HISTORY_LOOKBACK_DAYS = 250
RESEARCH_CANDIDATE_LIMIT = 100
CORE_INDEX_SYMBOLS = ("000300.SH", "000905.SH", "000001.SH")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _signal_value(signal: Any, name: str, default: Any = "") -> Any:
    if isinstance(signal, Mapping):
        return signal.get(name, default)
    return getattr(signal, name, default)


def _safe_get_lancedb_latest() -> str:
    """从 LanceDB 读最新交易日，失败时返回空串。"""

    try:
        from data_svc.query.fast_market_query import get_fast_market_query
        return get_fast_market_query().get_latest_trade_date() or ""
    except Exception:
        return ""


def _safe_get_lancedb_latest_from_health() -> str:
    """从 data_health_latest.json 里取 lancedb 数据集的最新日期。"""

    path = REPORT_DIR / "data_health_latest.json"
    if not path.exists():
        return ""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(report, Mapping):
        return ""
    for item in report.get("datasets", []):
        if isinstance(item, Mapping) and item.get("name") == LANCEDB_REPORT_FLAG:
            value = item.get("latest_date") or ""
            return str(value)[:10]
    return ""


def _resolve_global_latest_trade_date() -> str:
    """优先读 LanceDB 真值；失败再回退到 data_health 报告。"""

    return _safe_get_lancedb_latest() or _safe_get_lancedb_latest_from_health()


def _read_lancedb_table(
    table_name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    fields: list[str] | None = None,
):
    """读取 LanceDB 表；调用方必须根据返回内容决定真实 data_date。"""

    import polars as pl

    try:
        from data_svc.query.fast_market_query import get_fast_market_query

        fmq = get_fast_market_query()
        return fmq._read_table(table_name, start_date=start_date, end_date=end_date, fields=fields)
    except Exception:
        return pl.DataFrame()


def _latest_frame_date(df: Any) -> str:
    if df is None or getattr(df, "is_empty", lambda: True)():
        return ""
    if "trade_date" not in df.columns:
        return ""
    value = df.get_column("trade_date").max()
    return str(value)[:10] if value is not None else ""


def _is_stale(stage: StageResult, global_latest: str) -> bool:
    """``data_date`` 缺失或早于全局最新日期时为 stale。"""

    if not global_latest:
        return False
    if not stage.data_date:
        return False
    if stage.status in {"error", "skipped"}:
        return False  # 已经坏了，stale 标记意义不大
    return stage.data_date < global_latest


def _downgrade_for_staleness(
    stage: StageResult, global_latest: str
) -> StageResult:
    """stale stage 降级为 warning，并补充说明。"""

    if not _is_stale(stage, global_latest):
        return stage
    if stage.status == "ok":
        stage.status = "warning"
        stage.summary = f"{stage.summary}（stale: {stage.data_date} < {global_latest}）"
    stage.stale_reason = f"data_date {stage.data_date} 早于 LanceDB {global_latest}"
    return stage


class QuantFlowPipeline:
    stage_names = [
        "data_health_check",
        "market_regime_detect",
        "dragon_eye_summary",
        "stock_screener_candidates",
        "strategy_signal_scan",
        "portfolio_risk_check",
        "final_research_brief",
    ]

    def __init__(self) -> None:
        self.results: dict[str, StageResult] = {}
        self.global_latest_trade_date: str = ""

    def _run_stage(
        self, name: str, handler: Callable[[], StageResult]
    ) -> StageResult:
        try:
            result = handler()
        except Exception as exc:
            result = StageResult(
                name,
                "error",
                "阶段执行失败",
                {},
                "local_structured_data",
                str(exc),
                [str(exc)],
            )
        # stale 判定在所有 stage 跑完后做（需要 global_latest_trade_date）
        self.results[name] = result
        return result

    def data_health_check(self) -> StageResult:
        from server.services.data_health_service import build_data_health_report

        report = build_data_health_report(write_files=True)
        data_date = ""
        for item in report.get("datasets", []):
            if isinstance(item, Mapping) and item.get("name") == LANCEDB_REPORT_FLAG:
                data_date = str(item.get("latest_date") or "")[:10]
                break
        if not data_date:
            data_date = _safe_get_lancedb_latest()
        return StageResult(
            "data_health_check",
            "ok" if report["status"] == "ok" else "warning",
            f"数据健康状态：{report['status']}",
            report,
            "local_structured_data",
            report["message"],
            [],
            data_date=data_date,
        )

    def market_regime_detect(self) -> StageResult:
        import polars as pl

        target_date = self.global_latest_trade_date or _safe_get_lancedb_latest()
        if not target_date:
            return StageResult(
                "market_regime_detect", "warning", "暂无本地证据", {},
                "local_lancedb", "无法确定 LanceDB 最新交易日", [],
            )

        start_date = (
            datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=60)
        ).strftime("%Y-%m-%d")
        index_df = _read_lancedb_table(
            "index_daily",
            start_date,
            target_date,
            fields=[
                "trade_date", "symbol", "name", "close", "amount",
            ],
        )
        factor_df = _read_lancedb_table(
            "factors",
            target_date,
            target_date,
            fields=["trade_date", "stock_code", "ret_1d"],
        )
        limit_up_df = _read_lancedb_table(
            "dragon_eye_limit_up",
            target_date,
            target_date,
            fields=["trade_date", "stock_code"],
        )

        component_dates = {
            "index_daily": _latest_frame_date(index_df),
            "factors": _latest_frame_date(factor_df),
            "dragon_eye_limit_up": _latest_frame_date(limit_up_df),
        }
        # data_date 取 index_daily 为基准；factors 缺失时不拉低 data_date
        # 因为 breadth 可从 daily_ohlcv 兜底计算
        required_dates = [component_dates["index_daily"]]
        if component_dates["factors"]:
            required_dates.append(component_dates["factors"])
        data_date = min(required_dates) if required_dates else ""
        errors: list[str] = []

        index_trends: list[dict[str, Any]] = []
        if not index_df.is_empty() and {"symbol", "close"}.issubset(index_df.columns):
            for symbol in CORE_INDEX_SYMBOLS:
                rows = (
                    index_df.filter(pl.col("symbol") == symbol)
                    .drop_nulls("close")
                    .sort("trade_date")
                )
                if rows.is_empty():
                    continue
                item: dict[str, Any] = {
                    "symbol": symbol,
                    "name": rows.get_column("name")[-1] if "name" in rows.columns else symbol,
                    "data_date": str(rows.get_column("trade_date")[-1])[:10],
                }
                for days in (5, 10, 20):
                    key = f"return_{days}d"
                    if len(rows) > days:
                        start_close = float(rows.get_column("close")[-(days + 1)])
                        end_close = float(rows.get_column("close")[-1])
                        item[key] = (
                            end_close / start_close - 1.0 if start_close else None
                        )
                    else:
                        item[key] = None
                index_trends.append(item)
        if not index_trends:
            errors.append("index_daily 缺少可计算的核心指数历史")

        breadth: dict[str, Any] = {}
        if not factor_df.is_empty() and "ret_1d" in factor_df.columns:
            valid = factor_df.filter(pl.col("ret_1d").is_not_null())
            breadth = {
                "rise_count": valid.filter(pl.col("ret_1d") > 0).height,
                "fall_count": valid.filter(pl.col("ret_1d") < 0).height,
                "flat_count": valid.filter(pl.col("ret_1d") == 0).height,
                "sample_count": len(valid),
                "source": "lancedb.factors.ret_1d",
            }
        else:
            # 兜底：从 daily_ohlcv 计算 ret_1d
            ohlcv_df = _read_lancedb_table(
                "daily_ohlcv",
                start_date,
                target_date,
                fields=["trade_date", "stock_code", "close"],
            )
            if not ohlcv_df.is_empty() and "close" in ohlcv_df.columns:
                ohlcv_df = ohlcv_df.sort(["stock_code", "trade_date"])
                # 统一 trade_date 为 string 类型，避免 date vs string 比较报错
                if ohlcv_df["trade_date"].dtype != pl.Utf8:
                    ohlcv_df = ohlcv_df.with_columns(
                        pl.col("trade_date").cast(pl.Utf8)
                    )
                ohlcv_df = ohlcv_df.with_columns(
                    pl.col("close").pct_change(1).over("stock_code").alias("ret_1d")
                )
                today_rows = ohlcv_df.filter(pl.col("trade_date").str.slice(0, 10) == target_date)
                valid = today_rows.filter(pl.col("ret_1d").is_not_null())
                if not valid.is_empty():
                    breadth = {
                        "rise_count": valid.filter(pl.col("ret_1d") > 0).height,
                        "fall_count": valid.filter(pl.col("ret_1d") < 0).height,
                        "flat_count": valid.filter(pl.col("ret_1d") == 0).height,
                        "sample_count": len(valid),
                        "source": "lancedb.daily_ohlcv.close_pct_change(兜底)",
                    }
            if not breadth:
                errors.append("factors 缺少 ret_1d，且 daily_ohlcv 兜底计算失败，无法计算涨跌家数")

        turnover: dict[str, Any] = {}
        if not index_df.is_empty() and {"symbol", "amount"}.issubset(index_df.columns):
            benchmark_rows = (
                index_df.filter(pl.col("symbol") == "000300.SH")
                .drop_nulls("amount")
                .sort("trade_date")
            )
            if len(benchmark_rows) >= 2:
                latest_amount = float(benchmark_rows.get_column("amount")[-1])
                previous_amount = float(benchmark_rows.get_column("amount")[-2])
                previous_change = (
                    latest_amount / previous_amount - 1.0 if previous_amount else None
                )
                prior_values = benchmark_rows.get_column("amount")[:-1].tail(5)
                prior_average = (
                    float(prior_values.mean()) if len(prior_values) else None
                )
                turnover = {
                    "benchmark": "000300.SH",
                    "latest_amount": latest_amount,
                    "previous_change": previous_change,
                    "vs_prior_5d_average": (
                        latest_amount / prior_average - 1.0
                        if prior_average else None
                    ),
                    "source": "lancedb.index_daily.amount",
                }
            else:
                errors.append("index_daily 成交额历史不足")

        limit_down_count = None
        limit_down_source = "unavailable"
        if not factor_df.is_empty() and "ret_1d" in factor_df.columns:
            limit_down_count = factor_df.filter(pl.col("ret_1d") <= -0.095).height
            limit_down_source = "lancedb.factors.ret_1d<=-9.5%（推算）"
        elif breadth and breadth.get("source", "").startswith("lancedb.daily_ohlcv"):
            # 兜底：从 ohlcv 计算的 ret_1d 推算跌停数
            limit_down_count = valid.filter(pl.col("ret_1d") <= -0.095).height if not valid.is_empty() else None
            limit_down_source = "lancedb.daily_ohlcv.ret_1d<=-9.5%（兜底推算）" if limit_down_count is not None else "unavailable"
        limits = {
            "limit_up_count": len(limit_up_df) if not limit_up_df.is_empty() else None,
            "limit_up_source": (
                "lancedb.dragon_eye_limit_up"
                if not limit_up_df.is_empty() else "unavailable"
            ),
            "limit_down_count": limit_down_count,
            "limit_down_source": limit_down_source,
        }

        benchmark = next(
            (item for item in index_trends if item["symbol"] == "000300.SH"),
            None,
        )
        rise_ratio = (
            breadth.get("rise_count", 0) / breadth["sample_count"]
            if breadth.get("sample_count") else None
        )
        risk_reasons: list[str] = []
        if benchmark and benchmark.get("return_20d") is not None:
            if benchmark["return_20d"] <= -0.05:
                risk_reasons.append("沪深300近20日跌幅不低于5%")
        if rise_ratio is not None and rise_ratio < 0.35:
            risk_reasons.append("上涨家数占比低于35%")
        if limit_down_count is not None and limit_down_count >= 50:
            risk_reasons.append("推算跌停数量不低于50")
        risk_state = "风险偏高" if risk_reasons else (
            "风险常态" if benchmark and rise_ratio is not None else "数据不足"
        )

        benchmark_20d = benchmark.get("return_20d") if benchmark else None
        if benchmark_20d is None:
            label = "市场状态数据不足"
        elif benchmark_20d > 0.03:
            label = "指数近20日偏强"
        elif benchmark_20d < -0.03:
            label = "指数近20日偏弱"
        else:
            label = "指数近20日震荡"
        status = "warning" if errors or data_date != target_date else "ok"
        return StageResult(
            "market_regime_detect",
            status,
            f"{label}，{risk_state}",
            {
                "target_date": target_date,
                "component_dates": component_dates,
                "index_trends": index_trends,
                "breadth": breadth,
                "turnover": turnover,
                "limits": limits,
                "risk_state": risk_state,
                "risk_reasons": risk_reasons,
                "regime": label,
            },
            "local_lancedb",
            (
                "基于 LanceDB 指数、因子和涨停证据客观计算；"
                "跌停数量为 ret_1d 阈值推算，不构成投资建议"
            ),
            errors,
            data_date=data_date,
        )

    def dragon_eye_summary(self) -> StageResult:
        base = PROJECT_ROOT / "data" / "spider_data" / "dragon_eye" / "data_lake"
        date_dirs = sorted((item for item in base.glob("*") if item.is_dir()), reverse=True)
        if not date_dirs:
            return StageResult(
                "dragon_eye_summary", "skipped", "暂无本地证据", {},
                "local_spider", "未找到 DragonEye 日期目录", [],
            )
        from data_svc.ingestion.dragon_eye_adapter import DragonEyeAdapter

        adapter = DragonEyeAdapter()
        latest_partial_date = ""
        latest_partial_status: dict[str, Any] = {}
        latest_complete = None
        for date_dir in date_dirs:
            status = adapter.inspect_local_date(date_dir.name)
            if not latest_partial_date and (
                status.get("has_ladder")
                or status.get("has_limit_up")
                or status.get("has_sentiment")
            ):
                latest_partial_date = date_dir.name
                latest_partial_status = status
            if status.get("complete"):
                latest_complete = date_dir
                break
        if latest_complete is None:
            # 没有完整证据，但允许返回局部证据 + 缺失清单
            missing_parts = latest_partial_status.get("missing_parts") or []
            if not latest_partial_status:
                return StageResult(
                    "dragon_eye_summary", "skipped", "暂无本地证据", {},
                    "local_spider", "未找到 DragonEye 日期目录", [],
                )
            target_date = self.global_latest_trade_date or _safe_get_lancedb_latest()
            summary_text = (
                "DragonEye 只有梯队数据"
                if (latest_partial_status.get("has_ladder")
                    or latest_partial_status.get("has_limit_up"))
                and not latest_partial_status.get("has_sentiment")
                else "DragonEye 局部证据可用"
            )
            summary_text = (
                summary_text
                + f"（缺 {', '.join(missing_parts) or '未知'}），"
                "不能生成完整情绪判断。"
            )
            return StageResult(
                "dragon_eye_summary",
                "warning",
                summary_text,
                {
                    "date": latest_partial_date,
                    "complete_evidence_date": "",
                    "latest_partial_date": latest_partial_date,
                    "latest_partial_status": latest_partial_status,
                    "evidence": {},
                    "update_command": (
                        "python scripts/update/update_dragon_eye_evidence.py "
                        f"--target-date {target_date}"
                    ) if target_date else "",
                    # 任务一要求的完整性字段
                    "has_ladder": latest_partial_status.get("has_ladder", False),
                    "has_limit_up": latest_partial_status.get("has_limit_up", False),
                    "has_sentiment": latest_partial_status.get("has_sentiment", False),
                    "has_theme_flow": latest_partial_status.get("has_theme_flow", False),
                    "evidence_date": latest_partial_date,
                    "completeness_score": latest_partial_status.get("completeness_score", 0.0),
                    "missing_parts": missing_parts,
                    "partial_success": True,
                },
                "local_spider",
                (
                    "DragonEye 完整证据要求同时具备龙头和情绪数据；"
                    "当前仅有局部证据，已在 evidence_only 模式降级"
                ),
                [f"missing_components: {', '.join(missing_parts) or 'unknown'}"],
                data_date=latest_partial_date,
            )
        evidence: dict[str, Any] = {}
        errors: list[str] = []
        for name in (
            "market_sentiment_cycle", "sector_heat_stats", "risk_monitor_list",
        ):
            path = latest_complete / f"{name}.json"
            if not path.exists():
                continue
            try:
                evidence[name] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{latest_complete.name}/{path.name}: {exc}")
        target_date = self.global_latest_trade_date or _safe_get_lancedb_latest()
        complete_status = adapter.inspect_local_date(latest_complete.name)
        incomplete_latest = bool(
            target_date
            and latest_partial_date == target_date
            and not latest_partial_status.get("complete")
        )
        # final summary：若目标日仅有局部证据，要明确告警而不是伪装成完整
        if incomplete_latest:
            summary_text = (
                f"DragonEye 完整证据日期 {latest_complete.name}，"
                f"{latest_partial_date} 仅有局部证据"
                + (
                    f"（缺 {', '.join(latest_partial_status.get('missing_parts', []))}）"
                    if latest_partial_status.get("missing_parts") else ""
                )
            )
        else:
            summary_text = f"DragonEye 完整证据日期 {latest_complete.name}"
        return StageResult(
            "dragon_eye_summary",
            "warning" if errors or incomplete_latest else "ok",
            summary_text,
            {
                "date": latest_complete.name,
                "complete_evidence_date": latest_complete.name,
                "latest_partial_date": latest_partial_date,
                "latest_partial_status": latest_partial_status,
                "evidence": evidence,
                "update_command": (
                    "python scripts/update/update_dragon_eye_evidence.py "
                    f"--target-date {target_date}"
                ) if target_date else "",
                # 任务一要求的完整性字段
                "has_ladder": complete_status.get("has_ladder", False),
                "has_limit_up": complete_status.get("has_limit_up", False),
                "has_sentiment": complete_status.get("has_sentiment", False),
                "has_theme_flow": complete_status.get("has_theme_flow", False),
                "evidence_date": complete_status.get("evidence_date", latest_complete.name),
                "completeness_score": complete_status.get("completeness_score", 1.0),
                "missing_parts": complete_status.get("missing_parts", []),
                "partial_success": False,
            },
            "local_spider",
            (
                "完整证据要求同时具备龙头和情绪数据；"
                "局部目录不会覆盖完整证据日期"
            ),
            errors,
            data_date=latest_complete.name,
        )

    def stock_screener_candidates(self) -> StageResult:
        import polars as pl

        target_date = self.global_latest_trade_date or _safe_get_lancedb_latest()
        if not target_date:
            return StageResult(
                "stock_screener_candidates", "warning", "暂无本地证据",
                {"candidates": [], "no_candidates": True},
                "local_lancedb_factors", "无法确定最新交易日", [],
                candidate_count=0,
            )
        factor_fields = [
            "trade_date", "stock_code", "ma5", "ma10", "ma20", "rsi_12",
            "return_5d", "return_20d", "volatility_20d", "ret_1d",
        ]
        factor_df = _read_lancedb_table(
            "factors", target_date, target_date, fields=factor_fields
        )
        factor_date = _latest_frame_date(factor_df)
        if factor_df.is_empty():
            return StageResult(
                "stock_screener_candidates", "warning", "暂无最新因子候选",
                {
                    "candidates": [],
                    "no_candidates": True,
                    "candidate_source": "lancedb.factors",
                    "filter_rules": [],
                },
                "local_lancedb_factors",
                f"factors 在 {target_date} 无可用数据",
                [],
                candidate_count=0,
            )
        required = [
            "ma5", "ma10", "ma20", "rsi_12", "return_5d",
            "return_20d", "volatility_20d",
        ]
        missing = [name for name in required if name not in factor_df.columns]
        if missing:
            return StageResult(
                "stock_screener_candidates", "warning", "因子字段不足",
                {
                    "candidates": [],
                    "no_candidates": True,
                    "candidate_source": "lancedb.factors",
                    "missing_fields": missing,
                },
                "local_lancedb_factors",
                f"缺少筛选字段: {', '.join(missing)}",
                [],
                data_date=factor_date,
                candidate_count=0,
            )

        valid = factor_df.filter(
            pl.all_horizontal([pl.col(name).is_not_null() for name in required])
        )
        volatility_limit = (
            float(valid.get_column("volatility_20d").quantile(0.7))
            if not valid.is_empty() else None
        )
        filter_rules = [
            "ma5 >= ma10 >= ma20",
            "return_5d > 0",
            "return_20d > 0",
            "45 <= rsi_12 <= 75",
            "volatility_20d <= 当日有效样本70%分位",
        ]
        if volatility_limit is None:
            filtered = valid.head(0)
        else:
            filtered = valid.filter(
                (pl.col("ma5") >= pl.col("ma10"))
                & (pl.col("ma10") >= pl.col("ma20"))
                & (pl.col("return_5d") > 0)
                & (pl.col("return_20d") > 0)
                & pl.col("rsi_12").is_between(45, 75, closed="both")
                & (pl.col("volatility_20d") <= volatility_limit)
            )
        total_count = len(filtered)

        name_map: dict[str, str] = {}
        stock_info = _read_lancedb_table(
            "stock_info", fields=["stock_code", "name"]
        )
        if not stock_info.is_empty() and {"stock_code", "name"}.issubset(stock_info.columns):
            name_map = {
                str(item["stock_code"]): str(item["name"] or "")
                for item in stock_info.select(["stock_code", "name"]).to_dicts()
            }
        display_df = filtered.sort(
            ["return_20d", "stock_code"], descending=[True, False]
        ).head(RESEARCH_CANDIDATE_LIMIT)
        candidates = []
        for row in display_df.to_dicts():
            symbol = str(row.get("stock_code") or "")
            candidates.append(
                {
                    "symbol": symbol,
                    "name": name_map.get(symbol, ""),
                    "return_5d": row.get("return_5d"),
                    "return_20d": row.get("return_20d"),
                    "rsi_12": row.get("rsi_12"),
                    "volatility_20d": row.get("volatility_20d"),
                }
            )
        no_candidates = total_count == 0
        return StageResult(
            "stock_screener_candidates",
            "warning" if no_candidates or factor_date != target_date else "ok",
            "no_candidates" if no_candidates else f"最新因子研究候选 {total_count} 条",
            {
                "candidates": candidates,
                "no_candidates": no_candidates,
                "candidate_count": total_count,
                "returned_count": len(candidates),
                "candidate_source": "lancedb.factors",
                "filter_rules": filter_rules,
                "volatility_70th_percentile": volatility_limit,
                "data_date": factor_date,
            },
            "local_lancedb_factors",
            "候选仅为透明因子规则的研究样本，不构成投资建议",
            [],
            data_date=factor_date,
            candidate_count=total_count,
        )

    # ------------------------------------------------------------------
    # 策略信号扫描：从"warning 占位"升级为真实可执行的扫描器
    # ------------------------------------------------------------------
    def strategy_signal_scan(self) -> StageResult:
        """扫描已启用策略，输出 buy / sell / watch + reason + blocked_reason。

        诊断输出 (即使无信号也必须解释)：
        - total_scanned
        - buy_count / sell_count / watch_count / no_signal_count
        - top_blocked_reasons
        - per_symbol_diagnosis：每只股票触发的 blocked_reason 类别
        """

        latest = self.global_latest_trade_date or _safe_get_lancedb_latest()
        if not latest:
            return StageResult(
                "strategy_signal_scan",
                "skipped",
                "暂无本地证据",
                {"buy": [], "sell": [], "watch": [], "blocked_reason": "无法定位最新交易日"},
                "local_signals",
                "LanceDB 缺少最新交易日，无法定位信号扫描目标日",
                [],
                signal_count=0,
            )

        # 1. 读取已启用策略列表（从 StrategyFactory 自动发现，缺省全部启用）
        # TODO(P2-边界): research 不应直接依赖 core.quant，需通过 application/ 编排层注入。
        enabled_strategies: list[dict[str, Any]] = []
        try:
            from core.quant.strategies.strategy_factory import get_factory
            factory = get_factory()
            enabled_strategies = factory.list_strategies() or []
        except Exception as exc:
            return StageResult(
                "strategy_signal_scan",
                "warning",
                "策略工厂不可用，回退到规则引擎",
                {
                    "buy": [], "sell": [], "watch": [],
                    "enabled_strategies": [],
                    "blocked_reason": f"strategy_factory_unavailable: {exc}",
                    "diagnosis": {
                        "total_scanned": 0,
                        "buy_count": 0, "sell_count": 0, "watch_count": 0,
                        "no_signal_count": 0,
                        "top_blocked_reasons": [
                            {"reason": "strategy_factory_unavailable", "count": 0}
                        ],
                    },
                },
                "local_signals",
                "已尝试启用 SignalEngine 默认规则",
                [str(exc)],
                stale_reason="策略扫描未开始，无可确认的实际扫描日期",
                signal_count=0,
            )

        # 2. 准备扫描股票池：优先自选研究池，否则取 LanceDB 全部股票前 N 只
        symbols = self._resolve_scan_symbols(limit=DEFAULT_SIGNAL_SCAN_LIMIT)
        if not symbols:
            return StageResult(
                "strategy_signal_scan",
                "warning",
                "无可扫描股票",
                {
                    "buy": [], "sell": [], "watch": [],
                    "enabled_strategies": enabled_strategies,
                    "blocked_reason": "no_scan_universe",
                    "diagnosis": {
                        "total_scanned": 0,
                        "buy_count": 0, "sell_count": 0, "watch_count": 0,
                        "no_signal_count": 0,
                        "top_blocked_reasons": [
                            {"reason": "no_scan_universe", "count": 0}
                        ],
                    },
                },
                "local_signals",
                "watchlist 与 LanceDB 股票池均为空",
                [],
                stale_reason="策略扫描未开始，无可确认的实际扫描日期",
                signal_count=0,
            )

        # 3. 跑 SignalEngine（统一规则引擎；配置化策略按其 enabled 字段过滤）
        # TODO(P2-边界): research 不应直接依赖 core.quant，需通过 application/ 编排层注入。
        try:
            from core.quant.portfolio.signal_engine import SignalEngine
            engine = SignalEngine()
        except Exception as exc:
            return StageResult(
                "strategy_signal_scan",
                "warning",
                "SignalEngine 不可用",
                {
                    "buy": [], "sell": [], "watch": [],
                    "enabled_strategies": enabled_strategies,
                    "blocked_reason": f"signal_engine_unavailable: {exc}",
                    "diagnosis": {
                        "total_scanned": 0,
                        "buy_count": 0, "sell_count": 0, "watch_count": 0,
                        "no_signal_count": 0,
                        "top_blocked_reasons": [
                            {"reason": "signal_engine_unavailable", "count": 0}
                        ],
                    },
                },
                "local_signals",
                "无法加载信号引擎",
                [str(exc)],
                stale_reason="策略扫描未开始，无可确认的实际扫描日期",
                signal_count=0,
            )

        # 4. 收集 per-symbol 诊断：每只股票扫描前的预检分类
        #    - insufficient_data: 拉不到 30 天数据
        #    - stale_data: 数据最新日期远早于目标日
        #    - risk_blocked: 风险规则命中（占位）
        #    - strategy_disabled: 已启用策略数为 0
        per_symbol_diagnosis: list[dict[str, Any]] = []
        blocked_counter: dict[str, int] = {}
        risk_blocked: set[str] = set()
        for symbol in symbols:
            try:
                data = engine.get_stock_data(symbol, end_date=latest)
            except Exception as exc:
                per_symbol_diagnosis.append({
                    "symbol": symbol,
                    "blocked_reason": "engine_error",
                    "detail": str(exc)[:120],
                })
                blocked_counter["engine_error"] = blocked_counter.get("engine_error", 0) + 1
                continue
            payload = data if isinstance(data, dict) else {}
            close = payload.get("close", None)
            trade_dates = payload.get("trade_date", None)
            close_len = 0
            try:
                if close is not None:
                    close_len = int(len(close))
            except TypeError:
                close_len = 0
            if close_len < 30:
                per_symbol_diagnosis.append({
                    "symbol": symbol,
                    "blocked_reason": "insufficient_data",
                    "detail": f"close 序列长度 {close_len}",
                })
                blocked_counter["insufficient_data"] = blocked_counter.get("insufficient_data", 0) + 1
                continue
            try:
                latest_trade_date = (
                    str(trade_dates[-1])[:10] if trade_dates is not None and len(trade_dates) else ""
                )
            except TypeError:
                latest_trade_date = ""
            if latest_trade_date and latest_trade_date.replace("-", "") < latest.replace("-", ""):
                per_symbol_diagnosis.append({
                    "symbol": symbol,
                    "blocked_reason": "stale_data",
                    "detail": f"data latest={latest_trade_date}, target={latest}",
                })
                blocked_counter["stale_data"] = blocked_counter.get("stale_data", 0) + 1
                continue
            per_symbol_diagnosis.append({
                "symbol": symbol,
                "blocked_reason": "",
                "detail": "",
            })

        # 5. 跑实际扫描
        try:
            raw_signals = engine.generate_signals(symbols, signal_date=latest)
            raw_signals = raw_signals if isinstance(raw_signals, Mapping) else {}
        except Exception as exc:
            return StageResult(
                "strategy_signal_scan",
                "warning",
                "信号扫描异常",
                {
                    "buy": [], "sell": [], "watch": [],
                    "enabled_strategies": enabled_strategies,
                    "blocked_reason": f"generate_signals_failed: {exc}",
                    "diagnosis": {
                        "total_scanned": len(symbols),
                        "buy_count": 0, "sell_count": 0, "watch_count": 0,
                        "no_signal_count": len(symbols),
                        "top_blocked_reasons": sorted(
                            [{"reason": k, "count": v} for k, v in blocked_counter.items()],
                            key=lambda item: -item["count"],
                        ),
                        "per_symbol_diagnosis": per_symbol_diagnosis[:50],
                    },
                },
                "local_signals",
                "扫描器抛出异常，已降级为不输出信号",
                [str(exc)],
                stale_reason="策略扫描未完成，无可确认的实际扫描日期",
                signal_count=0,
            )

        # 6. 统一格式：每个信号带 reason（来自 Signal.details，否则用 signal_name）
        buy: list[dict[str, Any]] = []
        sell: list[dict[str, Any]] = []
        watch: list[dict[str, Any]] = []
        symbols_with_signal: set[str] = set()
        for bucket, target in (
            ("buy", buy),
            ("sell", sell),
            ("watch", watch),
        ):
            signals = raw_signals.get(bucket, [])
            if not isinstance(signals, (list, tuple)):
                signals = []
            for signal in signals:
                item = self._signal_to_dict(signal)
                target.append(item)
                symbols_with_signal.add(str(item.get("stock_code") or ""))

        # 7. 把"无信号"的诊断补全：未命中规则的标的归为 rule_not_matched
        for entry in per_symbol_diagnosis:
            if entry["blocked_reason"]:
                continue
            symbol = entry["symbol"]
            if symbol in symbols_with_signal:
                continue
            entry["blocked_reason"] = "rule_not_matched"
            blocked_counter["rule_not_matched"] = (
                blocked_counter.get("rule_not_matched", 0) + 1
            )

        # 8. 风险屏蔽（占位）：如果信号来自已禁用策略，标记为 strategy_disabled
        rule_engine = getattr(engine, "rules", {}) or {}
        if not isinstance(rule_engine, Mapping):
            rule_engine = {}

        def _disabled_count(group: str, side: str) -> int:
            section = rule_engine.get(group)
            configs = section.get(side) if isinstance(section, Mapping) else {}
            if not isinstance(configs, Mapping):
                return 0
            return sum(
                1 for cfg in configs.values()
                if isinstance(cfg, Mapping) and not cfg.get("enabled", False)
            )

        disabled_buy = _disabled_count("buy_signals", "right_side")
        disabled_sell = _disabled_count("sell_signals", "right_side")
        disabled_watch = _disabled_count("watch_signals", "left_side")
        disabled_rule_count = disabled_buy + disabled_sell + disabled_watch
        if disabled_rule_count and not (buy or sell or watch):
            blocked_counter["strategy_disabled"] = disabled_rule_count

        no_signal_count = max(0, len(symbols) - len(symbols_with_signal) - len(risk_blocked))
        top_blocked_reasons = sorted(
            [{"reason": k, "count": v} for k, v in blocked_counter.items() if v],
            key=lambda item: -item["count"],
        )
        if not top_blocked_reasons and not (buy or sell or watch):
            top_blocked_reasons = [
                {"reason": "rule_not_matched", "count": no_signal_count}
            ]

        blocked_reason = ""
        if not (buy or sell or watch):
            if top_blocked_reasons:
                top_reason = top_blocked_reasons[0]["reason"]
                blocked_reason = f"no_signals_today:{top_reason}"
            else:
                blocked_reason = "no_signals_today"

        return StageResult(
            "strategy_signal_scan",
            "ok" if (buy or sell or watch) else "warning",
            (
                f"已启用 {len(enabled_strategies)} 个策略，"
                f"扫描 {len(symbols)} 只股票："
                f"buy={len(buy)} sell={len(sell)} watch={len(watch)}"
            ),
            {
                "data_date": latest,
                "enabled_strategies": enabled_strategies,
                "scanned_symbols": symbols,
                "buy": buy,
                "sell": sell,
                "watch": watch,
                "blocked_reason": blocked_reason,
                "diagnosis": {
                    "total_scanned": len(symbols),
                    "buy_count": len(buy),
                    "sell_count": len(sell),
                    "watch_count": len(watch),
                    "no_signal_count": no_signal_count,
                    "top_blocked_reasons": top_blocked_reasons,
                    "disabled_rule_count": disabled_rule_count,
                    "per_symbol_diagnosis": per_symbol_diagnosis[:50],
                },
            },
            "local_signals",
            "信号来自 SignalEngine 默认规则与已注册策略；不构成投资建议",
            [],
            data_date=latest,
            signal_count=len(buy) + len(sell) + len(watch),
        )

    @staticmethod
    def _signal_to_dict(signal: Any) -> dict[str, Any]:
        """把 Signal dataclass 序列化成下游友好的 dict。"""

        reason = _signal_value(signal, "details") or _signal_value(signal, "signal_name")
        return {
            "stock_code": _signal_value(signal, "stock_code"),
            "stock_name": _signal_value(signal, "stock_name"),
            "signal_date": str(_signal_value(signal, "signal_date"))[:10],
            "signal_type": _signal_value(signal, "signal_type"),
            "signal_name": _signal_value(signal, "signal_name"),
            "signal_strength": _safe_float(_signal_value(signal, "signal_strength")),
            "price_at_signal": _safe_float(_signal_value(signal, "price_at_signal")),
            "reason": reason,
        }

    @staticmethod
    def _resolve_scan_symbols(limit: int) -> list[str]:
        """扫描股票池：watchlist 优先；否则从 LanceDB 取前 N 只。"""

        watchlist_path = PARQUET_DIR / "watchlist.parquet"
        if watchlist_path.exists():
            try:
                import duckdb
                escaped = str(watchlist_path).replace("'", "''")
                rows = duckdb.connect(database=":memory:").execute(
                    f"""
                    SELECT stock_code
                    FROM read_parquet('{escaped}')
                    WHERE COALESCE(is_active, true)
                    ORDER BY stock_code
                    LIMIT {limit}
                    """
                ).fetchall()
                codes = [str(row[0]) for row in rows if row and row[0]]
                if codes:
                    return codes
            except Exception:
                pass

        # 退路：LanceDB list_symbols
        try:
            from data_svc.storage.lancedb_reader import LanceDBDataReader
            reader = LanceDBDataReader()
            symbols = reader.list_symbols() or []
            return [
                str(symbol).strip()
                for symbol in symbols
                if symbol and str(symbol).strip()
            ][:limit]
        except Exception:
            return []

    def portfolio_risk_check(self) -> StageResult:
        path = PARQUET_DIR / "portfolio_positions.parquet"
        if not path.exists():
            return StageResult(
                "portfolio_risk_check", "ok", "demo_mode: 无本地持仓记录",
                {
                    "active_position_count": 0,
                    "symbols": [],
                    "valuation_date": "",
                    "valuation_complete": True,
                    "mode": "demo",
                    "schema": {"required_quantity_field": "quantity/shares"},
                },
                "local_portfolio", "demo_mode: 无本地持仓记录", [],
            )
        import duckdb
        import polars as pl

        escaped = str(path).replace("'", "''")
        connection = duckdb.connect(database=":memory:")
        columns = {
            str(row[0])
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
            ).fetchall()
        }
        # 动态选择存在列：避免在缺失字段时直接 SQL 报错
        select_columns = ["stock_code"]
        for optional in ("shares", "quantity", "buy_price", "cost"):
            if optional in columns:
                select_columns.append(optional)
        select_sql = ", ".join(select_columns)
        rows = connection.execute(
            f"""
            SELECT {select_sql}
            FROM read_parquet('{escaped}')
            WHERE COALESCE(is_active, true)
            """
        ).fetchall()
        position_records: list[dict[str, Any]] = []
        for item in rows:
            if not item or not item[0]:
                continue
            record: dict[str, Any] = {
                "stock_code": str(item[0] or ""),
                "shares": 0.0,
                "quantity": None,
                "buy_price": 0.0,
                "cost": 0.0,
            }
            for offset, field_name in enumerate(select_columns[1:], start=1):
                value = item[offset] if offset < len(item) else None
                if field_name == "quantity" and value is not None:
                    record[field_name] = float(value)
                elif field_name in ("shares", "buy_price", "cost") and value is not None:
                    record[field_name] = float(value)
            position_records.append(record)
        symbols = sorted({record["stock_code"] for record in position_records})
        if not symbols:
            return StageResult(
                "portfolio_risk_check", "ok", "demo_mode: 无本地持仓记录",
                {
                    "active_position_count": 0,
                    "symbols": [],
                    "valuation_date": "",
                    "valuation_complete": True,
                    "mode": "demo",
                    "schema": {"required_quantity_field": "quantity/shares"},
                },
                "local_portfolio", "demo_mode: 无本地持仓记录", [],
            )

        from infrastructure.data_providers.base import normalize_symbol

        normalized_symbols = [
            normalize_symbol(symbol)["symbol"] for symbol in symbols
        ]
        target_date = self.global_latest_trade_date or _safe_get_lancedb_latest()
        price_df = _read_lancedb_table(
            "daily_ohlcv",
            target_date,
            target_date,
            fields=["stock_code", "trade_date", "close"],
        )
        if not price_df.is_empty():
            price_df = price_df.filter(
                pl.col("stock_code").is_in(normalized_symbols)
            ).drop_nulls("close")
        price_marks = {
            str(row["stock_code"]): {
                "latest_price": float(row["close"]),
                "price_date": str(row["trade_date"])[:10],
            }
            for row in price_df.to_dicts()
        } if not price_df.is_empty() else {}
        marked_dates = sorted(
            {item["price_date"] for item in price_marks.values() if item["price_date"]}
        )
        valuation_date = (
            marked_dates[0]
            if len(price_marks) == len(normalized_symbols) and len(marked_dates) == 1
            else ""
        )
        missing_price_symbols = [
            symbol for symbol in normalized_symbols if symbol not in price_marks
        ]
        # Schema 标准化：quantity 优先，shares 兼容；两者都没有则视为缺字段
        quantity_fields = {"shares", "quantity"}
        has_quantity = bool(columns & quantity_fields)
        # 进一步按行判断：是不是每一条记录都有有效 quantity 值
        rows_with_quantity = 0
        for record in position_records:
            qty = record.get("quantity")
            if qty is not None and qty > 0:
                rows_with_quantity += 1
            elif record.get("shares", 0) > 0:
                rows_with_quantity += 1
        all_rows_have_quantity = rows_with_quantity == len(position_records)

        # 估值计算：market_value / unrealized_pnl / weight
        evaluated: list[dict[str, Any]] = []
        total_market_value = 0.0
        for record in position_records:
            normalized = normalize_symbol(record["stock_code"])["symbol"]
            mark = price_marks.get(normalized)
            qty = record.get("quantity")
            held_quantity = qty if (qty is not None and qty > 0) else record.get("shares", 0.0)
            item: dict[str, Any] = {
                "stock_code": record["stock_code"],
                "normalized_symbol": normalized,
                "quantity": held_quantity,
                "shares": record.get("shares", 0.0),
                "has_quantity_field": qty is not None and qty > 0,
                "buy_price": record.get("buy_price", 0.0),
                "cost": record.get("cost", 0.0),
            }
            if mark:
                latest_price = mark["latest_price"]
                market_value = latest_price * held_quantity
                cost = record.get("cost", 0.0) or 0.0
                unrealized_pnl = market_value - cost if cost > 0 else None
                item.update({
                    "latest_price": latest_price,
                    "price_date": mark["price_date"],
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "valuation_date": mark["price_date"],
                })
                total_market_value += market_value
            evaluated.append(item)
        for item in evaluated:
            mv = item.get("market_value")
            if mv and total_market_value > 0:
                item["weight"] = mv / total_market_value
            else:
                item["weight"] = None

        valuation_complete = bool(
            valuation_date and has_quantity and all_rows_have_quantity
        )
        errors = []
        if not has_quantity:
            errors.append("持仓文件缺少 shares/quantity，不能计算完整市值")
        elif not all_rows_have_quantity:
            errors.append("部分持仓行 quantity/shares 缺失，无法准确估值")
        if missing_price_symbols:
            errors.append(f"缺少最新价: {', '.join(missing_price_symbols)}")
        status = "ok" if valuation_complete else "warning"
        reason = (
            "已按 LanceDB 最新收盘价完成持仓估值"
            if valuation_complete
            else "；".join(errors) or "无法完成持仓估值"
        )
        return StageResult(
            "portfolio_risk_check",
            status,
            (
                f"持仓 {len(symbols)} 条，最新价标记日期 {valuation_date}"
                if valuation_date else f"持仓 {len(symbols)} 条，最新价估值不完整"
            ),
            {
                "active_position_count": len(symbols),
                "symbols": symbols,
                "normalized_symbols": normalized_symbols,
                "price_marks": price_marks,
                "valuation_date": valuation_date,
                "valuation_complete": valuation_complete,
                "missing_price_symbols": missing_price_symbols,
                "missing_position_fields": (
                    [] if has_quantity else ["shares_or_quantity"]
                ),
                "checks": ["字段完整性", "最新价可用性"],
                "schema": {
                    "primary_quantity_field": "quantity",
                    "legacy_compat_fields": ["shares"],
                    "required_columns": ["stock_code", "quantity"],
                    "optional_columns": ["shares", "buy_price", "cost", "buy_date"],
                },
                "positions": evaluated,
                "summary": {
                    "position_count": len(evaluated),
                    "rows_with_quantity": rows_with_quantity,
                    "all_rows_have_quantity": all_rows_have_quantity,
                    "total_market_value": total_market_value,
                    "has_quantity_field": has_quantity,
                },
            },
            "local_portfolio+lancedb.daily_ohlcv",
            reason,
            errors,
            data_date=valuation_date,
        )

    def final_research_brief(self) -> StageResult:
        health = self.results.get("data_health_check")
        market = self.results.get("market_regime_detect")
        dragon = self.results.get("dragon_eye_summary")
        candidates_stage = self.results.get("stock_screener_candidates")
        signals_stage = self.results.get("strategy_signal_scan")
        portfolio_stage = self.results.get("portfolio_risk_check")
        dragon_data = dragon.data if dragon else {}
        latest_partial = dragon_data.get("latest_partial_status") or {}
        current_dragon = (
            latest_partial
            if latest_partial.get("evidence_date") == self.global_latest_trade_date
            else dragon_data
        )
        dragon_partial = bool(
            current_dragon.get("partial_success")
            or current_dragon.get("status") == "partial"
        )
        dragon_missing = list(current_dragon.get("missing_parts") or [])
        dragon_incomplete = (
            dragon_partial
            or dragon is None
            or dragon.status in {"warning", "error", "skipped"}
            or (
                self.global_latest_trade_date
                and current_dragon.get("evidence_date")
                and current_dragon.get("evidence_date") < self.global_latest_trade_date
            )
        )
        failed = [
            name
            for name, result in self.results.items()
            if result.status in {"error", "skipped"}
        ]
        stale_modules = [
            name
            for name, result in self.results.items()
            if _is_stale(result, self.global_latest_trade_date)
        ]
        incomplete = bool(
            failed or stale_modules or not health or health.status in {"warning", "error"}
            or dragon_incomplete
            or any(result.errors for result in self.results.values())
        )
        dragon_explanation = ""
        if dragon is None or dragon.status == "skipped":
            dragon_explanation = "DragonEye 暂无本地证据"
        elif dragon_incomplete and current_dragon:
            has_ladder = bool(
                current_dragon.get("has_ladder")
                or current_dragon.get("has_limit_up")
            )
            has_sentiment = bool(current_dragon.get("has_sentiment"))
            if has_ladder and not has_sentiment:
                dragon_explanation = (
                    "DragonEye 只有梯队数据，缺 sentiment，不能生成完整情绪判断。"
                )
            else:
                parts = dragon_missing or ["未知组件"]
                dragon_explanation = (
                    "DragonEye 局部证据可用（缺 "
                    + "、".join(parts)
                    + "），不能生成完整情绪判断。"
                )

        market_data = market.data if market else {}
        market_regime = str(market_data.get("regime") or "")
        market_risk_state = str(market_data.get("risk_state") or "")
        if not market or market.status in {"error", "skipped"}:
            market_strength_label = "unavailable"
        elif "偏强" in market_regime and market_risk_state != "风险偏高":
            market_strength_label = "偏强"
        elif "偏弱" in market_regime or market_risk_state == "风险偏高":
            market_strength_label = "偏弱"
        else:
            market_strength_label = "震荡"
        market_strength = {
            "label": market_strength_label,
            "summary": market.summary if market else "unavailable",
            "risk_state": market_risk_state or "unavailable",
            "risk_reasons": market_data.get("risk_reasons") or [],
            "data_date": market.data_date if market else "",
        }

        direction_rows: list[dict[str, Any]] = []
        current_evidence_date = str(current_dragon.get("evidence_date") or "")
        if (
            current_evidence_date == self.global_latest_trade_date
            and not dragon_incomplete
        ):
            raw_themes = (
                dragon_data.get("evidence", {})
                .get("sector_heat_stats", {})
                .get("data", [])
            )
            if isinstance(raw_themes, list):
                direction_rows = [
                    {"name": item.get("name"), "count": item.get("count")}
                    for item in raw_themes[:10]
                    if isinstance(item, dict) and item.get("name")
                ]
        strong_direction_result = {
            "status": "available" if direction_rows else "unavailable",
            "evidence_date": current_evidence_date or "unavailable",
            "theme_flow": current_dragon.get("theme_flow") or "unavailable",
            "directions": direction_rows,
            "reason": (
                "按本地 sector_heat_stats 客观排序"
                if direction_rows else dragon_explanation or "暂无本地证据"
            ),
        }

        candidate_data = candidates_stage.data if candidates_stage else {}
        candidate_rows = (
            candidate_data.get("candidates", [])
            if isinstance(candidate_data, dict) else []
        )
        candidate_conclusion = {
            "status": (
                "available"
                if candidate_rows
                else "empty" if candidates_stage and candidates_stage.status != "error"
                else "unavailable"
            ),
            "count": (
                candidate_data.get("candidate_count", len(candidate_rows))
                if isinstance(candidate_data, dict) else len(candidate_rows)
            ),
            "candidates": candidate_rows[:20],
            "reason": candidates_stage.summary if candidates_stage else "unavailable",
            "data_date": candidates_stage.data_date if candidates_stage else "",
        }

        signals = signals_stage.data if signals_stage else {}
        buy_count = len(signals.get("buy", []) or [])
        sell_count = len(signals.get("sell", []) or [])
        watch_count = len(signals.get("watch", []) or [])
        total_signal_count = buy_count + sell_count + watch_count
        signal_available = bool(
            signals_stage and signals_stage.status not in {"error", "skipped"}
        )
        has_signal = signal_available and total_signal_count > 0
        no_signal_reason = ""
        if not has_signal:
            top_reasons = (
                signals.get("diagnosis", {}).get("top_blocked_reasons") or []
            )
            no_signal_reason = (
                signals.get("blocked_reason")
                or (top_reasons[0].get("reason") if top_reasons else "")
                or (signals_stage.reason if signals_stage else "")
                or "unavailable"
            )
        signal_conclusion = {
            "status": (
                "有信号" if has_signal
                else "无信号" if signal_available
                else "unavailable"
            ),
            "has_signal": has_signal if signal_available else None,
            "buy_count": buy_count if signal_available else None,
            "sell_count": sell_count if signal_available else None,
            "watch_count": watch_count if signal_available else None,
            "no_signal_reason": no_signal_reason or "不适用",
            "scan_status": signals_stage.status if signals_stage else "unavailable",
            "data_date": signals_stage.data_date if signals_stage else "",
        }

        portfolio = portfolio_stage.data if portfolio_stage else {}
        active_positions = portfolio.get("active_position_count")
        valuation_complete = portfolio.get("valuation_complete")
        if not portfolio_stage or portfolio_stage.status in {"error", "skipped"}:
            portfolio_label = "unavailable"
            has_portfolio_risk = None
        elif active_positions == 0:
            portfolio_label = "无本地持仓记录"
            has_portfolio_risk = False
        elif valuation_complete is False or portfolio_stage.errors:
            portfolio_label = "存在持仓数据风险"
            has_portfolio_risk = True
        else:
            portfolio_label = "当前检查项未发现持仓风险"
            has_portfolio_risk = False
        portfolio_risk_conclusion = {
            "status": portfolio_label,
            "has_risk": has_portfolio_risk,
            "active_position_count": active_positions,
            "valuation_complete": valuation_complete,
            "risk_reasons": portfolio_stage.errors if portfolio_stage else [],
            "data_date": portfolio_stage.data_date if portfolio_stage else "",
            "details": portfolio,
        }

        data_gaps: list[dict[str, str]] = []
        for name in failed:
            result = self.results.get(name)
            data_gaps.append({
                "module": name,
                "reason": result.reason if result else "阶段失败或跳过",
            })
        for name in stale_modules:
            result = self.results.get(name)
            data_gaps.append({
                "module": name,
                "reason": result.stale_reason if result else "数据已过期",
            })
        for name, result in self.results.items():
            if result.status == "warning":
                for error in result.errors:
                    data_gaps.append({"module": name, "reason": error})
        for part in dragon_missing:
            data_gaps.append({
                "module": "dragon_eye_summary",
                "reason": f"缺少 {part}",
            })
        unique_gaps: list[dict[str, str]] = []
        seen_gaps: set[tuple[str, str]] = set()
        for gap in data_gaps:
            key = (gap["module"], gap["reason"])
            if key not in seen_gaps:
                seen_gaps.add(key)
                unique_gaps.append(gap)

        direction_text = (
            "、".join(item["name"] for item in direction_rows)
            if direction_rows else "unavailable"
        )
        summary = (
            f"市场强弱：{market_strength_label}；"
            f"强势方向：{direction_text}；"
            f"研究候选：{len(candidate_rows)} 条；"
            f"策略信号：{signal_conclusion['status']}"
        )
        if signal_conclusion["status"] != "有信号":
            summary += f"（{signal_conclusion['no_signal_reason']}）"
        summary += (
            f"；持仓风险：{portfolio_label}；"
            f"数据缺口：{len(unique_gaps)} 项。"
        )
        if incomplete:
            summary += " 仅输出研究事实，不能据此下结论。"
        else:
            summary += " 结果仅用于研究复核，不构成投资建议。"

        if dragon_incomplete:
            research_output_level = "EVIDENCE_ONLY"
        elif incomplete:
            research_output_level = "EVIDENCE_ONLY"
        else:
            research_output_level = "RESEARCH_REVIEW_ONLY"

        # 6 级质量等级判定
        if failed:
            research_output_level = "DATA_MISSING"
        elif not health or health.status in {"error"}:
            research_output_level = "DATA_MISSING"
        elif health.status in {"warning"} and stale_modules:
            research_output_level = "DATA_STALE"

        # Evidence Center 收集
        from core.research.evidence import EvidenceCenter
        ec = EvidenceCenter()
        ec.collect_from_dragon_eye(current_dragon, self.global_latest_trade_date or "")
        brief_data_for_ec = {
            "market_strength_conclusion": market_strength,
            "signal_conclusion": signal_conclusion,
        }
        ec.collect_from_quant_flow(brief_data_for_ec, self.global_latest_trade_date or "")
        # v2: 扩容采集
        ec.collect_from_stock_screener(candidate_data, self.global_latest_trade_date or "")
        ec.collect_from_strategy_signals(signals, self.global_latest_trade_date or "")
        market_stage = self.results.get("market_regime_detect")
        ec.collect_from_market_regime(
            market_stage.data if market_stage else {},
            self.global_latest_trade_date or "",
        )
        portfolio_stage = self.results.get("portfolio_risk_check")
        ec.collect_from_portfolio_risk(
            portfolio_stage.data if portfolio_stage else {},
            self.global_latest_trade_date or "",
        )
        ec.collect_from_industry_chain_if_available(self.global_latest_trade_date or "")
        ec.collect_from_finance_radar_if_available(self.global_latest_trade_date or "")
        stale_ev_count = ec.mark_stale(self.global_latest_trade_date or "")
        ec_export = ec.export()
        evidence_count = ec_export["total"]
        stale_evidence_count = ec_export["stale"]

        # level_reason / blocking_gaps / warning_gaps
        blocking_gaps: list[str] = []
        warning_gaps: list[str] = []
        level_reason = ""

        if failed:
            blocking_gaps.extend(f"{name}: 阶段失败" for name in failed)
            level_reason = f"存在失败阶段: {', '.join(failed)}"
        elif stale_modules:
            blocking_gaps.extend(f"{name}: 数据过期" for name in stale_modules)
            level_reason = f"存在过期模块: {', '.join(stale_modules)}"
        elif dragon_incomplete:
            blocking_gaps.append("DragonEye 证据不完整")
            if current_dragon.get("evidence_date") and self.global_latest_trade_date:
                if current_dragon.get("evidence_date") < self.global_latest_trade_date:
                    level_reason = f"DragonEye 证据日期({current_dragon.get('evidence_date')})落后于全局日期({self.global_latest_trade_date})"
                else:
                    level_reason = f"DragonEye 证据不完整: 缺 {', '.join(dragon_missing) if dragon_missing else '未知组件'}"
            else:
                level_reason = "DragonEye 证据不完整"
        elif incomplete:
            level_reason = "存在数据缺口，仅输出研究事实"
        else:
            level_reason = "所有阶段数据完整，结果仅用于研究复核"

        for gap in unique_gaps:
            if gap.get("module") in failed or gap.get("module") in stale_modules:
                continue  # 已在 blocking_gaps
            warning_gaps.append(f"{gap.get('module')}: {gap.get('reason')}")

        return StageResult(
            "final_research_brief",
            "warning" if incomplete else "ok",
            summary,
            {
                "data_health": self.results.get("data_health_check").summary
                if self.results.get("data_health_check") else "未运行",
                "global_latest_trade_date": self.global_latest_trade_date,
                "market_regime": self.results.get("market_regime_detect").summary
                if self.results.get("market_regime_detect") else "未运行",
                "strong_directions": dragon_data,
                "research_candidates": candidate_data,
                "strategy_signals": signals,
                "portfolio_risk": portfolio,
                "market_strength_conclusion": market_strength,
                "strong_direction_conclusion": strong_direction_result,
                "candidate_stock_conclusion": candidate_conclusion,
                "signal_conclusion": signal_conclusion,
                "portfolio_risk_conclusion": portfolio_risk_conclusion,
                "data_gap_conclusion": {
                    "has_gaps": bool(unique_gaps),
                    "count": len(unique_gaps),
                    "items": unique_gaps,
                    "can_conclude": not bool(unique_gaps),
                },
                "operational_note": summary,
                "research_output_level": research_output_level,
                "level_reason": level_reason,
                "blocking_gaps": blocking_gaps,
                "warning_gaps": warning_gaps,
                "evidence_count": evidence_count,
                "stale_evidence_count": stale_evidence_count,
                "stale_modules": stale_modules,
                "failed_or_skipped_modules": failed,
                "dragon_eye_explanation": dragon_explanation,
                "dragon_eye_partial": dragon_partial,
                "dragon_eye_missing_parts": dragon_missing,
            },
            "quant_flow_pipeline",
            "汇总前序阶段，不添加外部或随机结论",
            [],
        )

    def run(self, write_files: bool = True) -> dict[str, Any]:
        started_at = datetime.now().astimezone()
        handlers = [
            self.data_health_check,
            self.market_regime_detect,
            self.dragon_eye_summary,
            self.stock_screener_candidates,
            self.strategy_signal_scan,
            self.portfolio_risk_check,
            self.final_research_brief,
        ]
        # 1) 解析全局最新交易日
        self.global_latest_trade_date = _resolve_global_latest_trade_date()
        # 2) 跑前置 6 个 stage（final_brief 必须最后跑）
        for name, handler in zip(self.stage_names[:-1], handlers[:-1]):
            self._run_stage(name, handler)
        # 3) stale 降级
        for name, result in self.results.items():
            _downgrade_for_staleness(result, self.global_latest_trade_date)
        # 4) 跑 final_brief
        self._run_stage("final_research_brief", self.final_research_brief)
        report = {
            "run_at": started_at.isoformat(timespec="seconds"),
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "local_structured_data",
            "status": "error" if any(
                item.status == "error" for item in self.results.values()
            ) else (
                "warning" if any(
                    item.status in {"warning", "skipped"}
                    for item in self.results.values()
                ) else "ok"
            ),
            "global_latest_trade_date": self.global_latest_trade_date,
            "stale_modules": self.results["final_research_brief"].data.get(
                "stale_modules", []
            ),
            "stages": [asdict(self.results[name]) for name in self.stage_names],
            "final_brief": asdict(self.results["final_research_brief"]),
        }
        if write_files:
            self.write_reports(report)
        return report

    @staticmethod
    def write_reports(report: dict[str, Any]) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "quant_flow_latest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            "# QuantFlow 五分钟投研流水线报告",
            "",
            f"- 运行时间：{report['run_at']}",
            f"- 总体状态：`{report['status']}`",
            f"- 数据来源：`{report['source']}`",
            f"- 全局最新交易日：`{report.get('global_latest_trade_date') or 'N/A'}`",
            "",
            "## 阶段结果",
            "",
            "| 阶段 | 状态 | data_date | 摘要 | 原因 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for stage in report["stages"]:
            lines.append(
                f"| {stage['stage']} | {stage['status']} | "
                f"{stage.get('data_date') or '-'} | "
                f"{stage['summary']} | {stage['reason']} |"
            )
        final_data = report["final_brief"]["data"]
        signals = final_data.get("strategy_signals", {}) or {}
        diagnosis = signals.get("diagnosis", {}) or {}
        dragon_data = final_data.get("strong_directions", {}) or {}
        portfolio_data = final_data.get("portfolio_risk", {}) or {}
        lines.extend(
            [
                "",
                "## DragonEye 解释",
                "",
                f"- 完整证据日期：`{dragon_data.get('complete_evidence_date') or dragon_data.get('date') or '-'}`",
                f"- 完整证据日期 evidence_date：`{dragon_data.get('evidence_date') or '-'}`",
                f"- has_ladder：`{dragon_data.get('has_ladder')}`",
                f"- has_limit_up：`{dragon_data.get('has_limit_up')}`",
                f"- has_sentiment：`{dragon_data.get('has_sentiment')}`",
                f"- has_theme_flow：`{dragon_data.get('has_theme_flow')}`",
                f"- completeness_score：`{dragon_data.get('completeness_score')}`",
                f"- missing_parts：`{', '.join(dragon_data.get('missing_parts') or []) or '无'}`",
                f"- partial_success：`{dragon_data.get('partial_success')}`",
                f"- 解释：{final_data.get('dragon_eye_explanation') or '-'}",
                "",
                "## 最终简报",
                "",
                f"- 摘要：{report['final_brief'].get('summary', '')}",
                f"- 全局最新交易日：{final_data.get('global_latest_trade_date') or 'N/A'}",
                f"- 今天市场强不强：{json.dumps(final_data.get('market_strength_conclusion', {}), ensure_ascii=False)}",
                f"- 哪些方向强：{json.dumps(final_data.get('strong_direction_conclusion', {}), ensure_ascii=False)}",
                f"- 哪些候选股值得看：{json.dumps(final_data.get('candidate_stock_conclusion', {}), ensure_ascii=False)}",
                f"- 有没有策略信号：{json.dumps(final_data.get('signal_conclusion', {}), ensure_ascii=False)}",
                f"- 当前持仓有没有风险：{json.dumps(final_data.get('portfolio_risk_conclusion', {}), ensure_ascii=False)}",
                f"- 哪些数据不完整：{json.dumps(final_data.get('data_gap_conclusion', {}), ensure_ascii=False)}",
                f"- 数据健康：{final_data.get('data_health', '未运行')}",
                f"- 市场状态：{final_data.get('market_regime', '未运行')}",
                f"- 强势方向：{json.dumps(final_data.get('strong_directions', {}), ensure_ascii=False)}",
                f"- 研究候选：{json.dumps(final_data.get('research_candidates', []), ensure_ascii=False)}",
                f"- 策略信号 buy：{len(signals.get('buy', []) or [])} 条",
                f"- 策略信号 sell：{len(signals.get('sell', []) or [])} 条",
                f"- 策略信号 watch：{len(signals.get('watch', []) or [])} 条",
                f"- 策略信号 blocked_reason：{signals.get('blocked_reason') or '无'}",
                f"- 策略信号诊断：total_scanned={diagnosis.get('total_scanned')}, "
                f"buy={diagnosis.get('buy_count')}, sell={diagnosis.get('sell_count')}, "
                f"watch={diagnosis.get('watch_count')}, no_signal={diagnosis.get('no_signal_count')}",
                f"- 无信号原因 TOP：{json.dumps(diagnosis.get('top_blocked_reasons') or [], ensure_ascii=False)}",
                f"- 持仓风险：{json.dumps(final_data.get('portfolio_risk', {}), ensure_ascii=False)}",
                f"- 持仓 schema：{json.dumps(portfolio_data.get('schema') or {}, ensure_ascii=False)}",
                f"- 持仓总市值：{portfolio_data.get('summary', {}).get('total_market_value') if isinstance(portfolio_data.get('summary'), dict) else '-'}",
                f"- 今日操作说明：{final_data.get('operational_note', '')}",
                f"- research_output_level：{final_data.get('research_output_level')}",
                f"- 失败或跳过模块：{', '.join(final_data.get('failed_or_skipped_modules', [])) or '无'}",
                f"- stale_modules：{', '.join(final_data.get('stale_modules', [])) or '无'}",
            ]
        )
        (REPORT_DIR / "quant_flow_latest.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    print(
        json.dumps(
            QuantFlowPipeline().run(write_files=True),
            ensure_ascii=False,
            indent=2,
        )
    )
