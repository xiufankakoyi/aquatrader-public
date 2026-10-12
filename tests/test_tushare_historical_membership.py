"""Tushare 历史上市状态与 P2.10 PIT 股票池 smoke test。"""
from __future__ import annotations

import pandas as pd

from infrastructure.data_providers.market import tushare_provider
from infrastructure.data_providers.market.tushare_provider import TushareProvider
from sandbox.p2_10_build_universe import (
    build_universe,
    filter_historical_membership,
    map_business_model,
    validate_pit_profile,
)
from sandbox.p2_10_shadow_acceptance import (
    compute_evidence_fingerprint,
    compute_universe_fingerprint,
)


class _FakePro:
    def stock_basic(self, *, list_status: str, **_kwargs):
        rows = {
            "L": [{
                "ts_code": "000001.SZ", "symbol": "000001", "name": "当前上市",
                "area": "深圳", "industry": "银行", "list_date": "19910403",
                "delist_date": "", "list_status": "L", "business_scope": "",
            }],
            "D": [{
                "ts_code": "000002.SZ", "symbol": "000002", "name": "后来退市",
                "area": "深圳", "industry": "地产", "list_date": "19910129",
                "delist_date": "20250102", "list_status": "D", "business_scope": "",
            }],
            "P": [{
                "ts_code": "000003.SZ", "symbol": "000003", "name": "暂停上市",
                "area": "深圳", "industry": "制造", "list_date": "19910703",
                "delist_date": "", "list_status": "P", "business_scope": "",
            }],
        }
        return pd.DataFrame(rows[list_status])


def test_provider_loads_listed_delisted_and_paused_populations(monkeypatch):
    monkeypatch.setattr(tushare_provider, "_ts_pro", _FakePro())
    provider = TushareProvider()
    monkeypatch.setattr(provider, "is_available", lambda: True)
    frame = provider.get_stock_membership_history()
    assert set(frame["list_status"]) == {"L", "D", "P"}
    assert set(frame["symbol"]) == {"000001", "000002", "000003"}


def test_historical_filter_includes_company_delisted_after_as_of():
    frame = pd.DataFrame([
        {"symbol": "000001", "list_date": "19910403", "delist_date": "", "list_status": "L"},
        {"symbol": "000002", "list_date": "19910129", "delist_date": "20250102", "list_status": "D"},
        {"symbol": "000004", "list_date": "19920101", "delist_date": "20200101", "list_status": "D"},
        {"symbol": "000005", "list_date": "20250101", "delist_date": "", "list_status": "L"},
    ])
    result = filter_historical_membership(frame, "2024-12-31")
    assert set(result["symbol"]) == {"000001", "000002"}


def test_builder_does_not_infer_full_p1_model_from_load_counters():
    companies = [{
        "symbol": "000001.SZ", "name": "测试", "industry": "银行",
        "list_date": "19910403", "delist_date": "", "source_list_status": "L",
    }]
    load_results = [{
        "statements_loaded": 8, "valuation_loaded": 3, "consensus_loaded": 0,
        "data_source": "tushare",
    }]
    universe = build_universe(companies, load_results, as_of="2024-12-31")
    assert "p1_complete" not in universe["companies"][0]
    assert "p1_model_evidence" not in universe["companies"][0]
    assert universe["trading_days_covered"] == 0
    assert universe["experiment"]["data_fingerprint"] == compute_universe_fingerprint(universe)


def test_unknown_industry_is_not_guessed_as_growth():
    assert map_business_model("") == "unclassified"
    assert map_business_model("无法识别行业") == "unclassified"


def test_pit_profile_requires_valid_time_membership_and_local_evidence():
    profile = {
        "as_of": "2024-12-31",
        "name": "时点名称",
        "industry": "银行",
        "business_model": "financial",
        "business_model_evidence_refs": ["local://profile/business-model"],
        "risk_status": "normal",
        "risk_status_evidence_refs": ["local://profile/risk-status"],
        "source_refs": ["local://profile/company"],
        "available_at": "2024-12-30",
        "membership_intervals": [
            {"valid_from": "1991-04-03", "valid_to": "", "status": "listed"}
        ],
    }
    profile["profile_hash"] = compute_evidence_fingerprint(profile, "profile_hash")
    assert validate_pit_profile(profile, "2024-12-31") is True
    profile["available_at"] = "2025-01-02"
    assert validate_pit_profile(profile, "2024-12-31") is False
