"""CalibrationController 边界回归测试。"""
from __future__ import annotations

import pytest

from core.research.control_plane.calibration import CalibrationController
from core.research.control_plane.contracts import CalibrationRun
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.test_run_record import TestRunStore


def _setup(tmp_path, metrics):
    repo = ResearchRepository(tmp_path / "research.db")
    controller = CalibrationController(repo)
    run = controller.create_shadow(CalibrationRun(
        domain="knowledge",
        sample_size=100,
        metrics=metrics,
    ))
    test_run = TestRunStore(repo).record_run(
        code_version="v1",
        test_suite="tests/test_feedback_loop_integration.py",
        passed=True,
    )
    return controller, run, test_run


def test_activate_rejects_malformed_or_missing_metrics(tmp_path):
    controller, run, test_run = _setup(
        tmp_path,
        {"walk_forward_score": "bad", "false_promotion_rate": "bad"},
    )

    with pytest.raises(ValueError, match="walk-forward"):
        controller.activate(
            run.calibration_id,
            "approver",
            test_run_id=test_run.test_run_id,
            code_version="v1",
        )


def test_rollback_rejects_whitespace_approver(tmp_path):
    controller, run, _ = _setup(
        tmp_path,
        {"walk_forward_score": 0.7, "false_promotion_rate": 0.05},
    )

    with pytest.raises(ValueError, match="审批人"):
        controller.rollback(run.calibration_id, "   ")
