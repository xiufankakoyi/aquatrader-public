"""Schema promotion gate.

Decides whether a proposed schema change or an event-graph candidate is ready
to be promoted into the canonical/stable ontology.
"""
from __future__ import annotations

from typing import Any

from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry
from core.research.perilla.shared.ontology.schema_proposal import SchemaProposal


class SchemaPromotionGate:
    """Gatekeeper for promoting schema proposals or candidates into ontology."""

    def __init__(
        self,
        min_score: float = 0.7,
        require_review: bool = True,
        allow_conflicts: bool = False,
    ) -> None:
        """Initialize the gate with promotion thresholds.

        Args:
            min_score: Minimum score required for promotion.
            require_review: If True, proposals must be explicitly approved.
            allow_conflicts: If False, proposals conflicting with existing
                canonical terms are rejected.
        """
        self.min_score = min_score
        self.require_review = require_review
        self.allow_conflicts = allow_conflicts

    def can_promote(
        self,
        proposal: SchemaProposal,
        registry: OntologyRegistry,
    ) -> dict[str, Any]:
        """Evaluate whether a proposal can be promoted.

        Args:
            proposal: Schema proposal or candidate metadata.
            registry: Current ontology registry to check conflicts against.

        Returns:
            Dict with ``passed`` (bool), ``reason`` (str), and ``score`` (float).
        """
        if proposal.is_empty():
            return {
                "passed": False,
                "reason": "proposal contains no schema changes",
                "score": proposal.promotion_score,
            }

        if proposal.promotion_score < self.min_score:
            return {
                "passed": False,
                "reason": f"promotion_score={proposal.promotion_score:.3f} below min_score={self.min_score:.3f}",
                "score": proposal.promotion_score,
            }

        if self.require_review and proposal.status not in ("approved",):
            return {
                "passed": False,
                "reason": f"status={proposal.status} not approved",
                "score": proposal.promotion_score,
            }

        if proposal.status == "rejected":
            return {
                "passed": False,
                "reason": "proposal has been rejected",
                "score": proposal.promotion_score,
            }

        conflicts = self._detect_conflicts(proposal, registry)
        if conflicts and not self.allow_conflicts:
            return {
                "passed": False,
                "reason": f"conflicts with existing ontology: {conflicts}",
                "score": proposal.promotion_score,
            }

        validation = self._validate_terms(proposal, registry)
        if not validation["valid"]:
            return {
                "passed": False,
                "reason": validation["reason"],
                "score": proposal.promotion_score,
            }

        return {
            "passed": True,
            "reason": "promotion gate passed",
            "score": proposal.promotion_score,
        }

    def review(
        self,
        proposal: SchemaProposal,
        approved: bool,
        reviewer: str = "",
        comment: str = "",
    ) -> None:
        """Apply a manual review decision to a proposal.

        Args:
            proposal: Proposal to review.
            approved: True to approve, False to reject.
            reviewer: Reviewer identifier.
            comment: Optional review comment.
        """
        proposal.status = "approved" if approved else "rejected"
        proposal.reviewed_by = reviewer
        proposal.review_comment = comment
        proposal.touch()

    def _detect_conflicts(
        self,
        proposal: SchemaProposal,
        registry: OntologyRegistry,
    ) -> list[str]:
        """Detect conflicts between proposal and existing canonical terms."""
        conflicts: list[str] = []

        for rel in proposal.proposed_relations:
            relation_value = rel.get("value", "")
            if registry.is_valid_relation(relation_value):
                conflicts.append(f"relation:{relation_value}")

        for node_type in proposal.proposed_node_types:
            if registry.is_valid_node_type(node_type):
                conflicts.append(f"node:{node_type}")

        for evt in proposal.proposed_event_types:
            event_value = evt.get("value", "")
            if registry.is_valid_event_type(event_value):
                conflicts.append(f"event:{event_value}")

        for axis in proposal.proposed_axes:
            axis_value = axis.get("value", "")
            if registry.is_valid_axis(axis_value):
                conflicts.append(f"axis:{axis_value}")

        for pt in proposal.proposed_path_types:
            path_type = pt.get("value", "")
            if path_type in registry.all_path_types():
                conflicts.append(f"path_type:{path_type}")

        for mapping in proposal.proposed_event_to_relation:
            event_type = mapping.get("event_type", "")
            existing = registry.event_type_to_relation(event_type)
            if existing and existing != mapping.get("relation"):
                conflicts.append(f"event_to_relation:{event_type}")

        return conflicts

    def _validate_terms(
        self,
        proposal: SchemaProposal,
        registry: OntologyRegistry,
    ) -> dict[str, Any]:
        """Validate that proposed terms reference valid existing terms."""
        for mapping in proposal.proposed_event_to_relation:
            relation = mapping.get("relation", "")
            if relation and not registry.is_valid_relation(relation):
                return {
                    "valid": False,
                    "reason": f"event_to_relation references unknown relation: {relation}",
                }

        for mapping in proposal.proposed_signal_to_event:
            event_type = mapping.get("event_type", "")
            if event_type and not registry.is_valid_event_type(event_type):
                return {
                    "valid": False,
                    "reason": f"signal_to_event references unknown event_type: {event_type}",
                }

        for mapping in proposal.proposed_perilla_source_to_sag:
            sag_source = mapping.get("sag_source", "")
            if sag_source and not registry.is_valid_source_type(sag_source):
                return {
                    "valid": False,
                    "reason": f"perilla_source_to_sag references unknown sag_source: {sag_source}",
                }

        for pt in proposal.proposed_path_types:
            for axis in pt.get("required_axes", []):
                if not registry.is_valid_axis(axis):
                    return {
                        "valid": False,
                        "reason": f"path_type references unknown axis: {axis}",
                    }

        return {"valid": True, "reason": ""}
