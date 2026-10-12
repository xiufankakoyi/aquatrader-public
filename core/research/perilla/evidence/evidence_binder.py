"""Evidence binder.

Binds raw evidence records to graph nodes, edges, and event claims.
Implements lightweight entity resolution (fuzzy name matching via
normalization) and claim keyword matching against relation types.
"""
from __future__ import annotations

import re
from typing import Any

from core.research.perilla.evidence.evidence_schema import RawEvidenceRecord


_RELATION_KEYWORDS: dict[str, list[str]] = {
    "SUPPLIES_TO": ["供应", "供给", "提供", "供货", "上游", "原材料"],
    "COMPETES_WITH": ["竞争", "对手", "竞品", "同类", "替代"],
    "BENEFITS_FROM": ["受益", "利好", "带动", "促进", "推动", "提振"],
    "HURT_BY": ["受损", "利空", "打压", "压制", "拖累", "冲击"],
    "MAPS_TO": ["映射", "对应", "关联", "属于"],
    "CAUSES": ["导致", "引起", "引发", "造成", "驱动"],
    "CORRELATES_WITH": ["相关", "联动", "同步"],
    "BLOCKS": ["阻断", "阻碍", "阻止", "限制"],
    "ENABLES": ["使能", "支持", "助力", "赋能"],
}


def _normalize_name(name: str) -> str:
    """标准化实体名称：去空格、去标点、转小写。"""
    if not name:
        return ""
    cleaned = re.sub(r"[\s\-_\.·,，。、（）()【】\[\]\"'《》<>]+", "", str(name))
    return cleaned.lower()


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _keyword_match_score(text: str, keywords: list[str]) -> float:
    """计算文本与关键词列表的匹配得分（0~1）。"""
    if not text or not keywords:
        return 0.0
    text_lower = _text(text).lower()
    keywords = [kw for kw in keywords if _text(kw)] if isinstance(keywords, (list, tuple, set)) else []
    hits = sum(1 for kw in keywords if _text(kw).lower() in text_lower)
    return min(1.0, hits / max(len(keywords), 1))


class EvidenceBinder:
    """Binds raw evidence to ontology elements.

    Implements:
      1. Entity resolution via name normalization and fuzzy matching
      2. Claim-to-relation keyword matching for edge relevance
      3. Binding confidence scoring
    """

    def __init__(self, entity_aliases: dict[str, str] | None = None) -> None:
        """初始化。

        Args:
            entity_aliases: 实体别名映射，alias -> canonical_name。
        """
        self._aliases: dict[str, str] = {}
        if isinstance(entity_aliases, dict):
            for alias, canonical in entity_aliases.items():
                alias_key = _normalize_name(alias)
                canonical_name = _text(canonical)
                if alias_key and canonical_name:
                    self._aliases[alias_key] = canonical_name
        self._relation_keywords = dict(_RELATION_KEYWORDS)

    def resolve_entity(self, name: str) -> tuple[str, float]:
        """解析实体名称到规范名。

        Args:
            name: 原始实体名。

        Returns:
            (canonical_name, confidence) 元组。confidence 1.0 表示精确/别名命中。
        """
        name = _text(name)
        if not name:
            return ("", 0.0)
        norm = _normalize_name(name)
        if not norm:
            return ("", 0.0)

        if norm in self._aliases:
            return (self._aliases[norm], 1.0)

        for alias_norm, canonical in self._aliases.items():
            if alias_norm and (alias_norm in norm or norm in alias_norm):
                overlap = min(len(alias_norm), len(norm)) / max(len(alias_norm), len(norm))
                if overlap >= 0.6:
                    return (canonical, round(overlap, 2))

        return (name, 0.5)

    def match_relation(self, evidence_text: str) -> tuple[str, float]:
        """从证据文本中匹配最可能的关系类型。

        Args:
            evidence_text: 证据文本内容。

        Returns:
            (relation_type, confidence) 元组。
        """
        evidence_text = _text(evidence_text)
        if not evidence_text:
            return ("UNKNOWN", 0.0)

        best_rel = "UNKNOWN"
        best_score = 0.0
        for rel_type, keywords in self._relation_keywords.items():
            score = _keyword_match_score(evidence_text, keywords)
            if score > best_score:
                best_score = score
                best_rel = rel_type

        return (best_rel, round(best_score, 2))

    def bind_to_edge(
        self,
        evidence: RawEvidenceRecord,
        head: str,
        relation: str,
        tail: str,
    ) -> dict[str, Any]:
        """Bind a raw evidence record to a graph edge.

        执行实体消解、关系关键词匹配，并计算绑定置信度。

        Args:
            evidence: Raw evidence record.
            head: Edge head entity (original name).
            relation: Edge relation type.
            tail: Edge tail entity (original name).

        Returns:
            Binding payload with resolution info and confidence score.
        """
        if not isinstance(evidence, RawEvidenceRecord):
            return {"evidence_id": "", "edge": (_text(head), _text(relation), _text(tail)), "binding_confidence": 0.0}
        head = _text(head)
        relation = _text(relation)
        tail = _text(tail)
        resolved_head, head_conf = self.resolve_entity(head)
        resolved_tail, tail_conf = self.resolve_entity(tail)

        ev_text = _text(evidence.evidence_text)
        matched_rel, rel_match_score = self.match_relation(ev_text)
        relation_consistent = (
            relation.upper() == matched_rel.upper()
            or matched_rel == "UNKNOWN"
        )
        relation_bonus = rel_match_score if relation_consistent else 0.0

        text_len = len(ev_text)
        text_quality = min(1.0, text_len / 200.0)

        has_head = bool(resolved_head) and (
            _normalize_name(resolved_head) in _normalize_name(ev_text)
            or head_conf >= 0.8
        )
        has_tail = bool(resolved_tail) and (
            _normalize_name(resolved_tail) in _normalize_name(ev_text)
            or tail_conf >= 0.8
        )
        entity_match_score = (
            (1.0 if has_head else 0.3) + (1.0 if has_tail else 0.3)
        ) / 2.0

        binding_confidence = round(
            min(
                1.0,
                entity_match_score * 0.4
                + (head_conf + tail_conf) / 2.0 * 0.2
                + relation_bonus * 0.2
                + text_quality * 0.2,
            ),
            4,
        )

        return {
            "evidence_id": _text(evidence.evidence_id),
            "edge": (head, relation, tail),
            "resolved_edge": (resolved_head, relation, resolved_tail),
            "source_type": _text(evidence.source_type),
            "binding_confidence": binding_confidence,
            "entity_resolution": {
                "head": {"original": head, "resolved": resolved_head, "confidence": head_conf},
                "tail": {"original": tail, "resolved": resolved_tail, "confidence": tail_conf},
            },
            "relation_match": {
                "declared": relation,
                "detected": matched_rel,
                "match_score": rel_match_score,
                "consistent": relation_consistent,
            },
            "text_quality": round(text_quality, 2),
        }

    def register_alias(self, alias: str, canonical: str) -> None:
        """注册一个实体别名。

        Args:
            alias: 别名。
            canonical: 规范名。
        """
        alias_key = _normalize_name(alias)
        canonical_name = _text(canonical)
        if alias_key and canonical_name:
            self._aliases[alias_key] = canonical_name
