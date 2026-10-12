"""
示例策略 - 演示如何使用持仓状态管理功能

这个策略展示了如何：
1. 使用策略运行器自动管理的持仓状态（持仓天数等）
2. 在策略逻辑中访问持仓信息
3. 基于持仓天数进行交易决策
"""

from core.quant.strategies.strategy_framework import StrategyBase
import pandas as pd
from typing import Dict, Any


class ExampleStrategyWithHoldingState(StrategyBase):
    """
    示例策略：基于持仓天数进行交易

    策略逻辑：
    - 买入：RSI < 30（超卖）
    - 卖出：持仓超过 10 天 或 RSI > 70（超买）

    这个策略展示了如何使用引擎自动管理的持仓状态
    """

    strategy_name = "示例策略（持仓状态）"
    needs_today_pool = True  # 策略需要当日股票池数据

    def get_required_indicators(self) -> list:
        """声明需要的指标"""
        return [
            {
                'type': 'rsi',
                'column': 'close',
                'window': 14,
                'name': 'rsi14'
            }
        ]

    def generate_signals(self, current_date: str, stock_pool_today: pd.DataFrame, data_query) -> Dict[str, str]:
        """
        生成交易信号

        注意：持仓状态（holding_state）由引擎自动管理，策略可以直接访问
        """
        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        signals = {}

        # 检查指标列是否存在
        if 'rsi14' not in stock_pool_today.columns:
            return {}

        # 获取当前持仓状态（由引擎自动维护）
        # holding_state 格式：{stock_code: {'entry_date': '2024-01-01', 'days_held': 5, ...}}
        holding_state = getattr(self, 'holding_state', {})

        # 遍历股票池
        for _, row in stock_pool_today.iterrows():
            stock_code = row.get('stock_code')
            if not stock_code:
                continue

            rsi = row.get('rsi14', 50)

            # 检查是否已持仓
            is_holding = stock_code in holding_state
            holding_info = holding_state.get(stock_code, {})
            days_held = holding_info.get('days_held', 0)

            # 策略逻辑
            if not is_holding:
                # 未持仓：买入条件
                if rsi < 30:  # 超卖
                    signals[stock_code] = 'buy'
                else:
                    signals[stock_code] = 'hold'
            else:
                # 已持仓：卖出条件
                # 条件1：持仓超过 10 天
                # 条件2：RSI > 70（超买）
                if days_held >= 10 or rsi > 70:
                    signals[stock_code] = 'sell'
                else:
                    signals[stock_code] = 'hold'

        return signals

    def get_holding_info(self, stock_code: str) -> Dict[str, Any]:
        """
        获取指定股票的持仓信息（辅助方法）

        这个方法展示了如何访问持仓状态
        """
        holding_state = getattr(self, 'holding_state', {})
        return holding_state.get(stock_code, {})

