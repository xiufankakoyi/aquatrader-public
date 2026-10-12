from __future__ import annotations

from core.research.pit_certification.forward_cohort import (
    append_observation,
    build_forward_cohort,
    evaluate_collection,
    read_observation_refs,
    record_forward_close,
    validate_cohort_manifest,
    validate_forward_ai_review,
    validate_observation_chain,
)


def test_forward_cohort_is_explicitly_shadow_and_hashes_inputs() -> None:
    cohort = build_forward_cohort(
        members=[{"symbol": "600519.SH", "industry": "白酒"}],
        input_records=[{"symbol": "600519.SH", "trade_date": "2026-07-22", "close": 1400.0}],
        source_hashes={"p1_evidence": "a" * 64},
        code_hash="b" * 64,
        started_at="2026-07-23T23:00:00+08:00",
    )

    assert cohort["mode"] == "forward_shadow"
    assert cohort["historical_certification_claimed"] is False
    assert len(cohort["provenance"]["input_fingerprint"]) == 64
    assert cohort["provenance"]["model"]["status"] == "forward_shadow"
    assert cohort["ai_review"]["status"] == "AI_INTERNAL_PENDING_REVIEW"
    assert cohort["ai_review"]["reviewer_id"] == ""
    assert cohort["baseline"]["latest_trade_date"] == "2026-07-22"
    assert cohort["baseline"]["input_records"] == [
        {"symbol": "600519.SH", "trade_date": "2026-07-22", "close": 1400.0}
    ]


def test_observation_chain_is_immutable_and_deduplicates_trade_date() -> None:
    cohort_hash = "c" * 64
    first = append_observation(
        [],
        {
            "trade_date": "2026-07-23",
            "available_at": "2026-07-23T15:10:00+08:00",
            "research_report_as_of": "2026-07-23T15:05:00+08:00",
            "outputs": [{"symbol": "600519.SH", "score": 0.5}],
        },
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
        written_at="2026-07-23T15:11:00+08:00",
    )
    duplicate = append_observation(
        first,
        {
            "trade_date": "2026-07-23",
            "available_at": "2026-07-23T15:12:00+08:00",
            "research_report_as_of": "2026-07-23T15:05:00+08:00",
            "outputs": [{"symbol": "600519.SH", "score": 0.7}],
        },
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
        written_at="2026-07-23T15:13:00+08:00",
    )
    second = append_observation(
        first,
        {
            "trade_date": "2026-07-24",
            "available_at": "2026-07-24T15:10:00+08:00",
            "research_report_as_of": "2026-07-24T15:05:00+08:00",
            "outputs": [{"symbol": "600519.SH", "score": 0.4}],
        },
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
        written_at="2026-07-24T15:11:00+08:00",
    )

    assert duplicate == first
    assert first[0]["previous_hash"] == cohort_hash
    assert second[1]["previous_hash"] == first[0]["record_hash"]
    assert second[1]["record_hash"] != first[0]["record_hash"]
    assert validate_observation_chain(
        second,
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
    )["valid"] is True


def test_forward_chain_rejects_baseline_report_mismatch_and_tampering() -> None:
    cohort_hash = "c" * 64
    base = {
        "trade_date": "2026-07-22",
        "available_at": "2026-07-22T15:10:00+08:00",
        "research_report_as_of": "2026-07-22T15:05:00+08:00",
    }
    for observation in (
        base,
        {**base, "trade_date": "2026-07-23"},
        {
            **base,
            "trade_date": "2026-07-23",
            "research_report_as_of": "2026-07-22T15:05:00+08:00",
        },
        {
            **base,
            "trade_date": "2026-07-23",
            "available_at": "2026-07-23T14:59:59+08:00",
            "research_report_as_of": "2026-07-23T15:05:00+08:00",
        },
    ):
        try:
            append_observation(
                [],
                observation,
                cohort_hash=cohort_hash,
                baseline_trade_date="2026-07-22",
                written_at="2026-07-23T15:20:00+08:00",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid forward observation must fail closed")

    valid = append_observation(
        [],
        {
            "trade_date": "2026-07-23",
            "available_at": "2026-07-23T15:10:00+08:00",
            "research_report_as_of": "2026-07-23T15:05:00+08:00",
            "value": 1,
        },
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
        written_at="2026-07-23T15:20:00+08:00",
    )
    valid[0]["value"] = 2
    assert validate_observation_chain(
        valid,
        cohort_hash=cohort_hash,
        baseline_trade_date="2026-07-22",
    )["valid"] is False


def test_forward_ai_review_is_scoped_to_target_cohort() -> None:
    cohort_hash = "c" * 64
    accepted = validate_forward_ai_review(
        {
            "decision": "accepted_for_forward_collection",
            "review_target_hash": cohort_hash,
            "reviewed_results_hash": cohort_hash,
            "reviewer_kind": "AI_INTERNAL",
        },
        cohort_hash=cohort_hash,
    )
    assert accepted["accepted"] is True
    assert accepted["scope"] == "forward_collection_only"
    assert accepted["production_certified"] is False
    assert accepted["p3_certified"] is False
    assert validate_forward_ai_review(
        {
            "decision": "accepted_for_forward_collection",
            "review_target_hash": "d" * 64,
            "reviewed_results_hash": cohort_hash,
            "reviewer_kind": "AI_INTERNAL",
        },
        cohort_hash=cohort_hash,
    )["accepted"] is False


def test_cohort_manifest_hash_rejects_member_tampering() -> None:
    cohort = build_forward_cohort(
        members=[{"symbol": "600519.SH"}],
        input_records=[{"trade_date": "2026-07-22"}],
        source_hashes={},
        code_hash="b" * 64,
        started_at="2026-07-23T09:00:00+08:00",
    )
    assert validate_cohort_manifest(cohort)["valid"] is True
    cohort["members"][0]["symbol"] = "000001.SZ"
    assert validate_cohort_manifest(cohort)["valid"] is False


def test_twenty_distinct_days_are_required_without_lowering_p3_scope() -> None:
    records = [
        {"trade_date": f"2026-08-{day:02d}", "record_hash": str(day)}
        for day in range(1, 21)
    ]

    status = evaluate_collection(
        records,
        company_count=20,
        industry_count=3,
        history_years=0,
        market_states={"sideways"},
    )

    assert status["forward_20d_complete"] is True
    assert status["p3_scope_complete"] is False
    assert status["certification_ready"] is False


def test_daemon_hook_failure_is_separate_from_close_success(tmp_path) -> None:
    failed = record_forward_close(
        tmp_path / "missing_cohort",
        {"trade_date": "2026-07-23", "benchmark": {"fingerprint": "a" * 64}},
    )

    assert failed["hook_ok"] is False
    assert failed["close_success"] is None


def test_learning_reads_same_available_at_reference(tmp_path) -> None:
    root = tmp_path / "cohort"
    root.mkdir()
    cohort = build_forward_cohort(
        members=[],
        input_records=[{"trade_date": "2026-07-22"}],
        source_hashes={},
        code_hash="b" * 64,
        started_at="2026-07-23T09:00:00+08:00",
    )
    cohort["ai_review"].update({
        "status": "AI_INTERNAL_ACCEPTED_FOR_FORWARD_COLLECTION",
        "reviewer_id": "test-reviewer",
        "reviewer_kind": "AI_INTERNAL",
        "review_model": "test-model",
        "reviewed_results_hash": cohort["cohort_hash"],
        "decision": "accepted_for_forward_collection",
        "signed_at": "2026-07-23T09:01:00+08:00",
    })
    (root / "manifest.json").write_text(
        __import__("json").dumps(cohort), encoding="utf-8"
    )
    record_forward_close(
        root,
        {
            "trade_date": "2026-07-23",
            "available_at": "2026-07-23T15:10:00+08:00",
            "research_report_as_of": "2026-07-23T15:05:00+08:00",
            "benchmark": {"fingerprint": "a" * 64, "return": -0.01},
        },
    )

    refs = read_observation_refs(root)

    assert refs["2026-07-23"]["available_at"] == "2026-07-23T15:10:00+08:00"
    assert refs["2026-07-23"]["benchmark_fingerprint"] == "a" * 64
    assert refs["2026-07-23"]["benchmark_return"] == -0.01
    assert len(refs["2026-07-23"]["record_hash"]) == 64
