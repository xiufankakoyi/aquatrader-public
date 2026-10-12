"""
相似度匹配模块

提供K线形态相似度匹配的完整流水线：
归一化 -> 相关性粗筛 -> DTW精细匹配 -> 模式增强

典型用法:
    engine = SimilarityEngine(corr_threshold=0.85, pattern_type="breakout_volume")
    results = engine.match(template_close, candidate_windows, top_n=10)
"""

import numpy as np
from loguru import logger

from .normalizer import normalize_kline
from .correlation_filter import correlation_filter
from .dtw_matcher import dtw_match
from .pattern_enhancer import PatternEnhancer
from .window_generator import generate_sliding_windows


class SimilarityEngine:
    """
    相似度匹配引擎

    组合归一化、相关性粗筛、DTW精细匹配和模式增强组件，
    提供一站式K线形态相似度匹配能力。

    Attributes:
        corr_threshold: 相关性粗筛阈值
        enhancer: 模式增强器实例
    """

    def __init__(
        self,
        corr_threshold: float = 0.85,
        pattern_type: str = None,
    ):
        """
        初始化相似度匹配引擎

        Args:
            corr_threshold: 皮尔逊相关系数阈值，低于此值的候选被过滤
            pattern_type: 模式增强类型，None表示不启用增强，
                支持 "breakout_volume"、"limit_break"、"n_shape"
        """
        self.corr_threshold = corr_threshold
        self.enhancer = PatternEnhancer(pattern_type=pattern_type)

    def match(
        self,
        template_close: np.ndarray,
        candidate_windows: list[dict],
        top_n: int = 10,
    ) -> list[dict]:
        """
        执行完整的相似度匹配流水线

        Args:
            template_close: 归一化后的模板收盘价序列
            candidate_windows: 候选窗口列表，每个dict包含
                "normalized" (归一化序列) 和 "metadata" (stock_code, start_date, end_date等)
            top_n: 返回的最大结果数

        Returns:
            top_n 个匹配结果，每个包含 metadata + similarity_score
        """
        if len(candidate_windows) == 0:
            return []

        template_close = np.asarray(template_close, dtype=np.float64)

        logger.debug(
            f"Starting similarity match: "
            f"{len(candidate_windows)} candidates, top_n={top_n}"
        )

        filtered = correlation_filter(
            template_close, candidate_windows, self.corr_threshold
        )
        logger.debug(f"Correlation filter: {len(filtered)} passed")

        matched = dtw_match(template_close, filtered, top_n=top_n)
        logger.debug(f"DTW match: {len(matched)} results")

        if self.enhancer.pattern_type is not None and len(matched) > 0:
            template_data = {"close": template_close}
            matched = self.enhancer.enhance(matched, template_data)
            logger.debug(
                f"Pattern enhancement ({self.enhancer.pattern_type}): "
                f"{len(matched)} results"
            )

        results = []
        for item in matched:
            result = dict(item.get("metadata", {}))
            result["similarity_score"] = item.get("similarity_score", 0.0)
            if "enhanced_score" in item:
                result["enhanced_score"] = item["enhanced_score"]
            if "corr_score" in item:
                result["corr_score"] = item["corr_score"]
            results.append(result)

        return results


__all__ = [
    "normalize_kline",
    "correlation_filter",
    "dtw_match",
    "PatternEnhancer",
    "generate_sliding_windows",
    "SimilarityEngine",
]
