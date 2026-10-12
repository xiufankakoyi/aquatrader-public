import numpy as np

from core.quant.backtest.factor_matrix import FactorMatrixBuilder


def test_adjusted_prices_copy_available_values_and_preserve_missing_targets() -> None:
    factors = [
        "open",
        "high",
        "low",
        "close",
        "open_adj",
        "high_adj",
        "low_adj",
        "close_adj",
    ]
    values = np.full((2, 2, len(factors)), np.nan, dtype=np.float32)
    values[0, 0, 0] = 10.0
    values[0, 0, 1] = 11.0
    values[0, 0, 2] = 9.0
    values[0, 0, 3] = 10.5
    values[0, 1, 4] = 99.0
    values[1, 0, 5] = 88.0

    result = FactorMatrixBuilder(custom_factors=factors)._compute_adjusted_prices(
        values,
        stock_codes=["A", "B"],
        trading_dates=["2026-01-01", "2026-01-02"],
        factor_names=factors,
    )

    assert result[0, 0, 4] == 10.0
    assert result[0, 0, 5] == 11.0
    assert result[0, 0, 6] == 9.0
    assert result[0, 0, 7] == 10.5
    assert result[0, 1, 4] == 99.0
    assert result[1, 0, 5] == 88.0
    assert np.isnan(result[1, 1, 4])
