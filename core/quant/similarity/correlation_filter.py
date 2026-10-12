"""
皮尔逊相关系数粗筛模块

对候选窗口进行快速相关性过滤，
保留与模板序列相关系数超过阈值的候选，减少DTW计算量。
"""

import numpy as np


def correlation_filter(
    template: np.ndarray,
    candidates: list[dict],
    threshold: float = 0.85,
) -> list[dict]:
    """
    基于皮尔逊相关系数对候选序列进行粗筛

    Args:
        template: 归一化后的模板序列
        candidates: 每个dict包含 "normalized" (np.ndarray) 和 "metadata"
        threshold: 相关系数阈值，低于此值的候选被过滤

    Returns:
        相关系数 >= threshold 的候选列表，每个dict增加 "corr_score" 字段
    """
    if len(candidates) == 0:
        return []

    template = np.asarray(template, dtype=np.float64)
    filtered = []

    for candidate in candidates:
        normalized = np.asarray(candidate["normalized"], dtype=np.float64)

        if len(normalized) != len(template):
            continue

        if np.std(normalized) == 0 or np.std(template) == 0:
            continue

        corr_matrix = np.corrcoef(template, normalized)
        corr_score = corr_matrix[0, 1]

        if np.isnan(corr_score):
            continue

        if corr_score >= threshold:
            result = dict(candidate)
            result["corr_score"] = float(corr_score)
            filtered.append(result)

    return filtered
