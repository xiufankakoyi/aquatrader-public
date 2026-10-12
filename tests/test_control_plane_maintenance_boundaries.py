from __future__ import annotations

from unittest.mock import MagicMock

from core.research.control_plane.contracts import Claim, EvidenceRecord
from core.research.control_plane.maintenance import invalidate_ambiguous_entity_evidence


def _repository(evidence, claim):
    repository = MagicMock()
    repository.list_evidence.return_value = [evidence]
    repository.get_claim.return_value = claim
    repository.list_claims.return_value = []
    repository.list_evidence.side_effect = [
        [evidence],
        [evidence],
    ]
    return repository


def _evidence() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="ev-1",
        claim_id="claim-1",
        extraction_method="grounded_rule",
        quote="",
        locator={"url": "http://[invalid"},
        metadata={"source_backend": "unknown"},
    )


def test_empty_quote_and_malformed_url_do_not_abort_maintenance() -> None:
    evidence = _evidence()
    repository = _repository(evidence, Claim(claim_id="claim-1"))

    result = invalidate_ambiguous_entity_evidence(repository)

    assert result["invalidated_evidence"] == 0
    repository.save_evidence.assert_called_once()


def test_malformed_metadata_and_claim_attributes_degrade_safely() -> None:
    evidence = _evidence()
    evidence.metadata = ["bad"]
    evidence.locator = ["bad"]
    claim = Claim(claim_id="claim-1")
    claim.attributes = ["bad"]
    repository = _repository(evidence, claim)

    result = invalidate_ambiguous_entity_evidence(repository)

    assert result["invalidated_evidence"] == 0
    assert isinstance(evidence.metadata, dict)
    assert isinstance(evidence.locator, dict)
