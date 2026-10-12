from __future__ import annotations

from unittest.mock import MagicMock

from core.research.control_plane.contracts import Claim, EvidenceRecord, EvidenceStance
from core.research.control_plane.local_retrieval import LocalEvidenceRetriever


def _claim() -> Claim:
    return Claim(claim_id="claim-1", counter_search_completed=True)


def _evidence(evidence_id: str, lineage_id: str, first_party: bool) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-1",
        lineage_id=lineage_id,
        is_first_party=first_party,
        stance=EvidenceStance.SUPPORT.value,
        quote="evidence",
    )


def test_malformed_repository_result_degrades_to_empty() -> None:
    repository = MagicMock()
    repository.list_evidence.return_value = None

    result = LocalEvidenceRetriever(repository).query(_claim())

    assert result.reason_code == "local_empty"
    assert result.evidence == []


def test_duplicate_first_party_lineage_does_not_count_as_two_sources() -> None:
    repository = MagicMock()
    repository.list_evidence.return_value = [
        _evidence("ev-1", "lineage-1", True),
        _evidence("ev-2", "lineage-1", True),
        _evidence("ev-3", "lineage-2", False),
    ]

    result = LocalEvidenceRetriever(
        repository, min_first_party_sources=2
    ).query(_claim())

    assert result.hit is False
    assert result.first_party_sources == 1
    assert result.independent_sources == 2


def test_mutated_counter_flag_is_not_truthy() -> None:
    repository = MagicMock()
    repository.list_evidence.return_value = [
        _evidence("ev-1", "lineage-1", True),
        _evidence("ev-2", "lineage-2", False),
    ]
    claim = _claim()
    claim.counter_search_completed = "false"

    assert LocalEvidenceRetriever(repository).query(claim).hit is False
