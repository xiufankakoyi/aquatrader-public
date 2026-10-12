"""
Unified data query adapter for LanceDB.

直接通过 lancedb.connect() 读取数据，避免与 lancedb_reader.py 形成循环依赖。
兼容方法返回 Pandas DataFrame，热路径可调用 Polars/Arrow 变体避免转换开销。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Set
import time

import pandas as pd
import polars as pl
import pyarrow as pa
from loguru import logger

try:
    import lancedb

    LANCEDB_AVAILABLE = True
except ImportError:  # pragma: no cover - lancedb 是项目核心依赖
    lancedb = None  # type: ignore
    LANCEDB_AVAILABLE = False


PROJECT_ROOT = Path(__file__).parent.parent

_stock_basic_polars_cache: Optional[pl.DataFrame] = None
_fund_basic_cache = None
_libraries_cache: Optional[Set[str]] = None
_symbols_cache = {}
_cache_timestamp = 0.0
_CACHE_TTL = 300


def _invalidate_cache_if_needed() -> None:
    global _libraries_cache, _symbols_cache, _cache_timestamp
    current_time = time.time()
    if current_time - _cache_timestamp > _CACHE_TTL:
        _libraries_cache = None
        _symbols_cache = {}
        _cache_timestamp = current_time


def get_libraries_cached() -> Set[str]:
    global _libraries_cache
    _invalidate_cache_if_needed()

    if _libraries_cache is None:
        lancedb_path = PROJECT_ROOT / "data" / "lancedb"
        if lancedb_path.exists():
            _libraries_cache = {d.name for d in lancedb_path.iterdir() if d.is_dir()}
        else:
            _libraries_cache = set()
    return _libraries_cache


def _to_pandas_compat(df: pl.DataFrame) -> pd.DataFrame:
    """仅在 API 兼容边界转换为 Pandas。"""
    if df.is_empty():
        return pd.DataFrame()
    try:
        return df.to_pandas(use_pyarrow_extension_array=True)
    except TypeError:
        return df.to_pandas()


def _normalize_date_text(value: Any) -> str:
    """统一日期为 YYYY-MM-DD 字符串。"""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value)[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


class UnifiedDataQuery:
    """统一数据查询门面，直接对接 LanceDB。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(PROJECT_ROOT / "data" / "lancedb")
        self._db = None
        # 兼容旧调用：reader 现在指向自身，避免 lancedb_reader.py 空壳死循环
        self._reader = None
        # 预加载数据缓存：{date_str: pl.DataFrame}
        # 由 preload_backtest_data 填充，由 get_stock_pool_from_preloaded 读取
        self._preloaded_data: dict = {}
        self._preloaded_date_range: Optional[tuple] = None

    # ------------------------------------------------------------------
    # LanceDB 连接管理
    # ------------------------------------------------------------------
    def _get_db(self):
        """惰性连接 LanceDB，连接失败返回 None。"""
        if not LANCEDB_AVAILABLE:
            return None
        if self._db is None:
            try:
                self._db = lancedb.connect(self.db_path)
            except Exception as exc:
                logger.warning("[UnifiedDataQuery] LanceDB 连接失败 path={}: {}", self.db_path, exc)
                self._db = None
        return self._db

    def _list_tables(self) -> List[str]:
        """列出 LanceDB 中所有表名。"""
        db = self._get_db()
        if db is None:
            return []
        try:
            listed = db.list_tables()
            return listed.tables if hasattr(listed, "tables") else list(listed)
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] list_tables 失败: {}", exc)
            return []

    def _open_table(self, table_name: str):
        """打开指定表，不存在或失败返回 None。"""
        db = self._get_db()
        if db is None:
            return None
        try:
            return db.open_table(table_name)
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] open_table {} 失败: {}", table_name, exc)
            return None

    @property
    def reader(self):
        """兼容旧代码对 reader 属性的访问，返回自身以提供 read_table/list_symbols 等接口。"""
        return self if LANCEDB_AVAILABLE else None

    # ------------------------------------------------------------------
    # 核心读取：直接通过 LanceDB scanner 过滤
    # ------------------------------------------------------------------
    def _read_polars(
        self,
        symbol: Optional[str],
        start: Optional[str],
        end: Optional[str],
        columns: Optional[List[str]] = None,
        table_name: str = "daily_ohlcv",
    ) -> pl.DataFrame:
        """
        从 LanceDB 读取数据并返回 Polars DataFrame。

        Args:
            symbol: 股票/指数代码，None 表示全部。
            start: 起始日期 YYYY-MM-DD。
            end: 结束日期 YYYY-MM-DD。
            columns: 需要返回的列名列表，None 表示全部。
            table_name: 表名，如 daily_ohlcv / index_daily / stock_info。

        Returns:
            pl.DataFrame，底层不可用或表为空时返回空表。
        """
        table = self._open_table(table_name)
        if table is None:
            return pl.DataFrame()

        try:
            schema_cols = {field.name for field in table.schema}
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] 读取 schema 失败 table={}: {}", table_name, exc)
            return pl.DataFrame()

        # 构造 LanceDB scanner 的 filter / columns 参数
        scanner_columns = self._resolve_scanner_columns(columns, schema_cols, table_name)
        scanner_filter = self._build_scanner_filter(symbol, start, end, schema_cols)

        try:
            arrow_table = table.to_lance().scanner(
                columns=scanner_columns,
                filter=scanner_filter,
            ).to_table()
            df = pl.from_arrow(arrow_table)
            if df.is_empty():
                return pl.DataFrame()
            # trade_date 统一为字符串 YYYY-MM-DD，便于下游处理
            if "trade_date" in df.columns and df.schema["trade_date"] != pl.Utf8:
                df = df.with_columns(
                    pl.col("trade_date").cast(pl.Utf8).str.slice(0, 10).alias("trade_date")
                )
            return df
        except Exception as exc:
            logger.warning(
                "[UnifiedDataQuery] read error table={} symbol={} start={} end={}: {}",
                table_name, symbol, start, end, exc,
            )
            return pl.DataFrame()

    @staticmethod
    def _resolve_scanner_columns(
        columns: Optional[List[str]],
        schema_cols: Set[str],
        table_name: str,
    ) -> Optional[List[str]]:
        """根据请求列与 schema 取交集；无交集时返回 None 表示读全部。"""
        if not columns:
            return None
        available = [c for c in columns if c in schema_cols]
        # stock_info 表没有 trade_date，daily_ohlcv 至少返回 trade_date 便于下游处理
        if table_name == "daily_ohlcv" and "trade_date" in schema_cols and "trade_date" not in available:
            available.append("trade_date")
        return available or None

    @staticmethod
    def _build_scanner_filter(
        symbol: Optional[str],
        start: Optional[str],
        end: Optional[str],
        schema_cols: Set[str],
    ) -> Optional[str]:
        """构造 LanceDB scanner 的 SQL 过滤表达式。"""
        conditions = []
        if symbol and "stock_code" in schema_cols:
            conditions.append(f"stock_code = '{symbol}'")
        elif symbol and "symbol" in schema_cols:
            conditions.append(f"symbol = '{symbol}'")

        if start and "trade_date" in schema_cols:
            start_text = _normalize_date_text(start)
            conditions.append(f"trade_date >= date '{start_text}'")
        if end and "trade_date" in schema_cols:
            end_text = _normalize_date_text(end)
            conditions.append(f"trade_date <= date '{end_text}'")

        if not conditions:
            return None
        return " AND ".join(conditions)

    # ------------------------------------------------------------------
    # 公共查询接口
    # ------------------------------------------------------------------
    def get_stock_history(
        self,
        symbol: str,
        start: str,
        end: str,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        return _to_pandas_compat(self.get_stock_history_polars(symbol, start, end, columns))

    def get_stock_history_polars(
        self,
        symbol: str,
        start: str,
        end: str,
        columns: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        return self._read_polars(symbol, start, end, columns=columns, table_name="daily_ohlcv")

    def get_stock_history_arrow(
        self,
        symbol: str,
        start: str,
        end: str,
        columns: Optional[List[str]] = None,
    ) -> pa.Table:
        return self.get_stock_history_polars(symbol, start, end, columns).to_arrow()

    def get_stock_basic(self) -> pd.DataFrame:
        return _to_pandas_compat(self.get_stock_basic_polars())

    def get_stock_basic_polars(self) -> pl.DataFrame:
        global _stock_basic_polars_cache

        if _stock_basic_polars_cache is not None:
            return _stock_basic_polars_cache.clone()

        df = self._read_polars(None, None, None, table_name="stock_info")
        if not df.is_empty():
            _stock_basic_polars_cache = df.clone()
        return df

    def get_index_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return _to_pandas_compat(self.get_index_daily_polars(symbol, start, end))

    def get_index_daily_polars(self, symbol: str, start: str, end: str) -> pl.DataFrame:
        return self._read_polars(symbol, start, end, table_name="index_daily")

    def get_fund_nav(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        return _to_pandas_compat(self.get_fund_nav_polars(symbol, start, end))

    def get_fund_nav_polars(self, symbol: str, start: str, end: str) -> pl.DataFrame:
        return self._read_polars(symbol, start, end, table_name="fund_nav")

    def get_stock_pool(self, date: str) -> pd.DataFrame:
        """
        获取指定日期的全部股票行情数据（股票池）。

        Args:
            date: 日期 YYYY-MM-DD。

        Returns:
            Pandas DataFrame，包含当日所有股票的 OHLCV 及衍生指标。
            底层不可用或无数据时返回空 DataFrame。
        """
        df = self._read_polars(
            symbol=None,
            start=date,
            end=date,
            table_name="daily_ohlcv",
        )
        return _to_pandas_compat(df)

    def get_stock_pool_pl(self, date: str) -> pl.DataFrame:
        """get_stock_pool 的 Polars 版本，供 data_layer 优先调用避免转换开销。"""
        return self._read_polars(
            symbol=None,
            start=date,
            end=date,
            table_name="daily_ohlcv",
        )

    # ------------------------------------------------------------------
    # 预加载优化：一次 LanceDB 查询替代 N 次每日查询
    # ------------------------------------------------------------------
    def preload_backtest_data(self, start: str, end: str, columns: Optional[List[str]] = None) -> None:
        """
        一次性加载 [start, end] 区间内的全部股票数据到内存。

        优化点：
        1. 只加载实际有数据的交易日（通过 get_trade_dates 过滤）
        2. 一次 LanceDB scanner 查询读取整个区间，避免每日重复查表
        3. 按 trade_date 分组存入 _preloaded_data，供 get_stock_pool_from_preloaded O(1) 取数
        4. 列裁剪：仅读取 columns 指定的列，减少 IO 和内存（None 时读全部列）

        注意：这里不构建因子矩阵（_factor_matrix 保持 None），因为 signal_layer
        对 numpy 数组格式的 stock_pool 不兼容（直接返回空信号）。
        每日数据走 get_stock_pool_from_preloaded 路径，返回 pl.DataFrame，
        既保持性能（O(1) 内存取数）又保持格式兼容。

        Args:
            start: 起始日期 YYYY-MM-DD
            end: 结束日期 YYYY-MM-DD
            columns: 需要加载的列名列表（列裁剪优化），None 表示读全部列
        """
        t0 = time.time()
        # 1. 先获取区间内实际有数据的交易日（只加载可用项）
        trading_dates = self.get_trade_dates(start, end)
        if not trading_dates:
            logger.warning("[UnifiedDataQuery] preload: 区间 {} ~ {} 无交易日数据", start, end)
            self._preloaded_data = {}
            self._preloaded_date_range = (start, end)
            return

        logger.info(
            "[UnifiedDataQuery] preload: {} ~ {} 共 {} 个交易日, columns={}",
            start, end, len(trading_dates), "all" if columns is None else f"{len(columns)}列",
        )

        # 2. 一次性从 LanceDB 读取整个区间的数据（带列裁剪）
        df_all = self._read_polars(
            symbol=None,
            start=start,
            end=end,
            columns=columns,
            table_name="daily_ohlcv",
        )
        if df_all.is_empty():
            logger.warning("[UnifiedDataQuery] preload: LanceDB 读取数据为空")
            self._preloaded_data = {}
            self._preloaded_date_range = (start, end)
            return

        # 3. 按 trade_date 分组存入字典
        try:
            if "trade_date" not in df_all.columns:
                logger.warning("[UnifiedDataQuery] preload: 数据缺少 trade_date 列")
                self._preloaded_data = {}
                self._preloaded_date_range = (start, end)
                return

            if df_all.schema["trade_date"] != pl.Utf8:
                df_all = df_all.with_columns(
                    pl.col("trade_date").cast(pl.Utf8).str.slice(0, 10).alias("trade_date")
                )

            grouped = df_all.partition_by("trade_date", as_dict=True)
            self._preloaded_data = {}
            for key, df_day in grouped.items():
                if isinstance(key, tuple):
                    date_str = str(key[0])[:10]
                else:
                    date_str = str(key)[:10]
                self._preloaded_data[date_str] = df_day

            self._preloaded_date_range = (start, end)
            elapsed = time.time() - t0
            logger.info(
                "[UnifiedDataQuery] preload 完成: {} 个日期, {} 行, 用时 {:.2f}s",
                len(self._preloaded_data),
                len(df_all),
                elapsed,
            )
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] preload 分组失败: {}", exc)
            self._preloaded_data = {}
            self._preloaded_date_range = (start, end)

    def get_stock_pool_from_preloaded(self, date: str) -> Optional[pl.DataFrame]:
        """
        从预加载数据中取出指定日期的股票池。

        Args:
            date: 日期 YYYY-MM-DD

        Returns:
            Polars DataFrame，未预加载或日期不存在时返回 None。
        """
        if not hasattr(self, "_preloaded_data") or not self._preloaded_data:
            logger.debug("[UnifiedDataQuery] get_stock_pool_from_preloaded: _preloaded_data 为空")
            return None
        date_str = str(date)[:10]
        result = self._preloaded_data.get(date_str)
        logger.debug(
            "[UnifiedDataQuery] get_stock_pool_from_preloaded({}): {} 行",
            date_str,
            len(result) if result is not None else "None",
        )
        return result

    def get_all_daily_data_for_period(self, start: str, end: str) -> dict:
        """
        加载区间全部日线数据并按日期分组返回。

        data_layer._preload_data 通过 hasattr 检查此方法是否存在来决定是否走预加载路径，
        因此这里既作为能力标记，也作为预加载的实际入口（委托给 preload_backtest_data）。

        Args:
            start: 起始日期 YYYY-MM-DD
            end: 结束日期 YYYY-MM-DD

        Returns:
            dict: {date_str: pl.DataFrame}，由 preload_backtest_data 填充的 _preloaded_data
        """
        self.preload_backtest_data(start, end)
        return self._preloaded_data

    def get_trade_dates(self, start: str, end: str) -> List[str]:
        """从 daily_ohlcv 表提取 [start, end] 区间内的交易日列表。"""
        df = self._read_polars(
            symbol=None,
            start=start,
            end=end,
            columns=["trade_date"],
            table_name="daily_ohlcv",
        )
        if df.is_empty() or "trade_date" not in df.columns:
            return []
        try:
            dates = df["trade_date"].unique().sort().to_list()
            return [str(d)[:10] for d in dates]
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] trade_dates 解析失败: {}", exc)
            return []

    # 别名：回测引擎 data_layer.py 调用的是 get_trading_dates（带 ing）
    def get_trading_dates(self, start: str, end: str) -> List[str]:
        """get_trade_dates 的别名，兼容回测引擎 data_layer 调用。"""
        return self.get_trade_dates(start, end)

    def _query_df(self, query: str, params: Optional[List[Any]] = None) -> pd.DataFrame:
        """
        兼容 OptimizedStockDataQuery 的原始 SQL 查询接口。

        UnifiedDataQuery 面向 LanceDB，对 SQLite/Parquet 的原始 SQL 查询返回空 DataFrame，
        调用方应自行处理空结果降级。
        """
        logger.debug(
            "[UnifiedDataQuery] _query_df is not supported; query={}", query[:50]
        )
        return pd.DataFrame()

    def list_symbols(self, table_name: str = "daily_ohlcv") -> List[str]:
        """返回指定表中所有股票代码。"""
        df = self._read_polars(
            symbol=None,
            start=None,
            end=None,
            columns=["stock_code"] if table_name == "daily_ohlcv" else ["symbol"],
            table_name=table_name,
        )
        if df.is_empty():
            return []
        col = "stock_code" if "stock_code" in df.columns else "symbol"
        try:
            return sorted({str(v) for v in df[col].to_list() if v is not None})
        except Exception as exc:
            logger.warning("[UnifiedDataQuery] list_symbols 解析失败: {}", exc)
            return []

    # ------------------------------------------------------------------
    # 兼容 LanceDBDataReader 旧接口（供 stock_data_service 等调用方使用）
    # ------------------------------------------------------------------
    def read_table(
        self,
        table_name: str,
        symbol: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """LanceDBDataReader.read_table 的兼容实现，返回 Pandas DataFrame。"""
        df = self._read_polars(
            symbol=symbol,
            start=start,
            end=end,
            columns=fields,
            table_name=table_name,
        )
        return _to_pandas_compat(df)

    def get_daily(self, symbol: str, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
        """LanceDBDataReader.get_daily 的兼容实现。"""
        return self.read_table("daily_ohlcv", symbol=symbol, start=start, end=end)

    def get_symbols(self, table_name: str = "daily_ohlcv") -> List[str]:
        """LanceDBDataReader.get_symbols 的兼容实现。"""
        return self.list_symbols(table_name)

    def get_db_stats(self) -> dict:
        """LanceDBDataReader.get_db_stats 的兼容实现，供 FastMarketQuery 使用。"""
        df = self._read_polars(
            symbol=None,
            start=None,
            end=None,
            columns=["trade_date"],
            table_name="daily_ohlcv",
        )
        if df.is_empty() or "trade_date" not in df.columns:
            return {"latest_date": None, "row_count": 0}
        try:
            dates = df["trade_date"].sort()
            return {
                "latest_date": str(dates[-1])[:10],
                "row_count": len(df),
            }
        except Exception:
            return {"latest_date": None, "row_count": 0}


_unified_data_query_instance: Optional[UnifiedDataQuery] = None

# 兼容旧代码中使用的别名
UnifiedDataQueryAdapter = UnifiedDataQuery


def get_unified_data_query(db_path: Optional[str] = None) -> UnifiedDataQuery:
    global _unified_data_query_instance
    if _unified_data_query_instance is None:
        _unified_data_query_instance = UnifiedDataQuery(db_path)
    return _unified_data_query_instance


def reset_unified_data_query() -> None:
    global _unified_data_query_instance, _stock_basic_polars_cache, _fund_basic_cache
    _unified_data_query_instance = None
    _stock_basic_polars_cache = None
    _fund_basic_cache = None


def get_stock_daily_latest_polars(
    columns: Optional[List[str]] = None,
) -> pl.DataFrame:
    """Return all rows from the latest local trading date as a Polars frame."""
    query = get_unified_data_query()
    stats = query.get_db_stats()
    latest_date = stats.get("latest_date")
    if not latest_date:
        return pl.DataFrame()

    default_columns = [
        "stock_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    return query._read_polars(
        symbol=None,
        start=latest_date,
        end=latest_date,
        columns=columns or default_columns,
        table_name="daily_ohlcv",
    )
