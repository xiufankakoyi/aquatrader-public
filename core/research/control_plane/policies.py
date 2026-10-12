"""Claim 充分性、状态迁移和 point-in-time 策略。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import (
    Claim, ClaimStatus, EvidenceRecord, EvidenceStance, _as_mapping, _safe_bool,
    _safe_float, _safe_text, _text_list,
)


# API 路径合法状态（严格相邻迁移）
_API_PATH: list[str] = ["pending", "supported", "active", "trusted"]

# Orchestrator 路径合法状态（允许前向跳跃）
_ORCH_PATH: list[str] = ["discovered", "candidate", "supported", "active", "trusted"]

# 终态：可从任意非终态迁入
_TERMINAL_STATES: set[str] = {"rejected", "conflicted", "dormant", "pruned"}

# 新建 Claim 允许的初始状态
_INITIAL_STATES: set[str] = {"pending", "discovered"}


def validate_state_transition(old_status: str, new_status: str) -> None:
    """校验 Claim research_status 迁移合法性，非法迁移抛出 ValueError。

    合法路径（API 严格相邻）：pending → supported → active → trusted
    兼容路径（Orchestrator 前向跳跃）：discovered → candidate → supported → active → trusted
    终态（任意非终态可迁入）：rejected / conflicted / dormant / pruned

    Args:
        old_status: 迁移前状态；空串表示新建 Claim。
        new_status: 迁移后状态。

    Raises:
        ValueError: 当迁移路径非法时，message 为 "INVALID_STATE_TRANSITION"。
    """
    old = _safe_text(old_status).lower()
    new = _safe_text(new_status).lower()

    # 无变化始终允许
    if old == new:
        return

    # 新建 Claim：只允许初始状态
    if old == "":
        if new in _INITIAL_STATES:
            return
        raise ValueError("INVALID_STATE_TRANSITION")

    # 终态：任意非终态均可迁入
    if new in _TERMINAL_STATES:
        return

    # API 路径：仅允许相邻前向迁移
    if old in _API_PATH and new in _API_PATH:
        if _API_PATH.index(new) == _API_PATH.index(old) + 1:
            return
        raise ValueError("INVALID_STATE_TRANSITION")

    # Orchestrator 路径：允许前向跳跃
    if old in _ORCH_PATH and new in _ORCH_PATH:
        if _ORCH_PATH.index(new) > _ORCH_PATH.index(old):
            return
        raise ValueError("INVALID_STATE_TRANSITION")

    # 跨路径等价迁移：pending <-> discovered
    if {old, new} == {"pending", "discovered"}:
        return

    # 受审计的恢复路径：冲突消解或过期证据补齐后回到候选层，
    # 后续仍需按正常前向路径重新升级，不能直接恢复 TRUSTED。
    if old in {"conflicted", "dormant"} and new in {"candidate", "supported", "active"}:
        return

    raise ValueError("INVALID_STATE_TRANSITION")


def parse_time(value: str) -> datetime | None:
    """安全解析 ISO 时间字符串；空值或格式错误时返回 None，不抛异常。"""
    value = _safe_text(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def format_pit_or_unknown(value: str) -> str:
    """将 point-in-time 字段格式化为展示字符串；缺失或无效时返回 'Unknown'。"""
    if not value:
        return "Unknown"
    if parse_time(value) is None:
        return "Unknown"
    return value


def evidence_visible_as_of(evidence: EvidenceRecord, as_of: str, *, historical: bool = True) -> bool:
    """历史模式缺少可见时间时保守排除，实时模式允许但由 UI 标记未知。"""
    if not isinstance(evidence, EvidenceRecord):
        return False
    cutoff = parse_time(as_of)
    if cutoff is None:
        return not historical
    published = parse_time(evidence.published_at)
    retrieved = parse_time(evidence.retrieved_at)
    visible_at = retrieved or published
    if visible_at is None:
        return not historical
    return visible_at <= cutoff and (published is None or published <= cutoff)


def evidence_is_current(evidence: EvidenceRecord, as_of: str = "") -> bool:
    if not isinstance(evidence, EvidenceRecord):
        return False
    if evidence.stance in {EvidenceStance.STALE.value, EvidenceStance.RETRACTED.value}:
        return False
    cutoff = parse_time(as_of) if as_of else datetime.now(timezone.utc)
    if as_of and cutoff is None:
        return False
    valid_from = parse_time(evidence.valid_from)
    valid_to = parse_time(evidence.valid_to)
    if cutoff is None:
        return True
    return not ((valid_from and cutoff < valid_from) or (valid_to and cutoff > valid_to))


@dataclass
class SufficiencyResult:
    sufficient: bool
    target_status: str
    independent_sources: int
    first_party_sources: int
    support_count: int
    counter_count: int
    slots_complete: bool
    expired_count: int = 0
    reasons: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    marginal_value: float = 0.0  # 继续检索的边际价值估计 [0,1]
    decision_score: float = 0.0  # 用于状态迁移审计的统一分数


class ClaimPolicy:
    """唯一 Claim 状态策略，不使用文档数量作为达标条件。"""

    VERSION = "research-policy-v1"

    @classmethod
    def apply_policy(cls, policy: Any | None = None) -> "ClaimPolicy":
        """从 PolicyVersion 构造 ClaimPolicy，policy 为 None 时使用默认参数。

        Args:
            policy: PolicyVersion 或 None。当前 ClaimPolicy 无可调参数，
                保留此方法以便未来注入检索域参数（如 stop_min_independent_sources）。

        Returns:
            ClaimPolicy 实例。
        """
        return cls()

    @staticmethod
    def _independent(evidence: list[EvidenceRecord]) -> list[EvidenceRecord]:
        strongest: dict[str, EvidenceRecord] = {}
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, EvidenceRecord):
                continue
            key = (
                _safe_text(item.lineage_id)
                or _safe_text(item.correlation_group)
                or _safe_text(item.source_trace_id)
                or _safe_text(item.evidence_id)
            )
            current = strongest.get(key)
            weight = _safe_float(item.source_quality) * _safe_float(item.match_score)
            current_weight = (
                _safe_float(current.source_quality) * _safe_float(current.match_score)
                if current is not None else 0.0
            )
            if current is None or weight > current_weight:
                strongest[key] = item
        return list(strongest.values())

    def evaluate(
        self,
        claim: Claim,
        evidence: list[EvidenceRecord],
        *,
        as_of: str = "",
        historical: bool = False,
        tier_cost: float = 0.5,
        node_importance: float = 0.5,
    ) -> SufficiencyResult:
        """评估 Claim 充分性。

        Args:
            claim: 待评估 Claim。
            evidence: 证据记录列表。
            as_of: 历史时点；为空则使用当前时间。
            historical: 是否启用历史模式（缺少 PIT 时排除）。
            tier_cost: 下一质量层检索成本 [0,1]，越高越应停止。
            node_importance: 节点重要程度 [0,1]，越高越值得继续检索。
        """
        if not isinstance(claim, Claim):
            return SufficiencyResult(False, ClaimStatus.DISCOVERED.value, 0, 0, 0, 0, False, reasons=["claim 无效"], reason_codes=["invalid_claim"])
        evidence = list(evidence) if isinstance(evidence, (list, tuple, set)) else []
        for item in evidence:
            if isinstance(item, EvidenceRecord):
                item.metadata = _as_mapping(item.metadata)
        safe_tier_cost = max(0.0, min(1.0, _safe_float(tier_cost, 0.5)))
        safe_node_importance = max(0.0, min(1.0, _safe_float(node_importance, 0.5)))
        eligible = [
            item for item in evidence
            if isinstance(item, EvidenceRecord)
            if item.can_affect_confidence
            and evidence_is_current(item, as_of)
            and (not as_of or evidence_visible_as_of(item, as_of, historical=historical))
        ]
        expired = [
            item for item in evidence
            if isinstance(item, EvidenceRecord)
            and item.can_affect_confidence
            and not evidence_is_current(item, as_of)
        ]
        independent = self._independent(eligible)
        support = [item for item in independent if item.stance == EvidenceStance.SUPPORT.value]
        counter = [item for item in independent if item.stance == EvidenceStance.COUNTER.value]
        first_party = [item for item in support if _safe_bool(item.is_first_party)]
        covered = set()
        for item in support:
            covered.update(_text_list(item.metadata.get("slots")))
        required_slots = _text_list(claim.required_slots)
        slots_complete = set(required_slots).issubset(covered) if required_slots else bool(support)
        conflict = bool(counter) and bool(support)
        reasons: list[str] = []
        reason_codes: list[str] = []

        prune_reason = _safe_text(_as_mapping(claim.attributes).get("prune_reason"))
        if prune_reason in {"generic_node", "duplicate_node", "low_value_edge"}:
            target = ClaimStatus.PRUNED.value
            reasons.append(f"结构剪枝:{prune_reason}")
            reason_codes.append("pruned")
        elif any(
            item.can_affect_confidence
            or item.stance in {EvidenceStance.STALE.value, EvidenceStance.RETRACTED.value}
            or _safe_bool(item.metadata.get("invalidated"))
            for item in evidence
            if isinstance(item, EvidenceRecord)
        ) and not eligible:
            target = ClaimStatus.DORMANT.value
            reasons.append("现有证据均已过期、撤回或在 as_of 时点不可见")
            reason_codes.append("expired_or_invisible")
        elif not support:
            target = ClaimStatus.REJECTED.value if len(counter) >= 2 else ClaimStatus.DISCOVERED.value
            reasons.append("没有独立支持证据")
            reason_codes.append("no_support")
        elif conflict:
            target = ClaimStatus.CONFLICTED.value
            reasons.append("支持与反证并存，需复核")
            reason_codes.append("unresolved_conflict")
        elif not slots_complete:
            target = ClaimStatus.CANDIDATE.value
            reasons.append("必填证据槽不完整")
            reason_codes.append("incomplete_slots")
        elif len(support) < 2 and not first_party:
            target = ClaimStatus.CANDIDATE.value
            reasons.append("独立来源不足且无第一方来源")
            reason_codes.append("insufficient_sources")
        elif sum(
            max(0.0, min(1.0, item.source_quality))
            * max(0.0, min(1.0, item.match_score))
            for item in support
        ) < 1.2 and not first_party:
            target = ClaimStatus.CANDIDATE.value
            reasons.append("独立来源质量权重不足且无第一方来源")
            reason_codes.append("insufficient_source_quality")
        elif not (
            _safe_bool(claim.counter_evidence_evaluated)
            or _safe_bool(claim.counter_search_completed)
        ):
            target = ClaimStatus.SUPPORTED.value
            reasons.append("尚未完成反证搜索与评估")
            reason_codes.append("counter_search_pending")
        elif _safe_bool(claim.high_impact) and not first_party:
            target = ClaimStatus.SUPPORTED.value
            reasons.append("高影响关系缺少第一方来源")
            reason_codes.append("high_impact_missing_first_party")
        else:
            target = ClaimStatus.ACTIVE.value
            reason_codes.append("sufficient")

        weights = [
            max(0.0, min(1.5, _safe_float(item.source_quality)))
            * max(0.0, min(1.0, _safe_float(item.match_score)))
            for item in support
        ]
        counter_weights = [
            max(0.0, min(1.5, _safe_float(item.source_quality)))
            * max(0.0, min(1.0, _safe_float(item.match_score)))
            for item in counter
        ]
        logit = (sum(weights) - 1.5 * sum(counter_weights)) - 1.0
        confidence = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, logit))))
        # 决策分数：综合置信度与覆盖完整度
        coverage_score = 1.0 if slots_complete else 0.5
        decision_score = 0.6 * confidence + 0.25 * coverage_score + 0.15 * (1.0 if first_party else 0.0)
        # 边际价值：证据越多、来源越独立，继续搜索价值越低；
        # 节点越重要越值得继续，下一层检索成本越高越应提前停止。
        base_value = max(0.0, 1.0 - min(1.0, len(independent) / 4.0) * confidence)
        importance_multiplier = 0.5 + 0.5 * safe_node_importance
        cost_multiplier = 1.0 - 0.5 * safe_tier_cost
        marginal_value = max(0.0, min(1.0, base_value * importance_multiplier * cost_multiplier))
        return SufficiencyResult(
            sufficient=target == ClaimStatus.ACTIVE.value,
            target_status=target,
            independent_sources=len(independent),
            first_party_sources=len(first_party),
            support_count=len(support),
            counter_count=len(counter),
            slots_complete=slots_complete,
            expired_count=len(expired),
            reasons=reasons,
            reason_codes=reason_codes,
            confidence=round(confidence, 4),
            marginal_value=round(marginal_value, 4),
            decision_score=round(decision_score, 4),
        )
