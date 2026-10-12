from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.research_graph.schema import (
    AUDIT_THEME_RELATION_TYPES,
    EdgeStatus,
    EvidenceLevel,
    NodeType,
    RelationType,
)


CONFIDENCE_SCORE = {
    EvidenceLevel.PROVEN.value: 1.0,
    EvidenceLevel.LIKELY.value: 0.75,
    EvidenceLevel.SPECULATIVE.value: 0.35,
    EvidenceLevel.CONFLICTED.value: 0.15,
    EvidenceLevel.DISPROVEN.value: 0.0,
}


@dataclass
class RankedBenefitPath:
    path_id: str
    score: float
    score_breakdown: dict[str, float]
    path: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlphaPathRanker:
    """Rank verified benefit paths, never naked stocks or audit-only edges."""

    def rank(self, paths: list[dict[str, Any]]) -> dict[str, Any]:
        ranked = [
            self._score_path(index, path)
            for index, path in enumerate(paths, start=1)
            if self._is_rankable(path)
        ]
        ranked.sort(key=lambda item: (-item.score, item.path_id))
        return {
            "result_type": "ranked_benefit_paths",
            "ranking_object": "主题 → 瓶颈 → 工艺/材料/设备 → 公司 → 证据",
            "paths": [item.to_dict() for item in ranked],
        }

    def _score_path(self, index: int, path: dict[str, Any]) -> RankedBenefitPath:
        edges = path.get("edges") or []
        nodes = path.get("nodes") or []
        confidence_values = [
            CONFIDENCE_SCORE.get(edge.get("confidence"), 0.0)
            for edge in edges
        ]
        confidence_score = (
            sum(confidence_values) / len(confidence_values)
            if confidence_values else 0.0
        )
        evidence_count = sum(len(edge.get("evidence_ids") or []) for edge in edges)
        counter_count = sum(len(edge.get("counter_evidence_ids") or []) for edge in edges)
        evidence_score = min(1.0, evidence_count / max(1, len(edges)))
        counter_penalty = min(0.5, counter_count * 0.15)
        bottleneck_score = 1.0 if self._has_bottleneck_signal(path) else 0.0
        concrete_chain_score = 1.0 if self._has_concrete_chain(nodes) else 0.3
        score = max(
            0.0,
            0.35 * confidence_score
            + 0.25 * evidence_score
            + 0.20 * bottleneck_score
            + 0.20 * concrete_chain_score
            - counter_penalty,
        )
        return RankedBenefitPath(
            path_id=f"path_{index:03d}",
            score=round(score, 4),
            score_breakdown={
                "confidence": round(confidence_score, 4),
                "evidence": round(evidence_score, 4),
                "bottleneck": round(bottleneck_score, 4),
                "concrete_chain": round(concrete_chain_score, 4),
                "counter_penalty": round(counter_penalty, 4),
            },
            path=path,
        )

    @staticmethod
    def _is_rankable(path: dict[str, Any]) -> bool:
        edges = path.get("edges") or []
        if not edges:
            return False
        return all(
            edge.get("status") == EdgeStatus.ACTIVE.value
            and bool(edge.get("evidence_ids"))
            and not edge.get("counter_evidence_ids")
            and edge.get("relation") not in AUDIT_THEME_RELATION_TYPES
            for edge in edges
        )

    @staticmethod
    def _has_bottleneck_signal(path: dict[str, Any]) -> bool:
        for node in path.get("nodes") or []:
            if node.get("type") == NodeType.CONSTRAINT.value:
                return True
        for edge in path.get("edges") or []:
            if edge.get("relation") in {RelationType.BOTTLENECK_OF.value, RelationType.CONSTRAINED_BY.value}:
                return True
            if edge.get("qualifiers", {}).get("constraint_reason"):
                return True
        return False

    @staticmethod
    def _has_concrete_chain(nodes: list[dict[str, Any]]) -> bool:
        node_types = {node.get("type") for node in nodes}
        return bool(
            NodeType.THEME.value in node_types
            and {NodeType.PROCESS.value, NodeType.MATERIAL.value, NodeType.EQUIPMENT.value} & node_types
            and {NodeType.COMPANY.value, NodeType.SUBSIDIARY.value} & node_types
        )
