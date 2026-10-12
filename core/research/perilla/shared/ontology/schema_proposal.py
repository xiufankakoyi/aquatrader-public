"""Schema proposal data model.

Represents a proposed extension or modification to the canonical ontology.
A proposal may include new relations, node types, event types, axes, path types,
event-to-relation mappings, signal-to-event mappings, or Perilla-source-to-SAG-source
mappings. Proposals move through:

  draft → pending_review → approved|rejected → applied
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SchemaProposal:
    """Proposal for adding or modifying ontology schema elements."""

    proposal_id: str
    title: str
    description: str = ""
    rationale: str = ""
    # Proposed schema additions
    proposed_relations: list[dict[str, Any]] = field(default_factory=list)
    proposed_node_types: list[str] = field(default_factory=list)
    proposed_event_types: list[dict[str, Any]] = field(default_factory=list)
    proposed_axes: list[dict[str, Any]] = field(default_factory=list)
    proposed_path_types: list[dict[str, Any]] = field(default_factory=list)
    proposed_event_to_relation: list[dict[str, str]] = field(default_factory=list)
    proposed_signal_to_event: list[dict[str, str]] = field(default_factory=list)
    proposed_perilla_source_to_sag: list[dict[str, str]] = field(default_factory=list)
    # Supporting evidence and review metadata
    evidence: list[dict[str, Any]] = field(default_factory=list)
    status: str = "draft"
    submitted_by: str = ""
    reviewed_by: str = ""
    review_comment: str = ""
    promotion_score: float = 0.0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        """Ensure timestamps are set."""
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        """Serialize proposal to a plain dictionary."""
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "proposed_relations": self.proposed_relations,
            "proposed_node_types": self.proposed_node_types,
            "proposed_event_types": self.proposed_event_types,
            "proposed_axes": self.proposed_axes,
            "proposed_path_types": self.proposed_path_types,
            "proposed_event_to_relation": self.proposed_event_to_relation,
            "proposed_signal_to_event": self.proposed_signal_to_event,
            "proposed_perilla_source_to_sag": self.proposed_perilla_source_to_sag,
            "evidence": self.evidence,
            "status": self.status,
            "submitted_by": self.submitted_by,
            "reviewed_by": self.reviewed_by,
            "review_comment": self.review_comment,
            "promotion_score": self.promotion_score,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SchemaProposal":
        """Deserialize proposal from a plain dictionary."""
        data = dict(data) if isinstance(data, Mapping) else {}

        def _list_value(name: str) -> list[Any]:
            value = data.get(name, [])
            return list(value) if isinstance(value, (list, tuple)) else []

        try:
            promotion_score = float(data.get("promotion_score", 0.0))
        except (TypeError, ValueError):
            promotion_score = 0.0
        if not math.isfinite(promotion_score):
            promotion_score = 0.0
        return cls(
            proposal_id=str(data.get("proposal_id", "")),
            title=data.get("title", ""),
            description=data.get("description", ""),
            rationale=data.get("rationale", ""),
            proposed_relations=_list_value("proposed_relations"),
            proposed_node_types=_list_value("proposed_node_types"),
            proposed_event_types=_list_value("proposed_event_types"),
            proposed_axes=_list_value("proposed_axes"),
            proposed_path_types=_list_value("proposed_path_types"),
            proposed_event_to_relation=_list_value("proposed_event_to_relation"),
            proposed_signal_to_event=_list_value("proposed_signal_to_event"),
            proposed_perilla_source_to_sag=_list_value("proposed_perilla_source_to_sag"),
            evidence=_list_value("evidence"),
            status=data.get("status", "draft"),
            submitted_by=data.get("submitted_by", ""),
            reviewed_by=data.get("reviewed_by", ""),
            review_comment=data.get("review_comment", ""),
            promotion_score=promotion_score,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now().isoformat()

    def is_empty(self) -> bool:
        """Return True if the proposal does not contain any schema changes."""
        return not any([
            self.proposed_relations,
            self.proposed_node_types,
            self.proposed_event_types,
            self.proposed_axes,
            self.proposed_path_types,
            self.proposed_event_to_relation,
            self.proposed_signal_to_event,
            self.proposed_perilla_source_to_sag,
        ])
