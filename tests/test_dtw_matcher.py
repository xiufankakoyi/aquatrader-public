import numpy as np

from core.quant.similarity.dtw_matcher import _dtw_distance, dtw_match


def _reference_dtw(s1: np.ndarray, s2: np.ndarray) -> float:
    matrix = np.full((len(s1) + 1, len(s2) + 1), np.inf)
    matrix[0, 0] = 0.0
    for i in range(1, len(s1) + 1):
        for j in range(1, len(s2) + 1):
            matrix[i, j] = (s1[i - 1] - s2[j - 1]) ** 2 + min(
                matrix[i - 1, j], matrix[i, j - 1], matrix[i - 1, j - 1]
            )
    return float(np.sqrt(matrix[-1, -1]))


def test_dtw_distance_keeps_existing_distance_definition() -> None:
    left = np.array([0.0, 1.0, 0.5, 2.0])
    right = np.array([0.0, 0.8, 1.2, 2.0])
    assert _dtw_distance(left, right) == _reference_dtw(left, right)


def test_dtw_match_skips_length_mismatch_and_returns_top_n() -> None:
    template = np.array([0.0, 1.0, 0.0])
    candidates = [
        {"normalized": np.array([0.0, 1.0, 0.0]), "metadata": {"id": "same"}},
        {"normalized": np.array([1.0, 1.0]), "metadata": {"id": "wrong-length"}},
        {"normalized": np.array([1.0, 0.0, 1.0]), "metadata": {"id": "other"}},
    ]

    result = dtw_match(template, candidates, top_n=1)

    assert len(result) == 1
    assert result[0]["metadata"]["id"] == "same"
    assert result[0]["similarity_score"] == 1.0
