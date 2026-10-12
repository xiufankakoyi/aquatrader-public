"""Competency questions for ontology validation.

Defines canonical questions used to evaluate whether a claim, event, or
candidate satisfies the domain ontology.

TODO: Expand with concrete competency question templates and scoring rubrics.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompetencyQuestion:
    """A single competency question used to validate ontology coverage."""

    question_id: str
    question_text: str
    expected_answer_type: str = "boolean"


SIX_QUESTION_KEYS: frozenset[str] = frozenset({
    "attach_rate", "bom_share", "supplier_concentration",
    "capacity_expansion", "switching_cost", "pricing_state",
})


DEFAULT_COMPETENCY_QUESTIONS: list[CompetencyQuestion] = [
    CompetencyQuestion("cq_001", "该节点是否属于已知材料/设备/工艺轴？", "axis"),
    CompetencyQuestion("cq_002", "该关系是否有明确的上下游语义？", "boolean"),
    CompetencyQuestion("cq_003", "该证据是否来自可溯源的来源？", "boolean"),
]
