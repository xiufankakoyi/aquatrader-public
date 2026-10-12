"""P3 PIT 生产认证来源门槛。"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Mapping

from core.research.pit_certification.contracts import (
    PITCertificationResult,
    compute_results_hash,
)

_NON_PRODUCTION = re.compile(r"(?:^|[_-])(mock|demo|test|fixture|sample)(?:[_-]|$)", re.I)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.I)


def audit_production_results(
    results: list[PITCertificationResult],
    provenance: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """验证结果是否具有可进入 P3 正式统计的最小生产来源证据。"""
    safe_results = [
        result for result in results
        if isinstance(result, PITCertificationResult)
    ] if isinstance(results, (list, tuple)) else []
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(provenance, Iterable):
        for raw_item in provenance:
            if not isinstance(raw_item, Mapping):
                continue
            item = dict(raw_item)
            indexed[(str(item.get("symbol", "")), str(item.get("pit_time", "")))] = item
    mock_count = 0
    missing_count = 0
    for result in safe_results:
        if (
            not result.model_version
            or not result.policy_version
            or _NON_PRODUCTION.search(result.model_version)
            or _NON_PRODUCTION.search(result.policy_version)
        ):
            mock_count += 1

        item = indexed.get((result.symbol, result.pit_time), {})
        refs = item.get("data_source_refs")
        try:
            pit_date = date.fromisoformat(result.pit_time[:10])
            model_date = date.fromisoformat(str(item.get("model_effective_from", ""))[:10])
            policy_date = date.fromisoformat(str(item.get("policy_effective_from", ""))[:10])
            versions_effective = model_date <= pit_date and policy_date <= pit_date
        except (TypeError, ValueError):
            versions_effective = False
        if not (
            isinstance(refs, list)
            and refs
            and all(isinstance(ref, str) and ref for ref in refs)
            and _SHA256.fullmatch(str(item.get("input_fingerprint", "")))
            and _SHA256.fullmatch(str(item.get("code_fingerprint", "")))
            and versions_effective
        ):
            missing_count += 1

    value_shapes = {
        (
            result.prediction_value,
            result.prediction_interval_low,
            result.prediction_interval_high,
            result.actual_value,
            result.consensus_value,
        )
        for result in safe_results
    }
    constant_demo_pattern = len(safe_results) >= 3 and len(value_shapes) == 1
    valid = bool(safe_results) and not mock_count and not missing_count and not constant_demo_pattern
    return {
        "production_provenance_valid": valid,
        "total_results": len(safe_results),
        "mock_or_demo_results": mock_count,
        "missing_provenance_results": missing_count,
        "constant_demo_pattern": constant_demo_pattern,
    }


def audit_ai_review(
    review: Mapping[str, Any],
    results: list[PITCertificationResult],
) -> dict[str, Any]:
    """验证 AI-only 独立复核身份、版本和结果哈希契约。"""
    review_data = dict(review) if isinstance(review, Mapping) else {}
    safe_results = [
        result for result in results
        if isinstance(result, PITCertificationResult)
    ] if isinstance(results, (list, tuple)) else []
    reasons: list[str] = []
    reviewer = str(review_data.get("reviewer_id", ""))
    author = str(review_data.get("model_author", ""))
    if not reviewer or reviewer == author:
        reasons.append("reviewer_not_independent")
    if review_data.get("reviewer_kind") != "ai":
        reasons.append("reviewer_kind_not_ai")
    for field in ("review_model", "review_prompt_version", "signed_at"):
        if not review_data.get(field):
            reasons.append(f"missing_{field}")
    if review_data.get("reviewed_results_hash") != compute_results_hash(safe_results):
        reasons.append("results_hash_mismatch")
    if review_data.get("decision") != "accepted":
        reasons.append("decision_not_accepted")
    return {"ai_review_valid": not reasons, "reasons": reasons}
