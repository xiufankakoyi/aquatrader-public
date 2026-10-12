"""跨主题/跨行业动态事件传播。

解决痛点 5:
  缺失"跨主题/跨行业联动"——能从一个主题的新证据自动激活相邻主题的研究。

发现连接的方式(全部基于规则与统计,不引入外部 ML):
  1. 共享实体:两个主题的神经元 canonical_name 或 aliases 有重叠
  2. 图谱突触:两个主题的神经元之间存在 EXISTING_NEURON 突触
  3. 名称相似:同 node_type 且名称 Jaccard 相似度 >= 0.5

激活策略:
  - 仅当源神经元被新证据命中且自身置信度足够(confidence >= threshold)时才传播
  - 目标神经元若置信度较低(confidence <= cap)或处于低代谢状态,
    则被激活并生成 REQUEST_GROWTH_MINING pulse
  - 所有连接强度低于 min_link_strength 的链接被忽略,避免噪声扩散
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrossThemeLink:
    """跨主题连接描述。"""

    source_theme: str
    target_theme: str
    source_neuron_id: str
    target_neuron_id: str
    link_type: str  # shared_entity / synapse_bridge / similar_name
    strength: float  # 0.0-1.0
    reason: str = ""


@dataclass
class ThemeGraph:
    """主题关联图。"""

    theme_to_neurons: dict[str, set[str]] = field(default_factory=dict)
    links: list[CrossThemeLink] = field(default_factory=list)

    def related_themes(self, theme: str) -> set[str]:
        """返回与指定主题相关的其他主题集合。"""
        related: set[str] = set()
        for link in self.links:
            if link.source_theme == theme:
                related.add(link.target_theme)
            elif link.target_theme == theme:
                related.add(link.source_theme)
        return related


@dataclass
class PropagationResult:
    """跨主题传播结果,由调用方(environment)统一持久化。"""

    activated_neurons: list[Any] = field(default_factory=list)
    emitted_pulses: list[Any] = field(default_factory=list)
    links_used: list[CrossThemeLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化 dict。"""
        return {
            "activated_neuron_count": len(self.activated_neurons),
            "emitted_pulse_count": len(self.emitted_pulses),
            "links_used": [
                {
                    "source_theme": link.source_theme,
                    "target_theme": link.target_theme,
                    "source_neuron_id": link.source_neuron_id,
                    "target_neuron_id": link.target_neuron_id,
                    "link_type": link.link_type,
                    "strength": link.strength,
                    "reason": link.reason,
                }
                for link in self.links_used
            ],
        }


class DynamicEventPropagation:
    """跨主题事件传播器。

    基于规则发现主题间连接,并在新证据到达时激活相邻主题的低置信度神经元。
    """

    def __init__(
        self,
        confidence_threshold: float = 0.5,
        activation_confidence_cap: float = 0.6,
        max_propagation_links: int = 20,
        min_link_strength: float = 0.3,
    ) -> None:
        """初始化。

        Args:
            confidence_threshold: 源神经元触发跨主题传播的最低置信度。
            activation_confidence_cap: 目标神经元被激活的置信度上限。
            max_propagation_links: 主题图保留的最大连接数,按强度截断。
            min_link_strength: 可传播的最小连接强度。
        """
        self._confidence_threshold = confidence_threshold
        self._activation_confidence_cap = activation_confidence_cap
        self._max_propagation_links = max_propagation_links
        self._min_link_strength = min_link_strength
        self._theme_graph: ThemeGraph | None = None

    def build_theme_graph(
        self,
        neurons: list[Any],
        synapses: list[Any],
    ) -> ThemeGraph:
        """从神经元和突触构建主题关联图。

        连接发现规则:
          1. 共享实体:两个主题的神经元 canonical_name 或 aliases 有重叠
          2. 名称相似:同 node_type 且名称 Jaccard 相似度 >= 0.5
          3. 突触桥接:两个主题的神经元之间存在 EXISTING_NEURON 突触

        Args:
            neurons: 非 tombstone 的 ResearchNeuron 列表。
            synapses: ResearchSynapse 列表。

        Returns:
            ThemeGraph 主题关联图。
        """
        theme_to_neurons: dict[str, set[str]] = {}
        neuron_by_id: dict[str, Any] = {}
        for neuron in neurons:
            if getattr(neuron, "is_tombstone", False):
                continue
            theme = getattr(neuron, "root_theme", "") or "unknown"
            theme_to_neurons.setdefault(theme, set()).add(neuron.neuron_id)
            neuron_by_id[neuron.neuron_id] = neuron

        links: list[CrossThemeLink] = []
        seen_pairs: set[tuple[str, str, str, str]] = set()

        themes = list(theme_to_neurons.keys())
        for i, theme_a in enumerate(themes):
            for theme_b in themes[i + 1 :]:
                if theme_a == theme_b:
                    continue
                for nid_a in theme_to_neurons[theme_a]:
                    neuron_a = neuron_by_id[nid_a]
                    names_a = {neuron_a.canonical_name.lower()}
                    names_a.update(a.lower().strip() for a in neuron_a.aliases if a)
                    for nid_b in theme_to_neurons[theme_b]:
                        neuron_b = neuron_by_id[nid_b]
                        names_b = {neuron_b.canonical_name.lower()}
                        names_b.update(a.lower().strip() for a in neuron_b.aliases if a)

                        shared = names_a & names_b
                        if shared:
                            for name in shared:
                                pair = (theme_a, theme_b, nid_a, nid_b)
                                if pair in seen_pairs:
                                    continue
                                seen_pairs.add(pair)
                                links.append(
                                    CrossThemeLink(
                                        source_theme=theme_a,
                                        target_theme=theme_b,
                                        source_neuron_id=nid_a,
                                        target_neuron_id=nid_b,
                                        link_type="shared_entity",
                                        strength=0.9,
                                        reason=f"共享实体:{name}",
                                    )
                                )
                            continue

                        if neuron_a.node_type == neuron_b.node_type:
                            sim = self._name_jaccard(
                                neuron_a.canonical_name, neuron_b.canonical_name
                            )
                            if sim >= 0.5:
                                pair = (theme_a, theme_b, nid_a, nid_b)
                                if pair in seen_pairs:
                                    continue
                                seen_pairs.add(pair)
                                links.append(
                                    CrossThemeLink(
                                        source_theme=theme_a,
                                        target_theme=theme_b,
                                        source_neuron_id=nid_a,
                                        target_neuron_id=nid_b,
                                        link_type="similar_name",
                                        strength=round(sim, 4),
                                        reason=(
                                            f"同类型相似名称:{neuron_a.canonical_name} "
                                            f"~ {neuron_b.canonical_name}"
                                        ),
                                    )
                                )

        for syn in synapses:
            if getattr(syn, "status", "") in ("PRUNED",):
                continue
            if getattr(syn, "target_kind", "") != "EXISTING_NEURON" or not getattr(
                syn, "target_neuron_id", None
            ):
                continue
            src = neuron_by_id.get(syn.source_neuron_id)
            tgt = neuron_by_id.get(syn.target_neuron_id)
            if src is None or tgt is None:
                continue
            src_theme = getattr(src, "root_theme", "") or "unknown"
            tgt_theme = getattr(tgt, "root_theme", "") or "unknown"
            if src_theme == tgt_theme:
                continue
            pair = (src_theme, tgt_theme, src.neuron_id, tgt.neuron_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            strength = max(0.3, min(1.0, float(getattr(syn, "strength", 0.5))))
            links.append(
                CrossThemeLink(
                    source_theme=src_theme,
                    target_theme=tgt_theme,
                    source_neuron_id=src.neuron_id,
                    target_neuron_id=tgt.neuron_id,
                    link_type="synapse_bridge",
                    strength=round(strength, 4),
                    reason=f"突触连接:{syn.direction}({syn.relation})",
                )
            )

        links.sort(key=lambda link: link.strength, reverse=True)
        links = links[: self._max_propagation_links]

        self._theme_graph = ThemeGraph(theme_to_neurons=theme_to_neurons, links=links)
        return self._theme_graph

    def propagate(
        self,
        source_neuron_ids: list[str],
        neurons: list[Any],
        synapses: list[Any],
    ) -> PropagationResult:
        """根据命中的源神经元,计算需要激活的相邻主题神经元和 pulse。

        传播条件:
          - 源神经元 confidence >= confidence_threshold(自身证据充足才传播)
          - 目标神经元 confidence <= activation_confidence_cap 或处于低代谢状态
          - 连接强度 >= min_link_strength
          - 目标神经元未被 tombstone

        Args:
            source_neuron_ids: 本次被新证据命中的源神经元 ID 列表。
            neurons: 当前超图中所有非 tombstone 神经元。
            synapses: 当前超图中所有非 PRUNED 突触。

        Returns:
            PropagationResult,含待激活神经元和待发送 pulse。
        """
        from core.research_graph.neuron.models import GrowthPulse

        result = PropagationResult()
        if not source_neuron_ids:
            return result

        if self._theme_graph is None:
            self.build_theme_graph(neurons, synapses)

        neuron_by_id = {
            n.neuron_id: n for n in neurons if not getattr(n, "is_tombstone", False)
        }
        source_neurons = [
            neuron_by_id[nid] for nid in source_neuron_ids if nid in neuron_by_id
        ]

        for src in source_neurons:
            src_conf = float(getattr(src, "confidence", 0.0))
            if src_conf < self._confidence_threshold:
                continue

            src_theme = getattr(src, "root_theme", "") or "unknown"
            for link in self._theme_graph.links:
                if link.strength < self._min_link_strength:
                    continue
                if (
                    link.source_neuron_id != src.neuron_id
                    and link.target_neuron_id != src.neuron_id
                ):
                    continue

                if link.source_neuron_id == src.neuron_id:
                    target_id = link.target_neuron_id
                else:
                    target_id = link.source_neuron_id

                if target_id == src.neuron_id:
                    continue

                target = neuron_by_id.get(target_id)
                if target is None or getattr(target, "is_tombstone", False):
                    continue

                target_theme = getattr(target, "root_theme", "") or "unknown"
                if target_theme == src_theme:
                    continue

                target_conf = float(getattr(target, "confidence", 0.0))
                low_confidence = target_conf <= self._activation_confidence_cap
                low_metabolism = target.metabolism_state in (
                    "DORMANT",
                    "WEAKENING",
                    "DECAYING",
                )
                if not (low_confidence or low_metabolism):
                    continue

                target.metabolism_state = "ACTIVE"
                pulse = GrowthPulse(
                    pulse_id=f"pulse_cross_theme_{src.neuron_id}_{target_id}",
                    emitter_neuron_id=src.neuron_id,
                    pulse_type="REQUEST_GROWTH_MINING",
                    root_theme=target_theme,
                    reason=f"跨主题联动:{link.reason}",
                    priority=int(50 + link.strength * 30),
                    suggested_queries=[f"{target.canonical_name} 最新进展"],
                    payload={
                        "topic": target.canonical_name,
                        "node": target.neuron_id,
                        "cross_theme_source": src_theme,
                        "cross_theme_link_type": link.link_type,
                        "trigger_neuron_id": src.neuron_id,
                        "expected_gain": round(link.strength, 4),
                    },
                )

                result.activated_neurons.append(target)
                result.emitted_pulses.append(pulse)
                result.links_used.append(link)

        # 按神经元 ID 去重,保留第一个
        unique_activated: dict[str, Any] = {}
        unique_pulses: list[Any] = []
        for neuron, pulse in zip(result.activated_neurons, result.emitted_pulses):
            if neuron.neuron_id not in unique_activated:
                unique_activated[neuron.neuron_id] = neuron
                unique_pulses.append(pulse)
        result.activated_neurons = list(unique_activated.values())
        result.emitted_pulses = unique_pulses
        return result

    @staticmethod
    def _name_jaccard(a: str, b: str) -> float:
        """计算两个名称的字符级 Jaccard 相似度。

        Args:
            a: 名称 A。
            b: 名称 B。

        Returns:
            相似度,范围 [0.0, 1.0]。
        """
        if not a or not b:
            return 0.0
        tokens_a = set(a.lower())
        tokens_b = set(b.lower())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        return intersection / union if union else 0.0
