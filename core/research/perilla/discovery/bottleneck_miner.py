"""Perilla Discovery Engine - 瓶颈矿工模块.

扫描动态供应链中的节点，检测瓶颈信号，通过六问评分体系
量化评估节点成为紫苏（关键瓶颈）的概率，生成瓶颈假设。
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List

from .models import (
    BOTTLENECK_STATUSES,
    SIX_QUESTION_KEYS,
    BottleneckHypothesis,
    DynamicSupplyChain,
    EarlySignal,
    SourceDocument,
    SupplyChainNode,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default

# 需要进行瓶颈检测的供应链层级
_BOTTLENECK_LAYERS: frozenset[str] = frozenset({
    "material", "equipment", "testing", "consumable", "component",
})

# 瓶颈信号关键词映射：关键词模式 → 信号描述
_SIGNAL_PATTERNS: List[tuple[re.Pattern[str], str]] = [
    (re.compile(r"供不应求|产能不足|交期延长"), "capacity"),
    (re.compile(r"认证周期|客户验证|送样"), "certification"),
    (re.compile(r"独家|唯一|寡头"), "concentration"),
    (re.compile(r"进口替代|国产替代"), "substitution"),
    (re.compile(r"良率|良品率"), "yield"),
    (re.compile(r"扩产|新增产能"), "expansion"),
]

# 六问评分默认值（信息不足时使用）
_DEFAULT_SCORES: Dict[str, int] = {
    "attach_rate": 3,
    "bom_share": 2,
    "supplier_concentration": 3,
    "capacity_expansion": 3,
    "switching_cost": 2,
    "pricing_state": 2,
}

# 信号类型 → 六问维度调整映射
_SIGNAL_SCORE_ADJUSTMENTS: Dict[str, Dict[str, int]] = {
    "capacity": {"capacity_expansion": 2, "pricing_state": 1},
    "certification": {"switching_cost": 2, "supplier_concentration": 1},
    "concentration": {"supplier_concentration": 2, "pricing_state": 1},
    "substitution": {"supplier_concentration": 1, "pricing_state": 1},
    "yield": {"capacity_expansion": 1, "attach_rate": 1},
    "expansion": {"capacity_expansion": 1},
}


class BottleneckMiner:
    """瓶颈矿工 - 在供应链中挖掘瓶颈节点.

    对供应链中 material/equipment/testing/consumable/component 层级的
    节点进行瓶颈信号检测、六问评分和分类，生成 BottleneckHypothesis。
    """

    def mine_bottlenecks(
        self,
        chain: DynamicSupplyChain,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> List[BottleneckHypothesis]:
        """挖掘供应链中的瓶颈节点，生成瓶颈假设列表.

        Args:
            chain: 动态供应链实例。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            瓶颈假设列表，仅包含可检测层级的节点。
        """
        nodes = _items(getattr(chain, "nodes", None))
        if not chain or not nodes:
            logger.warning("供应链为空或无节点，跳过瓶颈挖掘")
            return []

        source_docs = _items(source_docs)
        early_signals = _items(early_signals)

        hypotheses: List[BottleneckHypothesis] = []

        for node in nodes:
            if _text(getattr(node, "layer", "")) not in _BOTTLENECK_LAYERS:
                continue

            try:
                hypothesis = self._analyze_node(node, chain, source_docs, early_signals)
                if hypothesis is not None:
                    hypotheses.append(hypothesis)
            except Exception:
                logger.exception(
                    "分析节点 %s(%s) 时发生异常", node.node_id, node.name
                )

        logger.info(
            "供应链 %s 共 %d 个可检测节点，生成 %d 个瓶颈假设",
            _text(getattr(chain, "chain_id", "")),
            sum(
                1
                for n in nodes
                if _text(getattr(n, "layer", "")) in _BOTTLENECK_LAYERS
            ),
            len(hypotheses),
        )
        return hypotheses

    def _analyze_node(
        self,
        node: SupplyChainNode,
        chain: DynamicSupplyChain,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> BottleneckHypothesis:
        """分析单个节点，生成瓶颈假设.

        Args:
            node: 供应链节点。
            chain: 所属供应链。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            该节点的瓶颈假设。
        """
        # 1. 检测瓶颈信号
        signals = self._detect_bottleneck_signals(node, source_docs, early_signals)

        # 2. 六问评分
        scores, is_hypothesis = self._score_six_questions(node, signals, source_docs)

        # 3. 计算总分
        total_score = sum(scores.values())

        # 4. 分类瓶颈状态
        status = self._classify_bottleneck(total_score, is_hypothesis)

        # 5. 计算紫苏概率
        perilla_prob = self._compute_perilla_probability(scores, len(signals))

        # 6. 收集证据 ID
        evidence_ids = [
            item
            for item in _items(getattr(node, "evidence_ids", None))
            if isinstance(item, str)
        ]
        node_name = _text(getattr(node, "name", ""))
        for signal in early_signals:
            claim = _text(getattr(signal, "claim", ""))
            evidence_text = _text(getattr(signal, "evidence_text", ""))
            source_document_id = _text(getattr(signal, "source_document_id", ""))
            if node_name and (node_name in claim or node_name in evidence_text):
                if source_document_id and source_document_id not in evidence_ids:
                    evidence_ids.append(source_document_id)

        # 7. 判断是否需要验证
        needs_verification = is_hypothesis or not signals

        return BottleneckHypothesis(
            hypothesis_id=new_id("bh_"),
            concept_id=_text(getattr(chain, "concept_id", "")),
            chain_id=_text(getattr(chain, "chain_id", "")),
            node_id=_text(getattr(node, "node_id", "")),
            node_name=_text(getattr(node, "name", "")),
            bottleneck_signals=signals,
            six_question_scores=scores,
            total_score=total_score,
            perilla_probability=perilla_prob,
            evidence_ids=evidence_ids,
            is_hypothesis=is_hypothesis,
            needs_verification=needs_verification,
            status=status,
        )

    def _detect_bottleneck_signals(
        self,
        node: SupplyChainNode,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> List[str]:
        """扫描来源文档和早期信号，检测节点的瓶颈信号.

        在来源文档文本和早期信号声明中搜索与节点名称相关的
        瓶颈关键词，返回检测到的信号描述列表。

        Args:
            node: 供应链节点。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            检测到的瓶颈信号描述列表，如 ["capacity: 供不应求", ...]。
        """
        node_name = _text(getattr(node, "name", ""))
        if not node_name:
            return []

        signals: List[str] = []
        seen: set[str] = set()

        # 构建与该节点相关的文本语料
        corpus_texts: List[str] = []
        for doc in _items(source_docs):
            content = _text(getattr(doc, "content_text", ""))
            if node_name in content:
                corpus_texts.append(content)

        for es in _items(early_signals):
            claim = _text(getattr(es, "claim", ""))
            evidence_text = _text(getattr(es, "evidence_text", ""))
            if node_name in claim or node_name in evidence_text:
                if claim:
                    corpus_texts.append(claim)
                if evidence_text:
                    corpus_texts.append(evidence_text)

        # 在语料中搜索瓶颈信号模式
        for text in corpus_texts:
            for pattern, signal_type in _SIGNAL_PATTERNS:
                match = pattern.search(text)
                if match:
                    signal_key = f"{signal_type}:{match.group()}"
                    if signal_key not in seen:
                        seen.add(signal_key)
                        signals.append(f"{signal_type}: {match.group()}")

        return signals

    def _score_six_questions(
        self,
        node: SupplyChainNode,
        signals: List[str],
        source_docs: List[SourceDocument],
    ) -> tuple[Dict[str, int], bool]:
        """对节点进行六问评分.

        六个维度各 0-5 分：attach_rate, bom_share, supplier_concentration,
        capacity_expansion, switching_cost, pricing_state。
        信息不足时使用默认值；根据检测到的信号类型调整分数。

        Args:
            node: 供应链节点。
            signals: 检测到的瓶颈信号列表。
            source_docs: 来源文档列表。

        Returns:
            (scores, is_hypothesis) 元组：
            - scores: 六问评分字典，每项 0-5 整数。
            - is_hypothesis: 是否全部为假设评分（无直接证据）。
        """
        scores = dict(_DEFAULT_SCORES)
        has_direct_evidence = False

        # 解析信号类型
        signal_types: set[str] = set()
        for sig in _items(signals):
            if not isinstance(sig, str):
                continue
            # 信号格式为 "signal_type: keyword"
            parts = sig.split(":", 1)
            if parts:
                signal_types.add(parts[0].strip())

        # 根据信号类型调整评分
        for sig_type in signal_types:
            adjustments = _SIGNAL_SCORE_ADJUSTMENTS.get(sig_type, {})
            for dim, delta in adjustments.items():
                if dim in scores:
                    scores[dim] = min(5, scores[dim] + delta)
            has_direct_evidence = True

        # 如果节点自身标记为潜在瓶颈，提升供应商集中度和扩产难度
        if bool(getattr(node, "is_potential_bottleneck", False)):
            scores["supplier_concentration"] = min(5, scores["supplier_concentration"] + 1)
            scores["capacity_expansion"] = min(5, scores["capacity_expansion"] + 1)

        # 在来源文档中搜索直接证据关键词来微调
        node_name = _text(getattr(node, "name", ""))
        for doc in _items(source_docs):
            text = _text(getattr(doc, "content_text", ""))
            if not node_name or node_name not in text:
                continue
            # 如果有明确的份额/占比描述，提升 bom_share
            if re.search(r"\d+%|\d+成|占比|份额", text):
                scores["bom_share"] = min(5, scores["bom_share"] + 1)
                has_direct_evidence = True
            # 如果有明确的供应商数量描述，提升 supplier_concentration
            if re.search(r"仅\s*\d+|只有\s*\d+|前\s*\d+|CR\d|市占率", text):
                scores["supplier_concentration"] = min(5, scores["supplier_concentration"] + 1)
                has_direct_evidence = True

        # 判断是否全部为假设评分
        is_hypothesis = not has_direct_evidence

        return scores, is_hypothesis

    def _classify_bottleneck(self, total_score: int, is_hypothesis: bool) -> str:
        """根据总分和假设标记分类瓶颈状态.

        Args:
            total_score: 六问总分（0-30）。
            is_hypothesis: 是否为假设评分。

        Returns:
            瓶颈状态字符串，属于 BOTTLENECK_STATUSES 集合。
        """
        total_score = _finite(total_score)
        if total_score >= 20 and not bool(is_hypothesis):
            return "STRONG_BOTTLENECK"
        if total_score >= 15:
            return "POSSIBLE_BOTTLENECK"
        if total_score >= 10:
            return "WEAK_BOTTLENECK"
        return "HYPOTHESIS_ONLY"

    def _compute_perilla_probability(
        self, scores: Dict[str, int], signal_count: int
    ) -> float:
        """计算紫苏概率（节点成为关键瓶颈的概率）.

        基于六问评分的加权和与信号数量综合计算，归一化到 [0.0, 1.0]。

        六问权重分配：
        - supplier_concentration: 0.25（供应商集中度最关键）
        - capacity_expansion: 0.20（扩产难度次之）
        - switching_cost: 0.15（切换成本）
        - attach_rate: 0.15（绑定率）
        - pricing_state: 0.15（定价权）
        - bom_share: 0.10（BOM 份额）

        信号加成：每个信号 +0.03，上限 0.15。

        Args:
            scores: 六问评分字典，每项 0-5。
            signal_count: 检测到的瓶颈信号数量。

        Returns:
            紫苏概率，范围 [0.0, 1.0]。
        """
        weights: Dict[str, float] = {
            "supplier_concentration": 0.25,
            "capacity_expansion": 0.20,
            "switching_cost": 0.15,
            "attach_rate": 0.15,
            "pricing_state": 0.15,
            "bom_share": 0.10,
        }

        # 加权评分（每项 0-5，权重和为 1.0，最大加权分为 5.0）
        score_map = scores if isinstance(scores, Mapping) else {}
        weighted_sum = 0.0
        for dim, weight in weights.items():
            score = min(5.0, max(0.0, _finite(score_map.get(dim, 0))))
            weighted_sum += score * weight

        # 归一化到 [0, 1]：最大可能加权分为 5.0
        score_component = weighted_sum / 5.0

        # 信号加成
        signal_bonus = min(max(0.0, _finite(signal_count)) * 0.03, 0.15)

        probability = min(1.0, score_component + signal_bonus)
        return round(probability, 4)
