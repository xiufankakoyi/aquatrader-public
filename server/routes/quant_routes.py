"""QUANT API 路由。

端点：
  GET  /api/quant/health                       健康检查
  GET  /api/quant/signals                      查询信号列表（支持过滤）
  GET  /api/quant/signals/ready                获取 READY 状态信号
  GET  /api/quant/signals/watch                获取 WATCH 状态信号
  GET  /api/quant/signals/<signal_id>          查询单个信号
  POST /api/quant/signals                      保存 QuantSignal（严格校验）
  GET  /api/quant/sectors/rotation             板块轮动聚合视图
  GET  /api/quant/sectors/<sector_id>          板块详情（该板块下所有信号）
  GET  /api/quant/screenings                   日线筛选结果列表
  GET  /api/quant/screenings/<screening_id>    单个筛选结果
  GET  /api/quant/phases                       板块阶段枚举（文档化）
  GET  /api/quant/statuses                     信号状态枚举（文档化）

P0-5 修复要点：
1. 中文乱码：用 json.dumps(ensure_ascii=False) + Response 替代 jsonify
2. 新增板块轮动、板块详情、日线筛选、筛选结果 4 个端点
3. POST 严格校验：必填字段、枚举值、审计字段过滤
4. schema_version 动态读取 SCHEMA_VERSION 常量
5. JSON 类型一致：依赖 repository._row_to_dict 反序列化

不输出买卖、仓位、止盈止损建议。本阶段仅 WATCH/READY/REJECTED/EXPIRED 四态。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, request

from core.quant.contracts import (
    QuantSignal,
    RejectReason,
    ScreeningResult,
    SectorPhase,
    SignalStatus,
)
from core.quant.repository import DEFAULT_DB_PATH, SCHEMA_VERSION, QuantRepository


quant_bp = Blueprint("quant", __name__, url_prefix="/api/quant")
_repository: Any = None


def _repo() -> QuantRepository:
    """懒加载 QUANT 仓库单例。"""
    global _repository
    if _repository is None:
        _repository = QuantRepository(Path(DEFAULT_DB_PATH))
    return _repository


def _json_response(payload: dict[str, Any], status: int = 200) -> tuple[Response, int]:
    """构造 JSON 响应，确保中文不转义。

    P0-5：Flask jsonify 默认 ensure_ascii=True 会把中文转成 \\uXXXX，
    改用 json.dumps(ensure_ascii=False) + 显式 charset=utf-8。

    Args:
        payload: 响应体字典。
        status: HTTP 状态码。

    Returns:
        (Response, status) 元组。
    """
    body = json.dumps(payload, ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json; charset=utf-8"), status


def _ok(data: Any = None, status: int = 200, **meta: Any):
    """统一成功响应。"""
    payload = {"ok": True, "data": data if data is not None else {}, **meta}
    return _json_response(payload, status)


def _error(message: str, code: str = "BAD_REQUEST", status: int = 400):
    """统一错误响应。"""
    payload = {"ok": False, "error": {"code": code, "message": message}}
    return _json_response(payload, status)


@quant_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    recent = _repo().list_signals(limit=1)
    return _ok({
        "status": "ok",
        "db_path": DEFAULT_DB_PATH,
        "schema_version": SCHEMA_VERSION,
        "has_signals": len(recent) > 0,
    })


@quant_bp.route("/signals", methods=["GET"])
def list_signals():
    """查询 QuantSignal 列表。

    查询参数：
      as_of         按日期过滤（精确匹配）
      status        按状态过滤（watch/ready/rejected/expired）
      sector_id     按板块过滤
      symbol        按股票代码过滤
      candidate_id  按候选 ID 过滤
      limit         返回上限（默认 100，最大 500）
    """
    as_of = request.args.get("as_of", "")
    status = request.args.get("status", "")
    sector_id = request.args.get("sector_id", "")
    symbol = request.args.get("symbol", "")
    candidate_id = request.args.get("candidate_id", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))

    items = _repo().list_signals(
        as_of=as_of, status=status, sector_id=sector_id, symbol=symbol,
        candidate_id=candidate_id, limit=limit,
    )
    return _ok(items, total=len(items))


@quant_bp.get("/signals/ready")
def list_ready_signals():
    """获取 READY 状态信号。"""
    as_of = request.args.get("as_of", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))
    items = _repo().list_ready_signals(as_of=as_of, limit=limit)
    return _ok(items, total=len(items))


@quant_bp.get("/signals/watch")
def list_watch_signals():
    """获取 WATCH 状态信号。"""
    as_of = request.args.get("as_of", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))
    items = _repo().list_watch_signals(as_of=as_of, limit=limit)
    return _ok(items, total=len(items))


@quant_bp.get("/signals/<signal_id>")
def get_signal(signal_id: str):
    """查询单个 QuantSignal。"""
    item = _repo().get_signal(signal_id)
    if not item:
        return _error("信号不存在", "SIGNAL_NOT_FOUND", 404)
    return _ok(item)


@quant_bp.post("/signals")
def save_signal():
    """保存 QuantSignal（严格校验）。

    P0-5：POST 接口仅用于外部打标签场景，严格执行字段校验：
    - 必填字段：signal_id, candidate_id, symbol, sector_id, as_of, status
    - status 必须是 SignalStatus 枚举值
    - sector_phase 必须是 SectorPhase 枚举值
    - 审计字段（created_at/strategy_version）由系统生成，不接受外部覆盖
    - 不允许写入买卖/仓位/止盈止损字段

    请求体：QuantSignal.to_dict() 输出格式的字典。
    """
    payload = request.get_json(silent=True) or {}
    signal, error = _signal_from_payload_strict(payload)
    if error:
        return _error(error, "INVALID_SIGNAL", 400)

    saved = _repo().save_signal(signal)
    if not saved:
        return _error("保存失败", "SAVE_FAILED", 500)
    return _ok(signal.to_dict(), 201)


@quant_bp.get("/sectors/rotation")
def list_sector_rotation():
    """板块轮动聚合视图。

    P0-5：从 signals 表按 sector_id 聚合，返回每个板块的：
    - sector_id, sector_phase
    - 各状态信号数量（watch/ready/rejected/expired）
    - 最新评估时点（as_of）

    查询参数：
      as_of  按日期过滤（精确匹配）
      limit  返回上限（默认 100，最大 500）
    """
    as_of = request.args.get("as_of", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))

    items = _repo().list_signals(as_of=as_of, limit=limit)

    # 按 sector_id 聚合
    sectors: dict[str, dict[str, Any]] = {}
    for item in items:
        sid = item.get("sector_id", "")
        if not sid:
            continue
        if sid not in sectors:
            sectors[sid] = {
                "sector_id": sid,
                "sector_phase": item.get("sector_phase", SectorPhase.UNKNOWN.value),
                "counts": {"watch": 0, "ready": 0, "rejected": 0, "expired": 0},
                "latest_as_of": item.get("as_of", ""),
                "symbols": set(),
            }
        s = sectors[sid]
        status = item.get("status", "")
        if status in s["counts"]:
            s["counts"][status] += 1
        if item.get("as_of", "") > s["latest_as_of"]:
            s["latest_as_of"] = item.get("as_of", "")
            s["sector_phase"] = item.get("sector_phase", s["sector_phase"])
        s["symbols"].add(item.get("symbol", ""))

    # 转为列表，set 转 list
    result = []
    for sid, s in sectors.items():
        s["symbol_count"] = len(s["symbols"])
        s["symbols"] = sorted(s["symbols"])
        result.append(s)
    return _ok(result, total=len(result))


@quant_bp.get("/sectors/<sector_id>")
def get_sector_detail(sector_id: str):
    """板块详情：返回该板块下的所有信号。

    Args:
        sector_id: 板块 ID（URL 路径参数）。

    查询参数：
      as_of  按日期过滤
      status 按状态过滤
      limit  返回上限（默认 100，最大 500）
    """
    as_of = request.args.get("as_of", "")
    status = request.args.get("status", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))

    items = _repo().list_signals(
        as_of=as_of, status=status, sector_id=sector_id, limit=limit
    )
    if not items:
        return _error("板块不存在或无信号", "SECTOR_NOT_FOUND", 404)

    # 聚合板块概览
    overview = {
        "sector_id": sector_id,
        "sector_phase": items[0].get("sector_phase", SectorPhase.UNKNOWN.value),
        "total_signals": len(items),
        "counts": {"watch": 0, "ready": 0, "rejected": 0, "expired": 0},
    }
    for item in items:
        st = item.get("status", "")
        if st in overview["counts"]:
            overview["counts"][st] += 1

    return _ok({"overview": overview, "signals": items})


@quant_bp.get("/screenings")
def list_screenings():
    """日线筛选结果列表。

    P0-5：从 signals 表提取所有 screening_result 非空的记录，
    返回筛选结果摘要列表。

    查询参数：
      as_of         按日期过滤
      symbol        按股票代码过滤
      candidate_id  按候选 ID 过滤
      limit         返回上限（默认 100，最大 500）
    """
    as_of = request.args.get("as_of", "")
    symbol = request.args.get("symbol", "")
    candidate_id = request.args.get("candidate_id", "")
    limit = request.args.get("limit", 100, type=int)
    limit = max(1, min(limit, 500))

    items = _repo().list_signals(
        as_of=as_of, symbol=symbol, candidate_id=candidate_id, limit=limit
    )
    # 只返回有筛选结果的记录，提取筛选摘要
    screenings = []
    for item in items:
        sr = item.get("screening_result")
        if not sr or not isinstance(sr, dict):
            continue
        screenings.append({
            "screening_id": sr.get("screening_id", ""),
            "candidate_id": sr.get("candidate_id", item.get("candidate_id", "")),
            "symbol": sr.get("symbol", item.get("symbol", "")),
            "as_of": sr.get("as_of", item.get("as_of", "")),
            "sector_phase": sr.get("sector_phase", ""),
            "all_pass": sr.get("all_pass", False),
            "ready_eligible": sr.get("ready_eligible", False),
            "degraded": sr.get("degraded", False),
            "reject_reasons": sr.get("reject_reasons", []),
            "signal_id": item.get("signal_id", ""),
            "signal_status": item.get("status", ""),
        })
    return _ok(screenings, total=len(screenings))


@quant_bp.get("/screenings/<screening_id>")
def get_screening(screening_id: str):
    """查询单个日线筛选结果。

    P0-5：按 screening_id 在 signals 表的 screening_result JSON 内查找。
    返回完整的 ScreeningResult 字典 + 关联的 signal_id。

    Args:
        screening_id: 筛选结果 ID（URL 路径参数）。
    """
    # screening_id 嵌在 screening_result JSON 内，需要全表扫描
    # 用较大的 limit 确保能找到；若数据量大后续可建索引表
    items = _repo().list_signals(limit=500)
    for item in items:
        sr = item.get("screening_result")
        if not sr or not isinstance(sr, dict):
            continue
        if sr.get("screening_id") == screening_id:
            return _ok({
                "screening_result": sr,
                "signal_id": item.get("signal_id", ""),
                "signal_status": item.get("status", ""),
            })
    return _error("筛选结果不存在", "SCREENING_NOT_FOUND", 404)


@quant_bp.get("/phases")
def list_phases():
    """板块阶段枚举（文档化目的）。"""
    phases = [
        {"value": p.value, "name": p.name, "description": _phase_description(p)}
        for p in SectorPhase
    ]
    return _ok(phases)


@quant_bp.get("/statuses")
def list_statuses():
    """信号状态枚举（文档化目的）。"""
    statuses = [
        {"value": s.value, "name": s.name, "description": _status_description(s)}
        for s in SignalStatus
    ]
    return _ok(statuses)


@quant_bp.get("/reject_reasons")
def list_reject_reasons():
    """拒绝原因枚举（文档化目的，P0-5 新增）。"""
    reasons = [
        {"value": r.value, "name": r.name, "description": _reject_reason_description(r)}
        for r in RejectReason
    ]
    return _ok(reasons)


# ==================== 工具函数 ====================

# POST 接口必填字段
_REQUIRED_SIGNAL_FIELDS = (
    "signal_id", "candidate_id", "symbol", "sector_id", "as_of", "status",
)

# POST 接口不允许外部覆盖的审计字段（由系统生成）
_PROTECTED_FIELDS = (
    "created_at", "updated_at", "strategy_version",
)


def _signal_from_payload_strict(
    payload: dict[str, Any],
) -> tuple[QuantSignal | None, str]:
    """从请求载荷严格构造 QuantSignal。

    P0-5：严格校验：
    - 必填字段全部存在且非空
    - status 必须是 SignalStatus 枚举值
    - sector_phase 必须是 SectorPhase 枚举值（缺失默认 UNKNOWN）
    - 拒绝覆盖审计字段
    - 不接受买卖/仓位字段

    Args:
        payload: 请求体字典。

    Returns:
        (QuantSignal 实例, 错误信息)；成功时错误信息为空字符串。
    """
    if not isinstance(payload, dict):
        return None, "请求体必须是 JSON 对象"

    # 检查必填字段
    missing = [f for f in _REQUIRED_SIGNAL_FIELDS if not str(payload.get(f) or "").strip()]
    if missing:
        return None, f"必填字段缺失: {missing}"

    # 检查审计字段不被覆盖
    protected_present = [f for f in _PROTECTED_FIELDS if f in payload and payload[f]]
    if protected_present:
        return None, f"审计字段由系统生成，不接受外部覆盖: {protected_present}"

    # 检查买卖/仓位字段（防御性：不接受投资建议字段）
    forbidden = ["buy", "sell", "position", "stop_loss", "take_profit", "target_price"]
    forbidden_present = [f for f in forbidden if f in payload]
    if forbidden_present:
        return None, f"不接受投资建议字段: {forbidden_present}"

    # status 枚举校验
    status_raw = str(payload.get("status") or "")
    try:
        status = SignalStatus(status_raw)
    except ValueError:
        return None, f"无效的 status: {status_raw}，可选值: {[s.value for s in SignalStatus]}"

    # sector_phase 枚举校验（缺失默认 UNKNOWN）
    phase_raw = str(payload.get("sector_phase") or SectorPhase.UNKNOWN.value)
    try:
        sector_phase = SectorPhase(phase_raw)
    except ValueError:
        return None, f"无效的 sector_phase: {phase_raw}，可选值: {[p.value for p in SectorPhase]}"

    # reject_reasons 枚举校验
    reject_reasons: list[RejectReason] = []
    for r in payload.get("reject_reasons") or []:
        try:
            reject_reasons.append(RejectReason(str(r)))
        except ValueError:
            return None, f"无效的 reject_reason: {r}"

    # screening_result 可选
    screening_result = None
    sr_raw = payload.get("screening_result")
    if sr_raw is not None:
        if not isinstance(sr_raw, dict):
            return None, "screening_result 必须是对象"
        screening_result = _screening_result_from_dict(sr_raw, sector_phase)

    # decision_trace 校验
    decision_trace = payload.get("decision_trace") or []
    if not isinstance(decision_trace, list):
        return None, "decision_trace 必须是数组"
    decision_trace = [str(t) for t in decision_trace]

    # 追溯字段（可选，但若提供则校验类型）
    mapping_confidence = 0.0
    if "mapping_confidence" in payload:
        try:
            mapping_confidence = float(payload["mapping_confidence"])
            if not 0.0 <= mapping_confidence <= 1.0:
                return None, "mapping_confidence 必须在 [0, 1] 范围内"
        except (TypeError, ValueError):
            return None, "mapping_confidence 必须是数字"

    try:
        signal = QuantSignal(
            signal_id=str(payload["signal_id"]),
            candidate_id=str(payload["candidate_id"]),
            symbol=str(payload["symbol"]),
            sector_id=str(payload["sector_id"]),
            as_of=str(payload["as_of"]),
            status=status,
            sector_phase=sector_phase,
            research_score=float(payload.get("research_score") or 0.0),
            screening_result=screening_result,
            reject_reasons=reject_reasons,
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            degraded=bool(payload.get("degraded") or False),
            decision_trace=decision_trace,
            # 追溯字段
            thesis_id=str(payload.get("thesis_id") or ""),
            claim_id=str(payload.get("claim_id") or ""),
            company_id=str(payload.get("company_id") or ""),
            mapping_confidence=mapping_confidence,
            evidence_as_of=str(payload.get("evidence_as_of") or ""),
            strategy_version=str(payload.get("strategy_version") or ""),
        )
        return signal, ""
    except (TypeError, ValueError) as exc:
        return None, f"字段类型错误: {exc}"


def _screening_result_from_dict(
    raw: dict[str, Any], default_phase: SectorPhase
) -> ScreeningResult:
    """从字典构造 ScreeningResult。"""
    phase_raw = str(raw.get("sector_phase") or default_phase.value)
    try:
        phase = SectorPhase(phase_raw)
    except ValueError:
        phase = default_phase

    reject_reasons: list[RejectReason] = []
    for r in raw.get("reject_reasons") or []:
        try:
            reject_reasons.append(RejectReason(str(r)))
        except ValueError:
            continue

    details = raw.get("details") or {}
    if not isinstance(details, dict):
        details = {}

    return ScreeningResult(
        screening_id=str(raw.get("screening_id") or ""),
        candidate_id=str(raw.get("candidate_id") or ""),
        symbol=str(raw.get("symbol") or ""),
        as_of=str(raw.get("as_of") or ""),
        sector_phase=phase,
        trend_pass=bool(raw.get("trend_pass") or False),
        position_pass=bool(raw.get("position_pass") or False),
        acceleration_pass=bool(raw.get("acceleration_pass") or False),
        liquidity_pass=bool(raw.get("liquidity_pass") or False),
        tradability_pass=bool(raw.get("tradability_pass") or False),
        pit_pass=bool(raw.get("pit_pass") or False),
        reject_reasons=reject_reasons,
        trend_score=float(raw.get("trend_score") or 0.0),
        position_score=float(raw.get("position_score") or 0.0),
        liquidity_score=float(raw.get("liquidity_score") or 0.0),
        details=details,
        degraded=bool(raw.get("degraded") or False),
    )


def _phase_description(phase: SectorPhase) -> str:
    """板块阶段中文描述。"""
    descriptions = {
        SectorPhase.UNKNOWN: "未知：板块数据缺失，禁止 READY",
        SectorPhase.ICE: "冰点：板块持续低迷，成交萎缩",
        SectorPhase.RECOVERY: "复苏：底部放量，领涨品种出现",
        SectorPhase.STARTUP: "启动：板块共振，多品种齐涨",
        SectorPhase.ACCELERATION: "加速：涨幅扩大，成交激增",
        SectorPhase.CLIMAX: "高潮：顶部放量滞涨，分歧加大",
        SectorPhase.DIVERGENCE: "分歧：品种分化，领涨滞涨",
        SectorPhase.RETREAT: "撤退：板块回调，资金流出",
    }
    return descriptions.get(phase, "")


def _status_description(status: SignalStatus) -> str:
    """信号状态中文描述。"""
    descriptions = {
        SignalStatus.WATCH: "观察池：通过板块筛选，待日线确认",
        SignalStatus.READY: "就绪池：通过全部筛选，可进入下一阶段",
        SignalStatus.REJECTED: "拒绝：未通过某项筛选",
        SignalStatus.EXPIRED: "过期：超过 as_of 有效期或 PIT 违规",
    }
    return descriptions.get(status, "")


def _reject_reason_description(reason: RejectReason) -> str:
    """拒绝原因中文描述。"""
    descriptions = {
        RejectReason.TREND_FAIL: "趋势不满足",
        RejectReason.POSITION_FAIL: "位置不满足",
        RejectReason.ACCELERATION_FAIL: "加速赶顶（负面信号）",
        RejectReason.LIQUIDITY_FAIL: "流动性不足",
        RejectReason.TRADABILITY_FAIL: "不可交易（ST/停牌/涨跌停）",
        RejectReason.PIT_VIOLATION: "PIT 违规",
        RejectReason.SECTOR_REJECT: "板块阶段不支持",
        RejectReason.SECTOR_UNKNOWN: "板块数据缺失",
        RejectReason.EXPIRED: "超过有效期",
        RejectReason.MISSING_DATA: "数据缺失安全降级",
        RejectReason.RESEARCH_GATE_FAIL: "Research 准入门禁未通过",
        RejectReason.DEGRADED: "关键字段降级，禁止 READY",
    }
    return descriptions.get(reason, "")
