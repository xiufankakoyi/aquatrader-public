"""Schema evolution engine.

Applies approved schema proposals to the canonical ontology in a versioned
and auditable manner. Works together with ``SchemaPromotionGate`` and
``OntologyVersioning``.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry
from core.research.perilla.shared.ontology.ontology_versioning import (
    OntologyVersion,
    OntologyVersioning,
)
from core.research.perilla.shared.ontology.schema_proposal import SchemaProposal
from core.research.perilla.shared.ontology.schema_promotion_gate import (
    SchemaPromotionGate,
)

logger = logging.getLogger(__name__)


class SchemaEvolutionEngine:
    """Engine for applying ontology schema changes."""

    def __init__(
        self,
        registry: OntologyRegistry,
        versioning: OntologyVersioning | None = None,
        gate: SchemaPromotionGate | None = None,
    ) -> None:
        """Initialize evolution engine.

        Args:
            registry: Ontology registry to evolve.
            versioning: Versioning tracker. If None, a default one is created.
            gate: Promotion gate. If None, a default one is created.
        """
        self.registry = registry
        self.versioning = versioning if versioning is not None else OntologyVersioning()
        self.gate = gate if gate is not None else SchemaPromotionGate()

    def propose(
        self,
        title: str,
        description: str = "",
        rationale: str = "",
        submitted_by: str = "",
        **changes: Any,
    ) -> SchemaProposal:
        """Create a new schema proposal from proposed changes.

        Args:
            title: Short title for the proposal.
            description: Detailed description.
            rationale: Why the change is needed.
            submitted_by: Submitter identifier.
            **changes: Keyword arguments mapping to proposal fields, e.g.
                ``proposed_relations``, ``proposed_path_types``.

        Returns:
            A new SchemaProposal in ``draft`` status.
        """
        import uuid

        proposal = SchemaProposal(
            proposal_id=f"schema-prop-{uuid.uuid4().hex[:12]}",
            title=title,
            description=description,
            rationale=rationale,
            submitted_by=submitted_by,
            status="draft",
        )
        for field in (
            "proposed_relations",
            "proposed_node_types",
            "proposed_event_types",
            "proposed_axes",
            "proposed_path_types",
            "proposed_event_to_relation",
            "proposed_signal_to_event",
            "proposed_perilla_source_to_sag",
            "evidence",
        ):
            if field in changes:
                value = changes[field]
                setattr(
                    proposal,
                    field,
                    list(value) if isinstance(value, (list, tuple)) else [],
                )
        return proposal

    def submit_for_review(
        self,
        proposal: SchemaProposal,
        score: float = 0.0,
    ) -> None:
        """Submit a draft proposal for review.

        Args:
            proposal: Proposal to submit.
            score: Optional promotion score computed from evidence.
        """
        if not isinstance(proposal, SchemaProposal):
            raise TypeError("proposal must be a SchemaProposal")
        proposal.status = "pending_review"
        try:
            normalized_score = float(score)
        except (TypeError, ValueError):
            normalized_score = 0.0
        proposal.promotion_score = (
            normalized_score if math.isfinite(normalized_score) else 0.0
        )
        proposal.touch()

    def apply(self, proposal: SchemaProposal) -> dict[str, Any]:
        """Apply an approved schema proposal to the ontology registry.

        Args:
            proposal: Schema proposal to apply.

        Returns:
            Result dict with ``success``, ``version``, ``changes``, and ``reason``.
        """
        if not isinstance(proposal, SchemaProposal):
            return {
                "success": False,
                "version": self.registry.version,
                "changes": [],
                "reason": "proposal must be a SchemaProposal",
            }

        gate_result = self.gate.can_promote(proposal, self.registry)
        if not gate_result["passed"]:
            return {
                "success": False,
                "version": self.registry.version,
                "changes": [],
                "reason": gate_result["reason"],
            }

        applied_changes: list[dict[str, Any]] = []

        for rel in self._mappings(proposal.proposed_relations):
            value = rel.get("value", "")
            if not value:
                continue
            self.registry.register_relation(
                value,
                {k: v for k, v in rel.items() if k != "value"},
            )
            applied_changes.append({"type": "relation", "value": value})

        for node_type in self._strings(proposal.proposed_node_types):
            self.registry.register_node_type(node_type)
            applied_changes.append({"type": "node_type", "value": node_type})

        for evt in self._mappings(proposal.proposed_event_types):
            value = evt.get("value", "")
            if not value:
                continue
            self.registry.register_event_type(
                value,
                {k: v for k, v in evt.items() if k != "value"},
            )
            applied_changes.append({"type": "event_type", "value": value})

        for axis in self._mappings(proposal.proposed_axes):
            value = axis.get("value", "")
            if not value:
                continue
            self.registry.register_axis(
                value,
                {k: v for k, v in axis.items() if k != "value"},
            )
            applied_changes.append({"type": "axis", "value": value})

        for pt in self._mappings(proposal.proposed_path_types):
            value = pt.get("value", "")
            if not value:
                continue
            self.registry.register_path_type(
                value,
                {k: v for k, v in pt.items() if k != "value"},
            )
            applied_changes.append({"type": "path_type", "value": value})

        for mapping in self._mappings(proposal.proposed_event_to_relation):
            event_type = mapping.get("event_type", "")
            relation = mapping.get("relation", "")
            if not event_type or not relation:
                continue
            self.registry.register_event_to_relation(event_type, relation)
            applied_changes.append({
                "type": "event_to_relation",
                "event_type": event_type,
                "relation": relation,
            })

        for mapping in self._mappings(proposal.proposed_signal_to_event):
            signal_type = mapping.get("signal_type", "")
            event_type = mapping.get("event_type", "")
            if not signal_type or not event_type:
                continue
            self.registry.register_signal_to_event(signal_type, event_type)
            applied_changes.append({
                "type": "signal_to_event",
                "signal_type": signal_type,
                "event_type": event_type,
            })

        for mapping in self._mappings(proposal.proposed_perilla_source_to_sag):
            perilla_source = mapping.get("perilla_source", "")
            sag_source = mapping.get("sag_source", "")
            if not perilla_source or not sag_source:
                continue
            self.registry.register_perilla_source_to_sag(perilla_source, sag_source)
            applied_changes.append({
                "type": "perilla_source_to_sag",
                "perilla_source": perilla_source,
                "sag_source": sag_source,
            })

        if not applied_changes:
            return {
                "success": False,
                "version": self.registry.version,
                "changes": [],
                "reason": "no applicable changes found in proposal",
            }

        new_version = self.versioning.next_patch_version(self.registry.version)
        self.registry.set_version(new_version)
        proposal.status = "applied"
        proposal.touch()

        version = OntologyVersion(
            version=new_version,
            description=proposal.title,
            changes=applied_changes,
            applied_by=proposal.reviewed_by or "system",
            proposal_id=proposal.proposal_id,
        )
        self.versioning.register(version)

        return {
            "success": True,
            "version": new_version,
            "changes": applied_changes,
            "reason": "proposal applied successfully",
        }

    @staticmethod
    def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
        """Return only mapping entries from a proposal collection."""
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        """Return non-empty string entries from a proposal collection."""
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, str) and item)

    def auto_promote_candidate(
        self,
        title: str,
        promotion_score: float,
        **changes: Any,
    ) -> dict[str, Any]:
        """Convenience method for promoting high-confidence discovered candidates.

        Creates a proposal, marks it approved (bypassing manual review if the
        gate is configured to allow it), and applies it.

        Args:
            title: Proposal title.
            promotion_score: Confidence score for the candidate.
            **changes: Proposed schema changes.

        Returns:
            Result dict from ``apply``.
        """
        proposal = self.propose(title, **changes)
        try:
            normalized_score = float(promotion_score)
        except (TypeError, ValueError):
            normalized_score = 0.0
        proposal.promotion_score = (
            normalized_score if math.isfinite(normalized_score) else 0.0
        )
        self.gate.review(proposal, approved=True, reviewer="auto_promote")
        return self.apply(proposal)
