"""Knowledge 域事实结果记录，独立于 Alpha OutcomeTracker。

fact_label 只接受事实确认标签（confirmed/denied/expired/conflicted），
不接收任何股价收益字段。已 resolve 的记录再修改产生新审计行，不静默覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .contracts import Serializable, _as_mapping, _safe_float, _safe_int, _safe_text, _text_list, new_id, utc_now


class FactLabel(str, Enum):
    """事实结果标签强枚举。"""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"


#: 合法状态转换：pending → confirmed/denied/expired/conflicted
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    FactLabel.PENDING.value: {
        FactLabel.CONFIRMED.value,
        FactLabel.DENIED.value,
        FactLabel.EXPIRED.value,
        FactLabel.CONFLICTED.value,
    },
}

#: 终态集合：已 resolve 的标签不能再转换
TERMINAL_LABELS: frozenset[str] = frozenset({
    FactLabel.CONFIRMED.value,
    FactLabel.DENIED.value,
    FactLabel.EXPIRED.value,
    FactLabel.CONFLICTED.value,
})


@dataclass
class FactOutcomeRecord(Serializable):
    """Knowledge 域事实结果记录。

    独立于 Alpha OutcomeTracker，不包含任何交易收益字段。
    transition_id 唯一约束保证幂等；已 resolve 的记录再修改产生新审计行。
    """

    record_id: str = field(default_factory=lambda: new_id("fact"))
    transition_id: str = ""
    claim_id: str = ""
    candidate_id: str = ""
    promotion_type: str = ""          # "claim" | "candidate_node" | "candidate_edge"
    from_status: str = ""
    to_status: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    evidence_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_score: float = 0.0
    confidence: float = 0.0
    independent_sources: int = 0
    policy_version: str = ""
    promoted_at: str = ""
    as_of: str = ""
    # resolve 阶段
    fact_label: str = FactLabel.PENDING.value
    resolved_at: str = ""
    resolved_by: str = ""
    resolution_evidence_ids: list[str] = field(default_factory=list)
    resolution_published_at: str = ""
    resolution_retrieved_at: str = ""
    # 审计链
    previous_record_id: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.record_id = _safe_text(self.record_id) or new_id("fact")
        for name in (
            "transition_id", "claim_id", "candidate_id", "promotion_type", "from_status",
            "to_status", "policy_version", "promoted_at", "as_of",
            "resolved_at", "resolved_by", "resolution_published_at",
            "resolution_retrieved_at", "previous_record_id",
        ):
            setattr(self, name, _safe_text(getattr(self, name, None)))
        self.fact_label = _safe_text(self.fact_label, FactLabel.PENDING.value)
        self.evidence_ids = _text_list(self.evidence_ids)
        self.source_types = _text_list(self.source_types)
        self.resolution_evidence_ids = _text_list(self.resolution_evidence_ids)
        self.evidence_snapshot = _as_mapping(self.evidence_snapshot)
        self.decision_score = _safe_float(self.decision_score)
        self.confidence = _safe_float(self.confidence)
        self.independent_sources = _safe_int(self.independent_sources, 0, minimum=0)


class FactOutcomeStore:
    """事实结果的持久化与 resolve 逻辑。

    通过 ResearchRepository 持久化，transition_id 唯一约束保证幂等。
    resolve 时自动生成 Knowledge 域反馈事件，驱动校准闭环。
    """

    def __init__(self, repository: Any, feedback_controller: Any | None = None) -> None:
        """初始化。

        Args:
            repository: ResearchRepository 实例。
            feedback_controller: 可选的 FeedbackController，resolve 时自动记录 Knowledge 反馈。
        """
        self.repository = repository
        self.feedback_controller = feedback_controller

    def record_promotion(
        self,
        *,
        transition_id: str,
        claim_id: str,
        candidate_id: str = "",
        promotion_type: str = "claim",
        from_status: str = "",
        to_status: str = "",
        evidence_ids: list[str] | None = None,
        source_types: list[str] | None = None,
        evidence_snapshot: dict[str, Any] | None = None,
        decision_score: float = 0.0,
        confidence: float = 0.0,
        independent_sources: int = 0,
        policy_version: str = "",
        promoted_at: str = "",
        as_of: str = "",
    ) -> FactOutcomeRecord:
        """记录晋升快照；同一 transition_id 幂等。

        Args:
            transition_id: 关联 StateTransition.transition_id，唯一约束。
            claim_id: 关联 Claim ID。
            candidate_id: 候选边晋升时关联的 candidate_id。
            promotion_type: "claim" / "candidate_node" / "candidate_edge"。
            from_status: 晋升前状态。
            to_status: 晋升后状态。
            evidence_ids: 参与决策的证据 ID 列表。
            source_types: 证据来源类型列表。
            evidence_snapshot: 后验、决策分数等快照。
            decision_score: SufficiencyResult.decision_score。
            confidence: SufficiencyResult.confidence。
            independent_sources: SufficiencyResult.independent_sources。
            policy_version: 策略版本字符串。
            promoted_at: 晋升时间戳。
            as_of: PIT 时点。

        Returns:
            FactOutcomeRecord 实例。
        """
        transition_id = _safe_text(transition_id)
        claim_id = _safe_text(claim_id)
        if not transition_id or not claim_id:
            raise ValueError("transition_id and claim_id are required")
        existing = self.repository.get_fact_outcome_by_transition(transition_id)
        if existing is not None:
            return existing
        record = FactOutcomeRecord(
            transition_id=transition_id,
            claim_id=claim_id,
            candidate_id=candidate_id,
            promotion_type=promotion_type,
            from_status=from_status,
            to_status=to_status,
            evidence_ids=_text_list(evidence_ids),
            source_types=_text_list(source_types),
            evidence_snapshot=_as_mapping(evidence_snapshot),
            decision_score=decision_score,
            confidence=confidence,
            independent_sources=independent_sources,
            policy_version=policy_version,
            promoted_at=_safe_text(promoted_at) or utc_now(),
            as_of=as_of,
        )
        self.repository.save_fact_outcome(record)
        return record

    def resolve_outcome(
        self,
        record_id: str,
        fact_label: str,
        *,
        resolved_by: str = "",
        resolution_evidence_ids: list[str] | None = None,
        resolution_published_at: str = "",
        resolution_retrieved_at: str = "",
    ) -> FactOutcomeRecord:
        """resolve 事实结果。

        pending → confirmed/denied/expired/conflicted。
        已 resolve 的记录再修改产生新审计行，不静默覆盖。

        Args:
            record_id: 待 resolve 的记录 ID。
            fact_label: 目标标签，必须是合法终态。
            resolved_by: resolve 来源（announcement/manual_review/fact_update）。
            resolution_evidence_ids: resolve 依据的证据 ID。
            resolution_published_at: resolve 证据发布时间。
            resolution_retrieved_at: resolve 证据检索时间。

        Returns:
            resolve 后的 FactOutcomeRecord。

        Raises:
            ValueError: fact_label 非法或状态转换不合法。
            KeyError: record_id 不存在。
        """
        fact_label = _safe_text(fact_label)
        record_id = _safe_text(record_id)
        if fact_label not in TERMINAL_LABELS:
            raise ValueError(f"非法 fact_label: {fact_label}，必须是 {TERMINAL_LABELS}")
        record = self.repository.get_fact_outcome(record_id)
        if record is None:
            raise KeyError(record_id)
        if record.fact_label not in {FactLabel.PENDING.value, *TERMINAL_LABELS}:
            raise ValueError(f"当前 fact_label 非法: {record.fact_label}")

        if record.fact_label == FactLabel.PENDING.value:
            record.fact_label = fact_label
            record.resolved_by = _safe_text(resolved_by)
            record.resolution_evidence_ids = _text_list(resolution_evidence_ids)
            record.resolution_published_at = _safe_text(resolution_published_at)
            record.resolution_retrieved_at = _safe_text(resolution_retrieved_at)
            record.resolved_at = utc_now()
            self.repository.save_fact_outcome(record)
            self._emit_knowledge_feedback(record)
            return record

        new_record = FactOutcomeRecord(
            record_id=new_id("fact"),
            transition_id=record.transition_id,
            claim_id=record.claim_id,
            candidate_id=record.candidate_id,
            promotion_type=record.promotion_type,
            from_status=record.from_status,
            to_status=record.to_status,
            evidence_ids=list(record.evidence_ids),
            source_types=list(record.source_types),
            evidence_snapshot=dict(record.evidence_snapshot),
            decision_score=record.decision_score,
            confidence=record.confidence,
            independent_sources=record.independent_sources,
            policy_version=record.policy_version,
            promoted_at=record.promoted_at,
            as_of=record.as_of,
            fact_label=fact_label,
            resolved_by=_safe_text(resolved_by),
            resolution_evidence_ids=_text_list(resolution_evidence_ids),
            resolution_published_at=_safe_text(resolution_published_at),
            resolution_retrieved_at=_safe_text(resolution_retrieved_at),
            resolved_at=utc_now(),
            previous_record_id=record.record_id,
        )
        self.repository.save_fact_outcome(new_record)
        self._emit_knowledge_feedback(new_record)
        return new_record

    def _emit_knowledge_feedback(self, record: FactOutcomeRecord) -> None:
        """resolve 后自动生成 Knowledge 域反馈事件。

        事实标签映射为 fact_label 数值：
        - confirmed → 1（事实确认，来源权重应提升）
        - denied → 0（事实否定，来源权重应降低）
        - expired → 0.5（过期，中性偏弱）
        - conflicted → 0.5（冲突，中性偏弱）

        Args:
            record: 已 resolve 的 FactOutcomeRecord。
        """
        if self.feedback_controller is None:
            return
        label_to_value = {
            FactLabel.CONFIRMED.value: 1,
            FactLabel.DENIED.value: 0,
            FactLabel.EXPIRED.value: 0.5,
            FactLabel.CONFLICTED.value: 0.5,
        }
        fact_value = label_to_value.get(record.fact_label)
        if fact_value is None:
            return
        try:
            from .contracts import FeedbackDomain, FeedbackEvent
            # 每条来源类型生成一条反馈，用于校准对应来源权重
            for source_type in (_text_list(record.source_types) or ["unknown"]):
                event = FeedbackEvent(
                    domain=FeedbackDomain.KNOWLEDGE.value,
                    event_type="fact_resolution",
                    event_kind=record.promotion_type or "claim",
                    target_id=record.claim_id,
                    outcome=record.fact_label,
                    metrics={
                        "fact_label": fact_value,
                        "decision_score": record.decision_score,
                        "confidence": record.confidence,
                        "independent_sources": record.independent_sources,
                        "policy_version": record.policy_version,
                    },
                    attribution={
                        "source_type": source_type,
                        "candidate_id": record.candidate_id,
                        "transition_id": record.transition_id,
                        "resolved_by": record.resolved_by,
                    },
                )
                self.feedback_controller.record(event, generate_task=False)
        except Exception:
            # 反馈记录失败不应影响 resolve 主流程
            pass

    def list_pending(self, limit: int = 100) -> list[FactOutcomeRecord]:
        """列出待 resolve 的记录。"""
        limit = max(1, _safe_int(limit, 100))
        return self.repository.list_fact_outcomes(
            fact_label=FactLabel.PENDING.value, limit=limit
        )

    def list_resolved(self, limit: int = 100) -> list[FactOutcomeRecord]:
        """列出已 resolve 的记录。"""
        limit = max(1, _safe_int(limit, 100))
        records: list[FactOutcomeRecord] = []
        for label in sorted(TERMINAL_LABELS):
            records.extend(
                self.repository.list_fact_outcomes(fact_label=label, limit=limit)
            )
        return records[:limit]
