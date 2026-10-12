"""
中长期趋势策略 V3 - 向量化版本

使用声明式因子系统，策略编写者只需关注交易逻辑：
1. 继承 VectorizedStrategyBase
2. 声明 required_factors（可选，数据库因子自动注入）
3. 实现 generate_signals_vectorized
4. 调用 self.prepare_data() 后直接使用 self.ma5, self.close 等
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import logging
logger = logging.getLogger(__name__)
import pandas as pd

from core.quant.strategies.vectorized_base import VectorizedStrategyBase


@dataclass(frozen=True)
class TrendFollowV3Config:
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


class TrendFollowStrategyV3(VectorizedStrategyBase):
    """
    中长期趋势跟踪策略 V3 - 向量化版本
    
    核心理念：
    - 只做确定性高的趋势
    - 严格的风控
    - 减少交易频率
    
    使用方式：
        class MyStrategy(VectorizedStrategyBase):
            # 数据库因子自动注入，无需声明
            # ma5, ma10, ma20, close, volume_ratio, is_st, total_mv 等
            
            def generate_signals_vectorized(self, ...):
                self.prepare_data(...)
                
                # 直接使用矩阵
                ma5 = self.ma5      # (T, N)
                close = self.close  # (T, N)
                
                # 编写交易逻辑...
    """
    strategy_id = "trend_follow_v3"
    strategy_name = "中长期趋势跟踪策略V3(向量化)"
    
    needs_today_pool = False

    def __init__(self, **kwargs):
        super().__init__()
        base_config = TrendFollowV3Config()
        
        overrides = {}
        for key, val in kwargs.items():
            if hasattr(base_config, key):
                overrides[key] = val
        
        self.config = replace(base_config, **overrides) if overrides else base_config
        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day
        
        # 声明需要的因子（从 ArcticDB factor 库读取）
        self.required_factors = ['ma5', 'ma10', 'ma20']
        
        self._entry_info: Dict[str, dict] = {}
        self._highest_price: Dict[str, float] = {}
        self._trend_ma: Dict[str, int] = {}
        self._hold_days: Dict[str, int] = {}
        
        self._signal_matrix: Optional[np.ndarray] = None
        self._current_day_idx: int = 0

    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, pd.DataFrame]] = None,
        price_matrix_adj: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """
        向量化信号生成
        
        返回：
            np.ndarray: (T, N) 信号矩阵
                1 = 买入
                2 = 卖出
                0 = 无操作
        """
        self.prepare_data(preloaded_data, trading_dates, stock_codes, price_matrix)
        
        T = self.T
        N = self.N
        
        self._signal_matrix = np.zeros((T, N), dtype=np.int8)
        
        if np.all(np.isnan(self.ma5)) or np.all(np.isnan(self.ma10)) or np.all(np.isnan(self.ma20)):
            logger.warning("[趋势V3] 缺少均线数据")
            return self._signal_matrix
        
        if np.all(np.isnan(self.close)):
            logger.warning("[趋势V3] 缺少收盘价数据")
            return self._signal_matrix
        
        buy_mask, sell_mask = self._compute_signals_vectorized()
        
        self._signal_matrix[buy_mask] = 1
        self._signal_matrix[sell_mask] = 2
        
        return self._signal_matrix
    
    def _compute_signals_vectorized(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        向量化计算买卖信号
        
        返回：
            Tuple[np.ndarray, np.ndarray]: (买入坐标, 卖出坐标)
        """
        T = self.T
        N = self.N
        
        close = self.close
        ma5 = self.ma5
        ma10 = self.ma10
        ma20 = self.ma20
        
        trend_ok = (
            (ma5 > ma10) &
            (ma10 > ma20) &
            np.isfinite(ma5) & np.isfinite(ma10) & np.isfinite(ma20)
        )
        
        price_above_ma5 = (close > ma5) & np.isfinite(close)
        
        volume_ok = np.ones((T, N), dtype=bool)
        if self.volume_ratio is not None:
            volume_ok = (self.volume_ratio >= self.config.volume_ratio_min) | ~np.isfinite(self.volume_ratio)
        
        bias = np.full((T, N), np.nan, dtype=np.float32)
        valid_ma5 = ma5 > 0
        bias = np.where(valid_ma5, (close / ma5 - 1), np.nan)
        bias_ok = (bias < self.config.bias_threshold_high) | ~np.isfinite(bias)
        
        not_st = np.ones((T, N), dtype=bool)
        if self.is_st is not None:
            not_st = (self.is_st == 0)
        
        listed_long_enough = np.ones((T, N), dtype=bool)
        if self.days_listed is not None:
            listed_long_enough = (self.days_listed >= self.config.min_list_days) | ~np.isfinite(self.days_listed)
        
        market_cap_ok = np.ones((T, N), dtype=bool)
        if self.total_mv is not None:
            market_cap_ok = (
                (self.total_mv >= self.config.market_cap_min) &
                (self.total_mv <= self.config.market_cap_max)
            ) | ~np.isfinite(self.total_mv)
        
        buy_mask = (
            trend_ok &
            price_above_ma5 &
            volume_ok &
            bias_ok &
            not_st &
            listed_long_enough &
            market_cap_ok
        )
        
        sell_mask = np.zeros((T, N), dtype=bool)
        
        return buy_mask, sell_mask

    def generate_signals(
        self,
        current_date,
        stock_pool_today,
        data_query
    ) -> Dict[str, str]:
        """
        传统接口：从预计算的信号矩阵提取当日信号
        """
        if self._signal_matrix is None:
            return {}
        
        try:
            date_idx = self._trading_dates.index(current_date)
        except ValueError:
            return {}
        
        if date_idx >= self._signal_matrix.shape[0]:
            return {}
        
        signals = self._signal_matrix[date_idx]
        stock_codes = self._stock_codes
        
        result: Dict[str, str] = {}
        
        for j, signal in enumerate(signals):
            if signal == 1:
                code = stock_codes[j]
                result[code] = "buy"
            elif signal == 2:
                code = stock_codes[j]
                result[code] = "sell"
        
        return result

    def on_trade_executed(self, trade_info: dict):
        """交易执行回调"""
        action = trade_info.get('action')
        code = trade_info.get('code') or trade_info.get('stock_code')
        
        if action == 'buy':
            self._entry_info[code] = {
                'entry_price': trade_info.get('price', 0),
                'entry_date': trade_info.get('date'),
            }
            self._highest_price[code] = trade_info.get('price', 0)
            
            if self.ma5 is not None and self.ma10 is not None and self.close is not None:
                try:
                    date_str = trade_info.get('date')
                    if date_str in self._trading_dates:
                        t_idx = self._trading_dates.index(date_str)
                        code_idx = self._stock_codes.index(code)
                        
                        ma5_val = self.ma5[t_idx, code_idx]
                        ma10_val = self.ma10[t_idx, code_idx]
                        close_val = self.close[t_idx, code_idx]
                        
                        if np.isfinite(ma5_val) and np.isfinite(ma10_val) and np.isfinite(close_val):
                            dist_ma5 = abs(close_val / ma5_val - 1) if ma5_val > 0 else 1
                            dist_ma10 = abs(close_val / ma10_val - 1) if ma10_val > 0 else 1
                            self._trend_ma[code] = 5 if dist_ma5 < dist_ma10 else 10
                except (ValueError, IndexError):
                    pass
        
        elif action == 'sell':
            self._entry_info.pop(code, None)
            self._highest_price.pop(code, None)
            self._trend_ma.pop(code, None)
            self._hold_days.pop(code, None)
