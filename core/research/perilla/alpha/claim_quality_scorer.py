"""Claim 质量评分模块。

将 claim 的可信度拆分为可解释、可测试的多个维度，
供 ClaimPromotionEngine 组合成最终 claim_score。
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    ClaimScoreComponents,
    NodeType,
    RelationType,
    get_source_weight,
)
from core.research.perilla.alpha.sag_knowledge_base import Claim, SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_get(store: Any, key: Any) -> Any:
    if not isinstance(store, Mapping):
        return None
    try:
        return store.get(key)
    except (TypeError, ValueError):
        return None

# 营销语/过度概括/无意义修饰词
MARKETING_WORDS: frozenset[str] = frozenset([
    "引领", "颠覆", "革命性", "突破性", "重磅", "史诗级", "超级", "顶级",
    "首选", "最佳", "最强", "独一无二", "无可替代", "遥遥领先",
])

OVERGENERALIZED_WORDS: frozenset[str] = frozenset([
    "产业链", "上游", "下游", "中游", "相关企业", "主要玩家", "行业空间",
    "市场规模", "技术路线", "发展趋势", "市场需求", "应用场景",
    "解决方案", "核心材料", "核心部件",
])

# 关系词到 relation 的映射，用于 relation_clarity
RELATION_KEYWORDS: dict[str, list[str]] = {
    RelationType.MADE_OF.value: ["由", "采用", "使用", "包含", "内有", "构成", "材料", "部件"],
    RelationType.SUPPLIED_BY.value: ["供应商", "供货", "供应", "采购自", "来自", "提供"],
    RelationType.IMPORT_DEPENDENT_ON.value: ["进口", "海外", "依赖进口", "海外依赖", "受制于人"],
    RelationType.SUBSTITUTE_FOR.value: ["替代", "国产替代", "进口替代"],
    RelationType.BOTTLENECK_OF.value: ["瓶颈", "卡脖子", "难以替代", "核心难点", "关键制约"],
    RelationType.CONSTRAINED_BY.value: ["受限于", "受制于", "依赖", "短缺"],
    RelationType.PROCESSED_BY.value: ["加工", "制造", "生产", "工艺", "制程"],
    RelationType.TESTED_BY.value: ["测试", "检测", "量测", "检验"],
    RelationType.CERTIFIED_BY.value: ["认证", "车规", "军规", "AEC-Q", "ISO", "IATF"],
    RelationType.COST_DRIVER_OF.value: ["成本", "占成本", "BOM", "降价", "毛利率"],
    RelationType.PERFORMANCE_DRIVER_OF.value: ["决定性能", "影响性能", "性能关键", "速率", "带宽"],
}

# 低质量来源集合
LOW_QUALITY_SOURCES: frozenset[str] = frozenset([
    "web_search_snippet", "web_search", "forum", "social_media", "llm_inference",
])

# 认可的高质量来源（用于 noise penalty 判断）
RECOGNIZED_SOURCES: frozenset[str] = frozenset([
    "announcement", "annual_report", "prospectus", "official_website",
    "julang_report", "broker_report", "industry_whitepaper", "whitepaper",
    "wencai", "eastmoney", "cninfo_interaction", "investor_relation",
])


def _parse_timestamp(timestamp: str) -> datetime | None:
    """解析 ISO 格式时间戳，失败返回 None。"""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        # 处理带 Z 的 UTC 时间
        ts = timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        return None


class ClaimQualityScorer:
    """对单个 claim 进行多维度质量评分。"""

    def __init__(self, kb: SAGKnowledgeBase | None = None) -> None:
        """初始化。

        Args:
            kb: SAG 知识库实例，用于图结构噪声评分；None 时 noise_penalty 退化为简单规则。
        """
        self.kb = kb

    def evidence_clarity(self, claim: Claim) -> float:
        """评估 evidence 是否清晰、具体、可验证。

        检查 evidence 中是否包含 head/tail、数字、时间、关系词。
        正式来源（source_weight >= 0.95）有地板保护，避免文本模糊被严重拉低。

        Returns:
            0.0-1.0 的清晰度分数
        """
        if claim is None:
            return 0.0
        evidence = str(_field(claim, "evidence", "") or "").strip()
        if not evidence:
            return 0.0

        score = 0.4  # 基础分
        text = evidence.lower()

        # 包含 head/tail
        head = str(_field(claim, "head", "") or "").strip()
        tail = str(_field(claim, "tail", "") or "").strip()
        if head and head in evidence:
            score += 0.15
        if tail and tail in evidence:
            score += 0.15

        # 包含数字（更具体）
        if re.search(r"\d+(?:\.\d+)?%?", evidence):
            score += 0.1

        # 包含时间/年份
        if re.search(r"20\d{2}年?|20\d{2}-\d{2}|Q[1-4]", evidence):
            score += 0.1

        # 包含关系词
        relation = _field(claim, "relation")
        relation_keywords = RELATION_KEYWORDS.get(relation, []) if isinstance(relation, str) else []
        if any(kw in text for kw in relation_keywords):
            score += 0.1

        source_weight = _score(get_source_weight(_field(claim, "source_type", "")), 0.5)
        if source_weight >= 0.95:
            # 正式来源地板保护：不低于 0.4
            score = max(score, 0.4)

        return min(1.0, score)

    def relation_clarity(self, claim: Claim) -> float:
        """评估 relation 在 evidence 中是否明确出现或强烈暗示。

        Returns:
            0.0-1.0 的关系清晰度分数
        """
        if claim is None:
            return 0.0
        evidence = str(_field(claim, "evidence", "") or "").strip()
        relation = _field(claim, "relation")
        if not evidence or not relation:
            return 0.0

        keywords = RELATION_KEYWORDS.get(relation, []) if isinstance(relation, str) else []
        text = evidence.lower()

        # 直接关系词命中
        direct_hit = any(kw in text for kw in keywords)
        if direct_hit:
            return 0.85

        # 根据 relation 语义做弱推断
        if relation == RelationType.MADE_OF.value and any(kw in text for kw in ["核心", "主要", "关键", "由"]):
            return 0.5
        if relation == RelationType.SUPPLIED_BY.value and any(kw in text for kw in ["客户", "厂商", "公司", "股份"]):
            return 0.5

        return 0.3

    def text_quality_score(self, evidence: str) -> float:
        """评估 evidence 文本质量，过滤营销语和过度概括。

        Returns:
            0.0-1.0 的文本质量分数
        """
        evidence = str(evidence or "")
        if not evidence:
            return 0.0

        score = 1.0
        text = evidence.strip()

        # 营销语扣分
        marketing_hits = [w for w in MARKETING_WORDS if w in text]
        score -= min(0.4, len(marketing_hits) * 0.15)

        # 过度概括词扣分
        overgeneralized_hits = [w for w in OVERGENERALIZED_WORDS if w in text]
        score -= min(0.3, len(overgeneralized_hits) * 0.1)

        # 无意义修饰词密度
        meaningless_words = ["非常", "极其", "十分", "特别", "相当", "很大程度上"]
        meaningless_hits = sum(1 for w in meaningless_words if w in text)
        score -= min(0.2, meaningless_hits * 0.05)

        # 长度过短或过长
        length = len(text)
        if length < 8:
            score -= 0.3
        elif length > 300:
            score -= 0.1

        return max(0.0, min(1.0, score))

    def recency_score(self, timestamp: str) -> float:
        """根据时间戳评估 recency，越新越接近 1.0。

        Returns:
            0.0-1.0 的时效性分数
        """
        dt = _parse_timestamp(timestamp)
        if dt is None:
            return 0.5  # 无法解析时给中等分

        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        days = (now - dt).days
        if days < 0:
            # 未来时间，可信度低
            return 0.3
        if days <= 90:
            return 1.0
        if days <= 365:
            return 0.85
        if days <= 365 * 2:
            return 0.7
        if days <= 365 * 3:
            return 0.6
        if days <= 365 * 5:
            return 0.5
        return 0.4

    def noise_penalty(self, claim: Claim, kb: SAGKnowledgeBase | None = None) -> float:
        """基于图结构和来源多样性计算噪声惩罚。

        一个节点如果满足以下条件，noise_score 提高：
        - 连接度极低（1-2 条边）
        - 边大多来自 web_search_snippet / forum / social_media
        - 没有向下游的具体材料/工艺/设备/供应商边
        - 没有公司映射
        - 没有来自巨浪研报、公告、年报、官网、问财、东财等补充来源
        - entity_type = generic_term 或 resolver_confidence 很低

        保护规则：
        - 命中 material_equipment_terms 的具体节点不会被过度惩罚。
        - 用户标记 useful 的节点直接返回 0。
        - 标题/摘要/营销语且低质量来源额外惩罚。

        Returns:
            0.0-0.3 的噪声惩罚值
        """
        if claim is None:
            return 0.0
        kb = kb or self.kb
        tail = str(_field(claim, "tail", "") or "").strip()
        if not tail or not kb:
            return 0.0

        penalty = 0.0
        nodes = getattr(kb, "nodes", {})
        node = _mapping_get(nodes, tail)
        if not node:
            return 0.0

        # 用户明确标记 useful 的节点不惩罚
        metadata = _field(node, "metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if metadata.get("user_marked") == "useful":
            return 0.0

        try:
            edges_out = kb.get_neighbors(tail, direction="out") or []
            edges_in = kb.get_neighbors(tail, direction="in") or []
        except (AttributeError, KeyError, TypeError, ValueError):
            return 0.0
        edges_out = edges_out if isinstance(edges_out, (list, tuple)) else []
        edges_in = edges_in if isinstance(edges_in, (list, tuple)) else []
        all_edges = edges_out + edges_in

        # 判断 tail 是否为具体材料/设备/工艺/耗材/检测/认证节点
        is_concrete_non_company = _field(node, "node_type", "") in {
            NodeType.MATERIAL.value,
            NodeType.COMPONENT.value,
            NodeType.EQUIPMENT.value,
            NodeType.PROCESS.value,
            NodeType.TESTING.value,
            NodeType.CONSUMABLE.value,
            NodeType.CERTIFICATION.value,
        }

        # 连接度极低
        if len(all_edges) <= 1:
            penalty += 0.1
        elif len(all_edges) <= 2:
            penalty += 0.05

        # 来源多样性差
        source_types: set[str] = set()
        low_quality_count = 0
        recognized_count = 0
        for edge in all_edges:
            claim_ids = _field(edge, "claim_ids", [])
            claim_ids = [claim_ids] if isinstance(claim_ids, str) else claim_ids
            for cid in claim_ids if isinstance(claim_ids, (list, tuple, set)) else ():
                claims = getattr(kb, "claims", {})
                c = _mapping_get(claims, cid)
                if c:
                    source_type = str(_field(c, "source_type", ""))
                    source_types.add(source_type)
                    if source_type in LOW_QUALITY_SOURCES:
                        low_quality_count += 1
                    if source_type in RECOGNIZED_SOURCES:
                        recognized_count += 1

        if len(source_types) <= 1:
            penalty += 0.05
        if low_quality_count > 0 and recognized_count == 0:
            penalty += 0.1

        # 没有向下游的具体边（材料/工艺/设备/供应商）
        downstream_concrete = any(
            _field(e, "relation", "") in (
                RelationType.MADE_OF.value,
                RelationType.PROCESSED_BY.value,
                RelationType.TESTED_BY.value,
                RelationType.SUPPLIED_BY.value,
                RelationType.CERTIFIED_BY.value,
            )
            for e in edges_out
        )
        if not downstream_concrete and is_concrete_non_company:
            penalty += 0.05

        # 泛概念节点
        if _field(node, "node_type", "") == NodeType.GENERIC.value:
            penalty += 0.1

        # resolver_confidence 很低（如果已有）
        resolver_confidence = _finite(metadata.get("resolver_confidence"), 1.0)
        if resolver_confidence < 0.3:
            penalty += 0.05

        # 标题/摘要/营销语且低质量来源额外惩罚
        evidence = str(_field(claim, "evidence", "") or "").strip()
        if _field(claim, "source_type", "") in LOW_QUALITY_SOURCES:
            if len(evidence) < 20:
                penalty += 0.05
            marketing_hits = [w for w in MARKETING_WORDS if w in evidence]
            if len(marketing_hits) > 2:
                penalty += 0.05

        # 保护具体非公司节点：惩罚上限降低
        max_penalty = 0.2 if is_concrete_non_company else 0.3
        return min(max_penalty, penalty)

    def compute_components(
        self,
        claim: Claim,
        kb: SAGKnowledgeBase | None = None,
    ) -> ClaimScoreComponents:
        """计算 claim 评分的所有组成部分。

        Args:
            claim: 待评分 claim
            kb: SAG 知识库，用于 noise penalty

        Returns:
            ClaimScoreComponents 实例
        """
        if claim is None:
            return ClaimScoreComponents()
        kb = kb or self.kb
        source_weight = _score(get_source_weight(_field(claim, "source_type", "")), 0.5)

        # extractor_confidence 默认值
        extractor_confidence = _score(_field(claim, "confidence", 0.0))
        if extractor_confidence <= 0.0:
            extractor_type = _field(claim, "extractor_type") or "rule"
            if extractor_type == "llm":
                extractor_confidence = 0.85
            elif extractor_type == "seed":
                extractor_confidence = 0.60
            elif extractor_type == "migration":
                extractor_confidence = 0.50
            else:
                extractor_confidence = 0.55

        return ClaimScoreComponents(
            source_weight=source_weight,
            extractor_confidence=extractor_confidence,
            evidence_clarity=self.evidence_clarity(claim),
            relation_clarity=self.relation_clarity(claim),
            recency_score=self.recency_score(_field(claim, "timestamp", "")),
            text_quality_score=self.text_quality_score(_field(claim, "evidence", "")),
            noise_penalty=self.noise_penalty(claim, kb),
            contradiction_penalty=0.0,  # 由 ContradictionDetector 设置
        )
