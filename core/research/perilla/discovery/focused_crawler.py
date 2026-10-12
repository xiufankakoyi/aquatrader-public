"""Focused web crawler for Perilla Discovery Engine.

Crawls individual URLs using :class:`HTMLTextExtractor`, converting
extracted content into :class:`SourceDocument` instances with disk
caching for deduplication.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import List, Optional

from .html_text_extractor import HTMLTextExtractor
from .models import SourceDocument, new_id, now_iso

logger = logging.getLogger(__name__)

_USER_AGENT = "AquaTrade-PerillaDiscovery/1.0"


class FocusedCrawler:
    """Crawl URLs and extract text content into SourceDocuments.

    Uses :class:`HTMLTextExtractor` for fetching and text extraction,
    with a local disk cache to avoid re-crawling already-processed URLs.

    Crawling is sequential (not parallel) to respect rate limits.

    Args:
        cache_dir: Directory for caching extracted text and raw HTML.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        cache_dir: str = "data/web_cache/perilla_discovery",
        timeout: int = 30,
    ) -> None:
        try:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError):
            self._cache_dir = Path("data/web_cache/perilla_discovery")
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._timeout = max(1, int(float(timeout)))
        except (TypeError, ValueError, OverflowError):
            self._timeout = 30
        self._extractor = HTMLTextExtractor(cache_dir=self._cache_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def crawl_url(self, url: str) -> Optional[SourceDocument]:
        """Crawl a single URL and return a SourceDocument.

        Fetches the URL, extracts main text via trafilatura (with fallback),
        and persists the result to the cache directory.

        Args:
            url: The URL to crawl.

        Returns:
            A :class:`SourceDocument` with ``source_type="official_site"``,
            or ``None`` on any failure (network error, empty content, etc.).
        """
        if not isinstance(url, str) or not url.strip():
            logger.warning("Empty URL provided to crawl_url")
            return None
        url = url.strip()

        try:
            content = self._extractor.extract_from_url(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Extraction failed for url=%s: %s", url, exc)
            return None

        if not content.success or not content.main_text:
            logger.debug("No usable text extracted from url=%s: %s", url, content.error)
            return None

        # Generate document ID and persist to cache
        doc_id = new_id("cwl_")
        try:
            content = self._extractor.save_extracted(content, doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save extracted content for url=%s: %s", url, exc)

        # Determine source_type based on URL pattern
        source_type = self._infer_source_type(url)

        # Determine credibility hint
        credibility_hint = self._infer_credibility(url)

        return SourceDocument(
            document_id=doc_id,
            source_type=source_type,
            url=url,
            title=content.title,
            site_name=self._extract_site_name(url),
            published_at=content.date or None,
            fetched_at=now_iso(),
            content_text=content.main_text,
            raw_html_path=content.raw_html_path or None,
            text_hash=content.text_hash,
            credibility_hint=credibility_hint,
            metadata={
                "extraction_method": content.extraction_method,
                "author": content.author,
            },
        )

    def crawl_urls(self, urls: List[str], max_concurrent: int = 3) -> List[SourceDocument]:
        """Crawl multiple URLs sequentially.

        The ``max_concurrent`` parameter is accepted for API compatibility
        but crawling is always sequential to respect rate limits.

        Args:
            urls: A list of URLs to crawl.
            max_concurrent: Ignored; kept for interface compatibility.

        Returns:
            A list of :class:`SourceDocument` from successfully crawled URLs.
        """
        if not isinstance(urls, (list, tuple)):
            return []

        results: List[SourceDocument] = []

        for url in urls:
            doc = self.crawl_url(url)
            if doc is not None:
                results.append(doc)

        return results

    def is_url_cached(self, url: str) -> bool:
        """Check if a URL's extracted text is already cached on disk.

        Uses a URL-based hash to look up cached text files.

        Args:
            url: The URL to check.

        Returns:
            ``True`` if a cached text file exists for this URL.
        """
        if not isinstance(url, str) or not url:
            return False

        url_hash = self._url_to_hash(url)
        cached_path = self._cache_dir / f"{url_hash}_text.txt"
        return cached_path.exists()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _url_to_hash(url: str) -> str:
        """Compute a stable hash from a URL for cache key purposes.

        Args:
            url: The URL to hash.

        Returns:
            A 12-character hex digest derived from the URL.
        """
        if not isinstance(url, str) or not url:
            return ""
        return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _extract_site_name(url: str) -> str:
        """Extract a site name from a URL's domain.

        Args:
            url: The URL to extract from.

        Returns:
            The domain portion of the URL, or an empty string on failure.
        """
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            return parsed.netloc or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _infer_source_type(url: str) -> str:
        """Infer the source_type based on URL patterns.

        Args:
            url: The URL to classify.

        Returns:
            A source type string from :data:`SOURCE_TYPES`.
        """
        if not isinstance(url, str):
            return "official_site"
        url_lower = url.lower()

        # Government / official
        if any(domain in url_lower for domain in (".gov.cn", ".gov/", "gov.")):
            return "announcement"

        # Company official sites
        if any(domain in url_lower for domain in (".com.cn/about", ".com/about", "ir.")):
            return "official_site"

        # Recruitment
        if any(kw in url_lower for kw in ("zhaopin", "51job", "liepin", "recruit", "career")):
            return "recruitment"

        # Tender / bidding
        if any(kw in url_lower for kw in ("zhaobiao", "bidding", "tender", "zbgg")):
            return "tender"

        # Expo / exhibition
        if any(kw in url_lower for kw in ("expo", "exhibition", "zhanguan", "fair")):
            return "expo"

        # News
        if any(kw in url_lower for kw in ("news", "xinwen", "report", "article")):
            return "news"

        # Research reports
        if any(kw in url_lower for kw in ("research", "yanbao", "report")):
            return "report"

        # Forum / social
        if any(kw in url_lower for kw in ("forum", "bbs", "tieba", "zhihu", "weibo")):
            return "forum"

        return "official_site"

    @staticmethod
    def _infer_credibility(url: str) -> str:
        """Infer a credibility hint based on URL domain patterns.

        Args:
            url: The URL to evaluate.

        Returns:
            A credibility hint string.
        """
        if not isinstance(url, str):
            return "unknown"
        url_lower = url.lower()

        # High credibility: government, official exchanges, major financial media
        high_domains = (".gov.cn", ".gov/", "sse.com.cn", "szse.cn", "cninfo.com.cn")
        if any(domain in url_lower for domain in high_domains):
            return "high"

        # Medium credibility: established news, research, company IR
        medium_domains = (
            "eastmoney.com", "sina.com.cn", "163.com", "sohu.com",
            "caixin.com", "yicai.com", "stcn.com", "cs.com.cn",
        )
        if any(domain in url_lower for domain in medium_domains):
            return "medium"

        # Low credibility: forums, social media, unknown
        low_domains = ("zhihu.com", "weibo.com", "tieba.baidu.com", "xueqiu.cn")
        if any(domain in url_lower for domain in low_domains):
            return "low"

        return "unknown"
