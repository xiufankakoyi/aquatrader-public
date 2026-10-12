"""RSS feed collector for Perilla Discovery Engine.

Collects and parses RSS/Atom feeds using the ``feedparser`` library,
converting each feed item into a :class:`SourceDocument`.
"""
from __future__ import annotations

import logging
from typing import List

from .models import SourceDocument, new_id, now_iso

logger = logging.getLogger(__name__)

# Attempt to import feedparser; set to None if unavailable
try:
    import feedparser  # type: ignore[import-untyped]
except ImportError:
    feedparser = None  # type: ignore[assignment]
    logger.warning("feedparser is not installed; RSS collection will return empty results")


class RSSCollector:
    """Collect and parse RSS/Atom feeds into SourceDocument instances.

    Gracefully handles missing ``feedparser`` dependency and malformed
    feeds by returning empty lists instead of raising exceptions.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def collect(self, feed_url: str, max_items: int = 20) -> List[SourceDocument]:
        """Parse a single RSS/Atom feed and return SourceDocument instances.

        Args:
            feed_url: The URL of the RSS/Atom feed.
            max_items: Maximum number of items to extract from the feed.

        Returns:
            A list of :class:`SourceDocument` with ``source_type="rss"``.
            Returns an empty list if feedparser is unavailable or the feed
            cannot be parsed.
        """
        if feedparser is None:
            logger.warning("feedparser not available; skipping feed_url=%s", feed_url)
            return []

        if not feed_url or not isinstance(feed_url, str) or not feed_url.strip():
            logger.warning("Empty feed_url provided, returning empty list")
            return []
        try:
            max_items = max(0, int(max_items))
        except (TypeError, ValueError):
            max_items = 20

        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedparser.parse raised for feed_url=%s: %s", feed_url, exc)
            return []

        # feedparser sets bozo bit on parse errors; check for severe errors
        if parsed.bozo and not parsed.entries:
            logger.warning(
                "RSS feed parse error for feed_url=%s: %s",
                feed_url,
                getattr(parsed, "bozo_exception", "unknown"),
            )
            return []

        feed_title = ""
        if parsed.feed and parsed.feed.get("title"):
            feed_title = parsed.feed["title"]

        results: List[SourceDocument] = []
        entries = parsed.entries[:max_items]

        for entry in entries:
            try:
                doc = self._entry_to_document(entry, feed_title)
                if doc is not None:
                    results.append(doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse RSS entry: %s", exc)

        return results

    def collect_multiple(
        self, feed_urls: List[str], max_items_per_feed: int = 10
    ) -> List[SourceDocument]:
        """Collect from multiple RSS/Atom feeds.

        Args:
            feed_urls: A list of feed URLs to collect from.
            max_items_per_feed: Maximum items per individual feed.

        Returns:
            A combined list of :class:`SourceDocument` from all feeds.
        """
        if not feed_urls:
            return []

        all_results: List[SourceDocument] = []
        for url in feed_urls:
            items = self.collect(url, max_items=max_items_per_feed)
            all_results.extend(items)

        return all_results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _entry_to_document(entry: object, feed_title: str = "") -> SourceDocument | None:
        """Convert a feedparser entry to a SourceDocument.

        Args:
            entry: A feedparser entry object (dict-like).
            feed_title: The title of the parent feed.

        Returns:
            A :class:`SourceDocument` instance, or ``None`` if the entry
            lacks a URL.
        """
        # feedparser entries are dict-like; use getattr with defaults
        url = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""

        # Skip entries without a URL
        if not url:
            return None

        # Extract publication date
        published_at: str | None = None
        published = getattr(entry, "published", None)
        if published:
            published_at = str(published)
        else:
            updated = getattr(entry, "updated", None)
            if updated:
                published_at = str(updated)

        # Extract author
        author = ""
        author_detail = getattr(entry, "author_detail", None)
        if author_detail and hasattr(author_detail, "name"):
            author = author_detail.name or ""
        elif getattr(entry, "author", None):
            author = str(entry.author)

        # Build metadata
        metadata: dict = {}
        tags = getattr(entry, "tags", None)
        if tags:
            metadata["tags"] = [t.get("term", "") for t in tags if isinstance(t, dict)]

        if author:
            metadata["author"] = author

        return SourceDocument(
            document_id=new_id("rss_"),
            source_type="rss",
            url=url,
            title=title,
            site_name=feed_title,
            published_at=published_at,
            fetched_at=now_iso(),
            content_text=summary,
            credibility_hint="medium",
            metadata=metadata,
        )
