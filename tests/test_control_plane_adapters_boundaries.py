"""控制面适配器边界回归测试。"""
from __future__ import annotations

from core.research.control_plane.adapters import adapt_candidate, adapt_legacy_evidence, adapt_raw_document


def test_raw_document_parses_boolean_text_instead_of_using_truthiness():
    _, document = adapt_raw_document({
        "content_available": "false",
        "content_text": "",
        "parse_status": None,
    })

    assert document.content_available is False
    assert document.parse_status == "unknown"


def test_legacy_evidence_parses_derived_boolean_text():
    evidence = adapt_legacy_evidence({"is_derived": "false"})

    assert evidence.evidence_kind == "document"


def test_candidate_adapter_does_not_stringify_malformed_scalars():
    claim = adapt_candidate({
        "candidate_id": None,
        "subject_id": None,
        "status": None,
        "attributes": {"mapping_confidence": 2.0},
    })

    assert claim.subject_id == ""
    assert claim.research_status == "DISCOVERED"
    assert claim.mapping_confidence == 1.0
