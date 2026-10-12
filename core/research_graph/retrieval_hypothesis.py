"""检索假设契约。历史 pseudo_evidence 仅作为只读输入。

硬性规则（必须全局遵守）：
- RetrievalHypothesis 只能用于构造查询原型，不能写入 evidence_slots
- 不能增加 posterior、不能触发状态升级
- 任何 evidence_kind 为 retrieval_hypothesis / query_prototype / generated / pseudo_evidence
  的记录都必须在进入 EvidenceSlot、posterior、状态机前被拦截
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.research_graph.models import utc_now


# 被禁止进入证据系统的生成式假设类型
BLOCKED_EVIDENCE_KINDS: frozenset[str] = frozenset({
    "pseudo_evidence",
    "retrieval_hypothesis",
    "query_prototype",
    "generated",
})


@dataclass
class RetrievalHypothesis:
    hypothesis_id: str
    candidate_id: str
    query_prototype: str
    reason: str = ""
    generated_by: str = "rule"
    prompt_version: str = ""
    created_at: str = field(default_factory=utc_now)
    evidence_kind: str = "retrieval_hypothesis"
    eligible_for_evidence_slot: bool = False
    eligible_for_posterior: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def to_slot_safe_dict(self) -> dict[str, Any]:
        """返回可存入 attributes 的字典，自带阻断标记。"""
        return {
            **self.to_dict(),
            "eligible_for_evidence_slot": False,
            "eligible_for_posterior": False,
        }


def is_blocked_evidence_kind(evidence_kind: str | None) -> bool:
    """判断某 evidence_kind 是否被禁止进入证据槽/后验/状态升级。"""
    return str(evidence_kind or "").lower() in BLOCKED_EVIDENCE_KINDS


def ensure_blocked_flags(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """确保 attributes 中的检索假设都带有阻断标记。"""
    attrs = dict(attributes or {})
    hypotheses = attrs.get("retrieval_hypotheses")
    if isinstance(hypotheses, list):
        sanitized: list[Any] = []
        for item in hypotheses:
            if isinstance(item, dict):
                item = {**item, "eligible_for_evidence_slot": False, "eligible_for_posterior": False}
            sanitized.append(item)
        attrs["retrieval_hypotheses"] = sanitized
    return attrs


def read_hypotheses(attributes: dict[str, Any] | None) -> list[str]:
    """优先读新字段，旧 pseudo_evidence 仅兼容为查询文本。"""
    attrs = attributes or {}
    current = attrs.get("retrieval_hypotheses")
    if isinstance(current, list):
        return [str(item.get("query_prototype", "") if isinstance(item, dict) else item) for item in current if item]
    legacy = attrs.get("pseudo_evidence")
    return [str(item) for item in legacy] if isinstance(legacy, list) else []

