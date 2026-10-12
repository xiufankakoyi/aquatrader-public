"""Shared exceptions for Perilla modules.

Domain-specific error types used by discovery, evidence, event, and alpha
components. Expand this module as new failure modes require explicit handling.
"""
from __future__ import annotations


class PerillaError(Exception):
    """Base exception for all Perilla domain errors."""


class OntologyError(PerillaError):
    """Raised when ontology validation or lookup fails."""


class EvidenceError(PerillaError):
    """Raised when evidence processing or grading fails."""
