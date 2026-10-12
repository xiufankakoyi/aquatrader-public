"""
回测数据层

负责数据加载、缓存、股票池、因子矩阵构建。
"""

import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# 可选依赖导入
try:
    import polars as pl
    POLARS_AVAILABLE = True
except ImportError:
    POLARS_AVAILABLE = False
    pl = None

from config.logger import get_logger
from .models import TimeGranularity

logger = get_logger(__name__)


class DataLayer:
    """
    回测数据层

    负责数据加载、缓存、股票池、因子矩阵构建。
    """

    def __init__(self, engine):
        """
        初始化数据层。

        Args:
            engine: UnifiedBacktestEngine 实例，作为共享状态容器。
        """
        self.engine = engine

    def __getattr__(self, name):
        """将未在层中定义的属性访问委托给引擎。"""
        return getattr(self.engine, name)

    def __setattr__(self, name, value):
        """将属性赋值委托给引擎，仅保留 engine 引用在层自身。"""
        if name == 'engine':
            super().__setattr__(name, value)
        else:
            setattr(self.engine, name, value)

    def _normalize_datetime(self, dt: Union[str, pd.Timestamp, datetime]) -> pd.Timestamp:
        """统一转换为 pd.Timestamp"""
        if isinstance(dt, str):
            return pd.to_datetime(dt)
        elif isinstance(dt, datetime):
            return pd.Timestamp(dt)
        elif isinstance(dt, pd.Timestamp):
            return dt
        else:
            raise TypeError(f"不支持的时间类型: {type(dt)}")

    def _get_time_series(
        self,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp
    ) -> List[pd.Timestamp]:
        """根据时间粒度获取时间序列"""
        if self.config.time_granularity == TimeGranularity.DAILY:
            try:
                trading_dates = self.data_query.get_trading_dates(
                    start_ts.strftime("%Y-%m-%d"),
                    end_ts.strftime("%Y-%m-%d")
                )
                return [pd.to_datetime(d) for d in trading_dates]
            except Exception as e:
                logger.error(f"获取交易日失败: {e}")
                return []

        elif self.config.time_granularity == TimeGranularity.MINUTE:
            # 简化实现：实际应该生成交易时间
            time_series = []
            current = start_ts.replace(hour=9, minute=30, second=0, microsecond=0)
            while current <= end_ts:
                time_series.append(current)
                current += timedelta(minutes=1)
            return time_series

        else:
            raise NotImplementedError(f"Tick级别回测需要实现tick数据查询")

    def _preload_data(
        self,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        strategy: Any = None
    ) -> Optional[Dict[str, 'pl.DataFrame']]:
        """
        预加载回测期间的数据（返回 Polars DataFrame 实现零拷贝）

        优化：自动推断策略需要的列，减少内存占用和加载时间

        优先级：
        1. UnifiedDataManager 内存缓存（零磁盘IO）
        2. 原有的 OptimizedStockDataQuery 预加载

        Args:
            start_ts: 开始时间戳
            end_ts: 结束时间戳
            strategy: 策略实例（用于自动推断所需列）
        """
        from core.utils.column_inference import ColumnInference

        MINIMAL_FIELDS = [
            'stock_code', 'trade_date',
            'open', 'high', 'low', 'close', 'volume', 'amount',
            'adj_factor', 'prev_close',
            'total_mv', 'limit_up', 'limit_down'
        ]

        if strategy is not None:
            try:
                inferred_fields = ColumnInference.get_required_columns(strategy)
                MINIMAL_FIELDS = list(set(MINIMAL_FIELDS) | set(inferred_fields))
                logger.info(f"[Engine] 自动推断所需列: {sorted(inferred_fields)}")
            except Exception as e:
                logger.warning(f"[Engine] 列推断失败，使用默认列: {e}")

        load_start_str: str
        load_end_str = end_ts.strftime("%Y-%m-%d")

        warmup_days = self.config.warmup_days
        try:
            hist_dates = self.data_query.get_trading_dates(
                (start_ts - timedelta(days=60)).strftime("%Y-%m-%d"),
                (start_ts - timedelta(days=1)).strftime("%Y-%m-%d")
            )
            if len(hist_dates) >= warmup_days:
                warmup_start_str = hist_dates[-warmup_days]
            elif hist_dates:
                warmup_start_str = hist_dates[0]
            else:
                warmup_start_str = start_ts.strftime("%Y-%m-%d")
        except Exception:
            warmup_start_str = start_ts.strftime("%Y-%m-%d")

        try:
            from data_svc.unified_data_manager import get_unified_manager
            manager = get_unified_manager()

            logger.info(f"[Engine] 检查缓存: cache_loaded={manager._cache_loaded}, range={manager._preloaded_date_range}")

            try:
                prev_trading_dates = self.data_query.get_trading_dates(
                    (start_ts - timedelta(days=10)).strftime("%Y-%m-%d"),
                    (start_ts - timedelta(days=1)).strftime("%Y-%m-%d")
                )
                if prev_trading_dates:
                    actual_start = prev_trading_dates[-1]
                else:
                    actual_start = start_ts.strftime("%Y-%m-%d")
            except Exception:
                actual_start = (start_ts - timedelta(days=1)).strftime("%Y-%m-%d")

            if manager._cache_loaded and manager._preloaded_date_range:
                cached_start, cached_end = manager._preloaded_date_range

                logger.info(f"[Engine] 日期范围: warmup_start={warmup_start_str}, actual_start={actual_start}, load_end={load_end_str}, cached=({cached_start}, {cached_end})")

                if warmup_start_str >= cached_start and load_end_str <= cached_end:
                    logger.info(f"[Engine] 使用 UnifiedDataManager 内存缓存: {warmup_start_str} ~ {load_end_str}")

                    preloaded_data = manager.get_preloaded_data(warmup_start_str, load_end_str)
                    stock_daily = preloaded_data.get('daily')

                    logger.info(f"[Engine] daily: {len(stock_daily) if stock_daily is not None else 0} 行")

                    if stock_daily is not None and not stock_daily.is_empty():
                        logger.info(f"[Engine] 内存缓存命中: {len(stock_daily)} 行")

                        self._build_factor_matrix_from_df(stock_daily)

                        return {'daily': stock_daily}
                else:
                    logger.info(f"[Engine] 日期范围不匹配，从 LanceDB 加载")
            else:
                logger.info(f"[Engine] 缓存未加载，从 LanceDB 加载数据")

            try:
                logger.info(f"[Engine] 从 LanceDB 读取（列过滤）: {warmup_start_str} ~ {load_end_str}")
                stock_daily = manager.read('daily', start_date=warmup_start_str, end_date=load_end_str, fields=MINIMAL_FIELDS)

                if stock_daily is not None and not stock_daily.is_empty():
                    logger.info(f"[Engine] 从 LanceDB 读取成功: {len(stock_daily)} 行, 列数: {len(stock_daily.columns)}")

                    self._build_factor_matrix_from_df(stock_daily)

                    return {'daily': stock_daily}
                else:
                    logger.warning(f"[Engine] 从 LanceDB 读取数据为空")
            except Exception as e:
                logger.warning(f"[Engine] 从 LanceDB 读取失败: {e}")
        except Exception as e:
            logger.warning(f"[Engine] UnifiedDataManager 缓存未命中: {e}")

        if not hasattr(self.data_query, 'get_all_daily_data_for_period'):
            return None

        try:
            warmup_days = self.config.warmup_days
            hist_dates = self.data_query.get_trading_dates(
                (start_ts - timedelta(days=60)).strftime("%Y-%m-%d"),
                (start_ts - timedelta(days=1)).strftime("%Y-%m-%d")
            )

            if len(hist_dates) >= warmup_days:
                load_start_str = hist_dates[-warmup_days]
            elif hist_dates:
                load_start_str = hist_dates[0]
            else:
                load_start_str = start_ts.strftime("%Y-%m-%d")

            full_start = actual_start if 'actual_start' in locals() else load_start_str
            logger.info(f"[Engine] 预加载完整回测数据: {full_start} ~ {load_end_str}")
            import time as _time_mod
            _t_pre = _time_mod.perf_counter()
            # 列裁剪：只读取传统策略需要的列（OHLCV + 状态字段），减少 LanceDB IO
            self.data_query.preload_backtest_data(full_start, load_end_str, columns=MINIMAL_FIELDS)
            _t_pre_end = _time_mod.perf_counter()
            preloaded = getattr(self.data_query, '_preloaded_data', None)

            if preloaded is not None:
                logger.info(f"[Engine] 成功预加载 {len(preloaded)} 个数据点, preload 耗时={(_t_pre_end-_t_pre)*1000:.1f}ms")

                _t_fm = _time_mod.perf_counter()
                # 仅当策略支持向量化信号生成时才构建因子矩阵；
                # 传统策略走预加载路径（返回 DataFrame），不需要因子矩阵
                is_vectorized = strategy is not None and hasattr(strategy, 'generate_signals_vectorized')
                if is_vectorized:
                    self._build_factor_matrix(preloaded)
                else:
                    self._factor_matrix = None
                    logger.info(f"[Engine] 传统策略，跳过因子矩阵构建")
                logger.info(f"[PERF-TRACE] _build_factor_matrix 耗时: {(_time_mod.perf_counter()-_t_fm)*1000:.1f}ms (vectorized={is_vectorized})")

            return preloaded

        except Exception as e:
            logger.warning(f"[Engine] 预加载失败: {e}")
            return None

    def _build_factor_matrix(
        self,
        preloaded_data: Dict[str, 'pl.DataFrame']
    ) -> None:
        """
        构建三维因子矩阵

        将预加载数据转换为 (T, N, F) 三维矩阵
        """
        try:
            from core.quant.backtest.factor_matrix import build_factor_matrix

            self._factor_matrix = build_factor_matrix(preloaded_data)

            logger.info(
                f"[Engine] 因子矩阵构建完成: "
                f"T={len(self._factor_matrix.dates)}, "
                f"N={len(self._factor_matrix.codes_str)}, "
                f"F={len(self._factor_matrix.factor_names)}"
            )
        except Exception as e:
            logger.warning(f"[Engine] 因子矩阵构建失败: {e}")
            self._factor_matrix = None

    def _build_factor_matrix_from_df(
        self,
        df: 'pl.DataFrame'
    ) -> None:
        """
        从单个 DataFrame 构建因子矩阵（零拷贝优化版）

        直接处理完整 DataFrame，无需按日期分区

        修复：计算 is_limit_up, is_limit_down, is_suspended 字段
        """
        try:
            from core.quant.backtest.factor_matrix import FactorMatrixBuilder

            # 修复：计算涨停/跌停/停牌状态字段
            df_enhanced = self._compute_status_fields(df)

            builder = FactorMatrixBuilder()
            # FIX: 禁用缓存，确保使用计算后的状态字段
            self._factor_matrix = builder.build_from_single_dataframe(df_enhanced, use_cache=False)

            logger.info(
                f"[Engine] 因子矩阵构建完成: "
                f"T={len(self._factor_matrix.dates)}, "
                f"N={len(self._factor_matrix.codes_str)}, "
                f"F={len(self._factor_matrix.factor_names)}"
            )
        except Exception as e:
            logger.warning(f"[Engine] 因子矩阵构建失败: {e}")
            import traceback
            traceback.print_exc()
            self._factor_matrix = None

    def _compute_status_fields(self, df: 'pl.DataFrame') -> 'pl.DataFrame':
        """
        计算股票状态字段（涨停、跌停、停牌）

        原始数据中的 limit_up/limit_down 是价格，需要计算是否达到涨跌停
        FIX: 总是重新计算这些字段，即使原始数据中已经存在
        """
        df_enhanced = df

        # FIX: 总是重新计算 is_limit_up，即使原始数据中已经存在
        if 'limit_up' in df.columns and 'close' in df.columns:
            df_enhanced = df_enhanced.with_columns([
                (pl.col('close') >= pl.col('limit_up')).cast(pl.Float64).alias('is_limit_up')
            ])
        else:
            # 如果没有数据，默认全部为 0（未涨停）
            df_enhanced = df_enhanced.with_columns([
                pl.lit(0.0).cast(pl.Float64).alias('is_limit_up')
            ])

        # FIX: 总是重新计算 is_limit_down，即使原始数据中已经存在
        if 'limit_down' in df.columns and 'close' in df.columns:
            df_enhanced = df_enhanced.with_columns([
                (pl.col('close') <= pl.col('limit_down')).cast(pl.Float64).alias('is_limit_down')
            ])
        else:
            df_enhanced = df_enhanced.with_columns([
                pl.lit(0.0).cast(pl.Float64).alias('is_limit_down')
            ])

        # FIX: 总是重新计算 is_suspended，即使原始数据中已经存在
        if 'volume' in df.columns and 'close' in df.columns:
            df_enhanced = df_enhanced.with_columns([
                ((pl.col('volume') == 0) | (pl.col('close') == 0)).cast(pl.Float64).alias('is_suspended')
            ])
        else:
            df_enhanced = df_enhanced.with_columns([
                pl.lit(0.0).cast(pl.Float64).alias('is_suspended')
            ])

        return df_enhanced

    def _preload_factors(
        self,
        strategy: Any,
        preloaded_data: Optional[Dict[str, 'pl.DataFrame']]
    ) -> None:
        """
        预加载策略声明的因子

        通过 core.quant.factors.factor_layer.FactorLayer 统一加载：
        1. 优先从 daily 表列读取（零拷贝）
        2. 缺失时从 FactorCalculator / Parquet 加载
        3. 仍缺失时通过 FactorPrecomputeEngine DAG 计算（MA/RSI/MACD/形态等）
        """
        if preloaded_data is None:
            return

        required_factors = getattr(strategy, 'required_factors', [])
        if not required_factors:
            return

        try:
            from core.quant.factors.factor_layer import FactorLayer

            stock_daily_df = preloaded_data.get('daily')
            if stock_daily_df is None or len(stock_daily_df) == 0:
                logger.warning("[Engine] preloaded_data 中没有 daily 数据")
                self._preloaded_factors = {}
                return

            is_polars = hasattr(stock_daily_df, 'columns') and not isinstance(stock_daily_df, pd.DataFrame)
            if is_polars:
                trading_dates = sorted(stock_daily_df['trade_date'].unique().to_list())
                stock_codes = sorted(stock_daily_df['stock_code'].unique().to_list())
            else:
                trading_dates = sorted(stock_daily_df['trade_date'].unique().tolist())
                stock_codes = sorted(stock_daily_df['stock_code'].unique().tolist())

            layer = FactorLayer(self.engine)
            factor_data = layer.load_factors(
                required_factors=required_factors,
                trading_dates=[str(d)[:10] for d in trading_dates],
                stock_codes=[str(c).zfill(6) for c in stock_codes],
                preloaded_data=preloaded_data,
            )

            self._preloaded_factors = factor_data
            layer.inject_into_strategy(strategy, factor_data)

            if factor_data:
                logger.info(f"[Engine] 成功预加载 {len(factor_data)} 个因子: {list(factor_data.keys())}")
            else:
                logger.warning("[Engine] 未能加载任何因子")

        except Exception as e:
            logger.warning(f"[Engine] 因子预加载失败: {e}")
            import traceback
            traceback.print_exc()
            self._preloaded_factors = {}

    def _load_day_data(
        self,
        current_time: pd.Timestamp
    ) -> Tuple[Any, bool, Dict[str, Dict[str, Any]]]:
        """
        加载当日数据（优先使用预加载数据）

        【性能优化】：
        1. 优先从预加载数据获取（DataFrame，传统策略友好）
        2. 回退到 Polars 查询
        3. 再回退到因子矩阵（向量化策略仍可从 self._factor_matrix 访问）
        4. 最后查询数据库

        注：向量化策略的信号生成不依赖 stock_pool 参数，而是用 preloaded_data
        和 self._factor_matrix，因此预加载路径不影响向量化模式性能。
        """
        import time
        t0_total = time.perf_counter()

        date_str = current_time.strftime("%Y-%m-%d")
        use_pl = False
        stock_pool = None

        # 优先级 1：预加载数据（DataFrame，传统策略友好）
        if hasattr(self.data_query, 'get_stock_pool_from_preloaded'):
            t1 = time.perf_counter()
            preloaded_df = self.data_query.get_stock_pool_from_preloaded(date_str)
            logger.debug(f"[_load_day_data] 预加载数据查询耗时: {(time.perf_counter()-t1)*1000:.1f}ms")
            if preloaded_df is not None and len(preloaded_df) > 0:
                stock_pool = preloaded_df
                use_pl = True

        # 优先级 2：Polars 查询
        if stock_pool is None and hasattr(self.data_query, 'get_stock_pool_pl'):
            t1 = time.perf_counter()
            stock_pool = self.data_query.get_stock_pool_pl(date_str)
            logger.debug(f"[_load_day_data] Polars数据查询耗时: {(time.perf_counter()-t1)*1000:.1f}ms")
            if stock_pool is not None and hasattr(stock_pool, 'is_empty') and not stock_pool.is_empty():
                use_pl = True

        # 优先级 3：因子矩阵（预加载未命中时降级，向量化策略仍可访问 self._factor_matrix）
        if stock_pool is None and self._factor_matrix is not None:
            t1 = time.perf_counter()
            result = self._load_day_data_from_matrix(date_str)
            logger.debug(f"[_load_day_data] 因子矩阵加载耗时: {(time.perf_counter()-t1)*1000:.1f}ms")
            return result

        # 优先级 4：数据库查询
        if stock_pool is None:
            t1 = time.perf_counter()
            stock_pool = self._get_stock_pool_at_time(current_time)
            logger.debug(f"[_load_day_data] 数据库查询耗时: {(time.perf_counter()-t1)*1000:.1f}ms")

        t1 = time.perf_counter()
        data_dict = {}
        if use_pl:
            needed_cols = [c for c in ['stock_code', 'close', 'open', 'adj_factor', 'total_mv',
                                       'is_suspended', 'is_limit_up', 'is_limit_down']
                          if c in stock_pool.columns]
            data_dict = {
                str(row[0]): dict(zip(needed_cols, row))
                for row in stock_pool.select(needed_cols).iter_rows()
            }
        elif stock_pool is not None and hasattr(stock_pool, 'empty') and not stock_pool.empty:
            data_dict = stock_pool.set_index('stock_code').to_dict('index')

        logger.debug(f"[_load_day_data] dict构建耗时: {(time.perf_counter()-t1)*1000:.1f}ms, 总耗时: {(time.perf_counter()-t0_total)*1000:.1f}ms")

        return stock_pool, use_pl, data_dict

    def _load_day_data_from_matrix(
        self,
        date_str: str
    ) -> Tuple[np.ndarray, bool, Dict[str, Dict[str, Any]]]:
        """
        从因子矩阵加载当日数据

        返回：
            factor_slice: (N, F) 二维矩阵
            use_pl: 固定为 False
            data_dict: 用于交易执行的字典（包含每只股票的数据）
        """
        fm = self._factor_matrix
        date_str_normalized = date_str
        if date_str not in fm.date_to_idx:
            if ' ' in date_str:
                date_str_normalized = date_str.split(' ')[0]
            else:
                date_str_normalized = date_str + ' 00:00:00'

        date_idx = fm.date_to_idx.get(date_str_normalized, -1)

        if date_idx < 0:
            return None, False, {}

        factor_slice = fm.values[date_idx, :, :]

        import time as time_module
        t0 = time_module.perf_counter()

        # FIX: 构建完整的 data_dict，包含每只股票的 close 价格
        data_dict = self._build_data_dict_fast(factor_slice, fm)

        logger.debug(f"[_load_day_data_from_matrix] _build_data_dict_fast耗时: {(time_module.perf_counter()-t0)*1000:.1f}ms")

        return factor_slice, False, data_dict

    def _build_data_dict_fast(
        self,
        factor_slice: np.ndarray,
        fm: Any
    ) -> Dict:
        """
        快速构建data_dict（NumPy结构化数组优化版）

        性能优化：
        1. 使用NumPy向量化操作代替Python循环
        2. 使用结构化数组替代字典
        """
        factor_names = fm.factor_names
        codes = fm.codes_str
        N = len(codes)

        factor_idx = {name: i for i, name in enumerate(factor_names)}

        # 创建NumPy结构化数组（零拷贝）
        dtype = np.dtype([
            ('open', np.float64),
            ('close', np.float64),
            ('high', np.float64),
            ('low', np.float64),
            ('volume', np.float64),
            ('amount', np.float64),
            ('total_mv', np.float64),
            ('float_mv', np.float64),
            ('turnover_rate', np.float64),
            ('volume_ratio', np.float64),
            ('is_limit_up', np.int8),
            ('is_limit_down', np.int8),
            ('is_suspended', np.int8),
            ('is_st', np.int8),
            ('adj_factor', np.float64),
            ('prev_close', np.float64),
        ])

        market_data = np.zeros(N, dtype=dtype)

        # 向量化填充
        for fname, npname in [
            ('open', 'open'), ('close', 'close'), ('high', 'high'), ('low', 'low'),
            ('volume', 'volume'), ('amount', 'amount'), ('total_mv', 'total_mv'),
            ('float_mv', 'float_mv'), ('turnover_rate', 'turnover_rate'),
            ('volume_ratio', 'volume_ratio'), ('adj_factor', 'adj_factor'),
            ('prev_close', 'prev_close'), ('is_limit_up', 'is_limit_up'),
            ('is_limit_down', 'is_limit_down'), ('is_suspended', 'is_suspended'),
            ('is_st', 'is_st')
        ]:
            if fname in factor_idx:
                col = factor_slice[:, factor_idx[fname]]
                col = np.where(np.isnan(col), 0, col)
                if npname in ['is_limit_up', 'is_limit_down', 'is_suspended', 'is_st']:
                    market_data[npname] = col.astype(np.int8)
                else:
                    market_data[npname] = col

        # 返回结构化数组 + 代码列表（避免字典推导）
        return {
            '_codes': codes,
            '_market_data': market_data,
            '_factor_slice': factor_slice,
        }

    def _build_market_df_from_matrix(
        self,
        factor_slice: np.ndarray,
        fm: Any
    ):
        """
        直接从因子矩阵构建 Polars DataFrame（避免中间字典转换）
        """
        factor_names = fm.factor_names
        codes = fm.codes_str
        N = len(codes)

        factor_idx = {name: i for i, name in enumerate(factor_names)}

        def get_col(name, default):
            idx = factor_idx.get(name, -1)
            if idx >= 0:
                col = factor_slice[:, idx]
                return np.nan_to_num(col, nan=default)
            return np.full(N, default)

        open_col = get_col('open', 0.0).astype(np.float64)
        close_col = get_col('close', 0.0).astype(np.float64)
        is_limit_up_col = get_col('is_limit_up', 0).astype(bool)
        is_limit_down_col = get_col('is_limit_down', 0).astype(bool)
        is_suspended_col = get_col('is_suspended', 0).astype(bool)
        total_mv_col = get_col('total_mv', 0.0).astype(np.float64)
        adj_factor_col = get_col('adj_factor', 1.0).astype(np.float64)

        return pl.DataFrame({
            'code': codes,
            'open': open_col,
            'close': close_col,
            'is_suspended': is_suspended_col,
            'is_limit_up': is_limit_up_col,
            'is_limit_down': is_limit_down_col,
            'adj_factor': adj_factor_col,
            'total_mv': total_mv_col
        })

    def _get_stock_pool_at_time(self, date: Union[str, pd.Timestamp]) -> pd.DataFrame:
        """获取指定日期的股票池（带缓存）"""
        if isinstance(date, pd.Timestamp):
            date_str = date.strftime("%Y-%m-%d")
        else:
            date_str = str(date)

        # 缓存检查
        if date_str in self._stock_pool_cache:
            self._cache_order.remove(date_str)
            self._cache_order.append(date_str)
            return self._stock_pool_cache[date_str]

        # 加载数据
        try:
            result = self.data_query.get_stock_pool(date_str)
            self._stock_pool_cache[date_str] = result
            self._cache_order.append(date_str)

            # LRU淘汰
            if len(self._stock_pool_cache) > self._cache_max_size:
                oldest = self._cache_order.pop(0)
                self._stock_pool_cache.pop(oldest, None)

            return result
        except Exception as e:
            logger.error(f"获取股票池失败 {date_str}: {e}")
            return pd.DataFrame()
