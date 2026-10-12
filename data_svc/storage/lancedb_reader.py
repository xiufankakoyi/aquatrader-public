"""LanceDB 数据读取器。

历史版本是空壳，所有方法转发回 UnifiedDataQuery，形成死循环。
现在 UnifiedDataQuery 已直接对接 LanceDB，本模块作为薄包装层保留，
供 quant_flow_pipeline、stock_data_service 等历史调用方无感使用。
"""

from typing import List, Optional

import pandas as pd
import polars as pl

from data_svc.unified_data_query import UnifiedDataQuery


class LanceDBDataReader:
    """LanceDB 数据读取器，内部委托 UnifiedDataQuery 实现。"""

    def __init__(self, db_path: Optional[str] = None):
        # 委托给 UnifiedDataQuery，由它直接连接 LanceDB
        self.query = UnifiedDataQuery(db_path=db_path)

    def read_table(
        self,
        table_name: str,
        symbol: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        按表名读取数据。

        Args:
            table_name: 表名，如 ``daily_ohlcv``、``index_daily``、``stock_info``。
            symbol: 股票/指数代码。
            start: 起始日期 YYYY-MM-DD。
            end: 结束日期 YYYY-MM-DD。
            fields: 需要的字段，None 表示全部。

        Returns:
            查询结果 DataFrame，底层不可用时返回空表。
        """
        return self.query.read_table(table_name, symbol, start, end, fields=fields)

    def read_all(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """读取全市场日线并保留 Polars 类型，供批量因子任务使用。"""
        return self.query._read_polars(
            symbol=None,
            start=start_date,
            end=end_date,
            columns=fields,
            table_name="daily_ohlcv",
        )

    def get_daily(self, symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
        """获取单股票日线数据。"""
        return self.query.get_daily(symbol, start, end)

    def get_symbols(self, table_name: str = "daily_ohlcv") -> List[str]:
        """返回指定表的可用代码列表。"""
        return self.query.list_symbols(table_name)

    def get_db_stats(self) -> dict:
        """返回数据库统计信息，供 FastMarketQuery 使用。"""
        return self.query.get_db_stats()


# 模块级单例与工厂函数
_lancedb_reader_instance: Optional["LanceDBDataReader"] = None


def get_lancedb_reader(db_path: Optional[str] = None) -> "LanceDBDataReader":
    """返回 LanceDBDataReader 单例工厂函数。

    多处模块通过 `from data_svc.storage.lancedb_reader import get_lancedb_reader`
    引用本函数（如 screener_routes.get_lancedb / screener_data_service /
    optimized_data_query）。缺失会导致 ImportError，使 screener 全套端点
    （/filter、/stats、/field_stats、/export、/rank）运行时崩溃。

    Args:
        db_path: LanceDB 数据库路径，缺省由 UnifiedDataQuery 自行决定。

    Returns:
        LanceDBDataReader 单例（首次调用时构造）。
    """
    global _lancedb_reader_instance
    if _lancedb_reader_instance is None:
        _lancedb_reader_instance = LanceDBDataReader(db_path=db_path)
    return _lancedb_reader_instance
