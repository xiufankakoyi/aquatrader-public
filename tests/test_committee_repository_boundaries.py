"""CommitteeRepository 持久化边界回归测试。"""
from __future__ import annotations

from core.research.committee.contracts import CommitteeWorkflow
from core.research.committee.repository import CommitteeRepository


def test_save_degrades_mutated_nullable_text_fields(tmp_path):
    repo = CommitteeRepository(tmp_path / "committee.db")
    workflow = CommitteeWorkflow(workflow_id="wf_1")
    workflow.thesis_id = None
    workflow.company_symbol = None
    workflow.as_of = None
    workflow.status = None
    workflow.lead_analyst_user_id = None

    repo.save(workflow)

    stored = repo.get(" wf_1 ")
    assert stored is not None
    assert stored["thesis_id"] == ""


def test_repository_queries_normalize_text_filters(tmp_path):
    repo = CommitteeRepository(tmp_path / "committee.db")
    repo.save(CommitteeWorkflow(
        workflow_id="wf_1",
        thesis_id="thesis_1",
        company_symbol="300308.SZ",
        status="draft",
    ))

    assert len(repo.list_by_thesis(thesis_id=" thesis_1 ")) == 1
    assert len(repo.list_by_company(company_symbol=" 300308.SZ ", status=" draft ")) == 1
    assert len(repo.list_by_status(status=" draft ")) == 1
