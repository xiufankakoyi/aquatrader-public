"""P0-P7 统一研究控制面验收测试。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest
from flask import Flask

from core.research.control_plane.calibration import CalibrationController
from core.research.control_plane.adapters import adapt_candidate, adapt_legacy_evidence, adapt_raw_document
from core.research.control_plane.contracts import (
    CalibrationRun, Claim, ClaimStatus, DocumentVersion, EvidenceRecord, FeedbackDomain,
    FeedbackEvent, ResearchTask, ReviewItem, TaskBudget,
)
from core.research.control_plane.downstream import eligible_claims, explainable_company_scores
from core.research.control_plane.feedback import FeedbackController
from core.research.control_plane.gaps import GapType, ResearchGap
from core.research.control_plane.governance import SourceGovernance
from core.research.control_plane.contracts import SourceTrace
from core.research.control_plane.orchestrator import ResearchOrchestrator, SearchTier
from core.research.control_plane.policies import ClaimPolicy, evidence_visible_as_of
from core.research.control_plane.validity_config import compute_valid_until
from core.research.control_plane.repository import ResearchRepository


@pytest.fixture
def repo(tmp_path):
    return ResearchRepository(tmp_path / "research.db")


def _support(claim_id: str, evidence_id: str, lineage: str, *, first_party: bool = False, slots=None):
    return EvidenceRecord(
        evidence_id=evidence_id, claim_id=claim_id, lineage_id=lineage,
        stance="support", quote="可定位原文", source_quality=1.2, match_score=0.9,
        grounding_status="exact", is_first_party=first_party,
        published_at="2025-01-01T00:00:00+00:00", retrieved_at="2025-01-02T00:00:00+00:00",
        metadata={"slots": slots or ["identity", "relationship"]},
    )


def test_schema_migration_is_idempotent_and_complete(repo):
    repo.migrate(); repo.migrate()
    with sqlite3.connect(repo.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"research_tasks", "documents", "document_versions", "claims", "evidence", "claim_evidence", "state_transitions", "review_items", "feedback_events", "policy_versions", "calibration_runs"} <= tables


def test_contract_and_adapter_boundaries_degrade_without_raising():
    assert ResearchTask.from_dict(None).task_id
    assert TaskBudget.from_dict({"max_requests": "bad", "max_tokens": -4}).max_requests == 20
    assert TaskBudget.from_dict({"max_requests": "bad", "max_tokens": -4}).max_tokens == 0
    assert DocumentVersion(content_text=None).content_text == ""
    assert adapt_raw_document(None)[1].content_available is False
    evidence = adapt_legacy_evidence({"metadata": [], "quality": "bad", "match_score": float("nan")})
    assert evidence.source_quality == 0.5
    assert evidence.match_score == 0.0
    claim = adapt_candidate({"attributes": [], "support_terms": "bad", "activation_score": "bad"})
    assert claim.support_terms == []
    assert claim.evidence_confidence == 0.0


def test_policy_downstream_and_validity_boundaries_fail_closed():
    policy = ClaimPolicy()
    assert policy.evaluate(None, [None], tier_cost="bad", node_importance=float("nan")).target_status == ClaimStatus.DISCOVERED.value
    assert eligible_claims([None], None) == []
    assert explainable_company_scores([None], None, None, policy_version=None) == []
    assert compute_valid_until(claim_type="news", custom_config=[]).__class__ is str


def test_calibration_boundaries_fail_closed(repo):
    controller = CalibrationController(repo)
    with pytest.raises(ValueError, match="类型无效"):
        controller.create_shadow(None)
    run = CalibrationRun(
        domain="alpha",
        parameters={"weight": float("nan")},
        bounds={"weight": {"min": "bad", "max": "bad"}},
    )
    controller.create_shadow(run)
    assert run.parameters["weight"] == 0.0


def test_repository_rejects_wrong_types_and_skips_corrupt_payloads(repo):
    with pytest.raises(TypeError, match="期望 Claim"):
        repo.save_claim(None)
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO research_tasks(task_id, status, created_at, payload_json) VALUES(?,?,?,?)",
            ("bad-task", "queued", "", "not-json"),
        )
    assert repo.get_task("bad-task") is None
    assert repo.list_tasks(limit="bad") == []
    assert repo.get_claim([]) is None


def test_generated_hypothesis_is_rejected_by_transaction_store(repo):
    item = EvidenceRecord(claim_id="c1", quote="模型猜测", evidence_kind="retrieval_hypothesis")
    assert repo.save_evidence(item) is False
    assert repo.list_evidence("c1") == []


def test_claim_policy_uses_independent_sources_and_counter_search(repo):
    claim = Claim(claim_id="c1", counter_search_completed=False)
    duplicated = [_support("c1", "e1", "same"), _support("c1", "e2", "same")]
    result = ClaimPolicy().evaluate(claim, duplicated)
    assert result.independent_sources == 1
    assert result.target_status == ClaimStatus.CANDIDATE.value

    claim.counter_search_completed = True
    independent = [duplicated[0], _support("c1", "e3", "other")]
    result = ClaimPolicy().evaluate(claim, independent)
    assert result.sufficient is True
    assert result.target_status == ClaimStatus.ACTIVE.value


def test_high_impact_claim_requires_first_party_source():
    claim = Claim(claim_id="c1", high_impact=True, counter_search_completed=True)
    result = ClaimPolicy().evaluate(claim, [_support("c1", "e1", "a"), _support("c1", "e2", "b")])
    assert result.target_status == ClaimStatus.SUPPORTED.value
    result = ClaimPolicy().evaluate(claim, [_support("c1", "e1", "a", first_party=True), _support("c1", "e2", "b")])
    assert result.target_status == ClaimStatus.ACTIVE.value


def test_counter_evidence_creates_conflict():
    claim = Claim(claim_id="c1", counter_search_completed=True)
    counter = EvidenceRecord(claim_id="c1", evidence_id="x", lineage_id="counter", stance="counter", quote="公司否认", grounding_status="exact", source_quality=1.2, match_score=1.0)
    result = ClaimPolicy().evaluate(claim, [_support("c1", "e1", "support"), counter])
    assert result.target_status == ClaimStatus.CONFLICTED.value


def test_point_in_time_blocks_future_and_missing_time():
    future = EvidenceRecord(published_at="2030-01-01T00:00:00+00:00", retrieved_at="2030-01-01T00:00:00+00:00")
    missing = EvidenceRecord(published_at="", retrieved_at="")
    assert not evidence_visible_as_of(future, "2029-01-01T00:00:00+00:00", historical=True)
    assert not evidence_visible_as_of(missing, "2029-01-01T00:00:00+00:00", historical=True)
    assert evidence_visible_as_of(missing, "2029-01-01T00:00:00+00:00", historical=False)


def test_expired_claim_dormant_and_duplicate_claim_pruned():
    claim = Claim(claim_id="c1", counter_search_completed=True)
    stale = _support("c1", "e1", "l1"); stale.stance = "stale"
    assert ClaimPolicy().evaluate(claim, [stale]).target_status == ClaimStatus.DORMANT.value
    claim.attributes["prune_reason"] = "duplicate_node"
    assert ClaimPolicy().evaluate(claim, []).target_status == ClaimStatus.PRUNED.value


def test_gap_priority_is_not_hardcoded_by_node_type():
    first = ResearchGap("g1", GapType.EVIDENCE.value, node_type="PRODUCT", research_importance=0.8)
    second = ResearchGap("g2", GapType.EVIDENCE.value, node_type="MATERIAL", research_importance=0.8)
    assert first.priority == second.priority
    assert first.to_task().task_type == GapType.EVIDENCE.value


def test_source_governance_blocks_robots_and_does_not_equate_type_with_truth():
    blocked = SourceGovernance().evaluate(SourceTrace(robots_policy="blocked", source_type="official"))
    allowed = SourceGovernance().evaluate(SourceTrace(robots_policy="allow", source_type="news"))
    assert not blocked.allowed
    assert allowed.allowed and allowed.reliability_prior < 1.0


class _Retriever:
    def __init__(self): self.calls = []
    def search(self, query, *, tier, limit): self.calls.append((query, tier)); return [{"text": query}]


class _Extractor:
    def extract(self, document, claim, *, query_purpose):
        if query_purpose == "counter": return []
        suffix = document["text"] + str(len(document["text"]))
        return [_support(claim.claim_id, f"ev_{abs(hash(suffix))}", f"lineage_{abs(hash(suffix))}", first_party="high" in document["text"])]


def test_orchestrator_routes_every_result_through_unified_evaluation(repo):
    claim = Claim(claim_id="c1", subject_name="公司", relation="供应", object_name="产品")
    repo.save_claim(claim)
    task = ResearchTask(target_claim_id="c1", budget=TaskBudget(max_requests=10, max_documents=10).to_dict())
    repo.save_task(task)
    retriever = _Retriever()
    summary = ResearchOrchestrator(repo, retriever, _Extractor(), tiers=(SearchTier("high", 3), SearchTier("medium", 3))).execute(task)
    assert summary.requests_used >= 2
    assert repo.list_transitions("c1")
    assert repo.get_task(task.task_id).status in {"succeeded", "partial"}
    assert any("否认" in query for query, _tier in retriever.calls)


def test_orchestrator_preserves_monotonic_claim_status_on_weaker_recheck(
    repo,
):
    claim = Claim(
        claim_id="c-monotonic",
        subject_name="公司",
        relation="属于",
        object_name="主题",
        research_status=ClaimStatus.DISCOVERED.value,
        required_slots=["identity", "relationship"],
    )
    repo.save_claim(claim)
    repo.save_evidence(
        _support(
            claim.claim_id,
            "ev-monotonic",
            "lineage-monotonic",
            slots=["identity"],
        )
    )
    claim.research_status = ClaimStatus.SUPPORTED.value
    repo.save_claim(claim)
    orchestrator = ResearchOrchestrator(
        repo,
        _Retriever(),
        _Extractor(),
    )

    decision = orchestrator._evaluate_and_transition(
        claim,
        SearchTier("high", 3),
    )

    persisted = repo.get_claim(claim.claim_id)
    assert persisted is not None
    assert persisted.research_status == ClaimStatus.SUPPORTED.value
    assert decision.target_status == ClaimStatus.SUPPORTED.value
    assert "monotonic_status_preserved" in decision.reason_codes


def test_feedback_domains_are_isolated_and_generate_domain_task(repo):
    for domain, expected in [("knowledge", "knowledge_revalidation"), ("retrieval", "retrieval_policy_review"), ("alpha", "alpha_model_review")]:
        event = FeedbackController(repo).record(FeedbackEvent(domain=domain, target_id="x", event_type="test"))
        assert repo.get_task(event.generated_task_id).task_type == expected
    with pytest.raises(ValueError):
        FeedbackController(repo).record(FeedbackEvent(domain="mixed"))


def test_calibration_requires_shadow_guards_and_approval(repo):
    from core.research.control_plane.test_run_record import TestRunStore
    controller = CalibrationController(repo)
    test_store = TestRunStore(repo)
    run = controller.create_shadow(CalibrationRun(domain="alpha", sample_size=99, metrics={"walk_forward_score": 0.8}))
    # 门槛不足时应拒绝
    with pytest.raises(ValueError): controller.activate(run.calibration_id, "审核人")
    # 缺少 test_run_id 时应拒绝
    run.sample_size = 120; run.metrics = {"walk_forward_score": 0.60, "false_promotion_rate": 0.02}; repo.save_calibration(run)
    with pytest.raises(ValueError, match="必须提供 test_run_id"):
        controller.activate(run.calibration_id, "审核人")
    # 服务端有可信测试记录时激活成功
    test_record = test_store.record_run(
        code_version="abc123",
        test_suite="tests/test_feedback_loop_integration.py",
        passed=True,
    )
    active = controller.activate(
        run.calibration_id, "审核人",
        test_run_id=test_record.test_run_id, code_version="abc123",
    )
    assert active.status == "active"

    # 首次激活的 policy 无 previous_version，回滚应拒绝
    with pytest.raises(ValueError, match="无 previous_version"):
        controller.rollback(run.calibration_id, "审核人")

    # 激活第二个版本后回滚，应恢复第一个版本
    run2 = controller.create_shadow(CalibrationRun(
        domain="alpha", sample_size=150,
        metrics={"walk_forward_score": 0.70, "false_promotion_rate": 0.03},
    ))
    test_record2 = test_store.record_run(
        code_version="abc123",
        test_suite="tests/test_feedback_loop_integration.py",
        passed=True,
    )
    active2 = controller.activate(
        run2.calibration_id, "审核人",
        test_run_id=test_record2.test_run_id, code_version="abc123",
    )
    assert active2.status == "active"
    # 回滚第二个版本，应恢复第一个版本
    rolled = controller.rollback(run2.calibration_id, "审核人")
    assert rolled.status == "rolled_back"
    # 验证第一个版本已恢复为 active
    restored = repo.get_active_policy("alpha")
    assert restored is not None
    assert restored.policy_version == active.policy_version


def test_test_run_record_normalizes_malformed_optional_fields(repo):
    from core.research.control_plane.test_run_record import TestRunRecord, TestRunStore

    record = TestRunRecord.from_dict(
        {
            "test_run_id": "  run-normalized  ",
            "code_version": None,
            "test_suite": 123,
            "passed": "false",
            "metrics": ["bad"],
        }
    )
    assert record.test_run_id == "run-normalized"
    assert record.code_version == ""
    assert record.test_suite == ""
    assert record.passed is False
    assert record.metrics == {}

    stored = TestRunStore(repo).record_run(
        code_version="abc123",
        test_suite="tests/smoke.py",
        passed="false",
        metrics=["bad"],
    )
    assert stored.passed is False
    assert stored.metrics == {}


def test_downstream_excludes_exploration_and_conflicted_claims():
    active = Claim(claim_id="a", object_id="company", research_status="ACTIVE", evidence_confidence=0.8, mapping_confidence=0.9)
    candidate = Claim(claim_id="b", object_id="company", research_status="CANDIDATE")
    conflicted = Claim(claim_id="c", object_id="company", research_status="ACTIVE", conflict_status="unresolved")
    evidence = {"a": [_support("a", "e1", "l1")], "b": [_support("b", "e2", "l2")], "c": [_support("c", "e3", "l3")]}
    allowed = eligible_claims([active, candidate, conflicted], evidence)
    assert [item.claim_id for item in allowed] == ["a"]
    scores = explainable_company_scores([active], evidence, {"company": {"event_impact": 0.7}}, policy_version="v1")
    assert scores[0]["claim_ids"] == ["a"]
    assert scores[0]["event_features"] == {"event_impact": 0.7}


def test_research_api_smoke(repo, monkeypatch):
    import server.routes.research_routes as routes
    monkeypatch.setattr(routes, "_repository", repo)
    app = Flask(__name__); app.register_blueprint(routes.research_bp); client = app.test_client()
    created = client.post("/api/research/claims", json={"claim_id": "c1", "subject_name": "测试公司"})
    assert created.status_code == 201
    rejected = client.post("/api/research/evidence", json={"claim_id": "c1", "quote": "猜测", "evidence_kind": "generated"})
    assert rejected.status_code == 422
    assert client.get("/api/research/claims/c1").status_code == 200
    assert client.get("/api/research/health").get_json()["data"]["schema_version"] >= 3


def test_acceptance_report_contains_required_metrics(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    report = repo.acceptance_report("2025-01-01T00:00:00+00:00")
    assert set(report) >= {
        "migration_counts", "failed_degradations", "claim_state_distribution",
        "source_independence_rate", "future_evidence_blocked",
    }
