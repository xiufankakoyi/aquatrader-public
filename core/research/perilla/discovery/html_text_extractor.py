"""HTML text extractor for Perilla Discovery Engine.

Uses trafilatura as the primary extraction engine with a regex-based
fallback when trafilatura returns None. Extracted content is cached
to disk for downstream processing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_USER_AGENT = "AquaTrade-PerillaDiscovery/1.0"
_REQUEST_TIMEOUT = 30  # seconds
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
@dataclass
class ExtractedContent:
    """Container for the result of HTML text extraction.

    Attributes:
        title: Page title extracted from metadata.
        main_text: Main body text extracted from the HTML.
        author: Author name if available.
        date: Publication date if available.
        metadata: Additional metadata returned by trafilatura (JSON dict).
        text_hash: MD5 hex digest of ``main_text``.
        raw_html_path: Path where the raw HTML was saved.
        extracted_text_path: Path where the extracted text was saved.
        extraction_method: One of ``trafilatura``, ``fallback``, ``manual``.
        success: Whether extraction produced usable text.
        error: Error message if extraction failed.
    """

    title: str = ""
    main_text: str = ""
    author: str = ""
    date: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    text_hash: str = ""
    raw_html_path: str = ""
    extracted_text_path: str = ""
    extraction_method: str = ""
    success: bool = False
    error: str = ""
    raw_html: str = ""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------
class HTMLTextExtractor:
    """Extract readable text from HTML pages.

    Uses *trafilatura* as the primary extraction engine.  When trafilatura
    returns ``None`` (e.g. for very short or malformed pages), a simple
    regex-based tag stripper is used as fallback.

    Args:
        cache_dir: Directory used for persisting raw HTML and extracted text.
    """

    def __init__(self, cache_dir: str = "data/web_cache/perilla_discovery") -> None:
        try:
            cache_path = Path(cache_dir).expanduser().resolve()
            cache_path.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError):
            cache_path = (Path.cwd() / "data" / "web_cache" / "perilla_discovery").resolve()
            cache_path.mkdir(parents=True, exist_ok=True)
        self._cache_dir = cache_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def extract_from_html(self, html: str, url: str = "") -> ExtractedContent:
        """Extract text from an HTML string.

        Args:
            html: Raw HTML content.
            url: Source URL (stored in metadata, not fetched).

        Returns:
            An :class:`ExtractedContent` instance.  Check ``success`` to
            determine whether extraction produced usable text.
        """
        if not isinstance(html, str):
            return ExtractedContent(error="Invalid HTML input")
        if not html.strip():
            return ExtractedContent(error="Empty HTML input")

        source_url = url.strip() if isinstance(url, str) else ""

        # --- trafilatura primary path ---
        content = self._extract_with_trafilatura(html, source_url)
        if content.success:
            content.raw_html = html
            return content

        # --- fallback: regex tag stripping ---
        logger.debug("trafilatura returned no text, using fallback for url=%s", source_url)
        content = self._extract_with_fallback(html, source_url)
        content.raw_html = html
        return content

    def extract_from_url(self, url: str) -> ExtractedContent:
        """Fetch a URL and extract text from the response.

        Args:
            url: The URL to fetch.

        Returns:
            An :class:`ExtractedContent` instance.  On network / HTTP errors
            ``success`` is ``False`` and ``error`` contains a description.
        """
        if not isinstance(url, str):
            return ExtractedContent(error="Invalid URL provided")
        url = url.strip()
        if not url:
            return ExtractedContent(error="Empty URL provided")

        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError:
            return ExtractedContent(error="Invalid URL provided")
        if not parsed.scheme or not parsed.netloc or not hostname:
            return ExtractedContent(error="Invalid URL provided")
        if parsed.scheme.lower() not in {"http", "https"}:
            return ExtractedContent(error="Unsupported URL scheme")

        try:
            response = requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Failed to fetch url=%s: %s", url, exc)
            return ExtractedContent(error=f"Request failed: {exc}")

        html = response.text if isinstance(response.text, str) else ""
        content = self.extract_from_html(html, url=url)
        return content

    def save_extracted(
        self, content: ExtractedContent, document_id: str
    ) -> ExtractedContent:
        """Persist raw HTML and extracted text to the cache directory.

        After saving, ``raw_html_path``, ``extracted_text_path`` and
        ``text_hash`` are updated on the returned content object.

        Args:
            content: The extraction result to persist.
            document_id: Unique identifier used for file naming.

        Returns:
            A **new** :class:`ExtractedContent` with updated paths and hash.
        """
        if not isinstance(content, ExtractedContent):
            return ExtractedContent(error="Invalid extracted content")

        result = ExtractedContent(
            title=self._text_or_empty(content.title),
            main_text=self._text_or_empty(content.main_text),
            author=self._text_or_empty(content.author),
            date=self._text_or_empty(content.date),
            metadata=dict(content.metadata) if isinstance(content.metadata, dict) else {},
            extraction_method=self._text_or_empty(content.extraction_method),
            success=bool(content.success),
            error=self._text_or_empty(content.error),
            raw_html=self._text_or_empty(content.raw_html),
        )

        # Compute text hash
        if result.main_text:
            result.text_hash = self._compute_text_hash(result.main_text)

        # Save raw HTML — best effort; do not crash on write failure
        safe_document_id = self._safe_document_id(document_id)
        raw_path = self._cache_dir / f"{safe_document_id}_raw.html"
        try:
            raw_path.write_text(result.raw_html, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to save raw HTML for doc_id=%s: %s", document_id, exc)

        result.raw_html_path = str(raw_path)

        # Save extracted text
        text_path = self._cache_dir / f"{safe_document_id}_text.txt"
        try:
            text_path.write_text(result.main_text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to save extracted text for doc_id=%s: %s", document_id, exc)

        result.extracted_text_path = str(text_path)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _extract_with_trafilatura(
        self, html: str, url: str
    ) -> ExtractedContent:
        """Attempt extraction using trafilatura.

        Args:
            html: Raw HTML string.
            url: Source URL for metadata.

        Returns:
            An :class:`ExtractedContent` with ``extraction_method`` set to
            ``"trafilatura"`` on success.
        """
        content = ExtractedContent()

        try:
            import trafilatura  # noqa: WPS433 – optional dependency
        except ImportError:
            content.error = "trafilatura is not installed"
            return content

        try:
            # Main text extraction
            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
                url=url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trafilatura.extract raised: %s", exc)
            content.error = f"trafilatura.extract failed: {exc}"
            return content

        if not text or not text.strip():
            return content  # success stays False; caller will try fallback

        content.main_text = text.strip()
        content.extraction_method = "trafilatura"
        content.success = True

        # Metadata extraction (best effort)
        try:
            meta_json = trafilatura.extract(html, output_format="json", url=url)
            if meta_json:
                meta = json.loads(meta_json)
                if not isinstance(meta, dict):
                    return content
                content.title = self._text_or_empty(meta.get("title"))
                content.author = self._text_or_empty(meta.get("author"))
                content.date = self._text_or_empty(meta.get("date"))
                # Preserve remaining fields as metadata dict
                for key in ("title", "author", "date"):
                    meta.pop(key, None)
                content.metadata = meta
        except Exception as exc:  # noqa: BLE001
            logger.debug("trafilatura metadata extraction failed: %s", exc)

        return content

    @staticmethod
    def _text_or_empty(value: Any) -> str:
        return value if isinstance(value, str) else ""

    def _extract_with_fallback(
        self, html: str, url: str
    ) -> ExtractedContent:
        """Fallback extraction using simple regex tag stripping.

        Args:
            html: Raw HTML string.
            url: Source URL (stored in metadata only).

        Returns:
            An :class:`ExtractedContent` with ``extraction_method`` set to
            ``"fallback"`` on success.
        """
        content = ExtractedContent()

        try:
            # Strip HTML tags
            text = _TAG_RE.sub("", html)
            # Collapse excessive whitespace
            text = _WS_RE.sub("\n\n", text).strip()
        except Exception as exc:  # noqa: BLE001
            content.error = f"Fallback extraction failed: {exc}"
            return content

        if not text:
            content.error = "Fallback extraction produced empty text"
            return content

        content.main_text = text
        content.extraction_method = "fallback"
        content.success = True

        # Try to extract <title> as a best-effort title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            content.title = _TAG_RE.sub("", title_match.group(1)).strip()

        if url:
            content.metadata["url"] = url

        return content

    @staticmethod
    def _safe_document_id(document_id: str) -> str:
        """Return a single safe cache filename component."""
        value = re.sub(r"[^A-Za-z0-9_.-]", "_", str(document_id)).strip("._")
        if not value or len(value) > 96:
            value = hashlib.sha256(str(document_id).encode("utf-8")).hexdigest()[:32]
        return value

    @staticmethod
    def _compute_text_hash(text: str) -> str:
        """Compute MD5 hex digest of *text*.

        Args:
            text: The string to hash.

        Returns:
            32-character lowercase hex digest.
        """
        return hashlib.md5(text.encode("utf-8")).hexdigest()
