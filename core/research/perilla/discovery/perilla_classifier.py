"""Perilla Discovery Engine v1.25 - 分类层.

将公司候选、瓶颈假设和供应链信息融合，对每个候选进行分类，
生成 PerillaDiscoveryItem 列表。分类按优先级从高到低依次匹配。
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

from .models import (
    DISCOVERY_CLASSIFICATIONS,
    EVIDENCE_LEVEL_CLAIM_ONLY,
    EVIDENCE_LEVEL_CONFIRMED,
    EVIDENCE_LEVEL_EARLY_SIGNAL,
    EVIDENCE_LEVEL_HYPOTHESIS,
    EVIDENCE_LEVEL_MAIN_ANCHOR,
    EVIDENCE_LEVEL_NON_CORE,
    EVIDENCE_LEVEL_PROBABLE,
    EVIDENCE_LEVEL_RUMOR,
    MARKETS,
    REVIEW_STATUSES,
    SUPPLY_CHAIN_LAYERS,
    BottleneckHypothesis,
    CompanyCandidate,
    DynamicSupplyChain,
    PerillaDiscoveryItem,
    new_id,
)

logger = logging.getLogger(__name__)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default

# 非核心市场集合
_NON_CORE_MARKETS = {"HK", "JP", "TW", "KR", "EU", "OTHER"}

# 终端/模块层级集合 —— 属于链主/锚点层级
_ANCHOR_LAYERS = {"terminal", "module"}

# 概念标签类来源 —— 仅有概念标签而无实质供货证据
_CONCEPT_SOURCE_TYPES = {"concept_page", "index_page", "sector_page"}

# 早期信号类来源
_EARLY_SIGNAL_TYPES = {"recruitment", "expo", "tender", "rss", "change_event"}

# 谣言/论坛类来源
_RUMOR_SOURCE_TYPES = {"forum", "social", "rumor", "meeting_note"}

# 证据等级到概率的默认映射（无瓶颈假设时的降级估算）
_EVIDENCE_LEVEL_PROBABILITY: Dict[str, float] = {
    EVIDENCE_LEVEL_CONFIRMED: 0.85,
    EVIDENCE_LEVEL_PROBABLE: 0.60,
    EVIDENCE_LEVEL_MAIN_ANCHOR: 0.0,
    EVIDENCE_LEVEL_CLAIM_ONLY: 0.15,
    EVIDENCE_LEVEL_EARLY_SIGNAL: 0.20,
    EVIDENCE_LEVEL_RUMOR: 0.10,
    EVIDENCE_LEVEL_HYPOTHESIS: 0.05,
    EVIDENCE_LEVEL_NON_CORE: 0.0,
}


class PerillaClassifier:
    """紫苏分类器 —— 对公司候选进行分类并生成发现条目.

    分类按优先级从高到低依次匹配，首个命中规则决定分类结果。
    优先级顺序：
    1. MAIN_ANCHOR     - 终端/链主公司
    2. PERILLA_CANDIDATE - 紫苏候选（有瓶颈+强证据）
    3. CONCEPT_CLAIM_ONLY - 仅有概念标签
    4. EARLY_SIGNAL_WATCH - 早期信号观察
    5. RUMOR_WATCH     - 谣言观察
    6. HYPOTHESIS_ONLY - 纯假设
    7. NON_CORE_MARKET - 非核心市场
    8. NEEDS_HUMAN_REVIEW - 需人工审核（兜底）
    """

    def classify(
        self,
        candidates: List[CompanyCandidate],
        bottlenecks: List[BottleneckHypothesis],
        chain: DynamicSupplyChain,
    ) -> List[PerillaDiscoveryItem]:
        """对公司候选列表进行分类，生成发现条目.

        Args:
            candidates: 公司候选列表。
            bottlenecks: 瓶颈假设列表。
            chain: 动态供应链。

        Returns:
            分类后的 PerillaDiscoveryItem 列表。
        """
        if not isinstance(candidates, (list, tuple)) or not candidates:
            logger.warning("分类器收到空候选列表，返回空结果")
            return []

        # 构建瓶颈查找表：node_id -> BottleneckHypothesis
        bottleneck_by_node: Dict[str, BottleneckHypothesis] = {
            b.node_id: b
            for b in (bottlenecks if isinstance(bottlenecks, (list, tuple)) else [])
            if isinstance(b, BottleneckHypothesis) and b.node_id
        }

        # 构建节点查找表：node_id -> SupplyChainNode
        chain_nodes = getattr(chain, "nodes", ())
        node_by_id = {
            n.node_id: n for n in (chain_nodes if isinstance(chain_nodes, (list, tuple)) else [])
            if getattr(n, "node_id", "")
        }

        items: List[PerillaDiscoveryItem] = []
        for candidate in candidates:
            try:
                item = self._classify_one(candidate, bottleneck_by_node, node_by_id, chain)
                items.append(item)
            except Exception:
                logger.exception(
                    "分类候选 %s (%s) 时发生异常，降级为 NEEDS_HUMAN_REVIEW",
                    getattr(candidate, "candidate_id", ""),
                    getattr(candidate, "company_name", ""),
                )
                items.append(self._make_fallback_item(candidate))

        logger.info("分类完成: %d 个候选 -> %d 个条目", len(candidates), len(items))
        return items

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _classify_one(
        self,
        candidate: CompanyCandidate,
        bottleneck_by_node: Dict[str, BottleneckHypothesis],
        node_by_id: Dict,
        chain: DynamicSupplyChain,
    ) -> PerillaDiscoveryItem:
        """对单个候选执行分类逻辑.

        Args:
            candidate: 公司候选。
            bottleneck_by_node: 按节点 ID 索引的瓶颈假设字典。
            node_by_id: 按节点 ID 索引的供应链节点字典。
            chain: 动态供应链。

        Returns:
            分类后的 PerillaDiscoveryItem。
        """
        bottleneck = self._find_bottleneck_for_candidate(candidate, list(bottleneck_by_node.values()))
        chain_node = node_by_id.get(candidate.chain_node_id)
        perilla_prob = self._compute_perilla_probability_for_item(candidate, bottleneck)

        # 按优先级依次匹配分类规则
        classification = self._resolve_classification(
            candidate, bottleneck, chain_node, perilla_prob
        )

        # 判断是否需要人工审核
        needs_review = self._check_needs_human_review(candidate, classification, bottleneck)

        # 确定审核状态
        review_status = self._determine_review_status(
            classification, candidate.evidence_level, needs_review
        )

        # 构建风险标记
        risk_flags = self._build_risk_flags(candidate, classification, bottleneck)

        # 构建瓶颈逻辑说明
        bottleneck_logic = self._build_bottleneck_logic(bottleneck)

        # 构建证据摘要
        evidence_summary = self._build_evidence_summary(candidate, bottleneck)

        # 收集来源 URL
        source_urls = self._collect_source_urls(candidate, bottleneck)

        item = PerillaDiscoveryItem(
            item_id=new_id("pdi_"),
            concept=chain.concept_id,  # 由上层填充概念名称
            demand_anchor=chain.demand_anchor_id,
            chain_node=chain_node.name if chain_node else candidate.chain_node_id,
            company_name=candidate.company_name,
            symbol=candidate.symbol,
            market=candidate.market,
            classification=classification,
            evidence_level=candidate.evidence_level,
            perilla_probability=round(perilla_prob, 4),
            bottleneck_logic=bottleneck_logic,
            evidence_summary=evidence_summary,
            source_urls=source_urls,
            risk_flags=risk_flags,
            review_status=review_status,
        )
        return item

    def _resolve_classification(
        self,
        candidate: CompanyCandidate,
        bottleneck: Optional[BottleneckHypothesis],
        chain_node: Optional[object],
        perilla_prob: float,
    ) -> str:
        """按优先级解析分类标签.

        优先级顺序：MAIN_ANCHOR > PERILLA_CANDIDATE > CONCEPT_CLAIM_ONLY >
        EARLY_SIGNAL_WATCH > RUMOR_WATCH > HYPOTHESIS_ONLY > NON_CORE_MARKET >
        NEEDS_HUMAN_REVIEW。

        Args:
            candidate: 公司候选。
            bottleneck: 关联的瓶颈假设（可能为 None）。
            chain_node: 关联的供应链节点（可能为 None）。
            perilla_prob: 紫苏概率。

        Returns:
            分类标签字符串。
        """
        evidence = getattr(candidate, "evidence_level", "")
        sources = {
            source for source in (candidate.mapping_sources or [])
            if isinstance(source, str)
        }

        # 1. MAIN_ANCHOR: 终端/链主公司
        if evidence == EVIDENCE_LEVEL_MAIN_ANCHOR:
            return "MAIN_ANCHOR"
        if chain_node is not None and hasattr(chain_node, "layer"):
            if chain_node.layer in _ANCHOR_LAYERS and chain_node.is_anchor:
                return "MAIN_ANCHOR"

        # 2. PERILLA_CANDIDATE: 非终端/锚点层，有瓶颈假设，强证据
        is_anchor_layer = (
            chain_node is not None
            and hasattr(chain_node, "layer")
            and chain_node.layer in _ANCHOR_LAYERS
        )
        if not is_anchor_layer:
            has_bottleneck = bottleneck is not None
            strong_evidence = evidence in (EVIDENCE_LEVEL_CONFIRMED, EVIDENCE_LEVEL_PROBABLE)
            high_prob = perilla_prob >= 0.5
            high_bottleneck_score = bottleneck is not None and _safe_float(bottleneck.total_score) >= 15

            if has_bottleneck and strong_evidence and (high_prob or high_bottleneck_score):
                return "PERILLA_CANDIDATE"

        # 3. CONCEPT_CLAIM_ONLY: 仅有概念标签
        if evidence == EVIDENCE_LEVEL_CLAIM_ONLY:
            return "CONCEPT_CLAIM_ONLY"
        if sources & _CONCEPT_SOURCE_TYPES and evidence not in (
            EVIDENCE_LEVEL_CONFIRMED,
            EVIDENCE_LEVEL_PROBABLE,
            EVIDENCE_LEVEL_MAIN_ANCHOR,
        ):
            return "CONCEPT_CLAIM_ONLY"

        # 4. EARLY_SIGNAL_WATCH: 早期信号
        if evidence == EVIDENCE_LEVEL_EARLY_SIGNAL:
            return "EARLY_SIGNAL_WATCH"
        if sources & _EARLY_SIGNAL_TYPES and evidence not in (
            EVIDENCE_LEVEL_CONFIRMED,
            EVIDENCE_LEVEL_PROBABLE,
        ):
            return "EARLY_SIGNAL_WATCH"

        # 5. RUMOR_WATCH: 谣言/论坛
        if evidence == EVIDENCE_LEVEL_RUMOR:
            return "RUMOR_WATCH"
        if sources & _RUMOR_SOURCE_TYPES:
            return "RUMOR_WATCH"

        # 6. HYPOTHESIS_ONLY: 纯 AI 推理
        if evidence == EVIDENCE_LEVEL_HYPOTHESIS:
            return "HYPOTHESIS_ONLY"

        # 7. NON_CORE_MARKET: 非核心市场
        if getattr(candidate, "market", "") in _NON_CORE_MARKETS:
            return "NON_CORE_MARKET"

        # 8. NEEDS_HUMAN_REVIEW: 兜底
        return "NEEDS_HUMAN_REVIEW"

    def _find_bottleneck_for_candidate(
        self,
        candidate: CompanyCandidate,
        bottlenecks: List[BottleneckHypothesis],
    ) -> Optional[BottleneckHypothesis]:
        """通过 chain_node_id 查找候选关联的瓶颈假设.

        优先通过 candidate.bottleneck_hypothesis_id 精确匹配，
        其次通过 chain_node_id 关联匹配。

        Args:
            candidate: 公司候选。
            bottlenecks: 瓶颈假设列表。

        Returns:
            匹配的 BottleneckHypothesis，未找到返回 None。
        """
        # 精确匹配：通过 bottleneck_hypothesis_id
        if candidate.bottleneck_hypothesis_id:
            for b in bottlenecks:
                if b.hypothesis_id == candidate.bottleneck_hypothesis_id:
                    return b

        # 关联匹配：通过 chain_node_id
        if candidate.chain_node_id:
            for b in bottlenecks:
                if b.node_id == candidate.chain_node_id:
                    return b

        return None

    def _compute_perilla_probability_for_item(
        self,
        candidate: CompanyCandidate,
        bottleneck: Optional[BottleneckHypothesis],
    ) -> float:
        """计算紫苏概率.

        优先使用瓶颈假设的 perilla_probability，
        否则根据 evidence_level 降级估算。

        Args:
            candidate: 公司候选。
            bottleneck: 关联的瓶颈假设（可能为 None）。

        Returns:
            紫苏概率 [0, 1]。
        """
        # 优先使用瓶颈假设的概率
        if bottleneck is not None and _safe_float(bottleneck.perilla_probability) > 0:
            return min(max(_safe_float(bottleneck.perilla_probability), 0.0), 1.0)

        # 降级：根据证据等级估算
        prob = _EVIDENCE_LEVEL_PROBABILITY.get(getattr(candidate, "evidence_level", ""), 0.0)
        return prob

    def _determine_review_status(
        self,
        classification: str,
        evidence_level: str,
        needs_human_review: bool,
    ) -> str:
        """确定审核状态.

        规则：
        - PERILLA_CANDIDATE + CONFIRMED_SUPPLIER -> auto_pass
        - needs_human_review=True -> pending_human_review
        - RUMOR_WATCH / HYPOTHESIS_ONLY -> pending_human_review
        - 其他 -> pending_human_review（保守默认）

        Args:
            classification: 分类标签。
            evidence_level: 证据等级。
            needs_human_review: 是否需要人工审核。

        Returns:
            审核状态字符串。
        """
        # PERILLA_CANDIDATE 且有确认供货证据 -> 自动通过
        if classification == "PERILLA_CANDIDATE" and evidence_level == EVIDENCE_LEVEL_CONFIRMED:
            return "auto_pass"

        # 明确标记需要人工审核
        if needs_human_review:
            return "pending_human_review"

        # 谣言和纯假设默认需要审核
        if classification in ("RUMOR_WATCH", "HYPOTHESIS_ONLY"):
            return "pending_human_review"

        # 保守默认：需要人工审核
        return "pending_human_review"

    def _check_needs_human_review(
        self,
        candidate: CompanyCandidate,
        classification: str,
        bottleneck: Optional[BottleneckHypothesis],
    ) -> bool:
        """检查是否需要人工审核.

        触发条件：
        - 候选本身标记 needs_human_review
        - 证据冲突（多个来源给出矛盾信号）
        - 市场/代码无法解析
        - 证据弱但潜在重要性高

        Args:
            candidate: 公司候选。
            classification: 分类标签。
            bottleneck: 关联的瓶颈假设。

        Returns:
            是否需要人工审核。
        """
        # 候选本身标记
        if getattr(candidate, "needs_human_review", False):
            return True

        # 市场/代码无法解析
        if getattr(candidate, "market", "") in ("UNKNOWN", "") or getattr(candidate, "symbol", None) is None:
            return True

        # 高潜力但证据弱
        if classification == "PERILLA_CANDIDATE" and candidate.evidence_level == EVIDENCE_LEVEL_PROBABLE:
            return True

        # 瓶颈假设需要验证
        if bottleneck is not None and bottleneck.needs_verification:
            return True

        return False

    def _build_risk_flags(
        self,
        candidate: CompanyCandidate,
        classification: str,
        bottleneck: Optional[BottleneckHypothesis],
    ) -> List[str]:
        """构建风险标记列表.

        Args:
            candidate: 公司候选。
            classification: 分类标签。
            bottleneck: 关联的瓶颈假设。

        Returns:
            风险标记字符串列表。
        """
        flags: List[str] = []

        if getattr(candidate, "market", "") in _NON_CORE_MARKETS:
            flags.append(f"非核心市场({getattr(candidate, 'market', '')})")

        if getattr(candidate, "symbol", None) is None:
            flags.append("缺少证券代码")

        if candidate.evidence_level in (EVIDENCE_LEVEL_RUMOR, EVIDENCE_LEVEL_HYPOTHESIS):
            flags.append("证据来源不可靠")

        if classification == "CONCEPT_CLAIM_ONLY":
            flags.append("仅概念标签，无供货证据")

        if bottleneck is not None and bottleneck.is_hypothesis:
            flags.append("瓶颈为假设，未充分验证")

        # 证据冲突检测：mapping_sources 中同时出现矛盾来源类型
        sources = {
            source for source in (candidate.mapping_sources or [])
            if isinstance(source, str)
        }
        has_reliable = bool(sources & {"official_site", "announcement", "report"})
        has_unreliable = bool(sources & _RUMOR_SOURCE_TYPES)
        if has_reliable and has_unreliable:
            flags.append("证据来源冲突")

        return flags

    def _build_bottleneck_logic(self, bottleneck: Optional[BottleneckHypothesis]) -> str:
        """构建瓶颈逻辑说明文本.

        Args:
            bottleneck: 瓶颈假设（可能为 None）。

        Returns:
            瓶颈逻辑说明字符串。
        """
        if bottleneck is None:
            return ""

        parts: List[str] = []
        if bottleneck.status:
            parts.append(f"瓶颈状态: {bottleneck.status}")
        if bottleneck.total_score > 0:
            parts.append(f"六问总分: {bottleneck.total_score}/60")
        if bottleneck.perilla_probability > 0:
            parts.append(f"紫苏概率: {bottleneck.perilla_probability:.1%}")

        # 添加高分维度
        high_dims = [
            f"{k}={v}" for k, v in bottleneck.six_question_scores.items() if v >= 7
        ]
        if high_dims:
            parts.append(f"高分维度: {', '.join(high_dims)}")

        if bottleneck.bottleneck_signals:
            parts.append(f"瓶颈信号: {'; '.join(bottleneck.bottleneck_signals[:3])}")

        return " | ".join(parts)

    def _build_evidence_summary(
        self,
        candidate: CompanyCandidate,
        bottleneck: Optional[BottleneckHypothesis],
    ) -> str:
        """构建证据摘要文本.

        Args:
            candidate: 公司候选。
            bottleneck: 关联的瓶颈假设。

        Returns:
            证据摘要字符串。
        """
        parts: List[str] = []

        if candidate.claim_summary:
            parts.append(candidate.claim_summary)

        if candidate.mapping_sources:
            parts.append(f"来源: {', '.join(candidate.mapping_sources[:5])}")

        if candidate.evidence_level:
            parts.append(f"证据等级: {candidate.evidence_level}")

        return " | ".join(parts) if parts else "暂无本地证据"

    def _collect_source_urls(
        self,
        candidate: CompanyCandidate,
        bottleneck: Optional[BottleneckHypothesis],
    ) -> List[str]:
        """收集来源 URL.

        当前从候选和瓶颈的证据 ID 中收集。
        实际 URL 需要上层通过 SourceDocument 查找填充。

        Args:
            candidate: 公司候选。
            bottleneck: 关联的瓶颈假设。

        Returns:
            来源 URL 列表（当前返回证据 ID 列表作为占位）。
        """
        urls: List[str] = []
        # 证据 ID 作为占位，实际 URL 由报告层替换
        for eid in candidate.evidence_ids[:5]:
            urls.append(eid)
        if bottleneck is not None:
            for eid in bottleneck.evidence_ids[:3]:
                if eid not in urls:
                    urls.append(eid)
        return urls

    def _make_fallback_item(self, candidate: CompanyCandidate) -> PerillaDiscoveryItem:
        """为分类失败的候选生成降级条目.

        Args:
            candidate: 公司候选。

        Returns:
            降级为 NEEDS_HUMAN_REVIEW 的 PerillaDiscoveryItem。
        """
        return PerillaDiscoveryItem(
            item_id=new_id("pdi_"),
            company_name=getattr(candidate, "company_name", ""),
            symbol=getattr(candidate, "symbol", None),
            market=getattr(candidate, "market", "UNKNOWN"),
            classification="NEEDS_HUMAN_REVIEW",
            evidence_level=getattr(candidate, "evidence_level", "INSUFFICIENT_EVIDENCE"),
            perilla_probability=0.0,
            bottleneck_logic="分类异常，需人工审核",
            evidence_summary=getattr(candidate, "claim_summary", "") or "分类过程异常",
            risk_flags=["分类异常"],
            review_status="pending_human_review",
        )
