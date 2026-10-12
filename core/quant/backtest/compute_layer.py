"""
回测计算层

负责价格矩阵构建、向量化计算。
"""

from typing import Any, Dict, List

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

logger = get_logger(__name__)


class ComputeLayer:
    """
    回测计算层

    负责价格矩阵构建、向量化计算。
    """

    def __init__(self, engine):
        """
        初始化计算层。

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

    def _build_price_matrix_fallback(
        self,
        preloaded_data: Dict,
        trading_dates: List[str],
        stock_codes_list: List[str]
    ) -> np.ndarray:
        """回退方案：从 DataFrame 构建价格矩阵（支持 Polars）"""
        import polars as pl

        T = len(trading_dates)
        N = len(stock_codes_list)

        price_matrix = np.full((T, N, 4), np.nan, dtype=np.float32)

        code_to_idx = {code: i for i, code in enumerate(stock_codes_list)}
        date_to_idx = {date: i for i, date in enumerate(trading_dates)}

        if 'daily' in preloaded_data:
            stock_daily = preloaded_data['daily']

            df_with_idx = stock_daily.select(['trade_date', 'stock_code', 'open', 'high', 'low', 'close']).with_columns([
                pl.col('trade_date').cast(pl.Utf8).replace_strict(date_to_idx, default=None).alias('date_idx'),
                pl.col('stock_code').cast(pl.Utf8).str.strip_chars().replace_strict(code_to_idx, default=None).alias('code_idx')
            ])

            df_with_idx = df_with_idx.filter(
                pl.col('date_idx').is_not_null() & pl.col('code_idx').is_not_null()
            )

            if len(df_with_idx) > 0:
                t_indices = df_with_idx['date_idx'].to_numpy().astype(np.int32)
                n_indices = df_with_idx['code_idx'].to_numpy().astype(np.int32)

                price_matrix[t_indices, n_indices, 0] = df_with_idx['open'].to_numpy()
                price_matrix[t_indices, n_indices, 1] = df_with_idx['high'].to_numpy()
                price_matrix[t_indices, n_indices, 2] = df_with_idx['low'].to_numpy()
                price_matrix[t_indices, n_indices, 3] = df_with_idx['close'].to_numpy()
        else:
            first_df = next(iter(preloaded_data.values())) if preloaded_data else None
            is_polars = hasattr(first_df, 'columns') and not isinstance(first_df, pd.DataFrame)

            if is_polars:
                all_data_list = []
                for date_str, df_day in preloaded_data.items():
                    if df_day is not None and hasattr(df_day, 'is_empty') and not df_day.is_empty() and date_str in date_to_idx:
                        df_copy = df_day.select(['stock_code', 'open', 'high', 'low', 'close']).clone()
                        df_copy = df_copy.with_columns(pl.lit(date_to_idx[date_str]).alias('date_idx'))
                        all_data_list.append(df_copy)

                if all_data_list:
                    all_data_pl = pl.concat(all_data_list)
                    all_data_pl = all_data_pl.with_columns(
                        pl.col('stock_code').cast(pl.Utf8).str.strip_chars().replace_strict(code_to_idx, default=None).alias('code_idx')
                    )
                    all_data_pl = all_data_pl.filter(pl.col('code_idx').is_not_null())

                    if len(all_data_pl) > 0:
                        t_indices = all_data_pl['date_idx'].to_numpy().astype(np.int32)
                        n_indices = all_data_pl['code_idx'].to_numpy().astype(np.int32)

                        price_matrix[t_indices, n_indices, 0] = all_data_pl['open'].to_numpy()
                        price_matrix[t_indices, n_indices, 1] = all_data_pl['high'].to_numpy()
                        price_matrix[t_indices, n_indices, 2] = all_data_pl['low'].to_numpy()
                        price_matrix[t_indices, n_indices, 3] = all_data_pl['close'].to_numpy()
            else:
                all_data_list = []
                for date_str, df_day in preloaded_data.items():
                    if df_day is None:
                        continue
                    if isinstance(df_day, np.ndarray):
                        if df_day.size == 0:
                            continue
                    elif hasattr(df_day, 'empty') and df_day.empty:
                        continue
                    if date_str not in date_to_idx:
                        continue
                    df_copy = df_day[['stock_code', 'open', 'high', 'low', 'close']].copy()
                    df_copy['date_idx'] = date_to_idx[date_str]
                    all_data_list.append(df_copy)

                if all_data_list:
                    all_data_df = pd.concat(all_data_list, ignore_index=True)
                    all_data_df['stock_code'] = all_data_df['stock_code'].astype(str).str.strip()
                    all_data_df['code_idx'] = all_data_df['stock_code'].map(code_to_idx)
                    all_data_df = all_data_df.dropna(subset=['code_idx'])

                    if not all_data_df.empty:
                        all_data_df['code_idx'] = all_data_df['code_idx'].astype(int)
                        t_indices = all_data_df['date_idx'].values.astype(int)
                        n_indices = all_data_df['code_idx'].values

                        price_matrix[t_indices, n_indices, 0] = all_data_df['open'].values
                        price_matrix[t_indices, n_indices, 1] = all_data_df['high'].values
                        price_matrix[t_indices, n_indices, 2] = all_data_df['low'].values
                        price_matrix[t_indices, n_indices, 3] = all_data_df['close'].values

        return price_matrix

    def _build_price_matrix_from_cache(
        self,
        preloaded_data: Dict,
        trading_dates: List[str],
        stock_codes_list: List[str],
        T: int,
        N: int
    ) -> np.ndarray:
        """从 MatrixCacheManager 构建价格矩阵"""
        from core.quant.backtest.matrix_cache_manager import get_matrix_cache_manager

        cache_manager = get_matrix_cache_manager()
        start_date = trading_dates[0] if trading_dates else ""
        end_date = trading_dates[-1] if trading_dates else ""

        cached_data = cache_manager.load_matrix_mmap(start_date, end_date, stock_codes_list)

        # 验证缓存形状是否匹配当前回测参数
        cache_valid = False
        if cached_data is not None:
            cached_dates = cached_data.get('trading_dates', [])
            cached_codes = cached_data.get('stock_codes', [])
            if len(cached_dates) == T and len(cached_codes) == N:
                cache_valid = True

        if cache_valid:
            # 缓存命中且形状匹配 - 使用内存映射的矩阵
            matrices = cached_data['matrices']
            price_matrix = np.stack([
                matrices['open'],      # 0: open
                matrices['high'],      # 1: high
                matrices['low'],       # 2: low
                matrices['close']      # 3: close
            ], axis=2)
            logger.info(f"[_build_price_matrix_from_cache] 矩阵缓存命中 (内存映射)")
            return price_matrix
        else:
            # 缓存未命中 - 构建矩阵并保存到缓存
            logger.info(f"[_build_price_matrix_from_cache] 矩阵缓存未命中，开始构建...")

            # 构建矩阵并保存
            cache_result = cache_manager.build_and_save_matrix(
                preloaded_data, trading_dates, stock_codes_list
            )

            if cache_result is not None:
                # 使用刚构建的矩阵
                matrices = cache_result['matrices']
                price_matrix = np.stack([
                    matrices['open'],
                    matrices['high'],
                    matrices['low'],
                    matrices['close']
                ], axis=2)
                return price_matrix
            else:
                # 缓存已存在（在 build_and_save_matrix 中检查），重新加载
                cached_data = cache_manager.load_matrix_mmap(start_date, end_date, stock_codes_list)
                if cached_data:
                    matrices = cached_data['matrices']
                    price_matrix = np.stack([
                        matrices['open'],
                        matrices['high'],
                        matrices['low'],
                        matrices['close']
                    ], axis=2)
                    return price_matrix
                else:
                    # 回退到原始构建方式
                    return self._build_price_matrix_fallback(
                        preloaded_data, trading_dates, stock_codes_list
                    )
