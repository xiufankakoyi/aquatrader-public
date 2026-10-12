"""
Pattern Enhancer V2 Module

Scene-differentiated configuration and end-to-end skeleton-based matcher.
"""

import numpy as np

from .zigzag import identify_extrema, build_segments
from .rhythm import extract_rhythm_vector, to_array as rhythm_to_array, cosine_similarity as rhythm_cosine_similarity
from .structure_encoder import StructureEncoder, compute_direction_similarity, fast_filter
from .segment_match import (
    segment_edit_distance,
    align_segments,
    compute_structure_similarity,
    compute_rhythm_similarity,
    compute_ma_fit_similarity,
    compute_weighted_score,
)


class SceneConfig:
    """
    Scene-differentiated configuration.

    Defines weights for different matching scenes.

    Attributes:
        structure_weight: Weight for structure similarity
        rhythm_weight: Weight for rhythm similarity
        ma_fit_weight: Weight for MA fit similarity
        description: Human-readable description
    """

    BREAKOUT_VOLUME = None
    LIMIT_BREAK = None
    N_SHAPE = None
    TREND = None
    DEFAULT = None

    def __init__(
        self,
        structure_weight: float,
        rhythm_weight: float,
        ma_fit_weight: float,
        description: str,
    ):
        self.structure_weight = structure_weight
        self.rhythm_weight = rhythm_weight
        self.ma_fit_weight = ma_fit_weight
        self.description = description

    @classmethod
    def from_scene(cls, scene: str) -> "SceneConfig":
        """
        Get configuration from scene name.

        Args:
            scene: Scene name (default/breakout_volume/limit_break/n_shape/trend)

        Returns:
            SceneConfig instance
        """
        scene_map = {
            "default": cls.DEFAULT,
            "breakout_volume": cls.BREAKOUT_VOLUME,
            "limit_break": cls.LIMIT_BREAK,
            "n_shape": cls.N_SHAPE,
            "trend": cls.TREND,
        }
        config = scene_map.get(scene.lower())
        if config is None:
            return cls.DEFAULT
        return config

    def to_weights(self) -> dict:
        """
        Convert to weight dictionary.

        Returns:
            Dictionary with structure, rhythm, ma_fit keys
        """
        return {
            "structure": self.structure_weight,
            "rhythm": self.rhythm_weight,
            "ma_fit": self.ma_fit_weight,
        }


SceneConfig.BREAKOUT_VOLUME = SceneConfig(0.15, 0.65, 0.20, "连板爆量场景")
SceneConfig.LIMIT_BREAK = SceneConfig(0.20, 0.60, 0.20, "断板推新高场景")
SceneConfig.N_SHAPE = SceneConfig(0.10, 0.70, 0.20, "N字走势场景")
SceneConfig.TREND = SceneConfig(0.15, 0.50, 0.35, "趋势跟踪场景")
SceneConfig.DEFAULT = SceneConfig(0.20, 0.60, 0.20, "默认平衡场景")


class SkeletonMatcher:
    """
    End-to-end skeleton-based similarity matcher.

    Combines all matching layers: ZigZag extraction -> Rhythm extraction ->
    Structure encoding -> Coarse filtering -> Segment alignment -> Fine matching.

    Attributes:
        min_pct: Minimum percentage for ZigZag turning point detection
        min_bars: Minimum bars between turning points
        ma_window: Moving average window for MA fit
        scene: Scene configuration
    """

    def __init__(
        self,
        min_pct: float = 0.03,
        min_bars: int = 2,
        ma_window: int = 20,
        scene: str = "default",
    ):
        """
        Initialize skeleton matcher.

        Args:
            min_pct: Minimum turning point percentage (default 3%)
            min_bars: Minimum bars between turning points (default 2)
            ma_window: MA window for fit calculation (default 20)
            scene: Scene name (default/breakout_volume/limit_break/n_shape/trend)
        """
        self.min_pct = min_pct
        self.min_bars = min_bars
        self.ma_window = ma_window
        self.scene_config = SceneConfig.from_scene(scene)
        self.encoder = StructureEncoder(min_pct=min_pct, min_bars=min_bars, ma_windows=[ma_window])

    def encode(self, klines: dict) -> dict:
        """
        Encode K-line sequence into structure descriptor.

        Args:
            klines: Dictionary with open/high/low/close/volume arrays

        Returns:
            Structure descriptor dictionary
        """
        return self.encoder.encode(klines)

    def filter(self, template_code: dict, candidate_codes: list[dict]) -> list[dict]:
        """
        Fast filter candidates by structure.

        Args:
            template_code: Template structure descriptor
            candidate_codes: List of candidate structure descriptors

        Returns:
            Filtered list of candidates
        """
        return fast_filter(template_code, candidate_codes)

    def match(
        self,
        template_klines: dict,
        candidate_klines: dict,
    ) -> dict:
        """
        Compute comprehensive similarity between template and candidate.

        Args:
            template_klines: Template K-line data
            candidate_klines: Candidate K-line data

        Returns:
            Dictionary with keys: structure_score, rhythm_score, ma_fit_score, total_score
        """
        t_encoded = self.encoder.encode(template_klines)
        c_encoded = self.encoder.encode(candidate_klines)

        t_segs = t_encoded.get("segment_durations", [])
        c_segs = c_encoded.get("segment_durations", [])

        t_pct_changes = t_encoded.get("segment_pct_changes", [])
        c_pct_changes = c_encoded.get("segment_pct_changes", [])

        t_segments = []
        for i, d in enumerate(t_segs):
            seg = {"duration": d, "pct_change": 0.0}
            if i < len(t_pct_changes):
                seg["pct_change"] = t_pct_changes[i]
            t_segments.append(seg)

        c_segments = []
        for i, d in enumerate(c_segs):
            seg = {"duration": d, "pct_change": 0.0}
            if i < len(c_pct_changes):
                seg["pct_change"] = c_pct_changes[i]
            c_segments.append(seg)

        t_seq = list(t_encoded.get("direction_sequence", ""))
        c_seq = list(c_encoded.get("direction_sequence", ""))
        edit_dist = segment_edit_distance(t_seq, c_seq)

        aligned = align_segments(t_segments, c_segments, edit_dist)

        t_seq = tuple(t_encoded.get("direction_sequence", ""))

        t_rhythms = t_encoded.get("rhythm_vectors", [])
        c_rhythms = c_encoded.get("rhythm_vectors", [])

        structure_score = compute_structure_similarity(
            t_segments, aligned, t_seq
        )
        rhythm_score = compute_rhythm_similarity(
            list(t_rhythms), list(c_rhythms), aligned
        )
        ma_fit_score = compute_ma_fit_similarity(
            template_klines, candidate_klines, self.ma_window
        )

        total_score = compute_weighted_score(
            structure_score,
            rhythm_score,
            ma_fit_score,
            self.scene_config.to_weights(),
        )

        return {
            "structure_score": float(structure_score),
            "rhythm_score": float(rhythm_score),
            "ma_fit_score": float(ma_fit_score) if ma_fit_score is not None else None,
            "total_score": float(total_score),
        }

    def batch_match(
        self,
        template_klines: dict,
        candidate_list: list[dict],
        top_n: int = 10,
        min_score: float = 0.0,
        corr_threshold: float = 0.5,
        skip_layer1: bool = False,
    ) -> list[dict]:
        """
        Batch match with three-tier filtering.

        Pipeline:
        1. Layer 1 - Fast direction fingerprint index: O(1) lookup by MA crossover sequence
           (skipped if skip_layer1=True, assuming candidates already filtered)
        2. Layer 2 - Pearson correlation with early abandonment: O(n) fast filter
        3. Layer 3 - Skeleton fine matching on remaining candidates

        Args:
            template_klines: Template K-line data
            candidate_list: List of candidates, each with 'klines' and optional 'metadata'
            top_n: Maximum number of results to return
            min_score: Minimum total_score threshold
            corr_threshold: Pearson correlation threshold for early filter (default 0.5)
            skip_layer1: If True, skip Layer 1 direction filtering (candidates already filtered)

        Returns:
            List of match results sorted by total_score descending
        """
        t_close = np.asarray(template_klines["close"], dtype=np.float64)
        t_normalized = (t_close - t_close[0]) / t_close[0] if t_close[0] != 0 else t_close

        if skip_layer1:
            layer1_candidates = list(range(len(candidate_list)))
        else:
            template_seq = _fast_direction_sequence(t_close)
            layer1_indices = _build_direction_index_fast(candidate_list)
            layer1_candidates = layer1_indices.get(template_seq, [])

        if not layer1_candidates:
            return []

        layer2_results = _pearson_filter_with_early_abandon(
            t_normalized, layer1_candidates, candidate_list, corr_threshold
        )

        if not layer2_results:
            return []

        results = []
        for idx, corr_score in layer2_results:
            cand = candidate_list[idx]
            cand_klines = cand.get("klines", cand)
            metadata = cand.get("metadata", {})

            match_result = self.match(template_klines, cand_klines)
            match_result["metadata"] = metadata
            match_result["corr_score"] = corr_score

            if match_result["total_score"] >= min_score:
                results.append(match_result)

        results.sort(key=lambda x: x["total_score"], reverse=True)

        return results[:top_n]


def _fast_direction_sequence(close: np.ndarray, threshold_pct: float = 0.02, num_segments: int = 8) -> str:
    """
    Fast direction sequence extraction using segmented approach.
    Divides window into num_segments and computes direction for each.

    Args:
        close: Close price array
        threshold_pct: Minimum percentage change to consider direction (default 0.02 = 2%)
        num_segments: Number of segments to divide window into (default 8)

    Returns:
        Direction sequence string: e.g., 'U-D-U-D-F-F-U-D' for 8 segments
    """
    n = len(close)
    if n < num_segments:
        return "F"

    segment_size = n / num_segments
    parts = []
    for i in range(num_segments):
        start = int(i * segment_size)
        end = int((i + 1) * segment_size)
        if start >= n:
            break
        seg_close = close[start:end]
        if len(seg_close) < 2:
            continue
        pct_change = (seg_close[-1] - seg_close[0]) / seg_close[0] if seg_close[0] != 0 else 0
        if abs(pct_change) < threshold_pct:
            parts.append("F")
        elif pct_change > 0:
            parts.append("U")
        else:
            parts.append("D")

    return "-".join(parts) if parts else "F"


def _fast_direction_sequence_extended(close: np.ndarray, short_ma: int = 5, long_ma: int = 20) -> str:
    """
    Extended direction sequence using MA crossover.
    Falls back to simple change detection for short windows.

    Args:
        close: Close price array
        short_ma: Short MA window (default 5)
        long_ma: Long MA window (default 20)

    Returns:
        Direction sequence string: 'U' (up), 'D' (down), 'F' (flat)
    """
    n = len(close)
    if n < long_ma + 1:
        total_change = close[-1] - close[0]
        pct_change = total_change / close[0] if close[0] != 0 else 0
        threshold = 0.02
        if abs(pct_change) < threshold:
            return "F"
        return "U" if pct_change > 0 else "D"

    short_ma_arr = np.convolve(close, np.ones(short_ma)/short_ma, mode='valid')
    long_ma_arr = np.convolve(close, np.ones(long_ma)/long_ma, mode='valid')

    diff_len = len(short_ma_arr) - len(long_ma_arr)
    if diff_len > 0:
        short_ma_arr = short_ma_arr[diff_len:]

    positions = np.sign(short_ma_arr - long_ma_arr)
    changes = np.diff(positions)

    direction_map = {1: "U", -1: "D", 0: "F"}
    seq_parts = []
    last_pos = int(positions[0]) if len(positions) > 0 else 0
    seq_parts.append(direction_map.get(last_pos, "F"))

    for change in changes:
        if change != 0:
            new_pos = int(positions[np.where(np.diff(positions) == change)[0][0] + 1]) if len(np.where(np.diff(positions) == change)[0]) > 0 else 0
            seq_parts.append(direction_map.get(new_pos, "F"))

    return "".join(seq_parts) if seq_parts else "F"


def _build_direction_index_fast(candidate_list: list[dict]) -> dict:
    """
    Layer 1 Fast: Build hash-based index by direction sequence for O(1) lookup.
    Uses MA crossover instead of full ZigZag encoding - ~100x faster.

    Args:
        candidate_list: List of candidates

    Returns:
        Dict mapping direction_sequence -> list of candidate indices
    """
    index: dict = {}
    for i, cand in enumerate(candidate_list):
        cand_klines = cand.get("klines", cand)
        close = np.asarray(cand_klines.get("close", []), dtype=np.float64)
        seq = _fast_direction_sequence(close)
        if seq not in index:
            index[seq] = []
        index[seq].append(i)
    return index


def _build_direction_index(candidate_list: list[dict], encoder: StructureEncoder) -> dict:
    """
    Layer 1: Build hash-based index by direction sequence for O(1) lookup.

    Args:
        candidate_list: List of candidates
        encoder: StructureEncoder instance

    Returns:
        Dict mapping direction_sequence -> list of candidate indices
    """
    index: dict = {}
    for i, cand in enumerate(candidate_list):
        cand_klines = cand.get("klines", cand)
        code = encoder.encode(cand_klines)
        seq = code.get("direction_sequence", "")
        if seq not in index:
            index[seq] = []
        index[seq].append(i)
    return index


def _pearson_filter_with_early_abandon(
    template_normalized: np.ndarray,
    candidate_indices: list,
    candidate_list: list,
    threshold: float = 0.5,
    max_candidates: int = 1000,
) -> list:
    """
    Layer 2: Fast Pearson correlation filter with early abandonment.

    Uses early abandonment: if running correlation drops below threshold,
    stop computing and reject the candidate.

    Args:
        template_normalized: Normalized template close prices
        candidate_indices: List of candidate indices to check
        candidate_list: Full candidate list for looking up klines
        threshold: Minimum correlation threshold
        max_candidates: Maximum number to process in this layer

    Returns:
        List of (index, correlation_score) tuples for candidates passing filter
    """
    results = []
    n = len(template_normalized)
    mean_t = np.mean(template_normalized)

    processed = 0
    for idx in candidate_indices:
        if processed >= max_candidates:
            break

        cand = candidate_list[idx]
        cand_klines = cand.get("klines", {})
        c_close = np.asarray(cand_klines.get("close", []), dtype=np.float64)

        if len(c_close) != n or n == 0:
            continue

        c_normalized = (c_close - c_close[0]) / c_close[0] if c_close[0] != 0 else c_close
        mean_c = np.mean(c_normalized)

        numerator = 0.0
        sum_t_sq = 0.0
        sum_c_sq = 0.0
        valid = True
        running_corr = 1.0

        for j in range(n):
            dt = template_normalized[j] - mean_t
            dc = c_normalized[j] - mean_c
            numerator += dt * dc
            sum_t_sq += dt * dt
            sum_c_sq += dc * dc

            if j > 2:
                denom = np.sqrt(sum_t_sq * sum_c_sq)
                if denom > 0:
                    running_corr = numerator / denom
                    if running_corr < threshold:
                        valid = False
                        break

        if valid and sum_t_sq > 0 and sum_c_sq > 0:
            corr = numerator / np.sqrt(sum_t_sq * sum_c_sq)
            if corr >= threshold:
                results.append((idx, float(corr)))

        processed += 1

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:100]
