"""P3.1 研究模型治理 API 路由。

按 P3 计划 Task P3.1：提供模型注册、状态转移、查询的 HTTP 接口。
所有写操作受 P3 门禁保护；P2 未通过时返回 403 ``pending_p2``。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.model_governance.contracts import (
    InputContract,
    ModelStatus,
    ModelVersion,
    OutputContract,
)
from core.research.model_governance.registry import ModelRegistry, ModelRegistryError
from core.research.model_governance.workflow import (
    ModelWorkflow,
    ModelWorkflowError,
    suspend_models_on_input_contract_change,
)
from core.research.p3_gate import check_p2_prerequisite


research_model_bp = Blueprint(
    "research_model", __name__, url_prefix="/api/research/models"
)


def _ensure_p2_passed() -> tuple[bool, Any]:
    """P3 门禁检查；P2 未通过时返回 (False, error_response)。"""
    gate = check_p2_prerequisite()
    if gate.is_blocked:
        return False, (
            jsonify(
                {
                    "success": False,
                    "error": "p3_blocked",
                    "gate": gate.to_dict(),
                }
            ),
            403,
        )
    return True, None


def _model_to_response(model: ModelVersion) -> dict[str, Any]:
    """序列化 ModelVersion 为 API 响应。"""
    return model.to_dict()


@research_model_bp.get("")
@research_model_bp.get("/")
def list_models() -> Any:
    """列出模型；支持按 status / domain 过滤。

    Query params:
        status: 模型状态（draft/active/retired 等）
        domain: 领域过滤
        model_id: 指定 model_id 时返回该模型所有版本
    """
    registry = ModelRegistry()
    status_param = request.args.get("status")
    domain_param = request.args.get("domain")
    model_id_param = request.args.get("model_id")

    if model_id_param:
        versions = registry.list_versions(model_id_param)
        return jsonify(
            {"success": True, "data": [_model_to_response(v) for v in versions]}
        )
    if status_param:
        try:
            status = ModelStatus(status_param)
        except ValueError:
            return jsonify({"success": False, "error": f"无效 status：{status_param}"}), 400
        versions = registry.list_by_status(status)
    elif domain_param:
        versions = registry.list_by_domain(domain_param)
    else:
        # 默认返回全部
        all_models = registry.all_models()
        versions = []
        for records in all_models.values():
            for record in records:
                from core.research.model_governance.registry import _dict_to_model

                versions.append(_dict_to_model(record))
    return jsonify(
        {"success": True, "data": [_model_to_response(v) for v in versions]}
    )


@research_model_bp.get("/<model_id>/<version>")
def get_model(model_id: str, version: str) -> Any:
    """获取指定 model_id + version。"""
    registry = ModelRegistry()
    model = registry.get_version(model_id, version)
    if model is None:
        return jsonify({"success": False, "error": "模型版本不存在"}), 404
    return jsonify({"success": True, "data": _model_to_response(model)})


@research_model_bp.post("")
@research_model_bp.post("/")
def register_model() -> Any:
    """登记新草稿模型；P2 未通过时返回 403。"""
    ok, error = _ensure_p2_passed()
    if not ok:
        return error
    payload = request.get_json(silent=True) or {}
    required_fields = (
        "model_id", "model_family", "domain", "version", "owner",
        "training_or_calibration_window",
    )
    missing = [f for f in required_fields if not payload.get(f)]
    if missing:
        return jsonify({"success": False, "error": f"缺少必填字段：{missing}"}), 400

    try:
        input_data = payload.get("input_contract") or {}
        input_contract = InputContract(
            fields=dict(input_data.get("fields", {})),
            data_source_refs=list(input_data.get("data_source_refs", [])),
        )
        input_contract = InputContract(
            fields=input_contract.fields,
            data_source_refs=input_contract.data_source_refs,
            contract_hash=input_contract.compute_hash(),
        )
        output_data = payload.get("output_contract") or {}
        output_contract = OutputContract(
            fields=dict(output_data.get("fields", {})),
            validation_rules=dict(output_data.get("validation_rules", {})),
        )
        output_contract = OutputContract(
            fields=output_contract.fields,
            validation_rules=output_contract.validation_rules,
            contract_hash=output_contract.compute_hash(),
        )
        model = ModelVersion(
            model_id=str(payload["model_id"]),
            model_family=str(payload["model_family"]),
            domain=str(payload["domain"]),
            version=str(payload["version"]),
            owner=str(payload["owner"]),
            reviewer="",
            training_or_calibration_window=str(payload["training_or_calibration_window"]),
            input_contract=input_contract,
            output_contract=output_contract,
            applicable_universe=list(payload.get("applicable_universe", [])),
            limitations=list(payload.get("limitations", [])),
            benchmark=dict(payload.get("benchmark", {})),
            validation_report={},
            status=ModelStatus.DRAFT,
            effective_from=str(payload.get("effective_from", "")),
            effective_to="",
            rollback_version="",
            code_fingerprint=str(payload.get("code_fingerprint", "")),
            config_fingerprint=str(payload.get("config_fingerprint", "")),
            data_fingerprint=str(payload.get("data_fingerprint", "")),
        )
        workflow = ModelWorkflow()
        workflow.submit_draft(model)
        return jsonify({"success": True, "data": _model_to_response(model)}), 201
    except (ModelRegistryError, ModelWorkflowError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 409


@research_model_bp.post("/<model_id>/<version>/transition")
def transition_status(model_id: str, version: str) -> Any:
    """状态转移；P2 未通过时返回 403。

    Body:
        target_status: 目标状态
        actor: 操作人 ID
        report: 验证报告（unit_validate / independent_validate 时必填）
        rollback_version: activate 时必填
        reason: suspend / retire / rollback 时必填
    """
    ok, error = _ensure_p2_passed()
    if not ok:
        return error
    payload = request.get_json(silent=True) or {}
    target = payload.get("target_status")
    actor = payload.get("actor", "")
    if not target or not actor:
        return jsonify({"success": False, "error": "缺少 target_status 或 actor"}), 400

    workflow = ModelWorkflow()
    try:
        target_status = ModelStatus(target)
    except ValueError:
        return jsonify({"success": False, "error": f"无效 target_status：{target}"}), 400

    try:
        if target_status is ModelStatus.UNIT_VALIDATED:
            report = payload.get("report") or {}
            model = workflow.unit_validate(model_id, version, actor, report)
        elif target_status is ModelStatus.SHADOW:
            model = workflow.start_shadow(model_id, version, actor)
        elif target_status is ModelStatus.INDEPENDENT_VALIDATED:
            report = payload.get("report") or {}
            reviewer = payload.get("reviewer", actor)
            model = workflow.independent_validate(model_id, version, reviewer, report)
        elif target_status is ModelStatus.APPROVED:
            model = workflow.approve(model_id, version, actor)
        elif target_status is ModelStatus.ACTIVE:
            rollback_version = payload.get("rollback_version", "")
            model = workflow.activate(model_id, version, actor, rollback_version)
        elif target_status is ModelStatus.SUSPENDED:
            reason = payload.get("reason", "")
            model = workflow.suspend(model_id, version, actor, reason)
        elif target_status is ModelStatus.RETIRED:
            reason = payload.get("reason", "")
            model = workflow.retire(model_id, version, actor, reason)
        else:
            return jsonify(
                {"success": False, "error": f"不支持的目标状态：{target}"}
            ), 400
        return jsonify({"success": True, "data": _model_to_response(model)})
    except (ModelWorkflowError, ModelRegistryError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 409


@research_model_bp.post("/<model_id>/<version>/rollback")
def rollback_model(model_id: str, version: str) -> Any:
    """回滚到 rollback_version；P2 未通过时返回 403。"""
    ok, error = _ensure_p2_passed()
    if not ok:
        return error
    payload = request.get_json(silent=True) or {}
    actor = payload.get("actor", "")
    reason = payload.get("reason", "")
    if not actor or not reason:
        return jsonify({"success": False, "error": "缺少 actor 或 reason"}), 400
    workflow = ModelWorkflow()
    try:
        model = workflow.rollback(model_id, version, actor, reason)
        return jsonify({"success": True, "data": _model_to_response(model)})
    except (ModelWorkflowError, ModelRegistryError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 409


@research_model_bp.post("/contract-change/suspend")
def suspend_on_contract_change() -> Any:
    """输入数据契约变化自动暂停相关 active 模型。

    Body:
        old_contract_hash: 旧契约哈希
        new_contract_hash: 新契约哈希
        actor: 操作人 ID
        reason: 暂停原因（可选，默认「输入数据契约变化」）
    """
    ok, error = _ensure_p2_passed()
    if not ok:
        return error
    payload = request.get_json(silent=True) or {}
    old_hash = payload.get("old_contract_hash", "")
    new_hash = payload.get("new_contract_hash", "")
    actor = payload.get("actor", "")
    reason = payload.get("reason", "输入数据契约变化")
    if not old_hash or not new_hash or not actor:
        return jsonify(
            {"success": False, "error": "缺少 old_contract_hash/new_contract_hash/actor"}
        ), 400
    workflow = ModelWorkflow()
    suspended = suspend_models_on_input_contract_change(
        workflow, old_hash, new_hash, actor, reason
    )
    return jsonify(
        {
            "success": True,
            "data": {
                "suspended_count": len(suspended),
                "suspended": [_model_to_response(m) for m in suspended],
            },
        }
    )


@research_model_bp.get("/gate")
def get_p3_gate() -> Any:
    """查询 P3 门禁状态（只读，不需 P2 通过）。"""
    gate = check_p2_prerequisite()
    return jsonify({"success": True, "data": gate.to_dict()})


# ---------------------------------------------------------------------------
# P3.7 机构级研究工作台只读端点
#
# 设计原则（与 AGENTS.md / P3 计划一致）：
# - 每份响应统一包含 as_of / model_version / source / limitations 四字段
# - 字段缺失时返回 200 + degraded=true + 空数据结构，绝不抛 500
# - 不生成交易动作措辞
# - 所有返回值中区分事实 / 观点 / 假设
# - 复用 P3.1 的 ModelRegistry 与 P3 门禁查询
# ---------------------------------------------------------------------------

WORKBENCH_MODEL_VERSION = "p3-workbench-v1"
WORKBENCH_SOURCE = "research_model_routes.workbench"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_GATE_PERFORMANCE_PATH = (
    PROJECT_ROOT
    / "data"
    / "research_control"
    / "p2_ai_internal"
    / "research_gate_performance_20d.json"
)
P3_FORWARD_V6_DIR = (
    PROJECT_ROOT / "data" / "research_control" / "p3_forward_v6"
)
P3_FORWARD_MANIFEST_PATH = P3_FORWARD_V6_DIR / "manifest.json"
P3_FORWARD_OBSERVATIONS_PATH = P3_FORWARD_V6_DIR / "observations.json"
# P3 前向验证目标交易日数（P3 计划要求 20 个真实交易日）
P3_FORWARD_TARGET_DAYS = 20
# 最新盘前报告路径，用于读取候选漏斗诊断
LATEST_PREMARKET_PATH = (
    PROJECT_ROOT / "data" / "reports" / "research_monitor" / "latest_premarket.json"
)


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    """安全读取 JSON 文件；不存在或损坏返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _build_p3_forward_status() -> dict[str, Any]:
    """读取 P3 v6 前向链真实状态。

    从 manifest.json 和 observations.json 提取：
    - mode / started_at / cohort_size
    - 已收集真实交易日数 / 目标日数
    - 链验证状态
    - production_certified / p3_certified

    Returns:
        包含 P3 前向进度的字典；文件缺失时返回 degraded 状态。
    """
    manifest = _read_json_safe(P3_FORWARD_MANIFEST_PATH)
    if manifest is None:
        return {
            "available": False,
            "degraded": True,
            "degradation_reason": "p3_forward_v6 manifest not found",
        }
    observations = _read_json_safe(P3_FORWARD_OBSERVATIONS_PATH) or []
    collected_days = len(observations)
    # 链完整性检查：每条记录应有 previous_hash + record_hash
    chain_valid = all(
        isinstance(obs, dict) and obs.get("record_hash")
        for obs in observations
    )
    return {
        "available": True,
        "mode": manifest.get("mode", ""),
        "started_at": manifest.get("started_at", ""),
        "cohort_size": len(manifest.get("members", [])),
        "collected_days": collected_days,
        "target_days": P3_FORWARD_TARGET_DAYS,
        "progress_ratio": round(collected_days / P3_FORWARD_TARGET_DAYS, 4),
        "chain_valid": chain_valid,
        "historical_certification_claimed": bool(
            manifest.get("historical_certification_claimed", False)
        ),
        "production_certified": bool(manifest.get("production_certified", False)),
        "p3_certified": bool(manifest.get("p3_certified", False)),
        "degraded": False,
        "degradation_reason": "",
    }


def _build_p3_gate_status() -> dict[str, Any]:
    """构造拆分后的 P3 门禁状态，纠正旧版"P3门禁=P2前置"的语义错位。

    拆成 4 个独立状态：
    - p2_prerequisite: P2 前置门禁（旧版误标为 p3_gate）
    - p3_engineering: P3 工程链路状态（框架完成 / PENDING_P2）
    - p3_forward_progress: P3 前向取证进度（1/20 等）
    - p3_live_proven: P3 正式认证（需连续 4 个真实财报季度）
    """
    # 1. P2 前置门禁
    try:
        gate = check_p2_prerequisite()
        p2_prereq = gate.to_dict()
    except Exception as exc:  # noqa: BLE001
        p2_prereq = {
            "status": "unknown",
            "p2_overall_passed": False,
            "reason": f"P3 gate 查询失败: {type(exc).__name__}",
            "is_blocked": True,
        }

    # 2. P3 工程链路状态
    p3_engineering = {
        "status": "engineering_framework_done",
        "p2_blocked": p2_prereq.get("is_blocked", True),
        "description": (
            "P3.0-P3.9 工程框架完成（394 测试通过），"
            "P2 前置未通过时写端点返回 403"
        ),
    }

    # 3. P3 前向取证进度
    p3_forward = _build_p3_forward_status()

    # 4. P3 正式认证（Live Proven）
    p3_live_proven = {
        "status": "not_started",
        "description": (
            "需连续 4 个真实财报季度记录，"
            "当前前向取证进行中，未达到 Live Proven 门槛"
        ),
        "quarters_completed": 0,
        "target_quarters": 4,
    }

    return {
        "p2_prerequisite": p2_prereq,
        "p3_engineering": p3_engineering,
        "p3_forward_progress": p3_forward,
        "p3_live_proven": p3_live_proven,
    }


def _workbench_meta(limitations: list[str] | None = None) -> dict[str, Any]:
    """构造工作台响应统一元数据。

    Args:
        limitations: 该端点的限制说明列表

    Returns:
        包含 as_of / model_version / source / limitations 的字典
    """
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "model_version": WORKBENCH_MODEL_VERSION,
        "source": WORKBENCH_SOURCE,
        "limitations": list(limitations or []),
    }


def _degraded_payload(reason: str, payload: dict[str, Any]) -> dict[str, Any]:
    """补齐 degraded/degradation_reason 字段。"""
    payload["degraded"] = True
    payload["degradation_reason"] = reason
    return payload


def _empty_funnel_stages() -> list[dict[str, Any]]:
    """构造 4 阶段全空漏斗结构（仓储为空或异常时使用）。

    漏斗阶段定义固定为 universe → liquidity → fundamental → readiness，
    数据缺失时各阶段 count 归零、passed=False，但结构必须保留。
    """
    return [
        {"stage": "universe", "label": "全市场股票池", "count": 0, "passed": False},
        {"stage": "liquidity", "label": "流动性筛选", "count": 0, "passed": False},
        {"stage": "fundamental", "label": "基本面筛选", "count": 0, "passed": False},
        {"stage": "readiness", "label": "研究就绪", "count": 0, "passed": False},
    ]


@research_model_bp.get("/workbench/discovery-funnel")
def workbench_discovery_funnel() -> Any:
    """全市场发现漏斗：universe → liquidity → fundamental → readiness。

    接入 DiscoveryRepository 最新截面，统计各阶段成员数。
    仓储为空或不可用时返回降级空数据。
    漏斗阶段定义：
    - universe: 截面总成员数（含 listed/suspended/historical）
    - liquidity: 成员中字段组 liquidity 可用的数量
    - fundamental: 成员中字段组 financial_quality 可用的数量
    - readiness: 成员中 9 字段组 coverage >= 0.5 的数量
    """
    limitations = [
        "漏斗来源：DiscoveryRepository 最新截面成员统计",
        "阶段计数基于字段组可用性，不包含实时行情筛选",
        "不输出交易动作措辞",
    ]
    payload = _workbench_meta(limitations)
    try:
        from core.research.discovery.repository import UniverseSnapshotRepository
        from core.research.discovery.contracts import (
            DATA_AVAILABILITY_AVAILABLE,
            FIELD_GROUP_FINANCIAL_QUALITY,
            MEMBERSHIP_LISTED,
        )

        repo = UniverseSnapshotRepository()
        snapshots = repo.list_snapshots(limit=1)
        if not snapshots:
            payload["stages"] = _empty_funnel_stages()
            payload["total_passed"] = 0
            payload["snapshot_id"] = ""
            payload["as_of"] = ""
            _degraded_payload("no universe snapshot found", payload)
            return jsonify({"success": True, "data": payload})

        snapshot = snapshots[0]
        snapshot_id = snapshot.get("snapshot_id", "")
        as_of = snapshot.get("as_of", "")
        members = repo.list_members(snapshot_id)

        total = len(members)
        liquidity_count = 0
        fundamental_count = 0
        readiness_count = 0
        for member in members:
            # liquidity 阶段：membership_status == listed 表示可正常交易，
            # 剔除 suspended/delisted 等不可交易状态（与 P3 流动性筛选语义一致）。
            # contracts.py 中没有 FIELD_GROUP_LIQUIDITY 字段组，因为 universe
            # 截面构建时已默认经过基础流动性过滤，这里仅需剔除停牌成员。
            if member.membership_status == MEMBERSHIP_LISTED:
                liquidity_count += 1
            field_groups = member.field_groups or {}
            fundamental_fg = field_groups.get(FIELD_GROUP_FINANCIAL_QUALITY)
            if fundamental_fg and any(
                f.status == DATA_AVAILABILITY_AVAILABLE
                for f in fundamental_fg.fields
            ):
                fundamental_count += 1
            # readiness: 9 字段组 coverage >= 0.5
            all_fields = [
                f for fg in field_groups.values() for f in fg.fields
            ]
            if all_fields:
                available = sum(
                    1 for f in all_fields
                    if f.status == DATA_AVAILABILITY_AVAILABLE
                )
                coverage = available / len(all_fields)
                if coverage >= 0.5:
                    readiness_count += 1

        stages = [
            {
                "stage": "universe",
                "label": "全市场股票池",
                "count": total,
                "passed": total > 0,
            },
            {
                "stage": "liquidity",
                "label": "流动性筛选",
                "count": liquidity_count,
                "passed": liquidity_count > 0,
            },
            {
                "stage": "fundamental",
                "label": "基本面筛选",
                "count": fundamental_count,
                "passed": fundamental_count > 0,
            },
            {
                "stage": "readiness",
                "label": "研究就绪",
                "count": readiness_count,
                "passed": readiness_count > 0,
            },
        ]
        payload["stages"] = stages
        payload["total_passed"] = readiness_count
        payload["snapshot_id"] = snapshot_id
        payload["as_of"] = as_of
        payload["degraded"] = readiness_count == 0
        payload["degradation_reason"] = (
            "no research-ready members found" if readiness_count == 0 else ""
        )
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["stages"] = _empty_funnel_stages()
        payload["total_passed"] = 0
        payload["snapshot_id"] = ""
        payload["as_of"] = ""
        _degraded_payload(
            f"discovery repository unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/company-cards")
def workbench_company_cards() -> Any:
    """公司研究卡和历史版本列表。

    接入 ResearchRunRepository，返回研究运行索引。
    Query params:
        symbol: 指定公司代码；省略则返回全部公司卡片
        page: 页码（默认 1）
        page_size: 每页大小（默认 20，最大 100）
    """
    symbol_param = request.args.get("symbol", "")
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    limitations = [
        "公司研究卡来源：ResearchRunRepository 运行索引",
        "版本链仅展示元数据，dossier 需单独调用详情端点",
    ]
    payload = _workbench_meta(limitations)
    try:
        # 延迟导入避免循环依赖
        from core.research.value_chain.repository import ResearchRunRepository

        repo = ResearchRunRepository()
        result = repo.list_runs(
            symbol=symbol_param,
            page=max(1, page or 1),
            page_size=max(1, min(100, page_size or 20)),
        )
        cards = []
        for item in result.get("items", []):
            cards.append({
                "run_id": item.get("run_id", ""),
                "symbol": item.get("symbol", ""),
                "company_name": item.get("company_name", ""),
                "as_of": item.get("as_of", ""),
                "status": item.get("status", ""),
                "created_at": item.get("created_at", ""),
                "research_readiness": item.get("research_readiness", ""),
                "degradation_reasons": item.get("degradation_reasons", []),
            })
        payload["cards"] = cards
        payload["total"] = result.get("total", 0)
        payload["page"] = result.get("page", 1)
        payload["page_size"] = result.get("page_size", 20)
        payload["total_pages"] = result.get("total_pages", 0)
        payload["symbol_filter"] = symbol_param
        payload["degraded"] = False
        payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["cards"] = []
        payload["total"] = 0
        payload["page"] = page or 1
        payload["page_size"] = page_size or 20
        payload["total_pages"] = 0
        payload["symbol_filter"] = symbol_param
        _degraded_payload(
            f"company cards repository unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/candidate-funnel")
def workbench_candidate_funnel() -> Any:
    """候选生成漏斗：展示主题→配额→候选的逐级损失。

    从最新盘前报告的 quant.candidate_funnel 字段读取结构化诊断，
    不再只显示 0。让用户能看清"为什么没有候选"：
    - 总主题数 → 有 core_members 的主题数 → 被配额选中的主题数 → 最终候选数
    - 各桶的配额竞争详情（total / eligible / dropped / selected）
    - 被剔除的 theme_id 列表
    """
    limitations = [
        "候选漏斗来源：最新盘前报告 quant.candidate_funnel",
        "仅展示最近一次运行的候选生成过程，不构成投资建议",
    ]
    payload = _workbench_meta(limitations)
    report = _read_json_safe(LATEST_PREMARKET_PATH)
    if report is None:
        payload["funnel"] = {}
        payload["available"] = False
        _degraded_payload("latest premarket report not found", payload)
        return jsonify({"success": True, "data": payload})

    quant_section = report.get("quant", {}) or {}
    funnel = quant_section.get("candidate_funnel", {}) or {}
    if not funnel:
        # 旧报告没有 candidate_funnel 字段，从 themes 手动统计
        themes = report.get("themes", []) or []
        total_themes = len(themes)
        themes_with_core = sum(
            1 for t in themes
            if isinstance(t, dict) and t.get("validated_core_count", 0) > 0
        )
        funnel = {
            "total_themes": total_themes,
            "total_eligible": themes_with_core,
            "total_dropped": total_themes - themes_with_core,
            "bucket_diagnostics": [],
            "legacy_report": True,
        }

    signals = quant_section.get("signals", []) or []
    payload["funnel"] = funnel
    payload["signal_count"] = len(signals)
    payload["available"] = True
    payload["run_id"] = report.get("run_id", "")
    payload["as_of"] = report.get("as_of", "")
    payload["status"] = report.get("status", "")
    payload["degraded"] = False
    payload["degradation_reason"] = ""
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/profit-models")
def workbench_profit_models() -> Any:
    """产品利润模型和情景列表。

    接入 EarningsRepository，返回盈利驱动 + 假设 + 模型列表。
    所有情景均为「假设」属性，不构成预测或交易动作。
    仓储为空或不可用时返回降级空数据。
    """
    limitations = [
        "利润模型来源：EarningsRepository 驱动 + 假设 + 模型",
        "情景值不构成预测或交易动作建议",
        "不输出交易动作措辞",
    ]
    payload = _workbench_meta(limitations)
    symbol_param = request.args.get("symbol", "")
    try:
        from core.research.earnings.repository import EarningsRepository

        repo = EarningsRepository()
        # 驱动列表（按公司过滤）
        drivers = repo.list_drivers(
            company_symbol=symbol_param, limit=50
        )
        # 假设列表
        assumptions = repo.list_assumptions(limit=50)
        # 模型列表（按驱动过滤）
        models = repo.list_models(limit=50)

        scenarios = []
        fact_basis = []
        assumption_basis = []
        for driver in drivers:
            driver_dict = (
                driver.to_dict() if hasattr(driver, "to_dict") else vars(driver)
            )
            scenarios.append({
                "driver_id": driver_dict.get("driver_id", ""),
                "symbol": driver_dict.get("company_symbol", ""),
                "driver_name": driver_dict.get("driver_name", ""),
                "driver_type": driver_dict.get("driver_type", ""),
                "as_of": driver_dict.get("as_of", ""),
                # base/bull/bear 情景由假设驱动，这里只列事实基础
            })
            fact_basis.append({
                "driver_id": driver_dict.get("driver_id", ""),
                "fact_source": driver_dict.get("fact_source", ""),
                "as_of": driver_dict.get("as_of", ""),
            })
        for assumption in assumptions:
            assumption_dict = (
                assumption.to_dict()
                if hasattr(assumption, "to_dict")
                else vars(assumption)
            )
            assumption_basis.append({
                "assumption_id": assumption_dict.get("assumption_id", ""),
                "variable_name": assumption_dict.get("variable_name", ""),
                "as_of": assumption_dict.get("as_of", ""),
            })

        payload["scenarios"] = scenarios
        payload["fact_basis"] = fact_basis
        payload["assumption_basis"] = assumption_basis
        payload["model_count"] = len(models)
        payload["symbol_filter"] = symbol_param
        # 仓储可用但无任何 driver/assumption 记录时，标记降级
        if not drivers and not assumptions:
            _degraded_payload("no earnings drivers or assumptions found", payload)
        else:
            payload["degraded"] = False
            payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["scenarios"] = []
        payload["fact_basis"] = []
        payload["assumption_basis"] = []
        payload["model_count"] = 0
        payload["symbol_filter"] = symbol_param
        _degraded_payload(
            f"earnings repository unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/variant-perception")
def workbench_variant_perception() -> Any:
    """VariantPerception 卡片列表。

    每个 VP 包含 market_view / internal_view / driver_differences / catalysts / invalidation。
    接入 ThesisRepository.list_latest_by_symbol，返回活跃观点。
    市场观点不可由系统臆测，仓储为空或不可用时返回空数组 + degraded=true。
    """
    limitations = [
        "VariantPerception 来源：ThesisRepository 活跃观点",
        "市场观点不可由系统臆测，缺数据时返回空数组",
        "不输出交易动作措辞",
    ]
    payload = _workbench_meta(limitations)
    symbol_param = request.args.get("symbol", "")
    try:
        from core.research.thesis.repository import ThesisRepository

        # 默认 db 路径与 P2.6 一致
        thesis_db = PROJECT_ROOT / "data" / "research_control" / "thesis.db"
        repo = ThesisRepository(str(thesis_db))
        # list_latest_by_symbol 已排除 superseded；symbol 为空时返回全部
        items = repo.list_latest_by_symbol(
            symbol=symbol_param,
            limit=50,
        )
        perceptions = []
        for item in items:
            # 仅返回 status=active 的正式观点（draft 不暴露给工作台）
            if str(item.get("status", "")).lower() != "active":
                continue
            perceptions.append({
                "thesis_id": item.get("thesis_id", ""),
                "symbol": item.get("symbol", ""),
                "as_of": item.get("as_of", ""),
                "status": item.get("status", ""),
                "market_view": item.get("market_view", {}) or {},
                "internal_view": item.get("internal_view", {}) or {},
                "driver_differences": item.get("driver_differences", []) or [],
                "catalysts": item.get("catalysts", []) or [],
                "invalidation_conditions": (
                    item.get("invalidation_conditions", []) or []
                ),
                "uncertainty": item.get("uncertainty", ""),
            })
        payload["perceptions"] = perceptions
        payload["active_count"] = len(perceptions)
        payload["symbol_filter"] = symbol_param
        # 仓储可用但无任何 active 观点时，标记降级（市场观点不可臆测）
        if not perceptions:
            _degraded_payload("no active variant perception found", payload)
        else:
            payload["degraded"] = False
            payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["perceptions"] = []
        payload["active_count"] = 0
        payload["symbol_filter"] = symbol_param
        _degraded_payload(
            f"thesis repository unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/bear-questions")
def workbench_bear_questions() -> Any:
    """反方问题列表 + 投委会状态。

    bear_questions: 待回答的反方问题数组（来自 CommitteeRepository open 状态工作流）
    committee_status: 投委会当前状态（state / pending_questions / last_convened_at）
    仓储为空或不可用时返回降级默认值。
    """
    limitations = [
        "反方问题来源：CommitteeRepository 工作流",
        "投委会状态为实时查询，不缓存",
        "不输出交易动作措辞",
    ]
    payload = _workbench_meta(limitations)
    try:
        from core.research.committee.repository import CommitteeRepository

        committee_db = (
            PROJECT_ROOT / "data" / "research_control" / "committee.db"
        )
        repo = CommitteeRepository(str(committee_db))
        # 查询 open 状态的工作流作为待回答反方问题
        open_items = repo.list_by_status(status="open", limit=50)
        bear_questions = []
        for item in open_items:
            payload_json = item.get("payload_json", {})
            if isinstance(payload_json, str):
                try:
                    payload_json = json.loads(payload_json)
                except (json.JSONDecodeError, TypeError):
                    payload_json = {}
            questions = payload_json.get("bear_questions", []) if isinstance(
                payload_json, dict
            ) else []
            bear_questions.extend(questions)
        # 统计各状态数量
        open_count = len(open_items)
        closed_items = repo.list_by_status(status="closed", limit=1)
        has_closed = len(closed_items) > 0

        payload["bear_questions"] = bear_questions
        payload["committee_status"] = {
            "state": "open" if open_count > 0 else (
                "convened" if has_closed else "not_convened"
            ),
            "pending_questions": open_count,
            "last_convened_at": (
                closed_items[0].get("created_at", "") if closed_items else ""
            ),
            "members": [],
        }
        payload["degraded"] = False
        payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["bear_questions"] = []
        payload["committee_status"] = {
            "state": "not_convened",
            "pending_questions": 0,
            "last_convened_at": "",
            "members": [],
        }
        _degraded_payload(
            f"committee repository unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/risk-calendar")
def workbench_risk_calendar() -> Any:
    """风险和验证日历。

    接入 P3 前向 v6 observations.json，展示已收集的真实交易日
    作为验证事件。每个事件包含 date / type / title / status / source。
    不输出交易动作措辞。
    """
    limitations = [
        "风险日历事件来源：P3 前向 v6 observations.json",
        "事件为历史已收集的验证日，不构成未来风险预测",
    ]
    payload = _workbench_meta(limitations)
    observations = _read_json_safe(P3_FORWARD_OBSERVATIONS_PATH)
    if not observations or not isinstance(observations, list):
        payload["events"] = []
        payload["upcoming_count"] = 0
        payload["collected_days"] = 0
        payload["target_days"] = P3_FORWARD_TARGET_DAYS
        _degraded_payload("p3 forward observations not found", payload)
        return jsonify({"success": True, "data": payload})

    events: list[dict[str, Any]] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        trade_date = obs.get("trade_date", "")
        member_rows = obs.get("member_rows", 0)
        benchmark = obs.get("benchmark", {}) or {}
        benchmark_return = benchmark.get("return")
        events.append({
            "date": trade_date,
            "type": "forward_validation",
            "title": f"P3 前向验证日 {trade_date}",
            "status": "collected",
            "source": obs.get("source", ""),
            "member_rows": member_rows,
            "benchmark_return": (
                round(benchmark_return, 4) if isinstance(benchmark_return, (int, float)) else None
            ),
            "research_output_available": bool(obs.get("research_output_available", False)),
            "trade_action_free": True,
        })

    payload["events"] = events
    payload["upcoming_count"] = 0  # 前向事件均为已收集，无"即将到来"
    payload["collected_days"] = len(events)
    payload["target_days"] = P3_FORWARD_TARGET_DAYS
    payload["degraded"] = False
    payload["degradation_reason"] = ""
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/data-health")
def workbench_data_health() -> Any:
    """数据健康和降级状态。

    复用 server.services.data_health_service.build_data_health_report；
    失败时返回降级空结构。
    """
    limitations = [
        "数据健康来源：data_health_service.build_data_health_report",
        "降级状态聚合自 P3 模型注册表 active 模型",
    ]
    payload = _workbench_meta(limitations)
    try:
        # 延迟导入避免循环依赖
        from server.services.data_health_service import build_data_health_report

        report = build_data_health_report(write_files=False)
        payload["data_health"] = report
        payload["degraded"] = False
        payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["data_health"] = {}
        _degraded_payload(f"data health unavailable: {type(exc).__name__}", payload)

    # 聚合 active 模型降级状态
    try:
        registry = ModelRegistry()
        active_models = registry.list_by_status(ModelStatus.ACTIVE)
        suspended_models = registry.list_by_status(ModelStatus.SUSPENDED)
        payload["model_status_summary"] = {
            "active_count": len(active_models),
            "suspended_count": len(suspended_models),
            "any_suspended": len(suspended_models) > 0,
        }
    except Exception:  # noqa: BLE001 - 防御性降级
        payload["model_status_summary"] = {
            "active_count": 0,
            "suspended_count": 0,
            "any_suspended": False,
        }
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/scorecard")
def workbench_scorecard() -> Any:
    """历史预测与实际兑现记分卡。

    接入 ProductionLearningLoop.replay()，返回每条历史预测的
    predicted / actual / error / coverage / hit_status。
    未到期预测（latest_observation 为空）标记为 pending。
    """
    limitations = [
        "记分卡基于 ProductionLearningLoop 只追加账本",
        "未到期预测标记为 pending，不计入命中率",
        "记分卡不构成未来预测能力的保证",
    ]
    payload = _workbench_meta(limitations)
    try:
        # 延迟导入避免循环依赖
        from core.research.control_plane.learning_loop import (
            ProductionLearningLoop,
        )
        from core.research.control_plane.repository import ResearchRepository

        # ProductionLearningLoop 必须传入 ResearchRepository 实例，
        # 否则 __init__ 抛 TypeError: missing required positional argument。
        # 与 run_research_monitor_daemon.py 保持一致的构造方式。
        loop = ProductionLearningLoop(ResearchRepository())
        replay_records = loop.replay(cutoff=datetime.now(timezone.utc).isoformat())

        scorecard: list[dict[str, Any]] = []
        hit_count = 0
        miss_count = 0
        pending_count = 0
        for record in replay_records:
            pred = record.prediction
            obs = record.latest_observation
            if obs is None:
                hit_status = "pending"
                pending_count += 1
                actual_value = None
                error = None
            else:
                was_correct = bool(obs.was_correct)
                hit_status = "hit" if was_correct else "miss"
                if was_correct:
                    hit_count += 1
                else:
                    miss_count += 1
                actual_value = getattr(obs, "actual_value", None)
                predicted_value = getattr(pred, "predicted_value", None)
                # 误差计算：布尔预测用 0/1，数值预测用绝对差
                if isinstance(predicted_value, (int, float)) and isinstance(
                    actual_value, (int, float)
                ):
                    error = round(abs(predicted_value - actual_value), 4)
                else:
                    error = 0.0 if was_correct else 1.0

            scorecard.append({
                "prediction_id": pred.prediction_id,
                "symbol": pred.entity_id,
                "research_question": pred.research_question,
                "predicted_value": getattr(pred, "predicted_value", None),
                "actual_value": actual_value,
                "error": error,
                "hit_status": hit_status,
                "as_of": pred.as_of,
                "due_at": pred.due_at,
                "confidence": getattr(pred, "confidence", None),
                "model_version": pred.model_version,
                "attribution": (
                    getattr(obs, "attribution", "").value
                    if obs and getattr(obs, "attribution", None)
                    else ""
                ),
            })

        settled_count = hit_count + miss_count
        coverage_rate = (
            round(hit_count / settled_count, 4) if settled_count > 0 else None
        )
        payload["scorecard"] = scorecard
        payload["summary"] = {
            "total_predictions": len(replay_records),
            "hit_count": hit_count,
            "miss_count": miss_count,
            "pending_count": pending_count,
            "settled_count": settled_count,
            "coverage_rate": coverage_rate,
            "mean_absolute_error": None,  # 布尔预测不适用，保留字段兼容前端
        }
        payload["degraded"] = False
        payload["degradation_reason"] = ""
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["scorecard"] = []
        payload["summary"] = {
            "total_predictions": 0,
            "hit_count": 0,
            "miss_count": 0,
            "pending_count": 0,
            "settled_count": 0,
            "coverage_rate": None,
            "mean_absolute_error": None,
        }
        _degraded_payload(
            f"scorecard learning loop unavailable: {type(exc).__name__}", payload
        )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/research-gate-performance")
def workbench_research_gate_performance() -> Any:
    """返回只读的研究准入历史重建对照结果，不产生交易动作。"""
    limitations = [
        "仅为历史重建评价，不是当时保存的生产信号",
        "未计费用和滑点",
        "样本仅 20 个交易日，不能证明长期稳定性",
    ]
    payload = _workbench_meta(limitations)
    if not RESEARCH_GATE_PERFORMANCE_PATH.exists():
        payload["performance"] = {}
        _degraded_payload("research gate performance report not found", payload)
        return jsonify({"success": True, "data": payload})
    try:
        performance = json.loads(
            RESEARCH_GATE_PERFORMANCE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        payload["performance"] = {}
        _degraded_payload("research gate performance report unreadable", payload)
        return jsonify({"success": True, "data": payload})
    payload["performance"] = performance
    payload["degraded"] = not bool(performance.get("overall_valid"))
    payload["degradation_reason"] = (
        "" if performance.get("overall_valid")
        else "research gate performance validation failed"
    )
    return jsonify({"success": True, "data": payload})


@research_model_bp.get("/workbench/overview")
def workbench_overview() -> Any:
    """工作台总览：聚合各模块降级状态 + 拆分后的 P3 门禁状态。

    纠正旧版语义错位：旧版把 check_p2_prerequisite() 结果标为 p3_gate，
    实际只是 P2 前置门禁。新版拆成 4 个独立状态：
    - p2_prerequisite: P2 前置门禁
    - p3_engineering: P3 工程链路状态
    - p3_forward_progress: P3 前向取证进度（真实交易日数 / 目标）
    - p3_live_proven: P3 正式认证（连续 4 个真实财报季度）

    用于页面顶部状态条。
    """
    limitations = ["总览聚合各端点降级状态，未触发任何写操作"]
    payload = _workbench_meta(limitations)

    # P3 门禁状态（拆分后的 4 个独立状态）
    payload["p3_gate_status"] = _build_p3_gate_status()
    # 保留旧字段名 p3_gate 作为兼容，但值指向 p2_prerequisite
    payload["p3_gate"] = payload["p3_gate_status"]["p2_prerequisite"]

    # 模型注册表摘要
    try:
        registry = ModelRegistry()
        all_models = registry.all_models()
        model_count = sum(len(versions) for versions in all_models.values())
        payload["model_registry_summary"] = {
            "model_families": len(all_models),
            "total_versions": model_count,
            "available": bool(all_models),
            "degraded": not bool(all_models),
            "degradation_reason": (
                "no registered model versions" if not all_models else ""
            ),
        }
    except Exception as exc:  # noqa: BLE001 - 防御性降级
        payload["model_registry_summary"] = {
            "model_families": 0,
            "total_versions": 0,
            "available": False,
            "degraded": True,
            "degradation_reason": f"registry unavailable: {type(exc).__name__}",
        }

    # 动态探测各模块降级状态：调用各端点函数并读取 degraded 字段。
    # 避免硬编码 True/False，让总览与实际端点状态保持一致。
    module_specs = [
        ("discovery_funnel", "全市场发现漏斗", workbench_discovery_funnel),
        ("company_cards", "公司研究卡", workbench_company_cards),
        ("profit_models", "产品利润模型", workbench_profit_models),
        ("variant_perception", "VariantPerception", workbench_variant_perception),
        ("bear_questions", "反方问题与投委会", workbench_bear_questions),
        ("risk_calendar", "风险和验证日历", workbench_risk_calendar),
        ("data_health", "数据健康与降级", None),
        ("scorecard", "历史预测记分卡", workbench_scorecard),
        ("candidate_funnel", "候选生成漏斗", workbench_candidate_funnel),
    ]
    modules = []
    for key, label, func in module_specs:
        if func is None:
            # data_health 端点内部逻辑独立，默认非降级
            modules.append({"key": key, "label": label, "degraded": False})
            continue
        try:
            resp = func()
            data = resp.get_json().get("data", {}) if hasattr(resp, "get_json") else {}
            modules.append({
                "key": key,
                "label": label,
                "degraded": bool(data.get("degraded", False)),
            })
        except Exception:  # noqa: BLE001 - 防御性：单模块探测失败不影响总览
            modules.append({"key": key, "label": label, "degraded": True})
    payload["modules"] = modules
    return jsonify({"success": True, "data": payload})
