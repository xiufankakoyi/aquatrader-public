"""
段内K线阴阳节奏特征提取模块

提供基于阴阳线节奏的特征向量提取与相似度计算能力。
"""

import numpy as np
from dataclasses import dataclass


@dataclass
class RhythmVector:
    """
    段内节奏向量

    Attributes:
        bullish_density: 阳线密度 (0~1)
        body_fullness: 实体饱满度 (0~1)
        wave_uniformity: 波动均匀性 (0~1)
        volume_uniformity: 量能均匀性 (0~1)
        rebound_frequency: 回踩反包频率 (0~1)
        max_bullish_streak_ratio: 最大连续阳线比 (0~1)
        avg_drawdown: 平均回撤幅度 (0~1)
        volume_trend: 量能趋势 (0~2, 1=持平, >1=后半段放量, <1=前半段放量)
    """

    bullish_density: float
    body_fullness: float
    wave_uniformity: float
    volume_uniformity: float
    rebound_frequency: float
    max_bullish_streak_ratio: float
    avg_drawdown: float
    volume_trend: float


def extract_rhythm_vector(klines: dict) -> RhythmVector:
    """
    从K线数据提取段内节奏向量

    Args:
        klines: K线数据字典，包含
            - open: np.ndarray
            - high: np.ndarray
            - low: np.ndarray
            - close: np.ndarray
            - volume: np.ndarray

    Returns:
        RhythmVector: 8维节奏特征向量
    """
    open_arr = np.asarray(klines["open"], dtype=np.float64)
    high_arr = np.asarray(klines["high"], dtype=np.float64)
    low_arr = np.asarray(klines["low"], dtype=np.float64)
    close_arr = np.asarray(klines["close"], dtype=np.float64)
    volume_arr = np.asarray(klines["volume"], dtype=np.float64)

    n = len(open_arr)
    if n == 0:
        return RhythmVector(
            bullish_density=0.0,
            body_fullness=0.0,
            wave_uniformity=0.5,
            volume_uniformity=0.5,
            rebound_frequency=0.0,
            max_bullish_streak_ratio=0.0,
            avg_drawdown=0.0,
            volume_trend=1.0,
        )

    bullish_density = _calc_bullish_density(close_arr, open_arr)
    body_fullness = _calc_body_fullness(open_arr, high_arr, low_arr, close_arr)
    wave_uniformity = _calc_wave_uniformity(high_arr, low_arr, n)
    volume_uniformity = _calc_volume_uniformity(volume_arr)
    rebound_frequency = _calc_rebound_frequency(close_arr, open_arr, high_arr, low_arr)
    max_bullish_streak_ratio = _calc_max_bullish_streak_ratio(close_arr, open_arr)
    avg_drawdown = _calc_avg_drawdown(close_arr, open_arr, high_arr)
    volume_trend = _calc_volume_trend(volume_arr, n)

    return RhythmVector(
        bullish_density=bullish_density,
        body_fullness=body_fullness,
        wave_uniformity=wave_uniformity,
        volume_uniformity=volume_uniformity,
        rebound_frequency=rebound_frequency,
        max_bullish_streak_ratio=max_bullish_streak_ratio,
        avg_drawdown=avg_drawdown,
        volume_trend=volume_trend,
    )


def _calc_bullish_density(close_arr: np.ndarray, open_arr: np.ndarray) -> float:
    """计算阳线密度"""
    if len(close_arr) == 0:
        return 0.0
    bullish_count = np.sum(close_arr >= open_arr)
    return bullish_count / len(close_arr)


def _calc_body_fullness(
    open_arr: np.ndarray,
    high_arr: np.ndarray,
    low_arr: np.ndarray,
    close_arr: np.ndarray,
) -> float:
    """计算实体饱满度"""
    body = np.abs(close_arr - open_arr)
    amplitude = high_arr - low_arr

    valid_mask = amplitude >= 0.01
    if not np.any(valid_mask):
        return 0.0

    valid_body = body[valid_mask]
    valid_amplitude = amplitude[valid_mask]

    return np.mean(valid_body / valid_amplitude)


def _calc_wave_uniformity(
    high_arr: np.ndarray, low_arr: np.ndarray, n: int
) -> float:
    """计算波动均匀性"""
    if n < 3:
        return 0.5

    amplitude = high_arr - low_arr
    avg_price = (high_arr + low_arr) / 2
    relative_wave = amplitude / avg_price

    return 1.0 / (1.0 + np.std(relative_wave))


def _calc_volume_uniformity(volume_arr: np.ndarray) -> float:
    """计算量能均匀性"""
    mean_vol = np.mean(volume_arr)
    if mean_vol == 0:
        return 0.5

    cv = np.std(volume_arr) / mean_vol
    return 1.0 / (1.0 + cv)


def _calc_rebound_frequency(
    close_arr: np.ndarray, open_arr: np.ndarray, high_arr: np.ndarray, low_arr: np.ndarray
) -> float:
    """计算回踩反包频率"""
    n = len(close_arr)
    if n < 3:
        return 0.0

    is_bullish = close_arr >= open_arr
    is_bearish = ~is_bullish

    total_retreat = 0
    rebound_count = 0

    i = 0
    while i < n:
        if is_bearish[i]:
            retreat_start = i
            retreat_length = 0

            while i < n and is_bearish[i]:
                retreat_length += 1
                i += 1

            total_retreat += 1

            if i < n and is_bullish[i]:
                prev_bearish_body = max(close_arr[retreat_start], open_arr[retreat_start]) - \
                                    min(close_arr[retreat_start], open_arr[retreat_start])

                curr_bullish_body = max(close_arr[i], open_arr[i]) - \
                                    min(close_arr[i], open_arr[i])

                if retreat_length >= 2 and curr_bullish_body > prev_bearish_body:
                    rebound_count += 1
        else:
            i += 1

    if total_retreat == 0:
        return 0.0

    return rebound_count / total_retreat


def _calc_max_bullish_streak_ratio(close_arr: np.ndarray, open_arr: np.ndarray) -> float:
    """计算最大连续阳线比"""
    n = len(close_arr)
    if n == 0:
        return 0.0

    is_bullish = close_arr >= open_arr
    total_bullish = np.sum(is_bullish)

    if total_bullish == 0:
        return 0.0

    max_streak = 0
    current_streak = 0

    for bullish in is_bullish:
        if bullish:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak / total_bullish


def _calc_avg_drawdown(
    close_arr: np.ndarray, open_arr: np.ndarray, high_arr: np.ndarray
) -> float:
    """计算平均回撤幅度"""
    n = len(close_arr)
    if n < 2:
        return 0.0

    is_bullish = close_arr >= open_arr

    drawdowns = []
    in_retreat = False
    retreat_high = 0.0

    for i in range(n):
        if is_bullish[i]:
            if in_retreat and retreat_high > 0:
                local_low = np.min(close_arr[i:])
                drawdown = (retreat_high - local_low) / retreat_high
                if drawdown > 0:
                    drawdowns.append(drawdown)
            in_retreat = False
        else:
            if not in_retreat and i > 0:
                in_retreat = True
                retreat_high = np.max(high_arr[:i])

    if len(drawdowns) == 0:
        return 0.0

    return np.mean(drawdowns)


def _calc_volume_trend(volume_arr: np.ndarray, n: int) -> float:
    """计算量能趋势"""
    if n < 2:
        return 1.0

    if n < 4:
        mid = 1
    else:
        mid = n // 2

    first_half = volume_arr[:mid]
    second_half = volume_arr[mid:]

    mean_first = np.mean(first_half)
    if mean_first == 0:
        return 1.0

    mean_second = np.mean(second_half)
    return mean_second / mean_first


def to_array(vector: RhythmVector) -> np.ndarray:
    """
    将RhythmVector转换为8维numpy数组

    Args:
        vector: 节奏向量

    Returns:
        np.ndarray: 8维特征数组，顺序为：
            bullish_density, body_fullness, wave_uniformity,
            volume_uniformity, rebound_frequency, max_bullish_streak_ratio,
            avg_drawdown, volume_trend
    """
    return np.array([
        vector.bullish_density,
        vector.body_fullness,
        vector.wave_uniformity,
        vector.volume_uniformity,
        vector.rebound_frequency,
        vector.max_bullish_streak_ratio,
        vector.avg_drawdown,
        vector.volume_trend,
    ], dtype=np.float64)


def cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """
    计算两个节奏向量的余弦相似度

    Args:
        v1: 第一个节奏向量 (8维)
        v2: 第二个节奏向量 (8维)

    Returns:
        float: 余弦相似度 (0~1)
    """
    v1 = np.asarray(v1, dtype=np.float64)
    v2 = np.asarray(v2, dtype=np.float64)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    dot_product = np.dot(v1, v2)
    similarity = dot_product / (norm1 * norm2)

    return max(0.0, min(1.0, similarity))
