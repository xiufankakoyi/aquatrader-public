"""
示例策略 - 演示如何使用动态指标注入功能

这个策略展示了如何：
1. 通过 get_required_indicators 声明需要的指标
2. 在策略逻辑中使用这些指标
"""

from core.quant.strategies.strategy_framework import StrategyBase
import pandas as pd
from typing import List, Dict, Any


class ExampleStrategyWithIndicators(StrategyBase):
    """
    示例策略：使用 RSI 和 ATR 指标进行交易

    策略逻辑：
    - 当 RSI < 30 且 ATR 上升时买入
    - 当 RSI > 70 时卖出
    """

    strategy_name = "示例策略（带指标）"
    needs_today_pool = True  # 策略需要当日股票池数据

    def get_required_indicators(self) -> List[Dict[str, Any]]:
        """
        声明策略需要的指标

        返回指标配置列表，每个配置包含：
        - type: 指标类型（'ma', 'ema', 'rsi', 'macd', 'bollinger', 'atr'）
        - column: 计算指标的基础列（如 'close', 'high', 'low'）
        - window: 窗口大小（周期）
        - name: 指标列名（可选，默认使用 type_window 格式）
        """
        return [
            {
                'type': 'rsi',
                'column': 'close',
                'window': 14,
                'name': 'rsi14'
            },
            {
                'type': 'atr',
                'window': 14,
                'name': 'atr14'
            },
            {
                'type': 'ma',
                'column': 'close',
                'window': 20,
                'name': 'ma20'
            }
        ]

    def generate_signals(self, current_date: str, stock_pool_today: pd.DataFrame, data_query) -> Dict[str, str]:
        """
        生成交易信号

        注意：在回测前，引擎已经通过 prepare_strategy_execution 计算并注入了
        RSI、ATR、MA20 等指标，所以 stock_pool_today 中应该已经包含这些列
        """
        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        signals = {}

        # 检查指标列是否存在
        required_cols = ['rsi14', 'atr14', 'ma20']
        missing_cols = [col for col in required_cols if col not in stock_pool_today.columns]
        if missing_cols:
            # 如果指标不存在，说明 prepare_strategy_execution 可能失败了
            # 这里可以选择回退到不使用指标，或者报错
            print(f"警告：指标列缺失: {missing_cols}，策略将使用基础数据")
            return {}

        # 获取历史数据（用于计算 ATR 的变化趋势）
        for stock_code in stock_pool_today['stock_code']:
            try:
                # 获取当前股票的数据
                stock_data = stock_pool_today[stock_pool_today['stock_code'] == stock_code]
                if stock_data.empty:
                    continue

                row = stock_data.iloc[0]
                rsi = row.get('rsi14', 50)
                atr = row.get('atr14', 0)
                ma20 = row.get('ma20', row.get('close', 0))
                close = row.get('close', 0)

                # 获取历史数据以计算 ATR 趋势
                start_date = (pd.to_datetime(current_date) - pd.Timedelta(days=30)).strftime('%Y-%m-%d')
                history = data_query.get_batch_stock_history(
                    [stock_code],
                    start_date,
                    current_date,
                    columns=['stock_code', 'trade_date', 'atr14']
                )

                # 计算 ATR 趋势（简单判断：最近 5 天的 ATR 是否上升）
                atr_rising = False
                if not history.empty and 'atr14' in history.columns:
                    recent_atr = history['atr14'].tail(5)
                    if len(recent_atr) >= 2:
                        atr_rising = recent_atr.iloc[-1] > recent_atr.iloc[-2]

                # 策略逻辑
                # 买入条件：RSI < 30（超卖）且 ATR 上升（波动增加）且价格在 MA20 上方
                if rsi < 30 and atr_rising and close > ma20:
                    signals[stock_code] = 'buy'
                # 卖出条件：RSI > 70（超买）
                elif rsi > 70:
                    signals[stock_code] = 'sell'
                else:
                    signals[stock_code] = 'hold'

            except Exception as e:
                print(f"处理股票 {stock_code} 时出错: {e}")
                signals[stock_code] = 'hold'

        return signals

