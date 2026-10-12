"""统一研究控制面 API。所有响应字段缺失时安全降级。"""
from __future__ import annotations

import os
import threading
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.control_plane.calibration import CalibrationController
from core.research.control_plane.contracts import (
    CalibrationRun, Claim, ClaimStatus, EvidenceRecord, FeedbackEvent,
    ResearchTask, StateTransition, TaskStatus, utc_now,
)
from core.research.control_plane.feedback import FeedbackController
from core.research.control_plane.domain_calibrators import fit_domain
from core.research.control_plane.policies import ClaimPolicy, evidence_visible_as_of, validate_state_transition
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.test_run_record import TestRunRecord, TestRunStore

research_bp = Blueprint("research", __name__, url_prefix="/api/research")
_repository: ResearchRepository | None = None
# P0.4: 公司研究档案服务（惰性初始化，便于单测注入）
_company_research_service: Any = None


@research_bp.before_request
def _feature_gate():
    enabled = os.getenv("RESEARCH_CONTROL_PLANE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if not enabled and request.endpoint != "research.health":
        return jsonify({
            "ok": False,
            "error": {"code": "FEATURE_DISABLED", "message": "统一研究控制面未启用"},
        }), 503


def get_research_repository() -> ResearchRepository:
    global _repository
    if _repository is None:
        _repository = ResearchRepository()
    return _repository


def get_company_research_service():
    """惰性初始化公司研究档案服务。"""
    global _company_research_service
    if _company_research_service is None:
        from server.services.research.company_research_service import (
            CompanyResearchService,
        )
        _company_research_service = CompanyResearchService()
    return _company_research_service


def _ok(data: Any = None, **meta: Any):
    return jsonify({"ok": True, "data": data if data is not None else {}, "meta": meta})


def _error(message: str, status: int = 400, code: str = "BAD_REQUEST", trace_id: str | None = None):
    import uuid as _uuid
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "trace_id": trace_id or ("tr_" + _uuid.uuid4().hex[:12]),
        },
    }
    return jsonify(payload), status


@research_bp.get("/health")
def health():
    repo = get_research_repository()
    return _ok({"status": "ok", "module": "research_control", **repo.stats()})


@research_bp.route("/tasks", methods=["GET", "POST"])
def tasks():
    repo = get_research_repository()
    if request.method == "GET":
        items = repo.list_tasks(request.args.get("status", ""), request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))
    payload = request.get_json(silent=True) or {}
    task = ResearchTask.from_dict(payload)
    if not task.status:
        task.status = TaskStatus.QUEUED.value
    repo.save_task(task)
    # 异步启动研究执行器
    _start_research_executor(task, repo)
    return _ok(task.to_dict()), 201


def _start_research_executor(task: ResearchTask, repo: ResearchRepository) -> None:
    """异步启动研究执行器，更新 task 状态。

    使用 threading 后台执行，不阻塞 API 响应。
    执行器失败时 task.status 设为 FAILED，failure 字段记录原因。
    """
    def _run() -> None:
        try:
            task.status = TaskStatus.RUNNING.value
            task.started_at = utc_now()
            repo.save_task(task)

            # 调用真实研究流程（基于 task.params 中的 topic）
            from sandbox.run_real_research_pipeline import run_pipeline_for_task
            result = run_pipeline_for_task(task)

            task.status = TaskStatus.SUCCEEDED.value if result.get("success") else TaskStatus.PARTIAL.value
            task.finished_at = utc_now()
            if not result.get("success"):
                task.failure = {"reason": result.get("reason", "unknown")}
            repo.save_task(task)
        except Exception as exc:
            task.status = TaskStatus.FAILED.value
            task.finished_at = utc_now()
            task.failure = {"reason": str(exc)}
            repo.save_task(task)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


@research_bp.route("/tasks/<task_id>", methods=["GET", "POST"])
def task_detail(task_id: str):
    repo = get_research_repository()
    task = repo.get_task(task_id)
    if task is None:
        return _error("研究任务不存在", 404, "TASK_NOT_FOUND")
    if request.method == "GET":
        return _ok(task.to_dict())
    action = str((request.get_json(silent=True) or {}).get("action") or "")
    if action == "cancel" and task.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.RETRYING.value}:
        task.status = TaskStatus.CANCELLED.value
        task.finished_at = utc_now()
    elif action == "retry" and task.status in {TaskStatus.FAILED.value, TaskStatus.PARTIAL.value, TaskStatus.CANCELLED.value}:
        task.status = TaskStatus.QUEUED.value
        task.failure = {}
        task.finished_at = ""
    else:
        return _error("当前状态不支持该操作", 409, "INVALID_TASK_TRANSITION")
    repo.save_task(task)
    return _ok(task.to_dict())


@research_bp.get("/tasks/<task_id>/attempts")
def task_attempts(task_id: str):
    """Read the durable attempt/checkpoint audit trail for one task."""
    repo = get_research_repository()
    if repo.get_task(task_id) is None:
        return _error("研究任务不存在", 404, "TASK_NOT_FOUND")
    from core.research.control_plane.task_resumer import TaskResumer

    limit = request.args.get("limit", 100, type=int)
    items = TaskResumer(repo).list_attempts(task_id, limit=limit)
    return _ok(
        [item.to_dict() for item in items],
        total=len(items),
        task_id=task_id,
    )


@research_bp.route("/claims", methods=["GET", "POST"])
def claims():
    repo = get_research_repository()
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        claim = Claim.from_dict(payload)
        # 检查是新建还是更新
        existing = repo.get_claim(claim.claim_id)
        if existing is None:
            # 新建 Claim：强制 research_status = "pending"
            client_status = str(payload.get("research_status") or "").lower()
            if client_status and client_status != "pending":
                return _error(
                    f"新建 Claim 状态必须为 pending，拒绝传入: {payload.get('research_status')}",
                    409, "INVALID_STATE_TRANSITION",
                )
            claim.research_status = "pending"
        else:
            # 更新已有 Claim：校验状态迁移合法性
            old_status = existing.research_status
            new_status = claim.research_status or old_status
            if old_status.lower() != new_status.lower():
                try:
                    validate_state_transition(old_status, new_status)
                except ValueError:
                    return _error(
                        f"非法状态迁移: {old_status} → {new_status}",
                        409, "INVALID_STATE_TRANSITION",
                    )
        repo.save_claim(claim)
        return _ok(claim.to_dict()), 201
    status = request.args.get("status", "")
    view = request.args.get("view", "")
    as_of = request.args.get("as_of", "")
    items = repo.list_claims(status, request.args.get("limit", 100, type=int))
    if view == "research":
        items = [item for item in items if item.research_status in {ClaimStatus.ACTIVE.value, ClaimStatus.TRUSTED.value} and item.conflict_status in {"", "none", "resolved"}]
    output = []
    blocked_future = 0
    for item in items:
        evidence = repo.list_evidence(item.claim_id, 1000)
        if as_of:
            visible = [ev for ev in evidence if evidence_visible_as_of(ev, as_of, historical=True)]
            blocked_future += len(evidence) - len(visible)
            if view == "research" and not visible:
                continue
        output.append(item.to_dict())
    return _ok(output, total=len(output), future_evidence_blocked=blocked_future)


@research_bp.get("/claims/<claim_id>")
def claim_detail(claim_id: str):
    repo = get_research_repository()
    claim = repo.get_claim(claim_id)
    if claim is None:
        return _error("Claim 不存在", 404, "CLAIM_NOT_FOUND")
    evidence = repo.list_evidence(claim_id, 1000)
    transitions = repo.list_transitions(claim_id, 1000)
    decision = ClaimPolicy().evaluate(claim, evidence)
    return _ok({
        "claim": claim.to_dict(), "evidence": [item.to_dict() for item in evidence],
        "transitions": [item.to_dict() for item in transitions], "sufficiency": decision.__dict__,
    })


@research_bp.route("/evidence", methods=["GET", "POST"])
def evidence():
    repo = get_research_repository()
    if request.method == "GET":
        items = repo.list_evidence(request.args.get("claim_id", ""), request.args.get("limit", 200, type=int))
        as_of = request.args.get("as_of", "")
        historical = request.args.get("historical", "false").lower() == "true"
        if as_of:
            items = [item for item in items if evidence_visible_as_of(item, as_of, historical=historical)]
        return _ok([item.to_dict() for item in items], total=len(items))
    item = EvidenceRecord.from_dict(request.get_json(silent=True) or {})
    if not item.quote:
        return _error("Evidence 必须包含可定位原文 quote", 422, "EVIDENCE_NOT_GROUNDED")
    if not repo.save_evidence(item):
        return _error("生成式检索假设不能写入 Evidence", 422, "GENERATED_EVIDENCE_REJECTED")
    return _ok(item.to_dict()), 201


@research_bp.route("/reviews", methods=["GET"])
def reviews():
    repo = get_research_repository()
    items = repo.list_reviews(request.args.get("status", "pending"), request.args.get("limit", 100, type=int))
    return _ok([item.to_dict() for item in items], total=len(items))


@research_bp.post("/reviews/<review_id>")
def resolve_review(review_id: str):
    """处理人工复核动作。

    P4.2: Review accept/reject 只记录人工动作和审计，不等于 FactOutcome
    confirmed/denied，不得直接将 Claim 升为 ACTIVE/TRUSTED。事实标签只能
    来自后续公告/财报/明确事实判定/合法撤回更正（通过 FactOutcomeStore.
    resolve_outcome）。
    """
    repo = get_research_repository()
    item = repo.get_review(review_id)
    if item is None:
        return _error("审核项不存在", 404, "REVIEW_NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "")
    reviewer = str(payload.get("reviewer") or "")
    if action not in {"accept", "reject", "request_evidence"} or not reviewer:
        return _error("action 和 reviewer 必填")
    claim = repo.get_claim(item.claim_id)
    if action == "request_evidence":
        task = ResearchTask(task_type="evidence_completion", trigger_source="manual_review", target_claim_id=item.claim_id, parent_task_id=str(payload.get("parent_task_id") or ""))
        repo.save_task(task)
        item.resolution = f"request_evidence:{task.task_id}"
    elif claim is not None:
        # P4.2: 不再直接修改 Claim 状态（不升 TRUSTED / 不降 REJECTED）
        # 只记录人工审核动作 + 创建 FactOutcome(pending) 等待后续事实判定
        from core.research.control_plane.fact_outcome import FactOutcomeStore
        fact_store = FactOutcomeStore(repo, FeedbackController(repo))
        transition_id = f"review:{review_id}:{action}"
        fact_store.record_promotion(
            transition_id=transition_id,
            claim_id=claim.claim_id,
            promotion_type="claim",
            from_status=claim.research_status,
            to_status=claim.research_status,  # 不改变状态
            evidence_snapshot={"review_action": action, "reviewer": reviewer},
            policy_version=str(payload.get("policy_version") or ""),
            promoted_at=utc_now(),
        )
        # FeedbackEvent 记录人工动作，不使用 fact_label（事实标签只能由 FactOutcome resolve 产生）
        FeedbackController(repo).record(
            FeedbackEvent(
                domain="knowledge",
                event_type="manual_review_action",
                target_id=claim.claim_id,
                metrics={"manual_action": action},
                attribution={"reviewer": reviewer, "source_type": str(payload.get("source_type") or "unknown")},
            ),
            generate_task=False,
        )
        item.resolution = action
    item.status = "resolved"
    item.reviewer = reviewer
    item.resolved_at = utc_now()
    repo.save_review(item)
    return _ok(item.to_dict())


@research_bp.route("/feedback", methods=["GET", "POST"])
def feedback():
    repo = get_research_repository()
    if request.method == "GET":
        items = repo.list_feedback(request.args.get("domain", ""), request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))
    try:
        item = FeedbackController(repo).record(FeedbackEvent.from_dict(request.get_json(silent=True) or {}))
    except ValueError as exc:
        return _error(str(exc), 422, "INVALID_FEEDBACK_DOMAIN")
    return _ok(item.to_dict()), 201


@research_bp.route("/calibrations", methods=["GET", "POST"])
def calibrations():
    repo = get_research_repository()
    controller = CalibrationController(repo)
    if request.method == "GET":
        items = repo.list_calibrations(request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))
    payload = request.get_json(silent=True) or {}
    if payload.get("action") == "fit":
        try:
            run = fit_domain(repo, str(payload.get("domain") or ""))
        except ValueError as exc:
            return _error(str(exc), 422, "INVALID_CALIBRATION_DOMAIN")
    else:
        run = controller.create_shadow(CalibrationRun.from_dict(payload))
    return _ok(run.to_dict()), 201


@research_bp.post("/calibrations/<calibration_id>")
def calibration_action(calibration_id: str):
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "")
    approver = str(payload.get("approver") or "")
    controller = CalibrationController(get_research_repository())
    try:
        if action == "activate":
            run = controller.activate(
                calibration_id,
                approver,
                test_run_id=str(payload.get("test_run_id") or ""),
                code_version=str(payload.get("code_version") or ""),
            )
        elif action == "rollback":
            run = controller.rollback(calibration_id, approver)
        else:
            run = None
    except KeyError:
        return _error("校准版本不存在", 404, "CALIBRATION_NOT_FOUND")
    except ValueError as exc:
        return _error(str(exc), 422, "CALIBRATION_GUARD_FAILED")
    if run is None:
        return _error("只支持 activate/rollback")
    return _ok(run.to_dict())


@research_bp.get("/test-runs")
def test_runs():
    """查询测试运行记录。

    测试运行记录只能由 CI 或本地测试运行器通过 TestRunStore.record_run() 写入，
    API 层不提供 POST 创建入口，防止调用方伪造 passed=true。
    """
    repo = get_research_repository()
    runs = repo.list_test_runs(request.args.get("limit", 100, type=int))
    return _ok([item.to_dict() for item in runs], total=len(runs))


@research_bp.get("/policies")
def policies():
    """查询策略版本，支持按 domain 和 status 过滤。"""
    repo = get_research_repository()
    domain = request.args.get("domain", "")
    status = request.args.get("status", "")
    items = repo.list_policies(status=status, limit=request.args.get("limit", 100, type=int), domain=domain)
    return _ok([item.to_dict() for item in items], total=len(items))


@research_bp.get("/policies/<policy_version>")
def policy_detail(policy_version: str):
    """查询单个策略版本详情。"""
    repo = get_research_repository()
    item = repo.get_policy(policy_version)
    if item is None:
        return _error("策略版本不存在", 404, "POLICY_NOT_FOUND")
    return _ok(item.to_dict())


@research_bp.get("/")
def summary():
    return _ok(get_research_repository().stats())


@research_bp.get("/acceptance-report")
def acceptance_report():
    return _ok(get_research_repository().acceptance_report(str(request.args.get("as_of") or "")))


@research_bp.get("/budget")
def budget():
    """Read the persistent daily budget and reservation audit snapshot."""
    repo = get_research_repository()
    from core.research.control_plane.budget_manager import GlobalBudgetManager

    budget_date = str(
        request.args.get("budget_date") or request.args.get("date") or ""
    )[:10]
    manager = GlobalBudgetManager(repository=repo, budget_date=budget_date or None)
    snapshot = manager.snapshot(budget_date=budget_date or None)
    payload = snapshot.to_dict()
    payload["reservations"] = manager.list_reservations(
        budget_date=budget_date or None,
        task_id=str(request.args.get("task_id") or ""),
        limit=request.args.get("limit", 100, type=int),
    )
    return _ok(payload)


# ---------------------------------------------------------------------------
# 阶段8: 投研驾驶舱增强端点
#
# 所有新增端点遵循以下约定：
# 1. 使用 _ok() 返回标准 {ok, data, meta} 结构；
# 2. 字段缺失时降级为空值或 "未知"，不抛 5xx；
# 3. 不直接暴露内部文件路径（如 db_path / state_path）。
# ---------------------------------------------------------------------------


@research_bp.get("/fact-outcomes")
def fact_outcomes():
    """查询 Knowledge 域事实结果（pending / resolved）。

    GET 参数：
        status: pending | resolved，默认 pending。
        limit:  最大返回数量，默认 100。

    Returns:
        _ok() 包装的 records 列表；status=resolved 时合并所有终态记录。
    """
    repo = get_research_repository()
    from core.research.control_plane.fact_outcome import FactOutcomeStore

    store = FactOutcomeStore(repo)
    status = str(request.args.get("status") or "pending").lower()
    limit = request.args.get("limit", 100, type=int)
    if status == "resolved":
        items = store.list_resolved(limit=limit)
    else:
        items = store.list_pending(limit=limit)
    return _ok([item.to_dict() for item in items], total=len(items), status=status)


@research_bp.get("/shadow-features")
def shadow_features():
    """查询 shadow 特征列表（8 个特征名 + 版本信息）。

    GET 参数：
        symbol: 可选证券代码，原样回显，默认空串。
        as_of:  可选 PIT 时点，原样回显，默认空串。

    Returns:
        feature_names: 8 个 shadow 特征名顺序列表。
        feature_version / policy_version: 版本标识。
        symbol / as_of: 回显参数（不强制校验，缺失时为空串）。
        description: 中文描述，便于前端展示。
    """
    from core.quant.theme.shadow_features import (
        FEATURE_VERSION,
        POLICY_VERSION,
        SHADOW_FEATURE_NAMES,
    )

    symbol = str(request.args.get("symbol") or "")
    as_of = str(request.args.get("as_of") or "")
    feature_desc = {
        "earnings_revision": "盈利预测修正",
        "expectation_gap": "预期差距",
        "eps_impact": "EPS 影响度",
        "theme_phase": "主题阶段",
        "technical_state": "技术状态",
        "phase_technical_interaction": "阶段×技术交互",
        "core_identity": "核心身份",
        "research_evidence_quality": "研究证据质量",
    }
    payload = {
        "symbol": symbol,
        "as_of": as_of,
        "feature_names": list(SHADOW_FEATURE_NAMES),
        "feature_version": FEATURE_VERSION,
        "policy_version": POLICY_VERSION,
        "descriptions": {name: feature_desc.get(name, name) for name in SHADOW_FEATURE_NAMES},
    }
    return _ok(payload, count=len(SHADOW_FEATURE_NAMES))


@research_bp.get("/market-session")
def market_session():
    """查询当前 A 股市场时段状态。

    GET 参数：
        date: 可选日期字符串（YYYY-MM-DD HH:MM:SS 或 ISO），默认当前时间。

    Returns:
        session: 当前时段枚举值（closed/pre_market/call_auction/morning/...）。
        session_label: 中文标签。
        is_trading_time: 是否处于可下单时段（MORNING/AFTERNOON/CALL_AUCTION）。
        is_trading_day: 是否为交易日。
        market_time: 市场本地时间 ISO 字符串。
        next_session: 下一时段枚举值。
    """
    from core.runtime.market_session import MarketSession, MarketSessionStateMachine

    machine = MarketSessionStateMachine()
    raw_now = str(request.args.get("date") or "")
    if raw_now:
        try:
            from datetime import datetime
            now = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
        except ValueError:
            now = None
    else:
        now = None
    if now is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

    session = machine.current_session(now)
    next_session = machine.next_session(now)
    session_label_map = {
        MarketSession.CLOSED: "休市",
        MarketSession.PRE_MARKET: "盘前",
        MarketSession.CALL_AUCTION: "集合竞价",
        MarketSession.MORNING: "上午连续交易",
        MarketSession.LUNCH_BREAK: "午休",
        MarketSession.AFTERNOON: "下午连续交易",
        MarketSession.POST_MARKET: "盘后",
    }
    payload = {
        "session": session.value,
        "session_label": session_label_map.get(session, "未知"),
        "is_trading_time": machine.is_trading_time(now),
        "is_trading_day": machine.is_trading_day(now),
        "market_time": now.isoformat(timespec="seconds"),
        "next_session": next_session.value,
    }
    return _ok(payload)


@research_bp.get("/validity-config")
def validity_config():
    """查询有效期分域配置。

    Returns:
        claim_types: Claim 类型 → {days, label} 映射。
        evidence_types: Evidence 来源类型 → {days, label} 映射。
        policy_version: 策略版本标识（预留扩展）。
        unknown_label: 无法确定时的展示文案。
    """
    from core.research.control_plane.validity_config import (
        CLAIM_TYPE_VALIDITY_DAYS,
        EVIDENCE_TYPE_VALIDITY_DAYS,
    )

    unknown_label = "未知"
    claim_type_labels = {
        "business_facts": "业务事实",
        "product_facts": "产品事实",
        "supply_chain": "供应链",
        "earnings_forecast": "盈利预测",
        "guidance": "公司指引",
        "event_impact": "事件影响",
        "policy_impact": "政策影响",
        "market_position": "市场地位",
        "technology_readiness": "技术成熟度",
    }
    evidence_type_labels = {
        "announcement": "公告",
        "annual_report": "年报",
        "quarterly_report": "季报",
        "interactive_platform": "互动平台",
        "company_website": "官网",
        "research_report": "研报",
        "news": "新闻",
        "market_data": "行情",
    }

    def _build_entry(days: int | None) -> dict:
        return {
            "days": days if days is not None else None,
            "label": "长期有效" if days is None else f"{days} 天",
        }

    payload = {
        "claim_types": {
            k: {**_build_entry(v), "name": claim_type_labels.get(k, k)}
            for k, v in CLAIM_TYPE_VALIDITY_DAYS.items()
        },
        "evidence_types": {
            k: {**_build_entry(v), "name": evidence_type_labels.get(k, k)}
            for k, v in EVIDENCE_TYPE_VALIDITY_DAYS.items()
        },
        "policy_version": "validity_config_v1",
        "unknown_label": unknown_label,
    }
    return _ok(payload)


@research_bp.get("/push-dedup")
def push_dedup():
    """查询推送去重状态（已推送记录数 + 失败重试队列）。

    GET 参数：
        date:  可选交易日期（YYYY-MM-DD），过滤当日记录；缺省返回全部。
        limit: 最大失败重试记录数，默认 100。

    Returns:
        pushed_count: 已去重推送记录总数（按 date 过滤）。
        failures: 失败重试队列列表。
        failure_count: 失败队列长度。
        date: 过滤日期回显（缺省为空串）。
        policy_version: 推送策略版本标识。
    """
    from core.runtime.push_dedup import PushDedupStore

    try:
        store = PushDedupStore()
    except Exception:
        # DB 不可用时降级为空状态，不让驾驶舱崩溃
        return _ok({
            "pushed_count": 0,
            "failures": [],
            "failure_count": 0,
            "date": "",
            "policy_version": "push_dedup_v1",
        })

    date = str(request.args.get("date") or "")
    limit = request.args.get("limit", 100, type=int)
    failures = store.list_failures(limit=limit)

    # 直接通过 SQL 查询已推送记录数（不暴露 db_path 给前端）
    # GET 请求只读，绝不调用 purge_for_date 等删除接口。
    try:
        import sqlite3
        with sqlite3.connect(str(store.db_path)) as conn:
            if date:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {store.table_name} WHERE trade_date=?",
                    (date,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {store.table_name}"
                ).fetchone()
        pushed_count = int(row[0] or 0) if row else 0
    except Exception:
        pushed_count = 0

    payload = {
        "pushed_count": pushed_count,
        "failures": failures,
        "failure_count": len(failures),
        "date": date,
        "policy_version": "push_dedup_v1",
    }
    return _ok(payload)


# ---------------------------------------------------------------------------
# P0.4: 公司研究档案 API
#
# 统一暴露公司档案与历史运行，不暴露本地路径或内部异常。
# - POST /api/research/companies/{symbol}/runs：创建一次研究运行
# - GET  /api/research/companies/{symbol}/runs：分页列出运行索引
# - GET  /api/research/companies/{symbol}/latest：读取最新运行
# - GET  /api/research/runs/{run_id}：读取单个运行详情
# - GET  /api/research/runs/{run_id}/diff?base_run_id=...：比较两个运行
#
# 约定：
# 1. 缺数据返回 200 + degraded=true；仅无效输入返回 4xx。
# 2. 错误统一返回中文摘要 + 稳定 code + trace_id。
# 3. 列表默认不返回大体积 source_snapshots；详情支持 include= 懒加载。
# 4. 不输出买入/卖出/仓位/止盈/止损等建议。
# ---------------------------------------------------------------------------


def _parse_include_param(raw: str) -> list[str]:
    """解析 include 查询参数，支持逗号分隔或多次出现。"""
    if not raw:
        return []
    items: list[str] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            items.append(piece)
    return items


@research_bp.route("/companies/<symbol>/runs", methods=["GET", "POST"])
def company_runs(symbol: str):
    """列出或创建公司研究运行。"""
    service = get_company_research_service()
    from server.services.research.company_research_service import (
        CompanyResearchServiceError,
    )
    if request.method == "GET":
        try:
            result = service.list_runs(
                symbol,
                as_of=str(request.args.get("as_of") or ""),
                status=str(request.args.get("status") or ""),
                page=request.args.get("page", 1, type=int),
                page_size=request.args.get("page_size", 20, type=int),
            )
        except CompanyResearchServiceError as exc:
            return _error(exc.message, 400, exc.code)
        return _ok(result["items"], **{
            "page": result["page"], "page_size": result["page_size"],
            "total": result["total"], "total_pages": result["total_pages"],
            "symbol": symbol,
        })
    # POST：创建新 run
    payload = request.get_json(silent=True) or {}
    as_of = str(payload.get("as_of") or request.args.get("as_of") or "")
    # 严格校验：非整数/非布尔字符串一律 4xx，禁止落到 500
    from server.services.research.company_research_service import (
        _parse_bool,
        _parse_int,
    )
    try:
        lookback_days = _parse_int(
            payload.get("lookback_days"),
            field="lookback_days", default=120,
        )
        enqueue_gaps = _parse_bool(
            payload.get("enqueue_gaps"),
            field="enqueue_gaps", default=False,
        )
    except CompanyResearchServiceError as exc:
        return _error(exc.message, 400, exc.code)
    try:
        result = service.create_run(
            symbol, as_of=as_of,
            lookback_days=lookback_days, enqueue_gaps=enqueue_gaps,
        )
    except CompanyResearchServiceError as exc:
        return _error(exc.message, 400, exc.code)
    return _ok(result), 201


@research_bp.get("/companies/<symbol>/latest")
def company_latest(symbol: str):
    """读取公司最新研究运行（不存在时返回 200 + degraded=true）。"""
    service = get_company_research_service()
    from server.services.research.company_research_service import (
        CompanyResearchServiceError,
    )
    try:
        result = service.get_latest(symbol)
    except CompanyResearchServiceError as exc:
        return _error(exc.message, 400, exc.code)
    # 即使 degraded=true 也返回 200，前端依据字段降级展示
    return _ok(result)


@research_bp.get("/runs/<run_id>")
def run_detail(run_id: str):
    """读取单个运行详情，支持 include= 懒加载。"""
    service = get_company_research_service()
    from server.services.research.company_research_service import (
        CompanyResearchServiceError,
    )
    include = _parse_include_param(str(request.args.get("include") or ""))
    try:
        result = service.get_run(run_id, include=include or None)
    except CompanyResearchServiceError as exc:
        status_code = 404 if exc.code == "RUN_NOT_FOUND" else 400
        return _error(exc.message, status_code, exc.code)
    return _ok(result)


@research_bp.get("/runs/<run_id>/diff")
def run_diff(run_id: str):
    """比较两个 run 的事实/假设/预期/估值/缺口变化。"""
    service = get_company_research_service()
    from server.services.research.company_research_service import (
        CompanyResearchServiceError,
    )
    base_run_id = str(request.args.get("base_run_id") or "")
    if not base_run_id:
        return _error("base_run_id 参数必填", 400, "MISSING_BASE_RUN_ID")
    try:
        result = service.diff_runs(run_id, base_run_id=base_run_id)
    except CompanyResearchServiceError as exc:
        status_code = 404 if exc.code == "RUN_NOT_FOUND" else 400
        return _error(exc.message, status_code, exc.code)
    return _ok(result)
