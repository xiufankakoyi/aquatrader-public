"""CommitteeWorkflowEngine 边界回归测试。"""
from __future__ import annotations

from core.research.committee.contracts import (
    CommitteeWorkflow,
    ROLE_LEAD_ANALYST,
    ROLE_FINANCIAL_QUALITY_REVIEWER,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    STATUS_DRAFT,
    STATUS_EVIDENCE_REVIEW,
)
from core.research.committee.workflow import CommitteeWorkflowEngine


def _workflow() -> CommitteeWorkflow:
    return CommitteeWorkflow(
        workflow_id="wf_1",
        lead_analyst_user_id="lead_1",
        financial_quality_reviewer_user_id="finance_1",
        status=STATUS_DRAFT,
    )


def test_add_review_does_not_split_scalar_evidence_reference_into_characters():
    workflow = _workflow()
    success, error_code, _ = CommitteeWorkflowEngine().add_review(
        workflow,
        role=ROLE_FINANCIAL_QUALITY_REVIEWER,
        reviewer_user_id="finance_1",
        evidence_refs="evidence_1",
    )

    assert success, error_code
    assert workflow.reviews[-1].evidence_refs == []


def test_transition_skips_malformed_review_in_audit_snapshot():
    workflow = _workflow()
    workflow.reviews.append(None)

    success, error_code, _ = CommitteeWorkflowEngine().transition(
        workflow,
        STATUS_EVIDENCE_REVIEW,
        operator_user_id="lead_1",
        operator_role=ROLE_LEAD_ANALYST,
    )

    assert success, error_code
    assert workflow.audit_trail[-1].review_snapshot == []


def test_update_assumptions_degrades_missing_old_hash():
    workflow = _workflow()
    workflow.core_assumptions_hash = None

    success, error_code, _ = CommitteeWorkflowEngine().update_core_assumptions(
        workflow,
        new_assumptions=["假设 A"],
        operator_user_id="lead_1",
        operator_role=ROLE_LEAD_ANALYST,
    )

    assert success, error_code
    assert "core_assumptions updated" in workflow.audit_trail[-1].reason


def test_transition_rejects_trade_action_keyword_in_audit_reason():
    workflow = _workflow()

    success, error_code, _ = CommitteeWorkflowEngine().transition(
        workflow,
        STATUS_EVIDENCE_REVIEW,
        operator_user_id="lead_1",
        operator_role=ROLE_LEAD_ANALYST,
        reason=f"建议{FORBIDDEN_TRADE_ACTION_KEYWORDS[0]}",
    )

    assert not success
    assert error_code == "trade_action_keyword"
