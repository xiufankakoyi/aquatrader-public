# strategies/simple_test_strategy.py
"""
简单的测试策略，用于验证回测引擎是否能正常产生交易记录
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import pandas as pd

from core.quant.strategies.strategy_framework import StrategyBase


@dataclass(frozen=True)
class SimpleTestConfig:
    """简单测试策略配置"""
    market_cap_min: float = 10 * 10_000  # 10亿
    market_cap_max: float = 100 * 10_000  # 100亿
    buy_every_n_days: int = 5  # 每N天买入一次
    sell_after_days: int = 10  # 持有N天后卖出
    max_stocks: int = 5  # 最多持有5只股票


class SimpleTestStrategy(StrategyBase):
    """
    简单的测试策略，用于验证回测引擎是否能正常产生交易记录
    """
    strategy_id = "simple_test"
    strategy_name = "简单测试策略"
    needs_today_pool = True  # 策略需要当日股票池数据

    PARAM_SPEC = [
        {
            "key": "market_cap_min",
            "label": "最小市值（亿）",
            "group": "基础配置",
            "type": "float",
            "min": 1.0,
            "max": 500.0,
            "step": 1.0,
            "default": SimpleTestConfig().market_cap_min / 10_000,
            "optimize": False,
        },
        {
            "key": "market_cap_max",
            "label": "最大市值（亿）",
            "group": "基础配置",
            "type": "float",
            "min": 1.0,
            "max": 1000.0,
            "step": 1.0,
            "default": SimpleTestConfig().market_cap_max / 10_000,
            "optimize": False,
        },
        {
            "key": "buy_every_n_days",
            "label": "每N天买入一次",
            "group": "交易配置",
            "type": "int",
            "min": 1,
            "max": 30,
            "step": 1,
            "default": SimpleTestConfig().buy_every_n_days,
            "optimize": False,
        },
        {
            "key": "sell_after_days",
            "label": "持有N天后卖出",
            "group": "交易配置",
            "type": "int",
            "min": 1,
            "max": 60,
            "step": 1,
            "default": SimpleTestConfig().sell_after_days,
            "optimize": False,
        },
    ]

    @classmethod
    def get_param_spec(cls):
        return cls.PARAM_SPEC

    def __init__(
        self,
        config: SimpleTestConfig | None = None,
        market_cap_min: float | None = None,
        market_cap_max: float | None = None,
        buy_every_n_days: int | None = None,
        sell_after_days: int | None = None,
        max_stocks: int | None = None,
    ):
        super().__init__(name=self.strategy_name)
        base_config = config or SimpleTestConfig()

        overrides = {}
        if market_cap_min is not None:
            overrides["market_cap_min"] = float(market_cap_min) * 10_000  # 转换为亿元
        if market_cap_max is not None:
            overrides["market_cap_max"] = float(market_cap_max) * 10_000  # 转换为亿元
        if buy_every_n_days is not None:
            overrides["buy_every_n_days"] = int(buy_every_n_days)
        if sell_after_days is not None:
            overrides["sell_after_days"] = int(sell_after_days)
        if max_stocks is not None:
            overrides["max_stocks"] = int(max_stocks)

        self.config = base_config.__class__(**{**base_config.__dict__, **overrides}) if overrides else base_config

        self.required_days = 0
        self.position_ratio = 1.0 / self.config.max_stocks
        self.current_positions: Dict[str, int] = {}  # 存储每个股票的持有天数
        self.trading_day_count = 0  # 交易天数计数器

    def generate_signals(self, current_date, stock_pool_today, data_query):
        """生成交易信号"""
        final_signals: Dict[str, str] = {}

        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        self.trading_day_count += 1

        # 1) 卖出逻辑：持有天数超过设定值则卖出
        sell_codes = []
        for code, hold_days in self.current_positions.items():
            self.current_positions[code] += 1
            if self.current_positions[code] >= self.config.sell_after_days:
                sell_codes.append(code)
                final_signals[code] = "sell"
                print(f"[测试策略] {current_date} 卖出 {code}，持有 {self.current_positions[code]} 天")

        # 从持仓中移除已卖出的股票
        for code in sell_codes:
            del self.current_positions[code]

        # 2) 买入逻辑：每N天买入一次，最多持有max_stocks只股票
        if self.trading_day_count % self.config.buy_every_n_days == 0 and len(self.current_positions) < self.config.max_stocks:
            # 筛选符合市值条件的股票
            mask = ((stock_pool_today["total_mv"] >= self.config.market_cap_min) & 
                    (stock_pool_today["total_mv"] <= self.config.market_cap_max))
            mask &= stock_pool_today["is_st"].eq(0)
            
            candidate_stocks = stock_pool_today[mask].copy()
            if candidate_stocks.empty:
                print(f"[测试策略] {current_date} 没有符合条件的股票")
                return final_signals

            # 按成交额降序排序，选择前N只
            sort_col = "amount" if "amount" in candidate_stocks.columns else "total_mv"
            candidate_stocks = candidate_stocks.sort_values(sort_col, ascending=False)
            
            # 选择未持仓的股票
            available_stocks = candidate_stocks[~candidate_stocks["stock_code"].isin(self.current_positions.keys())]
            if available_stocks.empty:
                print(f"[测试策略] {current_date} 没有可买入的新股票")
                return final_signals

            # 买入前几只股票
            buy_candidates = available_stocks["stock_code"].tolist()[:self.config.max_stocks - len(self.current_positions)]
            for code in buy_candidates:
                final_signals[code] = "buy"
                self.current_positions[code] = 0  # 重置持有天数
                print(f"[测试策略] {current_date} 买入 {code}")

        return final_signals

    def _pre_screen_stocks(self, stock_pool: pd.DataFrame, date: str, data_query) -> List[str]:
        """预筛选股票"""
        return []

    def _get_sell_signals(self, stock_pool, date, data_query):
        """获取卖出信号"""
        return []


# 注册策略
# 注意：这个策略会在策略工厂初始化时自动注册
