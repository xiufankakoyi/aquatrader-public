"""P3.2 周期/资源行业模型。

按 P3 计划 Task P3.2 §3：
- 周期/资源：产量、商品价格、成本曲线、资本开支和库存周期
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


class CyclicalModel(DomainModelBase):
    """周期/资源行业模型。

    驱动逻辑：产量 × 商品价格 - 成本曲线 - 资本开支
    风险因子：商品价格周期、库存周期、资本开支节奏
    """

    model_id = "cyclical_v1"
    applicable_industries = (
        "采掘", "钢铁", "有色金属", "化工", "建筑材料",
        "煤炭", "石油石化", "基础化学",
    )

    def supports(self, profile: CompanyProfile) -> bool:
        """判断是否为周期/资源行业。"""
        industry = _safe_text(getattr(profile, "industry", None))
        business_model = _safe_text(getattr(profile, "business_model", None))
        if industry in self.applicable_industries:
            return True
        if business_model == "cyclical" and any(
            keyword in industry for keyword in ("资源", "周期", "大宗")
        ):
            return True
        return False

    def required_inputs(self) -> InputRequirement:
        """返回周期行业模型输入要求。"""
        return InputRequirement(
            required_fields={
                "production_volume": "float 产量（万吨）",
                "commodity_price": "float 商品价格（元/吨）",
                "unit_cost": "float 单位成本（元/吨）",
                "capex": "float 资本开支（万元）",
            },
            optional_fields={
                "inventory_cycle_days": "int 库存周期天数",
                "cost_curve_position": "float 成本曲线分位 [0,1]",
                "capacity_utilization": "float 产能利用率",
            },
            data_source_refs=["tushare", "commodity_market_data"],
        )

    def build_forecast(
        self,
        snapshot: dict[str, Any],
        assumptions: dict[str, Any],
    ) -> IntegratedForecast:
        """构建周期行业整合预测。"""
        snapshot = _as_mapping(snapshot)
        assumptions = _as_mapping(assumptions)
        symbol = _safe_text(snapshot.get("symbol"))
        period = _safe_text(assumptions.get("forecast_period"), "2024Q4")
        volume = _safe_float(snapshot.get("production_volume"))
        price = _safe_float(snapshot.get("commodity_price"))
        unit_cost = _safe_float(snapshot.get("unit_cost"))
        capex = _safe_float(snapshot.get("capex"))
        # 假设
        volume_change = _safe_float(assumptions.get("volume_change"))
        price_change = _safe_float(assumptions.get("price_change"))
        cost_change = _safe_float(assumptions.get("cost_change"))
        # 预测
        forecast_volume = volume * (1 + volume_change)
        forecast_price = price * (1 + price_change)
        forecast_cost = unit_cost * (1 + cost_change)
        revenue = forecast_volume * forecast_price
        total_cost = forecast_volume * forecast_cost
        # 折旧：资本开支 10 年线性折旧
        depreciation = capex / 10
        operating_profit = revenue - total_cost - depreciation
        # 利息费用假设 5% 资本开支
        interest = capex * 0.05
        net_profit = operating_profit - interest
        # 周期行业现金流波动大
        cash_flow = net_profit + depreciation - capex * 0.3
        # 估值：周期行业用 PB 法，假设 1-3 倍
        # 简化用净资产的 1-3 倍
        book_value = capex * 5  # 简化
        valuation_range = {
            "low": book_value * 1.0,
            "mid": book_value * 2.0,
            "high": book_value * 3.0,
        }
        confidence = _safe_float(assumptions.get("confidence"), 0.5)  # 周期行业置信度较低
        return IntegratedForecast(
            symbol=symbol,
            forecast_period=period,
            revenue=revenue,
            net_profit=net_profit,
            cash_flow=cash_flow,
            valuation_range=valuation_range,
            confidence=confidence,
            drivers={
                "production_volume": forecast_volume,
                "commodity_price": forecast_price,
                "unit_cost": forecast_cost,
                "capex": capex,
            },
            assumptions=[
                f"产量变化 {volume_change:.1%}",
                f"价格变化 {price_change:.1%}",
                f"成本变化 {cost_change:.1%}",
            ],
        )

    def build_risks(self, snapshot: dict[str, Any]) -> ResearchRisk:
        """构建周期行业研究风险。"""
        snapshot = _as_mapping(snapshot)
        symbol = _safe_text(snapshot.get("symbol"))
        risk_factors: dict[str, str] = {}
        cost_position = _safe_float(snapshot.get("cost_curve_position"), -1.0)
        if cost_position > 0.7:
            risk_factors["成本曲线"] = f"成本分位 {cost_position:.0%}，行业高位"
        inventory_days = _safe_float(snapshot.get("inventory_cycle_days"))
        if inventory_days > 60:
            risk_factors["库存周期"] = f"库存 {inventory_days} 天，下行压力"
        utilization = _safe_float(snapshot.get("capacity_utilization"), 1.0)
        if utilization < 0.7:
            risk_factors["产能利用"] = f"利用率 {utilization:.0%}，过剩风险"
        risk_level = "high" if len(risk_factors) >= 2 else "medium" if risk_factors else "low"
        return ResearchRisk(
            symbol=symbol,
            risk_factors=risk_factors,
            risk_level=risk_level,
            mitigation=["跟踪商品价格曲线", "监控库存周期拐点"],
            invalidation_conditions=[
                "商品价格跌破现金成本",
                "产能利用率持续低于 60%",
                "库存周期进入主动去库存阶段",
            ],
        )

    def validate(self, output: IntegratedForecast) -> ValidationReport:
        """验证周期行业模型输出。"""
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
        if confidence is None or not (0 <= confidence <= 1):
            errors.append(f"置信度越界：{output.confidence}")
        cash_flow = _finite_float(output.cash_flow)
        if cash_flow is not None and cash_flow < 0:
            warnings.append("现金流为负，需关注资本开支节奏")
        return ValidationReport(
            passed=not errors,
            errors=errors,
            warnings=warnings,
            metrics={
                "operating_margin": (
                    net_profit / revenue if revenue and net_profit is not None else 0.0
                ),
            },
        )
