"""P2.7 投委会工作流 API 端点。

Blueprint url_prefix="/api/research/committee"

按 P2 计划 §242-279 Task P2.7 验收点 3：API 有权限边界、审计和文档。

权限边界：
- 所有写操作必须显式提供 operator_user_id + operator_role
- 引擎通过 _is_operator_assigned 校验操作人是否被分配为该角色
- 同一身份不得同时为主分析师与独立反方（强制要求 1）

审计：
- 引擎每次 transition / add_review / update_core_assumptions 自动追加 AuditEntry
- AuditEntry 含 from_status / to_status / operator / reason / review_snapshot / 时间

文档：
- 每个端点含 DocString 说明用途、参数、错误码
- 所有响应含 notice 字段提示"审批仅表示研究材料达到监控标准，不表示交易建议。"

端点：
- GET  /health                          健康检查 + 存储统计
- GET  /adversary_checklist             获取反方 7 项检查清单模板
- POST /workflows                       创建工作流（draft 状态）
- GET  /workflows                       列表查询（支持 thesis_id/company_symbol/status 过滤）
- GET  /workflows/<workflow_id>         单个工作流详情
- POST /workflows/<workflow_id>/reviews 添加复核记录
- POST /workflows/<workflow_id>/transition 执行状态迁移
- POST /workflows/<workflow_id>/core_assumptions 更新核心假设（旧审批自动失效）

所有端点不返回交易动作建议。
缺必填字段返回 400；非法迁移返回 409；权限不足返回 403。
错误响应统一 {ok: false, error: {code, message, trace_id}}
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.committee import (
    ADVERSARY_CHECK_ITEMS,
    ADVERSARY_CHECK_LABELS,
    APPROVAL_NOTICE,
    CommitteeRepository,
    CommitteeWorkflow,
    CommitteeWorkflowEngine,
    REVIEW_ROLES,
    REVIEW_STATUSES,
    ReviewQuestion,
    WORKFLOW_STATUSES,
    build_adversary_checklist_template,
)


committee_bp = Blueprint("committee", __name__, url_prefix="/api/research/committee")

_repository: Any = None
_engine: CommitteeWorkflowEngine | None = None

NOTICE: str = APPROVAL_NOTICE


def _repo() -> CommitteeRepository:
    """惰性初始化仓储单例。"""
    global _repository
    if _repository is None:
        _repository = CommitteeRepository(Path("data/research_control/committee.db"))
    return _repository


def _engine_singleton() -> CommitteeWorkflowEngine:
    """惰性初始化引擎单例。"""
    global _engine
    if _engine is None:
        _engine = CommitteeWorkflowEngine()
    return _engine


def _trace_id() -> str:
    """生成 trace_id。"""
    return f"tr_{uuid.uuid4().hex[:12]}"


def _ok(data: Any, status: int = 200, **meta: Any):
    """统一成功响应。"""
    payload: dict[str, Any] = {
        "ok": True,
        "data": data,
        "notice": NOTICE,
        "trace_id": _trace_id(),
    }
    payload.update(meta)
    return jsonify(payload), status


def _error(
    message: str,
    code: str = "BAD_REQUEST",
    status: int = 400,
    trace_id: str | None = None,
):
    """统一错误响应。"""
    return jsonify({
        "ok": False,
        "error": {"code": code, "message": message, "trace_id": trace_id or _trace_id()},
        "notice": NOTICE,
    }), status


def _parse_questions(raw: list[dict] | None) -> list[ReviewQuestion]:
    """解析复核问题列表。"""
    if not raw:
        return []
    result: list[ReviewQuestion] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(ReviewQuestion(
                question_id=str(item.get("question_id", "")),
                text=str(item.get("text", "")),
                answer=str(item.get("answer", "")),
                evidence_refs=[
                    str(x) for x in (item.get("evidence_refs") or []) if x
                ],
                answered_at=str(item.get("answered_at", "")),
            ))
        except (TypeError, ValueError):
            continue
    return result


def _parse_review_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
    """提取并校验复核请求 payload。

    Returns:
        (review_fields, error_code, error_message)
    """
    role = str(payload.get("role", "")).strip()
    reviewer_user_id = str(payload.get("reviewer_user_id", "")).strip()
    if not role or not reviewer_user_id:
        return {}, "INSUFFICIENT_INPUT", "role / reviewer_user_id 均为必填"
    if role not in REVIEW_ROLES:
        return {}, "INVALID_ROLE", f"非法角色：{role}（合法角色：{', '.join(REVIEW_ROLES)}）"

    status_value = str(payload.get("status", "concerns")).strip()
    if status_value not in REVIEW_STATUSES:
        return {}, "INVALID_REVIEW_STATUS", (
            f"非法复核状态：{status_value}（合法状态：{', '.join(REVIEW_STATUSES)}）"
        )

    questions = _parse_questions(payload.get("questions"))
    return {
        "role": role,
        "reviewer_user_id": reviewer_user_id,
        "reviewer_display_name": str(payload.get("reviewer_display_name", "")),
        "status": status_value,
        "questions": questions,
        "summary": str(payload.get("summary", "")),
        "evidence_refs": [
            str(x) for x in (payload.get("evidence_refs") or []) if x
        ],
    }, None, None


def _parse_transition_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
    """提取并校验状态迁移 payload。

    Returns:
        (transition_fields, error_code, error_message)
    """
    target_status = str(payload.get("target_status", "")).strip()
    operator_user_id = str(payload.get("operator_user_id", "")).strip()
    operator_role = str(payload.get("operator_role", "")).strip()
    if not target_status or not operator_user_id or not operator_role:
        return {}, "INSUFFICIENT_INPUT", (
            "target_status / operator_user_id / operator_role 均为必填"
        )
    if target_status not in WORKFLOW_STATUSES:
        return {}, "INVALID_TARGET_STATUS", (
            f"非法目标状态：{target_status}（合法状态：{', '.join(WORKFLOW_STATUSES)}）"
        )
    if operator_role not in REVIEW_ROLES:
        return {}, "INVALID_OPERATOR_ROLE", (
            f"非法操作人角色：{operator_role}（合法角色：{', '.join(REVIEW_ROLES)}）"
        )
    return {
        "target_status": target_status,
        "operator_user_id": operator_user_id,
        "operator_role": operator_role,
        "reason": str(payload.get("reason", "")),
    }, None, None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@committee_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    return _ok({"status": "ok", **_repo().stats()})


@committee_bp.get("/adversary_checklist")
def adversary_checklist():
    """获取反方 7 项检查清单模板。

    返回 7 个 ReviewQuestion，覆盖：
    替代技术 / 需求不及预期 / 价格下降 / 成本上升 / 竞争 / 治理 / 估值假设
    """
    template = build_adversary_checklist_template()
    return _ok({
        "items": ADVERSARY_CHECK_ITEMS,
        "labels": ADVERSARY_CHECK_LABELS,
        "questions": [q.to_dict() for q in template],
    })


@committee_bp.route("/workflows", methods=["GET", "POST"])
def workflows():
    """工作流集合：POST 创建；GET 列表查询。

    POST 必填：thesis_id / company_symbol / as_of / lead_analyst_user_id /
              independent_adversary_user_id（不得与 lead 相同）
    可选：core_assumptions / 各角色 user_id

    GET 参数：thesis_id / company_symbol / status / as_of / limit
    """
    if request.method == "GET":
        thesis_id = request.args.get("thesis_id", "")
        company_symbol = request.args.get("company_symbol", "")
        status = request.args.get("status", "")
        as_of = request.args.get("as_of", "")
        limit = request.args.get("limit", 100, type=int)

        items: list[dict[str, Any]] = []
        if thesis_id:
            items = _repo().list_by_thesis(thesis_id=thesis_id, limit=limit)
        elif company_symbol:
            items = _repo().list_by_company(
                company_symbol=company_symbol,
                as_of=as_of or None,
                status=status or None,
                limit=limit,
            )
        elif status:
            items = _repo().list_by_status(status=status, limit=limit)
        return _ok(items, total=len(items))

    # POST 创建工作流
    payload = request.get_json(silent=True) or {}
    thesis_id = str(payload.get("thesis_id", "")).strip()
    company_symbol = str(payload.get("company_symbol", "")).strip()
    as_of = str(payload.get("as_of", "")).strip()
    lead_analyst_user_id = str(payload.get("lead_analyst_user_id", "")).strip()
    independent_adversary_user_id = str(payload.get("independent_adversary_user_id", "")).strip()

    if not thesis_id or not company_symbol or not as_of:
        return _error(
            "thesis_id / company_symbol / as_of 均为必填",
            "INSUFFICIENT_INPUT", 400,
        )
    if not lead_analyst_user_id:
        return _error("lead_analyst_user_id 必填", "INSUFFICIENT_INPUT", 400)
    if not independent_adversary_user_id:
        return _error("independent_adversary_user_id 必填", "INSUFFICIENT_INPUT", 400)
    if lead_analyst_user_id == independent_adversary_user_id:
        return _error(
            "lead_analyst_user_id 与 independent_adversary_user_id 不得相同（强制要求 1）",
            "SAME_IDENTITY_FOR_LEAD_AND_ADVERSARY", 400,
        )

    workflow = CommitteeWorkflow(
        workflow_id=f"wf_{uuid.uuid4().hex[:16]}",
        thesis_id=thesis_id,
        company_symbol=company_symbol,
        as_of=as_of,
        lead_analyst_user_id=lead_analyst_user_id,
        financial_quality_reviewer_user_id=str(payload.get("financial_quality_reviewer_user_id", "")),
        industry_supply_chain_reviewer_user_id=str(payload.get("industry_supply_chain_reviewer_user_id", "")),
        model_reviewer_user_id=str(payload.get("model_reviewer_user_id", "")),
        independent_adversary_user_id=independent_adversary_user_id,
        chair_user_id=str(payload.get("chair_user_id", "")),
        core_assumptions=[
            str(x) for x in (payload.get("core_assumptions") or []) if x
        ],
    )

    is_valid, err_msg = workflow.validate_role_assignment()
    if not is_valid:
        return _error(err_msg, "SAME_IDENTITY_FOR_LEAD_AND_ADVERSARY", 400)

    try:
        _repo().save(workflow)
    except ValueError as exc:
        return _error(str(exc), "WORKFLOW_ALREADY_EXISTS", 409)

    return _ok(workflow.to_dict(), 201)


@committee_bp.get("/workflows/<workflow_id>")
def workflow_detail(workflow_id: str):
    """单个工作流详情。"""
    item = _repo().get(workflow_id)
    if not item:
        return _error("工作流不存在", "WORKFLOW_NOT_FOUND", 404)
    return _ok(item)


@committee_bp.post("/workflows/<workflow_id>/reviews")
def add_review(workflow_id: str):
    """添加复核记录。

    权限边界：reviewer_user_id 必须被分配为该角色
    强制要求 1：独立反方不能与主分析师相同
    AGENTS.md：复核内容不得含交易动作词

    必填：role / reviewer_user_id
    可选：reviewer_display_name / status (默认 concerns) / questions / summary / evidence_refs
    """
    raw = _repo().get(workflow_id)
    if not raw:
        return _error("工作流不存在", "WORKFLOW_NOT_FOUND", 404)
    workflow = CommitteeWorkflow(**{
        k: v for k, v in raw.items()
        if k in CommitteeWorkflow.__dataclass_fields__
    })

    payload = request.get_json(silent=True) or {}
    fields, err_code, err_msg = _parse_review_payload(payload)
    if err_code:
        return _error(err_msg, err_code, 400)

    success, eng_err_code, eng_err_msg = _engine_singleton().add_review(
        workflow,
        role=fields["role"],
        reviewer_user_id=fields["reviewer_user_id"],
        reviewer_display_name=fields["reviewer_display_name"],
        status=fields["status"],
        questions=fields["questions"],
        summary=fields["summary"],
        evidence_refs=fields["evidence_refs"],
    )
    if not success:
        status_code = _error_status_for_engine_code(eng_err_code)
        return _error(eng_err_msg, eng_err_code, status_code)

    if not _repo().update(workflow):
        return _error("工作流更新失败", "WORKFLOW_NOT_FOUND", 404)

    return _ok(workflow.to_dict())


@committee_bp.post("/workflows/<workflow_id>/transition")
def transition_workflow(workflow_id: str):
    """执行状态迁移。

    权限边界：operator_user_id 必须被分配为 operator_role
    强制复核：缺任一强制复核角色不能进入 approved_for_monitoring（验收点 1）
    反方检查清单：adversarial_review → committee_ready 必须完整回答 7 项

    必填：target_status / operator_user_id / operator_role
    可选：reason
    """
    raw = _repo().get(workflow_id)
    if not raw:
        return _error("工作流不存在", "WORKFLOW_NOT_FOUND", 404)
    workflow = CommitteeWorkflow(**{
        k: v for k, v in raw.items()
        if k in CommitteeWorkflow.__dataclass_fields__
    })

    payload = request.get_json(silent=True) or {}
    fields, err_code, err_msg = _parse_transition_payload(payload)
    if err_code:
        return _error(err_msg, err_code, 400)

    success, eng_err_code, eng_err_msg = _engine_singleton().transition(
        workflow,
        fields["target_status"],
        operator_user_id=fields["operator_user_id"],
        operator_role=fields["operator_role"],
        reason=fields["reason"],
    )
    if not success:
        status_code = _error_status_for_engine_code(eng_err_code)
        return _error(eng_err_msg, eng_err_code, status_code)

    if not _repo().update(workflow):
        return _error("工作流更新失败", "WORKFLOW_NOT_FOUND", 404)

    return _ok(workflow.to_dict())


@committee_bp.post("/workflows/<workflow_id>/core_assumptions")
def update_core_assumptions(workflow_id: str):
    """更新核心假设。

    验收点 2：修改核心假设后旧审批自动失效。
    - 如果新假设哈希与旧哈希不同且当前状态为 approved_for_monitoring：
      自动标记 invalidated_by_assumption_change=True 并将状态置为 expired
    - 哈希相同时不触发失效

    权限边界：operator_user_id 必须被分配为 operator_role

    必填：new_assumptions / operator_user_id / operator_role
    """
    raw = _repo().get(workflow_id)
    if not raw:
        return _error("工作流不存在", "WORKFLOW_NOT_FOUND", 404)
    workflow = CommitteeWorkflow(**{
        k: v for k, v in raw.items()
        if k in CommitteeWorkflow.__dataclass_fields__
    })

    payload = request.get_json(silent=True) or {}
    new_assumptions_raw = payload.get("new_assumptions")
    if not isinstance(new_assumptions_raw, list):
        return _error("new_assumptions 必须为列表", "INSUFFICIENT_INPUT", 400)
    new_assumptions = [str(x) for x in new_assumptions_raw if x]
    operator_user_id = str(payload.get("operator_user_id", "")).strip()
    operator_role = str(payload.get("operator_role", "")).strip()
    if not operator_user_id or not operator_role:
        return _error(
            "operator_user_id / operator_role 均为必填",
            "INSUFFICIENT_INPUT", 400,
        )
    if operator_role not in REVIEW_ROLES:
        return _error(
            f"非法操作人角色：{operator_role}",
            "INVALID_OPERATOR_ROLE", 400,
        )

    success, eng_err_code, eng_err_msg = _engine_singleton().update_core_assumptions(
        workflow,
        new_assumptions=new_assumptions,
        operator_user_id=operator_user_id,
        operator_role=operator_role,
    )
    if not success:
        status_code = _error_status_for_engine_code(eng_err_code)
        return _error(eng_err_msg, eng_err_code, status_code)

    if not _repo().update(workflow):
        return _error("工作流更新失败", "WORKFLOW_NOT_FOUND", 404)

    return _ok(workflow.to_dict())


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _error_status_for_engine_code(code: str) -> int:
    """根据引擎错误码映射 HTTP 状态码。

    Args:
        code: 引擎错误码

    Returns:
        HTTP 状态码
    """
    mapping = {
        "invalid_transition": 409,
        "missing_required_reviews": 409,
        "same_identity_for_lead_and_adversary": 400,
        "adversary_checklist_incomplete": 409,
        "trade_action_keyword": 400,
        "permission_denied": 403,
        "already_invalidated": 409,
        "terminal_state": 409,
    }
    return mapping.get(code, 400)
