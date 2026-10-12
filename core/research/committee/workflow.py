"""P2.7 投委会工作流引擎。

按 P2 计划 §242-279 Task P2.7：
- 状态机：draft → evidence_review → model_review → adversarial_review
        → committee_ready → approved_for_monitoring / returned / rejected / expired
- 强制要求：
  1. 同一执行身份不得同时完成主分析和独立反方签署
  2. 每次状态变化保存问题、回答、证据和时间
  3. 反方必须检查 7 项
  4. 审批仅表示研究材料达到监控标准，不表示交易建议

验收点：
- 缺任一强制复核不能进入 approved_for_monitoring
- 修改核心假设后旧审批自动失效
- API 有权限边界、审计和文档
"""
from __future__ import annotations

from .contracts import (
    ADVERSARY_CHECK_ITEMS,
    ADVERSARY_CHECK_LABELS,
    AuditEntry,
    CommitteeWorkflow,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    REQUIRED_REVIEWS_FOR_TRANSITION,
    REVIEW_ROLES,
    REVIEW_STATUSES,
    ROLE_CHAIR,
    ROLE_INDEPENDENT_ADVERSARY,
    ROLE_LEAD_ANALYST,
    ReviewQuestion,
    ReviewRecord,
    REVIEW_STATUS_CONCERNS,
    STATUS_ADVERSARIAL_REVIEW,
    STATUS_APPROVED_FOR_MONITORING,
    STATUS_COMMITTEE_READY,
    STATUS_EXPIRED,
    STATUS_REJECTED,
    STATUS_RETURNED,
    TERMINAL_STATUSES,
    TRANSITIONS,
    _safe_text,
    utc_now,
    _text_list,
    _hash_assumptions,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_INVALID_TRANSITION: str = "invalid_transition"
ERR_MISSING_REQUIRED_REVIEWS: str = "missing_required_reviews"
ERR_SAME_IDENTITY: str = "same_identity_for_lead_and_adversary"
ERR_ADVERSARY_CHECKLIST_INCOMPLETE: str = "adversary_checklist_incomplete"
ERR_TRADE_ACTION_KEYWORD: str = "trade_action_keyword"
ERR_PERMISSION_DENIED: str = "permission_denied"
ERR_ALREADY_INVALIDATED: str = "already_invalidated"
ERR_TERMINAL_STATE: str = "terminal_state"


# ---------------------------------------------------------------------------
# 反方检查清单模板
# ---------------------------------------------------------------------------


def build_adversary_checklist_template() -> list[ReviewQuestion]:
    """构造反方检查清单 7 项模板。

    强制要求 3：反方必须检查替代技术、需求不及预期、价格下降、成本上升、竞争、治理和估值假设。
    """
    return [
        ReviewQuestion(
            question_id=f"adv_check_{item}",
            text=f"请评估【{ADVERSARY_CHECK_LABELS[item]}】风险：是否存在该风险？具体场景与证据？",
        )
        for item in ADVERSARY_CHECK_ITEMS
    ]


def is_adversary_checklist_complete(questions: list[ReviewQuestion]) -> tuple[bool, list[str]]:
    """检查反方检查清单是否完整。

    Returns:
        (is_complete, missing_items)
    """
    answered_items: set[str] = set()
    questions = questions if isinstance(questions, list) else []
    for q in questions:
        if not isinstance(q, ReviewQuestion):
            continue
        # 从 question_id 提取 item key
        for item in ADVERSARY_CHECK_ITEMS:
            if q.question_id == f"adv_check_{item}" and q.is_answered():
                answered_items.add(item)
                break
    missing = [item for item in ADVERSARY_CHECK_ITEMS if item not in answered_items]
    return len(missing) == 0, missing


def _review_snapshot(workflow: CommitteeWorkflow) -> list[dict[str, object]]:
    """只把有效复核写入审计快照，避免外部污染阻塞状态迁移。"""
    reviews = workflow.reviews if isinstance(workflow.reviews, list) else []
    return [review.to_dict() for review in reviews if isinstance(review, ReviewRecord)]


# ---------------------------------------------------------------------------
# 工作流引擎
# ---------------------------------------------------------------------------


class CommitteeWorkflowEngine:
    """投委会工作流引擎。

    状态机校验 + 角色权限 + 强制复核 + 假设变更失效 + 审计追踪。
    """

    def can_transition(
        self,
        workflow: CommitteeWorkflow,
        target_status: str,
        *,
        operator_user_id: str,
        operator_role: str,
    ) -> tuple[bool, str, str]:
        """检查是否可以从当前状态迁移到目标状态。

        Args:
            workflow: 工作流对象
            target_status: 目标状态
            operator_user_id: 操作人用户 ID
            operator_role: 操作人角色

        Returns:
            (can_transition, error_code, error_message)
        """
        if not isinstance(workflow, CommitteeWorkflow) or not isinstance(target_status, str):
            return False, ERR_INVALID_TRANSITION, "workflow 或目标状态无效"
        if not isinstance(workflow.status, str):
            return False, ERR_INVALID_TRANSITION, "workflow 当前状态无效"

        # 终态不可迁移
        if workflow.status in TERMINAL_STATUSES:
            return False, ERR_TERMINAL_STATE, f"workflow 已处于终态：{workflow.status}"

        # 已被假设变更失效的工作流不可再迁移
        if workflow.invalidated_by_assumption_change:
            return False, ERR_ALREADY_INVALIDATED, "workflow 已被假设变更失效"

        # 校验迁移合法性
        allowed = TRANSITIONS.get(workflow.status, ())
        if target_status not in allowed:
            return False, ERR_INVALID_TRANSITION, (
                f"不允许的状态迁移：{workflow.status} → {target_status}"
            )

        # 退回 / 拒绝 / 过期不需要强制复核，但仍需操作权限
        if target_status in (STATUS_RETURNED, STATUS_REJECTED, STATUS_EXPIRED):
            # 任何被分配的角色都可以触发退回/拒绝/过期
            if not self._is_operator_assigned(workflow, operator_user_id, operator_role):
                return False, ERR_PERMISSION_DENIED, (
                    f"操作人 {operator_user_id} 未被分配为角色 {operator_role}"
                )
            return True, "", ""

        # 进入下一阶段的迁移：校验强制复核
        key = (workflow.status, target_status)
        required_roles = REQUIRED_REVIEWS_FOR_TRANSITION.get(key, ())
        for role in required_roles:
            if not workflow.has_review_by_role(role):
                return False, ERR_MISSING_REQUIRED_REVIEWS, (
                    f"缺少角色 {role} 的通过复核（{workflow.status} → {target_status}）"
                )

        # 反方审查 → 投委会就绪：额外校验 7 项检查清单完整
        if (
            workflow.status == STATUS_ADVERSARIAL_REVIEW
            and target_status == STATUS_COMMITTEE_READY
        ):
            adversary_reviews = workflow.get_reviews_by_role(ROLE_INDEPENDENT_ADVERSARY)
            all_questions: list[ReviewQuestion] = []
            for r in adversary_reviews:
                all_questions.extend(q for q in r.questions if isinstance(q, ReviewQuestion))
            is_complete, missing = is_adversary_checklist_complete(all_questions)
            if not is_complete:
                return False, ERR_ADVERSARY_CHECKLIST_INCOMPLETE, (
                    f"反方检查清单不完整，缺失：{', '.join(ADVERSARY_CHECK_LABELS[m] for m in missing)}"
                )

            # 强制要求 1：同一执行身份不得同时完成主分析和独立反方签署
            for r in adversary_reviews:
                if (
                    r.reviewer_user_id
                    and r.reviewer_user_id == workflow.lead_analyst_user_id
                ):
                    return False, ERR_SAME_IDENTITY, (
                        f"独立反方 {r.reviewer_user_id} 与主分析师相同（违反强制要求 1）"
                    )

        # 操作权限校验
        if not self._is_operator_assigned(workflow, operator_user_id, operator_role):
            return False, ERR_PERMISSION_DENIED, (
                f"操作人 {operator_user_id} 未被分配为角色 {operator_role}"
            )

        return True, "", ""

    def transition(
        self,
        workflow: CommitteeWorkflow,
        target_status: str,
        *,
        operator_user_id: str,
        operator_role: str,
        reason: str = "",
    ) -> tuple[bool, str, str]:
        """执行状态迁移。

        Args:
            workflow: 工作流对象（将被原地修改）
            target_status: 目标状态
            operator_user_id: 操作人用户 ID
            operator_role: 操作人角色
            reason: 操作原因

        Returns:
            (success, error_code, error_message)
        """
        safe_reason = _safe_text(reason)
        if any(keyword in safe_reason for keyword in FORBIDDEN_TRADE_ACTION_KEYWORDS):
            return False, ERR_TRADE_ACTION_KEYWORD, "审计原因含交易动作关键词"
        can, err_code, err_msg = self.can_transition(
            workflow,
            target_status,
            operator_user_id=operator_user_id,
            operator_role=operator_role,
        )
        if not can:
            return False, err_code, err_msg

        from_status = workflow.status
        workflow.status = target_status
        workflow.updated_at = utc_now()

        # 添加审计条目
        audit = AuditEntry(
            audit_id=f"audit_{workflow.workflow_id}_{len(workflow.audit_trail)+1}",
            workflow_id=workflow.workflow_id,
            from_status=from_status,
            to_status=target_status,
            operator_user_id=operator_user_id,
            operator_role=operator_role,
            reason=reason,
            review_snapshot=_review_snapshot(workflow),
            created_at=workflow.updated_at,
        )
        workflow.audit_trail.append(audit)
        return True, "", ""

    def add_review(
        self,
        workflow: CommitteeWorkflow,
        *,
        role: str,
        reviewer_user_id: str,
        reviewer_display_name: str = "",
        status: str = REVIEW_STATUS_CONCERNS,
        questions: list[ReviewQuestion] | None = None,
        summary: str = "",
        evidence_refs: list[str] | None = None,
    ) -> tuple[bool, str, str]:
        """添加复核记录。

        强制要求 2：每次状态变化保存问题、回答、证据和时间。
        强制要求 1：同一执行身份不得同时完成主分析和独立反方签署。
        AGENTS.md：复核内容不得含交易动作词。

        Args:
            workflow: 工作流对象（将被原地修改）
            role: 复核角色
            reviewer_user_id: 复核人用户 ID
            reviewer_display_name: 复核人显示名
            status: 复核状态（pass / fail / concerns / blocked）
            questions: 问题列表
            summary: 复核总结
            evidence_refs: 证据 ID 列表

        Returns:
            (success, error_code, error_message)
        """
        if not isinstance(workflow, CommitteeWorkflow):
            return False, ERR_PERMISSION_DENIED, "workflow 类型无效"
        if role not in REVIEW_ROLES:
            return False, ERR_PERMISSION_DENIED, f"非法角色：{role}"
        if status not in REVIEW_STATUSES:
            return False, ERR_PERMISSION_DENIED, f"非法复核状态：{status}"

        # 强制要求 1：独立反方不能与主分析师相同（先于权限检查，捕获身份冲突）
        if role == ROLE_INDEPENDENT_ADVERSARY and reviewer_user_id == workflow.lead_analyst_user_id:
            return False, ERR_SAME_IDENTITY, (
                "独立反方与主分析师不得为同一用户（违反强制要求 1）"
            )

        # 校验 reviewer_user_id 是否被分配为该角色
        if not self._is_user_assigned_to_role(workflow, reviewer_user_id, role):
            return False, ERR_PERMISSION_DENIED, (
                f"用户 {reviewer_user_id} 未被分配为角色 {role}"
            )

        # 校验复核内容不含交易动作词
        record = ReviewRecord(
            review_id=f"rev_{workflow.workflow_id}_{len(workflow.reviews)+1}",
            workflow_id=workflow.workflow_id,
            role=role,
            reviewer_user_id=reviewer_user_id,
            reviewer_display_name=reviewer_display_name,
            status=status,
            questions=list(questions) if isinstance(questions, (list, tuple)) else [],
            summary=summary,
            evidence_refs=_text_list(evidence_refs),
            created_at=utc_now(),
        )
        if record.contains_trade_action():
            return False, ERR_TRADE_ACTION_KEYWORD, (
                f"复核内容含交易动作关键词（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )

        workflow.reviews.append(record)
        workflow.updated_at = utc_now()
        return True, "", ""

    def update_core_assumptions(
        self,
        workflow: CommitteeWorkflow,
        *,
        new_assumptions: list[str],
        operator_user_id: str,
        operator_role: str,
    ) -> tuple[bool, str, str]:
        """更新核心假设。

        验收点 2：修改核心假设后旧审批自动失效。
        - 如果新假设哈希与旧哈希不同：
          - 若 workflow.status == APPROVED_FOR_MONITORING：自动标记为 invalidated + status=EXPIRED
          - 添加审计条目记录假设变更
        - 哈希相同时不触发失效

        Args:
            workflow: 工作流对象（将被原地修改）
            new_assumptions: 新的核心假设列表
            operator_user_id: 操作人用户 ID
            operator_role: 操作人角色

        Returns:
            (success, error_code, error_message)
        """
        if not isinstance(workflow, CommitteeWorkflow):
            return False, ERR_PERMISSION_DENIED, "workflow 类型无效"
        if workflow.status in TERMINAL_STATUSES and workflow.status != STATUS_APPROVED_FOR_MONITORING:
            return False, ERR_TERMINAL_STATE, f"workflow 已处于终态：{workflow.status}"

        if not self._is_operator_assigned(workflow, operator_user_id, operator_role):
            return False, ERR_PERMISSION_DENIED, (
                f"操作人 {operator_user_id} 未被分配为角色 {operator_role}"
            )

        normalized_assumptions = _text_list(new_assumptions)
        new_hash = _hash_assumptions(normalized_assumptions) if normalized_assumptions else ""
        old_hash = _safe_text(workflow.core_assumptions_hash)
        hash_changed = new_hash != old_hash

        from_status = workflow.status
        workflow.core_assumptions = normalized_assumptions
        workflow.core_assumptions_hash = new_hash
        workflow.updated_at = utc_now()

        if hash_changed and from_status == STATUS_APPROVED_FOR_MONITORING:
            # 验收点 2：旧审批自动失效
            workflow.invalidated_by_assumption_change = True
            workflow.invalidated_at = utc_now()
            workflow.status = STATUS_EXPIRED

        # 审计条目
        audit = AuditEntry(
            audit_id=f"audit_{workflow.workflow_id}_{len(workflow.audit_trail)+1}",
            workflow_id=workflow.workflow_id,
            from_status=from_status,
            to_status=workflow.status,
            operator_user_id=operator_user_id,
            operator_role=operator_role,
            reason=(
                f"core_assumptions updated (hash: {old_hash[:8]} → {new_hash[:8]}); "
                f"invalidated={workflow.invalidated_by_assumption_change}"
            ),
            review_snapshot=_review_snapshot(workflow),
            evidence_refs=[],
            created_at=workflow.updated_at,
        )
        workflow.audit_trail.append(audit)
        return True, "", ""

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _is_operator_assigned(
        self,
        workflow: CommitteeWorkflow,
        operator_user_id: str,
        operator_role: str,
    ) -> bool:
        """校验操作人是否被分配为指定角色。"""
        if not operator_user_id or not operator_role:
            return False
        if operator_role not in REVIEW_ROLES:
            return False
        assigned_id = self._get_role_assignee(workflow, operator_role)
        return assigned_id == operator_user_id

    def _is_user_assigned_to_role(
        self,
        workflow: CommitteeWorkflow,
        user_id: str,
        role: str,
    ) -> bool:
        """校验用户是否被分配为指定角色。"""
        if not user_id or not role:
            return False
        return self._get_role_assignee(workflow, role) == user_id

    @staticmethod
    def _get_role_assignee(workflow: CommitteeWorkflow, role: str) -> str:
        """获取角色对应的用户 ID。"""
        mapping = {
            ROLE_LEAD_ANALYST: workflow.lead_analyst_user_id,
            "financial_quality_reviewer": workflow.financial_quality_reviewer_user_id,
            "industry_supply_chain_reviewer": workflow.industry_supply_chain_reviewer_user_id,
            "model_reviewer": workflow.model_reviewer_user_id,
            ROLE_INDEPENDENT_ADVERSARY: workflow.independent_adversary_user_id,
            ROLE_CHAIR: workflow.chair_user_id,
        }
        return mapping.get(role, "")


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_committee_workflow_engine() -> CommitteeWorkflowEngine:
    """构造默认 CommitteeWorkflowEngine。"""
    return CommitteeWorkflowEngine()


__all__ = [
    "ERR_ADVERSARY_CHECKLIST_INCOMPLETE",
    "ERR_ALREADY_INVALIDATED",
    "ERR_INVALID_TRANSITION",
    "ERR_MISSING_REQUIRED_REVIEWS",
    "ERR_PERMISSION_DENIED",
    "ERR_SAME_IDENTITY",
    "ERR_TERMINAL_STATE",
    "ERR_TRADE_ACTION_KEYWORD",
    "CommitteeWorkflowEngine",
    "build_adversary_checklist_template",
    "build_committee_workflow_engine",
    "is_adversary_checklist_complete",
]
