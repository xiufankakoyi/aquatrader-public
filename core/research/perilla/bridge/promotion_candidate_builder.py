"""Promotion candidate builder.

Scans the event graph for high-confidence EventEdges and converts them into
``PromotionCandidate`` objects that can be reviewed before entering the stable
graph. The event-type to stable-relation mapping is resolved through the
shared ``OntologyRegistry`` instead of a hardcoded dictionary.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.bridge.models import normalize_name, stable_id
from core.research.perilla.event.event_graph_schema import (
    EventEdge,
    PromotionCandidate,
)
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

logger = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(0.0, min(1.0, number))


def _bounded_int(value: Any, default: int, maximum: int = 10000) -> int:
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


class PromotionCandidateBuilder:
    """将高置信 EventClaim / EventEdge 转成 stable claim 候选。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        ontology_registry: OntologyRegistry | None = None,
        min_edge_confidence: float = 0.55,
        min_source_count: int = 2,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            entity_resolver: 实体解析器。
            ontology_registry: Ontology 注册表，None 则使用默认值。
            min_edge_confidence: 事件边晋升最低置信度。
            min_source_count: 晋升所需最少不同来源数。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.ontology_registry = ontology_registry or OntologyRegistry.default()
        self.min_edge_confidence = _score(min_edge_confidence, 0.55)
        self.min_source_count = _bounded_int(min_source_count, 2, 10000)

    def build_candidates(
        self,
        max_candidates: int = 50,
    ) -> list[PromotionCandidate]:
        """扫描事件图中满足晋升条件的 EventEdge，生成 PromotionCandidate。

        Args:
            max_candidates: 最大候选数。

        Returns:
            PromotionCandidate 列表。
        """
        candidates: list[PromotionCandidate] = []
        max_candidates = _bounded_int(max_candidates, 50)
        event_edges = getattr(self.kb, "event_edges", {})
        edges = event_edges.values() if isinstance(event_edges, Mapping) else _items(event_edges)
        for edge in edges:
            candidate = self._build_candidate_for_edge(edge)
            if candidate:
                candidates.append(candidate)

        candidates.sort(key=lambda c: c.promotion_score, reverse=True)
        return candidates[:max_candidates]

    def _build_candidate_for_edge(
        self,
        edge: EventEdge,
    ) -> PromotionCandidate | None:
        """为单个 EventEdge 构建晋升候选。"""
        confidence = _score(_field(edge, "confidence"))
        source_count = _bounded_int(_field(edge, "source_count"), 0)
        if confidence < self.min_edge_confidence:
            return None
        if source_count < self.min_source_count:
            return None
        if _text(_field(edge, "conflict_status")) in ("conflicted", "needs_review"):
            return None

        # 将事件类型映射为稳定图关系类型（通过 ontology registry）
        try:
            proposed_relation = self.ontology_registry.event_type_to_relation(_text(_field(edge, "event_type")))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if not proposed_relation:
            return None

        # 检查实体位置合法性
        subject = normalize_name(_text(_field(edge, "subject")))
        object_entity = normalize_name(_text(_field(edge, "object")))
        try:
            position_valid = self.entity_resolver.is_company_position_valid(
                subject, proposed_relation, object_entity
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            position_valid = False
        if not position_valid:
            return None

        # 聚合证据摘要
        evidence_summary = "; ".join(
            _text(_field(self.kb.event_claims.get(cid), "evidence"))[:120]
            for cid in _items(_field(edge, "supporting_claim_ids"))[:3]
            if isinstance(getattr(self.kb, "event_claims", {}), Mapping) and cid in self.kb.event_claims
        )

        event_type = _text(_field(edge, "event_type"))
        promotion_score = confidence * min(1.0, source_count / 3.0)
        candidate_id = stable_id(
            f"promo|{subject}|{event_type}|{object_entity}"
        )
        source_weights = _field(edge, "source_weights", {})
        source_types = sorted(_text(source) for source in source_weights if _text(source)) if isinstance(source_weights, Mapping) else []
        return PromotionCandidate(
            candidate_id=candidate_id,
            event_edge_key=(subject, event_type, object_entity),
            proposed_head=subject,
            proposed_relation=proposed_relation,
            proposed_tail=object_entity,
            evidence_summary=evidence_summary,
            source_types=source_types,
            source_count=source_count,
            event_claim_ids=[_text(cid) for cid in _items(_field(edge, "supporting_claim_ids")) if _text(cid)],
            promotion_score=promotion_score,
            status="pending_review",
            reason=(
                f"event_edge confidence={confidence:.2f}, "
                f"sources={source_count}, "
                f"claims={_bounded_int(_field(edge, 'claim_count'), 0)}"
            ),
        )
