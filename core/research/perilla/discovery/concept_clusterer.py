"""Perilla Discovery Engine - 概念聚类与需求锚点识别.

将同义概念合并为规范名称，并基于规则识别需求锚点类型，
为后续供应链构建提供标准化的概念输入。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Set

from .models import (
    CONFIDENCE_LEVELS,
    ConceptSignal,
    DemandAnchor,
    SourceDocument,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _field(value: Any, name: str, default: Any = "") -> Any:
    """统一读取 dataclass 和映射式边界输入。"""
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default

# ---------------------------------------------------------------------------
# 预定义别名组 —— 将常见同义概念映射到规范名称
# ---------------------------------------------------------------------------

_ALIAS_GROUPS: List[Dict[str, object]] = [
    {"canonical": "AIPC", "aliases": ["AI PC", "AI电脑", "端侧AI PC", "AI个人电脑"]},
    {"canonical": "光互连", "aliases": ["光通信", "CPO", "外置光源", "辉光链", "光电互连"]},
    {"canonical": "具身智能", "aliases": ["人形机器人", "灵巧手", "Optimus", "Embodied AI"]},
    {"canonical": "固态电池", "aliases": ["全固态电池", "固态锂电池"]},
    {"canonical": "HBM", "aliases": ["高带宽内存", "High Bandwidth Memory"]},
    {"canonical": "铜互连", "aliases": ["铜连接", "铜缆", "AEC"]},
]

def _normalize_name(name: str) -> str:
    """规范化概念名称，用于同义匹配.

    转小写、去首尾空白、移除常见后缀词（概念/板块/主题/题材）。

    Args:
        name: 原始概念名称。

    Returns:
        规范化后的名称字符串。
    """
    result = _text(name).strip().lower()
    # 移除常见后缀
    for suffix in ("概念", "板块", "主题", "题材"):
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result.strip()


# 公开别名，供外部复用
normalize_name = _normalize_name


# 需求锚点规则: (关键词集合, 锚点类型)
_DEMAND_ANCHOR_RULES: List[tuple[Set[str], str]] = [
    ({"PC", "手机", "汽车"}, "terminal_product"),
    ({"AI", "智能"}, "application_scenario"),
    ({"电池", "芯片", "光"}, "technology_route"),
    ({"碳中和", "新基建"}, "policy_theme"),
]


class ConceptClusterer:
    """概念聚类器 —— 合并同义概念并识别需求锚点.

    通过预定义别名组将不同表述的同一概念合并为规范名称，
    然后基于关键词规则为概念识别需求锚点类型。

    Usage::

        clusterer = ConceptClusterer()
        merged = clusterer.cluster_concepts(signals)
        anchors = clusterer.identify_demand_anchors(merged[0], docs)
    """

    def cluster_concepts(self, signals: List[ConceptSignal]) -> List[ConceptSignal]:
        """合并属于同一别名组的概念信号.

        对于每个别名组：规范名称作为合并后名称，别名取并集，
        热度/新颖度取最大值，来源取并集，置信度取最高。
        不属于任何别名组的信号原样传递。

        Args:
            signals: 待合并的概念信号列表。

        Returns:
            合并后的概念信号列表，顺序不保证。
        """
        signals = [
            signal
            for signal in _items(signals)
            if _text(_field(signal, "name"))
        ]
        if not signals:
            return []

        merged_results: List[ConceptSignal] = []
        # 记录已被合并的信号索引，避免重复
        consumed: Set[int] = set()

        # 第一轮：按别名组进行合并
        for group in _ALIAS_GROUPS:
            canonical = _text(group.get("canonical"))
            group_aliases: Set[str] = {
                _text(alias) for alias in _items(group.get("aliases")) if _text(alias)
            }
            group_aliases.add(canonical)
            normalized_group_aliases = {
                _normalize_name(alias) for alias in group_aliases
            }

            # 查找属于此组的信号
            group_signals: List[tuple[int, ConceptSignal]] = []
            for idx, sig in enumerate(signals):
                if idx in consumed:
                    continue
                norm_name = _normalize_name(_field(sig, "name"))
                signal_aliases = {
                    _normalize_name(alias)
                    for alias in _items(_field(sig, "aliases", None))
                    if _text(alias)
                }
                # 检查信号名称是否匹配组内任一别名
                if norm_name in normalized_group_aliases:
                    group_signals.append((idx, sig))
                    consumed.add(idx)
                # 也检查信号的 aliases 字段
                elif any(
                    alias in normalized_group_aliases
                    for alias in signal_aliases
                ):
                    group_signals.append((idx, sig))
                    consumed.add(idx)

            if not group_signals:
                continue

            # 合并同组信号
            merged = self._merge_group(canonical, group_aliases, group_signals)
            if merged is not None:
                merged_results.append(merged)
            logger.debug(
                "合并概念组: canonical=%s, 合并了 %d 个信号",
                canonical,
                len(group_signals),
            )

        # 第二轮：未匹配的信号原样传递
        for idx, sig in enumerate(signals):
            if idx not in consumed:
                merged_results.append(sig)

        return merged_results

    def identify_demand_anchors(
        self,
        concept: ConceptSignal,
        source_docs: List[SourceDocument],
    ) -> List[DemandAnchor]:
        """基于规则识别概念的需求锚点.

        规则优先级：
        1. 名称含 PC/手机/汽车 → terminal_product
        2. 名称含 AI/智能 → application_scenario
        3. 名称含 电池/芯片/光 → technology_route
        4. 名称含 碳中和/新基建 → policy_theme
        5. 无匹配 → unknown

        Args:
            concept: 已聚类的概念信号。
            source_docs: 来源文档列表（用于提取证据 ID）。

        Returns:
            识别到的需求锚点列表，至少包含一个锚点。
        """
        if concept is None:
            return []
        name = _text(_field(concept, "name"))
        if not name:
            return []
        evidence_ids = [
            _text(_field(doc, "document_id"))
            for doc in _items(source_docs)
            if _text(_field(doc, "document_id"))
        ]
        concept_id = _text(_field(concept, "concept_id"))
        confidence = _text(_field(concept, "confidence"))
        confidence = confidence if confidence in CONFIDENCE_LEVELS else "low"
        name_upper = name.upper()

        matched_anchors: List[DemandAnchor] = []

        for keywords, anchor_type in _DEMAND_ANCHOR_RULES:
            if any(kw in name or kw.upper() in name_upper for kw in keywords):
                anchor = DemandAnchor(
                    anchor_id=new_id("da_"),
                    concept_id=concept_id,
                    name=f"{name}-{anchor_type}",
                    anchor_type=anchor_type,
                    description=f"基于规则识别: 概念名称 '{name}' 包含关键词，"
                    f"锚定为 {anchor_type} 类型",
                    evidence_ids=evidence_ids,
                    confidence=confidence,
                )
                matched_anchors.append(anchor)

        # 无规则匹配时生成 unknown 类型锚点
        if not matched_anchors:
            anchor = DemandAnchor(
                anchor_id=new_id("da_"),
                concept_id=concept_id,
                name=f"{name}-unknown",
                anchor_type="unknown",
                description=f"概念 '{name}' 未匹配任何需求锚点规则，"
                f"标记为 unknown 类型",
                evidence_ids=evidence_ids,
                confidence="low",
            )
            matched_anchors.append(anchor)

        return matched_anchors

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_group(
        canonical: str,
        group_aliases: Set[str],
        group_signals: List[tuple[int, ConceptSignal]],
    ) -> ConceptSignal | None:
        """将同一别名组内的信号合并为单个 ConceptSignal.

        Args:
            canonical: 规范名称。
            group_aliases: 组内别名集合。
            group_signals: (原始索引, ConceptSignal) 列表。

        Returns:
            合并后的 ConceptSignal。
        """
        group_signals = [
            (idx, signal)
            for idx, signal in _items(group_signals)
            if _text(_field(signal, "name"))
        ]
        if not group_signals:
            return None
        canonical = _text(canonical)

        # 收集所有别名（排除规范名称自身）
        all_aliases: Set[str] = set()
        for _, sig in group_signals:
            all_aliases.update(
                alias
                    for alias in _items(_field(sig, "aliases", None))
                if _text(alias)
            )
            # 信号原名如果不是规范名称，也作为别名
            signal_name = _text(_field(sig, "name"))
            if signal_name != canonical:
                all_aliases.add(signal_name)
        all_aliases.discard(canonical)

        # 合并 detected_from
        all_detected_from: List[str] = []
        seen_from: Set[str] = set()
        for _, sig in group_signals:
            for src in _items(_field(sig, "detected_from", None)):
                if not _text(src):
                    continue
                if src not in seen_from:
                    all_detected_from.append(src)
                    seen_from.add(src)

        # 合并 source_types
        all_source_types: List[str] = []
        seen_types: Set[str] = set()
        for _, sig in group_signals:
            for st in _items(_field(sig, "source_types", None)):
                if not _text(st):
                    continue
                if st not in seen_types:
                    all_source_types.append(st)
                    seen_types.add(st)

        # 合并 raw_evidence_ids
        all_evidence_ids: List[str] = []
        seen_evidence: Set[str] = set()
        for _, sig in group_signals:
            for eid in _items(_field(sig, "raw_evidence_ids", None)):
                if not _text(eid):
                    continue
                if eid not in seen_evidence:
                    all_evidence_ids.append(eid)
                    seen_evidence.add(eid)

        # 置信度排序: high > medium > low
        _confidence_rank = {"high": 3, "medium": 2, "low": 1}
        confidences = [
            _text(_field(sig, "confidence")) for _, sig in group_signals
        ]
        best_confidence = max(
            confidences,
            key=lambda confidence: _confidence_rank.get(confidence, 0),
        ) or "low"

        # 时间范围
        first_seen = min(
            (
                _text(_field(sig, "first_seen_at"))
                for _, sig in group_signals
                if _text(_field(sig, "first_seen_at"))
            ),
            default="",
        )
        last_seen = max(
            (
                _text(_field(sig, "last_seen_at"))
                for _, sig in group_signals
                if _text(_field(sig, "last_seen_at"))
            ),
            default="",
        )

        # 使用第一个信号的 concept_id 作为合并后的 ID
        first_signal = group_signals[0][1]

        return ConceptSignal(
            concept_id=_text(_field(first_signal, "concept_id")) or new_id("cs_"),
            name=canonical,
            aliases=sorted(all_aliases),
            detected_from=all_detected_from,
            source_types=all_source_types,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            heat_score=max(
                _finite(_field(sig, "heat_score", 0.0))
                for _, sig in group_signals
            ),
            novelty_score=max(
                _finite(_field(sig, "novelty_score", 0.0))
                for _, sig in group_signals
            ),
            confidence=best_confidence,
            raw_evidence_ids=all_evidence_ids,
            notes=f"由 {len(group_signals)} 个同义概念合并而来",
        )
