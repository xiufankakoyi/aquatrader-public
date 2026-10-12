"""Committee contracts 边界回归测试。"""
from __future__ import annotations

from core.research.committee.contracts import CommitteeWorkflow


def test_workflow_recomputes_assumption_hash_instead_of_trusting_stale_input():
    workflow = CommitteeWorkflow(
        core_assumptions=[" 假设 A ", None],
        core_assumptions_hash="stale-hash",
    )
    expected = CommitteeWorkflow(core_assumptions=["假设 A"]).core_assumptions_hash

    assert workflow.core_assumptions == ["假设 A"]
    assert workflow.core_assumptions_hash == expected


def test_workflow_with_no_assumptions_does_not_keep_stale_hash():
    workflow = CommitteeWorkflow(core_assumptions=[], core_assumptions_hash="stale-hash")

    assert workflow.core_assumptions_hash == ""
