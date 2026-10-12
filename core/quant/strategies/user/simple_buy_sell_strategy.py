from core.quant.strategies.strategy_framework import StrategyBase


class SimpleBuySellStrategy(StrategyBase):
    """最简单的买入卖出策略 - 用于验证回测链路"""
    strategy_id = "user_simple_buy_sell_strategy"
    strategy_name = "SimpleBuySellStrategy"

    def __init__(self, name=None):
        super().__init__(name)
        self._first_day = True
        self._buy_date = None

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        最简逻辑：
        - 第一个交易日买入第一只股票
        - 持有 5 个交易日后卖出
        - 其他情况 hold
        """
        signals = {}

        if stock_pool_today is None or len(stock_pool_today) == 0:
            return signals

        # 取第一只股票
        first_row = stock_pool_today.iloc[0]
        code = first_row.get("stock_code") or first_row.get("symbol_code")
        if not code:
            return signals

        if self._first_day:
            # 第一天买入
            signals[code] = "buy"
            self._first_day = False
            self._buy_date = current_date
        elif self._buy_date and self._holding_days(current_date, self._buy_date) >= 5:
            # 持有 5 天后卖出
            signals[code] = "sell"
            self._buy_date = None
        else:
            signals[code] = "hold"

        return signals

    @staticmethod
    def _holding_days(current_date: str, buy_date: str) -> int:
        """简单估算持有天数（日历日差）。"""
        from datetime import datetime
        try:
            d1 = datetime.strptime(current_date[:10], "%Y-%m-%d")
            d2 = datetime.strptime(buy_date[:10], "%Y-%m-%d")
            return (d1 - d2).days
        except Exception:
            return 0
