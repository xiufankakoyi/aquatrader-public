"""GlobalBudgetManager 边界回归测试。"""
from __future__ import annotations

from pathlib import Path

from core.research.control_plane.budget_manager import GlobalBudgetManager
from core.research.control_plane.contracts import TaskBudget


def _manager(tmp_path: Path) -> GlobalBudgetManager:
    return GlobalBudgetManager(
        db_path=tmp_path / "budget.db",
        daily_total=TaskBudget(max_requests=10, max_seconds=10, max_tokens=10, max_cost=10, max_documents=10),
        min_per_task=TaskBudget(max_requests=1, max_seconds=1, max_tokens=1, max_cost=1, max_documents=1),
        budget_date="2026-07-14",
    )


def test_idle_budget_check_rejects_invalid_request_without_crashing(tmp_path):
    manager = _manager(tmp_path)

    assert manager.is_idle_budget_within_cap(None) is False


def test_invalid_task_filter_and_budget_date_degrade_to_safe_defaults(tmp_path):
    manager = _manager(tmp_path)

    snapshot = manager.snapshot(budget_date=123)
    assert snapshot.date == "2026-07-14"
    assert manager.list_reservations(task_id=["bad"]) == []
