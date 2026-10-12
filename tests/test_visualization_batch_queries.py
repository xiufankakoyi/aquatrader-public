import pandas as pd

from server.visualization_api import BacktestVisualizationAPI


class _Query:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = []

    def _query_df(self, query, params=None):
        self.calls.append((query, params))
        return self.frame


def _api(query: _Query) -> BacktestVisualizationAPI:
    api = object.__new__(BacktestVisualizationAPI)
    api.data_query = query
    api.stock_info_map = {}
    api._initialized = True
    return api


def test_global_latest_factors_are_loaded_in_one_query() -> None:
    query = _Query(
        pd.DataFrame(
            [
                {"stock_code": "1", "adj_factor": 2.0},
                {"stock_code": "2", "adj_factor": 3.0},
            ]
        )
    )
    factors = _api(query)._get_global_latest_factors(["1", "2"])

    assert factors == {"1": 2.0, "2": 3.0}
    assert len(query.calls) == 1
    assert "ROW_NUMBER" in query.calls[0][0]


def test_missing_stock_names_are_loaded_in_one_query() -> None:
    query = _Query(
        pd.DataFrame(
            [
                {"stock_code": "1", "stock_name": "甲"},
                {"stock_code": "2", "stock_name": "乙"},
            ]
        )
    )
    names = _api(query)._load_missing_stock_names(["1", "2"])

    assert names == {"1": "甲", "2": "乙"}
    assert len(query.calls) == 1
    assert "stock_code IN" in query.calls[0][0]
