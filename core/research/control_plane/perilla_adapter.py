"""现有 SearchDiscovery 到统一控制面的实际适配。"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import (
    Claim, DocumentVersion, EvidenceRecord, SourceTrace, _as_mapping, _safe_float,
    _safe_int, _safe_text, _text_list, utc_now,
)
from .governance import SourceGovernance
from .repository import ResearchRepository


def _document_field(document: Any, name: str, default: Any = "") -> Any:
    if isinstance(document, Mapping):
        return document.get(name, default)
    return getattr(document, name, default)


class SearchDiscoveryRetriever:
    def __init__(self, discovery: Any, *, body_crawler: Any | None = None) -> None:
        self.discovery = discovery
        self.body_crawler = body_crawler

    def search(self, query: str, *, tier: str, limit: int) -> list[Any]:
        if self.discovery is None or not callable(getattr(self.discovery, "search", None)):
            return []
        limit = max(1, _safe_int(limit, 1))
        try:
            raw_documents = self.discovery.search(query) or []
        except Exception:
            return []
        if isinstance(raw_documents, Mapping):
            raw_documents = raw_documents.get("documents", [])
        documents = list(raw_documents)[:limit] if isinstance(raw_documents, (list, tuple)) else []
        resolved: list[Any] = []
        for document in documents:
            url = _safe_text(_document_field(document, "url"))
            try:
                parsed_url = urlsplit(url)
            except ValueError:
                parsed_url = urlsplit("")
            if (
                self.body_crawler is not None
                and url
                and parsed_url.scheme.lower() in {"http", "https"}
                and bool(parsed_url.netloc)
            ):
                try:
                    body_document = self.body_crawler.crawl_url(url)
                except Exception:
                    body_document = None
                if body_document is not None and _safe_text(
                    _document_field(body_document, "content_text")
                ):
                    body_metadata = _as_mapping(
                        _document_field(body_document, "metadata", {})
                    )
                    body_metadata.update({
                        "retrieval_tier": tier,
                        "search_result_url": url,
                        "body_fetched": True,
                        "snippet_only": False,
                        "content_status": "body",
                    })
                    if isinstance(body_document, Mapping):
                        body_document = dict(body_document)
                        body_document["metadata"] = body_metadata
                    else:
                        try:
                            body_document.metadata = body_metadata
                        except (AttributeError, TypeError):
                            continue
                    resolved.append(body_document)
                    continue
            metadata = _as_mapping(_document_field(document, "metadata", {}))
            # Search APIs return a title/snippet, not the publisher's body.
            # Keep the result for discovery/audit, but make the evidence gate
            # reject it until a body crawler produces a new document.
            metadata.update({
                "snippet_only": True,
                "content_status": "snippet",
                "retrieval_tier": tier,
            })
            if isinstance(document, Mapping):
                document = dict(document)
                document["metadata"] = metadata
            else:
                try:
                    document.metadata = metadata
                except Exception:
                    continue
            resolved.append(document)
        return resolved


class GroundedEvidenceExtractor:
    GROUNDING_VERSION = "relationship_grounding_v5"
    RELATION_TERMS = re.compile(
        r"概念|业务|产品|供应|供货|合作|参与|布局|属于|相关|产业链|订单|客户|项目|"
        r"技术|研发|生产|销售|收入|服务|子公司|投资|收购|封装|测试"
    )

    def __init__(self, repository: ResearchRepository, governance: SourceGovernance | None = None) -> None:
        self.repository = repository
        self.governance = governance or SourceGovernance()

    def extract(self, document: Any, claim: Claim, *, query_purpose: str) -> list[EvidenceRecord]:
        if document is None or not isinstance(claim, Claim):
            return []
        text = _safe_text(
            _document_field(document, "content_text")
            or _document_field(document, "content")
        )
        title = _safe_text(_document_field(document, "title"))
        url = _safe_text(
            _document_field(document, "url")
            or _document_field(document, "source_url")
        )
        combined = f"{title}\n{text}".strip()
        subject = _safe_text(claim.subject_name) or _safe_text(claim.subject_id)
        obj = _safe_text(claim.object_name) or _safe_text(claim.object_id)
        # Security-code-first entity disambiguation.  Short A-share names are
        # frequently substrings of unrelated groups (e.g. 中国长城 vs 中国长城
        # 工业集团); require the six-digit code for those web snippets.
        code_match = re.search(r"\d{6}", str(claim.subject_id or ""))
        security_code = code_match.group(0) if code_match else ""
        document_metadata = _as_mapping(_document_field(document, "metadata", {}))
        metadata_symbol_match = bool(
            security_code
            and security_code
            == re.sub(r"\D", "", str(document_metadata.get("symbol") or ""))[:6]
        )
        if security_code:
            subject_anchor = security_code if security_code in combined else ""
            if not subject_anchor and metadata_symbol_match and subject.lower() in combined.lower():
                subject_anchor = subject
            if not subject_anchor and len(self._normalized(subject)) > 6:
                subject_anchor = subject if subject.lower() in combined.lower() else ""
        else:
            subject_anchor = subject if subject and subject.lower() in combined.lower() else ""
        if not subject_anchor:
            return []
        object_terms = [
            str(value) for value in [
                obj, *(_text_list(_as_mapping(claim.attributes).get("object_aliases")))
            ] if str(value or "").strip()
        ]
        matched_object = next(
            (term for term in object_terms if term.lower() in combined.lower()), ""
        )
        if obj and not matched_object:
            return []
        if matched_object:
            official_symbol_grounded = bool(
                metadata_symbol_match
                and document_metadata.get("source_backend") == "cninfo_pdf"
            )
            official_grounded_quote = ""
            if official_symbol_grounded:
                official_grounded_quote = self._official_relationship_context(
                    object_terms, combined
                )
                if not official_grounded_quote:
                    return []
            elif not self._relation_grounded(subject_anchor, matched_object, combined):
                return []
        else:
            official_grounded_quote = ""
        source_type = _safe_text(
            _document_field(document, "source_type"), "unknown"
        )
        canonical = self._canonical_url(url)
        try:
            canonical_parts = urlsplit(canonical)
        except ValueError:
            canonical_parts = urlsplit("")
        domain = canonical_parts.netloc.lower()
        generic_directory_page = (
            domain.startswith("quote.eastmoney.com")
            or (
                domain.endswith("data.eastmoney.com")
                and "/notices/stock/" in canonical_parts.path.lower()
            )
            or (
                domain.endswith("vip.stock.finance.sina.com.cn")
                and "vcb_allbulletin" in canonical_parts.path.lower()
            )
        )
        if generic_directory_page or any(
            marker in title for marker in ("最新价格_行情_走势图", "实时行情走势")
        ):
            return []
        if any(host in domain for host in ("cninfo.com.cn", "sse.com.cn", "szse.cn")):
            source_type = "announcement"
        elif any(host in domain for host in ("caifuhao.eastmoney.com", "emcreative.eastmoney.com")):
            source_type = "forum"
        metadata = document_metadata
        publisher_family = self._publisher_family(domain)
        title_key = self._normalized(title)[:160]
        lineage = str(
            metadata.get("source_lineage_id")
            or hashlib.sha1(
                f"{publisher_family}|{title_key}".encode("utf-8")
            ).hexdigest()[:20]
        )
        trace = SourceTrace(
            original_source=url, canonical_url=canonical, lineage_id=lineage,
            provider=publisher_family or _safe_text(
                _document_field(document, "site_name"), "unknown"
            ),
            source_type=source_type,
            license_policy=_safe_text(metadata.get("license_policy"), "unknown"),
            robots_policy=_safe_text(metadata.get("robots_policy"), "unknown"),
            availability_status="ok",
            retrieved_at=str(
                _document_field(document, "retrieved_at")
                or _document_field(document, "fetched_at")
                or utc_now()
            ),
            metadata=metadata,
        )
        governance = self.governance.evaluate(trace)
        if not governance.allowed:
            return []
        try:
            self.repository.save_source_trace(trace)
        except Exception:
            return []
        published_at = _safe_text(_document_field(document, "published_at"))
        doc = DocumentVersion(
            document_id=_safe_text(_document_field(document, "document_id")),
            # Evidence quotes are sliced from ``combined`` (title + body), so
            # the persisted document must use that same canonical text or the
            # repository's fail-closed quote-grounding check will reject valid
            # official documents.
            title=title, content_text=combined, source_trace_id=trace.source_trace_id,
            published_at=published_at, retrieved_at=trace.retrieved_at,
            parse_status="ok" if text else "partial", metadata=metadata,
        )
        try:
            self.repository.save_document(doc)
        except Exception:
            return []
        stance = (
            "counter"
            if self._counter_grounded(object_terms, combined)
            else "support" if query_purpose == "support" else "neutral"
        )
        quote = (official_grounded_quote or combined)[:1000]
        quote_start = combined.find(quote)
        if quote_start < 0:
            quote_start = 0
        first_party = source_type in {
            "official", "announcement", "financial_report", "company_website",
            "official_site", "investor_interaction", "cninfo_interaction",
        }
        # 证据分层：董秘互动 + 公司公告 + 财报 + 官网 → 第一层级（100% 采信）
        # 法律层面：公司公告错误发布需承担法律责任，董秘互动内容基本为事实。
        # 第一方来源 source_quality=1.0，其他来源沿用 governance 先验。
        source_quality = max(
            0.0, min(1.0, 1.0 if first_party else _safe_float(governance.reliability_prior))
        )
        evidence_id = "ev_grounded_" + hashlib.sha1(
            f"{claim.claim_id}|{lineage}|{stance}|{quote}".encode("utf-8")
        ).hexdigest()[:16]
        return [EvidenceRecord(
            evidence_id=evidence_id,
            claim_id=claim.claim_id, document_version_id=doc.document_version_id,
            source_trace_id=trace.source_trace_id, lineage_id=lineage, stance=stance,
            quote=quote, locator={
                "url": canonical,
                "char_start": quote_start,
                "char_end": quote_start + len(quote),
            },
            source_quality=source_quality, match_score=1.0,
            published_at=published_at, retrieved_at=trace.retrieved_at,
            extraction_method="grounded_rule", grounding_status="exact",
            is_first_party=first_party, metadata={
                "slots": ["identity", "relationship"],
                "query_purpose": query_purpose,
                "matched_object_term": matched_object,
                "grounding_version": self.GROUNDING_VERSION,
                "evidence_tier": "first_party_authoritative" if first_party else "second_hand",
                **metadata,
            },
        )]

    @staticmethod
    def _normalized(value: Any) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value or "").lower())

    @classmethod
    def _relation_grounded(cls, subject: str, obj: str, text: str) -> bool:
        lowered = text.lower()
        subject_positions = [
            match.start() for match in re.finditer(re.escape(subject.lower()), lowered)
        ]
        object_positions = [
            match.start() for match in re.finditer(re.escape(obj.lower()), lowered)
        ]
        if not subject_positions or not object_positions:
            return False
        subject_pos, object_pos = min(
            ((s, o) for s in subject_positions for o in object_positions),
            key=lambda pair: abs(pair[0] - pair[1]),
        )
        if abs(subject_pos - object_pos) > 180:
            return False
        start = max(0, min(subject_pos, object_pos) - 60)
        end = min(len(text), max(subject_pos + len(subject), object_pos + len(obj)) + 80)
        return bool(cls.RELATION_TERMS.search(text[start:end]))

    @classmethod
    def _object_relation_grounded(cls, obj: str, text: str) -> bool:
        """Compatibility wrapper for official relationship grounding."""
        return bool(cls._official_relationship_context([obj], text))

    @classmethod
    def _official_relationship_context(
        cls, object_terms: list[str], text: str
    ) -> str:
        """Return the strongest company-specific excerpt from an official PDF.

        A theme word in an industry-overview paragraph is not company
        membership.  The excerpt must contain direct operating action (R&D,
        production, capacity, customer, order, investment, subsidiary, etc.)
        or a company marker plus concrete business language.  Generic market
        and industry passages are penalized.
        """
        action_terms = re.compile(
            r"布局|成立|研发|生产|销售|量产|导入|突破|提供|推进|优化|建设|承接|中标|签订|"
            r"合作|客户|订单|收入|产能|子公司|收购|从事|主营"
        )
        business_terms = re.compile(r"业务|产品|技术|项目|服务|封装|测试|制造")
        company_terms = re.compile(r"公司|本公司|上市公司|控股子公司|报告期内")
        generic_terms = re.compile(
            r"行业情况|行业格局|行业发展|行业技术|市场规模|数据显示|预测|"
            r"公司所处的行业|研究报告显示|行业整体|新兴行业|行业需求|行业趋势"
        )
        best: tuple[int, str] | None = None
        segments = [segment.strip() for segment in re.split(r"\n\s*\n", text) if segment.strip()]
        for segment in segments:
            # The retriever prepends ``公告主体`` for identity/audit.  It is
            # not semantic proof that the following market paragraph describes
            # the company's own operations, so exclude the injected prefix
            # from relationship scoring.
            semantic = re.sub(
                r"^公告主体：.*?命中研究词「.*?」：\s*",
                "",
                segment,
                count=1,
                flags=re.DOTALL,
            )
            lowered = semantic.lower()
            for raw_term in object_terms:
                term = str(raw_term or "").lower()
                if not term:
                    continue
                for match in re.finditer(re.escape(term), lowered):
                    context = semantic[
                        max(0, match.start() - 180): min(len(semantic), match.end() + 220)
                    ]
                    score = 0
                    has_action = bool(action_terms.search(context))
                    has_business = bool(business_terms.search(context))
                    has_company = bool(company_terms.search(context))
                    if has_action:
                        score += 3
                    if has_company:
                        score += 2
                    if has_business:
                        score += 1
                    if generic_terms.search(context):
                        score -= 4
                    if score < 4 or not has_company or not (has_action or has_business):
                        continue
                    candidate = context.strip()[:1000]
                    if best is None or score > best[0]:
                        best = (score, candidate)
        return best[1] if best else ""

    @staticmethod
    def _counter_grounded(object_terms: list[str], text: str) -> bool:
        """Require an explicit negated relationship near the researched object.

        Publisher disclaimers such as ``并未经过本网站证实`` and the standard
        listed-company phrase ``没有虚假记载、误导性陈述`` are not relationship
        counter-evidence.
        """
        compact = re.sub(r"\s+", "", str(text or ""))
        for raw_term in object_terms:
            term = re.escape(str(raw_term or "").strip())
            if not term:
                continue
            patterns = (
                rf"(?:不涉及|无相关业务|未开展|尚未开展|否认涉及|澄清不涉及|从未从事)"
                rf"[^。；;\n]{{0,50}}{term}",
                rf"{term}[^。；;\n]{{0,50}}(?:不涉及|无关|不存在|未开展|尚未开展|终止|取消|撤回)",
                rf"(?:未供货|停止供货|终止合作|取消合作)[^。；;\n]{{0,50}}{term}",
                rf"{term}[^。；;\n]{{0,50}}(?:未供货|停止供货|终止合作|取消合作)",
            )
            if any(re.search(pattern, compact, flags=re.IGNORECASE) for pattern in patterns):
                return True
        return False

    @staticmethod
    def _publisher_family(domain: str) -> str:
        domain = str(domain or "").lower().split(":", 1)[0]
        if not domain:
            return "unknown"
        for family in (
            "eastmoney.com", "cninfo.com.cn", "sse.com.cn", "szse.cn",
            "10jqka.com.cn", "sina.com.cn", "stcn.com", "cs.com.cn",
        ):
            if domain == family or domain.endswith("." + family):
                return family
        parts = domain.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else domain

    @staticmethod
    def _canonical_url(url: str) -> str:
        url = _safe_text(url)
        if not url:
            return ""
        try:
            parts = urlsplit(url)
        except ValueError:
            return ""
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return ""
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))

