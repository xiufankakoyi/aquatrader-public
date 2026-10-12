from __future__ import annotations

import pandas as pd

from infrastructure.data_providers.base import (
    normalize_symbol,
    normalize_symbols,
    stock_code,
)


def test_symbol_helpers_preserve_canonical_exchange_contract() -> None:
    values = normalize_symbols(pd.Series(["sh.600519", "000001", "830799.BJ"]))

    assert isinstance(values, pd.Series)
    assert values.tolist() == ["600519.SH", "000001.SZ", "830799.BJ"]
    assert normalize_symbol("SZ000001") == {
        "symbol": "000001.SZ",
        "exchange": "SZ",
        "raw_symbol": "SZ000001",
    }
    assert stock_code("600519.SH") == "600519"


def test_symbol_helpers_degrade_gracefully_for_missing_or_unknown_values() -> None:
    assert normalize_symbol("")["symbol"] == ""
    assert normalize_symbols([]).empty
    assert stock_code("UNKNOWN") == "UNKNOWN"
