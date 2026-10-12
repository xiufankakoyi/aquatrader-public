"""Path quality evaluator for Graph-first Alpha Discovery Engine.

Evaluates path-level quality metrics including upstream bottleneck coverage,
company position validity, trusted claim coverage, source diversity,
contradiction ratio, evidence gaps, and noise ratio.

In the Path-based Alpha Discovery pipeline, this module serves as the unified
quality assessment layer: it consumes PathCandidate, PathEvidenceBundle,
PathVerificationResult and event overlay, and produces a PathQualityReport.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import harmonic_mean
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    NODE_TYPES,
    NodeType,
    SourceType,
    StructuralAxis,
    get_source_weight,
)
from core.research.perilla.alpha.claim_quality_scorer import ClaimQualityScorer
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.alpha.frontier_scorer import FrontierScorer
from core.research.perilla.alpha.path_discovery_schema import (
    PathCandidate,
    PathEvidenceBundle,
    PathQualityReport,
    PathVerificationResult,
)
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


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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


def _safe_int(value: Any, default: int = 0, maximum: int = 1000000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default

_BOTTLENECK_AXES: set[str] = {
    StructuralAxis.MATERIAL.value,
    StructuralAxis.EQUIPMENT.value,
    StructuralAxis.PROCESS.value,
    StructuralAxis.CERTIFICATION.value,
    "import_dependency_axis",
    "supplier_axis",
    "capacity_axis",
}

_VALID_PATH_TYPES: set[str] = {
    "supplier_dependency_path",
    "material_bottleneck_path",
    "equipment_bottleneck_path",
    "process_barrier_path",
    "certification_barrier_path",
    "domestic_substitution_path",
    "import_dependency_path",
    "event_boosted_path",
    "unknown",
}

# Quality label thresholds (applied to final_quality_score and component checks)
_QUALITY_LABELS: list[tuple[str, float]] = [
    ("REJECTED_PATH", 0.0),
    ("CONTRADICTED_PATH", 0.0),
    ("WEAK_EVIDENCE_PATH", 0.25),
    ("PROBE_FIRST_PATH", 0.45),
    ("PROMOTION_READY_PATH", 0.70),
    ("STRONG_ALPHA_PATH", 0.80),
]


@dataclass
class PathQualityMetrics:
    """Aggregated path quality metrics for a root or a single path."""

    upstream_bottleneck_path_ratio: float = 0.0
    company_node_position_valid_rate: float = 0.0
    trusted_claim_coverage_rate: float = 0.0
    generic_node_ratio: float = 0.0
    source_diversity_score: float = 0.0
    contradiction_ratio: float = 0.0
    unresolved_conflict_count: int = 0
    evidence_gap_count: int = 0
    noise_node_ratio: float = 0.0
    valid_company_mapping_ratio: float = 0.0
    invalid_company_positions: list[dict[str, Any]] = field(default_factory=list)
    evidence_gaps: list[dict[str, Any]] = field(default_factory=list)
    path_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation."""
        return {
            "upstream_bottleneck_path_ratio": _score(self.upstream_bottleneck_path_ratio),
            "company_node_position_valid_rate": _score(self.company_node_position_valid_rate),
            "trusted_claim_coverage_rate": _score(self.trusted_claim_coverage_rate),
            "generic_node_ratio": _score(self.generic_node_ratio),
            "source_diversity_score": _score(self.source_diversity_score),
            "contradiction_ratio": _score(self.contradiction_ratio),
            "unresolved_conflict_count": _safe_int(self.unresolved_conflict_count),
            "evidence_gap_count": _safe_int(self.evidence_gap_count),
            "noise_node_ratio": _score(self.noise_node_ratio),
            "valid_company_mapping_ratio": _score(self.valid_company_mapping_ratio),
            "invalid_company_positions": _items(self.invalid_company_positions),
            "evidence_gaps": _items(self.evidence_gaps),
            "path_count": _safe_int(self.path_count),
        }


class PathQualityEvaluator:
    """Evaluate quality metrics for paths in the SAG knowledge base."""

    def __init__(
        self,
        entity_resolver: EntityResolver | None = None,
        quality_scorer: ClaimQualityScorer | None = None,
    ) -> None:
        """Initialize the evaluator.

        Args:
            entity_resolver: Resolver for entity type and position checks.
            quality_scorer: Scorer for noise penalty computation.
        """
        self.entity_resolver = entity_resolver or EntityResolver()
        self.quality_scorer = quality_scorer or ClaimQualityScorer(kb=None)

    # ------------------------------------------------------------------
    # Path-aware unified evaluation (new main API)
    # ------------------------------------------------------------------

    def evaluate_path_candidate(
        self,
        candidate: PathCandidate,
        bundle: PathEvidenceBundle | None = None,
        verification: PathVerificationResult | None = None,
        event_overlay: dict[str, Any] | None = None,
    ) -> PathQualityReport:
        """Evaluate a PathCandidate and produce a unified PathQualityReport.

        This is the core entry point for the Path-based Alpha Discovery pipeline.
        It never invents evidence; all scores are derived from the provided
        bundle, verification result, event overlay and KB state.

        Args:
            candidate: The structural path candidate.
            bundle: Optional evidence bundle bound to the path.
            verification: Optional LLM/rule verification result.
            event_overlay: Optional event overlay dict (e.g. from
                DynamicEventPropagationEngine) with keys such as
                event_boost_score, event_risk_score, freshness_score.

        Returns:
            PathQualityReport with quality_label and sub-scores.
        """
        if not isinstance(candidate, PathCandidate):
            candidate = PathCandidate(path_id="")
        bundle = bundle if isinstance(bundle, PathEvidenceBundle) else PathEvidenceBundle(
            path_candidate=candidate
        )
        verification = (
            verification
            if isinstance(verification, PathVerificationResult)
            else PathVerificationResult(path_id=candidate.path_id)
        )
        event_overlay = dict(event_overlay) if isinstance(event_overlay, Mapping) else {}

        structural_score = self._structural_score(candidate)
        evidence_score = self._evidence_score(candidate, bundle, verification)
        source_score = self._source_score(bundle)
        consistency_score = self._consistency_score(bundle, verification)
        freshness_score = self._freshness_score(bundle, event_overlay)
        bottleneck_score = self._bottleneck_score(candidate, verification)
        diversity_score = self._diversity_score(candidate)

        final_quality_score = self._aggregate(
            structural_score,
            evidence_score,
            source_score,
            consistency_score,
            freshness_score,
            bottleneck_score,
            diversity_score,
        )

        quality_label = self._select_quality_label(
            final_quality_score,
            evidence_score,
            consistency_score,
            structural_score,
            bottleneck_score,
            bundle,
            verification,
        )

        return PathQualityReport(
            path_id=candidate.path_id,
            structural_score=round(structural_score, 3),
            evidence_score=round(evidence_score, 3),
            source_score=round(source_score, 3),
            consistency_score=round(consistency_score, 3),
            freshness_score=round(freshness_score, 3),
            bottleneck_score=round(bottleneck_score, 3),
            diversity_score=round(diversity_score, 3),
            final_quality_score=round(final_quality_score, 3),
            quality_label=quality_label,
        )

    @staticmethod
    def _structural_score(candidate: PathCandidate) -> float:
        """Score how well the path conforms to ontology and meta-path constraints."""
        nodes = _items(_field(candidate, "nodes", []))
        node_types = _items(_field(candidate, "node_types", []))
        axes = [axis for axis in _items(_field(candidate, "axes", [])) if isinstance(axis, str)]
        hop_count = _safe_int(_field(candidate, "hop_count", 0))
        if len(nodes) < 2 or hop_count < 1:
            return 0.0

        score = 0.5

        # Valid path type
        path_type = str(_field(candidate, "path_type", "unknown") or "unknown")
        if path_type in _VALID_PATH_TYPES and path_type != "unknown":
            score += 0.2

        # Meta-path match
        if str(_field(candidate, "meta_path", "") or ""):
            score += 0.15

        # Hop count within preferred 1-3 range; 4-hop requires strong evidence elsewhere
        if hop_count <= 3:
            score += 0.1
        elif hop_count == 4:
            score += 0.02

        # Node types present and not all generic
        if node_types:
            generic_ratio = sum(
                1 for t in node_types if t == NodeType.GENERIC.value
            ) / len(node_types)
            score -= generic_ratio * 0.25

        # Axes coverage: at least one bottleneck axis improves structural fit
        if axes and set(axes) & _BOTTLENECK_AXES:
            score += 0.05

        return max(0.0, min(1.0, score))

    def _evidence_score(
        self,
        candidate: PathCandidate,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
    ) -> float:
        """Score evidence coverage over the path's critical edges."""
        total_edges = max(1, len(_items(candidate.edges)))
        coverage = _score(_field(bundle, "coverage_score", 0.0))
        supported_edges = _items(_field(verification, "supported_edges", []))
        weak_edges = _items(_field(verification, "weak_edges", []))
        unsupported_edges = _items(_field(verification, "unsupported_edges", []))

        supported_ratio = min(1.0, len(supported_edges) / total_edges)
        weak_ratio = min(1.0, len(weak_edges) / total_edges)
        unsupported_ratio = min(1.0, len(unsupported_edges) / total_edges)

        score = (
            coverage * 0.35
            + supported_ratio * 0.35
            + _score(_field(verification, "evidence_strength", 0.0)) * 0.20
            - unsupported_ratio * 0.15
            - weak_ratio * 0.05
        )

        # LLM verifier invalidity is a strong signal of missing or wrong evidence
        if not _field(verification, "is_valid", False) and _score(_field(verification, "validity_score", 0.0)) < 0.3:
            score -= 0.25

        return max(0.0, min(1.0, score))

    def _source_score(self, bundle: PathEvidenceBundle) -> float:
        """Score source reliability based on the evidence bundle's source summary."""
        source_summary = _mapping(_field(bundle, "source_summary", {}))
        if not source_summary:
            return 0.0

        counts = {
            source_type: max(0.0, _finite(count))
            for source_type, count in source_summary.items()
        }
        total = sum(counts.values())
        if total == 0:
            return 0.0

        weighted = sum(
            count * get_source_weight(str(source_type))
            for source_type, count in counts.items()
        )
        avg_weight = weighted / total

        # Diversity bonus: at least two distinct high-quality sources
        distinct_sources = len(counts)
        diversity_bonus = min(0.15, 0.05 * max(0, distinct_sources - 1))

        return max(0.0, min(1.0, avg_weight + diversity_bonus))

    def _consistency_score(
        self,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
    ) -> float:
        """Score absence of contradictions along the path."""
        score = 1.0

        signal_count = len(_items(_field(bundle, "contradiction_signals", [])))
        contradiction_count = len(_items(_field(verification, "contradictions", [])))

        score -= min(0.5, signal_count * 0.15)
        score -= min(0.4, contradiction_count * 0.20)

        if _field(verification, "is_valid", False) is False and _score(_field(verification, "validity_score", 0.0)) < 0.3:
            score -= 0.1

        return max(0.0, min(1.0, score))

    @staticmethod
    def _freshness_score(
        bundle: PathEvidenceBundle,
        event_overlay: dict[str, Any],
    ) -> float:
        """Score evidence/event freshness without letting events override structure."""
        # Event overlay freshness (capped influence)
        event_freshness = _score(_mapping(event_overlay).get("freshness_score", 0.0))

        # Evidence timestamps: recent evidence within 180 days scores higher
        from datetime import datetime

        now = datetime.now()
        evidence_freshness_values: list[float] = []
        for chunk in _items(_field(bundle, "evidence_chunks", [])):
            if not isinstance(chunk, Mapping):
                continue
            ts = chunk.get("timestamp") or chunk.get("claim_timestamp")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                days = (now - dt).days
                if days < 0:
                    days = 0
                evidence_freshness_values.append(max(0.0, 1.0 - days / 180.0))
            except Exception:
                continue

        evidence_freshness = (
            sum(evidence_freshness_values) / len(evidence_freshness_values)
            if evidence_freshness_values
            else 0.5
        )

        # Events provide a boost but cannot dominate structural evidence
        return max(0.0, min(1.0, evidence_freshness * 0.7 + event_freshness * 0.3))

    def _bottleneck_score(
        self,
        candidate: PathCandidate,
        verification: PathVerificationResult,
    ) -> float:
        """Score whether the path expresses bottleneck / irreplaceability / hard-to-expand."""
        score = 0.0

        bottleneck_path_types = {
            "material_bottleneck_path",
            "equipment_bottleneck_path",
            "process_barrier_path",
            "certification_barrier_path",
            "import_dependency_path",
        }
        path_type = str(_field(candidate, "path_type", "unknown") or "unknown")
        if path_type in bottleneck_path_types:
            score += 0.6
        elif path_type == "domestic_substitution_path":
            score += 0.45
        elif path_type == "supplier_dependency_path":
            score += 0.35

        # Axes alignment
        axes = [axis for axis in _items(_field(candidate, "axes", [])) if isinstance(axis, str)]
        if axes:
            axis_set = set(axes)
            if axis_set & {
                StructuralAxis.MATERIAL.value,
                StructuralAxis.EQUIPMENT.value,
                StructuralAxis.PROCESS.value,
            }:
                score += 0.15
            if StructuralAxis.CERTIFICATION.value in axis_set:
                score += 0.15
            if StructuralAxis.DOMESTIC_SUBSTITUTION.value in axis_set:
                score += 0.10

        # Verifier bottleneck type
        if str(_field(verification, "bottleneck_type", "unknown") or "unknown") != "unknown":
            score += 0.1

        return max(0.0, min(1.0, score))

    def _diversity_score(self, candidate: PathCandidate) -> float:
        """Score structural diversity / novelty of the path."""
        score = 0.3

        distinct_node_types = len({str(item) for item in _items(_field(candidate, "node_types", []))})
        distinct_axes = len({str(item) for item in _items(_field(candidate, "axes", []))})
        distinct_relations = len({str(item) for item in _items(_field(candidate, "relations", []))})

        score += min(0.25, distinct_node_types * 0.08)
        score += min(0.25, distinct_axes * 0.08)
        score += min(0.20, distinct_relations * 0.07)

        # Prefer paths that reach a different target entity than the root
        if _field(candidate, "target_entity", "") and _field(candidate, "target_entity", "") != _field(candidate, "root_entity", ""):
            score += 0.05

        return max(0.0, min(1.0, score))

    @staticmethod
    def _aggregate(
        structural_score: float,
        evidence_score: float,
        source_score: float,
        consistency_score: float,
        freshness_score: float,
        bottleneck_score: float,
        diversity_score: float,
    ) -> float:
        """Aggregate sub-scores into a final quality score.

        Weights emphasize evidence + consistency + bottleneck semantic, while
        freshness and diversity provide moderate bonuses.
        """
        return (
            structural_score * 0.15
            + evidence_score * 0.25
            + source_score * 0.10
            + consistency_score * 0.15
            + freshness_score * 0.05
            + bottleneck_score * 0.20
            + diversity_score * 0.10
        )

    @staticmethod
    def _select_quality_label(
        final_score: float,
        evidence_score: float,
        consistency_score: float,
        structural_score: float,
        bottleneck_score: float,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
    ) -> str:
        """Select a quality label based on the aggregated scores.

        The label ordering follows severity: contradicted > rejected > weak >
        probe-first > promotion-ready > strong alpha.
        """
        # Contradiction overrides everything
        if _score(consistency_score) < 0.35 or _items(_field(verification, "contradictions", [])):
            return "CONTRADICTED_PATH"

        # Rejected if structurally invalid and no evidence
        if structural_score < 0.2 and evidence_score < 0.2:
            return "REJECTED_PATH"

        # Weak evidence: not enough support to act on
        if _score(evidence_score) < 0.25 or _score(_field(bundle, "coverage_score", 0.0)) < 0.25:
            return "WEAK_EVIDENCE_PATH"

        # Probe first: promising but gaps remain
        if _score(evidence_score) < 0.55 or _score(_field(bundle, "coverage_score", 0.0)) < 0.55:
            return "PROBE_FIRST_PATH"

        # Promotion ready: solid but not necessarily a top alpha
        if _score(final_score) >= 0.70 and _score(evidence_score) >= 0.55 and _score(consistency_score) >= 0.65:
            return "PROMOTION_READY_PATH"

        # Strong alpha: high final score with good bottleneck signal
        if _score(final_score) >= 0.80 and _score(bottleneck_score) >= 0.55 and _score(consistency_score) >= 0.70:
            return "STRONG_ALPHA_PATH"

        if _score(final_score) >= 0.70:
            return "PROMOTION_READY_PATH"

        return "PROBE_FIRST_PATH"

    # ------------------------------------------------------------------
    # Legacy per-path / per-root evaluation (kept for existing callers)
    # ------------------------------------------------------------------

    def evaluate_path(
        self,
        kb: SAGKnowledgeBase,
        path: list[str],
    ) -> dict[str, Any]:
        """Evaluate a single path and return per-path metrics.

        Args:
            kb: SAG knowledge base.
            path: List of node names from root to leaf.

        Returns:
            Dictionary with per-path quality fields.
        """
        path = [str(item) for item in path if item is not None] if isinstance(path, (list, tuple)) else []
        if len(path) < 2:
            return {
                "path": path,
                "is_upstream_bottleneck": False,
                "company_node_position_valid_rate": 0.0,
                "trusted_claim_coverage_rate": 0.0,
                "generic_node_ratio": 0.0,
                "source_diversity_score": 0.0,
                "contradiction_ratio": 0.0,
                "evidence_gap_count": 0,
                "noise_node_ratio": 0.0,
                "valid_company_mapping": False,
                "invalid_company_positions": [],
                "evidence_gaps": [],
            }

        edges_in_path: list[Any] = []
        nodes = getattr(kb, "nodes", {})
        neighbor_getter = getattr(kb, "get_neighbors", None)
        for i in range(len(path) - 1):
            head = path[i]
            tail = path[i + 1]
            edge = None
            try:
                neighbors = neighbor_getter(head, direction="out") if callable(neighbor_getter) else []
            except (AttributeError, KeyError, TypeError, ValueError):
                neighbors = []
            for candidate in neighbors if isinstance(neighbors, (list, tuple)) else ():
                if _field(candidate, "tail") == tail:
                    edge = candidate
                    break
            if edge is not None:
                edges_in_path.append(edge)

        # Upstream bottleneck: path touches at least one bottleneck axis node
        bottleneck_node_count = 0
        for node_name in path:
            node = _mapping_get(nodes, node_name)
            if not node:
                continue
            node_axes = [axis for axis in _items(_field(node, "axes", None)) if isinstance(axis, str)]
            if node_axes and set(node_axes) & _BOTTLENECK_AXES:
                bottleneck_node_count += 1
            if _field(node, "node_type", None) in {
                NodeType.MATERIAL.value,
                NodeType.EQUIPMENT.value,
                NodeType.PROCESS.value,
                NodeType.TESTING.value,
                NodeType.CONSUMABLE.value,
                NodeType.CERTIFICATION.value,
            }:
                bottleneck_node_count += 1
        is_upstream_bottleneck = bottleneck_node_count > 0

        # Company position validity
        total_company_positions = 0
        valid_company_positions = 0
        invalid_company_positions: list[dict[str, Any]] = []
        for edge in edges_in_path:
            head = _field(edge, "head", "")
            relation = _field(edge, "relation", "")
            tail = _field(edge, "tail", "")
            try:
                head_resolved = self.entity_resolver.resolve(head)
                tail_resolved = self.entity_resolver.resolve(tail)
            except (AttributeError, TypeError, ValueError):
                head_resolved = tail_resolved = None
            for name, resolved in [(head, head_resolved), (tail, tail_resolved)]:
                if _field(resolved, "entity_type", "") == "company":
                    total_company_positions += 1
                    try:
                        valid = self.entity_resolver.is_company_position_valid(head, relation, tail)
                    except (AttributeError, TypeError, ValueError):
                        valid = True
                    if valid:
                        valid_company_positions += 1
                    else:
                        invalid_company_positions.append({
                            "node": name,
                            "edge": (head, relation, tail),
                        })

        company_valid_rate = (
            valid_company_positions / total_company_positions
            if total_company_positions > 0
            else 1.0
        )

        # Trusted claim coverage and source diversity
        edges_with_trusted = 0
        total_distinct_sources = 0
        contradiction_count = 0
        evidence_gaps: list[dict[str, Any]] = []

        for edge in edges_in_path:
            claim_ids = _items(_field(edge, "claim_ids", None))
            claims = getattr(kb, "claims", {})
            claims = claims if isinstance(claims, Mapping) else {}
            edge_claims = []
            for cid in claim_ids:
                claim = _mapping_get(claims, cid)
                if claim is not None:
                    edge_claims.append(claim)
            source_types = {
                str(_field(c, "source_type", "unknown"))
                for c in edge_claims
            }
            total_distinct_sources += len(source_types)

            trusted_supported = [
                c for c in edge_claims
                if _field(c, "status", None) in ("trusted", "supported")
            ]
            if trusted_supported:
                edges_with_trusted += 1

            if _field(edge, "conflict_status", "none") != "none" or _field(edge, "contradicting_claim_ids", None):
                contradiction_count += 1

            if len(source_types) < 2 or not trusted_supported:
                evidence_gaps.append({
                    "head": _field(edge, "head", ""),
                    "tail": _field(edge, "tail", ""),
                    "relation": _field(edge, "relation", ""),
                    "source_types": sorted(source_types),
                    "missing": "multi_source_or_trusted_claim" if len(source_types) < 2 else "trusted_or_supported_claim",
                })

        trusted_coverage = (
            edges_with_trusted / len(edges_in_path)
            if edges_in_path
            else 0.0
        )
        source_diversity = (
            min(1.0, total_distinct_sources / max(1, 2 * len(edges_in_path)))
            if edges_in_path
            else 0.0
        )
        contradiction_ratio = (
            contradiction_count / len(edges_in_path)
            if edges_in_path
            else 0.0
        )

        # Generic node ratio
        generic_count = 0
        noise_count = 0
        for node_name in path:
            node = _mapping_get(nodes, node_name)
            if not node:
                continue
            if _field(node, "node_type", "") == NodeType.GENERIC.value:
                generic_count += 1
            metadata = _mapping(_field(node, "metadata", None))
            if _finite(metadata.get("noise_score", 0)) > 0.15:
                noise_count += 1
            elif metadata.get("user_marked") == "useful":
                continue
            else:
                # Approximate noise via resolver confidence
                try:
                    resolved = self.entity_resolver.resolve(node_name)
                except (AttributeError, TypeError, ValueError):
                    resolved = None
                if _score(_field(resolved, "resolver_confidence", 0.0)) < 0.3 and _field(resolved, "entity_type", "") == "generic_term":
                    noise_count += 1

        generic_ratio = generic_count / len(path) if path else 0.0
        noise_ratio = noise_count / len(path) if path else 0.0

        # Valid company mapping: path has at least one company node and all are valid
        has_company = False
        for node_name in path:
            try:
                if self.entity_resolver.is_company(node_name):
                    has_company = True
                    break
            except (AttributeError, TypeError, ValueError):
                continue
        valid_company_mapping = (
            has_company and total_company_positions > 0 and valid_company_positions == total_company_positions
        )

        return {
            "path": path,
            "is_upstream_bottleneck": is_upstream_bottleneck,
            "company_node_position_valid_rate": company_valid_rate,
            "trusted_claim_coverage_rate": trusted_coverage,
            "generic_node_ratio": generic_ratio,
            "source_diversity_score": source_diversity,
            "contradiction_ratio": contradiction_ratio,
            "evidence_gap_count": len(evidence_gaps),
            "noise_node_ratio": noise_ratio,
            "valid_company_mapping": valid_company_mapping,
            "invalid_company_positions": invalid_company_positions,
            "evidence_gaps": evidence_gaps,
        }

    def evaluate_root(
        self,
        kb: SAGKnowledgeBase,
        root: str,
        paths: list[dict[str, Any]] | None = None,
        max_depth: int = 5,
        max_paths: int = 200,
    ) -> PathQualityMetrics:
        """Evaluate aggregate path quality metrics for a root concept.

        Args:
            kb: SAG knowledge base.
            root: Root concept name.
            paths: Pre-computed scored paths. If None, paths are enumerated.
            max_depth: Maximum path depth when enumerating paths.
            max_paths: Maximum number of paths when enumerating.

        Returns:
            PathQualityMetrics aggregate object.
        """
        max_depth = max(1, _safe_int(max_depth, 5, 50))
        max_paths = max(1, _safe_int(max_paths, 200, 5000))
        if paths is None:
            scorer = FrontierScorer(kb=kb, entity_resolver=self.entity_resolver)
            paths = scorer.enumerate_paths(root, max_depth=max_depth, max_paths=max_paths)

        metrics = PathQualityMetrics()
        if not paths:
            return metrics

        evaluated: list[dict[str, Any]] = []
        for path_data in _items(paths):
            path = _mapping(path_data).get("path", [])
            if not isinstance(path, (list, tuple)) or len(path) < 2:
                continue
            evaluated.append(self.evaluate_path(kb, path))

        if not evaluated:
            return metrics

        metrics.path_count = len(evaluated)
        metrics.upstream_bottleneck_path_ratio = sum(
            1 for e in evaluated if e.get("is_upstream_bottleneck")
        ) / len(evaluated)
        metrics.company_node_position_valid_rate = sum(
            _score(e.get("company_node_position_valid_rate", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.trusted_claim_coverage_rate = sum(
            _score(e.get("trusted_claim_coverage_rate", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.generic_node_ratio = sum(
            _score(e.get("generic_node_ratio", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.source_diversity_score = sum(
            _score(e.get("source_diversity_score", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.contradiction_ratio = sum(
            _score(e.get("contradiction_ratio", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.evidence_gap_count = sum(
            e.get("evidence_gap_count", 0) for e in evaluated
        )
        metrics.noise_node_ratio = sum(
            _score(e.get("noise_node_ratio", 0)) for e in evaluated
        ) / len(evaluated)
        metrics.valid_company_mapping_ratio = sum(
            1 for e in evaluated if e.get("valid_company_mapping")
        ) / len(evaluated)

        for e in evaluated:
            metrics.invalid_company_positions.extend(e.get("invalid_company_positions", []))
            metrics.evidence_gaps.extend(e.get("evidence_gaps", []))

        edges = getattr(kb, "edges", {})
        edges = edges.values() if isinstance(edges, Mapping) else []
        metrics.unresolved_conflict_count = sum(
            1
            for edge in edges
            if _field(edge, "conflict_status", "none") not in ("none", "resolved")
        )

        return metrics
