from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .contracts import (
    ConfidenceInterval,
    RESEARCH_ASSUMPTION_SOURCE_TYPES,
    RESEARCH_ASSUMPTION_STATUSES,
    ResearchAssumption,
    ResearchFact,
)


_FINANCE_METRICS: dict[str, tuple[str, str]] = {
    "revenue": ("CNY", "yuan"),
    "net_profit": ("CNY", "yuan"),
    "operating_cash_flow": ("CNY", "yuan"),
    "gross_margin": ("", "ratio"),
    "net_margin": ("", "ratio"),
    "roe": ("", "ratio"),
    "roic": ("", "ratio"),
    "debt_ratio": ("", "ratio"),
}


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item]


def _iso_date(value: Any) -> str:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _facts_from_finance_snapshot(
    symbol: str,
    snapshot: dict[str, Any],
) -> list[ResearchFact]:
    capability = str(snapshot.get("capability") or "finance_snapshot")
    snapshot_provider = str(snapshot.get("provider") or "")
    facts: list[ResearchFact] = []
    for record in _records(snapshot.get("records")):
        provider = str(record.get("provider") or snapshot_provider)
        period = str(record.get("period") or "")
        published_at = _iso_date(record.get("published_at"))
        if not provider or not period or not published_at:
            continue
        trace_id = f"{provider}:{capability}:{symbol}:{period}"
        for metric, (currency, unit) in _FINANCE_METRICS.items():
            value = record.get(metric)
            if value is None:
                continue
            evidence_id = f"{trace_id}:{metric}"
            facts.append(ResearchFact(
                fact_id=evidence_id,
                entity_id=symbol,
                metric_or_relation=metric,
                value=value,
                unit=unit,
                currency=currency,
                period=period,
                published_at=published_at,
                retrieved_at=str(record.get("updated_at") or ""),
                available_at=published_at,
                valid_from=published_at,
                valid_to="",
                source_trace_id=trace_id,
                evidence_ids=[evidence_id],
                grounding_status="grounding_ok",
                confidence=ConfidenceInterval(),
                observation_kind="company_disclosed",
            ))
    return facts


def _grounding_status(value: Any) -> str:
    status = str(value or "").lower()
    if status in {"exact", "grounded", "grounding_ok"}:
        return "grounding_ok"
    if status in {"partial", "grounding_partial"}:
        return "grounding_partial"
    return "missing"


def _fact_from_edge(edge: dict[str, Any], observation_kind: str) -> ResearchFact | None:
    claim_id = str(edge.get("claim_id") or "")
    if not claim_id:
        return None
    evidence = _records(edge.get("evidence"))
    primary = evidence[0] if evidence else {}
    confidence = edge.get("evidence_confidence")
    score = (
        float(confidence)
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        and isfinite(float(confidence))
        else None
    )
    metadata = primary.get("metadata") if isinstance(primary.get("metadata"), dict) else {}
    published_at = str(primary.get("published_at") or "")
    return ResearchFact(
        fact_id=claim_id,
        entity_id=str(edge.get("subject_id") or ""),
        metric_or_relation=str(edge.get("relation") or ""),
        value=edge.get("object_name"),
        unit="relation_target",
        currency="",
        period=str(metadata.get("period") or edge.get("period") or ""),
        published_at=published_at,
        retrieved_at=str(primary.get("retrieved_at") or ""),
        available_at=str(primary.get("available_at") or published_at),
        valid_from=str(primary.get("valid_from") or ""),
        valid_to=str(primary.get("valid_to") or ""),
        source_trace_id=str(primary.get("source_trace_id") or ""),
        evidence_ids=[str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")],
        grounding_status=_grounding_status(primary.get("grounding_status")),
        confidence=ConfidenceInterval(low=score, high=score),
        observation_kind=observation_kind,
    )


def _fact_from_driver(
    symbol: str,
    driver: dict[str, Any],
    evidence_by_claim: dict[str, dict[str, Any]],
) -> ResearchFact | None:
    claim_id = str(driver.get("claim_id") or "")
    if not claim_id:
        return None
    evidence = evidence_by_claim.get(claim_id, {})
    evidence = evidence if isinstance(evidence, Mapping) else {}
    published_at = str(driver.get("published_at") or evidence.get("published_at") or "")
    evidence_id = str(driver.get("source_evidence_id") or evidence.get("evidence_id") or "")
    unit = str(driver.get("unit") or "")
    return ResearchFact(
        fact_id=claim_id,
        entity_id=symbol,
        metric_or_relation=str(driver.get("metric") or ""),
        value=driver.get("value"),
        unit=unit,
        currency="CNY" if unit == "yuan" else "",
        period=str(driver.get("period") or ""),
        published_at=published_at,
        retrieved_at=str(evidence.get("retrieved_at") or ""),
        available_at=str(evidence.get("available_at") or published_at),
        valid_from=str(evidence.get("valid_from") or ""),
        valid_to=str(evidence.get("valid_to") or ""),
        source_trace_id=str(evidence.get("source_trace_id") or ""),
        evidence_ids=[evidence_id] if evidence_id else [],
        grounding_status=_grounding_status(evidence.get("grounding_status")),
        confidence=ConfidenceInterval(),
        observation_kind="verified_fact",
    )


def build_formal_research_records(
    *,
    symbol: str,
    industry_context: dict[str, Any],
    profit_bridge: dict[str, Any],
    source_snapshots: dict[str, Any] | None = None,
) -> tuple[list[ResearchFact], list[ResearchAssumption]]:
    """把兼容字段投影为 P0 正式记录；来源类型不明的假设拒绝进入正式集合。"""
    facts: list[ResearchFact] = []
    evidence_by_claim: dict[str, dict[str, Any]] = {}
    observed_claim_ids = {
        str(item.get("claim_id"))
        for item in _records(profit_bridge.get("observed_historical_drivers"))
        if item.get("claim_id")
    }
    for key, kind in (
        ("verified_edges", "verified_fact"),
        ("observed_facts", "verified_fact"),
        ("forward_plans", "company_disclosed"),
    ):
        for edge in _records(industry_context.get(key)):
            claim_id = str(edge.get("claim_id") or "")
            evidence = _records(edge.get("evidence"))
            if claim_id and evidence:
                evidence_by_claim[claim_id] = evidence[0]
            if key == "observed_facts" and claim_id in observed_claim_ids:
                continue
            fact = _fact_from_edge(edge, kind)
            if fact is not None:
                facts.append(fact)
    for driver in _records(profit_bridge.get("observed_historical_drivers")):
        fact = _fact_from_driver(symbol, driver, evidence_by_claim)
        if fact is not None:
            facts.append(fact)
    finance_snapshot = (source_snapshots or {}).get("finance") or {}
    if isinstance(finance_snapshot, dict):
        facts.extend(_facts_from_finance_snapshot(symbol, finance_snapshot))

    assumptions: list[ResearchAssumption] = []
    for item in _records(profit_bridge.get("forward_driver_assumptions")):
        source_type = str(item.get("source_type") or "")
        assumption_id = str(item.get("assumption_id") or item.get("claim_id") or "")
        interval = item.get("interval") if isinstance(item.get("interval"), dict) else {}
        if (
            source_type not in RESEARCH_ASSUMPTION_SOURCE_TYPES
            or not assumption_id
            or not item.get("owner")
            or not item.get("falsification_condition")
            or (interval.get("low") is None and interval.get("high") is None)
        ):
            continue
        status = str(item.get("status") or "draft")
        if status not in RESEARCH_ASSUMPTION_STATUSES:
            status = "draft"
        assumptions.append(ResearchAssumption(
            assumption_id=assumption_id,
            symbol=symbol,
            variable_name=str(item.get("variable_name") or item.get("metric") or ""),
            interval=ConfidenceInterval(low=interval.get("low"), high=interval.get("high")),
            unit=str(item.get("unit") or ""),
            period=str(item.get("period") or ""),
            source_type=source_type,
            source_fact_ids=_string_list(item.get("source_fact_ids")),
            source_evidence_ids=_string_list(item.get("source_evidence_ids")),
            created_at=str(item.get("created_at") or ""),
            available_at=str(item.get("available_at") or ""),
            expires_at=str(item.get("expires_at") or ""),
            owner=str(item.get("owner") or ""),
            model_version=str(item.get("model_version") or "research_assumption_v1"),
            status=status,
            falsification_condition=str(item.get("falsification_condition") or ""),
        ))
    return facts, assumptions
