from __future__ import annotations

from core.research.pit_certification.contracts import (
    CertificationStatus,
    MarketState,
    PITCertificationResult,
    compute_results_hash,
)
from core.research.pit_certification.provenance import (
    audit_ai_review,
    audit_production_results,
)
from core.research.pit_certification.report_generator import generate_certification_report
from core.research.pit_certification.production_preflight import evaluate_preflight


def _result(
    symbol: str,
    *,
    model_version: str = "revenue_model_v1",
    policy_version: str = "research_policy_v1",
    prediction: float = 100.0,
    actual: float = 105.0,
) -> PITCertificationResult:
    return PITCertificationResult(
        symbol=symbol,
        company_name=symbol,
        industry="消费",
        pit_time="2024-06-30",
        status=CertificationStatus.SUCCESS,
        prediction_value=prediction,
        prediction_interval_low=prediction * 0.9,
        prediction_interval_high=prediction * 1.1,
        actual_value=actual,
        model_version=model_version,
        policy_version=policy_version,
        market_state=MarketState.SIDEWAYS,
    )


def _provenance(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "pit_time": "2024-06-30",
        "data_source_refs": ["lancedb:financial_indicator"],
        "input_fingerprint": "a" * 64,
        "code_fingerprint": "b" * 64,
        "model_effective_from": "2024-01-01",
        "policy_effective_from": "2024-01-01",
    }


def test_mock_versions_and_fixed_demo_values_cannot_be_certified() -> None:
    results = [
        _result(f"{index:06d}.SZ", model_version="mock_model_v1", policy_version="mock_policy_v1")
        for index in range(3)
    ]

    audit = audit_production_results(
        results,
        [_provenance(result.symbol) for result in results],
    )

    assert audit["production_provenance_valid"] is False
    assert audit["mock_or_demo_results"] == 3
    assert audit["constant_demo_pattern"] is True


def test_missing_fingerprints_and_sources_cannot_be_certified() -> None:
    result = _result("600519.SH")

    audit = audit_production_results(
        [result],
        [{"symbol": result.symbol, "pit_time": result.pit_time}],
    )

    assert audit["production_provenance_valid"] is False
    assert audit["missing_provenance_results"] == 1


def test_model_or_policy_created_after_pit_time_cannot_be_certified() -> None:
    result = _result("600519.SH")
    provenance = _provenance(result.symbol)
    provenance["model_effective_from"] = "2024-07-01"

    audit = audit_production_results([result], [provenance])

    assert audit["production_provenance_valid"] is False
    assert audit["missing_provenance_results"] == 1


def test_distinct_real_results_with_complete_provenance_pass() -> None:
    results = [
        _result("600519.SH", prediction=100.0, actual=103.0),
        _result("000858.SZ", prediction=91.0, actual=87.0),
        _result("300750.SZ", prediction=126.0, actual=130.0),
    ]

    audit = audit_production_results(
        results,
        [_provenance(result.symbol) for result in results],
    )

    assert audit["production_provenance_valid"] is True
    assert audit["mock_or_demo_results"] == 0
    assert audit["missing_provenance_results"] == 0
    assert audit["constant_demo_pattern"] is False


def test_ai_review_must_be_independent_and_cover_exact_results() -> None:
    results = [_result("600519.SH", prediction=100.0, actual=103.0)]
    results_hash = compute_results_hash(results)

    accepted = audit_ai_review(
        {
            "reviewer_id": "codex-reviewer",
            "reviewer_kind": "ai",
            "model_author": "aquatrade-research-os",
            "review_model": "gpt-production-reviewer",
            "review_prompt_version": "p3-independent-review-v1",
            "reviewed_results_hash": results_hash,
            "signed_at": "2026-07-23T12:00:00+00:00",
            "decision": "accepted",
        },
        results,
    )
    rejected = audit_ai_review(
        {
            "reviewer_id": "aquatrade-research-os",
            "reviewer_kind": "ai",
            "model_author": "aquatrade-research-os",
            "review_model": "gpt-production-reviewer",
            "review_prompt_version": "p3-independent-review-v1",
            "reviewed_results_hash": results_hash,
            "signed_at": "2026-07-23T12:00:00+00:00",
            "decision": "accepted",
        },
        results,
    )

    assert accepted["ai_review_valid"] is True
    assert rejected["ai_review_valid"] is False
    assert "reviewer_not_independent" in rejected["reasons"]


def test_report_never_marks_mock_checkpoint_production_ready() -> None:
    results = [
        _result(f"{index:06d}.SZ", model_version="mock_model_v1", policy_version="mock_policy_v1")
        for index in range(3)
    ]

    report = generate_certification_report(
        results,
        production_provenance=[_provenance(result.symbol) for result in results],
    )

    assert report.hard_gates["production_provenance_valid"] is False
    assert report.hard_gates["ai_review_valid"] is False
    assert report.hard_gates["certification_ready"] is False


def test_preflight_blocks_without_active_model_and_policy() -> None:
    result = _result("600519.SH", prediction=100.0, actual=103.0)

    preflight = evaluate_preflight(
        [result],
        [_provenance(result.symbol)],
        {},
        active_model_count=0,
        active_policy_count=0,
    )

    assert preflight["status"] == "blocked"
    assert "no_active_production_model" in preflight["blockers"]
    assert "no_active_research_policy" in preflight["blockers"]
