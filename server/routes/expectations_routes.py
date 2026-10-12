"""Stage 3 Research API: expectations, interval gaps, implied reverse-engineering,
and thesis lifecycle with full state machine.

扩展版本：
- 新增 /market_expectations 端点
- 新增 /implied_reverse 端点（反推隐含预期）
- 新增 /theses/<id>/add-contradiction 端点
- 新增 /theses/<id>/check-expiry 端点
- 新增 /theses/<id>/promote 端点
- P1.7 新增 /aquatrade_forecasts 端点（第四类独立预期）
- P1.7 新增 /consensus_summary 端点（聚合统计 + PIT 重建）
- 所有端点不返回买卖、仓位、止盈止损建议。
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.earnings.interval import Interval
from core.research.expectations.contracts import (
    AquatradeForecast,
    Catalyst,
    CompanyGuidance,
    ConsensusEstimate,
    ConsensusSummary,
    ExpectationGap,
    ExpectationValue,
    ImpliedExpectation,
    ImpliedExpectationInput,
    InvestmentThesis,
    MarketExpectation,
    ValuationSnapshot,
)
from core.research.expectations.engine import (
    AquatradeForecastBuilder,
    ConsensusSummaryEngine,
    ExpectationGapEngine,
    ImpliedExpectationEngine,
    ThesisLifecycle,
)


expectations_bp = Blueprint("expectations", __name__, url_prefix="/api/research/expectations")
_repository: Any = None


def _repo() -> Any:
    global _repository
    if _repository is None:
        from core.research.expectations.repository import ExpectationRepository
        _repository = ExpectationRepository(Path("data/research_control/expectations.db"))
    return _repository


def _ok(data: Any, status: int = 200, **meta: Any):
    return jsonify({"ok": True, "data": data, **meta}), status


def _error(message: str, code: str = "BAD_REQUEST", status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def _values(raw: list[dict[str, Any]]) -> list[ExpectationValue]:
    return [ExpectationValue.from_dict(item) for item in (raw or [])]


@expectations_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    return _ok({"status": "ok", **_repo().stats()})


def _domain_endpoint(table: str, cls: type, id_field: str):
    """通用领域端点处理。"""
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = _repo().list(table, index_value=company, limit=request.args.get("limit", 100, type=int))
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    payload["values"] = _values(payload.get("values", []))
    if cls is ConsensusEstimate:
        payload["dispersion"] = Interval.from_dict(payload.get("dispersion", {}))
    item = cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})
    _repo().save(table, item)
    return _ok(asdict(item), 201)


@expectations_bp.route("/guidance", methods=["GET", "POST"])
def guidance():
    """公司正式指引管理。"""
    return _domain_endpoint("company_guidance", CompanyGuidance, "guidance_id")


@expectations_bp.route("/consensus", methods=["GET", "POST"])
def consensus():
    """公开一致预期管理。"""
    return _domain_endpoint("consensus_estimates", ConsensusEstimate, "consensus_id")


@expectations_bp.route("/implied", methods=["GET", "POST"])
def implied():
    """价格隐含预期管理。"""
    return _domain_endpoint("implied_expectations", ImpliedExpectation, "implied_id")


@expectations_bp.route("/market_expectations", methods=["GET", "POST"])
def market_expectations():
    """统一市场预期管理。

    不同来源禁止混并，每个实例对应单一来源。
    """
    if request.method == "GET":
        symbol = request.args.get("symbol", "")
        items = _repo().list("market_expectations", index_value=symbol,
                             limit=request.args.get("limit", 100, type=int))
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    item = MarketExpectation.from_dict(payload)
    _repo().save("market_expectations", item)
    return _ok(item.to_dict(), 201)


@expectations_bp.post("/implied_reverse")
def implied_reverse():
    """从估值数据反推市场隐含预期。

    反推结果必须标记为 implied，不得标记为 consensus。
    """
    payload = request.get_json(silent=True) or {}
    inp = ImpliedExpectationInput(
        symbol=str(payload.get("symbol", "")),
        current_price=float(payload.get("current_price", 0)),
        shares_outstanding=float(payload.get("shares_outstanding", 0)),
        trailing_EPS=float(payload.get("trailing_EPS", 0)),
        pe_ratio=float(payload.get("pe_ratio", 0)),
        pb_ratio=float(payload.get("pb_ratio", 0)),
        growth_rate_assumed=float(payload.get("growth_rate_assumed", 0)),
        margin_assumed=float(payload.get("margin_assumed", 0)),
        market_data_version=str(payload.get("market_data_version", "")),
        as_of=str(payload.get("as_of", "")),
    )
    forecast_period = str(payload.get("forecast_period", ""))
    result = ImpliedExpectationEngine().reverse_engineer(inp, forecast_period=forecast_period)
    if result is None:
        return _error("输入数据不完整，无法反推隐含预期", "INSUFFICIENT_INPUT", 400)
    if bool(payload.get("save", True)):
        _repo().save("market_expectations", result)
    return _ok(result.to_dict())


@expectations_bp.route("/valuations", methods=["GET", "POST"])
def valuations():
    """估值快照管理。"""
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = _repo().list("valuation_snapshots", index_value=company)
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    item = ValuationSnapshot(**{k: v for k, v in payload.items() if k in ValuationSnapshot.__dataclass_fields__})
    _repo().save("valuation_snapshots", item)
    return _ok(asdict(item), 201)


@expectations_bp.route("/gaps", methods=["GET", "POST"])
def gaps():
    """预期差计算与查询。"""
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = _repo().list("expectation_gaps", index_value=company)
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    result = ExpectationGapEngine().compute(
        company_symbol=str(payload.get("company_symbol") or ""),
        metric=str(payload.get("metric") or ""), period=str(payload.get("period") or ""),
        research_value=Interval.from_dict(payload.get("research_value", {})),
        earnings_result_id=str(payload.get("earnings_result_id") or ""),
        earnings_data_version=str(payload.get("earnings_data_version") or ""),
        expectation_id=str(payload.get("expectation_id") or ""),
        expectation_kind=str(payload.get("expectation_kind") or ""),
        expectation_value=(ExpectationValue.from_dict(payload["expectation_value"])
                           if payload.get("expectation_value") else None),
        market_data_version=str(payload.get("market_data_version") or ""),
        as_of=str(payload.get("as_of") or ""), historical=bool(payload.get("historical", True)),
        expectation_source=str(payload.get("expectation_source", "")),
        catalyst_window=str(payload.get("catalyst_window", "")),
        valuation_level=str(payload.get("valuation_level", "")),
        crowding_available=bool(payload.get("crowding_available", False)),
        revision_speed=str(payload.get("revision_speed", "")),
        data_expires_at=str(payload.get("data_expires_at", "")),
    )
    if bool(payload.get("save", True)):
        _repo().save("expectation_gaps", result)
    return _ok(asdict(result))


@expectations_bp.route("/theses", methods=["GET", "POST"])
def theses():
    """投资观点管理。"""
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = _repo().list("investment_theses", index_value=company)
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    item = InvestmentThesis.from_dict(payload)
    _repo().save_thesis_bundle(item, [])
    return _ok(item.to_dict(), 201)


@expectations_bp.get("/theses/<thesis_id>")
def thesis_detail(thesis_id: str):
    """查询单个观点。"""
    item = _repo().get("investment_theses", thesis_id)
    return _ok(item) if item else _error("研究观点不存在", "THESIS_NOT_FOUND", 404)


@expectations_bp.post("/theses/<thesis_id>/evaluate-claims")
def evaluate_thesis(thesis_id: str):
    """Claim 复核 → 降级 + 审计。"""
    raw = _repo().get("investment_theses", thesis_id)
    if not raw:
        return _error("研究观点不存在", "THESIS_NOT_FOUND", 404)
    thesis = InvestmentThesis.from_dict(raw)
    payload = request.get_json(silent=True) or {}
    thesis, reviews = ThesisLifecycle().evaluate_claims(
        thesis, payload.get("claim_states", {}), operator="api",
    )
    _repo().save_thesis_bundle(thesis, reviews)
    return _ok({"thesis": thesis.to_dict(), "reviews": [asdict(item) for item in reviews]})


@expectations_bp.post("/theses/<thesis_id>/add-contradiction")
def add_contradiction(thesis_id: str):
    """新反证进入后降级。"""
    raw = _repo().get("investment_theses", thesis_id)
    if not raw:
        return _error("研究观点不存在", "THESIS_NOT_FOUND", 404)
    thesis = InvestmentThesis.from_dict(raw)
    payload = request.get_json(silent=True) or {}
    evidence_id = str(payload.get("evidence_id", ""))
    reason = str(payload.get("reason", ""))
    thesis, review = ThesisLifecycle().add_contradictory_evidence(
        thesis, evidence_id, reason=reason, operator="api",
    )
    reviews = [review] if review else []
    _repo().save_thesis_bundle(thesis, reviews)
    return _ok({"thesis": thesis.to_dict(), "reviews": [asdict(r) for r in reviews]})


@expectations_bp.post("/theses/<thesis_id>/check-expiry")
def check_expiry(thesis_id: str):
    """检查观点有效期和催化剂窗口。"""
    raw = _repo().get("investment_theses", thesis_id)
    if not raw:
        return _error("研究观点不存在", "THESIS_NOT_FOUND", 404)
    thesis = InvestmentThesis.from_dict(raw)
    payload = request.get_json(silent=True) or {}
    as_of = str(payload.get("as_of", ""))
    thesis, review1 = ThesisLifecycle().check_catalyst_expiry(thesis, as_of, operator="api")
    thesis, review2 = ThesisLifecycle().check_expiry(thesis, as_of, operator="api")
    reviews = [r for r in (review1, review2) if r]
    if reviews:
        _repo().save_thesis_bundle(thesis, reviews)
    return _ok({"thesis": thesis.to_dict(), "reviews": [asdict(r) for r in reviews]})


@expectations_bp.post("/theses/<thesis_id>/promote")
def promote_thesis(thesis_id: str):
    """观点状态升级。"""
    raw = _repo().get("investment_theses", thesis_id)
    if not raw:
        return _error("研究观点不存在", "THESIS_NOT_FOUND", 404)
    thesis = InvestmentThesis.from_dict(raw)
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", ""))
    lc = ThesisLifecycle()
    if action == "to_active":
        thesis, review = lc.promote_to_active(
            thesis, has_earnings_impact=bool(payload.get("has_earnings_impact", True)),
            operator="api",
        )
    elif action == "to_high_conviction":
        thesis, review = lc.promote_to_high_conviction(
            thesis,
            gap_significant=bool(payload.get("gap_significant", True)),
            catalyst_in_window=bool(payload.get("catalyst_in_window", True)),
            evidence_sufficient=bool(payload.get("evidence_sufficient", True)),
            operator="api",
        )
    elif action == "archive":
        thesis, review = lc.archive(thesis, operator="api")
    else:
        return _error(f"未知操作: {action}", "UNKNOWN_ACTION", 400)
    reviews = [review] if review else []
    if thesis.status != raw.get("status"):
        _repo().save_thesis_bundle(thesis, reviews)
    return _ok({"thesis": thesis.to_dict(), "reviews": [asdict(r) for r in reviews]})


# ---------------------------------------------------------------------------
# P1.7 新增端点：AquaTrade 预测与一致预期聚合统计
# ---------------------------------------------------------------------------

@expectations_bp.route("/aquatrade_forecasts", methods=["GET", "POST"])
def aquatrade_forecasts():
    """AquaTrade 独立研究预测管理（第四类独立预期）。

    与公司指引、第三方一致预期、市场隐含预期并列存储，互不覆盖。
    POST 必须显式提供 model_version、as_of、metric、period 等必填字段；
    缺字段返回 400，禁止默认兜底。
    """
    if request.method == "GET":
        items = _repo().list_aquatrade_forecasts(
            company_symbol=request.args.get("company_symbol", ""),
            period=request.args.get("period", ""),
            scenario=request.args.get("scenario", ""),
            as_of=request.args.get("as_of", ""),
            limit=request.args.get("limit", 100, type=int),
        )
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    try:
        item = AquatradeForecastBuilder().build(
            company_symbol=str(payload.get("company_symbol", "")),
            metric=str(payload.get("metric", "")),
            period=str(payload.get("period", "")),
            as_of=str(payload.get("as_of", "")),
            value_low=float(payload.get("value_low", 0)),
            value_high=float(payload.get("value_high", 0)),
            unit=str(payload.get("unit", "")),
            currency=str(payload.get("currency", "CNY")),
            model_version=str(payload.get("model_version", "")),
            integrated_forecast_id=str(payload.get("integrated_forecast_id", "")),
            scenario=str(payload.get("scenario", "base")),
            assumptions_snapshot=payload.get("assumptions_snapshot"),
            formula_trace=payload.get("formula_trace"),
            source_evidence_id=str(payload.get("source_evidence_id", "")),
            revision_of=str(payload.get("revision_of", "")),
            revision_reason=str(payload.get("revision_reason", "")),
        )
    except ValueError as exc:
        return _error(str(exc), "INSUFFICIENT_INPUT", 400)
    _repo().save_aquatrade_forecast(item)
    return _ok(item.to_dict(), 201)


@expectations_bp.get("/aquatrade_forecasts/latest")
def aquatrade_forecast_latest():
    """获取 PIT 可见的最新 AquaTrade 预测。"""
    item = _repo().get_latest_aquatrade_forecast(
        company_symbol=request.args.get("company_symbol", ""),
        period=request.args.get("period", ""),
        scenario=request.args.get("scenario", ""),
        as_of=request.args.get("as_of", ""),
    )
    return _ok(item) if item else _error("无匹配的 AquaTrade 预测", "NOT_FOUND", 404)


@expectations_bp.route("/consensus_summary", methods=["GET", "POST"])
def consensus_summary():
    """一致预期聚合统计：中位数、分位数、来源数、分歧度、修正方向。

    POST 接受 company_symbol/metric/period/as_of，从持久化的 ConsensusEstimate
    列表通过 PIT 重建后聚合；来源不足时 status="insufficient_sources"。
    GET 直接查询已缓存的 ConsensusSummary 列表。
    """
    if request.method == "GET":
        items = _repo().list_consensus_summaries(
            company_symbol=request.args.get("company_symbol", ""),
            metric=request.args.get("metric", ""),
            period=request.args.get("period", ""),
            as_of=request.args.get("as_of", ""),
            status=request.args.get("status", ""),
            limit=request.args.get("limit", 100, type=int),
        )
        return _ok(items, total=len(items))
    payload = request.get_json(silent=True) or {}
    company_symbol = str(payload.get("company_symbol", ""))
    metric = str(payload.get("metric", ""))
    period = str(payload.get("period", ""))
    as_of = str(payload.get("as_of", ""))
    if not (company_symbol and metric and period and as_of):
        return _error("company_symbol/metric/period/as_of 均为必填", "INSUFFICIENT_INPUT", 400)
    historical = bool(payload.get("historical", True))
    min_sources = payload.get("min_sources_threshold")
    min_sources_int = int(min_sources) if isinstance(min_sources, (int, str)) and str(min_sources).isdigit() else None

    # PIT 重建当时可见的所有机构 ConsensusEstimate
    visible_payloads = _repo().list_consensus_visible_as_of(
        company_symbol, metric, period, as_of, historical=historical,
    )
    visible_estimates = [ConsensusEstimate.from_dict(p) for p in visible_payloads]

    # 查询上一期 ConsensusSummary（用于修正方向）
    previous = None
    prev_payloads = _repo().list_consensus_summaries(
        company_symbol=company_symbol, metric=metric, period=period,
        as_of=as_of, status="ok", limit=1,
    )
    if prev_payloads:
        # 取最近一次 status="ok" 的 summary，跳过当前 as_of 自身
        for prev in prev_payloads:
            if prev.get("as_of") != as_of and prev.get("status") == "ok":
                previous = ConsensusSummary.from_dict(prev)
                break

    summary = ConsensusSummaryEngine().compute_summary(
        company_symbol=company_symbol,
        metric=metric,
        period=period,
        as_of=as_of,
        estimates=visible_estimates,
        previous_summary=previous,
        min_sources_threshold=min_sources_int,
        historical=historical,
    )
    if bool(payload.get("save", True)):
        _repo().save_consensus_summary(summary)
    return _ok(summary.to_dict())
