"""研究下游门禁：SAG 事件特征先于公司评分，并输出可追溯解释。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import Claim, ClaimStatus, EvidenceRecord, _safe_float, _safe_text
from .policies import evidence_is_current, evidence_visible_as_of


RESEARCH_STATUSES = {ClaimStatus.ACTIVE.value, ClaimStatus.TRUSTED.value}


def eligible_claims(
    claims: list[Claim], evidence_by_claim: dict[str, list[EvidenceRecord]],
    *, as_of: str = "", historical: bool = False,
) -> list[Claim]:
    claims = claims if isinstance(claims, list) else []
    evidence_by_claim = evidence_by_claim if isinstance(evidence_by_claim, Mapping) else {}
    result: list[Claim] = []
    for claim in claims:
        if not isinstance(claim, Claim):
            continue
        if claim.research_status not in RESEARCH_STATUSES or claim.conflict_status not in {"", "none", "resolved"}:
            continue
        if (claim.object_id or claim.high_impact) and claim.mapping_confidence < 0.6:
            continue
        evidence = evidence_by_claim.get(claim.claim_id, [])
        evidence = evidence if isinstance(evidence, list) else []
        visible = [
            item for item in evidence
            if isinstance(item, EvidenceRecord)
            if evidence_is_current(item, as_of)
            and (not as_of or evidence_visible_as_of(item, as_of, historical=historical))
        ]
        if visible:
            result.append(claim)
    return result


def explainable_company_scores(
    claims: list[Claim], evidence_by_claim: dict[str, list[EvidenceRecord]],
    event_features: dict[str, dict[str, float]], *, policy_version: str,
    as_of: str = "",
) -> list[dict[str, Any]]:
    """先合并 SAG 事件特征，再计算研究相关分；不生成交易建议。"""
    allowed = eligible_claims(claims, evidence_by_claim, as_of=as_of, historical=bool(as_of))
    evidence_by_claim = evidence_by_claim if isinstance(evidence_by_claim, Mapping) else {}
    event_features = event_features if isinstance(event_features, Mapping) else {}
    grouped: dict[str, dict[str, Any]] = {}
    for claim in allowed:
        company_id = claim.object_id or claim.subject_id
        if not company_id:
            continue
        events = event_features.get(company_id, {})
        events = events if isinstance(events, Mapping) else {}
        safe_events = {
            str(key): max(0.0, min(1.0, _safe_float(value)))
            for key, value in events.items()
            if isinstance(key, (str, int))
        }
        event_score = sum(safe_events.values()) / max(len(safe_events), 1)
        score = max(0.0, min(
            1.0,
            0.65 * max(0.0, min(1.0, claim.evidence_confidence))
            + 0.20 * max(0.0, min(1.0, claim.mapping_confidence))
            + 0.15 * event_score,
        ))
        row = grouped.setdefault(company_id, {
            "company_id": company_id, "score": 0.0, "claim_ids": [], "evidence_ids": [],
            "event_features": safe_events, "as_of": _safe_text(as_of), "policy_version": _safe_text(policy_version),
        })
        row["score"] = max(row["score"], round(score, 4))
        row["claim_ids"].append(claim.claim_id)
        row["evidence_ids"].extend(
            item.evidence_id
            for item in evidence_by_claim.get(claim.claim_id, [])
            if isinstance(item, EvidenceRecord)
        )
    return sorted(grouped.values(), key=lambda item: (-item["score"], item["company_id"]))
