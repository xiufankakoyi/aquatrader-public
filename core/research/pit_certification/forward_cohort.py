"""Forward-only P3 shadow collection with cohort-bound hash-chain evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("Asia/Shanghai")


def _hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def build_forward_cohort(
    *,
    members: list[dict[str, Any]],
    input_records: list[dict[str, Any]],
    source_hashes: dict[str, str],
    code_hash: str,
    started_at: str,
    universe_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the baseline; baseline rows are never forward observations."""
    policy = {
        "policy_id": "p3-forward-research-observation-v2",
        "status": "forward_shadow",
        "effective_from": started_at,
        "minimum_days": 20,
        "p3_min_companies": 300,
        "p3_min_industries": 20,
        "p3_min_years": 5,
    }
    members = _records(members)
    input_records = _records(input_records)
    source_hashes = {
        str(key): str(value)
        for key, value in dict(source_hashes or {}).items()
    } if isinstance(source_hashes, Mapping) else {}
    code_hash = str(code_hash or "")
    started_at = str(started_at or "")
    baseline_dates = sorted({
        str(record.get("trade_date") or "")[:10]
        for record in input_records
        if str(record.get("trade_date") or "")
    })
    baseline_trade_date = baseline_dates[-1] if baseline_dates else ""
    payload = {
        "schema_version": 2,
        "mode": "forward_shadow",
        "started_at": started_at,
        "available_at": started_at,
        "historical_certification_claimed": False,
        "research_scope": "研究观察，不构成交易指令",
        "members": members,
        "universe_policy": dict(universe_policy) if isinstance(universe_policy, Mapping) else {},
        "baseline": {
            "latest_trade_date": baseline_trade_date,
            "input_records": input_records,
            "input_fingerprint": _hash(input_records),
            "forward_day_count": 0,
        },
        "provenance": {
            "source_hashes": source_hashes,
            "input_fingerprint": _hash(input_records),
            "data_fingerprint": _hash(
                {"records": input_records, "sources": source_hashes}
            ),
            "code_fingerprint": code_hash,
            "model": {
                "model_id": "aquatrade-research-os-forward",
                "status": "forward_shadow",
                "effective_from": started_at,
                "fingerprint": code_hash,
            },
            "policy": {**policy, "fingerprint": _hash(policy)},
        },
    }
    payload["cohort_hash"] = _hash(payload)
    payload["ai_review"] = {
        "status": "AI_INTERNAL_PENDING_REVIEW",
        "reviewer_id": "",
        "reviewer_kind": "",
        "model_author": "aquatrade-research-os",
        "review_model": "",
        "review_prompt_version": "p3-forward-cohort-review-v2",
        "review_target_hash": payload["cohort_hash"],
        "reviewed_results_hash": "",
        "decision": "pending",
        "signed_at": "",
    }
    return payload


def _parse_exact_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw or "T" not in raw:
        raise ValueError("exact timezone-aware timestamp required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(MARKET_TZ)


def _validate_observation_record(
    record: dict[str, Any],
    *,
    baseline_trade_date: str,
) -> list[str]:
    if not isinstance(record, Mapping):
        return ["record_not_mapping"]
    errors: list[str] = []
    trade_date = str(record.get("trade_date") or "")[:10]
    if not trade_date or trade_date <= str(baseline_trade_date or "")[:10]:
        errors.append("trade_date_not_after_baseline")
    report_as_of = str(record.get("research_report_as_of") or "")
    if not report_as_of or report_as_of[:10] != trade_date:
        errors.append("research_report_as_of_mismatch")
    try:
        available_at = _parse_exact_timestamp(record.get("available_at"))
        written_at = _parse_exact_timestamp(record.get("written_at"))
        market_close = datetime.combine(
            datetime.fromisoformat(trade_date).date(),
            time(15, 0),
            tzinfo=MARKET_TZ,
        )
        if available_at < market_close:
            errors.append("available_before_market_close")
        if available_at > written_at:
            errors.append("available_after_write")
    except (TypeError, ValueError):
        errors.append("invalid_available_or_written_at")
    return errors


def validate_observation_chain(
    records: list[dict[str, Any]],
    *,
    cohort_hash: str,
    baseline_trade_date: str,
) -> dict[str, Any]:
    """Recompute the whole chain and verify that every row belongs to the cohort."""
    errors: list[str] = []
    expected_previous = cohort_hash
    seen_dates: set[str] = set()
    if not isinstance(records, (list, tuple)):
        records = []
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, Mapping):
            errors.append(f"record_not_mapping:{index}")
            continue
        record = dict(raw_record)
        trade_date = str(record.get("trade_date") or "")[:10]
        if trade_date in seen_dates:
            errors.append(f"duplicate_trade_date:{index}")
        seen_dates.add(trade_date)
        if record.get("cohort_hash") != cohort_hash:
            errors.append(f"cohort_hash_mismatch:{index}")
        if record.get("previous_hash") != expected_previous:
            errors.append(f"previous_hash_mismatch:{index}")
        errors.extend(
            f"{error}:{index}"
            for error in _validate_observation_record(
                record,
                baseline_trade_date=baseline_trade_date,
            )
        )
        payload = dict(record)
        claimed_hash = str(payload.pop("record_hash", ""))
        if claimed_hash != _hash(payload):
            errors.append(f"record_hash_mismatch:{index}")
        expected_previous = claimed_hash
    return {
        "valid": not errors,
        "errors": errors,
        "record_count": len(records),
        "chain_head": expected_previous if records else cohort_hash,
    }


def append_observation(
    existing: list[dict[str, Any]],
    observation: dict[str, Any],
    *,
    cohort_hash: str,
    baseline_trade_date: str,
    written_at: str | None = None,
) -> list[dict[str, Any]]:
    """Append one post-baseline trade date after validating the existing chain."""
    if not isinstance(existing, (list, tuple)):
        raise ValueError("existing observations must be a list")
    chain = validate_observation_chain(
        existing,
        cohort_hash=cohort_hash,
        baseline_trade_date=baseline_trade_date,
    )
    if not chain["valid"]:
        raise ValueError(f"invalid existing observation chain: {chain['errors']}")
    if not isinstance(observation, Mapping):
        raise ValueError("forward observation must be a mapping")
    if any(
        isinstance(item, Mapping) and item.get("trade_date") == observation.get("trade_date")
        for item in existing
    ):
        return existing
    record = dict(observation)
    record["cohort_hash"] = cohort_hash
    record["written_at"] = (
        written_at or datetime.now(MARKET_TZ).isoformat(timespec="seconds")
    )
    errors = _validate_observation_record(
        record,
        baseline_trade_date=baseline_trade_date,
    )
    if errors:
        raise ValueError(f"invalid forward observation: {errors}")
    record["previous_hash"] = (
        existing[-1]["record_hash"] if existing else cohort_hash
    )
    record["record_hash"] = _hash(record)
    return [*existing, record]


def validate_forward_ai_review(
    review: dict[str, Any],
    *,
    cohort_hash: str,
) -> dict[str, Any]:
    """Validate only permission to collect this cohort, never production/P3."""
    accepted = bool(
        isinstance(review, dict)
        and review.get("decision") == "accepted_for_forward_collection"
        and review.get("review_target_hash") == cohort_hash
        and review.get("reviewed_results_hash") == cohort_hash
        and str(review.get("reviewer_kind") or "").upper() == "AI_INTERNAL"
    )
    return {
        "accepted": accepted,
        "scope": "forward_collection_only",
        "production_certified": False,
        "p3_certified": False,
        "reason": "" if accepted else "forward_ai_review_not_accepted_for_target",
    }


def validate_cohort_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify that the reviewed cohort payload itself has not been changed."""
    if not isinstance(manifest, Mapping):
        return {"valid": False, "claimed_hash": "", "calculated_hash": ""}
    claimed_hash = str(manifest.get("cohort_hash") or "")
    payload = dict(manifest)
    payload.pop("cohort_hash", None)
    payload.pop("ai_review", None)
    calculated_hash = _hash(payload)
    return {
        "valid": bool(claimed_hash) and claimed_hash == calculated_hash,
        "claimed_hash": claimed_hash,
        "calculated_hash": calculated_hash,
    }


def evaluate_collection(
    records: list[dict[str, Any]],
    *,
    company_count: int,
    industry_count: int,
    history_years: float = 0.0,
    market_states: set[str] | None = None,
) -> dict[str, Any]:
    """Report collection progress without promoting it to production/P3."""
    valid_records = _records(records)
    days = len({
        str(record.get("trade_date", ""))
        for record in valid_records
        if record.get("trade_date")
    })
    forward_complete = days >= 20
    p3_scope_observed = (
        company_count >= 300
        and industry_count >= 20
        and history_years >= 5
        and {"up", "down", "sideways"}.issubset(market_states or set())
    )
    return {
        "collected_trade_days": days,
        "forward_20d_complete": forward_complete,
        "p3_scope_complete": p3_scope_observed,
        "certification_ready": False,
        "production_certified": False,
        "p3_certified": False,
    }


def record_forward_close(root: Path, observation: dict[str, Any]) -> dict[str, Any]:
    """Shared CLI/daemon append gate; failure never changes close task status."""
    try:
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError("forward cohort 尚未 start")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_audit = validate_cohort_manifest(manifest)
        if not manifest_audit["valid"]:
            raise ValueError("cohort_manifest_hash_mismatch")
        cohort_hash = str(manifest.get("cohort_hash") or "")
        manifest_data = dict(manifest) if isinstance(manifest, Mapping) else {}
        baseline = manifest_data.get("baseline")
        universe_policy = manifest_data.get("universe_policy")
        baseline_trade_date = str(
            (baseline.get("latest_trade_date") if isinstance(baseline, Mapping) else "")
            or (universe_policy.get("latest_trade_date") if isinstance(universe_policy, Mapping) else "")
            or ""
        )
        review = validate_forward_ai_review(
            manifest_data.get("ai_review") or {},
            cohort_hash=cohort_hash,
        )
        if not review["accepted"]:
            raise ValueError(review["reason"])
        observations_path = root / "observations.json"
        existing = (
            json.loads(observations_path.read_text(encoding="utf-8"))
            if observations_path.exists()
            else []
        )
        if not isinstance(existing, list):
            raise ValueError("observations payload must be a list")
        updated = append_observation(
            existing,
            observation,
            cohort_hash=cohort_hash,
            baseline_trade_date=baseline_trade_date,
        )
        temporary = observations_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(observations_path)
        return {
            "hook_ok": True,
            "close_success": None,
            "appended": len(updated) > len(existing),
            "record_hash": updated[-1]["record_hash"] if updated else "",
        }
    except Exception as exc:
        return {
            "hook_ok": False,
            "close_success": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def read_observation_refs(root: Path) -> dict[str, dict[str, Any]]:
    """Expose the same immutable observation references to learning."""
    path = root / "observations.json"
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    refs: dict[str, dict[str, Any]] = {}
    if not isinstance(records, list):
        return refs
    for record in records:
        if not isinstance(record, Mapping):
            continue
        record = dict(record)
        trade_date = str(record.get("trade_date", ""))
        benchmark = record.get("benchmark") or {}
        if not isinstance(benchmark, Mapping):
            benchmark = {}
        if not trade_date or not record.get("record_hash"):
            continue
        refs[trade_date] = {
            "available_at": str(record.get("available_at", "")),
            "record_hash": str(record["record_hash"]),
            "benchmark_fingerprint": str(benchmark.get("fingerprint", "")),
            "benchmark_return": benchmark.get("return"),
        }
    return refs
