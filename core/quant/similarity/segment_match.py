"""
Segment Alignment and Fine Similarity Computation Module

Provides core algorithms for segment-level alignment and精细 similarity scoring
between K-line sequences.
"""

import numpy as np

from .structure_encoder import StructureEncoder
from .rhythm import extract_rhythm_vector, cosine_similarity as rhythm_cosine_similarity, RhythmVector


def segment_edit_distance(seq1: list[str], seq2: list[str]) -> int:
    """
    Compute edit distance between two direction sequences.

    Args:
        seq1: First direction sequence (list of 'U'/'D'/'F')
        seq2: Second direction sequence (list of 'U'/'D'/'F')

    Returns:
        Edit distance (number of operations needed)
    """
    m, n = len(seq1), len(seq2)
    dp = np.zeros((m + 1, n + 1), dtype=np.int32)

    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if seq1[i - 1] == seq2[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    return int(dp[m][n])


def align_segments(template_segments: list, candidate_segments: list, edit_distance: int) -> list[tuple]:
    """
    Align template and candidate segments by pairing them.

    Args:
        template_segments: List of template segments
        candidate_segments: List of candidate segments
        edit_distance: Pre-computed edit distance between sequences

    Returns:
        List of (template_segment, candidate_segment) tuples.
        None indicates a virtual segment inserted for alignment.
    """
    t_len = len(template_segments)
    c_len = len(candidate_segments)

    if t_len == c_len:
        return list(zip(template_segments, candidate_segments))

    if abs(t_len - c_len) == 1:
        longer_segs = template_segments if t_len > c_len else candidate_segments
        shorter_segs = candidate_segments if t_len > c_len else template_segments
        insert_idx = _find_best_insertion(longer_segs, shorter_segs)

        aligned = []
        for i, seg in enumerate(longer_segs):
            if i == insert_idx:
                aligned.append((seg, None) if t_len > c_len else (None, seg))
            if i < len(shorter_segs):
                aligned.append((seg, shorter_segs[i]) if t_len > c_len else (shorter_segs[i], seg))
        return aligned

    base_segs = template_segments if t_len < c_len else candidate_segments
    compare_segs = candidate_segments if t_len < c_len else template_segments
    return list(zip(base_segs, compare_segs))


def _find_best_insertion(longer_segs: list, shorter_segs: list) -> int:
    """
    Find the optimal insertion index for a virtual segment.

    Args:
        longer_segs: The longer segment list
        shorter_segs: The shorter segment list

    Returns:
        Best insertion index
    """
    best_idx = 0
    min_error = float('inf')

    for insert_idx in range(len(longer_segs)):
        error = 0
        long_idx = 0
        for short_idx in range(len(shorter_segs)):
            if long_idx == insert_idx:
                long_idx += 1
            if long_idx < len(longer_segs):
                error += abs(_segment_duration(longer_segs[long_idx]) -
                            _segment_duration(shorter_segs[short_idx]))
                long_idx += 1

        if error < min_error:
            min_error = error
            best_idx = insert_idx

    return best_idx


def _segment_duration(seg: dict) -> int:
    """Get duration from a segment dictionary."""
    if isinstance(seg, dict):
        return seg.get('duration', 1)
    if hasattr(seg, 'duration'):
        return seg.duration
    return 1


def _segment_pct(seg: dict) -> float:
    """Get price change percentage from a segment dictionary."""
    if isinstance(seg, dict):
        return seg.get("pct_change", seg.get("amplitude", 0.0))
    if hasattr(seg, "pct_change"):
        return seg.pct_change
    if hasattr(seg, "amplitude"):
        return seg.amplitude
    return 0.0


def compute_structure_similarity(
    template_segments: list,
    aligned_pairs: list[tuple],
    direction_sequence: tuple = None,
) -> float:
    """
    Compute structure similarity between aligned segment pairs.

    Args:
        template_segments: List of template segments
        aligned_pairs: List of (template_segment, candidate_segment) tuples
        direction_sequence: Tuple of direction chars from template segments

    Returns:
        Structure similarity score (0~1)
    """
    if not aligned_pairs:
        return 0.0

    t_total = sum(_segment_duration(seg) for seg in template_segments) if template_segments else 1
    max_diff = 2.0
    alpha1 = 0.2

    scores = []
    valid_count = 0

    for idx, (t_seg, c_seg) in enumerate(aligned_pairs):
        if t_seg is None or c_seg is None:
            scores.append(0.0)
            continue

        t_dur = _segment_duration(t_seg)
        c_dur = _segment_duration(c_seg)
        t_pct = _segment_pct(t_seg)
        c_pct = _segment_pct(c_seg)

        pct_diff = abs(t_pct - c_pct) / max_diff
        if direction_sequence and idx < len(direction_sequence):
            if direction_sequence[idx] == "U" and c_pct < 0:
                pct_diff = 2.0
            elif direction_sequence[idx] == "D" and c_pct > 0:
                pct_diff = 2.0
        pct_score = max(0.0, 1.0 - pct_diff / 2.0)

        dur_ratio_t = t_dur / t_total
        dur_ratio_c = c_dur / t_total
        dur_diff = abs(dur_ratio_t - dur_ratio_c)
        dur_score = max(0.0, 1.0 - dur_diff)

        seg_score = alpha1 * pct_score + (1 - alpha1) * dur_score
        scores.append(seg_score)
        valid_count += 1

    if valid_count == 0:
        return 0.0

    return float(np.mean(scores))


def compute_rhythm_similarity(template_rhythms: list, candidate_rhythms: list, aligned_pairs: list[tuple]) -> float:
    """
    Compute rhythm similarity between aligned segment pairs.

    Args:
        template_rhythms: List of template RhythmVectors
        candidate_rhythms: List of candidate RhythmVectors
        aligned_pairs: List of (template_segment, candidate_segment) tuples

    Returns:
        Rhythm similarity score (0~1)
    """
    if not aligned_pairs:
        return 0.0

    similarities = []
    valid_count = 0

    t_idx = 0
    for t_seg, c_seg in aligned_pairs:
        if t_seg is not None and c_seg is not None:
            if t_idx < len(template_rhythms):
                t_rhythm = template_rhythms[t_idx]
                c_rhythm = candidate_rhythms[t_idx] if t_idx < len(candidate_rhythms) else None

                if c_rhythm is not None:
                    t_arr = _rhythm_to_array(t_rhythm)
                    c_arr = _rhythm_to_array(c_rhythm)
                    sim = rhythm_cosine_similarity(t_arr, c_arr)
                    similarities.append(sim)
                    valid_count += 1
        t_idx += 1

    if not similarities:
        return 0.0

    base_score = float(np.mean(similarities))
    valid_ratio = valid_count / len(aligned_pairs)

    return base_score * valid_ratio


def _rhythm_to_array(rhythm) -> np.ndarray:
    """
    Convert a RhythmVector to numpy array.

    Args:
        rhythm: RhythmVector object

    Returns:
        Numpy array representation
    """
    if isinstance(rhythm, RhythmVector):
        return np.array([
            rhythm.bullish_density,
            rhythm.body_fullness,
            rhythm.wave_uniformity,
            rhythm.volume_uniformity,
            rhythm.rebound_frequency,
            rhythm.max_bullish_streak_ratio,
            rhythm.avg_drawdown,
            rhythm.volume_trend,
        ], dtype=np.float64)

    return np.array([0.0])


def compute_ma_fit_similarity(template_klines: dict, candidate_klines: dict, ma_window: int = 20) -> float | None:
    """
    Compute moving average fit similarity between template and candidate.

    Args:
        template_klines: Template K-line data
        candidate_klines: Candidate K-line data
        ma_window: Moving average window (default 20)

    Returns:
        MA fit similarity score (0~1), or None if insufficient data
    """
    t_close = np.asarray(template_klines.get("close", []), dtype=np.float64)
    c_close = np.asarray(candidate_klines.get("close", []), dtype=np.float64)

    if len(t_close) < ma_window or len(c_close) < ma_window:
        return None

    t_ma = _compute_ma(t_close, ma_window)
    c_ma = _compute_ma(c_close, ma_window)

    valid_mask_t = ~np.isnan(t_ma)
    valid_mask_c = ~np.isnan(c_ma)

    t_ma_valid = t_ma[valid_mask_t]
    c_ma_valid = c_ma[valid_mask_c]
    t_close_valid = t_close[valid_mask_t]
    c_close_valid = c_close[valid_mask_c]

    min_len = min(len(t_ma_valid), len(c_ma_valid))
    # 允许至少 1 个有效数据点（当窗口大小等于 MA 窗口时）
    if min_len < 1:
        return None

    t_ma_valid = t_ma_valid[-min_len:]
    c_ma_valid = c_ma_valid[-min_len:]
    t_close_v = t_close_valid[-min_len:]
    c_close_v = c_close_valid[-min_len:]

    t_deviation = (t_close_v - t_ma_valid) / np.where(t_ma_valid != 0, t_ma_valid, 1.0)
    c_deviation = (c_close_v - c_ma_valid) / np.where(c_ma_valid != 0, c_ma_valid, 1.0)

    t_mean = np.mean(t_deviation)
    c_mean = np.mean(c_deviation)
    t_std = np.std(t_deviation)
    c_std = np.std(c_deviation)

    # 当只有一个数据点时，标准差为 0，此时直接比较偏差
    if min_len == 1:
        deviation_diff = abs(t_deviation[0] - c_deviation[0])
        return float(1.0 / (1.0 + deviation_diff))

    if t_std < 1e-10 or c_std < 1e-10:
        return None

    corr = np.mean((t_deviation - t_mean) * (c_deviation - c_mean)) / (t_std * c_std)
    corr = float(np.clip(corr, -1.0, 1.0))

    return (corr + 1.0) / 2.0


def _compute_ma(prices: np.ndarray, window: int) -> np.ndarray:
    """
    Compute simple moving average.

    Args:
        prices: Price array
        window: MA window size

    Returns:
        MA array
    """
    n = len(prices)
    if n < window:
        return np.full(n, np.nan)

    ma = np.full(n, np.nan)
    for i in range(window - 1, n):
        ma[i] = np.mean(prices[i - window + 1:i + 1])
    return ma


def compute_weighted_score(
    structure_sim: float,
    rhythm_sim: float,
    ma_fit_sim: float | None,
    weights: dict = None,
) -> float:
    """
    Compute weighted comprehensive similarity score.

    Args:
        structure_sim: Structure similarity score (0~1)
        rhythm_sim: Rhythm similarity score (0~1)
        ma_fit_sim: MA fit similarity score (0~1), or None
        weights: Weight dictionary with keys 'structure', 'rhythm', 'ma_fit'

    Returns:
        Weighted similarity score (0~1)
    """
    if weights is None:
        weights = {"structure": 0.20, "rhythm": 0.60, "ma_fit": 0.20}

    if ma_fit_sim is None:
        total_weight = weights["structure"] + weights["rhythm"]
        if total_weight == 0:
            return 0.0
        w_structure = weights["structure"] / total_weight
        w_rhythm = weights["rhythm"] / total_weight
        return w_structure * structure_sim + w_rhythm * rhythm_sim

    return (weights["structure"] * structure_sim +
            weights["rhythm"] * rhythm_sim +
            weights["ma_fit"] * ma_fit_sim)


class SegmentMatcher:
    """
    Segment Alignment Fine Matcher

    Combines structure encoding, coarse filtering, and fine segment matching
    to compute comprehensive similarity scores.

    Attributes:
        encoder: StructureEncoder instance
        ma_window: Moving average window for MA fit calculation
        weights: Weight dictionary for score components
    """

    def __init__(
        self,
        min_pct: float = 0.03,
        min_bars: int = 2,
        ma_window: int = 20,
        weights: dict = None,
    ):
        """
        Initialize the segment matcher.

        Args:
            min_pct: Minimum percentage threshold for segment detection
            min_bars: Minimum number of bars for a valid segment
            ma_window: Moving average window for MA fit calculation
            weights: Weight dictionary for score components
        """
        self.encoder = StructureEncoder(min_pct, min_bars, [ma_window])
        self.ma_window = ma_window
        self.weights = weights or {"structure": 0.20, "rhythm": 0.60, "ma_fit": 0.20}

    def match(self, template_klines: dict, candidate_klines: dict) -> dict:
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

        t_rhythms = []
        for seg in t_encoded.get("rhythm_vectors", []):
            t_rhythms.append(seg)

        c_rhythms = []
        for seg in c_encoded.get("rhythm_vectors", []):
            c_rhythms.append(seg)

        structure_score = compute_structure_similarity(
            t_segments, aligned, t_seq
        )
        rhythm_score = compute_rhythm_similarity(t_rhythms, c_rhythms, aligned)
        ma_fit_score = compute_ma_fit_similarity(template_klines, candidate_klines, self.ma_window)

        total_score = compute_weighted_score(
            structure_score, rhythm_score, ma_fit_score, self.weights
        )

        return {
            "structure_score": structure_score,
            "rhythm_score": rhythm_score,
            "ma_fit_score": ma_fit_score,
            "total_score": total_score,
        }

    def batch_match(self, template_klines: dict, candidate_list: list[dict]) -> list[dict]:
        """
        Batch match template against multiple candidates.

        Args:
            template_klines: Template K-line data
            candidate_list: List of candidates, each with 'klines' and optional 'metadata'

        Returns:
            List of match results sorted by total_score descending
        """
        results = []
        for candidate in candidate_list:
            cand_klines = candidate.get("klines", candidate)
            metadata = candidate.get("metadata", {})

            match_result = self.match(template_klines, cand_klines)
            match_result["metadata"] = metadata

            results.append(match_result)

        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results


__all__ = [
    "segment_edit_distance",
    "align_segments",
    "compute_structure_similarity",
    "compute_rhythm_similarity",
    "compute_ma_fit_similarity",
    "compute_weighted_score",
    "SegmentMatcher",
]
