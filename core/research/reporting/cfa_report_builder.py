"""P1.9 CFA 风格研究报告构建器与持久化仓储。

CfaReportBuilder 顺序构建固定 10 章节，每章节独立 try/except，
缺数据时降级 partial 而不抛异常。ReportRepository 提供 SQLite
持久化，PIT 过滤，幂等迁移。

设计要点：
- 复用各领域已实现模块（fundamentals / operating_model / earnings / industry /
  governance / expectations / valuation），不重新发明数据结构
- 每章节独立降级；任一 missing -> overall=partial；多数 missing -> degraded
- notice 固定「研究数据产物，不包含交易动作建议。」
- 章节 9 含反证证据与模型失效条件，但不下买卖结论
- 章节 10 收集所有 source_refs + model_versions，完整审计链路
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .contracts import (
    NOTICE,
    OVERALL_STATUS_DEGRADED,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    ReportSection,
    ReportSectionContent,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_NOT_APPLICABLE,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_TITLES,
    CfaReport,
    compute_overall_status,
    empty_section,
    new_id,
    utc_now,
)


def _safe_limit(value: Any, default: int = 100) -> int:
    try:
        return max(1, min(int(value), 1000))
    except (TypeError, ValueError, OverflowError):
        return default


def _decode_payload(raw: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# A 股代码正则：6 位数字 + .SH/.SZ/.BJ
SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_call(func: Callable[[], Any]) -> Any:
    """安全调用函数，捕获所有异常返回 None。

    Args:
        func: 无参可调用对象

    Returns:
        函数返回值；异常返回 None
    """
    try:
        return func()
    except Exception:
        return None


def _safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    """安全获取对象属性。"""
    if obj is None:
        return default
    return getattr(obj, attr, default)

def _to_dict_safe(value: Any) -> Any:
    """安全序列化任意对象为 dict 或基础类型。"""
    if value is None:
        return None
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return value.to_dict()
        except Exception:
            return None
    if is_dataclass(value):
        try:
            return asdict(value)
        except Exception:
            return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_dict_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_dict_safe(v) for k, v in value.items()}
    return str(value)


def _maybe_call(repo: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    """若 repo 提供 method_name 则调用，否则返回 None。"""
    if repo is None:
        return None
    method = getattr(repo, method_name, None)
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except Exception:
        return None


def _collect_source_refs(*values: Any) -> list[str]:
    """从多个对象收集 source_ref / source_refs / source_trace_id。"""
    refs: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                refs.extend(_collect_source_refs(item))
            continue
        if isinstance(value, dict):
            for key in ("source_ref", "source_refs", "source_trace_id",
                       "source_claim_id", "source_evidence_id"):
                v = value.get(key)
                if isinstance(v, str) and v:
                    refs.append(v)
                elif isinstance(v, list):
                    refs.extend([str(x) for x in v if x])
            continue
        for key in ("source_ref", "source_trace_id",
                   "source_claim_id", "source_evidence_id"):
            v = getattr(value, key, None)
            if isinstance(v, str) and v:
                refs.append(v)
        v = getattr(value, "source_refs", None)
        if isinstance(v, list):
            refs.extend([str(x) for x in v if x])
    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# 构建器
# ---------------------------------------------------------------------------

class CfaReportBuilder:
    """CFA 风格研究报告构建器。

    依赖注入各领域仓储；每章节独立 try/except；缺数据降级 partial。
    不输出买入/卖出/仓位/止盈/止损等投资建议。

    Attributes:
        fundamentals_repo: 标准化三表 + 质量 + 异常 仓储
        operating_repo: 经营驱动模型仓储
        earnings_repo: 盈利预测仓储
        industry_repo: 产业画像仓储（可为 None）
        governance_repo: 治理台账仓储
        expectation_repo: 公司指引/一致预期/隐含预期/AquatradeForecast 仓储
        valuation_repo: 多方法估值仓储
    """

    def __init__(
        self,
        fundamentals_repo: Any = None,
        operating_repo: Any = None,
        earnings_repo: Any = None,
        industry_repo: Any = None,
        governance_repo: Any = None,
        expectation_repo: Any = None,
        valuation_repo: Any = None,
        company_meta: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """初始化构建器，注入各领域仓储。

        Args:
            fundamentals_repo: 标准化三表仓储
            operating_repo: 经营驱动模型仓储
            earnings_repo: 盈利预测仓储
            industry_repo: 产业画像仓储
            governance_repo: 治理台账仓储
            expectation_repo: 预期仓储
            valuation_repo: 估值仓储
        """
        self.fundamentals_repo = fundamentals_repo
        self.operating_repo = operating_repo
        self.earnings_repo = earnings_repo
        self.industry_repo = industry_repo
        self.governance_repo = governance_repo
        self.expectation_repo = expectation_repo
        self.valuation_repo = valuation_repo
        self.company_meta = company_meta or {}

    # ---- 主入口 ----

    def build(
        self,
        company_symbol: str,
        as_of: str,
        lookback_days: int = 120,
    ) -> CfaReport:
        """构建完整 10 章节 CFA 报告。

        Args:
            company_symbol: 公司代码（必须 6 位数字 + .SH/.SZ/.BJ）
            as_of: PIT 时点（ISO 8601 或 YYYY-MM-DD）
            lookback_days: 回看天数，默认 120

        Returns:
            CfaReport，固定含 10 章节；缺数据章节 section_status=missing
        """
        if not company_symbol or not SYMBOL_PATTERN.match(company_symbol):
            # 即使 symbol 非法也返回 degraded 报告，不抛异常
            report = CfaReport(
                company_symbol=company_symbol or "",
                company_name="",
                as_of=as_of,
                lookback_days=lookback_days,
                sections=[],
                overall_status=OVERALL_STATUS_DEGRADED,
                degradation_reasons=[f"invalid_symbol:{company_symbol}"],
                notice=NOTICE,
                model_version="v1",
            )
            for section_value in ReportSection.ordered_values():
                report.sections.append(
                    empty_section(section_value, reason="invalid_symbol")
                )
            return report

        # 先构建 1-8 章节
        sections: list[ReportSectionContent] = []
        early_builders: list[tuple[str, Callable[[], ReportSectionContent]]] = [
            (ReportSection.SCOPE_AND_AS_OF.value, lambda: self._build_scope_section(
                company_symbol, as_of, lookback_days)),
            (ReportSection.STRATEGY_BUSINESS_MODEL.value,
             lambda: self._build_strategy_section(company_symbol, as_of)),
            (ReportSection.INDUSTRY_COMPETITION.value,
             lambda: self._build_industry_section(company_symbol, as_of)),
            (ReportSection.FINANCIAL_QUALITY.value,
             lambda: self._build_financial_quality_section(company_symbol, as_of)),
            (ReportSection.DRIVERS_AND_FORECAST.value,
             lambda: self._build_drivers_forecast_section(company_symbol, as_of)),
            (ReportSection.EXPECTATION_DIFFERENCES.value,
             lambda: self._build_expectation_differences_section(company_symbol, as_of)),
            (ReportSection.VALUATION_SENSITIVITY.value,
             lambda: self._build_valuation_section(company_symbol, as_of)),
            (ReportSection.GOVERNANCE_CAPITAL.value,
             lambda: self._build_governance_section(company_symbol, as_of)),
        ]
        for section_value, builder_fn in early_builders:
            section = _safe_call(builder_fn)
            if section is None:
                section = empty_section(section_value, reason="builder_exception")
            sections.append(section)

        # 章节 9 与 10 依赖 1-8 的 limitations / source_refs
        risks_section = _safe_call(
            lambda: self._build_risks_section(company_symbol, as_of, sections)
        )
        if risks_section is None:
            risks_section = empty_section(
                ReportSection.RISKS_COUNTEREVIDENCE.value,
                reason="builder_exception",
            )
        sections.append(risks_section)

        sources_section = _safe_call(
            lambda: self._build_sources_section(company_symbol, as_of, sections)
        )
        if sources_section is None:
            sources_section = empty_section(
                ReportSection.SOURCES_AND_VERSIONS.value,
                reason="builder_exception",
            )
        sections.append(sources_section)

        overall_status, reasons = compute_overall_status(sections)
        company_name = self._lookup_company_name(company_symbol, as_of)
        return CfaReport(
            company_symbol=company_symbol,
            company_name=company_name,
            as_of=as_of,
            lookback_days=lookback_days,
            sections=sections,
            overall_status=overall_status,
            degradation_reasons=reasons,
            notice=NOTICE,
            model_version="v1",
        )

    # ---- 章节构建 ----

    def _build_scope_section(
        self,
        company_symbol: str,
        as_of: str,
        lookback_days: int,
    ) -> ReportSectionContent:
        """章节 1：研究范围与 as_of。"""
        company_name = self._lookup_company_name(company_symbol, as_of)
        industry = self._lookup_industry_name(company_symbol, as_of)
        content: dict[str, Any] = {
            "company_symbol": company_symbol,
            "company_name": company_name,
            "industry": industry,
            "as_of": as_of,
            "lookback_days": lookback_days,
            "data_scope": "本报告仅纳入 as_of 时点前可获取的本地结构化证据。",
            "pit_principle": "未来数据不进入历史报告；PIT 过滤由各领域仓储强制。",
        }
        limitations: list[str] = []
        if not company_name:
            limitations.append("company_name_missing")
        if not industry:
            limitations.append("industry_missing")
        status = SECTION_STATUS_OK if not limitations else SECTION_STATUS_PARTIAL
        return ReportSectionContent(
            section=ReportSection.SCOPE_AND_AS_OF.value,
            title=SECTION_TITLES[ReportSection.SCOPE_AND_AS_OF.value],
            content=content,
            limitations=limitations,
            source_refs=[],
            section_status=status,
        )

    def _build_strategy_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 2：公司战略和商业模式。"""
        stmt = self._fetch_latest_statement(company_symbol, as_of)
        operating = self._fetch_latest_operating_model(company_symbol, as_of)
        business_model = _safe_get(operating, "business_model", "")
        segments = _safe_get(operating, "segments", []) or []
        currency = _safe_get(operating, "currency", "")
        unit = _safe_get(operating, "unit", "")
        content: dict[str, Any] = {
            "business_model": business_model or "未识别",
            "segments": [_to_dict_safe(s) for s in segments] if segments else [],
            "currency": currency,
            "unit": unit,
            "strategy_note": (
                "公司战略和商业模式描述需结合公司公告、年报与产业研究证据，"
                "本章节仅汇总结构化字段，不作战略评价。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(stmt, operating)
        if not business_model:
            limitations.append("business_model_missing")
        if not segments:
            limitations.append("segments_missing")
        status = SECTION_STATUS_OK
        if not business_model and not segments:
            status = SECTION_STATUS_MISSING
        elif limitations:
            status = SECTION_STATUS_PARTIAL
        return ReportSectionContent(
            section=ReportSection.STRATEGY_BUSINESS_MODEL.value,
            title=SECTION_TITLES[ReportSection.STRATEGY_BUSINESS_MODEL.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_industry_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 3：行业、竞争和产业链。

        P1.10 整改：IndustryRepository 已落地，从仓储取 IndustryProfile
        （合并 economics + competition + lifecycle 三部分）；缺数据降级 missing；
        lifecycle_stage 缺时间和证据降级 partial；company_mappings 缺本地证据
        显示「暂无本地证据」（AGENTS.md）。
        """
        profile = _maybe_call(
            self.industry_repo, "get_latest_profile",
            company_symbol, as_of,
        ) if self.industry_repo else None
        if profile is None:
            profile = _maybe_call(
                self.industry_repo, "get_profile",
                company_symbol, as_of,
            ) if self.industry_repo else None
        content: dict[str, Any] = {
            "industry_profile": _to_dict_safe(profile),
            "member_of_theme_filter": (
                "严格过滤 member_of_theme 来源，不进入市场份额与竞争结论。"
            ),
            "company_mapping_note": "公司映射证据为空时显示「暂无本地证据」。",
            "industry_repo_status": (
                "active" if self.industry_repo is not None else "missing"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(profile)
        if profile is None:
            limitations.append("industry_profile_missing")
            status = SECTION_STATUS_MISSING
        else:
            status = SECTION_STATUS_OK
            # 检查 lifecycle_stage 是否有时间和证据
            lifecycle = _safe_get(profile, "lifecycle_stage", None)
            if lifecycle is not None:
                if not _safe_get(lifecycle, "stage_evidence_id", ""):
                    limitations.append("lifecycle_stage_no_evidence")
                    status = SECTION_STATUS_PARTIAL
                if not _safe_get(lifecycle, "stage_dated_at", ""):
                    limitations.append("lifecycle_stage_no_time")
                    status = SECTION_STATUS_PARTIAL
            else:
                # lifecycle_stage 为空：标记 missing（赛道阶段必须有时间和证据）
                limitations.append("lifecycle_stage_missing")
                status = SECTION_STATUS_PARTIAL
            # 检查 member_of_theme 过滤
            landscape = _safe_get(profile, "competitive_landscape", None)
            if landscape is not None:
                if not _safe_get(landscape, "member_of_theme_excluded", True):
                    limitations.append("member_of_theme_not_excluded")
                    status = SECTION_STATUS_PARTIAL
            # 检查公司映射本地证据
            mappings = _safe_get(profile, "company_mappings", []) or []
            for idx, mapping in enumerate(mappings):
                if not _safe_get(mapping, "has_local_evidence", False):
                    limitations.append(f"company_mapping_{idx}_no_local_evidence")
                    if status == SECTION_STATUS_OK:
                        status = SECTION_STATUS_PARTIAL
            # 检查 gaps
            gaps = _safe_get(profile, "gaps", []) or []
            if gaps:
                limitations.append(f"industry_gaps_count:{len(gaps)}")
                if status == SECTION_STATUS_OK:
                    status = SECTION_STATUS_PARTIAL
            if limitations and status == SECTION_STATUS_OK:
                status = SECTION_STATUS_PARTIAL
        return ReportSectionContent(
            section=ReportSection.INDUSTRY_COMPETITION.value,
            title=SECTION_TITLES[ReportSection.INDUSTRY_COMPETITION.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_financial_quality_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 4：财务质量与会计调整。"""
        stmt = self._fetch_latest_statement(company_symbol, as_of)
        quality_report = _maybe_call(
            self.fundamentals_repo, "get_latest_quality_report",
            company_symbol, as_of,
        ) if self.fundamentals_repo else None
        anomaly_report = _maybe_call(
            self.fundamentals_repo, "get_latest_anomaly_report",
            company_symbol, as_of,
        ) if self.fundamentals_repo else None

        content: dict[str, Any] = {
            "statement": _to_dict_safe(stmt),
            "quality_report": _to_dict_safe(quality_report),
            "anomaly_report": _to_dict_safe(anomaly_report),
            "audit_boundary": (
                "异常是审查提示，不等于财务造假；需结合行业周期、会计政策与"
                "原始凭证进一步判断。本引擎只负责标记观察点，不下结论。"
            ),
            "adjustment_note": (
                "会计调整账本按一次性损益/资产处置/减值/政府补助等 8 类记录，"
                "每项调整必须有 source_ref；无来源只能创建 gap。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(stmt, quality_report, anomaly_report)
        if stmt is None and quality_report is None and anomaly_report is None:
            limitations.append("no_financial_data")
            status = SECTION_STATUS_MISSING
        else:
            if stmt is None:
                limitations.append("statement_missing")
            if quality_report is None:
                limitations.append("quality_report_missing")
            if anomaly_report is None:
                limitations.append("anomaly_report_missing")
            status = SECTION_STATUS_PARTIAL if limitations else SECTION_STATUS_OK
        return ReportSectionContent(
            section=ReportSection.FINANCIAL_QUALITY.value,
            title=SECTION_TITLES[ReportSection.FINANCIAL_QUALITY.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_drivers_forecast_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 5：产品驱动和盈利预测。"""
        operating = self._fetch_latest_operating_model(company_symbol, as_of)
        forecast = _maybe_call(
            self.earnings_repo, "get_latest_forecast",
            company_symbol, as_of,
        ) or _maybe_call(
            self.earnings_repo, "get_latest_integrated_forecast",
            company_symbol, as_of,
        )
        historical_drivers: list[Any] = []
        forward_assumptions: list[Any] = []
        if operating is not None:
            for driver in _safe_get(operating, "drivers", []) or []:
                if _safe_get(driver, "observation_kind", "") == "historical_disclosed":
                    historical_drivers.append(_to_dict_safe(driver))
            for seg in _safe_get(operating, "segments", []) or []:
                for driver in _safe_get(seg, "drivers", []) or []:
                    if _safe_get(driver, "observation_kind", "") == "historical_disclosed":
                        historical_drivers.append(_to_dict_safe(driver))
            for asm in _safe_get(operating, "assumptions", []) or []:
                forward_assumptions.append(_to_dict_safe(asm))

        content: dict[str, Any] = {
            "historical_drivers": historical_drivers,
            "forward_assumptions": forward_assumptions,
            "integrated_forecast": _to_dict_safe(forecast),
            "observation_vs_assumption_note": (
                "历史观察值（observation_kind=historical_disclosed）不得自动成为未来假设；"
                "前瞻假设（ForwardAssumption）必须有 source_ref，"
                "定性词如「提升/增长」不能转成数值幅度。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(operating, forecast)
        if operating is None and forecast is None:
            limitations.append("no_operating_or_forecast_data")
            status = SECTION_STATUS_MISSING
        else:
            if operating is None:
                limitations.append("operating_model_missing")
            if forecast is None:
                limitations.append("integrated_forecast_missing")
            if not historical_drivers:
                limitations.append("no_historical_drivers")
            if not forward_assumptions:
                limitations.append("no_forward_assumptions")
            status = SECTION_STATUS_PARTIAL if limitations else SECTION_STATUS_OK
        return ReportSectionContent(
            section=ReportSection.DRIVERS_AND_FORECAST.value,
            title=SECTION_TITLES[ReportSection.DRIVERS_AND_FORECAST.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_expectation_differences_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 6：公司指引、一致预期和研究差异。"""
        repo = self.expectation_repo
        guidance_list = _maybe_call(
            repo, "list_company_guidance",
            company_symbol=company_symbol, as_of=as_of, limit=10,
        ) if repo else None
        consensus_list = _maybe_call(
            repo, "list_consensus_estimates",
            company_symbol=company_symbol, as_of=as_of, limit=10,
        ) if repo else None
        implied_list = _maybe_call(
            repo, "list_implied_expectations",
            company_symbol=company_symbol, as_of=as_of, limit=10,
        ) if repo else None
        aquatrade_list = _maybe_call(
            repo, "list_aquatrade_forecasts",
            company_symbol=company_symbol, as_of=as_of, limit=10,
        ) if repo else None
        consensus_summary = _maybe_call(
            repo, "get_latest_consensus_summary",
            company_symbol, as_of,
        ) if repo else None
        expectation_gaps = _maybe_call(
            repo, "list_expectation_gaps",
            company_symbol=company_symbol, as_of=as_of, limit=10,
        ) if repo else None

        content: dict[str, Any] = {
            "company_guidance": _to_dict_safe(guidance_list) or [],
            "consensus_estimates": _to_dict_safe(consensus_list) or [],
            "implied_expectations": _to_dict_safe(implied_list) or [],
            "aquatrade_forecasts": _to_dict_safe(aquatrade_list) or [],
            "consensus_summary": _to_dict_safe(consensus_summary),
            "expectation_gaps": _to_dict_safe(expectation_gaps) or [],
            "separation_note": (
                "公司指引、第三方一致预期、市场隐含预期和 AquaTrade 预测为四类独立预期，"
                "互相独立，不得互相覆盖；来源不足时 status=insufficient_sources，"
                "预期差 gap 不可计算，禁止用 0/均值/中位数兜底。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(
            guidance_list, consensus_list, implied_list,
            aquatrade_list, consensus_summary, expectation_gaps,
        )
        any_data = any([
            guidance_list, consensus_list, implied_list,
            aquatrade_list, consensus_summary, expectation_gaps,
        ])
        if not any_data:
            limitations.append("no_expectation_data")
            status = SECTION_STATUS_MISSING
        else:
            if not guidance_list:
                limitations.append("company_guidance_missing")
            if not consensus_list:
                limitations.append("consensus_estimates_missing")
            if not implied_list:
                limitations.append("implied_expectations_missing")
            if not aquatrade_list:
                limitations.append("aquatrade_forecasts_missing")
            status = SECTION_STATUS_PARTIAL if limitations else SECTION_STATUS_OK
        return ReportSectionContent(
            section=ReportSection.EXPECTATION_DIFFERENCES.value,
            title=SECTION_TITLES[ReportSection.EXPECTATION_DIFFERENCES.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_valuation_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 7：估值方法与敏感性。"""
        repo = self.valuation_repo
        # 优先取估值报告
        reports = _maybe_call(
            repo, "list_reports",
            company_symbol=company_symbol, as_of=as_of, limit=1,
        ) if repo else None
        valuation_report = None
        if reports:
            valuation_report = reports[0]
        # 同时取估值案例与敏感性
        cases = _maybe_call(
            repo, "list_cases",
            company_symbol=company_symbol, as_of=as_of, limit=20,
        ) if repo else None
        sensitivity_points: list[Any] = []
        if cases:
            for case in cases:
                sens = _safe_get(case, "sensitivity", []) if case else None
                if sens:
                    sensitivity_points.extend([_to_dict_safe(s) for s in sens])

        content: dict[str, Any] = {
            "valuation_report": _to_dict_safe(valuation_report),
            "valuation_cases": _to_dict_safe(cases) or [],
            "sensitivity_points": sensitivity_points,
            "range_note": (
                "估值输出区间与敏感性，不输出单一合理价值；"
                "不同方法差异在 method_differences 中解释，不平均掩盖；"
                "overall_range=min(low)~max(high)，标记为 min_max。"
            ),
            "no_action_note": (
                "本章节不输出买入/卖出/仓位/止盈/止损等交易动作建议。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(valuation_report, cases)
        if valuation_report is None and not cases:
            limitations.append("no_valuation_data")
            status = SECTION_STATUS_MISSING
        else:
            if valuation_report is None:
                limitations.append("valuation_report_missing")
            if not cases:
                limitations.append("valuation_cases_missing")
            if not sensitivity_points:
                limitations.append("sensitivity_missing")
            status = SECTION_STATUS_PARTIAL if limitations else SECTION_STATUS_OK
        return ReportSectionContent(
            section=ReportSection.VALUATION_SENSITIVITY.value,
            title=SECTION_TITLES[ReportSection.VALUATION_SENSITIVITY.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_governance_section(
        self,
        company_symbol: str,
        as_of: str,
    ) -> ReportSectionContent:
        """章节 8：管理层、治理和资本配置。"""
        ledger = _maybe_call(
            self.governance_repo, "get_latest_ledger",
            company_symbol, as_of,
        ) if self.governance_repo else None
        content: dict[str, Any] = {
            "governance_ledger": _to_dict_safe(ledger),
            "emotional_news_note": (
                "人员评价不能由情绪新闻直接生成；is_from_emotional_news=True 的"
                "评价仅记录 blocker，不进入正式 personnel_assessments。"
            ),
            "commitment_note": (
                "管理层承诺按原文—时间—目标—兑现—偏差 5 项齐全保存；"
                "缺任一项触发 blocker gap。"
            ),
            "risk_separation_note": (
                "治理风险与财务预测影响分开表达；potential_financial_impact "
                "仅描述性字段，不进入预测引擎。"
            ),
        }
        limitations: list[str] = []
        source_refs: list[str] = _collect_source_refs(ledger)
        if ledger is None:
            limitations.append("governance_ledger_missing")
            status = SECTION_STATUS_MISSING
        else:
            commitments = _safe_get(ledger, "management_commitments", []) or []
            persons = _safe_get(ledger, "management_persons", []) or []
            risks = _safe_get(ledger, "governance_risks", []) or []
            capital_actions = _safe_get(ledger, "capital_actions", []) or []
            if not commitments:
                limitations.append("commitments_empty")
            if not persons:
                limitations.append("management_persons_empty")
            if not risks:
                limitations.append("governance_risks_empty")
            if not capital_actions:
                limitations.append("capital_actions_empty")
            status = SECTION_STATUS_PARTIAL if limitations else SECTION_STATUS_OK
        return ReportSectionContent(
            section=ReportSection.GOVERNANCE_CAPITAL.value,
            title=SECTION_TITLES[ReportSection.GOVERNANCE_CAPITAL.value],
            content=content,
            limitations=limitations,
            source_refs=source_refs,
            section_status=status,
        )

    def _build_risks_section(
        self,
        company_symbol: str,
        as_of: str,
        early_sections: list[ReportSectionContent] | None = None,
    ) -> ReportSectionContent:
        """章节 9：重要风险、反证和模型限制。

        汇总各章节 limitations；含反证证据与模型失效条件；
        不下买卖结论。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            early_sections: 已构建的 1-8 章节列表，用于聚合 limitations
        """
        all_limitations: list[str] = []
        for section in (early_sections or []):
            for lim in section.limitations:
                all_limitations.append(f"{section.section}:{lim}")

        counter_evidence: list[str] = []
        # 反证证据：异常信号 + governance 风险事件
        anomaly_report = _maybe_call(
            self.fundamentals_repo, "get_latest_anomaly_report",
            company_symbol, as_of,
        ) if self.fundamentals_repo else None
        if anomaly_report is not None:
            signals = _safe_get(anomaly_report, "signals", []) or []
            for sig in signals:
                obs = _safe_get(sig, "observation", "")
                if obs:
                    counter_evidence.append(str(obs))
        ledger = _maybe_call(
            self.governance_repo, "get_latest_ledger",
            company_symbol, as_of,
        ) if self.governance_repo else None
        if ledger is not None:
            for risk in _safe_get(ledger, "governance_risks", []) or []:
                desc = _safe_get(risk, "description", "")
                if desc:
                    counter_evidence.append(str(desc))
            for evt in _safe_get(ledger, "risk_events", []) or []:
                desc = _safe_get(evt, "description", "")
                if desc:
                    counter_evidence.append(str(desc))

        model_failure_conditions: list[str] = [
            "三表不平衡或单位/币种冲突时，正式预测阻断。",
            "缺关键假设 source_ref 时，估值方法返回 not_applicable。",
            "PIT 过滤失败导致未来数据泄漏时，整批认证作废。",
            "公司映射证据为空时，章节 3 显示「暂无本地证据」。",
        ]
        content: dict[str, Any] = {
            "aggregated_limitations": all_limitations,
            "counter_evidence": counter_evidence,
            "model_failure_conditions": model_failure_conditions,
            "no_trade_advice_note": (
                "本章节汇总风险与反证，不下买入/卖出/仓位/止盈/止损等买卖结论；"
                "异常是审查提示，不等于财务造假。"
            ),
        }
        limitations: list[str] = []
        if not counter_evidence:
            limitations.append("no_counter_evidence")
        status = SECTION_STATUS_OK if counter_evidence else SECTION_STATUS_PARTIAL
        return ReportSectionContent(
            section=ReportSection.RISKS_COUNTEREVIDENCE.value,
            title=SECTION_TITLES[ReportSection.RISKS_COUNTEREVIDENCE.value],
            content=content,
            limitations=limitations,
            source_refs=[],
            section_status=status,
        )

    def _build_sources_section(
        self,
        company_symbol: str,
        as_of: str,
        early_sections: list[ReportSectionContent] | None = None,
    ) -> ReportSectionContent:
        """章节 10：来源、模型版本和记录留存信息。

        收集所有 source_refs + model_versions；完整审计链路。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点
            early_sections: 已构建的 1-9 章节列表，用于聚合 source_refs
        """
        all_refs: list[str] = []
        seen: set[str] = set()
        for section in (early_sections or []):
            for ref in section.source_refs:
                if ref and ref not in seen:
                    seen.add(ref)
                    all_refs.append(ref)

        model_versions: dict[str, str] = {
            "cfa_report_builder": "v1",
            "fundamentals": "v1",
            "operating_model": "v1",
            "earnings": "v1",
            "industry": "v1",
            "governance": "v1",
            "expectations": "v1",
            "valuation": "v1",
        }
        # 若各领域对象携带 schema_version，则覆盖默认值
        for section in (early_sections or []):
            content = section.content or {}
            for key in ("statement", "quality_report", "anomaly_report",
                       "industry_profile", "governance_ledger",
                       "valuation_report", "integrated_forecast"):
                obj = content.get(key)
                if isinstance(obj, dict):
                    sv = obj.get("schema_version") or obj.get("model_version")
                    if sv:
                        model_versions[key] = str(sv)

        content: dict[str, Any] = {
            "source_refs": all_refs,
            "model_versions": model_versions,
            "as_of": as_of,
            "company_symbol": company_symbol,
            "report_id": new_id("cfar"),
            "audit_chain_note": (
                "本章节收集所有章节的 source_refs 与各领域 model_versions；"
                "report_id 与 created_at 用于审计链路追溯。"
            ),
        }
        status = SECTION_STATUS_OK if all_refs else SECTION_STATUS_PARTIAL
        return ReportSectionContent(
            section=ReportSection.SOURCES_AND_VERSIONS.value,
            title=SECTION_TITLES[ReportSection.SOURCES_AND_VERSIONS.value],
            content=content,
            limitations=[] if all_refs else ["no_source_refs_collected"],
            source_refs=all_refs,
            section_status=status,
        )

    # ---- 辅助方法 ----

    def _fetch_latest_statement(self, company_symbol: str, as_of: str) -> Any:
        """从 fundamentals_repo 取最新 NormalizedFinancialStatement。"""
        repo = self.fundamentals_repo
        if repo is None:
            return None
        # 优先尝试 PIT 接口
        for method_name in (
            "get_latest_statement", "get_latest", "latest_statement",
        ):
            stmt = _maybe_call(repo, method_name, company_symbol, as_of)
            if stmt is not None:
                return stmt
        return None

    def _fetch_latest_operating_model(self, company_symbol: str, as_of: str) -> Any:
        """从 operating_repo 取最新 OperatingModel（P1.10 桥接层适配）。

        优先调用 ``get_latest_model(symbol, as_of)``（不限期间，自动选最新），
        回退到 ``get_latest(symbol, as_of)`` / ``get_latest(symbol, "", as_of)``，
        兼容 P1.9 mock 仓储与 P1.1-P1.8 真实仓储。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点

        Returns:
            最新 OperatingModel；无返回 None
        """
        repo = self.operating_repo
        if repo is None:
            return None
        # 优先尝试不限期间的适配方法（P1.10 新增）
        for method_name in ("get_latest_model", "get_latest_statement"):
            model = _maybe_call(repo, method_name, company_symbol, as_of)
            if model is not None:
                return model
        # 回退到旧签名：(symbol, as_of) 与 (symbol, "", as_of)
        for args in ((company_symbol, as_of), (company_symbol, "", as_of)):
            model = _maybe_call(repo, "get_latest", *args)
            if model is not None:
                return model
        return None

    def _lookup_company_name(self, company_symbol: str, as_of: str) -> str:
        """尝试从各领域获取公司名。"""
        for repo, methods in (
            (self.governance_repo, ("get_latest_ledger",)),
            (self.fundamentals_repo, ("get_latest_statement", "get_latest")),
            (self.operating_repo, ("get_latest",)),
        ):
            if repo is None:
                continue
            for method_name in methods:
                obj = _maybe_call(repo, method_name, company_symbol, as_of)
                if obj is None:
                    continue
                name = _safe_get(obj, "company_name") or _safe_get(obj, "symbol")
                if isinstance(name, str) and name and name != company_symbol:
                    return name
        return str(self.company_meta.get(company_symbol, {}).get("name") or "")

    def _lookup_industry_name(self, company_symbol: str, as_of: str) -> str:
        """尝试从产业画像获取行业名。"""
        if self.industry_repo is not None:
            for method_name in ("get_latest_profile", "get_profile"):
                profile = _maybe_call(self.industry_repo, method_name, company_symbol, as_of)
                if profile is None:
                    continue
                name = _safe_get(profile, "industry_name", "")
                if name:
                    return str(name)
        return str(self.company_meta.get(company_symbol, {}).get("industry") or "")


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------

class ReportRepository:
    """SQLite 持久化 CFA 报告。

    SCHEMA_VERSION=1，幂等迁移。
    - cfa_reports 表（report_id PK, company_symbol + as_of 索引）
    - save_report / get_report / list_reports / get_latest_report（PIT 过滤）
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化仓储并执行幂等迁移。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``CFA_REPORT_DB_PATH`` 读取，默认
                ``data/research_control/cfa_reports.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "CFA_REPORT_DB_PATH",
                "data/research_control/cfa_reports.db",
            )
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = Path(__file__).resolve().parents[3] / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """获取 SQLite 连接上下文管理器。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """幂等迁移：创建表与索引，记录 schema 版本。"""
        with self._lock, self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cfa_schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cfa_reports ("
                "report_id TEXT PRIMARY KEY, "
                "company_symbol TEXT NOT NULL DEFAULT '', "
                "as_of TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL DEFAULT '', "
                "overall_status TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cfa_reports_symbol "
                "ON cfa_reports(company_symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cfa_reports_as_of "
                "ON cfa_reports(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cfa_reports_status "
                "ON cfa_reports(overall_status)"
            )
            conn.execute(
                "INSERT INTO cfa_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def _payload(item: Any) -> dict[str, Any]:
        """统一转为 dict payload。"""
        if hasattr(item, "to_dict"):
            payload = item.to_dict()
            if isinstance(payload, dict):
                return payload
            raise TypeError("report to_dict() must return a mapping")
        if is_dataclass(item):
            try:
                return asdict(item)
            except Exception:
                pass
        return dict(item) if isinstance(item, dict) else {"value": item}

    def save_report(self, report: Any) -> None:
        """保存 CFA 报告。

        Args:
            report: CfaReport 对象或 dict

        Raises:
            ValueError: 当缺少 report_id
        """
        payload = self._payload(report)
        report_id = str(payload.get("report_id") or "")
        if not report_id:
            raise ValueError("missing report_id")
        company_symbol = str(payload.get("company_symbol") or "")
        as_of = str(payload.get("as_of") or "")
        created_at = str(payload.get("created_at") or "")
        overall_status = str(payload.get("overall_status") or "")
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO cfa_reports("
                "report_id, company_symbol, as_of, created_at, "
                "overall_status, payload_json) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(report_id) DO UPDATE SET "
                "company_symbol=excluded.company_symbol, "
                "as_of=excluded.as_of, "
                "created_at=excluded.created_at, "
                "overall_status=excluded.overall_status, "
                "payload_json=excluded.payload_json",
                (
                    report_id,
                    company_symbol,
                    as_of,
                    created_at,
                    overall_status,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """按 report_id 获取 CFA 报告。

        Args:
            report_id: 报告唯一 ID

        Returns:
            报告 dict；无返回 None
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM cfa_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
        return _decode_payload(row[0]) if row else None

    def list_reports(
        self,
        *,
        company_symbol: str = "",
        as_of: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询 CFA 报告列表，支持 symbol/as_of/status/limit 过滤。

        Args:
            company_symbol: 公司代码过滤
            as_of: PIT 过滤，仅返回 as_of <= 该时点的记录
            status: overall_status 过滤
            limit: 上限，1-1000

        Returns:
            报告 dict 列表，按 created_at DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        if status:
            clauses.append("overall_status=?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM cfa_reports"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(
                query, (*params, _safe_limit(limit)),
            ).fetchall()
        return [payload for row in rows if (payload := _decode_payload(row[0])) is not None]

    def get_latest_report(
        self,
        company_symbol: str,
        as_of: str = "",
    ) -> dict[str, Any] | None:
        """按 PIT 取最新报告。

        Args:
            company_symbol: 公司代码
            as_of: PIT 时点；空表示不限

        Returns:
            最新报告 dict；无返回 None
        """
        items = self.list_reports(
            company_symbol=company_symbol,
            as_of=as_of,
            limit=1,
        )
        return items[0] if items else None

    def stats(self) -> dict[str, Any]:
        """返回仓储统计信息。"""
        with self.connect() as conn:
            count = int(
                conn.execute("SELECT COUNT(*) FROM cfa_reports").fetchone()[0]
            )
        return {"schema_version": self.SCHEMA_VERSION, "counts": {"cfa_reports": count}}
