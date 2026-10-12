from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from core.research.control_plane.calibration import CalibrationController
from core.research.control_plane.learning_loop import (
    AppendOnlyViolation,
    AttributionCategory,
    OutcomeObservation,
    PredictionRecord,
    ProductionLearningLoop,
)
from core.research.control_plane.repository import ResearchRepository


@pytest.fixture
def loop(tmp_path) -> ProductionLearningLoop:
    repository = ResearchRepository(tmp_path / "research_control.db")
    return ProductionLearningLoop(
        repository,
        clock=lambda: datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
    )


def _prediction(**overrides) -> PredictionRecord:
    values = {
        "prediction_id": "pred-1",
        "entity_id": "600519.SH",
        "research_question": "next_report_revenue_growth",
        "predicted_value": 0.08,
        "confidence": 0.7,
        "as_of": "2026-07-01T15:00:00+00:00",
        "due_at": "2026-07-02T15:00:00+00:00",
        "model_version": "research-v1",
        "policy_version": "policy-v1",
        "evidence_refs": ["evidence-1"],
        "feature_snapshot_hash": "sha256:abc",
    }
    values.update(overrides)
    return PredictionRecord(**values)


def _observation(**overrides) -> OutcomeObservation:
    values = {
        "observation_id": "obs-1",
        "prediction_id": "pred-1",
        "observed_at": "2026-07-03T09:00:00+00:00",
        "actual_value": 0.03,
        "was_correct": False,
        "attribution": AttributionCategory.MODEL,
        "attribution_reason": "growth assumption was too optimistic",
        "evidence_refs": ["announcement-1"],
    }
    values.update(overrides)
    return OutcomeObservation(**values)


def test_prediction_ledger_is_append_only(loop):
    first = loop.append_prediction(_prediction())

    with pytest.raises(AppendOnlyViolation):
        loop.append_prediction(_prediction(predicted_value=0.09))

    assert loop.get_prediction(first.prediction_id) == first


def test_outcome_must_be_cross_day_and_causally_available(loop):
    loop.append_prediction(_prediction())

    with pytest.raises(ValueError, match="cross-day"):
        loop.append_observation(
            _observation(observed_at="2026-07-01T16:00:00+00:00")
        )

    with pytest.raises(ValueError, match="due_at"):
        loop.append_observation(
            _observation(observed_at="2026-07-02T14:59:59+00:00")
        )


@pytest.mark.parametrize(
    "category",
    [
        AttributionCategory.MODEL,
        AttributionCategory.DATA,
        AttributionCategory.MAPPING,
        AttributionCategory.TIMING,
        AttributionCategory.ASSUMPTION,
        AttributionCategory.MARKET_REGIME,
    ],
)
def test_every_supported_attribution_is_persisted(loop, category):
    prediction_id = f"pred-{category.value}"
    loop.append_prediction(_prediction(prediction_id=prediction_id))
    observation = loop.append_observation(
        _observation(
            observation_id=f"obs-{category.value}",
            prediction_id=prediction_id,
            attribution=category,
        )
    )

    assert observation.attribution is category
    assert loop.list_observations(prediction_id=prediction_id) == [observation]


def test_correction_appends_a_new_row_and_replay_respects_cutoff(loop):
    loop.append_prediction(_prediction())
    original = loop.append_observation(_observation())
    correction = loop.append_observation(
        _observation(
            observation_id="obs-2",
            observed_at="2026-07-04T09:00:00+00:00",
            actual_value=0.04,
            supersedes_observation_id=original.observation_id,
        )
    )

    earlier = loop.replay("2026-07-03T12:00:00+00:00")
    later = loop.replay("2026-07-05T12:00:00+00:00")

    assert len(loop.list_observations(prediction_id="pred-1")) == 2
    assert earlier[0].latest_observation == original
    assert later[0].latest_observation == correction


def test_correction_timestamp_must_advance(loop):
    loop.append_prediction(_prediction())
    original = loop.append_observation(_observation())

    with pytest.raises(ValueError, match="later than"):
        loop.append_observation(
            _observation(
                observation_id="obs-2",
                observed_at="2026-07-02T18:00:00+00:00",
                supersedes_observation_id=original.observation_id,
            )
        )


def test_shadow_proposal_uses_existing_gate_and_short_sample_stays_inactive(loop):
    loop.append_prediction(_prediction())
    loop.append_observation(_observation())

    proposal = loop.generate_shadow_proposal(
        as_of="2026-07-05T12:00:00+00:00"
    )

    assert proposal.status == "shadow"
    assert proposal.domain == "alpha"
    assert proposal.sample_size == 1
    assert proposal.metrics["attribution_counts"]["MODEL"] == 1
    assert loop.repository.get_calibration(proposal.calibration_id) is not None
    with pytest.raises(ValueError, match="1 < 20"):
        CalibrationController(loop.repository).activate(
            proposal.calibration_id,
            "ai_internal_reviewer",
            test_run_id="not-reached",
            code_version="not-reached",
        )


def test_shadow_proposal_deduplicates_same_entity_due_cohort(loop):
    loop.append_prediction(_prediction())
    loop.append_prediction(
        _prediction(
            prediction_id="pred-rerun",
            as_of="2026-07-01T16:00:00+00:00",
        )
    )
    loop.append_observation(_observation())
    loop.append_observation(
        _observation(
            observation_id="obs-rerun",
            prediction_id="pred-rerun",
        )
    )

    proposal = loop.generate_shadow_proposal(
        as_of="2026-07-05T12:00:00+00:00"
    )

    assert proposal.sample_size == 1
    assert proposal.metrics["duplicate_cohorts_excluded"] == 1
    assert proposal.metrics["duplicate_prediction_ids"] == ["pred-rerun"]


def test_alpha_sample_size_excludes_data_and_timing_failures(loop):
    for index, category in enumerate(
        (AttributionCategory.MODEL, AttributionCategory.DATA, AttributionCategory.TIMING)
    ):
        prediction_id = f"pred-{index}"
        loop.append_prediction(_prediction(
            prediction_id=prediction_id,
            entity_id=f"entity-{index}",
        ))
        loop.append_observation(_observation(
            observation_id=f"obs-{index}",
            prediction_id=prediction_id,
            attribution=category,
        ))

    proposal = loop.generate_shadow_proposal(
        as_of="2026-07-05T12:00:00+00:00"
    )

    assert proposal.sample_size == 1
    assert proposal.metrics["total_outcomes"] == 3
    assert proposal.metrics["excluded_non_model_outcomes"] == 2


def test_replay_is_deterministic_and_excludes_future_records(loop):
    loop.append_prediction(_prediction())
    loop.append_prediction(
        _prediction(
            prediction_id="pred-future",
            as_of="2026-07-10T15:00:00+00:00",
            due_at="2026-07-11T15:00:00+00:00",
        )
    )
    loop.append_observation(_observation())
    cutoff = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)

    first = loop.replay(cutoff)
    second = loop.replay(cutoff.isoformat())

    assert first == second
    assert [item.prediction.prediction_id for item in first] == ["pred-1"]


def test_due_queue_only_contains_mature_unsettled_predictions(loop):
    loop.append_prediction(_prediction())
    loop.append_prediction(
        _prediction(
            prediction_id="pred-later",
            as_of="2026-07-02T15:00:00+00:00",
            due_at="2026-07-10T15:00:00+00:00",
        )
    )

    assert [
        item.prediction_id
        for item in loop.list_due_predictions("2026-07-05T12:00:00+00:00")
    ] == ["pred-1"]

    loop.append_observation(_observation())
    assert loop.list_due_predictions("2026-07-05T12:00:00+00:00") == []


def test_late_prediction_cannot_backfill_historical_replay(tmp_path):
    now = [datetime(2026, 7, 10, 12, tzinfo=timezone.utc)]
    loop = ProductionLearningLoop(
        ResearchRepository(tmp_path / "late-prediction.db"),
        clock=lambda: now[0],
    )

    stored = loop.append_prediction(
        _prediction(created_at="2020-01-01T00:00:00+00:00")
    )

    assert stored.recorded_at == "2026-07-10T12:00:00+00:00"
    assert stored.created_at == stored.recorded_at
    assert loop.replay("2026-07-05T12:00:00+00:00") == []
    assert loop.list_due_predictions("2026-07-05T12:00:00+00:00") == []
    assert len(loop.replay("2026-07-11T12:00:00+00:00")) == 1


def test_late_observation_and_correction_do_not_rewrite_history(tmp_path):
    now = [datetime(2026, 7, 1, 12, tzinfo=timezone.utc)]
    loop = ProductionLearningLoop(
        ResearchRepository(tmp_path / "late-outcome.db"),
        clock=lambda: now[0],
    )
    loop.append_prediction(_prediction())
    now[0] = datetime(2026, 7, 3, 10, tzinfo=timezone.utc)
    original = loop.append_observation(_observation())
    now[0] = datetime(2026, 7, 10, 10, tzinfo=timezone.utc)
    correction = loop.append_observation(
        _observation(
            observation_id="obs-2",
            observed_at="2026-07-04T09:00:00+00:00",
            actual_value=0.04,
            supersedes_observation_id=original.observation_id,
            created_at="2020-01-01T00:00:00+00:00",
        )
    )

    before_original = loop.replay("2026-07-03T09:30:00+00:00")
    before_correction = loop.replay("2026-07-05T12:00:00+00:00")
    after_correction = loop.replay("2026-07-11T12:00:00+00:00")

    assert before_original[0].latest_observation is None
    assert before_correction[0].latest_observation == original
    assert after_correction[0].latest_observation == correction
    assert correction.recorded_at == "2026-07-10T10:00:00+00:00"


def test_legacy_tables_gain_conservative_recorded_at_on_migration(tmp_path):
    repository = ResearchRepository(tmp_path / "legacy.db")
    legacy = asdict(_prediction())
    legacy.pop("recorded_at", None)
    with repository.transaction() as conn:
        conn.execute(
            "CREATE TABLE learning_predictions ("
            "prediction_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, "
            "entity_id TEXT NOT NULL, payload_json TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE learning_observations ("
            "observation_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, "
            "observed_at TEXT NOT NULL, supersedes_observation_id TEXT NOT NULL "
            "DEFAULT '', payload_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO learning_predictions"
            "(prediction_id,as_of,entity_id,payload_json) VALUES(?,?,?,?)",
            (
                legacy["prediction_id"],
                legacy["as_of"],
                legacy["entity_id"],
                json.dumps(legacy),
            ),
        )

    loop = ProductionLearningLoop(
        repository,
        clock=lambda: datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
    )

    assert loop.replay("2026-07-05T12:00:00+00:00") == []
    migrated = loop.replay("2026-07-11T12:00:00+00:00")[0].prediction
    assert migrated.recorded_at == "2026-07-10T12:00:00+00:00"


def test_learning_ledger_rejects_bad_inputs_and_skips_bad_rows(loop):
    with pytest.raises(ValueError, match="prediction type"):
        loop.append_prediction(None)
    assert loop.get_prediction([]) is None
    with loop.repository.transaction() as conn:
        conn.execute(
            "INSERT INTO learning_predictions"
            "(prediction_id, as_of, entity_id, recorded_at, payload_json) "
            "VALUES(?,?,?,?,?)",
            ("bad", "2026-07-01T00:00:00+00:00", "bad", "", "not-json"),
        )
    assert loop.get_prediction("bad") is None
    assert loop.replay("2026-07-05T12:00:00+00:00") == []
