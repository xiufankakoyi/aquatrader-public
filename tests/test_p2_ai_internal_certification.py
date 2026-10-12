from __future__ import annotations

from core.research.p2_ai_internal_certification import certify_ai_internal_replay


def _p1_audit(symbols: list[str]) -> dict:
    return {
        "companies": [
            {
                "symbol": symbol,
                "annual_report": {"published_at": "2026-04-30"},
            }
            for symbol in symbols
        ],
        "decision": {
            "status": "P1_AI_CERTIFIED",
            "overall_passed": True,
            "review_mode": "ai_only_internal",
            "human_independent_review": False,
            "identity_independent": False,
            "companies": [
                {"symbol": symbol, "passed": True, "failures": []}
                for symbol in symbols
            ],
        }
    }


def test_twenty_day_t_minus_one_replay_certifies_without_secret_key() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(10)]
    dates = [f"2026-06-{day:02d}" for day in range(1, 22)]
    rows = {
        symbol: [{"trade_date": date, "close": float(index + 1)}
                 for index, date in enumerate(dates)]
        for symbol in symbols
    }

    report = certify_ai_internal_replay(
        p1_audit=_p1_audit(symbols),
        trading_dates=dates,
        market_rows_by_symbol=rows,
    )

    assert report["status"] == "P2_AI_INTERNAL_CERTIFIED"
    assert report["overall_passed"] is True
    assert report["review_mode"] == "ai_only_internal"
    assert report["external_attestation_required"] is False
    assert report["observation_count"] == 20
    assert report["company_count"] == 10
    assert report["leakage_count"] == 0
    assert report["observation_runs"][0]["observation_date"] == dates[1]
    assert report["observation_runs"][0]["market_cutoff"] == dates[0]
    assert len(report["evidence_sha256"]) == 64


def test_replay_ignores_rows_after_each_observation_cutoff() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(10)]
    dates = [f"2026-06-{day:02d}" for day in range(1, 22)]
    rows = {
        symbol: [{"trade_date": date, "close": 1.0} for date in dates]
        for symbol in symbols
    }
    rows[symbols[0]].append({"trade_date": "2026-07-01", "close": 2.0})

    report = certify_ai_internal_replay(
        p1_audit=_p1_audit(symbols),
        trading_dates=dates,
        market_rows_by_symbol=rows,
    )

    assert report["overall_passed"] is True
    assert report["leakage_count"] == 0
    assert all(
        run["max_market_date_used"] <= run["market_cutoff"]
        for run in report["observation_runs"]
    )


def test_replay_fails_closed_when_a_company_has_no_cutoff_data() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(10)]
    dates = [f"2026-06-{day:02d}" for day in range(1, 22)]
    rows = {
        symbol: [{"trade_date": date, "close": 1.0} for date in dates]
        for symbol in symbols
    }
    rows[symbols[0]] = [{"trade_date": "2026-07-01", "close": 2.0}]

    report = certify_ai_internal_replay(
        p1_audit=_p1_audit(symbols),
        trading_dates=dates,
        market_rows_by_symbol=rows,
    )

    assert report["overall_passed"] is False
    assert report["status"] == "P2_AI_INTERNAL_BLOCKED"
    assert report["missing_market_inputs"] > 0


def test_institutional_scale_and_vp_are_disclosed_but_not_internal_blockers() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(10)]
    dates = [f"2026-06-{day:02d}" for day in range(1, 22)]
    rows = {
        symbol: [{"trade_date": date, "close": 1.0} for date in dates]
        for symbol in symbols
    }

    report = certify_ai_internal_replay(
        p1_audit=_p1_audit(symbols),
        trading_dates=dates,
        market_rows_by_symbol=rows,
    )

    assert report["institutional_certification"] is False
    assert report["institutional_deferred_gates"] == [
        "100_company_shadow",
        "30_complete_p1_models",
        "10_independently_reviewed_variant_perceptions",
        "trusted_external_attestation",
    ]


def test_p1_evidence_published_after_replay_window_is_blocked() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(10)]
    dates = [f"2026-06-{day:02d}" for day in range(1, 22)]
    audit = _p1_audit(symbols)
    audit["companies"][0]["annual_report"]["published_at"] = "2026-07-01"
    rows = {
        symbol: [{"trade_date": date, "close": 1.0} for date in dates]
        for symbol in symbols
    }

    report = certify_ai_internal_replay(
        p1_audit=audit,
        trading_dates=dates,
        market_rows_by_symbol=rows,
    )

    assert report["overall_passed"] is False
    assert report["future_p1_evidence_count"] == 1


def test_malformed_audit_and_market_rows_fail_closed_without_crashing() -> None:
    report = certify_ai_internal_replay(
        p1_audit={"decision": {"companies": [None]}},
        trading_dates=None,  # type: ignore[arg-type]
        market_rows_by_symbol={"A": [None, {"trade_date": "2026-01-01"}]},
    )

    assert report["overall_passed"] is False
    assert report["company_count"] == 0
    assert report["observation_count"] == 0
