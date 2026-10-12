"""
快速市场行情查询接口

为 server / core 各模块提供统一、轻量的行情数据访问入口。
当前实现基于 data_svc.unified_data_query.UnifiedDataQuery 做兼容封装，
并保留向 OptimizedStockDataQuery 等后端迁移的扩展空间。

设计原则：
- 所有查询默认返回 Polars DataFrame，与现有 Polars 流水线对齐。
- 底层不可用时返回空 DataFrame，避免调用方因数据缺失而崩溃。
- 保持与历史代码中 FastMarketQuery 的接口兼容（_read_table、get_stock_history 等）。
"""

from pathlib import Path
from typing import Any, List, Optional, Union

import polars as pl
from loguru import logger

from data_svc.unified_data_query import get_unified_data_query


PROJECT_ROOT = Path(__file__).parent.parent.parent


class _DummyReader:
    """底层读取器不可用时使用的占位读取器，避免 _reader 访问崩溃。"""

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def get_db_stats(self) -> dict:
        return {"latest_date": None, "row_count": 0}


class FastMarketQuery:
    """
    快速市场行情查询器

    提供与历史代码兼容的查询接口，包括：
    - _read_table: 通用表读取
    - get_stock_history: 单只股票历史行情
    - get_latest_snapshot: 最新交易日全市场快照
    - get_latest_trade_date: 最新交易日
    - get_stock_factors: 单只股票因子数据
    - get_factor_snapshot: 指定日期因子截面
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else PROJECT_ROOT / "data" / "lancedb"
        self._query = get_unified_data_query(str(self._db_path))

    @property
    def _reader(self) -> Any:
        """暴露底层读取器，保持与旧代码兼容。"""
        reader = self._query.reader
        if reader is None:
            return _DummyReader(self._db_path)
        return reader

    def _read_table(
        self,
        table_name: str,
        symbols: Optional[Union[str, List[str]]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """
        读取指定表的数据。

        Args:
            table_name: 表名，如 daily_ohlcv、index_daily、factor_daily、stock_info
            symbols: 单个股票代码或代码列表，为 None 时读取全量
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            fields: 返回列名列表

        Returns:
            Polars DataFrame，底层不可用时返回空表
        """
        try:
            # 统一字段名：UnifiedDataQuery 使用 column/columns，内部 reader 使用 fields
            if symbols is None:
                df = self._query._read_polars(
                    symbol=None,
                    start=start_date,
                    end=end_date,
                    columns=fields,
                    table_name=table_name,
                )
            elif isinstance(symbols, str):
                df = self._query._read_polars(
                    symbol=symbols,
                    start=start_date,
                    end=end_date,
                    columns=fields,
                    table_name=table_name,
                )
            else:
                # 多标的情况：逐只读取后合并
                frames: List[pl.DataFrame] = []
                for symbol in symbols:
                    sub = self._query._read_polars(
                        symbol=symbol,
                        start=start_date,
                        end=end_date,
                        columns=fields,
                        table_name=table_name,
                    )
                    if not sub.is_empty():
                        frames.append(sub)
                if frames:
                    df = pl.concat(frames, how="diagonal")
                else:
                    df = pl.DataFrame()

            if df.is_empty():
                return df

            # 确保字段存在并保留请求顺序
            if fields:
                available = [c for c in fields if c in df.columns]
                if available:
                    df = df.select(available)
            return df
        except Exception as e:
            logger.warning(f"[FastMarketQuery] _read_table 失败 table={table_name}: {e}")
            return pl.DataFrame()

    def get_stock_history(
        self,
        stock_code: str,
        start_date: str,
        end_date: str,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """获取单只股票历史行情，返回 Polars DataFrame。"""
        try:
            return self._query.get_stock_history_polars(
                symbol=stock_code,
                start=start_date,
                end=end_date,
                columns=fields,
            )
        except Exception as e:
            logger.warning(f"[FastMarketQuery] get_stock_history 失败 {stock_code}: {e}")
            return pl.DataFrame()

    def get_latest_snapshot(
        self,
        fields: Optional[List[str]] = None,
        table_name: str = "daily_ohlcv",
    ) -> pl.DataFrame:
        """获取最新交易日的全市场快照。"""
        try:
            latest_date = self.get_latest_trade_date(table_name=table_name)
            if not latest_date:
                return pl.DataFrame()
            return self._read_table(
                table_name=table_name,
                start_date=latest_date,
                end_date=latest_date,
                fields=fields,
            )
        except Exception as e:
            logger.warning(f"[FastMarketQuery] get_latest_snapshot 失败: {e}")
            return pl.DataFrame()

    def get_latest_trade_date(self, table_name: str = "daily_ohlcv") -> Optional[str]:
        """获取指定表最新交易日。"""
        try:
            reader = self._query.reader
            if reader is not None and hasattr(reader, "get_db_stats"):
                stats = reader.get_db_stats()
                latest = stats.get("latest_date")
                if latest:
                    return str(latest)[:10]

            # 兜底：从表中读取最大日期
            df = self._read_table(table_name=table_name, fields=["trade_date"])
            if df.is_empty() or "trade_date" not in df.columns:
                return None
            return str(df["trade_date"].max())[:10]
        except Exception as e:
            logger.warning(f"[FastMarketQuery] get_latest_trade_date 失败: {e}")
            return None

    def get_stock_factors(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """获取单只股票因子数据。"""
        return self._read_table(
            table_name="factor_daily",
            symbols=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=fields,
        )

    def get_factor_snapshot(
        self,
        date: str,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """获取指定日期的因子截面数据。"""
        return self._read_table(
            table_name="factor_daily",
            start_date=date,
            end_date=date,
            fields=fields,
        )


_fast_market_query_instance: Optional[FastMarketQuery] = None


def get_fast_market_query(db_path: Optional[str] = None) -> FastMarketQuery:
    """获取 FastMarketQuery 单例。"""
    global _fast_market_query_instance
    if _fast_market_query_instance is None:
        _fast_market_query_instance = FastMarketQuery(db_path)
    return _fast_market_query_instance


def reset_fast_market_query() -> None:
    """重置 FastMarketQuery 单例，主要用于测试。"""
    global _fast_market_query_instance
    _fast_market_query_instance = None
