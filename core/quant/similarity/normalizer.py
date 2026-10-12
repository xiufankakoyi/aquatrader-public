"""
K线序列Z-Score归一化模块

将原始收盘价序列归一化为零均值单位方差的序列，
消除不同股票价格量纲差异，便于相似度比较。
"""

import numpy as np


def normalize_kline(prices: np.ndarray) -> np.ndarray:
    """
    对收盘价序列进行Z-Score归一化

    Args:
        prices: 原始收盘价序列

    Returns:
        归一化后的序列 (prices - mean) / std，
        若std==0返回全0序列，若输入长度为0返回空数组
    """
    if len(prices) == 0:
        return np.array([], dtype=np.float64)

    prices = np.asarray(prices, dtype=np.float64)
    mean = np.mean(prices)
    std = np.std(prices)

    if std == 0:
        return np.zeros_like(prices)

    return (prices - mean) / std
