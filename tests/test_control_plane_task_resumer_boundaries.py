from __future__ import annotations

from core.research.control_plane.contracts import ResearchTask
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.task_resumer import ResearchTaskAttempt, TaskResumer


def test_queued_task_with_only_finished_attempt_is_not_resumable(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    task = ResearchTask(task_id="task-finished", status="queued")
    repository.save_task(task)
    resumer = TaskResumer(repository)
    resumer.save_attempt(ResearchTaskAttempt(
        attempt_id="attempt-finished",
        task_id=task.task_id,
        run_id=task.run_id,
        attempt_number=1,
        status="partial",
        finished_at="2026-07-31T00:00:00+00:00",
    ))

    assert resumer.list_resumable_tasks() == []
