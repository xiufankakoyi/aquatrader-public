"""Production premarket, intraday and close research monitoring runtime.

Outputs research states only (READY/WATCH/REJECTED/UNKNOWN).  It never emits
orders, positions, entry prices, exits, profit targets or stop losses.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from core.quant.research_levels import calculate_technical_context
from core.runtime.research_digest import (
    build_short_recap,
    compare_previous_mentions,
    summarize_board_fund_flow,
    summarize_external_markets,
    load_external_market_snapshot,
)
# P3.8 observability 门面 import；子模块按需惰性使用，不改变现有运行时逻辑
from core.runtime.observability import (  # noqa: F401  保留门面导出便于外部调用
    TraceContext,
    get_global_collector,
    get_structured_logger,
    get_trace_id,
    mask_sensitive,
)
from infrastructure.data_providers.market.tdx_provider import _tdx_market

logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class RuntimeResult:
    mode: str
    status: str
    run_id: str
    as_of: str
    report_path: str = ""
    json_path: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "run_id": self.run_id,
            "as_of": self.as_of,
            "report_path": self.report_path,
            "json_path": self.json_path,
            "payload": self.payload,
            "errors": self.errors,
            "exit_code": self.exit_code,
        }


class ResearchMonitor:
    VERSION = "research_monitor_v1"
    FAVORABLE_STATE_SCORES = {
        "MA10_RETEST": 1.00,
        "MA20_RETEST": 0.95,
        "MA30_RETEST": 0.90,
        "MA5_HOLD": 0.85,
        "REVERSAL_CONFIRMING": 0.80,
        "TREND_PULLBACK": 0.78,
        "REACCELERATION": 0.70,
        "TRENDING": 0.65,
        "NEUTRAL": 0.40,
        "ACCELERATION_RISK": 0.10,
        "TREND_BROKEN": 0.00,
    }

    def __init__(
        self,
        *,
        output_dir: str | Path | None = None,
        research_repository: Any | None = None,
        data_query: Any | None = None,
        theme_engine: Any | None = None,
        theme_review_processor: Any | None = None,
        runtime_factory: Any | None = None,
        research_runtime_factory: Any | None = None,
        company_ir_resolver: Any | None = None,
        official_ir_urls: list[str] | None = None,
        company_ir_urls: list[str] | None = None,
        push_dedup_store: Any | None = None,
        market_session_machine: Any | None = None,
        quote_freshness_checker: Any | None = None,
        price_anomaly_detector: Any | None = None,
        quant_repository: Any | None = None,
        persist_quant_signals: bool = False,
    ) -> None:
        """初始化研究监控运行时。

        Args:
            output_dir: 输出目录。``None`` 时从环境变量
                ``RESEARCH_MONITOR_OUTPUT_DIR`` 读取，默认
                ``data/reports/research_monitor``。
            research_repository: 研究控制面存储，``None`` 时自动创建
                ``ResearchRepository()``（路径由 ``RESEARCH_CONTROL_DB_PATH``
                环境变量控制）。
            data_query: 统一数据查询。
            theme_engine: 主题引擎。
            theme_review_processor: 主题审查处理器。
            runtime_factory / research_runtime_factory: 研究任务运行时工厂。
            company_ir_resolver: 公司 IR 解析器。
            official_ir_urls / company_ir_urls: 官方 IR URL 列表。
            push_dedup_store: 推送去重存储。``None`` 时懒初始化。
            market_session_machine: 市场时段状态机。``None`` 时懒初始化。
            quote_freshness_checker: 行情新鲜度校验器。``None`` 时懒初始化。
            price_anomaly_detector: 价格异动检测器。``None`` 时懒初始化。
            quant_repository: QUANT 信号存储；测试可注入临时仓库。
            persist_quant_signals: 是否把主题研究信号写入 QUANT 生产库。
        """
        import os as _os
        if output_dir is None:
            output_dir = _os.getenv(
                "RESEARCH_MONITOR_OUTPUT_DIR",
                "data/reports/research_monitor",
            )
        self.output_dir = Path(output_dir)
        if research_repository is None:
            from core.research.control_plane.repository import ResearchRepository

            research_repository = ResearchRepository()
        if data_query is None:
            from data_svc.unified_data_query import UnifiedDataQuery

            data_query = UnifiedDataQuery()
        self.repository = research_repository
        self.data_query = data_query
        self._theme_engine = theme_engine
        self._theme_review_processor = theme_review_processor
        self._research_runtime_factory = runtime_factory or research_runtime_factory
        self._company_ir_resolver = company_ir_resolver
        self._official_ir_urls = list(official_ir_urls or [])
        self._company_ir_urls = list(company_ir_urls or [])
        self._quant_repository = quant_repository
        self._persist_quant_signals = bool(
            persist_quant_signals or quant_repository is not None
        )
        # P6 组件：支持注入，也支持懒初始化
        self._push_dedup_store = push_dedup_store
        self._market_session_machine = market_session_machine
        self._quote_freshness_checker = quote_freshness_checker
        self._price_anomaly_detector = price_anomaly_detector

    def run(
        self,
        *,
        mode: str,
        as_of: str = "",
        external_push: bool = False,
        fail_on_empty: bool = False,
        fail_on_degraded: bool = False,
        max_research_tasks: int = 6,
    ) -> RuntimeResult:
        effective_as_of = as_of or datetime.now(MARKET_TZ).isoformat(timespec="seconds")
        if mode == "premarket":
            return self.run_premarket(
                as_of=effective_as_of,
                external_push=external_push,
                fail_on_empty=fail_on_empty,
                fail_on_degraded=fail_on_degraded,
                max_research_tasks=max_research_tasks,
            )
        if mode == "monitor":
            result = self.run_monitor(as_of=effective_as_of, external_push=external_push)
            return self._apply_runtime_quality_gate(result, fail_on_empty, fail_on_degraded)
        if mode == "close":
            result = self.run_close(as_of=effective_as_of, external_push=external_push)
            return self._apply_runtime_quality_gate(result, fail_on_empty, fail_on_degraded)
        if mode == "tail_research":
            result = self.run_tail_research(
                as_of=effective_as_of, external_push=external_push
            )
            return self._apply_runtime_quality_gate(result, fail_on_empty, fail_on_degraded)
        if mode == "push":
            return self.push_latest_premarket(
                as_of=effective_as_of, external_push=external_push
            )
        return RuntimeResult(
            mode=mode,
            status="failed",
            run_id=self._run_id(mode, effective_as_of),
            as_of=effective_as_of,
            errors=[f"unsupported mode: {mode}"],
            exit_code=2,
        )

    @staticmethod
    def _apply_runtime_quality_gate(
        result: RuntimeResult, fail_on_empty: bool, fail_on_degraded: bool
    ) -> RuntimeResult:
        rows = (
            result.payload.get("observations")
            or result.payload.get("close_watchlist")
            or result.payload.get("snapshots")
            or []
        )
        quality = (result.payload.get("data_quality") or {}).get("status")
        if fail_on_empty and not rows:
            result.exit_code = 3
        elif fail_on_degraded and (result.status != "success" or quality in {"degraded", "insufficient"}):
            result.exit_code = 4
        return result

    def push_latest_premarket(
        self, *, as_of: str, external_push: bool
    ) -> RuntimeResult:
        """Push the exact same-day dry-run artifact without rerunning research."""
        run_id = self._run_id("push", as_of)
        latest = self._read_latest_premarket()
        if latest is None or str(latest.get("as_of") or "")[:10] != as_of[:10]:
            return RuntimeResult(
                mode="push", status="failed", run_id=run_id, as_of=as_of,
                errors=["same-day premarket dry-run artifact missing"], exit_code=1,
            )
        report_path = self.output_dir / "latest_premarket.md"
        push_result = self._maybe_push(
            external_push=external_push,
            trade_date=as_of[:10],
            title=f"AquaTrade 盘前研究复盘 {as_of[:10]}",
            summary=(
                f"动态主题 {len(latest.get('themes') or [])} 个，Research 已验证 "
                f"{latest.get('validated_observation_count', 0)} 个，READY/WATCH "
                f"{latest.get('ready_watch_count', 0)} 个。"
            ),
            report_path=report_path,
            payload=latest,
        )
        status = "success" if push_result.get("sent") else "partial"
        if not external_push:
            status = "skipped"
        payload = {
            "schema_version": self.VERSION,
            "mode": "push",
            "status": status,
            "run_id": run_id,
            "as_of": as_of,
            "source_premarket_run_id": latest.get("run_id", ""),
            "push": push_result,
        }
        return RuntimeResult(
            mode="push", status=status, run_id=run_id, as_of=as_of,
            report_path=str(report_path), payload=payload,
            exit_code=0 if status in {"success", "skipped"} else 1,
        )

    def run_premarket(
        self,
        *,
        as_of: str,
        external_push: bool,
        fail_on_empty: bool,
        fail_on_degraded: bool,
        max_research_tasks: int,
    ) -> RuntimeResult:
        started = time.time()
        run_id = self._run_id("premarket", as_of)
        errors: list[str] = []
        calendar = self._resolve_calendar(as_of)
        pretrade_date = str(calendar.get("pretrade_date") or "")
        health = self._market_data_health(pretrade_date)
        if not health.get("usable"):
            errors.append(str(health.get("error") or "上一交易日日线不可用"))

        maintenance: dict[str, Any] = {}
        try:
            from core.research.control_plane.maintenance import (
                invalidate_ambiguous_entity_evidence,
            )

            maintenance = invalidate_ambiguous_entity_evidence(
                self.repository, as_of=as_of
            )
        except Exception as exc:
            errors.append(f"evidence maintenance failed: {exc}")

        runtime = None
        try:
            runtime = self._build_research_runtime(as_of=as_of)
            self._recover_stale_dynamic_tasks(
                as_of=as_of, runtime=runtime, errors=errors
            )
        except Exception as exc:
            errors.append(f"research task runtime init failed: {exc}")

        engine, reviewer = self._load_theme_components()
        packets: list[Any] = []
        if not errors:
            try:
                packets = engine.generate(
                    as_of=as_of,
                    trade_date=as_of[:10],
                    max_themes=10,
                )
            except Exception as exc:
                errors.append(f"dynamic theme scan failed: {exc}")

        initial_reviews = self._review_packets(reviewer, packets, as_of, errors)
        blocked_theme_ids = {
            item["packet"].theme_id
            for item in initial_reviews
            if item["summary"].get("research_gate_status") == "rejected"
        }
        active_theme_ids = {
            item["packet"].theme_id for item in initial_reviews
            if item["packet"].theme_id not in blocked_theme_ids
        }
        active_theme_priorities = {
            item["packet"].theme_id: self._theme_runtime_priority(item["packet"])
            for item in initial_reviews
            if item["packet"].theme_id in active_theme_ids
        }
        task_runs = self._execute_research_tasks(
            as_of=as_of,
            blocked_theme_ids=blocked_theme_ids,
            active_theme_ids=active_theme_ids,
            active_theme_priorities=active_theme_priorities,
            max_tasks=max_research_tasks,
            errors=errors,
            runtime=runtime,
        )
        final_reviews = self._review_packets(reviewer, packets, as_of, errors)
        enhanced_packets = [item["enhanced"] for item in final_reviews]

        daily_data_map = self._load_daily_data(
            enhanced_packets, end_date=pretrade_date, errors=errors
        )
        quant_result = self._run_theme_quant(
            enhanced_packets,
            daily_data_map=daily_data_map,
            as_of=as_of,
            errors=errors,
        )
        observations = self._build_observations(final_reviews, quant_result)
        validated_count = sum(
            1 for item in observations if item.get("research_validated")
        )
        ready_watch_count = sum(
            1 for item in observations
            if item.get("research_validated")
            and item.get("status") in {"READY", "WATCH"}
        )
        degraded = bool(
            errors
            or not packets
            or health.get("degraded")
            or any(item["enhanced"].degraded for item in final_reviews)
        )
        status = "failed" if errors and not packets else "partial" if degraded else "success"
        if not packets and not errors:
            status = "empty"

        payload = {
            "schema_version": self.VERSION,
            "mode": "premarket",
            "status": status,
            "run_id": run_id,
            "as_of": as_of,
            "elapsed_seconds": round(time.time() - started, 2),
            "calendar": calendar,
            "health": health,
            "maintenance": maintenance,
            "themes": [self._theme_payload(item) for item in final_reviews],
            "research_task_runs": task_runs,
            "quant": self._quant_payload(quant_result),
            "observations": observations,
            "validated_observation_count": validated_count,
            "ready_watch_count": ready_watch_count,
            "research_stats": self.repository.stats(),
            "errors": errors,
            "notice": "仅为研究观察状态，不构成买卖、仓位、止盈或止损建议。",
        }
        markdown = self._premarket_markdown(payload)
        json_path, report_path = self._write_outputs(
            as_of[:10], run_id, "premarket", payload, markdown
        )
        if status in {"success", "partial", "empty"} and not external_push:
            self._write_push_gate(as_of[:10], run_id, json_path, status)
        push_result = self._maybe_push(
            external_push=external_push,
            trade_date=as_of[:10],
            title=f"AquaTrade 盘前研究复盘 {as_of[:10]}",
            summary=(
                f"动态主题 {len(packets)} 个，Research 已验证观察 {validated_count} 个，"
                f"其中 READY/WATCH {ready_watch_count} 个，状态 {status}。"
            ),
            report_path=report_path,
            payload=payload,
        )
        payload["push"] = push_result
        self._rewrite_json(json_path, payload)

        exit_code = 1 if status == "failed" else 0
        if fail_on_empty and validated_count == 0:
            exit_code = 3
        if fail_on_degraded and degraded:
            exit_code = 4
        return RuntimeResult(
            mode="premarket",
            status=status,
            run_id=run_id,
            as_of=as_of,
            report_path=str(report_path),
            json_path=str(json_path),
            payload=payload,
            errors=errors,
            exit_code=exit_code,
        )

    def run_monitor(self, *, as_of: str, external_push: bool) -> RuntimeResult:
        run_id = self._run_id("monitor", as_of)
        recovered_tasks, recovery_errors = self._recover_dynamic_tasks_at_startup(
            as_of=as_of
        )
        latest = self._read_latest_premarket()
        if latest is None:
            return RuntimeResult(
                mode="monitor", status="failed", run_id=run_id, as_of=as_of,
                errors=["latest premarket result missing", *recovery_errors], exit_code=1,
            )
        source_as_of = str(latest.get("as_of") or "")
        if source_as_of[:10] != as_of[:10]:
            return RuntimeResult(
                mode="monitor", status="failed", run_id=run_id, as_of=as_of,
                errors=[
                    "latest premarket result is not from the current trade date: "
                    f"{source_as_of[:10] or 'unknown'}",
                    *recovery_errors,
                ],
                exit_code=1,
            )
        observations = list(latest.get("observations") or [])
        symbols = list(dict.fromkeys(
            str(item.get("symbol") or "") for item in observations if item.get("symbol")
        ))[:30]
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            quotes = ProviderRegistry().get_realtime_quotes(symbols)
        except Exception as exc:
            quotes = pd.DataFrame()
            error = str(exc)
        else:
            error = ""
        if quotes is None or quotes.empty:
            return RuntimeResult(
                mode="monitor", status="failed", run_id=run_id, as_of=as_of,
                errors=[error or "realtime quote failover returned empty", *recovery_errors],
                exit_code=1,
            )
        rows = quotes.to_dict(orient="records")
        observations_by_code: dict[str, list[dict[str, Any]]] = {}
        for item in observations:
            code = self._code(item.get("symbol"))
            if code:
                observations_by_code.setdefault(code, []).append(item)
        # P6: 解析当前市场时段
        market_session_value = self._resolve_market_session(as_of)
        try:
            now_dt = datetime.fromisoformat(as_of)
        except Exception:
            now_dt = datetime.now(MARKET_TZ)
        # P6: 价格异动检测器创建的 RetrievalHypothesis 列表（不直接升级优先级）
        anomaly_hypotheses: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        stale_quote_count = 0
        for row in rows:
            code = self._code(row.get("symbol") or row.get("stock_code"))
            linked_observations = observations_by_code.get(code, [])
            observation = sorted(
                linked_observations,
                key=lambda item: (
                    0 if item.get("research_validated") else 1,
                    {"READY": 0, "WATCH": 1, "UNKNOWN": 2, "REJECTED": 3}.get(
                        str(item.get("status") or ""), 4
                    ),
                ),
            )[0] if linked_observations else {}
            theme_names = list(dict.fromkeys(
                str(item.get("theme_name") or "")
                for item in linked_observations if item.get("theme_name")
            ))
            claim_ids = list(dict.fromkeys(
                str(claim_id)
                for item in linked_observations
                for claim_id in (item.get("claim_ids") or [])
                if claim_id
            ))
            market = observation.get("market_features") or {}
            price = self._float(row.get("price") or row.get("close"))
            ma20 = self._float(market.get("ma20"))
            ma5 = self._float(market.get("ma5"))
            market_timestamp = str(row.get("market_timestamp") or "")
            quote_trade_date = self._market_timestamp_date(market_timestamp)
            pct_chg = self._float(row.get("pct_chg"))
            # P6: 使用 QuoteFreshnessChecker 替换简单的日期比较
            quote_fresh = quote_trade_date == as_of[:10]
            freshness_result: dict[str, Any] = {"is_fresh": quote_fresh, "staleness_seconds": 0.0, "reason": "", "degraded": False}
            try:
                from core.runtime.market_session import MarketSession
                session_enum = MarketSession(market_session_value) if market_session_value != "unknown" else None
                if session_enum is not None:
                    fr = self.quote_freshness_checker.check(
                        exchange_timestamp=market_timestamp or None,
                        as_of_date=quote_trade_date or None,
                        session=session_enum,
                        now=now_dt,
                    )
                    quote_fresh = fr.is_fresh
                    freshness_result = fr.to_dict()
            except Exception as exc:
                logger.debug("quote freshness check failed symbol=%s error=%s", row.get("symbol"), exc)
            state = "UNCHANGED"
            if not quote_fresh:
                state = "STALE_QUOTE"
                stale_quote_count += 1
            elif price > 0 and ma20 > 0 and price < ma20:
                state = "TREND_BREAK_RISK"
            elif price > 0 and ma5 > 0 and abs(price / ma5 - 1.0) <= 0.02:
                state = "MA5_PROXIMITY"
            elif abs(pct_chg) >= 7.0:
                state = "INTRADAY_EXTENSION"
            # P6: 价格异动只创建 RetrievalHypothesis，不直接升级优先级
            symbol_str = str(row.get("symbol") or row.get("stock_code") or "")
            try:
                from core.runtime.market_session import MarketSession as _MS
                session_for_anomaly = _MS(market_session_value) if market_session_value != "unknown" else None
                if session_for_anomaly is not None:
                    anomaly = self.price_anomaly_detector.detect(
                        symbol=symbol_str,
                        pct_chg=pct_chg,
                        session=session_for_anomaly,
                        now=now_dt,
                    )
                    if anomaly is not None:
                        anomaly_hypotheses.append({
                            "symbol": anomaly.symbol,
                            "anomaly_type": anomaly.anomaly_type,
                            "pct_chg": anomaly.pct_chg,
                            "hypothesis_text": anomaly.hypothesis_text,
                            "created_at": anomaly.created_at,
                            "candidate_id": anomaly.candidate_id,
                            "eligible_for_evidence_slot": False,
                        })
            except Exception as exc:
                logger.debug("price anomaly detection failed symbol=%s error=%s", symbol_str, exc)
            snapshots.append({
                "symbol": symbol_str,
                "stock_name": str(row.get("stock_name") or observation.get("company_name") or ""),
                "theme_name": "、".join(theme_names),
                "theme_names": theme_names,
                "claim_ids": claim_ids,
                "price": round(price, 4),
                "pct_chg": round(pct_chg, 2),
                "provider": str(row.get("provider") or "unknown"),
                "market_timestamp": market_timestamp,
                "quote_trade_date": quote_trade_date,
                "quote_fresh": quote_fresh,
                "freshness": freshness_result,
                "updated_at": str(row.get("updated_at") or as_of),
                "monitor_state": state,
                "premarket_status": observation.get("status", "UNKNOWN"),
                "research_validated": any(
                    bool(item.get("research_validated")) for item in linked_observations
                ),
            })
        # 盘中指数快照：本地 A 股指数 + 外围市场（韩国 KOSPI、日经 225 等）
        market_indices = self._fetch_monitor_market_indices_safe(as_of=as_of)
        # 市场情绪：涨跌家数 + 热门板块 + 总成交额（从行情 provider 聚合）
        market_sentiment = self._fetch_market_sentiment_safe(as_of=as_of)
        payload = {
            "schema_version": self.VERSION,
            "mode": "monitor",
            "status": "partial" if stale_quote_count or recovery_errors else "success",
            "run_id": run_id,
            "as_of": as_of,
            "snapshot_count": len(snapshots),
            "stale_quote_count": stale_quote_count,
            "source_premarket_run_id": latest.get("run_id", ""),
            "market_session": market_session_value,
            "research_recovery": {
                "recovered_tasks": recovered_tasks,
                "errors": recovery_errors,
            },
            "snapshots": snapshots,
            "anomaly_hypotheses": anomaly_hypotheses,
            "market_indices": market_indices,
            "market_sentiment": market_sentiment,
            "notice": "盘中状态监控，不构成交易指令。",
        }
        # 为指数和个股附加分时图（TDX 真实数据优先，失败则降级为空）
        try:
            self._attach_intraday_charts(payload)
        except Exception as exc:
            logger.warning("附加分时图失败: %s", exc)

        html_content = self._monitor_html(payload)
        json_path, report_path = self._write_outputs(
            as_of[:10], run_id, "monitor", payload, self._monitor_markdown(payload),
            html_content=html_content,
        )
        # 推送使用 HTML 报告路径
        html_report_path = report_path.with_suffix(".html")
        self._append_monitor_history(as_of[:10], payload)
        if external_push and anomaly_hypotheses and stale_quote_count == 0:
            payload["push"] = self._maybe_push(
                external_push=True,
                trade_date=as_of[:10],
                title=f"AquaTrade 盘中异动提醒 {as_of[11:16]}",
                summary=f"检测到 {len(anomaly_hypotheses)} 个价格异动，已触发事实检索。",
                report_path=html_report_path if html_report_path.exists() else report_path,
                payload=payload,
                html_content=html_content,
            )
        elif external_push:
            payload["push"] = {
                "requested": True,
                "sent": False,
                "reason": "stale_market_data" if stale_quote_count else "no_alert_condition",
            }
        else:
            payload["push"] = {
                "requested": False,
                "sent": False,
                "reason": "external_push_disabled",
            }
        self._rewrite_json(json_path, payload)
        return RuntimeResult(
            mode="monitor", status=payload["status"], run_id=run_id, as_of=as_of,
            report_path=str(report_path), json_path=str(json_path), payload=payload,
            errors=recovery_errors,
        )

    def run_close(self, *, as_of: str, external_push: bool) -> RuntimeResult:
        return self._run_close_report(as_of=as_of, external_push=external_push)

    def _legacy_run_close(self, *, as_of: str, external_push: bool) -> RuntimeResult:
        run_id = self._run_id("close", as_of)
        recovered_tasks, recovery_errors = self._recover_dynamic_tasks_at_startup(
            as_of=as_of
        )
        history_path = self.output_dir / as_of[:10] / "monitor_history.jsonl"
        snapshots: list[dict[str, Any]] = []
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                try:
                    snapshots.append(json.loads(line))
                except Exception:
                    continue
        payload = {
            "schema_version": self.VERSION,
            "mode": "close",
            "status": "partial" if recovery_errors else "success",
            "run_id": run_id,
            "as_of": as_of,
            "monitor_cycles": len(snapshots),
            "last_snapshot": snapshots[-1] if snapshots else {},
            "research_recovery": {
                "recovered_tasks": recovered_tasks,
                "errors": recovery_errors,
            },
            "notice": "收盘归档仅用于复盘与反馈归因。",
        }
        markdown = (
            f"# AquaTrade 收盘监控归档 {as_of[:10]}\n\n"
            f"- 盘中监控轮次：{len(snapshots)}\n"
            f"- 归档时间：{as_of}\n\n"
            "仅用于研究复盘，不构成投资建议。\n"
        )
        json_path, report_path = self._write_outputs(
            as_of[:10], run_id, "close", payload, markdown
        )
        payload["push"] = self._maybe_push(
            external_push=external_push,
            trade_date=as_of[:10],
            title=f"AquaTrade 收盘归档 {as_of[:10]}",
            summary=f"盘中监控 {len(snapshots)} 轮已归档。",
            report_path=report_path,
            payload=payload,
        )
        self._rewrite_json(json_path, payload)
        return RuntimeResult(
            mode="close", status=payload["status"], run_id=run_id, as_of=as_of,
            report_path=str(report_path), json_path=str(json_path), payload=payload,
            errors=recovery_errors,
        )

    def _run_close_report(self, *, as_of: str, external_push: bool) -> RuntimeResult:
        run_id = self._run_id("close", as_of)
        recovered_tasks, recovery_errors = self._recover_dynamic_tasks_at_startup(
            as_of=as_of
        )
        history_path = self.output_dir / as_of[:10] / "monitor_history.jsonl"
        history: list[dict[str, Any]] = []
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                try:
                    history.append(json.loads(line))
                except Exception:
                    continue
        last_snapshot = history[-1] if history else {}
        source_snapshots = self._attach_local_first_party_evidence(
            list(last_snapshot.get("snapshots") or []),
            as_of=as_of,
        )
        analysis = self._build_session_analysis(source_snapshots, as_of=as_of)
        tail_path = self.output_dir / "latest_tail_research.json"
        try:
            tail_payload = json.loads(tail_path.read_text(encoding="utf-8"))
        except Exception:
            tail_payload = {}
        if str(tail_payload.get("as_of") or "")[:10] == as_of[:10]:
            analysis = {
                key: tail_payload[key]
                for key in (
                    "market_review",
                    "theme_strength",
                    "theme_candidate_rankings",
                    "close_watchlist",
                    "official_announcement_watchlist",
                    "next_day_checks",
                    "chart_data",
                    "fund_flow",
                    "external_markets",
                    "external_market_status",
                    "previous_mentions",
                    "self_learning_corrections",
                    "volume_anomalies",
                    "ai_summary",
                )
                if key in tail_payload
            }
        analysis = self._enrich_report_context(
            analysis, source_snapshots, as_of=as_of
        )
        payload = {
            "schema_version": self.VERSION,
            "mode": "close",
            "status": "partial" if recovery_errors else "success",
            "run_id": run_id,
            "as_of": as_of,
            "monitor_cycles": len(history),
            "last_snapshot": last_snapshot,
            **analysis,
            "analysis_source": (
                "same_day_full_market_tail_research"
                if str(tail_payload.get("as_of") or "")[:10] == as_of[:10]
                else "latest_monitor_fallback"
            ),
            "research_recovery": {
                "recovered_tasks": recovered_tasks,
                "errors": recovery_errors,
            },
            "notice": "收盘归档仅用于研究复盘与后续事实验证。",
        }
        markdown = self._session_analysis_markdown(payload, title="收盘研究复盘")
        json_path, report_path = self._write_outputs(
            as_of[:10], run_id, "close", payload, markdown
        )
        if external_push:
            self._write_push_gate(as_of[:10], run_id, json_path, payload["status"])
        payload["push"] = self._maybe_push(
            external_push=external_push,
            trade_date=as_of[:10],
            title=f"AquaTrade 收盘研究复盘 {as_of[:10]}",
            summary=(
                f"监控 {len(history)} 轮；有效样本 "
                f"{payload['market_review']['sample_count']} 个；"
                f"强势主题 {len(payload['theme_strength'])} 个。"
            ),
            report_path=report_path,
            payload=payload,
        )
        self._rewrite_json(json_path, payload)
        return RuntimeResult(
            mode="close", status=payload["status"], run_id=run_id, as_of=as_of,
            report_path=str(report_path), json_path=str(json_path), payload=payload,
            errors=recovery_errors,
        )

    def run_tail_research(self, *, as_of: str, external_push: bool) -> RuntimeResult:
        """Generate the once-daily 14:50 research observation pool."""
        run_id = self._run_id("tail_research", as_of)
        latest_path = self.output_dir / "latest_monitor.json"
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            latest = {}
        if str(latest.get("as_of") or "")[:10] != as_of[:10]:
            latest = {}
        source_snapshots = list(latest.get("snapshots") or [])
        discovered, discovery_errors = self._discover_dynamic_theme_snapshots(as_of=as_of)
        merged_by_code = {
            self._code(row.get("symbol")): dict(row)
            for row in source_snapshots if self._code(row.get("symbol"))
        }
        for row in discovered:
            code = self._code(row.get("symbol"))
            previous = merged_by_code.get(code, {})
            row["theme_names"] = list(dict.fromkeys(
                list(previous.get("theme_names") or []) + list(row.get("theme_names") or [])
            ))
            row["claim_ids"] = list(dict.fromkeys(
                list(previous.get("claim_ids") or []) + list(row.get("claim_ids") or [])
            ))
            row["research_validated"] = bool(
                previous.get("research_validated") or row.get("research_validated")
            )
            merged_by_code[code] = {**previous, **row}
        enriched_snapshots = self._attach_local_first_party_evidence(
            list(merged_by_code.values()),
            as_of=as_of,
        )
        enriched_snapshots = self._enrich_candidate_research_features(
            enriched_snapshots, as_of=as_of, max_candidates=50
        )
        analysis = self._build_session_analysis(enriched_snapshots, as_of=as_of)
        analysis = self._enrich_report_context(
            analysis, enriched_snapshots, as_of=as_of
        )
        quote_scan_metrics = dict(
            (discovered[0].get("quote_scan_metrics") or {}) if discovered else {}
        )
        themed_member_count = sum(
            bool(row.get("theme_names")) for row in discovered
        )
        if discovery_errors:
            status = "partial" if enriched_snapshots else "failed"
        elif not enriched_snapshots:
            status = "empty"
        else:
            status = "success"
        payload = {
            "schema_version": self.VERSION,
            "mode": "tail_research",
            "status": status,
            "run_id": run_id,
            "as_of": as_of,
            "source_monitor_run_id": latest.get("run_id", ""),
            "dynamic_discovery": {
                "discovered_snapshot_count": len(discovered),
                "universe_scope": (
                    discovered[0].get("universe_scope") if discovered else "RESEARCH_POOL"
                ),
                "market_symbol_count": quote_scan_metrics.get(
                    "requested_symbol_count", len(discovered)
                ),
                "fresh_quote_count": analysis["market_review"]["fresh_quote_count"],
                "theme_member_count": themed_member_count,
                "membership_coverage_ratio": None,
                "membership_coverage_reason": "主题源未报告可验证的全量成员总数",
                "quote_scan_metrics": quote_scan_metrics,
                "errors": discovery_errors,
            },
            **analysis,
            "notice": "尾盘研究观察池仅用于研究与次日事实验证。",
        }
        markdown = self._session_analysis_markdown(payload, title="14:50 尾盘研究观察")
        json_path, report_path = self._write_outputs(
            as_of[:10], run_id, "tail_research", payload, markdown
        )
        if external_push:
            self._write_push_gate(as_of[:10], run_id, json_path, payload["status"])
        payload["push"] = self._maybe_push(
            external_push=external_push,
            trade_date=as_of[:10],
            title=f"AquaTrade 14:50 尾盘研究观察 {as_of[:10]}",
            summary=(
                f"研究观察候选 {len(payload['close_watchlist'])} 个；"
                f"强势主题 {len(payload['theme_strength'])} 个。"
            ),
            report_path=report_path,
            payload=payload,
        )
        self._rewrite_json(json_path, payload)
        return RuntimeResult(
            mode="tail_research", status=status, run_id=run_id, as_of=as_of,
            report_path=str(report_path), json_path=str(json_path), payload=payload,
            errors=discovery_errors,
            exit_code=1 if status == "failed" else 0,
        )

    def _discover_dynamic_theme_snapshots(
        self, *, as_of: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Refresh structured theme membership before the 14:50 report."""
        errors: list[str] = []
        try:
            engine = self._theme_engine
            if engine is None:
                engine, _ = self._load_theme_components()
            packets = engine.generate(
                as_of=as_of,
                trade_date=as_of[:10],
                max_themes=50,
                max_source_boards=300,
            )
        except Exception as exc:
            return [], [f"dynamic theme discovery failed: {exc}"]
        metadata: dict[str, dict[str, Any]] = {}
        for packet in packets or []:
            theme_name = str(getattr(packet, "theme_name", "") or "")
            for member in getattr(packet, "members", []) or []:
                value = member if isinstance(member, dict) else vars(member)
                code = self._code(value.get("symbol"))
                if not code:
                    continue
                item = metadata.setdefault(code, {
                    "symbol": code,
                    "stock_name": str(value.get("company_name") or ""),
                    "theme_names": [],
                    "concept_theme_names": [],
                    "claim_ids": [],
                    "research_validated": False,
                })
                if theme_name and theme_name not in item["theme_names"]:
                    item["theme_names"].append(theme_name)
                if theme_name and theme_name not in item["concept_theme_names"]:
                    item["concept_theme_names"].append(theme_name)
                item["claim_ids"] = list(dict.fromkeys(
                    item["claim_ids"] + list(value.get("research_claim_ids") or [])
                ))
                claim_status = str(value.get("research_claim_status") or "").upper()
                item["research_validated"] = bool(
                    item["research_validated"]
                    or (item["claim_ids"] and claim_status in {"ACTIVE", "TRUSTED"})
                )
        universe_scope = "RESEARCH_POOL"
        try:
            basics = self.data_query.get_stock_basic()
        except Exception as exc:
            basics = pd.DataFrame()
            errors.append(f"full market universe unavailable: {exc}")
        universe: dict[str, str] = {}
        industries: dict[str, str] = {}
        if basics is not None and not basics.empty:
            for row in basics.to_dict(orient="records"):
                code = self._code(
                    row.get("symbol") or row.get("stock_code") or row.get("ts_code")
                )
                if code:
                    universe[code] = str(row.get("name") or row.get("stock_name") or "")
                    industry = str(row.get("industry") or "").strip()
                    if industry:
                        industries[code] = industry
            if universe:
                universe_scope = "FULL_MARKET"
        if not universe:
            universe = {
                code: str(item.get("stock_name") or "")
                for code, item in metadata.items()
            }
        if not universe:
            return [], errors
        for code, industry in industries.items():
            item = metadata.get(code)
            if item is not None and industry not in item["theme_names"]:
                item["theme_names"].append(industry)
        use_local_close = (
            as_of[:10] < datetime.now(MARKET_TZ).date().isoformat()
            or as_of[11:16] >= "15:05"
        )
        quote_rows: list[dict[str, Any]] = []
        quote_metrics: dict[str, Any] = {}
        if use_local_close:
            quote_rows, quote_metrics = self._load_local_close_quotes(
                universe, trade_date=as_of[:10]
            )
        if not quote_rows:
            try:
                quote_rows, quote_metrics = self._fetch_realtime_quotes_batched(
                    list(universe), batch_size=80, max_workers=8
                )
            except Exception as exc:
                errors.append(f"dynamic theme quote refresh failed: {exc}")
                quote_rows = []
                quote_metrics = {}
        if not quote_rows:
            quote_rows, local_metrics = self._load_local_close_quotes(
                universe, trade_date=as_of[:10]
            )
            if quote_rows:
                quote_metrics = local_metrics
        errors.extend(str(item) for item in quote_metrics.get("errors") or [])
        if not quote_rows:
            return [], [*errors, "dynamic theme quote refresh returned empty"]
        output: list[dict[str, Any]] = []
        market_symbol_count = len(universe)
        for row in quote_rows:
            code = self._code(row.get("symbol") or row.get("stock_code"))
            if code not in universe:
                continue
            timestamp = str(row.get("market_timestamp") or "")
            base = metadata.get(code, {
                "symbol": code,
                "stock_name": universe.get(code, ""),
                "theme_names": [industries[code]] if code in industries else [],
                "concept_theme_names": [],
                "claim_ids": [],
                "research_validated": False,
            })
            output.append({
                **base,
                "stock_name": str(
                    row.get("stock_name") or base["stock_name"] or universe.get(code) or "未知"
                ),
                "price": round(self._float(row.get("price") or row.get("close")), 4),
                "pct_chg": round(self._float(row.get("pct_chg")), 2),
                "turnover_rate": round(self._float(row.get("turnover_rate")), 2),
                "volume_ratio": round(self._float(row.get("volume_ratio")), 2),
                "provider": str(row.get("provider") or "unknown"),
                "market_timestamp": timestamp,
                "quote_fresh": self._market_timestamp_date(timestamp) == as_of[:10],
                "premarket_status": "WATCH" if base["research_validated"] else "UNKNOWN",
                "universe_scope": universe_scope,
                "market_symbol_count": market_symbol_count,
                "quote_scan_metrics": quote_metrics,
            })
        return output, errors

    def _load_local_close_quotes(
        self, universe: dict[str, str], *, trade_date: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Use the exact same-day local close when realtime providers are unavailable."""
        try:
            frame = self.data_query.get_stock_pool(trade_date)
        except Exception as exc:
            return [], {
                "requested_symbol_count": len(universe),
                "returned_quote_count": 0,
                "failed_batch_count": 1,
                "fallback": "local_daily",
                "errors": [f"local daily close unavailable: {exc}"],
            }
        if frame is None or frame.empty:
            return [], {
                "requested_symbol_count": len(universe),
                "returned_quote_count": 0,
                "failed_batch_count": 1,
                "fallback": "local_daily",
                "errors": ["local daily close returned empty"],
            }
        rows: list[dict[str, Any]] = []
        timestamp = trade_date.replace("-", "") + "150000"
        for source in frame.to_dict(orient="records"):
            code = self._code(
                source.get("stock_code") or source.get("symbol") or source.get("ts_code")
            )
            if code not in universe:
                continue
            source_date = str(source.get("trade_date") or "")[:10]
            if source_date and source_date != trade_date:
                continue
            rows.append(
                {
                    "symbol": code,
                    "stock_name": universe.get(code, ""),
                    "price": self._float(source.get("close")),
                    "pct_chg": self._float(
                        source.get("change_pct") or source.get("pct_chg")
                    ),
                    "turnover_rate": self._float(source.get("turnover_rate")),
                    "volume_ratio": self._float(source.get("volume_ratio")),
                    "provider": "local_daily",
                    "market_timestamp": timestamp,
                }
            )
        return rows, {
            "requested_symbol_count": len(universe),
            "returned_quote_count": len(rows),
            "failed_batch_count": 0 if rows else 1,
            "fallback": "local_daily",
            "source_as_of": trade_date,
            "errors": [],
        }

    def _attach_local_first_party_evidence(
        self,
        snapshots: list[dict[str, Any]],
        *,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Join ACTIVE/TRUSTED local claims and grounded first-party evidence."""
        output = [dict(row) for row in snapshots if isinstance(row, dict)]
        try:
            claims = list(self.repository.list_claims(status="ACTIVE", limit=500))
            claims.extend(self.repository.list_claims(status="TRUSTED", limit=500))
        except Exception as exc:
            logger.warning("加载本地 claims 失败，跳过一方证据关联: %s", exc)
            return output
        claims_by_code: dict[str, list[Any]] = {}
        for claim in claims:
            if as_of and not self._record_visible_at(
                claim, as_of=as_of, fields=("created_at", "valid_from")
            ):
                continue
            code = self._code(getattr(claim, "subject_id", ""))
            if code:
                claims_by_code.setdefault(code, []).append(claim)
        for row in output:
            code = self._code(row.get("symbol"))
            linked_claims = claims_by_code.get(code, [])
            eligible_ids = [
                str(getattr(claim, "claim_id", ""))
                for claim in linked_claims if getattr(claim, "claim_id", "")
            ]
            linked_ids = list(dict.fromkeys(
                list(row.get("claim_ids") or []) + eligible_ids
            ))
            first_party_quotes: list[str] = []
            for claim_id in eligible_ids:
                try:
                    evidence_rows = self.repository.list_evidence(
                        claim_id=claim_id, limit=20
                    )
                except Exception as exc:
                    logger.debug("加载 claim evidence 失败 claim_id=%s error=%s", claim_id, exc)
                    continue
                for evidence in evidence_rows:
                    if as_of and not self._record_visible_at(
                        evidence,
                        as_of=as_of,
                        fields=(
                            "published_at",
                            "retrieved_at",
                            "created_at",
                            "valid_from",
                        ),
                    ):
                        continue
                    metadata = getattr(evidence, "metadata", {}) or {}
                    invalidated = bool(metadata.get("invalidated"))
                    if (
                        bool(getattr(evidence, "is_first_party", False))
                        and str(getattr(evidence, "stance", "")) == "support"
                        and not invalidated
                    ):
                        quote = str(getattr(evidence, "quote", "") or "").strip()
                        if quote:
                            first_party_quotes.append(quote[:160])
            row["claim_ids"] = linked_ids
            row["first_party_evidence_count"] = len(first_party_quotes)
            row["first_party_evidence_quotes"] = list(
                dict.fromkeys(first_party_quotes)
            )[:3]
            row["research_validated"] = bool(first_party_quotes)
        return output

    @staticmethod
    def _record_visible_at(
        record: Any,
        *,
        as_of: str,
        fields: tuple[str, ...],
    ) -> bool:
        """Fail closed unless a record has a provable timestamp visible at as_of."""
        try:
            cutoff = ResearchMonitor._parse_pit_timestamp(as_of)
        except (TypeError, ValueError):
            return False
        timestamps: list[datetime] = []
        for field_name in fields:
            value = (
                record.get(field_name)
                if isinstance(record, dict)
                else getattr(record, field_name, "")
            )
            if not str(value or "").strip():
                continue
            try:
                timestamps.append(ResearchMonitor._parse_pit_timestamp(str(value)))
            except (TypeError, ValueError):
                return False
        return bool(timestamps) and all(timestamp <= cutoff for timestamp in timestamps)

    @staticmethod
    def _parse_pit_timestamp(value: str) -> datetime:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("empty PIT timestamp")
        date_only = bool(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw)
            or re.fullmatch(r"\d{8}", raw)
        )
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MARKET_TZ)
        if date_only:
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
        return parsed.astimezone(MARKET_TZ)

    def _enrich_candidate_research_features(
        self,
        snapshots: list[dict[str, Any]],
        *,
        as_of: str,
        max_candidates: int = 50,
    ) -> list[dict[str, Any]]:
        """Add local five-session strength and first-party announcements.

        Only the strongest members of each mapped theme cross the external
        announcement boundary; the full-market quote scan itself stays cheap.
        """
        output = [dict(row) for row in snapshots]
        strongest = sorted(
            (
                row for row in output
                if self._float(row.get("pct_chg")) > 0 and row.get("theme_names")
            ),
            key=lambda row: self._float(row.get("pct_chg")),
            reverse=True,
        )
        target_codes = list(dict.fromkeys(
            self._code(row.get("symbol")) for row in strongest
            if self._code(row.get("symbol"))
        ))[: max(0, int(max_candidates))]
        concept_members = sorted(
            (row for row in strongest if row.get("concept_theme_names")),
            key=lambda row: self._float(row.get("pct_chg")),
            reverse=True,
        )[:50]
        target_codes = list(dict.fromkeys(
            target_codes + [self._code(row.get("symbol")) for row in concept_members]
        ))
        if not target_codes:
            return output
        by_code = {self._code(row.get("symbol")): row for row in output}
        trade_date = as_of[:10]
        start_date = (datetime.fromisoformat(trade_date) - timedelta(days=30)).date().isoformat()

        def apply_history(code: str, history: pd.DataFrame) -> None:
            row = by_code[code]
            row["five_session_pct"] = self._compound_five_session_strength(
                history,
                live_pct_chg=self._float(row.get("pct_chg")),
                trade_date=trade_date,
            )
            row.update(calculate_technical_context(history, as_of=trade_date))
            if history is not None and not history.empty:
                completed = history
                if "trade_date" in completed.columns:
                    completed = completed[
                        completed["trade_date"].astype(str).str[:10] < trade_date
                    ].sort_values("trade_date")
                if not completed.empty:
                    latest = completed.iloc[-1]
                    if not self._float(row.get("turnover_rate")):
                        row["turnover_rate"] = round(
                            self._float(latest.get("turnover_rate")), 2
                        )
                    if not self._float(row.get("volume_ratio")):
                        row["volume_ratio"] = round(
                            self._float(latest.get("volume_ratio")), 2
                        )

        try:
            bulk_history = self.data_query.get_stock_history(None, start_date, trade_date)
        except Exception as exc:
            logger.warning("bulk candidate history enrichment failed: %s", type(exc).__name__)
            bulk_history = pd.DataFrame()
        history_by_code: dict[str, pd.DataFrame] = {}
        if bulk_history is not None and not bulk_history.empty:
            symbol_column = next(
                (name for name in ("stock_code", "symbol", "ts_code") if name in bulk_history.columns),
                "",
            )
            if symbol_column:
                selected = bulk_history.copy()
                selected["_code"] = selected[symbol_column].map(self._code)
                selected = selected[selected["_code"].isin(target_codes)]
                history_by_code = {
                    str(code): frame.drop(columns=["_code"])
                    for code, frame in selected.groupby("_code", sort=False)
                }
        for code in target_codes:
            apply_history(code, history_by_code.get(code, pd.DataFrame()))

        from infrastructure.data_providers.research.cninfo_provider import CninfoProvider

        cninfo = CninfoProvider()

        def fetch_announcements(code: str) -> tuple[str, list[dict[str, Any]]]:
            return code, list(cninfo.get_announcements(code=code, count=5) or [])

        executor = ThreadPoolExecutor(max_workers=min(8, len(target_codes)))
        futures = {}
        try:
            futures = {executor.submit(fetch_announcements, code): code for code in target_codes}
            for future in as_completed(futures, timeout=15):
                code = futures[future]
                try:
                    _, announcements = future.result()
                except Exception as exc:
                    logger.warning(
                        "candidate announcement enrichment failed symbol=%s error=%s",
                        code,
                        exc,
                    )
                    continue
                normalized = self._pit_announcements(
                    announcements, trade_date=trade_date, as_of=as_of
                )
                row = by_code[code]
                row["announcements"] = normalized
                row["announcement_titles"] = [item["title"] for item in normalized]
                row["first_party_evidence_count"] = max(
                    int(row.get("first_party_evidence_count") or 0), len(normalized)
                )
        except TimeoutError:
            unfinished = sum(not future.done() for future in futures)
            logger.warning(
                "candidate announcement enrichment timed out unfinished=%s",
                unfinished,
            )
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        forecast_registry = None
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            forecast_registry = ProviderRegistry()
            forecast_start = (
                datetime.fromisoformat(trade_date) - timedelta(days=7)
            ).date().isoformat()
            forecasts = forecast_registry.get_earnings_forecasts(
                forecast_start, trade_date, None
            )
        except Exception as exc:
            logger.warning("earnings forecast enrichment failed: %s", type(exc).__name__)
            forecasts = pd.DataFrame()
        if forecasts is not None and not forecasts.empty:
            forecast_rows = self._pit_earnings_forecasts(
                forecasts.sort_values("announcement_date").to_dict(orient="records"),
                as_of=as_of,
            )
            for item in forecast_rows:
                code = self._code(item.get("symbol"))
                if code in by_code:
                    by_code[code]["earnings_forecast"] = item
        missing_concept_codes = [
            self._code(row.get("symbol")) for row in concept_members
            if not by_code[self._code(row.get("symbol"))].get("earnings_forecast")
        ]
        if missing_concept_codes and forecast_registry is not None:
            try:
                fallback_forecasts = forecast_registry.get_earnings_forecasts(
                    start_date, trade_date, missing_concept_codes
                )
            except Exception as exc:
                logger.warning("concept forecast fallback failed: %s", type(exc).__name__)
                fallback_forecasts = pd.DataFrame()
            fallback_rows = self._pit_earnings_forecasts(
                fallback_forecasts.to_dict(orient="records"),
                as_of=as_of,
            )
            for item in fallback_rows:
                code = self._code(item.get("symbol"))
                if code in by_code:
                    by_code[code]["earnings_forecast"] = item
        return output

    @staticmethod
    def _pit_earnings_forecasts(
        forecasts: list[dict[str, Any]],
        *,
        as_of: str,
    ) -> list[dict[str, Any]]:
        """Keep only forecast records provably available at the exact cutoff."""
        output: list[dict[str, Any]] = []
        precise_fields = ("available_at", "published_at", "created_at")
        for item in forecasts:
            if not isinstance(item, dict):
                continue
            fields = tuple(
                field for field in precise_fields if str(item.get(field) or "").strip()
            )
            if not fields:
                fields = ("announcement_date",)
            if ResearchMonitor._record_visible_at(
                item,
                as_of=as_of,
                fields=fields,
            ):
                output.append(item)
        return output

    @staticmethod
    def _pit_announcements(
        announcements: list[dict[str, Any]],
        *,
        trade_date: str,
        as_of: str | None = None,
        max_age_days: int = 30,
    ) -> list[dict[str, Any]]:
        """Normalize announcements and retain only recent PIT-visible items."""
        end = datetime.fromisoformat(trade_date).date()
        cutoff = end - timedelta(days=max(0, max_age_days))
        visibility_cutoff = ResearchMonitor._parse_pit_timestamp(
            as_of or f"{trade_date}T23:59:59.999999+08:00"
        )
        output: list[dict[str, Any]] = []
        for item in announcements:
            if not isinstance(item, dict):
                continue
            title = str(
                item.get("title")
                or item.get("announcement_title")
                or item.get("公告标题")
                or ""
            ).strip()
            raw_date = str(
                item.get("published_at")
                or item.get("publish_time")
                or item.get("announcement_time")
                or item.get("date")
                or item.get("announcement_date")
                or item.get("公告日期")
                or ""
            )
            if not title or not raw_date:
                continue
            try:
                published_at = ResearchMonitor._parse_pit_timestamp(raw_date)
                announced_on = published_at.date()
            except (TypeError, ValueError):
                continue
            if (
                not cutoff <= announced_on <= end
                or published_at > visibility_cutoff
            ):
                continue
            output.append({
                "title": title,
                "date": announced_on.isoformat(),
                "url": str(item.get("url") or item.get("announcement_url") or ""),
                "source": str(item.get("source") or item.get("provider") or "cninfo"),
            })
        return output

    def _enrich_report_context(
        self,
        analysis: dict[str, Any],
        snapshots: list[dict[str, Any]],
        *,
        as_of: str,
    ) -> dict[str, Any]:
        """Attach optional market context without blocking the core report."""
        trade_date = as_of[:10]
        theme_names = [
            str(row.get("theme_name") or "")
            for row in analysis.get("theme_strength") or []
            if str(row.get("theme_name") or "")
        ]
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            registry = ProviderRegistry()
            board_flow = registry.get_board_fund_flow(trade_date)
            fund_flow = summarize_board_fund_flow(board_flow, theme_names=theme_names)
            fund_flow["error"] = "" if fund_flow["status"] == "ok" else "上游未返回板块资金流"
        except Exception as exc:
            registry = None
            fund_flow = {
                "status": "unavailable",
                "source": "",
                "theme_matches": [],
                "leaders": [],
                "error": str(exc)[:200],
            }
        flow_by_theme = {
            row["theme_name"]: row for row in fund_flow.get("theme_matches") or []
        }
        for theme in analysis.get("theme_strength") or []:
            matched = flow_by_theme.get(str(theme.get("theme_name") or ""))
            theme["main_net_inflow"] = matched.get("main_net_inflow") if matched else None
            theme["fund_flow_source"] = matched.get("provider", "") if matched else ""

        watchlist = analysis.get("close_watchlist") or []
        stock_codes = [self._code(row.get("symbol")) for row in watchlist]
        if (
            registry is not None
            and stock_codes
            and fund_flow.get("status") == "ok"
            and "ths" not in str(fund_flow.get("source") or "").lower()
        ):
            try:
                stock_flow = registry.get_stock_fund_flow(trade_date, stock_codes)
            except Exception as exc:
                stock_flow = pd.DataFrame()
                fund_flow["stock_error"] = str(exc)[:200]
            if stock_flow is not None and not stock_flow.empty:
                if "trade_date" in stock_flow.columns:
                    stock_flow = stock_flow.sort_values("trade_date")
                by_code = {
                    self._code(row.get("symbol")): row
                    for row in stock_flow.to_dict(orient="records")
                }
                for row in watchlist:
                    flow = by_code.get(self._code(row.get("symbol")))
                    row["main_net_inflow"] = (
                        self._float(flow.get("main_net_inflow")) if flow else None
                    )
                    row["main_net_inflow_pct"] = (
                        self._float(flow.get("main_net_inflow_pct")) if flow else None
                    )
                    row["fund_flow_source"] = str(flow.get("provider") or "") if flow else ""
        for row in watchlist:
            row.setdefault("main_net_inflow", None)
            row.setdefault("main_net_inflow_pct", None)
            row.setdefault("fund_flow_source", "")

        external_snapshot = load_external_market_snapshot(as_of=as_of)
        external_markets = external_snapshot["markets"]

        previous = self._read_previous_close_payload(trade_date)
        previous_mentions, corrections = compare_previous_mentions(
            list(previous.get("close_watchlist") or []), snapshots
        )
        previous_themes = {
            str(row.get("theme_name") or "")
            for row in previous.get("theme_strength") or []
        }
        volume_anomalies = [
            {
                "symbol": row.get("symbol"),
                "stock_name": row.get("stock_name"),
                "theme_name": row.get("theme_name"),
                "volume_vs_ma5": row.get("volume_vs_ma5"),
                "message": "前热板块出现量能异动",
            }
            for row in watchlist
            if row.get("volume_anomaly")
            and previous_themes.intersection(str(row.get("theme_name") or "").split("、"))
        ]

        analysis.update(
            fund_flow=fund_flow,
            external_markets=external_markets,
            external_market_status={
                key: external_snapshot[key]
                for key in ("status", "source", "attempts", "error")
            },
            previous_mentions=previous_mentions,
            self_learning_corrections=corrections,
            volume_anomalies=volume_anomalies,
        )
        review = analysis.get("market_review") or {}
        fresh = self._float(review.get("fresh_quote_count"))
        market_count = self._float(review.get("market_symbol_count"))
        domestic_complete = bool(
            market_count > 0
            and fresh / market_count >= 0.8
            and analysis.get("theme_strength")
        )
        missing = []
        if fund_flow.get("status") != "ok":
            missing.append("主力资金流")
        if external_snapshot.get("status") != "ok":
            missing.append("外围指数")
        if not domestic_complete:
            quality_status = "insufficient"
            ranking_scope = "suppressed"
        elif missing:
            quality_status = "degraded"
            ranking_scope = "domestic_only"
        else:
            quality_status = "complete"
            ranking_scope = "full_context"
        analysis["data_quality"] = {
            "status": quality_status,
            "ranking_scope": ranking_scope,
            "missing": missing,
            "message": (
                "国内核心数据不足，不输出完整研究排名"
                if quality_status == "insufficient"
                else f"降级版：缺少{'、'.join(missing)}，观察优先级仅基于国内可验证数据"
                if missing
                else "关键数据完整"
            ),
        }
        analysis["ai_summary"] = self._generate_grounded_ai_summary(analysis)
        return analysis

    def _read_previous_close_payload(self, trade_date: str) -> dict[str, Any]:
        candidates: list[tuple[str, Path]] = []
        paths = [
            *self.output_dir.glob("close_close_*.json"),
            *self.output_dir.glob("*/close_close_*.json"),
        ]
        for path in paths:
            match = re.search(r"close_close_(\d{8})_", path.name)
            if not match:
                continue
            date = f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:8]}"
            if date < trade_date:
                candidates.append((date, path))
        for _, path in sorted(candidates, reverse=True):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("读取历史 close payload 失败 path=%s error=%s", path, exc)
                continue
        return {}

    @staticmethod
    def _generate_grounded_ai_summary(analysis: dict[str, Any]) -> str:
        themes = [
            str(row.get("theme_name") or "")
            for row in analysis.get("theme_strength") or []
            if str(row.get("theme_name") or "")
        ]
        leaders = [
            str(row.get("stock_name") or row.get("symbol") or "")
            for row in analysis.get("close_watchlist") or []
            if str(row.get("stock_name") or row.get("symbol") or "")
        ]
        if not themes or not leaders:
            return ""
        review = analysis.get("market_review") or {}
        average = ResearchMonitor._float(review.get("average_pct_chg"))
        tone = "偏强" if average >= 0.3 else "震荡" if average > -0.3 else "偏弱"
        flow_names = [
            str(row.get("board_name") or "")
            for row in (analysis.get("fund_flow") or {}).get("leaders") or []
            if str(row.get("board_name") or "")
        ][:2]
        catalyst = str((analysis.get("close_watchlist") or [{}])[0].get("catalyst") or "暂无硬催化")
        breadth = f"上涨{int(ResearchMonitor._float(review.get('rising_count')))}/下跌{int(ResearchMonitor._float(review.get('falling_count')))}"
        theme_text = "、".join(themes[:3])
        flow_text = "、".join(flow_names) or "资金流数据缺失"
        fallback = (
            f"{tone}，{breadth}；{theme_text}居前；资金流靠前为{flow_text}；"
            f"{leaders[0]}：{catalyst}；次日验证板块扩散与量能。（规则总结）"
        )
        facts = {
            "market_tone": tone,
            "breadth": breadth,
            "leading_themes": themes[:3],
            "fund_flow_leaders": flow_names,
            "research_subject": leaders[0],
            "catalyst": catalyst,
            "next_check": "板块扩散与量能",
        }
        try:
            from core.utils.llm_usage import call_llm_with_budget, load_llm_providers

            providers = sorted(
                load_llm_providers(),
                key=lambda provider: (
                    provider.name != "backup",
                    "deepseek-v4-flash" not in provider.model_name,
                ),
            )
            content = call_llm_with_budget(
                [
                    {
                        "role": "system",
                        "content": (
                            "依据给定事实写60至120字中文复盘摘要，只回传JSON {summary:string}。"
                            "必须包含市场广度、前三方向、资金流靠前方向、研究对象催化和次日验证项；"
                            "不得新增原因、数字、公司、板块或交易动作。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(facts, ensure_ascii=False),
                    },
                ],
                source="research.close_short_summary",
                providers=providers,
                temperature=0.0,
                max_completion_tokens=160,
                response_format_json=True,
                timeout=5,
                allow_no_provider=True,
            )
            parsed = json.loads(content)
        except Exception as exc:
            logger.warning("grounded AI summary unavailable: %s", exc)
            return fallback
        summary = str(parsed.get("summary") or "").strip()
        forbidden = ("买入", "卖出", "仓位", "止盈", "止损", "推荐")
        if (
            not summary
            or themes[0] not in summary
            or leaders[0] not in summary
            or any(word in summary for word in forbidden)
        ):
            logger.warning("grounded AI summary rejected: required facts missing")
            return fallback
        return summary[:160].rstrip("。") + "（AI生成总结）"

    @classmethod
    def _build_session_analysis(
        cls, snapshots: list[dict[str, Any]], *, as_of: str
    ) -> dict[str, Any]:
        fresh = [
            row for row in snapshots
            if isinstance(row, dict)
            and bool(row.get("quote_fresh"))
            and cls._market_timestamp_date(row.get("market_timestamp")) == as_of[:10]
        ]
        changes = [cls._float(row.get("pct_chg")) for row in fresh]
        full_market = any(row.get("universe_scope") == "FULL_MARKET" for row in snapshots)
        market_symbol_count = max(
            (int(cls._float(row.get("market_symbol_count"))) for row in snapshots),
            default=0,
        )
        market_review = {
            "scope": "全市场实时行情" if full_market else "研究观察池样本，非全市场",
            "universe_scope": "FULL_MARKET" if full_market else "RESEARCH_POOL",
            "market_symbol_count": market_symbol_count or len(snapshots),
            "fresh_quote_count": len(fresh),
            "sample_count": len(fresh),
            "rising_count": sum(value > 0 for value in changes),
            "falling_count": sum(value < 0 for value in changes),
            "flat_count": sum(value == 0 for value in changes),
            "average_pct_chg": round(sum(changes) / len(changes), 2) if changes else 0.0,
            "near_limit_up_count": sum(cls._is_near_price_limit(row, 1) for row in fresh),
            "near_limit_down_count": sum(cls._is_near_price_limit(row, -1) for row in fresh),
            "stale_excluded_count": len(snapshots) - len(fresh),
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in fresh:
            names = row.get("theme_names") or []
            if not names and row.get("theme_name"):
                names = str(row["theme_name"]).split("、")
            for name in dict.fromkeys(str(item).strip() for item in names if item):
                grouped.setdefault(name, []).append(row)
        themes: list[dict[str, Any]] = []
        for name, members in grouped.items():
            ordered = sorted(
                members, key=lambda row: cls._float(row.get("pct_chg")), reverse=True
            )
            values = [cls._float(row.get("pct_chg")) for row in members]
            average = sum(values) / len(values)
            breadth = sum(value > 0 for value in values) / len(values)
            themes.append({
                "theme_name": name,
                "member_count": len(members),
                "rising_count": sum(value > 0 for value in values),
                "falling_count": sum(value < 0 for value in values),
                "average_pct_chg": round(average, 2),
                "strength_score": round(average * 0.7 + breadth * 3.0, 4),
                "core_symbol": str(ordered[0].get("symbol") or ""),
                "core_company": str(ordered[0].get("stock_name") or "未知"),
                "core_pct_chg": round(cls._float(ordered[0].get("pct_chg")), 2),
            })
        themes = [
            row for row in themes
            if row["average_pct_chg"] > 0
            and row["rising_count"] >= (row["member_count"] + 1) // 2
        ]
        themes.sort(key=lambda row: (row["strength_score"], row["member_count"]), reverse=True)
        concept_names = {
            str(name)
            for row in fresh
            for name in (row.get("concept_theme_names") or [])
            if str(name)
        }
        selected_themes = themes[:10]
        selected_names = {row["theme_name"] for row in selected_themes}
        selected_themes.extend(
            row for row in themes
            if row["theme_name"] in concept_names and row["theme_name"] not in selected_names
        )
        strong_theme_names = {row["theme_name"] for row in themes}
        theme_candidate_rankings = [
            {
                "theme_name": theme["theme_name"],
                "member_count": len(grouped.get(theme["theme_name"], [])),
                "ranked_members": cls._rank_research_candidates(
                    grouped.get(theme["theme_name"], []), limit=10
                ),
            }
            for theme in selected_themes
        ]
        ranked = cls._rank_research_candidates(
            [
                row for row in fresh
                if cls._float(row.get("pct_chg")) > 0
                and strong_theme_names.intersection(row.get("theme_names") or [])
            ],
            limit=5,
        )
        watchlist = []
        for row in ranked:
            claim_count = len(row.get("claim_ids") or [])
            causal_announcements = list(row.get("causal_announcements") or [])
            first_party_quotes = [
                str(item.get("title") or "") for item in causal_announcements
                if str(item.get("title") or "")
            ]
            first_party_count = len(first_party_quotes)
            if not first_party_count:
                first_party_quotes = list(row.get("first_party_evidence_quotes") or [])
                first_party_count = len(first_party_quotes)
            validated = bool(row.get("research_validated") or causal_announcements)
            technical_reference = "；".join(
                f"{label} {cls._float(value):.2f}"
                for label, value in (
                    ("前高", row.get("previous_high")),
                    ("MA5", row.get("ma5")),
                    ("MA10", row.get("ma10")),
                )
                if value not in (None, "")
            ) or "暂无数据"
            watchlist.append({
                "symbol": str(row.get("symbol") or ""),
                "stock_name": str(row.get("stock_name") or "未知"),
                "theme_name": "、".join(row.get("theme_names") or []) or str(row.get("theme_name") or "未分类"),
                "pct_chg": round(cls._float(row.get("pct_chg")), 2),
                "five_session_pct": round(cls._float(row.get("five_session_pct")), 2),
                "turnover_rate": round(cls._float(row.get("turnover_rate")), 2),
                "volume_ratio": round(cls._float(row.get("volume_ratio")), 2),
                "ma5": row.get("ma5"),
                "ma10": row.get("ma10"),
                "previous_high": row.get("previous_high"),
                "support_label": str(row.get("support_label") or "暂无数据"),
                "support_price": row.get("support_price"),
                "technical_reference": technical_reference,
                "volume_vs_ma5": row.get("volume_vs_ma5"),
                "volume_anomaly": bool(row.get("volume_anomaly")),
                "rank": int(row.get("rank") or 0),
                "research_score": round(cls._float(row.get("research_score")), 4),
                "score_components": dict(row.get("score_components") or {}),
                "selection_reason": str(row.get("selection_reason") or ""),
                "risk_explanation": str(row.get("risk_explanation") or ""),
                "comparison_explanation": str(row.get("comparison_explanation") or ""),
                "announcements": causal_announcements,
                "quote_fresh": True,
                "market_timestamp": str(row.get("market_timestamp") or ""),
                "research_status": str(row.get("premarket_status") or "UNKNOWN"),
                "evidence_status": "已验证（公司公告）" if causal_announcements else "已验证" if validated else "证据待补充",
                "evidence_summary": (
                    f"已关联 {first_party_count} 条公司公告/董秘第一方证据："
                    + "；".join(first_party_quotes)
                    if first_party_count
                    else (
                        f"已关联 {claim_count} 条本地有效证据；来源类型需回查"
                        if validated and claim_count else "暂无本地证据"
                    )
                ),
                "evidence_reason": (
                    "公司公告为第一方事实，已通过日期与公司实体映射；仅按标题明确语义确认催化"
                    if causal_announcements
                    else "第一方证据已落库，并通过公司实体映射"
                    if first_party_count
                    else "证据已通过状态、时效与公司实体映射门禁"
                    if validated and claim_count
                    else (
                        "已有 Claim，但未通过 ACTIVE/TRUSTED、时效或公司实体映射门禁"
                        if claim_count
                        else "未关联可定位的公司公告或董秘公开信息 Claim"
                    )
                ),
                "catalyst": str(row.get("catalyst") or "待第一方来源核验"),
                "risk_condition": "板块强度收缩、相对强度转弱或第一方证据被更正",
                "next_day_check": "验证主题上涨家数、核心相对强度及新增公告/董秘信息",
            })
        selected_codes = {cls._code(row.get("symbol")) for row in watchlist}
        official_announcement_watchlist = []
        for row in cls._rank_research_candidates(
            [
                row
                for row in fresh
                if row.get("announcement_titles")
                and cls._code(row.get("symbol")) not in selected_codes
                and strong_theme_names.intersection(row.get("theme_names") or [])
            ],
            limit=30,
        ):
            causal_announcements = list(row.get("causal_announcements") or [])
            if not causal_announcements:
                continue
            official_announcement_watchlist.append({
                "symbol": str(row.get("symbol") or ""),
                "stock_name": str(row.get("stock_name") or "未知"),
                "theme_name": "、".join(row.get("theme_names") or []),
                "pct_chg": round(cls._float(row.get("pct_chg")), 2),
                "research_score": round(cls._float(row.get("research_score")), 4),
                "announcement_titles": [
                    str(item.get("title") or "") for item in causal_announcements
                    if str(item.get("title") or "")
                ],
                "announcements": causal_announcements,
                "catalyst": str(row.get("catalyst") or "官方公告内容方向待解析"),
                "support_label": str(row.get("support_label") or "暂无数据"),
                "support_price": row.get("support_price"),
                "note": "有近期直接相关公司公告，但综合研究分未进入前五",
            })
        return {
            "market_review": market_review,
            "theme_strength": selected_themes,
            "theme_candidate_rankings": theme_candidate_rankings,
            "close_watchlist": watchlist,
            "official_announcement_watchlist": official_announcement_watchlist,
            "next_day_checks": [
                "主题上涨家数是否继续扩散，而非仅由单一公司拉动",
                "核心公司相对强度是否延续，板块平均涨跌幅是否转弱",
                "是否出现新的公司公告或董秘公开信息支持当日催化",
                "高波动公司是否出现事实更正、风险公告或强度快速退潮",
            ],
            "chart_data": [
                {"label": row["theme_name"], "value": row["average_pct_chg"]}
                for row in themes[:10]
            ],
        }

    @classmethod
    def _is_near_price_limit(cls, row: dict[str, Any], direction: int) -> bool:
        code = cls._code(row.get("symbol") or row.get("stock_code"))
        name = str(row.get("stock_name") or row.get("name") or "").upper()
        if "ST" in name:
            threshold = 4.8
        elif code.startswith(("300", "301", "688")):
            threshold = 19.5
        elif code.startswith(("4", "8", "92")):
            threshold = 29.5
        else:
            threshold = 9.5
        change = cls._float(row.get("pct_chg"))
        return change >= threshold if direction > 0 else change <= -threshold

    @staticmethod
    def _session_analysis_markdown(payload: dict[str, Any], *, title: str) -> str:
        review = payload["market_review"]

        def amount(value: Any) -> str:
            if value in (None, ""):
                return "暂无"
            number = ResearchMonitor._float(value)
            return f"{number / 100000000:.2f}亿" if abs(number) >= 100000000 else f"{number / 10000:.2f}万"

        score_weights = {
            "daily_strength": ("当日强度", 0.30),
            "persistence": ("近5日", 0.20),
            "catalyst": ("催化", 0.20),
            "evidence_quality": ("证据", 0.15),
            "turnover_quality": ("换手", 0.10),
            "volume_confirmation": ("量能", 0.05),
        }

        def score_detail(row: dict[str, Any]) -> str:
            components = row.get("score_components") or {}
            return "；".join(
                f"{label}{ResearchMonitor._float(components.get(key)):.2f}×{weight:.0%}="
                f"{ResearchMonitor._float(components.get(key)) * weight:.3f}"
                for key, (label, weight) in score_weights.items()
            )

        lines = [
            f"# AquaTrade {title} {payload['as_of'][:10]}",
            "",
            f"- 数据质量：{(payload.get('data_quality') or {}).get('message', '未评估')}",
            "", "## 今日市场回顾", "",
            f"- 统计口径：{review['scope']}",
            f"- 市场覆盖：{review['fresh_quote_count']}/{review['market_symbol_count']}；范围标记 {review['universe_scope']}",
            f"- 有效样本：{review['sample_count']} 个；上涨 {review['rising_count']} 个；下跌 {review['falling_count']} 个；平均涨跌幅 {review['average_pct_chg']:.2f}%",
            f"- 近涨停 {review['near_limit_up_count']} 个；近跌停 {review['near_limit_down_count']} 个；已剔除过期行情 {review['stale_excluded_count']} 个",
            "", "## 外围市场（截至复盘时点）", "",
        ]
        for row in payload.get("external_markets") or []:
            lines.append(
                f"- {row['name']}：{row['pct_chg']:+.2f}%｜{row['as_of'] or '时间未知'}｜{row['source']}"
            )
        if not payload.get("external_markets"):
            status = payload.get("external_market_status") or {}
            lines.append(f"- 暂无可用外围指数：{status.get('error') or '上游未返回数据'}")
        flow = payload.get("fund_flow") or {}
        lines.extend(["", "## 主力资金流", ""])
        for row in flow.get("leaders") or []:
            lines.append(
                f"- {row['board_name']}：{amount(row['main_net_inflow'])}｜"
                f"{row['provider']}｜{row.get('methodology') or '供应商口径'}｜"
                f"{row.get('as_of') or '时间未知'}"
            )
        if not flow.get("leaders"):
            lines.append(f"- 暂无可用资金流：{flow.get('error') or '上游未返回数据'}")
        lines.extend([
            "", "## 强势主题与涨幅最高公司", "",
        ])
        for row in payload["theme_strength"]:
            lines.append(
                f"- {row['theme_name']}：成员 {row['member_count']}；"
                f"上涨 {row['rising_count']} / 下跌 {row['falling_count']}；"
                f"平均 {row['average_pct_chg']:+.2f}%；强度 {row['strength_score']:.4f}；"
                f"涨幅最高 {row['core_company']} {row['core_pct_chg']:+.2f}%；"
                f"主力净流入 {amount(row.get('main_net_inflow'))}。"
            )
        if not payload["theme_strength"]:
            lines.append("- 暂无可用主题数据。")
        lines.extend([
            "", "## 研究观察优先级说明", "",
            "- 该分数用于安排研究顺序，不是收益预测。",
            "- 公式：当日强度30% + 近5日20% + 催化20% + 证据15% + 换手10% + 量能5%。",
            "- 催化分衡量事件类型；证据分衡量来源质量，二者含义不同。",
            "", "## 板块内全成员比较（每板块前10）", "",
        ])
        for theme in payload.get("theme_candidate_rankings") or []:
            lines.append(f"### {theme['theme_name']}（已比较 {theme['member_count']} 个成员）")
            for row in theme.get("ranked_members") or []:
                lines.append(
                    f"- 第{row['rank']}名 {row.get('stock_name') or row.get('symbol')}："
                    f"研究观察优先级 {row['research_score']:.4f}；{score_detail(row)}；"
                    f"{row['selection_reason']}；{row['comparison_explanation']}；{row['risk_explanation']}。"
                )
            lines.append("")
        if not payload.get("theme_candidate_rankings"):
            lines.append("- 暂无可比较的本地结构化主题成员。")
        lines.extend(["", "## 数据图表", "", "```text"])
        for row in payload["theme_strength"][:8]:
            width = max(0, min(20, int(round(max(0.0, row["average_pct_chg"]) * 2))))
            lines.append(f"{row['theme_name']:<10} {'█' * width} {row['average_pct_chg']:.2f}%")
        if not payload["theme_strength"]:
            lines.append("暂无可用数据")
        lines.extend([
            "```", "", "## 尾盘研究观察池", "",
        ])
        for row in payload["close_watchlist"]:
            support = row.get("technical_reference") or row.get("support_label") or "暂无数据"
            lines.extend([
                f"### {row['rank']}. {row['stock_name']}（{row['symbol']}）｜{row['theme_name']}",
                f"- 行情：当日 {row['pct_chg']:+.2f}%；近5日 {row['five_session_pct']:+.2f}%；换手 {row['turnover_rate']:.2f}%",
                f"- 技术参考：{support}",
                f"- 主力净流入：{amount(row.get('main_net_inflow'))}",
                f"- 研究观察优先级：{row['research_score']:.4f}（仅用于研究排序）",
                f"- 分项贡献：{score_detail(row)}",
                "",
            ])
        if not payload["close_watchlist"]:
            lines.append("- 暂无满足新鲜度条件的观察对象。")
        lines.extend(["", "## 入选、落选与风险解释", ""])
        for row in payload["close_watchlist"]:
            lines.append(
                f"- {row['stock_name']}：{row['selection_reason']}；{row['comparison_explanation']}；风险观察：{row['risk_explanation']}。"
            )
        if not payload["close_watchlist"]:
            lines.append("- 暂无可解释排名结果。")
        lines.extend(["", "## 催化与证据", ""])
        for row in payload["close_watchlist"]:
            lines.append(
                f"- {row['stock_name']}（{row['symbol']}）：{row['catalyst']}；{row['evidence_summary']}。证据说明：{row['evidence_reason']}。风险/失效条件：{row['risk_condition']}。次日验证：{row['next_day_check']}。"
            )
        if not payload["close_watchlist"]:
            lines.append("- 暂无本地证据。")
        lines.extend(["", "## 官方公告补充观察（未进前五）", ""])
        official_watchlist = payload.get("official_announcement_watchlist") or []
        for row in official_watchlist:
            titles = "；".join(row.get("announcement_titles") or [])
            support = str(row.get("support_label") or "暂无数据")
            if row.get("support_price") not in (None, ""):
                support += f" {ResearchMonitor._float(row.get('support_price')):.2f}"
            lines.append(
                f"- {row.get('stock_name')}（{row.get('symbol')}）｜{row.get('theme_name')}｜"
                f"当日 {ResearchMonitor._float(row.get('pct_chg')):.2f}%｜{support}｜{titles}。"
                f"说明：{row.get('note')}；公告事实有效，不自动推断业绩方向。"
            )
        if not official_watchlist:
            lines.append("- 暂无未进前五但具有 PIT 有效公司公告的对象。")
        lines.extend(["", "## 公告与新闻头条", ""])
        evidence_headlines = []
        for row in [*payload["close_watchlist"], *official_watchlist]:
            for announcement in row.get("announcements") or []:
                evidence_headlines.append(
                    f"- {row['stock_name']}｜{announcement.get('date') or '日期未知'}｜{announcement.get('title')}｜{announcement.get('source') or '来源未知'}｜{announcement.get('url') or '暂无链接'}"
                )
        lines.extend(evidence_headlines or ["- 暂无已完成公司映射的本地公告或新闻证据。"])
        lines.extend(["", "## 前热板块量能异动", ""])
        for row in payload.get("volume_anomalies") or []:
            lines.append(
                f"- {row.get('stock_name') or row.get('symbol')}｜{row.get('theme_name')}｜"
                f"量能为近5日均量 {ResearchMonitor._float(row.get('volume_vs_ma5')):.2f} 倍"
            )
        if not payload.get("volume_anomalies"):
            lines.append("- 暂未检测到前热板块量能异动。")
        lines.extend(["", "## 昨日观察复核与自学习", ""])
        for row in payload.get("previous_mentions") or []:
            current = row.get("current_pct_chg")
            change = f"{ResearchMonitor._float(current):+.2f}%" if current is not None else "待验证"
            label = f"｜{row['learning_label']}" if row.get("learning_label") else ""
            lines.append(f"- {row['stock_name']}：{change}｜{row['outcome']}{label}")
        if not payload.get("previous_mentions"):
            lines.append("- 暂无昨日观察对象可复核。")
        if payload.get("ai_summary"):
            lines.extend(["", "## AI 短总结", "", f"- {payload['ai_summary']}"])
        lines.extend(["", "## 次日验证项", ""])
        lines.extend(f"- {item}" for item in payload["next_day_checks"])
        lines.extend(["", "仅用于研究观察与事实验证，不构成投资建议。"])
        return "\n".join(lines) + "\n"

    def _load_theme_components(self) -> tuple[Any, Any]:
        if self._theme_engine is None or self._theme_review_processor is None:
            from server.routes.theme_routes import (
                _get_theme_engine,
                _get_theme_review_processor,
            )

            self._theme_engine = self._theme_engine or _get_theme_engine()
            self._theme_review_processor = (
                self._theme_review_processor or _get_theme_review_processor()
            )
        return self._theme_engine, self._theme_review_processor

    # ------------------------------------------------------------------
    # P6 组件懒初始化
    # ------------------------------------------------------------------

    @property
    def market_session_machine(self) -> Any:
        """市场时段状态机（懒初始化）。"""
        if self._market_session_machine is None:
            from core.runtime.market_session import MarketSessionStateMachine

            self._market_session_machine = MarketSessionStateMachine()
        return self._market_session_machine

    @property
    def quote_freshness_checker(self) -> Any:
        """行情新鲜度校验器（懒初始化）。"""
        if self._quote_freshness_checker is None:
            from core.runtime.quote_freshness import QuoteFreshnessChecker

            self._quote_freshness_checker = QuoteFreshnessChecker()
        return self._quote_freshness_checker

    @property
    def price_anomaly_detector(self) -> Any:
        """价格异动检测器（懒初始化）。"""
        if self._price_anomaly_detector is None:
            from core.runtime.market_session import PriceAnomalyDetector

            self._price_anomaly_detector = PriceAnomalyDetector()
        return self._price_anomaly_detector

    @property
    def push_dedup_store(self) -> Any | None:
        """推送去重存储（懒初始化，失败时返回 None 降级）。"""
        if self._push_dedup_store is None:
            try:
                from core.runtime.push_dedup import PushDedupStore

                self._push_dedup_store = PushDedupStore()
            except Exception as exc:
                logger.warning("push dedup store init failed: %s", exc)
                self._push_dedup_store = None
        return self._push_dedup_store

    def _resolve_market_session(self, as_of: str) -> str:
        """从 as_of 时间戳解析当前市场时段。

        Args:
            as_of: ISO 时间戳。

        Returns:
            时段字符串（如 ``morning`` / ``lunch_break``），解析失败时
            返回 ``unknown``。
        """
        try:
            now = datetime.fromisoformat(as_of)
        except Exception:
            return "unknown"
        try:
            session = self.market_session_machine.current_session(now)
            return session.value
        except Exception:
            return "unknown"

    def _build_research_runtime(self, *, as_of: str, task_context: dict[str, Any] | None = None) -> Any:
        """Return the one monitor runtime used by all dynamic-theme tasks."""
        factory = self._research_runtime_factory
        if factory is None:
            from core.research.control_plane.runtime_factory import ResearchRuntimeFactory

            factory = ResearchRuntimeFactory(
                self.repository,
                output_dir=self.output_dir / "research_cache",
                company_ir_resolver=self._company_ir_resolver,
            )
            self._research_runtime_factory = factory
        builder = getattr(factory, "build", None)
        if callable(builder):
            return builder(as_of=as_of, task_context=task_context or {})
        if callable(factory):
            return factory(as_of=as_of, task_context=task_context or {})
        raise TypeError("research runtime factory must be callable or expose build()")

    def _recover_stale_dynamic_tasks(self, *, as_of: str, runtime: Any, errors: list[str]) -> int:
        """Queue stale dynamic-theme tasks after restoring their checkpoints."""
        resumer = getattr(runtime, "task_resumer", None)
        if resumer is None:
            return 0
        list_resumable = getattr(resumer, "list_resumable_tasks", None)
        resume = getattr(resumer, "resume_task", None)
        if not callable(list_resumable) or not callable(resume):
            return 0
        recovered = 0
        try:
            candidates = list(list_resumable() or [])
        except Exception as exc:
            errors.append(f"stale research task scan failed: {exc}")
            return 0
        for task in candidates:
            if task.params.get("workflow") != "dynamic_theme_review":
                continue
            try:
                resume(task)
                recovered += 1
            except Exception as exc:
                errors.append(f"stale research task recovery failed({task.task_id}): {exc}")
        return recovered

    def _recover_dynamic_tasks_at_startup(self, *, as_of: str) -> tuple[int, list[str]]:
        """在 monitor/close 入口先恢复可续跑的动态主题任务。"""
        errors: list[str] = []
        try:
            runtime = self._build_research_runtime(as_of=as_of)
            recovered = self._recover_stale_dynamic_tasks(
                as_of=as_of, runtime=runtime, errors=errors
            )
        except Exception as exc:
            errors.append(f"research task runtime init failed: {exc}")
            recovered = 0
        return recovered, errors

    def _review_packets(
        self, reviewer: Any, packets: list[Any], as_of: str, errors: list[str]
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for packet in packets:
            try:
                result = reviewer.review(packet, as_of=as_of)
                output.append({
                    "packet": packet,
                    "enhanced": result.enhanced_packet,
                    "summary": result.review_summary,
                })
            except Exception as exc:
                errors.append(f"theme review failed({packet.theme_name}): {exc}")
        return output

    def _execute_research_tasks(
        self,
        *,
        as_of: str,
        blocked_theme_ids: set[str],
        active_theme_ids: set[str],
        active_theme_priorities: dict[str, float],
        max_tasks: int,
        errors: list[str],
        runtime: Any | None = None,
    ) -> list[dict[str, Any]]:
        if runtime is None:
            try:
                runtime = self._build_research_runtime(as_of=as_of)
            except Exception as exc:
                errors.append(f"research task runtime init failed: {exc}")
                return []
        tasks = [
            task for task in self.repository.list_tasks(limit=500)
            if task.status in {"queued", "retrying"}
            and task.params.get("workflow") == "dynamic_theme_review"
        ]
        for task in tasks:
            if task.params.get("theme_id") in blocked_theme_ids:
                task.status = "cancelled"
                task.finished_at = as_of
                task.failure = {
                    "code": "THEME_BLOCKING_NEGATIVE",
                    "message": "主题存在阻断性负面信息，取消正向补证",
                }
                self.repository.save_task(task)
        tasks = [
            task for task in tasks if task.params.get("theme_id") not in blocked_theme_ids
            and task.params.get("theme_id") in active_theme_ids
        ]
        if not tasks or max_tasks <= 0:
            return []
        orchestrator = getattr(runtime, "orchestrator", None)
        if orchestrator is None:
            errors.append("research task runtime init failed: orchestrator missing")
            return []

        output: list[dict[str, Any]] = []
        seen_claim_ids: set[str] = set()
        for task in sorted(
            tasks,
            key=lambda item: -(
                10.0 * float(active_theme_priorities.get(
                    str(item.params.get("theme_id") or ""), 0.0
                ))
                + float(item.priority or 0.0)
            ),
        )[:max_tasks]:
            claim_id = str(task.target_claim_id or "")
            if claim_id and claim_id in seen_claim_ids:
                continue
            if claim_id:
                seen_claim_ids.add(claim_id)
            task.params = dict(task.params or {})
            task.params.setdefault("as_of", as_of)
            if self._official_ir_urls and not task.params.get("official_ir_urls"):
                task.params["official_ir_urls"] = list(self._official_ir_urls)
            if self._company_ir_urls and not task.params.get("company_ir_urls"):
                task.params["company_ir_urls"] = list(self._company_ir_urls)
            started = time.time()
            try:
                summary = orchestrator.execute(task)
                output.append({
                    **summary.__dict__,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "symbol": task.params.get("symbol", ""),
                    "theme_name": task.params.get("theme_name", ""),
                })
            except Exception as exc:
                errors.append(f"research task failed({task.task_id}): {exc}")
        return output

    def _load_daily_data(
        self, packets: list[Any], *, end_date: str, errors: list[str]
    ) -> dict[str, pd.DataFrame]:
        if not end_date:
            return {}
        symbols = sorted({
            member.symbol for packet in packets for member in packet.core_members
            if member.symbol
        })
        start = (
            datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=120)
        ).strftime("%Y-%m-%d")
        output: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                frame = self.data_query.get_stock_history(
                    symbol, start, end_date,
                    columns=["trade_date", "stock_code", "close", "volume", "amount"],
                )
                if frame is not None and not frame.empty:
                    output[symbol] = frame
            except Exception as exc:
                errors.append(f"daily history failed({symbol}): {exc}")
        return output

    def _run_theme_quant(
        self,
        packets: list[Any],
        *,
        daily_data_map: dict[str, pd.DataFrame],
        as_of: str,
        errors: list[str],
    ) -> Any:
        if not packets:
            return None
        try:
            from core.quant.pipeline import QuantPipeline
            if self._persist_quant_signals and self._quant_repository is None:
                from core.quant.repository import QuantRepository

                self._quant_repository = QuantRepository()

            return QuantPipeline(
                research_repository=self.repository,
                repository=(
                    self._quant_repository
                    if self._persist_quant_signals
                    else None
                ),
            ).run_with_themes(
                packets,
                current_date=as_of,
                daily_data_map=daily_data_map,
                enforce_research_gate=True,
            )
        except Exception as exc:
            errors.append(f"theme quant pipeline failed: {exc}")
            return None

    def _build_observations(
        self, reviews: list[dict[str, Any]], quant_result: Any
    ) -> list[dict[str, Any]]:
        screening_by_symbol_theme: dict[tuple[str, str], Any] = {}
        if quant_result is not None:
            for item in quant_result.theme_screening_results:
                screening_by_symbol_theme[(item.symbol, item.theme_id)] = item
        output: list[dict[str, Any]] = []
        for item in reviews:
            packet = item["enhanced"]
            summary = item["summary"]
            core_by_symbol = {
                member.symbol: member for member in packet.core_members
            }
            core_symbols = set(core_by_symbol)
            exploration = sorted(
                [member for member in packet.members if member.symbol not in core_symbols],
                key=lambda member: -self._observation_score(member),
            )
            # Research-validated core members are never truncated from the
            # monitoring set. Exploration rows only fill the remaining slots.
            ranked = list(packet.core_members) + exploration[
                : max(0, 6 - len(packet.core_members))
            ]
            for original_member in ranked:
                member = core_by_symbol.get(original_member.symbol, original_member)
                screening = screening_by_symbol_theme.get((member.symbol, packet.theme_id))
                validated = member.symbol in core_symbols
                if summary.get("research_gate_status") == "rejected" or packet.negative_risk_score >= 0.7:
                    status = "REJECTED"
                elif screening is not None:
                    status = screening.status
                elif validated:
                    status = "WATCH"
                else:
                    status = "UNKNOWN"
                output.append({
                    "theme_id": packet.theme_id,
                    "theme_run_id": packet.theme_run_id,
                    "theme_name": packet.theme_name,
                    "theme_type": packet.theme_type,
                    "theme_phase": packet.phase,
                    "symbol": member.symbol,
                    "company_name": member.company_name,
                    "status": status,
                    "research_validated": validated,
                    "claim_ids": list(member.research_claim_ids),
                    "claim_status": member.research_claim_status,
                    "membership_confidence": round(float(member.membership_confidence or 0.0), 4),
                    "mapping_confidence": round(float(member.mapping_confidence or 0.0), 4),
                    "market_role": member.market_role,
                    "market_features": dict(member.market_features or {}),
                    "technical_state": (
                        (screening.technical_state or (
                            member.market_features or {}
                        ).get("technical_state", "UNKNOWN"))
                        if screening is not None
                        else (member.market_features or {}).get("technical_state", "UNKNOWN")
                    ),
                    "final_score": round(
                        float(screening.final_score if screening is not None else 0.0), 4
                    ),
                    "degradation_reasons": list(packet.degradation_reasons),
                    "source_snapshot_id": packet.source_snapshot_id,
                    "strategy_version": packet.strategy_version,
                })
        return sorted(
            output,
            key=lambda row: (
                {"READY": 0, "WATCH": 1, "UNKNOWN": 2, "REJECTED": 3}.get(row["status"], 4),
                -float(row.get("final_score") or 0.0),
                -float((row.get("market_features") or {}).get("leadership_score") or 0.0),
            ),
        )

    def _observation_score(self, member: Any) -> float:
        market = member.market_features or {}
        state = str(market.get("technical_state") or "NEUTRAL")
        return (
            0.55 * self.FAVORABLE_STATE_SCORES.get(state, 0.35)
            + 0.30 * float(market.get("leadership_score") or 0.0)
            + 0.15 * float(member.membership_confidence or 0.0)
        )

    @staticmethod
    def _theme_runtime_priority(packet: Any) -> float:
        type_score = {
            "market_discovery": 1.0,
            "research_discovery": 0.75,
            "news_discovery": 0.75,
            "ice_reversal": 0.70,
            "legacy_monitor": 0.60,
        }.get(str(packet.theme_type or ""), 0.50)
        phase_score = {
            "STARTUP": 0.30,
            "MAIN_WAVE": 0.25,
            "REVERSAL_CONFIRMING": 0.20,
            "ICE": 0.05,
            "UNKNOWN": 0.0,
        }.get(str(packet.phase or ""), 0.0)
        catalyst_score = min(0.15, 0.05 * len(packet.catalysts or []))
        return round(type_score + phase_score + catalyst_score, 4)

    def _resolve_calendar(self, as_of: str) -> dict[str, Any]:
        date = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(MARKET_TZ).date()
        start = date - timedelta(days=20)
        source = "weekday_fallback"
        degraded = True
        open_dates: list[str] = []
        try:
            from config.config import Config
            import tushare as ts

            token = getattr(Config, "TUSHARE_TOKEN", "")
            if token:
                frame = ts.pro_api(token).trade_cal(
                    exchange="SSE",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=date.strftime("%Y%m%d"),
                )
                open_dates = sorted(
                    frame[frame["is_open"] == 1]["cal_date"].astype(str).map(
                        lambda value: f"{value[:4]}-{value[4:6]}-{value[6:8]}"
                    ).tolist()
                )
                source = "tushare"
                degraded = False
        except Exception as exc:
            logger.warning("Tushare calendar unavailable: %s", exc)
        if not open_dates:
            cursor = start
            while cursor <= date:
                if cursor.weekday() < 5:
                    open_dates.append(cursor.isoformat())
                cursor += timedelta(days=1)
        date_text = date.isoformat()
        previous = [item for item in open_dates if item < date_text]
        return {
            "date": date_text,
            "is_open": date_text in open_dates,
            "pretrade_date": previous[-1] if previous else "",
            "source": source,
            "degraded": degraded,
        }

    def _market_data_health(self, pretrade_date: str) -> dict[str, Any]:
        if not pretrade_date:
            return {"usable": False, "degraded": True, "error": "pretrade date missing"}
        try:
            frame = self.data_query.get_stock_pool(pretrade_date)
            count = 0 if frame is None else len(frame)
            return {
                "usable": count >= 3000,
                "degraded": count < 4500,
                "market_trade_date": pretrade_date,
                "row_count": count,
                "error": "" if count else "daily stock pool empty",
            }
        except Exception as exc:
            return {"usable": False, "degraded": True, "error": str(exc)}

    def _theme_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        packet = item["enhanced"]
        summary = item["summary"]
        # 拆分 validated_core_count 以区分"证据未通过"与"角色未通过"：
        # - policy_validated_count: 通过 Claim/Evidence policy 的成员数（不论角色）
        # - core_role_count: 通过 policy 且 market_role ∈ {core, leader} 的成员数
        # - validated_core_count: 向后兼容别名，等价于 core_role_count
        member_reviews = summary.get("member_reviews", []) or []
        policy_validated_count = sum(
            1 for r in member_reviews
            if isinstance(r, dict) and r.get("status") == "validated"
        )
        core_role_count = sum(
            1 for r in member_reviews
            if isinstance(r, dict)
            and r.get("status") == "validated"
            and r.get("market_role") in {"core", "leader"}
        )
        return {
            "theme_id": packet.theme_id,
            "theme_run_id": packet.theme_run_id,
            "theme_name": packet.theme_name,
            "theme_type": packet.theme_type,
            "phase": packet.phase,
            "phase_confidence": round(float(packet.phase_confidence or 0.0), 4),
            "market_features": packet.market_features,
            "negative_risk_score": round(float(packet.negative_risk_score or 0.0), 4),
            "negative_status": summary.get("negative_status", "unknown"),
            "research_gate_status": summary.get("research_gate_status", "pending"),
            "member_count": len(packet.members),
            "policy_validated_count": policy_validated_count,
            "core_role_count": core_role_count,
            "validated_core_count": core_role_count,  # 向后兼容别名
            "source_snapshot_id": packet.source_snapshot_id,
            "source_status": packet.source_status,
            "degraded": packet.degraded,
            "degradation_reasons": list(dict.fromkeys(packet.degradation_reasons)),
            "generated_task_ids": summary.get("generated_task_ids", []),
        }

    @staticmethod
    def _quant_payload(result: Any) -> dict[str, Any]:
        if result is None:
            return {"signals": [], "screening": [], "errors": ["not_run"]}
        return {
            "signals": [item.to_dict() for item in result.signals],
            "screening": [item.__dict__ for item in result.theme_screening_results],
            "admission": {
                key: value.__dict__ for key, value in result.admission_results.items()
            },
            "errors": list(result.errors),
            "degraded": result.degraded,
            "saved_count": int(getattr(result, "saved_count", 0) or 0),
            # 候选生成漏斗诊断，用于工作台"为什么没有候选"结构化端点
            "candidate_funnel": getattr(result, "candidate_funnel", {}) or {},
        }

    def _premarket_markdown(self, payload: dict[str, Any]) -> str:
        lines = [
            f"# AquaTrade 盘前研究复盘 {payload['as_of'][:10]}",
            "",
            f"- 研究时点：{payload['as_of']}",
            f"- 上一完整交易日：{payload['calendar'].get('pretrade_date') or '未知'}",
            f"- 行情覆盖：{payload['health'].get('row_count', 0)} 只",
            f"- 动态主题：{len(payload['themes'])} 个",
            f"- Research 已验证观察：{payload['validated_observation_count']} 个",
            f"- READY/WATCH：{payload.get('ready_watch_count', 0)} 个",
            f"- 运行状态：{payload['status']}",
            "",
            "## 动态主题",
            "",
            "| 主题 | 类型 | 阶段 | 阶段置信度 | 负面状态 | Research 门禁 | 验证核心数 |",
            "|---|---|---|---:|---|---|---:|",
        ]
        for theme in payload["themes"]:
            lines.append(
                f"| {theme['theme_name']} | {theme['theme_type']} | {theme['phase'] or 'UNKNOWN'} | "
                f"{theme['phase_confidence']:.4f} | {theme['negative_status']} | "
                f"{theme['research_gate_status']} | {theme['validated_core_count']} |"
            )
        lines.extend([
            "",
            "## Research 验证核心（日线状态）",
            "",
            "| 状态 | 证券 | 公司 | 主题 | 技术状态 | Claim 状态 | 领导分数 | 5日/20日 |",
            "|---|---|---|---|---|---|---:|---:|",
        ])
        validated_rows = [
            row for row in payload["observations"] if row.get("research_validated")
        ]
        for row in validated_rows:
            market = row.get("market_features") or {}
            lines.append(
                f"| {row['status']} | {row['symbol']} | {row.get('company_name') or '暂无本地证据'} | "
                f"{row['theme_name']} | {row['technical_state']} | "
                f"{row.get('claim_status') or 'ACTIVE'} | "
                f"{float(market.get('leadership_score') or 0.0):.4f} | "
                f"{float(market.get('ret5') or 0.0) * 100:.2f}% / "
                f"{float(market.get('ret20') or 0.0) * 100:.2f}% |"
            )
        if not validated_rows:
            lines.append("| 暂无 | - | 暂无本地证据 | - | - | - | 0.0000 | 0.00% / 0.00% |")
        lines.extend([
            "",
            "## 探索观察（证据未闭环）",
            "",
            "| 证券 | 公司 | 主题 | 技术状态 | 研究状态 |",
            "|---|---|---|---|---|",
        ])
        exploration_rows = [
            row for row in payload["observations"] if not row.get("research_validated")
        ]
        for row in exploration_rows[:20]:
            lines.append(
                f"| {row['symbol']} | {row.get('company_name') or '暂无本地证据'} | "
                f"{row['theme_name']} | {row['technical_state']} | {row['status']} |"
            )
        lines.extend([
            "",
            "## 研究任务",
            "",
        ])
        if payload["research_task_runs"]:
            for task in payload["research_task_runs"]:
                lines.append(
                    f"- {task.get('symbol') or '未知'} / {task.get('theme_name') or '未知'}："
                    f"{task.get('status')}，新增证据 {task.get('evidence_saved', 0)} 条，"
                    f"{task.get('stopped_reason') or '无停止原因'}"
                )
        else:
            lines.append("- 本轮无可执行补证任务。")
        if payload["errors"]:
            lines.extend(["", "## 降级与错误", ""])
            lines.extend(f"- {error}" for error in payload["errors"])
        lines.extend([
            "",
            "---",
            "状态仅用于研究观察与后续验证，不构成买入、卖出、仓位、止盈或止损建议。",
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _monitor_markdown(payload: dict[str, Any]) -> str:
        """生成手机友好的盘中监控 markdown。

        优化点：
        - 开头展示 A 股核心指数 + 外围市场（韩国 KOSPI、日经 225）涨跌幅
        - 个股表格精简为 4 列（证券公司、主题、涨跌幅、状态），适配手机窄屏
        - 主题字段截断到首个，避免多主题拼接撑爆表格
        - 涨跌幅带 emoji 箭头，状态用简短中文
        """
        as_of = str(payload.get("as_of") or "")
        lines = [f"# 盘中监控 {as_of[11:16] or as_of}", ""]

        # 指数概览块
        indices_block = ResearchMonitor._format_market_indices_block(
            payload.get("market_indices") or {}
        )
        if indices_block:
            lines.extend(indices_block)
            lines.append("")

        snapshot_count = int(payload.get("snapshot_count", 0))
        stale_count = int(payload.get("stale_quote_count", 0))
        lines.append(f"监控样本 {snapshot_count} 只，过期 {stale_count} 只")
        lines.append("")

        # 个股表（4 列，手机友好）
        lines.extend([
            "| 标的 | 主题 | 涨跌 | 状态 |",
            "|---|---|---:|---|",
        ])
        snapshots = payload.get("snapshots") or []
        # 按涨跌幅排序，跌幅大的排前（监控风险优先）
        sorted_snapshots = sorted(
            snapshots,
            key=lambda r: float(r.get("pct_chg") or 0.0),
        )
        for row in sorted_snapshots:
            symbol = str(row.get("symbol") or "")
            name = str(row.get("stock_name") or "未知").strip()
            # 主题取第一个，避免多主题撑爆表格
            theme_raw = str(row.get("theme_name") or "")
            theme = theme_raw.split("、")[0] if theme_raw else "-"
            pct = float(row.get("pct_chg") or 0.0)
            # 涨跌 emoji
            arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
            state_raw = str(row.get("monitor_state") or "")
            state_text = {
                "TREND_BREAK_RISK": "破位",
                "MA5_PROXIMITY": "贴MA5",
                "INTRADAY_EXTENSION": "异动",
                "STALE_QUOTE": "过期",
                "UNCHANGED": "-",
            }.get(state_raw, state_raw)
            lines.append(
                f"| {symbol} {name} | {theme} | {arrow}{abs(pct):.2f}% | {state_text} |"
            )
        lines.extend(["", "仅为实时研究监控，不构成交易指令。"])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_market_indices_block(indices: dict[str, Any]) -> list[str]:
        """格式化指数概览块（同花顺样式：表格横向布局）。

        布局参考同花顺手机版：
        - A 股市场：上证指数 / 深证成指 / 创业板指 / 科创50（4 列）
        - 外围市场：韩国 KOSPI / 日经 225（2 列，盘中只看这两个亚洲市场）
        - 每列 3 行：名称（标题行）/ 价格+箭头（加粗）/ 涨跌额 涨跌幅（小字）

        Args:
            indices: _fetch_monitor_market_indices 返回的字典。

        Returns:
            markdown 行列表，空数据返回空列表。
        """
        if not indices:
            return []
        lines: list[str] = []
        domestic = indices.get("domestic") or []
        overseas = indices.get("overseas") or []

        def _fmt_amount_pct(item: dict[str, Any]) -> tuple[str, str]:
            """格式化涨跌额和涨跌幅。

            Args:
                item: 指数数据字典。

            Returns:
                (amount_str, pct_str) 元组，如 ("+12.34", "+0.32%")。
            """
            pct = float(item.get("change_pct") or item.get("pct_chg") or 0.0)
            amount = float(item.get("change_amount") or 0.0)
            sign_amt = "+" if amount >= 0 else ""
            sign_pct = "+" if pct >= 0 else ""
            return f"{sign_amt}{amount:.2f}", f"{sign_pct}{pct:.2f}%"

        def _render_table(items: list[dict[str, Any]]) -> list[str]:
            """渲染同花顺样式指数表格。

            Args:
                items: 指数数据列表。

            Returns:
                markdown 行列表（含表头、分隔行、价格行、涨跌行）。
            """
            if not items:
                return []

            def arrow(pct: float) -> str:
                """根据涨跌幅返回箭头符号。"""
                return "↑" if pct > 0 else "↓" if pct < 0 else "→"

            # 第一行：指数名称
            header = "| " + " | ".join(
                f"**{str(item.get('name') or '')}**" for item in items
            ) + " |"
            # 第二行：分隔
            sep = "|" + "|".join(["---:"] * len(items)) + "|"
            # 第三行：价格+箭头（加粗）
            price_row = "| " + " | ".join(
                f"**{float(item.get('price') or 0):.2f}** {arrow(float(item.get('change_pct') or 0))}"
                for item in items
            ) + " |"
            # 第四行：涨跌额 涨跌幅
            chg_row = "| " + " | ".join(
                "{} {}".format(*_fmt_amount_pct(item)) for item in items
            ) + " |"
            return [header, sep, price_row, chg_row]

        # A 股市场（4 列横向）
        if domestic:
            lines.append("**A 股市场**")
            lines.append("")
            lines.extend(_render_table(domestic[:4]))
            lines.append("")

        # 外围市场（2 列，盘中只看 KOSPI + 日经225）
        if overseas:
            lines.append("**外围市场**")
            lines.append("")
            lines.extend(_render_table(overseas[:2]))
        return lines

    def _attach_intraday_charts(self, payload: dict[str, Any]) -> None:
        """为 A 股指数和监控样本附加 SVG 分时图。

        优先使用 TDX 批量接口获取真实分钟数据；TDX 返回脏数据时清洗或丢弃，
        避免把异常价格（如 0.01、数百万）画进图表导致纵轴爆炸。

        Args:
            payload: run_monitor 生成的监控 payload，会被原地修改。
        """
        try:
            from infrastructure.data_providers.market.intraday_chart import (
                build_dual_intraday_svg,
                build_intraday_svg,
                clean_minute_data,
            )
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            registry = ProviderRegistry()
            tdx = registry.providers.get("tdx")
            if tdx is None or not tdx.is_available():
                return

            def _derive_last_close(item: dict[str, Any]) -> float:
                """优先使用 last_close，缺失时根据 price 和涨跌幅反推。"""
                last_close = float(item.get("last_close") or 0)
                if last_close > 0:
                    return last_close
                price = float(item.get("price") or 0)
                pct = float(item.get("change_pct") or item.get("pct_chg") or 0)
                if price > 0 and pct != -100:
                    return price / (1 + pct / 100)
                return 0.0

            # 1. A 股指数分时图
            indices_config = [
                ("000001", 1), ("399001", 0), ("399006", 0), ("000688", 1),
            ]
            index_data_map = tdx.get_intraday_series_batch(indices_config)
            market_indices = payload.get("market_indices") or {}
            name_to_code = {
                "上证指数": "000001", "深证成指": "399001",
                "创业板指": "399006", "科创50": "000688",
                "上证50": "000016", "沪深300": "000300",
            }
            domestic = market_indices.get("domestic") or []
            sh_item: dict[str, Any] | None = None
            sz_item: dict[str, Any] | None = None
            for item in domestic:
                code = str(item.get("code") or "")[:6]
                if not code:
                    code = name_to_code.get(str(item.get("name") or ""), "")
                if code:
                    item["code"] = code
                last_close = _derive_last_close(item)
                data = clean_minute_data(index_data_map.get(code, []), last_close)
                if data:
                    item["chart_svg"] = build_intraday_svg(data, last_close)
                if code == "000001":
                    sh_item = item
                elif code == "399001":
                    sz_item = item

            # 上证 + 深证合并大图
            if sh_item and sz_item:
                sh_close = _derive_last_close(sh_item)
                sz_close = _derive_last_close(sz_item)
                sh_data = clean_minute_data(index_data_map.get("000001", []), sh_close)
                sz_data = clean_minute_data(index_data_map.get("399001", []), sz_close)
                if sh_data and sz_data and sh_close > 0 and sz_close > 0:
                    payload["dual_index_svg"] = build_dual_intraday_svg(
                        sh_data, sh_close, sz_data, sz_close, name_a="上证", name_b="深证"
                    )

            # 2. 个股分时图
            snapshots = payload.get("snapshots") or []
            stock_codes = []
            for s in snapshots:
                code = self._code(s.get("symbol"))
                if code:
                    stock_codes.append((code, _tdx_market(code)))
            if stock_codes:
                stock_data_map = tdx.get_intraday_series_batch(stock_codes)
                for s in snapshots:
                    code = self._code(s.get("symbol"))
                    last_close = _derive_last_close(s)
                    data = clean_minute_data(stock_data_map.get(code, []), last_close)
                    if data:
                        s["chart_svg"] = build_intraday_svg(data, last_close)
        except Exception as exc:
            logger.warning("附加分时图异常: %s", exc)

    @staticmethod
    def _monitor_html(payload: dict[str, Any]) -> str:
        """生成带 SVG 分时图的手机友好 HTML 报告。

        Args:
            payload: run_monitor 生成的监控 payload。

        Returns:
            HTML 字符串，可直接用于 PushPlus html 模板推送。
        """
        as_of = str(payload.get("as_of") or "")
        title_time = as_of[11:16] or as_of

        def _fmt_color(pct: float) -> tuple[str, str]:
            if pct > 0:
                return "rgb(231, 76, 60)", "+"
            if pct < 0:
                return "rgb(39, 174, 96)", ""
            return "rgb(127, 140, 141)", ""

        def _index_card(item: dict[str, Any], *, full_width: bool = False) -> str:
            name = str(item.get("name") or "")
            price = float(item.get("price") or 0)
            pct = float(item.get("change_pct") or item.get("pct_chg") or 0)
            amount = float(item.get("change_amount") or 0)
            color, sign = _fmt_color(pct)
            chart_svg = str(item.get("chart_svg") or "")
            width = "100%" if full_width else "48%"
            return f"""
<div style="width:{width};margin-bottom:10px;box-sizing:border-box;border:1px solid rgb(236,240,241);border-radius:6px;padding:8px;background:rgb(255,255,255);">
  <div style="font-size:13px;color:rgb(44,62,80);font-weight:bold;margin-bottom:4px;white-space:nowrap;">{name}</div>
  <div style="display:flex;align-items:baseline;justify-content:space-between;">
    <div style="font-size:20px;font-weight:bold;color:{color};">{price:.2f}</div>
    <div style="text-align:right;">
      <div style="font-size:12px;color:{color};white-space:nowrap;">{sign}{pct:.2f}%</div>
      <div style="font-size:11px;color:{color};white-space:nowrap;">{sign}{amount:.2f}</div>
    </div>
  </div>
  {chart_svg}
</div>"""

        def _market_sentiment_card(sentiment: dict[str, Any]) -> str:
            up = int(sentiment.get("up_count") or 0)
            down = int(sentiment.get("down_count") or 0)
            total = max(1, up + down)
            up_pct = up / total * 100
            hot = str(sentiment.get("hot_sector") or "-")
            hot_pct = float(sentiment.get("hot_sector_pct") or 0)
            amount = float(sentiment.get("total_amount") or 0)
            # amount_change_pct 需要 historical 对比，get_market_overview 不提供时为 None
            amount_chg_raw = sentiment.get("amount_change_pct")
            has_amount_chg = amount_chg_raw is not None and amount_chg_raw != ""
            amount_chg = float(amount_chg_raw) if has_amount_chg else 0.0
            if amount >= 1e8:
                amount_text = f"{amount / 1e8:.0f}亿"
            elif amount > 0:
                amount_text = f"{amount / 1e4:.1f}万"
            else:
                amount_text = "--"
            if has_amount_chg:
                arrow = "↑" if amount_chg >= 0 else "↓"
                amount_chg_display = f"{arrow}{abs(amount_chg):.2f}%"
                amount_chg_color = "rgb(231,76,60)" if amount_chg >= 0 else "rgb(39,174,96)"
            else:
                amount_chg_display = "—"
                amount_chg_color = "rgb(127,140,141)"
            return f"""
<div style="width:100%;margin-bottom:10px;box-sizing:border-box;border:1px solid rgb(236,240,241);border-radius:8px;padding:10px;background:rgb(255,255,255);">
  <div style="font-size:13px;color:rgb(44,62,80);font-weight:bold;margin-bottom:6px;">市场情绪</div>
  <div style="display:flex;justify-content:space-between;font-size:12px;color:rgb(44,62,80);margin-bottom:6px;">
    <span>涨 <span style="color:rgb(231,76,60);font-weight:bold;">{up if up else '--'}</span></span>
    <span>跌 <span style="color:rgb(39,174,96);font-weight:bold;">{down if down else '--'}</span></span>
  </div>
  <div style="width:100%;height:8px;background:rgb(39,174,96);border-radius:4px;overflow:hidden;margin-bottom:8px;">
    <div style="width:{up_pct:.1f}%;height:100%;background:rgb(231,76,60);"></div>
  </div>
  <div style="font-size:12px;color:rgb(44,62,80);margin-bottom:4px;">热门：{hot} <span style="color:{'rgb(231,76,60)' if hot_pct>=0 else 'rgb(39,174,96)'}">{hot_pct:+.2f}%</span></div>
  <div style="font-size:12px;color:rgb(127,140,141);">成交 {amount_text} <span style="color:{amount_chg_color};">{amount_chg_display}</span></div>
</div>"""

        def _snapshot_row(item: dict[str, Any]) -> str:
            symbol = str(item.get("symbol") or "")
            name = str(item.get("stock_name") or "").strip()
            pct = float(item.get("pct_chg") or 0)
            color, sign = _fmt_color(pct)
            state_raw = str(item.get("monitor_state") or "")
            state_text, state_color = {
                "TREND_BREAK_RISK": ("破位", "rgb(231, 76, 60)"),
                "MA5_PROXIMITY": ("贴MA5", "rgb(243, 156, 18)"),
                "INTRADAY_EXTENSION": ("异动", "rgb(231, 76, 60)"),
                "STALE_QUOTE": ("过期", "rgb(149, 165, 166)"),
                "UNCHANGED": ("-", "rgb(189, 195, 199)"),
            }.get(state_raw, (state_raw, "rgb(127, 140, 141)"))
            theme = str(item.get("theme_name") or "").split("、")[0] or "-"
            chart_svg = str(item.get("chart_svg") or "")
            return f"""
<div style="border-bottom:1px solid rgb(236,240,241);padding:8px 0;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
    <div>
      <span style="font-size:14px;font-weight:bold;color:rgb(44,62,80);">{symbol}</span>
      <span style="font-size:12px;color:rgb(127,140,141);margin-left:4px;">{name}</span>
    </div>
    <div style="text-align:right;">
      <div style="font-size:14px;font-weight:bold;color:{color};white-space:nowrap;">{sign}{abs(pct):.2f}%</div>
      <div style="font-size:11px;color:{state_color};white-space:nowrap;">{state_text} · {theme}</div>
    </div>
  </div>
  {chart_svg}
</div>"""

        market_indices = payload.get("market_indices") or {}
        domestic = market_indices.get("domestic") or []
        overseas = market_indices.get("overseas") or []
        snapshots = payload.get("snapshots") or []
        # 按涨跌幅排序，手机端最多展示 4 个，避免 PushPlus 内容超限
        sorted_snapshots = sorted(snapshots, key=lambda r: float(r.get("pct_chg") or 0))[:4]

        # 上证+深证合并大图（只显示上证数据，深证以虚线叠加）
        sh_item = next((i for i in domestic if str(i.get("code") or "")[:6] == "000001"), None)
        dual_index_svg = str(payload.get("dual_index_svg") or "")
        if dual_index_svg and sh_item:
            pct = float(sh_item.get("change_pct") or sh_item.get("pct_chg") or 0)
            if pct > 0:
                color, sign = "rgb(231, 76, 60)", "+"
            elif pct < 0:
                color, sign = "rgb(39, 174, 96)", ""
            else:
                color, sign = "rgb(127, 140, 141)", ""
            dual_header = f"""
  <div style="font-size:13px;color:rgb(44,62,80);font-weight:bold;margin-bottom:4px;white-space:nowrap;">{sh_item.get('name', '上证指数')}</div>
  <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px;">
    <div style="font-size:20px;font-weight:bold;color:{color};">{float(sh_item.get('price', 0)):.2f}</div>
    <div style="text-align:right;">
      <div style="font-size:12px;color:{color};white-space:nowrap;">{sign}{pct:.2f}%</div>
      <div style="font-size:11px;color:{color};white-space:nowrap;">{sign}{float(sh_item.get('change_amount', 0)):.2f}</div>
    </div>
  </div>"""
            dual_card = f"""
<div style="width:100%;margin-bottom:10px;box-sizing:border-box;border:1px solid rgb(236,240,241);border-radius:6px;padding:8px;background:rgb(255,255,255);">
  {dual_header}
  {dual_index_svg}
</div>"""
        else:
            dual_card = ""

        # 其余指数（创业板、科创50等）按 2x2 小卡片展示
        sz_item = next((i for i in domestic if str(i.get("code") or "")[:6] == "399001"), None)
        small_indices = [i for i in domestic if i not in (sh_item, sz_item)]
        small_index_cards = "".join(_index_card(item) for item in small_indices[:4])

        overseas_cards = "".join(
            f"<div style=\"font-size:12px;color:rgb(44,62,80);\">{o.get('name', '')} <span style=\"color:{'rgb(231,76,60)' if float(o.get('change_pct') or o.get('pct_chg') or 0) >= 0 else 'rgb(39,174,96)'}\">{float(o.get('change_pct') or o.get('pct_chg') or 0):+.2f}%</span></div>"
            for o in overseas[:2]
        )

        snapshot_cards = "".join(_snapshot_row(item) for item in sorted_snapshots)

        stale_count = int(payload.get("stale_quote_count", 0))
        stale_hint = f"，过期 {stale_count} 只" if stale_count else ""

        # 市场情绪：优先使用 payload 中的结构化数据，否则留空占位
        sentiment = payload.get("market_sentiment") or {}
        sentiment_card = _market_sentiment_card(sentiment)

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body {{ color-scheme: light only; -webkit-color-scheme: light only; }}
</style>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:rgb(44,62,80);margin:0;padding:12px;background:transparent;">

<h3 style="font-size:16px;margin:0 0 10px 0;color:rgb(44,62,80);">盘中监控 {title_time}</h3>

{dual_card}

<div style="display:flex;flex-wrap:wrap;justify-content:space-between;">
{small_index_cards}
</div>

{sentiment_card}

<div style="display:flex;justify-content:space-between;align-items:center;margin:8px 0 12px 0;padding:8px;border:1px solid rgb(236,240,241);border-radius:6px;background:rgb(255,255,255);">
  <div style="font-size:12px;color:rgb(127,140,141);">外围市场</div>
  <div style="display:flex;gap:12px;">{overseas_cards}</div>
</div>

<div style="font-size:12px;color:rgb(127,140,141);margin-bottom:8px;">监控样本 {len(snapshots)} 只{stale_hint}</div>

<div style="border:1px solid rgb(236,240,241);border-radius:6px;padding:0 10px;background:rgb(255,255,255);">
{snapshot_cards}
</div>

<p style="font-size:11px;color:rgb(189,195,199);margin-top:12px;text-align:center;">仅为实时研究监控，不构成交易指令。</p>

</body>
</html>"""

    def _fetch_monitor_market_indices_safe(self, *, as_of: str) -> dict[str, Any]:
        """_fetch_monitor_market_indices 的安全包装：任何异常都降级为空字典。

        Args:
            as_of: ISO 时间戳。

        Returns:
            指数快照字典，失败时返回空字典（不影响 monitor 主流程）。
        """
        try:
            return self._fetch_monitor_market_indices(as_of=as_of)
        except Exception as exc:
            logger.debug("获取市场指数失败，降级为空字典: %s", exc)
            return {}

    def _fetch_market_sentiment_safe(self, *, as_of: str) -> dict[str, Any]:
        """从行情 provider 获取市场情绪数据（涨跌家数 + 热门板块 + 总成交额）。

        调用 ProviderRegistry.get_market_overview() 聚合全市场实时行情，
        将 DataFrame 单行结果转换为 _market_sentiment_card 期望的 dict 格式。
        任何异常都降级为空字典，不影响 monitor 主流程。

        Args:
            as_of: ISO 时间戳，用于标注 trade_date。

        Returns:
            {up_count, down_count, hot_sector, hot_sector_pct, total_amount,
             amount_change_pct: None}，失败时返回空字典。
        """
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            registry = ProviderRegistry()
            trade_date = as_of[:10]
            df = registry.get_market_overview(trade_date=trade_date)
            if df is None or df.empty:
                return {}
            row = df.iloc[0]
            return {
                "up_count": int(row.get("up_count") or 0),
                "down_count": int(row.get("down_count") or 0),
                "hot_sector": str(row.get("hot_sector") or ""),
                "hot_sector_pct": float(row.get("hot_sector_pct") or 0),
                "total_amount": float(row.get("total_amount") or 0),
                # amount_change_pct 需要历史对比数据，暂不提供，卡片降级显示 "—"
                "amount_change_pct": None,
            }
        except Exception as exc:
            logger.warning("获取市场情绪数据失败，降级为空字典: %s", exc)
            return {}

    def _fetch_monitor_market_indices(self, *, as_of: str) -> dict[str, Any]:
        """抓取盘中指数快照：A 股核心指数 + 外围市场。

        A 股指数优先用 TDX（本地通达信），失败时降级到 registry。
        外围市场复用 load_external_market_snapshot（Yahoo finance）。

        Args:
            as_of: ISO 时间戳。

        Returns:
            {"domestic": [...], "overseas": [...], "errors": [...]}
        """
        result: dict[str, Any] = {"domestic": [], "overseas": [], "errors": []}

        # 1. A 股核心指数（TDX 优先）
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            registry = ProviderRegistry()
            tdx = registry.providers.get("tdx")
            if tdx and tdx.is_available():
                domestic = tdx.get_index_quotes()
                if domestic:
                    result["domestic"] = domestic
                    return result
        except Exception as exc:
            result["errors"].append(f"tdx_index: {exc}")

        # 降级：registry.get_realtime_quotes 用指数代码
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            registry = ProviderRegistry()
            index_codes = ["000001", "399001", "399006", "000688", "000300"]
            df = registry.get_realtime_quotes(symbols=index_codes)
            if df is not None and not df.empty:
                name_map = {
                    "000001": "上证指数", "399001": "深证成指",
                    "399006": "创业板指", "000688": "科创50",
                    "000300": "沪深300",
                }
                domestic = []
                for _, row in df.iterrows():
                    code = str(row.get("symbol") or row.get("ts_code") or "")[:6]
                    name = name_map.get(code, code)
                    pct = float(row.get("pct_chg") or 0.0)
                    price = float(row.get("price") or row.get("close") or 0.0)
                    domestic.append({"name": name, "price": round(price, 2), "change_pct": round(pct, 2)})
                if domestic:
                    result["domestic"] = domestic
        except Exception as exc:
            result["errors"].append(f"registry_index: {exc}")

        # 2. 外围市场（盘中只看韩国 KOSPI + 日经 225，与 A 股重合时间最多、相似度最高）
        try:
            from core.runtime.research_digest import load_external_market_snapshot
            external = load_external_market_snapshot(as_of=as_of)
            overseas = external.get("markets") or []
            # 盘中只保留 KOSPI 和日经225，过滤掉美股（盘中美股是前日收盘数据，参考价值低）
            asian_only = [
                item for item in overseas
                if any(p in str(item.get("name", "")).lower()
                       for p in ("kospi", "韩国", "日经", "n225"))
            ]
            # 按 KOSPI 优先排序
            priority = {"kospi": 0, "韩国": 0, "日经": 1, "n225": 1}
            asian_only.sort(
                key=lambda r: min(
                    (rank for key, rank in priority.items()
                     if key in str(r.get("name", "")).lower()),
                    default=99,
                )
            )
            result["overseas"] = asian_only[:2]
        except Exception as exc:
            result["errors"].append(f"external: {exc}")

        return result

    def _write_outputs(
        self,
        trade_date: str,
        run_id: str,
        mode: str,
        payload: dict[str, Any],
        markdown: str,
        html_content: str = "",
    ) -> tuple[Path, Path]:
        target = self.output_dir / trade_date
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / f"{mode}_{run_id}.json"
        report_path = target / f"{mode}_{run_id}.md"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report_path.write_text(markdown, encoding="utf-8")
        if html_content:
            html_path = target / f"{mode}_{run_id}.html"
            html_path.write_text(html_content, encoding="utf-8")
            latest_html = self.output_dir / f"latest_{mode}.html"
            self.output_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(html_path, latest_html)
        latest_json = self.output_dir / f"latest_{mode}.json"
        latest_md = self.output_dir / f"latest_{mode}.md"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(json_path, latest_json)
        shutil.copyfile(report_path, latest_md)
        if mode == "premarket":
            daily_brief = Path("data/reports/daily_brief.md")
            daily_brief.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report_path, daily_brief)
        return json_path, report_path

    def _write_push_gate(
        self, trade_date: str, run_id: str, json_path: Path, status: str
    ) -> None:
        gate = self.output_dir / "latest_success.json"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(json.dumps({
            "trade_date": trade_date,
            "run_id": run_id,
            "json_path": str(json_path),
            "status": status,
            "created_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _maybe_push(
        self,
        *,
        external_push: bool,
        trade_date: str,
        title: str,
        summary: str,
        report_path: Path,
        payload: dict[str, Any],
        html_content: str = "",
    ) -> dict[str, Any]:
        if not external_push:
            return {"requested": False, "sent": False, "reason": "external_push_disabled"}
        gate = self.output_dir / "latest_success.json"
        try:
            gate_payload = json.loads(gate.read_text(encoding="utf-8"))
        except Exception:
            return {"requested": True, "sent": False, "reason": "same_day_dry_run_missing"}
        if gate_payload.get("trade_date") != trade_date:
            return {"requested": True, "sent": False, "reason": "same_day_dry_run_missing"}
        payload_mode = str(payload.get("mode") or "")
        if (
            payload_mode not in {"monitor", "tail_research", "close"}
            and payload.get("run_id")
            and gate_payload.get("run_id") != payload.get("run_id")
        ):
            return {"requested": True, "sent": False, "reason": "dry_run_artifact_mismatch"}
        freshness_error = self._push_market_freshness_error(payload)
        if freshness_error:
            return {
                "requested": True,
                "sent": False,
                **freshness_error,
            }
        # P6: 推送去重持久化 — 同一 (date, entity, state_change, policy_version) 只推送一次
        anomalies = payload.get("anomaly_hypotheses") or []
        if payload_mode == "monitor" and anomalies:
            entity_id = "|".join(sorted(
                f"{item.get('symbol', '')}:{item.get('anomaly_type', '')}"
                for item in anomalies if isinstance(item, dict)
            )) or trade_date
            state_change = "intraday_anomaly"
        else:
            entity_id = payload.get("run_id") or trade_date
            state_change = payload.get("status") or payload_mode or "unknown"
        policy_version = self.VERSION
        dedup_store = self.push_dedup_store
        if dedup_store is not None:
            try:
                if not dedup_store.should_push(
                    date=trade_date,
                    entity_id=entity_id,
                    state_change=state_change,
                    policy_version=policy_version,
                ):
                    return {"requested": True, "sent": False, "reason": "dedup_skipped"}
            except Exception as exc:
                logger.warning("push dedup check failed: %s", exc)
        # 读取报告完整 Markdown 内容注入 payload，让飞书卡片能渲染详细。
        # 飞书单卡片 markdown 元素字符上限约 30000，超过时按段落截断到安全阈值。
        markdown_content = ""
        try:
            if report_path is not None and Path(report_path).exists():
                markdown_content = Path(report_path).read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("读取推送报告 markdown 失败: %s", exc)
        # 飞书卡片单 markdown 元素安全阈值，留出 header/链接余量
        markdown_cap = 28000
        if len(markdown_content) > markdown_cap:
            markdown_content = markdown_content[:markdown_cap] + "\n\n...（已截断，完整报告见本地文件）"
        try:
            from core.notifications.models import NotificationPayload
            from core.notifications.notification_service import get_notification_service_core

            # 透传结构化数据（themes/observations/research_task_runs 等），
            # 让 FeishuNotifier 能构造飞书原生 table 卡片，渲染优于 markdown 表格。
            structured_data: dict[str, Any] = {
                "report_path": str(report_path),
                "run_id": payload.get("run_id", ""),
                "status": payload.get("status", ""),
                "markdown_content": markdown_content,
                "html_content": html_content,
                "clawbot_short_text": build_short_recap(payload),
                # 本地日志不能冒充外部送达；任一已配置外部通道成功即可。
                "delivery_required_any": ["openclaw", "pushplus", "feishu"],
            }
            for key in (
                "themes",
                "observations",
                "research_task_runs",
                "ready_watch_count",
                "validated_observation_count",
                "as_of",
                "calendar",
                "health",
                "snapshots",
                "anomaly_hypotheses",
                "market_review",
                "theme_strength",
                "close_watchlist",
                "official_announcement_watchlist",
                "next_day_checks",
                "chart_data",
                "fund_flow",
                "external_markets",
                "previous_mentions",
                "self_learning_corrections",
                "volume_anomalies",
                "ai_summary",
            ):
                value = payload.get(key)
                if value is not None:
                    structured_data[key] = value

            sent = get_notification_service_core().notify(NotificationPayload(
                event_type="daily_brief_ready",
                title=title,
                summary=summary,
                priority="medium",
                links=[],
                data=structured_data,
            ))
        except Exception as exc:
            if dedup_store is not None:
                try:
                    dedup_store.record_failure(
                        date=trade_date,
                        entity_id=entity_id,
                        state_change=state_change,
                        policy_version=policy_version,
                        error=str(exc),
                    )
                except Exception:
                    pass
            return {"requested": True, "sent": False, "reason": str(exc)}
        if sent and dedup_store is not None:
            try:
                dedup_store.record_push(
                    date=trade_date,
                    entity_id=entity_id,
                    state_change=state_change,
                    policy_version=policy_version,
                    payload={"run_id": payload.get("run_id", "")},
                )
            except Exception as exc:
                logger.warning("push dedup record failed: %s", exc)
        elif not sent and dedup_store is not None:
            try:
                dedup_store.record_failure(
                    date=trade_date,
                    entity_id=entity_id,
                    state_change=state_change,
                    policy_version=policy_version,
                    error="all_channels_failed",
                )
            except Exception:
                pass
        return {"requested": True, "sent": bool(sent), "reason": "" if sent else "all_channels_failed"}

    def _push_market_freshness_error(
        self, payload: dict[str, Any]
    ) -> dict[str, str]:
        """阻止包含落后日线的结构化报告对外推送。"""
        watchlist = payload.get("close_watchlist") or []
        if watchlist:
            expected_date = str(payload.get("as_of") or "")[:10]
            dates = [
                self._market_timestamp_date(row.get("market_timestamp"))
                for row in watchlist if isinstance(row, dict)
            ]
            stale = any(
                not bool(row.get("quote_fresh"))
                for row in watchlist if isinstance(row, dict)
            ) or any(value != expected_date for value in dates)
            if stale:
                return {
                    "reason": "stale_market_data",
                    "latest_market_date": expected_date,
                    "payload_market_date": min(dates) if dates else "",
                }
            return {}
        observations = payload.get("observations") or []
        if not observations:
            return {}
        try:
            latest_market_date = str(
                (self.data_query.get_db_stats() or {}).get("latest_date") or ""
            )[:10]
        except Exception as exc:
            logger.warning("读取最新行情日期失败，禁止推送: %s", exc)
            return {"reason": "market_freshness_unavailable"}
        if not latest_market_date:
            return {"reason": "market_freshness_unavailable"}
        market_dates = [
            str((item.get("market_features") or {}).get("market_trade_date") or "")[:10]
            for item in observations
            if isinstance(item, dict)
        ]
        market_dates = [value for value in market_dates if value]
        if not market_dates:
            return {
                "reason": "market_data_date_missing",
                "latest_market_date": latest_market_date,
                "payload_market_date": "",
            }
        payload_market_date = min(market_dates)
        if payload_market_date < latest_market_date:
            return {
                "reason": "stale_market_data",
                "latest_market_date": latest_market_date,
                "payload_market_date": payload_market_date,
            }
        return {}

    def _read_latest_premarket(self) -> dict[str, Any] | None:
        path = self.output_dir / "latest_premarket.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("读取 latest premarket 失败 path=%s error=%s", path, exc)
            return None

    def _append_monitor_history(self, trade_date: str, payload: dict[str, Any]) -> None:
        path = self.output_dir / trade_date / "monitor_history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _fetch_realtime_quotes_batched(
        symbols: list[str], *, batch_size: int = 80, max_workers: int = 8
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch a large quote universe without creating an oversized request."""
        from infrastructure.data_providers.provider_registry import ProviderRegistry

        unique_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols if symbol))
        size = max(1, int(batch_size))
        batches = [unique_symbols[index:index + size] for index in range(0, len(unique_symbols), size)]
        started = time.perf_counter()
        rows: list[dict[str, Any]] = []
        failures: list[str] = []

        def fetch(batch: list[str]) -> list[dict[str, Any]]:
            frame = ProviderRegistry().get_realtime_quotes(batch)
            if frame is None or frame.empty:
                raise RuntimeError("empty quote batch")
            return frame.to_dict(orient="records")

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(batches) or 1))) as executor:
            future_to_batch = {executor.submit(fetch, batch): batch for batch in batches}
            for future in as_completed(future_to_batch):
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    batch = future_to_batch[future]
                    failures.append(f"{batch[0]}..{batch[-1]}: {exc}")

        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = ResearchMonitor._code(row.get("symbol") or row.get("stock_code"))
            if code:
                deduped[code] = row
        metrics = {
            "requested_symbol_count": len(unique_symbols),
            "returned_quote_count": len(deduped),
            "batch_count": len(batches),
            "failed_batch_count": len(failures),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "errors": failures,
        }
        logger.info("full-market quote scan metrics=%s", metrics)
        return list(deduped.values()), metrics

    @staticmethod
    def _compound_five_session_strength(
        history: pd.DataFrame | None, *, live_pct_chg: float, trade_date: str
    ) -> float:
        """Compound four completed sessions with the current live session."""
        changes: list[float] = []
        if history is not None and not history.empty and "change_pct" in history.columns:
            frame = history.copy()
            if "trade_date" in frame.columns:
                frame = frame[frame["trade_date"].astype(str).str[:10] < trade_date]
                frame = frame.sort_values("trade_date")
            for value in frame["change_pct"].tail(4).tolist():
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if parsed == parsed:
                    changes.append(parsed)
        changes.append(float(live_pct_chg or 0.0))
        compounded = 1.0
        for value in changes[-5:]:
            compounded *= 1.0 + value / 100.0
        return round((compounded - 1.0) * 100.0, 2)

    @classmethod
    def _announcement_catalyst_context(
        cls, row: dict[str, Any]
    ) -> tuple[float, str, list[dict[str, Any]]]:
        """Select recent first-party announcements with direct catalyst semantics."""
        announcements = [
            item for item in row.get("announcements") or [] if isinstance(item, dict)
        ]
        candidates: list[dict[str, Any]] = []
        trade_date_text = cls._market_timestamp_date(row.get("market_timestamp"))
        if announcements and trade_date_text:
            end = datetime.fromisoformat(trade_date_text).date()
            cutoff = end - timedelta(days=7)
            for item in announcements:
                try:
                    published = datetime.fromisoformat(
                        str(item.get("date") or "")[:10]
                    ).date()
                except ValueError:
                    continue
                if cutoff <= published <= end:
                    candidates.append(item)
        elif not announcements:
            candidates = [
                {"title": str(title), "date": "", "source": ""}
                for title in row.get("announcement_titles") or []
                if str(title or "").strip()
            ]

        classified: list[tuple[float, str, dict[str, Any]]] = []
        forecast = row.get("earnings_forecast") or {}
        forecast_type = str(forecast.get("forecast_type") or "").strip()
        if forecast_type:
            positive = forecast_type in {"预增", "略增", "扭亏", "续盈"}
            negative = forecast_type in {"预减", "略减", "首亏", "续亏"}
            minimum = cls._float(forecast.get("change_min_pct"))
            maximum = cls._float(forecast.get("change_max_pct"))
            if positive and (minimum or maximum):
                detail = f" {minimum:+.2f}%至{maximum:+.2f}%"
            elif negative:
                profit_min = cls._float(forecast.get("net_profit_min_wan")) / 10000
                profit_max = cls._float(forecast.get("net_profit_max_wan")) / 10000
                detail = f"（净利润{profit_min:.2f}亿至{profit_max:.2f}亿）"
            else:
                detail = ""
            raw_date = str(forecast.get("announcement_date") or "")
            published = (
                f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                if len(raw_date) >= 8 and "-" not in raw_date else raw_date[:10]
            )
            evidence = {
                "title": f"业绩预告：{forecast_type}{detail}",
                "date": published,
                "source": str(forecast.get("provider") or "tushare"),
                "evidence_type": "earnings_forecast",
            }
            classified.append(
                (0.9 if positive else 0.0 if negative else 0.1,
                 f"业绩预告：{forecast_type}{detail}", evidence)
            )
        for item in candidates:
            title = re.sub(r"\s+", "", str(item.get("title") or ""))
            if any(word in title for word in ("业绩预增", "中报预增", "半年度业绩预增")):
                classified.append((1.0, "中报业绩预增公告", item))
            elif "半年度业绩公告" in title or "半年度业绩预告" in title:
                classified.append((0.3, "半年度业绩公告（方向待解析）", item))
            elif "临床试验批准通知书" in title or "临床试验申请获得批准" in title:
                phase_match = re.search(r"([ⅠⅡⅢⅣIV一二三四]+)期", title)
                phase = f"{phase_match.group(1)}期" if phase_match else ""
                focus = "BCR前列腺癌" if "BCR" in title and "前列腺癌" in title else ""
                classified.append((0.65, f"{focus}{phase}临床试验获批", item))
            elif any(word in title for word in ("目录调整", "生产许可证", "药品注册")):
                classified.append((0.8, "监管或产品进展公告", item))
            elif "临床" in title and any(word in title for word in ("最终分析结果", "主要终点")):
                classified.append((0.35, "临床结果公告（方向待解析）", item))
            elif "专利" in title:
                classified.append((0.4, "专利进展公告", item))

        if not classified:
            return 0.0, "暂无与当日直接相关的硬催化", []
        classified.sort(key=lambda value: value[0], reverse=True)
        score, label, _ = classified[0]
        return score, label, [item for _, _, item in classified]

    @staticmethod
    def _rank_research_candidates(
        rows: list[dict[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        """Rank research subjects and explain every component.

        The score measures research priority, not expected return or a trading
        action.  Missing fields deliberately contribute zero instead of making
        the report fail.
        """

        def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
            return max(lower, min(upper, value))

        def number(value: Any) -> float:
            try:
                parsed = float(value)
                return parsed if parsed == parsed else 0.0
            except (TypeError, ValueError):
                return 0.0

        def turnover_score(turnover: float) -> float:
            if 2.0 <= turnover <= 15.0:
                return 1.0
            if 15.0 < turnover <= 25.0:
                return clamp(1.0 - (turnover - 15.0) / 12.5)
            if turnover > 25.0:
                return 0.1
            return clamp(turnover / 2.0)

        scored: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            daily = number(row.get("pct_chg"))
            persistence = number(row.get("five_session_pct"))
            turnover = number(row.get("turnover_rate"))
            volume_ratio = number(row.get("volume_ratio"))
            catalyst, catalyst_label, causal_announcements = (
                ResearchMonitor._announcement_catalyst_context(row)
            )
            evidence_count = len(causal_announcements)
            if evidence_count <= 0 and row.get("research_validated"):
                evidence_count = max(
                    len(row.get("first_party_evidence_quotes") or []),
                    len(row.get("claim_ids") or []),
                )

            components = {
                "daily_strength": clamp(daily / 10.0),
                "persistence": clamp(persistence / 50.0),
                "catalyst": catalyst,
                "evidence_quality": clamp(float(evidence_count)),
                "turnover_quality": turnover_score(turnover),
                "volume_confirmation": clamp(volume_ratio / 1.5),
            }
            score = (
                components["daily_strength"] * 0.30
                + components["persistence"] * 0.20
                + components["catalyst"] * 0.20
                + components["evidence_quality"] * 0.15
                + components["turnover_quality"] * 0.10
                + components["volume_confirmation"] * 0.05
            )
            reasons = [
                f"当日涨幅{daily:.2f}%",
                f"近5日强度{persistence:.2f}%",
                catalyst_label,
            ]
            risks: list[str] = []
            if turnover > 25.0:
                risks.append(f"换手率{turnover:.2f}%偏高，交易拥挤")
            elif turnover <= 0:
                risks.append("换手率字段缺失")
            volume_vs_ma5 = number(row.get("volume_vs_ma5"))
            if volume_vs_ma5 >= 1.8:
                risks.append(f"量能为近5日均量{volume_vs_ma5:.2f}倍，持续性待验证")
            if daily >= 9.5:
                risks.append("单日波动较大，次日强度可能变化")
            if "临床" in catalyst_label:
                risks.append("临床进展不等同商业化结果")
            if evidence_count <= 0:
                risks.append("暂无与当日直接相关的第一方证据")
            forecast_type = str((row.get("earnings_forecast") or {}).get("forecast_type") or "")
            if forecast_type in {"预减", "略减", "首亏", "续亏"}:
                risks.append(f"业绩预告为{forecast_type}，盈利方向承压")
            if not risks:
                risks.append("未发现显著拥挤风险")

            row.update(
                research_score=round(score, 4),
                score_components={key: round(value, 4) for key, value in components.items()},
                selection_reason="；".join(reasons),
                risk_explanation="；".join(risks),
                catalyst=catalyst_label,
                causal_announcements=causal_announcements,
                causal_first_party_evidence_count=len(causal_announcements),
                relevant_evidence_count=evidence_count,
            )
            scored.append(row)

        scored.sort(
            key=lambda item: (
                -number(item.get("research_score")),
                -number(item.get("pct_chg")),
                str(item.get("symbol") or ""),
            )
        )
        selected = scored[: max(0, int(limit))]
        leader_name = str(selected[0].get("stock_name") or selected[0].get("symbol") or "") if selected else ""
        for index, row in enumerate(selected, start=1):
            row["rank"] = index
            if index == 1:
                row["comparison_explanation"] = "综合研究优先级居首"
            else:
                row["comparison_explanation"] = f"综合研究优先级落后于{leader_name}"
        return selected

    @staticmethod
    def _rewrite_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    @staticmethod
    def _run_id(mode: str, as_of: str) -> str:
        return f"{mode}_{as_of[:10].replace('-', '')}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _code(value: Any) -> str:
        match = re.search(r"\d{6}", str(value or ""))
        return match.group(0) if match else ""

    @staticmethod
    def _market_timestamp_date(value: Any) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) < 8:
            return ""
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"

    @staticmethod
    def _float(value: Any) -> float:
        try:
            number = float(value)
            return number if number == number else 0.0
        except Exception:
            return 0.0
