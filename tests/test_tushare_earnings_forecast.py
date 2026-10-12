from __future__ import annotations

import json
import pandas as pd

import infrastructure.data_providers.market.tushare_provider as provider_module
from core.research.value_chain.source_gateway import ResearchSourceGateway
from infrastructure.data_providers.market.tushare_provider import TushareProvider


def test_tushare_earnings_forecast_normalizes_direction_and_range(monkeypatch) -> None:
    class FakePro:
        @staticmethod
        def forecast(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "603127.SH", "ann_date": "20260715", "end_date": "20260630",
                "type": "预增", "p_change_min": 884.9, "p_change_max": 1377.4,
                "net_profit_min": 60013.0, "net_profit_max": 90019.0,
                "summary": "预计净利润增长", "change_reason": "生物资产公允价值变动",
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())

    result = TushareProvider().get_earnings_forecasts(
        "2026-07-01", "2026-07-17", ["603127"]
    )

    assert result.iloc[0]["forecast_type"] == "预增"
    assert result.iloc[0]["change_min_pct"] == 884.9
    assert result.iloc[0]["change_max_pct"] == 1377.4
    assert result.iloc[0]["provider"] == "tushare"


def test_tushare_earnings_forecast_can_scan_one_announcement_day(monkeypatch) -> None:
    calls = []

    class FakePro:
        @staticmethod
        def forecast(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame([{
                "ts_code": "600664.SH", "ann_date": "20260710", "end_date": "20260630",
                "type": "预增", "p_change_min": 46.4, "p_change_max": 68.36,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())

    result = TushareProvider().get_earnings_forecasts(
        "2026-07-10", "2026-07-10", None
    )

    assert calls == [{"ann_date": "20260710"}]
    assert result.iloc[0]["symbol"] == "600664.SH"


def test_tushare_consensus_estimates_normalize_wan_yuan_to_yuan(monkeypatch, tmp_path) -> None:
    class FakePro:
        @staticmethod
        def report_rc(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "name": "测试公司", "report_date": "20260707",
                "report_title": "盈利预测更新", "report_type": "点评", "org_name": "机构甲",
                "author_name": "分析师甲", "quarter": "2027Q4", "op_rt": 120000.0,
                "op_pr": 30000.0, "tp": 29000.0, "np": 25000.0, "eps": 2.5,
                "pe": 20.0, "roe": 15.0,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())
    monkeypatch.setenv("TUSHARE_CONSENSUS_CACHE_DIR", str(tmp_path / "consensus"))

    result = TushareProvider().get_consensus_estimates(
        "2026-01-01", "2026-07-18", ["300308.SZ"]
    )

    assert result.iloc[0]["revenue_forecast_yuan"] == 1_200_000_000.0
    assert result.iloc[0]["net_profit_forecast_yuan"] == 250_000_000.0
    assert result.iloc[0]["forecast_period"] == "2027Q4"


def test_tushare_consensus_uses_persistent_cache_after_provider_failure(monkeypatch, tmp_path) -> None:
    calls = {"count": 0}

    class FakePro:
        @staticmethod
        def report_rc(**_kwargs):
            calls["count"] += 1
            if calls["count"] > 1:
                raise RuntimeError("rate limited")
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "name": "测试公司", "report_date": "20260707",
                "quarter": "2027Q4", "org_name": "机构甲", "np": 25000.0,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())
    monkeypatch.setenv("TUSHARE_CONSENSUS_CACHE_DIR", str(tmp_path / "consensus"))

    first = TushareProvider().get_consensus_estimates(
        "2026-01-01", "2026-07-18", ["300308.SZ"]
    )
    second = TushareProvider().get_consensus_estimates(
        "2026-01-01", "2026-07-18", ["300308.SZ"]
    )

    assert len(first) == 1
    assert len(second) == 1
    assert second.iloc[0]["net_profit_forecast_yuan"] == 250_000_000.0


def test_tushare_valuation_history_normalizes_market_value_units(monkeypatch) -> None:
    class FakePro:
        @staticmethod
        def daily_basic(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "trade_date": "20260717", "close": 88.0,
                "turnover_rate": 2.0, "pe": 30.0, "pe_ttm": 32.0, "pb": 8.0,
                "ps": 10.0, "ps_ttm": 9.0, "dv_ratio": 0.3, "dv_ttm": 0.4,
                "total_mv": 10_000.0, "circ_mv": 9_000.0,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())

    result = TushareProvider().get_valuation_history(
        "2026-07-01", "2026-07-18", ["300308.SZ"]
    )

    assert result.iloc[0]["trade_date"] == "2026-07-17"
    assert result.iloc[0]["total_market_cap"] == 100_000_000.0
    assert result.iloc[0]["float_market_cap"] == 90_000_000.0
    assert result.iloc[0]["pe_ttm"] == 32.0


def test_tushare_finance_snapshot_includes_balance_sheet(monkeypatch) -> None:
    class FakePro:
        @staticmethod
        def income(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "ann_date": "20260420",
                "end_date": "20251231", "revenue": 1_000.0,
                "n_income_attr_p": 100.0,
            }])

        @staticmethod
        def cashflow(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "ann_date": "20260420",
                "end_date": "20251231", "n_cashflow_act": 120.0,
            }])

        @staticmethod
        def fina_indicator(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def balancesheet(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "300308.SZ", "ann_date": "20260420",
                "end_date": "20251231", "total_assets": 5_000.0,
                "total_liab": 2_000.0, "total_hldr_eqy_exc_min_int": 3_000.0,
                "money_cap": 800.0,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())

    result = TushareProvider().get_finance_snapshot(
        ["300308.SZ"], as_of="2026-07-18", periods=1,
    )

    row = result.iloc[0]
    assert row["total_assets"] == 5_000.0
    assert row["total_liabilities"] == 2_000.0
    assert row["total_equity"] == 3_000.0
    assert row["cash_and_equivalents"] == 800.0


def test_tushare_finance_snapshot_uses_equity_including_minority_and_cash_reconciliation(monkeypatch) -> None:
    class FakePro:
        @staticmethod
        def income(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "600438.SH", "ann_date": "20241025",
                "end_date": "20240930", "revenue": 1_000.0,
                "n_income_attr_p": 100.0,
            }])

        @staticmethod
        def cashflow(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "600438.SH", "ann_date": "20241025",
                "end_date": "20240930", "n_cashflow_act": 120.0,
                "n_incr_cash_cash_equ": 20.0, "c_cash_equ_beg_period": 780.0,
                "c_cash_equ_end_period": 800.0,
            }])

        @staticmethod
        def fina_indicator(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def balancesheet(**_kwargs):
            return pd.DataFrame([{
                "ts_code": "600438.SH", "ann_date": "20241025",
                "end_date": "20240930", "total_assets": 5_100.0,
                "total_liab": 2_000.0, "total_hldr_eqy_exc_min_int": 3_000.0,
                "minority_int": 100.0, "total_hldr_eqy_inc_min_int": 3_100.0,
                "total_liab_hldr_eqy": 5_100.0, "money_cap": 800.0,
                "st_borr": 200.0, "lt_borr": 300.0, "bond_payable": 50.0,
            }])

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())

    row = TushareProvider().get_finance_snapshot(
        ["600438.SH"], as_of="2024-12-31", periods=1,
    ).iloc[0]

    assert row["total_equity"] == 3_100.0
    assert row["minority_interest"] == 100.0
    assert row["total_debt"] == 550.0
    assert row["cash_change"] == 20.0
    assert row["cash_and_equivalents_begin"] == 780.0
    assert row["cash_and_equivalents_end"] == 800.0


def test_tushare_consensus_batch_uses_full_daily_page_budget_and_resumes(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    class FakePro:
        @staticmethod
        def report_rc(**kwargs):
            calls.append(kwargs)
            offset = kwargs["offset"]
            return pd.DataFrame([{
                "ts_code": "600438.SH" if offset % 6000 == 0 else "000001.SZ",
                "name": "测试公司", "report_date": "20240701",
                "quarter": "2024Q4", "org_name": f"机构{offset}", "np": 1.0,
            }] * 3000)

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())
    monkeypatch.setenv("TUSHARE_CONSENSUS_CACHE_DIR", str(tmp_path / "consensus"))
    monkeypatch.setenv("TUSHARE_REPORT_RC_MIN_INTERVAL_SECONDS", "0")

    provider = TushareProvider()
    first = provider.get_consensus_estimates(
        "2024-01-01", "2024-12-31", ["600438.SH", "601012.SH"]
    )
    second = provider.get_consensus_estimates(
        "2024-01-01", "2024-12-31", ["600438.SH", "601012.SH"]
    )

    assert [call["offset"] for call in calls[:10]] == list(range(0, 30_000, 3_000))
    assert calls[0]["start_date"] == "20241201"
    assert calls[0]["end_date"] == "20241231"
    assert len(calls) == 10
    assert all(call["limit"] == 3_000 for call in calls)
    assert set(first["symbol"]) == {"600438.SH"}
    assert set(second["symbol"]) == {"600438.SH"}

    state_path = tmp_path / "consensus" / "pagination_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["_daily_usage"]["date"] = "2000-01-01"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    provider.get_consensus_estimates(
        "2024-01-01", "2024-12-31", ["600438.SH", "601012.SH"]
    )
    assert calls[10]["offset"] == 30_000


def test_tushare_consensus_syncs_upstream_daily_limit_to_local_state(monkeypatch, tmp_path) -> None:
    class FakePro:
        @staticmethod
        def report_rc(**_kwargs):
            raise Exception("抱歉，您访问接口(report_rc)频率超限(10次/天)")

    monkeypatch.setattr(provider_module, "_ts_pro", FakePro())
    monkeypatch.setenv("TUSHARE_CONSENSUS_CACHE_DIR", str(tmp_path / "consensus"))
    monkeypatch.setenv("TUSHARE_REPORT_RC_MIN_INTERVAL_SECONDS", "0")

    result = TushareProvider().get_consensus_estimates(
        "2024-01-01", "2024-12-31", ["600438.SH", "601012.SH"]
    )

    state = json.loads(
        (tmp_path / "consensus" / "pagination_state.json").read_text(encoding="utf-8")
    )
    assert result.empty
    assert state["_daily_usage"]["calls"] == 10


def test_research_gateway_reuses_consensus_snapshot_without_future_leakage(tmp_path) -> None:
    class Registry:
        logs = []

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame()

    gateway = ResearchSourceGateway(Registry(), cache_dir=tmp_path)
    path = gateway._cache_path("300308.SZ", "consensus_estimates")
    path.parent.mkdir(parents=True)
    path.write_text(__import__("json").dumps({
        "schema_version": "research_source_snapshot_v1",
        "symbol": "300308.SZ",
        "capability": "consensus_estimates",
        "provider": "tushare",
        "source_as_of": "2026-07-18",
        "cached_at": "2026-07-18T12:00:00+08:00",
        "records": [
            {"report_date": "2026-07-07", "org_name": "A", "forecast_period": "2027Q4"},
            {"report_date": "2026-07-19", "org_name": "B", "forecast_period": "2027Q4"},
        ],
    }), encoding="utf-8")

    live = gateway._capture(
        "consensus_estimates", "2026-07-18", lambda: Registry.get_consensus_estimates(),
        visible_time_fields=("report_date",),
    )
    result = gateway._fallback_to_cache(
        "300308.SZ", live, visible_time_fields=("report_date",)
    )

    assert result.status == "ok"
    assert len(result.records) == 1
    assert result.records[0]["org_name"] == "A"
    assert result.excluded_future_records == 1
    assert result.provider == "local_snapshot_cache:tushare"
