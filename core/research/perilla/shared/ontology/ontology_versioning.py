"""Ontology versioning utilities.

Tracks version history for the canonical ontology and supports migration
between versions. Version history is persisted as JSON Lines so that each
schema change is append-only and auditable.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VERSIONS_PATH = os.path.join(
    "data", "ontology", "ontology_versions.jsonl"
)


@dataclass
class OntologyVersion:
    """A snapshot of an ontology version."""

    version: str
    description: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    applied_by: str = ""
    proposal_id: str = ""

    def __post_init__(self) -> None:
        """Ensure timestamp is set."""
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize version to a plain dictionary."""
        return {
            "version": self.version,
            "description": self.description,
            "changes": self.changes,
            "created_at": self.created_at,
            "applied_by": self.applied_by,
            "proposal_id": self.proposal_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "OntologyVersion":
        """Deserialize version from a plain dictionary."""
        if not isinstance(data, Mapping) or not data.get("version"):
            raise ValueError("ontology version must include version")
        return cls(
            version=data["version"],
            description=data.get("description", ""),
            changes=list(data.get("changes", [])),
            created_at=data.get("created_at", ""),
            applied_by=data.get("applied_by", ""),
            proposal_id=data.get("proposal_id", ""),
        )


class OntologyVersioning:
    """Manages ontology version history with persistence."""

    def __init__(self, path: str | None = None) -> None:
        """Initialize versioning, optionally loading persisted history.

        Args:
            path: Path to the JSONL version history file.
        """
        self._path = path or DEFAULT_VERSIONS_PATH
        self._versions: list[OntologyVersion] = []
        self._load()

    def _load(self) -> None:
        """Load version history from disk."""
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        self._versions.append(OntologyVersion.from_dict(data))
                    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                        logger.warning("Skipping malformed ontology version line")
                        continue
            logger.info("Loaded %d ontology versions from %s", len(self._versions), self._path)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load ontology versions from %s: %s", self._path, e)

    def _save(self) -> None:
        """Persist version history to disk."""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            for version in self._versions:
                f.write(json.dumps(version.to_dict(), ensure_ascii=False) + "\n")

    def register(self, version: OntologyVersion) -> None:
        """Register a new ontology version and persist it.

        Args:
            version: Version snapshot to register.
        """
        self._versions.append(version)
        self._save()

    def current(self) -> OntologyVersion | None:
        """Return the most recently registered version, if any."""
        return self._versions[-1] if self._versions else None

    def list_versions(self) -> list[OntologyVersion]:
        """Return all registered versions in chronological order."""
        return list(self._versions)

    def next_patch_version(self, base_version: str | None = None) -> str:
        """Return the next patch version based on the current version.

        Args:
            base_version: Optional version string to bump. If omitted, the most
                recently registered version is used.
        """
        current = self.current()
        version = base_version or (current.version if current else "1.0.0")
        parts = version.split(".")
        try:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{major}.{minor}.{patch + 1}"
        except (ValueError, IndexError):
            return "1.0.1"

    def next_minor_version(self) -> str:
        """Return the next minor version based on the current version."""
        current = self.current()
        if current is None:
            return "1.1.0"
        parts = current.version.split(".")
        try:
            major, minor = int(parts[0]), int(parts[1])
            return f"{major}.{minor + 1}.0"
        except (ValueError, IndexError):
            return "1.1.0"

    def rollback_to(self, version: str) -> OntologyVersion | None:
        """Mark a rollback to a previous version.

        This does not mutate the ontology registry itself; it records the
        rollback intent in the version history.

        Args:
            version: Target version to roll back to.

        Returns:
            The newly registered rollback version, or None if target not found.
        """
        target = next((v for v in self._versions if v.version == version), None)
        if target is None:
            return None
        rollback = OntologyVersion(
            version=f"{self.next_patch_version()}-rollback-to-{version}",
            description=f"Rollback to version {version}",
            changes=[{"type": "rollback", "target_version": version}],
            applied_by="system",
        )
        self.register(rollback)
        return rollback
