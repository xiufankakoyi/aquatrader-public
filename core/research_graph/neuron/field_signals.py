"""FieldSignalCalculator — 神经元内部工具,计算 7 维度局部场信号。

纯函数式,不持有状态。环境不调用此类(Spec 4.2.3)。
维度与 direction/gap 字段映射(Spec 4.2.3):
    industry_flow: UPSTREAM_MATERIAL + DOWNSTREAM_APPLICATION + material_depth/equipment_gap
    substitute_branch: LATERAL_SUBSTITUTE + substitution_pressure
    synthesis_tree: SYNTHESIS_PARENT/CHILD + synthesis_depth/synthesis_pressure
    company_chain: UP_COMPANY + company_mapping_pressure
    time_clock: TIME_CLOCK + staleness
    constraint_bottleneck: CONSTRAINT_PRESSURE + bottleneck_pressure
    evidence_strength: 全部证据 + evidence_gap + source_diversity
"""
from __future__ import annotations

from core.research_graph.models import Evidence

from core.research_graph.neuron.enums import SynapseDirection
from core.research_graph.neuron.models import (
    FieldSignal,
    NeuronContext,
    ResearchNeuron,
    ResearchSynapse,
)


class FieldSignalCalculator:
    """神经元内部工具,基于快照计算各维度局部场信号。"""

    @staticmethod
    def calculate(
        neuron: ResearchNeuron,
        context: NeuronContext | None,
        incident_synapses: list[ResearchSynapse],
        new_evidence: list[Evidence],
        counter_evidence: list[Evidence],
    ) -> list[FieldSignal]:
        """计算 7 个默认维度的局部场信号。

        Args:
            neuron: 目标神经元。
            context: NeuronContext(本版本未使用,保留接口一致性)。
            incident_synapses: 关联突触列表。
            new_evidence: 新到达证据。
            counter_evidence: 反证证据。

        Returns:
            7 个 FieldSignal 列表。
        """
        syn_by_dir: dict[str, list[ResearchSynapse]] = {}
        for syn in incident_synapses:
            syn_by_dir.setdefault(syn.direction, []).append(syn)

        total_ev = len(new_evidence) + len(counter_evidence)
        contradiction = min(1.0, len(counter_evidence) / max(total_ev, 1))

        signals: list[FieldSignal] = [
            FieldSignalCalculator._industry_flow(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._substitute_branch(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._synthesis_tree(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._company_chain(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._time_clock(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._constraint_bottleneck(neuron, syn_by_dir, contradiction),
            FieldSignalCalculator._evidence_strength(neuron, syn_by_dir, contradiction, total_ev),
        ]
        return signals

    @staticmethod
    def _industry_flow(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """industry_flow: UPSTREAM_MATERIAL + DOWNSTREAM_APPLICATION + material_depth/equipment_gap。"""
        count = len(syn_by_dir.get(SynapseDirection.UPSTREAM_MATERIAL.value, [])) + \
                len(syn_by_dir.get(SynapseDirection.DOWNSTREAM_APPLICATION.value, []))
        pressure = min(1.0, count * 0.2 + neuron.equipment_gap * 0.5)
        gap = max(0.0, 1.0 - neuron.material_depth) * 0.5 + neuron.equipment_gap * 0.5
        return FieldSignal(
            dimension="industry_flow",
            pressure=pressure,
            gap_score=min(1.0, gap),
            evidence_strength=min(1.0, neuron.material_depth),
            contradiction=contradiction,
        )

    @staticmethod
    def _substitute_branch(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """substitute_branch: LATERAL_SUBSTITUTE + substitution_pressure。"""
        count = len(syn_by_dir.get(SynapseDirection.LATERAL_SUBSTITUTE.value, []))
        pressure = min(1.0, count * 0.2 + neuron.substitution_pressure * 0.7)
        gap = neuron.substitution_pressure
        return FieldSignal(
            dimension="substitute_branch",
            pressure=pressure,
            gap_score=gap,
            evidence_strength=min(1.0, count * 0.3),
            contradiction=contradiction,
        )

    @staticmethod
    def _synthesis_tree(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """synthesis_tree: SYNTHESIS_PARENT/CHILD + synthesis_depth/synthesis_pressure。"""
        count = len(syn_by_dir.get(SynapseDirection.SYNTHESIS_PARENT.value, [])) + \
                len(syn_by_dir.get(SynapseDirection.SYNTHESIS_CHILD.value, []))
        pressure = min(1.0, count * 0.2 + neuron.synthesis_pressure * 0.7)
        gap = max(0.0, 1.0 - neuron.synthesis_depth) * 0.5 + neuron.synthesis_pressure * 0.5
        return FieldSignal(
            dimension="synthesis_tree",
            pressure=pressure,
            gap_score=min(1.0, gap),
            evidence_strength=min(1.0, neuron.synthesis_depth),
            contradiction=contradiction,
        )

    @staticmethod
    def _company_chain(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """company_chain: UP_COMPANY + company_mapping_pressure。"""
        count = len(syn_by_dir.get(SynapseDirection.UP_COMPANY.value, []))
        pressure = min(1.0, count * 0.2 + neuron.company_mapping_pressure * 0.8)
        gap = neuron.company_mapping_pressure
        return FieldSignal(
            dimension="company_chain",
            pressure=pressure,
            gap_score=gap,
            evidence_strength=min(1.0, count * 0.3),
            contradiction=contradiction,
        )

    @staticmethod
    def _time_clock(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """time_clock: TIME_CLOCK + staleness。"""
        count = len(syn_by_dir.get(SynapseDirection.TIME_CLOCK.value, []))
        pressure = min(1.0, count * 0.2 + neuron.staleness * 0.8)
        gap = neuron.staleness
        return FieldSignal(
            dimension="time_clock",
            pressure=pressure,
            gap_score=gap,
            evidence_strength=max(0.0, 1.0 - neuron.staleness),
            contradiction=contradiction,
        )

    @staticmethod
    def _constraint_bottleneck(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
    ) -> FieldSignal:
        """constraint_bottleneck: CONSTRAINT_PRESSURE + bottleneck_pressure。"""
        count = len(syn_by_dir.get(SynapseDirection.CONSTRAINT_PRESSURE.value, []))
        pressure = min(1.0, count * 0.2 + neuron.bottleneck_pressure * 0.8)
        gap = neuron.bottleneck_pressure
        return FieldSignal(
            dimension="constraint_bottleneck",
            pressure=pressure,
            gap_score=gap,
            evidence_strength=min(1.0, count * 0.3),
            contradiction=contradiction,
        )

    @staticmethod
    def _evidence_strength(
        neuron: ResearchNeuron,
        syn_by_dir: dict[str, list[ResearchSynapse]],
        contradiction: float,
        total_ev: int,
    ) -> FieldSignal:
        """evidence_strength: 全部证据 + evidence_gap + source_diversity。"""
        pressure = min(1.0, neuron.evidence_gap * 0.8 + (1.0 - neuron.source_diversity) * 0.2)
        gap = neuron.evidence_gap
        strength = min(1.0, neuron.evidence_strength + total_ev * 0.1)
        return FieldSignal(
            dimension="evidence_strength",
            pressure=pressure,
            gap_score=gap,
            evidence_strength=strength,
            contradiction=contradiction,
        )
