"""神经元事件层:9 类事件 + EventRouter + EvidenceIndex。

所有事件为不可变 dataclass,通过 event_type 字段区分类别。
EventRouter 负责把事件投递给 active_neuron_queue(环境层消费)。
EvidenceIndex 支持 evidence → neuron_id 的多键反查(任务第五节 push 机制)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.research_graph.models import Evidence, utc_now
from core.research_graph.neuron.enums import EventType

if TYPE_CHECKING:
    from core.research_graph.neuron.models import ResearchNeuron


@dataclass
class NeuronEvent:
    """所有神经元事件的基类。

    Attributes:
        event_id: 事件唯一 ID。
        event_type: 事件类型(EventType 枚举值),由子类固定。
        target_neuron_id: 目标神经元 ID。
        payload: 事件负载,由子类语义决定。
        created_at: 创建时间(ISO8601)。
    """
    event_id: str
    target_neuron_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    event_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "target_neuron_id": self.target_neuron_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass
class EvidenceArrivedEvent(NeuronEvent):
    """新支持证据到达。payload: new_evidence_ids."""
    event_type: str = EventType.EVIDENCE_ARRIVED.value


@dataclass
class CounterEvidenceArrivedEvent(NeuronEvent):
    """新反证到达。payload: counter_evidence_ids."""
    event_type: str = EventType.COUNTER_EVIDENCE_ARRIVED.value


@dataclass
class ManualMutationEvent(NeuronEvent):
    """用户手动修改。payload: mutation_refs, manual_action."""
    event_type: str = EventType.MANUAL_MUTATION.value


@dataclass
class NeighborStateChangedEvent(NeuronEvent):
    """邻居神经元状态变化。payload: neighbor_neuron_id, changed_fields."""
    event_type: str = EventType.NEIGHBOR_STATE_CHANGED.value


@dataclass
class TimeTickMetabolismEvent(NeuronEvent):
    """时间 tick 触发代谢。payload: current_time, staleness_delta."""
    event_type: str = EventType.TIME_TICK_METABOLISM.value


@dataclass
class GrowthPulseAbsorbedEvent(NeuronEvent):
    """生长脉冲被吸收。payload: pulse_id, absorbed_by."""
    event_type: str = EventType.GROWTH_PULSE_ABSORBED.value


@dataclass
class EvidenceRetractedEvent(NeuronEvent):
    """证据被撤回。payload: retracted_evidence_ids."""
    event_type: str = EventType.EVIDENCE_RETRACTED.value


@dataclass
class MergeCandidateEvent(NeuronEvent):
    """合并候选。payload: candidate_neuron_ids, similarity_score."""
    event_type: str = EventType.MERGE_CANDIDATE.value


@dataclass
class SplitCandidateEvent(NeuronEvent):
    """分裂候选。payload: reason(too_coarse), suggested_split_dims."""
    event_type: str = EventType.SPLIT_CANDIDATE.value


class EventRouter:
    """事件路由器:按 target_neuron_id 收集事件,供环境层 tick 时拉取。

    非线程安全,由环境层 event_loop 单线程驱动。
    """

    def __init__(self) -> None:
        self._queue: dict[str, list[NeuronEvent]] = {}

    def dispatch(self, event: NeuronEvent) -> None:
        """投递事件到目标神经元的队列。"""
        self._queue.setdefault(event.target_neuron_id, []).append(event)

    def events_for(self, neuron_id: str) -> list[NeuronEvent]:
        """返回该神经元当前待处理事件(不消费)。"""
        return list(self._queue.get(neuron_id, []))

    def drain(self, neuron_id: str) -> list[NeuronEvent]:
        """消费并返回该神经元全部待处理事件。"""
        return self._queue.pop(neuron_id, [])

    def pending_neuron_ids(self) -> list[str]:
        """返回当前有待处理事件的神经元 ID 列表。"""
        return list(self._queue.keys())


class EvidenceIndex:
    """证据路由索引(任务第五节 push 机制)。

    索引键:node_id / synapse_id / canonical_name / aliases / topic(root_theme)/ symbol / anchor_terms。
    index_neuron 建立正向索引;lookup 从 evidence 的 supports/contradicts/quote 反查相关 neuron_id。
    """

    def __init__(self) -> None:
        self._by_node: dict[str, set[str]] = {}
        self._by_canonical: dict[str, set[str]] = {}
        self._by_topic: dict[str, set[str]] = {}
        self._by_symbol: dict[str, set[str]] = {}

    def index_neuron(self, neuron: "ResearchNeuron") -> None:
        """神经元注册时建立索引。"""
        nid = neuron.neuron_id
        self._by_node.setdefault(neuron.node_id, set()).add(nid)
        if neuron.canonical_name:
            self._by_canonical.setdefault(neuron.canonical_name, set()).add(nid)
        for alias in neuron.aliases:
            self._by_canonical.setdefault(alias, set()).add(nid)
        if neuron.root_theme:
            self._by_topic.setdefault(neuron.root_theme, set()).add(nid)
        symbol = (neuron.source_node_ref or {}).get("symbol", "")
        if symbol:
            self._by_symbol.setdefault(symbol, set()).add(nid)

    def remove_neuron(self, neuron_id: str) -> None:
        """移除神经元的全部索引项(merge/prune 后清理)。"""
        for mapping in (self._by_node, self._by_canonical, self._by_topic, self._by_symbol):
            for key, ids in list(mapping.items()):
                ids.discard(neuron_id)
                if not ids:
                    del mapping[key]

    def lookup_by_node(self, node_id: str) -> set[str]:
        """按 node_id 查询关联神经元。"""
        return set(self._by_node.get(node_id, set()))

    def lookup_by_canonical(self, name: str) -> set[str]:
        """按 canonical_name / alias 查询。"""
        return set(self._by_canonical.get(name, set()))

    def lookup(self, evidence: Evidence) -> set[str]:
        """从 evidence 的 supports/contradicts/quote 反查相关 neuron_id。

        Args:
            evidence: 证据实例。

        Returns:
            相关 neuron_id 集合。
        """
        hit: set[str] = set()
        for node_id in list(evidence.supports) + list(getattr(evidence, "contradicts", [])):
            hit.update(self._by_node.get(node_id, set()))
        # quote 中匹配 canonical_name / alias
        quote = (evidence.quote or "") + " " + (evidence.summary or "")
        for name, ids in self._by_canonical.items():
            if name and name in quote:
                hit.update(ids)
        return hit
