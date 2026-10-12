"""
回测信号层

负责策略上下文设置、信号生成。
"""

import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class SignalLayer:
    """
    回测信号层

    负责策略上下文设置、信号生成。
    """

    def __init__(self, engine):
        """
        初始化信号层。

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

    def _set_strategy_context(
        self,
        strategy: Any,
        current_time: pd.Timestamp,
        portfolio: Dict[str, int],
        cash: float
    ):
        """设置策略运行时上下文"""
        if hasattr(strategy, 'set_runtime_context'):
            strategy.set_runtime_context(
                current_date=current_time.strftime("%Y-%m-%d"),
                portfolio=portfolio,
                cash=cash
            )

    def _generate_signals(
        self,
        strategy: Any,
        current_time: pd.Timestamp,
        stock_pool: Any,
        preloaded_data: Optional[Dict],
        idx: int,
        time_series: List[pd.Timestamp]
    ) -> Dict[str, Any]:
        """生成交易信号"""
        # 检查是否是配置化策略
        if isinstance(strategy, dict) or hasattr(strategy, 'to_dict'):
            return self._generate_signals_from_config(strategy, current_time, stock_pool)

        # 检查向量化模式
        if hasattr(strategy, 'generate_signals_vectorized') and preloaded_data is not None:
            if idx == 1:
                # FIX: 传递 current_time 给 _generate_vectorized_signals
                return self._generate_vectorized_signals(strategy, preloaded_data, time_series, current_time)
            elif self._vectorized_mode:
                return self._get_vectorized_signals_for_day(current_time)

        # 传统模式
        if hasattr(strategy, 'generate_signals'):
            stock_pool_for_strategy = stock_pool
            if hasattr(stock_pool, 'to_pandas'):
                stock_pool_for_strategy = stock_pool.to_pandas()
            elif isinstance(stock_pool, np.ndarray):
                return {}
            return strategy.generate_signals(
                current_date=current_time.strftime("%Y-%m-%d"),
                stock_pool_today=stock_pool_for_strategy,
                data_query=self.data_query
            )

        return {}

    def _generate_signals_from_config(
        self,
        config: Any,
        current_time: pd.Timestamp,
        stock_pool: Any
    ) -> Dict[str, Any]:
        """从配置生成信号（待实现）"""
        # TODO: 集成配置化策略系统
        logger.warning("配置化策略系统尚未实现")
        return {}

    def _generate_vectorized_signals(
        self,
        strategy: Any,
        preloaded_data: Dict,
        time_series: List[pd.Timestamp],
        current_time: pd.Timestamp
    ) -> Dict[str, Any]:
        """生成向量化信号 - 使用矩阵缓存管理器"""
        import time
        t_start = time.perf_counter()

        # FIX: 使用 time_series 作为 trading_dates，确保与回测范围一致
        backtest_dates = [ts.strftime("%Y-%m-%d") for ts in time_series]

        if 'daily' in preloaded_data:
            stock_daily = preloaded_data['daily']
            all_codes = stock_daily['stock_code'].unique().to_list()
            # FIX: 确保股票代码是6位字符串格式
            stock_codes_list = sorted([str(c).zfill(6) for c in all_codes])
        else:
            all_codes = set()
            for df in preloaded_data.values():
                if df is not None and len(df) > 0:
                    if hasattr(df, 'is_empty'):
                        all_codes.update(df['stock_code'].unique().to_list())
                    else:
                        all_codes.update(df['stock_code'].unique())
            # FIX: 确保股票代码是6位字符串格式
            stock_codes_list = sorted([str(c).zfill(6) for c in all_codes])

        # FIX: 优先使用 self._factor_matrix，确保信号生成和交易执行使用相同的数据
        if self._factor_matrix is not None:
            fm = self._factor_matrix
            factor_idx = {name: i for i, name in enumerate(fm.factor_names)}

            # 检查因子矩阵是否包含所需字段
            open_idx = factor_idx.get('open', -1)
            high_idx = factor_idx.get('high', -1)
            low_idx = factor_idx.get('low', -1)
            close_idx = factor_idx.get('close', -1)

            # 前复权价格索引
            open_adj_idx = factor_idx.get('open_adj', -1)
            high_adj_idx = factor_idx.get('high_adj', -1)
            low_adj_idx = factor_idx.get('low_adj', -1)
            close_adj_idx = factor_idx.get('close_adj', -1)

            if open_idx >= 0 and high_idx >= 0 and low_idx >= 0 and close_idx >= 0:
                # 检查因子矩阵的日期和股票代码是否匹配
                if len(fm.codes_str) == len(stock_codes_list):
                    # 不复权价格矩阵（用于交易执行）
                    price_matrix = np.stack([
                        fm.values[:, :, open_idx],
                        fm.values[:, :, high_idx],
                        fm.values[:, :, low_idx],
                        fm.values[:, :, close_idx]
                    ], axis=2)

                    # 前复权价格矩阵（用于指标计算）
                    # 如果有前复权价格字段，使用它；否则使用不复权价格
                    if open_adj_idx >= 0 and close_adj_idx >= 0:
                        price_matrix_adj = np.stack([
                            fm.values[:, :, open_adj_idx],
                            fm.values[:, :, high_adj_idx] if high_adj_idx >= 0 else fm.values[:, :, high_idx],
                            fm.values[:, :, low_adj_idx] if low_adj_idx >= 0 else fm.values[:, :, low_idx],
                            fm.values[:, :, close_adj_idx]
                        ], axis=2)
                    else:
                        price_matrix_adj = price_matrix

                    # 使用因子矩阵的完整日期列表
                    trading_dates = fm.dates
                    T = len(trading_dates)
                    logger.info(f"[_generate_vectorized_signals] 使用 self._factor_matrix (完整日期: {len(fm.dates)}天)")
                    t_matrix_build = (time.perf_counter() - t_start) * 1000
                    t_prepare_data = 0
                else:
                    # 回退到 MatrixCacheManager
                    T = len(backtest_dates)
                    N = len(stock_codes_list)
                    price_matrix = self.engine.compute_layer._build_price_matrix_from_cache(
                        preloaded_data, backtest_dates, stock_codes_list, T, N
                    )
                    price_matrix_adj = price_matrix
                    trading_dates = backtest_dates
                    t_matrix_build = (time.perf_counter() - t_start) * 1000
                    t_prepare_data = t_matrix_build
            else:
                # 回退到 MatrixCacheManager
                T = len(backtest_dates)
                N = len(stock_codes_list)
                price_matrix = self.engine.compute_layer._build_price_matrix_from_cache(
                    preloaded_data, backtest_dates, stock_codes_list, T, N
                )
                price_matrix_adj = price_matrix
                trading_dates = backtest_dates
                t_matrix_build = (time.perf_counter() - t_start) * 1000
                t_prepare_data = t_matrix_build
        else:
            # 回退到 MatrixCacheManager
            T = len(backtest_dates)
            N = len(stock_codes_list)
            price_matrix = self.engine.compute_layer._build_price_matrix_from_cache(
                preloaded_data, backtest_dates, stock_codes_list, T, N
            )
            price_matrix_adj = price_matrix
            trading_dates = backtest_dates
            t_matrix_build = (time.perf_counter() - t_start) * 1000
            t_prepare_data = t_matrix_build

        t_matrix_built = time.perf_counter()

        # [声明式因子系统] 注入预加载的因子到策略
        if hasattr(self, '_preloaded_factors') and self._preloaded_factors:
            strategy.factors = self._preloaded_factors
            for factor_name, matrix in self._preloaded_factors.items():
                setattr(strategy, factor_name, matrix)

        # 调用策略的向量化信号生成
        # 传递两个价格矩阵：
        # - price_matrix: 不复权价格（用于交易）
        # - price_matrix_adj: 前复权价格（用于指标计算）
        signal_matrix = strategy.generate_signals_vectorized(
            price_matrix=price_matrix,
            trading_dates=trading_dates,
            stock_codes=stock_codes_list,
            data_query=self.data_query,
            preloaded_data=preloaded_data,
            price_matrix_adj=price_matrix_adj
        )

        t_signals_done = time.perf_counter()

        # 保存向量化状态
        self._vectorized_mode = True
        self._signal_matrix = signal_matrix
        self._stock_codes_list = stock_codes_list
        # FIX: _date_to_idx 基于完整日期构建（包含 warmup）
        # 这样 _get_vectorized_signals_for_day 可以正确获取任何日期的信号
        # 标准化日期格式为 YYYY-MM-DD（不含时间），与 _backtest_dates 保持一致
        trading_dates_str = [d.split(' ')[0] if isinstance(d, str) and ' ' in d else str(d)[:10] for d in trading_dates]
        self._date_to_idx = {d: i for i, d in enumerate(trading_dates_str)}
        # FIX: 保存回测日期范围，用于过滤信号
        self._backtest_dates = set(backtest_dates)

        # DEBUG
        # 性能日志
        matrix_time = (t_matrix_built - t_start) * 1000
        signal_time = (t_signals_done - t_matrix_built) * 1000
        total_time = (t_signals_done - t_start) * 1000
        logger.info(f"[_generate_vectorized_signals] matrix_build: {t_matrix_build:.1f}ms, strategy: {signal_time:.1f}ms, 总计: {total_time:.1f}ms")

        # FIX: 不返回第一天的信号，让主循环统一处理所有天数
        # 这样可以避免第一天的 equity curve 被重复添加
        return self._get_vectorized_signals_for_day(current_time)

    def _get_vectorized_signals_for_day(self, current_time: pd.Timestamp) -> Dict[str, Any]:
        """从向量化矩阵获取指定日期的信号（优化版）"""
        current_date_str = current_time.strftime("%Y-%m-%d")

        # FIX: 检查当前日期是否在回测日期范围内
        # 如果是 warmup 日期，不生成信号
        if hasattr(self, '_backtest_dates') and current_date_str not in self._backtest_dates:
            return {}

        t_idx = self._date_to_idx.get(current_date_str, -1)

        if t_idx < 0 or self._signal_matrix is None:
            return {}

        # FIX: 检查索引是否越界
        if t_idx >= self._signal_matrix.shape[0]:
            return {}

        signals = {}
        day_signals = self._signal_matrix[t_idx, :]

        # FIX: 支持两种信号格式：1=买入, -1或2=卖出
        buy_mask = day_signals == 1
        sell_mask = (day_signals == -1) | (day_signals == 2)

        if buy_mask.any():
            buy_indices = np.where(buy_mask)[0]
            for idx in buy_indices:
                signals[self._stock_codes_list[idx]] = {'action': 'buy', 'indicators': {}}

        if sell_mask.any():
            sell_indices = np.where(sell_mask)[0]
            for idx in sell_indices:
                signals[self._stock_codes_list[idx]] = {'action': 'sell', 'indicators': {}}

        return signals
