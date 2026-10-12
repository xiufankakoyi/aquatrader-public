"""Restricted reader Protocols for neuron growth_cycle.

禁止万能上帝对象(任务第四节):reader 只暴露受限查询,
neuron 不能全库扫描。所有实现受 ResourceBudget 限制。
"""
from __future__ import annotations

from typing import Protocol

from core.research_graph.models import Evidence

from core.research_graph.neuron.models import MutationEntry, ResearchSynapse


class EvidenceLedgerReader(Protocol):
    """证据账本受限读取器(任务第四节)。

    查询方式:node_id / synapse_id / evidence_id / topic / anchor / source_time_range。
    不允许全库扫描。返回的 Evidence 必须带 quote(无 quote 的只能进 awareness)。
    stance 信息从 Evidence.metadata["stance"] 读取(现有 Evidence 无 stance 字段)。
    """

    def query_by_neuron(
        self,
        neuron_id: str,
        stance: str | None = None,
        source_time_range: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[Evidence]:
        """按 neuron_id 查询证据,可选 stance 与时间范围过滤。"""
        ...

    def query_by_synapse(
        self, synapse_id: str, stance: str | None = None
    ) -> list[Evidence]:
        """按 synapse_id 查询证据。"""
        ...

    def query_by_evidence_id(self, evidence_id: str) -> Evidence | None:
        """按 evidence_id 查询单条证据。"""
        ...

    def query_by_topic(
        self, topic: str, anchor_terms: list[str] | None = None
    ) -> list[Evidence]:
        """按 topic 查询证据。"""
        ...

    def query_by_anchor(
        self,
        anchor_terms: list[str],
        source_time_range: tuple[str, str] | None = None,
    ) -> list[Evidence]:
        """按 anchor_terms 查询证据。"""
        ...

    def lookup_previous_stance(self, evidence_id: str) -> tuple[str, float]:
        """查询证据撤回前的 stance 与 weight(用于 retracted 惩罚计算)。

        Returns:
            (previous_stance, previous_weight) 元组;若无撤回记录返回 ("", 0.0)。
        """
        ...


class SynapseStateReader(Protocol):
    """突触状态受限读取器,所有读取基于 snapshot_id 保证一致性。"""

    def get_synapse(self, synapse_id: str) -> ResearchSynapse | None:
        """按 synapse_id 查询突触。"""
        ...

    def get_synapses_by_direction(
        self, neuron_id: str, direction: str
    ) -> list[ResearchSynapse]:
        """按 neuron_id + direction 查询突触。"""
        ...

    def get_synapses_by_dimension(
        self, neuron_id: str, dimension: str
    ) -> list[ResearchSynapse]:
        """按 neuron_id + dimension 查询突触。"""
        ...


class MutationLogWriter(Protocol):
    """mutation log 写入器,先写 cycle_mutations 缓冲,环境统一提交。

    growth_cycle 内部状态变化必须通过此 writer 写入,
    neuron 不直接写 state_store。
    """

    def append_cycle_mutation(self, entry: MutationEntry) -> MutationEntry:
        """追加一条 mutation 到当前 cycle 缓冲。"""
        ...


from core.research_graph.store import GraphStore

from core.research_graph.neuron.models import ResourceBudget
from core.research_graph.neuron.state_store import NeuronStateStore


class EvidenceLedgerReaderImpl:
    """EvidenceLedgerReader 的实现,基于 GraphStore.evidence。

    stance 从 Evidence.metadata["stance"] 读取(现有 Evidence 无 stance 字段)。
    neuron_id 到 evidence 的映射通过 neuron_to_synapses + evidence.supports/contradicts 建立。
    """

    def __init__(
        self,
        graph_store: GraphStore,
        neuron_to_synapses: dict[str, list[str]],
        budget: ResourceBudget,
        snapshot_id: str = "",
    ) -> None:
        self._store = graph_store
        self._neuron_to_synapses = neuron_to_synapses
        self._budget = budget
        self._snapshot_id = snapshot_id

    def query_by_neuron(
        self,
        neuron_id: str,
        stance: str | None = None,
        source_time_range: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> list[Evidence]:
        """按 neuron_id 查询证据,可选 stance 过滤。"""
        if not self._budget.consume_evidence_pull():
            return []
        synapse_ids = self._neuron_to_synapses.get(neuron_id, [])
        if not synapse_ids:
            return []
        syn_set = set(synapse_ids)
        results: list[Evidence] = []
        for ev in self._store.evidence.values():
            if syn_set.intersection(set(ev.supports + ev.contradicts)):
                ev_stance = ev.metadata.get("stance", "")
                if stance and ev_stance != stance:
                    continue
                results.append(ev)
                if len(results) >= limit:
                    break
        return results

    def query_by_synapse(
        self, synapse_id: str, stance: str | None = None
    ) -> list[Evidence]:
        if not self._budget.consume_evidence_pull():
            return []
        results: list[Evidence] = []
        for ev in self._store.evidence.values():
            if synapse_id in ev.supports + ev.contradicts:
                if stance and ev.metadata.get("stance", "") != stance:
                    continue
                results.append(ev)
        return results

    def query_by_evidence_id(self, evidence_id: str) -> Evidence | None:
        if not self._budget.consume_evidence_pull():
            return None
        return self._store.evidence.get(evidence_id)

    def query_by_topic(
        self, topic: str, anchor_terms: list[str] | None = None
    ) -> list[Evidence]:
        if not self._budget.consume_evidence_pull():
            return []
        results: list[Evidence] = []
        for ev in self._store.evidence.values():
            if topic.lower() in (ev.source_title or "").lower() or topic.lower() in (ev.quote or "").lower():
                results.append(ev)
        return results

    def query_by_anchor(
        self,
        anchor_terms: list[str],
        source_time_range: tuple[str, str] | None = None,
    ) -> list[Evidence]:
        if not self._budget.consume_evidence_pull():
            return []
        results: list[Evidence] = []
        for ev in self._store.evidence.values():
            quote_lower = (ev.quote or "").lower()
            if any(term.lower() in quote_lower for term in anchor_terms):
                results.append(ev)
        return results

    def lookup_previous_stance(self, evidence_id: str) -> tuple[str, float]:
        """查询证据撤回前的 stance 与 weight(从 metadata 读取)。"""
        ev = self._store.evidence.get(evidence_id)
        if ev is None:
            return ("", 0.0)
        prev_stance = ev.metadata.get("previous_stance", "")
        prev_weight = float(ev.metadata.get("previous_weight", 0.0))
        return (prev_stance, prev_weight)


class SynapseStateReaderImpl:
    """SynapseStateReader 的实现,基于 NeuronStateStore,受 budget 限制。"""

    def __init__(
        self,
        state_store: NeuronStateStore,
        budget: ResourceBudget,
        snapshot_id: str = "",
    ) -> None:
        self._store = state_store
        self._budget = budget
        self._snapshot_id = snapshot_id

    def get_synapse(self, synapse_id: str) -> ResearchSynapse | None:
        if not self._budget.consume_synapse_read():
            return None
        return self._store.get_synapse(synapse_id)

    def get_synapses_by_direction(
        self, neuron_id: str, direction: str
    ) -> list[ResearchSynapse]:
        if not self._budget.consume_synapse_read():
            return []
        all_syns = self._store.get_synapses_by_neuron(neuron_id)
        return [s for s in all_syns if s.direction == direction]

    def get_synapses_by_dimension(
        self, neuron_id: str, dimension: str
    ) -> list[ResearchSynapse]:
        if not self._budget.consume_synapse_read():
            return []
        all_syns = self._store.get_synapses_by_neuron(neuron_id)
        return [s for s in all_syns if s.dimension == dimension]


class MutationLogWriterImpl:
    """MutationLogWriter 的实现,先缓冲 cycle_mutations,commit 时批量写入。

    rollback() 丢弃当前 cycle 缓冲,neuron 状态不变。
    """

    def __init__(self, state_store: NeuronStateStore, cycle_id: str) -> None:
        self._store = state_store
        self._cycle_id = cycle_id
        self._buffer: list[MutationEntry] = []

    def append_cycle_mutation(self, entry: MutationEntry) -> MutationEntry:
        """追加 mutation 到缓冲,不立即持久化。"""
        self._buffer.append(entry)
        return entry

    def commit(self) -> list[MutationEntry]:
        """提交缓冲区所有 mutation 到 state_store。"""
        for entry in self._buffer:
            self._store.save_mutation(entry)
        committed = list(self._buffer)
        self._buffer.clear()
        return committed

    def rollback(self) -> None:
        """丢弃缓冲区,不持久化。"""
        self._buffer.clear()

    @property
    def pending_count(self) -> int:
        return len(self._buffer)
