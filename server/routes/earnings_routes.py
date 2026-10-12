"""盈利映射层 API。

端点：
  GET/POST /api/research/earnings/drivers       盈利驱动管理
  GET/POST /api/research/earnings/models        模型容器管理（demand/capacity/pricing/margin）
  GET      /api/research/earnings/models/by_driver
  GET      /api/research/earnings/models/<model_id>
  GET/POST /api/research/earnings/assumptions   计算假设管理（替代原 /inputs）
  GET/POST /api/research/earnings/inputs        /assumptions 兼容别名
  GET/POST /api/research/earnings/exposures     收入敞口管理
  GET/POST /api/research/earnings/capitals      股本版本管理
  GET      /api/research/earnings/scenarios     情景查询
  GET/POST /api/research/earnings/results       测算结果管理（兼容旧名）
  GET/POST /api/research/earnings/impacts       测算结果管理（新名）
  GET/POST /api/research/earnings/forecasts     三表联动预测管理（P1.4）
  GET      /api/research/earnings/forecasts/latest
  GET      /api/research/earnings/forecasts/<forecast_id>
  GET      /api/research/earnings/forecasts/versions
  GET      /api/research/earnings/gaps          数据缺口
  POST     /api/research/earnings/compute       执行盈利测算
  POST     /api/research/earnings/recompute     基于变更假设增量重算
  POST     /api/research/earnings/forecasts/build 构建三表联动预测
  GET      /api/research/earnings/health        健康检查

所有响应字段缺失时安全降级，不输出买卖/仓位/止盈止损建议。
"""
from __future__ import annotations

import os
from typing import Any

from flask import Blueprint, jsonify, request

from core.research.earnings.contracts import (
    CapacityModel,
    DemandModel,
    EarningsDriver,
    EarningsImpact,
    EarningsScenarioKind,
    EarningsStatus,
    ForecastHorizon,
    IntegratedForecast,
    ImpactAssumption,
    InputGap,
    MarginModel,
    PricingModel,
    RevenueExposure,
    RevenueExposureSource,
    ShareCapitalVersion,
)
from core.research.earnings.engine import (
    EarningsImpactEngine,
    EarningsImpactRequest,
    ForecastBuildRequest,
    IntegratedForecastEngine,
)
from core.research.earnings.repository import EarningsRepository

earnings_bp = Blueprint("earnings", __name__, url_prefix="/api/research/earnings")
_repository: EarningsRepository | None = None
_engine: EarningsImpactEngine | None = None


def _get_repository() -> EarningsRepository:
    global _repository
    if _repository is None:
        _repository = EarningsRepository()
    return _repository


def _get_engine() -> EarningsImpactEngine:
    global _engine
    if _engine is None:
        _engine = EarningsImpactEngine()
    return _engine


def _ok(data: Any = None, **meta: Any):
    return jsonify({"ok": True, "data": data if data is not None else {}, "meta": meta})


def _error(message: str, status: int = 400, code: str = "BAD_REQUEST"):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


@earnings_bp.get("/health")
def health():
    """健康检查 + 存储统计。"""
    repo = _get_repository()
    return _ok({"status": "ok", "module": "earnings_impact", **repo.stats()})


@earnings_bp.route("/drivers", methods=["GET", "POST"])
def drivers():
    """盈利驱动管理。

    GET: 查询驱动列表，支持 company_symbol 过滤。
    POST: 创建或更新驱动。
    """
    repo = _get_repository()
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = repo.list_drivers(company, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    driver = EarningsDriver.from_dict(payload.get("driver", {}))
    repo.save_driver(driver)
    return _ok({"driver": driver.to_dict()}), 201


@earnings_bp.route("/models", methods=["GET", "POST"])
def models():
    """驱动与模型容器管理。

    GET: 查询驱动列表，支持 company_symbol 过滤（兼容旧行为）。
    POST: 创建驱动，可选附带 demand/capacity/pricing/margin 模型（模型容器版本化持久化）。
    """
    repo = _get_repository()
    if request.method == "GET":
        company = request.args.get("company_symbol", "")
        items = repo.list_drivers(company, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    driver = EarningsDriver.from_dict(payload.get("driver", {}))
    repo.save_driver(driver)

    models_saved: list[str] = []
    if "demand" in payload:
        demand = DemandModel.from_dict(payload["demand"])
        demand.driver_id = driver.driver_id
        repo.save_assumption(demand.market_size)
        repo.save_assumption(demand.per_unit_usage)
        repo.save_assumption(demand.market_growth_delta_input)
        repo.save_demand_model(demand)
        models_saved.append("demand")
    if "capacity" in payload:
        capacity = CapacityModel.from_dict(payload["capacity"])
        capacity.driver_id = driver.driver_id
        repo.save_assumption(capacity.nameplate_capacity)
        repo.save_assumption(capacity.effective_capacity)
        repo.save_assumption(capacity.utilization_rate)
        repo.save_assumption(capacity.yield_rate)
        repo.save_capacity_model(capacity)
        models_saved.append("capacity")
    if "pricing" in payload:
        pricing = PricingModel.from_dict(payload["pricing"])
        pricing.driver_id = driver.driver_id
        repo.save_assumption(pricing.asp)
        repo.save_assumption(pricing.unit_cost)
        repo.save_pricing_model(pricing)
        models_saved.append("pricing")
    if "margin" in payload:
        margin = MarginModel.from_dict(payload["margin"])
        margin.driver_id = driver.driver_id
        repo.save_assumption(margin.gross_margin)
        repo.save_assumption(margin.expense_ratio)
        repo.save_assumption(margin.tax_rate)
        repo.save_margin_model(margin)
        models_saved.append("margin")
    if "exposure" in payload:
        exposure = RevenueExposure.from_dict(payload["exposure"])
        exposure.driver_id = driver.driver_id
        repo.save_exposure(exposure)
        models_saved.append("exposure")
    if "capital" in payload:
        capital = ShareCapitalVersion.from_dict(payload["capital"])
        repo.save_capital(capital)
        models_saved.append("capital")

    return _ok({"driver": driver.to_dict(), "models_saved": models_saved}), 201


@earnings_bp.get("/models/by_driver")
def models_by_driver():
    """查询指定 driver 下的所有版本化模型容器。

    Query params:
        driver_id: 驱动 ID（必填）。
        model_type: 可选过滤（demand/capacity/pricing/margin）。
    """
    driver_id = request.args.get("driver_id", "")
    if not driver_id:
        return _error("缺少 driver_id 参数", 400, "MISSING_DRIVER_ID")
    model_type = request.args.get("model_type", "")
    repo = _get_repository()
    items = repo.list_models(driver_id, model_type, request.args.get("limit", 100, type=int))
    return _ok([item.to_dict() for item in items], total=len(items))


@earnings_bp.get("/models/<model_id>")
def model_detail(model_id: str):
    """查询单个模型容器详情。"""
    repo = _get_repository()
    item = repo.get_model(model_id)
    if item is None:
        return _error("模型不存在", 404, "MODEL_NOT_FOUND")
    return _ok(item.to_dict())


@earnings_bp.route("/assumptions", methods=["GET", "POST"])
def assumptions():
    """计算假设管理。

    GET: 查询假设列表，支持 variable_name 过滤。
    POST: 创建或更新假设。
    """
    repo = _get_repository()
    if request.method == "GET":
        variable_name = request.args.get("variable_name", "")
        items = repo.list_assumptions(
            variable_name, request.args.get("limit", 100, type=int)
        )
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = ImpactAssumption.from_dict(payload)
    repo.save_assumption(item)
    return _ok(item.to_dict()), 201


@earnings_bp.route("/inputs", methods=["GET", "POST"])
def inputs():
    """/assumptions 的兼容别名（旧端点）。"""
    repo = _get_repository()
    if request.method == "GET":
        name = request.args.get("name", "")
        items = repo.list_inputs(name, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = ImpactAssumption.from_dict(payload)
    repo.save_input(item)
    return _ok(item.to_dict()), 201


@earnings_bp.route("/exposures", methods=["GET", "POST"])
def exposures():
    """收入敞口管理。"""
    repo = _get_repository()
    if request.method == "GET":
        driver_id = request.args.get("driver_id", "")
        items = repo.list_exposures(driver_id, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = RevenueExposure.from_dict(payload)
    repo.save_exposure(item)
    return _ok(item.to_dict()), 201


@earnings_bp.route("/capitals", methods=["GET", "POST"])
def capitals():
    """股本版本管理。"""
    repo = _get_repository()
    if request.method == "GET":
        company_symbol = request.args.get("company_symbol", "")
        items = repo.list_capitals(company_symbol, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = ShareCapitalVersion.from_dict(payload)
    repo.save_capital(item)
    return _ok(item.to_dict()), 201


@earnings_bp.route("/scenarios", methods=["GET"])
def scenarios():
    """查询情景列表。

    GET: 按 impact_id 或 kind 查询。
    """
    repo = _get_repository()
    impact_id = request.args.get("impact_id", "")
    kind = request.args.get("kind", "")
    results = repo.list_impacts(limit=200)
    scenario_list: list[dict[str, Any]] = []
    for impact in results:
        if impact_id and impact.impact_id != impact_id:
            continue
        for scn in impact.scenarios:
            if kind and scn.kind != kind:
                continue
            scenario_list.append({**scn.to_dict(), "impact_id": impact.impact_id})
    return _ok(scenario_list, total=len(scenario_list))


@earnings_bp.route("/impacts", methods=["GET", "POST"])
def impacts():
    """测算结果管理（新名）。

    GET: 查询结果列表，支持 driver_id/status 过滤。
    POST: 保存已有结果（通常由 compute 生成）。
    """
    repo = _get_repository()
    if request.method == "GET":
        driver_id = request.args.get("driver_id", "")
        status = request.args.get("status", "")
        items = repo.list_impacts(driver_id, status, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = EarningsImpact.from_dict(payload)
    repo.save_impact(item)
    return _ok(item.to_dict()), 201


@earnings_bp.route("/results", methods=["GET", "POST"])
def results():
    """测算结果管理（兼容旧名 /results）。"""
    repo = _get_repository()
    if request.method == "GET":
        driver_id = request.args.get("driver_id", "")
        status = request.args.get("status", "")
        items = repo.list_results(driver_id, status, request.args.get("limit", 100, type=int))
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = EarningsImpact.from_dict(payload)
    repo.save_result(item)
    return _ok(item.to_dict()), 201


@earnings_bp.get("/impacts/<impact_id>")
def impact_detail(impact_id: str):
    """查询单个测算结果详情（新名）。"""
    repo = _get_repository()
    item = repo.get_impact(impact_id)
    if item is None:
        return _error("结果不存在", 404, "IMPACT_NOT_FOUND")
    return _ok(item.to_dict())


@earnings_bp.get("/results/<result_id>")
def result_detail(result_id: str):
    """查询单个测算结果详情（兼容旧名）。"""
    repo = _get_repository()
    item = repo.get_result(result_id)
    if item is None:
        return _error("结果不存在", 404, "RESULT_NOT_FOUND")
    return _ok(item.to_dict())


@earnings_bp.get("/gaps")
def gaps():
    """查询数据缺口。"""
    repo = _get_repository()
    driver_id = request.args.get("driver_id", "")
    items = repo.list_gaps(driver_id, request.args.get("limit", 100, type=int))
    return _ok([item.to_dict() for item in items], total=len(items))


def _parse_compute_payload(payload: dict[str, Any]) -> EarningsImpactRequest:
    """解析 compute/recompute 请求体为 EarningsImpactRequest。"""
    driver = EarningsDriver.from_dict(payload["driver"])
    exposure = RevenueExposure.from_dict(payload["exposure"]) if "exposure" in payload else None
    demand = DemandModel.from_dict(payload["demand"]) if "demand" in payload else None
    capacity = CapacityModel.from_dict(payload["capacity"]) if "capacity" in payload else None
    pricing = PricingModel.from_dict(payload["pricing"]) if "pricing" in payload else None
    margin = MarginModel.from_dict(payload["margin"]) if "margin" in payload else None
    market_share = ImpactAssumption.from_dict(payload["market_share"]) if "market_share" in payload else None
    capital_versions = [
        ShareCapitalVersion.from_dict(c) for c in payload.get("capital_versions", [])
    ]
    return EarningsImpactRequest(
        driver=driver,
        exposure=exposure,
        demand=demand,
        capacity=capacity,
        pricing=pricing,
        margin=margin,
        capital_versions=capital_versions,
        market_share=market_share,
        as_of=payload.get("as_of", ""),
        historical=payload.get("historical", True),
    )


@earnings_bp.post("/compute")
def compute():
    """执行盈利测算。

    请求体：
      driver: EarningsDriver
      exposure: RevenueExposure (可选)
      demand: DemandModel (可选)
      capacity: CapacityModel (可选)
      pricing: PricingModel (可选)
      margin: MarginModel (可选)
      capital_versions: list[ShareCapitalVersion] (可选)
      market_share: ImpactAssumption (可选)
      as_of: str (PIT 时点)
      historical: bool (默认 true)
      save: bool (是否保存到 DB，默认 true)
    """
    payload = request.get_json(silent=True) or {}
    if "driver" not in payload:
        return _error("缺少 driver 字段", 400, "MISSING_DRIVER")

    try:
        request_obj = _parse_compute_payload(payload)
    except Exception as exc:
        return _error(f"输入解析失败: {exc}", 400, "PARSE_ERROR")

    engine = _get_engine()
    result = engine.compute(request_obj)

    if payload.get("save", True):
        repo = _get_repository()
        repo.save_driver(request_obj.driver)
        if request_obj.exposure is not None:
            repo.save_exposure(request_obj.exposure)
        if request_obj.demand is not None:
            repo.save_assumption(request_obj.demand.market_size)
            repo.save_assumption(request_obj.demand.per_unit_usage)
            repo.save_assumption(request_obj.demand.market_growth_delta_input)
            repo.save_demand_model(request_obj.demand)
        if request_obj.capacity is not None:
            repo.save_assumption(request_obj.capacity.nameplate_capacity)
            repo.save_assumption(request_obj.capacity.effective_capacity)
            repo.save_assumption(request_obj.capacity.utilization_rate)
            repo.save_assumption(request_obj.capacity.yield_rate)
            repo.save_capacity_model(request_obj.capacity)
        if request_obj.pricing is not None:
            repo.save_assumption(request_obj.pricing.asp)
            repo.save_assumption(request_obj.pricing.unit_cost)
            repo.save_pricing_model(request_obj.pricing)
        if request_obj.margin is not None:
            repo.save_assumption(request_obj.margin.gross_margin)
            repo.save_assumption(request_obj.margin.expense_ratio)
            repo.save_assumption(request_obj.margin.tax_rate)
            repo.save_margin_model(request_obj.margin)
        if request_obj.market_share is not None:
            repo.save_assumption(request_obj.market_share)
        for cap in request_obj.capital_versions:
            repo.save_capital(cap)
        repo.save_impact(result)

    return _ok(result.to_dict())


@earnings_bp.post("/recompute")
def recompute():
    """基于变更假设增量重算。

    请求体：
      base_request: 原始 EarningsImpactRequest 的 JSON
      changed_assumptions: list[ImpactAssumption] 变更后的假设
      save: bool (是否保存到 DB，默认 true)
    """
    payload = request.get_json(silent=True) or {}
    if "base_request" not in payload:
        return _error("缺少 base_request 字段", 400, "MISSING_BASE_REQUEST")
    if "changed_assumptions" not in payload:
        return _error("缺少 changed_assumptions 字段", 400, "MISSING_CHANGED_ASSUMPTIONS")

    try:
        base_request = _parse_compute_payload(payload["base_request"])
        changed_assumptions = [
            ImpactAssumption.from_dict(a) for a in payload["changed_assumptions"]
        ]
    except Exception as exc:
        return _error(f"输入解析失败: {exc}", 400, "PARSE_ERROR")

    engine = _get_engine()
    result = engine.recompute(base_request, changed_assumptions)

    if payload.get("save", True):
        repo = _get_repository()
        repo.save_impact(result)

    return _ok(result.to_dict())


# ============================================================================
# P1.4：三表联动盈利预测 API
# ============================================================================

def _parse_forecast_build_payload(payload: dict[str, Any]) -> ForecastBuildRequest:
    """解析 /forecasts/build 请求体为 ForecastBuildRequest。"""
    symbol = str(payload.get("symbol", ""))
    as_of = str(payload.get("as_of", ""))
    period = str(payload.get("period", ""))
    horizon = str(payload.get("horizon", ForecastHorizon.ANNUAL.value))
    scenario = str(payload.get("scenario", EarningsScenarioKind.BASELINE.value))
    historical = bool(payload.get("historical", True))
    forecast_version = int(payload.get("forecast_version", 1))
    model_version = str(payload.get("model_version", "integrated_forecast_v1"))
    currency = str(payload.get("currency", "CNY"))
    unit = str(payload.get("unit", "yuan"))

    # 解析 segment_impacts（list[EarningsImpact]）
    segment_impacts = [
        EarningsImpact.from_dict(item) for item in payload.get("segment_impacts", [])
    ]

    # 解析 balance_inputs（dict[str, ImpactAssumption]）
    balance_inputs_raw = payload.get("balance_inputs", {}) or {}
    balance_inputs: dict[str, ImpactAssumption] = {}
    for var_name, item in balance_inputs_raw.items():
        balance_inputs[var_name] = ImpactAssumption.from_dict(item)

    # 解析 opening_balance（dict[str, float]）
    opening_balance_raw = payload.get("opening_balance", {}) or {}
    opening_balance: dict[str, float] = {}
    for k, v in opening_balance_raw.items():
        try:
            opening_balance[k] = float(v)
        except (TypeError, ValueError):
            continue

    # 解析 share_capital（可选）
    share_capital = None
    if payload.get("share_capital"):
        share_capital = ShareCapitalVersion.from_dict(payload["share_capital"])

    # 解析 capital_versions
    capital_versions = [
        ShareCapitalVersion.from_dict(c) for c in payload.get("capital_versions", [])
    ]

    return ForecastBuildRequest(
        symbol=symbol,
        as_of=as_of,
        period=period,
        horizon=horizon,
        scenario=scenario,
        segment_impacts=segment_impacts,
        balance_inputs=balance_inputs,
        opening_balance=opening_balance,
        share_capital=share_capital,
        capital_versions=capital_versions,
        historical=historical,
        model_version=model_version,
        forecast_version=forecast_version,
        currency=currency,
        unit=unit,
    )


@earnings_bp.post("/forecasts/build")
def forecasts_build():
    """构建三表联动盈利预测。

    请求体：
      symbol: str (必填)
      as_of: str (PIT 时点，必填)
      period: str (预测期间，必填)
      horizon: str ("quarterly" / "annual"，默认 annual)
      scenario: str ("bearish" / "baseline" / "bullish"，默认 baseline)
      segment_impacts: list[EarningsImpact] (产品/分部增量列表)
      balance_inputs: dict[str, ImpactAssumption] (DSO/DIO/capex_ratio/tax_rate 等)
      opening_balance: dict[str, float] (期初资产负债表)
      share_capital: ShareCapitalVersion (可选)
      capital_versions: list[ShareCapitalVersion] (可选)
      historical: bool (默认 true)
      model_version: str
      forecast_version: int (递增)
      currency: str
      unit: str
      save: bool (默认 true)
    """
    payload = request.get_json(silent=True) or {}
    if not payload.get("symbol"):
        return _error("缺少 symbol 字段", 400, "MISSING_SYMBOL")
    if not payload.get("as_of") and payload.get("historical", True):
        return _error("historical=true 时必须提供 as_of", 400, "MISSING_AS_OF")
    if not payload.get("period"):
        return _error("缺少 period 字段", 400, "MISSING_PERIOD")

    try:
        request_obj = _parse_forecast_build_payload(payload)
    except Exception as exc:
        return _error(f"输入解析失败: {exc}", 400, "PARSE_ERROR")

    forecast = IntegratedForecastEngine.build_forecast(request_obj)

    if payload.get("save", True) and forecast.as_of:
        repo = _get_repository()
        try:
            repo.save_forecast(forecast)
        except ValueError as exc:
            return _error(str(exc), 409, "FORECAST_VERSION_EXISTS")

    return _ok(forecast.to_dict())


@earnings_bp.route("/forecasts", methods=["GET", "POST"])
def forecasts():
    """三表联动预测管理。

    GET: 查询预测列表，支持 symbol/period/scenario 过滤。
    POST: 直接保存已构建的预测（用于外部生成的 forecast 持久化）。
    """
    repo = _get_repository()
    if request.method == "GET":
        symbol = request.args.get("symbol", "")
        period = request.args.get("period", "")
        scenario = request.args.get("scenario", "")
        items = repo.list_forecasts(
            symbol=symbol, period=period, scenario=scenario,
            limit=request.args.get("limit", 100, type=int),
        )
        return _ok([item.to_dict() for item in items], total=len(items))

    payload = request.get_json(silent=True) or {}
    item = IntegratedForecast.from_dict(payload)
    try:
        repo.save_forecast(item)
    except ValueError as exc:
        return _error(str(exc), 409, "FORECAST_VERSION_EXISTS")
    return _ok(item.to_dict()), 201


@earnings_bp.get("/forecasts/latest")
def forecast_latest():
    """获取 PIT 可见的最新预测版本。

    Query params:
        symbol: 公司代码（必填）
        period: 期间（必填）
        scenario: 情景过滤（可选）
        as_of: PIT 时点（可选）
    """
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "")
    scenario = request.args.get("scenario", "")
    as_of = request.args.get("as_of", "")
    if not symbol or not period:
        return _error("缺少 symbol 或 period 参数", 400, "MISSING_PARAMS")
    repo = _get_repository()
    item = repo.get_latest_forecast(symbol, period, scenario=scenario, as_of=as_of)
    if item is None:
        return _error("预测不存在", 404, "FORECAST_NOT_FOUND")
    return _ok(item.to_dict())


@earnings_bp.get("/forecasts/versions")
def forecast_versions():
    """查询预测的所有版本号。

    Query params:
        symbol: 公司代码（必填）
        period: 期间（必填）
        scenario: 情景过滤（可选）
    """
    symbol = request.args.get("symbol", "")
    period = request.args.get("period", "")
    scenario = request.args.get("scenario", "")
    if not symbol or not period:
        return _error("缺少 symbol 或 period 参数", 400, "MISSING_PARAMS")
    repo = _get_repository()
    versions = repo.list_forecast_versions(symbol, period, scenario=scenario)
    return _ok({"versions": versions, "total": len(versions)})


@earnings_bp.get("/forecasts/<forecast_id>")
def forecast_detail(forecast_id: str):
    """查询单个预测详情。"""
    repo = _get_repository()
    item = repo.get_forecast(forecast_id)
    if item is None:
        return _error("预测不存在", 404, "FORECAST_NOT_FOUND")
    return _ok(item.to_dict())
