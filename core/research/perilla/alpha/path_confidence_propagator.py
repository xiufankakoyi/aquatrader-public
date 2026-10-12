"""Path confidence propagation for Graph-first Alpha Discovery Engine.

Provides incremental path confidence updates based on edge confidence changes.
Path confidence is kept separate from path alpha score as required.
"""
from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import harmonic_mean
from typing import Any

from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.alpha.frontier_scorer import FrontierScorer
from core.research.perilla.alpha.path_discovery_schema import (
    PathCandidate,
    PathEvidenceBundle,
    PathQualityReport,
    PathVerificationResult,
)
from core.research.perilla.alpha.path_quality_evaluator import PathQualityEvaluator
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(dict(item))
        except (TypeError, ValueError):
            continue
    return result


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_get(store: Any, key: Any) -> Any:
    if not isinstance(store, Mapping):
        return None
    try:
        return store.get(key)
    except (TypeError, ValueError):
        return None


def _edge_key(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return tuple(str(item) for item in value)


def _safe_hop_count(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(int(value), 1000))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class PathConfidenceTrace:
    """单步置信度传播记录。"""

    step: str
    value: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "step": str(self.step or ""),
            "value": _score(self.value),
            "detail": str(self.detail or ""),
        }


@dataclass
class PathRecord:
    """A record representing a single root-to-leaf path with confidence metadata."""

    path_id: str
    path: list[str]
    path_alpha_score: float = 0.0
    path_confidence: float = 0.0
    path_evidence_gap: list[dict[str, Any]] = field(default_factory=list)
    path_noise_score: float = 0.0
    edge_confidences: list[float] = field(default_factory=list)
    event_boost_score: float = 0.0
    event_risk_score: float = 0.0
    event_freshness_score: float = 0.0
    event_adjusted_confidence: float = 0.0
    stable_confidence: float = 0.0
    final_confidence: float = 0.0
    ontology_bonus: float = 0.0
    contradiction_penalty: float = 0.0
    length_penalty: float = 1.0
    propagation_trace: list[PathConfidenceTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation."""
        return {
            "path_id": str(self.path_id or ""),
            "path": [str(node) for node in self.path if node is not None] if isinstance(self.path, (list, tuple)) else [],
            "path_alpha_score": _score(self.path_alpha_score),
            "path_confidence": _score(self.path_confidence),
            "path_evidence_gap": _mappings(self.path_evidence_gap),
            "path_noise_score": _score(self.path_noise_score),
            "edge_confidences": [_score(value) for value in self.edge_confidences] if isinstance(self.edge_confidences, (list, tuple)) else [],
            "event_boost_score": _score(self.event_boost_score),
            "event_risk_score": _score(self.event_risk_score),
            "event_freshness_score": _score(self.event_freshness_score),
            "event_adjusted_confidence": _score(self.event_adjusted_confidence),
            "stable_confidence": _score(self.stable_confidence),
            "final_confidence": _score(self.final_confidence),
            "ontology_bonus": _score(self.ontology_bonus),
            "contradiction_penalty": _score(self.contradiction_penalty),
            "length_penalty": _score(self.length_penalty, 1.0),
            "propagation_trace": [t.to_dict() for t in self.propagation_trace if isinstance(t, PathConfidenceTrace)],
        }


class PathConfidencePropagator:
    """Propagates edge confidence changes to affected paths incrementally."""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        event_engine: Any | None = None,
    ) -> None:
        """Initialize the propagator.

        Args:
            kb: SAG knowledge base.
            entity_resolver: Optional entity resolver for quality evaluation.
            event_engine: Optional dynamic event propagation engine for event-aware confidence.
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.event_engine = event_engine
        self.path_quality_evaluator = PathQualityEvaluator(
            entity_resolver=self.entity_resolver,
        )
        self._path_index: dict[str, set[str]] = {}
        self._path_records: dict[str, PathRecord] = {}

    @staticmethod
    def _path_id(path: list[str]) -> str:
        """Generate a stable path id from a list of node names."""
        stable = "|".join(str(item) for item in path if item is not None) if isinstance(path, (list, tuple)) else ""
        return "path-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def compute_path_confidence(
        edge_confidences: list[float],
        has_conflict: bool = False,
        has_gap: bool = False,
        event_overlay: dict[str, Any] | None = None,
    ) -> float:
        """Compute path confidence from edge confidences.

        Uses harmonic mean of edge confidences with penalties for conflicts
        and unresolved evidence gaps. Optionally applies event overlay.

        Args:
            edge_confidences: List of edge confidence values.
            has_conflict: Whether the path contains a conflicted edge.
            has_gap: Whether the path contains an evidence gap.
            event_overlay: Optional event overlay dict for event-aware adjustment.

        Returns:
            Path confidence value in [0, 1].
        """
        confidences = [_score(value) for value in edge_confidences] if isinstance(edge_confidences, (list, tuple)) else []
        if not confidences:
            stable_confidence = 0.0
        else:
            hmean = harmonic_mean(confidences)
            min_edge_penalty = min(confidences)
            conflict_penalty = 0.7 if has_conflict is True else 1.0
            gap_penalty = 0.8 if has_gap is True else 1.0
            stable_confidence = hmean * min_edge_penalty * conflict_penalty * gap_penalty

        if isinstance(event_overlay, Mapping):
            event_boost = _score(event_overlay.get("event_boost_score", 0.0))
            event_risk = _score(event_overlay.get("event_risk_score", 0.0))
            freshness = _score(event_overlay.get("freshness_score", 0.0))
            contradiction_penalty = _score(event_overlay.get("contradiction_pressure", 0.0))
            event_adjusted_confidence = (
                stable_confidence * (1.0 + event_boost) * (1.0 + freshness * 0.05)
                - event_risk * 0.35
                - contradiction_penalty * 0.4
            )
            return max(0.0, min(1.0, event_adjusted_confidence))

        return max(0.0, min(1.0, stable_confidence))

    def propagate_path(
        self,
        candidate: PathCandidate,
        bundle: PathEvidenceBundle | None = None,
        verification: PathVerificationResult | None = None,
        quality_report: PathQualityReport | None = None,
        event_overlay: dict[str, Any] | None = None,
    ) -> PathRecord:
        """对单条 PathCandidate 执行路径感知置信度传播。

        保留 stable_confidence / event_adjusted_confidence / final_confidence 三层分数，
        并输出 propagation_trace 说明每一步变化。

        Args:
            candidate: 路径候选。
            bundle: 可选路径证据包。
            verification: 可选验证结果。
            quality_report: 可选路径质量报告。
            event_overlay: 可选事件覆盖信息。

        Returns:
            PathRecord，包含完整传播痕迹。
        """
        if not isinstance(candidate, PathCandidate):
            return PathRecord(path_id="", path=[])
        candidate_path_id = _field(candidate, "path_id", "")
        bundle = bundle if isinstance(bundle, PathEvidenceBundle) else PathEvidenceBundle(path_candidate=candidate)
        verification = verification if isinstance(verification, PathVerificationResult) else PathVerificationResult(path_id=candidate_path_id)
        quality_report = quality_report if isinstance(quality_report, PathQualityReport) else PathQualityReport(path_id=candidate_path_id)
        event_overlay = dict(event_overlay) if isinstance(event_overlay, Mapping) else {}

        trace: list[PathConfidenceTrace] = []
        path_id = _field(candidate, "path_id", "")
        raw_path = _field(candidate, "nodes", [])
        path = [str(item) for item in raw_path if item is not None] if isinstance(raw_path, (list, tuple)) else []

        # 1. Edge-level confidences
        edge_confidences: list[float] = []
        candidate_edges = _field(candidate, "edges", [])
        candidate_edges = candidate_edges if isinstance(candidate_edges, (list, tuple)) else []
        graph_edges = getattr(self.kb, "edges", {})
        for edge_key in candidate_edges:
            edge = _mapping_get(graph_edges, edge_key)
            if edge is None:
                edge_confidences.append(0.0)
            else:
                try:
                    self.kb.update_edge_confidence(edge_key)
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
                edge_confidences.append(_score(_field(edge, "confidence", 0.0)))
        trace.append(PathConfidenceTrace(
            step="edge_confidences",
            value=round(sum(edge_confidences) / max(1, len(edge_confidences)), 3),
            detail=f"edges={len(edge_confidences)}, confidences={edge_confidences}",
        ))

        # 2. Claim-level evidence strength
        claim_strengths: list[float] = []
        stable_claims = _field(bundle, "stable_claims", [])
        for claim in stable_claims if isinstance(stable_claims, (list, tuple)) else ():
            strength = _field(claim, "claim_score", None) or _field(claim, "confidence", 0.0)
            claim_strengths.append(_score(strength))
        avg_claim_strength = sum(claim_strengths) / max(1, len(claim_strengths))
        trace.append(PathConfidenceTrace(
            step="claim_strength",
            value=round(avg_claim_strength, 3),
            detail=f"claims={len(claim_strengths)}",
        ))

        # 3. Base stable confidence
        if not edge_confidences:
            stable_confidence = 0.0
        else:
            confidences = [_score(c) for c in edge_confidences]
            hmean = harmonic_mean(confidences)
            min_edge = min(confidences)
            stable_confidence = hmean * min_edge * (0.5 + 0.5 * avg_claim_strength)
        stable_confidence = max(0.0, min(1.0, stable_confidence))
        trace.append(PathConfidenceTrace(
            step="stable_confidence",
            value=round(stable_confidence, 3),
            detail="harmonic_mean * min_edge * claim_strength_factor",
        ))

        # 4. Contradiction penalty
        contradiction_penalty = 0.0
        verification_contradictions = _field(verification, "contradictions", [])
        bundle_contradictions = _field(bundle, "contradiction_signals", [])
        if isinstance(verification_contradictions, (list, tuple)) and verification_contradictions:
            contradiction_penalty = min(0.5, 0.15 * len(verification_contradictions))
        if isinstance(bundle_contradictions, (list, tuple)) and bundle_contradictions:
            contradiction_penalty = max(contradiction_penalty, min(0.3, 0.1 * len(bundle_contradictions)))
        consistency_score = _score(_field(quality_report, "consistency_score", 0.0))
        if consistency_score < 0.35:
            contradiction_penalty = max(contradiction_penalty, 0.3)
        trace.append(PathConfidenceTrace(
            step="contradiction_penalty",
            value=round(contradiction_penalty, 3),
            detail=f"consistency_score={consistency_score}",
        ))

        # 5. Ontology compatibility bonus
        ontology_bonus = 0.0
        promotable_types = {
            "supplier_dependency_path",
            "material_bottleneck_path",
            "equipment_bottleneck_path",
            "process_barrier_path",
            "certification_barrier_path",
            "domestic_substitution_path",
            "import_dependency_path",
        }
        path_type = _field(candidate, "path_type", "")
        if path_type in promotable_types:
            ontology_bonus += 0.05
        if _field(candidate, "meta_path", ""):
            ontology_bonus += 0.03
        if _score(_field(quality_report, "structural_score", 0.0)) >= 0.7:
            ontology_bonus += 0.02
        trace.append(PathConfidenceTrace(
            step="ontology_bonus",
            value=round(ontology_bonus, 3),
            detail=f"path_type={path_type}, meta_path={_field(candidate, 'meta_path', '')}",
        ))

        # 6. Path length penalty
        hop_count = _safe_hop_count(_field(candidate, "hop_count", 0))
        if hop_count <= 2:
            length_penalty = 1.0
        elif hop_count == 3:
            length_penalty = 0.95
        else:
            length_penalty = 0.85
        trace.append(PathConfidenceTrace(
            step="length_penalty",
            value=round(length_penalty, 3),
            detail=f"hop_count={hop_count}",
        ))

        # 7. Event overlay adjustment
        event_boost = _score(event_overlay.get("event_boost_score", 0.0))
        event_risk = _score(event_overlay.get("event_risk_score", 0.0))
        freshness = _score(event_overlay.get("freshness_score", 0.0))
        contradiction_pressure = _score(event_overlay.get("contradiction_pressure", 0.0))
        event_adjusted_confidence = (
            stable_confidence * (1.0 + event_boost) * (1.0 + freshness * 0.05)
            - event_risk * 0.25
            - max(contradiction_penalty, contradiction_pressure) * 0.35
        )
        event_adjusted_confidence = max(0.0, min(1.0, event_adjusted_confidence))
        trace.append(PathConfidenceTrace(
            step="event_adjusted_confidence",
            value=round(event_adjusted_confidence, 3),
            detail=f"boost={event_boost}, risk={event_risk}, freshness={freshness}",
        ))

        # 8. Final confidence
        final_confidence = (
            event_adjusted_confidence
            * (1.0 - contradiction_penalty + ontology_bonus)
            * length_penalty
        )
        final_confidence = max(0.0, min(1.0, final_confidence))
        trace.append(PathConfidenceTrace(
            step="final_confidence",
            value=round(final_confidence, 3),
            detail="event_adjusted * (1 - contradiction + ontology) * length",
        ))

        return PathRecord(
            path_id=path_id,
            path=path,
            edge_confidences=edge_confidences,
            stable_confidence=round(stable_confidence, 3),
            event_adjusted_confidence=round(event_adjusted_confidence, 3),
            final_confidence=round(final_confidence, 3),
            ontology_bonus=round(ontology_bonus, 3),
            contradiction_penalty=round(contradiction_penalty, 3),
            length_penalty=round(length_penalty, 3),
            propagation_trace=trace,
        )

    def rebuild_path_index(
        self,
        root: str,
        max_depth: int = 5,
        max_paths: int = 200,
    ) -> dict[str, set[str]]:
        """Rebuild the reverse index from edges to paths.

        Args:
            root: Root concept name.
            max_depth: Maximum path depth.
            max_paths: Maximum number of paths to enumerate.

        Returns:
            Mapping from edge key "head|relation|tail" to set of path ids.
        """
        self._path_index = {}
        self._path_records = {}
        root = str(root or "")
        max_depth = _safe_hop_count(max_depth, 5)
        max_paths = _safe_hop_count(max_paths, 200)
        if not root or max_depth <= 0 or max_paths <= 0:
            return self._path_index

        event_result: Any = None
        event_overlays: dict[str, dict[str, Any]] = {}
        if self.event_engine is not None:
            try:
                event_result = self.event_engine.propagate_for_root(root)
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("事件 overlay 重建失败", exc_info=True)
            event_overlays = _field(event_result, "path_overlays", {})
            event_overlays = dict(event_overlays) if isinstance(event_overlays, Mapping) else {}

        scorer = FrontierScorer(kb=self.kb, entity_resolver=self.entity_resolver)
        scored_paths = scorer.enumerate_paths(root, max_depth=max_depth, max_paths=max_paths)

        for path_data in scored_paths if isinstance(scored_paths, (list, tuple)) else ():
            if not isinstance(path_data, Mapping):
                continue
            path = path_data.get("path", [])
            path = [str(item) for item in path if item is not None] if isinstance(path, (list, tuple)) else []
            if len(path) < 2:
                continue

            path_id = self._path_id(path)
            event_overlay = event_overlays.get("->".join(path))
            record = PathRecord(
                path_id=path_id,
                path=path,
                path_alpha_score=path_data.get("path_alpha_score", 0.0),
            )

            # Build edge confidence list and reverse index
            edge_confidences: list[float] = []
            has_conflict = False
            has_gap = False

            for i in range(len(path) - 1):
                head = path[i]
                tail = path[i + 1]
                edge = None
                try:
                    neighbors = self.kb.get_neighbors(head, direction="out")
                except (AttributeError, KeyError, TypeError, ValueError):
                    neighbors = []
                for candidate in neighbors if isinstance(neighbors, (list, tuple)) else ():
                    if _field(candidate, "tail") == tail:
                        edge = candidate
                        break

                if edge is None:
                    edge_confidences.append(0.0)
                    has_gap = True
                    continue

                edge_key = (
                    _field(edge, "head", head),
                    _field(edge, "relation", ""),
                    _field(edge, "tail", tail),
                )
                try:
                    self.kb.update_edge_confidence(edge_key)
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
                edge_confidences.append(_score(_field(edge, "confidence", 0.0)))

                edge_index_key = "|".join(str(item) for item in edge_key)
                self._path_index.setdefault(edge_index_key, set()).add(path_id)

                if _field(edge, "conflict_status", "none") != "none":
                    has_conflict = True

            # Evaluate evidence gaps
            try:
                path_quality = self.path_quality_evaluator.evaluate_path(self.kb, path)
            except (AttributeError, KeyError, TypeError, ValueError):
                path_quality = {}
            path_quality = path_quality if isinstance(path_quality, Mapping) else {}
            record.path_evidence_gap = _mappings(path_quality.get("evidence_gaps", []))
            record.path_noise_score = _score(path_quality.get("noise_node_ratio", 0.0))
            has_gap = has_gap or bool(record.path_evidence_gap)

            record.edge_confidences = edge_confidences
            record.path_confidence = self.compute_path_confidence(
                edge_confidences,
                has_conflict=has_conflict,
                has_gap=has_gap,
            )
            if event_overlay is not None:
                event_overlay = event_overlay if isinstance(event_overlay, Mapping) else {}
                record.event_boost_score = _score(event_overlay.get("event_boost_score", 0.0))
                record.event_risk_score = _score(event_overlay.get("event_risk_score", 0.0))
                record.event_freshness_score = _score(event_overlay.get("freshness_score", 0.0))
                record.event_adjusted_confidence = self.compute_path_confidence(
                    edge_confidences,
                    has_conflict=has_conflict,
                    has_gap=has_gap,
                    event_overlay=event_overlay,
                )
            self._path_records[path_id] = record

        return self._path_index

    def update_paths_for_edge(
        self,
        edge_key: tuple[str, str, str],
    ) -> dict[str, Any]:
        """Update confidence for all paths containing the given edge.

        Args:
            edge_key: Tuple (head, relation, tail).

        Returns:
            Dictionary with affected path ids and updated path records.
        """
        normalized_edge_key = _edge_key(edge_key)
        if normalized_edge_key is None:
            return {"affected_path_ids": [], "updated_paths": []}
        head, relation, tail = normalized_edge_key
        edge_str = f"{head}|{relation}|{tail}"

        # Ensure edge confidence is up to date
        try:
            self.kb.update_edge_confidence(normalized_edge_key)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        edge = _mapping_get(getattr(self.kb, "edges", {}), normalized_edge_key)
        if edge is None:
            return {"affected_path_ids": [], "updated_paths": []}

        affected_path_ids = list(self._path_index.get(edge_str, set()))
        updated_paths: list[dict[str, Any]] = []

        # Cache event overlays per root to avoid redundant propagation calls.
        event_overlays_by_root: dict[str, dict[str, dict[str, Any]]] = {}

        for path_id in affected_path_ids:
            record = self._path_records.get(path_id)
            if record is None or not record.path:
                continue

            # Recompute edge confidences for the path
            edge_confidences: list[float] = []
            has_conflict = False
            has_gap = False

            for i in range(len(record.path) - 1):
                h = record.path[i]
                t = record.path[i + 1]
                path_edge = None
                try:
                    neighbors = self.kb.get_neighbors(h, direction="out")
                except (AttributeError, KeyError, TypeError, ValueError):
                    neighbors = []
                for candidate in neighbors if isinstance(neighbors, (list, tuple)) else ():
                    if _field(candidate, "tail") == t:
                        path_edge = candidate
                        break

                if path_edge is None:
                    edge_confidences.append(0.0)
                    has_gap = True
                    continue

                edge_confidences.append(_score(_field(path_edge, "confidence", 0.0)))
                if _field(path_edge, "conflict_status", "none") != "none":
                    has_conflict = True

            try:
                path_quality = self.path_quality_evaluator.evaluate_path(self.kb, record.path)
            except (AttributeError, KeyError, TypeError, ValueError):
                path_quality = {}
            path_quality = path_quality if isinstance(path_quality, Mapping) else {}
            record.path_evidence_gap = _mappings(path_quality.get("evidence_gaps", []))
            record.path_noise_score = _score(path_quality.get("noise_node_ratio", 0.0))
            has_gap = has_gap or bool(record.path_evidence_gap)

            record.edge_confidences = edge_confidences
            record.path_confidence = self.compute_path_confidence(
                edge_confidences,
                has_conflict=has_conflict,
                has_gap=has_gap,
            )

            if self.event_engine is not None:
                root = record.path[0]
                if root not in event_overlays_by_root:
                    try:
                        event_result = self.event_engine.propagate_for_root(root)
                    except (AttributeError, KeyError, TypeError, ValueError):
                        event_result = None
                    overlays = _field(event_result, "path_overlays", {})
                    event_overlays_by_root[root] = dict(overlays) if isinstance(overlays, Mapping) else {}
                event_overlay = event_overlays_by_root[root].get("->".join(record.path))
                if event_overlay is not None:
                    event_overlay = event_overlay if isinstance(event_overlay, Mapping) else {}
                    record.event_boost_score = _score(event_overlay.get("event_boost_score", 0.0))
                    record.event_risk_score = _score(event_overlay.get("event_risk_score", 0.0))
                    record.event_freshness_score = _score(event_overlay.get("freshness_score", 0.0))
                    record.event_adjusted_confidence = self.compute_path_confidence(
                        edge_confidences,
                        has_conflict=has_conflict,
                        has_gap=has_gap,
                        event_overlay=event_overlay,
                    )

            updated_paths.append(record.to_dict())

        return {
            "affected_path_ids": affected_path_ids,
            "updated_paths": updated_paths,
        }

    def get_path_records(self) -> list[PathRecord]:
        """Return all currently indexed path records."""
        return list(self._path_records.values())
