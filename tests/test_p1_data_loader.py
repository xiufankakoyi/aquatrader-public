from __future__ import annotations

import pandas as pd

from core.research.expectations.repository import ExpectationRepository
from core.research.fundamentals.repository import StatementRepository
from core.research.integration.p1_data_loader import P1DataLoader
from core.research.valuation.repository import ValuationRepository


def test_row_to_statement_preserves_balance_sheet_lines() -> None:
    row = pd.Series({
        "period": "2025-12-31",
        "published_at": "2026-04-20",
        "revenue": 1_000.0,
        "net_profit": 100.0,
        "operating_cash_flow": 120.0,
        "total_assets": 5_000.0,
        "total_liabilities": 2_000.0,
        "total_equity": 3_000.0,
        "cash_and_equivalents": 800.0,
    })

    statement = P1DataLoader(provider=object())._row_to_statement("300308.SZ", row)

    assert statement is not None
    assert statement.period == "2025-12-31"
    assert statement.available_at == "2026-04-20"
    lines = {line.metric_name: line.value for line in statement.balance_sheet}
    assert lines == {
        "total_assets": 5_000.0,
        "total_liabilities": 2_000.0,
        "total_equity": 3_000.0,
        "cash_and_equivalents": 800.0,
    }


def test_row_to_statement_preserves_debt_minority_and_cash_reconciliation_lines() -> None:
    row = pd.Series({
        "period": "2024-09-30", "published_at": "2024-10-25",
        "revenue": 1_000.0, "net_profit": 100.0,
        "total_assets": 5_100.0, "total_liabilities": 2_000.0,
        "total_equity": 3_100.0, "minority_interest": 100.0,
        "total_debt": 550.0, "cash_and_equivalents": 800.0,
        "cash_change": 20.0, "cash_and_equivalents_begin": 780.0,
        "cash_and_equivalents_end": 800.0,
    })

    statement = P1DataLoader(provider=object())._row_to_statement("600438.SH", row)

    balance = {line.metric_name: line.value for line in statement.balance_sheet}
    cashflow = {line.metric_name: line.value for line in statement.cash_flow_statement}
    assert balance["minority_interest"] == 100.0
    assert balance["total_debt"] == 550.0
    assert cashflow == {
        "cash_change": 20.0,
        "cash_and_equivalents_begin": 780.0,
        "cash_and_equivalents_end": 800.0,
    }


def test_row_to_statement_degrades_non_scalar_and_non_finite_values() -> None:
    row = {
        "period": "2025-12-31",
        "published_at": "2026-04-20",
        "revenue": [1_000.0],
        "net_profit": float("inf"),
        "operating_cash_flow": 120.0,
    }

    statement = P1DataLoader(provider=object())._row_to_statement("300308.SZ", row)

    assert statement is not None
    assert [line.metric_name for line in statement.income_statement] == []
    assert [line.metric_name for line in statement.cash_flow_statement] == [
        "operating_cash_flow",
    ]


def test_load_statements_does_not_use_dataframe_iterrows(monkeypatch) -> None:
    class Provider:
        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame([{
                "period": "2025-12-31",
                "published_at": "2026-04-20",
                "revenue": 1_000.0,
                "net_profit": 100.0,
            }])

    class Repository:
        def __init__(self) -> None:
            self.saved = []

        def save(self, statement) -> None:
            self.saved.append(statement)

    def fail_iterrows(*_args, **_kwargs):
        raise AssertionError("P1 loader should not iterate pandas Series rows")

    monkeypatch.setattr(pd.DataFrame, "iterrows", fail_iterrows)
    repository = Repository()
    loader = P1DataLoader(fundamentals_repo=repository, provider=Provider())

    assert loader._load_statements("600519.SH", "600519", "2026-07-18") == 1
    assert len(repository.saved) == 1


def test_load_batch_fetches_consensus_once_for_all_symbols(tmp_path) -> None:
    calls: list[dict] = []

    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_consensus_estimates(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame([{
                "symbol": symbol, "report_date": "2024-07-01",
                "org_name": "机构甲", "author_name": "分析师甲",
                "forecast_period": "2024Q4", "eps_forecast": 1.0,
                "provider": "tushare", "updated_at": "2024-07-02",
            } for symbol in ("600438.SH", "601012.SH")])

    repo = ExpectationRepository(tmp_path / "expectations.db")
    results = P1DataLoader(expectation_repo=repo, provider=Provider()).load_batch(
        ["600438.SH", "601012.SH"], "2024-12-31"
    )

    assert len(calls) == 1
    assert calls[0]["symbols"] == ["600438", "601012"]
    assert [result.consensus_loaded for result in results] == [1, 1]


def test_load_company_persists_consensus_rows_idempotently(tmp_path) -> None:
    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame([{
                "symbol": "300308.SZ",
                "report_date": "2026-07-07",
                "org_name": "机构甲",
                "author_name": "分析师甲",
                "forecast_period": "2027Q4",
                "revenue_forecast_yuan": 1_200_000_000.0,
                "net_profit_forecast_yuan": 250_000_000.0,
                "eps_forecast": 2.5,
                "provider": "tushare",
                "updated_at": "2026-07-08T00:00:00+00:00",
            }])

    repo = ExpectationRepository(tmp_path / "expectations.db")
    loader = P1DataLoader(expectation_repo=repo, provider=Provider())

    first = loader.load_company("300308.SZ", "2026-07-18")
    second = loader.load_company("300308.SZ", "2026-07-18")

    items = repo.list("consensus_estimates", index_value="300308.SZ")
    assert first.consensus_loaded == 1
    assert second.consensus_loaded == 1
    assert len(items) == 1
    assert items[0]["institution_id"] == "机构甲"
    assert {value["metric"] for value in items[0]["values"]} == {
        "revenue", "net_profit", "eps",
    }


def test_load_company_builds_historical_valuation_cases(tmp_path) -> None:
    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**_kwargs):
            return pd.DataFrame([
                {"trade_date": "2026-07-14", "close": 80.0, "pe_ttm": 20.0,
                 "pb": 4.0, "ps_ttm": 5.0, "total_market_cap": 800_000_000.0,
                 "provider": "tushare", "updated_at": "2026-07-14T18:00:00+00:00"},
                {"trade_date": "2026-07-15", "close": 84.0, "pe_ttm": 21.0,
                 "pb": 4.2, "ps_ttm": 5.2, "total_market_cap": 840_000_000.0,
                 "provider": "tushare", "updated_at": "2026-07-15T18:00:00+00:00"},
                {"trade_date": "2026-07-16", "close": 88.0, "pe_ttm": 22.0,
                 "pb": 4.4, "ps_ttm": 5.4, "total_market_cap": 880_000_000.0,
                 "provider": "tushare", "updated_at": "2026-07-16T18:00:00+00:00"},
                {"trade_date": "2026-07-17", "close": 92.0, "pe_ttm": 23.0,
                 "pb": 4.6, "ps_ttm": 5.6, "total_market_cap": 920_000_000.0,
                 "provider": "tushare", "updated_at": "2026-07-17T18:00:00+00:00"},
            ])

    repo = ValuationRepository(tmp_path / "valuation.db")
    loader = P1DataLoader(valuation_repo=repo, provider=Provider())

    first = loader.load_company("300308.SZ", "2026-07-18")
    second = loader.load_company("300308.SZ", "2026-07-18")

    items = repo.list_cases(company_symbol="300308.SZ", as_of="2026-07-18")
    assert first.valuation_loaded == 3
    assert second.valuation_loaded == 3
    assert len(items) == 3
    assert {item["method"] for item in items} == {
        "historical_pe", "historical_pb", "historical_ps",
    }
    assert all(item["computability"] == "computable" for item in items)
    assert all(item["source_refs"] for item in items)


def test_valuation_snapshot_includes_same_industry_same_cutoff_peer_basis(tmp_path) -> None:
    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**kwargs):
            symbols = kwargs["symbols"]
            if symbols == ["600001"]:
                return pd.DataFrame([{
                    "symbol": "600001.SH", "trade_date": "2026-07-17", "close": 10,
                    "pe_ttm": 20, "pb": 2, "ps_ttm": 1.5,
                    "total_market_cap": 1000, "provider": "tushare",
                    "updated_at": "2026-07-17T18:00:00+08:00",
                }])
            return pd.DataFrame([
                {"symbol": "600002.SH", "trade_date": "2026-07-17", "pe_ttm": 18,
                 "pb": 1.8, "ps_ttm": 1.2, "total_market_cap": 800, "provider": "tushare"},
                {"symbol": "600003.SH", "trade_date": "2026-07-16", "pe_ttm": 22,
                 "pb": 2.2, "ps_ttm": 1.7, "total_market_cap": 1200, "provider": "tushare"},
                {"symbol": "600004.SH", "trade_date": "2026-07-17", "pe_ttm": 30,
                 "pb": 3, "ps_ttm": 3, "total_market_cap": 9000, "provider": "tushare"},
            ])

    universe = [
        {"symbol": "600001.SH", "name": "目标", "industry": "行业甲",
         "business_structure": "结构甲", "business_structure_source_ref": "annual_report:600001"},
        {"symbol": "600002.SH", "name": "同业甲", "industry": "行业甲",
         "business_structure": "结构甲", "business_structure_source_ref": "annual_report:600002"},
        {"symbol": "600003.SH", "name": "同业乙", "industry": "行业甲",
         "business_structure": "结构甲", "business_structure_source_ref": "annual_report:600003"},
        {"symbol": "600004.SH", "name": "规模过大", "industry": "行业甲",
         "business_structure": "结构甲", "business_structure_source_ref": "annual_report:600004"},
        {"symbol": "600005.SH", "name": "其他行业", "industry": "行业乙"},
    ]
    repo = ValuationRepository(tmp_path / "valuation.db")
    P1DataLoader(
        valuation_repo=repo, provider=Provider(), peer_universe=universe,
    ).load_company("600001.SH", "2026-07-18")

    peers = repo.list_cases(company_symbol="600001.SH")[0]["inputs_snapshot"]["comparable_companies"]
    assert [peer["symbol"] for peer in peers] == ["600002.SH", "600003.SH"]
    assert all(peer["industry"] == "行业甲" for peer in peers)
    assert all(peer["multiple_type"] == "pe_ttm" for peer in peers)
    assert all(peer["currency"] == "CNY" for peer in peers)
    assert all(peer["accounting_standard"] == "PRC_ASBE" for peer in peers)
    assert all(peer["business_structure"] == "结构甲" for peer in peers)
    assert all(peer["business_structure_source_ref"].startswith("annual_report:") for peer in peers)
    assert all(peer["source_ref"].startswith("tushare:valuation_history:") for peer in peers)


def test_comparable_peers_are_empty_without_business_structure_evidence() -> None:
    class Provider:
        @staticmethod
        def get_valuation_history(**_kwargs):
            return pd.DataFrame([{
                "symbol": "600002.SH", "trade_date": "2026-07-17", "pe_ttm": 18,
                "total_market_cap": 800, "provider": "tushare",
            }])

    loader = P1DataLoader(
        provider=Provider(),
        peer_universe=[
            {"symbol": "600001.SH", "name": "目标", "industry": "行业甲"},
            {"symbol": "600002.SH", "name": "同业", "industry": "行业甲"},
        ],
    )

    assert loader._load_comparable_peers("600001.SH", 1000, "2026-07-18") == []


def test_historical_valuation_ids_ignore_provider_retrieval_time(tmp_path) -> None:
    """同一 PIT 数据重复拉取时，provider 抓取时间变化不能制造重复案例。"""
    retrieval_times = iter((
        "2026-07-22T09:00:00+00:00",
        "2026-07-22T09:01:00+00:00",
    ))

    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**_kwargs):
            updated_at = next(retrieval_times)
            return pd.DataFrame([{
                "trade_date": "2026-07-17", "close": 92.0, "pe_ttm": 23.0,
                "pb": 4.6, "ps_ttm": 5.6, "total_market_cap": 920_000_000.0,
                "provider": "tushare", "updated_at": updated_at,
            }])

    repo = ValuationRepository(tmp_path / "valuation.db")
    loader = P1DataLoader(valuation_repo=repo, provider=Provider())

    loader.load_company("300308.SZ", "2026-07-18")
    loader.load_company("300308.SZ", "2026-07-18")

    assert len(repo.list_cases(company_symbol="300308.SZ", as_of="2026-07-18")) == 3


def test_historical_valuation_inputs_reuse_latest_financial_position(tmp_path) -> None:
    class Provider:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_finance_snapshot(**_kwargs):
            return pd.DataFrame([{
                "period": "2024-09-30", "published_at": "2024-10-25",
                "revenue": 1_000.0, "net_profit": 100.0,
                "total_assets": 5_100.0, "total_liabilities": 2_000.0,
                "total_equity": 3_100.0, "minority_interest": 100.0,
                "total_debt": 550.0, "cash_and_equivalents": 800.0,
            }])

        @staticmethod
        def get_consensus_estimates(**_kwargs):
            return pd.DataFrame()

        @staticmethod
        def get_valuation_history(**_kwargs):
            return pd.DataFrame([{
                "trade_date": "2024-12-30", "close": 10.0,
                "pe_ttm": 20.0, "pb": 2.0, "ps_ttm": 1.5,
                "total_market_cap": 10_000.0, "provider": "tushare",
                "updated_at": "2024-12-30T18:00:00+08:00",
            }])

    valuation = ValuationRepository(tmp_path / "valuation.db")
    loader = P1DataLoader(
        fundamentals_repo=StatementRepository(), valuation_repo=valuation,
        provider=Provider(),
    )

    loader.load_company("600438.SH", "2024-12-31")

    snapshot = valuation.list_cases(
        company_symbol="600438.SH", as_of="2024-12-31"
    )[0]["inputs_snapshot"]
    assert snapshot["cash_and_equivalents"] == 800.0
    assert snapshot["total_debt"] == 550.0
    assert snapshot["minority_interest"] == 100.0
    assert snapshot["financial_data_version"] == "2024-10-25"
