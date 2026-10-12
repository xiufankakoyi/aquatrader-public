# strategies/optimized_strategy.py
import pandas as pd
import numpy as np
from config.config import Config
from core.quant.strategies.strategy_framework import StrategyBase

class OptimizedStrategy(StrategyBase):
    # CHANGED: 内部定义策略名称
    strategy_id = "optimized_volume_v1"
    strategy_name = "优化版市值量价策略"

    def __init__(self):
        super().__init__(name=self.strategy_name)
        self._cache = {}  # 添加缓存机制

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        优化版策略生成 - 向量化计算 + 缓存
        """
        signals = {}

        if stock_pool_today is None:
            return signals

        import numpy as np
        if isinstance(stock_pool_today, np.ndarray):
            return signals

        if hasattr(stock_pool_today, 'empty') and stock_pool_today.empty:
            return signals

        stock_pool = stock_pool_today

        # 使用向量化操作替代循环
        try:
            # 1. 预筛选候选股票（向量化）
            candidate_mask = (
                (stock_pool['total_mv'] >= 500000) &
                (stock_pool['total_mv'] <= 5000000) &
                (stock_pool['close'] >= Config.MIN_PRICE) &
                (stock_pool['turnover_rate'] >= 2.0) &
                (stock_pool['turnover_rate'] <= 20.0) &
                (stock_pool['is_limit_up'] == 0) &
                (stock_pool['close'] > stock_pool['ma20'])
            )

            candidate_stocks = stock_pool[candidate_mask]

            if len(candidate_stocks) == 0:
                return signals

            # 2. 批量计算量比（避免逐股查询）
            candidate_stocks = self._batch_calculate_volume_ratio(
                candidate_stocks, current_date, data_query
            )

            # 3. 最终筛选条件（向量化）
            # CHANGED: 尾盘买入 - 大阳线条件：close >= open * 1.03（优先使用 open/close，若无则用 change_pct）
            has_open_close = 'open' in candidate_stocks.columns and 'close' in candidate_stocks.columns
            has_change_pct = 'change_pct' in candidate_stocks.columns

            if has_open_close:
                # 优先使用 open/close 判定大阳线
                big_yang_mask = candidate_stocks['close'] >= candidate_stocks['open'] * 1.03
                # 防御：如果 open 为 0 或缺失，回退到 change_pct
                if has_change_pct:
                    big_yang_mask = big_yang_mask | (
                        (candidate_stocks['open'].isna() | (candidate_stocks['open'] <= 0)) &
                        (candidate_stocks['change_pct'] >= 3.0)
                    )
            elif has_change_pct:
                # 只有 change_pct 时，使用 change_pct >= 3%
                big_yang_mask = candidate_stocks['change_pct'] >= 3.0
            else:
                # 防御：两者都没有时，跳过此条件（TODO: 需要补充数据源）
                print("[优化策略] 警告：缺少 open/close 或 change_pct，无法判定大阳线，跳过该条件")
                big_yang_mask = pd.Series(True, index=candidate_stocks.index)

            # CHANGED: 尾盘买入 - 排除冲高回落：(high - close) / close <= 0.03
            if 'high' in candidate_stocks.columns and 'close' in candidate_stocks.columns:
                pullback_mask = (
                    (candidate_stocks['high'] - candidate_stocks['close']) /
                    candidate_stocks['close'].replace(0, pd.NA)
                ) <= 0.03
                pullback_mask = pullback_mask.fillna(False)  # 处理除零或缺失值
            else:
                # TODO: 若无 high 列，保留注释但不影响其他逻辑
                pullback_mask = pd.Series(True, index=candidate_stocks.index)

            buy_mask = (
                (candidate_stocks['volume_ratio'] > 1.2) &
                big_yang_mask &  # CHANGED: 尾盘买入 - 大阳线条件
                pullback_mask  # CHANGED: 尾盘买入 - 排除冲高回落
            )

            # 4. 生成信号
            buy_stocks = candidate_stocks[buy_mask]
            for stock_code in buy_stocks['stock_code']:
                signals[stock_code] = 'buy'

            # 对其他股票标记hold
            for stock_code in candidate_stocks['stock_code']:
                if stock_code not in signals:
                    signals[stock_code] = 'hold'

        except Exception as e:
            print(f"策略生成出错: {e}")

        return signals

    def _batch_calculate_volume_ratio(self, stocks, current_date, data_query):
        """批量计算量比 - 大幅减少数据库查询次数"""
        try:
            # 转换日期格式
            current_date_str = current_date
            if isinstance(current_date, pd.Timestamp):
                current_date_str = current_date.strftime('%Y-%m-%d')

            # 计算开始日期
            if isinstance(current_date, pd.Timestamp):
                start_date = (current_date - pd.Timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                current_dt = pd.to_datetime(current_date)
                start_date = (current_dt - pd.Timedelta(days=30)).strftime('%Y-%m-%d')

            # 批量获取所有候选股票的历史数据
            stock_codes = stocks['stock_code'].tolist()
            batch_history = self._get_batch_stock_history(
                stock_codes, start_date, current_date_str, data_query
            )

            # 计算量比
            volume_ratios = []
            for stock_code in stock_codes:
                if stock_code in batch_history and len(batch_history[stock_code]) >= 5:
                    history_df = batch_history[stock_code]
                    current_volume = stocks[stocks['stock_code'] == stock_code]['volume'].iloc[0]
                    avg_volume_5d = history_df['volume'].tail(5).mean()
                    volume_ratio = current_volume / avg_volume_5d if avg_volume_5d > 0 else 0
                else:
                    volume_ratio = 0
                volume_ratios.append(volume_ratio)

            # 添加量比列
            stocks = stocks.copy()
            stocks['volume_ratio'] = volume_ratios

            return stocks

        except Exception as e:
            print(f"批量计算量比出错: {e}")
            stocks['volume_ratio'] = 0
            return stocks

    def _get_batch_stock_history(self, stock_codes, start_date, end_date, data_query):
        """批量获取股票历史数据 - 减少数据库查询次数"""
        batch_history = {}

        try:
            # 使用优化后的批量查询方法
            batch_df = data_query.get_batch_stock_history(
                stock_codes, start_date, end_date,
                columns=['stock_code', 'trade_date', 'volume']
            )

            if not batch_df.empty:
                for stock_code, group in batch_df.groupby('stock_code'):
                    batch_history[stock_code] = group

        except Exception as e:
            print(f"批量获取股票历史数据出错: {e}")

        return batch_history