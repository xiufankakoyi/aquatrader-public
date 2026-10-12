"""产业逻辑到公司盈利/估值的桥接。

解决差距 7：valuation_bridge.py 是占位符，产业逻辑到公司盈利的连接没有实现。

核心能力：
  1. 将产业节点（材料/设备/产品/工艺）映射到相关公司
  2. 基于图谱连接强度和证据内容，估算公司收入暴露度
  3. 基于供需/价格信号，推断毛利率影响方向
  4. 输出结构化推理链，供下游估值模型使用

约束：本模块不输出具体买入/卖出建议。

重要边界：本模块的 revenue_exposure 是基于图连接强度和证据数量的启发式推断，
只能标记为 exploration_hypothesis，禁止进入正式 EarningsImpactResult。
正式盈利测算必须使用 EarningsImpactEngine + 财报分部/公司披露/人工审核的 RevenueExposure。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValuationSignal:
    """产业逻辑到估值的映射信号。

    重要：revenue_exposure 是探索假设，不是正式盈利测算输入。
    is_exploration_hypothesis 始终为 True，硬阻断进入 EarningsImpactResult。

    Attributes:
        neuron_id: 产业节点 ID。
        company_symbol: 公司代码或名称。
        revenue_exposure: 收入暴露度 0.0-1.0（探索假设，非正式）。
        margin_direction: 毛利率影响方向 positive/negative/neutral。
        confidence: 信号置信度 0.0-1.0。
        reasoning: 结构化推理链文本。
        evidence_summary: 支撑证据摘要。
        is_exploration_hypothesis: 是否为探索假设（始终 True，硬阻断进入正式结果）。
    """

    neuron_id: str
    company_symbol: str = ""
    revenue_exposure: float = 0.0
    margin_direction: str = "neutral"
    confidence: float = 0.0
    reasoning: str = ""
    evidence_summary: str = ""
    is_exploration_hypothesis: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "neuron_id": self.neuron_id,
            "company_symbol": self.company_symbol,
            "revenue_exposure": round(self.revenue_exposure, 4),
            "margin_direction": self.margin_direction,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "evidence_summary": self.evidence_summary,
            "is_exploration_hypothesis": self.is_exploration_hypothesis,
        }


@dataclass
class ValuationBridgeReport:
    """估值桥接报告。

    所有信号均为探索假设，不可作为正式盈利测算输入。
    """

    signals: list[ValuationSignal] = field(default_factory=list)
    placeholder: bool = False
    is_exploration_hypothesis: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化 dict。"""
        return {
            "signals": [s.to_dict() for s in self.signals],
            "placeholder": self.placeholder,
            "signal_count": len(self.signals),
            "is_exploration_hypothesis": self.is_exploration_hypothesis,
            "warning": "本报告为探索假设，不可作为正式 EarningsImpactResult 的输入",
        }


class ValuationBridge:
    """产业逻辑到公司估值/盈利预测的桥接器。"""

    # 正面毛利率信号词
    _POSITIVE_MARGIN_WORDS: set[str] = {
        "涨价", "提价", "供不应求", "产能利用率", "满产", "订单饱满",
        "国产替代", "份额提升", "下游需求旺盛", "价格上涨",
    }

    # 负面毛利率信号词
    _NEGATIVE_MARGIN_WORDS: set[str] = {
        "降价", "价格下跌", "供过于求", "产能过剩", "原材料涨价",
        "成本上升", "毛利率下降", "亏损", "价格战",
    }

    def __init__(
        self,
        entity_resolver: Any | None = None,
        kb: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            entity_resolver: EntityResolver 实例，用于识别公司节点。
            kb: SAGKnowledgeBase 实例，用于遍历图谱边。
        """
        self._entity_resolver = entity_resolver
        self._kb = kb
        self._enabled = True

    def analyze(
        self,
        neuron: Any,
        evidence_list: list[Any],
    ) -> ValuationBridgeReport:
        """分析神经元对公司估值的潜在影响。

        Args:
            neuron: ResearchNeuron 实例或兼容对象。
            evidence_list: 关联 Evidence 列表。

        Returns:
            ValuationBridgeReport，含一个或多个 ValuationSignal。
        """
        if neuron is None:
            return ValuationBridgeReport(placeholder=True)

        neuron_id = getattr(neuron, "neuron_id", "")
        neuron_name = getattr(neuron, "canonical_name", "")
        node_type = getattr(neuron, "node_type", "")

        signals: list[ValuationSignal] = []
        margin_direction = self._infer_margin_direction(evidence_list)
        evidence_summary = self._summarize_evidence(evidence_list)

        if self._is_company_node(neuron_name):
            # 直接分析公司本身
            exposure = self._company_direct_exposure(neuron, evidence_list)
            signals.append(
                ValuationSignal(
                    neuron_id=neuron_id,
                    company_symbol=neuron_name,
                    revenue_exposure=round(exposure, 4),
                    margin_direction=margin_direction,
                    confidence=round(min(1.0, 0.5 + 0.1 * len(evidence_list)), 4),
                    reasoning=f"{neuron_name} 为直接分析对象，收入暴露度={exposure:.2f}",
                    evidence_summary=evidence_summary,
                )
            )
        else:
            # 产业节点：找相关公司并估算暴露度
            company_links = self._find_related_companies(neuron)
            for company_name, strength in company_links:
                exposure = self._exposure_from_strength(strength, evidence_list)
                signals.append(
                    ValuationSignal(
                        neuron_id=neuron_id,
                        company_symbol=company_name,
                        revenue_exposure=round(exposure, 4),
                        margin_direction=margin_direction,
                        confidence=round(min(1.0, strength * (0.6 + 0.05 * len(evidence_list))), 4),
                        reasoning=(
                            f"{neuron_name}({node_type}) 通过产业链关系连接到 {company_name}, "
                            f"连接强度={strength:.2f}, 推断收入暴露度={exposure:.2f}"
                        ),
                        evidence_summary=evidence_summary,
                    )
                )

        if not signals:
            signals.append(
                ValuationSignal(
                    neuron_id=neuron_id,
                    reasoning=f"未找到 {neuron_name} 到公司的明确映射",
                    evidence_summary=evidence_summary,
                )
            )

        return ValuationBridgeReport(signals=signals, placeholder=False)

    def is_enabled(self) -> bool:
        """是否启用估值桥接。"""
        return self._enabled

    def _is_company_node(self, name: str) -> bool:
        """判断名称是否为公司。"""
        if self._entity_resolver is not None:
            return self._entity_resolver.is_company(name)
        # 简单启发式兜底：强公司标识词或在末尾的泛标识词
        strong_markers = ("股份", "有限公司", "集团", "公司")
        if any(s in name for s in strong_markers):
            return True
        weak_end_markers = ("科技", "电子", "材料")
        for suffix in weak_end_markers:
            if name.endswith(suffix):
                return True
        return False

    def _find_related_companies(self, neuron: Any) -> list[tuple[str, float]]:
        """找到与产业节点相关的公司及其连接强度。

        优先使用 KB 边，否则使用突触列表。
        """
        neuron_id = getattr(neuron, "neuron_id", "")
        neuron_name = getattr(neuron, "canonical_name", "")
        if not neuron_id:
            return []

        results: list[tuple[str, float]] = []
        seen: set[str] = set()

        # 1. 通过 KB edges
        if self._kb is not None:
            edges = getattr(self._kb, "edges", {})
            for edge in edges.values():
                head = str(getattr(edge, "head", ""))
                tail = str(getattr(edge, "tail", ""))
                if neuron_name not in (head, tail):
                    continue
                other = tail if head == neuron_name else head
                if not self._is_company_node(other):
                    continue
                if other in seen:
                    continue
                seen.add(other)
                strength = getattr(edge, "confidence", 0.5)
                results.append((other, float(strength)))

        # 2. 通过突触列表
        synapses = getattr(neuron, "synapses", []) or []
        if not synapses and hasattr(neuron, "incoming_synapses"):
            synapses = list(neuron.incoming_synapses) + list(neuron.outgoing_synapses)
        for syn in synapses:
            if getattr(syn, "status", "") in ("PRUNED",):
                continue
            src_id = getattr(syn, "source_neuron_id", "")
            tgt_id = getattr(syn, "target_neuron_id", "")
            if src_id != neuron_id and tgt_id != neuron_id:
                continue
            other_id = tgt_id if src_id == neuron_id else src_id
            other = None
            if self._kb is not None:
                other = self._kb.nodes.get(other_id)
            if other is None:
                continue
            other_name = getattr(other, "canonical_name", other_id)
            if not self._is_company_node(other_name):
                continue
            if other_name in seen:
                continue
            seen.add(other_name)
            strength = max(0.1, min(1.0, float(getattr(syn, "strength", 0.5))))
            results.append((other_name, strength))

        # 3. 兜底：若节点名本身就是公司，返回自身
        if self._is_company_node(neuron_name) and neuron_name not in seen:
            results.append((neuron_name, 1.0))

        return sorted(results, key=lambda x: x[1], reverse=True)[:20]

    def _company_direct_exposure(
        self,
        neuron: Any,
        evidence_list: list[Any],
    ) -> float:
        """估算公司节点的直接收入暴露度。"""
        base = 0.5
        # 证据越多、质量越高，暴露度越接近 1
        for ev in evidence_list[:5]:
            quality = float(getattr(ev, "quality_score", 0.5) or 0.5)
            base += 0.1 * quality
        return min(1.0, base)

    def _exposure_from_strength(
        self,
        strength: float,
        evidence_list: list[Any],
    ) -> float:
        """基于连接强度和证据数量估算收入暴露度。"""
        evidence_boost = min(0.3, 0.05 * len(evidence_list))
        return max(0.0, min(1.0, strength * 0.8 + evidence_boost))

    def _infer_margin_direction(self, evidence_list: list[Any]) -> str:
        """从证据文本推断毛利率影响方向。"""
        if not evidence_list:
            return "neutral"

        def _extract_text(ev: Any) -> str:
            if isinstance(ev, dict):
                return str(ev.get("evidence", "") or ev.get("evidence_text", "") or ev.get("summary", ""))
            return str(getattr(ev, "evidence_text", "") or getattr(ev, "evidence", "") or getattr(ev, "summary", ""))

        positive = 0
        negative = 0
        text = " ".join(_extract_text(ev) for ev in evidence_list)

        for word in self._POSITIVE_MARGIN_WORDS:
            if word in text:
                positive += 1
        for word in self._NEGATIVE_MARGIN_WORDS:
            if word in text:
                negative += 1

        if positive > negative:
            return "positive"
        if negative > positive:
            return "negative"
        return "neutral"

    def _summarize_evidence(self, evidence_list: list[Any]) -> str:
        """生成证据摘要。"""
        if not evidence_list:
            return "无证据"
        summaries: list[str] = []
        for ev in evidence_list[:3]:
            if isinstance(ev, dict):
                text = str(ev.get("evidence", "") or ev.get("evidence_text", "") or ev.get("summary", ""))
            else:
                text = str(getattr(ev, "evidence_text", "") or getattr(ev, "evidence", "") or getattr(ev, "summary", ""))
            summaries.append(text[:60])
        return "; ".join(summaries) or "证据摘要为空"
