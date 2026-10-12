"""Production retrieval adapters for first-party and web evidence."""
from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from pypdf import PdfReader

from core.research.perilla.discovery.models import SourceDocument
from core.research.control_plane.contracts import (
    Claim,
    _as_mapping,
    _safe_bool,
    _safe_float,
    _safe_int,
    _safe_text,
    _text_list,
)

logger = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


class CninfoPdfRetriever:
    """Retrieve and ground official CNINFO PDF announcement excerpts."""

    def __init__(
        self,
        repository: Any,
        *,
        provider: Any | None = None,
        as_of: str = "",
        cache_dir: str | Path = "data/research_cache/cninfo_pdf",
        max_pdf_attempts: int = 3,
        request_timeout: float = 12.0,
    ) -> None:
        if provider is None:
            from infrastructure.data_providers.research.cninfo_provider import CninfoProvider

            provider = CninfoProvider()
        self.repository = repository
        self.provider = provider
        self.as_of = _safe_text(as_of)
        self.cache_dir = Path(cache_dir)
        self.max_pdf_attempts = max(1, min(_safe_int(max_pdf_attempts, 3), 20))
        self.request_timeout = max(3.0, _safe_float(request_timeout, 12.0))
        self._document_cache: dict[str, SourceDocument | None] = {}

    def search(
        self,
        query: str,
        *,
        tier: str,
        limit: int,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        limit = max(1, _safe_int(limit, 1))
        tier = _safe_text(tier).lower()
        # ``first_party`` is the explicit Phase 3 route.  ``high`` remains
        # valid for the legacy ProductionResearchRetriever path.
        if tier not in {"high", "first_party"}:
            return []
        code_match = re.search(r"\d{6}", str(query or ""))
        if not code_match:
            return []
        code = code_match.group(0)
        context = _as_mapping(context)
        raw_terms = context.get("discovery_terms", [])
        discovery_terms = _text_list(raw_terms)
        if discovery_terms:
            claim = Claim(
                subject_id=_safe_text(context.get("symbol"), code),
                subject_name=_safe_text(context.get("company_name"), code),
                object_name=discovery_terms[0],
                attributes={"object_aliases": discovery_terms[1:], "retrieval_only": True},
            )
            terms = list(dict.fromkeys(discovery_terms))
        else:
            claim = self._claim_for_code(code, query=query)
            if claim is None:
                return []
            terms = list(dict.fromkeys([
                str(claim.object_name or ""),
                *(str(value) for value in (claim.attributes or {}).get("object_aliases", [])),
            ]))
        terms = [term for term in terms if len(term.strip()) >= 2]
        if not terms:
            return []

        rows: list[dict[str, Any]] = []
        periodic_only = _safe_bool(context.get("periodic_only"))
        targeted_search = getattr(self.provider, "search_announcements", None)
        if not periodic_only and callable(targeted_search):
            for term in terms[:3]:
                try:
                    rows.extend(_mapping_rows(targeted_search(f"{code} {term}", count=8)))
                except Exception as exc:
                    logger.warning(
                        "CNINFO targeted search failed(%s, %s): %s", code, term, exc
                    )
        if not periodic_only:
            try:
                rows.extend(_mapping_rows(self.provider.get_announcements(code, count=20)))
            except Exception as exc:
                logger.warning("CNINFO announcement list failed(%s): %s", code, exc)
        try:
            rows.extend(_mapping_rows(self.provider.get_periodic_reports(code, count=6)))
        except Exception as exc:
            logger.warning("CNINFO periodic list failed(%s): %s", code, exc)
        rows = self._dedupe_and_rank(rows)

        documents: list[SourceDocument] = []
        attempted = 0
        for row in rows:
            if attempted >= self.max_pdf_attempts or len(documents) >= limit:
                break
            if not self._visible_as_of(str(row.get("date") or "")):
                continue
            url = str(row.get("pdf_url") or "")
            if not url:
                continue
            attempted += 1
            document = self._document_from_pdf(row, claim, terms)
            if document is not None:
                documents.append(document)
        return documents[:limit]

    def _claim_for_code(self, code: str, *, query: str = "") -> Any | None:
        try:
            raw_claims = self.repository.list_claims(limit=10000)
        except Exception as exc:
            logger.warning("research claim lookup failed(%s): %s", code, exc)
            return None
        if not isinstance(raw_claims, (list, tuple)):
            return None
        candidates = [
            claim
            for claim in raw_claims
            if re.sub(r"\D", "", _safe_text(_field(claim, "subject_id")))[:6] == code
        ]
        query_lower = str(query or "").lower()
        for claim in candidates:
            terms = [
                _safe_text(_field(claim, "object_name")),
                *_text_list(_as_mapping(_field(claim, "attributes")).get("object_aliases", [])),
            ]
            if any(term and term.lower() in query_lower for term in terms):
                return claim
        return candidates[0] if candidates else None

    def _visible_as_of(self, date_text: str) -> bool:
        if not self.as_of or not date_text:
            return True
        return date_text[:10] <= self.as_of[:10]

    @staticmethod
    def _dedupe_and_rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for row in _mapping_rows(rows):
            key = _safe_text(row.get("pdf_url")) or _safe_text(row.get("title"))
            if key:
                unique[key] = row

        def rank(row: dict[str, Any]) -> tuple[int, str]:
            title = _safe_text(row.get("title"))
            # A recent periodic report is preferable to a very old exact-title
            # hit.  Targeted full-text rows remain useful as a bounded fallback.
            score = 50 if _safe_bool(row.get("targeted_search")) else 0
            if _safe_text(row.get("report_type")):
                score += 100
            if any(marker in title for marker in ("提示性公告", "预约披露", "披露时间")):
                score -= 150
            if re.search(r"(?:年度|半年度|一季度|三季度|第一季度|第三季度)报告$", title):
                score += 120
            for marker, weight in (
                ("问询函的回复", 100), ("年度报告", 90), ("年报", 85),
                ("异常波动", 80), ("季度报告", 70), ("投资者关系", 65),
                ("风险提示", 60),
            ):
                if marker in title:
                    score += weight
            return (score, _safe_text(row.get("date")))

        return sorted(unique.values(), key=rank, reverse=True)

    def _document_from_pdf(
        self, row: dict[str, Any], claim: Any, terms: list[str]
    ) -> SourceDocument | None:
        url = _safe_text(row.get("pdf_url")).replace(
            "http://static.cninfo.com.cn", "https://static.cninfo.com.cn"
        )
        if not url:
            return None
        cache_key = self._document_cache_key(url, terms)
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]
        try:
            payload = self._load_pdf(url)
            reader = PdfReader(io.BytesIO(payload), strict=False)
            matches: list[tuple[int, str, str]] = []
            for page_number, page in enumerate(reader.pages[:200], start=1):
                text = str(page.extract_text() or "")
                if not text:
                    continue
                matched_terms = [
                    term for term in terms if term.lower() in text.lower()
                ]
                if matched_terms:
                    # Keep the full matched page (bounded) so the extractor can
                    # distinguish an industry-overview mention from a later
                    # company-specific business paragraph on the same page.
                    matches.append((page_number, " / ".join(matched_terms), text[:12000]))
                if len(matches) >= 4:
                    break
            if not matches:
                self._document_cache[cache_key] = None
                return None

            code = re.sub(r"\D", "", _safe_text(_field(claim, "subject_id")))[:6]
            company = _safe_text(_field(claim, "subject_name")) or _safe_text(row.get("name"))
            content_parts = [f"公告主体：{company}（{code}）"]
            for page_number, term, excerpt in matches:
                content_parts.append(
                    f"公告主体：{company}（{code}）。第{page_number}页，"
                    f"命中研究词「{term}」：\n{excerpt}"
                )
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            document_id = "cninfo_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
            document = SourceDocument(
                document_id=document_id,
                source_type="announcement",
                url=url,
                title=str(row.get("title") or "CNINFO 公告"),
                site_name="cninfo.com.cn",
                published_at=str(row.get("date") or "") or None,
                fetched_at=fetched_at,
                content_text="\n\n".join(content_parts),
                text_hash=hashlib.sha256(payload).hexdigest(),
                credibility_hint="high",
                metadata={
                    "symbol": code,
                    "company_name": company,
                    "matched_terms": [term for _, term, _ in matches],
                    "matched_pages": [page for page, _, _ in matches],
                    "source_backend": "cninfo_pdf",
                    "provider": "cninfo",
                    "canonical_url": url,
                    "source_lineage_id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:20],
                    "robots_policy": "allow",
                    "license_policy": "official_cninfo",
                    "rate_limit_policy": "provider_default",
                    "availability_status": "ok",
                },
            )
            self._document_cache[cache_key] = document
            return document
        except Exception as exc:
            logger.warning("CNINFO PDF extraction failed(%s): %s", url, exc)
            self._document_cache[cache_key] = None
            return None

    @staticmethod
    def _document_cache_key(url: str, terms: list[str]) -> str:
        normalized_terms = "|".join(sorted({term.lower() for term in _text_list(terms)}))
        return hashlib.sha1(f"{url}|{normalized_terms}".encode("utf-8")).hexdigest()

    def _load_pdf(self, url: str) -> bytes:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or hostname not in {
            "static.cninfo.com.cn", "www.cninfo.com.cn", "cninfo.com.cn",
        }:
            raise ValueError("CNINFO PDF URL is not an allowed HTTPS host")
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{key}.pdf"
        if path.exists() and path.stat().st_size > 0:
            return path.read_bytes()
        response = requests.get(
            url,
            timeout=self.request_timeout,
            headers={"User-Agent": "Mozilla/5.0 AquaTrade-Research/1.0"},
        )
        response.raise_for_status()
        payload = getattr(response, "content", b"")
        if not isinstance(payload, bytes):
            raise ValueError("CNINFO response body is not bytes")
        if not payload.startswith(b"%PDF"):
            raise ValueError("CNINFO response is not PDF")
        if len(payload) > 25 * 1024 * 1024:
            raise ValueError("CNINFO PDF exceeds 25MB safety limit")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".pdf.tmp")
        temp.write_bytes(payload)
        temp.replace(path)
        return payload


class ProductionResearchRetriever:
    """Official-first retriever with bounded web fallback."""

    def __init__(self, official: CninfoPdfRetriever, web: Any) -> None:
        self.official = official
        self.web = web

    def search(self, query: str, *, tier: str, limit: int) -> list[Any]:
        limit = max(1, _safe_int(limit, 1))
        try:
            official_rows = list(self.official.search(query, tier=tier, limit=limit) or [])
        except Exception as exc:
            logger.warning("official research retrieval failed: %s", exc)
            official_rows = []
        if len(official_rows) >= limit:
            return official_rows[:limit]
        try:
            web_rows = list(self.web.search(query, tier=tier, limit=limit) or [])
        except Exception as exc:
            logger.warning("web research retrieval failed: %s", exc)
            web_rows = []
        seen = {_safe_text(_field(item, "url")) for item in official_rows}
        merged = list(official_rows)
        for item in web_rows:
            url = _safe_text(_field(item, "url"))
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            merged.append(item)
            if len(merged) >= limit:
                break
        return merged
