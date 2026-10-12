import pandas as pd

from core.utils.indicator_calculator import IndicatorCalculator


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "stock_code": ["A", "A", "A", "B", "B", "B"],
            "close": [1.0, 2.0, 3.0, 10.0, 12.0, 11.0],
        },
        index=[10, 4, 8, 21, 2, 15],
    )


def test_grouped_macd_keeps_original_row_alignment() -> None:
    result = IndicatorCalculator().calculate_macd(_prices(), group_by="stock_code")
    expected = IndicatorCalculator().calculate_macd(
        _prices().sort_values(["stock_code"], kind="stable"), group_by="stock_code"
    )

    assert list(result.index) == [10, 4, 8, 21, 2, 15]
    assert result.loc[10, "macd"] == expected.iloc[0]["macd"]
    assert result.loc[21, "macd"] == expected.iloc[3]["macd"]


def test_grouped_bollinger_keeps_original_row_alignment() -> None:
    result = IndicatorCalculator().calculate_bollinger_bands(
        _prices(), window=2, group_by="stock_code"
    )

    assert list(result.index) == [10, 4, 8, 21, 2, 15]
    assert pd.isna(result.loc[10, "middle"])
    assert result.loc[8, "middle"] == 2.5
    assert result.loc[15, "middle"] == 11.5
