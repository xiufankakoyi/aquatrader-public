from __future__ import annotations

import pandas as pd
import time

import infrastructure.data_providers.market.akshare_provider as provider_module
from infrastructure.data_providers.market.akshare_provider import AkshareProvider


def test_board_fund_flow_uses_current_akshare_eastmoney_api(monkeypatch) -> None:
    class FakeAkshare:
        @staticmethod
        def stock_sector_fund_flow_rank(**_kwargs):
            return pd.DataFrame([
                {"名称": "医药商业", "今日涨跌幅": 2.1, "今日主力净流入-净额": 1_500_000_000,
                 "今日主力净流入-净占比": 3.2}
            ])

    monkeypatch.setattr(provider_module, "_ak", FakeAkshare())

    result = AkshareProvider().get_board_fund_flow("2026-07-17")

    assert result.iloc[0]["board_name"] == "医药商业"
    assert result.iloc[0]["main_net_inflow"] == 1_500_000_000
    assert result.iloc[0]["provider"] == "akshare-eastmoney"
    assert result.iloc[0]["methodology"] == "东方财富行业资金流"


def test_board_fund_flow_falls_back_to_ths_without_mixing_providers(monkeypatch) -> None:
    class FakeAkshare:
        @staticmethod
        def stock_sector_fund_flow_rank(**_kwargs):
            raise RuntimeError("eastmoney unavailable")

    monkeypatch.setattr(provider_module, "_ak", FakeAkshare())
    monkeypatch.setattr(
        provider_module,
        "_load_ths_industry_fund_flow",
        lambda: pd.DataFrame([{"行业": "化学制药", "行业-涨跌幅": 4.2, "净额": 8.5}]),
    )

    result = AkshareProvider().get_board_fund_flow("2026-07-17")

    assert result.iloc[0]["board_name"] == "化学制药"
    assert result.iloc[0]["main_net_inflow"] == 850_000_000
    assert set(result["provider"]) == {"akshare-ths"}
    assert result.iloc[0]["methodology"] == "同花顺行业资金流（净额字段，亿元）"


def test_board_fund_flow_times_out_hung_primary_source(monkeypatch) -> None:
    class FakeAkshare:
        @staticmethod
        def stock_sector_fund_flow_rank(**_kwargs):
            time.sleep(1)
            return pd.DataFrame()

    monkeypatch.setattr(provider_module, "_ak", FakeAkshare())
    monkeypatch.setattr(
        provider_module,
        "_load_ths_industry_fund_flow",
        lambda: pd.DataFrame([{"行业": "化学制药", "净额": 1.0}]),
    )
    monkeypatch.setattr(provider_module, "_FUND_FLOW_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()

    result = AkshareProvider().get_board_fund_flow("2026-07-17")

    assert time.monotonic() - started < 0.2
    assert result.iloc[0]["provider"] == "akshare-ths"
