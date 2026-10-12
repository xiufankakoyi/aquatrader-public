from __future__ import annotations

from core.research.control_plane.learning_bridge import (
    AsOfDailyBar,
    DailyLearningBridge,
)
from core.research.control_plane.learning_loop import (
    AttributionCategory,
    PredictionRecord,
    ProductionLearningLoop,
)
from core.research.control_plane.repository import ResearchRepository
from datetime import datetime, timezone


class FakeBarReader:
    def __init__(self, bars):
        self.bars = bars

    def first_bar_after(self, symbol, *, after_date, as_of):
        return self.bars.get(symbol)


def _bridge(tmp_path) -> DailyLearningBridge:
    repository = ResearchRepository(tmp_path / "research_control.db")
    return DailyLearningBridge(
        ProductionLearningLoop(
            repository,
            clock=lambda: datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
        )
    )


def _watch(**overrides):
    row = {
        "symbol": "600519.SH",
        "status": "WATCH",
        "research_validated": True,
        "claim_ids": ["claim-1"],
        "source_snapshot_id": "snapshot-1",
        "final_score": 0.72,
        "mapping_confidence": 0.9,
        "degradation_reasons": [],
    }
    row.update(overrides)
    return row


def test_capture_only_accepts_evidenced_ready_or_watch_and_is_idempotent(tmp_path):
    bridge = _bridge(tmp_path)
    observations = [
        _watch(),
        _watch(symbol="000001.SZ", status="UNKNOWN"),
        _watch(symbol="000002.SZ", claim_ids=[], source_snapshot_id=""),
    ]

    first = bridge.capture_observations(
        observations,
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    second = bridge.capture_observations(
        observations,
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    assert first.appended == 1
    assert first.skipped == 2
    assert second.appended == 0
    assert second.duplicates == 1
    prediction = bridge.loop.get_prediction(first.prediction_ids[0])
    assert prediction is not None
    assert prediction.predicted_value is True


def test_same_entity_and_due_session_is_not_learned_twice_on_rerun(tmp_path):
    bridge = _bridge(tmp_path)
    first = bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T07:30:00+08:00",
        due_at="2026-07-02T16:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    second = bridge.capture_observations(
        [_watch(final_score=0.81)],
        as_of="2026-07-01T08:00:00+08:00",
        due_at="2026-07-02T16:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    assert first.appended == 1
    assert second.appended == 0
    assert second.duplicates == 1
    assert second.prediction_ids == first.prediction_ids


def test_zero_quant_score_uses_evidence_confidence_for_watch_hypothesis(tmp_path):
    bridge = _bridge(tmp_path)

    result = bridge.capture_observations(
        [_watch(final_score=0.0, mapping_confidence=0.8, membership_confidence=0.6)],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    prediction = bridge.loop.get_prediction(result.prediction_ids[0])
    assert prediction is not None
    assert prediction.predicted_value is True
    assert prediction.confidence == 0.7


def test_missing_bar_stays_pending_and_does_not_create_shadow(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00",
        bar_reader=FakeBarReader({}),
    )

    assert result.settled == 0
    assert result.pending == 1
    assert result.shadow_calibration_id == ""
    assert bridge.loop.list_due_predictions("2026-07-02T15:30:00+08:00")


def test_future_available_bar_is_rejected_without_fabricating_outcome(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    reader = FakeBarReader(
        {
            "600519.SH": AsOfDailyBar(
                symbol="600519.SH",
                trade_date="2026-07-02",
                available_at="2026-07-03T15:01:00+08:00",
                close=100.0,
                previous_close=99.0,
                source_ref="daily_ohlcv:600519.SH:2026-07-02",
                price_available_at_ref="exchange_close:SSE:2026-07-02",
                benchmark_return=0.002,
                benchmark_source_ref="index_daily:000300.SH:2026-07-02",
                benchmark_available_at="2026-07-02T15:06:00+08:00",
                benchmark_available_at_ref="exchange_close:CSI300:2026-07-02",
            )
        }
    )

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00", bar_reader=reader
    )

    assert result.settled == 0
    assert result.pending == 1
    assert any(
        reason.startswith("future_available_at:")
        for reason in result.degradation_reasons
    )


def test_valid_bar_settles_and_only_creates_shadow_proposal(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    reader = FakeBarReader(
        {
            "600519.SH": AsOfDailyBar(
                symbol="600519.SH",
                trade_date="2026-07-02",
                available_at="2026-07-02T15:05:00+08:00",
                close=101.0,
                previous_close=100.0,
                source_ref="daily_ohlcv:600519.SH:2026-07-02",
                price_available_at_ref="exchange_close:SSE:2026-07-02",
                benchmark_return=0.002,
                benchmark_source_ref="index_daily:000300.SH:2026-07-02",
                benchmark_available_at="2026-07-02T15:06:00+08:00",
                benchmark_available_at_ref="exchange_close:CSI300:2026-07-02",
            )
        }
    )

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00", bar_reader=reader
    )
    observations = bridge.loop.list_observations(
        prediction_id=result.settled_prediction_ids[0]
    )
    proposal = bridge.loop.repository.get_calibration(
        result.shadow_calibration_id
    )

    assert result.settled == 1
    assert observations[0].actual_value == 0.008
    assert observations[0].was_correct is True
    assert observations[0].attribution is AttributionCategory.MODEL
    assert proposal is not None
    assert proposal.status == "shadow"
    status = bridge.status(
        "2026-07-02T15:30:00+08:00",
        capture=bridge.capture_observations(
            [_watch()],
            as_of="2026-07-01T15:10:00+08:00",
            due_at="2026-07-02T15:00:00+08:00",
            model_version="monitor-v1",
            policy_version="policy-v1",
        ),
        settlement=result,
    )
    assert status == {
        "predictions_appended": 0,
        "due_pending": 0,
        "outcomes_appended": 1,
        "attribution_counts": {"MODEL": 1},
        "latest_shadow_run_id": result.shadow_calibration_id,
        "latest_shadow_status": "shadow",
    }


def test_missed_due_day_settles_on_first_later_observation_as_timing_feedback(
    tmp_path,
):
    bridge = _bridge(tmp_path)
    capture = bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    reader = FakeBarReader(
        {
            "600519.SH": AsOfDailyBar(
                symbol="600519.SH",
                trade_date="2026-07-03",
                available_at="2026-07-03T15:05:00+08:00",
                close=101.0,
                previous_close=100.0,
                source_ref="daily_ohlcv:600519.SH:2026-07-03",
                price_available_at_ref="exchange_close:SSE:2026-07-03",
                benchmark_return=0.002,
                benchmark_source_ref="index_daily:000300.SH:2026-07-03",
                benchmark_available_at="2026-07-03T15:06:00+08:00",
                benchmark_available_at_ref="exchange_close:CSI300:2026-07-03",
            )
        }
    )

    result = bridge.settle_after_close(
        as_of="2026-07-03T15:30:00+08:00",
        bar_reader=reader,
    )
    observation = bridge.loop.list_observations(
        prediction_id=capture.prediction_ids[0]
    )[0]

    assert result.settled == 1
    assert result.pending == 0
    assert observation.attribution is AttributionCategory.TIMING
    assert observation.attribution_reason == "scheduled_observation_day_missed"
    assert observation.outcome_context["scheduled_due_date"] == "2026-07-02"
    assert observation.outcome_context["observation_delay_days"] == 1


def test_explicit_data_mapping_timing_assumption_and_market_rules(tmp_path):
    bridge = _bridge(tmp_path)
    as_of = "2026-07-01T15:10:00+08:00"
    rows = [
        _watch(symbol="DATA", degradation_reasons=["provider_partial"]),
        _watch(symbol="MAPPING", mapping_confidence=0.3),
        _watch(symbol="TIMING", timing_risk=True),
        _watch(symbol="ASSUMPTION", assumption_refs=["assumption-1"]),
        _watch(symbol="MARKET"),
    ]
    bridge.capture_observations(
        rows,
        as_of=as_of,
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    bars = {
        symbol: AsOfDailyBar(
            symbol=symbol,
            trade_date="2026-07-02",
            available_at="2026-07-02T15:05:00+08:00",
            close=97.0 if symbol == "MARKET" else 99.0,
            previous_close=100.0,
            source_ref=f"daily:{symbol}",
            price_available_at_ref=f"exchange_close:{symbol}",
            benchmark_return=-0.02 if symbol == "MARKET" else 0.0,
            benchmark_source_ref=f"index:{symbol}",
            benchmark_available_at="2026-07-02T15:06:00+08:00",
            benchmark_available_at_ref=f"exchange_close:index:{symbol}",
            degradation_reasons=["bar_partial"] if symbol == "DATA" else [],
        )
        for symbol in ["DATA", "MAPPING", "TIMING", "ASSUMPTION", "MARKET"]
    }

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00",
        bar_reader=FakeBarReader(bars),
    )
    categories = {
        bridge.loop.list_observations(prediction_id=prediction_id)[0].attribution
        for prediction_id in result.settled_prediction_ids
    }

    assert categories == {
        AttributionCategory.DATA,
        AttributionCategory.MAPPING,
        AttributionCategory.TIMING,
        AttributionCategory.ASSUMPTION,
        AttributionCategory.MARKET_REGIME,
    }


def test_missing_benchmark_keeps_prediction_pending(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.capture_observations(
        [_watch()],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )
    bar = AsOfDailyBar(
        symbol="600519.SH",
        trade_date="2026-07-02",
        available_at="2026-07-02T15:05:00+08:00",
        close=101.0,
        previous_close=100.0,
        source_ref="daily:600519.SH",
        price_available_at_ref="exchange_close:SSE:2026-07-02",
    )

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00",
        bar_reader=FakeBarReader({"600519.SH": bar}),
    )

    assert result.settled == 0
    assert result.pending == 1
    assert "missing_benchmark" in result.degradation_reasons[0]


def test_bad_observation_numbers_are_skipped_without_stopping_batch(tmp_path):
    bridge = _bridge(tmp_path)

    result = bridge.capture_observations(
        [
            _watch(),
            _watch(symbol="BAD-MAPPING", mapping_confidence="N/A"),
            _watch(symbol="BAD-SCORE", final_score=float("nan")),
            "bad-shape",
        ],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    assert result.appended == 1
    assert result.skipped == 3
    assert any("invalid_mapping_confidence" in reason for reason in result.degradation_reasons)
    assert any("invalid_final_score" in reason for reason in result.degradation_reasons)
    assert "invalid_observation_shape" in result.degradation_reasons


def test_bad_price_numbers_stay_pending_without_stopping_batch(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.capture_observations(
        [_watch(symbol="GOOD"), _watch(symbol="NAN"), _watch(symbol="INF")],
        as_of="2026-07-01T15:10:00+08:00",
        due_at="2026-07-02T15:00:00+08:00",
        model_version="monitor-v1",
        policy_version="policy-v1",
    )

    def bar(symbol, *, close=101.0, benchmark=0.0):
        return AsOfDailyBar(
            symbol=symbol,
            trade_date="2026-07-02",
            available_at="2026-07-02T15:05:00+08:00",
            close=close,
            previous_close=100.0,
            source_ref=f"daily:{symbol}",
            price_available_at_ref=f"available:{symbol}",
            benchmark_return=benchmark,
            benchmark_source_ref=f"benchmark:{symbol}",
            benchmark_available_at="2026-07-02T15:06:00+08:00",
            benchmark_available_at_ref=f"benchmark_available:{symbol}",
        )

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00",
        bar_reader=FakeBarReader(
            {
                "GOOD": bar("GOOD"),
                "NAN": bar("NAN", close=float("nan")),
                "INF": bar("INF", benchmark=float("inf")),
            }
        ),
    )

    assert result.settled == 1
    assert result.pending == 2
    assert any(reason.startswith("invalid_bar_price:NAN") for reason in result.degradation_reasons)
    assert any(reason.startswith("invalid_benchmark:INF") for reason in result.degradation_reasons)


def test_bad_persisted_mapping_is_pending_without_calling_reader(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.loop.append_prediction(
        PredictionRecord(
            prediction_id="legacy-bad-mapping",
            entity_id="BAD",
            research_question=bridge.QUESTION,
            predicted_value=0.5,
            confidence=0.5,
            as_of="2026-07-01T15:10:00+08:00",
            due_at="2026-07-02T15:00:00+08:00",
            model_version="legacy",
            policy_version="legacy",
            evidence_refs=["claim"],
            feature_snapshot_hash="sha256:legacy",
            context={"mapping_confidence": "N/A"},
        )
    )
    reader = FakeBarReader({})

    result = bridge.settle_after_close(
        as_of="2026-07-02T15:30:00+08:00",
        bar_reader=reader,
    )

    assert result.pending == 1
    assert result.degradation_reasons == ["invalid_mapping_confidence:BAD"]
