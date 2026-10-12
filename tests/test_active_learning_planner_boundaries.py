"""ActiveLearningPlanner 输入和持久化边界回归测试。"""

from __future__ import annotations

from collections import UserDict

from core.research.alpha.active_learning_planner import (
    ActiveLearningPlanner,
    LearningPlanItem,
)


def test_invalid_gap_identity_is_dropped(tmp_path) -> None:
    planner = ActiveLearningPlanner(tmp_path)

    plans = planner.plan_from_gaps([{"gap_id": None, "missing_entity": None}])

    assert plans == []


def test_mapping_alpha_path_results_contribute_path_confidence(tmp_path) -> None:
    planner = ActiveLearningPlanner(tmp_path)

    plans = planner.plan_from_alpha_result(
        {
            "root": "theme",
            "alpha_path_results": [
                UserDict({"path": ["node"], "path_confidence": 0.9}),
            ],
            "evidence_gaps": [
                {"gap_id": "gap-1", "path": ["node"]},
            ],
        }
    )

    assert len(plans) == 1
    assert plans[0].current_confidence == 0.9


def test_mark_scheduled_handles_missing_collection(tmp_path) -> None:
    planner = ActiveLearningPlanner(tmp_path)

    assert planner.mark_scheduled(None) == []  # type: ignore[arg-type]


def test_load_skips_empty_gap_id(tmp_path) -> None:
    planner = ActiveLearningPlanner(tmp_path)
    planner.save_plans(
        [LearningPlanItem(plan_id="p", gap_id="", notes="invalid")],
        "plans.json",
    )

    assert planner.load_plans("plans.json") == []
