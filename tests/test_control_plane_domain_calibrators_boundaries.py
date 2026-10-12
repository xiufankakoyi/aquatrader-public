"""分域校准器边界回归测试。"""
from __future__ import annotations

from core.research.control_plane.domain_calibrators import (
    KnowledgePolicyCalibrator,
    RetrievalPolicyCalibrator,
    _brier_score,
    _calibration_curve,
    _precision_recall,
)


def test_knowledge_calibrator_skips_unhashable_labels():
    calibrator = KnowledgePolicyCalibrator(None)

    params = calibrator._fit_params([
        {"metrics": {"fact_label": []}, "attribution": {}},
        {"metrics": {"fact_label": 1}, "attribution": {}},
    ])

    assert params["source_weights"]["unknown"] > 0


def test_calibrators_degrade_malformed_parameter_maps():
    knowledge = KnowledgePolicyCalibrator(None)._evaluate(
        [{"metrics": {"fact_label": 1}, "attribution": {"source_type": "unknown"}}],
        {"source_weights": {"unknown": "bad"}, "source_discount_kappa": "bad", "claim_activation_threshold": "bad"},
    )
    retrieval = RetrievalPolicyCalibrator(None)._evaluate(
        [{"metrics": {"tier": "high"}, "attribution": {}}],
        {"tier_utilities": []},
    )

    assert 0.0 <= knowledge["brier_score"] <= 1.0
    assert 0.0 <= retrieval["brier_score"] <= 1.0


def test_metric_helpers_fail_closed_for_malformed_numbers():
    assert _brier_score(["bad"], [1]) == 1.0
    assert _calibration_curve(["bad"], [1]) == []
    assert _precision_recall(["bad"], [1]) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}
