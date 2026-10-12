# core/strategies/ma_comparison_strategy.py

import pandas as pd
from typing import Dict, List, Any
from core.quant.strategies.strategy_framework import StrategyBase

class MAComparisonStrategy(StrategyBase):
    """
    双均线校准策略 (MA5 vs MA10)

    本策略用于核心逻辑校准，具有以下特性：
    1. 标的固定：仅针对中国银行 (601988) 进行交易。
    2. 信号逻辑：基于 T 日收盘价计算 MA5/MA10。
       - 金叉 (MA5上穿MA10): 买入信号。
       - 死叉 (MA5下穿MA10): 卖出信号。
    3. 执行时间：T+1 日开盘价成交。
    4. 仓位管理：全仓买入 (95% 比例)，卖出时全仓平仓。

    注意：在前端运行回测时，请确保：
    - 股票池中包含 "601988"。
    - 佣金和最小手续费设置为 0（以实现“零摩擦”测试）。
    """
    strategy_name = "双均线校准策略 (MA5 vs MA10)"

    def __init__(self, name: str = None):
        super().__init__(name)
        self.required_days = 60  # 保证足够的历史数据计算 MA10
        self.execution_price = {
            "buy": "open",
            "sell": "open",
            "default": "open"
        }
        # 默认持仓比例 (1.0 表示全仓)
        self.position_ratio = 1.0
        self.max_positions = 1

        # 【验证专用】零摩擦设置
        self.commission_rate = 0.0
        self.min_commission = 0.0
        self.sell_tax = 0.0

    def generate_signals(self, current_date: str, stock_pool_today: Any, data_query) -> Dict[str, str]:
        # 标的：601988 (中国银行) - 数据库中使用无后缀代码
        target_code = "601988"

        # 兼容 Polars 和 Pandas
        is_empty = False
        if stock_pool_today is None:
            is_empty = True
        elif hasattr(stock_pool_today, "is_empty"):
            is_empty = stock_pool_today.is_empty()
        elif hasattr(stock_pool_today, "empty"):
            is_empty = stock_pool_today.empty
        else:
            is_empty = len(stock_pool_today) == 0

        if is_empty:
            return {}

        # 确保目标在股票池中
        found = False
        if hasattr(stock_pool_today, "filter"):
            # Polars
            # 注意: stock_code 可能是 int 或 str，统一按 str 比较
            if target_code in [str(x) for x in stock_pool_today['stock_code'].to_list()]:
                found = True
        else:
            # Pandas
            if target_code in stock_pool_today['stock_code'].astype(str).values:
                found = True

        if not found:
            return {}

        # 获取历史数据。我们需要计算到 T 日（current_date 的前一天）的 MA
        # 这里的 current_date 是 T+1 日（交易执行日）
        # 所以我们需要 fetch 到 current_date 之前的最后一条记录作为 T 日信号依据

        # 为了避免多次数据库查询，我们可以获取更长一点的历史
        lookback_start = (pd.to_datetime(current_date) - pd.Timedelta(days=60)).strftime('%Y-%m-%d')

        try:
            # 获取历史数据，包含当日（为确保能过滤掉它）
            history = data_query.get_batch_stock_history(
                [target_code],
                lookback_start,
                current_date,
                columns=['stock_code', 'trade_date', 'close']
            )

            if history.empty:
                return {}

            history = history.sort_values('trade_date')

            # 【关键】过滤掉 current_date 当天的数据，只使用 T 日及之前的数据产生信号
            # 这样 signals 产生的决策是基于 T 日收盘，执行在 T+1 日开盘
            history_t = history[history['trade_date'] < current_date].copy()

            if len(history_t) < 11:
                return {}

            # 计算指标 (使用收盘价)
            history_t['ma5'] = history_t['close'].rolling(window=5).mean()
            history_t['ma10'] = history_t['close'].rolling(window=10).mean()

            # 获取 T 日和 T-1 日的指标值
            t_row = history_t.iloc[-1]
            t_1_row = history_t.iloc[-2]

            ma5_t = t_row['ma5']
            ma10_t = t_row['ma10']
            ma5_t_1 = t_1_row['ma5']
            ma10_t_1 = t_1_row['ma10']

            if pd.isna(ma5_t_1) or pd.isna(ma10_t_1):
                return {}

            # 金叉：T-1日 MA5 <= MA10，T日 MA5 > MA10
            if ma5_t_1 <= ma10_t_1 and ma5_t > ma10_t:
                return {target_code: 'buy'}

            # 死叉：T-1日 MA5 >= MA10，T日 MA5 < MA10
            elif ma5_t_1 >= ma10_t_1 and ma5_t < ma10_t:
                return {target_code: 'sell'}

        except Exception as e:
            print(f"[MAStrategy] 信号计算错误: {e}")

        return {}
