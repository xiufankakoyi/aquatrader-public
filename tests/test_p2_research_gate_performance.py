from __future__ import annotations

from core.research.p2_research_gate_performance import evaluate_research_gate


def _bars(multiplier: float) -> list[dict]:
    rows = []
    for day in range(1, 31):
        rows.append({
            "trade_date": f"2026-06-{day:02d}",
            "open": multiplier * day,
            "close": multiplier * day + 1.0,
        })
    return rows


def test_signal_uses_t_minus_one_and_execution_uses_close_to_next_open() -> None:
    bars = {"A": _bars(1.0), "B": _bars(2.0)}
    report = evaluate_research_gate(
        bars_by_symbol=bars,
        all_symbols=["A", "B"],
        gated_symbols=["A"],
        top_k=1,
        evaluation_days=20,
        momentum_lookback=5,
    )

    first = report["daily_results"][0]
    assert first["signal_date"] == "2026-06-09"
    assert first["entry_date"] == "2026-06-10"
    assert first["exit_date"] == "2026-06-11"
    assert first["research_gated"]["symbols"] == ["A"]
    assert first["research_gated"]["return"] == (11.0 - 11.0) / 11.0
    assert first["leakage_detected"] is False


def test_active_baseline_uses_same_rule_without_research_gate() -> None:
    bars = {"A": _bars(1.0), "B": _bars(2.0)}
    report = evaluate_research_gate(
        bars_by_symbol=bars,
        all_symbols=["A", "B"],
        gated_symbols=["A"],
        top_k=1,
        evaluation_days=20,
        momentum_lookback=5,
    )

    first = report["daily_results"][0]
    assert first["active_baseline"]["symbols"] == ["B"]
    assert first["passive_equal_weight"]["symbols"] == ["A", "B"]
    assert report["method"]["costs_and_slippage"] == 0.0


def test_missing_next_open_fails_closed_instead_of_using_future_replacement() -> None:
    bars = {"A": _bars(1.0)}
    bars["A"] = [
        row for row in bars["A"] if row["trade_date"] != "2026-06-11"
    ]
    report = evaluate_research_gate(
        bars_by_symbol=bars,
        all_symbols=["A"],
        gated_symbols=["A"],
        trading_dates=[f"2026-06-{day:02d}" for day in range(1, 31)],
        top_k=1,
        evaluation_days=20,
        momentum_lookback=5,
    )

    assert report["overall_valid"] is False
    assert report["unresolved_execution_count"] > 0


def test_malformed_market_rows_are_skipped_and_do_not_crash() -> None:
    bars = {"A": [None, {"trade_date": "2026-06-01", "close": "bad"}]}

    report = evaluate_research_gate(
        bars_by_symbol=bars,
        all_symbols=["A"],
        gated_symbols=["A"],
        trading_dates=[f"2026-06-{day:02d}" for day in range(1, 31)],
        top_k=1,
        evaluation_days=20,
        momentum_lookback=5,
    )

    assert report["overall_valid"] is False
    assert report["daily_results"]
