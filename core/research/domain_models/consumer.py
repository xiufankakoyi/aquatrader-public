"""P3.2 消费/渠道行业模型。

按 P3 计划 Task P3.2 §2：
- 消费/渠道：销量、价格、门店/渠道、库存、获客和复购
- 只处理行业特有逻辑，通用三表和估值复用 P1
"""
from __future__ import annotations

from typing import Any

from core.research.domain_models.base import (
    _as_mapping,
    _finite_float,
    _safe_float,
    _safe_text,
    CompanyProfile,
    DomainModelBase,
    InputRequirement,
    IntegratedForecast,
    ResearchRisk,
    ValidationReport,
)


class ConsumerModel(DomainModelBase):
    """消费/渠道行业模型。

    驱动逻辑：门店数 × 单店收入 + 线上销量 × 客单价 - 获客成本
    风险因子：复购率、获客成本上升、库存周转
    """

    model_id = "consumer_v1"
    applicable_industries = (
        "食品饮料", "纺织服装", "商业百货", "零售", "餐饮",
        "家电", "美容护理", "农林牧渔", "轻工制造",
    )

    def supports(self, profile: CompanyProfile) -> bool:
        """判断是否为消费/渠道行业。"""
        industry = _safe_text(getattr(profile, "industry", None))
        business_model = _safe_text(getattr(profile, "business_model", None))
        if industry in self.applicable_industries:
            return True
        if business_model == "stable_cashflow" and any(
            keyword in industry for keyword in ("消费", "零售", "食品")
        ):
            return True
        return False

    def required_inputs(self) -> InputRequirement:
        """返回消费行业模型输入要求。"""
        return InputRequirement(
            required_fields={
                "store_count": "int 门店数",
                "revenue_per_store": "float 单店年收入（万元）",
                "online_revenue": "float 线上收入（万元）",
                "customer_acquisition_cost": "float 获客成本（元/人）",
                "repurchase_rate": "float 复购率 [0,1]",
            },
            optional_fields={
                "inventory_turnover_days": "int 库存周转天数",
                "gross_margin": "float 毛利率",
                "marketing_ratio": "float 营销费用率",
            },
            data_source_refs=["tushare", "company_annual_report"],
        )

    def build_forecast(
        self,
        snapshot: dict[str, Any],
        assumptions: dict[str, Any],
    ) -> IntegratedForecast:
        """构建消费行业整合预测。"""
        snapshot = _as_mapping(snapshot)
        assumptions = _as_mapping(assumptions)
        symbol = _safe_text(snapshot.get("symbol"))
        period = _safe_text(assumptions.get("forecast_period"), "2024Q4")
        store_count = _safe_float(snapshot.get("store_count"))
        rev_per_store = _safe_float(snapshot.get("revenue_per_store"))
        online_rev = _safe_float(snapshot.get("online_revenue"))
        cac = _safe_float(snapshot.get("customer_acquisition_cost"))
        repurchase = _safe_float(snapshot.get("repurchase_rate"), 0.5)
        # 假设
        store_growth = _safe_float(assumptions.get("store_growth"))
        rev_per_store_growth = _safe_float(assumptions.get("revenue_per_store_growth"))
        online_growth = _safe_float(assumptions.get("online_growth"))
        cac_change = _safe_float(assumptions.get("cac_change"))
        # 预测
        forecast_stores = store_count * (1 + store_growth)
        forecast_rev_per_store = rev_per_store * (1 + rev_per_store_growth)
        forecast_online = online_rev * (1 + online_growth)
        forecast_cac = cac * (1 + cac_change)
        offline_revenue = forecast_stores * forecast_rev_per_store
        revenue = offline_revenue + forecast_online
        # 假设毛利率 40%，营销费用 15%
        gross_margin = _safe_float(snapshot.get("gross_margin"), 0.4)
        marketing_ratio = _safe_float(snapshot.get("marketing_ratio"), 0.15)
        gross_profit = revenue * gross_margin
        marketing_cost = revenue * marketing_ratio
        # 获客成本：新增客户数 × CAC（简化为收入的 5%）
        new_customer_cost = revenue * 0.05 * (forecast_cac / 100)
        net_profit = gross_profit - marketing_cost - new_customer_cost
        cash_flow = net_profit * 0.8  # 消费行业现金流转化率较高
        valuation_range = {
            "low": net_profit * 15,
            "mid": net_profit * 25,
            "high": net_profit * 35,
        }
        confidence = _safe_float(assumptions.get("confidence"), 0.65)
        return IntegratedForecast(
            symbol=symbol,
            forecast_period=period,
            revenue=revenue,
            net_profit=net_profit,
            cash_flow=cash_flow,
            valuation_range=valuation_range,
            confidence=confidence,
            drivers={
                "store_count": forecast_stores,
                "revenue_per_store": forecast_rev_per_store,
                "online_revenue": forecast_online,
                "repurchase_rate": repurchase,
                "cac": forecast_cac,
            },
            assumptions=[
                f"门店增长 {store_growth:.1%}",
                f"单店收入增长 {rev_per_store_growth:.1%}",
                f"线上增长 {online_growth:.1%}",
                f"获客成本变化 {cac_change:.1%}",
            ],
        )

    def build_risks(self, snapshot: dict[str, Any]) -> ResearchRisk:
        """构建消费行业研究风险。"""
        snapshot = _as_mapping(snapshot)
        symbol = _safe_text(snapshot.get("symbol"))
        risk_factors: dict[str, str] = {}
        repurchase = _safe_float(snapshot.get("repurchase_rate"), 0.5)
        if repurchase < 0.3:
            risk_factors["复购率"] = f"复购率 {repurchase:.0%}，客户黏性不足"
        cac = _safe_float(snapshot.get("customer_acquisition_cost"))
        if cac > 200:
            risk_factors["获客成本"] = f"获客成本 {cac:.0f} 元/人，营销压力大"
        inventory_days = _safe_float(snapshot.get("inventory_turnover_days"))
        if inventory_days > 120:
            risk_factors["库存"] = f"库存周转 {inventory_days} 天，渠道压货风险"
        risk_level = "high" if len(risk_factors) >= 2 else "medium" if risk_factors else "low"
        return ResearchRisk(
            symbol=symbol,
            risk_factors=risk_factors,
            risk_level=risk_level,
            mitigation=["跟踪月度复购率", "监控获客成本趋势"],
            invalidation_conditions=[
                "复购率持续低于 20%",
                "获客成本上升超 50%",
                "核心品类市占率下滑",
            ],
        )

    def validate(self, output: IntegratedForecast) -> ValidationReport:
        """验证消费行业模型输出。"""
        if not isinstance(output, IntegratedForecast):
            return ValidationReport(False, ["预测输出类型无效"], [], {})
        errors: list[str] = []
        warnings: list[str] = []
        revenue = _finite_float(output.revenue)
        net_profit = _finite_float(output.net_profit)
        confidence = _finite_float(output.confidence)
        if revenue is None:
            errors.append("营收数值无效")
        elif revenue < 0:
            errors.append("营收不能为负")
        if confidence is None or confidence < 0 or confidence > 1:
            errors.append(f"置信度越界：{output.confidence}")
        if net_profit is not None and revenue:
            net_margin = net_profit / revenue
        else:
            net_margin = 0.0
        if revenue and net_profit is not None and net_margin < 0.05:
            warnings.append("净利率低于 5%，需关注盈利能力")
        return ValidationReport(
            passed=not errors,
            errors=errors,
            warnings=warnings,
            metrics={
                "net_margin": net_margin,
            },
        )
