from __future__ import annotations

import pandas as pd

from infrastructure.data_providers.market import akshare_provider
from infrastructure.data_providers.market.akshare_provider import AkshareProvider


class _FailingAkshare:
    @staticmethod
    def stock_board_concept_cons_em(symbol: str) -> pd.DataFrame:
        raise RuntimeError("ProxyError: eastmoney proxy failed")


def test_concept_members_fallback_uses_eastmoney_direct(monkeypatch) -> None:
    def fake_fetch(params: dict, host: str, timeout: int = 15) -> list[dict]:
        assert params["fs"] == "b:BK1134 f:!50"
        return [
            {"f12": "688256", "f14": "寒武纪", "f3": 14.2, "f5": 1000, "f6": 2000},
            {"f12": "688041", "f14": "海光信息", "f3": 6.4, "f5": 3000, "f6": 4000},
        ]

    monkeypatch.setattr(akshare_provider, "_ak", _FailingAkshare())
    monkeypatch.setattr(akshare_provider, "_eastmoney_suggest_concept_board", lambda query: ("BK1134", "算力概念"))
    monkeypatch.setattr(akshare_provider, "_eastmoney_fetch_clist", fake_fetch)

    result = AkshareProvider().get_concept_board_members("算力")

    assert result["provider"].unique().tolist() == ["akshare:eastmoney_direct"]
    assert result["board_code"].unique().tolist() == ["BK1134"]
    assert result["board_name"].unique().tolist() == ["算力概念"]
    assert result["symbol"].tolist() == ["688256", "688041"]
