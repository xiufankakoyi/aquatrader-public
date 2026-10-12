from __future__ import annotations

import sqlite3

import pytest

from core.research.control_plane.contracts import Claim, EvidenceRecord
from core.research.control_plane.repository import ResearchRepository


def test_future_schema_version_is_not_downgraded(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    repository = ResearchRepository(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            ("999",),
        )

    with pytest.raises(RuntimeError, match="unsupported research schema version"):
        ResearchRepository(db_path)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "999"


def test_claim_gate_finds_support_without_loading_a_bounded_evidence_page(tmp_path, monkeypatch) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(claim_id="claim-1")
    repository.save_claim(claim)
    assert repository.save_evidence(EvidenceRecord(
        evidence_id="evidence-1",
        claim_id=claim.claim_id,
        source_trace_id="source-1",
        stance="support",
    )) is True

    monkeypatch.setattr(repository, "list_evidence", lambda *args, **kwargs: [])
    claim.research_status = "SUPPORTED"
    repository.save_claim(claim)

    assert repository.get_claim(claim.claim_id).research_status == "SUPPORTED"
