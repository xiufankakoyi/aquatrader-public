from __future__ import annotations

from core.research.control_plane.gaps import GapType, ResearchGap


def test_gap_normalizes_type_and_bounded_factors() -> None:
    gap = ResearchGap(
        "gap-1",
        "unknown-gap",
        research_importance=9,
        evidence_gap=-2,
        expected_information_gain="bad",
    )

    assert gap.gap_type == GapType.EVIDENCE.value
    assert gap.research_importance == 1.0
    assert gap.evidence_gap == 0.0
    assert gap.expected_information_gain == 0.5


def test_gap_metadata_cannot_override_task_identity() -> None:
    gap = ResearchGap(
        "gap-1",
        GapType.MAPPING.value,
        target_claim_id="claim-1",
        metadata={"gap_id": "wrong", "gap_type": "wrong", "extra": "kept"},
    )

    task = gap.to_task()

    assert task.task_type == GapType.MAPPING.value
    assert task.params["gap_id"] == "gap-1"
    assert task.params["gap_type"] == GapType.MAPPING.value
    assert task.params["extra"] == "kept"
