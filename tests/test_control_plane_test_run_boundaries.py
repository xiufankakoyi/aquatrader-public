from __future__ import annotations

from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.test_run_record import TestRunStore


def test_test_run_verification_requires_a_nonempty_code_version(tmp_path) -> None:
    store = TestRunStore(ResearchRepository(tmp_path / "research.db"))
    record = store.record_run(
        code_version="   ",
        test_suite="tests/smoke.py",
        passed=True,
        finished_at="   ",
    )

    assert record.finished_at
    assert store.verify(record.test_run_id, "") is False
