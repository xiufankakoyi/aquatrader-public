"""研究下游门禁边界回归测试。"""
from __future__ import annotations

from core.research.control_plane.contracts import Claim, EvidenceRecord
from core.research.control_plane.downstream import explainable_company_scores


def test_downstream_scores_are_bounded_for_extreme_event_features():
    claim = Claim(
        claim_id="claim_1",
        object_id="company_1",
        research_status="ACTIVE",
        evidence_confidence=1.0,
        mapping_confidence=1.0,
    )
    evidence = {"claim_1": [EvidenceRecord(
        evidence_id="evidence_1",
        claim_id="claim_1",
        stance="support",
        quote="事实",
        grounding_status="exact",
    )]}

    rows = explainable_company_scores(
        [claim], evidence,
        {"company_1": {"extreme": 100.0, "broken": float("inf")}},
        policy_version="v1",
    )

    assert rows
    assert 0.0 <= rows[0]["score"] <= 1.0
    assert rows[0]["event_features"]["extreme"] == 1.0
