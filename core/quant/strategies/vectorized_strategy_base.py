# strategies/vectorized_strategy_base.py
"""
向量化策略基类 - 解决"混合体架构"的性能瓶颈

设计理念：
- 彻底向量化：策略直接操作全市场矩阵，而不是逐个股票循环
- 利用 NumPy/Pandas 的向量化操作，性能提升 100-1000 倍
- 适合大规模参数扫描和全市场回测

使用示例：
    class MyVectorizedStrategy(VectorizedStrategyBase):
        def generate_signals_vectorized(self, market_matrix, current_date):
            # market_matrix: pd.DataFrame with MultiIndex (stock_code, trade_date)
            # 列: ['open', 'high', 'low', 'close', 'volume', ...]

            # 向量化计算：全市场一次性计算
            signals = pd.Series('hold', index=market_matrix.index.get_level_values(0).unique())

            # 例如：收盘价 > MA20
            close = market_matrix['close'].unstack('stock_code')
            ma20 = close.rolling(20).mean()
            buy_mask = close.iloc[-1] > ma20.iloc[-1]
            signals[buy_mask] = 'buy'

            return signals
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Any
from core.quant.strategies.strategy_framework import StrategyBase


class VectorizedStrategyBase(StrategyBase):
    """
    向量化策略基类

    核心改进：
    1. generate_signals_vectorized() 方法接收全市场矩阵，一次性计算所有股票信号
    2. 不再使用 for code in stocks["stock_code"] 的 Python 循环
    3. 利用 Pandas/NumPy 的向量化操作，性能提升 100-1000 倍
    """

    # Base class for vectorized strategies

    def __init__(self, name: str | None = None):
        super().__init__(name)
        # 标记为向量化策略
        self.is_vectorized = True

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        重写基类方法，将事件驱动转换为向量化调用

        流程：
        1. 批量获取全市场历史数据（矩阵形式）
        2. 调用 generate_signals_vectorized() 进行向量化计算
        3. 返回信号字典
        """
        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        # 1. 预筛选
        candidate_df = self._pre_screen_stocks(stock_pool_today)
        if candidate_df.empty:
            return {}

        # 2. 批量获取历史数据（矩阵形式）
        market_matrix = self._prepare_market_matrix(
            candidate_df,
            current_date,
            data_query
        )

        if market_matrix.empty:
            return {}

        # 3. 向量化计算信号
        try:
            signals_series = self.generate_signals_vectorized(
                market_matrix,
                current_date
            )

            # 4. 转换为标准格式 {code: 'buy'/'sell'/'hold'}
            signals_dict = signals_series.to_dict()

            # 5. 缓存富信号（可选）
            self.last_rich_signals = {
                code: {
                    "action": signal,
                    "weight": 1.0 / len(signals_dict) if signal == 'buy' else 0.0,
                    "score": None,
                    "params": {},
                }
                for code, signal in signals_dict.items()
            }

            return signals_dict

        except Exception as e:
            print(f"[VectorizedStrategy] 向量化计算失败: {e}")
            import traceback
            traceback.print_exc()
            return {}

    def generate_signals_vectorized(
        self,
        market_matrix: pd.DataFrame,
        current_date: str
    ) -> pd.Series:
        """
        核心方法：向量化生成信号

        参数：
            market_matrix: pd.DataFrame
                - MultiIndex: (stock_code, trade_date)
                - 列: ['open', 'high', 'low', 'close', 'volume', ...]
                - 已按 (stock_code, trade_date) 排序
            current_date: str
                当前回测日期

        返回：
            pd.Series
                - Index: stock_code
                - Values: 'buy' / 'sell' / 'hold'

        子类必须实现此方法
        """
        raise NotImplementedError(
            "子类必须实现 generate_signals_vectorized() 方法"
        )

    def _prepare_market_matrix(
        self,
        stocks: pd.DataFrame,
        current_date: str,
        data_query
    ) -> pd.DataFrame:
        """
        准备全市场矩阵数据

        返回：
            pd.DataFrame with MultiIndex (stock_code, trade_date)
        """
        codes = stocks["stock_code"].tolist()
        start_date = self._get_start_date(current_date)

        try:
            # 批量获取历史数据
            batch_hist = data_query.get_batch_stock_history(
                codes,
                start_date,
                current_date,
                columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume", "adj_factor"],
            )

            if batch_hist.empty:
                return pd.DataFrame()

            # 【核心修复 B】统一使用前复权逻辑
            # 注意：策略层无法直接访问引擎的 latest_factors，这里使用后复权作为过渡方案
            # TODO: 后续应该由引擎统一提供前复权后的历史数据，策略层不再处理复权
            # 当前使用 apply_forward_adjustment（后复权）作为过渡，虽然不完美，但至少保证数据一致性
            from utils.price_adjustment import apply_forward_adjustment
            if 'adj_factor' in batch_hist.columns:
                batch_hist = apply_forward_adjustment(batch_hist)

            # 设置 MultiIndex
            batch_hist = batch_hist.set_index(['stock_code', 'trade_date'])
            batch_hist = batch_hist.sort_index()

            return batch_hist

        except Exception as e:
            print(f"[VectorizedStrategy] 准备市场矩阵失败: {e}")
            return pd.DataFrame()

    def _get_matrix_snapshot(
        self,
        market_matrix: pd.DataFrame,
        date: Optional[str] = None
    ) -> pd.DataFrame:
        """
        获取指定日期的市场快照（用于向量化计算）

        参数：
            market_matrix: 全市场矩阵
            date: 日期（如果为None，返回最新日期）

        返回：
            pd.DataFrame with Index=stock_code, Columns=OHLCV等
        """
        if market_matrix.empty:
            return pd.DataFrame()

        if date is None:
            # 获取最新日期
            latest_date = market_matrix.index.get_level_values(1).max()
            snapshot = market_matrix.xs(latest_date, level=1)
        else:
            snapshot = market_matrix.xs(date, level=1)

        return snapshot

    def _calculate_vectorized_ma(
        self,
        market_matrix: pd.DataFrame,
        column: str = 'close',
        window: int = 20
    ) -> pd.Series:
        """
        向量化计算移动平均线

        返回：
            pd.Series with MultiIndex (stock_code, trade_date)
        """
        if market_matrix.empty:
            return pd.Series(dtype=float)

        # 按股票分组，计算滚动均值
        ma_series = (
            market_matrix[column]
            .groupby(level=0)
            .rolling(window=window, min_periods=1)
            .mean()
        )

        return ma_series

    def _calculate_vectorized_indicator(
        self,
        market_matrix: pd.DataFrame,
        indicator_func,
        *args,
        **kwargs
    ) -> pd.Series:
        """
        通用的向量化指标计算

        参数：
            market_matrix: 全市场矩阵
            indicator_func: 指标计算函数，接受单个股票的 Series，返回 Series
            *args, **kwargs: 传递给 indicator_func 的参数

        返回：
            pd.Series with MultiIndex (stock_code, trade_date)
        """
        results = []

        for code in market_matrix.index.get_level_values(0).unique():
            stock_data = market_matrix.xs(code, level=0)
            indicator_values = indicator_func(stock_data, *args, **kwargs)
            indicator_values.index = pd.MultiIndex.from_product(
                [[code], indicator_values.index],
                names=['stock_code', 'trade_date']
            )
            results.append(indicator_values)

        if results:
            return pd.concat(results)
        else:
            return pd.Series(dtype=float)


