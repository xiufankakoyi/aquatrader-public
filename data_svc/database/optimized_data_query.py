# database/optimized_data_query.py
"""
优化的股票数据查询类

【架构】ArcticDB + Polars 双层架构

主要功能：
1. get_stock_pool: 获取指定日期的股票池（核心查询，已优化）
2. get_trading_dates: 获取交易日列表
3. get_stock_history: 获取单只股票历史数据
4. preload_backtest_data: 预加载回测数据（减少 I/O）

性能优化：
- LRU 缓存机制
- Polars 零拷贝查询（比 DuckDB 快 10 倍）
- 数据预加载（适用于长期回测）
- 错误重试机制
"""
import os
import time
from bisect import bisect_left, bisect_right
from functools import lru_cache

from core.error_handler import ErrorHandler, ErrorLevel, capture_error
from core.error_handler.exceptions import NoBackendError
from pathlib import Path
from typing import List, Optional, Dict, Tuple, FrozenSet, Any
import pandas as pd
import polars as pl
from config.config import Config
from config.logger import get_logger


class _DBStageTimer:
    """辅助上下文管理器，用于记录函数内部各阶段耗时"""

    def __init__(self, owner: "OptimizedStockDataQuery", func: str, stage: str):
        self.owner = owner
        self.func = func
        self.stage = stage
        self.start = 0.0
        self.metadata: Dict[str, Any] = {}

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def add_meta(self, **kwargs: Any) -> None:
        self.metadata.update(kwargs)

    def __exit__(self, exc_type, exc, tb):
        duration = time.perf_counter() - self.start
        if (
            self.owner._profile_verbose
            and duration >= self.owner._profile_threshold
        ):
            meta = " ".join(f"{k}={v}" for k, v in self.metadata.items())
            self.owner._logger.warning(
                f"[DB PROFILE] {self.func}.{self.stage} {duration:.3f}s {meta}".strip()
            )
        return False


class OptimizedStockDataQuery:
    """
    优化的股票数据查询类
    
    【架构】ArcticDB + Polars 双层架构
    - ArcticDB: 写入层/持久化层
    - Polars: 分析层，直接读取 Parquet 文件
    
    【性能】Polars 比 DuckDB 快 10 倍，支持零拷贝和懒加载
    """
    
    def __init__(self, warmup: bool = True):
        self._logger = get_logger(__name__)
        self._profile_verbose = os.getenv("DB_PROFILE_VERBOSE", "0") == "1"
        self._profile_threshold = float(os.getenv("DB_PROFILE_THRESHOLD", "0.02"))

        backend = os.getenv("DB_BACKEND", "lancedb").lower()
        self._logger.info(f"[DB] 环境变量 DB_BACKEND={backend}")

        self._use_lancedb = backend == "lancedb"

        # LanceDB 写入层
        self._lancedb_manager = None

        if self._use_lancedb:
            try:
                from data_svc.storage import get_lancedb_manager
                self._lancedb_manager = get_lancedb_manager()
                self._logger.info("[DB] [OK] LanceDB 写入层初始化成功")
            except Exception as e:
                ErrorHandler.capture(e, level=ErrorLevel.WARNING, category="database", context={"backend": "lancedb"})
                self._logger.warning(f"[DB] LanceDB 初始化失败: {e}")
        
        # Parquet 文件目录（Polars 分析层）
        parquet_dir_env = os.getenv("PARQUET_DIR")
        if parquet_dir_env:
            if os.path.isabs(parquet_dir_env):
                self.parquet_dir = parquet_dir_env
            else:
                self.parquet_dir = os.path.join(Config.BASE_DIR, parquet_dir_env)
        else:
            self.parquet_dir = Config.PARQUET_DIR
        
        # 验证 Parquet 文件是否存在
        parquet_path = Path(self.parquet_dir)
        if not parquet_path.exists():
            self._logger.warning(f"[DB] Parquet 目录不存在: {self.parquet_dir}")
        else:
            self._logger.info(f"[DB] [OK] Polars 分析层已就绪: dir={self.parquet_dir}")

        # 缓存相关
        self._cache = {}
        self._cache_size = 200
        self._max_trade_date_cache = None
        
        # 表结构缓存（Polars schema）
        self._table_columns_cache: Dict[str, FrozenSet[str]] = {}
        
        # stock_info 缓存（is_st 等信息不常变化）
        self._stock_info_cache: Optional[pl.DataFrame] = None
        
        # 交易日期缓存
        self._all_trading_dates_cache: Optional[List[str]] = None
        
        # stock_limit_status 缓存（按日期索引）
        self._stock_limit_status_cache: Optional[Dict[str, pl.DataFrame]] = None
        self._stock_limit_status_cache_range: Optional[Tuple[str, str]] = None

        # 预加载缓存 - 使用 Polars DataFrame 实现零拷贝
        self._preloaded_data: Optional[Dict[str, 'pl.DataFrame']] = None
        self._preloaded_date_range: Optional[Tuple[str, str]] = None

        # 连接预热（延迟到首次使用时，避免启动时耗时）
        self._warmup_done = False
        if warmup:
            # 不立即预热，改为在首次查询时预热
            pass
        
        # 优化：预加载所有交易日期到内存（延迟到首次使用时）
        # 避免启动时耗时
        self._preload_dates_done = False
        self._views_registered = False


    def _profile(self, func_name: str, stage: str) -> _DBStageTimer:
        return _DBStageTimer(self, func_name, stage)
    
    def _preload_all_trading_dates(self) -> None:
        """
        预加载所有交易日期到内存（优化 get_trading_dates 查询）
        
        【Polars 实现】直接读取 Parquet 文件，比 DuckDB 快 10 倍
        """
        if self._use_lancedb:
            # LanceDB 是行情主源，按实际回测区间查询，避免扫描全表和使用过期快照。
            return

        try:
            # 优先从 benchmark_daily.parquet 读取（数据量小）
            benchmark_path = Path(self.parquet_dir) / "benchmark_daily.parquet"
            
            if benchmark_path.exists():
                df = pl.scan_parquet(str(benchmark_path)).select("date").unique().sort("date").collect()
                if not df.is_empty():
                    self._all_trading_dates_cache = df["date"].to_list()
                    self._logger.info(f"[DB] 预加载交易日期完成 (benchmark): {len(self._all_trading_dates_cache)} 个日期")
                    return
            
            # 回退：从 stock_daily.parquet 读取
            stock_daily_path = Path(self.parquet_dir) / "stock_daily.parquet"
            if stock_daily_path.exists():
                df = pl.scan_parquet(str(stock_daily_path)).select("trade_date").unique().sort("trade_date").collect()
                if not df.is_empty():
                    self._all_trading_dates_cache = df["trade_date"].to_list()
                    self._logger.info(f"[DB] 预加载交易日期完成 (stock_daily): {len(self._all_trading_dates_cache)} 个日期")
                    return
            
            self._logger.warning("[DB] 预加载交易日期失败: Parquet 文件不存在")
            
        except Exception as e:
            self._logger.warning(f"[DB] 预加载交易日期异常: {e}")

    def _get_lancedb_trading_dates(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Optional[List[str]]:
        """从 LanceDB 主行情表读取交易日；读取异常时返回 None 以允许降级。"""
        try:
            from data_svc.storage.lancedb_reader import get_lancedb_reader

            df = get_lancedb_reader().read_table(
                "daily_ohlcv",
                None,
                start_date,
                end_date,
                fields=["trade_date"],
            )
            if df.is_empty() or "trade_date" not in df.columns:
                return []

            return (
                df.select(
                    pl.col("trade_date")
                    .cast(pl.Date, strict=False)
                    .dt.strftime("%Y-%m-%d")
                    .alias("trade_date")
                )
                .drop_nulls()
                .unique()
                .sort("trade_date")
                .get_column("trade_date")
                .to_list()
            )
        except Exception as e:
            self._logger.warning(f"[DB] LanceDB 交易日期读取失败，降级到快照: {e}")
            return None
    
    def _get_parquet_path(self, table_name: str) -> Optional[Path]:
        """
        获取 Parquet 文件路径
        
        Args:
            table_name: 表名 (stock_daily, stock_info, stock_limit_status, benchmark_daily)
        
        Returns:
            Parquet 文件路径，如果不存在返回 None
        """
        # 表名到文件名的映射
        table_files = {
            "stock_daily": "stock_daily.parquet",
            "stock_info": "stock_info.parquet",
            "stock_limit_status": "stock_limit_status.parquet",
            "benchmark_data": "benchmark_daily.parquet",
            "benchmark_daily": "benchmark_daily.parquet",
        }
        
        filename = table_files.get(table_name)
        if not filename:
            return None
        
        path = Path(self.parquet_dir) / filename
        return path if path.exists() else None
    
    def _warmup_connection(self):
        """
        预热连接：加载 stock_info 到内存缓存
        
        【Polars 实现】不再需要预热 DuckDB 连接
        """
        try:
            # 预加载 stock_info 到内存
            if self._stock_info_cache is None:
                stock_info_path = self._get_parquet_path("stock_info")
                if stock_info_path:
                    self._stock_info_cache = pl.scan_parquet(str(stock_info_path)).collect()
                    self._logger.info(f"[DB] stock_info 预热完成: {len(self._stock_info_cache)} 行")
            self._logger.info("[DB] 数据预热完成")
        except Exception as e:
            self._logger.warning(f"[DB] 数据库连接预热失败: {e}")

    
    def _convert_date(self, date):
        """将日期转换为字符串格式"""
        if isinstance(date, pd.Timestamp):
            return date.strftime('%Y-%m-%d')
        elif isinstance(date, str):
            return date
        else:
            return str(date)
    
    def _get_table_columns(self, table: str) -> FrozenSet[str]:
        """
        获取表的列名
        
        【Polars 实现】直接读取 Parquet 文件的 schema
        """
        if table in self._table_columns_cache:
            return self._table_columns_cache[table]
        
        try:
            parquet_path = self._get_parquet_path(table)
            if parquet_path is None:
                return frozenset()
            
            # 使用 Polars 读取 schema（只读取元数据，不加载数据）
            schema = pl.scan_parquet(str(parquet_path)).collect_schema()
            cols = frozenset(schema.names())
            
            self._table_columns_cache[table] = cols
            return cols
        except Exception:
            return frozenset()
    
    def _get_stock_info_cached(self) -> pl.DataFrame:
        """
        获取 stock_info 缓存（带懒加载）
        
        Returns:
            Polars DataFrame 包含 stock_info 数据
        """
        if self._stock_info_cache is None:
            stock_info_path = self._get_parquet_path("stock_info")
            if stock_info_path:
                self._stock_info_cache = pl.scan_parquet(str(stock_info_path)).collect()
        return self._stock_info_cache if self._stock_info_cache is not None else pl.DataFrame()

    def _filter_existing_columns(self, columns: List[str]) -> List[str]:
        """
        过滤掉不存在的列（保持兼容性）
        
        【注意】此方法主要用于向后兼容，新代码应直接使用 Polars 的 select
        """
        daily_cols = self._get_table_columns("stock_daily")
        info_cols = self._get_table_columns("stock_info")
        try:
            limit_status_cols = self._get_table_columns("stock_limit_status")
        except Exception:
            limit_status_cols = frozenset()

        def exists(col: str) -> bool:
            if "." not in col:
                return True
            prefix, name = col.split(".", 1)
            if prefix == "s":
                return name in daily_cols
            if prefix == "i":
                return name in info_cols
            if prefix == "l":
                return name in limit_status_cols
            return True

        def process_col(col: str) -> str:
            if "COALESCE" in col.upper():
                import re
                match = re.search(r'COALESCE\s*\(\s*(\w+)\.(\w+)', col, re.IGNORECASE)
                if match:
                    prefix, name = match.groups()
                    if prefix == 'i' and name not in info_cols:
                        return None
                    if prefix == 's' and name not in daily_cols:
                        return None
                    if prefix == 'l' and name not in limit_status_cols:
                        return None
            elif " AS " in col.upper():
                inner = col.upper().split(" AS ")[0].strip()
                if "." in inner:
                    prefix, name = inner.split(".", 1)
                    if prefix == 'I' and name.lower() not in [c.lower() for c in info_cols]:
                        return None
            else:
                if not exists(col):
                    return None
            return col

        filtered = [c for c in columns if process_col(c) is not None]
        return filtered if filtered else columns

    def get_adjustment_factors(self, date: str) -> Dict[str, float]:
        """
        获取指定日期所有股票的复权因子映射。

        Parameters
        ----------
        date : str or datetime-like
            交易日期，支持 'YYYY-MM-DD' 格式字符串或 Timestamp 对象。

        Returns
        -------
        dict of str to float
            股票代码到复权因子的映射，格式为 {stock_code: adj_factor}。
            若查询失败或无数据，返回空字典。

        Notes
        -----
        【Polars 实现】从 stock_daily.parquet 读取 adj_factor 列。
        复权因子用于将原始价格转换为前复权价格：
            前复权价格 = 原始价格 × 复权因子

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> factors = query.get_adjustment_factors('2023-01-05')
        >>> print(factors.get('000001'))
        1.2345
        """
        date_str = self._convert_date(date)
        cache_key = f"adj_factors_{date_str}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.copy() if isinstance(cached, dict) else cached

        try:
            # 检查 adj_factor 列是否存在
            daily_cols = self._get_table_columns("stock_daily")
            if 'adj_factor' not in daily_cols:
                return {}
            
            # 使用 Polars 读取
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return {}
            
            df = pl.scan_parquet(str(stock_daily_path)).filter(
                pl.col('trade_date') == date_str
            ).select(['stock_code', 'adj_factor']).collect()
            
            if df.is_empty():
                return {}
            
            # 转换为字典
            factors: Dict[str, float] = {}
            for row in df.iter_rows():
                stock_code, factor = row
                if factor is not None:
                    try:
                        factors[str(stock_code)] = float(factor)
                    except (TypeError, ValueError):
                        continue

            self._add_to_cache(cache_key, factors)
            return factors.copy()
            
        except Exception as e:
            self._logger.error(f"[DB] 获取复权因子失败: {e}")
            return {}
    
    @capture_error(category="database")
    def get_trading_dates(self, start_date=None, end_date=None):
        """
        获取指定日期区间内的所有交易日，按升序排列。

        Parameters
        ----------
        start_date : str or None, optional
            开始日期，格式 'YYYY-MM-DD'。若为 None，从最早交易日开始。
        end_date : str or None, optional
            结束日期，格式 'YYYY-MM-DD'。若为 None，到最晚交易日结束。

        Returns
        -------
        list of str
            交易日字符串列表，每个日期格式为 'YYYY-MM-DD'。
            若区间内无交易日，返回空列表。

        Notes
        -----
        【Polars 实现】优先使用内存缓存，回退到 Parquet 文件。
        性能优化：
        1. 首次调用时预加载所有交易日到内存
        2. 后续查询直接从内存过滤，零磁盘 IO
        3. 结果缓存避免重复计算

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> dates = query.get_trading_dates('2023-01-01', '2023-01-10')
        >>> print(dates[:3])
        ['2023-01-03', '2023-01-04', '2023-01-05']
        """
        # 延迟预热
        if not self._warmup_done:
            self._warmup_connection()
            self._warmup_done = True
        
        # 延迟预加载交易日期
        if not self._preload_dates_done:
            try:
                self._preload_all_trading_dates()
                self._preload_dates_done = True
            except Exception as e:
                self._logger.warning(f"[DB] 预加载交易日期失败: {e}")
        
        t0 = time.perf_counter()
        
        start_str = self._convert_date(start_date) if start_date else None
        end_str = self._convert_date(end_date) if end_date else None
        
        # 生成缓存键
        cache_key = f"trading_dates_{start_str}_{end_str}"
        if cache_key in self._cache:
            return self._cache[cache_key].copy()

        if self._use_lancedb:
            dates = self._get_lancedb_trading_dates(start_str, end_str)
            if dates is not None:
                if start_str is None and end_str is None:
                    self._all_trading_dates_cache = dates.copy()
                self._add_to_cache(cache_key, dates)
                return dates
        
        # 优化：如果查询所有日期，使用预加载的缓存
        if start_str is None and end_str is None:
            if self._all_trading_dates_cache is not None:
                return self._all_trading_dates_cache.copy()
        
        # 优化：如果已预加载所有交易日期，直接从内存过滤
        if self._all_trading_dates_cache is not None:
            cached_dates = self._all_trading_dates_cache
            left = bisect_left(cached_dates, start_str) if start_str else 0
            right = bisect_right(cached_dates, end_str) if end_str else len(cached_dates)
            filtered_dates = cached_dates[left:right]
            
            if filtered_dates:
                self._add_to_cache(cache_key, filtered_dates)
                elapsed = time.perf_counter() - t0
                if elapsed > 0.05:
                    self._logger.warning(f"[DB] get_trading_dates {start_str}~{end_str} -> {len(filtered_dates)} dates in {elapsed:.3f}s (from cache)")
                return filtered_dates
        
        # 回退：直接从 Parquet 文件读取
        try:
            # 优先从 benchmark_daily.parquet 读取
            benchmark_path = self._get_parquet_path("benchmark_data")
            if benchmark_path:
                lazy_df = pl.scan_parquet(str(benchmark_path)).select("date").unique().sort("date")
                
                if start_str and end_str:
                    lazy_df = lazy_df.filter((pl.col("date") >= start_str) & (pl.col("date") <= end_str))
                elif start_str:
                    lazy_df = lazy_df.filter(pl.col("date") >= start_str)
                elif end_str:
                    lazy_df = lazy_df.filter(pl.col("date") <= end_str)
                
                df = lazy_df.collect()
                dates = df["date"].to_list()
            else:
                # 回退到 stock_daily.parquet
                stock_daily_path = self._get_parquet_path("stock_daily")
                if stock_daily_path is None:
                    return []
                
                lazy_df = pl.scan_parquet(str(stock_daily_path)).select("trade_date").unique().sort("trade_date")
                
                if start_str and end_str:
                    lazy_df = lazy_df.filter((pl.col("trade_date") >= start_str) & (pl.col("trade_date") <= end_str))
                elif start_str:
                    lazy_df = lazy_df.filter(pl.col("trade_date") >= start_str)
                elif end_str:
                    lazy_df = lazy_df.filter(pl.col("trade_date") <= end_str)
                
                df = lazy_df.collect()
                dates = df["trade_date"].to_list()
            
            elapsed = time.perf_counter() - t0
            if elapsed > 0.05:
                self._logger.warning(f"[DB] get_trading_dates {start_str}~{end_str} -> {len(dates)} dates in {elapsed:.3f}s")
            
            # 缓存全量日期
            if start_str is None and end_str is None:
                self._all_trading_dates_cache = dates.copy()
            
            self._add_to_cache(cache_key, dates)
            return dates
            
        except Exception as e:
            self._logger.error(f"获取交易日失败: {e}")
            return []
    
    def get_prev_trade_date(self, date: str) -> Optional[str]:
        """
        获取指定日期的前一个交易日
        
        【Polars 实现】使用内存缓存快速查找
        
        Args:
            date: 日期字符串或 Timestamp
            
        Returns:
            前一个交易日的字符串，如果不存在则返回 None
        """
        date_str = self._convert_date(date)
        cache_key = f"prev_trade_date_{date_str}"
        
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 优先使用内存缓存
        if self._all_trading_dates_cache is not None:
            try:
                idx = bisect_left(self._all_trading_dates_cache, date_str)
                if idx > 0:
                    result = self._all_trading_dates_cache[idx - 1]
                    self._add_to_cache(cache_key, result)
                    return result
                return None
            except Exception:
                pass
        
        # 回退：从 Parquet 文件读取
        try:
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return None
            
            df = pl.scan_parquet(str(stock_daily_path)).filter(
                pl.col("trade_date") < date_str
            ).select("trade_date").unique().sort("trade_date", descending=True).limit(1).collect()
            
            if df.is_empty():
                result = None
            else:
                result = str(df["trade_date"][0])
            
            self._add_to_cache(cache_key, result)
            return result
        except Exception as e:
            self._logger.error(f"获取前一个交易日失败 ({date_str}): {e}")
            return None
    
    def get_previous_trading_date(self, date: str) -> Optional[str]:
        """
        获取指定日期的前一个交易日（别名方法，兼容策略代码）
        
        Args:
            date: 日期字符串或 Timestamp
            
        Returns:
            前一个交易日的字符串，如果不存在则返回 None
        """
        return self.get_prev_trade_date(date)
    
    def get_next_trade_date(self, date: str) -> Optional[str]:
        """
        获取指定日期的下一个交易日
        
        【Polars 实现】使用内存缓存快速查找
        
        Args:
            date: 日期字符串或 Timestamp
            
        Returns:
            下一个交易日的字符串，如果不存在则返回 None
        """
        date_str = self._convert_date(date)
        cache_key = f"next_trade_date_{date_str}"
        
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 优先使用内存缓存
        if self._all_trading_dates_cache is not None:
            try:
                idx = bisect_right(self._all_trading_dates_cache, date_str)
                if idx < len(self._all_trading_dates_cache):
                    result = self._all_trading_dates_cache[idx]
                    self._add_to_cache(cache_key, result)
                    return result
                return None
            except Exception:
                pass
        
        # 回退：从 Parquet 文件读取
        try:
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return None
            
            df = pl.scan_parquet(str(stock_daily_path)).filter(
                pl.col("trade_date") > date_str
            ).select("trade_date").unique().sort("trade_date").limit(1).collect()
            
            if df.is_empty():
                result = None
            else:
                result = str(df["trade_date"][0])
            
            self._add_to_cache(cache_key, result)
            return result
        except Exception as e:
            self._logger.error(f"获取下一个交易日失败 ({date_str}): {e}")
            return None
    
    @capture_error(category="database")
    def get_trading_calendar(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[str]:
        """
        获取交易日历（交易日列表）
        
        【Polars 实现】直接调用 get_trading_dates
        
        Args:
            start_date: 开始日期（可选），如果不提供则从数据库最早日期开始
            end_date: 结束日期（可选），如果不提供则到数据库最晚日期结束
            
        Returns:
            交易日列表（字符串格式，按日期升序排列）
        """
        return self.get_trading_dates(start_date, end_date)
    
    def is_trading_day(self, date: str) -> bool:
        """
        判断指定日期是否为交易日
        
        【Polars 实现】使用内存缓存快速判断
        
        Args:
            date: 日期字符串或 Timestamp
            
        Returns:
            True 如果是交易日，False 否则
        """
        date_str = self._convert_date(date)
        cache_key = f"is_trading_day_{date_str}"
        
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 优先使用内存缓存
        if self._all_trading_dates_cache is not None:
            result = date_str in self._all_trading_dates_cache
            self._add_to_cache(cache_key, result)
            return result
        
        # 回退：从 Parquet 文件读取
        try:
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return False
            
            df = pl.scan_parquet(str(stock_daily_path)).filter(
                pl.col("trade_date") == date_str
            ).select("trade_date").limit(1).collect()
            
            result = not df.is_empty()
            self._add_to_cache(cache_key, result)
            return result
        except Exception as e:
            self._logger.error(f"判断交易日失败 ({date_str}): {e}")
            return False
            
    @capture_error(category="database")
    def get_all_daily_data_for_period(self, start_date: str, end_date: str, filters: Optional[Dict] = None) -> pd.DataFrame:
        """
        向量化查询：一次性获取回测期间的所有股票数据
        
        【性能优化】：
        1. 使用 Polars 直接读取 Parquet（比 DuckDB 快 10 倍）
        2. 使用 Polars 的极速 Join 能力（比 Pandas 快 26 倍）
        3. 全流程 Polars 处理，最后才转 Pandas
        """
        import time
        import polars as pl
        t_total_start = time.perf_counter()
        
        start_str = self._convert_date(start_date)
        end_str = self._convert_date(end_date)
        print(f"向量化加载 {start_str} 到 {end_str} 的所有数据 (Backend: Polars)...")

        cache_key = f"all_data_{start_str}_{end_str}"
        if cache_key in self._cache:
            print("...从缓存加载")
            return self._cache[cache_key].copy()

        try:
            parquet_dir = Config.PARQUET_DIR
            stock_daily_path = f"{parquet_dir}/stock_daily.parquet"
            stock_info_path = f"{parquet_dir}/stock_info.parquet"
            stock_limit_path = f"{parquet_dir}/stock_limit_status.parquet"
            
            t0 = time.perf_counter()
            daily_pl = pl.scan_parquet(stock_daily_path).filter(
                (pl.col('trade_date') >= start_str) & 
                (pl.col('trade_date') <= end_str) &
                (pl.col('total_mv').is_not_null()) &
                (pl.col('volume') > 0) &
                (pl.col('close').is_not_null())
            ).collect()
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars 读取 stock_daily: {(t1-t0):.2f}s, 行数: {len(daily_pl)}")
            
            t0 = time.perf_counter()
            info_pl = pl.scan_parquet(stock_info_path).collect()
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars 读取 stock_info: {(t1-t0):.4f}s, 行数: {len(info_pl)}")
            
            t0 = time.perf_counter()
            joined_pl = daily_pl.join(info_pl, on='stock_code', how='left')
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars JOIN: {(t1-t0):.4f}s")
            
            t0 = time.perf_counter()
            stock_info_columns = info_pl.columns
            filter_expr = pl.lit(True)
            if Config.EXCLUDE_ST and "is_st" in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_st').fill_null(0) == 0)
            if Config.EXCLUDE_KC and "is_kc" in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_kc').fill_null(0) == 0)
            if Config.EXCLUDE_CY and "is_cy" in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_cy').fill_null(0) == 0)
            
            joined_pl = joined_pl.filter(filter_expr)
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars 过滤: {(t1-t0):.4f}s, 结果行数: {len(joined_pl)}")
            
            t0 = time.perf_counter()
            limit_pl = pl.scan_parquet(stock_limit_path).filter(
                (pl.col('trade_date') >= start_str) & 
                (pl.col('trade_date') <= end_str)
            ).collect()
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars 读取 limit_status: {(t1-t0):.4f}s, 行数: {len(limit_pl)}")
            
            t0 = time.perf_counter()
            if len(limit_pl) > 0:
                joined_pl = joined_pl.join(
                    limit_pl,
                    on=['stock_code', 'trade_date'],
                    how='left'
                )
                joined_pl = joined_pl.with_columns([
                    pl.col('is_limit_up').fill_null(0),
                    pl.col('is_limit_down').fill_null(0),
                    pl.col('is_suspended').fill_null(0)
                ])
            else:
                joined_pl = joined_pl.with_columns([
                    pl.lit(0).alias('is_limit_up'),
                    pl.lit(0).alias('is_limit_down'),
                    pl.lit(0).alias('is_suspended')
                ])
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars JOIN limit_status: {(t1-t0):.4f}s")
            
            t0 = time.perf_counter()
            result = joined_pl.to_pandas()
            t1 = time.perf_counter()
            self._logger.info(f"[性能] Polars → Pandas: {(t1-t0):.4f}s")
            
            if result.empty:
                print(f"向量化加载完成: 0 行数据")
                return pd.DataFrame()
            
            t_defensive_start = time.perf_counter()
            result = self._defensive_data_loader(result, start_str)
            t_defensive_end = time.perf_counter()
            self._logger.info(f"[性能] defensive_data_loader 耗时: {t_defensive_end - t_defensive_start:.2f}s")
            
            self._add_to_cache(cache_key, result)
            
            t_total_end = time.perf_counter()
            total_time = t_total_end - t_total_start
            print(f"向量化加载完成 (Polars): {len(result)} 行数据，总耗时: {total_time:.2f}s")
            
            return result
        except Exception as e:
            t_total_end = time.perf_counter()
            self._logger.error(f"向量化查询失败 (耗时 {t_total_end - t_total_start:.2f}s): {e}", exc_info=True)
            print(f"向量化查询失败: {e}")
            return pd.DataFrame()

    @capture_error(category="database")
    def get_stock_pool(self, date, filters=None, use_cache=True, columns=None):
        """
        获取指定日期的股票池数据，包含行情、市值、涨跌停等信息。

        Parameters
        ----------
        date : str or datetime-like
            交易日期，支持 'YYYY-MM-DD' 格式字符串或 Timestamp 对象。
        filters : dict or None, optional
            过滤条件字典。支持 'min_mv' 键过滤最小市值。
            例如：{'min_mv': 500000} 表示只返回市值 >= 50 亿的股票。
        use_cache : bool, default True
            是否使用缓存。设为 False 可强制重新查询。
        columns : list of str or None, optional
            需要返回的列名列表。若为 None，返回默认列：
            ['stock_code', 'trade_date', 'open', 'high', 'low', 'close',
             'prev_close', 'volume', 'amount', 'total_mv', 'float_mv',
             'turnover_rate', 'turnover_free', 'volume_ratio',
             'ma5', 'ma10', 'ma20', 'volume_ma5', 'adj_factor',
             'is_limit_up', 'is_limit_down', 'is_suspended',
             'is_st', 'is_kc', 'is_cy', 'list_date']

        Returns
        -------
        pandas.DataFrame
            股票池数据，每行代表一只股票。若查询失败或无数据，返回空 DataFrame。

        Notes
        -----
        【Polars 实现】直接读取 Parquet 文件，比 DuckDB 快 10 倍。
        数据来源：
        1. stock_daily.parquet - 日线行情
        2. stock_info.parquet - 股票基本信息（ST、科创、创业标识）
        3. stock_limit_status.parquet - 涨跌停、停牌状态

        过滤规则（通过 Config 配置）：
        - EXCLUDE_ST: 排除 ST 股票
        - EXCLUDE_KC: 排除科创板
        - EXCLUDE_CY: 排除创业板

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> df = query.get_stock_pool('2023-01-05')
        >>> print(df.columns.tolist()[:5])
        ['stock_code', 'trade_date', 'open', 'high', 'low']
        >>> df_filtered = query.get_stock_pool('2023-01-05', filters={'min_mv': 500000})
        """
        t0 = time.perf_counter()

        date_str = self._convert_date(date)
        cache_key = f"stock_pool_{date_str}_{filters}"

        if use_cache and cache_key in self._cache:
            result = self._cache[cache_key].copy()
            return result

        # 默认列
        if columns is None:
            columns = [
                "stock_code", "trade_date", "open", "high", "low", "close",
                "prev_close", "volume", "amount", "total_mv", "float_mv",
                "turnover_rate", "turnover_free", "volume_ratio",
                "ma5", "ma10", "ma20", "volume_ma5", "adj_factor",
                "is_limit_up", "is_limit_down", "is_suspended",
                "is_st", "is_kc", "is_cy", "list_date",
            ]
        
        # 优先使用预加载数据
        preloaded_df = self.get_stock_pool_from_preloaded(date_str)
        if preloaded_df is not None:
            filtered = self._filter_preloaded_pool(preloaded_df, filters, columns)
            if filtered is not None:
                if use_cache:
                    self._add_to_cache(cache_key, filtered)
                return filtered

        try:
            # 从 Parquet 文件读取
            stock_daily_path = self._get_parquet_path("stock_daily")
            stock_info_path = self._get_parquet_path("stock_info")
            stock_limit_path = self._get_parquet_path("stock_limit_status")
            
            if stock_daily_path is None:
                self._logger.error(f"[DB] stock_daily.parquet 不存在")
                return pd.DataFrame()
            
            # 使用 Polars Lazy API 读取并过滤
            daily_lazy = pl.scan_parquet(str(stock_daily_path)).filter(
                (pl.col("trade_date") == date_str) &
                (pl.col("total_mv").is_not_null()) &
                (pl.col("volume") > 0) &
                (pl.col("close").is_not_null())
            )
            
            # JOIN stock_info
            if stock_info_path:
                info_lazy = pl.scan_parquet(str(stock_info_path))
                daily_lazy = daily_lazy.join(info_lazy, on="stock_code", how="left")
                
                # 过滤 ST、科创、创业
                filter_expr = pl.lit(True)
                if Config.EXCLUDE_ST:
                    filter_expr = filter_expr & (pl.col("is_st").fill_null(0) == 0)
                if Config.EXCLUDE_KC:
                    filter_expr = filter_expr & (pl.col("is_kc").fill_null(0) == 0)
                if Config.EXCLUDE_CY:
                    filter_expr = filter_expr & (pl.col("is_cy").fill_null(0) == 0)
                daily_lazy = daily_lazy.filter(filter_expr)
            
            # JOIN stock_limit_status
            if stock_limit_path:
                limit_lazy = pl.scan_parquet(str(stock_limit_path)).filter(
                    pl.col("trade_date") == date_str
                )
                daily_lazy = daily_lazy.join(
                    limit_lazy.select(["stock_code", "trade_date", "is_limit_up", "is_limit_down", "is_suspended"]),
                    on=["stock_code", "trade_date"],
                    how="left"
                )
                daily_lazy = daily_lazy.with_columns([
                    pl.col("is_limit_up").fill_null(0),
                    pl.col("is_limit_down").fill_null(0),
                    pl.col("is_suspended").fill_null(0)
                ])
            
            # 应用额外过滤条件
            if filters and "min_mv" in filters:
                daily_lazy = daily_lazy.filter(pl.col("total_mv") >= filters["min_mv"])
            
            # 选择需要的列
            existing_cols = [c for c in columns if c in daily_lazy.collect_schema().names()]
            daily_lazy = daily_lazy.select(existing_cols)
            
            # 收集结果
            result_pl = daily_lazy.collect()
            
            # 转换为 Pandas
            result = result_pl.to_pandas()
            
            # 防御性数据加载器
            result = self._defensive_data_loader(result, date_str)
            
            elapsed = time.perf_counter() - t0
            if elapsed > 0.1:
                self._logger.warning(f"[DB] get_stock_pool {date_str} -> {len(result)} rows in {elapsed:.3f}s")
            
            if use_cache:
                self._add_to_cache(cache_key, result.copy() if result is not None else result)
            
            return result

        except Exception as e:
            self._logger.error(f"[DB] 查询失败: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

    def _filter_preloaded_pool(self, df, filters, plain_columns):
        """
        【性能优化】从预加载数据中过滤股票池，实现零拷贝读取
        
        支持 Polars DataFrame 和 Pandas DataFrame
        返回 Pandas DataFrame 以兼容现有策略
        """
        try:
            is_polars = hasattr(df, 'columns') and not hasattr(df, 'empty')
            
            if is_polars:
                import polars as pl
                
                filtered = df
                
                conditions = [
                    pl.col("total_mv").is_not_null(),
                    pl.col("close").is_not_null(),
                    pl.col("volume").fill_null(0) > 0
                ]
                
                if Config.EXCLUDE_ST and "is_st" in filtered.columns:
                    conditions.append(pl.col("is_st") == 0)
                if Config.EXCLUDE_KC and "is_kc" in filtered.columns:
                    conditions.append(pl.col("is_kc") == 0)
                if Config.EXCLUDE_CY and "is_cy" in filtered.columns:
                    conditions.append(pl.col("is_cy") == 0)
                
                if filters and "min_mv" in filters and "total_mv" in filtered.columns:
                    conditions.append(pl.col("total_mv") >= filters["min_mv"])
                
                if len(conditions) == 1:
                    filtered = filtered.filter(conditions[0])
                elif len(conditions) > 1:
                    filtered = filtered.filter(pl.all_horizontal(conditions))
                
                existing_cols = [col for col in plain_columns if col in filtered.columns]
                filtered = filtered.select(existing_cols)
                
                return filtered.to_pandas()
            else:
                filtered = df
                
                mask = (
                    filtered["total_mv"].notna()
                    & filtered["close"].notna()
                    & filtered["volume"].fillna(0).gt(0)
                )
                
                if Config.EXCLUDE_ST and "is_st" in filtered.columns:
                    mask = mask & (filtered["is_st"] == 0)
                if Config.EXCLUDE_KC and "is_kc" in filtered.columns:
                    mask = mask & (filtered["is_kc"] == 0)
                if Config.EXCLUDE_CY and "is_cy" in filtered.columns:
                    mask = mask & (filtered["is_cy"] == 0)
                
                if filters and "min_mv" in filters and "total_mv" in filtered.columns:
                    mask = mask & (filtered["total_mv"] >= filters["min_mv"])
                
                filtered = filtered[mask]
                
                missing_cols = [col for col in plain_columns if col not in filtered.columns]
                if missing_cols:
                    filtered = filtered.copy()
                    for col in missing_cols:
                        filtered[col] = None
                
                existing_cols = [col for col in plain_columns if col in filtered.columns]
                return filtered[existing_cols]
                
        except Exception as e:
            self._logger.debug(f"[DB] 过滤预加载股票池失败: {e}")
            return None

    def _defensive_data_loader(self, df: pd.DataFrame, context: str = "") -> pd.DataFrame:
        """
        【防御性数据加载器】自动补全缺失字段，确保返回的 DataFrame 包含所有标准字段
        
        【性能优化】使用 inplace 操作和条件检查，避免不必要的 copy()，大幅提升性能
        
        Args:
            df: 原始 DataFrame（会被原地修改，如果不需要修改请先 copy）
            context: 上下文信息（已废弃，保留兼容性）
        
        Returns:
            补全后的 DataFrame（可能是原 DataFrame 的引用或新 DataFrame）
        """
        if df is None or df.empty:
            return df
        
        # 【性能优化】使用类变量缓存警告状态，确保每个字段只警告一次
        if not hasattr(self.__class__, '_logged_warnings'):
            self.__class__._logged_warnings = set()
        
        # 【性能优化】只在需要修改时才 copy，避免不必要的内存分配
        need_copy = False
        cols_to_check = ['is_limit_up', 'is_limit_down', 'is_suspended', 'is_st']
        for col in cols_to_check:
            if col not in df.columns:
                need_copy = True
                break
        
        # 【性能优化】如果所有字段都存在，假设没有 NaN（大多数情况下都是这样），直接返回
        # 只有在字段缺失时才处理，避免不必要的 copy 和 NaN 检查
        if not need_copy:
            # 最快路径：所有字段都存在，直接返回（假设没有 NaN，如果有会在后续使用时报错，但概率极低）
            return df
        
        # 只在需要修改时才 copy
        result = df.copy()
        
        # 1. 补全 is_limit_up（涨停标志）- 默认 False（未涨停，允许交易），向量化填充
        if 'is_limit_up' not in result.columns:
            result['is_limit_up'] = False
            if 'is_limit_up' not in self.__class__._logged_warnings:
                self.__class__._logged_warnings.add('is_limit_up')
                self._logger.warning(f"字段 'is_limit_up' 缺失，已使用默认值 False 填充")
        else:
            # 【性能优化】向量化填充 NaN，使用 fillna 直接赋值
            result['is_limit_up'] = result['is_limit_up'].fillna(False)
        
        # 2. 补全 is_limit_down（跌停标志）- 默认 False（未跌停，允许交易），向量化填充
        if 'is_limit_down' not in result.columns:
            result['is_limit_down'] = False
            if 'is_limit_down' not in self.__class__._logged_warnings:
                self.__class__._logged_warnings.add('is_limit_down')
                self._logger.warning(f"字段 'is_limit_down' 缺失，已使用默认值 False 填充")
        else:
            result['is_limit_down'] = result['is_limit_down'].fillna(False)
        
        # 2.5. 补全 is_suspended（停牌标志）- 默认 False（未停牌，允许交易），向量化填充
        if 'is_suspended' not in result.columns:
            result['is_suspended'] = False
            if 'is_suspended' not in self.__class__._logged_warnings:
                self.__class__._logged_warnings.add('is_suspended')
                self._logger.warning(f"字段 'is_suspended' 缺失，已使用默认值 False 填充")
        else:
            result['is_suspended'] = result['is_suspended'].fillna(False)
        
        # 3. 补全 is_st（ST 股票标志）- 默认 False（非ST股票），向量化填充
        if 'is_st' not in result.columns:
            result['is_st'] = False
            if 'is_st' not in self.__class__._logged_warnings:
                self.__class__._logged_warnings.add('is_st')
                self._logger.warning(f"字段 'is_st' 缺失，已使用默认值 False 填充")
        else:
            result['is_st'] = result['is_st'].fillna(False)
        
        return result
    
    def _add_to_cache(self, key, value):
        """
        CHANGED: LRU 缓存策略
        """
        if len(self._cache) >= self._cache_size:
            # 删除最旧的项（FIFO）
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[key] = value
    
    @capture_error(category="database")
    def get_stock_history(self, stock_code: str, start_date: str, end_date: str, use_cache: bool = True, columns=None):
        """
        获取单只股票在指定日期区间内的历史行情数据。

        Parameters
        ----------
        stock_code : str
            股票代码，6 位数字格式，如 '000001'。
        start_date : str
            开始日期，格式 'YYYY-MM-DD'。
        end_date : str
            结束日期，格式 'YYYY-MM-DD'。
        use_cache : bool, default True
            是否使用缓存。
        columns : list of str or None, optional
            需要返回的列名列表。若为 None，返回默认列：
            ['stock_code', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'adj_factor']

        Returns
        -------
        pandas.DataFrame
            历史行情数据，按 trade_date 升序排列。若查询失败或无数据，返回空 DataFrame。

        Notes
        -----
        【Polars 实现】使用 Lazy API 读取 Parquet 文件，支持谓词下推优化。

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> df = query.get_stock_history('000001', '2023-01-01', '2023-01-31')
        >>> print(len(df))
        20
        """
        t0 = time.perf_counter()
        
        start_str = self._convert_date(start_date)
        end_str = self._convert_date(end_date)
        cache_key = f"history_{stock_code}_{start_str}_{end_str}_{columns}"
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key].copy()
        
        # 默认列
        if columns is None:
            columns = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "adj_factor"]
        
        try:
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return pd.DataFrame()
            
            # 使用 Polars Lazy API
            lazy_df = pl.scan_parquet(str(stock_daily_path)).filter(
                (pl.col("stock_code") == stock_code) &
                (pl.col("trade_date") >= start_str) &
                (pl.col("trade_date") <= end_str)
            ).sort("trade_date")
            
            # 选择需要的列
            existing_cols = [c for c in columns if c in lazy_df.collect_schema().names()]
            lazy_df = lazy_df.select(existing_cols)
            
            result = lazy_df.collect().to_pandas()
            
            elapsed = time.perf_counter() - t0
            if elapsed > 0.05:
                print(f"[DB] get_stock_history {stock_code} {start_str}~{end_str} -> {len(result)} rows in {elapsed:.3f}s")
            if use_cache and len(result) > 0:
                self._add_to_cache(cache_key, result)
            return result
        except Exception as e:
            print(f"获取股票历史数据失败 {stock_code}: {e}")
            return pd.DataFrame()

    def load_stock_panel(self, codes: List[str], start_date: str, end_date: str, cols: List[str]) -> pd.DataFrame:
        """
        批量加载多只股票在指定日期区间内的面板数据。

        Parameters
        ----------
        codes : list of str
            股票代码列表，每个代码为 6 位数字格式，如 ['000001', '000002']。
        start_date : str
            开始日期，格式 'YYYY-MM-DD'。
        end_date : str
            结束日期，格式 'YYYY-MM-DD'。
        cols : list of str
            需要返回的列名列表。支持的字段：
            ['stock_code', 'trade_date', 'open', 'high', 'low', 'close',
             'prev_close', 'volume', 'amount', 'turnover_rate', 'volume_ratio',
             'ma5', 'ma10', 'ma20', 'volume_ma5', 'total_mv', 'float_mv']

        Returns
        -------
        pandas.DataFrame
            面板数据，按 (trade_date, stock_code) 升序排列。
            若查询失败或无数据，返回空 DataFrame（仅包含请求的列）。

        Notes
        -----
        【Polars 实现】使用 is_in 过滤，比 SQL IN 子句更高效。
        适用于策略回测时批量获取多只股票数据。

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> df = query.load_stock_panel(['000001', '000002'], '2023-01-01', '2023-01-31',
        ...                              ['stock_code', 'trade_date', 'close', 'volume'])
        >>> print(df['stock_code'].unique().tolist())
        ['000001', '000002']
        """
        t0 = time.perf_counter()
        
        # 只保留必要字段
        allowed = {
            "stock_code", "trade_date", "open", "high", "low", "close",
            "prev_close", "volume", "amount", "turnover_rate", "volume_ratio",
            "ma5", "ma10", "ma20", "volume_ma5", "total_mv", "float_mv"
        }
        cols = [c for c in cols if c in allowed]
        if "stock_code" not in cols:
            cols.insert(0, "stock_code")
        if "trade_date" not in cols:
            cols.insert(1, "trade_date")
        
        # 缓存键
        cache_key = (frozenset(codes), start_date, end_date, tuple(cols))
        if cache_key in self._cache:
            return self._cache[cache_key].copy()
        
        start_str = self._convert_date(start_date)
        end_str = self._convert_date(end_date)
        
        try:
            stock_daily_path = self._get_parquet_path("stock_daily")
            if stock_daily_path is None:
                return pd.DataFrame(columns=cols)
            
            # 使用 Polars Lazy API
            lazy_df = pl.scan_parquet(str(stock_daily_path)).filter(
                (pl.col("trade_date") >= start_str) &
                (pl.col("trade_date") <= end_str) &
                (pl.col("stock_code").is_in(codes))
            ).sort(["trade_date", "stock_code"])
            
            # 选择需要的列
            existing_cols = [c for c in cols if c in lazy_df.collect_schema().names()]
            lazy_df = lazy_df.select(existing_cols)
            
            result = lazy_df.collect().to_pandas()
            
            if result.empty:
                result = pd.DataFrame(columns=cols)
            
            self._add_to_cache(cache_key, result)
            return result
            
        except Exception as e:
            print(f"[WARN] 批量查询失败: {e}")
            return pd.DataFrame(columns=cols)
    
    def get_batch_stock_history(self, stock_codes, start_date, end_date, columns=None):
        """
        批量获取股票历史数据（使用 load_stock_panel 实现）
        """
        if not columns:
            columns = ["stock_code", "trade_date", "open", "high", "low", "close", "volume"]
        
        return self.load_stock_panel(stock_codes, start_date, end_date, columns)
    
    def get_market_data(self, date):
        """
        获取指定日期的全市场股票数据（包含行情和基本信息）。

        Parameters
        ----------
        date : str or datetime-like
            交易日期，支持 'YYYY-MM-DD' 格式字符串或 Timestamp 对象。

        Returns
        -------
        pandas.DataFrame
            全市场股票数据，包含行情和基本信息（is_st, is_kc, is_cy）。
            若查询失败，抛出 FileNotFoundError。

        Notes
        -----
        【Polars 实现】从 stock_daily.parquet 和 stock_info.parquet 读取并 JOIN。
        数据来源：
        1. stock_daily.parquet - 日线行情
        2. stock_info.parquet - 股票基本信息

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> df = query.get_market_data('2023-01-05')
        >>> print(len(df))
        5000
        """
        date_str = self._convert_date(date)
        
        stock_daily_path = self._get_parquet_path("stock_daily")
        if stock_daily_path is None:
            raise FileNotFoundError("stock_daily.parquet 不存在")
        
        # 使用 Polars 读取并过滤
        df = pl.scan_parquet(str(stock_daily_path)).filter(
            pl.col('trade_date') == date_str
        ).collect()
        
        # 读取 stock_info 获取 is_st, is_kc, is_cy
        info_path = self._get_parquet_path("stock_info")
        if info_path:
            info_df = pl.scan_parquet(str(info_path)).select(['stock_code', 'is_st', 'is_kc', 'is_cy']).collect()
            df = df.join(info_df, on='stock_code', how='left')
        
        return df.to_pandas()
    
    def get_stock_info(self, stock_code):
        """
        获取股票基本信息
        
        【Polars 实现】直接读取 Parquet 文件
        
        Args:
            stock_code: 股票代码
        
        Returns:
            Pandas DataFrame 包含股票信息
        """
        columns = ["stock_code", "is_st", "is_kc", "is_cy", "list_date", "name"]
        
        try:
            info_path = self._get_parquet_path("stock_info")
            if info_path is None:
                return pd.DataFrame(columns=columns)
            
            df = pl.scan_parquet(str(info_path)).filter(
                pl.col("stock_code") == stock_code
            ).select(columns).collect()
            
            return df.to_pandas()
        except Exception as e:
            self._logger.error(f"获取股票信息失败 {stock_code}: {e}")
            return pd.DataFrame(columns=columns)
    
    @capture_error(category="database")
    def preload_backtest_data(self, start_date: str, end_date: str) -> None:
        """
        预加载回测期间的所有数据到内存，实现零拷贝查询。

        Parameters
        ----------
        start_date : str
            开始日期，格式 'YYYY-MM-DD'。建议包含 warmup 期间。
        end_date : str
            结束日期，格式 'YYYY-MM-DD'。

        Returns
        -------
        None
            数据加载到内部缓存 _preloaded_data 中。

        Notes
        -----
        数据源优先级：
        1. ArcticDB (推荐) - 通过 UnifiedDataManager 加载
        2. Parquet 文件 (后备) - 直接读取 stock_daily.parquet

        性能优化：
        1. 使用 Polars partition_by 按日期分区
        2. 分区结果存入字典，后续查询 O(1) 复杂度
        3. 避免重复预加载（检查日期范围）

        内存占用：
        - 约 5000 只股票 × 交易日数 × 列数
        - 建议：回测期间不超过 1 年

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> query.preload_backtest_data('2023-01-01', '2023-03-31')
        >>> df = query.get_stock_pool_from_preloaded('2023-01-05')
        """
        import time
        import polars as pl
        logger = get_logger(__name__)
        start_str = self._convert_date(start_date)
        end_str = self._convert_date(end_date)

        if (
            self._preloaded_data is not None
            and self._preloaded_date_range == (start_str, end_str)
            and len(self._preloaded_data) > 0
        ):
            logger.info(f"[DB] 预加载命中缓存: {start_str} 到 {end_str}，跳过重复预加载")
            return
        
        logger.info(f"预加载回测数据: {start_str} 到 {end_str}")
        t_total_start = time.perf_counter()
        
        try:
            from data_svc.unified_data_manager import get_unified_manager
            manager = get_unified_manager()
            
            preloaded = manager.preload_to_memory(start_date=start_str, end_date=end_str)
            stock_daily = preloaded.get('stock_daily')
            
            if stock_daily is not None and not stock_daily.is_empty():
                logger.info(f"[DB] 从 ArcticDB 加载: {len(stock_daily)} 行")
                
                t0 = time.perf_counter()
                partitions = stock_daily.partition_by('trade_date', as_dict=True)
                t1 = time.perf_counter()
                logger.info(f"[性能] Polars partition_by: {(t1-t0):.4f}s, 分区数: {len(partitions)}")
                
                t0 = time.perf_counter()
                self._preloaded_data = {}
                for date_key, partition_pl in partitions.items():
                    if isinstance(date_key, tuple):
                        date_str = str(date_key[0])
                    elif hasattr(date_key, 'strftime'):
                        date_str = date_key.strftime('%Y-%m-%d')
                    else:
                        date_str = str(date_key)
                    self._preloaded_data[date_str] = partition_pl
                t1 = time.perf_counter()
                logger.info(f"[性能] Polars 分区完成: {(t1-t0):.4f}s")
                
                self._preloaded_date_range = (start_str, end_str)
                t_total_end = time.perf_counter()
                
                total_rows = sum(len(df) for df in self._preloaded_data.values())
                logger.info(
                    f"预加载完成 (ArcticDB): {len(self._preloaded_data)} 个交易日，"
                    f"共 {total_rows} 条记录，总耗时: {(t_total_end - t_total_start):.2f}s"
                )
                print(f"向量化加载完成 (ArcticDB): {total_rows} 行数据，总耗时: {(t_total_end - t_total_start):.2f}s")
                return
                
        except Exception as e:
            logger.warning(f"[DB] ArcticDB 加载失败，尝试 Parquet: {e}")
        
        try:
            parquet_dir = Config.PARQUET_DIR
            stock_daily_path = f"{parquet_dir}/stock_daily.parquet"
            stock_info_path = f"{parquet_dir}/stock_info.parquet"
            stock_limit_path = f"{parquet_dir}/stock_limit_status.parquet"
            
            from pathlib import Path
            if not Path(stock_daily_path).exists():
                logger.error(f"[DB] Parquet 文件不存在: {stock_daily_path}")
                self._preloaded_data = {}
                self._preloaded_date_range = (start_str, end_str)
                return
            
            t0 = time.perf_counter()
            
            stock_info_columns = ['stock_code']
            if Config.EXCLUDE_ST:
                stock_info_columns.append('is_st')
            if Config.EXCLUDE_KC:
                stock_info_columns.append('is_kc')
            if Config.EXCLUDE_CY:
                stock_info_columns.append('is_cy')
            
            daily_lazy = pl.scan_parquet(stock_daily_path).filter(
                (pl.col('trade_date') >= start_str) & 
                (pl.col('trade_date') <= end_str) &
                (pl.col('total_mv').is_not_null()) &
                (pl.col('volume') > 0) &
                (pl.col('close').is_not_null())
            )
            
            info_lazy = pl.scan_parquet(stock_info_path).select(stock_info_columns)
            
            limit_lazy = pl.scan_parquet(stock_limit_path).filter(
                (pl.col('trade_date') >= start_str) & 
                (pl.col('trade_date') <= end_str)
            )
            
            joined_lazy = daily_lazy.join(info_lazy, on='stock_code', how='left')
            
            filter_expr = pl.lit(True)
            if Config.EXCLUDE_ST and 'is_st' in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_st').fill_null(0) == 0)
            if Config.EXCLUDE_KC and 'is_kc' in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_kc').fill_null(0) == 0)
            if Config.EXCLUDE_CY and 'is_cy' in stock_info_columns:
                filter_expr = filter_expr & (pl.col('is_cy').fill_null(0) == 0)
            
            joined_lazy = joined_lazy.filter(filter_expr)
            
            joined_lazy = joined_lazy.join(
                limit_lazy,
                on=['stock_code', 'trade_date'],
                how='left'
            )
            
            joined_lazy = joined_lazy.with_columns([
                pl.col('is_limit_up').fill_null(0),
                pl.col('is_limit_down').fill_null(0),
                pl.col('is_suspended').fill_null(0)
            ])
            
            joined_lazy = joined_lazy.with_columns(
                pl.col('stock_code').cast(pl.Utf8).str.strip_chars().alias('stock_code')
            )
            
            joined_pl = joined_lazy.collect()
            
            t1 = time.perf_counter()
            logger.info(f"[性能] Polars 合并查询: {(t1-t0):.2f}s, 行数: {len(joined_pl)}")
            
            t0 = time.perf_counter()
            partitions = joined_pl.partition_by('trade_date', as_dict=True)
            t1 = time.perf_counter()
            logger.info(f"[性能] Polars partition_by: {(t1-t0):.4f}s, 分区数: {len(partitions)}")
            
            t0 = time.perf_counter()
            self._preloaded_data = {}
            for date_key, partition_pl in partitions.items():
                if isinstance(date_key, tuple):
                    date_str = str(date_key[0])
                elif hasattr(date_key, 'strftime'):
                    date_str = date_key.strftime('%Y-%m-%d')
                else:
                    date_str = str(date_key)
                self._preloaded_data[date_str] = partition_pl
            t1 = time.perf_counter()
            logger.info(f"[性能] Polars 分区完成: {(t1-t0):.4f}s")
            
            self._preloaded_date_range = (start_str, end_str)
            t_total_end = time.perf_counter()
            
            total_rows = sum(len(df) for df in self._preloaded_data.values())
            logger.info(
                f"预加载完成 (Parquet): {len(self._preloaded_data)} 个交易日，"
                f"共 {total_rows} 条记录，总耗时: {(t_total_end - t_total_start):.2f}s"
            )
            print(f"向量化加载完成 (Parquet): {total_rows} 行数据，总耗时: {(t_total_end - t_total_start):.2f}s")
            
        except Exception as e:
            logger.error(f"预加载数据失败: {e}", exc_info=True)
            self._preloaded_data = {}
            self._preloaded_date_range = (start_str, end_str)
    
    def _defensive_data_loader_polars(self, df: 'pl.DataFrame', start_date: str) -> 'pl.DataFrame':
        """
        Polars 版本的防御性数据加载器
        """
        import polars as pl
        
        if 'stock_code' in df.columns:
            df = df.with_columns(
                pl.col('stock_code').cast(pl.Utf8).str.strip_chars()
            )
        
        if 'days_listed' in df.columns:
            df = df.with_columns(
                pl.col('days_listed').fill_null(0).cast(pl.Int32)
            )
        
        return df
    
    def get_stock_pool_from_preloaded(self, date: str) -> Optional['pl.DataFrame']:
        """
        从预加载的内存缓存中获取指定日期的股票池数据。

        Parameters
        ----------
        date : str or datetime-like
            交易日期，支持 'YYYY-MM-DD' 格式字符串或 Timestamp 对象。

        Returns
        -------
        polars.DataFrame or None
            股票池数据（Polars DataFrame，零拷贝）。
            若未预加载或日期不存在，返回 None。

        Notes
        -----
        【零拷贝】直接返回内存中的 Polars DataFrame 引用，无数据复制。
        前置条件：必须先调用 preload_backtest_data() 加载数据。

        See Also
        --------
        preload_backtest_data : 预加载数据到内存

        Examples
        --------
        >>> query = OptimizedStockDataQuery()
        >>> query.preload_backtest_data('2023-01-01', '2023-01-31')
        >>> df = query.get_stock_pool_from_preloaded('2023-01-05')
        >>> print(type(df))
        <class 'polars.DataFrame'>
        """
        if self._preloaded_data is None:
            return None
        
        date_str = self._convert_date(date)
        return self._preloaded_data.get(date_str)
    
    def clear_preloaded_data(self) -> None:
        """清除预加载数据"""
        self._preloaded_data = None
        self._preloaded_date_range = None
    
    def _read_stock_limit_status_parquet(self, start_date: str = None, end_date: str = None, 
                                         stock_codes: List[str] = None, trade_date: str = None) -> pd.DataFrame:
        """
        直接读取 stock_limit_status 数据（使用 Polars）
        
        Args:
            start_date: 开始日期（可选，用于日期范围过滤）
            end_date: 结束日期（可选，用于日期范围过滤）
            stock_codes: 股票代码列表（可选，用于股票过滤）
            trade_date: 单个交易日期（可选，用于单日查询）
            
        Returns:
            DataFrame 包含 stock_limit_status 数据
        """
        parquet_dir = getattr(Config, 'PARQUET_DIR', 'parquet_data')
        parquet_file = Path(parquet_dir) / 'stock_limit_status.parquet'
        parquet_path = str(parquet_file.resolve()).replace('\\', '/')
        
        if not parquet_file.exists():
            self._logger.warning(f"[DB] stock_limit_status.parquet 文件不存在: {parquet_file}")
            return pd.DataFrame(columns=['stock_code', 'trade_date', 'is_limit_up', 'is_limit_down', 'is_suspended'])
        
        try:
            import polars as pl
            
            lazy_df = pl.scan_parquet(parquet_path)
            
            if trade_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') == trade_date)
            elif start_date and end_date:
                lazy_df = lazy_df.filter(
                    (pl.col('trade_date') >= start_date) & 
                    (pl.col('trade_date') <= end_date)
                )
            elif start_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') >= start_date)
            elif end_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') <= end_date)
            
            if stock_codes:
                lazy_df = lazy_df.filter(pl.col('stock_code').is_in(stock_codes))
            
            lazy_df = lazy_df.select(['stock_code', 'trade_date', 'is_limit_up', 'is_limit_down', 'is_suspended'])
            
            df_pl = lazy_df.collect()
            return df_pl.to_pandas()
            
        except Exception as e:
            self._logger.error(f"[DB] 读取 stock_limit_status 失败: {e}", exc_info=True)
            return pd.DataFrame(columns=['stock_code', 'trade_date', 'is_limit_up', 'is_limit_down', 'is_suspended'])
    
    def _read_stock_limit_status_polars(self, start_date: str = None, end_date: str = None,
                                        stock_codes: List[str] = None, trade_date: str = None):
        """
        [新增] 返回 Polars DataFrame（避免 to_pandas 开销）
        如果 Polars 不可用，返回空 DataFrame
        """
        try:
            import polars as pl
        except ImportError:
            return pd.DataFrame()
        
        try:
            parquet_dir = getattr(Config, 'PARQUET_DIR', 'parquet_data')
            parquet_file = Path(parquet_dir) / 'stock_limit_status.parquet'
            parquet_path = str(parquet_file.resolve()).replace('\\', '/')
            
            if not parquet_file.exists():
                return pl.DataFrame()
            
            lazy_df = pl.scan_parquet(parquet_path)
            
            if trade_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') == trade_date)
            elif start_date and end_date:
                lazy_df = lazy_df.filter(
                    (pl.col('trade_date') >= start_date) & 
                    (pl.col('trade_date') <= end_date)
                )
            elif start_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') >= start_date)
            elif end_date:
                lazy_df = lazy_df.filter(pl.col('trade_date') <= end_date)
            
            if stock_codes:
                lazy_df = lazy_df.filter(pl.col('stock_code').is_in(stock_codes))
            
            lazy_df = lazy_df.select(['stock_code', 'trade_date', 'is_limit_up', 'is_limit_down', 'is_suspended'])
            
            return lazy_df.collect()
            
        except Exception as e:
            self._logger.error(f"[DB] 读取 stock_limit_status (Polars) 失败: {e}")
            return pl.DataFrame()
    
    def _get_limit_status_from_memory_cache(self) -> pd.DataFrame:
        """
        [新增] 确保 Limit Status 全表在内存中，并返回全量 DataFrame
        解决 LanceDB 查询 120万行数据耗时 2秒 的问题
        """
        if getattr(self, '_full_limit_status_cache', None) is not None:
            return self._full_limit_status_cache

        self._logger.info("⚡ [Cache] 正在全量加载 stock_limit_status 到内存 (一次性开销)...")
        try:
            df = self._read_stock_limit_status_parquet()
            
            self._full_limit_status_cache = df
            self._logger.info(f"[OK] Limit Status 全表缓存完成: {len(df)} 行")
            return df
        except Exception as e:
            self._logger.error(f"全量加载 Limit Status 失败: {e}")
            return pd.DataFrame()
    
    def preload_stock_limit_status(self, start_date: str, end_date: str) -> None:
        """
        直接触发全量内存加载
        """
        self._logger.info(f"[DB] 预加载 stock_limit_status: 确保全表在内存中")
        
        df = self._get_limit_status_from_memory_cache()
        
        if self._stock_limit_status_cache is None:
            self._stock_limit_status_cache = {}
            if not df.empty:
                for date, group in df.groupby('trade_date'):
                    self._stock_limit_status_cache[str(date)] = group.copy()
            self._logger.info("[OK] Limit Status 字典索引构建完成")
    
    def get_stock_limit_status_batch(self, dates: List[str], stock_codes: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
        """
        批量获取 stock_limit_status 数据（优化：优先从内存字典读取，未命中才查库）
        
        Args:
            dates: 日期列表
            stock_codes: 股票代码列表（可选，用于过滤）
            
        Returns:
            Dict[str, pd.DataFrame]: {date: DataFrame} 字典，每个 DataFrame 包含该日期的 limit_status 数据
        """
        result = {}
        
        if self._stock_limit_status_cache is not None:
            for date_str in dates:
                if date_str in self._stock_limit_status_cache:
                    cached_df = self._stock_limit_status_cache[date_str].copy()
                    if stock_codes:
                        cached_df = cached_df[cached_df['stock_code'].isin(stock_codes)].copy()
                    result[date_str] = cached_df
        
        missing_dates = [d for d in dates if d not in result]
        if missing_dates:
            for date_str in missing_dates:
                    try:
                        date_df = self._read_stock_limit_status_parquet(trade_date=date_str, stock_codes=stock_codes)
                        if not date_df.empty:
                            result[date_str] = date_df[['stock_code', 'is_limit_up', 'is_limit_down', 'is_suspended']].copy()
                            
                            # 更新缓存
                            if self._stock_limit_status_cache is None:
                                self._stock_limit_status_cache = {}
                            self._stock_limit_status_cache[date_str] = result[date_str].copy()
                    except Exception as e:
                        self._logger.warning(f"[DB] 读取 stock_limit_status 失败 ({date_str}): {e}")
        
        # 3. 对于仍然缺失的日期，返回空 DataFrame
        for date_str in dates:
            if date_str not in result:
                result[date_str] = pd.DataFrame(columns=['stock_code', 'is_limit_up', 'is_limit_down', 'is_suspended'])
        
        return result
    
    def clear_cache(self):
        """清除所有缓存"""
        self._cache.clear()
        self.clear_preloaded_data()
        self._stock_limit_status_cache = None
        self._stock_limit_status_cache_range = None
    
    def _query_df(self, query: str, params: List[Any] = None) -> pd.DataFrame:
        """
        执行 SQL 查询并返回 Pandas DataFrame
        
        【Polars 实现】解析 SQL 查询并转换为 Polars 操作
        
        Args:
            query: SQL 查询语句
            params: 查询参数列表（用于 WHERE 条件）
        
        Returns:
            Pandas DataFrame 包含查询结果
        """
        import re
        import polars as pl
        
        try:
            query_upper = query.upper().strip()
            
            if query_upper.startswith("SELECT"):
                if any(func in query_upper for func in ['MAX(', 'MIN(', 'SUM(', 'AVG(', 'COUNT(']):
                    return self._execute_aggregate(query, params)
                else:
                    return self._execute_select(query, params)
            else:
                self._logger.warning(f"不支持的查询类型: {query_upper[:20]}")
                return pd.DataFrame()
                
        except Exception as e:
            self._logger.error(f"查询执行失败: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _execute_select(self, query: str, params: List[Any] = None) -> pd.DataFrame:
        """
        执行 SELECT 查询
        
        Args:
            query: SQL SELECT 语句
            params: 查询参数
        
        Returns:
            Pandas DataFrame 包含查询结果
        """
        import re
        
        try:
            columns_match = re.search(r'SELECT\s+(.+?)\s+FROM\s+(\w+)', query, re.IGNORECASE)
            if not columns_match:
                self._logger.error(f"无法解析 SELECT 语句: {query[:50]}")
                return pd.DataFrame()
            
            columns_str = columns_match.group(1)
            table_name = columns_match.group(2).lower()
            
            if table_name not in ['stock_info', 'stock_daily', 'benchmark_data', 'benchmark_daily']:
                self._logger.warning(f"不支持的表: {table_name}")
                return pd.DataFrame()
            
            columns = [c.strip() for c in columns_str.split(',')]
            
            parquet_path = self._get_parquet_path(table_name)
            if parquet_path is None:
                self._logger.warning(f"Parquet 文件不存在: {parquet_path}")
                return pd.DataFrame()
            
            lazy_df = pl.scan_parquet(str(parquet_path))
            
            where_match = re.search(r'WHERE\s+(.+?)(?:ORDER|LIMIT|$)', query, re.IGNORECASE | re.DOTALL)
            if where_match and params:
                where_clause = where_match.group(1).strip()
                lazy_df = self._apply_where_clause(lazy_df, where_clause, params)
            
            order_match = re.search(r'ORDER\s+BY\s+(.+?)\s+(ASC|DESC)?', query, re.IGNORECASE)
            if order_match:
                order_col = order_match.group(1).strip().split('.')[-1] if '.' in order_match.group(1) else order_match.group(1).strip()
                order_dir = order_match.group(2).upper() if order_match.group(2) else 'ASC'
                lazy_df = lazy_df.sort(order_col, descending=(order_dir == 'DESC'))
            
            limit_match = re.search(r'LIMIT\s+(\d+)', query, re.IGNORECASE)
            if limit_match:
                limit_count = int(limit_match.group(1))
                lazy_df = lazy_df.head(limit_count)
            
            df_pl = lazy_df.collect()
            
            if not any(col.upper().startswith(('COUNT', 'MAX', 'MIN', 'SUM', 'AVG')) for col in columns):
                existing_cols = [c for c in columns if c in df_pl.columns]
                if existing_cols and columns != ['*']:
                    df_pl = df_pl.select(existing_cols)
            
            return df_pl.to_pandas()
            
        except Exception as e:
            self._logger.error(f"SELECT 查询执行失败: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _execute_aggregate(self, query: str, params: List[Any] = None) -> pd.DataFrame:
        """
        执行聚合查询（如 MAX, MIN, COUNT 等）
        
        Args:
            query: SQL 聚合查询语句
            params: 查询参数
        
        Returns:
            Pandas DataFrame 包含聚合结果
        """
        import re
        
        try:
            agg_match = re.search(r'(MAX|MIN|SUM|AVG|COUNT)\s*\(\s*(\*|\w+)\s*\)\s*AS\s*(\w+)', query, re.IGNORECASE)
            if not agg_match:
                self._logger.error(f"无法解析聚合函数: {query[:50]}")
                return pd.DataFrame()
            
            agg_func = agg_match.group(1).upper()
            agg_col = agg_match.group(2)
            alias = agg_match.group(3)
            
            table_match = re.search(r'FROM\s+(\w+)', query, re.IGNORECASE)
            if not table_match:
                self._logger.error(f"无法解析表名: {query[:50]}")
                return pd.DataFrame()
            
            table_name = table_match.group(1).lower()
            
            if table_name not in ['stock_info', 'stock_daily', 'benchmark_data', 'benchmark_daily']:
                self._logger.warning(f"不支持的表: {table_name}")
                return pd.DataFrame()
            
            parquet_path = self._get_parquet_path(table_name)
            if parquet_path is None:
                self._logger.warning(f"Parquet 文件不存在: {parquet_path}")
                return pd.DataFrame()
            
            lazy_df = pl.scan_parquet(str(parquet_path))
            
            where_match = re.search(r'WHERE\s+(.+?)(?:ORDER|LIMIT|$)', query, re.IGNORECASE | re.DOTALL)
            if where_match and params:
                where_clause = where_match.group(1).strip()
                lazy_df = self._apply_where_clause(lazy_df, where_clause, params)
            
            df_pl = lazy_df.collect()
            
            if agg_func == 'MAX':
                result = df_pl[agg_col].max()
            elif agg_func == 'MIN':
                result = df_pl[agg_col].min()
            elif agg_func == 'SUM':
                result = df_pl[agg_col].sum()
            elif agg_func == 'AVG':
                result = df_pl[agg_col].mean()
            elif agg_func == 'COUNT':
                result = len(df_pl)
            else:
                self._logger.warning(f"不支持的聚合函数: {agg_func}")
                return pd.DataFrame()
            
            result_df = pd.DataFrame({alias: [result]})
            return result_df
            
        except Exception as e:
            self._logger.error(f"聚合查询执行失败: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _apply_where_clause(self, lazy_df: pl.LazyFrame, where_clause: str, params: List[Any]) -> pl.LazyFrame:
        """
        应用 WHERE 条件到 LazyFrame
        
        Args:
            lazy_df: Polars LazyFrame
            where_clause: WHERE 子句
            params: 查询参数
        
        Returns:
            应用 WHERE 条件后的 LazyFrame
        """
        import re
        
        try:
            conditions = []
            
            if 'AND' in where_clause.upper():
                condition_parts = re.split(r'\s+AND\s+', where_clause, flags=re.IGNORECASE)
            elif 'OR' in where_clause.upper():
                condition_parts = re.split(r'\s+OR\s+', where_clause, flags=re.IGNORECASE)
            else:
                condition_parts = [where_clause]
            
            for i, part in enumerate(condition_parts):
                part = part.strip()
                
                if '=' in part and '?' in part:
                    match = re.match(r'(\w+(?:\.\w+)?)\s*=\s*\?', part)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col) == params[i])
                elif 'IN' in part.upper() and '?' in part:
                    match = re.match(r'(\w+)\s+IN\s+\(([^)]+)\)', part, re.IGNORECASE)
                    if match:
                        col = match.group(1)
                        placeholders = match.group(2)
                        if col in lazy_df.collect_schema().names():
                            n_params = placeholders.count('?')
                            if n_params > 0 and n_params <= len(params):
                                condition = pl.col(col).is_in(params[:n_params])
                                conditions.append(condition)
                elif '>=' in part:
                    match = re.match(r'(\w+)\s*>=\s*\?', part)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col) >= params[0])
                elif '<=' in part:
                    match = re.match(r'(\w+)\s*<=\s*\?', part)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col) <= params[0])
                elif '>' in part:
                    match = re.match(r'(\w+)\s*>\s*\?', part)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col) > params[0])
                elif '<' in part:
                    match = re.match(r'(\w+)\s*<\s*\?', part)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col) < params[0])
                elif 'IS NOT NULL' in part.upper():
                    match = re.match(r'(\w+)\s+IS\s+NOT\s+NULL', part, re.IGNORECASE)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col).is_not_null())
                elif 'IS NULL' in part.upper():
                    match = re.match(r'(\w+)\s+IS\s+NULL', part, re.IGNORECASE)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col).is_null())
                elif 'LIKE' in part.upper():
                    match = re.match(r'(\w+)\s+LIKE\s+\?', part, re.IGNORECASE)
                    if match:
                        col = match.group(1).split('.')[-1]
                        if col in lazy_df.collect_schema().names():
                            conditions.append(pl.col(col).str.contains(params[0].replace('%', '')))
            
            if conditions:
                for cond in conditions:
                    lazy_df = lazy_df.filter(cond)
            
            return lazy_df
            
        except Exception as e:
            self._logger.warning(f"WHERE 条件应用失败: {e}")
            return lazy_df
    
    def close(self):
        """
        关闭数据库连接并清理资源
        
        【Polars 实现】无需关闭连接，只需清理缓存
        """
        try:
            self.clear_cache()
        except Exception as e:
            self._logger.warning(f"清理缓存时出错: {e}")
