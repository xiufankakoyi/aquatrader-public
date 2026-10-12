from __future__ import annotations

import json
from datetime import datetime

from core.us_options import scanner
from core.us_options.config import ScanConfig, load_scan_config
from core.us_options.greeks import black_scholes_greeks
from core.us_options.provider import parse_occ_option_symbol
from core.us_options.scanner import (
    enrich_contract,
    evaluate_contracts,
    format_push_line,
    scan_us_options,
    write_scan_outputs,
)


def sample_contract(**overrides):
    row = {
        "symbol": "SOXS",
        "underlying_price": 4.0,
        "option_type": "call",
        "expiry": "2026-07-17",
        "strike": 4.0,
        "bid": 0.18,
        "ask": 0.21,
        "DTE": 17,
        "implied_volatility": 0.9,
        "delta": 0.22,
        "gamma": 0.08,
        "theta": -0.018,
        "volume": 2300,
        "openInterest": 18000,
        "data_source": "unit",
    }
    row.update(overrides)
    return row


def test_black_scholes_greeks_calculates_without_error():
    greeks = black_scholes_greeks(underlying_price=100, strike=105, dte=30, option_type="call", implied_volatility=0.5)

    assert greeks is not None
    assert 0 < greeks.delta < 1
    assert greeks.gamma > 0
    assert greeks.theta < 0


def test_cost_and_spread_calculation():
    row = enrich_contract(sample_contract(), ScanConfig(universe=["SOXS"], push_feishu=False))

    assert row["contract_cost"] == 21
    assert row["spread_pct"] == 0.153846


def test_bid_zero_contract_is_dead():
    row = enrich_contract(sample_contract(bid=0, ask=0.20), ScanConfig(universe=["SOXS"], push_feishu=False))

    assert row["alert_level"] == "DEAD"
    assert row["liquidity_grade"] == "DEAD"
    assert "NO_BID" in row["risk_flags"]


def test_ask_above_050_is_rejected_or_too_expensive():
    candidates, rejected = evaluate_contracts(
        [sample_contract(bid=0.48, ask=0.55)],
        ScanConfig(universe=["SOXS"], budget=50, push_feishu=False),
    )

    assert candidates == []
    assert rejected
    assert "TOO_EXPENSIVE" in rejected[0]["risk_flags"]


def test_high_volume_high_oi_tight_spread_enters_open():
    candidates, _ = evaluate_contracts(
        [sample_contract(bid=0.18, ask=0.21, volume=2300, openInterest=18000)],
        ScanConfig(universe=["SOXS"], push_feishu=False),
    )

    assert candidates
    assert candidates[0]["alert_level"] == "OPEN"
    assert candidates[0]["liquidity_grade"] == "LIQUID"


def test_bid_exists_but_wide_spread_enters_watch():
    candidates, rejected = evaluate_contracts(
        [sample_contract(bid=0.10, ask=0.30, volume=200, openInterest=1000)],
        ScanConfig(universe=["SOXS"], push_feishu=False),
    )

    assert rejected == []
    assert candidates[0]["alert_level"] == "WATCH"
    assert "WIDE_SPREAD" in candidates[0]["risk_flags"]


def test_short_push_text_excludes_greeks_dte_and_underlying_trigger_fields():
    row = enrich_contract(sample_contract(), ScanConfig(universe=["SOXS"], push_feishu=False))
    text = format_push_line(row)

    forbidden = ["DTE", "theta", "gamma", "delta", "IV", "涨跌", "突破", "放量", "趋势"]
    assert not any(token in text for token in forbidden)
    assert text.startswith("[OPEN] SOXS 4C 07/17")
    assert "Vol2.3k" in text
    assert "OI18k" in text


def test_candidates_jsonl_keeps_full_detail_fields(tmp_path):
    config = ScanConfig(universe=["SOXS"], output_root=str(tmp_path), push_feishu=False)
    candidates, rejected = evaluate_contracts([sample_contract()], config)
    write_scan_outputs(
        config=config,
        raw_contracts=[sample_contract()],
        candidates=candidates,
        rejected=rejected,
        failures=[],
        successful_symbols=1,
        run_time=datetime(2026, 7, 1, 9, 30, 0),
    )

    row = json.loads((tmp_path / "20260701_093000" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["DTE"] == 17
    assert row["theta"] == -0.018
    assert row["gamma"] == 0.08
    assert row["implied_volatility"] == 0.9


def test_missing_data_does_not_crash_and_is_rejected():
    candidates, rejected = evaluate_contracts(
        [sample_contract(ask=None, volume=None)],
        ScanConfig(universe=["SOXS"], push_feishu=False),
    )

    assert candidates == []
    assert rejected
    assert "DATA_MISSING" in rejected[0]["risk_flags"]


def test_candidates_output_contains_only_long_buy_strategies():
    candidates, _ = evaluate_contracts(
        [sample_contract(option_type="call"), sample_contract(option_type="put", delta=-0.22)],
        ScanConfig(universe=["SOXS"], top_n=10, push_feishu=False),
    )

    assert candidates
    assert {row["side"] for row in candidates} == {"buy_to_open"}
    assert {row["strategy"] for row in candidates} <= {"long_call", "long_put"}


def test_scan_report_and_feishu_preview_can_be_generated(tmp_path):
    config = ScanConfig(universe=["SOXS"], output_root=str(tmp_path), top_n=10, push_feishu=False)
    candidates, rejected = evaluate_contracts([sample_contract()], config)
    result = write_scan_outputs(
        config=config,
        raw_contracts=[sample_contract()],
        candidates=candidates,
        rejected=rejected,
        failures=[],
        successful_symbols=1,
        run_time=datetime(2026, 7, 1, 9, 30, 0),
    )

    run_dir = tmp_path / "20260701_093000"
    assert result["candidate_count"] == 1
    assert (run_dir / "scan_report.md").exists()
    assert (run_dir / "feishu_message.txt").exists()


def test_auto_data_source_falls_back_when_yfinance_is_unavailable(monkeypatch, tmp_path):
    class BrokenProvider:
        def __init__(self):
            raise RuntimeError("yfinance is not installed")

    class FakeCboeProvider:
        data_source = "cboe_delayed"

        def fetch_symbol(self, symbol, config):
            return [sample_contract(symbol=symbol, data_source=self.data_source)], None

    monkeypatch.setattr(scanner, "YFinanceOptionProvider", BrokenProvider)
    monkeypatch.setattr(scanner, "CboeDelayedOptionProvider", FakeCboeProvider)

    result = scan_us_options(ScanConfig(universe=["SOXS", "SQQQ"], output_root=str(tmp_path), push_feishu=False))

    assert result["raw_count"] == 2
    assert result["failed_symbols"] == 0


def test_parse_occ_option_symbol():
    parsed = parse_occ_option_symbol("SOXS260717C00004000")

    assert parsed is not None
    assert parsed["expiry"] == "2026-07-17"
    assert parsed["option_type"] == "call"
    assert parsed["strike"] == 4


def test_universe_config_dedupes_user_duplicates():
    config = load_scan_config(None, universe=["soxs", "sqqq", "mull", "nvdl", "mull", "nvdl", "fxi"])

    assert config.universe == ["SOXS", "SQQQ", "MULL", "NVDL", "FXI"]
