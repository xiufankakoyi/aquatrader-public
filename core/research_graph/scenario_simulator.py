"""情景分析与压力测试。

解决痛点 3:如果原材料涨价 20%、需求不及预期、产能受限,
下游结论会受到多大影响?

核心思想:
  - 复用现有超图突触结构
  - 用突触 strength 作为因果传播强度
  - 对目标神经元做 do(shock) 冲击,沿突触传播 N 步
  - 输出受影响神经元及置信度变化

不引入外部因果推断库,全部基于现有 state_store 和 synapse 表。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImpactNode:
    """单个神经元在情景模拟中的受影响结果。"""

    neuron_id: str
    canonical_name: str
    node_type: str
    baseline_confidence: float
    simulated_confidence: float
    delta: float
    path: list[str] = field(default_factory=list)


@dataclass
class ScenarioReport:
    """情景模拟报告。"""

    target_neuron_id: str
    shock: float
    steps: int
    impacted: list[ImpactNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化 dict。"""
        return {
            "target_neuron_id": self.target_neuron_id,
            "shock": self.shock,
            "steps": self.steps,
            "impacted": [
                {
                    "neuron_id": n.neuron_id,
                    "canonical_name": n.canonical_name,
                    "node_type": n.node_type,
                    "baseline_confidence": n.baseline_confidence,
                    "simulated_confidence": n.simulated_confidence,
                    "delta": n.delta,
                    "path": list(n.path),
                }
                for n in self.impacted
            ],
        }


class ScenarioSimulator:
    """基于超图突触的情景模拟器。

    支持两种数据后端:
      1. NeuronStateStore:传入持久化存储实例
      2. 内存模式:直接传入 neurons 和 synapses 字典,便于上层模块无 state_store 时调用
    """

    def __init__(
        self,
        state_store: Any | None = None,
        neurons: dict[str, Any] | None = None,
        synapses: dict[str, Any] | None = None,
    ) -> None:
        """初始化。

        Args:
            state_store: NeuronStateStore 实例,可选。
            neurons: 内存模式下的神经元字典 {neuron_id: neuron}。
            synapses: 内存模式下的突触字典 {synapse_id: synapse}。
        """
        self._state_store = state_store
        self._neurons = neurons or {}
        self._synapses = synapses or {}
        self._memory_mode = state_store is None

    @classmethod
    def from_neurons_and_synapses(
        cls,
        neurons: dict[str, Any],
        synapses: dict[str, Any],
    ) -> "ScenarioSimulator":
        """从内存神经元和突触构造模拟器。

        Args:
            neurons: 神经元字典。
            synapses: 突触字典。

        Returns:
            ScenarioSimulator 内存模式实例。
        """
        return cls(neurons=neurons, synapses=synapses)

    def _get_neuron(self, neuron_id: str) -> Any | None:
        """按 ID 查询神经元,优先内存模式。"""
        if self._memory_mode:
            return self._neurons.get(neuron_id)
        if self._state_store is None:
            return None
        return self._state_store.get_neuron(neuron_id)

    def _get_synapses_by_neuron(self, neuron_id: str) -> list[Any]:
        """按神经元 ID 查询 incident 突触,优先内存模式。"""
        if self._memory_mode:
            result: list[Any] = []
            for syn in self._synapses.values():
                if (
                    getattr(syn, "source_neuron_id", "") == neuron_id
                    or getattr(syn, "target_neuron_id", "") == neuron_id
                ):
                    result.append(syn)
            return result
        if self._state_store is None:
            return []
        return self._state_store.get_synapses_by_neuron(neuron_id)

    def simulate_intervention(
        self,
        target_neuron_id: str,
        shock: float,
        steps: int = 2,
        min_delta: float = 0.01,
    ) -> ScenarioReport:
        """对目标神经元施加冲击,模拟沿突触传播的影响。

        例如:do(原材料涨价 20%) → 上游神经元 confidence +0.2,
        沿 UPSTREAM_OF 突触传播给下游,影响程度按突触 strength 衰减。

        Args:
            target_neuron_id: 施加冲击的神经元 ID。
            shock: 冲击量,范围 [-1, 1],正值表示利好,负值表示利空。
            steps: 最大传播步数。
            min_delta: 忽略小于该阈值的影响。

        Returns:
            ScenarioReport 情景报告。
        """
        report = ScenarioReport(
            target_neuron_id=target_neuron_id,
            shock=shock,
            steps=steps,
        )

        target = self._get_neuron(target_neuron_id)
        if target is None:
            return report

        baseline = getattr(target, "confidence", 0.0)
        # 目标神经元自身影响
        report.impacted.append(
            ImpactNode(
                neuron_id=target_neuron_id,
                canonical_name=target.canonical_name,
                node_type=target.node_type,
                baseline_confidence=baseline,
                simulated_confidence=max(0.0, min(1.0, baseline + shock)),
                delta=shock,
                path=[target_neuron_id],
            )
        )

        # BFS 沿突触传播
        deltas: dict[str, float] = {target_neuron_id: shock}
        visited: set[str] = {target_neuron_id}
        queue: list[tuple[str, float, list[str]]] = [
            (target_neuron_id, shock, [target_neuron_id])
        ]
        step = 0
        while queue and step < steps:
            next_queue: list[tuple[str, float, list[str]]] = []
            for current_id, current_delta, path in queue:
                synapses = self._get_synapses_by_neuron(current_id)
                for syn in synapses:
                    # 只沿 source→target 方向传播(从当前神经元作为 source 的突触)
                    if getattr(syn, "source_neuron_id", "") != current_id:
                        continue
                    next_id = getattr(syn, "target_neuron_id", "")
                    if not next_id:
                        continue
                    if next_id in visited:
                        continue
                    strength = getattr(syn, "strength", 0.0) or 0.0
                    # 衰减系数,避免过度传播
                    attenuation = 0.8
                    next_delta = current_delta * strength * attenuation
                    if abs(next_delta) < min_delta:
                        continue
                    visited.add(next_id)
                    deltas[next_id] = next_delta
                    next_path = path + [next_id]
                    next_queue.append((next_id, next_delta, next_path))
                    downstream = self._get_neuron(next_id)
                    if downstream is not None:
                        dc_base = getattr(downstream, "confidence", 0.0)
                        report.impacted.append(
                            ImpactNode(
                                neuron_id=next_id,
                                canonical_name=downstream.canonical_name,
                                node_type=downstream.node_type,
                                baseline_confidence=dc_base,
                                simulated_confidence=max(
                                    0.0, min(1.0, dc_base + next_delta)
                                ),
                                delta=next_delta,
                                path=next_path,
                            )
                        )
            queue = next_queue
            step += 1

        return report

    def run_stress_tests(
        self,
        target_neuron_id: str,
        scenarios: list[tuple[str, float, int]],
    ) -> dict[str, Any]:
        """批量运行多个压力情景。

        Args:
            target_neuron_id: 目标神经元 ID。
            scenarios: [(label, shock, steps), ...]

        Returns:
            dict,label -> ScenarioReport dict。
        """
        result: dict[str, Any] = {}
        for label, shock, steps in scenarios:
            report = self.simulate_intervention(target_neuron_id, shock, steps)
            result[label] = report.to_dict()
        return result
