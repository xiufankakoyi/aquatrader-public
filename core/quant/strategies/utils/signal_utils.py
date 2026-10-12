"""
策略信号工具库

提供常用信号检测函数，策略开发者直接调用即可。
"""
import numpy as np
from typing import Union, Optional, Tuple


def crossover(fast: np.ndarray, slow: np.ndarray) -> np.ndarray:
    """
    检测上穿信号（金叉）
    
    Args:
        fast: 快线 (T,) 或
        slow: 慢线 (T,) 或
    
    Returns:
        bool array, True 表示发生金叉
    
    Example:
        golden = crossover(ma5, ma10)  # MA5 上穿 MA10
    """
    if fast.ndim == 1 and slow.ndim == 1:
        return (fast[:-1] <= slow[:-1]) & (fast[1:] > slow[1:])
    elif fast.ndim == 2 and slow.ndim == 2:
        return (fast[:-1] <= slow[:-1]) & (fast[1:] > slow[1:])
    else:
        raise ValueError("fast and slow must have same dimensions")


def crossunder(fast: np.ndarray, slow: np.ndarray) -> np.ndarray:
    """
    检测下穿信号（死叉）
    
    Args:
        fast: 快线
        slow: 慢线
    
    Returns:
        bool array, True 表示发生死叉
    
    Example:
        death = crossunder(ma5, ma10)  # MA5 下穿 MA10
    """
    if fast.ndim == 1 and slow.ndim == 1:
        return (fast[:-1] >= slow[:-1]) & (fast[1:] < slow[1:])
    elif fast.ndim == 2 and slow.ndim == 2:
        return (fast[:-1] >= slow[:-1]) & (fast[1:] < slow[1:])
    else:
        raise ValueError("fast and slow must have same dimensions")


def above(series: np.ndarray, threshold: Union[float, np.ndarray]) -> np.ndarray:
    """
    检测上穿阈值
    
    Example:
        oversold = above(rsi, 30)  # RSI 上穿 30（超卖反弹）
    """
    if isinstance(threshold, (int, float)):
        return series > threshold
    else:
        return series > threshold


def below(series: np.ndarray, threshold: Union[float, np.ndarray]) -> np.ndarray:
    """
    检测下穿阈值
    
    Example:
        overbought = below(rsi, 70)  # RSI 下穿 70（超买回落）
    """
    if isinstance(threshold, (int, float)):
        return series < threshold
    else:
        return series < threshold


def rising(series: np.ndarray, window: int = 1) -> np.ndarray:
    """
    检测连续上涨
    
    Example:
        up = rising(close, 3)  # 连续3天上涨
    """
    if series.ndim == 1:
        diff = np.diff(series)
        result = np.ones(len(series), dtype=bool)
        for i in range(window, len(series)):
            result[i] = np.all(diff[i-window:i] > 0)
        return result
    else:
        diff = np.diff(series, axis=0)
        result = np.ones(series.shape, dtype=bool)
        for i in range(window, series.shape[0]):
            result[i] = np.all(diff[i-window:i] > 0, axis=0)
        return result


def falling(series: np.ndarray, window: int = 1) -> np.ndarray:
    """
    检测连续下跌
    """
    if series.ndim == 1:
        diff = np.diff(series)
        result = np.ones(len(series), dtype=bool)
        for i in range(window, len(series)):
            result[i] = np.all(diff[i-window:i] < 0)
        return result
    else:
        diff = np.diff(series, axis=0)
        result = np.ones(series.shape, dtype=bool)
        for i in range(window, series.shape[0]):
            result[i] = np.all(diff[i-window:i] < 0, axis=0)
        return result


def in_range(series: np.ndarray, low: float, high: float) -> np.ndarray:
    """
    检测是否在区间内
    
    Example:
        valid = in_range(rsi, 30, 70)  # RSI 在 30-70 之间
    """
    return (series >= low) & (series <= high)


def breakout(high_series: np.ndarray, low_series: np.ndarray, close: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    检测突破信号（布林带、唐奇安通道等）
    
    Args:
        high_series: 上轨
        low_series: 下轨
        close: 收盘价
    
    Returns:
        (upper_breakout, lower_breakout): 上突破、下突破
    
    Example:
        up_break, down_break = breakout(boll_upper, boll_lower, close)
    """
    upper_breakout = close > high_series
    lower_breakout = close < low_series
    return upper_breakout, lower_breakout


def divergence(price: np.ndarray, indicator: np.ndarray, window: int = 5) -> Tuple[np.ndarray, np.ndarray]:
    """
    检测背离信号
    
    Args:
        price: 价格序列
        indicator: 指标序列
        window: 检测窗口
    
    Returns:
        (bullish_div, bearish_div): 底背离、顶背离
    
    Example:
        bull_div, bear_div = divergence(close, rsi, 5)
    """
    T = len(price)
    bullish_div = np.zeros(T, dtype=bool)
    bearish_div = np.zeros(T, dtype=bool)
    
    for i in range(window, T):
        price_slice = price[i-window:i+1]
        ind_slice = indicator[i-window:i+1]
        
        price_lowest = np.argmin(price_slice) == window
        ind_higher = indicator[i] > np.min(ind_slice[:-1])
        
        price_highest = np.argmax(price_slice) == window
        ind_lower = indicator[i] < np.max(ind_slice[:-1])
        
        bullish_div[i] = price_lowest and ind_higher
        bearish_div[i] = price_highest and ind_lower
    
    return bullish_div, bearish_div


SIGNAL_FUNCTIONS = {
    'crossover': crossover,
    'crossunder': crossunder,
    'above': above,
    'below': below,
    'rising': rising,
    'falling': falling,
    'in_range': in_range,
    'breakout': breakout,
    'divergence': divergence,
}
