"""Change detection monitor for Perilla Discovery Engine.

Imports change events from changedetection.io JSON exports and parses
webhook payloads, converting them into :class:`SourceDocument` instances.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import List, Optional

from .models import SourceDocument, new_id, now_iso

logger = logging.getLogger(__name__)


def _text(value: object, default: str = "") -> str:
    """将外部字段安全转换为文本。"""
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


class ChangeMonitor:
    """Monitor and import change events from changedetection.io.

    Supports two ingestion modes:

    1. **Bulk import** from a changedetection.io JSON export file
       via :meth:`import_changes`.
    2. **Real-time webhook** via :meth:`parse_webhook`.

    Both methods return :class:`SourceDocument` instances with
    ``source_type="change_event"``.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def import_changes(self, json_path: str) -> List[SourceDocument]:
        """Import changedetection.io JSON export and convert to SourceDocuments.

        The expected JSON format is a dict where each key is a watch UUID
        and each value is a dict containing at least ``url``, ``title``,
        and ``last_changed`` fields.

        Args:
            json_path: Path to the changedetection.io JSON export file.

        Returns:
            A list of :class:`SourceDocument` with ``source_type="change_event"``.
            Returns an empty list if the file is not found or the format
            is invalid.
        """
        try:
            path = Path(json_path)
        except (TypeError, ValueError):
            logger.warning("Invalid change detection export path: %r", json_path)
            return []

        try:
            if not path.exists():
                logger.warning("Change detection export file not found: %s", json_path)
                return []
        except OSError as exc:
            logger.warning("Cannot inspect change detection file '%s': %s", json_path, exc)
            return []

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except OSError as exc:
            logger.warning("Failed to read change detection file '%s': %s", json_path, exc)
            return []
        except (json.JSONDecodeError, UnicodeError) as exc:
            logger.warning("Invalid JSON in change detection file '%s': %s", json_path, exc)
            return []

        if not isinstance(data, dict):
            logger.warning(
                "Unexpected JSON structure in '%s': expected dict, got %s",
                json_path,
                type(data).__name__,
            )
            return []

        results: List[SourceDocument] = []

        for watch_id, watch_data in data.items():
            try:
                if not isinstance(watch_data, dict):
                    logger.debug("Skipping non-dict watch entry: %s", watch_id)
                    continue

                doc = self._watch_to_document(watch_id, watch_data)
                if doc is not None:
                    results.append(doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse watch entry '%s': %s", watch_id, exc)

        return results

    def parse_webhook(self, payload: dict) -> Optional[SourceDocument]:
        """Parse a changedetection.io webhook payload into a SourceDocument.

        Expected payload fields:

        - ``url``: The watched URL.
        - ``title``: Title of the watched page.
        - ``last_changed``: Timestamp of the last change.
        - ``watch_uuid``: Unique identifier for the watch.

        Args:
            payload: A dictionary representing the webhook payload.

        Returns:
            A :class:`SourceDocument` with ``source_type="change_event"``,
            or ``None`` if the payload is invalid.
        """
        if not isinstance(payload, Mapping):
            logger.warning("Webhook payload is not a dict: %s", type(payload).__name__)
            return None

        url = _text(payload.get("url"))
        if not url:
            logger.warning("Webhook payload missing 'url' field")
            return None

        title = _text(payload.get("title"))
        last_changed = payload.get("last_changed") or payload.get("last_checked") or None
        watch_uuid = payload.get("watch_uuid", "")

        # Extract diff / snippet if available
        content_text = ""
        diff = payload.get("diff", "")
        snippet = payload.get("snippet", "")
        if diff:
            content_text = _text(diff)
        elif snippet:
            content_text = _text(snippet)

        metadata: dict = {}
        if watch_uuid:
            metadata["watch_uuid"] = watch_uuid
        if payload.get("previous_hash"):
            metadata["previous_hash"] = payload["previous_hash"]
        if payload.get("current_hash"):
            metadata["current_hash"] = payload["current_hash"]

        return SourceDocument(
            document_id=new_id("chg_"),
            source_type="change_event",
            url=url,
            title=title,
            published_at=_text(last_changed) or None,
            fetched_at=now_iso(),
            content_text=content_text,
            credibility_hint="medium",
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _watch_to_document(watch_id: str, watch_data: dict) -> SourceDocument | None:
        """Convert a single watch entry from the JSON export to a SourceDocument.

        Args:
            watch_id: The watch UUID from the export.
            watch_data: The watch data dict.

        Returns:
            A :class:`SourceDocument`, or ``None`` if the entry lacks a URL.
        """
        if not isinstance(watch_data, Mapping):
            return None
        url = _text(watch_data.get("url"))
        if not url:
            return None

        title = _text(watch_data.get("title") or watch_data.get("label"))
        last_changed = watch_data.get("last_changed") or watch_data.get("last_checked") or None

        # Build content from available text fields
        content_parts: list[str] = []
        for field_name in ("last_notification_text", "last_error", "note"):
            value = watch_data.get(field_name, "")
            if value:
                content_parts.append(f"{field_name}: {value}")
        content_text = "\n".join(content_parts)

        metadata: dict = {"watch_id": watch_id}
        if watch_data.get("tag"):
            metadata["tag"] = watch_data["tag"]
        if watch_data.get("paused") is not None:
            metadata["paused"] = watch_data["paused"]

        return SourceDocument(
            document_id=new_id("chg_"),
            source_type="change_event",
            url=url,
            title=title,
            published_at=_text(last_changed) or None,
            fetched_at=now_iso(),
            content_text=content_text,
            credibility_hint="medium",
            metadata=metadata,
        )
