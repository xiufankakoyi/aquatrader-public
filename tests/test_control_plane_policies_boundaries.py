from __future__ import annotations

from core.research.control_plane.contracts import Claim, EvidenceRecord
from core.research.control_plane.policies import ClaimPolicy, evidence_is_current


def _evidence(evidence_id: str, stance: str = "support") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-1",
        lineage_id=evidence_id,
        stance=stance,
        quote="evidence",
        source_quality=1.0,
        match_score=1.0,
        grounding_status="exact",
        metadata={"slots": ["identity", "relationship"]},
    )


def test_invalid_as_of_fails_closed_for_currentness() -> None:
    evidence = _evidence("ev-1")

    assert evidence_is_current(evidence, "not-a-timestamp") is False


def test_malformed_nested_fields_degrade_without_raising() -> None:
    claim = Claim(
        claim_id="claim-1",
        required_slots=["identity", "relationship"],
        counter_search_completed=True,
    )
    evidence = _evidence("ev-1")
    evidence.metadata = None
    evidence.source_quality = "bad"
    evidence.match_score = "bad"
    claim.attributes = []
    claim.high_impact = "false"

    result = ClaimPolicy().evaluate(claim, [evidence])

    assert result.target_status == "candidate"
    assert 0.0 <= result.confidence <= 1.0


def test_extreme_counter_sample_does_not_overflow_sigmoid() -> None:
    claim = Claim(claim_id="claim-1", counter_search_completed=True)
    evidence = [_evidence(f"ev-{index}", "counter") for index in range(1000)]

    result = ClaimPolicy().evaluate(claim, evidence)

    assert result.target_status in {"discovered", "rejected"}
    assert 0.0 <= result.confidence <= 1.0
