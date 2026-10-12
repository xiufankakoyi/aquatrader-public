"""P0 八维研究 readiness 构建器。

按 docs/research/institutional/p0-contracts.md §4 构建：
- fact_completeness
- source_traceability
- source_independence
- pit_integrity
- forecast_input_completeness
- model_computability
- valuation_computability
- research_uncertainty

严格规则：
1. market_implied_scenarios 只能提升 model_computability.market_expectation_computability，
   不能提升 forecast_input_completeness。
2. 缺少产能/销量/ASP/份额/成本前瞻证据时，model_computability 必须为 blocked 或 partial。
3. 技术状态仅写入 technical_context，不参与 fact_completeness / forecast_input_completeness /
   valuation_computability。
4. 所有维度均缺失时，research_uncertainty.score 必须为 1.0；不得返回中性 0.5。
5. score=None 表示不可计算；禁止返回中性分。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    ReadinessDimension,
    ResearchReadiness,
)


FORWARD_DRIVER_METRICS: tuple[str, ...] = (
    "capacity", "sales_volume", "price", "asp", "market_share", "cost",
)


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _has_forward_driver_evidence(
    profit_bridge: dict[str, Any],
    expectation_context: dict[str, Any],
) -> tuple[bool, list[str]]:
    """检查前瞻驱动证据。

    返回 (是否充分, 缺失字段列表)。
    market_implied_scenarios 不算前瞻驱动证据。
    """
    forward_assumptions = _records(profit_bridge.get("forward_driver_assumptions"))
    forward_disclosed = _records(profit_bridge.get("forward_disclosed_indicators"))

    # market_implied 不属于前瞻驱动证据
    present_metrics: set[str] = set()
    for assumption in forward_assumptions:
        metric = str(assumption.get("metric") or "").lower()
        if metric:
            present_metrics.add(metric)
    for indicator in forward_disclosed:
        # 定性指引（direction=increase）不构成数值前瞻证据
        if indicator.get("eligible_as_forward_assumption"):
            metric = str(indicator.get("metric") or "").lower()
            if metric:
                present_metrics.add(metric)
    # 观察到的事实只能算历史，不能直接当成前瞻证据；但若公司指引中明确给出未来数值，则可记入
    missing = [metric for metric in FORWARD_DRIVER_METRICS if metric not in present_metrics]
    # 至少需要一个数值前瞻证据
    sufficient = bool(present_metrics) and not any(
        metric in {"capacity", "sales_volume", "asp", "market_share", "cost"}
        and metric not in present_metrics
        for metric in ("capacity", "sales_volume", "asp", "market_share", "cost")
    )
    return sufficient, missing


def _build_fact_completeness(
    *,
    industry_context: dict[str, Any],
    financial_quality: dict[str, Any],
    expectation_context: dict[str, Any],
) -> ReadinessDimension:
    """构建事实完整度维度。

    技术状态不参与此维度。
    """
    verified_edges = _records(industry_context.get("verified_edges"))
    observed_facts = _records(industry_context.get("observed_facts"))
    forward_plans = _records(industry_context.get("forward_plans"))
    financial_status = str(financial_quality.get("status") or "")
    verified_expectations = _records(expectation_context.get("verified_expectations"))
    evidence_refs: list[str] = []
    for source in (verified_edges, observed_facts, forward_plans):
        for edge in source:
            for evidence in _records(edge.get("evidence")):
                evidence_id = str(evidence.get("evidence_id") or "")
                if evidence_id and evidence_id not in evidence_refs:
                    evidence_refs.append(evidence_id)
    missing_items: list[str] = []
    if not verified_edges:
        missing_items.append("verified_causal_edges_missing")
    if financial_status != "ok":
        missing_items.append(f"financial_quality_status:{financial_status or 'unknown'}")
    if not verified_expectations:
        missing_items.append("verified_market_expectation_missing")
    if missing_items:
        status = "partial" if evidence_refs else "blocked"
        score: float | None = min(1.0, len(evidence_refs) / 10.0) if evidence_refs else None
    else:
        status = "ok"
        score = 1.0
    return ReadinessDimension(
        status=status,
        score=score,
        missing_items=missing_items,
        evidence_refs=evidence_refs,
    )


def _build_source_traceability(
    *,
    source_snapshots: dict[str, Any],
    financial_quality: dict[str, Any],
) -> ReadinessDimension:
    """构建来源可追溯率维度。"""
    total = 0
    traced = 0
    missing_items: list[str] = []
    evidence_refs: list[str] = []
    for capability, snapshot in source_snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        status = str(snapshot.get("status") or "")
        if status in {"empty", "failed"}:
            missing_items.append(f"source_{capability}_{status}")
            continue
        records = _records(snapshot.get("records"))
        total += len(records)
        for record in records:
            if record.get("provider") or record.get("source") or record.get("source_trace_id"):
                traced += 1
            evidence_id = record.get("evidence_id") or record.get("source_evidence_id")
            if evidence_id:
                evidence_refs.append(str(evidence_id))
    if total == 0:
        return ReadinessDimension(
            status="blocked",
            score=None,
            missing_items=missing_items or ["no_source_records_available"],
            evidence_refs=[],
        )
    score = round(traced / total, 6)
    status = "ok" if score >= 0.9 else "partial" if score >= 0.5 else "blocked"
    return ReadinessDimension(
        status=status,
        score=score,
        missing_items=missing_items,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
    )


def _build_source_independence(
    *,
    industry_context: dict[str, Any],
    expectation_context: dict[str, Any],
    source_snapshots: dict[str, Any],
) -> ReadinessDimension:
    """构建来源独立性维度。

    通过来源数量与发布渠道多样性评估。
    """
    sources: set[str] = set()
    missing_items: list[str] = []
    for edge in _records(industry_context.get("verified_edges")):
        for evidence in _records(edge.get("evidence")):
            trace_id = str(evidence.get("source_trace_id") or "")
            if trace_id:
                sources.add(trace_id)
    for snapshot in source_snapshots.values():
        if not isinstance(snapshot, dict):
            continue
        provider = str(snapshot.get("provider") or "")
        if provider:
            sources.add(provider)
    for expectation in _records(expectation_context.get("verified_expectations")):
        trace_id = str(expectation.get("source_trace_id") or "")
        if trace_id:
            sources.add(trace_id)
    if not sources:
        return ReadinessDimension(
            status="blocked",
            score=None,
            missing_items=["independent_sources_missing"],
            evidence_refs=[],
        )
    score = min(1.0, len(sources) / 3.0)
    status = "ok" if len(sources) >= 3 else "partial" if len(sources) >= 2 else "blocked"
    if status != "ok":
        missing_items.append("independent_sources_insufficient")
    return ReadinessDimension(
        status=status,
        score=score,
        missing_items=missing_items,
        evidence_refs=sorted(sources),
    )


def _build_pit_integrity(
    *,
    source_snapshots: dict[str, Any],
    expectation_context: dict[str, Any],
    as_of: str,
) -> ReadinessDimension:
    """构建 PIT 完整性维度。

    统计被排除的未来记录与未知时间记录数。
    """
    total_excluded_future = 0
    total_excluded_unknown = 0
    total_visible = 0
    missing_items: list[str] = []
    for capability, snapshot in source_snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        total_excluded_future += _safe_count(snapshot.get("excluded_future_records"))
        total_excluded_unknown += _safe_count(snapshot.get("excluded_unknown_time_records"))
        total_visible += len(_records(snapshot.get("records")))
    if total_visible == 0:
        return ReadinessDimension(
            status="not_computable",
            score=None,
            missing_items=["no_visible_records_for_pit"],
            evidence_refs=[],
        )
    pit_score = round(1.0 - (total_excluded_future + total_excluded_unknown) / max(1, total_visible + total_excluded_future + total_excluded_unknown), 6)
    status = "ok" if total_excluded_future == 0 and total_excluded_unknown == 0 else "partial"
    if total_excluded_future > 0:
        missing_items.append(f"future_records_excluded:{total_excluded_future}")
    if total_excluded_unknown > 0:
        missing_items.append(f"unknown_time_records_excluded:{total_excluded_unknown}")
    return ReadinessDimension(
        status=status,
        score=pit_score,
        missing_items=missing_items,
        evidence_refs=[],
    )


def _build_forecast_input_completeness(
    *,
    profit_bridge: dict[str, Any],
    industry_context: dict[str, Any],
) -> ReadinessDimension:
    """构建前瞻驱动证据完整度维度。

    严格规则：market_implied_scenarios 不算前瞻驱动证据。
    """
    market_implied_scenarios = _records(profit_bridge.get("market_implied_scenarios"))
    sufficient, missing = _has_forward_driver_evidence(
        profit_bridge, industry_context,
    )
    forward_assumptions = _records(profit_bridge.get("forward_driver_assumptions"))
    # 只有 eligible_as_forward_assumption=True 的披露项才能算前瞻数值证据；
    # 定性指引（如 direction=increase）不构成数值前瞻证据，必须过滤掉。
    forward_disclosed_eligible = [
        indicator for indicator in _records(profit_bridge.get("forward_disclosed_indicators"))
        if indicator.get("eligible_as_forward_assumption")
    ]
    evidence_refs: list[str] = []
    for assumption in forward_assumptions:
        evidence_id = str(assumption.get("source_evidence_id") or "")
        if evidence_id:
            evidence_refs.append(evidence_id)
    for indicator in forward_disclosed_eligible:
        evidence_id = str(indicator.get("source_evidence_id") or "")
        if evidence_id:
            evidence_refs.append(evidence_id)
    # 即使有 market_implied_scenarios，前瞻驱动证据仍可能不足
    if sufficient:
        status = "ok"
        score: float | None = 1.0
    elif forward_assumptions or forward_disclosed_eligible:
        status = "partial"
        score = round(len(evidence_refs) / max(1, len(FORWARD_DRIVER_METRICS)), 6)
    else:
        status = "blocked"
        score = None
        if market_implied_scenarios:
            missing.append("market_implied_scenarios_not_counted_as_forecast_input")
    return ReadinessDimension(
        status=status,
        score=score,
        missing_items=missing,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
    )


def _build_model_computability(
    *,
    profit_bridge: dict[str, Any],
    forecast_completeness: ReadinessDimension,
) -> ReadinessDimension:
    """构建模型可计算性维度。

    market_implied_scenarios 只能提升 market_expectation_computability 子项，
    不能提升 forecast_input_completeness。
    """
    market_implied_scenarios = _records(profit_bridge.get("market_implied_scenarios"))
    forward_assumptions = _records(profit_bridge.get("forward_driver_assumptions"))
    forward_guidance = _records(profit_bridge.get("forward_guidance"))
    profit_status = str(profit_bridge.get("status") or "")
    missing_items: list[str] = []
    evidence_refs: list[str] = []
    for assumption in forward_assumptions:
        evidence_id = str(assumption.get("source_evidence_id") or "")
        if evidence_id:
            evidence_refs.append(evidence_id)
    # market_implied 子项可计算
    market_expectation_computable = bool(market_implied_scenarios)
    # forecast_input 必须独立判定
    if forecast_completeness.status in {"blocked", "not_computable"}:
        status = "blocked"
        score: float | None = None
        missing_items.append("forecast_input_completeness_blocked")
    elif forecast_completeness.status == "partial":
        status = "partial"
        score = 0.5 if market_expectation_computable else 0.3
        if not market_expectation_computable:
            missing_items.append("market_expectation_computability_missing")
    else:
        # forecast_completeness.status == "ok"
        status = "ok"
        score = 1.0 if market_expectation_computable else 0.8
        if not market_expectation_computable:
            missing_items.append("market_expectation_computability_partial")
    if not forward_guidance and profit_status != "partial":
        missing_items.append("forward_guidance_missing")
    return ReadinessDimension(
        status=status,
        score=score,
        missing_items=missing_items,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
    )


def _build_valuation_computability(
    *,
    valuation_context: dict[str, Any],
    valuation_state: dict[str, Any],
) -> ReadinessDimension:
    """构建估值可计算性维度。

    技术状态不参与此维度。
    """
    status = str(valuation_context.get("status") or valuation_state.get("status") or "")
    raw_history = valuation_context.get("history")
    history = raw_history if isinstance(raw_history, Mapping) else {}
    missing_items: list[str] = []
    if status == "ok":
        readiness_status = "ok"
        score: float | None = 1.0
    elif status == "partial":
        readiness_status = "partial"
        score = 0.5
        missing_items.append("valuation_history_partial")
    else:
        readiness_status = "blocked"
        score = None
        missing_items.append("valuation_history_missing")
    missing_metrics = [
        name for name, item in history.items()
        if not isinstance(item, Mapping) or item.get("percentile") is None
    ]
    if missing_metrics:
        missing_items.extend(f"valuation_percentile_missing:{name}" for name in missing_metrics)
    return ReadinessDimension(
        status=readiness_status,
        score=score,
        missing_items=missing_items,
        evidence_refs=[],
    )


def _build_research_uncertainty(
    *,
    fact_completeness: ReadinessDimension,
    source_traceability: ReadinessDimension,
    forecast_input_completeness: ReadinessDimension,
    model_computability: ReadinessDimension,
    valuation_computability: ReadinessDimension,
) -> ReadinessDimension:
    """构建研究不确定性维度。

    所有维度均缺失时 score 必须为 1.0；不得返回中性 0.5。
    """
    dimensions = [
        fact_completeness, source_traceability, forecast_input_completeness,
        model_computability, valuation_computability,
    ]
    available_scores = [dim.score for dim in dimensions if dim.score is not None]
    if not available_scores:
        # 所有维度均缺失时不确定性最高
        return ReadinessDimension(
            status="not_computable",
            score=1.0,
            missing_items=["all_readiness_dimensions_missing"],
            evidence_refs=[],
        )
    # 不确定性 = 1 - 平均置信度
    avg_confidence = sum(available_scores) / len(available_scores)
    uncertainty = max(0.0, min(1.0, 1.0 - avg_confidence))
    status = "ok" if uncertainty <= 0.3 else "partial" if uncertainty <= 0.6 else "blocked"
    return ReadinessDimension(
        status=status,
        score=round(uncertainty, 6),
        missing_items=[],
        evidence_refs=[],
    )


def build_research_readiness(
    *,
    industry_context: dict[str, Any],
    financial_quality: dict[str, Any],
    profit_bridge: dict[str, Any],
    expectation_context: dict[str, Any],
    valuation_context: dict[str, Any],
    valuation_state: dict[str, Any],
    source_snapshots: dict[str, Any],
    as_of: str,
) -> ResearchReadiness:
    """构建 P0 八维研究 readiness。

    严格按 docs/research/institutional/p0-contracts.md §4 边界构建：
    - market_implied_scenarios 只能提升 model_computability.market_expectation_computability
    - 缺少前瞻驱动证据时 model_computability 必须为 blocked 或 partial
    - 技术状态不参与 fact_completeness / forecast_input_completeness / valuation_computability
    - research_uncertainty.score 必须为 1.0 当所有维度均缺失时

    Args:
        industry_context: 产业因果上下文。
        financial_quality: 财务质量字典。
        profit_bridge: 利润桥产物。
        expectation_context: 期望上下文。
        valuation_context: 估值上下文。
        valuation_state: 当前估值状态。
        source_snapshots: 来源快照集合。
        as_of: PIT 时点。

    Returns:
        ResearchReadiness 八维对象。
    """
    fact_completeness = _build_fact_completeness(
        industry_context=industry_context,
        financial_quality=financial_quality,
        expectation_context=expectation_context,
    )
    source_traceability = _build_source_traceability(
        source_snapshots=source_snapshots,
        financial_quality=financial_quality,
    )
    source_independence = _build_source_independence(
        industry_context=industry_context,
        expectation_context=expectation_context,
        source_snapshots=source_snapshots,
    )
    pit_integrity = _build_pit_integrity(
        source_snapshots=source_snapshots,
        expectation_context=expectation_context,
        as_of=as_of,
    )
    forecast_input_completeness = _build_forecast_input_completeness(
        profit_bridge=profit_bridge,
        industry_context=industry_context,
    )
    model_computability = _build_model_computability(
        profit_bridge=profit_bridge,
        forecast_completeness=forecast_input_completeness,
    )
    valuation_computability = _build_valuation_computability(
        valuation_context=valuation_context,
        valuation_state=valuation_state,
    )
    research_uncertainty = _build_research_uncertainty(
        fact_completeness=fact_completeness,
        source_traceability=source_traceability,
        forecast_input_completeness=forecast_input_completeness,
        model_computability=model_computability,
        valuation_computability=valuation_computability,
    )
    return ResearchReadiness(
        fact_completeness=fact_completeness,
        source_traceability=source_traceability,
        source_independence=source_independence,
        pit_integrity=pit_integrity,
        forecast_input_completeness=forecast_input_completeness,
        model_computability=model_computability,
        valuation_computability=valuation_computability,
        research_uncertainty=research_uncertainty,
    )
