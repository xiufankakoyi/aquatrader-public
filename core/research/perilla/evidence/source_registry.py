"""Perilla Discovery Engine - Source Registry.

Manages discovery source entries with YAML-based configuration,
filtering, and export capabilities.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Allowed enum-like values for validation
VALID_SOURCE_TYPES = {
    "index_page", "concept_page", "sector_page", "official_site",
    "recruitment", "expo", "tender", "news", "forum", "social", "manual",
}

VALID_CRAWL_METHODS = {
    "search", "rss", "change_detection", "scrapy", "playwright", "manual",
}

VALID_CREDIBILITY_HINTS = {"high", "medium", "low"}


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = _text(value).lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    return default


@dataclass
class SourceEntry:
    """A single discovery source entry.

    Attributes:
        source_id: Unique identifier for the source.
        name: Human-readable name of the source.
        source_type: Type of the source (e.g. concept_page, recruitment).
        base_url: Root URL for the source.
        enabled: Whether the source is active.
        crawl_method: How to crawl this source.
        credibility_hint: Assessed credibility level.
        tags: Free-form tags for categorization and filtering.
        rate_limit_seconds: Minimum interval between requests (seconds).
        requires_login: Whether the source requires authentication.
        notes: Free-text notes about the source.
    """

    source_id: str
    name: str
    source_type: str
    base_url: str
    enabled: bool = True
    crawl_method: str = "search"
    credibility_hint: str = "medium"
    tags: List[str] = field(default_factory=list)
    rate_limit_seconds: int = 60
    requires_login: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        """Validate field values after initialization."""
        self.source_id = _text(self.source_id)
        self.name = _text(self.name)
        self.source_type = _text(self.source_type, "manual")
        self.base_url = _text(self.base_url)
        self.enabled = _as_bool(self.enabled, True)
        self.crawl_method = _text(self.crawl_method, "search")
        self.credibility_hint = _text(self.credibility_hint, "medium")
        self.requires_login = _as_bool(self.requires_login)
        self.notes = _text(self.notes)
        if isinstance(self.tags, str):
            self.tags = [self.tags]
        elif not isinstance(self.tags, (list, tuple, set)):
            self.tags = []
        else:
            self.tags = [str(tag) for tag in self.tags if str(tag).strip()]
        try:
            rate_limit = float(self.rate_limit_seconds)
        except (OverflowError, TypeError, ValueError):
            rate_limit = 60.0
        self.rate_limit_seconds = min(86400, max(0, int(rate_limit))) if math.isfinite(rate_limit) else 60
        if self.source_type not in VALID_SOURCE_TYPES:
            logger.warning(
                "SourceEntry '%s' has unknown source_type '%s'; "
                "expected one of %s",
                self.source_id, self.source_type, VALID_SOURCE_TYPES,
            )
        if self.crawl_method not in VALID_CRAWL_METHODS:
            logger.warning(
                "SourceEntry '%s' has unknown crawl_method '%s'; "
                "expected one of %s",
                self.source_id, self.crawl_method, VALID_CRAWL_METHODS,
            )
        if self.credibility_hint not in VALID_CREDIBILITY_HINTS:
            logger.warning(
                "SourceEntry '%s' has unknown credibility_hint '%s'; "
                "expected one of %s",
                self.source_id, self.credibility_hint, VALID_CREDIBILITY_HINTS,
            )


class SourceRegistry:
    """Registry for managing discovery source entries.

    Supports loading from YAML files or dicts, filtering by type/tags,
    and exporting back to dict format.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._entries: Dict[str, SourceEntry] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load_from_yaml(cls, path: str) -> SourceRegistry:
        """Load sources from a YAML config file.

        If the file does not exist, returns an empty registry instead
        of raising an exception.

        Args:
            path: Absolute or relative path to the YAML file.

        Returns:
            A SourceRegistry populated with entries from the file,
            or an empty registry if the file is missing.
        """
        registry = cls()
        try:
            file_path = Path(path)
        except (TypeError, ValueError):
            logger.warning("Invalid source registry path: %r", path)
            return registry

        if not file_path.is_file():
            logger.warning(
                "Source registry YAML not found: %s — returning empty registry",
                path,
            )
            return registry

        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            logger.error("Failed to parse YAML at %s: %s", path, exc)
            return registry

        if not isinstance(data, Mapping):
            logger.error(
                "Expected top-level dict in %s, got %s", path, type(data).__name__,
            )
            return registry

        registry.load_from_dict(data)
        return registry

    def load_from_dict(self, data: dict) -> None:
        """Load sources from a dictionary.

        Expected format::

            sources:
              - source_id: "example"
                name: "Example Source"
                source_type: "concept_page"
                base_url: "https://example.com"
                ...

        Args:
            data: Dictionary containing a ``sources`` key with a list
                  of source entry dicts.
        """
        if not isinstance(data, Mapping):
            logger.error("Expected source registry mapping, got %s", type(data).__name__)
            return
        sources_list = data.get("sources", [])
        if not isinstance(sources_list, (list, tuple)):
            logger.error(
                "'sources' key must be a list, got %s", type(sources_list).__name__,
            )
            return

        for idx, item in enumerate(sources_list):
            if not isinstance(item, Mapping):
                logger.warning("Skipping non-dict source entry at index %d", idx)
                continue
            try:
                entry = SourceEntry(
                    source_id=item.get("source_id", ""),
                    name=item.get("name", ""),
                    source_type=item.get("source_type", "manual"),
                    base_url=item.get("base_url", ""),
                    enabled=item.get("enabled", True),
                    crawl_method=item.get("crawl_method", "search"),
                    credibility_hint=item.get("credibility_hint", "medium"),
                    tags=item.get("tags", []) if isinstance(item.get("tags", []), (list, tuple, set, str)) else [],
                    rate_limit_seconds=item.get("rate_limit_seconds", 60),
                    requires_login=item.get("requires_login", False),
                    notes=item.get("notes", ""),
                )
                if not entry.source_id:
                    logger.warning("Skipping source entry with empty source_id at index %d", idx)
                    continue
                self.add_source(entry)
            except Exception as exc:
                logger.error(
                    "Failed to load source entry at index %d: %s", idx, exc,
                )

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def filter_by_type(self, source_type: str) -> List[SourceEntry]:
        """Filter sources by source_type, returning only enabled ones.

        Args:
            source_type: The source type to filter by.

        Returns:
            List of matching enabled SourceEntry instances.
        """
        source_type = _text(source_type)
        return [
            e for e in self._entries.values()
            if e.source_type == source_type and e.enabled
        ]

    def filter_by_tags(self, tags: List[str]) -> List[SourceEntry]:
        """Filter sources by tags (any match), returning only enabled ones.

        Args:
            tags: List of tags to match. A source matches if it has
                  at least one tag in common with the provided list.

        Returns:
            List of matching enabled SourceEntry instances.
        """
        if not isinstance(tags, (list, tuple, set)) or not tags:
            return []
        tag_set = {_text(tag) for tag in tags if _text(tag)}
        if not tag_set:
            return []
        return [
            e for e in self._entries.values()
            if e.enabled and tag_set.intersection(e.tags)
        ]

    def get_enabled_sources(self) -> List[SourceEntry]:
        """Return all enabled sources, excluding those requiring login.

        Sources with ``requires_login=True`` are skipped and a warning
        is logged for each skipped source.

        Returns:
            List of enabled SourceEntry instances that do not require login.
        """
        result: List[SourceEntry] = []
        for entry in self._entries.values():
            if not entry.enabled:
                continue
            if entry.requires_login:
                logger.warning(
                    "Skipping source '%s' (%s): requires_login=True",
                    entry.source_id, entry.name,
                )
                continue
            result.append(entry)
        return result

    def get_all_sources(self) -> List[SourceEntry]:
        """Return all sources including disabled ones.

        Returns:
            List of all SourceEntry instances in the registry.
        """
        return list(self._entries.values())

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_source(self, entry: SourceEntry) -> None:
        """Add a source entry to the registry.

        If an entry with the same source_id already exists, it will be
        overwritten and a warning will be logged.

        Args:
            entry: The SourceEntry to add.
        """
        if not isinstance(entry, SourceEntry) or not entry.source_id:
            logger.warning("Skipping invalid source entry: %r", entry)
            return
        if entry.source_id in self._entries:
            logger.warning(
                "Overwriting existing source entry: '%s'", entry.source_id,
            )
        self._entries[entry.source_id] = entry

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Export the registry as a dictionary.

        Returns:
            Dictionary with a ``sources`` key containing a list of
            source entry dicts, suitable for YAML serialization.
        """
        return {
            "sources": [asdict(entry) for entry in self._entries.values()],
        }

    def __len__(self) -> int:
        """Return the number of entries in the registry."""
        return len(self._entries)

    def __repr__(self) -> str:
        """Return a concise representation of the registry."""
        enabled = sum(1 for e in self._entries.values() if e.enabled)
        return (
            f"SourceRegistry(total={len(self._entries)}, enabled={enabled})"
        )
