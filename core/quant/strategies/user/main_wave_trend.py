"""
主升浪趋势跟踪策略 - 实盘策略回测版本

核心逻辑：
1. 趋势跟踪/右侧交易：只在上升趋势确立后（均线多头、突破平台）买入
2. 主升浪战法：聚焦于已经走出流畅上涨的个股，追求主升段的最大收益
3. 均线支撑买入法：依托5日、10日均线作为动态支撑位，回踩时低吸
4. 卖出规则：乖离过大的强势股或跌破趋势严格执行卖出
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from core.quant.strategies.vectorized_base import VectorizedStrategyBase


@dataclass(frozen=True)
class MainWaveConfig:
    """
    主升浪策略配置
    
    买入条件：
    - 趋势确认：MA5 > MA10 > MA20（均线多头排列）
    - 主升浪识别：近期有突破或连续上涨
    - 回踩买入：价格回踩至MA5或MA10附近（乖离率适中）
    - 量能配合：成交量放大
    
    卖出条件：
    - 乖离过大：价格远离MA5超过阈值
    - 趋势破坏：跌破MA10或MA20
    - 止损：固定止损或移动止损
    """
    # 趋势确认参数
    trend_ma_fast: int = 5
    trend_ma_mid: int = 10
    trend_ma_slow: int = 20
    
    # 主升浪识别参数
    breakout_days: int = 5
    breakout_pct: float = 0.08
    consecutive_up_days: int = 3
    
    # 回踩买入参数 (与聚宽一致)
    pullback_to_ma5_max: float = 0.03  # 聚宽: 0.03
    pullback_to_ma10_max: float = 0.03  # 聚宽: 0.03
    
    # 乖离率参数 (与聚宽一致)
    bias_normal_max: float = 0.05  # 买入时乖离限制
    bias_high_max: float = 0.10
    bias_extreme_max: float = 0.15  # 卖出乖离阈值 (与聚宽一致)
    
    # 量能参数 (与聚宽一致)
    volume_ratio_min: float = 1.5  # 聚宽: 1.5 (原来是0.8，太宽松了)
    
    # 仓位管理
    position_ratio: float = 0.20
    max_positions: int = 5
    max_stocks_per_day: int = 2
    
    # 止损止盈
    stop_loss_pct: float = 0.08
    trailing_stop_pct: float = 0.05
    take_profit_pct: float = 0.30
    
    # 持仓周期
    min_hold_days: int = 2
    max_hold_days: int = 60
    
    # 股票筛选
    min_list_days: int = 60
    market_cap_min: float = 30 * 10000
    market_cap_max: float = 2000 * 10000


class MainWaveTrendStrategy(VectorizedStrategyBase):
    """
    主升浪趋势跟踪策略
    
    策略理念：
    - 只做确定性高的主升浪
    - 右侧交易，不抄底
    - 回踩均线低吸，追涨不追高
    - 严格风控，趋势破坏即离场
    """
    strategy_id = "main_wave_trend"
    strategy_name = "【实盘】主升浪趋势跟踪策略"
    
    needs_today_pool = False

    def __init__(self, **kwargs):
        super().__init__()
        base_config = MainWaveConfig()
        
        overrides = {}
        for key, val in kwargs.items():
            if hasattr(base_config, key):
                overrides[key] = val
        
        self.config = replace(base_config, **overrides) if overrides else base_config
        self.required_days = self.config.min_list_days
        self.position_ratio = self.config.position_ratio
        self.max_stocks_per_day = self.config.max_stocks_per_day
        
        # 持仓状态追踪
        self._entry_info: Dict[str, dict] = {}
        self._highest_price: Dict[str, float] = {}
        self._hold_days: Dict[str, int] = {}
        self._support_ma: Dict[str, int] = {}
        
        # 信号矩阵
        self._signal_matrix: Optional[np.ndarray] = None
        self._trading_dates: List[str] = []
        self._stock_codes: List[str] = []

    def generate_signals_vectorized(
        self,
        price_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
        data_query,
        preloaded_data: Optional[Dict[str, pd.DataFrame]] = None
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
        
        self._trading_dates = trading_dates
        self._stock_codes = stock_codes
        
        T = self.T
        N = self.N
        
        self._signal_matrix = np.zeros((T, N), dtype=np.int8)
        
        if self.ma5 is None or self.ma10 is None or self.ma20 is None:
            print("[主升浪] 缺少均线数据")
            return self._signal_matrix
        
        if self.close is None:
            print("[主升浪] 缺少收盘价数据")
            return self._signal_matrix
        
        buy_mask, sell_mask = self._compute_signals_vectorized()
        
        self._signal_matrix[buy_mask] = 1
        self._signal_matrix[sell_mask] = 2
        
        return self._signal_matrix

    def _compute_signals_vectorized(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        向量化计算买卖信号
        
        【防止未来函数】
        在真实的交易中，开盘前（9:30）只能使用前一天的收盘数据：
        - 前一天的 close, high, low, volume
        - 前一天的均线数据（ma5, ma10, ma20）
        - 前一天的基本面数据（total_mv, is_st, days_listed）
        - 当天的 open 仅用于执行交易，不用于信号生成
        
        因此，在计算第 t 天的信号时，应该使用 t-1 及之前的数据。
        
        买入条件组合：
        1. 趋势确认：均线多头排列
        2. 主升浪特征：近期有突破或连续上涨
        3. 回踩信号：价格回踩至均线附近
        4. 量能配合：成交量放大
        
        卖出条件：
        1. 乖离过大
        2. 趋势破坏
        """
        T = self.T
        N = self.N
        
        # 【防止未来函数】使用前一天的收盘数据生成当天的交易信号
        # 第 t 天的信号基于 t-1 天的数据
        # 对于第 0 天，没有前一天数据，所以无法生成信号
        if T < 2:
            return np.zeros((T, N), dtype=bool), np.zeros((T, N), dtype=bool)
        
        # 使用前一天的收盘数据（shift by 1）
        # close[t] 实际上使用的是 close[t-1] 的数据
        close = np.roll(self.close, 1, axis=0)
        close[0, :] = np.nan  # 第 0 天没有前一天数据
        
        high = np.roll(self.high, 1, axis=0) if self.high is not None else close
        if self.high is not None:
            high[0, :] = np.nan
        
        low = np.roll(self.low, 1, axis=0) if self.low is not None else close
        if self.low is not None:
            low[0, :] = np.nan
        
        # 均线数据也使用前一天的
        ma5 = np.roll(self.ma5, 1, axis=0) if self.ma5 is not None else None
        ma10 = np.roll(self.ma10, 1, axis=0) if self.ma10 is not None else None
        ma20 = np.roll(self.ma20, 1, axis=0) if self.ma20 is not None else None
        
        if ma5 is not None:
            ma5[0, :] = np.nan
        if ma10 is not None:
            ma10[0, :] = np.nan
        if ma20 is not None:
            ma20[0, :] = np.nan
        
        # ========== 1. 趋势确认 ==========
        trend_bullish = (
            (ma5 > ma10) &
            (ma10 > ma20) &
            (ma5 > 0) & (ma10 > 0) & (ma20 > 0) &
            np.isfinite(ma5) & np.isfinite(ma10) & np.isfinite(ma20)
        )
        
        price_above_ma20 = (close > ma20) & np.isfinite(close)
        
        # ========== 2. 主升浪识别 ==========
        breakout_signal = self._detect_breakout(close, high, ma20)
        
        # ========== 3. 回踩买入信号 ==========
        pullback_signal = self._detect_pullback(close, low, ma5, ma10)
        
        # ========== 4. 量能条件 ==========
        volume_ok = np.ones((T, N), dtype=bool)
        if self.volume_ratio is not None:
            volume_ok = (
                (self.volume_ratio >= self.config.volume_ratio_min) |
                ~np.isfinite(self.volume_ratio)
            )
        
        # ========== 5. 乖离率检查（买入时不能太高） ==========
        bias = np.where(ma5 > 0, (close / ma5 - 1), np.nan)
        bias_ok_for_buy = (
            (bias < self.config.bias_normal_max) |
            ~np.isfinite(bias)
        )
        
        # ========== 6. 股票筛选条件（防止未来函数：使用前一天的基本面数据） ==========
        # 【防止未来函数】基本面数据（is_st, days_listed, total_mv）也使用前一天的
        not_st = np.ones((T, N), dtype=bool)
        if self.is_st is not None:
            is_st_prev = np.roll(self.is_st, 1, axis=0)
            is_st_prev[0, :] = 0  # 第 0 天默认为非 ST
            not_st = (is_st_prev == 0)
        
        listed_long_enough = np.ones((T, N), dtype=bool)
        if self.days_listed is not None:
            days_listed_prev = np.roll(self.days_listed, 1, axis=0)
            days_listed_prev[0, :] = np.nan  # 第 0 天无数据
            listed_long_enough = (
                (days_listed_prev >= self.config.min_list_days) |
                ~np.isfinite(days_listed_prev)
            )
        
        market_cap_ok = np.ones((T, N), dtype=bool)
        if self.total_mv is not None:
            total_mv_prev = np.roll(self.total_mv, 1, axis=0)
            total_mv_prev[0, :] = np.nan  # 第 0 天无数据
            market_cap_ok = (
                (total_mv_prev >= self.config.market_cap_min) &
                (total_mv_prev <= self.config.market_cap_max)
            ) | ~np.isfinite(total_mv_prev)
        
        # ========== 买入信号组合 ==========
        buy_mask = (
            trend_bullish &
            price_above_ma20 &
            (breakout_signal | pullback_signal) &
            volume_ok &
            bias_ok_for_buy &
            not_st &
            listed_long_enough &
            market_cap_ok
        )
        
        # ========== 卖出信号 ==========
        sell_mask = self._compute_sell_signals(close, ma5, ma10, ma20, bias)
        
        return buy_mask, sell_mask

    def _detect_breakout(
        self, 
        close: np.ndarray, 
        high: np.ndarray, 
        ma20: np.ndarray
    ) -> np.ndarray:
        """
        检测突破信号
        
        条件：
        1. 近N日内有突破（创近期新高或突破MA20）
        2. 突破幅度超过阈值
        """
        T, N = close.shape
        breakout_days = self.config.breakout_days
        breakout_pct = self.config.breakout_pct
        
        breakout_signal = np.zeros((T, N), dtype=bool)
        
        for t in range(breakout_days + 1, T):
            # 近N日最高价
            recent_high = np.nanmax(high[t-breakout_days:t], axis=0)
            
            # 之前N日的最高价（用于判断突破）
            prev_high = np.nanmax(high[t-2*breakout_days:t-breakout_days], axis=0) if t >= 2*breakout_days else recent_high
            
            # 突破条件：创新高或突破MA20
            new_high = (high[t] >= recent_high) & (recent_high > prev_high)
            break_ma20 = (close[t] > ma20[t]) & (close[t-1] <= ma20[t-1])
            
            # 突破幅度
            if t > breakout_days:
                price_change = (close[t] - close[t-breakout_days]) / close[t-breakout_days]
                strong_breakout = np.isfinite(price_change) & (price_change >= breakout_pct)
            else:
                strong_breakout = np.zeros(N, dtype=bool)
            
            breakout_signal[t] = (new_high | break_ma20 | strong_breakout) & np.isfinite(high[t])
        
        return breakout_signal

    def _detect_pullback(
        self, 
        close: np.ndarray, 
        low: np.ndarray, 
        ma5: np.ndarray, 
        ma10: np.ndarray
    ) -> np.ndarray:
        """
        检测回踩买入信号
        
        条件：
        1. 价格回踩至MA5或MA10附近
        2. 回踩幅度在合理范围内
        """
        T, N = close.shape
        
        pullback_signal = np.zeros((T, N), dtype=bool)
        
        for t in range(1, T):
            # 回踩MA5 - 使用向量化条件
            ma5_t = ma5[t]
            ma5_positive = (ma5_t > 0) & np.isfinite(ma5_t)
            
            pullback_to_ma5 = np.zeros(N, dtype=bool)
            pullback_to_ma5[ma5_positive] = (
                (close[t][ma5_positive] >= ma5_t[ma5_positive] * (1 - self.config.pullback_to_ma5_max)) &
                (close[t][ma5_positive] <= ma5_t[ma5_positive] * (1 + self.config.pullback_to_ma5_max))
            )
            
            # 回踩MA10 - 使用向量化条件
            ma10_t = ma10[t]
            ma10_positive = (ma10_t > 0) & np.isfinite(ma10_t)
            
            pullback_to_ma10 = np.zeros(N, dtype=bool)
            pullback_to_ma10[ma10_positive] = (
                (close[t][ma10_positive] >= ma10_t[ma10_positive] * (1 - self.config.pullback_to_ma10_max)) &
                (close[t][ma10_positive] <= ma10_t[ma10_positive] * (1 + self.config.pullback_to_ma10_max))
            )
            
            pullback_signal[t] = pullback_to_ma5 | pullback_to_ma10
        
        return pullback_signal

    def _compute_sell_signals(
        self,
        close: np.ndarray,
        ma5: np.ndarray,
        ma10: np.ndarray,
        ma20: np.ndarray,
        bias: np.ndarray
    ) -> np.ndarray:
        """
        计算卖出信号 (与聚宽脚本一致)

        卖出条件：
        1. 乖离过大：价格远离MA5超过阈值 (15%)
        2. 趋势破坏：跌破MA10或MA20 (与聚宽一致，无缓冲)
        """
        T, N = close.shape

        sell_mask = np.zeros((T, N), dtype=bool)

        for t in range(1, T):
            # 乖离过大卖出 (>15%)
            bias_extreme = (
                np.isfinite(bias[t]) &
                (bias[t] > self.config.bias_extreme_max)
            )

            # 趋势破坏卖出 - 与聚宽一致：跌破就卖 (无缓冲)
            # 聚宽逻辑: if current_price < ma10 or current_price < ma20
            trend_broken = (
                (close[t] < ma10[t]) |  # 跌破MA10
                (close[t] < ma20[t])    # 跌破MA20
            )

            sell_mask[t] = bias_extreme | trend_broken

        return sell_mask
