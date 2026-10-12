"""
中长期趋势策略 V2 - 优化版

改进点：
1. 更严格的买入条件：趋势强度 + 成交量确认
2. 减少交易频率：只在趋势明确时买入
3. 更好的风控：动态止损 + 分批止盈
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from core.quant.strategies.strategy_framework import StrategyBase


@dataclass(frozen=True)
class TrendFollowV2Config:
    bias_threshold_high: float = 0.10
    bias_threshold_extreme: float = 0.15
    trend_ma_fast: int = 5
    trend_ma_slow: int = 10
    trend_ma_base: int = 20
    volume_ratio_min: float = 1.0
    min_list_days: int = 60
    position_ratio: float = 0.25
    max_stocks_per_day: int = 2
    max_positions: int = 6
    stop_loss_pct: float = 0.10
    trailing_stop_pct: float = 0.08
    min_hold_days: int = 3
    max_hold_days: int = 120
    
    market_cap_min: float = 20 * 10000
    market_cap_max: float = 5000 * 10000


class TrendFollowStrategyV2(StrategyBase):
    """
    中长期趋势跟踪策略 V2
    
    核心理念：
    - 只做确定性高的趋势
    - 严格的风控
    - 减少交易频率
    """
    strategy_id = "trend_follow_v2"
    strategy_name = "中长期趋势跟踪策略V2"
    needs_today_pool = True

    def __init__(self, **kwargs):
        super().__init__()
        base_config = TrendFollowV2Config()
        
        overrides = {}
        for key, val in kwargs.items():
            if hasattr(base_config, key):
                overrides[key] = val
        
        self.config = replace(base_config, **overrides) if overrides else base_config
        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day
        
        self._entry_info: Dict[str, dict] = {}
        self._highest_price: Dict[str, float] = {}
        self._trend_ma: Dict[str, int] = {}
        self._hold_days: Dict[str, int] = {}

    def generate_signals(self, current_date, stock_pool_today, data_query):
        final_signals: Dict[str, str] = {}

        if stock_pool_today is None or stock_pool_today.empty:
            return {}

        prev_date = self._get_previous_trading_date(current_date, data_query)
        if prev_date is None:
            return {}
        
        stock_pool_prev = data_query.get_stock_pool(prev_date)
        if stock_pool_prev is None or stock_pool_prev.empty:
            return {}

        for code in list(self._hold_days.keys()):
            self._hold_days[code] = self._hold_days.get(code, 0) + 1

        sell_codes = self._get_sell_signals(stock_pool_prev, prev_date, data_query, current_date)
        for code in sell_codes:
            final_signals[code] = "sell"
            self._hold_days.pop(code, None)

        current_positions = set(getattr(self, 'current_portfolio', {}).keys())
        if len(current_positions) >= self.config.max_positions:
            return final_signals

        buy_candidates = self._get_buy_candidates(stock_pool_prev, prev_date, data_query, current_positions)

        if buy_candidates:
            can_buy = self.config.max_positions - len(current_positions)
            limited = buy_candidates[:min(self.config.max_stocks_per_day, can_buy)]
            for code in limited:
                if code not in final_signals:
                    final_signals[code] = "buy"
                    self._hold_days[code] = 0
            print(f"[趋势V2] {current_date} 基于{prev_date}数据，买入: {limited}")
        
        return final_signals

    def _get_previous_trading_date(self, current_date: str, data_query) -> Optional[str]:
        """
        获取前一交易日
        
        【性能优化】使用交易日历查询，而不是逐日查询股票池
        """
        from datetime import timedelta
        
        try:
            # 【优化】使用 get_trading_dates 获取交易日历
            if hasattr(data_query, 'get_trading_dates'):
                current = pd.to_datetime(current_date)
                # 获取前10天的交易日
                start = (current - timedelta(days=10)).strftime("%Y-%m-%d")
                end = (current - timedelta(days=1)).strftime("%Y-%m-%d")
                trading_dates = data_query.get_trading_dates(start, end)
                
                if trading_dates:
                    # 返回最后一个交易日
                    return trading_dates[-1]
            
            # 【回退】逐日查询（慢，但保证正确性）
            current = pd.to_datetime(current_date)
            for i in range(1, 10):
                prev = current - timedelta(days=i)
                prev_str = prev.strftime("%Y-%m-%d")
                df = data_query.get_stock_pool(prev_str)
                if df is not None and not df.empty:
                    return prev_str
            return None
        except Exception:
            return None

    def _get_buy_candidates(self, stock_pool: pd.DataFrame, date: str, data_query, current_positions: set) -> List[str]:
        """买入逻辑：趋势确认买入"""
        if stock_pool is None or stock_pool.empty:
            return []

        df = stock_pool.copy()
        
        required_cols = ['ma5', 'ma10', 'ma20', 'close']
        for col in required_cols:
            if col not in df.columns:
                return []
        
        df = df.dropna(subset=required_cols)
        if df.empty:
            return []

        if 'is_st' in df.columns:
            df = df[df['is_st'] == 0]
        
        if 'list_days' in df.columns:
            df = df[df['list_days'] >= self.config.min_list_days]
        
        if 'total_mv' in df.columns:
            df = df[(df['total_mv'] >= self.config.market_cap_min) & 
                    (df['total_mv'] <= self.config.market_cap_max)]

        trend_ok = (
            (df['ma5'] > df['ma10']) &
            (df['ma10'] > df['ma20'])
        )

        price_above = df['close'] > df['ma5']

        volume_ok = True
        if 'volume_ratio' in df.columns:
            volume_ok = df['volume_ratio'] >= self.config.volume_ratio_min

        bias_ok = (df['close'] / df['ma5'] - 1) < self.config.bias_threshold_high

        final_mask = trend_ok & price_above & volume_ok & bias_ok
        
        candidates = df.loc[final_mask, 'stock_code'].tolist()
        candidates = [c for c in candidates if c not in current_positions]
        
        return candidates

    def _get_sell_signals(self, stock_pool: pd.DataFrame, date: str, data_query, current_date: str) -> List[str]:
        """卖出逻辑"""
        if stock_pool is None or stock_pool.empty:
            return []

        current_positions = set(getattr(self, 'current_portfolio', {}).keys())
        if not current_positions:
            return []

        sell_codes = []
        
        for code in current_positions:
            stock_data = stock_pool[stock_pool['stock_code'] == code]
            if stock_data.empty:
                continue
            
            row = stock_data.iloc[0]
            close = row.get('close', 0)
            ma5 = row.get('ma5', 0)
            ma10 = row.get('ma10', 0)
            ma20 = row.get('ma20', 0)
            
            if close <= 0 or ma5 <= 0:
                continue

            hold_days = self._hold_days.get(code, 0)
            if hold_days < self.config.min_hold_days:
                continue

            if hold_days > self.config.max_hold_days:
                sell_codes.append(code)
                print(f"[趋势V2] {current_date} {code} 持仓超过{self.config.max_hold_days}天，卖出")
                continue

            bias_ma5 = (close / ma5 - 1) if ma5 > 0 else 0
            
            if bias_ma5 > self.config.bias_threshold_extreme:
                sell_codes.append(code)
                print(f"[趋势V2] {current_date} {code} 乖离率过高({bias_ma5*100:.1f}%)，卖出")
                continue

            if bias_ma5 > self.config.bias_threshold_high:
                if close < ma5:
                    sell_codes.append(code)
                    print(f"[趋势V2] {current_date} {code} 高乖离后跌破MA5，卖出")
                    continue

            trend_ma = self._trend_ma.get(code, 10)
            trend_line = ma5 if trend_ma == 5 else ma10
            
            if trend_line > 0 and close < trend_line * 0.97:
                sell_codes.append(code)
                print(f"[趋势V2] {current_date} {code} 跌破MA{trend_ma}趋势线，卖出")
                continue

            if ma5 < ma10 or ma10 < ma20:
                sell_codes.append(code)
                print(f"[趋势V2] {current_date} {code} 均线空头排列，卖出")
                continue

            entry_info = self._entry_info.get(code, {})
            entry_price = entry_info.get('entry_price', 0)
            
            if entry_price > 0:
                loss_pct = (close / entry_price - 1)
                if loss_pct < -self.config.stop_loss_pct:
                    sell_codes.append(code)
                    print(f"[趋势V2] {current_date} {code} 触发止损({loss_pct*100:.1f}%)，卖出")
                    continue

            highest = self._highest_price.get(code, close)
            if highest > entry_price * 1.1:
                if close < highest * (1 - self.config.trailing_stop_pct):
                    sell_codes.append(code)
                    print(f"[趋势V2] {current_date} {code} 移动止盈(最高{highest:.2f})，卖出")
                    continue

        return sell_codes

    def on_trade_executed(self, trade_info: dict):
        action = trade_info.get('action')
        code = trade_info.get('code') or trade_info.get('stock_code')
        
        if action == 'buy':
            self._entry_info[code] = {
                'entry_price': trade_info.get('price', 0),
                'entry_date': trade_info.get('date'),
            }
            self._highest_price[code] = trade_info.get('price', 0)
            
            stock_pool = trade_info.get('stock_pool')
            if stock_pool is not None and not stock_pool.empty:
                stock_data = stock_pool[stock_pool['stock_code'] == code]
                if not stock_data.empty:
                    row = stock_data.iloc[0]
                    ma5 = row.get('ma5', 0)
                    ma10 = row.get('ma10', 0)
                    close = row.get('close', 0)
                    
                    dist_ma5 = abs(close / ma5 - 1) if ma5 > 0 else 1
                    dist_ma10 = abs(close / ma10 - 1) if ma10 > 0 else 1
                    
                    self._trend_ma[code] = 5 if dist_ma5 < dist_ma10 else 10
        
        elif action == 'sell':
            self._entry_info.pop(code, None)
            self._highest_price.pop(code, None)
            self._trend_ma.pop(code, None)
            self._hold_days.pop(code, None)
