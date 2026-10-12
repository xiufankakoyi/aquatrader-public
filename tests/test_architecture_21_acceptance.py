"""用户指出的 21 项架构问题逐项防回归验收。"""
from __future__ import annotations

from pathlib import Path

from core.research.control_plane.contracts import Claim, EvidenceRecord, FeedbackEvent, PolicyVersion
from core.research.control_plane.domain_calibrators import (
    AlphaPolicyCalibrator, KnowledgePolicyCalibrator, RetrievalPolicyCalibrator,
)
from core.research.control_plane.feedback import FeedbackController
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.tier_orchestrator import (
    AdaptiveSearchBudgetPolicy, EvidenceCoverageEvaluator, PerillaOrchestrator,
)
from core.research_graph.candidate_graph import CandidateStore
from core.research_graph.evidence_activator import EvidenceActivator

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_01_02_diagram_has_one_orchestrator_and_no_bypass_or_post_search_phase():
    diagram = _read("tiered_pipeline.mmd")
    assert "D1 -->|是: 停止| O1" not in diagram
    assert "D2 -->|是: 停止| O1" not in diagram
    assert "P6 — Perilla 分层调度" not in diagram
    # 统一贝叶斯评估链为必经路径，停止条件节点承载多维判断
    assert "LINEAGE --> B1 --> B2 --> B3 --> B4 --> STOP" in diagram
    assert "STOP -->|未充分 + 有下一层 + 边际价值≥0.15| PLAN" in diagram
    assert "STOP -->|充分 或 边际价值<0.15| COMMIT" in diagram
    assert "ResearchOrchestrator 统一调度" in diagram
    assert "无独立分层调度" in diagram


def test_03_runtime_disables_pseudo_evidence_pregen_and_posterior_matching():
    pipeline = _read("sandbox/run_real_research_pipeline.py")
    assert "enable_pseudo_pregen=True" not in pipeline
    assert "use_pseudo_evidence=True" not in pipeline
    assert "PseudoEvidenceGenerator" not in pipeline
    assert "lightweight_no_embedding" in pipeline
    diagram = _read("tiered_pipeline.mmd")
    assert "eligible_for_posterior=false" in diagram
    assert "eligible_for_evidence_slot=false" in diagram


def test_04_sag_precedes_company_score_in_diagram_and_runtime():
    diagram = _read("tiered_pipeline.mmd")
    assert "SAG --> SCORE" in diagram
    assert "SCORE --> EVAL" in diagram
    selector = _read("core/paper_trading/selector.py")
    assert "stable_graph + SAG_event_graph -> company_score" in selector


def test_05_three_feedback_domains_have_distinct_calibrators(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    assert KnowledgePolicyCalibrator(repo).domain == "knowledge"
    assert RetrievalPolicyCalibrator(repo).domain == "retrieval"
    assert AlphaPolicyCalibrator(repo).domain == "alpha"
    diagram = _read("tiered_pipeline.mmd")
    assert "FACT --> KO --> KC" in diagram
    assert "SEARCHMETRIC --> RO --> RC" in diagram
    assert "EVAL --> AO --> AC" in diagram
    assert "EVAL --> AO --> KC" not in diagram


def test_06_downstream_feedback_is_persisted_and_shadow_calibration_executes(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    FeedbackController(repo).record(
        FeedbackEvent(domain="retrieval", event_type="tier_completed", metrics={"tier": "high"}, attribution={"tier": "high"}),
        generate_task=False,
    )
    assert repo.list_feedback("retrieval")
    assert any(run.domain == "retrieval" for run in repo.list_calibrations())


def test_07_to_11_calibrators_cover_threshold_source_priority_importance_stop_and_budget(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    knowledge = KnowledgePolicyCalibrator(repo).fit().parameters
    retrieval = RetrievalPolicyCalibrator(repo).fit().parameters
    assert "source_weights" in knowledge
    assert "claim_activation_threshold" in knowledge
    assert "priority_features" in retrieval
    assert "expected_information_gain" in retrieval["priority_features"]
    assert retrieval["stop_min_independent_sources"] == 2
    assert retrieval["require_counter_search"] is True
    assert "recommended_budget" in retrieval


def test_12_real_search_entrypoints_share_perilla_orchestrator():
    knowledge = _read("core/research/perilla/discovery/knowledge_loop_engine.py")
    autonomous = _read("core/research/perilla/discovery/autonomous_perilla_engine.py")
    assert "PerillaOrchestrator" in knowledge
    assert "PerillaOrchestrator" in autonomous
    assert "len(tier_docs) >= 3" not in autonomous


def test_13_tiered_search_stops_only_after_bayesian_state_and_claim_sufficiency():
    source = _read("core/research_graph/tiered_evidence_searcher.py")
    # Claim 级充分性已迁移至 ResearchOrchestrator/ClaimPolicy；
    # TieredEvidenceSearcher 仅保留节点级贝叶斯早停启发。
    assert "_evidence_sufficient" not in source
    assert "_node_status_reached" in source
    assert "BayesianStatusUpgrader" in source
    assert "Claim 级充分性判断" in source
    assert "ResearchOrchestrator" in source
    assert "否认 澄清 未供货 终止" in source


def test_14_quality_is_counted_once():
    source = _read("core/research_graph/bayesian_status_upgrader.py")
    # 旧公式：quality 被非线性计算两次，不允许
    assert "quality * match_score * source_discount" not in source
    assert "quality * match_score * time_decay" not in source
    # 新公式：source_discount 是 quality 的唯一入口
    assert "source_discount * match_score * time_decay * correlation_discount" in source
    assert "source_discount = quality / (quality + self.source_discount_kappa)" in source


def test_15_16_lineage_discount_and_time_decay_are_runtime_fields():
    source = _read("core/research_graph/bayesian_status_upgrader.py")
    assert "source_lineage_id" in source
    assert "correlation_group" in source
    assert "time_decay" in source


def test_17_counter_search_is_explicit_in_all_orchestrated_paths():
    assert "否认 澄清 未供货 终止" in _read("core/research/perilla/discovery/knowledge_loop_engine.py")
    assert "否认 澄清 未供货 终止" in _read("core/research/perilla/discovery/autonomous_perilla_engine.py")
    assert "否认 澄清 未供货 终止" in _read("core/research_graph/tiered_evidence_searcher.py")


def test_18_unified_p1_contracts_exist():
    contract = _read("core/research/control_plane/contracts.py")
    for name in ("DocumentVersion", "SourceTrace", "Claim", "EvidenceRecord", "StateTransition"):
        assert f"class {name}" in contract


def test_19_point_in_time_is_enforced_for_research_and_paper_evaluation():
    assert "evidence_visible_as_of" in _read("core/research/control_plane/policies.py")
    assert "as_of=f\"{date}T00:00:00+00:00\"" in _read("core/paper_trading/orchestrator.py")


def test_20_execution_model_covers_slippage_limits_suspension_volume_and_fees():
    orchestrator = _read("core/paper_trading/orchestrator.py")
    data_source = _read("core/paper_trading/data_source.py")
    portfolio = _read("core/paper_trading/portfolio.py")
    for token in ("slippage_rate", "volume_cap_ratio", "is_suspended", "is_limit_up", "is_limit_down"):
        assert token in orchestrator or token in data_source
    for token in ("COMMISSION_RATE", "TAX_RATE", "MIN_COMMISSION"):
        assert token in portfolio


def test_21_trading_feedback_cannot_mutate_fact_confidence():
    paper = _read("core/paper_trading/orchestrator.py")
    assert 'domain="alpha"' in paper
    assert '"fact_confidence_mutation_allowed": False' in paper
    alpha = AlphaPolicyCalibrator.__doc__ or ""
    assert "事实层" in alpha


def test_adaptive_budget_changes_with_retrieval_outcomes(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    controller = FeedbackController(repo)
    controller.record(FeedbackEvent(domain="retrieval", metrics={"tier": "low", "effective_evidence_rate": 1.0, "independent_source_rate": 1.0, "unit_cost": 0.1}, attribution={"tier": "low"}), generate_task=False)
    plan = AdaptiveSearchBudgetPolicy(repo).plan({"high": [1, 2], "medium": [1, 2], "low": [1, 2]}, 9)
    assert plan.allocations["low"] > 0
    assert plan.utilities["low"] != 0.35


def test_adaptive_budget_consumes_runtime_web_stage_feedback(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    repo.save_feedback(FeedbackEvent(
        domain="retrieval",
        event_type="retrieval_metric",
        metrics={
            "tier": "web",
            "documents_found": 2,
            "evidence_saved": 2,
            "independent_sources": 2,
        },
    ))

    plan = AdaptiveSearchBudgetPolicy(repo).plan(
        {"high": [1], "medium": [1], "low": [1]},
        6,
    )

    assert plan.utilities["low"] != 0.35


def test_active_domain_policies_are_injected_at_runtime(tmp_path):
    repo = ResearchRepository(tmp_path / "control.db")
    repo.save_policy(PolicyVersion(
        policy_version="retrieval-test", domain="retrieval", status="active",
        parameters={
            "tier_utilities": {"high": 0.1, "medium": 0.2, "low": 2.0},
            "recommended_budget": 24,
            "stop_min_independent_sources": 3,
            "require_counter_search": True,
        },
    ))
    repo.save_policy(PolicyVersion(
        policy_version="knowledge-test", domain="knowledge", status="active",
        parameters={"source_weights": {"official": 1.23}, "claim_activation_threshold": 0.77},
    ))
    budget = AdaptiveSearchBudgetPolicy(repo)
    plan = budget.plan({"high": [1], "medium": [1], "low": [1]}, 6)
    assert plan.utilities["low"] == 2.0
    assert budget.recommended_total_budget(10, expected_information_gain=0.5) == 24
    orchestrator = PerillaOrchestrator(budget)
    assert orchestrator.evaluator.min_independent_sources == 3
    activator = EvidenceActivator(CandidateStore(tmp_path / "candidate.lance"), research_repository=repo)
    assert activator._source_quality_weights["official"] == 1.23
    assert activator._bayesian_upgrader.confidence_thresholds["SUPPORTED"] == 0.77


def test_counter_failure_does_not_satisfy_stop_and_reposts_fold_by_content(tmp_path):
    documents = [
        {"title": "同一公告", "content": "甲公司披露同一事实", "url": "https://a.example/1"},
        {"title": "同一公告", "content": "甲公司披露同一事实", "url": "https://b.example/2"},
    ]
    evaluator = EvidenceCoverageEvaluator()
    folded = evaluator.evaluate(documents, counter_search_completed=True)
    assert folded.independent_sources == 1
    orchestrator = PerillaOrchestrator(AdaptiveSearchBudgetPolicy(ResearchRepository(tmp_path / "counter.db")))
    result = orchestrator.execute(
        {"high": [{"priority": 1.0}], "medium": [], "low": []},
        lambda _tier, _batch: ([], [
            {"content": "来源一支持", "metadata": {"source_lineage_id": "one"}},
            {"content": "来源二支持", "metadata": {"source_lineage_id": "two"}},
        ], False),
        total_budget=1,
    )
    assert result.decision is not None
    assert result.decision.complete is False
    assert result.decision.counter_search_completed is False
