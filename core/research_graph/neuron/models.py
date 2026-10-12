"""Neuron engine data models.

所有 dataclass 与现有 GraphStore/HyperNode/Evidence 并存,不修改现有模型。
时间字段统一用 ISO8601 字符串(复用 core.research_graph.models.utc_now)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core.research_graph.models import utc_now

from core.research_graph.neuron.enums import (
    SynapseDirection,
    SynapseRelation,
    SynapseStatus,
    SynapseTargetKind,
)


@dataclass
class TargetDraft:
    """TARGET_DRAFT 突触的候选目标描述。"""

    proposed_node_type: str
    proposed_name: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposed_node_type": self.proposed_node_type,
            "proposed_name": self.proposed_name,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TargetDraft":
        return cls(
            proposed_node_type=payload.get("proposed_node_type", ""),
            proposed_name=payload.get("proposed_name", ""),
            reason=payload.get("reason", ""),
            evidence_ids=list(payload.get("evidence_ids", [])),
        )


@dataclass
class NeighborRef:
    """邻接神经元摘要,只含必要字段,不传全图。"""

    neuron_id: str
    node_id: str
    node_type: str
    canonical_name: str
    verification_status: str
    metabolism_state: str
    confidence: float
    relation: str
    direction: str
    dimension: str


@dataclass
class FieldSignal:
    """单一维度视角的局部场信号。"""

    dimension: str
    pressure: float
    gap_score: float
    evidence_strength: float
    contradiction: float
    notes: str = ""


@dataclass
class ResourceBudget:
    """单个 growth_cycle 的资源预算。"""

    max_evidence_pulls: int = 20
    max_synapse_reads: int = 30
    max_pulses_emittable: int = 3
    max_mutations: int = 50
    max_neighbor_depth: int = 1
    remaining_evidence_pulls: int = 20
    remaining_synapse_reads: int = 30

    def consume_evidence_pull(self, count: int = 1) -> bool:
        """消费 evidence 查询额度,返回是否成功。"""
        if self.remaining_evidence_pulls < count:
            return False
        self.remaining_evidence_pulls -= count
        return True

    def consume_synapse_read(self, count: int = 1) -> bool:
        """消费突触读取额度,返回是否成功。"""
        if self.remaining_synapse_reads < count:
            return False
        self.remaining_synapse_reads -= count
        return True


@dataclass
class ResearchNeuron:
    """超图神经元,带内部状态、突触、证据营养、代谢、生长能力。

    生命周期由 growth_cycle 驱动(Plan 2 实现)。
    突触与脉冲只存 ID 引用,完整对象在 NeuronStateStore 独立表。
    """

    # ── 1. 身份字段 ──
    neuron_id: str
    node_id: str
    node_type: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    root_theme: str = ""
    source_node_ref: dict[str, Any] = field(default_factory=dict)
    graph_id: str = ""
    created_from: str = "manual"
    dimensions_observed: list[str] = field(default_factory=list)

    # ── 2. 验证状态 ──
    verification_status: str = "UNVERIFIED_SEED"
    attention_required: bool = False
    # 核心论断,用于论点驱动而非单纯证据堆砌
    thesis: str = ""
    evidence_strength: float = 0.0
    support_count: int = 0
    counter_count: int = 0
    neutral_count: int = 0
    source_diversity: float = 0.0
    confidence: float = 0.0
    uncertainty: float = 0.5
    contradiction_pressure: float = 0.0
    evidence_quality_mix: dict[str, int] = field(default_factory=dict)

    # ── 3. 时间与代谢 ──
    first_seen_at: str = field(default_factory=utc_now)
    last_seen_at: str = field(default_factory=utc_now)
    last_evidence_at: str = ""
    staleness: float = 0.0
    metabolism_state: str = "ACTIVE"
    nutrition_level: float = 0.0
    revival_threshold: float = 0.3
    dormancy_threshold: float = 0.2
    decay_threshold: float = 0.1

    # ── 4. 生长状态 ──
    growth_energy: float = 0.5
    novelty: float = 0.5
    bottleneck_pressure: float = 0.0
    substitution_pressure: float = 0.0
    company_mapping_pressure: float = 0.0
    synthesis_pressure: float = 0.0
    material_depth: float = 0.0
    synthesis_depth: float = 0.0
    equipment_gap: float = 0.0
    evidence_gap: float = 0.0
    policy_gap: float | None = None
    price_gap: float | None = None
    patent_gap: float | None = None

    # ── 5/6/7. 突触、脉冲、自生成问题(ID 引用)──
    synapse_ids: list[str] = field(default_factory=list)
    emitted_pulse_ids: list[str] = field(default_factory=list)
    next_questions: list[str] = field(default_factory=list)

    # ── 内部快照与回滚支持 ──
    last_cycle_id: str = ""
    last_snapshot_id: str = ""

    # ── tombstone 标记(merge/prune 用,不物理删除)──
    is_tombstone: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为可持久化的 dict。"""
        return {
            "neuron_id": self.neuron_id,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "root_theme": self.root_theme,
            "source_node_ref": dict(self.source_node_ref),
            "graph_id": self.graph_id,
            "created_from": self.created_from,
            "dimensions_observed": list(self.dimensions_observed),
            "verification_status": self.verification_status,
            "attention_required": self.attention_required,
            "thesis": self.thesis,
            "evidence_strength": self.evidence_strength,
            "support_count": self.support_count,
            "counter_count": self.counter_count,
            "neutral_count": self.neutral_count,
            "source_diversity": self.source_diversity,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "contradiction_pressure": self.contradiction_pressure,
            "evidence_quality_mix": dict(self.evidence_quality_mix),
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "last_evidence_at": self.last_evidence_at,
            "staleness": self.staleness,
            "metabolism_state": self.metabolism_state,
            "nutrition_level": self.nutrition_level,
            "revival_threshold": self.revival_threshold,
            "dormancy_threshold": self.dormancy_threshold,
            "decay_threshold": self.decay_threshold,
            "growth_energy": self.growth_energy,
            "novelty": self.novelty,
            "bottleneck_pressure": self.bottleneck_pressure,
            "substitution_pressure": self.substitution_pressure,
            "company_mapping_pressure": self.company_mapping_pressure,
            "synthesis_pressure": self.synthesis_pressure,
            "material_depth": self.material_depth,
            "synthesis_depth": self.synthesis_depth,
            "equipment_gap": self.equipment_gap,
            "evidence_gap": self.evidence_gap,
            "policy_gap": self.policy_gap,
            "price_gap": self.price_gap,
            "patent_gap": self.patent_gap,
            "synapse_ids": list(self.synapse_ids),
            "emitted_pulse_ids": list(self.emitted_pulse_ids),
            "next_questions": list(self.next_questions),
            "last_cycle_id": self.last_cycle_id,
            "last_snapshot_id": self.last_snapshot_id,
            "is_tombstone": self.is_tombstone,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchNeuron":
        return cls(
            neuron_id=payload["neuron_id"],
            node_id=payload["node_id"],
            node_type=payload["node_type"],
            canonical_name=payload["canonical_name"],
            aliases=list(payload.get("aliases", [])),
            root_theme=payload.get("root_theme", ""),
            source_node_ref=dict(payload.get("source_node_ref", {})),
            graph_id=payload.get("graph_id", ""),
            created_from=payload.get("created_from", "manual"),
            dimensions_observed=list(payload.get("dimensions_observed", [])),
            verification_status=payload.get("verification_status", "UNVERIFIED_SEED"),
            attention_required=payload.get("attention_required", False),
            thesis=payload.get("thesis", ""),
            evidence_strength=payload.get("evidence_strength", 0.0),
            support_count=payload.get("support_count", 0),
            counter_count=payload.get("counter_count", 0),
            neutral_count=payload.get("neutral_count", 0),
            source_diversity=payload.get("source_diversity", 0.0),
            confidence=payload.get("confidence", 0.0),
            uncertainty=payload.get("uncertainty", 0.5),
            contradiction_pressure=payload.get("contradiction_pressure", 0.0),
            evidence_quality_mix=dict(payload.get("evidence_quality_mix", {})),
            first_seen_at=payload.get("first_seen_at", ""),
            last_seen_at=payload.get("last_seen_at", ""),
            last_evidence_at=payload.get("last_evidence_at", ""),
            staleness=payload.get("staleness", 0.0),
            metabolism_state=payload.get("metabolism_state", "ACTIVE"),
            nutrition_level=payload.get("nutrition_level", 0.0),
            revival_threshold=payload.get("revival_threshold", 0.3),
            dormancy_threshold=payload.get("dormancy_threshold", 0.2),
            decay_threshold=payload.get("decay_threshold", 0.1),
            growth_energy=payload.get("growth_energy", 0.5),
            novelty=payload.get("novelty", 0.5),
            bottleneck_pressure=payload.get("bottleneck_pressure", 0.0),
            substitution_pressure=payload.get("substitution_pressure", 0.0),
            company_mapping_pressure=payload.get("company_mapping_pressure", 0.0),
            synthesis_pressure=payload.get("synthesis_pressure", 0.0),
            material_depth=payload.get("material_depth", 0.0),
            synthesis_depth=payload.get("synthesis_depth", 0.0),
            equipment_gap=payload.get("equipment_gap", 0.0),
            evidence_gap=payload.get("evidence_gap", 0.0),
            policy_gap=payload.get("policy_gap"),
            price_gap=payload.get("price_gap"),
            patent_gap=payload.get("patent_gap"),
            synapse_ids=list(payload.get("synapse_ids", [])),
            emitted_pulse_ids=list(payload.get("emitted_pulse_ids", [])),
            next_questions=list(payload.get("next_questions", [])),
            last_cycle_id=payload.get("last_cycle_id", ""),
            last_snapshot_id=payload.get("last_snapshot_id", ""),
            is_tombstone=payload.get("is_tombstone", False),
        )

    def growth_cycle(self, context: "NeuronContext") -> "GrowthCycleResult":
        """执行一次生长周期(Sense → Resolve → Commit)。

        延迟导入避免循环导入;实际逻辑在 neuron.py。

        Args:
            context: NeuronContext 上下文。

        Returns:
            GrowthCycleResult 完整结果。
        """
        from core.research_graph.neuron.neuron import growth_cycle as _growth_cycle
        return _growth_cycle(self, context)


@dataclass
class ResearchSynapse:
    """突触,连接两个神经元或指向候选目标/开放方向。

    target_kind 决定 target_neuron_id / target_draft 的填充要求。
    validate() 在 state_store.save_synapse() 前强制调用。
    """

    synapse_id: str
    source_neuron_id: str
    target_kind: str
    target_neuron_id: str | None = None
    target_draft: TargetDraft | None = None
    direction: str = ""
    dimension: str = ""
    relation: str = ""
    strength: float = 0.5
    confidence: float = 0.0
    pressure: float = 0.0
    inhibition: float = 0.0
    support_evidence_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)
    neutral_evidence_ids: list[str] = field(default_factory=list)
    status: str = "OPEN"
    attention_required: bool = False
    last_fired_at: str = ""
    mutation_history_ids: list[str] = field(default_factory=list)

    def validate(self) -> None:
        """校验 target_kind 与 target_neuron_id/target_draft 一致性 + 枚举值合法性。

        Raises:
            ValueError: target_kind 与填充字段不匹配,或枚举字段值非法。
        """
        # 枚举值校验
        valid_target_kinds = {e.value for e in SynapseTargetKind}
        if self.target_kind not in valid_target_kinds:
            raise ValueError(
                f"synapse {self.synapse_id} target_kind={self.target_kind!r} 不在合法枚举 {valid_target_kinds}"
            )
        if self.direction:
            valid_directions = {e.value for e in SynapseDirection}
            if self.direction not in valid_directions:
                raise ValueError(
                    f"synapse {self.synapse_id} direction={self.direction!r} 不在合法枚举 {valid_directions}"
                )
        if self.relation:
            valid_relations = {e.value for e in SynapseRelation}
            if self.relation not in valid_relations:
                raise ValueError(
                    f"synapse {self.synapse_id} relation={self.relation!r} 不在合法枚举 {valid_relations}"
                )
        if self.status:
            valid_statuses = {e.value for e in SynapseStatus}
            if self.status not in valid_statuses:
                raise ValueError(
                    f"synapse {self.synapse_id} status={self.status!r} 不在合法枚举 {valid_statuses}"
                )

        # target_kind 与填充字段一致性校验
        if self.target_kind == SynapseTargetKind.EXISTING_NEURON.value:
            if not self.target_neuron_id:
                raise ValueError(
                    f"EXISTING_NEURON synapse {self.synapse_id} 缺少 target_neuron_id"
                )
            if self.target_draft is not None:
                raise ValueError(
                    f"EXISTING_NEURON synapse {self.synapse_id} 不应填充 target_draft"
                )
        elif self.target_kind == SynapseTargetKind.TARGET_DRAFT.value:
            if self.target_draft is None:
                raise ValueError(
                    f"TARGET_DRAFT synapse {self.synapse_id} 缺少 target_draft"
                )
            if self.target_neuron_id:
                raise ValueError(
                    f"TARGET_DRAFT synapse {self.synapse_id} 不应填充 target_neuron_id"
                )
        elif self.target_kind == SynapseTargetKind.OPEN_DIRECTION.value:
            if self.target_neuron_id:
                raise ValueError(
                    f"OPEN_DIRECTION synapse {self.synapse_id} 不应填充 target_neuron_id"
                )
            if self.target_draft is not None:
                raise ValueError(
                    f"OPEN_DIRECTION synapse {self.synapse_id} 不应填充 target_draft"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "synapse_id": self.synapse_id,
            "source_neuron_id": self.source_neuron_id,
            "target_kind": self.target_kind,
            "target_neuron_id": self.target_neuron_id,
            "target_draft": self.target_draft.to_dict() if self.target_draft else None,
            "direction": self.direction,
            "dimension": self.dimension,
            "relation": self.relation,
            "strength": self.strength,
            "confidence": self.confidence,
            "pressure": self.pressure,
            "inhibition": self.inhibition,
            "support_evidence_ids": list(self.support_evidence_ids),
            "counter_evidence_ids": list(self.counter_evidence_ids),
            "neutral_evidence_ids": list(self.neutral_evidence_ids),
            "status": self.status,
            "attention_required": self.attention_required,
            "last_fired_at": self.last_fired_at,
            "mutation_history_ids": list(self.mutation_history_ids),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchSynapse":
        target_draft = None
        if payload.get("target_draft"):
            target_draft = TargetDraft.from_dict(payload["target_draft"])
        return cls(
            synapse_id=payload["synapse_id"],
            source_neuron_id=payload["source_neuron_id"],
            target_kind=payload["target_kind"],
            target_neuron_id=payload.get("target_neuron_id"),
            target_draft=target_draft,
            direction=payload.get("direction", ""),
            dimension=payload.get("dimension", ""),
            relation=payload.get("relation", ""),
            strength=payload.get("strength", 0.5),
            confidence=payload.get("confidence", 0.0),
            pressure=payload.get("pressure", 0.0),
            inhibition=payload.get("inhibition", 0.0),
            support_evidence_ids=list(payload.get("support_evidence_ids", [])),
            counter_evidence_ids=list(payload.get("counter_evidence_ids", [])),
            neutral_evidence_ids=list(payload.get("neutral_evidence_ids", [])),
            status=payload.get("status", "OPEN"),
            attention_required=payload.get("attention_required", False),
            last_fired_at=payload.get("last_fired_at", ""),
            mutation_history_ids=list(payload.get("mutation_history_ids", [])),
        )


@dataclass
class GrowthPulse:
    """生长脉冲,由 ResearchNeuron.growth_cycle() 发出,环境消费不生成。

    emitter_synapse_id: 由特定突触压力触发的 pulse 填此字段;
                        神经元级 pulse(SPLIT/MERGE 等)留空。
    related_node_ids: consume_pulse 回写 EvidenceArrivedEvent 时识别相关神经元。
    """

    pulse_id: str
    emitter_neuron_id: str
    pulse_type: str
    emitter_synapse_id: str | None = None
    root_theme: str = ""
    dimension: str = ""
    reason: str = ""
    priority: int = 50
    suggested_queries: list[str] = field(default_factory=list)
    expected_node_types: list[str] = field(default_factory=list)
    required_evidence_gate: dict[str, Any] = field(default_factory=dict)
    emitted_at: str = field(default_factory=utc_now)
    status: str = "EMITTED"
    blocked_reason: str = ""
    absorbed_by: str = ""
    expires_at: str = ""
    mutation_history_ids: list[str] = field(default_factory=list)
    related_node_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pulse_id": self.pulse_id,
            "emitter_neuron_id": self.emitter_neuron_id,
            "emitter_synapse_id": self.emitter_synapse_id,
            "pulse_type": self.pulse_type,
            "root_theme": self.root_theme,
            "dimension": self.dimension,
            "reason": self.reason,
            "priority": self.priority,
            "suggested_queries": list(self.suggested_queries),
            "expected_node_types": list(self.expected_node_types),
            "required_evidence_gate": dict(self.required_evidence_gate),
            "emitted_at": self.emitted_at,
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "absorbed_by": self.absorbed_by,
            "expires_at": self.expires_at,
            "mutation_history_ids": list(self.mutation_history_ids),
            "related_node_ids": list(self.related_node_ids),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GrowthPulse":
        return cls(
            pulse_id=payload["pulse_id"],
            emitter_neuron_id=payload["emitter_neuron_id"],
            pulse_type=payload["pulse_type"],
            emitter_synapse_id=payload.get("emitter_synapse_id"),
            root_theme=payload.get("root_theme", ""),
            dimension=payload.get("dimension", ""),
            reason=payload.get("reason", ""),
            priority=payload.get("priority", 50),
            suggested_queries=list(payload.get("suggested_queries", [])),
            expected_node_types=list(payload.get("expected_node_types", [])),
            required_evidence_gate=dict(payload.get("required_evidence_gate", {})),
            emitted_at=payload.get("emitted_at", ""),
            status=payload.get("status", "EMITTED"),
            blocked_reason=payload.get("blocked_reason", ""),
            absorbed_by=payload.get("absorbed_by", ""),
            expires_at=payload.get("expires_at", ""),
            mutation_history_ids=list(payload.get("mutation_history_ids", [])),
            related_node_ids=list(payload.get("related_node_ids", [])),
            payload=dict(payload.get("payload", {})),
        )


@dataclass
class MutationEntry:
    """神经元引擎专用 mutation 记录,与现有 MutationLog 并存。

    支持 tombstone(is_tombstone=True)与回滚(rollback_of 指向原 mutation_id)。
    """

    mutation_id: str
    cycle_id: str
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    operator: str = "neuron_engine"
    is_tombstone: bool = False
    rollback_of: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "cycle_id": self.cycle_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "before": dict(self.before),
            "after": dict(self.after),
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "operator": self.operator,
            "is_tombstone": self.is_tombstone,
            "rollback_of": self.rollback_of,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MutationEntry":
        return cls(
            mutation_id=payload["mutation_id"],
            cycle_id=payload["cycle_id"],
            action=payload["action"],
            target_type=payload["target_type"],
            target_id=payload["target_id"],
            before=dict(payload.get("before", {})),
            after=dict(payload.get("after", {})),
            reason=payload.get("reason", ""),
            evidence_ids=list(payload.get("evidence_ids", [])),
            operator=payload.get("operator", "neuron_engine"),
            is_tombstone=payload.get("is_tombstone", False),
            rollback_of=payload.get("rollback_of", ""),
            created_at=payload.get("created_at", ""),
        )


@dataclass
class WeightedEvidence:
    """带权重的证据包装,用于 Resolve Phase 聚合计算。

    previous_stance/previous_weight 仅 retracted 证据填充,
    用于计算撤回惩罚(扣除之前 support 的贡献)。
    """

    evidence_id: str
    source_type: str
    stance: str
    weight: float
    quote: str = ""
    span_start: int = 0
    span_end: int = 0
    previous_stance: str = ""
    previous_weight: float = 0.0


@dataclass
class EvidenceAggregate:
    """Resolve Phase 的证据聚合结果。

    has_strong_counter_no_support: 有强反证且无有效 support。
    has_strong_counter_on_company_mapping: 强反证命中 UP_COMPANY/MAPS_TO synapse。
    """

    support: list[WeightedEvidence]
    counter: list[WeightedEvidence]
    neutral: list[WeightedEvidence]
    stale: list[WeightedEvidence]
    retracted: list[WeightedEvidence]
    source_diversity: float
    avg_weight: float
    weighted_evidence_strength: float
    contradiction_pressure: float
    counter_weight_ratio: float
    support_count: int
    counter_count: int
    has_strong_counter_no_support: bool
    has_strong_counter_on_company_mapping: bool


@dataclass
class SynapseUpdate:
    """Resolve Phase 计算出的突触变更,Commit Phase 应用。"""

    synapse_id: str
    new_strength: float | None = None
    new_confidence: float | None = None
    new_pressure: float | None = None
    new_inhibition: float | None = None
    new_status: str | None = None
    new_attention_required: bool | None = None
    added_support_evidence: list[str] = field(default_factory=list)
    added_counter_evidence: list[str] = field(default_factory=list)
    added_neutral_evidence: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class NeuronContext:
    """growth_cycle 的上下文,禁止万能上帝对象(任务第四节)。

    neighbor_refs 只含 1 跳邻接;incident_synapse_ids 只存 ID,
    neuron 通过 synapse_state_reader 按需读取。
    reader/writer 均为 Protocol,便于单测注入 mock。
    """

    graph_snapshot_id: str
    current_time: str
    cycle_id: str
    neighbor_refs: list[NeighborRef]
    incident_synapse_ids: list[str]
    new_evidence_refs: list[str]
    stale_evidence_refs: list[str]
    manual_mutation_refs: list[str]
    incoming_pulses: list["GrowthPulse"]
    resource_budget: ResourceBudget
    policy_config: Any  # NeuronPolicyConfig,Plan 5 实现;此处用 Any 避免循环导入
    synapse_state_reader: Any  # SynapseStateReader Protocol,Plan 6 实现
    evidence_ledger_reader: Any  # EvidenceLedgerReader Protocol,Plan 6 实现
    mutation_log_writer: Any  # MutationLogWriter Protocol,Plan 6 实现
    trigger_event_type: str = ""
    # Plan 6 新增:counter evidence / time tick payload 透传
    counter_evidence_refs: list[str] = field(default_factory=list)
    staleness_delta: float = 0.0
    tick_reason: str = ""
