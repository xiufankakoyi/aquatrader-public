from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .readiness import build_research_readiness


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def build_research_assessment(
    *,
    industry_context: dict[str, Any],
    financial_quality: dict[str, Any],
    profit_bridge: dict[str, Any],
    structured_consensus: dict[str, Any],
    valuation_context: dict[str, Any],
    technical_context: dict[str, Any],
    degradation_reasons: list[str],
    expectation_context: dict[str, Any] | None = None,
    source_snapshots: dict[str, Any] | None = None,
    valuation_state: dict[str, Any] | None = None,
    as_of: str = "",
) -> dict[str, Any]:
    """Compose an auditable research thesis; never emit a trading action.

    P0 边界：
    - 新增 research_readiness 八维字段作为正式门禁。
    - 旧 evidence_completeness / dimension_ready 字段保留兼容读取但标记 deprecated。
    - technical_context 不参与 readiness 任何维度。
    """
    industry_context = industry_context if isinstance(industry_context, Mapping) else {}
    financial_quality = financial_quality if isinstance(financial_quality, Mapping) else {}
    profit_bridge = profit_bridge if isinstance(profit_bridge, Mapping) else {}
    structured_consensus = structured_consensus if isinstance(structured_consensus, Mapping) else {}
    valuation_context = valuation_context if isinstance(valuation_context, Mapping) else {}
    technical_context = technical_context if isinstance(technical_context, Mapping) else {}
    financial_metrics = (
        financial_quality.get("metrics")
        if isinstance(financial_quality.get("metrics"), Mapping)
        else {}
    )
    raw_periods = structured_consensus.get("periods")
    periods = raw_periods if isinstance(raw_periods, Mapping) else {}
    annual_consensus = {
        period: value for period, value in periods.items()
        if str(period).endswith("Q4") and isinstance(value, Mapping)
    }
    consensus_dispersion: dict[str, Any] = {}
    for period, item in annual_consensus.items():
        metric = item.get("net_profit_yuan")
        metric = metric if isinstance(metric, Mapping) else {}
        median = _number(metric.get("median"))
        if median is None or median == 0:
            continue
        low = _number(metric.get("low"))
        high = _number(metric.get("high"))
        if low is None:
            low = median
        if high is None:
            high = median
        consensus_dispersion[period] = {
            "source_count": _safe_count(item.get("source_count")),
            "net_profit_interquartile_spread_ratio": round(
                (high - low) / abs(median),
                6,
            ),
        }
    scenarios = _records(profit_bridge.get("market_implied_scenarios"))
    disclosed_directions = _records(profit_bridge.get("forward_disclosed_indicators"))
    direction_increase = any(item.get("direction") == "increase" for item in disclosed_directions)
    execution_constraints = []
    for scenario in scenarios:
        required_change = _number(scenario.get("capacity_change_required"))
        execution_constraints.append({
            "period": scenario.get("period"),
            "required_capacity_change_under_flat_efficiency": required_change,
            "management_direction_disclosed": "increase" if direction_increase else "unknown",
            "direction_consistent": (
                bool(direction_increase and required_change is not None and required_change > 0)
            ),
            "magnitude_verified": False,
            "model_kind": scenario.get("model_kind"),
        })
    dimension_ready = {
        "causal_chain": industry_context.get("chain_status") == "verified",
        "financial_quality": financial_quality.get("status") == "ok",
        "profit_constraints": bool(scenarios or profit_bridge.get("forward_guidance")),
        "market_expectations": structured_consensus.get("status") == "verified",
        "valuation_history": valuation_context.get("status") == "ok",
        "technical_context": technical_context.get("status") == "ok",
    }
    evidence_completeness = round(sum(dimension_ready.values()) / len(dimension_ready), 6)
    claims = _records(industry_context.get("verified_edges"))
    ontology = _records(industry_context.get("ontology_edges"))
    gaps = [str(gap) for gap in degradation_reasons if gap]
    raw_history = valuation_context.get("history")
    valuation_history = raw_history if isinstance(raw_history, Mapping) else {}

    readiness = build_research_readiness(
        industry_context=industry_context,
        financial_quality=financial_quality,
        profit_bridge=profit_bridge,
        expectation_context=expectation_context or {},
        valuation_context=valuation_context,
        valuation_state=valuation_state or {},
        source_snapshots=source_snapshots or {},
        as_of=as_of,
    )

    return {
        "scope": "research_prioritization_not_trade_signal",
        "status": "evidence_complete_with_open_assumptions" if evidence_completeness == 1.0 else "research_incomplete",
        # Deprecated: 旧单一汇总分数，保留兼容读取，不得作为 P0 门禁。
        "evidence_completeness": evidence_completeness,
        "deprecation_notice": (
            "evidence_completeness 与 dimension_ready 已 deprecated；"
            "P0 正式门禁使用 research_readiness 八维字段。"
        ),
        "dimension_ready": dimension_ready,
        "research_readiness": readiness.to_dict(),
        "core_thesis": {
            "product_claims": [
                {"claim_id": edge.get("claim_id"), "relation": edge.get("relation"), "object": edge.get("object_name")}
                for edge in claims if str(edge.get("relation") or "").upper() in {"PRODUCES", "DEVELOPS"}
            ],
            "demand_claims": [
                {"claim_id": edge.get("claim_id"), "relation": edge.get("relation"), "object": edge.get("object_name")}
                for edge in claims if str(edge.get("relation") or "").upper() in {"SUPPLIES_TO", "BENEFITS_FROM"}
            ],
            "upstream_constraints": [
                {"claim_id": edge.get("claim_id"), "subject": edge.get("subject_name"), "object": edge.get("object_name")}
                for edge in ontology if str(edge.get("relation") or "").upper() == "REQUIRES"
            ],
        },
        "fundamental_observations": {
            key: value.get("value") if isinstance(value, dict) else value
            for key, value in financial_metrics.items()
        },
            "consensus_dispersion": consensus_dispersion,
            "valuation_percentiles": {
                metric: values.get("percentile")
                for metric, values in valuation_history.items()
                if isinstance(values, Mapping)
            },
        "execution_constraints": execution_constraints,
        "technical_state": technical_context.get("technical_state", "UNKNOWN"),
        "open_research_gaps": list(dict.fromkeys(gaps)),
        "next_research_focus": [
            {
                "gap": gap,
                "reason": (
                    "需要公司披露的数值幅度，才能把市场隐含约束升级为公司前瞻利润桥"
                    if gap == "company_forward_driver_evidence_missing" else "继续补充可定位证据"
                ),
            }
            for gap in dict.fromkeys(gaps)
        ],
        "prohibited_outputs": ["buy", "sell", "position", "take_profit", "stop_loss"],
    }
