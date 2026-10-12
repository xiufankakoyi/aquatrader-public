"""Axis registry for structural/semantic axes.

Maps canonical axis identifiers to human-readable labels, keywords, and
validation rules used by the alpha path discovery and scoring modules.

TODO: Populate axis definitions from the canonical ontology source.
"""
from __future__ import annotations

from core.research.perilla.shared.ontology.ontology_models import (
    STRUCTURAL_AXES,
)


class AxisRegistry:
    """Registry for structural axes."""

    def __init__(self) -> None:
        """Initialize the axis registry with default axes."""
        self._axes: set[str] = set(STRUCTURAL_AXES)

    def is_valid(self, axis: str) -> bool:
        """Return True if the axis is a known structural axis.

        Args:
            axis: Axis value to validate.

        Returns:
            True when the axis is registered.
        """
        return isinstance(axis, str) and axis in self._axes

    def all_axes(self) -> frozenset[str]:
        """Return all registered axes."""
        return frozenset(self._axes)
