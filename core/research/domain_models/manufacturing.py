"""P3.2 制造/硬件行业模型。

按 P3 计划 Task P3.2 §1：
- 制造/硬件：产能、良率、ASP、材料成本、客户认证
- 只处理行业特有逻辑，通用三表和估值复用 P1
- 不输出交易动作
"""
from __future__ import annotations

from collections.abc import Mapping
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


class ManufacturingModel(DomainModelBase):
    """制造/硬件行业模型。

    驱动逻辑：产能 × 良率 × ASP - 材料成本 - 固定成本 = 毛利
    风险因子：客户认证周期、产能爬坡、材料价格波动
    """

    model_id = "manufacturing_v1"
    applicable_industries = (
        "半导体", "电子", "机械", "电气设备", "汽车零部件",
        "国防军工", "家电", "通信设备",
    )

    def supports(self, profile: CompanyProfile) -> bool:
        """判断是否为制造/硬件行业。"""
        industry = _safe_text(getattr(profile, "industry", None))
        business_model = _safe_text(getattr(profile, "business_model", None))
        sub_industry = _safe_text(getattr(profile, "sub_industry", None))
        if industry in self.applicable_industries:
            return True
        # 子行业匹配
        if sub_industry in self.applicable_industries:
            return True
        # 商业模式匹配
        if business_model == "cyclical" and any(
            keyword in industry for keyword in ("制造", "硬件", "设备")
        ):
            return True
        return False

    def required_inputs(self) -> InputRequirement:
        """返回制造行业模型输入要求。"""
        return InputRequirement(
            required_fields={
                "capacity": "float 产能（万件/年）",
                "utilization_rate": "float 产能利用率 [0,1]",
                "yield_rate": "float 良率 [0,1]",
                "asp": "float 平均售价（元/件）",
                "material_cost_per_unit": "float 单位材料成本（元/件）",
                "fixed_cost": "float 固定成本（万元）",
            },
            optional_fields={
                "customer_certification_period": "int 客户认证周期（月）",
                "r_and_d_ratio": "float 研发费用率",
                "inventory_days": "int 库存周转天数",
            },
            data_source_refs=["tushare", "company_annual_report"],
        )

    def build_forecast(
        self,
        snapshot: dict[str, Any],
        assumptions: dict[str, Any],
    ) -> IntegratedForecast:
        """构建制造行业整合预测。

        Args:
            snapshot: 含 capacity / utilization_rate / yield_rate / asp / material_cost_per_unit / fixed_cost
            assumptions: 含产能扩张系数、ASP 变化率、材料成本变化率

        Returns:
            IntegratedForecast: 含营收、净利润、现金流、估值区间
        """
        snapshot = _as_mapping(snapshot)
        assumptions = _as_mapping(assumptions)
        symbol = _safe_text(snapshot.get("symbol"))
        period = _safe_text(assumptions.get("forecast_period"), "2024Q4")
        # 基础驱动
        capacity = _safe_float(snapshot.get("capacity"))
        utilization = _safe_float(snapshot.get("utilization_rate"), 0.8)
        yield_rate = _safe_float(snapshot.get("yield_rate"), 0.95)
        asp = _safe_float(snapshot.get("asp"))
        material_cost = _safe_float(snapshot.get("material_cost_per_unit"))
        fixed_cost = _safe_float(snapshot.get("fixed_cost"))
        # 假设变化
        capacity_growth = _safe_float(assumptions.get("capacity_growth"))
        asp_change = _safe_float(assumptions.get("asp_change"))
        material_cost_change = _safe_float(assumptions.get("material_cost_change"))
        # 预测
        forecast_capacity = capacity * (1 + capacity_growth)
        forecast_asp = asp * (1 + asp_change)
        forecast_material = material_cost * (1 + material_cost_change)
        effective_volume = forecast_capacity * utilization * yield_rate
        revenue = effective_volume * forecast_asp
        gross_profit = revenue - effective_volume * forecast_material
        net_profit = gross_profit - fixed_cost
        # 现金流简化：净利润 + 折旧（假设 10% 固定成本）
        cash_flow = net_profit + fixed_cost * 0.1
        # 估值区间：PE 法，假设 10-20 倍
        valuation_range = {
            "low": net_profit * 10,
            "mid": net_profit * 15,
            "high": net_profit * 20,
        }
        confidence = _safe_float(assumptions.get("confidence"), 0.6)
        return IntegratedForecast(
            symbol=symbol,
            forecast_period=period,
            revenue=revenue,
            net_profit=net_profit,
            cash_flow=cash_flow,
            valuation_range=valuation_range,
            confidence=confidence,
            drivers={
                "capacity": forecast_capacity,
                "utilization_rate": utilization,
                "yield_rate": yield_rate,
                "asp": forecast_asp,
                "material_cost_per_unit": forecast_material,
            },
            assumptions=[
                f"产能增长 {capacity_growth:.1%}",
                f"ASP 变化 {asp_change:.1%}",
                f"材料成本变化 {material_cost_change:.1%}",
            ],
        )

    def build_risks(self, snapshot: dict[str, Any]) -> ResearchRisk:
        """构建制造行业研究风险。"""
        snapshot = _as_mapping(snapshot)
        symbol = _safe_text(snapshot.get("symbol"))
        risk_factors: dict[str, str] = {}
        cert_period = _safe_float(snapshot.get("customer_certification_period"))
        if cert_period > 12:
            risk_factors["客户认证"] = f"认证周期 {cert_period} 个月，新客户拓展缓慢"
        utilization = _safe_float(snapshot.get("utilization_rate"), 0.8)
        if utilization < 0.6:
            risk_factors["产能利用率"] = f"利用率 {utilization:.0%}，固定成本负担重"
        inventory_days = _safe_float(snapshot.get("inventory_days"))
        if inventory_days > 90:
            risk_factors["库存"] = f"库存周转 {inventory_days} 天，需求疲软风险"
        risk_level = "high" if len(risk_factors) >= 2 else "medium" if risk_factors else "low"
        return ResearchRisk(
            symbol=symbol,
            risk_factors=risk_factors,
            risk_level=risk_level,
            mitigation=["跟踪季度产能利用率", "监控客户认证进展"],
            invalidation_conditions=[
                "产能利用率持续低于 50%",
                "核心客户认证失败",
                "ASP 持续下跌超 20%",
            ],
        )

    def validate(self, output: IntegratedForecast) -> ValidationReport:
        """验证制造行业模型输出。"""
        if not isinstance(output, IntegratedForecast):
            return ValidationReport(False, ["预测输出类型无效"], [], {})
        errors: list[str] = []
        warnings: list[str] = []
        revenue = _finite_float(output.revenue)
        net_profit = _finite_float(output.net_profit)
        cash_flow = _finite_float(output.cash_flow)
        confidence = _finite_float(output.confidence)
        if revenue is None:
            errors.append("营收数值无效")
        elif revenue < 0:
            errors.append("营收不能为负")
        if net_profit is not None and net_profit < 0:
            warnings.append("净利润为负，需检查成本结构")
        if confidence is None or not (0 <= confidence <= 1):
            errors.append(f"置信度越界：{output.confidence}")
        valuation_range = output.valuation_range
        mid = (
            _finite_float(valuation_range.get("mid"))
            if isinstance(valuation_range, Mapping)
            else None
        )
        if not mid:
            errors.append("估值区间 mid 不能为空")
        return ValidationReport(
            passed=not errors,
            errors=errors,
            warnings=warnings,
            metrics={
                "gross_margin": (
                    (revenue - cash_flow) / revenue
                    if revenue and cash_flow is not None else 0.0
                ),
            },
        )
