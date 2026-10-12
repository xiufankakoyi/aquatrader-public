from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.research.control_plane.learning_loop import (
    AttributionCategory,
    OutcomeObservation,
    PredictionRecord,
    ProductionLearningLoop,
)
from core.research.control_plane.repository import ResearchRepository


def _loop(tmp_path):
    return ProductionLearningLoop(
        ResearchRepository(tmp_path / "learning.db"),
        clock=lambda: datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
    )


def _prediction(**overrides):
    values = {
        "prediction_id": "pred-1",
        "entity_id": "600519.SH",
        "research_question": "question",
        "predicted_value": 0.1,
        "confidence": 0.7,
        "as_of": "2026-07-01T15:00:00Z",
        "due_at": "2026-07-02T15:00:00Z",
        "model_version": "model-v1",
        "policy_version": "policy-v1",
        "evidence_refs": ("evidence-1",),
        "feature_snapshot_hash": "sha256:test",
        "context": None,
    }
    values.update(overrides)
    return PredictionRecord(**values)


def test_prediction_normalizes_z_timestamps_and_collection_fields(tmp_path):
    stored = _loop(tmp_path).append_prediction(_prediction())

    assert stored.as_of == "2026-07-01T15:00:00+00:00"
    assert stored.due_at == "2026-07-02T15:00:00+00:00"
    assert stored.evidence_refs == ["evidence-1"]
    assert stored.context == {}


def test_prediction_preserves_supplied_business_timezone(tmp_path):
    stored = _loop(tmp_path).append_prediction(_prediction(
        as_of="2026-07-01T15:00:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
    ))

    assert stored.as_of == "2026-07-01T15:00:00+08:00"
    assert stored.due_at == "2026-07-02T15:00:00+08:00"


def test_non_boolean_observation_is_rejected(tmp_path):
    loop = _loop(tmp_path)
    loop.append_prediction(_prediction())

    with pytest.raises(ValueError, match="was_correct"):
        loop.append_observation(
            OutcomeObservation(
                prediction_id="pred-1",
                observed_at="2026-07-03T09:00:00Z",
                actual_value=0.1,
                was_correct="false",
                attribution=AttributionCategory.MODEL,
                attribution_reason="test",
                evidence_refs=["evidence-2"],
            )
        )


def test_non_finite_prediction_value_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="serializable"):
        _loop(tmp_path).append_prediction(_prediction(predicted_value=float("nan")))


def test_corrupted_prediction_row_is_skipped_by_due_queue(tmp_path):
    loop = _loop(tmp_path)
    with loop.repository.transaction() as conn:
        conn.execute(
            "INSERT INTO learning_predictions"
            "(prediction_id, as_of, entity_id, recorded_at, payload_json) "
            "VALUES(?,?,?,?,?)",
            (
                "bad",
                "2026-07-01T00:00:00+00:00",
                "600000.SH",
                "2026-07-01T00:00:00+00:00",
                '{"prediction_id":"bad","as_of":"bad","due_at":"bad"}',
            ),
        )

    assert loop.list_due_predictions("2026-07-05T00:00:00Z") == []
