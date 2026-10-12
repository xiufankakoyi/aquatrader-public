"""P2.7 投委会工作流契约。

按 P2 计划 §242-279 Task P2.7：
- 6 个角色：主分析师 / 财务质量复核人 / 行业-供应链复核人 / 模型复核人 / 独立反方 / 主持人
- 状态机：draft → evidence_review → model_review → adversarial_review
        → committee_ready → approved_for_monitoring / returned / rejected / expired
- 强制要求：
  1. 同一执行身份不得同时完成主分析和独立反方签署
  2. 每次状态变化保存问题、回答、证据和时间
  3. 反方必须检查 7 项（替代技术 / 需求不及预期 / 价格下降 / 成本上升 / 竞争 / 治理 / 估值假设）
  4. 审批仅表示「研究材料达到监控标准」，不表示交易建议

验收点：
- 缺任一强制复核不能进入 approved_for_monitoring
- 修改核心假设后旧审批自动失效
- API 有权限边界、审计和文档
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# 角色常量
# ---------------------------------------------------------------------------

ROLE_LEAD_ANALYST: str = "lead_analyst"
ROLE_FINANCIAL_QUALITY_REVIEWER: str = "financial_quality_reviewer"
ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER: str = "industry_supply_chain_reviewer"
ROLE_MODEL_REVIEWER: str = "model_reviewer"
ROLE_INDEPENDENT_ADVERSARY: str = "independent_adversary"
ROLE_CHAIR: str = "chair"

REVIEW_ROLES: tuple[str, ...] = (
    ROLE_LEAD_ANALYST,
    ROLE_FINANCIAL_QUALITY_REVIEWER,
    ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER,
    ROLE_MODEL_REVIEWER,
    ROLE_INDEPENDENT_ADVERSARY,
    ROLE_CHAIR,
)

# 强制复核角色（缺任一不能进入 approved_for_monitoring）
REQUIRED_REVIEW_ROLES: tuple[str, ...] = (
    ROLE_FINANCIAL_QUALITY_REVIEWER,
    ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER,
    ROLE_MODEL_REVIEWER,
    ROLE_INDEPENDENT_ADVERSARY,
    ROLE_CHAIR,
)


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

STATUS_DRAFT: str = "draft"
STATUS_EVIDENCE_REVIEW: str = "evidence_review"
STATUS_MODEL_REVIEW: str = "model_review"
STATUS_ADVERSARIAL_REVIEW: str = "adversarial_review"
STATUS_COMMITTEE_READY: str = "committee_ready"
STATUS_APPROVED_FOR_MONITORING: str = "approved_for_monitoring"
STATUS_RETURNED: str = "returned"
STATUS_REJECTED: str = "rejected"
STATUS_EXPIRED: str = "expired"

WORKFLOW_STATUSES: tuple[str, ...] = (
    STATUS_DRAFT,
    STATUS_EVIDENCE_REVIEW,
    STATUS_MODEL_REVIEW,
    STATUS_ADVERSARIAL_REVIEW,
    STATUS_COMMITTEE_READY,
    STATUS_APPROVED_FOR_MONITORING,
    STATUS_RETURNED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
)

TERMINAL_STATUSES: tuple[str, ...] = (STATUS_REJECTED, STATUS_EXPIRED)

# 合法状态迁移
TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_DRAFT: (STATUS_EVIDENCE_REVIEW,),
    STATUS_EVIDENCE_REVIEW: (
        STATUS_MODEL_REVIEW,
        STATUS_RETURNED,
        STATUS_REJECTED,
    ),
    STATUS_MODEL_REVIEW: (
        STATUS_ADVERSARIAL_REVIEW,
        STATUS_RETURNED,
        STATUS_REJECTED,
    ),
    STATUS_ADVERSARIAL_REVIEW: (
        STATUS_COMMITTEE_READY,
        STATUS_RETURNED,
        STATUS_REJECTED,
    ),
    STATUS_COMMITTEE_READY: (
        STATUS_APPROVED_FOR_MONITORING,
        STATUS_RETURNED,
        STATUS_REJECTED,
        STATUS_EXPIRED,
    ),
    STATUS_APPROVED_FOR_MONITORING: (STATUS_EXPIRED,),
    STATUS_RETURNED: (STATUS_DRAFT,),
    STATUS_REJECTED: (),
    STATUS_EXPIRED: (),
}

# 状态迁移所需的强制复核角色（验收点 1）
REQUIRED_REVIEWS_FOR_TRANSITION: dict[tuple[str, str], tuple[str, ...]] = {
    (STATUS_EVIDENCE_REVIEW, STATUS_MODEL_REVIEW): (
        ROLE_FINANCIAL_QUALITY_REVIEWER,
        ROLE_INDUSTRY_SUPPLY_CHAIN_REVIEWER,
    ),
    (STATUS_MODEL_REVIEW, STATUS_ADVERSARIAL_REVIEW): (ROLE_MODEL_REVIEWER,),
    (STATUS_ADVERSARIAL_REVIEW, STATUS_COMMITTEE_READY): (ROLE_INDEPENDENT_ADVERSARY,),
    (STATUS_COMMITTEE_READY, STATUS_APPROVED_FOR_MONITORING): (ROLE_CHAIR,),
}

# 复核状态
REVIEW_STATUS_PASS: str = "pass"
REVIEW_STATUS_FAIL: str = "fail"
REVIEW_STATUS_CONCERNS: str = "concerns"
REVIEW_STATUS_BLOCKED: str = "blocked"

REVIEW_STATUSES: tuple[str, ...] = (
    REVIEW_STATUS_PASS,
    REVIEW_STATUS_FAIL,
    REVIEW_STATUS_CONCERNS,
    REVIEW_STATUS_BLOCKED,
)


# ---------------------------------------------------------------------------
# 反方检查清单 7 项（强制要求 3）
# ---------------------------------------------------------------------------

ADVERSARY_CHECK_ALTERNATIVE_TECH: str = "alternative_tech"
ADVERSARY_CHECK_DEMAND_BELOW_EXPECTATION: str = "demand_below_expectation"
ADVERSARY_CHECK_PRICE_DECLINE: str = "price_decline"
ADVERSARY_CHECK_COST_RISE: str = "cost_rise"
ADVERSARY_CHECK_COMPETITION: str = "competition"
ADVERSARY_CHECK_GOVERNANCE: str = "governance"
ADVERSARY_CHECK_VALUATION_ASSUMPTION: str = "valuation_assumption"

ADVERSARY_CHECK_ITEMS: tuple[str, ...] = (
    ADVERSARY_CHECK_ALTERNATIVE_TECH,
    ADVERSARY_CHECK_DEMAND_BELOW_EXPECTATION,
    ADVERSARY_CHECK_PRICE_DECLINE,
    ADVERSARY_CHECK_COST_RISE,
    ADVERSARY_CHECK_COMPETITION,
    ADVERSARY_CHECK_GOVERNANCE,
    ADVERSARY_CHECK_VALUATION_ASSUMPTION,
)

ADVERSARY_CHECK_LABELS: dict[str, str] = {
    ADVERSARY_CHECK_ALTERNATIVE_TECH: "替代技术",
    ADVERSARY_CHECK_DEMAND_BELOW_EXPECTATION: "需求不及预期",
    ADVERSARY_CHECK_PRICE_DECLINE: "价格下降",
    ADVERSARY_CHECK_COST_RISE: "成本上升",
    ADVERSARY_CHECK_COMPETITION: "竞争",
    ADVERSARY_CHECK_GOVERNANCE: "治理",
    ADVERSARY_CHECK_VALUATION_ASSUMPTION: "估值假设",
}


# ---------------------------------------------------------------------------
# AGENTS.md 禁词（审批仅表示研究材料达到监控标准，不表示交易建议）
# ---------------------------------------------------------------------------

FORBIDDEN_TRADE_ACTION_KEYWORDS: tuple[str, ...] = (
    "买入",
    "卖出",
    "止盈",
    "止损",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "调仓",
)

# 审批说明固定提示词
APPROVAL_NOTICE: str = "审批仅表示研究材料达到监控标准，不表示交易建议。"


def _safe_text(value: Any, default: str = "") -> str:
    """仅保留非空文本，避免外部 JSON 值污染状态机和审计。"""
    return value.strip() if isinstance(value, str) and value.strip() else default


def _text_list(value: Any) -> list[str]:
    """把外部列表收敛为非空文本列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 复核问题 / 复核记录 / 审计条目
# ---------------------------------------------------------------------------


@dataclass
class ReviewQuestion:
    """复核问题。

    Attributes:
        question_id: 问题 ID
        text: 问题文本
        answer: 回答文本
        evidence_refs: 证据 ID 列表
        answered_at: 回答时间
    """

    question_id: str = ""
    text: str = ""
    answer: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    answered_at: str = ""

    def __post_init__(self) -> None:
        self.question_id = _safe_text(self.question_id)
        self.text = _safe_text(self.text)
        self.answer = _safe_text(self.answer)
        self.evidence_refs = _text_list(self.evidence_refs)
        self.answered_at = _safe_text(self.answered_at)

    def is_answered(self) -> bool:
        """是否已回答。"""
        return bool(_safe_text(self.answer))

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        for field_name in ("text", "answer"):
            value = _safe_text(getattr(self, field_name, None))
            if value and any(kw in value for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "question_id": self.question_id,
            "text": self.text,
            "answer": self.answer,
            "evidence_refs": list(self.evidence_refs),
            "answered_at": self.answered_at,
        }


@dataclass
class ReviewRecord:
    """单条复核记录。

    每次状态变化必须保存问题、回答、证据和时间（强制要求 2）。

    Attributes:
        review_id: 复核记录 ID
        workflow_id: 关联工作流 ID
        role: 复核角色（REVIEW_ROLES 之一）
        reviewer_user_id: 复核人用户 ID
        reviewer_display_name: 复核人显示名
        status: 复核状态（pass / fail / concerns / blocked）
        questions: 问题列表
        summary: 复核总结
        evidence_refs: 证据 ID 列表
        created_at: 创建时间
    """

    review_id: str = ""
    workflow_id: str = ""
    role: str = ""
    reviewer_user_id: str = ""
    reviewer_display_name: str = ""
    status: str = REVIEW_STATUS_CONCERNS
    questions: list[ReviewQuestion] = field(default_factory=list)
    summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.review_id = _safe_text(self.review_id)
        self.workflow_id = _safe_text(self.workflow_id)
        self.role = _safe_text(self.role)
        self.reviewer_user_id = _safe_text(self.reviewer_user_id)
        self.reviewer_display_name = _safe_text(self.reviewer_display_name)
        self.status = _safe_text(self.status, REVIEW_STATUS_CONCERNS)
        self.questions = [q for q in self.questions if isinstance(q, ReviewQuestion)] if isinstance(self.questions, list) else []
        self.summary = _safe_text(self.summary)
        self.evidence_refs = _text_list(self.evidence_refs)
        self.created_at = _safe_text(self.created_at)

    def is_passing(self) -> bool:
        """是否通过（pass 或 concerns 均视为可继续）。"""
        return self.status in (REVIEW_STATUS_PASS, REVIEW_STATUS_CONCERNS)

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        summary = _safe_text(self.summary)
        if summary and any(kw in summary for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
            return True
        return any(
            isinstance(q, ReviewQuestion) and q.contains_trade_action()
            for q in self.questions
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "review_id": self.review_id,
            "workflow_id": self.workflow_id,
            "role": self.role,
            "reviewer_user_id": self.reviewer_user_id,
            "reviewer_display_name": self.reviewer_display_name,
            "status": self.status,
            "questions": [q.to_dict() for q in self.questions],
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }


@dataclass
class AuditEntry:
    """审计条目。

    每次状态变化必须记录（强制要求 2）。

    Attributes:
        audit_id: 审计 ID
        workflow_id: 关联工作流 ID
        from_status: 原状态
        to_status: 新状态
        operator_user_id: 操作人用户 ID
        operator_role: 操作人角色
        reason: 操作原因
        review_snapshot: 关联复核记录快照
        evidence_refs: 证据 ID 列表
        created_at: 创建时间
    """

    audit_id: str = ""
    workflow_id: str = ""
    from_status: str = ""
    to_status: str = ""
    operator_user_id: str = ""
    operator_role: str = ""
    reason: str = ""
    review_snapshot: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.audit_id = _safe_text(self.audit_id)
        self.workflow_id = _safe_text(self.workflow_id)
        self.from_status = _safe_text(self.from_status)
        self.to_status = _safe_text(self.to_status)
        self.operator_user_id = _safe_text(self.operator_user_id)
        self.operator_role = _safe_text(self.operator_role)
        self.reason = _safe_text(self.reason)
        self.review_snapshot = (
            [dict(item) for item in self.review_snapshot if isinstance(item, Mapping)]
            if isinstance(self.review_snapshot, list)
            else []
        )
        self.evidence_refs = _text_list(self.evidence_refs)
        self.created_at = _safe_text(self.created_at)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "audit_id": self.audit_id,
            "workflow_id": self.workflow_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "operator_user_id": self.operator_user_id,
            "operator_role": self.operator_role,
            "reason": self.reason,
            "review_snapshot": list(self.review_snapshot),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 投委会工作流主对象
# ---------------------------------------------------------------------------


@dataclass
class CommitteeWorkflow:
    """投委会工作流。

    Attributes:
        workflow_id: 工作流 ID
        thesis_id: 关联 VariantPerception.thesis_id
        company_symbol: 公司代码
        as_of: PIT 时点
        lead_analyst_user_id: 主分析师用户 ID（强制要求 1：不得与 independent_adversary 相同）
        financial_quality_reviewer_user_id: 财务质量复核人
        industry_supply_chain_reviewer_user_id: 行业/供应链复核人
        model_reviewer_user_id: 模型复核人
        independent_adversary_user_id: 独立反方
        chair_user_id: 主持人
        status: 当前状态
        reviews: 复核记录列表
        audit_trail: 审计条目列表
        core_assumptions: 核心假设列表（修改后旧审批失效）
        core_assumptions_hash: 核心假设哈希
        invalidated_by_assumption_change: 是否被假设变更失效
        invalidated_at: 失效时间
        created_at: 创建时间
        updated_at: 更新时间
    """

    workflow_id: str = ""
    thesis_id: str = ""
    company_symbol: str = ""
    as_of: str = ""

    lead_analyst_user_id: str = ""
    financial_quality_reviewer_user_id: str = ""
    industry_supply_chain_reviewer_user_id: str = ""
    model_reviewer_user_id: str = ""
    independent_adversary_user_id: str = ""
    chair_user_id: str = ""

    status: str = STATUS_DRAFT

    reviews: list[ReviewRecord] = field(default_factory=list)
    audit_trail: list[AuditEntry] = field(default_factory=list)

    core_assumptions: list[str] = field(default_factory=list)
    core_assumptions_hash: str = ""
    invalidated_by_assumption_change: bool = False
    invalidated_at: str = ""

    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        """默认 created_at / updated_at。"""
        for field_name in (
            "workflow_id", "thesis_id", "company_symbol", "as_of",
            "lead_analyst_user_id", "financial_quality_reviewer_user_id",
            "industry_supply_chain_reviewer_user_id", "model_reviewer_user_id",
            "independent_adversary_user_id", "chair_user_id", "status",
            "invalidated_at", "created_at", "updated_at",
        ):
            default = STATUS_DRAFT if field_name == "status" else ""
            setattr(self, field_name, _safe_text(getattr(self, field_name, None), default))
        self.reviews = [r for r in self.reviews if isinstance(r, ReviewRecord)] if isinstance(self.reviews, list) else []
        self.audit_trail = [a for a in self.audit_trail if isinstance(a, AuditEntry)] if isinstance(self.audit_trail, list) else []
        self.core_assumptions = _text_list(self.core_assumptions)
        self.invalidated_by_assumption_change = self.invalidated_by_assumption_change is True
        now = utc_now()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        self.core_assumptions_hash = (
            _hash_assumptions(self.core_assumptions)
            if self.core_assumptions else ""
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def validate_role_assignment(self) -> tuple[bool, str]:
        """校验角色分配。

        强制要求 1：同一执行身份不得同时完成主分析和独立反方签署。

        Returns:
            (is_valid, error_message)
        """
        if (
            self.lead_analyst_user_id
            and self.independent_adversary_user_id
            and self.lead_analyst_user_id == self.independent_adversary_user_id
        ):
            return (
                False,
                "lead_analyst_user_id 与 independent_adversary_user_id 不得相同",
            )
        return True, ""

    def has_review_by_role(self, role: str) -> bool:
        """是否已有该角色的通过复核。"""
        for r in self.reviews:
            if isinstance(r, ReviewRecord) and r.role == role and r.is_passing():
                return True
        return False

    def get_reviews_by_role(self, role: str) -> list[ReviewRecord]:
        """获取该角色的所有复核记录。"""
        return [r for r in self.reviews if isinstance(r, ReviewRecord) and r.role == role]

    def is_terminal(self) -> bool:
        """是否处于终态。"""
        return self.status in TERMINAL_STATUSES

    def is_approved(self) -> bool:
        """是否已审批通过。"""
        return self.status == STATUS_APPROVED_FOR_MONITORING

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "workflow_id": self.workflow_id,
            "thesis_id": self.thesis_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "lead_analyst_user_id": self.lead_analyst_user_id,
            "financial_quality_reviewer_user_id": self.financial_quality_reviewer_user_id,
            "industry_supply_chain_reviewer_user_id": self.industry_supply_chain_reviewer_user_id,
            "model_reviewer_user_id": self.model_reviewer_user_id,
            "independent_adversary_user_id": self.independent_adversary_user_id,
            "chair_user_id": self.chair_user_id,
            "status": self.status,
            "reviews": [r.to_dict() for r in self.reviews],
            "audit_trail": [a.to_dict() for a in self.audit_trail],
            "core_assumptions": list(self.core_assumptions),
            "core_assumptions_hash": self.core_assumptions_hash,
            "invalidated_by_assumption_change": self.invalidated_by_assumption_change,
            "invalidated_at": self.invalidated_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _hash_assumptions(assumptions: list[str]) -> str:
    """计算核心假设哈希。

    用于检测假设变更（验收点 2）。
    """
    raw = "\n".join(_text_list(assumptions))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
