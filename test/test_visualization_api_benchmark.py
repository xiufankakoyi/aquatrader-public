import pandas as pd
import polars as pl


class _FakeFastMarketQuery:
    """Fake FastMarketQuery for testing _get_benchmark_data_from_db."""

    def __init__(self):
        self.calls = []

    def _read_table(self, table_name, symbols=None, start_date=None, end_date=None, fields=None):
        self.calls.append((table_name, symbols, start_date, end_date, fields))
        return pl.DataFrame(
            {
                "trade_date": [pd.Timestamp("2026-06-10")],
                "close": [3500.0],
            }
        )


def _patch_fmq(monkeypatch):
    """Monkeypatch get_fast_market_query to return _FakeFastMarketQuery."""
    from data_svc.query import fast_market_query
    fake = _FakeFastMarketQuery()
    monkeypatch.setattr(fast_market_query, "get_fast_market_query", lambda: fake)
    return fake


def test_benchmark_accepts_exchange_suffix(monkeypatch):
    from server.visualization_api import BacktestVisualizationAPI

    fake = _patch_fmq(monkeypatch)

    api = BacktestVisualizationAPI.__new__(BacktestVisualizationAPI)
    result = api._get_benchmark_data_from_db(
        "000300.SH",
        "2026-06-01",
        "2026-06-12",
    )

    assert fake.calls == [
        (
            "index_daily",
            "000300.SH",
            "2026-06-01",
            "2026-06-12",
            ["trade_date", "close"],
        )
    ]
    assert result.to_dict("records") == [
        {"date": "2026-06-10", "close": 3500.0}
    ]


def test_benchmark_accepts_plain_code(monkeypatch):
    from server.visualization_api import BacktestVisualizationAPI

    fake = _patch_fmq(monkeypatch)

    api = BacktestVisualizationAPI.__new__(BacktestVisualizationAPI)
    api._get_benchmark_data_from_db("000905", "2026-06-01", "2026-06-12")

    assert fake.calls[0][1] == "000905.SH"
