# strategies/example_vectorized_strategy.py
"""
示例向量化策略 - 展示如何使用新架构

策略逻辑：
- 使用向量化计算，一次性处理全市场
- 动态计算指标（不依赖数据库预计算）
- 展示性能优势
"""

import pandas as pd
import numpy as np
from core.quant.strategies.vectorized_strategy_base import VectorizedStrategyBase
from core.utils.indicator_calculator import IndicatorCalculator


class ExampleVectorizedStrategy(VectorizedStrategyBase):
    """
    示例向量化策略

    策略逻辑：
    1. 收盘价 > MA20（买入信号）
    2. 收盘价 < MA5（卖出信号）
    3. 成交量 > 5日均量（过滤条件）

    性能优势：
    - 全市场一次性计算，无需循环
    - 动态计算指标，灵活性强
    - 适合大规模参数扫描
    """

    strategy_name = "示例向量化策略"

    def __init__(self, name: str | None = None, ma_short: int = 5, ma_long: int = 20):
        super().__init__(name)
        self.ma_short = ma_short
        self.ma_long = ma_long

        # 初始化指标计算器
        self.indicator_calculator = IndicatorCalculator(enable_cache=True)

    def generate_signals_vectorized(
        self,
        market_matrix: pd.DataFrame,
        current_date: str
    ) -> pd.Series:
        """
        向量化生成信号

        核心优势：
        - 一次性处理全市场，无需循环
        - 利用 Pandas 向量化操作
        """
        if market_matrix.empty:
            return pd.Series(dtype=str)

        # 获取最新日期的快照
        latest_date = market_matrix.index.get_level_values(1).max()
        snapshot = market_matrix.xs(latest_date, level=1)

        # 初始化信号：全部为 'hold'
        signals = pd.Series('hold', index=snapshot.index)

        # 方法1：使用指标计算器（推荐）
        # 批量计算所需指标
        indicators_df = self.indicator_calculator.calculate_batch(
            market_matrix.reset_index(),
            [
                {'type': 'ma', 'column': 'close', 'window': self.ma_short, 'name': 'ma5'},
                {'type': 'ma', 'column': 'close', 'window': self.ma_long, 'name': 'ma20'},
                {'type': 'ma', 'column': 'volume', 'window': 5, 'name': 'volume_ma5'},
            ],
            group_by='stock_code'
        )

        # 获取最新日期的指标值
        latest_indicators = indicators_df[
            indicators_df['trade_date'] == latest_date
        ].set_index('stock_code')

        # 向量化条件判断
        close = snapshot['close']
        ma5 = latest_indicators['ma5']
        ma20 = latest_indicators['ma20']
        volume = snapshot['volume']
        volume_ma5 = latest_indicators['volume_ma5']

        # 买入条件：收盘价 > MA20 且 成交量 > 5日均量
        buy_mask = (close > ma20) & (volume > volume_ma5)
        signals[buy_mask] = 'buy'

        # 卖出条件：收盘价 < MA5
        sell_mask = close < ma5
        signals[sell_mask] = 'sell'

        return signals

    def generate_signals_vectorized_alternative(
        self,
        market_matrix: pd.DataFrame,
        current_date: str
    ) -> pd.Series:
        """
        替代实现：直接使用 Pandas 向量化操作（更高效）

        展示如何直接操作 MultiIndex DataFrame，避免 reset_index
        """
        if market_matrix.empty:
            return pd.Series(dtype=str)

        # 方法2：直接使用 MultiIndex 操作（性能最优）
        # 按股票分组，计算滚动均值
        close_series = market_matrix['close']
        volume_series = market_matrix['volume']

        # 计算 MA（按股票分组）
        ma5 = close_series.groupby(level=0).rolling(window=self.ma_short, min_periods=1).mean()
        ma20 = close_series.groupby(level=0).rolling(window=self.ma_long, min_periods=1).mean()
        volume_ma5 = volume_series.groupby(level=0).rolling(window=5, min_periods=1).mean()

        # 获取最新日期
        latest_date = market_matrix.index.get_level_values(1).max()

        # 提取最新日期的值
        latest_close = close_series.xs(latest_date, level=1)
        latest_ma5 = ma5.xs(latest_date, level=1)
        latest_ma20 = ma20.xs(latest_date, level=1)
        latest_volume = volume_series.xs(latest_date, level=1)
        latest_volume_ma5 = volume_ma5.xs(latest_date, level=1)

        # 初始化信号
        signals = pd.Series('hold', index=latest_close.index)

        # 向量化条件判断
        buy_mask = (latest_close > latest_ma20) & (latest_volume > latest_volume_ma5)
        sell_mask = latest_close < latest_ma5

        signals[buy_mask] = 'buy'
        signals[sell_mask] = 'sell'

        return signals


class AdvancedVectorizedStrategy(VectorizedStrategyBase):
    """
    高级向量化策略示例

    展示：
    - 复杂指标计算（RSI、MACD）
    - 多条件组合
    - 动态权重分配
    """

    strategy_name = "高级向量化策略"

    def __init__(self, name: str | None = None):
        super().__init__(name)
        self.indicator_calculator = IndicatorCalculator()

    def generate_signals_vectorized(
        self,
        market_matrix: pd.DataFrame,
        current_date: str
    ) -> pd.Series:
        """使用RSI和MACD的向量化策略"""
        if market_matrix.empty:
            return pd.Series(dtype=str)

        latest_date = market_matrix.index.get_level_values(1).max()

        # 计算多个指标
        indicators_df = self.indicator_calculator.calculate_batch(
            market_matrix.reset_index(),
            [
                {'type': 'rsi', 'column': 'close', 'window': 14, 'name': 'rsi14'},
                {'type': 'macd', 'column': 'close', 'name': 'macd'},
                {'type': 'ma', 'column': 'close', 'window': 20, 'name': 'ma20'},
            ],
            group_by='stock_code'
        )

        # 获取最新指标值
        latest_indicators = indicators_df[
            indicators_df['trade_date'] == latest_date
        ].set_index('stock_code')

        snapshot = market_matrix.xs(latest_date, level=1)
        signals = pd.Series('hold', index=snapshot.index)

        # 复杂条件组合
        rsi = latest_indicators['rsi14']
        macd_hist = latest_indicators['macd_histogram']
        close = snapshot['close']
        ma20 = latest_indicators['ma20']

        # 买入：RSI < 30（超卖）且 MACD 金叉 且 价格 > MA20
        buy_mask = (rsi < 30) & (macd_hist > 0) & (close > ma20)
        signals[buy_mask] = 'buy'

        # 卖出：RSI > 70（超买）或 MACD 死叉
        sell_mask = (rsi > 70) | (macd_hist < 0)
        signals[sell_mask] = 'sell'

        return signals









