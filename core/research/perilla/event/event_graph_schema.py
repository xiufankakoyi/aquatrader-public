"""Event graph schema for SAG.

事件图是 SAG 中独立领域模型，承载从 Perilla Discovery、外部信号、
Alpha 证据缺口反馈等渠道进入的动态事件。事件图不直接复用稳定图
Claim/Edge 的状态机，晋升稳定图必须经过显式 PromotionGate 判断。

事件相关枚举定义在 shared.ontology.ontology_models 中，本模块保留
re-export 以维持既有导入路径的兼容性。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from collections.abc import Mapping
from typing import Any

from core.research.perilla.shared.ontology.ontology_models import (
    EVENT_CLAIM_STATUSES,
    EVENT_TYPES,
    EventClaimStatus,
    EventType,
)

# Re-export ontology enums so existing ``from .event_graph_schema import X`` keep working.
__all__ = [
    "EventType",
    "EVENT_TYPES",
    "EventClaimStatus",
    "EVENT_CLAIM_STATUSES",
    "EventClaim",
    "EventEdge",
    "PromotionCandidate",
]


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bounded_int(value: Any, default: int = 0, maximum: int = 1_000_000_000) -> int:
    try:
        number = int(float(value))
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(0, min(maximum, number))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _edge_keys(value: Any) -> list[tuple[str, str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [tuple(str(item) for item in key) for key in value
            if isinstance(key, (list, tuple)) and len(key) == 3]


@dataclass
class EventClaim:
    """事件 Claim，描述一次可溯源、有时效的事件声明。

    Attributes:
        event_id: 事件唯一标识。
        event_type: 标准化事件类型，必须在 EVENT_TYPES 中。
        raw_event_type: 原始事件类型字符串（如 Perilla 信号类型）。
        subject: 事件主体。
        object: 事件对象（可选，部分事件如概念信号可为空）。
        time: 事件发生时间（任意字符串，如 2026Q2 或 ISO 时间）。
        source_time: 来源发布时间。
        evidence: 证据文本片段。
        source_type: 来源类型。
        source_title: 来源标题。
        source_url: 来源 URL（可选）。
        confidence: 事件置信度 [0, 1]。
        score: 事件得分（兼容旧字段，默认等于 confidence）。
        status: 事件状态。
        source_id: 来源文档唯一标识。
        linked_node_ids: 关联的稳定图节点 ID 列表。
        linked_edge_keys: 关联的稳定图边键 (subject, relation, object) 列表。
        raw_payload: 原始负载，保留 Perilla 信号等原始结构。
        status_history: 状态变更历史。
        freshness_score: 新鲜度得分 [0, 1]。
        decay_score: 衰减得分 [0, 1]，通常为 1 - freshness_score。
        promotion_candidate: 是否可作为晋升稳定图候选。
        contradiction_candidate: 是否卷入潜在冲突。
        metadata: 扩展元数据。
    """

    event_id: str
    event_type: str
    raw_event_type: str | None = None
    subject: str = ""
    object: str = ""
    time: str = ""
    source_time: str = ""
    evidence: str = ""
    source_type: str = ""
    source_title: str = ""
    source_url: str = ""
    confidence: float = 0.0
    score: float = 0.0
    status: str = EventClaimStatus.ACTIVE.value
    source_id: str = ""
    linked_node_ids: list[str] = field(default_factory=list)
    linked_edge_keys: list[tuple[str, str, str]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    status_history: list[dict[str, Any]] = field(default_factory=list)
    freshness_score: float = 0.0
    decay_score: float = 0.0
    promotion_candidate: bool = False
    contradiction_candidate: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventClaim":
        """从 dict 反序列化，兼容缺失字段。"""
        if not isinstance(data, Mapping):
            raise TypeError("event claim payload must be a mapping")
        raw_status = data.get("status")
        status = raw_status.strip() if isinstance(raw_status, str) else EventClaimStatus.ACTIVE.value
        if status not in EVENT_CLAIM_STATUSES:
            status = EventClaimStatus.ACTIVE.value
        return cls(
            event_id=_text(data["event_id"]),
            event_type=_text(data["event_type"]),
            raw_event_type=_text(data.get("raw_event_type")) or None,
            subject=_text(data.get("subject")),
            object=_text(data.get("object")),
            time=_text(data.get("time")),
            source_time=_text(data.get("source_time", data.get("time", ""))),
            evidence=_text(data.get("evidence")),
            source_type=_text(data.get("source_type")),
            source_title=_text(data.get("source_title")),
            source_url=_text(data.get("source_url")),
            confidence=_finite_float(data.get("confidence")),
            score=_finite_float(data.get("score", data.get("confidence"))),
            status=status,
            source_id=_text(data.get("source_id")),
            linked_node_ids=_string_list(data.get("linked_node_ids", [])),
            linked_edge_keys=_edge_keys(data.get("linked_edge_keys", [])),
            raw_payload=_mapping(data.get("raw_payload", {})),
            status_history=list(data.get("status_history", [])) if isinstance(data.get("status_history", []), (list, tuple)) else [],
            freshness_score=_finite_float(data.get("freshness_score")),
            decay_score=_finite_float(data.get("decay_score")),
            promotion_candidate=bool(data.get("promotion_candidate", False)),
            contradiction_candidate=bool(data.get("contradiction_candidate", False)),
            metadata=_mapping(data.get("metadata", {})),
        )


@dataclass
class EventEdge:
    """事件边，聚合同一 subject + event_type + object 下的多个 EventClaim。"""

    subject: str
    event_type: str
    object: str
    confidence: float = 0.0
    claim_ids: list[str] = field(default_factory=list)
    source_weights: dict[str, float] = field(default_factory=dict)
    supporting_claim_ids: list[str] = field(default_factory=list)
    contradicting_claim_ids: list[str] = field(default_factory=list)
    deprecated_claim_ids: list[str] = field(default_factory=list)
    confidence_reason: str = ""
    conflict_status: str = "none"
    status: str = "active"
    first_seen: str = ""
    last_updated: str = ""
    latest_event_time: str = ""
    source_count: int = 0
    claim_count: int = 0
    linked_node_ids: list[str] = field(default_factory=list)
    linked_stable_edge_keys: list[tuple[str, str, str]] = field(default_factory=list)
    aggregate_freshness_score: float = 0.0
    aggregate_event_impact_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventEdge":
        """从 dict 反序列化，兼容缺失字段。"""
        if not isinstance(data, Mapping):
            raise TypeError("event edge payload must be a mapping")
        return cls(
            subject=_text(data["subject"]),
            event_type=_text(data["event_type"]),
            object=_text(data["object"]),
            confidence=_finite_float(data.get("confidence")),
            claim_ids=_string_list(data.get("claim_ids", [])),
            source_weights={
                str(key): _finite_float(value)
                for key, value in _mapping(data.get("source_weights", {})).items()
            },
            supporting_claim_ids=_string_list(data.get("supporting_claim_ids", [])),
            contradicting_claim_ids=_string_list(data.get("contradicting_claim_ids", [])),
            deprecated_claim_ids=_string_list(data.get("deprecated_claim_ids", [])),
            confidence_reason=_text(data.get("confidence_reason")),
            conflict_status=_text(data.get("conflict_status"), "none"),
            status=(data.get("status").strip() if isinstance(data.get("status"), str) else "active"),
            first_seen=_text(data.get("first_seen")),
            last_updated=_text(data.get("last_updated")),
            latest_event_time=_text(data.get("latest_event_time")),
            source_count=_bounded_int(data.get("source_count")),
            claim_count=_bounded_int(data.get("claim_count")),
            linked_node_ids=_string_list(data.get("linked_node_ids", [])),
            linked_stable_edge_keys=_edge_keys(data.get("linked_stable_edge_keys", [])),
            aggregate_freshness_score=_finite_float(data.get("aggregate_freshness_score")),
            aggregate_event_impact_score=_finite_float(data.get("aggregate_event_impact_score")),
            metadata=_mapping(data.get("metadata", {})),
        )


@dataclass
class PromotionCandidate:
    """从 EventClaim/EventEdge 生成的稳定图晋升候选。"""

    candidate_id: str
    event_edge_key: tuple[str, str, str]
    proposed_head: str
    proposed_relation: str
    proposed_tail: str
    evidence_summary: str
    source_types: list[str]
    source_count: int
    event_claim_ids: list[str]
    promotion_score: float
    status: str = "pending_review"
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "candidate_id": self.candidate_id,
            "event_edge_key": list(self.event_edge_key),
            "proposed_head": self.proposed_head,
            "proposed_relation": self.proposed_relation,
            "proposed_tail": self.proposed_tail,
            "evidence_summary": self.evidence_summary,
            "source_types": self.source_types,
            "source_count": self.source_count,
            "event_claim_ids": self.event_claim_ids,
            "promotion_score": self.promotion_score,
            "status": self.status,
            "reason": self.reason,
            "metadata": self.metadata,
        }
