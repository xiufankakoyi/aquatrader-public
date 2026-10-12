"""FactOutcomeStore 状态机边界回归测试。"""
from __future__ import annotations

import pytest

from core.research.control_plane.fact_outcome import FactOutcomeRecord, FactOutcomeStore
from core.research.control_plane.repository import ResearchRepository


def test_fact_label_defaults_to_pending_when_missing():
    record = FactOutcomeRecord(fact_label=None)

    assert record.fact_label == "pending"


def test_resolve_rejects_corrupted_existing_fact_label(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    repository.save_fact_outcome(FactOutcomeRecord(
        record_id="fact_1",
        transition_id="transition_1",
        claim_id="claim_1",
        fact_label="corrupted",
    ))

    with pytest.raises(ValueError, match="当前 fact_label"):
        FactOutcomeStore(repository).resolve_outcome("fact_1", "confirmed")
