"""Audited maintenance for evidence produced by older adapters."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .contracts import ClaimStatus, EvidenceRecord, _as_mapping, _safe_float, _safe_text, _text_list, utc_now
from .policies import ClaimPolicy


def invalidate_ambiguous_entity_evidence(repository: Any, *, as_of: str = "") -> dict[str, int]:
    """Invalidate old web snippets that did not ground the security code.

    Earlier rule extraction accepted a short company name as a substring.  A
    result about another similarly named group could therefore create a false
    counter/support.  Rows are retained for audit and marked invalidated; Claim
    conflicts are then re-evaluated through the normal policy.
    """
    invalidated = 0
    revalidated_evidence = 0
    lineage_rewritten = 0
    recovered_claims = 0
    affected_claim_ids: set[str] = set()
    if repository is None:
        return {
            "invalidated_evidence": 0,
            "revalidated_evidence": 0,
            "lineage_rewritten": 0,
            "recovered_claims": 0,
            "policy_revalidated_claims": 0,
        }
    as_of = _safe_text(as_of)
    try:
        evidence_rows = repository.list_evidence(limit=10000) or []
    except Exception:
        evidence_rows = []
    if not isinstance(evidence_rows, (list, tuple, set)):
        evidence_rows = []
    for evidence in evidence_rows:
        if not isinstance(evidence, EvidenceRecord):
            continue
        if evidence.extraction_method != "grounded_rule":
            continue
        try:
            claim = repository.get_claim(evidence.claim_id)
        except Exception:
            continue
        if claim is None:
            continue
        metadata = _as_mapping(evidence.metadata)
        locator = _as_mapping(evidence.locator)
        evidence.metadata = metadata
        evidence.locator = locator
        claim.attributes = _as_mapping(claim.attributes)
        url = _safe_text(locator.get("url"))
        try:
            url_parts = urlsplit(url)
        except ValueError:
            url_parts = urlsplit("")
        domain = url_parts.netloc.lower()
        path = url_parts.path.lower()
        generic_quote_page = (
            domain.startswith("quote.eastmoney.com")
            or (domain.endswith("data.eastmoney.com") and "/notices/stock/" in path)
            or (
                domain.endswith("vip.stock.finance.sina.com.cn")
                and "vcb_allbulletin" in path
            )
        ) or any(
            marker in str(evidence.quote or "")[:180]
            for marker in ("最新价格_行情_走势图", "实时行情走势")
        )
        code_match = re.search(r"\d{6}", str(claim.subject_id or ""))
        metadata_symbol = re.sub(r"\D", "", _safe_text(metadata.get("symbol")))[:6]
        official_symbol_match = bool(
            code_match
            and metadata.get("source_backend") == "cninfo_pdf"
            and metadata_symbol == code_match.group(0)
        )
        normalized_name = re.sub(
            r"[^0-9a-z\u4e00-\u9fa5]+", "", str(claim.subject_name or "").lower()
        )
        missing_required_code = bool(
            code_match
            and code_match.group(0) not in str(evidence.quote or "")
            and len(normalized_name) <= 6
            and not official_symbol_match
        )
        object_terms = [
            _safe_text(claim.object_name),
            *_text_list(_as_mapping(claim.attributes).get("object_aliases")),
        ]
        ungrounded_counter = bool(
            evidence.stance == "counter"
            and not _counter_grounded(object_terms, str(evidence.quote or ""))
        )
        ungrounded_official_support = False
        if (
            evidence.stance == "support"
            and metadata.get("source_backend") == "cninfo_pdf"
        ):
            from .perilla_adapter import GroundedEvidenceExtractor

            ungrounded_official_support = not bool(
                GroundedEvidenceExtractor._official_relationship_context(
                    object_terms, str(evidence.quote or "")
                )
            )
        if (
            metadata.get("invalidated")
            and metadata.get("invalidation_reason")
            == "entity_disambiguation_v2_missing_security_code"
            and official_symbol_match
            and not ungrounded_official_support
        ):
            evidence.metadata = {
                key: value for key, value in metadata.items()
                if key not in {"invalidated", "invalidation_reason", "invalidated_at"}
            }
            revalidated_evidence += 1
            affected_claim_ids.add(claim.claim_id)
        if not metadata.get("invalidated") and (
            missing_required_code
            or generic_quote_page
            or ungrounded_counter
            or ungrounded_official_support
        ):
            if generic_quote_page:
                reason = "generic_quote_page_not_relationship_evidence"
            elif ungrounded_counter:
                reason = "counter_not_grounded_to_researched_relationship"
            elif ungrounded_official_support:
                reason = "official_theme_mention_without_company_relationship"
            else:
                reason = "entity_disambiguation_v2_missing_security_code"
            evidence.metadata = {
                **metadata,
                "invalidated": True,
                "invalidation_reason": reason,
                "invalidated_at": as_of or utc_now(),
            }
            invalidated += 1
            affected_claim_ids.add(claim.claim_id)

        family = _publisher_family(domain)
        title = (_safe_text(evidence.quote).splitlines() or [""])[0]
        normalized_title = re.sub(
            r"[^0-9a-z\u4e00-\u9fa5]+", "", title.lower()
        )[:160]
        if family != "unknown" and normalized_title:
            import hashlib

            new_lineage = hashlib.sha1(
                f"{family}|{normalized_title}".encode("utf-8")
            ).hexdigest()[:20]
            if evidence.lineage_id != new_lineage:
                evidence.lineage_id = new_lineage
                lineage_rewritten += 1
                affected_claim_ids.add(claim.claim_id)
        if family == "eastmoney.com" and any(
            host in domain for host in ("caifuhao.", "emcreative.")
        ):
            evidence.source_quality = min(_safe_float(evidence.source_quality), 0.3)
        repository.save_evidence(evidence)

    policy = ClaimPolicy()
    try:
        claim_rows = repository.list_claims(limit=10000) or []
    except Exception:
        claim_rows = []
    if not isinstance(claim_rows, (list, tuple, set)):
        claim_rows = []
    for claim in claim_rows:
        if not hasattr(claim, "attributes"):
            continue
        claim.attributes = _as_mapping(claim.attributes)
        if claim.attributes.get("theme_id"):
            claim.high_impact = True
            repository.save_claim(claim)
            affected_claim_ids.add(claim.claim_id)

    policy_revalidated = 0
    for claim_id in affected_claim_ids:
        claim = repository.get_claim(claim_id)
        if claim is None:
            continue
        try:
            claim_evidence = repository.list_evidence(claim_id, limit=1000) or []
            if not isinstance(claim_evidence, (list, tuple, set)):
                claim_evidence = []
            decision = policy.evaluate(
                claim,
                claim_evidence,
                as_of=as_of,
                historical=False,
            )
        except Exception:
            continue
        if (
            str(claim.research_status).upper() == ClaimStatus.CONFLICTED.value
            and decision.target_status != ClaimStatus.CONFLICTED.value
        ):
            claim.conflict_status = "resolved"
            claim.research_status = decision.target_status
            claim.evidence_confidence = decision.confidence
            repository.save_claim(claim)
            recovered_claims += 1
        elif (
            str(claim.research_status).upper() in {
                ClaimStatus.ACTIVE.value, ClaimStatus.TRUSTED.value,
            }
            and decision.target_status != ClaimStatus.ACTIVE.value
        ):
            claim.attributes = {
                **_as_mapping(claim.attributes),
                "policy_revalidation_required": True,
                "policy_revalidation_reason": list(decision.reason_codes),
            }
            claim.research_status = ClaimStatus.DORMANT.value
            claim.evidence_confidence = decision.confidence
            repository.save_claim(claim)
            policy_revalidated += 1
    return {
        "invalidated_evidence": invalidated,
        "revalidated_evidence": revalidated_evidence,
        "lineage_rewritten": lineage_rewritten,
        "recovered_claims": recovered_claims,
        "policy_revalidated_claims": policy_revalidated,
    }


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


def _counter_grounded(object_terms: list[str], text: str) -> bool:
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
