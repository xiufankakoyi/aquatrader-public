"""Relation registry for canonical relation types.

Validates and resolves relation identifiers shared by the alpha graph and
event graph modules.

TODO: Add relation metadata and inference rules as the ontology matures.
"""
from __future__ import annotations

from core.research.perilla.shared.ontology.ontology_models import (
    RELATION_TYPES,
)


class RelationRegistry:
    """Registry for canonical relation types."""

    def __init__(self) -> None:
        """Initialize the registry with default relation types."""
        self._relations: set[str] = set(RELATION_TYPES)

    def is_valid(self, relation: str) -> bool:
        """Return True if the relation is a known relation type.

        Args:
            relation: Relation value to validate.

        Returns:
            True when the relation is registered.
        """
        return isinstance(relation, str) and relation in self._relations

    def all_relations(self) -> frozenset[str]:
        """Return all registered relation types."""
        return frozenset(self._relations)


# Re-export canonical labels for backward compatibility.
# The source of truth is ``shared.ontology.ontology_models``.
