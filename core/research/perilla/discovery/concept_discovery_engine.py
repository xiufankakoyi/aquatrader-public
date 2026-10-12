"""Rule-based concept discovery from Perilla research inbox."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from .concept_signal_loader import ConceptSignalLoader
from .concept_taxonomy import ConceptTaxonomy

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()

_EN_ABBR_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]{1,8}|[0-9](?:\.[0-9])?[TGV]|[0-9]{3,4}[GV])\b")
_CN_TECH_TERM_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,12}"
    r"(?:\u6a21\u5757|\u82af\u7247|\u5f15\u64ce|\u5668\u4ef6|\u7535\u6e90|"
    r"\u8fde\u63a5\u5668|\u63a7\u5236\u5668|\u5c01\u88c5|\u6750\u6599|\u8bbe\u5907|"
    r"\u4f9b\u7535|\u6db2\u51b7|\u5149\u4e92\u8fde|\u9ad8\u538b\u76f4\u6d41)"
)
_IMPORTANT_ABBR = {
    "CPO", "LPO", "NPO", "HBM", "CXL", "HVDC", "ASIC", "GPU", "DRAM",
    "NAND", "DDR5", "SSD", "UPS", "PDU", "PCB", "800V", "800G", "1.6T",
}
_TEXT_FIELDS = (
    "title", "summary", "content", "tags", "category", "bottleneck",
    "company_name", "symbol", "chain_node", "concept", "main_dish",
    "classification", "bottleneck_logic", "notes", "next_verification_queries",
)
_DATE_FIELDS = (
    "last_updated_at", "updated_at", "published_at", "created_at",
    "first_seen_at", "timestamp", "date",
)
_SAFE_CONCEPT_EVIDENCE_LEVELS = {
    "EARLY_SIGNAL",
    "CONCEPT_CLAIM_ONLY",
    "HYPOTHESIS",
}


@dataclass
class _ConceptAccumulator:
    normalized_name: str
    root_theme: str
    parent_concept: str
    related_terms: Set[str] = field(default_factory=set)
    related_companies: Set[str] = field(default_factory=set)
    source_items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evidence_levels: List[str] = field(default_factory=list)
    priority_scores: List[float] = field(default_factory=list)
    source_quality_scores: List[float] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""
    recent_count: int = 0
    occurrences: int = 0
    root_hits: int = 0


class ConceptDiscoveryEngine:
    """Discover new branch concepts from inbox text and existing reports."""

    def __init__(
        self,
        project_root: str = ".",
        output_dir: str = "data/reports/perilla_discovery",
        lookback_days: int = 30,
        min_score: float = 0.2,
    ) -> None:
        self.loader = ConceptSignalLoader(project_root=project_root, output_dir=output_dir)
        self.taxonomy = ConceptTaxonomy()
        self.lookback_days = max(0, int(_to_float(lookback_days)))
        self.min_score = max(0.0, min(1.0, _to_float(min_score)))

    def run(self) -> List[Dict[str, Any]]:
        """Run discovery and write discovered_concepts_latest.json."""
        inbox_items = [
            item for item in _items(self.loader.load_inbox_items())
            if isinstance(item, Mapping)
        ]
        title_items = [
            item for item in _items(self.loader.load_title_items())
            if isinstance(item, Mapping)
        ]
        concept_signals = self.loader.load_concept_signals() or []
        company_candidates = self.loader.load_company_candidates() or []

        existing_names = self._existing_concept_names(concept_signals)
        company_index = self._build_company_candidate_index(company_candidates)

        accumulators: dict[str, _ConceptAccumulator] = {}
        raw_candidate_count = 0

        for item in inbox_items + title_items:
            context = self._item_text(item)
            if not context:
                continue
            raw_terms = self._extract_candidate_terms(context)
            raw_candidate_count += len(raw_terms)
            for raw_term in raw_terms:
                normalized = self.taxonomy.normalize(raw_term, context)
                if not normalized:
                    continue
                root_theme = self.taxonomy.root_theme_for(normalized, context)
                if not root_theme:
                    continue
                parent_concept = self.taxonomy.parent_concept_for(normalized, root_theme)
                acc = accumulators.get(normalized)
                if acc is None:
                    acc = _ConceptAccumulator(
                        normalized_name=normalized,
                        root_theme=root_theme,
                        parent_concept=parent_concept,
                    )
                    accumulators[normalized] = acc
                acc.related_terms.add(raw_term)
                acc.occurrences += 1
                acc.root_hits += self._root_hit_count(root_theme, context)
                self._add_item_to_accumulator(acc, item, context)

        for normalized, acc in accumulators.items():
            for company in company_index.get(normalized, set()):
                acc.related_companies.add(company)

        discovered = [
            self._build_record(acc, existing_names)
            for acc in accumulators.values()
        ]
        discovered = [
            row for row in discovered
            if row["concept_score"] >= self.min_score
            and row["evidence_level"] in _SAFE_CONCEPT_EVIDENCE_LEVELS
        ]
        self._append_seed_hypotheses(discovered, existing_names)
        discovered.sort(
            key=lambda row: (
                row["concept_score"],
                row["recent_count"],
                row["source_count"],
            ),
            reverse=True,
        )

        output_path = self.loader.write_discovered_concepts(discovered)
        branch_counts = self._branch_counts(discovered, existing_names)
        logger.info("ConceptDiscoveryEngine: inbox 条数=%d", len(inbox_items))
        logger.info("ConceptDiscoveryEngine: 新闻/研报标题条数=%d", len(title_items))
        logger.info("ConceptDiscoveryEngine: 抽取候选概念数=%d", raw_candidate_count)
        logger.info("ConceptDiscoveryEngine: 归一化后概念数=%d", len(accumulators))
        logger.info("ConceptDiscoveryEngine: 每条主线新分支数量=%s", branch_counts)
        logger.info(
            "ConceptDiscoveryEngine: 进入 Pipeline 的 discovered concepts=%d, output=%s",
            len(discovered),
            output_path,
        )
        return discovered

    def to_concept_signals(self, discovered: Iterable[Dict[str, Any]]) -> list:
        """Convert records to ConceptSignal instances for the existing pipeline."""
        return self.loader.to_concept_signals(discovered, min_score=self.min_score)

    def _append_seed_hypotheses(
        self,
        discovered: List[Dict[str, Any]],
        existing_names: Set[str],
    ) -> None:
        """Ensure root-theme seed branches enter the question/search backlog."""
        present = {
            _text(item.get("normalized_name") or item.get("concept_name"))
            for item in _items(discovered)
            if isinstance(item, Mapping)
        }
        for theme in self.taxonomy.themes:
            for seed in theme.seeds:
                normalized = self.taxonomy.normalize(seed, seed)
                if not normalized or normalized in present or normalized in existing_names:
                    continue
                discovered.append(
                    {
                        "concept_name": normalized,
                        "normalized_name": normalized,
                        "root_theme": theme.name,
                        "parent_concept": self.taxonomy.parent_concept_for(
                            normalized,
                            theme.name,
                        ),
                        "related_terms": [seed],
                        "related_companies": [],
                        "source_items": [],
                        "source_count": 0,
                        "recent_count": 0,
                        "concept_score": self.min_score,
                        "novelty_score": 1.0,
                        "evidence_level": "HYPOTHESIS",
                        "discovery_reason": (
                            "\u4e3b\u7ebf taxonomy \u521d\u59cb\u79cd\u5b50\uff0c"
                            "\u6682\u65e0\u672c\u5730\u8bc1\u636e\uff1b"
                            "\u8fdb\u5165\u5f85\u9a8c\u8bc1\u95ee\u9898\u6c60\u548c\u641c\u7d22\u4efb\u52a1\u961f\u5217\u3002"
                        ),
                        "first_seen_at": "",
                        "last_seen_at": "",
                    }
                )
                present.add(normalized)

    def _extract_candidate_terms(self, text: str) -> List[str]:
        text = _text(text)
        if not text:
            return []
        candidates: list[str] = []
        text_upper = text.upper()
        for term in self.taxonomy.candidate_terms():
            if term in text or term.upper() in text_upper:
                candidates.append(term)

        for match in _EN_ABBR_RE.finditer(text_upper):
            token = match.group(0)
            if token in _IMPORTANT_ABBR:
                candidates.append(token)

        for match in _CN_TECH_TERM_RE.finditer(text):
            token = match.group(0)
            if 2 <= len(token) <= 16:
                candidates.append(token)

        return sorted(set(candidates), key=lambda value: (len(value), value), reverse=True)

    def _add_item_to_accumulator(
        self,
        acc: _ConceptAccumulator,
        item: Dict[str, Any],
        context: str,
    ) -> None:
        if not isinstance(item, Mapping):
            return
        item_id = str(item.get("item_id") or item.get("id") or item.get("url") or id(item))
        item_date = self._item_date(item)
        if item_date:
            if not acc.first_seen_at or item_date < acc.first_seen_at:
                acc.first_seen_at = item_date
            if not acc.last_seen_at or item_date > acc.last_seen_at:
                acc.last_seen_at = item_date
            if self._is_recent(item_date):
                acc.recent_count += 1

        evidence_level = str(item.get("evidence_level") or "")
        if evidence_level:
            acc.evidence_levels.append(evidence_level)
        acc.priority_scores.append(_to_float(item.get("research_priority_score")))
        acc.source_quality_scores.append(self._source_quality_score(item))

        company = self._format_company(item)
        if company:
            acc.related_companies.add(company)

        if item_id not in acc.source_items:
            acc.source_items[item_id] = {
                "item_id": item_id,
                "concept": item.get("concept", ""),
                "main_dish": item.get("main_dish", ""),
                "chain_node": item.get("chain_node", ""),
                "company_name": item.get("company_name", ""),
                "symbol": item.get("symbol", ""),
                "evidence_level": evidence_level,
                "inbox_status": item.get("inbox_status", ""),
                "last_seen_at": item_date or "",
            }

    def _build_record(
        self,
        acc: _ConceptAccumulator,
        existing_names: Set[str],
    ) -> Dict[str, Any]:
        source_count = len(acc.source_items)
        company_count = len(acc.related_companies)
        root_relevance = min(acc.root_hits / max(source_count * 3, 1), 1.0)
        recent_frequency = min(acc.recent_count / max(source_count, 1), 1.0)
        source_quality = _avg(acc.source_quality_scores)
        company_cooccurrence = min(math.log1p(company_count) / math.log1p(8), 1.0)
        novelty = 0.25 if acc.normalized_name in existing_names else 1.0
        evidence_strength = self._evidence_strength(acc.evidence_levels)
        score = (
            0.25 * root_relevance
            + 0.20 * recent_frequency
            + 0.18 * source_quality
            + 0.15 * company_cooccurrence
            + 0.12 * novelty
            + 0.10 * evidence_strength
        )
        score = round(min(max(score, 0.0), 1.0), 4)
        evidence_level = self._concept_evidence_level(score, source_count)
        related_terms = sorted(acc.related_terms)
        source_items = sorted(
            acc.source_items.values(),
            key=lambda item: str(item.get("last_seen_at", "")),
            reverse=True,
        )[:20]
        discovery_reason = self._discovery_reason(
            source_count=source_count,
            company_count=company_count,
            related_terms=related_terms,
            evidence_level=evidence_level,
        )

        return {
            "concept_name": acc.normalized_name,
            "normalized_name": acc.normalized_name,
            "root_theme": acc.root_theme,
            "parent_concept": acc.parent_concept,
            "related_terms": related_terms,
            "related_companies": sorted(acc.related_companies),
            "source_items": source_items,
            "source_count": source_count,
            "recent_count": acc.recent_count,
            "concept_score": score,
            "novelty_score": round(novelty, 4),
            "evidence_level": evidence_level,
            "discovery_reason": discovery_reason,
            "first_seen_at": acc.first_seen_at,
            "last_seen_at": acc.last_seen_at,
        }

    def _existing_concept_names(self, concept_signals: Iterable[Dict[str, Any]]) -> Set[str]:
        names: set[str] = set()
        for signal in _items(concept_signals):
            if not isinstance(signal, Mapping):
                continue
            context = " ".join(
                str(value)
                for value in [signal.get("name"), signal.get("notes"), signal.get("aliases")]
                if value
            )
            aliases = signal.get("aliases")
            if not isinstance(aliases, (list, tuple)):
                aliases = []
            raw_names = [signal.get("name")] + list(aliases)
            for raw_name in raw_names:
                normalized = self.taxonomy.normalize(str(raw_name or ""), context)
                if normalized:
                    names.add(normalized)
        return names

    def _build_company_candidate_index(
        self,
        company_candidates: Iterable[Dict[str, Any]],
    ) -> Dict[str, Set[str]]:
        index: dict[str, set[str]] = {}
        for candidate in _items(company_candidates):
            if not isinstance(candidate, Mapping):
                continue
            text = self._candidate_text(candidate)
            if not text:
                continue
            for raw_term in self._extract_candidate_terms(text):
                normalized = self.taxonomy.normalize(raw_term, text)
                if not normalized:
                    continue
                company = self._format_company(candidate)
                if company:
                    index.setdefault(normalized, set()).add(company)
        return index

    def _item_text(self, item: Dict[str, Any]) -> str:
        if not isinstance(item, Mapping):
            return ""
        parts: list[str] = []
        for field_name in _TEXT_FIELDS:
            value = item.get(field_name)
            parts.extend(_flatten_text(value))
        return " ".join(part for part in parts if part)

    def _candidate_text(self, candidate: Dict[str, Any]) -> str:
        if not isinstance(candidate, Mapping):
            return ""
        parts: list[str] = []
        for field_name in ("company_name", "symbol", "mapping_sources", "claim_summary"):
            parts.extend(_flatten_text(candidate.get(field_name)))
        return " ".join(parts)

    def _item_date(self, item: Dict[str, Any]) -> str:
        if not isinstance(item, Mapping):
            return ""
        for field_name in _DATE_FIELDS:
            value = item.get(field_name)
            if value:
                parsed = _parse_datetime(str(value))
                if parsed:
                    return parsed.isoformat()
                return str(value)
        return ""

    def _is_recent(self, iso_value: str) -> bool:
        parsed = _parse_datetime(iso_value)
        if parsed is None:
            return False
        cutoff = datetime.now() - timedelta(days=self.lookback_days)
        return parsed >= cutoff

    def _root_hit_count(self, root_theme: str, context: str) -> int:
        root_theme = _text(root_theme)
        context = _text(context)
        if not root_theme or not context:
            return 0
        text_upper = context.upper()
        for theme in self.taxonomy.themes:
            if theme.name != root_theme:
                continue
            return sum(
                1
                for seed in theme.seeds
                if seed in context or seed.upper() in text_upper
            )
        return 0

    def _source_quality_score(self, item: Dict[str, Any]) -> float:
        if not isinstance(item, Mapping):
            return 0.0
        priority_score = min(max(_to_float(item.get("research_priority_score")) / 100.0, 0.0), 1.0)
        priority_level = _text(item.get("research_priority_level"))
        if priority_level == "HIGH_PRIORITY_RESEARCH":
            level_score = 0.9
        elif priority_level == "MEDIUM_PRIORITY_RESEARCH":
            level_score = 0.6
        elif priority_level == "LOW_PRIORITY_RESEARCH":
            level_score = 0.4
        else:
            level_score = 0.25
        evidence_score = self._evidence_strength([str(item.get("evidence_level") or "")])
        return max(priority_score, level_score, evidence_score)

    @staticmethod
    def _evidence_strength(levels: Iterable[str]) -> float:
        weights = {
            "CONFIRMED_SUPPLIER": 0.9,
            "PROBABLE_SUPPLIER": 0.75,
            "EARLY_SIGNAL": 0.6,
            "CONCEPT_CLAIM_ONLY": 0.45,
            "HYPOTHESIS": 0.35,
            "RUMOR_ONLY": 0.2,
            "INSUFFICIENT_EVIDENCE": 0.15,
            "NON_CORE_MARKET": 0.1,
        }
        try:
            levels = iter(levels)
        except TypeError:
            return 0.0
        scores = [
            weights.get(_text(level), 0.15)
            for level in levels
            if _text(level)
        ]
        return _avg(scores)

    @staticmethod
    def _concept_evidence_level(score: float, source_count: int) -> str:
        score = _to_float(score)
        try:
            source_count = max(0, int(source_count))
        except (TypeError, ValueError):
            source_count = 0
        if score >= 0.62 and source_count >= 2:
            return "EARLY_SIGNAL"
        if score >= 0.38 or source_count >= 2:
            return "CONCEPT_CLAIM_ONLY"
        return "HYPOTHESIS"

    @staticmethod
    def _format_company(item: Dict[str, Any]) -> str:
        if not isinstance(item, Mapping):
            return ""
        company = _text(item.get("company_name")).strip()
        symbol = _text(item.get("symbol")).strip()
        if not company:
            return ""
        if symbol and symbol not in company and symbol not in {"未上市", "None", "null"}:
            return f"{company}({symbol})"
        return company

    @staticmethod
    def _discovery_reason(
        source_count: int,
        company_count: int,
        related_terms: List[str],
        evidence_level: str,
    ) -> str:
        terms = "、".join(related_terms[:5]) if related_terms else "暂无"
        return (
            f"来自 {source_count} 条 inbox 线索，关联 {company_count} 个公司共现；"
            f"核心词：{terms}；概念证据等级：{evidence_level}。"
        )

    @staticmethod
    def _branch_counts(
        discovered: Iterable[Dict[str, Any]],
        existing_names: Set[str],
    ) -> Dict[str, int]:
        counts: dict[str, int] = {"光": 0, "存": 0, "算": 0, "电": 0}
        for row in _items(discovered):
            if not isinstance(row, Mapping):
                continue
            name = _text(row.get("normalized_name"))
            if name in existing_names:
                continue
            root = _text(row.get("root_theme"))
            if root in counts:
                counts[root] += 1
        return counts


def _flatten_text(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            parts.extend(_flatten_text(item))
        return parts
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(_flatten_text(item))
        return parts
    return [str(value)]


def _parse_datetime(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[: len(fmt)], fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _to_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _avg(values: Iterable[float]) -> float:
    vals: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            vals.append(parsed)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)
