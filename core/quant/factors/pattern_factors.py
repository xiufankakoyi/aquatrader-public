"""
形态识别因子层

所有形态识别算法统一在此计算，策略层只使用结果
"""

import numpy as np
from numba import njit, prange
from typing import Dict, List, Tuple, Optional
import pandas as pd


# ============================================================================
# Numba 加速核心算法
# ============================================================================

@njit(cache=True, fastmath=True)
def _find_local_extrema_numba(prices: np.ndarray, window: int = 2) -> Tuple[np.ndarray, np.ndarray]:
    """
    查找局部极值点
    
    Returns:
        high_indices: 高点索引数组
        low_indices: 低点索引数组
    """
    n = len(prices)
    if n < 2 * window + 1:
        return np.array([-1], dtype=np.int32), np.array([-1], dtype=np.int32)
    
    highs = []
    lows = []
    
    for i in range(window, n - window):
        window_slice = prices[i-window:i+window+1]
        current = prices[i]
        
        if current == np.max(window_slice):
            highs.append(i)
        elif current == np.min(window_slice):
            lows.append(i)
    
    if len(highs) == 0:
        highs = [-1]
    if len(lows) == 0:
        lows = [-1]
    
    return np.array(highs, dtype=np.int32), np.array(lows, dtype=np.int32)


@njit(cache=True, fastmath=True)
def _calculate_apex_convergence(
    prices: np.ndarray,
    high_indices: np.ndarray,
    low_indices: np.ndarray,
    min_down_slope: float = -0.001,
    min_up_slope: float = -0.002,
    max_days_ahead: float = 3.0
) -> Tuple[float, float, float]:
    """
    计算收敛三角形信号
    
    Returns:
        (signal_strength, days_to_apex, confidence)
        signal_strength: 0-1，越大越应该买入
        days_to_apex: 距离交点的天数
        confidence: 置信度
    """
    # 过滤无效索引
    high_indices = high_indices[high_indices >= 0]
    low_indices = low_indices[low_indices >= 0]
    
    if len(high_indices) < 2 or len(low_indices) < 2:
        return 0.0, 999.0, 0.0
    
    t_now = len(prices) - 1
    
    # 从后往前找 ABCD 模式
    for i in range(len(high_indices) - 1, 0, -1):
        for j in range(len(low_indices) - 1, 0, -1):
            A_idx = high_indices[i-1]
            C_idx = high_indices[i]
            B_idx = low_indices[j-1]
            D_idx = low_indices[j]
            
            # 确保 A < B < C < D
            if not (A_idx < B_idx < C_idx < D_idx):
                continue
            
            A_price = prices[A_idx]
            B_price = prices[B_idx]
            C_price = prices[C_idx]
            D_price = prices[D_idx]
            
            # 计算斜率
            k1 = (C_price - A_price) / (C_idx - A_idx)  # 上轨斜率
            k2 = (D_price - B_price) / (D_idx - B_idx)  # 下轨斜率
            
            # 检查收敛条件
            if k1 >= k2:
                continue
            if k1 > min_down_slope:
                continue
            if k2 < min_up_slope:
                continue
            
            # 计算交点
            b1 = A_price - k1 * A_idx
            b2 = B_price - k2 * B_idx
            
            if abs(k1 - k2) < 1e-10:
                continue
            
            t_buy = (b2 - b1) / (k1 - k2)
            days_to_apex = t_buy - t_now
            
            # 检查是否在有效买入区间
            if 0 <= days_to_apex <= max_days_ahead:
                # 计算信号强度
                convergence_speed = k2 - k1  # 收敛速度
                confidence = min(convergence_speed * 1000, 1.0)
                signal_strength = confidence * (1 - days_to_apex / (max_days_ahead + 1))
                
                return signal_strength, days_to_apex, confidence
    
    return 0.0, 999.0, 0.0


@njit(parallel=True, cache=True, fastmath=True)
def _compute_apex_factor_matrix(close_matrix: np.ndarray, window: int = 2) -> np.ndarray:
    """
    向量化计算所有股票的收敛三角形因子
    
    Parameters:
        close_matrix: (T, N) 收盘价矩阵，T=时间, N=股票数
        window: 极值窗口
    
    Returns:
        factor_matrix: (T, N) 因子值矩阵，每行表示该日期各股票的信号强度
    """
    T, N = close_matrix.shape
    factor_matrix = np.zeros((T, N), dtype=np.float32)
    
    # 从第 20 天开始计算（需要足够的历史数据）
    for t in range(20, T):
        for j in prange(N):  # 并行处理每只股票
            # 使用到当前日期的历史数据
            hist_prices = close_matrix[:t+1, j]
            
            # 查找极值点
            high_indices, low_indices = _find_local_extrema_numba(hist_prices, window)
            
            # 计算收敛信号
            signal, days_to_apex, confidence = _calculate_apex_convergence(
                hist_prices, high_indices, low_indices
            )
            
            factor_matrix[t, j] = signal
    
    return factor_matrix


# ============================================================================
# 因子注册接口
# ============================================================================

class PatternFactorCalculator:
    """
    形态因子计算器
    
    供 FactorPrecomputeEngine 调用
    """
    
    @staticmethod
    def apex_convergence(close_matrix: np.ndarray, **kwargs) -> np.ndarray:
        """
        收敛三角形因子
        
        返回值: (T, N) 矩阵，每个值表示该日期该股票的买入信号强度 (0-1)
        """
        window = kwargs.get('window', 2)
        return _compute_apex_factor_matrix(close_matrix, window)
    
    @staticmethod
    def extrema_high(close_matrix: np.ndarray, window: int = 2) -> np.ndarray:
        """
        局部高点标记因子
        
        返回值: (T, N) 矩阵，1表示高点，0表示非高点
        """
        T, N = close_matrix.shape
        result = np.zeros((T, N), dtype=np.int8)
        
        for j in range(N):
            high_indices, _ = _find_local_extrema_numba(close_matrix[:, j], window)
            for idx in high_indices:
                if idx >= 0:
                    result[idx, j] = 1
        
        return result
    
    @staticmethod
    def extrema_low(close_matrix: np.ndarray, window: int = 2) -> np.ndarray:
        """
        局部低点标记因子
        
        返回值: (T, N) 矩阵，1表示低点，0表示非低点
        """
        T, N = close_matrix.shape
        result = np.zeros((T, N), dtype=np.int8)
        
        for j in range(N):
            _, low_indices = _find_local_extrema_numba(close_matrix[:, j], window)
            for idx in low_indices:
                if idx >= 0:
                    result[idx, j] = 1
        
        return result
    
    @staticmethod
    def double_bottom(close_matrix: np.ndarray, **kwargs) -> np.ndarray:
        """
        W底/双底形态因子
        
        返回值: (T, N) 矩阵，信号强度 (0-1)
        """
        T, N = close_matrix.shape
        factor = np.zeros((T, N), dtype=np.float32)
        
        tolerance = kwargs.get('tolerance', 0.03)  # 低点价格差异容忍度
        
        for j in range(N):
            for t in range(40, T):  # 需要至少40天数据
                hist = close_matrix[:t+1, j]
                _, low_indices = _find_local_extrema_numba(hist, window=3)
                
                if len(low_indices) < 2:
                    continue
                
                # 找最近两个低点
                for i in range(len(low_indices) - 1, 0, -1):
                    D1_idx = low_indices[i-1]
                    D2_idx = low_indices[i]
                    
                    if D2_idx - D1_idx < 10:  # 至少间隔10天
                        continue
                    
                    D1_price = hist[D1_idx]
                    D2_price = hist[D2_idx]
                    
                    # 检查是否为双底（价格相近）
                    price_diff = abs(D2_price - D1_price) / D1_price
                    if price_diff < tolerance:
                        # 检查中间是否有明显高点
                        mid_max = np.max(hist[D1_idx:D2_idx])
                        if mid_max > D1_price * 1.05:  # 中间有至少5%反弹
                            factor[t, j] = 1.0 - price_diff / tolerance
                            break
        
        return factor


# ============================================================================
# 因子注册表
# ============================================================================

PATTERN_FACTORS = {
    'apex_convergence': {
        'function': PatternFactorCalculator.apex_convergence,
        'dependencies': ['close'],
        'description': '收敛三角形买入信号强度 (0-1)',
    },
    'extrema_high': {
        'function': PatternFactorCalculator.extrema_high,
        'dependencies': ['close'],
        'description': '局部高点标记 (0/1)',
    },
    'extrema_low': {
        'function': PatternFactorCalculator.extrema_low,
        'dependencies': ['close'],
        'description': '局部低点标记 (0/1)',
    },
    'double_bottom': {
        'function': PatternFactorCalculator.double_bottom,
        'dependencies': ['close'],
        'description': 'W底/双底形态信号强度 (0-1)',
    },
}


def get_pattern_factor(name: str):
    """获取形态因子计算函数"""
    if name in PATTERN_FACTORS:
        return PATTERN_FACTORS[name]['function']
    return None


def list_pattern_factors():
    """列出所有可用的形态因子"""
    return {k: v['description'] for k, v in PATTERN_FACTORS.items()}