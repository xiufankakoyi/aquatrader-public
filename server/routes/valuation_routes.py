"""P1.8 多方法估值与敏感性 API 端点。

Blueprint url_prefix="/api/research/valuation"

端点：
- POST /cases       计算单个估值方法
- GET  /cases       查询案例列表（支持 company_symbol/method/as_of/limit）
- GET  /cases/<id>  单个案例详情
- POST /report      计算多方法估值报告
- GET  /reports     查询报告列表
- GET  /reports/<id> 报告详情
- POST /reverse_dcf Reverse DCF 单独端点

所有端点不返回买卖建议；notice 字段固定提示"研究数据产物，不包含交易动作建议。"
缺必填字段返回 400；方法不适用返回 200 + computability=not_applicable。
错误响应统一 {ok: false, error: {code, message, trace_id}}
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.earnings.interval import Interval
from core.research.valuation.contracts import (
    Computability,
    ValuationAssumption,
    ValuationCase,
    ValuationInput,
    ValuationMethod,
    ValuationReport,
)
from core.research.valuation.engine import ValuationEngine


valuation_bp = Blueprint("valuation", __name__, url_prefix="/api/research/valuation")
_repository: Any = None
_engine: ValuationEngine | None = None

NOTICE: str = "研究数据产物，不包含交易动作建议。"


def _repo() -> Any:
    """惰性初始化仓储单例。"""
    global _repository
    if _repository is None:
        from core.research.valuation.repository import ValuationRepository
        _repository = ValuationRepository(Path("data/research_control/valuation.db"))
    return _repository


def _engine_singleton() -> ValuationEngine:
    """惰性初始化引擎单例。"""
    global _engine
    if _engine is None:
        _engine = ValuationEngine()
    return _engine


def _trace_id() -> str:
    """生成 trace_id。"""
    return f"tr_{uuid.uuid4().hex[:12]}"


def _ok(data: Any, status: int = 200, **meta: Any):
    """统一成功响应。"""
    payload: dict[str, Any] = {"ok": True, "data": data, "notice": NOTICE, "trace_id": _trace_id()}
    payload.update(meta)
    return jsonify(payload), status


def _error(message: str, code: str = "BAD_REQUEST", status: int = 400, trace_id: str | None = None):
    """统一错误响应。"""
    return jsonify({
        "ok": False,
        "error": {"code": code, "message": message, "trace_id": trace_id or _trace_id()},
        "notice": NOTICE,
    }), status


def _parse_assumptions(raw: list[dict] | None) -> list[ValuationAssumption]:
    """解析假设列表。"""
    if not raw:
        return []
    result: list[ValuationAssumption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            result.append(ValuationAssumption(
                name=str(item.get("name", "")),
                value=float(item.get("value", 0.0)),
                unit=str(item.get("unit", "ratio")),
                source_ref=str(item.get("source_ref", "")),
                source_kind=str(item.get("source_kind", "")),
                as_of=str(item.get("as_of", "")),
                note=str(item.get("note", "")),
            ))
        except (TypeError, ValueError):
            continue
    return result


def _build_input(payload: dict[str, Any]) -> ValuationInput:
    """从请求 payload 构造 ValuationInput。"""
    assumptions = _parse_assumptions(payload.get("assumptions"))
    historical_multiples_raw = payload.get("historical_multiples", {}) or {}
    historical_multiples: dict[str, list[float]] = {}
    for key, vals in historical_multiples_raw.items():
        if not isinstance(vals, list):
            continue
        try:
            historical_multiples[str(key)] = [float(v) for v in vals]
        except (TypeError, ValueError):
            continue
    fcff_series = [float(x) for x in (payload.get("fcff_series") or []) if _is_number(x)]
    fcfe_series = [float(x) for x in (payload.get("fcfe_series") or []) if _is_number(x)]
    dividend_series = [float(x) for x in (payload.get("dividend_series") or []) if _is_number(x)]
    comparable_companies = [
        c for c in (payload.get("comparable_companies") or []) if isinstance(c, dict)
    ]
    return ValuationInput(
        company_symbol=str(payload.get("company_symbol", "")),
        as_of=str(payload.get("as_of", "")),
        scenario=str(payload.get("scenario", "base")),
        current_price=float(payload.get("current_price", 0.0) or 0.0),
        shares_outstanding=float(payload.get("shares_outstanding", 0.0) or 0.0),
        market_cap=float(payload.get("market_cap", 0.0) or 0.0),
        total_debt=float(payload.get("total_debt", 0.0) or 0.0),
        cash_and_equivalents=float(payload.get("cash_and_equivalents", 0.0) or 0.0),
        minority_interest=float(payload.get("minority_interest", 0.0) or 0.0),
        associates_investments=float(payload.get("associates_investments", 0.0) or 0.0),
        diluted_shares=float(payload.get("diluted_shares", 0.0) or 0.0),
        historical_multiples=historical_multiples,
        consensus_eps=float(payload.get("consensus_eps", 0.0) or 0.0),
        consensus_net_profit=float(payload.get("consensus_net_profit", 0.0) or 0.0),
        consensus_revenue=float(payload.get("consensus_revenue", 0.0) or 0.0),
        ebitda_ttm=float(payload.get("ebitda_ttm", 0.0) or 0.0),
        fcf_ttm=float(payload.get("fcf_ttm", 0.0) or 0.0),
        dividends_per_share=float(payload.get("dividends_per_share", 0.0) or 0.0),
        fcff_series=fcff_series,
        fcfe_series=fcfe_series,
        dividend_series=dividend_series,
        assumptions=assumptions,
        comparable_companies=comparable_companies,
        industry=str(payload.get("industry", "")),
        market_data_version=str(payload.get("market_data_version", "")),
        financial_data_version=str(payload.get("financial_data_version", "")),
        model_version=str(payload.get("model_version", "v1")),
    )


def _is_number(value: Any) -> bool:
    """判断 value 是否可转为 float。"""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@valuation_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    return _ok({"status": "ok", **_repo().stats()})


@valuation_bp.route("/cases", methods=["GET", "POST"])
def cases():
    """估值案例集合：POST 计算；GET 查询列表。"""
    if request.method == "GET":
        items = _repo().list_cases(
            company_symbol=request.args.get("company_symbol", ""),
            method=request.args.get("method", ""),
            as_of=request.args.get("as_of", ""),
            limit=request.args.get("limit", 100, type=int),
        )
        return _ok(items, total=len(items))

    payload = request.get_json(silent=True) or {}
    company_symbol = str(payload.get("company_symbol", "")).strip()
    method_value = str(payload.get("method", "")).strip()
    as_of = str(payload.get("as_of", "")).strip()
    if not company_symbol or not method_value or not as_of:
        return _error("company_symbol / method / as_of 均为必填", "INSUFFICIENT_INPUT", 400)
    try:
        ValuationMethod(method_value)
    except ValueError:
        return _error(f"未知估值方法: {method_value}", "UNKNOWN_METHOD", 400)

    inputs = _build_input(payload)
    case = _engine_singleton().compute_single(inputs, method_value)
    if bool(payload.get("save", True)):
        _repo().save_case(case)
    return _ok(case.to_dict())


@valuation_bp.get("/cases/<case_id>")
def case_detail(case_id: str):
    """单个估值案例详情。"""
    item = _repo().get_case(case_id)
    if not item:
        return _error("估值案例不存在", "CASE_NOT_FOUND", 404)
    return _ok(item)


@valuation_bp.route("/report", methods=["POST"])
def report():
    """计算多方法估值报告。"""
    payload = request.get_json(silent=True) or {}
    company_symbol = str(payload.get("company_symbol", "")).strip()
    as_of = str(payload.get("as_of", "")).strip()
    if not company_symbol or not as_of:
        return _error("company_symbol / as_of 均为必填", "INSUFFICIENT_INPUT", 400)

    inputs = _build_input(payload)
    methods = payload.get("methods")
    if methods is not None:
        if not isinstance(methods, list):
            return _error("methods 必须为列表", "INVALID_METHODS", 400)
        methods = [str(m) for m in methods]
    rep = _engine_singleton().compute_report(inputs, methods=methods)
    if bool(payload.get("save", True)):
        _repo().save_report(rep)
    return _ok(rep.to_dict())


@valuation_bp.get("/reports")
def reports():
    """查询估值报告列表。"""
    items = _repo().list_reports(
        company_symbol=request.args.get("company_symbol", ""),
        as_of=request.args.get("as_of", ""),
        limit=request.args.get("limit", 100, type=int),
    )
    return _ok(items, total=len(items))


@valuation_bp.get("/reports/<report_id>")
def report_detail(report_id: str):
    """单个估值报告详情。"""
    item = _repo().get_report(report_id)
    if not item:
        return _error("估值报告不存在", "REPORT_NOT_FOUND", 404)
    return _ok(item)


@valuation_bp.post("/reverse_dcf")
def reverse_dcf():
    """Reverse DCF 单独端点：反推市场隐含增长率。

    返回结果标记为 implied，不得冒充 consensus。
    """
    payload = request.get_json(silent=True) or {}
    company_symbol = str(payload.get("company_symbol", "")).strip()
    as_of = str(payload.get("as_of", "")).strip()
    if not company_symbol or not as_of:
        return _error("company_symbol / as_of 均为必填", "INSUFFICIENT_INPUT", 400)
    if not payload.get("fcff_series"):
        return _error("fcff_series 必填", "INSUFFICIENT_INPUT", 400)
    if not payload.get("assumptions"):
        return _error("assumptions 必填（需含 wacc）", "INSUFFICIENT_INPUT", 400)

    inputs = _build_input(payload)
    case, _gaps = _engine_singleton().reverse_dcf.reverse_engineer(inputs)
    if bool(payload.get("save", True)) and case.is_computable():
        _repo().save_case(case)
    return _ok(case.to_dict())
