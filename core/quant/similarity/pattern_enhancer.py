"""
模式增强模块

针对特定K线形态（5进6爆量、断板推新高、N字走势），
在DTW价格匹配基础上融合成交量、涨停标签、曲率等特征，
重新排序匹配结果以提升形态识别精度。
"""

import numpy as np
from loguru import logger

from .normalizer import normalize_kline
from .dtw_matcher import _dtw_distance


class PatternEnhancer:
    """
    特定模式增强器

    根据pattern_type调用不同的增强方法，
    将价格DTW得分与辅助特征加权融合后重新排序。

    Attributes:
        pattern_type: 模式类型，支持 "breakout_volume"、"limit_break"、"n_shape"
        price_weight: 价格得分权重
        volume_weight: 成交量得分权重
    """

    VALID_TYPES = {"breakout_volume", "limit_break", "n_shape"}

    def __init__(
        self,
        pattern_type: str = None,
        price_weight: float = 0.6,
        volume_weight: float = 0.4,
    ):
        """
        初始化模式增强器

        Args:
            pattern_type: 模式类型，None表示不启用增强
            price_weight: 价格得分权重
            volume_weight: 成交量得分权重
        """
        if pattern_type is not None and pattern_type not in self.VALID_TYPES:
            raise ValueError(
                f"Invalid pattern_type: {pattern_type}, "
                f"must be one of {self.VALID_TYPES} or None"
            )
        self.pattern_type = pattern_type
        self.price_weight = price_weight
        self.volume_weight = volume_weight

    def enhance(
        self,
        results: list[dict],
        template_data: dict = None,
    ) -> list[dict]:
        """
        根据pattern_type对匹配结果进行增强排序

        Args:
            results: DTW匹配结果列表，每个包含 similarity_score 和 metadata
            template_data: 模板原始数据，包含 "close"、"volume"（可选）等

        Returns:
            增强后重新排序的结果列表
        """
        if self.pattern_type is None or len(results) == 0:
            return results

        if template_data is None:
            logger.warning("template_data is None, skipping enhancement")
            return results

        enhancer_map = {
            "breakout_volume": self._enhance_breakout_volume,
            "limit_break": self._enhance_limit_break,
            "n_shape": self._enhance_n_shape,
        }

        enhancer_fn = enhancer_map.get(self.pattern_type)
        if enhancer_fn is None:
            return results

        return enhancer_fn(results, template_data)

    def _enhance_breakout_volume(
        self,
        results: list[dict],
        template_data: dict,
    ) -> list[dict]:
        """
        "5进6爆量"模式增强

        计算候选片段的成交量放量倍数和成交量序列相关系数，
        与价格DTW得分加权融合。

        Args:
            results: 匹配结果列表
            template_data: 模板原始数据，需包含 "close" 和 "volume"

        Returns:
            增强后重新排序的结果列表
        """
        template_volume = template_data.get("volume")
        if template_volume is None:
            return results

        template_volume = np.asarray(template_volume, dtype=np.float64)
        template_vol_norm = normalize_kline(template_volume)

        enhanced = []
        for result in results:
            candidate_volume = result.get("volume")
            if candidate_volume is None:
                enhanced.append(result)
                continue

            candidate_volume = np.asarray(candidate_volume, dtype=np.float64)
            candidate_vol_norm = normalize_kline(candidate_volume)

            price_score = result.get("similarity_score", 0.0)

            if len(candidate_volume) >= 6:
                recent_vol = candidate_volume[-1]
                prev_avg_vol = np.mean(candidate_volume[-6:-1])
                volume_ratio = recent_vol / prev_avg_vol if prev_avg_vol > 0 else 1.0
            else:
                volume_ratio = 1.0

            if (
                len(template_vol_norm) == len(candidate_vol_norm)
                and np.std(candidate_vol_norm) > 0
                and np.std(template_vol_norm) > 0
            ):
                vol_corr = float(np.corrcoef(template_vol_norm, candidate_vol_norm)[0, 1])
                if np.isnan(vol_corr):
                    vol_corr = 0.0
            else:
                vol_corr = 0.0

            volume_score = 0.5 * min(volume_ratio / 3.0, 1.0) + 0.5 * max(vol_corr, 0.0)
            combined_score = (
                self.price_weight * price_score
                + self.volume_weight * volume_score
            )

            enhanced_result = dict(result)
            enhanced_result["enhanced_score"] = float(combined_score)
            enhanced_result["volume_ratio"] = float(volume_ratio)
            enhanced_result["volume_corr"] = float(vol_corr)
            enhanced.append(enhanced_result)

        enhanced.sort(key=lambda x: x.get("enhanced_score", x.get("similarity_score", 0.0)), reverse=True)
        return enhanced

    def _enhance_limit_break(
        self,
        results: list[dict],
        template_data: dict,
    ) -> list[dict]:
        """
        "断板推新高"模式增强

        生成涨停/断板标签序列，计算标签匹配度，
        与价格DTW得分加权融合。

        Args:
            results: 匹配结果列表
            template_data: 模板原始数据，需包含 "close"

        Returns:
            增强后重新排序的结果列表
        """
        template_close = np.asarray(template_data["close"], dtype=np.float64)
        template_labels = self._generate_limit_labels(template_close)

        enhanced = []
        for result in results:
            candidate_close = result.get("close")
            if candidate_close is None:
                enhanced.append(result)
                continue

            candidate_close = np.asarray(candidate_close, dtype=np.float64)
            candidate_labels = self._generate_limit_labels(candidate_close)

            price_score = result.get("similarity_score", 0.0)

            min_len = min(len(template_labels), len(candidate_labels))
            if min_len == 0:
                label_match = 0.0
            else:
                matches = np.sum(template_labels[:min_len] == candidate_labels[:min_len])
                label_match = float(matches) / float(min_len)

            combined_score = (
                self.price_weight * price_score
                + self.volume_weight * label_match
            )

            enhanced_result = dict(result)
            enhanced_result["enhanced_score"] = float(combined_score)
            enhanced_result["label_match"] = float(label_match)
            enhanced.append(enhanced_result)

        enhanced.sort(key=lambda x: x.get("enhanced_score", x.get("similarity_score", 0.0)), reverse=True)
        return enhanced

    def _enhance_n_shape(
        self,
        results: list[dict],
        template_data: dict,
    ) -> list[dict]:
        """
        "N字走势"模式增强

        计算曲率特征（二阶差分），对曲率序列做DTW匹配，
        与价格DTW得分加权融合。

        Args:
            results: 匹配结果列表
            template_data: 模板原始数据，需包含 "close"

        Returns:
            增强后重新排序的结果列表
        """
        template_close = np.asarray(template_data["close"], dtype=np.float64)
        template_curvature = self._compute_curvature(template_close)

        enhanced = []
        for result in results:
            candidate_close = result.get("close")
            if candidate_close is None:
                enhanced.append(result)
                continue

            candidate_close = np.asarray(candidate_close, dtype=np.float64)
            candidate_curvature = self._compute_curvature(candidate_close)

            price_score = result.get("similarity_score", 0.0)

            if (
                len(template_curvature) > 0
                and len(candidate_curvature) > 0
                and len(template_curvature) == len(candidate_curvature)
            ):
                curvature_distance = _dtw_distance(template_curvature, candidate_curvature)
                curvature_score = 1.0 / (1.0 + curvature_distance)
            else:
                curvature_score = 0.0

            combined_score = (
                self.price_weight * price_score
                + self.volume_weight * curvature_score
            )

            enhanced_result = dict(result)
            enhanced_result["enhanced_score"] = float(combined_score)
            enhanced_result["curvature_score"] = float(curvature_score)
            enhanced.append(enhanced_result)

        enhanced.sort(key=lambda x: x.get("enhanced_score", x.get("similarity_score", 0.0)), reverse=True)
        return enhanced

    @staticmethod
    def _generate_limit_labels(close: np.ndarray) -> np.ndarray:
        """
        生成涨停/断板标签序列

        Args:
            close: 收盘价序列

        Returns:
            标签数组：1=涨停(涨幅>=9.8%), -1=断板(前日涨停今日未涨停), 0=其他
        """
        if len(close) < 2:
            return np.zeros(len(close), dtype=np.int32)

        pct_change = np.zeros(len(close), dtype=np.float64)
        pct_change[1:] = (close[1:] - close[:-1]) / np.where(close[:-1] > 0, close[:-1], 1.0)

        labels = np.zeros(len(close), dtype=np.int32)
        for i in range(1, len(close)):
            if pct_change[i] >= 0.098:
                labels[i] = 1
            elif labels[i - 1] == 1 and pct_change[i] < 0.098:
                labels[i] = -1

        return labels

    @staticmethod
    def _compute_curvature(prices: np.ndarray) -> np.ndarray:
        """
        计算价格序列的曲率特征（二阶差分）

        Args:
            prices: 价格序列

        Returns:
            曲率序列，长度比输入少2
        """
        if len(prices) < 3:
            return np.array([], dtype=np.float64)

        first_diff = np.diff(prices)
        second_diff = np.diff(first_diff)
        return second_diff
