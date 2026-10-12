from __future__ import annotations

import json
import hashlib
from pathlib import Path

from core.research.p3_gate import P3GateStatus, check_p2_prerequisite


def _seal(payload: dict) -> dict:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def test_explicit_ai_internal_p2_certification_unlocks_p3(tmp_path: Path) -> None:
    institutional = tmp_path / "institutional.json"
    institutional.write_text(
        json.dumps({"overall_passed": False, "artifact_status": "superseded_tombstone"}),
        encoding="utf-8",
    )
    internal = tmp_path / "internal.json"
    internal.write_text(json.dumps(_seal({
        "status": "P2_AI_INTERNAL_CERTIFIED",
        "overall_passed": True,
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "external_attestation_required": False,
        "institutional_certification": False,
        "company_count": 10,
        "observation_count": 20,
        "leakage_count": 0,
        "missing_market_inputs": 0,
        "future_p1_evidence_count": 0,
    })), encoding="utf-8")

    result = check_p2_prerequisite(
        institutional,
        ai_internal_report_path=internal,
    )

    assert result.status is P3GateStatus.OK
    assert result.certification_mode == "ai_only_internal"
    assert result.p2_overall_passed is True


def test_tampered_ai_internal_fingerprint_does_not_unlock_p3(tmp_path: Path) -> None:
    institutional = tmp_path / "institutional.json"
    institutional.write_text(json.dumps({"overall_passed": False}), encoding="utf-8")
    payload = _seal({
        "status": "P2_AI_INTERNAL_CERTIFIED",
        "overall_passed": True,
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "external_attestation_required": False,
        "institutional_certification": False,
        "company_count": 10,
        "observation_count": 20,
        "leakage_count": 0,
        "missing_market_inputs": 0,
        "future_p1_evidence_count": 0,
    })
    payload["company_count"] = 11
    internal = tmp_path / "internal.json"
    internal.write_text(json.dumps(payload), encoding="utf-8")

    result = check_p2_prerequisite(
        institutional,
        ai_internal_report_path=internal,
    )

    assert result.status is P3GateStatus.PENDING_P2


def test_incomplete_ai_internal_report_does_not_unlock_p3(tmp_path: Path) -> None:
    institutional = tmp_path / "institutional.json"
    institutional.write_text(json.dumps({"overall_passed": False}), encoding="utf-8")
    internal = tmp_path / "internal.json"
    internal.write_text(json.dumps({
        "status": "P2_AI_INTERNAL_CERTIFIED",
        "overall_passed": True,
        "review_mode": "ai_only_internal",
        "company_count": 10,
        "observation_count": 19,
        "leakage_count": 0,
    }), encoding="utf-8")

    result = check_p2_prerequisite(
        institutional,
        ai_internal_report_path=internal,
    )

    assert result.status is P3GateStatus.PENDING_P2
    assert result.is_blocked


def test_non_numeric_ai_internal_counts_do_not_crash_or_unlock(tmp_path: Path) -> None:
    institutional = tmp_path / "institutional.json"
    institutional.write_text(json.dumps({"overall_passed": False}), encoding="utf-8")
    internal = tmp_path / "internal.json"
    internal.write_text(json.dumps(_seal({
        "status": "P2_AI_INTERNAL_CERTIFIED",
        "overall_passed": True,
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "external_attestation_required": False,
        "institutional_certification": False,
        "company_count": "many",
        "observation_count": 20,
        "leakage_count": 0,
        "missing_market_inputs": 0,
        "future_p1_evidence_count": 0,
    })), encoding="utf-8")

    result = check_p2_prerequisite(institutional, ai_internal_report_path=internal)

    assert result.status is P3GateStatus.PENDING_P2
