"""P3.2 行业专用模型框架测试。"""
from __future__ import annotations

import pytest

from core.research.domain_models.base import (
    CompanyProfile,
    DomainModelError,
)
from core.research.domain_models.consumer import ConsumerModel
from core.research.domain_models.cyclical import CyclicalModel
from core.research.domain_models.manufacturing import ManufacturingModel
from core.research.domain_models.registry import (
    DomainModelRegistry,
    get_default_registry,
    register_default_models,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _profile(industry: str, business_model: str = "cyclical") -> CompanyProfile:
    return CompanyProfile(
        symbol="600519.SH",
        industry=industry,
        business_model=business_model,
        sub_industry="",
        listed_at="2001-08-27",
    )


def _manufacturing_snapshot() -> dict:
    return {
        "symbol": "600585.SH",
        "capacity": 1000.0,  # 万件/年
        "utilization_rate": 0.85,
        "yield_rate": 0.95,
        "asp": 100.0,  # 元/件
        "material_cost_per_unit": 50.0,
        "fixed_cost": 5000.0,  # 万元
        "customer_certification_period": 18,
        "inventory_days": 100,
    }


def _consumer_snapshot() -> dict:
    return {
        "symbol": "600519.SH",
        "store_count": 5000,
        "revenue_per_store": 200.0,
        "online_revenue": 50000.0,
        "customer_acquisition_cost": 250.0,
        "repurchase_rate": 0.45,
        "inventory_turnover_days": 60,
        "gross_margin": 0.5,
        "marketing_ratio": 0.12,
    }


def _cyclical_snapshot() -> dict:
    return {
        "symbol": "601899.SH",
        "production_volume": 100.0,
        "commodity_price": 5000.0,
        "unit_cost": 3500.0,
        "capex": 50000.0,
        "inventory_cycle_days": 45,
        "cost_curve_position": 0.4,
        "capacity_utilization": 0.85,
    }


# ---------------------------------------------------------------------------
# 注册表测试
# ---------------------------------------------------------------------------


class TestDomainModelRegistry:
    """行业模型注册表测试。"""

    def test_register_and_get(self) -> None:
        registry = DomainModelRegistry()
        model = ManufacturingModel()
        registry.register(model)
        assert registry.get("manufacturing_v1") is model
        assert registry.get("nonexistent") is None

    def test_register_duplicate_rejected(self) -> None:
        registry = DomainModelRegistry()
        registry.register(ManufacturingModel())
        with pytest.raises(DomainModelError, match="已存在"):
            registry.register(ManufacturingModel())

    def test_register_empty_id_rejected(self) -> None:
        registry = DomainModelRegistry()

        class EmptyModel(ManufacturingModel):
            model_id = ""

        with pytest.raises(DomainModelError, match="model_id 不能为空"):
            registry.register(EmptyModel())

    def test_select_for_returns_applicable(self) -> None:
        registry = DomainModelRegistry()
        registry.register(ManufacturingModel())
        registry.register(ConsumerModel())
        registry.register(CyclicalModel())
        # 半导体公司 → 制造模型
        semiconductor = _profile("半导体")
        applicable = registry.select_for(semiconductor)
        assert len(applicable) == 1
        assert applicable[0].model_id == "manufacturing_v1"

    def test_select_for_no_match(self) -> None:
        registry = DomainModelRegistry()
        registry.register(ManufacturingModel())
        # 金融行业 → 无匹配
        bank = _profile("银行", "financial")
        applicable = registry.select_for(bank)
        assert applicable == []

    def test_select_for_sorted_by_model_id(self) -> None:
        registry = DomainModelRegistry()
        registry.register(CyclicalModel())
        registry.register(ManufacturingModel())
        registry.register(ConsumerModel())
        # 化工 → 匹配周期；某些行业可能匹配多个
        all_models = registry.list_all()
        model_ids = [m.model_id for m in all_models]
        assert "manufacturing_v1" in model_ids
        assert "consumer_v1" in model_ids
        assert "cyclical_v1" in model_ids

    def test_register_default_models(self) -> None:
        """登记默认 3 个行业模型。"""
        registry = register_default_models()
        assert len(registry.list_all()) == 3
        assert registry.get("manufacturing_v1") is not None
        assert registry.get("consumer_v1") is not None
        assert registry.get("cyclical_v1") is not None

    def test_get_default_registry_singleton(self) -> None:
        """全局默认注册表是单例。"""
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2

    def test_registry_fails_closed_for_malformed_inputs(self) -> None:
        registry = DomainModelRegistry()
        assert registry.get([]) is None
        assert registry.select_for(None) == []
        with pytest.raises(DomainModelError, match="类型无效"):
            registry.register(None)


# ---------------------------------------------------------------------------
# 制造业模型测试
# ---------------------------------------------------------------------------


class TestManufacturingModel:
    """制造/硬件行业模型测试。"""

    def test_supports_semiconductor(self) -> None:
        model = ManufacturingModel()
        assert model.supports(_profile("半导体"))
        assert model.supports(_profile("电子"))
        assert model.supports(_profile("机械"))

    def test_not_supports_bank(self) -> None:
        model = ManufacturingModel()
        assert not model.supports(_profile("银行", "financial"))

    def test_required_inputs(self) -> None:
        model = ManufacturingModel()
        req = model.required_inputs()
        assert "capacity" in req.required_fields
        assert "yield_rate" in req.required_fields
        assert "asp" in req.required_fields
        assert "tushare" in req.data_source_refs

    def test_build_forecast(self) -> None:
        model = ManufacturingModel()
        forecast = model.build_forecast(
            _manufacturing_snapshot(),
            {"forecast_period": "2024Q4", "capacity_growth": 0.1, "asp_change": 0.05},
        )
        assert forecast.symbol == "600585.SH"
        assert forecast.revenue > 0
        assert forecast.valuation_range["mid"] == forecast.net_profit * 15
        assert len(forecast.assumptions) == 3

    def test_build_risks_high_utilization_ok(self) -> None:
        model = ManufacturingModel()
        snapshot = _manufacturing_snapshot()
        risk = model.build_risks(snapshot)
        # cert_period=18 > 12 → 客户认证风险
        assert "客户认证" in risk.risk_factors
        # inventory_days=100 > 90 → 库存风险
        assert "库存" in risk.risk_factors
        assert risk.risk_level == "high"

    def test_build_risks_low_utilization(self) -> None:
        model = ManufacturingModel()
        snapshot = _manufacturing_snapshot()
        snapshot["utilization_rate"] = 0.5
        risk = model.build_risks(snapshot)
        assert "产能利用率" in risk.risk_factors

    def test_validate_passes(self) -> None:
        model = ManufacturingModel()
        forecast = model.build_forecast(
            _manufacturing_snapshot(),
            {"confidence": 0.7},
        )
        report = model.validate(forecast)
        assert report.passed
        assert "gross_margin" in report.metrics

    def test_validate_rejects_negative_revenue(self) -> None:
        from core.research.domain_models.base import IntegratedForecast

        model = ManufacturingModel()
        bad_output = IntegratedForecast(
            symbol="X",
            forecast_period="2024Q4",
            revenue=-100.0,
            net_profit=-50.0,
            cash_flow=-30.0,
            valuation_range={"low": 0, "mid": 0, "high": 0},
            confidence=0.5,
            drivers={},
            assumptions=[],
        )
        report = model.validate(bad_output)
        assert not report.passed
        assert any("营收不能为负" in e for e in report.errors)

    def test_no_trade_actions_in_output(self) -> None:
        """按 AGENTS.md：不输出交易动作。"""
        model = ManufacturingModel()
        forecast = model.build_forecast(_manufacturing_snapshot(), {})
        risk = model.build_risks(_manufacturing_snapshot())
        # 检查输出字段不包含交易动作关键词
        forbidden = {"buy", "sell", "position", "stop_loss", "take_profit"}
        output_text = str(forecast) + str(risk)
        for word in forbidden:
            assert word not in output_text.lower(), f"输出含交易动作：{word}"

    def test_malformed_inputs_degrade_without_raising(self) -> None:
        model = ManufacturingModel()
        forecast = model.build_forecast(None, {"capacity": float("nan")})
        assert forecast.revenue == 0
        assert model.build_risks(None).risk_level == "low"
        assert not model.validate(None).passed


# ---------------------------------------------------------------------------
# 消费行业模型测试
# ---------------------------------------------------------------------------


class TestConsumerModel:
    """消费/渠道行业模型测试。"""

    def test_supports_food_beverage(self) -> None:
        model = ConsumerModel()
        assert model.supports(_profile("食品饮料", "stable_cashflow"))
        assert model.supports(_profile("商业百货", "stable_cashflow"))

    def test_not_supports_bank(self) -> None:
        model = ConsumerModel()
        assert not model.supports(_profile("银行", "financial"))

    def test_build_forecast(self) -> None:
        model = ConsumerModel()
        forecast = model.build_forecast(
            _consumer_snapshot(),
            {"forecast_period": "2024Q4", "store_growth": 0.05, "online_growth": 0.2},
        )
        assert forecast.symbol == "600519.SH"
        assert forecast.revenue > 0
        assert "store_count" in forecast.drivers

    def test_build_risks_high_cac(self) -> None:
        model = ConsumerModel()
        snapshot = _consumer_snapshot()
        risk = model.build_risks(snapshot)
        # cac=250 > 200 → 获客成本风险
        assert "获客成本" in risk.risk_factors

    def test_validate(self) -> None:
        model = ConsumerModel()
        forecast = model.build_forecast(_consumer_snapshot(), {"confidence": 0.7})
        report = model.validate(forecast)
        assert report.passed

    def test_malformed_inputs_degrade_without_raising(self) -> None:
        model = ConsumerModel()
        forecast = model.build_forecast(None, {"confidence": float("nan")})
        assert forecast.revenue == 0
        assert model.build_risks(None).risk_level == "low"
        assert not model.validate(None).passed


# ---------------------------------------------------------------------------
# 周期行业模型测试
# ---------------------------------------------------------------------------


class TestCyclicalModel:
    """周期/资源行业模型测试。"""

    def test_supports_steel(self) -> None:
        model = CyclicalModel()
        assert model.supports(_profile("钢铁"))
        assert model.supports(_profile("煤炭"))

    def test_build_forecast(self) -> None:
        model = CyclicalModel()
        forecast = model.build_forecast(
            _cyclical_snapshot(),
            {"forecast_period": "2024Q4", "price_change": -0.1},
        )
        assert forecast.symbol == "601899.SH"
        assert forecast.revenue > 0
        # 周期行业默认置信度较低
        assert forecast.confidence <= 0.7

    def test_build_risks_high_cost_position(self) -> None:
        model = CyclicalModel()
        snapshot = _cyclical_snapshot()
        snapshot["cost_curve_position"] = 0.85
        risk = model.build_risks(snapshot)
        assert "成本曲线" in risk.risk_factors

    def test_validate(self) -> None:
        model = CyclicalModel()
        forecast = model.build_forecast(_cyclical_snapshot(), {"confidence": 0.5})
        report = model.validate(forecast)
        assert report.passed
        assert "operating_margin" in report.metrics

    def test_malformed_inputs_degrade_without_raising(self) -> None:
        model = CyclicalModel()
        forecast = model.build_forecast(None, {"confidence": float("nan")})
        assert forecast.revenue == 0
        assert model.build_risks(None).risk_level == "low"
        assert not model.validate(None).passed
