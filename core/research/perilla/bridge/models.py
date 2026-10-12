"""Shared bridge data models and utilities.

This module holds the cross-cutting dataclasses and small helper functions used
by both ``PromotionCandidateBuilder`` and ``AlphaGapFeedbackEmitter`` so that
those modules can live in dedicated files without duplicating code.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PerillaSearchTask:
    """由 Alpha 证据缺口生成的 Perilla 可执行搜索任务。"""

    task_id: str
    target_entity: str
    missing_relation: str
    query_keywords: list[str]
    preferred_sources: list[str]
    priority: float
    reason: str
    origin_path: list[str] = field(default_factory=list)
    origin_gap: dict[str, Any] = field(default_factory=dict)
    capability_hint: str = "semantic_search"
    status: str = "pending"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "task_id": self.task_id,
            "target_entity": self.target_entity,
            "missing_relation": self.missing_relation,
            "query_keywords": self.query_keywords,
            "preferred_sources": self.preferred_sources,
            "priority": self.priority,
            "reason": self.reason,
            "origin_path": self.origin_path,
            "origin_gap": self.origin_gap,
            "capability_hint": self.capability_hint,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class EventClaimDraft:
    """Perilla 信号标准化后的中间结构，尚未写入 SAG。"""

    draft_id: str
    event_type: str
    raw_event_type: str | None
    subject: str
    object: str
    event_time: str
    source_time: str
    evidence: str
    source_type: str
    source_title: str
    source_url: str
    confidence: float
    source_id: str
    linked_node_ids: list[str]
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "draft_id": self.draft_id,
            "event_type": self.event_type,
            "raw_event_type": self.raw_event_type,
            "subject": self.subject,
            "object": self.object,
            "event_time": self.event_time,
            "source_time": self.source_time,
            "evidence": self.evidence,
            "source_type": self.source_type,
            "source_title": self.source_title,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "source_id": self.source_id,
            "linked_node_ids": self.linked_node_ids,
            "raw_payload": self.raw_payload,
        }


def now_iso() -> str:
    """Return current time as ISO string."""
    return datetime.now().isoformat()


def stable_id(text: str) -> str:
    """Return a stable short id for the given text."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def normalize_name(name: str) -> str:
    """归一化名称：去空格、去后缀。"""
    if name is None:
        return ""
    name = str(name).strip()
    name = name.replace(" ", "").replace("　", "")
    name = name.rstrip("。、；，")
    return name
