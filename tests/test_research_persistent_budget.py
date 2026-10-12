"""SQLite v4 budget reservation regressions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.research.control_plane.budget_manager import GlobalBudgetManager
from core.research.control_plane.contracts import TaskBudget
from core.research.control_plane.repository import ResearchRepository


def _budget(value: int) -> TaskBudget:
    return TaskBudget(
        max_requests=value,
        max_seconds=float(value),
        max_tokens=value,
        max_cost=float(value),
        max_documents=value,
    )


def test_schema_v4_reservation_is_idempotent_across_instances(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    manager = GlobalBudgetManager(
        repository=repository,
        daily_total=_budget(10),
        min_per_task=_budget(1),
        budget_date="2026-07-14",
    )

    first = manager.reserve(
        _budget(4), task_id="task-1", attempt_id="attempt-1", action_id="query-1"
    )
    replay = GlobalBudgetManager(
        repository=ResearchRepository(tmp_path / "research.db"),
        daily_total=_budget(10),
        min_per_task=_budget(1),
        budget_date="2026-07-14",
    ).reserve(
        _budget(4), task_id="task-1", attempt_id="attempt-1", action_id="query-1"
    )

    assert first.granted
    assert replay.already_reserved
    assert replay.reservation_id == first.reservation_id
    assert manager.snapshot().reserved.max_requests == 4


def test_release_returns_unused_capacity_after_restart(tmp_path: Path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    manager = GlobalBudgetManager(
        repository=repository,
        daily_total=_budget(10),
        min_per_task=_budget(1),
        budget_date="2026-07-14",
    )
    reservation = manager.reserve(
        _budget(8), task_id="task-1", attempt_id="attempt-1", action_id="query-1"
    )
    manager.consume(reservation.reservation_id, _budget(3))
    manager.release(reservation.reservation_id)

    restarted = GlobalBudgetManager(
        repository=ResearchRepository(tmp_path / "research.db"),
        daily_total=_budget(10),
        min_per_task=_budget(1),
        budget_date="2026-07-14",
    )
    snapshot = restarted.snapshot()
    assert snapshot.consumed.max_requests == 3
    assert snapshot.reserved.max_requests == 0
    assert snapshot.released.max_requests == 5
    assert snapshot.remaining.max_requests == 7


def test_multi_instance_reservations_are_atomic(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    ResearchRepository(db_path)

    def reserve(index: int) -> bool:
        manager = GlobalBudgetManager(
            repository=ResearchRepository(db_path),
            daily_total=_budget(10),
            min_per_task=_budget(1),
            budget_date="2026-07-14",
        )
        return manager.reserve(
            _budget(1),
            task_id=f"task-{index}",
            attempt_id="attempt-1",
            action_id="query-1",
        ).granted

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(reserve, range(12)))

    assert sum(results) == 10
    snapshot = GlobalBudgetManager(
        repository=ResearchRepository(db_path), budget_date="2026-07-14"
    ).snapshot()
    assert snapshot.reserved.max_requests == 10
    assert snapshot.remaining.max_requests == 0


def test_budget_api_degrades_for_malformed_inputs(tmp_path: Path) -> None:
    manager = GlobalBudgetManager(
        db_path=tmp_path / "budget.db",
        daily_total=_budget(5),
        min_per_task=_budget(1),
        budget_date="2026-07-14",
    )
    reservation = manager.reserve(None, task_id=None, attempt_id=None, action_id=None)
    assert reservation.granted is False
    assert manager.list_reservations(limit="bad") == []
    manager.release(reservation_id=None, unused={"max_requests": "bad"})
    assert manager.idle_budget_cap(float("nan")).max_requests == 1
