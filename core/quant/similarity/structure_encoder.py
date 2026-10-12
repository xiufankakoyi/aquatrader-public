"""
Skeleton Structure Encoder Module

Encodes K-line sequences into structure descriptors and provides coarse filtering
capabilities for similarity matching.
"""

import numpy as np

from .zigzag import identify_extrema, build_segments
from .rhythm import extract_rhythm_vector, RhythmVector as FullRhythmVector


def encode_structure(
    klines: dict,
    segments: list,
    ma_windows: list = None,
) -> dict:
    """
    Encode K-line sequence and trend segments into a structure descriptor.

    Args:
        klines: Dictionary containing open/high/low/close/volume as np.ndarray
        segments: List of trend segments from zigzag.build_segments()
        ma_windows: List of moving average windows (default [20])

    Returns:
        Dictionary containing:
            - direction_sequence: String of 'U'/'D'/'F' characters
            - segment_count: Number of segments
            - segment_durations: List of durations for each segment
            - skeleton_points: List of (index, price) tuples for turning points
            - ma_position: Dict mapping window to position relationship
            - ma_bias_ratio_range: [min, max] of bias ratios
            - rhythm_vectors: List of RhythmVector objects
    """
    if ma_windows is None:
        ma_windows = [20]

    close = np.asarray(klines["close"], dtype=np.float64)
    volume = np.asarray(klines["volume"], dtype=np.float64)
    n = len(close)

    direction_map = {1: "U", -1: "D", 0: "F"}
    direction_sequence = "".join(
        direction_map.get(seg.get("direction", 0), "F") for seg in segments
    )

    segment_durations = [seg.get("duration", 0) for seg in segments]

    skeleton_points = []
    for seg in segments:
        start_idx = seg.get("start_idx", 0)
        end_idx = seg.get("end_idx", start_idx)
        start_price = seg.get("start_price", close[start_idx] if n > 0 else 0.0)
        end_price = seg.get("end_price", close[end_idx] if n > 0 else 0.0)
        skeleton_points.append((start_idx, float(start_price)))
        skeleton_points.append((end_idx, float(end_price)))

    ma_position = {}
    for window in ma_windows:
        if n >= window:
            ma = _compute_ma(close, window)
            bias_ratios = (close[-len(ma):] - ma) / ma
            avg_bias = float(np.mean(bias_ratios))
            if avg_bias > 0.02:
                ma_position[window] = "above"
            elif avg_bias < -0.02:
                ma_position[window] = "below"
            else:
                ma_position[window] = "aligned"
        else:
            ma_position[window] = "aligned"

    ma_bias_ratio_range = [-0.05, 0.03]
    if n > 0 and ma_windows:
        all_bias_ratios = []
        for window in ma_windows:
            if n >= window:
                ma = _compute_ma(close, window)
                bias_ratios = ((close[-len(ma):] - ma) / ma).tolist()
                all_bias_ratios.extend(bias_ratios)
        if all_bias_ratios:
            ma_bias_ratio_range = [float(np.min(all_bias_ratios)), float(np.max(all_bias_ratios))]

    rhythm_vectors = []
    segment_pct_changes = []
    open_arr = np.asarray(klines.get("open", close), dtype=np.float64)
    high_arr = np.asarray(klines.get("high", close), dtype=np.float64)
    low_arr = np.asarray(klines.get("low", close), dtype=np.float64)

    for seg in segments:
        start_idx = seg.get("start_idx", 0)
        end_idx = seg.get("end_idx", start_idx)

        segment_pct_changes.append(seg.get("pct_change", 0.0))

        if end_idx < n and start_idx < n:
            seg_klines = {
                "open": open_arr[start_idx : end_idx + 1],
                "high": high_arr[start_idx : end_idx + 1],
                "low": low_arr[start_idx : end_idx + 1],
                "close": close[start_idx : end_idx + 1],
                "volume": volume[start_idx : end_idx + 1],
            }
            rhythm_vec = extract_rhythm_vector(seg_klines)
            rhythm_vectors.append(rhythm_vec)

    return {
        "direction_sequence": direction_sequence,
        "segment_count": len(segments),
        "segment_durations": segment_durations,
        "segment_pct_changes": segment_pct_changes,
        "skeleton_points": skeleton_points,
        "ma_position": ma_position,
        "ma_bias_ratio_range": ma_bias_ratio_range,
        "rhythm_vectors": rhythm_vectors,
    }


def _compute_ma(prices: np.ndarray, window: int) -> np.ndarray:
    """
    Compute simple moving average.

    Args:
        prices: Price array
        window: MA window size

    Returns:
        MA array with same length as input (padded with NaN at start)
    """
    n = len(prices)
    if n < window:
        return np.full(n, np.nan)
    ma = np.full(n, np.nan)
    for i in range(window - 1, n):
        ma[i] = np.mean(prices[i - window + 1 : i + 1])
    return ma


def compute_direction_similarity(seq1: str, seq2: str) -> float:
    """
    Compute similarity between two direction sequences.

    Scoring rules:
        - Perfect match: 1.0
        - Length difference: -0.2 per bar
        - Character mismatch: -0.3 per character

    Args:
        seq1: First direction sequence (e.g., "U-D-U")
        seq2: Second direction sequence (e.g., "U-D-F")

    Returns:
        Similarity score between 0.0 and 1.0
    """
    len1, len2 = len(seq1), len(seq2)
    max_len = max(len1, len2)

    if max_len == 0:
        return 1.0

    length_penalty = abs(len1 - len2) * 0.2

    min_len = min(len1, len2)
    match_count = sum(1 for i in range(min_len) if seq1[i] == seq2[i])
    mismatch_penalty = (min_len - match_count) * 0.3

    if len1 != len2:
        longer = seq1 if len1 > len2 else seq2
        extra_chars = longer[min_len:]
        mismatch_penalty += len(extra_chars) * 0.3

    score = 1.0 - length_penalty - mismatch_penalty
    return max(0.0, min(1.0, score))


def fast_filter(template_code: dict, candidate_codes: list[dict]) -> list[dict]:
    """
    Fast hash-based filtering of candidate structure codes.

    Filter conditions:
        1. direction_sequence must be identical
        2. segment_count difference <= 1
        3. ma_position relationships must be consistent (，允许 ±1 档容差)

    Args:
        template_code: Template structure descriptor
        candidate_codes: List of candidate structure descriptors

    Returns:
        Filtered list of candidates meeting all conditions
    """
    if not candidate_codes:
        return []

    template_seq = template_code.get("direction_sequence", "")
    template_count = template_code.get("segment_count", 0)
    template_ma = template_code.get("ma_position", {})

    filtered = []

    for candidate in candidate_codes:
        cand_seq = candidate.get("direction_sequence", "")
        if cand_seq != template_seq:
            continue

        cand_count = candidate.get("segment_count", 0)
        if abs(cand_count - template_count) > 1:
            continue

        cand_ma = candidate.get("ma_position", {})

        if not _ma_positions_compatible(template_ma, cand_ma):
            continue

        filtered.append(candidate)

    return filtered


def _ma_positions_compatible(ma1: dict, ma2: dict, tolerance: int = 1) -> bool:
    """
    Check if two MA position dictionaries are compatible within tolerance.

    Args:
        ma1: First MA position dict
        ma2: Second MA position dict
        tolerance: Allowed difference in position level (default 1)

    Returns:
        True if compatible, False otherwise
    """
    position_rank = {"below": 0, "aligned": 1, "above": 2}

    all_keys = set(ma1.keys()) | set(ma2.keys())
    for key in all_keys:
        pos1 = ma1.get(key, "aligned")
        pos2 = ma2.get(key, "aligned")
        rank1 = position_rank.get(pos1, 1)
        rank2 = position_rank.get(pos2, 1)
        if abs(rank1 - rank2) > tolerance:
            return False

    return True


class StructureEncoder:
    """
    Structure encoder for K-line sequences.

    Encodes K-line data into structure descriptors and provides coarse
    filtering capabilities for similarity matching.

    Attributes:
        min_pct: Minimum percentage threshold for segment detection
        min_bars: Minimum number of bars for a valid segment
        ma_windows: List of moving average windows for position analysis
    """

    def __init__(
        self,
        min_pct: float = 0.03,
        min_bars: int = 2,
        ma_windows: list = None,
    ):
        """
        Initialize the structure encoder.

        Args:
            min_pct: Minimum percentage change for trend detection (default 0.03)
            min_bars: Minimum bars for a valid segment (default 2)
            ma_windows: List of MA windows (default [20])
        """
        self.min_pct = min_pct
        self.min_bars = min_bars
        self.ma_windows = ma_windows if ma_windows is not None else [20]

    def encode(self, klines: dict) -> dict:
        """
        Encode K-line sequence into a structure descriptor.

        Args:
            klines: Dictionary containing open/high/low/close/volume arrays

        Returns:
            Structure descriptor dictionary
        """
        close_arr = np.asarray(klines["close"], dtype=np.float64)
        extrema = identify_extrema(close_arr, min_pct=self.min_pct, min_bars=self.min_bars)
        segments = build_segments(close_arr, extrema)
        return encode_structure(klines, segments, self.ma_windows)

    def filter(self, template: dict, candidates: list[dict]) -> list[dict]:
        """
        Filter candidates by structure similarity to template.

        Args:
            template: Template structure descriptor
            candidates: List of candidate structure descriptors

        Returns:
            Filtered list of candidates
        """
        return fast_filter(template, candidates)


__all__ = [
    "RhythmVector",
    "StructureEncoder",
    "encode_structure",
    "compute_direction_similarity",
    "fast_filter",
]
