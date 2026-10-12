"""可视化数据层优先使用已有 Polars 股票信息接口。"""

import polars as pl

from server.visualization_api import BacktestVisualizationAPI


class _PolarsOnlyQuery:
    def get_stock_basic_polars(self):
        return pl.DataFrame(
            {
                "stock_code": ["000001", "600000"],
                "stock_name": ["平安银行", "浦发银行"],
            }
        )


def test_load_stock_info_uses_polars_query_without_legacy_sql_adapter():
    api = BacktestVisualizationAPI.__new__(BacktestVisualizationAPI)
    api.data_query = _PolarsOnlyQuery()

    assert api._load_stock_info() == {"1": "平安银行", "600000": "浦发银行"}
