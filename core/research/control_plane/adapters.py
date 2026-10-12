"""历史模型到统一契约的防御性适配器。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    Claim,
    DocumentVersion,
    EvidenceRecord,
    SourceTrace,
    _as_mapping,
    _safe_float,
    _safe_text,
    _text_list,
)


def _value(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n", ""}:
            return False
    return default


def _unit_float(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _safe_float(value, default)))


def adapt_raw_document(raw: Any) -> tuple[SourceTrace, DocumentVersion]:
    url = _safe_text(_value(raw, "source_url", ""))
    source = SourceTrace(
        original_source=url,
        canonical_url=url,
        provider=_safe_text(_value(raw, "provider", "unknown"), "unknown"),
        source_type=_safe_text(_value(raw, "document_type", "unknown"), "unknown"),
        retrieved_at=_safe_text(_value(raw, "fetched_at", _value(raw, "created_at", ""))),
    )
    document = DocumentVersion(
        document_id=_safe_text(_value(raw, "document_id", "")),
        title=_safe_text(_value(raw, "title", "")),
        content_text=_safe_text(_value(raw, "content_text", "")),
        content_hash=_safe_text(_value(raw, "raw_hash", "")),
        source_trace_id=source.source_trace_id,
        published_at=_safe_text(_value(raw, "published_at", "")),
        retrieved_at=source.retrieved_at,
        content_available=_safe_bool(_value(raw, "content_available", False)),
        parse_status=_safe_text(_value(raw, "parse_status", "unknown"), "unknown"),
    )
    return source, document


def adapt_legacy_evidence(raw: Any, claim_id: str = "") -> EvidenceRecord:
    metadata = _as_mapping(_value(raw, "metadata", {}))
    kind = _safe_text(metadata.get("evidence_kind")) or (
        "generated" if _safe_bool(_value(raw, "is_derived", False)) else "document"
    )
    return EvidenceRecord(
        evidence_id=_safe_text(_value(raw, "evidence_id", "")),
        claim_id=_safe_text(claim_id),
        document_version_id=_safe_text(_value(raw, "document_id", "")),
        source_trace_id=_safe_text(_value(raw, "source_id", "")),
        lineage_id=_safe_text(metadata.get("source_lineage_id") or _value(raw, "raw_hash", "")),
        stance=_safe_text(metadata.get("stance"), "neutral"),
        evidence_kind=kind,
        quote=_safe_text(_value(raw, "quote", _value(raw, "evidence_text", ""))),
        source_quality=_unit_float(metadata.get("quality"), 0.5),
        match_score=_unit_float(metadata.get("match_score")),
        published_at=_safe_text(_value(raw, "publish_date", _value(raw, "data_date", ""))),
        retrieved_at=_safe_text(metadata.get("retrieved_at") or _value(raw, "trade_date", "")),
        extraction_method=_safe_text(_value(raw, "extracted_by", _value(raw, "extraction_method", "unknown")), "unknown"),
        grounding_status=_safe_text(_value(raw, "grounding_status", "unknown"), "unknown"),
        metadata=metadata,
    )


def adapt_candidate(candidate: Any) -> Claim:
    attributes = _as_mapping(_value(candidate, "attributes", {}))
    support_terms = _text_list(attributes.get("support_terms")) or _text_list(_value(candidate, "support_terms", []))
    counter_terms = _text_list(attributes.get("counter_terms")) or _text_list(_value(candidate, "counter_terms", []))
    importance = _safe_text(attributes.get("importance"), "medium")
    # 把 importance 也写回 attributes，供下游 ClaimPolicy 读取节点重要程度
    attributes["importance"] = importance
    return Claim(
        claim_id=_safe_text(_value(candidate, "candidate_id", "")),
        subject_id=_safe_text(attributes.get("subject_id") or _value(candidate, "subject_id", "")),
        subject_name=_safe_text(attributes.get("subject_name") or _value(candidate, "name", "")),
        relation=_safe_text(attributes.get("relation") or _value(candidate, "relation", "related_to"), "related_to"),
        object_id=_safe_text(attributes.get("object_id") or _value(candidate, "object_id", "")),
        object_name=_safe_text(attributes.get("object_name")),
        valid_from=_safe_text(attributes.get("valid_from")),
        valid_to=_safe_text(attributes.get("valid_to")),
        research_status=_safe_text(_value(candidate, "status", "DISCOVERED"), "DISCOVERED"),
        evidence_confidence=_unit_float(_value(candidate, "activation_score")),
        mapping_confidence=_unit_float(attributes.get("mapping_confidence")),
        support_terms=support_terms,
        counter_terms=counter_terms,
        attributes=attributes,
    )

