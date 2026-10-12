from __future__ import annotations

from unittest.mock import MagicMock

from flask import Flask

from core.research.control_plane.contracts import Claim, ResearchTask, TaskBudget
from core.research.control_plane.first_party_retriever import (
    CompanyIRSiteProvider,
    FirstPartyRetrievalResult,
    FirstPartyRetriever,
)
from core.research.control_plane.local_retrieval import LocalRetrievalResult
from core.research.control_plane.orchestrator import ResearchOrchestrator, SearchTier
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.task_resumer import ResearchTaskAttempt, TaskResumer
from core.research.perilla.discovery.models import SourceDocument


def _client(tmp_path, monkeypatch):
    import server.routes.research_routes as routes

    repo = ResearchRepository(tmp_path / "research.db")
    monkeypatch.setattr(routes, "_repository", repo)
    app = Flask(__name__)
    app.register_blueprint(routes.research_bp)
    return repo, app.test_client()


def test_attempt_audit_endpoint_returns_durable_attempts(tmp_path, monkeypatch):
    repo, client = _client(tmp_path, monkeypatch)
    task = ResearchTask(task_id="task-audit")
    repo.save_task(task)
    TaskResumer(repo).save_attempt(
        ResearchTaskAttempt(
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id="claim-audit",
            attempt_number=1,
            status="partial",
            current_stage="first_party",
            completed_query_keys=["first_party|support|query"],
        )
    )

    response = client.get("/api/research/tasks/task-audit/attempts")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload[0]["current_stage"] == "first_party"
    assert payload[0]["completed_query_keys"] == ["first_party|support|query"]


def test_budget_audit_endpoint_exposes_date_and_reservations(tmp_path, monkeypatch):
    repo, client = _client(tmp_path, monkeypatch)
    from core.research.control_plane.budget_manager import GlobalBudgetManager

    manager = GlobalBudgetManager(
        repository=repo,
        daily_total=TaskBudget(max_requests=5, max_documents=5),
        min_per_task=TaskBudget(max_requests=1, max_documents=1),
        budget_date="2026-07-14",
    )
    manager.reserve(
        TaskBudget(max_requests=2, max_documents=2),
        task_id="task-budget",
        attempt_id="attempt-budget",
        action_id="query-1",
    )

    response = client.get("/api/research/budget?budget_date=2026-07-14")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["budget_date"] == "2026-07-14"
    assert payload["reserved"]["max_requests"] == 2
    assert payload["reservations"][0]["task_id"] == "task-budget"


def test_acceptance_report_exposes_phase3_audit_fields(tmp_path):
    repo = ResearchRepository(tmp_path / "research.db")
    report = repo.acceptance_report("2026-07-14T08:00:00+08:00")

    assert report["attempt_count"] == 0
    assert report["first_party_evidence_rate"] == 0.0
    assert "budget_reserved" in report
    assert report["snippet_documents_intercepted"] == 0
    assert report["market_documents_intercepted"] == 0
    assert report["feedback_calibration_status"] == {
        "feedback": "not_started",
        "calibration": "not_started",
    }


def test_finalize_attempt_persists_checkpoint_on_task(tmp_path):
    repo = ResearchRepository(tmp_path / "research.db")
    task = ResearchTask(task_id="task-finalize", attempt_id="att-finalize", attempt_number=1)
    repo.save_task(task)

    TaskResumer(repo).finalize_attempt(
        task,
        status="partial",
        completed_stages=["local", "first_party"],
        seen_document_ids=["doc-1"],
        saved_evidence_ids=["ev-1"],
        consumed_budget={"requests": 2, "documents": 1},
        failure_type="budget_exhausted",
        next_action="retry_later",
    )

    persisted = ResearchRepository(tmp_path / "research.db").get_task(task.task_id)
    assert persisted is not None
    assert persisted.checkpoint["completed_stages"] == ["local", "first_party"]
    assert persisted.checkpoint["failure_type"] == "budget_exhausted"
    assert persisted.attempt_finished_at


def test_first_party_timeout_is_a_provider_specific_degradation():
    class TimeoutProvider:
        name = "timeout_provider"

        def health(self):
            return {"available": True, "timeout_seconds": 0.1}

        def search(self, query, *, limit=5, context=None):
            raise TimeoutError("provider timed out")

    class HealthyProvider:
        name = "healthy_provider"

        def health(self):
            return {"available": True, "timeout_seconds": 0.1}

        def search(self, query, *, limit=5, context=None):
            return [SourceDocument(
                document_id="healthy-doc",
                source_type="announcement",
                url="https://example.com/healthy",
                content_text="完整正文",
            )]

    result = FirstPartyRetriever(
        interactive_providers=[TimeoutProvider(), HealthyProvider()]
    ).search("600000 测试主题")

    assert [document.document_id for document in result.documents] == ["healthy-doc"]
    timeout_status = result.source_status["timeout_provider"]
    assert timeout_status.error_code == "provider_timeout"
    assert "timeout_provider:provider_timeout" in result.degradation_codes


def test_company_ir_normalizes_crawler_documents_and_forces_official_type():
    crawler = MagicMock()
    crawler.crawl_url.return_value = SourceDocument(
        source_type="web_page",
        content_text="公司投资者关系页面正文",
    )

    document = CompanyIRSiteProvider(crawler=crawler).search(
        "600000 测试公司",
        context={"official_ir_urls": ["https://example.com/ir"]},
    )[0]

    assert document.source_type == "official_site"
    assert document.url == "https://example.com/ir"
    assert document.document_id
    assert document.fetched_at
    assert document.text_hash
    assert document.metadata["source_lineage_id"]
    assert document.metadata["provider"] == "company_ir_site"


def test_checkpoint_budget_by_date_accumulates_across_attempts(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim-budget-checkpoint",
        subject_id="600000",
        subject_name="测试公司",
        relation="member_of_theme",
        object_name="测试主题",
        support_terms=["600000 测试主题"],
    )
    repository.save_claim(claim)
    task = ResearchTask(
        task_id="task-budget-checkpoint",
        target_claim_id=claim.claim_id,
        budget=TaskBudget(max_requests=2, max_documents=4).to_dict(),
        params={"as_of": "2026-07-14T08:00:00+08:00"},
        checkpoint={
            "budget_by_date": {
                "2026-07-14": {"requests": 3, "documents": 2}
            }
        },
    )
    repository.save_task(task)

    from core.research.control_plane.budget_manager import GlobalBudgetManager

    manager = GlobalBudgetManager(
        repository=repository,
        daily_total=TaskBudget(max_requests=20, max_documents=20),
        min_per_task=TaskBudget(max_requests=1, max_documents=1),
        budget_date="2026-07-14",
    )
    local = MagicMock()
    local.query.return_value = LocalRetrievalResult(reason_code="local_empty")
    first_party = MagicMock()
    first_party.search.return_value = FirstPartyRetrievalResult()
    legacy = MagicMock()
    legacy.search.return_value = []

    summary = ResearchOrchestrator(
        repository,
        legacy,
        MagicMock(),
        tiers=(SearchTier("high", 1, max_queries=1, query_suffixes=("官方",)),),
        local_retriever=local,
        first_party_retriever=first_party,
        global_budget_manager=manager,
    ).execute(task)

    persisted = repository.get_task(task.task_id)
    assert persisted is not None
    day_budget = persisted.checkpoint["budget_by_date"]["2026-07-14"]
    assert day_budget["requests"] == 3 + summary.requests_used
    assert day_budget["documents"] == 2 + summary.documents_seen


def test_local_short_circuit_reports_succeeded_summary(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim-local-summary",
        subject_id="600000",
        subject_name="测试公司",
        relation="member_of_theme",
        object_name="测试主题",
    )
    repository.save_claim(claim)
    task = ResearchTask(target_claim_id=claim.claim_id)
    local = MagicMock()
    local.query.return_value = LocalRetrievalResult(
        hit=True,
        reason_code="local_sufficient",
        independent_sources=2,
        first_party_sources=1,
        support_count=2,
    )
    first_party = MagicMock()
    legacy = MagicMock()

    summary = ResearchOrchestrator(
        repository,
        legacy,
        MagicMock(),
        local_retriever=local,
        first_party_retriever=first_party,
    ).execute(task)

    assert summary.status == "succeeded"
    assert task.status == "succeeded"
    first_party.search.assert_not_called()
    legacy.search.assert_not_called()
