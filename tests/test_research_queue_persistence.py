"""Perilla 搜索任务写入统一控制面的回归测试。"""
from core.research.control_plane.repository import ResearchRepository
from core.research.perilla.discovery.knowledge_loop_engine import PerillaKnowledgeLoopEngine


def test_only_executable_search_tasks_are_persisted_with_final_status(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "research.db"
    monkeypatch.setenv("RESEARCH_CONTROL_DB_PATH", str(db_path))
    engine = PerillaKnowledgeLoopEngine.__new__(PerillaKnowledgeLoopEngine)
    tasks = [
        {"task_id": "t1", "query": "公告", "priority_score": 0.9},
        {"task_id": "t2", "query": "研报", "priority_score": 0.8},
        {"task_id": "t3", "query": "不在本轮预算内", "priority_score": 0.7},
    ]
    runs = [
        {"task_id": "t1", "status": "completed", "event_count": 2},
        {"task_id": "t2", "status": "error", "event_count": 0,
         "errors": ["provider timeout"]},
    ]

    engine._persist_research_control_tasks(tasks, task_runs=runs, limit=2)

    repository = ResearchRepository(db_path)
    assert repository.get_task("t1").status == "succeeded"
    assert repository.get_task("t1").progress["documents_saved"] == 2
    assert repository.get_task("t2").status == "failed"
    assert repository.get_task("t2").failure["reason"] == "provider timeout"
    assert repository.get_task("t3") is None
