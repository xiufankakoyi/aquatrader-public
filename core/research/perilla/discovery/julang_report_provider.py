"""Julang report provider for Graph-first Alpha Discovery Engine.

Provides access to Julang research reports with local caching and graceful
fallback when the API is unavailable or unconfigured.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.research.perilla.alpha.report_graph_builder import ReportGraphBuilder
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)

_AVAILABILITY_TTL_SECONDS = 60
_SEARCH_CACHE_TTL_SECONDS = 300


class JulangReportProvider:
    """Provider for Julang research reports."""

    def __init__(self) -> None:
        """Initialize the provider and read configuration."""
        self.api_base = ""
        self.api_key = ""
        self.report_dir: Path | None = None
        self._load_config()

        # 缓存避免高频调用 health/API
        self._availability_cache: tuple[bool, float] | None = None
        self._search_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}

    def _load_config(self) -> None:
        """Load Julang configuration from secrets."""
        try:
            from config.secrets import Secrets

            self.api_base = str(getattr(Secrets, "JULANG_API_BASE", "") or "").rstrip("/")
            self.api_key = str(getattr(Secrets, "JULANG_API_KEY", "") or "")
            report_dir = getattr(Secrets, "JULANG_REPORT_DIR", None)
            if report_dir:
                self.report_dir = Path(report_dir)
                self.report_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("JulangReportProvider: failed to load config: %s", e)
            self.api_base = ""
            self.api_key = ""
            self.report_dir = None

    def is_available(self) -> bool:
        """Return True if the provider is configured and reachable."""
        if not self.api_base or not self.api_key:
            return False

        now = time.time()
        if self._availability_cache is not None:
            cached, cached_at = self._availability_cache
            if now - cached_at < _AVAILABILITY_TTL_SECONDS:
                return cached

        try:
            import requests

            resp = requests.get(
                f"{self.api_base}/health",
                headers=self._headers(),
                timeout=5,
            )
            available = resp.status_code == 200
            self._availability_cache = (available, now)
            return available
        except Exception:
            self._availability_cache = (False, now)
            return False

    def _headers(self) -> dict[str, str]:
        """Build request headers with API key."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _report_cache_path(report_dir: Path, report_id: str) -> Path:
        """Return local cache path for a report."""
        raw_id = report_id if isinstance(report_id, str) else ""
        safe_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
        return report_dir / f"julang_report_{safe_id}.txt"

    def _search_cache_key(self, query: str, count: int) -> str:
        """Generate stable cache key for a search query."""
        normalized_query = query if isinstance(query, str) else ""
        return hashlib.sha256(
            f"{normalized_query}|{count}".encode("utf-8")
        ).hexdigest()[:16]

    def search_reports(self, query: str, count: int = 5) -> list[dict[str, Any]]:
        """Search Julang reports by query.

        Args:
            query: Search query string.
            count: Maximum number of reports to return.

        Returns:
            List of report metadata dicts with report_id, title, date, industry.
        """
        if not isinstance(query, str) or not query.strip():
            return []
        try:
            count = max(0, min(50, int(float(count))))
        except (TypeError, ValueError, OverflowError):
            count = 5
        if count == 0 or not self.is_available():
            logger.info("JulangReportProvider: not available, returning empty search results")
            return []

        cache_key = self._search_cache_key(query, count)
        now = time.time()
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            results, cached_at = cached
            if now - cached_at < _SEARCH_CACHE_TTL_SECONDS:
                return [dict(item) for item in results]

        try:
            import requests

            resp = requests.post(
                f"{self.api_base}/reports/search",
                headers=self._headers(),
                json={"query": query, "count": count},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            reports = data.get("reports", []) if isinstance(data, dict) else []
            results = [
                {
                    "report_id": str(r.get("report_id", r.get("id", ""))),
                    "title": str(r.get("title", "")),
                    "date": str(r.get("date", r.get("publish_date", ""))),
                    "industry": str(r.get("industry", "")),
                }
                for r in reports if isinstance(reports, list)
                if isinstance(r, dict)
            ]
            self._search_cache[cache_key] = (results, now)
            if len(self._search_cache) > 128:
                oldest_key = min(
                    self._search_cache,
                    key=lambda key: self._search_cache[key][1],
                )
                self._search_cache.pop(oldest_key, None)
            return results
        except Exception as e:
            logger.warning("JulangReportProvider search failed: %s", e)
            return []

    def fetch_report_text(self, report_id: str) -> str:
        """Fetch report text, preferring local cache.

        Args:
            report_id: Report identifier.

        Returns:
            Report plain text.
        """
        if not isinstance(report_id, str) or not report_id.strip():
            return ""
        report_id = report_id.strip()
        if self.report_dir:
            cache_path = self._report_cache_path(self.report_dir, report_id)
            if cache_path.exists():
                try:
                    return cache_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.debug("JulangReportProvider cache read failed: %s", exc)

        if not self.is_available():
            logger.info("JulangReportProvider: not available, cannot fetch report %s", report_id)
            return ""

        try:
            import requests

            resp = requests.get(
                f"{self.api_base}/reports/{quote(report_id, safe='')}/text",
                headers=self._headers(),
                timeout=60,
            )
            resp.raise_for_status()
            text = str(resp.text or "").strip()

            if self.report_dir and text:
                cache_path = self._report_cache_path(self.report_dir, report_id)
                try:
                    cache_path.write_text(text, encoding="utf-8")
                except OSError as exc:
                    logger.debug("JulangReportProvider cache write failed: %s", exc)

            return text
        except Exception as e:
            logger.warning("JulangReportProvider fetch failed for %s: %s", report_id, e)
            return ""

    def ingest_to_kb(
        self,
        kb: SAGKnowledgeBase,
        query: str,
        root_concept: str,
        graph_builder: ReportGraphBuilder | None = None,
    ) -> int:
        """Search Julang reports and ingest claims into the knowledge base.

        Args:
            kb: SAG knowledge base.
            query: Search query.
            root_concept: Root concept for claim extraction.
            graph_builder: Optional graph builder. If None, a new one is created.

        Returns:
            Number of claims added.
        """
        if not callable(getattr(kb, "add_claim", None)) or not isinstance(query, str):
            return 0
        if not self.is_available():
            logger.info("JulangReportProvider: not available, skipping ingestion")
            return 0

        builder = graph_builder or ReportGraphBuilder(kb=kb)
        reports = self.search_reports(query)
        if not reports:
            return 0

        added = 0
        for report in reports if isinstance(reports, list) else []:
            if not isinstance(report, dict):
                continue
            report_id = report.get("report_id", "")
            title = report.get("title", "untitled")
            date = report.get("date", "")
            text = self.fetch_report_text(report_id)
            if not text:
                continue

            claims = builder.extract_claims_from_text(
                text=text,
                source_type="julang_report",
                source_title=f"巨浪研报: {title}",
                timestamp=date,
                root_concept=root_concept,
            )
            for c in claims if isinstance(claims, list) else []:
                if not isinstance(c, dict):
                    continue
                # Force source type to julang_report
                c["source_type"] = "julang_report"
                try:
                    added_claim = kb.add_claim(
                        head=c.get("head", ""),
                        relation=c.get("relation", ""),
                        tail=c.get("tail", ""),
                        evidence=c.get("evidence", ""),
                        source_type=c["source_type"],
                        source_title=c.get("source_title", ""),
                        timestamp=c.get("timestamp", ""),
                        confidence=c.get("confidence", 0.0),
                        head_type=c.get("head_type"),
                        tail_type=c.get("tail_type"),
                        metadata={"structure_axis": c.get("structure_axis")},
                    )
                except Exception as exc:
                    logger.warning("JulangReportProvider claim ingest failed: %s", exc)
                    continue
                if added_claim:
                    added += 1

        logger.info("JulangReportProvider ingested %d claims for %s", added, query)
        return added
