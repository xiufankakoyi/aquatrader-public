"""ResearchNeuron growth_cycle 三阶段算法(Sense/Resolve/Commit)。

所有函数接收 neuron: ResearchNeuron 作为首参,避免循环导入。
models.py 的 ResearchNeuron.growth_cycle() 延迟导入此模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.research_graph.models import Evidence

from core.research_graph.neuron.enums import (
    EvidenceStance,
    MetabolismState,
    NeuronMutationAction,
    PulseType,
    SynapseDirection,
    SynapseRelation,
    SynapseStatus,
    SynapseTargetKind,
    VerificationStatus,
)
from core.research_graph.neuron.models import (
    EvidenceAggregate,
    FieldSignal,
    GrowthPulse,
    MutationEntry,
    NeighborRef,
    NeuronContext,
    ResearchNeuron,
    ResearchSynapse,
    SynapseUpdate,
    WeightedEvidence,
)

from core.research_graph.neuron.policy import NeuronPolicyConfig


@dataclass
class SenseResult:
    """Sense Phase 输出,纯只读快照。"""
    field_signals: list[FieldSignal]
    new_evidence: list[Evidence]
    stale_evidence: list[Evidence]
    counter_evidence: list[Evidence]
    neighbor_states: list[NeighborRef]
    incident_synapses: list[ResearchSynapse]
    incoming_pulse_summaries: list[GrowthPulse]
    manual_mutations: list[MutationEntry]
    dimensions_observed: list[str]
    trigger_event_type: str = ""
    counter_evidence_preidentified: bool = False
    initial_contradiction_pressure: float = 0.0


@dataclass
class ResolveResult:
    """Resolve Phase 输出,纯计算结果。"""
    new_staleness: float
    new_nutrition: float
    new_confidence: float
    new_uncertainty: float
    new_contradiction_pressure: float
    new_metabolism_state: str
    new_verification_status: str
    new_attention_required: bool
    synapse_updates: list[SynapseUpdate]
    pulses_to_emit: list[GrowthPulse]
    next_questions: list[str]
    evidence_aggregate: EvidenceAggregate | None = None


@dataclass
class CommitResult:
    """Commit Phase 输出。"""
    mutations: list[MutationEntry]
    emitted_pulses: list[GrowthPulse]
    synapse_updates: list[SynapseUpdate] = field(default_factory=list)


@dataclass
class GrowthCycleResult:
    """growth_cycle 完整结果。"""
    cycle_id: str
    mutations: list[MutationEntry]
    emitted_pulses: list[GrowthPulse]
    new_verification_status: str
    new_metabolism_state: str
    synapse_updates: list[SynapseUpdate] = field(default_factory=list)


from core.research_graph.neuron.field_signals import FieldSignalCalculator


def _pull_evidence(
    neuron: ResearchNeuron,
    context: NeuronContext,
    force_counter: bool = False,
    force_retracted: bool = False,
) -> tuple[list[Evidence], list[Evidence], list[Evidence]]:
    """从 evidence_ledger_reader 拉取证据,budget 由 reader 实现负责扣减。

    Args:
        neuron: 目标神经元。
        context: NeuronContext。
        force_counter: CounterEvidenceArrivedEvent 触发,优先拉取 counter stance。
        force_retracted: EvidenceRetractedEvent 触发,优先拉取 retracted stance。

    Returns:
        (new_evidence, stale_evidence, counter_evidence) 元组。
    """
    reader = context.evidence_ledger_reader
    if reader is None:
        return [], [], []
    if force_counter:
        all_ev = reader.query_by_neuron(neuron.neuron_id, stance="counter")
        return [], [], all_ev
    if force_retracted:
        all_ev = reader.query_by_neuron(neuron.neuron_id, stance="retracted")
        return [], all_ev, []
    all_ev = reader.query_by_neuron(neuron.neuron_id)
    new_evidence: list[Evidence] = []
    stale_evidence: list[Evidence] = []
    counter_evidence: list[Evidence] = []
    for ev in all_ev:
        stance = ev.metadata.get("stance", "")
        if stance == EvidenceStance.COUNTER.value:
            counter_evidence.append(ev)
        elif stance == EvidenceStance.STALE.value:
            stale_evidence.append(ev)
        else:
            new_evidence.append(ev)
    return new_evidence, stale_evidence, counter_evidence


def _compute_dimensions_observed(
    neuron: ResearchNeuron,
    field_signals: list[FieldSignal],
) -> list[str]:
    """根据 field_signals 计算本轮回感知到的维度集合。

    规则(Spec 4.2.2):
    1. 保留 pressure > 0.1 或 gap_score > 0.1 或 evidence_strength > 0 的维度
    2. 按 field_pressure 降序排列
    3. 至少保留 1 个维度(若无信号,保留当前 dimensions_observed[0])
    """
    active = [sig for sig in field_signals
              if sig.pressure > 0.1 or sig.gap_score > 0.1 or sig.evidence_strength > 0]
    if not active:
        return neuron.dimensions_observed[:1] if neuron.dimensions_observed else ["evidence_strength"]
    active.sort(key=lambda s: s.pressure, reverse=True)
    return [sig.dimension for sig in active]


def _sense_phase(neuron: ResearchNeuron, context: NeuronContext) -> SenseResult:
    """Sense Phase:只读快照,不改状态。

    步骤:
    1. 按 trigger_event_type 决定证据拉取模式
    2. 通过 synapse_state_reader 读取 incident_synapses
    3. FieldSignalCalculator 计算 7 维度场信号
    4. 计算 dimensions_observed
    """
    force_counter = context.trigger_event_type == "CounterEvidenceArrivedEvent"
    force_retracted = context.trigger_event_type == "EvidenceRetractedEvent"

    new_evidence, stale_evidence, counter_evidence = _pull_evidence(
        neuron, context, force_counter=force_counter, force_retracted=force_retracted,
    )

    incident_synapses: list[ResearchSynapse] = []
    if context.synapse_state_reader is not None:
        for syn_id in context.incident_synapse_ids:
            syn = context.synapse_state_reader.get_synapse(syn_id)
            if syn is not None:
                incident_synapses.append(syn)

    field_signals = FieldSignalCalculator.calculate(
        neuron=neuron, context=context,
        incident_synapses=incident_synapses,
        new_evidence=new_evidence, counter_evidence=counter_evidence,
    )

    dimensions_observed = _compute_dimensions_observed(neuron, field_signals)

    return SenseResult(
        field_signals=field_signals,
        new_evidence=new_evidence,
        stale_evidence=stale_evidence,
        counter_evidence=counter_evidence,
        neighbor_states=list(context.neighbor_refs),
        incident_synapses=incident_synapses,
        incoming_pulse_summaries=list(context.incoming_pulses),
        manual_mutations=[],  # Plan 3 从 manual_mutation_refs 解析
        dimensions_observed=dimensions_observed,
        trigger_event_type=context.trigger_event_type,
        counter_evidence_preidentified=force_counter,
        initial_contradiction_pressure=0.1 if force_counter else 0.0,
    )


def _compute_staleness(
    neuron: ResearchNeuron, current_time: str, policy: NeuronPolicyConfig,
    staleness_delta: float = 0.0,
) -> float:
    """staleness 计算:基于 current_time 与 last_evidence_at 的天数差 + TimeTick delta。

    Args:
        neuron: 目标神经元。
        current_time: 当前时间 ISO8601。
        policy: 策略配置。
        staleness_delta: TimeTickMetabolismEvent 携带的增量,直接叠加。

    Returns:
        staleness [0,1],1 表示超过 staleness_threshold_days。
    """
    if not neuron.last_evidence_at:
        computed = 0.0
    else:
        try:
            current = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
            last = datetime.fromisoformat(neuron.last_evidence_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            computed = 0.0
        else:
            delta_days = (current - last).days
            computed = 0.0 if delta_days <= 0 else min(1.0, delta_days / policy.staleness_threshold_days)
    # 叠加 TimeTick 携带的 staleness_delta(Plan 6 修复三)
    return min(1.0, computed + staleness_delta)


def _aggregate_evidence(
    support: list[WeightedEvidence],
    counter: list[WeightedEvidence],
    neutral: list[WeightedEvidence],
    stale: list[WeightedEvidence],
    retracted: list[WeightedEvidence],
    incident_synapses: list[ResearchSynapse] | None = None,
) -> EvidenceAggregate:
    """证据聚合(Spec 4.5),识别 retracted + strong counter + 公司映射命中。

    Args:
        support/counter/neutral/stale/retracted: 按 stance 分类的加权证据列表。
        incident_synapses: 关联突触列表,用于检测强反证是否命中 UP_COMPANY/MAPS_TO。

    Returns:
        EvidenceAggregate 聚合结果。
    """
    policy = NeuronPolicyConfig()
    all_weights = [ev.weight for ev in support + counter if ev.stance != EvidenceStance.RETRACTED.value]
    avg_weight = sum(all_weights) / max(len(all_weights), 1)

    source_types = {ev.source_type for ev in support + counter}
    source_diversity = len(source_types) / max(len(policy.evidence_weight), 1)

    weighted_support = sum(ev.weight for ev in support if ev.stance != EvidenceStance.RETRACTED.value)
    weighted_counter_raw = sum(ev.weight for ev in counter if ev.stance != EvidenceStance.RETRACTED.value)
    weighted_evidence_strength = weighted_support + weighted_counter_raw
    contradiction_pressure = weighted_counter_raw / max(weighted_support + weighted_counter_raw, 1.0)
    counter_weight_ratio = weighted_counter_raw / max(weighted_support + weighted_counter_raw, 1.0)

    strong_counter_types = policy.strong_counter_source_types
    strong_counter_evs = [
        ev for ev in counter
        if ev.source_type in strong_counter_types and ev.stance == EvidenceStance.COUNTER.value
    ]
    has_strong_counter = bool(strong_counter_evs)
    has_strong_counter_no_support = has_strong_counter and not support

    # 强反证命中公司映射:检查是否有 UP_COMPANY/MAPS_TO synapse 被 counter 证据引用
    has_strong_counter_on_company_mapping = False
    if has_strong_counter and incident_synapses:
        strong_counter_ids = {ev.evidence_id for ev in strong_counter_evs}
        for syn in incident_synapses:
            if (syn.direction == SynapseDirection.UP_COMPANY.value
                    and syn.relation == SynapseRelation.MAPS_TO.value):
                if strong_counter_ids.intersection(set(syn.counter_evidence_ids)):
                    has_strong_counter_on_company_mapping = True
                    break

    return EvidenceAggregate(
        support=support,
        counter=counter,
        neutral=neutral,
        stale=stale,
        retracted=retracted,
        source_diversity=source_diversity,
        avg_weight=avg_weight,
        weighted_evidence_strength=weighted_evidence_strength,
        contradiction_pressure=contradiction_pressure,
        counter_weight_ratio=counter_weight_ratio,
        support_count=len(support),
        counter_count=len(counter),
        has_strong_counter_no_support=has_strong_counter_no_support,
        has_strong_counter_on_company_mapping=has_strong_counter_on_company_mapping,
    )


def _compute_nutrition(
    policy: NeuronPolicyConfig,
    evidence_agg: EvidenceAggregate,
    staleness: float,
) -> float:
    """nutrition_level 计算(Spec 4.4.1)。

    公式:nutrition = weighted_support + diversity_bonus - counter_penalty - staleness_pen - retracted_penalty
    """
    weighted_support = sum(ev.weight for ev in evidence_agg.support
                           if ev.stance != EvidenceStance.RETRACTED.value)
    weighted_counter = sum(
        ev.weight * (policy.strong_counter_weight_multiplier
                     if ev.source_type in policy.strong_counter_source_types else 1.0)
        for ev in evidence_agg.counter if ev.stance != EvidenceStance.RETRACTED.value)
    diversity_bonus = policy.nutrition_source_diversity_bonus * evidence_agg.source_diversity
    staleness_pen = min(staleness, policy.staleness_penalty_max)
    retracted_penalty = sum(ev.previous_weight for ev in evidence_agg.retracted
                            if ev.previous_stance == EvidenceStance.SUPPORT.value)
    nutrition = weighted_support + diversity_bonus - policy.nutrition_counter_penalty * weighted_counter - staleness_pen - retracted_penalty
    return max(0.0, min(1.0, nutrition))


def _compute_confidence(
    policy: NeuronPolicyConfig,
    evidence_agg: EvidenceAggregate,
    nutrition: float,
    staleness: float,
) -> float:
    """confidence 计算(Spec 4.4.2)。

    公式:confidence = clamp(evidence_strength - contradiction - staleness_pen - retracted_pen, 0, 1)
    """
    retracted_penalty = sum(ev.previous_weight for ev in evidence_agg.retracted
                            if ev.previous_stance == EvidenceStance.SUPPORT.value)
    raw = evidence_agg.weighted_evidence_strength - evidence_agg.contradiction_pressure \
          - min(staleness, policy.staleness_penalty_max) - retracted_penalty
    return max(0.0, min(1.0, raw))


def _compute_uncertainty(
    neuron: ResearchNeuron,
    policy: NeuronPolicyConfig,
    evidence_agg: EvidenceAggregate,
    confidence: float,
    staleness: float,
) -> float:
    """uncertainty 计算(Spec 4.4.3)。"""
    base = 0.5
    if evidence_agg.support and evidence_agg.counter:
        base += 0.2
    if evidence_agg.avg_weight < 0.5:
        base += 0.15
    if staleness > 0.5:
        base += 0.1
    if evidence_agg.source_diversity < 0.3:
        base += 0.1
    if not neuron.aliases or len(neuron.aliases) > 3:
        base += 0.05
    return max(0.0, min(1.0, base - confidence * 0.3))


def _compute_contradiction_pressure(
    neuron: ResearchNeuron,
    policy: NeuronPolicyConfig,
    evidence_agg: EvidenceAggregate,
    sense: SenseResult,
) -> float:
    """contradiction_pressure 计算(Spec 4.4.4),含初始压力 + retracted + strong counter。"""
    base = sum(ev.weight * (policy.strong_counter_weight_multiplier
                            if ev.source_type in policy.strong_counter_source_types else 1.0)
               for ev in evidence_agg.counter
               if ev.stance != EvidenceStance.RETRACTED.value) / max(len(evidence_agg.counter) + 1, 1)
    retracted_penalty = 0.05 * len(evidence_agg.retracted)
    coexist_penalty = 0.1 if (evidence_agg.support and evidence_agg.counter) else 0.0
    initial = sense.initial_contradiction_pressure if sense.counter_evidence_preidentified else 0.0
    return max(0.0, min(1.0, base + retracted_penalty + coexist_penalty + initial))


def _compute_metabolism_transition(
    neuron: ResearchNeuron,
    nutrition: float,
    staleness: float,
    contradiction: float,
    policy: NeuronPolicyConfig,
) -> str:
    """代谢状态转移(Spec 4.4.5)。

    转移图:ACTIVE → WEAKENING → DORMANT → DECAYING → PRUNED(单向)
    WEAKENING/DORMANT/DECAYING 可在 revival 条件满足时回升 ACTIVE。
    PRUNED 不可自然复活。

    Bug 1 修复:revival 条件加 staleness 迟滞阈值。
    旧 bug:WEAKENING→ACTIVE 只看 nutrition/contradiction,而 ACTIVE→WEAKENING 看 staleness,
    导致 staleness=0.8 时 WEAKENING↔ACTIVE 反复横跳。
    修复:revival 时要求 staleness < revival_staleness_threshold,形成迟滞带
    [revival_staleness_threshold, 0.7],此区间内状态稳定。
    """
    current = neuron.metabolism_state
    if current == MetabolismState.ACTIVE.value:
        # Plan 6 修复三:staleness > 0.8 时直接 DORMANT(严重 stale 跨级转移)
        if staleness > 0.8:
            return MetabolismState.DORMANT.value
        if nutrition < policy.dormancy_threshold or staleness > 0.7:
            return MetabolismState.WEAKENING.value
    if current == MetabolismState.WEAKENING.value:
        if nutrition < policy.dormancy_threshold or staleness > 0.8:
            return MetabolismState.DORMANT.value
        # Bug 1 修复:revival 加 staleness 迟滞条件
        if (nutrition > policy.revival_threshold
                and contradiction < 0.3
                and staleness < policy.revival_staleness_threshold):
            return MetabolismState.ACTIVE.value
    if current == MetabolismState.DORMANT.value:
        if nutrition < policy.decay_threshold and contradiction > 0.6:
            return MetabolismState.DECAYING.value
        # Bug 1 修复:revival 加 staleness 迟滞条件
        if (nutrition > policy.revival_threshold
                and contradiction < 0.3
                and staleness < policy.revival_staleness_threshold):
            return MetabolismState.ACTIVE.value
    if current == MetabolismState.DECAYING.value:
        if nutrition < policy.decay_threshold * 0.5 and contradiction > 0.8:
            return MetabolismState.PRUNED.value
        # Bug 1 修复:revival 加 staleness 迟滞条件
        if (nutrition > policy.revival_threshold
                and contradiction < 0.3
                and staleness < policy.revival_staleness_threshold):
            return MetabolismState.ACTIVE.value
    # PRUNED 或无转移条件满足
    return current


def _compute_verification_transition(
    neuron: ResearchNeuron,
    confidence: float,
    contradiction: float,
    metabolism: str,
    evidence_agg: EvidenceAggregate,
    policy: NeuronPolicyConfig,
) -> tuple[str, bool]:
    """验证状态转移(Spec 4.4.6)。"""
    if metabolism == MetabolismState.PRUNED.value:
        return VerificationStatus.PRUNED.value, True
    if metabolism == MetabolismState.DORMANT.value:
        return VerificationStatus.DORMANT.value, False
    if evidence_agg.has_strong_counter_no_support:
        return VerificationStatus.DISPROVEN.value, True
    if (evidence_agg.support_count > 0 and evidence_agg.counter_count > 0
            and evidence_agg.counter_weight_ratio > 0.3):
        return VerificationStatus.CONFLICTED.value, True
    if neuron.verification_status in (VerificationStatus.UNVERIFIED_SEED.value,
                                       VerificationStatus.CANDIDATE.value):
        if (confidence > 0.6 and contradiction < 0.3
                and metabolism == MetabolismState.ACTIVE.value
                and evidence_agg.support_count >= 2):
            return VerificationStatus.ACTIVE.value, False
    return neuron.verification_status, neuron.attention_required


def _compute_synapse_updates(
    neuron: ResearchNeuron,
    incident_synapses: list[ResearchSynapse],
    evidence_agg: EvidenceAggregate,
    policy: NeuronPolicyConfig,
) -> list[SynapseUpdate]:
    """计算突触更新(Spec 4.6),实现证据营养吸收与反证抑制。

    规则:
    1. 吸收 support: strength += ev.weight * 0.1, confidence += ev.weight * 0.05
    2. 吸收 counter: inhibition += ev.weight * 0.15, status → WEAKENING
    3. 强反证命中 UP_COMPANY/MAPS_TO: status → INHIBITED 或 CONFLICTED + attention_required
    4. 无证据 synapse: status → CONFLICTED + attention_required
    5. inhibition > 0.7: status → INHIBITED
    6. inhibition > 0.9 且无 support: status → PRUNED
    7. 突触互抑: 同 dimension 的 CONFLICTED synapse 互相加 inhibition 0.05
    """
    updates: list[SynapseUpdate] = []
    for syn in incident_synapses:
        update = SynapseUpdate(synapse_id=syn.synapse_id)
        new_strength = syn.strength
        new_confidence = syn.confidence
        new_inhibition = syn.inhibition
        new_status = syn.status
        new_attention = syn.attention_required

        # 规则1:吸收 support
        syn_support = [ev for ev in evidence_agg.support
                       if ev.evidence_id in syn.support_evidence_ids]
        if syn_support:
            for ev in syn_support:
                new_strength += ev.weight * 0.1
                new_confidence += ev.weight * 0.05
            update.added_support_evidence = [ev.evidence_id for ev in syn_support]

        # 规则2:吸收 counter
        syn_counter = [ev for ev in evidence_agg.counter
                       if ev.evidence_id in syn.counter_evidence_ids]
        if syn_counter:
            for ev in syn_counter:
                new_inhibition += ev.weight * 0.15
            new_status = SynapseStatus.WEAKENING.value
            update.added_counter_evidence = [ev.evidence_id for ev in syn_counter]

        # 规则3:强反证命中 UP_COMPANY/MAPS_TO
        is_company_mapping = (
            syn.direction == SynapseDirection.UP_COMPANY.value
            and syn.relation == SynapseRelation.MAPS_TO.value
        )
        if is_company_mapping and evidence_agg.has_strong_counter_on_company_mapping:
            new_status = SynapseStatus.INHIBITED.value
            new_attention = True

        # 规则4:无证据 synapse
        # Bug 5 修复:无证据 synapse 过于激进。旧规则只要 synapse 无证据就立刻 CONFLICTED,
        # 导致即使 neuron 有支持证据,关联 synapse 也被错误标记。
        # 修复:仅当 neuron 本身也缺乏支持证据(support_count == 0)时才 CONFLICTED,
        # neuron 有支持证据时保持 OPEN(等待证据绑定)。不覆盖规则3 已设置的 INHIBITED。
        has_any_ev = bool(syn.support_evidence_ids or syn.counter_evidence_ids or syn.neutral_evidence_ids)
        if (not has_any_ev and syn.status == SynapseStatus.OPEN.value
                and new_status == SynapseStatus.OPEN.value
                and evidence_agg.support_count == 0):
            new_status = SynapseStatus.CONFLICTED.value
            new_attention = True

        # 规则5:inhibition > 0.7
        if new_inhibition > 0.7 and new_status not in (SynapseStatus.INHIBITED.value, SynapseStatus.PRUNED.value):
            new_status = SynapseStatus.INHIBITED.value

        # 规则6:inhibition > 0.9 且无 support
        if new_inhibition > 0.9 and not syn.support_evidence_ids:
            new_status = SynapseStatus.PRUNED.value

        update.new_strength = new_strength
        update.new_confidence = new_confidence
        update.new_inhibition = new_inhibition
        update.new_status = new_status
        update.new_attention_required = new_attention
        updates.append(update)

    # 规则7:突触互抑(同 dimension 的 CONFLICTED synapse 互相加 inhibition 0.05)
    conflicted_by_dim: dict[str, list[SynapseUpdate]] = {}
    for u in updates:
        syn = next((s for s in incident_synapses if s.synapse_id == u.synapse_id), None)
        if syn and u.new_status == SynapseStatus.CONFLICTED.value:
            conflicted_by_dim.setdefault(syn.dimension, []).append(u)
    for dim_updates in conflicted_by_dim.values():
        if len(dim_updates) > 1:
            for u in dim_updates:
                u.new_inhibition = (u.new_inhibition or 0.0) + 0.05

    return updates


def _compute_pulses_to_emit(
    neuron: ResearchNeuron,
    sense: SenseResult,
    new_nutrition: float,
    new_metabolism: str,
    new_verification: str,
    policy: NeuronPolicyConfig,
) -> list[GrowthPulse]:
    """根据缺口/压力/瓶颈生成脉冲(Spec 4.9),受 budget 限制。

    生成规则(优先级排序)。由突触压力触发的脉冲填 emitter_synapse_id。
    """
    pulses: list[GrowthPulse] = []

    # 脉冲类型 → 触发它的 SynapseDirection 映射(用于回填 emitter_synapse_id)
    pulse_to_direction: dict[str, str] = {
        PulseType.EXPAND_UPSTREAM_MATERIAL.value: SynapseDirection.UPSTREAM_MATERIAL.value,
        PulseType.EXPAND_DOWNSTREAM_APPLICATION.value: SynapseDirection.DOWNSTREAM_APPLICATION.value,
        PulseType.EXPAND_SYNTHESIS_CHILDREN.value: SynapseDirection.SYNTHESIS_CHILD.value,
        PulseType.EXPAND_COMPANY_MAPPING.value: SynapseDirection.UP_COMPANY.value,
        PulseType.CHECK_SUBSTITUTE_BRANCH.value: SynapseDirection.LATERAL_SUBSTITUTE.value,
        PulseType.CHECK_CONSTRAINT.value: SynapseDirection.CONSTRAINT_PRESSURE.value,
        PulseType.CHECK_TIME_STAGE.value: SynapseDirection.TIME_CLOCK.value,
    }

    def _find_synapse_by_direction(direction: str) -> str | None:
        """从 incident_synapses 中找到指定 direction 的 synapse_id。"""
        for syn in sense.incident_synapses:
            if syn.direction == direction:
                return syn.synapse_id
        return None

    def _emit(pulse_type: str, priority: int, reason: str) -> None:
        direction = pulse_to_direction.get(pulse_type)
        emitter_synapse_id = _find_synapse_by_direction(direction) if direction else None
        pulses.append(GrowthPulse(
            pulse_id=f"pulse_{neuron.neuron_id}_{pulse_type}_{len(pulses)}",
            emitter_neuron_id=neuron.neuron_id,
            emitter_synapse_id=emitter_synapse_id,
            pulse_type=pulse_type,
            priority=priority,
            reason=reason,
            related_node_ids=[neuron.node_id],
        ))

    if new_verification == "CONFLICTED":
        _emit(PulseType.REQUEST_HUMAN_REVIEW.value, 90, "support/counter 证据冲突")
    # Bug 2 修复:DISPROVEN 也应触发人工审查(spec 6.4 DISPROVEN 是凋亡候选,需人工裁决)
    if new_verification == "DISPROVEN":
        _emit(PulseType.REQUEST_HUMAN_REVIEW.value, 90, "证据被反证推翻,需人工裁决")
    if any(s.contradiction > 0.3 for s in sense.field_signals):
        _emit(PulseType.CHECK_COUNTER_EVIDENCE.value, 85, "检测到反证压力")
    if neuron.evidence_gap > 0.6:
        _emit(PulseType.REQUEST_GROWTH_MINING.value, 80, "证据缺口过大")
    if neuron.company_mapping_pressure > 0.6:
        _emit(PulseType.EXPAND_COMPANY_MAPPING.value, 75, "公司映射压力")
    if neuron.equipment_gap > 0.5:
        _emit(PulseType.EXPAND_EQUIPMENT.value, 70, "设备缺口")
    if neuron.material_depth < 0.3:
        _emit(PulseType.EXPAND_UPSTREAM_MATERIAL.value, 65, "上游材料深度不足")
    if neuron.synthesis_depth < 0.3:
        _emit(PulseType.EXPAND_SYNTHESIS_CHILDREN.value, 60, "合成树深度不足")
    if neuron.substitution_pressure > 0.5:
        _emit(PulseType.CHECK_SUBSTITUTE_BRANCH.value, 55, "替代路线压力")
    if neuron.bottleneck_pressure > 0.6:
        _emit(PulseType.CHECK_CONSTRAINT.value, 50, "瓶颈约束压力")
    if new_metabolism == "DECAYING":
        _emit(PulseType.PRUNE_DISPROVEN.value, 40, "代谢衰减,候选凋亡")

    # Bug 2 修复:按 priority 降序排序后再切片,避免高优先级脉冲因生成顺序靠后被丢弃
    pulses.sort(key=lambda p: p.priority, reverse=True)
    return pulses[:3]  # max_pulses_emittable=3


def _generate_next_questions(
    neuron: ResearchNeuron,
    sense: SenseResult,
    new_verification: str,
    new_metabolism: str,
) -> list[str]:
    """自生成问题(Spec 4.10),上限 5 个。"""
    questions: list[str] = []
    if neuron.evidence_gap > 0.5:
        questions.append(f"需要更多来源验证 {neuron.canonical_name} 的产业链归属")
    if neuron.company_mapping_pressure > 0.5:
        questions.append(f"{neuron.canonical_name} 是否有 A 股映射?主体公司是谁?")
    if neuron.substitution_pressure > 0.5:
        questions.append(f"{neuron.canonical_name} 的替代路线有哪些?成熟度如何?")
    if new_verification == "CONFLICTED":
        questions.append("support 与 counter 证据冲突,需官方披露裁决")
    if new_metabolism == "DORMANT":
        questions.append("长期无新证据,是否需要重新激活挖掘?")
    return questions[:5]


def _process_manual_mutations(
    neuron: ResearchNeuron,
    sense: SenseResult,
    evidence_agg: EvidenceAggregate,
    policy: NeuronPolicyConfig,
) -> list[SynapseUpdate]:
    """处理人工修改事件(Spec 4.7)。

    规则:
    1. MANUAL_ADD_SYNAPSE: 无证据 → CONFLICTED + attention_required
    2. MANUAL_REMOVE_SYNAPSE: status=PRUNED,保留 mutation log
    3. MANUAL_UPDATE_SYNAPSE: 应用人工指定字段,attention_required=True
    """
    # Plan 3 从 sense.manual_mutations 解析;Plan 2 返回空列表
    return []


def _resolve_phase(
    neuron: ResearchNeuron,
    context: NeuronContext,
    sense: SenseResult,
) -> ResolveResult:
    """Resolve Phase:纯计算,综合所有信号计算净效应(Spec 4.3)。"""
    policy = context.policy_config or NeuronPolicyConfig()

    # 步骤 1:staleness(Plan 6 修复三:叠加 TimeTick staleness_delta)
    new_staleness = _compute_staleness(
        neuron, context.current_time, policy,
        staleness_delta=context.staleness_delta,
    )

    # Plan 6 修复四:CounterEvidenceArrived 触发时,把 counter evidence_ids
    # 注入 UP_COMPANY/MAPS_TO synapse 的 counter_evidence_ids(内存对象),
    # 使 _aggregate_evidence 的强反证命中检测能匹配
    if sense.counter_evidence_preidentified and sense.counter_evidence:
        counter_ids = {ev.evidence_id for ev in sense.counter_evidence}
        for syn in sense.incident_synapses:
            if (syn.direction == SynapseDirection.UP_COMPANY.value
                    and syn.relation == SynapseRelation.MAPS_TO.value):
                for cid in counter_ids:
                    if cid not in syn.counter_evidence_ids:
                        syn.counter_evidence_ids.append(cid)

    # 步骤 2:证据聚合
    support_list = [
        WeightedEvidence(
            evidence_id=ev.evidence_id, source_type=ev.source_type,
            stance=ev.metadata.get("stance", EvidenceStance.SUPPORT.value),
            weight=policy.evidence_weight.get(ev.source_type, 0.3),
            quote=ev.quote,
        ) for ev in sense.new_evidence
    ]
    counter_list = [
        WeightedEvidence(
            evidence_id=ev.evidence_id, source_type=ev.source_type,
            stance=ev.metadata.get("stance", EvidenceStance.COUNTER.value),
            weight=policy.evidence_weight.get(ev.source_type, 0.3),
            quote=ev.quote,
        ) for ev in sense.counter_evidence
    ]
    stale_list = [
        WeightedEvidence(
            evidence_id=ev.evidence_id, source_type=ev.source_type,
            stance=ev.metadata.get("stance", EvidenceStance.STALE.value),
            weight=policy.evidence_weight.get(ev.source_type, 0.3),
        ) for ev in sense.stale_evidence
    ]
    retracted_list: list[WeightedEvidence] = []  # 从 retracted stance 证据提取
    evidence_agg = _aggregate_evidence(
        support_list, counter_list, [], stale_list, retracted_list,
        incident_synapses=sense.incident_synapses,
    )

    # 步骤 3:nutrition
    new_nutrition = _compute_nutrition(policy, evidence_agg, new_staleness)
    # 步骤 4:confidence
    new_confidence = _compute_confidence(policy, evidence_agg, new_nutrition, new_staleness)
    # 步骤 5:uncertainty
    new_uncertainty = _compute_uncertainty(neuron, policy, evidence_agg, new_confidence, new_staleness)
    # 步骤 6:contradiction_pressure
    new_contradiction = _compute_contradiction_pressure(neuron, policy, evidence_agg, sense)
    # 步骤 7:metabolism
    new_metabolism = _compute_metabolism_transition(
        neuron, new_nutrition, new_staleness, new_contradiction, policy)
    # 步骤 8:verification
    new_verification, new_attention = _compute_verification_transition(
        neuron, new_confidence, new_contradiction, new_metabolism, evidence_agg, policy)
    # 步骤 8.5:人工修改(Plan 2 返回空)
    manual_updates = _process_manual_mutations(neuron, sense, evidence_agg, policy)
    # 步骤 9:突触更新
    synapse_updates = _compute_synapse_updates(neuron, sense.incident_synapses, evidence_agg, policy)
    synapse_updates.extend(manual_updates)
    # 步骤 10:脉冲生成
    pulses_to_emit = _compute_pulses_to_emit(
        neuron, sense, new_nutrition, new_metabolism, new_verification, policy)
    # 步骤 11:自生成问题
    next_questions = _generate_next_questions(neuron, sense, new_verification, new_metabolism)

    return ResolveResult(
        new_staleness=new_staleness,
        new_nutrition=new_nutrition,
        new_confidence=new_confidence,
        new_uncertainty=new_uncertainty,
        new_contradiction_pressure=new_contradiction,
        new_metabolism_state=new_metabolism,
        new_verification_status=new_verification,
        new_attention_required=new_attention,
        synapse_updates=synapse_updates,
        pulses_to_emit=pulses_to_emit,
        next_questions=next_questions,
        evidence_aggregate=evidence_agg,
    )


def _commit_phase(
    neuron: ResearchNeuron,
    context: NeuronContext,
    resolve: ResolveResult,
) -> CommitResult:
    """Commit Phase:应用状态变更,写 mutation log,发射脉冲(Spec 4.8)。

    Plan 6 修复一:将 resolve 结果回写到 neuron 对象字段,使 event_loop_tick
    的 save_neuron 能持久化新状态。所有 mutation 通过 mutation_log_writer 落库。
    """
    mutations: list[MutationEntry] = []
    writer = context.mutation_log_writer

    # ── Plan 6 修复一:回写 neuron 字段(BEFORE 快照用于 mutation)──
    before_verify = neuron.verification_status
    before_metab = neuron.metabolism_state
    before_nutrition = neuron.nutrition_level
    before_confidence = neuron.confidence

    neuron.verification_status = resolve.new_verification_status
    neuron.metabolism_state = resolve.new_metabolism_state
    neuron.confidence = resolve.new_confidence
    neuron.uncertainty = resolve.new_uncertainty
    neuron.staleness = resolve.new_staleness
    neuron.nutrition_level = resolve.new_nutrition
    neuron.contradiction_pressure = resolve.new_contradiction_pressure
    neuron.attention_required = resolve.new_attention_required
    if resolve.evidence_aggregate is not None:
        agg = resolve.evidence_aggregate
        neuron.support_count = agg.support_count
        neuron.counter_count = agg.counter_count
        neuron.evidence_strength = agg.weighted_evidence_strength
        neuron.source_diversity = agg.source_diversity
    from core.research_graph.models import utc_now
    neuron.last_seen_at = context.current_time or utc_now()
    if resolve.evidence_aggregate and resolve.evidence_aggregate.support:
        neuron.last_evidence_at = context.current_time or utc_now()

    # 验证状态变更 mutation
    if resolve.new_verification_status != before_verify:
        m = MutationEntry(
            mutation_id=f"nmut_{context.cycle_id}_verify",
            cycle_id=context.cycle_id,
            action=NeuronMutationAction.NEURON_VERIFICATION_CHANGED.value,
            target_type="neuron",
            target_id=neuron.neuron_id,
            before={"verification_status": before_verify},
            after={"verification_status": resolve.new_verification_status},
            reason=f"confidence={resolve.new_confidence:.2f}, contradiction={resolve.new_contradiction_pressure:.2f}",
        )
        mutations.append(m)
        if writer is not None:
            writer.append_cycle_mutation(m)

    # 代谢状态变更 mutation(Plan 6 修复三:含 staleness reason)
    if resolve.new_metabolism_state != before_metab:
        m = MutationEntry(
            mutation_id=f"nmut_{context.cycle_id}_metab",
            cycle_id=context.cycle_id,
            action=NeuronMutationAction.NEURON_METABOLISM_CHANGED.value,
            target_type="neuron",
            target_id=neuron.neuron_id,
            before={"metabolism_state": before_metab},
            after={"metabolism_state": resolve.new_metabolism_state},
            reason=f"nutrition={resolve.new_nutrition:.2f}, staleness={resolve.new_staleness:.2f}, contradiction={resolve.new_contradiction_pressure:.2f}",
        )
        mutations.append(m)
        if writer is not None:
            writer.append_cycle_mutation(m)

    # nutrition/confidence recompute mutation(总是记录)
    m_recompute = MutationEntry(
        mutation_id=f"nmut_{context.cycle_id}_nutrition",
        cycle_id=context.cycle_id,
        action=NeuronMutationAction.NEURON_NUTRITION_RECOMPUTED.value,
        target_type="neuron",
        target_id=neuron.neuron_id,
        before={"nutrition_level": before_nutrition, "confidence": before_confidence},
        after={"nutrition_level": resolve.new_nutrition, "confidence": resolve.new_confidence},
        reason="growth_cycle recompute",
    )
    mutations.append(m_recompute)
    if writer is not None:
        writer.append_cycle_mutation(m_recompute)

    # 突触变更 mutation(Plan 6 修复四:含 INHIBITED/CONFLICTED 审计)
    # Bug 3 修复:加变化检测,避免同一状态变更多次写入 mutation log
    for update in resolve.synapse_updates:
        if update.new_status is None and update.new_strength is None:
            continue
        # 读取当前 synapse 状态做对比,无变化则跳过
        current_syn = None
        if hasattr(context, 'synapse_state_reader') and context.synapse_state_reader is not None:
            try:
                current_syn = context.synapse_state_reader.get_synapse(update.synapse_id)
            except Exception:
                current_syn = None
        if current_syn is not None:
            status_changed = (update.new_status is not None
                              and update.new_status != current_syn.status)
            strength_changed = (update.new_strength is not None
                                and abs((update.new_strength or 0.0) - current_syn.strength) > 1e-9)
            inhibition_changed = (update.new_inhibition is not None
                                  and abs((update.new_inhibition or 0.0) - current_syn.inhibition) > 1e-9)
            attention_changed = (update.new_attention_required is not None
                                 and update.new_attention_required != current_syn.attention_required)
            if not (status_changed or strength_changed or inhibition_changed or attention_changed):
                continue
        action = NeuronMutationAction.SYNAPSE_STATUS_CHANGED.value
        if update.new_status == SynapseStatus.INHIBITED.value:
            action = NeuronMutationAction.SYNAPSE_INHIBITED.value
        elif update.new_status == SynapseStatus.PRUNED.value:
            action = NeuronMutationAction.SYNAPSE_PRUNED.value
        m = MutationEntry(
            mutation_id=f"nmut_{context.cycle_id}_syn_{update.synapse_id}",
            cycle_id=context.cycle_id,
            action=action,
            target_type="synapse",
            target_id=update.synapse_id,
            before={},
            after={
                k: v for k, v in {
                    "status": update.new_status,
                    "strength": update.new_strength,
                    "inhibition": update.new_inhibition,
                    "attention_required": update.new_attention_required,
                }.items() if v is not None
            },
            reason=update.reason or "synapse update",
            evidence_ids=list(update.added_counter_evidence),
        )
        mutations.append(m)
        if writer is not None:
            writer.append_cycle_mutation(m)

    # 脉冲发射 mutation
    for pulse in resolve.pulses_to_emit:
        m = MutationEntry(
            mutation_id=f"nmut_{context.cycle_id}_pulse_{pulse.pulse_id}",
            cycle_id=context.cycle_id,
            action=NeuronMutationAction.PULSE_EMITTED.value,
            target_type="pulse",
            target_id=pulse.pulse_id,
            before={},
            after={"pulse_type": pulse.pulse_type, "priority": pulse.priority},
            reason=pulse.reason,
        )
        mutations.append(m)
        if writer is not None:
            writer.append_cycle_mutation(m)

    return CommitResult(
        mutations=mutations,
        emitted_pulses=list(resolve.pulses_to_emit),
        synapse_updates=list(resolve.synapse_updates),
    )


def growth_cycle(neuron: ResearchNeuron, context: NeuronContext) -> GrowthCycleResult:
    """神经元一次生长周期,严格三阶段执行(Spec 4.1)。

    并发感知保证:
    - Sense Phase 所有读取基于 context.graph_snapshot_id
    - Resolve Phase 纯计算,无副作用
    - Commit Phase 只写 context.mutation_log_writer 的 cycle_mutations 缓冲
    """
    sense = _sense_phase(neuron, context)
    resolve = _resolve_phase(neuron, context, sense)
    commit = _commit_phase(neuron, context, resolve)
    return GrowthCycleResult(
        cycle_id=context.cycle_id,
        mutations=commit.mutations,
        emitted_pulses=commit.emitted_pulses,
        new_verification_status=resolve.new_verification_status,
        new_metabolism_state=resolve.new_metabolism_state,
        synapse_updates=commit.synapse_updates,
    )
