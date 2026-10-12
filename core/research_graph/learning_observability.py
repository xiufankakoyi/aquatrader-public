from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.research_graph.models import utc_now
from core.research_graph.schema import EdgeStatus


def _round4(value: float) -> float:
    return round(float(value), 4)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return _clamp(numerator / denominator)


def _report_to_dict(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, dict):
        return report
    if hasattr(report, "to_dict"):
        try:
            return report.to_dict()
        except Exception:
            return {}
    return {}


def _delta_items(delta_report: Any) -> list[dict[str, Any]]:
    payload = _report_to_dict(delta_report)
    items = payload.get("significant_deltas", [])
    return items if isinstance(items, list) else []


def _gap_items(gap_report: Any) -> list[dict[str, Any]]:
    payload = _report_to_dict(gap_report)
    gaps = payload.get("top_gaps") or payload.get("gaps") or []
    return gaps if isinstance(gaps, list) else []


@dataclass
class LearningObservabilityReporter:
    """Build learning progress artifacts from existing ResearchGraph state."""

    graph_store: Any | None = None
    neurons: list[Any] | None = None
    delta_report: Any | None = None
    gap_report: Any | None = None
    previous_snapshot: dict[str, Any] | None = None
    duplicate_count: int = 0
    llm_rejected_count: int = 0
    tick_count: int = 0

    def build_snapshot(
        self,
        *,
        graph_store: Any | None = None,
        neurons: list[Any] | None = None,
        delta_report: Any | None = None,
        gap_report: Any | None = None,
        previous_snapshot: dict[str, Any] | None = None,
        duplicate_count: int | None = None,
        llm_rejected_count: int | None = None,
        tick_count: int | None = None,
    ) -> dict[str, Any]:
        """Return all learning observability artifacts as a single payload."""
        graph_store = graph_store if graph_store is not None else self.graph_store
        neurons = neurons if neurons is not None else (self.neurons or [])
        delta_report = delta_report if delta_report is not None else self.delta_report
        gap_report = gap_report if gap_report is not None else self.gap_report
        previous_snapshot = (
            previous_snapshot if previous_snapshot is not None else self.previous_snapshot
        ) or {}
        duplicate_count = self.duplicate_count if duplicate_count is None else duplicate_count
        llm_rejected_count = (
            self.llm_rejected_count if llm_rejected_count is None else llm_rejected_count
        )
        tick_count = self.tick_count if tick_count is None else tick_count

        generated_at = utc_now()
        metrics = self._build_metrics(
            graph_store=graph_store,
            neurons=neurons,
            delta_report=delta_report,
            previous_snapshot=previous_snapshot,
            duplicate_count=duplicate_count,
            llm_rejected_count=llm_rejected_count,
        )
        curve = self._build_learning_curve(generated_at, metrics, delta_report)
        mastery = self._build_topic_mastery(neurons, gap_report)
        breakthrough = self._build_breakthrough_report(metrics, delta_report)
        return {
            "generated_at": generated_at,
            "tick_count": int(tick_count or 0),
            "learning_metrics": metrics,
            "learning_curve_latest": curve,
            "topic_mastery_profile": mastery,
            "breakthrough_report": breakthrough,
        }

    def write_artifacts(self, output_dir: str | Path) -> dict[str, Path]:
        """Write the four learning artifacts and return their paths."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        snapshot = self.build_snapshot()
        artifacts = {
            "learning_metrics": output_path / "learning_metrics.json",
            "learning_curve_latest": output_path / "learning_curve_latest.json",
            "topic_mastery_profile": output_path / "topic_mastery_profile.json",
            "breakthrough_report": output_path / "breakthrough_report.json",
        }
        for key, path in artifacts.items():
            path.write_text(
                json.dumps(snapshot[key], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self.append_history(output_path, snapshot=snapshot)
        return artifacts

    def append_history(
        self,
        output_dir: str | Path,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> Path:
        """Append one learning curve point to learning_curve_history.jsonl."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        snapshot = snapshot or self.build_snapshot()
        metrics = snapshot["learning_metrics"]
        row = {
            "generated_at": snapshot.get("generated_at", ""),
            "tick_count": int(snapshot.get("tick_count", 0) or 0),
            "canonical_evidence_count": metrics["canonical_evidence_count"],
            "active_edge_count": metrics["active_edge_count"],
            "avg_confidence": metrics["avg_confidence"],
            "avg_uncertainty": metrics["avg_uncertainty"],
            "breakthrough_score": metrics["breakthrough_score"],
            "duplicate_ratio": metrics["duplicate_ratio"],
            "llm_rejected_ratio": metrics["llm_rejected_ratio"],
        }
        history_path = output_path / "learning_curve_history.jsonl"
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return history_path

    def _build_metrics(
        self,
        *,
        graph_store: Any | None,
        neurons: list[Any],
        delta_report: Any,
        previous_snapshot: dict[str, Any],
        duplicate_count: int,
        llm_rejected_count: int,
    ) -> dict[str, Any]:
        evidence = getattr(graph_store, "evidence", {}) if graph_store is not None else {}
        edges = getattr(graph_store, "edges", {}) if graph_store is not None else {}
        nodes = getattr(graph_store, "nodes", {}) if graph_store is not None else {}

        canonical_evidence_count = len(evidence)
        active_edge_count = sum(
            1 for edge in edges.values() if getattr(edge, "status", "") == EdgeStatus.ACTIVE.value
        )
        conflicted_edge_count = sum(
            1
            for edge in edges.values()
            if getattr(edge, "status", "") == EdgeStatus.CONFLICTED.value
        )
        avg_confidence = _safe_avg(getattr(n, "confidence", 0.0) for n in neurons)
        avg_uncertainty = _safe_avg(getattr(n, "uncertainty", 0.0) for n in neurons)
        evidence_saturation = _safe_ratio(canonical_evidence_count, max(1, len(nodes) * 3))

        deltas = _delta_items(delta_report)
        new_evidence_delta = sum(len(item.get("new_evidence_ids") or []) for item in deltas)
        confidence_delta = sum(abs(float(item.get("confidence_delta") or 0.0)) for item in deltas)

        previous_metrics = previous_snapshot.get("learning_metrics", previous_snapshot)
        previous_evidence_count = int(previous_metrics.get("canonical_evidence_count", 0) or 0)
        if not new_evidence_delta and previous_evidence_count:
            new_evidence_delta = max(0, canonical_evidence_count - previous_evidence_count)
        previous_avg_confidence = previous_metrics.get("avg_confidence")
        if not confidence_delta and previous_avg_confidence is not None:
            confidence_delta = abs(avg_confidence - float(previous_avg_confidence or 0.0))

        duplicate_ratio = _safe_ratio(duplicate_count, duplicate_count + canonical_evidence_count)
        llm_evidence_count = sum(
            1
            for item in evidence.values()
            if "llm" in str(getattr(item, "extracted_by", "")).lower()
            or bool((getattr(item, "metadata", {}) or {}).get("llm_extracted"))
        )
        llm_rejected_ratio = _safe_ratio(
            llm_rejected_count,
            llm_rejected_count + llm_evidence_count,
        )

        active_edge_delta = max(
            0,
            active_edge_count - int(previous_metrics.get("active_edge_count", 0) or 0),
        )
        breakthrough_score = _breakthrough_score(
            new_evidence_delta=new_evidence_delta,
            confidence_delta=confidence_delta,
            active_edge_delta=active_edge_delta,
            conflicted_edge_count=conflicted_edge_count,
            duplicate_ratio=duplicate_ratio,
            llm_rejected_ratio=llm_rejected_ratio,
        )

        return {
            "canonical_evidence_count": canonical_evidence_count,
            "active_edge_count": active_edge_count,
            "conflicted_edge_count": conflicted_edge_count,
            "avg_confidence": _round4(avg_confidence),
            "avg_uncertainty": _round4(avg_uncertainty),
            "evidence_saturation": _round4(evidence_saturation),
            "new_evidence_delta": int(new_evidence_delta),
            "confidence_delta": _round4(confidence_delta),
            "duplicate_ratio": _round4(duplicate_ratio),
            "llm_rejected_ratio": _round4(llm_rejected_ratio),
            "breakthrough_score": _round4(breakthrough_score),
        }

    def _build_learning_curve(
        self,
        generated_at: str,
        metrics: dict[str, Any],
        delta_report: Any,
    ) -> dict[str, Any]:
        return {
            "generated_at": generated_at,
            "point": dict(metrics),
            "significant_deltas": _delta_items(delta_report),
        }

    def _build_topic_mastery(self, neurons: list[Any], gap_report: Any) -> dict[str, Any]:
        gaps = _gap_items(gap_report)
        gap_count_by_target: dict[str, int] = {}
        for gap in gaps:
            target_id = str(gap.get("target_id", ""))
            if target_id:
                gap_count_by_target[target_id] = gap_count_by_target.get(target_id, 0) + 1

        topics = []
        for neuron in neurons:
            support = int(getattr(neuron, "support_count", 0) or 0)
            counter = int(getattr(neuron, "counter_count", 0) or 0)
            total = support + counter
            topics.append(
                {
                    "topic_id": getattr(neuron, "root_theme", "") or getattr(neuron, "neuron_id", ""),
                    "neuron_id": getattr(neuron, "neuron_id", ""),
                    "canonical_name": getattr(neuron, "canonical_name", ""),
                    "mastery_score": _round4(getattr(neuron, "confidence", 0.0)),
                    "evidence_saturation": _round4(_safe_ratio(total, 3)),
                    "source_diversity": _round4(getattr(neuron, "source_diversity", 0.0)),
                    "contradiction_ratio": _round4(_safe_ratio(counter, total)),
                    "stale_ratio": _round4(getattr(neuron, "staleness", 0.0)),
                    "repeated_material_ratio": _round4(1.0 - getattr(neuron, "novelty", 1.0)),
                    "unresolved_gap_count": gap_count_by_target.get(
                        getattr(neuron, "neuron_id", ""),
                        1 if getattr(neuron, "evidence_gap", 0.0) >= 0.5 else 0,
                    ),
                    "last_breakthrough_at": getattr(neuron, "last_evidence_at", ""),
                }
            )
        return {
            "topics": topics,
            "totals": {
                "topic_count": len(topics),
                "avg_mastery_score": _round4(
                    _safe_avg(item["mastery_score"] for item in topics)
                ),
                "unresolved_gap_count": sum(item["unresolved_gap_count"] for item in topics),
            },
        }

    def _build_breakthrough_report(
        self,
        metrics: dict[str, Any],
        delta_report: Any,
    ) -> dict[str, Any]:
        return {
            "breakthrough_score": metrics["breakthrough_score"],
            "new_canonical_evidence": metrics["new_evidence_delta"],
            "confidence_delta_sum": metrics["confidence_delta"],
            "duplicate_ratio": metrics["duplicate_ratio"],
            "llm_rejected_ratio": metrics["llm_rejected_ratio"],
            "significant_deltas": _delta_items(delta_report),
        }


def _safe_avg(values: Any) -> float:
    items = [float(v or 0.0) for v in values]
    if not items:
        return 0.0
    return sum(items) / len(items)


def _breakthrough_score(
    *,
    new_evidence_delta: int,
    confidence_delta: float,
    active_edge_delta: int,
    conflicted_edge_count: int,
    duplicate_ratio: float,
    llm_rejected_ratio: float,
) -> float:
    if (
        new_evidence_delta <= 0
        and confidence_delta <= 0
        and active_edge_delta <= 0
        and conflicted_edge_count <= 0
    ):
        return 0.0
    score = (
        0.35 * _clamp(new_evidence_delta / 5)
        + 0.25 * _clamp(abs(confidence_delta))
        + 0.20 * _clamp(active_edge_delta / 3)
        + 0.10 * _clamp(conflicted_edge_count / 3)
        + 0.10 * (1.0 - _clamp(duplicate_ratio))
        - 0.20 * _clamp(llm_rejected_ratio)
    )
    return _clamp(score)
