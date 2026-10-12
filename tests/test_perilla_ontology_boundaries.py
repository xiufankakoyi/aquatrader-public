from __future__ import annotations

from core.research.perilla.shared.ontology.canonicalizer import (
    canonicalize_name,
    canonicalize_relation,
)
from core.research.perilla.shared.ontology.axis_registry import AxisRegistry
from core.research.perilla.shared.ontology.ontology_validator import (
    is_valid_axis,
    is_valid_claim_status,
)
from core.research.perilla.shared.ontology.relation_registry import RelationRegistry


def test_canonicalizer_treats_missing_values_as_empty() -> None:
    assert canonicalize_name(None) == ""
    assert canonicalize_relation(None) == ""
    assert canonicalize_name("  Acme  Corp ") == "AcmeCorp"
    assert canonicalize_relation("  SUPPLIED_BY ") == "supplied_by"


def test_axis_registry_rejects_unhashable_missing_values() -> None:
    registry = AxisRegistry()

    assert registry.is_valid(None) is False  # type: ignore[arg-type]
    assert registry.is_valid([]) is False  # type: ignore[arg-type]


def test_ontology_validators_fail_closed_for_non_strings() -> None:
    assert is_valid_axis([]) is False  # type: ignore[arg-type]
    assert is_valid_claim_status({}) is False  # type: ignore[arg-type]


def test_relation_registry_rejects_unhashable_missing_values() -> None:
    assert RelationRegistry().is_valid([]) is False  # type: ignore[arg-type]
