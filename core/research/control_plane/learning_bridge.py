"""Minimal daily-pipeline bridge for the governed production learning loop."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Protocol

from .learning_loop import (
    AppendOnlyViolation,
    AttributionCategory,
    OutcomeObservation,
    PredictionRecord,
    ProductionLearningLoop,
)


def _datetime(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return result
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp must be a timezone-aware ISO datetime") from exc


def _digest(payload: Any) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return [item for item in value if item]


@dataclass(frozen=True)
class AsOfDailyBar:
    """A daily bar whose availability is explicit and auditable."""

    symbol: str
    trade_date: str
    available_at: str
    close: float
    previous_close: float
    source_ref: str
    price_available_at_ref: str = ""
    benchmark_return: float | None = None
    benchmark_source_ref: str = ""
    benchmark_available_at: str = ""
    benchmark_available_at_ref: str = ""
    degradation_reasons: list[str] = field(default_factory=list)


class StrictAsOfBarReader(Protocol):
    def first_bar_after(
        self, symbol: str, *, after_date: str, as_of: str
    ) -> AsOfDailyBar | None: ...


@dataclass
class CaptureResult:
    appended: int = 0
    duplicates: int = 0
    skipped: int = 0
    prediction_ids: list[str] = field(default_factory=list)
    degradation_reasons: list[str] = field(default_factory=list)


@dataclass
class SettlementResult:
    settled: int = 0
    pending: int = 0
    settled_prediction_ids: list[str] = field(default_factory=list)
    shadow_calibration_id: str = ""
    attribution_counts: dict[str, int] = field(default_factory=dict)
    degradation_reasons: list[str] = field(default_factory=list)


class DailyLearningBridge:
    """Capture evidenced observations and settle them without auto-promotion."""

    QUESTION = "next_session_excess_return_positive"

    def __init__(self, loop: ProductionLearningLoop) -> None:
        self.loop = loop

    def capture_observations(
        self,
        observations: Iterable[dict[str, Any]],
        *,
        as_of: str,
        due_at: str,
        model_version: str,
        policy_version: str,
    ) -> CaptureResult:
        """Append evidenced READY/WATCH rows; identical reruns are no-ops."""
        generated_at = _datetime(as_of)
        observable_at = _datetime(due_at)
        if observable_at <= generated_at or observable_at.date() <= generated_at.date():
            raise ValueError("due_at must be an explicit later trading-day timestamp")
        result = CaptureResult()
        for row in observations if observations is not None else []:
            if not isinstance(row, Mapping):
                result.skipped += 1
                result.degradation_reasons.append("invalid_observation_shape")
                continue
            symbol_value = row.get("symbol")
            symbol = symbol_value.strip() if isinstance(symbol_value, str) else ""
            status = str(row.get("status") or "").upper()
            validated = bool(row.get("research_validated"))
            final_score = _finite_float(row.get("final_score"))
            if final_score is None:
                result.skipped += 1
                result.degradation_reasons.append(
                    f"invalid_final_score:{symbol or 'missing_symbol'}"
                )
                continue
            mapping_confidence = _finite_float(row.get("mapping_confidence"))
            if (
                mapping_confidence is None
                or not 0.0 <= mapping_confidence <= 1.0
            ):
                result.skipped += 1
                result.degradation_reasons.append(
                    f"invalid_mapping_confidence:{symbol or 'missing_symbol'}"
                )
                continue
            explicit_refs = _string_list(row.get("evidence_refs"))
            claim_ids = _string_list(row.get("claim_ids"))
            degradation_reasons = _string_list(row.get("degradation_reasons"))
            assumption_refs = _string_list(row.get("assumption_refs"))
            if any(
                item is None
                for item in (
                    explicit_refs,
                    claim_ids,
                    degradation_reasons,
                    assumption_refs,
                )
            ):
                result.skipped += 1
                result.degradation_reasons.append(
                    f"invalid_observation_shape:{symbol or 'missing_symbol'}"
                )
                continue
            evidence_refs = sorted(
                {
                    str(ref)
                    for ref in (
                        explicit_refs
                        + claim_ids
                        + [row.get("source_snapshot_id")]
                    )
                    if isinstance(ref, str) and ref
                }
            )
            if (
                not symbol
                or status not in {"READY", "WATCH"}
                or not validated
                or not evidence_refs
            ):
                result.skipped += 1
                result.degradation_reasons.append(
                    f"skipped_unqualified:{symbol or 'missing_symbol'}"
                )
                continue
            identity = {
                "symbol": symbol,
                "due_date": observable_at.date().isoformat(),
                "question": self.QUESTION,
                "model_version": model_version,
                "policy_version": policy_version,
            }
            prediction_id = f"daily_prediction_{_digest(identity)[:24]}"
            if self.loop.get_prediction(prediction_id) is not None:
                result.duplicates += 1
                result.prediction_ids.append(prediction_id)
                continue
            context = {
                "status": status,
                "mapping_confidence": mapping_confidence,
                "degradation_reasons": degradation_reasons,
                "assumption_refs": assumption_refs,
                "timing_risk": bool(row.get("timing_risk")),
                "methodology": "positive next-session close-to-close excess return",
            }
            membership_confidence = _finite_float(
                row.get("membership_confidence")
            )
            if (
                membership_confidence is None
                or not 0.0 <= membership_confidence <= 1.0
            ):
                membership_confidence = mapping_confidence
            confidence = (
                final_score
                if final_score > 0.0
                else (mapping_confidence + membership_confidence) / 2.0
            )
            record = PredictionRecord(
                prediction_id=prediction_id,
                entity_id=symbol,
                research_question=self.QUESTION,
                predicted_value=True,
                confidence=max(0.0, min(1.0, confidence)),
                as_of=as_of,
                due_at=due_at,
                model_version=model_version,
                policy_version=policy_version,
                evidence_refs=evidence_refs,
                feature_snapshot_hash=f"sha256:{_digest(row)}",
                context=context,
            )
            try:
                self.loop.append_prediction(record)
                result.appended += 1
            except AppendOnlyViolation:
                result.duplicates += 1
            result.prediction_ids.append(prediction_id)
        result.degradation_reasons = sorted(set(result.degradation_reasons))
        return result

    def settle_after_close(
        self, *, as_of: str, bar_reader: StrictAsOfBarReader
    ) -> SettlementResult:
        """Settle due predictions from bars visible by ``as_of``.

        Missing or PIT-invalid bars remain pending.  A successful batch creates
        one shadow calibration proposal and never calls ``activate``.
        """
        cutoff = _datetime(as_of)
        result = SettlementResult()
        for prediction in self.loop.list_due_predictions(as_of):
            if not isinstance(prediction.context, Mapping):
                result.pending += 1
                result.degradation_reasons.append(
                    f"invalid_prediction_shape:{prediction.entity_id}"
                )
                continue
            mapping_confidence = _finite_float(
                prediction.context.get("mapping_confidence")
            )
            if (
                mapping_confidence is None
                or not 0.0 <= mapping_confidence <= 1.0
            ):
                result.pending += 1
                result.degradation_reasons.append(
                    f"invalid_mapping_confidence:{prediction.entity_id}"
                )
                continue
            predicted_value = prediction.predicted_value
            predicted_score = (
                None
                if isinstance(predicted_value, bool)
                else _finite_float(predicted_value)
            )
            if not isinstance(predicted_value, bool) and predicted_score is None:
                result.pending += 1
                result.degradation_reasons.append(
                    f"invalid_final_score:{prediction.entity_id}"
                )
                continue
            predicted_positive = (
                predicted_value
                if isinstance(predicted_value, bool)
                else bool(predicted_score and predicted_score > 0.0)
            )
            try:
                due_date = _datetime(prediction.due_at).date()
                bar = bar_reader.first_bar_after(
                    prediction.entity_id,
                    after_date=(due_date - timedelta(days=1)).isoformat(),
                    as_of=as_of,
                )
            except Exception as exc:
                result.pending += 1
                result.degradation_reasons.append(
                    f"bar_reader_error:{prediction.entity_id}:{type(exc).__name__}"
                )
                continue
            invalid_reason = self._bar_invalid_reason(prediction, bar, cutoff)
            if invalid_reason:
                result.pending += 1
                result.degradation_reasons.append(invalid_reason)
                continue
            assert bar is not None
            close = _finite_float(bar.close)
            previous_close = _finite_float(bar.previous_close)
            benchmark_return = _finite_float(bar.benchmark_return)
            assert close is not None and previous_close is not None
            assert benchmark_return is not None
            stock_return = round(close / previous_close - 1.0, 8)
            excess_return = round(stock_return - benchmark_return, 8)
            observation_date = date.fromisoformat(bar.trade_date)
            observation_delay_days = (observation_date - due_date).days
            if observation_delay_days > 0:
                category = AttributionCategory.TIMING
                reason = "scheduled_observation_day_missed"
            else:
                category, reason = self._attribute(
                    prediction, bar, excess_return
                )
            observation_id = (
                f"daily_observation_{_digest([prediction.prediction_id, bar.trade_date, bar.source_ref])[:24]}"
            )
            observation = OutcomeObservation(
                observation_id=observation_id,
                prediction_id=prediction.prediction_id,
                observed_at=max(
                    _datetime(prediction.due_at),
                    _datetime(bar.available_at),
                    _datetime(bar.benchmark_available_at),
                ).isoformat(),
                actual_value=excess_return,
                was_correct=predicted_positive == (excess_return > 0.0),
                attribution=category,
                attribution_reason=reason,
                evidence_refs=sorted(
                    {
                        item
                        for item in (
                            _string_list(prediction.evidence_refs) or []
                        )
                        + [
                            bar.source_ref,
                            bar.price_available_at_ref,
                            bar.benchmark_source_ref,
                            bar.benchmark_available_at_ref,
                        ]
                        + (_string_list(prediction.context.get("assumption_refs")) or [])
                        if isinstance(item, str) and item
                    }
                ),
                outcome_context={
                    "stock_return": stock_return,
                    "benchmark_return": benchmark_return,
                    "excess_return": excess_return,
                    "trade_date": bar.trade_date,
                    "scheduled_due_date": due_date.isoformat(),
                    "observation_delay_days": observation_delay_days,
                    "price_available_at": bar.available_at,
                    "price_available_at_ref": bar.price_available_at_ref,
                    "benchmark_available_at": bar.benchmark_available_at,
                    "benchmark_available_at_ref": bar.benchmark_available_at_ref,
                    "price_source_ref": bar.source_ref,
                    "benchmark_source_ref": bar.benchmark_source_ref,
                    "methodology": self.QUESTION,
                },
            )
            try:
                self.loop.append_observation(observation)
                result.settled += 1
                result.settled_prediction_ids.append(prediction.prediction_id)
                result.attribution_counts[category.value] = (
                    result.attribution_counts.get(category.value, 0) + 1
                )
            except AppendOnlyViolation:
                # Another worker settled the deterministic observation first.
                continue
        if result.settled:
            proposal = self.loop.generate_shadow_proposal(as_of=as_of)
            result.shadow_calibration_id = proposal.calibration_id
        result.degradation_reasons = sorted(set(result.degradation_reasons))
        return result

    def status(
        self,
        as_of: str,
        *,
        capture: CaptureResult | None = None,
        settlement: SettlementResult | None = None,
    ) -> dict[str, Any]:
        """Return the compact fields consumed by the existing daily status."""
        snapshot = self.loop.stats(as_of)
        return {
            "predictions_appended": capture.appended if capture else 0,
            "due_pending": snapshot["due_pending"],
            "outcomes_appended": settlement.settled if settlement else 0,
            "attribution_counts": (
                dict(settlement.attribution_counts)
                if settlement
                else dict(snapshot["attribution_counts"])
            ),
            "latest_shadow_run_id": snapshot["latest_shadow_run_id"],
            "latest_shadow_status": snapshot["latest_shadow_status"],
        }

    @staticmethod
    def _bar_invalid_reason(
        prediction: PredictionRecord,
        bar: AsOfDailyBar | None,
        cutoff: datetime,
    ) -> str:
        if bar is None:
            return f"missing_daily_bar:{prediction.entity_id}"
        if not isinstance(bar, AsOfDailyBar):
            return f"invalid_bar_shape:{prediction.entity_id}"
        if (
            not bar.source_ref
            or not bar.available_at
            or not bar.price_available_at_ref
        ):
            return f"missing_bar_lineage:{prediction.entity_id}"
        if (
            bar.benchmark_return is None
            or not bar.benchmark_source_ref
            or not bar.benchmark_available_at
            or not bar.benchmark_available_at_ref
        ):
            return f"missing_benchmark:{prediction.entity_id}"
        if _finite_float(bar.benchmark_return) is None:
            return f"invalid_benchmark:{prediction.entity_id}"
        if bar.symbol != prediction.entity_id:
            return f"bar_symbol_mismatch:{prediction.entity_id}"
        try:
            available_at = _datetime(bar.available_at)
            benchmark_available_at = _datetime(bar.benchmark_available_at)
            trade_date = date.fromisoformat(bar.trade_date)
        except (TypeError, ValueError):
            return f"invalid_bar_timestamp:{prediction.entity_id}"
        if available_at > cutoff or benchmark_available_at > cutoff:
            return f"future_available_at:{prediction.entity_id}"
        prediction_date = _datetime(prediction.as_of).date()
        due_date = _datetime(prediction.due_at).date()
        if (
            trade_date <= prediction_date
            or trade_date < due_date
            or trade_date > cutoff.date()
        ):
            return f"bar_outside_as_of_window:{prediction.entity_id}"
        close = _finite_float(bar.close)
        previous_close = _finite_float(bar.previous_close)
        if close is None or previous_close is None or close <= 0 or previous_close <= 0:
            return f"invalid_bar_price:{prediction.entity_id}"
        return ""

    @staticmethod
    def _attribute(
        prediction: PredictionRecord,
        bar: AsOfDailyBar,
        actual_return: float,
    ) -> tuple[AttributionCategory, str]:
        context = prediction.context
        if bar.degradation_reasons or context.get("degradation_reasons"):
            return AttributionCategory.DATA, "explicit_data_degradation"
        mapping_confidence = _finite_float(context.get("mapping_confidence"))
        if mapping_confidence is None or mapping_confidence < 0.5:
            return AttributionCategory.MAPPING, "mapping_confidence_below_0.5"
        if context.get("timing_risk"):
            return AttributionCategory.TIMING, "explicit_timing_risk"
        if context.get("assumption_refs"):
            return AttributionCategory.ASSUMPTION, "explicit_assumption_requires_review"
        if (
            actual_return <= 0.0
            and bar.benchmark_return is not None
            and bar.benchmark_return <= -0.005
        ):
            return AttributionCategory.MARKET_REGIME, "broad_market_decline_rule"
        return AttributionCategory.MODEL, "residual_model_evaluation_rule"
