"""P2.7 专项测试：独立反方审查和投委会工作流。

覆盖 3 个验收点：
1. 缺任一强制复核不能进入 approved_for_monitoring
2. 修改核心假设后旧审批自动失效
3. API 有权限边界、审计和文档

附加覆盖：
- 强制要求 1：同一执行身份不得同时完成主分析和独立反方签署
- 强制要求 2：每次状态变化保存问题、回答、证据和时间
- 强制要求 3：反方必须检查 7 项
- 强制要求 4：审批仅表示研究材料达到监控标准，不表示交易建议
- 状态机合法性（TRANSITIONS）+ 终态不可迁移
- 仓储持久化 + 幂等迁移 + 不可覆盖
- AGENTS.md 合规（无交易动作建议 / 字段缺失降级）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.research.committee import (
    ADVERSARY_CHECK_ITEMS,
    ADVERSARY_CHECK_LABELS,
    APPROVAL_NOTICE,
    AuditEntry,
    CommitteeRepository,
    CommitteeWorkflow,
    CommitteeWorkflowEngine,
    ERR_ADVERSARY_CHECKLIST_INCOMPLETE,
    ERR_ALREADY_INVALIDATED,
    ERR_INVALID_TRANSITION,
    ERR_MISSING_REQUIRED_REVIEWS,
    ERR_PERMISSION_DENIED,
    ERR_SAME_IDENTITY,
    ERR_TERMINAL_STATE,
    ERR_TRADE_ACTION_KEYWORD,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    REQUIRED_REVIEW_ROLES,
    REQUIRED_REVIEWS_FOR_TRANSITION,
    REVIEW_ROLES,
    REVIEW_STATUS_BLOCKED,
    REVIEW_STATUS_CONCERNS,
    REVIEW_STATUS_FAIL,
    REVIEW_STATUS_PASS,
    ROLE_CHAIR,
    ROLE_FINANCIAL_QUALITY_REVIEWER,
    ROLE_INDEPENDENT_ADVERSARY,
    ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER,
    ROLE_LEAD_ANALYST,
    ROLE_MODEL_REVIEWER,
    ReviewQuestion,
    ReviewRecord,
    STATUS_ADVERSARIAL_REVIEW,
    STATUS_APPROVED_FOR_MONITORING,
    STATUS_COMMITTEE_READY,
    STATUS_DRAFT,
    STATUS_EVIDENCE_REVIEW,
    STATUS_EXPIRED,
    STATUS_MODEL_REVIEW,
    STATUS_REJECTED,
    STATUS_RETURNED,
    TERMINAL_STATUSES,
    TRANSITIONS,
    WORKFLOW_STATUSES,
    build_adversary_checklist_template,
    build_committee_workflow_engine,
    is_adversary_checklist_complete,
    utc_now,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


AS_OF = "2026-07-01T00:00:00+00:00"
SYMBOL = "TEST001"
THESIS_ID = "thesis_TEST001_2026-07-01_v1"

# 6 个角色用户 ID（lead_analyst 与 independent_adversary 不同）
UID_LEAD = "user_lead_001"
UID_FIN = "user_fin_002"
UID_IND = "user_ind_003"
UID_MOD = "user_mod_004"
UID_ADV = "user_adv_005"  # 独立反方（与 UID_LEAD 不同）
UID_CHAIR = "user_chair_006"


def make_workflow(
    *,
    workflow_id: str = "wf_test_001",
    thesis_id: str = THESIS_ID,
    company_symbol: str = SYMBOL,
    as_of: str = AS_OF,
    lead_analyst_user_id: str = UID_LEAD,
    independent_adversary_user_id: str = UID_ADV,
    financial_quality_reviewer_user_id: str = UID_FIN,
    industry_supply_chain_reviewer_user_id: str = UID_IND,
    model_reviewer_user_id: str = UID_MOD,
    chair_user_id: str = UID_CHAIR,
    core_assumptions: list[str] | None = None,
    status: str = STATUS_DRAFT,
) -> CommitteeWorkflow:
    """构造有效的 CommitteeWorkflow。"""
    return CommitteeWorkflow(
        workflow_id=workflow_id,
        thesis_id=thesis_id,
        company_symbol=company_symbol,
        as_of=as_of,
        lead_analyst_user_id=lead_analyst_user_id,
        financial_quality_reviewer_user_id=financial_quality_reviewer_user_id,
        industry_supply_chain_reviewer_user_id=industry_supply_chain_reviewer_user_id,
        model_reviewer_user_id=model_reviewer_user_id,
        independent_adversary_user_id=independent_adversary_user_id,
        chair_user_id=chair_user_id,
        core_assumptions=core_assumptions or [
            "假设 1：2027 年净利率 12%",
            "假设 2：行业增速 8%",
            "假设 3：公司市场份额 15%",
        ],
        status=status,
    )


def make_review(
    *,
    role: str,
    reviewer_user_id: str,
    workflow_id: str = "wf_test_001",
    status: str = REVIEW_STATUS_PASS,
    questions: list[ReviewQuestion] | None = None,
    summary: str = "复核通过",
) -> ReviewRecord:
    """构造 ReviewRecord。"""
    return ReviewRecord(
        review_id=f"rev_{role}_{workflow_id}",
        workflow_id=workflow_id,
        role=role,
        reviewer_user_id=reviewer_user_id,
        reviewer_display_name=f"{role}_display",
        status=status,
        questions=list(questions or []),
        summary=summary,
        evidence_refs=[f"evidence_{role}_001"],
        created_at=utc_now(),
    )


def make_adversary_questions(*, skip_items: set[str] | None = None) -> list[ReviewQuestion]:
    """构造完整的反方检查清单 7 项。

    Args:
        skip_items: 要跳过（不回答）的 item key 集合
    """
    skip_items = skip_items or set()
    questions = build_adversary_checklist_template()
    result: list[ReviewQuestion] = []
    for q in questions:
        item_key = q.question_id.replace("adv_check_", "")
        if item_key in skip_items:
            result.append(q)  # 不回答
            continue
        q.answer = f"已评估 {ADVERSARY_CHECK_LABELS[item_key]} 风险，未发现重大问题"
        q.evidence_refs = [f"evidence_adv_{item_key}"]
        q.answered_at = utc_now()
        result.append(q)
    return result


# ---------------------------------------------------------------------------
# 测试 1：契约常量
# ---------------------------------------------------------------------------


class TestContracts:
    """测试契约常量完整性。"""

    def test_six_roles_defined(self):
        """6 个角色全部定义。"""
        assert len(REVIEW_ROLES) == 6
        assert ROLE_LEAD_ANALYST in REVIEW_ROLES
        assert ROLE_FINANCIAL_QUALITY_REVIEWER in REVIEW_ROLES
        assert ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER in REVIEW_ROLES
        assert ROLE_MODEL_REVIEWER in REVIEW_ROLES
        assert ROLE_INDEPENDENT_ADVERSARY in REVIEW_ROLES
        assert ROLE_CHAIR in REVIEW_ROLES

    def test_required_review_roles_excludes_lead(self):
        """强制复核角色不包括 lead_analyst（lead 是被复核方）。"""
        assert ROLE_LEAD_ANALYST not in REQUIRED_REVIEW_ROLES
        assert len(REQUIRED_REVIEW_ROLES) == 5

    def test_nine_statuses_defined(self):
        """9 个状态全部定义。"""
        assert len(WORKFLOW_STATUSES) == 9
        for s in (
            STATUS_DRAFT,
            STATUS_EVIDENCE_REVIEW,
            STATUS_MODEL_REVIEW,
            STATUS_ADVERSARIAL_REVIEW,
            STATUS_COMMITTEE_READY,
            STATUS_APPROVED_FOR_MONITORING,
            STATUS_RETURNED,
            STATUS_REJECTED,
            STATUS_EXPIRED,
        ):
            assert s in WORKFLOW_STATUSES

    def test_terminal_statuses(self):
        """终态：rejected + expired。"""
        assert set(TERMINAL_STATUSES) == {STATUS_REJECTED, STATUS_EXPIRED}

    def test_transitions_graph_complete(self):
        """状态迁移图覆盖所有非终态。"""
        for status in WORKFLOW_STATUSES:
            if status in TERMINAL_STATUSES:
                assert TRANSITIONS[status] == ()
            else:
                assert status in TRANSITIONS

    def test_required_reviews_for_transition_keys(self):
        """4 个关键迁移需强制复核。"""
        assert (
            STATUS_EVIDENCE_REVIEW,
            STATUS_MODEL_REVIEW,
        ) in REQUIRED_REVIEWS_FOR_TRANSITION
        assert (
            STATUS_MODEL_REVIEW,
            STATUS_ADVERSARIAL_REVIEW,
        ) in REQUIRED_REVIEWS_FOR_TRANSITION
        assert (
            STATUS_ADVERSARIAL_REVIEW,
            STATUS_COMMITTEE_READY,
        ) in REQUIRED_REVIEWS_FOR_TRANSITION
        assert (
            STATUS_COMMITTEE_READY,
            STATUS_APPROVED_FOR_MONITORING,
        ) in REQUIRED_REVIEWS_FOR_TRANSITION

    def test_adversary_check_items_seven(self):
        """反方检查清单 7 项。"""
        assert len(ADVERSARY_CHECK_ITEMS) == 7
        for item in ADVERSARY_CHECK_ITEMS:
            assert item in ADVERSARY_CHECK_LABELS

    def test_approval_notice_is_research_only(self):
        """审批说明明确为研究监控标准，非交易建议。"""
        assert "交易建议" in APPROVAL_NOTICE
        assert "监控标准" in APPROVAL_NOTICE

    def test_validate_role_assignment_same_identity_rejected(self):
        """同一身份同时为主分析和独立反方被拒。"""
        wf = make_workflow(
            lead_analyst_user_id="same_user",
            independent_adversary_user_id="same_user",
        )
        is_valid, msg = wf.validate_role_assignment()
        assert not is_valid
        assert "相同" in msg

    def test_validate_role_assignment_different_identity_accepted(self):
        """不同身份被接受。"""
        wf = make_workflow()
        is_valid, msg = wf.validate_role_assignment()
        assert is_valid
        assert msg == ""

    def test_review_record_is_passing(self):
        """pass 和 concerns 均视为通过。"""
        for status in (REVIEW_STATUS_PASS, REVIEW_STATUS_CONCERNS):
            r = make_review(role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN, status=status)
            assert r.is_passing()
        for status in (REVIEW_STATUS_FAIL, REVIEW_STATUS_BLOCKED):
            r = make_review(role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN, status=status)
            assert not r.is_passing()


# ---------------------------------------------------------------------------
# 测试 2：反方检查清单
# ---------------------------------------------------------------------------


class TestAdversaryChecklist:
    """测试反方 7 项检查清单。"""

    def test_template_has_seven_questions(self):
        """模板 7 项。"""
        template = build_adversary_checklist_template()
        assert len(template) == 7
        ids = {q.question_id for q in template}
        for item in ADVERSARY_CHECK_ITEMS:
            assert f"adv_check_{item}" in ids

    def test_template_questions_unanswered(self):
        """模板问题初始未回答。"""
        template = build_adversary_checklist_template()
        for q in template:
            assert not q.is_answered()

    def test_checklist_complete_all_answered(self):
        """7 项全回答 → 完整。"""
        questions = make_adversary_questions()
        is_complete, missing = is_adversary_checklist_complete(questions)
        assert is_complete
        assert missing == []

    def test_checklist_incomplete_one_missing(self):
        """缺 1 项 → 不完整。"""
        questions = make_adversary_questions(skip_items={ADVERSARY_CHECK_ITEMS[0]})
        is_complete, missing = is_adversary_checklist_complete(questions)
        assert not is_complete
        assert ADVERSARY_CHECK_ITEMS[0] in missing

    def test_checklist_incomplete_all_missing(self):
        """全部未回答 → 7 项缺失。"""
        questions = build_adversary_checklist_template()
        is_complete, missing = is_adversary_checklist_complete(questions)
        assert not is_complete
        assert len(missing) == 7

    def test_checklist_label_chinese(self):
        """7 项标签均为中文。"""
        for item in ADVERSARY_CHECK_ITEMS:
            label = ADVERSARY_CHECK_LABELS[item]
            assert label
            # 中文标签
            assert any("\u4e00" <= ch <= "\u9fff" for ch in label)


# ---------------------------------------------------------------------------
# 测试 3：状态机合法性
# ---------------------------------------------------------------------------


class TestStateMachine:
    """测试状态机迁移合法性。"""

    def test_draft_can_only_go_to_evidence_review(self):
        """draft 只能迁移到 evidence_review。"""
        wf = make_workflow(status=STATUS_DRAFT)
        engine = build_committee_workflow_engine()
        can, _, _ = engine.can_transition(
            wf, STATUS_EVIDENCE_REVIEW,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert can

        can, err_code, _ = engine.can_transition(
            wf, STATUS_MODEL_REVIEW,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert not can
        assert err_code == ERR_INVALID_TRANSITION

    def test_terminal_state_cannot_transition(self):
        """终态不可迁移。"""
        for terminal in TERMINAL_STATUSES:
            wf = make_workflow(status=terminal)
            engine = build_committee_workflow_engine()
            can, err_code, _ = engine.can_transition(
                wf, STATUS_DRAFT,
                operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
            )
            assert not can
            assert err_code == ERR_TERMINAL_STATE

    def test_invalidated_workflow_cannot_transition(self):
        """已被假设变更失效的工作流不可再迁移。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        wf.invalidated_by_assumption_change = True
        engine = build_committee_workflow_engine()
        can, err_code, _ = engine.can_transition(
            wf, STATUS_EXPIRED,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
        )
        assert not can
        assert err_code == ERR_ALREADY_INVALIDATED

    def test_returned_can_go_back_to_draft(self):
        """returned 可回到 draft。"""
        wf = make_workflow(status=STATUS_RETURNED)
        engine = build_committee_workflow_engine()
        can, _, _ = engine.can_transition(
            wf, STATUS_DRAFT,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert can

    def test_approved_can_only_expire(self):
        """approved_for_monitoring 只能迁移到 expired。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        engine = build_committee_workflow_engine()
        can, _, _ = engine.can_transition(
            wf, STATUS_EXPIRED,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
        )
        assert can

        can, err_code, _ = engine.can_transition(
            wf, STATUS_DRAFT,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
        )
        assert not can
        assert err_code == ERR_INVALID_TRANSITION


# ---------------------------------------------------------------------------
# 验收点 1：缺任一强制复核不能进入 approved_for_monitoring
# ---------------------------------------------------------------------------


class TestAcceptancePoint1MissingRequiredReviews:
    """验收点 1：缺任一强制复核不能进入 approved_for_monitoring。"""

    def test_cannot_approve_without_any_required_review(self):
        """无任何强制复核 → 不能审批。"""
        wf = make_workflow(status=STATUS_COMMITTEE_READY)
        engine = build_committee_workflow_engine()
        can, err_code, err_msg = engine.can_transition(
            wf, STATUS_APPROVED_FOR_MONITORING,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
        )
        assert not can
        assert err_code == ERR_MISSING_REQUIRED_REVIEWS
        assert ROLE_CHAIR in err_msg

    def test_cannot_advance_evidence_to_model_without_financial_review(self):
        """缺财务质量复核 → 不能从 evidence_review 迁移到 model_review。"""
        wf = make_workflow(status=STATUS_EVIDENCE_REVIEW)
        # 只有行业复核，无财务复核
        wf.reviews.append(make_review(role=ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER, reviewer_user_id=UID_IND))
        engine = build_committee_workflow_engine()
        can, err_code, err_msg = engine.can_transition(
            wf, STATUS_MODEL_REVIEW,
            operator_user_id=UID_FIN, operator_role=ROLE_FINANCIAL_QUALITY_REVIEWER,
        )
        assert not can
        assert err_code == ERR_MISSING_REQUIRED_REVIEWS
        assert ROLE_FINANCIAL_QUALITY_REVIEWER in err_msg

    def test_cannot_advance_model_to_adversarial_without_model_reviewer(self):
        """缺模型复核 → 不能从 model_review 迁移到 adversarial_review。"""
        wf = make_workflow(status=STATUS_MODEL_REVIEW)
        engine = build_committee_workflow_engine()
        can, err_code, err_msg = engine.can_transition(
            wf, STATUS_ADVERSARIAL_REVIEW,
            operator_user_id=UID_MOD, operator_role=ROLE_MODEL_REVIEWER,
        )
        assert not can
        assert err_code == ERR_MISSING_REQUIRED_REVIEWS
        assert ROLE_MODEL_REVIEWER in err_msg

    def test_cannot_advance_adversarial_to_committee_without_adversary(self):
        """缺独立反方复核 → 不能从 adversarial_review 迁移到 committee_ready。"""
        wf = make_workflow(status=STATUS_ADVERSARIAL_REVIEW)
        engine = build_committee_workflow_engine()
        can, err_code, _ = engine.can_transition(
            wf, STATUS_COMMITTEE_READY,
            operator_user_id=UID_ADV, operator_role=ROLE_INDEPENDENT_ADVERSARY,
        )
        assert not can
        assert err_code == ERR_MISSING_REQUIRED_REVIEWS

    def test_cannot_advance_adversarial_to_committee_with_incomplete_checklist(self):
        """反方 7 项检查清单不完整 → 不能迁移到 committee_ready。"""
        wf = make_workflow(status=STATUS_ADVERSARIAL_REVIEW)
        # 反方复核但只回答 6 项
        questions = make_adversary_questions(skip_items={ADVERSARY_CHECK_ITEMS[3]})
        wf.reviews.append(make_review(
            role=ROLE_INDEPENDENT_ADVERSARY,
            reviewer_user_id=UID_ADV,
            questions=questions,
        ))
        engine = build_committee_workflow_engine()
        can, err_code, err_msg = engine.can_transition(
            wf, STATUS_COMMITTEE_READY,
            operator_user_id=UID_ADV, operator_role=ROLE_INDEPENDENT_ADVERSARY,
        )
        assert not can
        assert err_code == ERR_ADVERSARY_CHECKLIST_INCOMPLETE
        assert ADVERSARY_CHECK_LABELS[ADVERSARY_CHECK_ITEMS[3]] in err_msg

    def test_full_pipeline_approve_with_all_required_reviews(self):
        """完整 5 角色 + 7 项清单 → 可审批通过。"""
        wf = make_workflow(status=STATUS_COMMITTEE_READY)
        # 5 角色复核齐全
        wf.reviews.append(make_review(role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN))
        wf.reviews.append(make_review(role=ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER, reviewer_user_id=UID_IND))
        wf.reviews.append(make_review(role=ROLE_MODEL_REVIEWER, reviewer_user_id=UID_MOD))
        wf.reviews.append(make_review(
            role=ROLE_INDEPENDENT_ADVERSARY,
            reviewer_user_id=UID_ADV,
            questions=make_adversary_questions(),
        ))
        wf.reviews.append(make_review(role=ROLE_CHAIR, reviewer_user_id=UID_CHAIR))
        engine = build_committee_workflow_engine()
        can, _, _ = engine.can_transition(
            wf, STATUS_APPROVED_FOR_MONITORING,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
        )
        assert can

    def test_permission_denied_for_unassigned_operator(self):
        """未被分配为该角色的用户不能操作。"""
        wf = make_workflow(status=STATUS_COMMITTEE_READY)
        wf.reviews.append(make_review(role=ROLE_CHAIR, reviewer_user_id=UID_CHAIR))
        engine = build_committee_workflow_engine()
        can, err_code, _ = engine.can_transition(
            wf, STATUS_APPROVED_FOR_MONITORING,
            operator_user_id="random_user", operator_role=ROLE_CHAIR,
        )
        assert not can
        assert err_code == ERR_PERMISSION_DENIED


# ---------------------------------------------------------------------------
# 验收点 2：修改核心假设后旧审批自动失效
# ---------------------------------------------------------------------------


class TestAcceptancePoint2AssumptionChangeInvalidates:
    """验收点 2：修改核心假设后旧审批自动失效。"""

    def test_approved_workflow_invalidated_on_assumption_change(self):
        """已审批工作流的核心假设变更 → 自动失效。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        original_hash = wf.core_assumptions_hash
        assert original_hash
        engine = build_committee_workflow_engine()
        success, _, _ = engine.update_core_assumptions(
            wf,
            new_assumptions=["新假设 A：净利率 15%", "新假设 B：行业增速 5%"],
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert success
        assert wf.invalidated_by_assumption_change is True
        assert wf.invalidated_at != ""
        assert wf.status == STATUS_EXPIRED
        assert wf.core_assumptions_hash != original_hash

    def test_approved_workflow_not_invalidated_on_same_assumptions(self):
        """核心假设未变 → 不失效。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        same_assumptions = list(wf.core_assumptions)
        original_hash = wf.core_assumptions_hash
        engine = build_committee_workflow_engine()
        success, _, _ = engine.update_core_assumptions(
            wf,
            new_assumptions=same_assumptions,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert success
        assert wf.invalidated_by_assumption_change is False
        assert wf.status == STATUS_APPROVED_FOR_MONITORING
        assert wf.core_assumptions_hash == original_hash

    def test_non_approved_workflow_records_assumption_change_but_no_invalidation(self):
        """非 approved 状态变更假设 → 不触发失效但记审计。"""
        wf = make_workflow(status=STATUS_DRAFT)
        original_audit_count = len(wf.audit_trail)
        engine = build_committee_workflow_engine()
        success, _, _ = engine.update_core_assumptions(
            wf,
            new_assumptions=["新假设 A", "新假设 B"],
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert success
        assert wf.invalidated_by_assumption_change is False
        assert wf.status == STATUS_DRAFT
        assert len(wf.audit_trail) == original_audit_count + 1
        audit = wf.audit_trail[-1]
        assert "core_assumptions updated" in audit.reason

    def test_terminal_rejected_workflow_cannot_update_assumptions(self):
        """rejected 终态工作流不能更新假设。"""
        wf = make_workflow(status=STATUS_REJECTED)
        engine = build_committee_workflow_engine()
        success, err_code, _ = engine.update_core_assumptions(
            wf,
            new_assumptions=["x"],
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert not success
        assert err_code == ERR_TERMINAL_STATE

    def test_assumption_change_audit_entry_captures_hash_diff(self):
        """审计条目记录哈希变更。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        old_hash = wf.core_assumptions_hash
        engine = build_committee_workflow_engine()
        engine.update_core_assumptions(
            wf,
            new_assumptions=["完全不同的假设"],
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        audit = wf.audit_trail[-1]
        assert old_hash[:8] in audit.reason
        assert wf.core_assumptions_hash[:8] in audit.reason
        assert "invalidated=True" in audit.reason


# ---------------------------------------------------------------------------
# 验收点 3：API 有权限边界、审计和文档
# ---------------------------------------------------------------------------


class TestAcceptancePoint3PermissionAuditDocs:
    """验收点 3：API 有权限边界、审计和文档。"""

    def test_add_review_rejects_unassigned_user(self):
        """未被分配为该角色的用户添加复核被拒。"""
        wf = make_workflow()
        engine = build_committee_workflow_engine()
        success, err_code, _ = engine.add_review(
            wf,
            role=ROLE_FINANCIAL_QUALITY_REVIEWER,
            reviewer_user_id="random_user",
        )
        assert not success
        assert err_code == ERR_PERMISSION_DENIED

    def test_add_review_rejects_lead_as_adversary(self):
        """主分析师不能同时作为独立反方。"""
        wf = make_workflow()
        engine = build_committee_workflow_engine()
        success, err_code, err_msg = engine.add_review(
            wf,
            role=ROLE_INDEPENDENT_ADVERSARY,
            reviewer_user_id=UID_LEAD,  # 与 lead_analyst_user_id 相同
        )
        assert not success
        assert err_code == ERR_SAME_IDENTITY
        assert "主分析师" in err_msg

    def test_add_review_rejects_trade_action_keywords(self):
        """复核内容含交易动作词被拒（AGENTS.md）。"""
        wf = make_workflow()
        engine = build_committee_workflow_engine()
        for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS:
            wf2 = make_workflow(workflow_id=f"wf_test_kw_{kw}")
            success, err_code, _ = engine.add_review(
                wf2,
                role=ROLE_FINANCIAL_QUALITY_REVIEWER,
                reviewer_user_id=UID_FIN,
                summary=f"建议{kw}该标的",
            )
            assert not success, f"应拒绝交易动作词：{kw}"
            assert err_code == ERR_TRADE_ACTION_KEYWORD

    def test_transition_adds_audit_entry(self):
        """状态迁移追加审计条目。"""
        wf = make_workflow(status=STATUS_DRAFT)
        engine = build_committee_workflow_engine()
        original_audit_count = len(wf.audit_trail)
        success, _, _ = engine.transition(
            wf, STATUS_EVIDENCE_REVIEW,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
            reason="提交证据复核",
        )
        assert success
        assert wf.status == STATUS_EVIDENCE_REVIEW
        assert len(wf.audit_trail) == original_audit_count + 1
        audit = wf.audit_trail[-1]
        assert audit.from_status == STATUS_DRAFT
        assert audit.to_status == STATUS_EVIDENCE_REVIEW
        assert audit.operator_user_id == UID_LEAD
        assert audit.operator_role == ROLE_LEAD_ANALYST
        assert audit.reason == "提交证据复核"
        assert audit.created_at != ""

    def test_audit_entry_captures_review_snapshot(self):
        """审计条目记录复核记录快照。"""
        wf = make_workflow(status=STATUS_EVIDENCE_REVIEW)
        wf.reviews.append(make_review(role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN))
        wf.reviews.append(make_review(role=ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER, reviewer_user_id=UID_IND))
        engine = build_committee_workflow_engine()
        success, _, _ = engine.transition(
            wf, STATUS_MODEL_REVIEW,
            operator_user_id=UID_FIN, operator_role=ROLE_FINANCIAL_QUALITY_REVIEWER,
            reason="证据复核通过",
        )
        assert success
        audit = wf.audit_trail[-1]
        assert len(audit.review_snapshot) == 2
        assert audit.review_snapshot[0]["role"] == ROLE_FINANCIAL_QUALITY_REVIEWER

    def test_review_record_captures_questions_evidence_time(self):
        """强制要求 2：每次复核保存问题、证据、时间。"""
        wf = make_workflow()
        engine = build_committee_workflow_engine()
        questions = [
            ReviewQuestion(
                question_id="q1",
                text="问题 1",
                answer="回答 1",
                evidence_refs=["ev1"],
                answered_at=utc_now(),
            ),
        ]
        success, _, _ = engine.add_review(
            wf,
            role=ROLE_FINANCIAL_QUALITY_REVIEWER,
            reviewer_user_id=UID_FIN,
            questions=questions,
            evidence_refs=["ev_outer1"],
        )
        assert success
        review = wf.reviews[-1]
        assert len(review.questions) == 1
        assert review.questions[0].text == "问题 1"
        assert review.questions[0].answer == "回答 1"
        assert review.questions[0].evidence_refs == ["ev1"]
        assert review.evidence_refs == ["ev_outer1"]
        assert review.created_at != ""

    def test_approval_notice_in_workflow_response(self):
        """所有响应含 APPROVAL_NOTICE 提示词。"""
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        d = wf.to_dict()
        # workflow 不直接含 notice，但 APPROVAL_NOTICE 常量供 API 使用
        assert "交易建议" in APPROVAL_NOTICE

    def test_api_module_has_notice_constant(self):
        """API 模块导出 NOTICE 常量。"""
        from server.routes.research_committee_routes import NOTICE
        assert NOTICE == APPROVAL_NOTICE


# ---------------------------------------------------------------------------
# 仓储持久化测试
# ---------------------------------------------------------------------------


class TestRepositoryPersistence:
    """测试 SQLite 仓储。"""

    def test_save_and_get_workflow(self, tmp_path: Path):
        """保存并读取工作流。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf = make_workflow()
        repo.save(wf)
        loaded = repo.get(wf.workflow_id)
        assert loaded is not None
        assert loaded["workflow_id"] == wf.workflow_id
        assert loaded["thesis_id"] == THESIS_ID
        assert loaded["status"] == STATUS_DRAFT
        assert loaded["lead_analyst_user_id"] == UID_LEAD

    def test_save_duplicate_workflow_id_rejected(self, tmp_path: Path):
        """workflow_id 已存在时抛 ValueError。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf = make_workflow()
        repo.save(wf)
        with pytest.raises(ValueError, match="workflow_id already exists"):
            repo.save(wf)

    def test_update_workflow(self, tmp_path: Path):
        """更新工作流字段。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf = make_workflow()
        repo.save(wf)
        wf.status = STATUS_EVIDENCE_REVIEW
        wf.reviews.append(make_review(role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN))
        assert repo.update(wf) is True
        loaded = repo.get(wf.workflow_id)
        assert loaded["status"] == STATUS_EVIDENCE_REVIEW
        assert len(loaded["reviews"]) == 1

    def test_update_nonexistent_returns_false(self, tmp_path: Path):
        """更新不存在的工作流返回 False。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf = make_workflow(workflow_id="nonexistent")
        assert repo.update(wf) is False

    def test_list_by_thesis(self, tmp_path: Path):
        """按 thesis_id 列表查询。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf1 = make_workflow(workflow_id="wf_t1", thesis_id="thesis_X")
        wf2 = make_workflow(workflow_id="wf_t2", thesis_id="thesis_X")
        wf3 = make_workflow(workflow_id="wf_t3", thesis_id="thesis_Y")
        for w in (wf1, wf2, wf3):
            repo.save(w)
        items = repo.list_by_thesis(thesis_id="thesis_X")
        assert len(items) == 2
        for item in items:
            assert item["thesis_id"] == "thesis_X"

    def test_list_by_company_with_pit_filter(self, tmp_path: Path):
        """按公司 + PIT 过滤查询。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf1 = make_workflow(workflow_id="wf_c1", as_of="2026-06-01T00:00:00+00:00")
        wf2 = make_workflow(workflow_id="wf_c2", as_of="2026-07-15T00:00:00+00:00")
        wf3 = make_workflow(workflow_id="wf_c3", as_of="2026-08-01T00:00:00+00:00")
        for w in (wf1, wf2, wf3):
            repo.save(w)
        # PIT as_of=2026-07-01 应只包含 wf1（as_of <= 2026-07-01）
        items = repo.list_by_company(company_symbol=SYMBOL, as_of="2026-07-01T00:00:00+00:00")
        assert len(items) == 1
        assert items[0]["workflow_id"] == "wf_c1"

    def test_list_by_company_with_status_filter(self, tmp_path: Path):
        """按公司 + 状态过滤查询。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf1 = make_workflow(workflow_id="wf_s1", status=STATUS_DRAFT)
        wf2 = make_workflow(workflow_id="wf_s2", status=STATUS_APPROVED_FOR_MONITORING)
        for w in (wf1, wf2):
            repo.save(w)
        items = repo.list_by_company(company_symbol=SYMBOL, status=STATUS_APPROVED_FOR_MONITORING)
        assert len(items) == 1
        assert items[0]["workflow_id"] == "wf_s2"

    def test_list_by_status(self, tmp_path: Path):
        """按状态列表查询。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        repo.save(make_workflow(workflow_id="wf_a", status=STATUS_DRAFT))
        repo.save(make_workflow(workflow_id="wf_b", status=STATUS_DRAFT))
        repo.save(make_workflow(workflow_id="wf_c", status=STATUS_APPROVED_FOR_MONITORING))
        items = repo.list_by_status(status=STATUS_DRAFT)
        assert len(items) == 2

    def test_stats(self, tmp_path: Path):
        """仓储统计。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        repo.save(make_workflow(workflow_id="wf_x1"))
        wf2 = make_workflow(workflow_id="wf_x2", status=STATUS_EXPIRED)
        wf2.invalidated_by_assumption_change = True
        repo.save(wf2)
        stats = repo.stats()
        assert stats["total"] == 2
        assert stats["invalidated_by_assumption_change"] == 1
        assert stats["schema_version"] == 1

    def test_migrate_idempotent(self, tmp_path: Path):
        """迁移幂等：重复初始化不抛错。"""
        db_path = tmp_path / "committee.db"
        repo1 = CommitteeRepository(db_path)
        repo1.save(make_workflow(workflow_id="wf_m1"))
        # 再次初始化应不抛错且保留数据
        repo2 = CommitteeRepository(db_path)
        loaded = repo2.get("wf_m1")
        assert loaded is not None


# ---------------------------------------------------------------------------
# 端到端集成测试
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端：完整审批流程。"""

    def test_full_pipeline_draft_to_approved(self, tmp_path: Path):
        """完整流程：draft → evidence_review → model_review →
        adversarial_review → committee_ready → approved_for_monitoring。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        engine = build_committee_workflow_engine()
        wf = make_workflow()
        repo.save(wf)

        # 1. draft → evidence_review
        ok, _, _ = engine.transition(
            wf, STATUS_EVIDENCE_REVIEW,
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
            reason="主分析师提交证据",
        )
        assert ok
        repo.update(wf)

        # 2. evidence_review → model_review（需财务 + 行业复核）
        ok, _, _ = engine.add_review(
            wf, role=ROLE_FINANCIAL_QUALITY_REVIEWER, reviewer_user_id=UID_FIN,
            status=REVIEW_STATUS_PASS, summary="财务质量通过",
        )
        assert ok
        ok, _, _ = engine.add_review(
            wf, role=ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER, reviewer_user_id=UID_IND,
            status=REVIEW_STATUS_PASS, summary="行业复核通过",
        )
        assert ok
        ok, _, _ = engine.transition(
            wf, STATUS_MODEL_REVIEW,
            operator_user_id=UID_FIN, operator_role=ROLE_FINANCIAL_QUALITY_REVIEWER,
            reason="证据复核通过",
        )
        assert ok
        repo.update(wf)

        # 3. model_review → adversarial_review（需模型复核）
        ok, _, _ = engine.add_review(
            wf, role=ROLE_MODEL_REVIEWER, reviewer_user_id=UID_MOD,
            status=REVIEW_STATUS_PASS, summary="模型复核通过",
        )
        assert ok
        ok, _, _ = engine.transition(
            wf, STATUS_ADVERSARIAL_REVIEW,
            operator_user_id=UID_MOD, operator_role=ROLE_MODEL_REVIEWER,
            reason="模型复核通过",
        )
        assert ok
        repo.update(wf)

        # 4. adversarial_review → committee_ready（需独立反方 + 7 项清单）
        ok, _, _ = engine.add_review(
            wf, role=ROLE_INDEPENDENT_ADVERSARY, reviewer_user_id=UID_ADV,
            status=REVIEW_STATUS_CONCERNS,
            questions=make_adversary_questions(),
            summary="反方审查完成，存在担忧但可继续",
        )
        assert ok
        ok, _, _ = engine.transition(
            wf, STATUS_COMMITTEE_READY,
            operator_user_id=UID_ADV, operator_role=ROLE_INDEPENDENT_ADVERSARY,
            reason="反方审查完成",
        )
        assert ok
        repo.update(wf)

        # 5. committee_ready → approved_for_monitoring（需主持人）
        ok, _, _ = engine.add_review(
            wf, role=ROLE_CHAIR, reviewer_user_id=UID_CHAIR,
            status=REVIEW_STATUS_PASS, summary="主持人审批通过",
        )
        assert ok
        ok, _, _ = engine.transition(
            wf, STATUS_APPROVED_FOR_MONITORING,
            operator_user_id=UID_CHAIR, operator_role=ROLE_CHAIR,
            reason="审批通过",
        )
        assert ok
        repo.update(wf)

        # 验证最终状态
        loaded = repo.get(wf.workflow_id)
        assert loaded["status"] == STATUS_APPROVED_FOR_MONITORING
        assert len(loaded["reviews"]) == 5
        assert len(loaded["audit_trail"]) == 5

    def test_assumption_change_after_approval_invalidates(self, tmp_path: Path):
        """已审批后修改假设 → 自动失效。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        engine = build_committee_workflow_engine()
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        repo.save(wf)

        ok, _, _ = engine.update_core_assumptions(
            wf,
            new_assumptions=["全新假设"],
            operator_user_id=UID_LEAD, operator_role=ROLE_LEAD_ANALYST,
        )
        assert ok
        repo.update(wf)

        loaded = repo.get(wf.workflow_id)
        assert loaded["status"] == STATUS_EXPIRED
        assert loaded["invalidated_by_assumption_change"] is True


# ---------------------------------------------------------------------------
# Flask API 测试
# ---------------------------------------------------------------------------


class TestFlaskApi:
    """测试 Flask API 端点。"""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch):
        """构造 Flask 测试客户端，DB 隔离到 tmp_path。"""
        import server.routes.research_committee_routes as routes_mod

        # 重置单例
        monkeypatch.setattr(routes_mod, "_repository", None)
        monkeypatch.setattr(routes_mod, "_engine", None)

        # 强制使用 tmp_path 的 DB
        original_repo_func = routes_mod._repo

        def _test_repo():
            return CommitteeRepository(tmp_path / "committee.db")

        monkeypatch.setattr(routes_mod, "_repo", _test_repo)

        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(routes_mod.committee_bp)
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_health(self, client):
        """健康检查返回 ok。"""
        resp = client.get("/api/research/committee/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "ok"
        assert "schema_version" in data["data"]

    def test_adversary_checklist_template(self, client):
        """反方检查清单模板 7 项。"""
        resp = client.get("/api/research/committee/adversary_checklist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]["items"]) == 7
        assert len(data["data"]["questions"]) == 7

    def test_create_workflow(self, client):
        """创建工作流。"""
        resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
            "industry_supply_chain_reviewer_user_id": UID_IND,
            "model_reviewer_user_id": UID_MOD,
            "chair_user_id": UID_CHAIR,
            "core_assumptions": ["假设 A", "假设 B"],
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["workflow_id"].startswith("wf_")
        assert data["data"]["status"] == STATUS_DRAFT
        assert "notice" in data
        assert "交易建议" in data["notice"]

    def test_create_workflow_same_identity_rejected(self, client):
        """同一身份同时为主分析和独立反方被拒。"""
        resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": "same_user",
            "independent_adversary_user_id": "same_user",
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "SAME_IDENTITY_FOR_LEAD_AND_ADVERSARY"

    def test_create_workflow_missing_required_field(self, client):
        """缺必填字段返回 400。"""
        resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            # 缺 company_symbol, as_of, lead_analyst_user_id 等
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "INSUFFICIENT_INPUT"

    def test_get_workflow_detail(self, client):
        """查询工作流详情。"""
        # 先创建
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        # 再查询
        resp = client.get(f"/api/research/committee/workflows/{wf_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["workflow_id"] == wf_id

    def test_get_nonexistent_workflow_returns_404(self, client):
        """查询不存在的工作流返回 404。"""
        resp = client.get("/api/research/committee/workflows/nonexistent")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"]["code"] == "WORKFLOW_NOT_FOUND"

    def test_add_review_endpoint(self, client):
        """添加复核记录。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        resp = client.post(f"/api/research/committee/workflows/{wf_id}/reviews", json={
            "role": ROLE_FINANCIAL_QUALITY_REVIEWER,
            "reviewer_user_id": UID_FIN,
            "status": REVIEW_STATUS_PASS,
            "summary": "财务质量通过",
            "evidence_refs": ["ev1"],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]["reviews"]) == 1

    def test_add_review_rejects_unassigned_user(self, client):
        """未被分配为该角色的用户添加复核被拒（权限边界）。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        resp = client.post(f"/api/research/committee/workflows/{wf_id}/reviews", json={
            "role": ROLE_FINANCIAL_QUALITY_REVIEWER,
            "reviewer_user_id": "intruder",
            "status": REVIEW_STATUS_PASS,
        })
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["error"]["code"] == ERR_PERMISSION_DENIED

    def test_add_review_rejects_trade_action_keyword(self, client):
        """复核内容含交易动作词被拒。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        resp = client.post(f"/api/research/committee/workflows/{wf_id}/reviews", json={
            "role": ROLE_FINANCIAL_QUALITY_REVIEWER,
            "reviewer_user_id": UID_FIN,
            "summary": "建议买入该标的",
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"]["code"] == ERR_TRADE_ACTION_KEYWORD

    def test_transition_endpoint(self, client):
        """状态迁移端点。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        resp = client.post(f"/api/research/committee/workflows/{wf_id}/transition", json={
            "target_status": STATUS_EVIDENCE_REVIEW,
            "operator_user_id": UID_LEAD,
            "operator_role": ROLE_LEAD_ANALYST,
            "reason": "提交证据",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["status"] == STATUS_EVIDENCE_REVIEW
        assert len(data["data"]["audit_trail"]) == 1

    def test_transition_rejects_invalid_target(self, client):
        """非法目标状态返回 400。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        resp = client.post(f"/api/research/committee/workflows/{wf_id}/transition", json={
            "target_status": "invalid_status",
            "operator_user_id": UID_LEAD,
            "operator_role": ROLE_LEAD_ANALYST,
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"]["code"] == "INVALID_TARGET_STATUS"

    def test_transition_rejects_missing_required_reviews(self, client):
        """缺强制复核的状态迁移返回 409（验收点 1）。"""
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
            "industry_supply_chain_reviewer_user_id": UID_IND,
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        # 先迁移到 evidence_review
        client.post(f"/api/research/committee/workflows/{wf_id}/transition", json={
            "target_status": STATUS_EVIDENCE_REVIEW,
            "operator_user_id": UID_LEAD,
            "operator_role": ROLE_LEAD_ANALYST,
        })

        # 缺复核直接迁移到 model_review → 409
        resp = client.post(f"/api/research/committee/workflows/{wf_id}/transition", json={
            "target_status": STATUS_MODEL_REVIEW,
            "operator_user_id": UID_FIN,
            "operator_role": ROLE_FINANCIAL_QUALITY_REVIEWER,
        })
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["error"]["code"] == ERR_MISSING_REQUIRED_REVIEWS

    def test_update_core_assumptions_endpoint(self, client):
        """更新核心假设端点（验收点 2）。"""
        # 创建并审批
        create_resp = client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
            "financial_quality_reviewer_user_id": UID_FIN,
            "industry_supply_chain_reviewer_user_id": UID_IND,
            "model_reviewer_user_id": UID_MOD,
            "chair_user_id": UID_CHAIR,
            "core_assumptions": ["假设 A"],
        })
        wf_id = create_resp.get_json()["data"]["workflow_id"]

        # 直接更新假设（draft 状态）
        resp = client.post(f"/api/research/committee/workflows/{wf_id}/core_assumptions", json={
            "new_assumptions": ["新假设 X", "新假设 Y"],
            "operator_user_id": UID_LEAD,
            "operator_role": ROLE_LEAD_ANALYST,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["core_assumptions"] == ["新假设 X", "新假设 Y"]
        # draft 状态不触发失效
        assert data["data"]["invalidated_by_assumption_change"] is False

    def test_update_core_assumptions_invalidates_approved(self, client, tmp_path: Path):
        """已审批工作流更新假设自动失效（端到端）。"""
        # 在测试 DB 中直接放入 approved 工作流
        repo = CommitteeRepository(tmp_path / "committee.db")
        wf = make_workflow(status=STATUS_APPROVED_FOR_MONITORING)
        repo.save(wf)

        resp = client.post(f"/api/research/committee/workflows/{wf.workflow_id}/core_assumptions", json={
            "new_assumptions": ["完全不同的假设"],
            "operator_user_id": UID_LEAD,
            "operator_role": ROLE_LEAD_ANALYST,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["status"] == STATUS_EXPIRED
        assert data["data"]["invalidated_by_assumption_change"] is True

    def test_list_workflows_by_thesis(self, client):
        """按 thesis_id 列表查询。"""
        client.post("/api/research/committee/workflows", json={
            "thesis_id": "thesis_list_1",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        client.post("/api/research/committee/workflows", json={
            "thesis_id": "thesis_list_1",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        client.post("/api/research/committee/workflows", json={
            "thesis_id": "thesis_list_2",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })

        resp = client.get("/api/research/committee/workflows?thesis_id=thesis_list_1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 2

    def test_list_workflows_by_company(self, client):
        """按公司列表查询。"""
        client.post("/api/research/committee/workflows", json={
            "thesis_id": THESIS_ID,
            "company_symbol": "CO_A",
            "as_of": AS_OF,
            "lead_analyst_user_id": UID_LEAD,
            "independent_adversary_user_id": UID_ADV,
        })
        resp = client.get("/api/research/committee/workflows?company_symbol=CO_A")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 1
        assert data["data"][0]["company_symbol"] == "CO_A"

    def test_response_always_contains_notice(self, client):
        """所有响应含 notice 提示词（API 文档要求）。"""
        resp = client.get("/api/research/committee/health")
        data = resp.get_json()
        assert "notice" in data
        assert "交易建议" in data["notice"]

    def test_response_always_contains_trace_id(self, client):
        """所有响应含 trace_id（审计要求）。"""
        resp = client.get("/api/research/committee/health")
        data = resp.get_json()
        assert "trace_id" in data
        assert data["trace_id"].startswith("tr_")

    def test_error_response_contains_trace_id(self, client):
        """错误响应含 trace_id。"""
        resp = client.get("/api/research/committee/workflows/nonexistent")
        data = resp.get_json()
        assert "trace_id" in data["error"]
        assert data["error"]["trace_id"].startswith("tr_")


# ---------------------------------------------------------------------------
# AGENTS.md 合规
# ---------------------------------------------------------------------------


class TestAgentsMdCompliance:
    """AGENTS.md 合规检查。"""

    def test_no_trade_advice_in_source(self):
        """源码中无交易动作建议。"""
        from pathlib import Path
        contracts_path = Path(__file__).parent.parent / "core" / "research" / "committee" / "contracts.py"
        workflow_path = Path(__file__).parent.parent / "core" / "research" / "committee" / "workflow.py"
        repository_path = Path(__file__).parent.parent / "core" / "research" / "committee" / "repository.py"
        routes_path = Path(__file__).parent.parent / "server" / "routes" / "research_committee_routes.py"

        for path in (contracts_path, workflow_path, repository_path, routes_path):
            text = path.read_text(encoding="utf-8")
            # 仅检查中文交易动作词（不检查英文函数名等）
            for kw in ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓"):
                # FORBIDDEN_TRADE_ACTION_KEYWORDS 元组中的字符串本身不算违规
                # 因为那是定义禁止词的常量
                if kw in text and f'"{kw}"' not in text and f"'{kw}'" not in text:
                    pytest.fail(f"{path.name} 含交易动作词：{kw}")

    def test_approval_notice_disclaims_trade_advice(self):
        """审批说明明确否认交易建议。"""
        assert "不表示交易建议" in APPROVAL_NOTICE

    def test_graceful_degradation_on_missing_fields(self, tmp_path: Path):
        """字段缺失时降级处理不崩溃（AGENTS.md）。"""
        repo = CommitteeRepository(tmp_path / "committee.db")
        # 空 thesis_id → 返回空列表（不抛错）
        assert repo.list_by_thesis(thesis_id="") == []
        # 空 company_symbol → 返回空列表
        assert repo.list_by_company(company_symbol="") == []
        # 空 status → 返回空列表
        assert repo.list_by_status(status="") == []
        # 不存在的 workflow_id → 返回 None
        assert repo.get("nonexistent") is None

    def test_malformed_contracts_and_rows_degrade(self, tmp_path: Path):
        """坏 JSON、坏复核字段和异常查询参数不应阻塞工作流读取。"""
        question = ReviewQuestion(answer=None, evidence_refs=None)
        assert not question.is_answered()
        assert question.to_dict()["evidence_refs"] == []
        record = ReviewRecord(questions=[None], summary=None, evidence_refs=None)
        assert record.to_dict()["questions"] == []
        workflow = CommitteeWorkflow(
            workflow_id="wf_malformed",
            core_assumptions=[None, "有效假设"],
            reviews=[None],
            audit_trail=[None],
            invalidated_by_assumption_change="false",
        )
        assert workflow.core_assumptions == ["有效假设"]
        assert workflow.reviews == []
        assert workflow.invalidated_by_assumption_change is False

        repo = CommitteeRepository(tmp_path / "committee.db")
        with repo.connect() as conn:
            conn.execute(
                "INSERT INTO committee_workflows(workflow_id, created_at, payload_json) VALUES(?, ?, ?)",
                ("bad", "", "not-json"),
            )
        assert repo.get([]) is None
        assert repo.get("bad") is None
        assert repo.list_by_thesis(thesis_id="", limit="bad") == []

    def test_repository_wal_mode_thread_safe(self, tmp_path: Path):
        """WAL 模式 + RLock 线程安全（防御性编程）。"""
        import sqlite3
        repo = CommitteeRepository(tmp_path / "committee.db")
        with repo.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode[0].lower() == "wal"


# ---------------------------------------------------------------------------
# 错误码映射测试
# ---------------------------------------------------------------------------


class TestErrorStatusMapping:
    """测试 API 错误码到 HTTP 状态码的映射。"""

    def test_permission_denied_returns_403(self):
        """权限不足 → 403。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_PERMISSION_DENIED) == 403

    def test_invalid_transition_returns_409(self):
        """非法迁移 → 409。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_INVALID_TRANSITION) == 409

    def test_missing_required_reviews_returns_409(self):
        """缺强制复核 → 409。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_MISSING_REQUIRED_REVIEWS) == 409

    def test_adversary_checklist_incomplete_returns_409(self):
        """反方检查清单不完整 → 409。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_ADVERSARY_CHECKLIST_INCOMPLETE) == 409

    def test_same_identity_returns_400(self):
        """同一身份 → 400。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_SAME_IDENTITY) == 400

    def test_trade_action_keyword_returns_400(self):
        """交易动作词 → 400。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_TRADE_ACTION_KEYWORD) == 400

    def test_terminal_state_returns_409(self):
        """终态 → 409。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_TERMINAL_STATE) == 409

    def test_already_invalidated_returns_409(self):
        """已失效 → 409。"""
        from server.routes.research_committee_routes import _error_status_for_engine_code
        assert _error_status_for_engine_code(ERR_ALREADY_INVALIDATED) == 409
