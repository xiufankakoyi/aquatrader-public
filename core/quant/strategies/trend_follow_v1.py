"""
中长期趋势策略

买入逻辑（右侧买入）：
1. 趋势形成：MA5 > MA10 > MA20 多头排列
2. 股价站上趋势线且确认突破
3. 成交量配合

卖出逻辑：
1. 乖离率过高：股价偏离MA5超过阈值（走加速）
2. 趋势破坏：跌破所沿着的均线（动态判断MA5或MA10）
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from core.quant.strategies.strategy_framework import StrategyBase


@dataclass(frozen=True)
class TrendFollowConfig:
    bias_threshold_high: float = 0.12
    bias_threshold_extreme: float = 0.18
    trend_ma_fast: int = 5
    trend_ma_slow: int = 10
    trend_ma_base: int = 20
    volume_ratio_min: float = 1.2
    min_list_days: int = 60
    position_ratio: float = 0.2
    max_stocks_per_day: int = 3
    stop_loss_pct: float = 0.08
    trailing_stop_pct: float = 0.05
    max_hold_days: int = 60

    bank_codes: List[str] = field(default_factory=lambda: [
        "600036", "601166", "601328", "601398", "601939", "601988", "601288"
    ])


class TrendFollowStrategy(StrategyBase):
    """
    中长期趋势跟踪策略
    
    核心理念：
    - 右侧交易，顺势而为
    - 趋势形成时买入，趋势破坏时卖出
    - 动态识别股票沿哪条均线运行
    """
    strategy_id = "trend_follow_v1"
    strategy_name = "中长期趋势跟踪策略"
    needs_today_pool = True

    PARAM_SPEC = [
        {
            "key": "bias_threshold_high",
            "label": "高乖离率阈值(%)",
            "group": "卖出条件",
            "type": "float",
            "min": 5.0,
            "max": 25.0,
            "step": 1.0,
            "default": TrendFollowConfig().bias_threshold_high * 100,
            "optimize": True,
            "description": "股价偏离MA5超过此比例视为过热",
        },
        {
            "key": "stop_loss_pct",
            "label": "止损比例(%)",
            "group": "风控",
            "type": "float",
            "min": 3.0,
            "max": 15.0,
            "step": 0.5,
            "default": TrendFollowConfig().stop_loss_pct * 100,
            "optimize": True,
            "description": "跌破买入价此比例止损",
        },
        {
            "key": "trailing_stop_pct",
            "label": "移动止盈(%)",
            "group": "风控",
            "type": "float",
            "min": 3.0,
            "max": 15.0,
            "step": 0.5,
            "default": TrendFollowConfig().trailing_stop_pct * 100,
            "optimize": True,
            "description": "从最高点回撤此比例止盈",
        },
    ]

    def __init__(self, **kwargs):
        super().__init__()
        base_config = TrendFollowConfig()
        
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

    def generate_signals(self, current_date, stock_pool_today, data_query):
        final_signals: Dict[str, str] = {}

        if stock_pool_today is None:
            return {}
        if isinstance(stock_pool_today, np.ndarray):
            if stock_pool_today.size == 0:
                return {}
        elif hasattr(stock_pool_today, 'empty') and stock_pool_today.empty:
            return {}

        prev_date = self._get_previous_trading_date(current_date, data_query)
        if prev_date is None:
            return {}
        
        stock_pool_prev = data_query.get_stock_pool(prev_date)
        if stock_pool_prev is None:
            return {}
        if isinstance(stock_pool_prev, np.ndarray):
            if stock_pool_prev.size == 0:
                return {}
        elif hasattr(stock_pool_prev, 'empty') and stock_pool_prev.empty:
            return {}

        sell_codes = self._get_sell_signals(stock_pool_prev, prev_date, data_query, current_date)
        for code in sell_codes:
            final_signals[code] = "sell"

        buy_candidates = self._get_buy_candidates(stock_pool_prev, prev_date, data_query)

        if buy_candidates:
            limited = buy_candidates[:self.config.max_stocks_per_day]
            for code in limited:
                if code not in final_signals:
                    final_signals[code] = "buy"
            print(f"[趋势策略] {current_date} 基于{prev_date}数据，买入: {limited}")
        
        return final_signals

    def _get_previous_trading_date(self, current_date: str, data_query) -> Optional[str]:
        from datetime import timedelta
        
        try:
            current = pd.to_datetime(current_date)
            for i in range(1, 10):
                prev = current - timedelta(days=i)
                prev_str = prev.strftime("%Y-%m-%d")
                df = data_query.get_stock_pool(prev_str)
                if df is None:
                    continue
                if isinstance(df, np.ndarray):
                    if df.size > 0:
                        return prev_str
                elif hasattr(df, 'empty') and not df.empty:
                    return prev_str
            return None
        except Exception:
            return None

    def _get_buy_candidates(self, stock_pool: pd.DataFrame, date: str, data_query) -> List[str]:
        """买入逻辑：右侧趋势确认买入"""
        if stock_pool is None:
            return []
        if isinstance(stock_pool, np.ndarray):
            if stock_pool.size == 0:
                return []
        elif hasattr(stock_pool, 'empty') and stock_pool.empty:
            return []

        df = stock_pool.copy()
        
        required_cols = ['ma5', 'ma10', 'ma20', 'close', 'volume']
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

        trend_forming = (
            (df['ma5'] > df['ma10']) &
            (df['ma10'] > df['ma20']) &
            (df['close'] > df['ma5'])
        )

        prev_ma5 = df['ma5'].shift(1) if 'ma5' in df.columns else df['ma5']
        prev_ma10 = df['ma10'].shift(1) if 'ma10' in df.columns else df['ma10']
        
        trend_strengthening = (
            (df['ma5'] > df['ma5'].shift(1).fillna(df['ma5'])) &
            (df['ma10'] > df['ma10'].shift(1).fillna(df['ma10']))
        )

        volume_ok = True
        if 'volume_ratio' in df.columns:
            volume_ok = df['volume_ratio'] >= self.config.volume_ratio_min

        breakout = df['close'] > df['ma5'] * 1.02

        final_mask = trend_forming & trend_strengthening & volume_ok & breakout
        
        candidates = df.loc[final_mask, 'stock_code'].tolist()
        
        return candidates

    def _get_sell_signals(self, stock_pool: pd.DataFrame, date: str, data_query, current_date: str) -> List[str]:
        """卖出逻辑：乖离率过高 + 趋势破坏"""
        if stock_pool is None:
            return []
        if isinstance(stock_pool, np.ndarray):
            if stock_pool.size == 0:
                return []
        elif hasattr(stock_pool, 'empty') and stock_pool.empty:
            return []

        current_positions = set(getattr(self, 'current_portfolio', {}).keys())
        if not current_positions:
            return []

        sell_codes = []
        
        for code in current_positions:
            stock_data = stock_pool[stock_pool['stock_code'] == code]
            if isinstance(stock_data, np.ndarray):
                if stock_data.size == 0:
                    continue
            elif hasattr(stock_data, 'empty') and stock_data.empty:
                continue
            
            row = stock_data.iloc[0]
            close = row.get('close', 0)
            ma5 = row.get('ma5', 0)
            ma10 = row.get('ma10', 0)
            ma20 = row.get('ma20', 0)
            
            if close <= 0 or ma5 <= 0:
                continue

            bias_ma5 = (close / ma5 - 1) if ma5 > 0 else 0
            
            if bias_ma5 > self.config.bias_threshold_extreme:
                sell_codes.append(code)
                print(f"[趋势策略] {current_date} {code} 乖离率过高({bias_ma5*100:.1f}%)，卖出")
                continue

            if bias_ma5 > self.config.bias_threshold_high:
                if close < ma5:
                    sell_codes.append(code)
                    print(f"[趋势策略] {current_date} {code} 高乖离后跌破MA5，卖出")
                    continue

            trend_ma = self._trend_ma.get(code, 5)
            
            if trend_ma == 5:
                trend_line = ma5
            else:
                trend_line = ma10
            
            if trend_line > 0 and close < trend_line * 0.98:
                sell_codes.append(code)
                print(f"[趋势策略] {current_date} {code} 跌破MA{trend_ma}趋势线，卖出")
                continue

            if ma5 < ma10 or ma10 < ma20:
                sell_codes.append(code)
                print(f"[趋势策略] {current_date} {code} 均线空头排列，卖出")
                continue

            entry_info = self._entry_info.get(code, {})
            entry_price = entry_info.get('entry_price', 0)
            
            if entry_price > 0:
                loss_pct = (close / entry_price - 1)
                if loss_pct < -self.config.stop_loss_pct:
                    sell_codes.append(code)
                    print(f"[趋势策略] {current_date} {code} 触发止损({loss_pct*100:.1f}%)，卖出")
                    continue

            highest = self._highest_price.get(code, close)
            if close < highest * (1 - self.config.trailing_stop_pct):
                sell_codes.append(code)
                print(f"[趋势策略] {current_date} {code} 移动止盈(最高{highest:.2f})，卖出")
                continue

        return sell_codes

    def on_trade_executed(self, trade_info: dict):
        """交易执行后的回调，记录持仓信息"""
        action = trade_info.get('action')
        code = trade_info.get('code') or trade_info.get('stock_code')
        
        if action == 'buy':
            self._entry_info[code] = {
                'entry_price': trade_info.get('price', 0),
                'entry_date': trade_info.get('date'),
            }
            self._highest_price[code] = trade_info.get('price', 0)
            
            stock_pool = trade_info.get('stock_pool')
            if stock_pool is not None:
                if isinstance(stock_pool, np.ndarray):
                    if stock_pool.size > 0:
                        stock_data = stock_pool[stock_pool['stock_code'] == code]
                        if isinstance(stock_data, np.ndarray) and stock_data.size > 0:
                            row = stock_data[0] if len(stock_data) > 0 else None
                            if row is not None:
                                ma5 = row.get('ma5', 0) if hasattr(row, 'get') else 0
                                ma10 = row.get('ma10', 0) if hasattr(row, 'get') else 0
                                close = row.get('close', 0) if hasattr(row, 'get') else 0
                                
                                dist_ma5 = abs(close / ma5 - 1) if ma5 > 0 else 1
                                dist_ma10 = abs(close / ma10 - 1) if ma10 > 0 else 1
                                
                                if dist_ma5 < dist_ma10:
                                    self._trend_ma[code] = 5
                                else:
                                    self._trend_ma[code] = 10
                elif hasattr(stock_pool, 'empty') and not stock_pool.empty:
                    stock_data = stock_pool[stock_pool['stock_code'] == code]
                    if hasattr(stock_data, 'empty') and not stock_data.empty:
                        row = stock_data.iloc[0]
                        ma5 = row.get('ma5', 0)
                        ma10 = row.get('ma10', 0)
                        close = row.get('close', 0)
                        
                        dist_ma5 = abs(close / ma5 - 1) if ma5 > 0 else 1
                        dist_ma10 = abs(close / ma10 - 1) if ma10 > 0 else 1
                        
                        if dist_ma5 < dist_ma10:
                            self._trend_ma[code] = 5
                        else:
                            self._trend_ma[code] = 10
        
        elif action == 'sell':
            self._entry_info.pop(code, None)
            self._highest_price.pop(code, None)
            self._trend_ma.pop(code, None)

    def update_highest_price(self, code: str, current_price: float):
        """更新持仓最高价"""
        if current_price > self._highest_price.get(code, 0):
            self._highest_price[code] = current_price
