"""Ontology validation helpers.

Validates node types, relation types, event types, and statuses against the
canonical ontology value sets.

TODO: Add stricter validation rules (e.g. allowed head/tail node types per
relation) once the ontology is formalised.
"""
from __future__ import annotations

from core.research.perilla.shared.ontology.ontology_models import (
    CLAIM_STATUS_SET,
    EVENT_CLAIM_STATUSES,
)
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry


def _registry(registry: OntologyRegistry | None) -> OntologyRegistry:
    return registry if registry is not None else OntologyRegistry.default()


def is_valid_node_type(node_type: str, registry: OntologyRegistry | None = None) -> bool:
    """Return True if node_type is a known ontology node type."""
    return isinstance(node_type, str) and _registry(registry).is_valid_node_type(node_type)


def is_valid_relation_type(relation: str, registry: OntologyRegistry | None = None) -> bool:
    """Return True if relation is a known ontology relation type."""
    return isinstance(relation, str) and _registry(registry).is_valid_relation(relation)


def is_valid_event_type(event_type: str, registry: OntologyRegistry | None = None) -> bool:
    """Return True if event_type is a known ontology event type."""
    return isinstance(event_type, str) and _registry(registry).is_valid_event_type(event_type)


def is_valid_source_type(source_type: str, registry: OntologyRegistry | None = None) -> bool:
    """Return True if source_type is a known ontology source type."""
    return isinstance(source_type, str) and _registry(registry).is_valid_source_type(source_type)


def is_valid_axis(axis: str, registry: OntologyRegistry | None = None) -> bool:
    """Return True if axis is a known structural axis."""
    return isinstance(axis, str) and _registry(registry).is_valid_axis(axis)


def is_valid_claim_status(status: str) -> bool:
    """Return True if status is a known SAG claim status."""
    return isinstance(status, str) and status in CLAIM_STATUS_SET


def is_valid_event_claim_status(status: str) -> bool:
    """Return True if status is a known event claim status."""
    return isinstance(status, str) and status in EVENT_CLAIM_STATUSES
