"""Canonicalization helpers.

Normalises entity names, relation strings, and axis labels into canonical
ontology forms.

TODO: Add alias resolution and fuzzy matching integration with EntityResolver.
"""
from __future__ import annotations

import re


def canonicalize_name(name: str | None) -> str:
    """Return a canonicalised entity name.

    Args:
        name: Raw entity name.

    Returns:
        Normalised name with whitespace collapsed.
    """
    if name is None:
        return ""
    return re.sub(r"\s+", "", str(name).strip())


def canonicalize_relation(relation: str | None) -> str:
    """Return a canonicalised relation string.

    Args:
        relation: Raw relation value.

    Returns:
        Lower-case, stripped relation value.
    """
    if relation is None:
        return ""
    return str(relation).strip().lower()
