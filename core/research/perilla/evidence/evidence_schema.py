"""Evidence schema data models.

Defines raw evidence records, graded evidence, and evidence bundles used by
the evidence probe and evidence grader modules.

TODO: Align field names with discovery models and alpha path evidence bundles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawEvidenceRecord:
    """A single raw evidence record before grading."""

    evidence_id: str
    source_document_id: str
    source_type: str
    evidence_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GradedEvidenceRecord:
    """A raw evidence record after grading."""

    evidence_id: str
    source_document_id: str
    source_type: str
    evidence_text: str
    evidence_level: str
    confidence: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
