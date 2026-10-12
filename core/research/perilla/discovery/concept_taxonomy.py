"""Taxonomy and normalization rules for Perilla concept discovery.

The source stays ASCII-only so Windows console encoding cannot corrupt the
Chinese domain vocabulary. Runtime strings are regular Unicode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence


ROOT_LIGHT = "\u5149"
ROOT_MEMORY = "\u5b58"
ROOT_COMPUTE = "\u7b97"
ROOT_POWER = "\u7535"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class ThemeDefinition:
    """Root theme definition for concept discovery."""

    name: str
    seeds: tuple[str, ...]


THEME_DEFINITIONS: tuple[ThemeDefinition, ...] = (
    ThemeDefinition(
        name=ROOT_LIGHT,
        seeds=(
            "\u5149\u6a21\u5757", "CPO", "LPO", "NPO",
            "\u7845\u5149", "\u5149\u82af\u7247", "\u5149\u5f15\u64ce",
            "\u5149\u4e92\u8fde", "1.6T", "800G",
            "\u8584\u819c\u94cc\u9178\u9502",
            "\u78f7\u5316\u94df", "InP",
        ),
    ),
    ThemeDefinition(
        name=ROOT_MEMORY,
        seeds=(
            "HBM", "DRAM", "NAND", "DDR5", "CXL", "SSD",
            "\u5b58\u50a8\u63a7\u5236\u5668",
            "\u5148\u8fdb\u5c01\u88c5",
        ),
    ),
    ThemeDefinition(
        name=ROOT_COMPUTE,
        seeds=(
            "GPU", "ASIC", "AI\u82af\u7247", "AI\u670d\u52a1\u5668",
            "\u4ea4\u6362\u673a", "InfiniBand", "\u4ee5\u592a\u7f51",
            "\u6db2\u51b7", "\u7b97\u529b\u96c6\u7fa4",
            "GB200", "H100", "H200", "B200", "NVL72",
        ),
    ),
    ThemeDefinition(
        name=ROOT_POWER,
        seeds=(
            "\u7535\u6e90\u6a21\u5757", "UPS",
            "\u9ad8\u538b\u76f4\u6d41", "HVDC", "800V",
            "\u53d8\u538b\u5668", "\u6bcd\u7ebf", "\u94dc\u6392",
            "PDU", "\u8fde\u63a5\u5668",
        ),
    ),
)

ALIAS_NORMALIZATION: Dict[str, str] = {
    "CPO": "CPO",
    "\u5171\u5c01\u88c5\u5149\u5b66": "CPO",
    "CO-PACKAGED OPTICS": "CPO",
    "CO PACKAGED OPTICS": "CPO",
    "LPO": "LPO",
    "LINEAR-DRIVE PLUGGABLE OPTICS": "LPO",
    "LINEAR DRIVE PLUGGABLE OPTICS": "LPO",
    "NPO": "NPO",
    "NEAR-PACKAGED OPTICS": "NPO",
    "NEAR PACKAGED OPTICS": "NPO",
    "\u9ad8\u538b\u76f4\u6d41": "\u6570\u636e\u4e2d\u5fc3\u9ad8\u538b\u76f4\u6d41\u4f9b\u7535",
    "HVDC": "\u6570\u636e\u4e2d\u5fc3\u9ad8\u538b\u76f4\u6d41\u4f9b\u7535",
    "\u76f4\u6d41\u4f9b\u7535": "\u6570\u636e\u4e2d\u5fc3\u9ad8\u538b\u76f4\u6d41\u4f9b\u7535",
}

DATA_CENTER_800V_MARKERS: tuple[str, ...] = (
    "\u6570\u636e\u4e2d\u5fc3", "\u670d\u52a1\u5668", "HVDC",
    "\u7535\u6e90", "\u4f9b\u7535", "PDU", "\u6bcd\u7ebf",
)

EV_800V_MARKERS: tuple[str, ...] = (
    "\u65b0\u80fd\u6e90\u8f66", "\u65b0\u80fd\u6e90\u6c7d\u8f66",
    "\u5feb\u5145", "\u7535\u9a71", "SiC", "\u78b3\u5316\u7845",
)

PARENT_CONCEPTS: Dict[str, str] = {
    "CPO": "\u5149\u4e92\u8fde",
    "LPO": "\u5149\u4e92\u8fde",
    "NPO": "\u5149\u4e92\u8fde",
    "\u7845\u5149": "\u5149\u4e92\u8fde",
    "\u5149\u5f15\u64ce": "\u5149\u4e92\u8fde",
    "\u5149\u4e92\u8fde": "\u5149\u4e92\u8fde",
    "\u8584\u819c\u94cc\u9178\u9502": "\u9ad8\u901f\u5149\u5668\u4ef6",
    "\u5149\u6a21\u5757": "\u5149\u901a\u4fe1\u5668\u4ef6",
    "\u5149\u82af\u7247": "\u5149\u901a\u4fe1\u5668\u4ef6",
    "1.6T": "\u9ad8\u901f\u5149\u6a21\u5757",
    "800G": "\u9ad8\u901f\u5149\u6a21\u5757",
    "\u73af\u6c27\u6811\u8102": "\u7535\u5b50\u5c01\u88c5\u6750\u6599",
    "\u805a\u9170\u4e9a\u80fa": "\u9ad8\u6027\u80fd\u7535\u5b50\u6750\u6599",
    "\u9676\u74f7\u57fa\u677f": "\u9ad8\u6027\u80fd\u5c01\u88c5\u6750\u6599",
    "\u73bb\u7483\u57fa\u677f": "\u9ad8\u6027\u80fd\u5c01\u88c5\u6750\u6599",
    "\u6709\u673a\u7845": "\u7535\u5b50\u6750\u6599",
    "\u5bfc\u70ed\u6750\u6599": "\u70ed\u7ba1\u7406\u6750\u6599",
    "\u7535\u78c1\u5c4f\u853d\u6750\u6599": "\u7535\u78c1\u517c\u5bb9\u6750\u6599",
    "HBM": "\u5b58\u50a8",
    "DRAM": "\u5b58\u50a8",
    "NAND": "\u5b58\u50a8",
    "DDR5": "\u5b58\u50a8",
    "CXL": "\u5185\u5b58\u4e92\u8fde",
    "SSD": "\u5b58\u50a8",
    "\u5b58\u50a8\u63a7\u5236\u5668": "\u5b58\u50a8",
    "\u5148\u8fdb\u5c01\u88c5": "\u5148\u8fdb\u5c01\u88c5",
    "GPU": "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd",
    "ASIC": "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd",
    "AI\u82af\u7247": "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd",
    "AI\u670d\u52a1\u5668": "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd",
    "\u4ea4\u6362\u673a": "AI\u7b97\u529b\u7f51\u7edc",
    "InfiniBand": "AI\u7b97\u529b\u7f51\u7edc",
    "\u4ee5\u592a\u7f51": "AI\u7b97\u529b\u7f51\u7edc",
    "\u6db2\u51b7": "AI\u670d\u52a1\u5668\u6563\u70ed",
    "\u7b97\u529b\u96c6\u7fa4": "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd",
    "\u6570\u636e\u4e2d\u5fc3\u9ad8\u538b\u76f4\u6d41\u4f9b\u7535": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u6570\u636e\u4e2d\u5fc3800V\u4f9b\u7535": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u65b0\u80fd\u6e90\u8f66800V\u5e73\u53f0": "\u9ad8\u538b\u7535\u6c14\u5e73\u53f0",
    "\u7535\u6e90\u6a21\u5757": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "UPS": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u53d8\u538b\u5668": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u6bcd\u7ebf": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u94dc\u6392": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "PDU": "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535",
    "\u8fde\u63a5\u5668": "\u7535\u6c14\u8fde\u63a5",
}

EXTRA_CANDIDATE_TERMS: tuple[str, ...] = (
    "PCB", "\u94dc\u7f06", "\u5149\u901a\u4fe1",
    "\u5149\u5668\u4ef6", "\u6fc0\u5149\u5668", "\u8c03\u5236\u5668",
    "\u76f8\u5e72\u5149", "\u5149\u7535\u5171\u5c01\u88c5",
    "\u7845\u5149\u82af\u7247", "\u5149\u7535\u8f6c\u6362",
    "\u6db2\u51b7\u677f", "\u6d78\u6ca1\u5f0f\u6db2\u51b7",
)


class ConceptTaxonomy:
    """Small rule-based taxonomy used by concept discovery and scouting."""

    def __init__(self, themes: Sequence[ThemeDefinition] = THEME_DEFINITIONS) -> None:
        self._themes = tuple(
            theme for theme in (themes or ()) if isinstance(theme, ThemeDefinition)
        )
        self._seed_to_theme: Dict[str, str] = {}
        for theme in self._themes:
            for seed in theme.seeds:
                seed_text = _text(seed)
                if seed_text:
                    self._seed_to_theme[seed_text.upper()] = theme.name
                    self._seed_to_theme[seed_text] = theme.name

    @property
    def themes(self) -> tuple[ThemeDefinition, ...]:
        return self._themes

    def candidate_terms(self) -> List[str]:
        terms: list[str] = []
        for theme in self._themes:
            terms.extend(theme.seeds)
        terms.extend(ALIAS_NORMALIZATION.keys())
        terms.extend(EXTRA_CANDIDATE_TERMS)
        return sorted(set(terms), key=len, reverse=True)

    def seed_terms(self) -> List[str]:
        return [seed for theme in self._themes for seed in theme.seeds]

    def is_known_term(self, normalized_name: str) -> bool:
        name = _text(normalized_name)
        return bool(
            name in PARENT_CONCEPTS
            or name.upper() in self._seed_to_theme
            or name in self._seed_to_theme
        )

    def normalize(self, term: str, context: str = "") -> str:
        cleaned = self._clean_term(term)
        if not cleaned:
            return ""

        upper = cleaned.upper()
        if "800V" in upper or cleaned == "800\uff36":
            if self._contains_any(context, DATA_CENTER_800V_MARKERS):
                return "\u6570\u636e\u4e2d\u5fc3800V\u4f9b\u7535"
            if self._contains_any(context, EV_800V_MARKERS):
                return "\u65b0\u80fd\u6e90\u8f66800V\u5e73\u53f0"
            return "800V"

        if upper in ALIAS_NORMALIZATION:
            return ALIAS_NORMALIZATION[upper]
        if cleaned in ALIAS_NORMALIZATION:
            return ALIAS_NORMALIZATION[cleaned]
        return cleaned

    def root_theme_for(self, normalized_name: str, context: str = "") -> str:
        normalized_name = _text(normalized_name)
        context = _text(context)
        if not normalized_name:
            return ""

        root = self._seed_to_theme.get(normalized_name) or self._seed_to_theme.get(
            normalized_name.upper()
        )
        if root:
            return root

        theme_scores: dict[str, int] = {}
        text_upper = context.upper()
        for theme in self._themes:
            score = 0
            for seed in theme.seeds:
                if seed in context or seed.upper() in text_upper:
                    score += 1
            if normalized_name in context or normalized_name.upper() in text_upper:
                score += 2
            if score:
                theme_scores[theme.name] = score

        if theme_scores:
            return max(theme_scores.items(), key=lambda item: item[1])[0]

        if normalized_name in (
            "\u6570\u636e\u4e2d\u5fc3800V\u4f9b\u7535",
            "\u65b0\u80fd\u6e90\u8f66800V\u5e73\u53f0",
        ):
            return ROOT_POWER
        if normalized_name in ("PCB", "\u94dc\u7f06", "\u8fde\u63a5\u5668"):
            return ROOT_POWER
        return ""

    def parent_concept_for(self, normalized_name: str, root_theme: str) -> str:
        normalized_name = _text(normalized_name)
        root_theme = _text(root_theme)
        if normalized_name in PARENT_CONCEPTS:
            return PARENT_CONCEPTS[normalized_name]
        if root_theme == ROOT_LIGHT:
            return "\u5149\u4e92\u8fde"
        if root_theme == ROOT_MEMORY:
            return "\u5b58\u50a8"
        if root_theme == ROOT_COMPUTE:
            return "AI\u7b97\u529b\u57fa\u7840\u8bbe\u65bd"
        if root_theme == ROOT_POWER:
            return "\u6570\u636e\u4e2d\u5fc3\u4f9b\u7535"
        return ""

    @staticmethod
    def _contains_any(text: str, markers: Iterable[str]) -> bool:
        text = _text(text)
        if not text:
            return False
        text_upper = text.upper()
        try:
            markers = iter(markers)
        except TypeError:
            return False
        return any(
            _text(marker) and (_text(marker) in text or _text(marker).upper() in text_upper)
            for marker in markers
        )

    @staticmethod
    def _clean_term(term: str) -> str:
        return (
            _text(term)
            .strip(" \t\r\n,\uff0c.\u3002;\uff1b:\uff1a()\uff08\uff09[]\u3010\u3011<>\u300a\u300b")
        )
