"""Append-only production learning ledger backed by the research control plane.

This module records what the system knew, what later happened, and why a
prediction missed.  It only creates shadow calibration proposals; activation
and rollback remain owned by :class:`CalibrationController`.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from .calibration import CalibrationController
from .contracts import CalibrationRun, _as_mapping, _safe_float, _safe_text, _text_list
from .repository import ResearchRepository


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _instant(value: str | datetime) -> datetime:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp must be a timezone-aware ISO datetime") from exc


def _timestamp_text(value: str | datetime) -> str:
    """Validate a timestamp while retaining its supplied timezone for output."""
    if isinstance(value, str):
        text = value.strip()
        _instant(text)
        return text.replace("Z", "+00:00")
    return _instant(value).isoformat()


def _scalar_value(value: Any) -> float | str | bool | None:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        parsed = _safe_float(value, float("nan"))
        if parsed == parsed:
            return parsed
    raise ValueError("value must be serializable")


class AppendOnlyViolation(ValueError):
    """Raised when a caller tries to overwrite immutable learning evidence."""


class AttributionCategory(str, Enum):
    MODEL = "MODEL"
    DATA = "DATA"
    MAPPING = "MAPPING"
    TIMING = "TIMING"
    ASSUMPTION = "ASSUMPTION"
    MARKET_REGIME = "MARKET_REGIME"


@dataclass(frozen=True)
class PredictionRecord:
    prediction_id: str = field(default_factory=lambda: _id("prediction"))
    entity_id: str = ""
    research_question: str = ""
    predicted_value: float | str | bool | None = None
    confidence: float = 0.0
    as_of: str = ""
    due_at: str = ""
    model_version: str = ""
    policy_version: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    feature_snapshot_hash: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    recorded_at: str = ""


@dataclass(frozen=True)
class OutcomeObservation:
    observation_id: str = field(default_factory=lambda: _id("observation"))
    prediction_id: str = ""
    observed_at: str = ""
    actual_value: float | str | bool | None = None
    was_correct: bool = False
    attribution: AttributionCategory = AttributionCategory.ASSUMPTION
    attribution_reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    outcome_context: dict[str, Any] = field(default_factory=dict)
    supersedes_observation_id: str = ""
    created_at: str = field(default_factory=_utc_now)
    recorded_at: str = ""


@dataclass(frozen=True)
class ReplayRecord:
    prediction: PredictionRecord
    latest_observation: OutcomeObservation | None = None


class ProductionLearningLoop:
    """Immutable prediction/outcome ledger feeding the existing policy gate."""

    def __init__(
        self,
        repository: ResearchRepository,
        *,
        clock: Callable[[], str | datetime] | None = None,
    ) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._migrate()

    def _migrate(self) -> None:
        migration_at = _instant(self._clock()).isoformat()
        with self.repository.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS learning_predictions ("
                "prediction_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, "
                "entity_id TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL)"
            )
            prediction_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(learning_predictions)"
                ).fetchall()
            }
            if "recorded_at" not in prediction_columns:
                conn.execute(
                    "ALTER TABLE learning_predictions "
                    "ADD COLUMN recorded_at TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "UPDATE learning_predictions SET recorded_at=? "
                "WHERE recorded_at=''",
                (migration_at,),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_predictions_as_of "
                "ON learning_predictions(as_of, recorded_at)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS learning_observations ("
                "observation_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, "
                "observed_at TEXT NOT NULL, supersedes_observation_id TEXT NOT NULL DEFAULT '', "
                "recorded_at TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL, "
                "FOREIGN KEY(prediction_id) REFERENCES learning_predictions(prediction_id))"
            )
            observation_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(learning_observations)"
                ).fetchall()
            }
            if "recorded_at" not in observation_columns:
                conn.execute(
                    "ALTER TABLE learning_observations "
                    "ADD COLUMN recorded_at TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "UPDATE learning_observations SET recorded_at=? "
                "WHERE recorded_at=''",
                (migration_at,),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_observations_prediction "
                "ON learning_observations(prediction_id, observed_at, recorded_at)"
            )

    @staticmethod
    def _prediction_payload(item: PredictionRecord) -> str:
        return json.dumps(
            asdict(item), ensure_ascii=False, sort_keys=True, allow_nan=False
        )

    @staticmethod
    def _observation_payload(item: OutcomeObservation) -> str:
        payload = asdict(item)
        payload["attribution"] = item.attribution.value
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )

    @staticmethod
    def _decode_prediction(
        payload: str, recorded_at: str = ""
    ) -> PredictionRecord | None:
        if not isinstance(payload, str):
            return None
        try:
            data = json.loads(payload)
            if not isinstance(data, Mapping):
                return None
            data = dict(data)
            item = PredictionRecord(**data)
            prediction_id = _safe_text(item.prediction_id)
            entity_id = _safe_text(item.entity_id)
            research_question = _safe_text(item.research_question)
            model_version = _safe_text(item.model_version)
            policy_version = _safe_text(item.policy_version)
            feature_snapshot_hash = _safe_text(item.feature_snapshot_hash)
            evidence_refs = _text_list(item.evidence_refs)
            confidence = _safe_float(item.confidence, float("nan"))
            as_of = _timestamp_text(item.as_of)
            due_at = _timestamp_text(item.due_at)
            if (
                not prediction_id
                or not entity_id
                or not research_question
                or not model_version
                or not policy_version
                or not feature_snapshot_hash
                or not evidence_refs
                or confidence != confidence
                or not 0.0 <= confidence <= 1.0
                or _instant(due_at) <= _instant(as_of)
            ):
                return None
            item = replace(
                item,
                prediction_id=prediction_id,
                entity_id=entity_id,
                research_question=research_question,
                model_version=model_version,
                policy_version=policy_version,
                evidence_refs=evidence_refs,
                feature_snapshot_hash=feature_snapshot_hash,
                context=_as_mapping(item.context),
                confidence=confidence,
                as_of=as_of,
                due_at=due_at,
                predicted_value=_scalar_value(item.predicted_value),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return replace(item, recorded_at=recorded_at or item.recorded_at)

    @staticmethod
    def _decode_observation(
        payload: str, recorded_at: str = ""
    ) -> OutcomeObservation | None:
        if not isinstance(payload, str):
            return None
        try:
            data = json.loads(payload)
            if not isinstance(data, Mapping):
                return None
            data = dict(data)
            data["attribution"] = AttributionCategory(data["attribution"])
            item = OutcomeObservation(**data)
            prediction_id = _safe_text(item.prediction_id)
            observation_id = _safe_text(item.observation_id)
            attribution_reason = _safe_text(item.attribution_reason)
            evidence_refs = _text_list(item.evidence_refs)
            observed_at = _timestamp_text(item.observed_at)
            if (
                not prediction_id
                or not observation_id
                or not attribution_reason
                or not evidence_refs
                or not isinstance(item.was_correct, bool)
            ):
                return None
            item = replace(
                item,
                prediction_id=prediction_id,
                observation_id=observation_id,
                observed_at=observed_at,
                actual_value=_scalar_value(item.actual_value),
                attribution_reason=attribution_reason,
                evidence_refs=evidence_refs,
                supersedes_observation_id=_safe_text(
                    item.supersedes_observation_id
                ),
                outcome_context=_as_mapping(item.outcome_context),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return replace(item, recorded_at=recorded_at or item.recorded_at)

    def append_prediction(self, item: PredictionRecord) -> PredictionRecord:
        if not isinstance(item, PredictionRecord):
            raise ValueError("prediction type is invalid")
        prediction_id = _safe_text(item.prediction_id)
        entity_id = _safe_text(item.entity_id)
        research_question = _safe_text(item.research_question)
        model_version = _safe_text(item.model_version)
        policy_version = _safe_text(item.policy_version)
        feature_snapshot_hash = _safe_text(item.feature_snapshot_hash)
        if not all((prediction_id, entity_id, research_question)):
            raise ValueError("prediction identity fields are required")
        confidence = _safe_float(item.confidence, float("nan"))
        if confidence != confidence or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not model_version or not policy_version:
            raise ValueError("model_version and policy_version are required")
        evidence_refs = _text_list(item.evidence_refs)
        if not evidence_refs or not feature_snapshot_hash:
            raise ValueError("evidence_refs and feature_snapshot_hash are required")
        as_of, due_at = _instant(item.as_of), _instant(item.due_at)
        as_of_text = _timestamp_text(item.as_of)
        due_at_text = _timestamp_text(item.due_at)
        if due_at <= as_of:
            raise ValueError("due_at must be later than as_of")
        normalized = replace(
            item,
            prediction_id=prediction_id,
            entity_id=entity_id,
            research_question=research_question,
            predicted_value=_scalar_value(item.predicted_value),
            confidence=confidence,
            as_of=as_of_text,
            due_at=due_at_text,
            model_version=model_version,
            policy_version=policy_version,
            evidence_refs=evidence_refs,
            feature_snapshot_hash=feature_snapshot_hash,
            context=_as_mapping(item.context),
        )
        recorded_at = _instant(self._clock()).isoformat()
        stored = replace(
            normalized,
            created_at=recorded_at,
            recorded_at=recorded_at,
        )
        try:
            payload = self._prediction_payload(stored)
            with self.repository.transaction() as conn:
                conn.execute(
                    "INSERT INTO learning_predictions"
                    "(prediction_id,as_of,entity_id,recorded_at,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        stored.prediction_id,
                        as_of.isoformat(),
                        stored.entity_id,
                        recorded_at,
                        payload,
                    ),
                )
        except (TypeError, ValueError) as exc:
            raise ValueError("prediction payload is not serializable") from exc
        except sqlite3.IntegrityError as exc:
            raise AppendOnlyViolation(
                f"prediction {stored.prediction_id!r} already exists"
            ) from exc
        return stored

    def get_prediction(self, prediction_id: str) -> PredictionRecord | None:
        if not isinstance(prediction_id, str) or not prediction_id:
            return None
        with self.repository.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json,recorded_at FROM learning_predictions "
                "WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
        return self._decode_prediction(row[0], row[1]) if row else None

    def append_observation(self, item: OutcomeObservation) -> OutcomeObservation:
        if not isinstance(item, OutcomeObservation):
            raise ValueError("observation type is invalid")
        prediction_id = _safe_text(item.prediction_id)
        observation_id = _safe_text(item.observation_id)
        if not observation_id:
            raise ValueError("observation identity is required")
        prediction = self.get_prediction(prediction_id)
        if prediction is None:
            raise KeyError(prediction_id)
        if not isinstance(item.attribution, AttributionCategory):
            raise ValueError("unsupported attribution category")
        if not isinstance(item.was_correct, bool):
            raise ValueError("was_correct must be a boolean")
        observed_at = _instant(item.observed_at)
        observed_at_text = _timestamp_text(item.observed_at)
        as_of, due_at = _instant(prediction.as_of), _instant(prediction.due_at)
        if observed_at.date() <= as_of.date():
            raise ValueError("outcome observation must be cross-day")
        if observed_at < due_at:
            raise ValueError("outcome observation cannot precede due_at")
        attribution_reason = _safe_text(item.attribution_reason)
        evidence_refs = _text_list(item.evidence_refs)
        if not attribution_reason or not evidence_refs:
            raise ValueError("attribution_reason and evidence_refs are required")
        actual_value = _scalar_value(item.actual_value)
        supersedes_observation_id = _safe_text(item.supersedes_observation_id)
        recorded_at = _instant(self._clock()).isoformat()
        stored = replace(
            item,
            observation_id=observation_id,
            prediction_id=prediction_id,
            observed_at=observed_at_text,
            actual_value=actual_value,
            attribution_reason=attribution_reason,
            evidence_refs=evidence_refs,
            supersedes_observation_id=supersedes_observation_id,
            created_at=recorded_at,
            recorded_at=recorded_at,
        )
        with self.repository.transaction() as conn:
            existing = conn.execute(
                "SELECT observation_id, observed_at FROM learning_observations "
                "WHERE prediction_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                (prediction_id,),
            ).fetchone()
            if existing and supersedes_observation_id != existing[0]:
                raise AppendOnlyViolation(
                    "a correction must supersede the latest observation"
                )
            if existing and observed_at <= _instant(existing[1]):
                raise ValueError(
                    "correction observed_at must be later than the superseded observation"
                )
            if not existing and supersedes_observation_id:
                raise ValueError("supersedes_observation_id does not exist")
            try:
                payload = self._observation_payload(stored)
                conn.execute(
                    "INSERT INTO learning_observations"
                    "(observation_id,prediction_id,observed_at,"
                    "supersedes_observation_id,recorded_at,payload_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        stored.observation_id,
                        stored.prediction_id,
                        observed_at.isoformat(),
                        stored.supersedes_observation_id,
                        recorded_at,
                        payload,
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("observation payload is not serializable") from exc
            except sqlite3.IntegrityError as exc:
                raise AppendOnlyViolation(
                    f"observation {stored.observation_id!r} already exists"
                ) from exc
        return stored

    def list_observations(
        self, *, prediction_id: str, cutoff: str | datetime | None = None
    ) -> list[OutcomeObservation]:
        prediction_id = _safe_text(prediction_id)
        if not prediction_id:
            return []
        return self._list_observations_for_predictions(
            [prediction_id], cutoff
        ).get(prediction_id, [])

    def _list_observations_for_predictions(
        self,
        prediction_ids: list[str],
        cutoff: str | datetime | None = None,
    ) -> dict[str, list[OutcomeObservation]]:
        prediction_ids = [
            normalized
            for value in prediction_ids
            if (normalized := _safe_text(value))
        ]
        if not prediction_ids:
            return {}
        placeholders = ",".join("?" for _ in prediction_ids)
        query = (
            "SELECT prediction_id,payload_json,recorded_at "
            "FROM learning_observations "
            f"WHERE prediction_id IN ({placeholders})"
        )
        params: list[Any] = list(prediction_ids)
        if cutoff is not None:
            cutoff_iso = _instant(cutoff).isoformat()
            query += " AND observed_at<=? AND recorded_at<=?"
            params.extend((cutoff_iso, cutoff_iso))
        query += " ORDER BY prediction_id ASC, observed_at ASC, rowid ASC"
        with self.repository.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        observations: dict[str, list[OutcomeObservation]] = {
            prediction_id: [] for prediction_id in prediction_ids
        }
        for row in rows:
            item = self._decode_observation(row[1], row[2])
            if item is not None:
                observations.setdefault(row[0], []).append(item)
        return observations

    def replay(self, cutoff: str | datetime) -> list[ReplayRecord]:
        cutoff_iso = _instant(cutoff).isoformat()
        with self.repository.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json,recorded_at FROM learning_predictions "
                "WHERE as_of<=? AND recorded_at<=? "
                "ORDER BY as_of ASC, prediction_id ASC",
                (cutoff_iso, cutoff_iso),
            ).fetchall()
        predictions: list[PredictionRecord] = []
        for row in rows:
            prediction = self._decode_prediction(row[0], row[1])
            if prediction is not None:
                predictions.append(prediction)
        observations = self._list_observations_for_predictions(
            [prediction.prediction_id for prediction in predictions], cutoff_iso
        )
        result: list[ReplayRecord] = []
        for prediction in predictions:
            prediction_observations = observations.get(prediction.prediction_id, [])
            result.append(
                ReplayRecord(
                    prediction=prediction,
                    latest_observation=(
                        prediction_observations[-1]
                        if prediction_observations
                        else None
                    ),
                )
            )
        return result

    def list_due_predictions(
        self, as_of: str | datetime
    ) -> list[PredictionRecord]:
        """Return matured predictions that have no outcome as of the cutoff."""
        cutoff = _instant(as_of)
        return [
            item.prediction
            for item in self.replay(cutoff)
            if _instant(item.prediction.due_at) <= cutoff
            and item.latest_observation is None
        ]

    def generate_shadow_proposal(
        self, *, as_of: str | datetime
    ) -> CalibrationRun:
        settled_rows = [
            item for item in self.replay(as_of) if item.latest_observation is not None
        ]
        settled_by_cohort: dict[tuple[str, ...], ReplayRecord] = {}
        duplicate_prediction_ids: list[str] = []
        for item in settled_rows:
            prediction = item.prediction
            cohort_key = (
                prediction.entity_id,
                prediction.research_question,
                _instant(prediction.due_at).date().isoformat(),
                prediction.model_version,
                prediction.policy_version,
            )
            if cohort_key in settled_by_cohort:
                duplicate_prediction_ids.append(prediction.prediction_id)
                continue
            settled_by_cohort[cohort_key] = item
        settled = list(settled_by_cohort.values())
        attribution_counts = {category.value: 0 for category in AttributionCategory}
        for item in settled:
            attribution_counts[item.latest_observation.attribution.value] += 1
        model_settled = [
            item for item in settled
            if item.latest_observation.attribution is AttributionCategory.MODEL
        ]
        sample_size = len(model_settled)
        correct = sum(
            bool(item.latest_observation.was_correct) for item in model_settled
        )
        accuracy = correct / sample_size if sample_size else 0.0
        high_confidence = [
            item for item in model_settled if item.prediction.confidence >= 0.5
        ]
        high_confidence_misses = sum(
            not item.latest_observation.was_correct for item in high_confidence
        )
        false_promotion_rate = (
            high_confidence_misses / len(high_confidence)
            if high_confidence
            else 0.0
        )
        multiplier = round(max(0.8, min(1.2, 1.0 + (accuracy - 0.5) * 0.2)), 4)
        proposal = CalibrationRun(
            domain="alpha",
            status="shadow",
            sample_size=sample_size,
            metrics={
                "walk_forward_score": round(accuracy, 4),
                "false_promotion_rate": round(false_promotion_rate, 4),
                "attribution_counts": attribution_counts,
                "total_outcomes": len(settled),
                "excluded_non_model_outcomes": len(settled) - sample_size,
                "observation_ids": [
                    item.latest_observation.observation_id for item in model_settled
                ],
                "duplicate_cohorts_excluded": len(
                    duplicate_prediction_ids
                ),
                "duplicate_prediction_ids": duplicate_prediction_ids,
                "as_of": _instant(as_of).isoformat(),
            },
            parameters={"alpha_weight_multiplier": multiplier},
            bounds={"alpha_weight_multiplier": {"min": 0.8, "max": 1.2}},
        )
        return CalibrationController(self.repository).create_shadow(proposal)

    def stats(self, as_of: str | datetime) -> dict[str, Any]:
        """Return an as-of operational snapshot for existing status reports."""
        replayed = self.replay(as_of)
        latest = [
            item.latest_observation
            for item in replayed
            if item.latest_observation is not None
        ]
        attribution_counts: dict[str, int] = {}
        for observation in latest:
            key = observation.attribution.value
            attribution_counts[key] = attribution_counts.get(key, 0) + 1
        shadow = next(
            (
                run
                for run in self.repository.list_calibrations(limit=1000)
                if run.domain == "alpha" and run.status == "shadow"
            ),
            None,
        )
        return {
            "prediction_count": len(replayed),
            "due_pending": len(self.list_due_predictions(as_of)),
            "outcome_count": len(latest),
            "attribution_counts": attribution_counts,
            "latest_shadow_run_id": shadow.calibration_id if shadow else "",
            "latest_shadow_status": shadow.status if shadow else "",
        }
