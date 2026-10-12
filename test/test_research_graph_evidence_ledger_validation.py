from __future__ import annotations

import pytest

from core.research_graph.evidence_ledger import EvidenceLedger, validate_evidence_for_ledger
from core.research_graph.models import Evidence
from core.research_graph.schema import EvidenceLevel
from core.research_graph.store import GraphStore


def _evidence(**overrides) -> Evidence:
    payload = {
        "evidence_id": "ev_valid",
        "source_type": "announcement",
        "source_title": "test",
        "source_id": "src_1",
        "document_id": "doc_1",
        "quote": "company disclosed a traceable fact",
        "evidence_level": EvidenceLevel.SPECULATIVE.value,
    }
    payload.update(overrides)
    return Evidence(**payload)


def test_validate_evidence_accepts_canonical_record() -> None:
    validate_evidence_for_ledger(_evidence())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_id", ""),
        ("quote", ""),
        ("source_type", ""),
        ("evidence_level", "OBSERVED"),
    ],
)
def test_validate_evidence_rejects_required_field_gaps(field: str, value: str) -> None:
    ev = _evidence(**{field: value})
    with pytest.raises(ValueError):
        validate_evidence_for_ledger(ev)


def test_validate_evidence_requires_source_or_document_id() -> None:
    ev = _evidence(source_id="", document_id="")
    with pytest.raises(ValueError, match="source_id_or_document_id_required"):
        validate_evidence_for_ledger(ev)


def test_validate_evidence_requires_llm_trace_for_llm_extraction() -> None:
    ev = _evidence(extracted_by="llm_extractor")
    with pytest.raises(ValueError, match="llm_trace_path_required"):
        validate_evidence_for_ledger(ev)

    validate_evidence_for_ledger(
        _evidence(
            extracted_by="llm_extractor",
            metadata={"extraction_trace_path": "data/research_runs/run/llm_trace.jsonl"},
        )
    )


def test_ledger_does_not_write_invalid_evidence_to_graph_store() -> None:
    store = GraphStore()
    ledger = EvidenceLedger(store)

    with pytest.raises(ValueError):
        ledger.add(_evidence(evidence_level="OBSERVED"))

    assert store.evidence == {}

