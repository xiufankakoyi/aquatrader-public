"""Perilla Discovery Engine - 证据等级评分器.

对 CompanyCandidate 的证据进行多维度评分，确定证据等级、
置信度、是否需要人工审核、未确认原因及升级路径。
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from collections.abc import Mapping

from core.research.perilla.discovery.models import (
    CREDIBILITY_HINTS,
    EVIDENCE_LEVEL_CLAIM_ONLY,
    EVIDENCE_LEVEL_CONFIRMED,
    EVIDENCE_LEVEL_EARLY_SIGNAL,
    EVIDENCE_LEVEL_HYPOTHESIS,
    EVIDENCE_LEVEL_INSUFFICIENT,
    EVIDENCE_LEVEL_MAIN_ANCHOR,
    EVIDENCE_LEVEL_NON_CORE,
    EVIDENCE_LEVEL_PROBABLE,
    EVIDENCE_LEVEL_RUMOR,
    EVIDENCE_LEVELS,
    MARKETS,
    SOURCE_TYPES,
    CompanyCandidate,
    EarlySignal,
    SourceDocument,
)

logger = logging.getLogger(__name__)


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()

# ---------------------------------------------------------------------------
# 来源类型 -> 可信度映射
# ---------------------------------------------------------------------------

_SOURCE_CREDIBILITY_MAP: Dict[str, float] = {
    "announcement": 1.0,
    "annual_report": 1.0,
    "semiannual_report": 0.98,
    "quarterly_report": 0.95,
    "official_site": 0.95,
    "local_evidence": 0.9,
    "investor_interaction": 0.85,
    "report": 0.85,
    "tender": 0.8,
    "news": 0.7,
    "meeting_note": 0.65,
    "recruitment": 0.6,
    "expo": 0.55,
    "rss": 0.5,
    "change_event": 0.5,
    "search_result": 0.45,
    "sector_page": 0.4,
    "index_page": 0.3,
    "concept_page": 0.25,
    "forum": 0.2,
    "social": 0.15,
    "rumor": 0.1,
    "manual": 0.5,
}

# 官方来源类型集合
_OFFICIAL_SOURCE_TYPES = {
    "announcement",
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "official_site",
    "local_evidence",
}

# 供应关系关键词
_SUPPLY_KEYWORDS = (
    "供货", "供应商", "核心供应商", "进入供应链", "客户认证",
    "送样", "量产", "独家供应", "主要供应商", "合格供应商",
    "战略供应商", "供货商", "供货关系", "供应",
)

# 间接关系关键词
_INDIRECT_KEYWORDS = (
    "涉及", "布局", "相关", "概念", "题材", "板块",
    "有望", "预期", "或可", "可能",
)


class EvidenceGrader:
    """证据等级评分器.

    对公司候选的证据进行多维度评分，综合判定证据等级。
    评分维度包括来源可信度、证据时效性、关系直接性、
    来源数量、接地质量、市场相关性和早期信号价值。
    """

    def grade_evidence(
        self,
        candidate: CompanyCandidate,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> Tuple[str, str, bool, str, str]:
        """对公司候选进行证据等级评分.

        Args:
            candidate: 公司候选对象。
            source_docs: 关联的来源文档列表。
            early_signals: 关联的早期信号列表。

        Returns:
            (evidence_level, confidence, needs_human_review, why_not_confirmed, upgrade_path)
            - evidence_level: 证据等级字符串。
            - confidence: 置信度 (high / medium / low)。
            - needs_human_review: 是否需要人工审核。
            - why_not_confirmed: 未确认原因说明。
            - upgrade_path: 升级路径建议。
        """
        if not isinstance(candidate, CompanyCandidate):
            return EVIDENCE_LEVEL_INSUFFICIENT, "low", True, "候选公司字段缺失", "需要可核验的公司与来源信息"
        source_docs = [doc for doc in _items(source_docs) if isinstance(doc, SourceDocument)]
        early_signals = [signal for signal in _items(early_signals) if isinstance(signal, EarlySignal)]
        # 收集评分所需的原始数据
        source_types = [_text(_field(doc, "source_type")) for doc in source_docs]
        dates = self._collect_dates(source_docs, early_signals)
        texts = [_text(_field(doc, "content_text")) for doc in source_docs if _text(_field(doc, "content_text"))]
        market = _text(_field(candidate, "market"), "UNKNOWN")

        # ---- 七维度评分 ----
        scores: Dict[str, float] = {
            "source_credibility": self._score_source_credibility(source_types),
            "evidence_recency": self._score_evidence_recency(dates),
            "relation_directness": self._score_relation_directness(texts, _text(_field(candidate, "company_name"))),
            "source_count": self._score_source_count(len(source_docs)),
            "grounding_quality": self._score_grounding_quality(source_docs),
            "market_relevance": self._score_market_relevance(market),
            "early_signal_value": self._score_early_signal_value(early_signals),
        }

        logger.debug(
            "Evidence scores for %s: %s",
            _text(_field(candidate, "company_name")), scores,
        )

        # ---- 判定证据等级 ----
        evidence_level = self._determine_evidence_level(
            candidate, source_docs, source_types, early_signals, scores,
        )

        # ---- 判定置信度 ----
        confidence = self._determine_confidence(scores, evidence_level)

        # ---- 判定是否需要人工审核 ----
        needs_human_review = self._determine_needs_review(evidence_level, scores)

        # ---- 未确认原因 ----
        why_not_confirmed = self._explain_why_not_confirmed(
            evidence_level, scores, source_types,
        )

        # ---- 升级路径 ----
        upgrade_path = self._suggest_upgrade_path(
            evidence_level, scores, source_types,
        )

        logger.info(
            "Graded %s: level=%s, confidence=%s, review=%s",
            _text(_field(candidate, "company_name")), evidence_level, confidence, needs_human_review,
        )

        return evidence_level, confidence, needs_human_review, why_not_confirmed, upgrade_path

    # ------------------------------------------------------------------
    # 内部判定逻辑
    # ------------------------------------------------------------------

    def _determine_evidence_level(
        self,
        candidate: CompanyCandidate,
        source_docs: List[SourceDocument],
        source_types: List[str],
        early_signals: List[EarlySignal],
        scores: Dict[str, float],
    ) -> str:
        """根据评分和来源类型判定证据等级."""
        # MAIN_ANCHOR: 终端/主营公司
        if _text(_field(candidate, "evidence_level")) == EVIDENCE_LEVEL_MAIN_ANCHOR:
            return EVIDENCE_LEVEL_MAIN_ANCHOR

        # NON_CORE_MARKET: 非核心市场
        if scores["market_relevance"] < 0.5:
            return EVIDENCE_LEVEL_NON_CORE

        # 无外部证据
        if not source_docs and not early_signals:
            return EVIDENCE_LEVEL_HYPOTHESIS

        # 分类来源类型
        source_type_set = set(source_types)
        has_official = bool(source_type_set & _OFFICIAL_SOURCE_TYPES)
        has_concept_only = source_type_set <= {"concept_page", "index_page"}
        has_early_only = source_type_set <= {"recruitment", "expo", "tender"}
        has_rumor_only = source_type_set <= {"forum", "social", "rumor"}

        # CONFIRMED_SUPPLIER: 高可信 + 官方来源 + 直接关系
        if (
            scores["source_credibility"] >= 0.8
            and has_official
            and scores["relation_directness"] >= 0.7
        ):
            return EVIDENCE_LEVEL_CONFIRMED

        # PROBABLE_SUPPLIER: 中等可信 + 多来源
        if scores["source_credibility"] >= 0.6 and scores["source_count"] >= 0.4:
            return EVIDENCE_LEVEL_PROBABLE

        # CONCEPT_CLAIM_ONLY: 仅有概念/指数页面来源
        if has_concept_only and source_types:
            return EVIDENCE_LEVEL_CLAIM_ONLY

        # EARLY_SIGNAL: 仅有招聘/展会/招标来源
        if has_early_only and source_types:
            return EVIDENCE_LEVEL_EARLY_SIGNAL

        # RUMOR_ONLY: 仅有论坛/社交/传闻来源
        if has_rumor_only and source_types:
            return EVIDENCE_LEVEL_RUMOR

        # 冲突或不足
        if scores["source_credibility"] < 0.3 or scores["grounding_quality"] < 0.2:
            return EVIDENCE_LEVEL_INSUFFICIENT

        # 默认降级为 PROBABLE 或 INSUFFICIENT
        if scores["source_credibility"] >= 0.5:
            return EVIDENCE_LEVEL_PROBABLE

        return EVIDENCE_LEVEL_INSUFFICIENT

    def _determine_confidence(
        self, scores: Dict[str, float], evidence_level: str,
    ) -> str:
        """根据评分和证据等级判定置信度."""
        avg_score = sum(scores.values()) / len(scores) if scores else 0.0

        if evidence_level == EVIDENCE_LEVEL_CONFIRMED and avg_score >= 0.7:
            return "high"
        if evidence_level in (EVIDENCE_LEVEL_CONFIRMED, EVIDENCE_LEVEL_PROBABLE) and avg_score >= 0.5:
            return "medium"
        if evidence_level == EVIDENCE_LEVEL_MAIN_ANCHOR:
            return "high"
        if avg_score >= 0.4:
            return "medium"
        return "low"

    def _determine_needs_review(
        self, evidence_level: str, scores: Dict[str, float],
    ) -> bool:
        """判定是否需要人工审核."""
        always_review = {
            EVIDENCE_LEVEL_CLAIM_ONLY,
            EVIDENCE_LEVEL_RUMOR,
            EVIDENCE_LEVEL_HYPOTHESIS,
            EVIDENCE_LEVEL_INSUFFICIENT,
            EVIDENCE_LEVEL_NON_CORE,
        }
        if evidence_level in always_review:
            return True

        # PROBABLE 但评分偏低也需审核
        if evidence_level == EVIDENCE_LEVEL_PROBABLE and scores["source_credibility"] < 0.7:
            return True

        # EARLY_SIGNAL 需持续跟踪
        if evidence_level == EVIDENCE_LEVEL_EARLY_SIGNAL:
            return True

        return False

    def _explain_why_not_confirmed(
        self,
        evidence_level: str,
        scores: Dict[str, float],
        source_types: List[str],
    ) -> str:
        """生成未确认原因说明."""
        if evidence_level == EVIDENCE_LEVEL_CONFIRMED:
            return ""
        if evidence_level == EVIDENCE_LEVEL_MAIN_ANCHOR:
            return ""

        reasons = []
        source_type_set = set(source_types)

        if scores["source_credibility"] < 0.6:
            reasons.append("来源可信度不足")
        if not (source_type_set & _OFFICIAL_SOURCE_TYPES):
            reasons.append("缺乏官方来源确认")
        if scores["relation_directness"] < 0.5:
            reasons.append("供应链关系描述不直接")
        if scores["source_count"] < 0.4:
            reasons.append("来源数量不足")
        if scores["grounding_quality"] < 0.4:
            reasons.append("证据接地质量低")
        if scores["evidence_recency"] < 0.3:
            reasons.append("证据时效性差")
        if source_type_set <= {"concept_page", "index_page"}:
            reasons.append("仅出现在概念/指数页面")
        if source_type_set <= {"forum", "social", "rumor"}:
            reasons.append("仅来自论坛/社交/传闻")

        return "；".join(reasons) if reasons else "证据不足以确认"

    def _suggest_upgrade_path(
        self,
        evidence_level: str,
        scores: Dict[str, float],
        source_types: List[str],
    ) -> str:
        """建议升级路径."""
        if evidence_level == EVIDENCE_LEVEL_CONFIRMED:
            return ""
        if evidence_level == EVIDENCE_LEVEL_MAIN_ANCHOR:
            return ""

        source_type_set = set(source_types)

        # 按优先级给出升级建议
        if not (source_type_set & _OFFICIAL_SOURCE_TYPES):
            return "需要公告/官网确认"
        if scores["relation_directness"] < 0.5:
            return "需要客户认证来源"
        if scores["source_count"] < 0.4:
            return "需要更多新闻/研报交叉"
        if source_type_set <= {"concept_page", "index_page"}:
            return "需要排除蹭概念"
        if scores["grounding_quality"] < 0.4:
            return "需要产能或订单证据"
        if evidence_level == EVIDENCE_LEVEL_EARLY_SIGNAL:
            return "需要产能或订单证据"
        if evidence_level == EVIDENCE_LEVEL_RUMOR:
            return "需要更多新闻/研报交叉"
        if evidence_level == EVIDENCE_LEVEL_HYPOTHESIS:
            return "需要任何外部证据支持"

        return "需要更多交叉验证"

    # ------------------------------------------------------------------
    # 七维度评分方法
    # ------------------------------------------------------------------

    def _score_source_credibility(self, source_types: List[str]) -> float:
        """评分来源可信度.

        基于来源类型的可信度映射，取最高值并做归一化平滑。

        Args:
            source_types: 来源类型列表。

        Returns:
            可信度评分 [0, 1]。
        """
        if not source_types:
            return 0.0

        credibility_scores = []
        for st in source_types:
            score = _SOURCE_CREDIBILITY_MAP.get(_text(st), 0.3)
            credibility_scores.append(score)

        # 取最高可信度，并用均值做平滑
        max_cred = max(credibility_scores)
        avg_cred = sum(credibility_scores) / len(credibility_scores)
        # 加权: 70% 最高 + 30% 均值
        blended = 0.7 * max_cred + 0.3 * avg_cred
        return round(min(max(blended, 0.0), 1.0), 4)

    def _score_evidence_recency(self, dates: List[str]) -> float:
        """评分证据时效性.

        距今越近分数越高，180 天内满分，超过 730 天接近 0。

        Args:
            dates: ISO 格式日期字符串列表。

        Returns:
            时效性评分 [0, 1]。
        """
        if not dates:
            return 0.0

        now = datetime.now()
        recency_scores = []

        for date_str in dates:
            try:
                # 尝试解析 ISO 格式日期
                dt = datetime.fromisoformat(_text(date_str).replace("Z", "+00:00"))
                # 去掉时区信息以计算天数差
                dt_naive = dt.replace(tzinfo=None)
                days_diff = (now - dt_naive).days
            except (ValueError, TypeError):
                # 无法解析的日期视为中等时效
                recency_scores.append(0.3)
                continue

            if days_diff <= 0:
                recency_scores.append(1.0)
            elif days_diff <= 180:
                recency_scores.append(1.0 - (days_diff / 180) * 0.3)
            elif days_diff <= 730:
                recency_scores.append(0.7 * (1.0 - (days_diff - 180) / 550))
            else:
                recency_scores.append(0.05)

        # 取最近日期的评分
        return round(min(max(max(recency_scores), 0.0), 1.0), 4)

    def _score_relation_directness(
        self, texts: List[str], node_name: str,
    ) -> float:
        """评分供应链关系直接性.

        检查文本中是否包含公司与供应链节点之间的直接供应关系描述。

        Args:
            texts: 文档文本列表。
            node_name: 供应链节点/公司名称。

        Returns:
            直接性评分 [0, 1]。
        """
        if not texts:
            return 0.0

        direct_count = 0
        indirect_count = 0

        for text in texts:
            if not text:
                continue
            # 检查直接供应关系关键词
            for kw in _SUPPLY_KEYWORDS:
                if kw in text:
                    direct_count += 1
                    break
            else:
                # 检查间接关系关键词
                for kw in _INDIRECT_KEYWORDS:
                    if kw in text:
                        indirect_count += 1
                        break

        total = direct_count + indirect_count
        if total == 0:
            return 0.0

        # 直接关系占比越高，分数越高
        direct_ratio = direct_count / total
        # 基础分 + 直接关系加成
        score = 0.3 + 0.7 * direct_ratio
        return round(min(max(score, 0.0), 1.0), 4)

    def _score_source_count(self, count: int) -> float:
        """评分来源数量.

        使用对数缩放：1 个来源 0.2，3 个 0.5，5 个 0.7，10+ 接近 1.0。

        Args:
            count: 来源数量。

        Returns:
            来源数量评分 [0, 1]。
        """
        try:
            count = max(0, int(count))
        except (TypeError, ValueError, OverflowError):
            count = 0
        if count <= 0:
            return 0.0
        # 对数缩放，底数 3
        score = min(math.log(count + 1, 3) / math.log(11, 3), 1.0)
        return round(max(score, 0.0), 4)

    def _score_grounding_quality(self, docs: List[SourceDocument]) -> float:
        """评分接地质量.

        有完整文本内容的文档越多，接地质量越高。

        Args:
            docs: 来源文档列表。

        Returns:
            接地质量评分 [0, 1]。
        """
        if not docs:
            return 0.0

        has_text = 0
        has_title = 0
        has_url = 0

        for doc in docs:
            if _text(_field(doc, "content_text")) and len(_text(_field(doc, "content_text"))) > 50:
                has_text += 1
            if _text(_field(doc, "title")):
                has_title += 1
            if _text(_field(doc, "url")):
                has_url += 1

        total = len(docs)
        # 文本内容权重最高
        text_ratio = has_text / total
        title_ratio = has_title / total
        url_ratio = has_url / total

        score = 0.6 * text_ratio + 0.25 * title_ratio + 0.15 * url_ratio
        return round(min(max(score, 0.0), 1.0), 4)

    def _score_market_relevance(self, market: str) -> float:
        """评分市场相关性.

        A 股和美股为核心市场，其他市场降级。

        Args:
            market: 市场标识。

        Returns:
            市场相关性评分 [0, 1]。
        """
        market_score_map = {
            "A_SHARE": 1.0,
            "US": 0.9,
            "HK": 0.5,
            "JP": 0.4,
            "TW": 0.4,
            "KR": 0.35,
            "EU": 0.3,
            "OTHER": 0.2,
            "UNKNOWN": 0.1,
        }
        return market_score_map.get(_text(market).upper(), 0.1)

    def _score_early_signal_value(self, signals: List[EarlySignal]) -> float:
        """评分早期信号价值.

        招聘、展会、招标等信号具有较高早期预警价值。

        Args:
            signals: 早期信号列表。

        Returns:
            早期信号价值评分 [0, 1]。
        """
        if not signals:
            return 0.0

        signal_value_map = {
            "certification": 0.9,
            "sampling": 0.85,
            "capacity": 0.8,
            "supplier": 0.75,
            "customer": 0.7,
            "tender": 0.65,
            "recruitment": 0.55,
            "expo": 0.5,
            "partner": 0.45,
            "technical_route": 0.4,
            "other": 0.2,
            "rumor": 0.15,
            "concept_claim": 0.1,
        }

        values = []
        for sig in signals:
            val = signal_value_map.get(_text(_field(sig, "signal_type")), 0.2)
            # 高置信度信号加权
            confidence = _text(_field(sig, "confidence")).lower()
            if confidence == "high":
                val *= 1.2
            elif confidence == "medium":
                val *= 1.0
            else:
                val *= 0.8
            values.append(min(val, 1.0))

        # 取最高信号价值
        return round(min(max(max(values), 0.0), 1.0), 4)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_dates(
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> List[str]:
        """从来源文档和早期信号中收集所有日期字符串.

        Args:
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            ISO 格式日期字符串列表。
        """
        dates: List[str] = []
        for doc in _items(source_docs):
            published_at = _text(_field(doc, "published_at"))
            fetched_at = _text(_field(doc, "fetched_at"))
            if published_at:
                dates.append(published_at)
            if fetched_at:
                dates.append(fetched_at)
        # 早期信号没有独立日期字段，跳过
        return dates
