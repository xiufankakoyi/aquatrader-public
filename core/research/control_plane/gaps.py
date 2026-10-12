"""研究缺口分类与可解释优先级。节点类型不直接决定任务价值。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .contracts import ResearchTask, _as_mapping, _safe_float, _safe_text


class GapType(str, Enum):
    ONTOLOGY = "ontology_missing"
    EVIDENCE = "evidence_missing"
    MAPPING = "mapping_missing"
    CONFLICT = "conflict_review"
    EXPIRY = "expiry_revalidation"


@dataclass
class ResearchGap:
    gap_id: str
    gap_type: str
    target_claim_id: str = ""
    research_importance: float = 0.5
    evidence_gap: float = 0.5
    expected_information_gain: float = 0.5
    timeliness: float = 0.5
    cost_factor: float = 0.5
    node_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.gap_id = _safe_text(self.gap_id)
        self.gap_type = _safe_text(self.gap_type, GapType.EVIDENCE.value)
        if self.gap_type not in {item.value for item in GapType}:
            self.gap_type = GapType.EVIDENCE.value
        self.target_claim_id = _safe_text(self.target_claim_id)
        for name in (
            "research_importance", "evidence_gap", "expected_information_gain",
            "timeliness", "cost_factor",
        ):
            value = _safe_float(getattr(self, name), 0.5)
            setattr(self, name, max(0.0, min(1.0, value)))
        self.node_type = _safe_text(self.node_type)
        self.metadata = _as_mapping(self.metadata)

    @property
    def priority(self) -> float:
        benefit = (
            max(0.0, min(1.0, self.research_importance))
            * max(0.0, min(1.0, self.evidence_gap))
            * max(0.0, min(1.0, self.expected_information_gain))
            * max(0.0, min(1.0, self.timeliness))
        ) ** 0.25
        cost_discount = 1.0 - 0.5 * max(0.0, min(1.0, self.cost_factor))
        return round(benefit * cost_discount, 4)

    def to_task(self, trigger_source: str = "gap_analyzer") -> ResearchTask:
        params = _as_mapping(self.metadata)
        params.update({
            "gap_id": self.gap_id,
            "gap_type": self.gap_type,
            "node_type": self.node_type,
        })
        return ResearchTask(
            task_type=self.gap_type,
            trigger_source=_safe_text(trigger_source, "gap_analyzer"),
            target_claim_id=self.target_claim_id,
            priority=self.priority,
            params=params,
        )

