from __future__ import annotations

from core.research.control_plane.tier_orchestrator import (
    AdaptiveSearchBudgetPolicy,
    EvidenceCoverageEvaluator,
    PerillaOrchestrator,
)


def test_budget_policy_never_exceeds_zero_or_small_budget() -> None:
    policy = AdaptiveSearchBudgetPolicy()

    assert policy.plan({"high": [1], "medium": [1]}, 0).allocations == {
        "high": 0, "medium": 0, "low": 0
    }
    small = policy.plan(
        {"high": [1], "medium": [1], "low": [1]}, 2
    ).allocations
    assert sum(small.values()) <= 2


def test_coverage_evaluator_normalizes_input_shapes_and_flags() -> None:
    decision = EvidenceCoverageEvaluator(require_counter_search="false").evaluate(
        [{
            "title": "official source",
            "content": "supporting evidence",
            "source_type": "Official",
            "metadata": {"source_lineage_id": "lineage-1", "related_themes": ["theme"]},
        }],
        counter_search_completed="false",
        required_targets=["theme"],
        require_first_party="true",
    )

    assert decision.complete is False
    assert decision.first_party_sources == 1
    assert decision.coverage_ratio == 1.0
    assert decision.counter_search_completed is False


def test_tier_failure_isolated_and_lower_tier_can_continue() -> None:
    orchestrator = PerillaOrchestrator(AdaptiveSearchBudgetPolicy())

    def run_tier(tier, _tasks):
        if tier == "high":
            raise RuntimeError("provider unavailable")
        return ([], [{"content": "supporting evidence", "metadata": {"source_lineage_id": tier}}])

    result = orchestrator.execute(
        {"high": [{"priority": 1}], "medium": [{"priority": 1}]},
        run_tier,
        total_budget=2,
        required_targets=set(),
    )

    assert "high" in result.tiers_skipped
    assert "medium" in result.tiers_executed
    assert result.decision is not None
