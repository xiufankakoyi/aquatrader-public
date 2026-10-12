"""
DTW精细匹配模块

使用动态时间规整(DTW)算法对粗筛后的候选进行精细相似度匹配，
支持dtaidistance库加速，不可用时回退到纯NumPy实现。
"""

import numpy as np
from loguru import logger

try:
    from dtaidistance.dtw import distance as dtw_distance
    DTAIDISTANCE_AVAILABLE = True
except ImportError:
    DTAIDISTANCE_AVAILABLE = False
    logger.debug("dtaidistance not available, falling back to numpy DTW implementation")

try:
    from numba import njit

    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False


def _dtw_distance_python(s1: np.ndarray, s2: np.ndarray) -> float:
    """Memory-bounded Python fallback for DTW."""
    n, m = len(s1), len(s2)
    previous = np.full(m + 1, np.inf)
    current = np.full(m + 1, np.inf)
    previous[0] = 0.0

    for i in range(1, n + 1):
        current.fill(np.inf)
        for j in range(1, m + 1):
            cost = (s1[i - 1] - s2[j - 1]) ** 2
            current[j] = cost + min(
                previous[j],
                current[j - 1],
                previous[j - 1],
            )
        previous, current = current, previous

    return float(np.sqrt(previous[m]))


if NUMBA_AVAILABLE:

    @njit(cache=True)
    def _dtw_distance_numba(s1: np.ndarray, s2: np.ndarray) -> float:
        n, m = len(s1), len(s2)
        previous = np.full(m + 1, np.inf)
        current = np.full(m + 1, np.inf)
        previous[0] = 0.0

        for i in range(1, n + 1):
            current[:] = np.inf
            for j in range(1, m + 1):
                cost = (s1[i - 1] - s2[j - 1]) ** 2
                current[j] = cost + min(
                    previous[j],
                    current[j - 1],
                    previous[j - 1],
                )
            previous, current = current, previous

        return np.sqrt(previous[m])


def _dtw_distance(s1: np.ndarray, s2: np.ndarray) -> float:
    """
    标准DTW动态规划算法

    Args:
        s1: 第一条序列
        s2: 第二条序列

    Returns:
        DTW距离值
    """
    s1 = np.asarray(s1, dtype=np.float64)
    s2 = np.asarray(s2, dtype=np.float64)
    if NUMBA_AVAILABLE:
        return float(_dtw_distance_numba(s1, s2))
    return _dtw_distance_python(s1, s2)


def dtw_match(
    template: np.ndarray,
    candidates: list[dict],
    top_n: int = 10,
) -> list[dict]:
    """
    使用DTW算法对候选序列进行精细匹配

    Args:
        template: 归一化后的模板序列
        candidates: 粗筛后的候选列表，每个包含 "normalized" 和 "metadata"
        top_n: 返回的最大结果数

    Returns:
        按相似度降序排列的 top_n 结果，每个增加 "similarity_score" 字段
    """
    if len(candidates) == 0:
        return []

    template = np.asarray(template, dtype=np.float64)
    results = []

    for candidate in candidates:
        normalized = np.asarray(candidate["normalized"], dtype=np.float64)

        if len(normalized) != len(template):
            continue

        if DTAIDISTANCE_AVAILABLE:
            distance = dtw_distance(template, normalized)
        else:
            distance = _dtw_distance(template, normalized)

        similarity_score = 1.0 / (1.0 + distance)

        result = dict(candidate)
        result["similarity_score"] = float(similarity_score)
        results.append(result)

    results.sort(key=lambda x: x["similarity_score"], reverse=True)

    return results[:top_n]
