"""Shared confidence and evidence vocabulary constants.

Centralises confidence levels, evidence levels, and review statuses used across
Perilla discovery, evidence, and alpha modules.
"""
from __future__ import annotations


CONFIDENCE_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

CREDIBILITY_HINTS: frozenset[str] = frozenset({"high", "medium", "low", "unknown"})

EVIDENCE_LEVELS: frozenset[str] = frozenset({
    "CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER", "CONCEPT_CLAIM_ONLY",
    "EARLY_SIGNAL", "RUMOR_ONLY", "HYPOTHESIS", "MAIN_ANCHOR",
    "NON_CORE_MARKET", "INSUFFICIENT_EVIDENCE",
})

# 便捷别名：常用证据等级
EVIDENCE_LEVEL_CONFIRMED = "CONFIRMED_SUPPLIER"
EVIDENCE_LEVEL_PROBABLE = "PROBABLE_SUPPLIER"
EVIDENCE_LEVEL_CLAIM_ONLY = "CONCEPT_CLAIM_ONLY"
EVIDENCE_LEVEL_EARLY_SIGNAL = "EARLY_SIGNAL"
EVIDENCE_LEVEL_RUMOR = "RUMOR_ONLY"
EVIDENCE_LEVEL_HYPOTHESIS = "HYPOTHESIS"
EVIDENCE_LEVEL_MAIN_ANCHOR = "MAIN_ANCHOR"
EVIDENCE_LEVEL_NON_CORE = "NON_CORE_MARKET"
EVIDENCE_LEVEL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

REVIEW_STATUSES: frozenset[str] = frozenset({
    "auto_pass", "pending_human_review", "rejected",
})
