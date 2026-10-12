"""EarningsImpactEngine：盈利映射核心计算。

真实计算并保留区间传播：
  新增需求 = 市场规模变化 × 单机用量或渗透率
  公司可兑现销量 = min(新增需求 × 公司份额, 有效产能 × 利用率 × 良率 × 爬坡进度)
  新增收入 = 公司可兑现销量 × ASP × (1 - 收入确认滞后)
  新增毛利 = 新增收入 × 增量毛利率
  新增净利润 = 新增毛利 - 增量费用 - 税费
  EPS影响 = 新增净利润 / point-in-time 总股本

输出 bearish/base/bullish 三情景和敏感性矩阵。
缺输入返回 insufficient_data + gaps，严禁默认比例或伪精确。
valuation_bridge 启发式结果只能标为 exploration hypothesis，硬阻断进入正式 EarningsImpact。
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    AssumptionSourceType,
    CapacityModel,
    DemandModel,
    EarningsDriver,
    EarningsImpact,
    EarningsScenario,
    EarningsScenarioKind,
    EarningsStatus,
    ForecastFinancials,
    IntegratedForecast,
    ImpactAssumption,
    InputGap,
    MarginModel,
    PricingModel,
    RECONCILIATION_TOLERANCE,
    ReconciliationCheck,
    ReconciliationStatus,
    RevenueExposure,
    RevenueExposureSource,
    SensitivityItem,
    ShareCapitalVersion,
    UnitKind,
    convert_unit,
    utc_now,
)
from .interval import Interval
from .pit import filter_capitals_as_of, filter_inputs_as_of, parse_time


FORMULA_TEXT = (
    "新增需求 = 市场规模变化 × 单机用量或渗透率\n"
    "公司可兑现销量 = min(新增需求 × 公司份额, 有效产能 × 利用率 × 良率 × 爬坡进度)\n"
    "新增收入 = 公司可兑现销量 × ASP × (1 - 收入确认滞后)\n"
    "新增毛利 = 新增收入 × 增量毛利率\n"
    "新增净利润 = 新增毛利 - 增量费用 - 税费\n"
    "EPS影响 = 新增净利润 / point-in-time 总股本"
)

# 第二阶段占位符模块
_PLACEHOLDER_MODULES = [
    "minority_interest",
    "non_recurring_items",
    "historical_accounting_change",
]


@dataclass
class EarningsImpactRequest:
    """盈利测算请求。

    Attributes:
        driver: 盈利驱动。
        exposure: 收入敞口。
        demand: 需求模型。
        capacity: 产能模型。
        pricing: 定价模型。
        margin: 利润率模型。
        capital_versions: 股本版本列表（取 PIT 可见最新）。
        as_of: 测算时点（PIT）。
        market_share: 公司市场份额（ImpactAssumption，可选；缺失则阻断）。
        historical: 是否历史模式（True=严格 PIT）。
    """

    driver: EarningsDriver
    exposure: RevenueExposure | None = None
    demand: DemandModel | None = None
    capacity: CapacityModel | None = None
    pricing: PricingModel | None = None
    margin: MarginModel | None = None
    capital_versions: list[ShareCapitalVersion] = field(default_factory=list)
    as_of: str = ""
    market_share: ImpactAssumption | None = None
    historical: bool = True


class AssumptionBuilder:
    """从 Claim/Evidence 构造 ImpactAssumption。"""

    @staticmethod
    def from_claim(
        variable_name: str,
        value: Interval,
        unit: str,
        claim_id: str = "",
        evidence_id: str = "",
        source_type: str = AssumptionSourceType.DISCLOSED.value,
        **kwargs: Any,
    ) -> ImpactAssumption:
        """从 claim/evidence 创建假设。"""
        return ImpactAssumption(
            variable_name=variable_name,
            value=value,
            unit=unit,
            source_claim_id=claim_id,
            source_evidence_id=evidence_id,
            source_type=source_type,
            **kwargs,
        )

    @staticmethod
    def deduplicate(assumptions: list[ImpactAssumption]) -> list[ImpactAssumption]:
        """按 evidence_ids+variable_name 去重，保留 confidence 最高且 available_at 最新者。"""
        groups: dict[str, list[ImpactAssumption]] = {}
        for asm in assumptions:
            if not isinstance(asm, ImpactAssumption):
                continue
            evidence_id = str(asm.source_evidence_id or "").strip()
            key = f"{asm.variable_name}|{evidence_id}"
            groups.setdefault(key, []).append(asm)
        result: list[ImpactAssumption] = []
        for group in groups.values():
            sorted_group = sorted(
                group,
                key=lambda a: (a.confidence.mid, a.available_at or ""),
                reverse=True,
            )
            result.append(sorted_group[0])
        return result


class DimensionCurrencyValidator:
    """量纲和币种一致性校验。"""

    @staticmethod
    def validate(
        request: EarningsImpactRequest,
        exposure: RevenueExposure | None,
        gaps: list[InputGap],
    ) -> None:
        """拒绝无换算证据的币种混用和不兼容量纲。"""
        driver_id = request.driver.driver_id
        if exposure is not None and request.pricing is not None:
            base_currency = exposure.revenue_base.currency
            asp_currency = request.pricing.asp.currency
            if base_currency and asp_currency and base_currency != asp_currency:
                gaps.append(
                    InputGap(
                        driver_id=driver_id,
                        field_name="currency",
                        reason=f"收入基数币种 {base_currency} 与 ASP 币种 {asp_currency} 不一致，且无 FX 转换证据",
                        required_source_kind="fx_rate_with_as_of",
                        severity="blocker",
                    )
                )
        if request.demand is not None and request.capacity is not None:
            demand_unit = request.demand.per_unit_usage.unit
            capacity_input = (
                request.capacity.effective_capacity
                if request.capacity.effective_capacity.is_complete()
                else request.capacity.nameplate_capacity
            )
            if demand_unit != capacity_input.unit:
                converted, ok = convert_unit(
                    capacity_input.value,
                    capacity_input.unit,
                    demand_unit,
                )
                if not ok:
                    gaps.append(
                        InputGap(
                            driver_id=driver_id,
                            field_name="capacity.unit",
                            reason=f"需求量单位 {demand_unit} 与产能单位 {capacity_input.unit} 不兼容且无换算证据",
                            required_source_kind="unit_conversion",
                            severity="blocker",
                        )
                    )
        ratio_fields = []
        if request.market_share is not None:
            ratio_fields.append(("market_share.unit", request.market_share))
        if request.capacity is not None:
            ratio_fields.extend(
                [
                    ("utilization_rate.unit", request.capacity.utilization_rate),
                    ("yield_rate.unit", request.capacity.yield_rate),
                ]
            )
        if request.margin is not None:
            ratio_fields.extend(
                [
                    ("gross_margin.unit", request.margin.gross_margin),
                    ("expense_ratio.unit", request.margin.expense_ratio),
                    ("tax_rate.unit", request.margin.tax_rate),
                ]
            )
        for field_name, item in ratio_fields:
            if item.unit not in {UnitKind.RATIO.value, UnitKind.PERCENT.value}:
                gaps.append(
                    InputGap(
                        driver_id=driver_id,
                        field_name=field_name,
                        reason=f"{field_name} 必须是 ratio 或 percent",
                        severity="blocker",
                    )
                )


class CapacityConstraint:
    """产能约束：处理产能、利用率、良率、爬坡。"""

    @staticmethod
    def apply(
        demand_volume: Interval,
        capacity_model: CapacityModel,
        as_of: str,
        gaps: list[InputGap] | None = None,
    ) -> tuple[Interval, dict[str, Any]]:
        """返回公司可实现销量和 trace。"""
        trace: dict[str, Any] = {}
        capacity_input = (
            capacity_model.effective_capacity
            if capacity_model.effective_capacity.is_complete()
            else capacity_model.nameplate_capacity
        )
        eff_cap = capacity_input.value.clamp(floor=0.0)
        util = capacity_model.utilization_rate.value.clamp(floor=0.0, ceil=1.0)
        yield_rate = capacity_model.yield_rate.value.clamp(floor=0.0, ceil=1.0)
        ramp_progress = capacity_model.ramp_progress.clamp(floor=0.0, ceil=1.0)

        # 产能投产时间检查
        ramp_date = capacity_model.ramp_date
        if ramp_date and as_of:
            ramp_dt = parse_time(ramp_date)
            as_of_dt = parse_time(as_of)
            if ramp_dt is not None and as_of_dt is not None and ramp_dt > as_of_dt:
                eff_cap = Interval.point(0.0)
                trace["ramp_blocked"] = True
                if gaps is not None:
                    gaps.append(
                        InputGap(
                            driver_id=capacity_model.driver_id,
                            field_name="capacity.ramp_date",
                            reason=f"产能投产时间 {ramp_date} 晚于测算时点 {as_of}",
                            required_source_kind="company_disclosure",
                            severity="blocker",
                            suggested_task="确认产能实际投产时间或调整 as_of",
                        )
                    )

        capacity_volume = eff_cap * util * yield_rate * ramp_progress
        deliverable = Interval.minimum(demand_volume, capacity_volume)
        trace["capacity_volume"] = capacity_volume.to_dict()
        trace["utilization_rate"] = util.to_dict()
        trace["yield_rate"] = yield_rate.to_dict()
        trace["ramp_progress"] = ramp_progress.to_dict()
        return deliverable, trace


class DuplicateRevenueDetector:
    """多产品重复收入检测。"""

    @staticmethod
    def check(requests: list[EarningsImpactRequest]) -> list[InputGap]:
        """检测多个请求是否对同一公司-期间-收入基数重复计算。"""
        gaps: list[InputGap] = []
        seen: dict[str, list[str]] = {}
        for req in requests:
            if not isinstance(req, EarningsImpactRequest) or req.exposure is None:
                continue
            key = f"{req.driver.company_symbol}|{req.driver.period}|{req.exposure.revenue_base.as_of}"
            seen.setdefault(key, []).append(req.driver.product_name)
        for key, products in seen.items():
            if len(products) > 1:
                symbol, period, _ = key.split("|", 2)
                gaps.append(
                    InputGap(
                        driver_id="",
                        field_name="revenue_exposure.duplicate",
                        reason=f"公司 {symbol} 在 {period} 的多个产品 {products} 可能共享同一收入基数，存在重复计算风险",
                        required_source_kind="manual_audit",
                        severity="warning",
                        suggested_task="明确各产品收入基数边界或合并计算",
                    )
                )
        return gaps


class EarningsImpactBuilder:
    """组装 EarningsImpact。"""

    @staticmethod
    def build(
        request: EarningsImpactRequest,
        scenarios: list[EarningsScenario],
        sensitivity: list[SensitivityItem],
        gaps: list[InputGap],
        assumptions: list[ImpactAssumption],
        blocked_hypotheses: list[dict[str, Any]],
        as_of: str,
        data_version: str = "",
    ) -> EarningsImpact:
        """组装结果，包括最敏感变量、最大不确定性、证伪条件、占位符。"""
        driver = request.driver
        exposure = request.exposure
        currency = exposure.revenue_base.currency if exposure else "CNY"

        # 状态判定
        has_blocker = any(g.severity == "blocker" for g in gaps)
        if has_blocker or not scenarios:
            status = EarningsStatus.INSUFFICIENT_DATA.value
        else:
            status = EarningsStatus.OK.value
            if any(g.severity == "warning" for g in gaps):
                status = EarningsStatus.DEGRADED.value
            if _PLACEHOLDER_MODULES:
                status = EarningsStatus.PARTIAL.value

        # 综合结果取 base 情景
        base = next(
            (s for s in scenarios if s.kind == EarningsScenarioKind.BASELINE.value),
            None,
        )
        incremental_revenue = base.incremental_revenue if base else Interval.point(0.0)
        incremental_gross_profit = base.incremental_gross_profit if base else Interval.point(0.0)
        incremental_net_profit = base.incremental_net_profit if base else Interval.point(0.0)
        eps_impact = base.eps_impact if base else Interval.point(0.0)

        # 综合置信区间：取所有假设 confidence 的加权平均
        if assumptions:
            conf_values = [a.confidence.mid for a in assumptions if a.confidence]
            avg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.0
            confidence_interval = Interval.point(round(avg_conf, 4))
        else:
            confidence_interval = Interval.point(0.0)

        # 最敏感变量
        most_sensitive = ""
        if sensitivity:
            most_sensitive = max(sensitivity, key=lambda x: abs(x.elasticity)).input_name

        # 最大不确定性：区间宽度最大或置信度最低
        max_uncertainty = ""
        if assumptions:
            max_uncertainty = max(
                assumptions,
                key=lambda a: a.value.width / max(abs(a.value.mid), 1e-9) + (1 - a.confidence.mid),
            ).variable_name

        # 证伪条件
        falsification_conditions = EarningsImpactBuilder._falsification_conditions(
            request, scenarios, assumptions
        )

        # 证据溯源
        evidence_trace = [
            {
                "variable_name": a.variable_name,
                "assumption_id": a.assumption_id,
                "source_type": a.source_type,
                "source_claim_id": a.source_claim_id,
                "source_evidence_id": a.source_evidence_id,
                "value": a.value.to_dict(),
                "confidence": a.confidence.to_dict(),
            }
            for a in assumptions
        ]

        return EarningsImpact(
            driver_id=driver.driver_id,
            symbol=driver.company_symbol,
            company_name=driver.company_name,
            product=driver.product_name,
            period=driver.period,
            as_of=as_of,
            status=status,
            currency=currency,
            incremental_revenue=incremental_revenue,
            incremental_gross_profit=incremental_gross_profit,
            incremental_net_profit=incremental_net_profit,
            eps_impact=eps_impact,
            scenarios=scenarios,
            key_assumptions=assumptions,
            sensitivity=sensitivity,
            evidence_trace=evidence_trace,
            data_gaps=gaps,
            placeholders=_PLACEHOLDER_MODULES.copy(),
            falsification_conditions=falsification_conditions,
            confidence_interval=confidence_interval,
            formula=FORMULA_TEXT,
            base_inputs_snapshot=EarningsImpactEngine._snapshot_inputs(request),
            available_at=as_of,
            data_version=data_version,
        )

    @staticmethod
    def _falsification_conditions(
        request: EarningsImpactRequest,
        scenarios: list[EarningsScenario],
        assumptions: list[ImpactAssumption],
    ) -> list[str]:
        """生成关键证伪条件。"""
        conditions: list[str] = []
        if request.demand is not None:
            conditions.append(
                f"若市场规模变化低于 {request.demand.market_growth_delta_input.value.low}，"
                f"则新增需求预测不成立"
            )
        if request.capacity is not None:
            conditions.append(
                f"若产能投产时间晚于 {request.capacity.ramp_date} 或利用率低于 "
                f"{request.capacity.utilization_rate.value.low}，则销量预测不成立"
            )
        if request.pricing is not None:
            conditions.append(
                f"若 ASP 低于 {request.pricing.asp.value.low}，则收入预测不成立"
            )
        inferred = [a.variable_name for a in assumptions if a.is_inferred]
        if inferred:
            conditions.append(f"以下推断值需证据验证：{', '.join(inferred)}")
        return conditions


class EarningsImpactEngine:
    """盈利映射引擎。

    严格按规范实现真实计算，不做启发式打分。
    """

    # 变量对 EPS 的偏好方向：True 表示越大越好（bearish 取下界）
    _BENEFICIAL_DIRECTION: dict[str, bool] = {
        "market_size": True,
        "per_unit_usage": True,
        "market_growth_delta": True,
        "market_share": True,
        "effective_capacity": True,
        "utilization_rate": True,
        "yield_rate": True,
        "asp": True,
        "gross_margin": True,
        "realization_probability": True,
        "expense_ratio": False,
        "tax_rate": False,
        "unit_cost": False,
        "revenue_recognition_lag": False,
    }

    @classmethod
    def _scenario_point(cls, value: Interval, kind: str, variable_name: str = "") -> Interval:
        """由证据区间端点形成三情景，不再注入任意默认假设。"""
        beneficial = cls._BENEFICIAL_DIRECTION.get(variable_name, True)
        if kind == EarningsScenarioKind.BASELINE.value:
            return Interval.point(value.mid)
        elif kind == EarningsScenarioKind.BEARISH.value:
            selected = value.low if beneficial else value.high
        else:  # BULLISH
            selected = value.high if beneficial else value.low
        return Interval.point(selected)

    def compute(self, request: EarningsImpactRequest) -> EarningsImpact:
        """执行盈利测算，返回 EarningsImpact。

        Args:
            request: 盈利测算请求。

        Returns:
            EarningsImpact，包含三情景、敏感性矩阵和缺口。

        Raises:
            无；所有缺输入通过 insufficient_data + gaps 返回。
        """
        driver = request.driver
        gaps: list[InputGap] = []
        blocked_hypotheses: list[dict[str, Any]] = []

        # 0. historical=true 时缺 as_of 必须返回 insufficient_data + 审计 gap
        if request.historical and not request.as_of:
            gaps.append(
                InputGap(
                    driver_id=driver.driver_id,
                    field_name="as_of",
                    reason="historical=true 时必须提供 as_of 时点，禁止回退当前时间",
                    required_source_kind="point_in_time_marker",
                    severity="blocker",
                    suggested_task="补全测算时点 as_of（ISO 8601），确保只使用该时点已公开已获取的数据",
                )
            )
            return EarningsImpactBuilder.build(
                request, [], [], gaps, [], blocked_hypotheses, "",
                data_version="historical_missing_as_of",
            )
        as_of = request.as_of or utc_now()

        # 1. 硬阻断 valuation_bridge 启发式
        exposure = request.exposure
        if exposure is not None and not exposure.is_eligible_for_formal():
            blocked_hypotheses.append(
                {
                    "exposure_id": exposure.exposure_id,
                    "reason": "exploration_hypothesis 或来源类型不接受",
                    "source_kind": exposure.source_kind,
                    "is_exploration_hypothesis": exposure.is_exploration_hypothesis,
                }
            )
            exposure = None

        # 2. 收集所有假设
        assumptions = self._collect_assumptions(request, exposure)

        # 3. PIT 过滤
        if request.historical and as_of:
            visible_inputs, _ = filter_inputs_as_of(assumptions, as_of, historical=True)
            visible_capitals, _ = filter_capitals_as_of(
                request.capital_versions, as_of, historical=True
            )
        else:
            visible_inputs = assumptions
            visible_capitals = list(request.capital_versions)

        visible_ids = {a.assumption_id for a in visible_inputs}
        if request.historical:
            for asm in assumptions:
                if asm.assumption_id not in visible_ids:
                    gaps.append(
                        InputGap(
                            driver_id=driver.driver_id,
                            field_name=asm.variable_name or asm.assumption_id,
                            reason=f"{asm.variable_name or asm.assumption_id} 在 as_of 时点不可见",
                            required_source_kind=asm.source_kind,
                            severity="blocker",
                            suggested_task="使用 as_of 时点已公开且已获取的数据版本",
                        )
                    )

        # 4. 校验必要输入
        self._validate_inputs(request, exposure, gaps)
        DimensionCurrencyValidator.validate(request, exposure, gaps)

        # 5. 有 blocker 则返回
        if any(g.severity == "blocker" for g in gaps):
            return EarningsImpactBuilder.build(
                request, [], [], gaps, visible_inputs, blocked_hypotheses, as_of
            )

        # 6. 选取 PIT 可见最新股本
        capital = self._select_latest_capital(visible_capitals)
        if capital is None:
            gaps.append(
                InputGap(
                    driver_id=driver.driver_id,
                    field_name="shares_outstanding",
                    reason="无 PIT 可见的股本版本",
                    required_source_kind="company_disclosure",
                    severity="blocker",
                    suggested_task="补全公司财报披露的总股本数据",
                )
            )
            return EarningsImpactBuilder.build(
                request, [], [], gaps, visible_inputs, blocked_hypotheses, as_of
            )

        # 7. 计算三情景
        scenarios: list[EarningsScenario] = []
        for kind in [
            EarningsScenarioKind.BEARISH.value,
            EarningsScenarioKind.BASELINE.value,
            EarningsScenarioKind.BULLISH.value,
        ]:
            scenario = self._compute_scenario(request, exposure, capital, kind, as_of)
            scenarios.append(scenario)

        # 8. 敏感性矩阵
        sensitivity = self._compute_sensitivity(request, exposure, capital, as_of)

        # 9. 组装结果
        return EarningsImpactBuilder.build(
            request, scenarios, sensitivity, gaps, visible_inputs, blocked_hypotheses, as_of
        )

    def recompute(
        self,
        base_request: EarningsImpactRequest,
        changed_assumptions: list[ImpactAssumption],
    ) -> EarningsImpact:
        """基于变更的假设增量重算。

        Args:
            base_request: 原始请求。
            changed_assumptions: 变更后的假设列表（按 variable_name 匹配替换）。

        Returns:
            新的 EarningsImpact。
        """
        request = copy.deepcopy(base_request)
        changed_map = {a.variable_name: a for a in changed_assumptions}
        self._replace_assumption(request, changed_map)
        return self.compute(request)

    def _replace_assumption(
        self, request: EarningsImpactRequest, changed_map: dict[str, ImpactAssumption]
    ) -> None:
        """按 variable_name 替换 request 中的假设。"""
        if request.market_share is not None and request.market_share.variable_name in changed_map:
            request.market_share = changed_map[request.market_share.variable_name]
        if request.demand is not None:
            d = request.demand
            if d.market_size.variable_name in changed_map:
                d.market_size = changed_map[d.market_size.variable_name]
            if d.per_unit_usage.variable_name in changed_map:
                d.per_unit_usage = changed_map[d.per_unit_usage.variable_name]
            if d.market_growth_delta_input.variable_name in changed_map:
                d.market_growth_delta_input = changed_map[d.market_growth_delta_input.variable_name]
        if request.capacity is not None:
            c = request.capacity
            for attr in ["nameplate_capacity", "effective_capacity", "utilization_rate", "yield_rate"]:
                item = getattr(c, attr)
                if item.variable_name in changed_map:
                    setattr(c, attr, changed_map[item.variable_name])
        if request.pricing is not None:
            p = request.pricing
            for attr in ["asp", "price_pass_through", "unit_cost"]:
                item = getattr(p, attr)
                if item.variable_name in changed_map:
                    setattr(p, attr, changed_map[item.variable_name])
        if request.margin is not None:
            m = request.margin
            for attr in ["gross_margin", "expense_ratio", "tax_rate"]:
                item = getattr(m, attr)
                if item.variable_name in changed_map:
                    setattr(m, attr, changed_map[item.variable_name])

    def _collect_assumptions(
        self, request: EarningsImpactRequest, exposure: RevenueExposure | None
    ) -> list[ImpactAssumption]:
        """收集请求中所有可追溯的完整假设。

        未完整填写的可选字段（如 price_pass_through）不进入收集列表，
        避免在历史 PIT 过滤阶段被误拦截并产生伪 blocker。
        """
        assumptions: list[ImpactAssumption] = []
        if exposure is not None and exposure.revenue_base.is_complete():
            assumptions.append(exposure.revenue_base)
        if request.demand is not None:
            d = request.demand
            for item in [d.market_size, d.per_unit_usage, d.market_growth_delta_input]:
                if item.is_complete():
                    assumptions.append(item)
        if request.capacity is not None:
            c = request.capacity
            for item in [c.nameplate_capacity, c.effective_capacity, c.utilization_rate, c.yield_rate]:
                if item.is_complete():
                    assumptions.append(item)
        if request.pricing is not None:
            p = request.pricing
            for item in [p.asp, p.price_pass_through, p.unit_cost]:
                if item.is_complete():
                    assumptions.append(item)
        if request.margin is not None:
            m = request.margin
            for item in [m.gross_margin, m.expense_ratio, m.tax_rate]:
                if item.is_complete():
                    assumptions.append(item)
        if request.market_share is not None and request.market_share.is_complete():
            assumptions.append(request.market_share)
        return assumptions

    def _validate_inputs(
        self,
        request: EarningsImpactRequest,
        exposure: RevenueExposure | None,
        gaps: list[InputGap],
    ) -> None:
        """校验必要输入完整性。"""
        driver = request.driver

        def _add_gap(
            field_name: str,
            reason: str,
            required_kind: str = "",
            severity: str = "blocker",
            task: str = "",
        ) -> None:
            gaps.append(
                InputGap(
                    driver_id=driver.driver_id,
                    field_name=field_name,
                    reason=reason,
                    required_source_kind=required_kind,
                    severity=severity,
                    suggested_task=task,
                )
            )

        if exposure is None:
            _add_gap(
                "revenue_exposure",
                "缺少收入敞口或被阻断为探索假设",
                required_kind="financial_segment/company_disclosure/manual_audit",
                task="补全财报分部或公司披露的收入占比数据",
            )
        else:
            if exposure.exposure_ratio.mid <= 0:
                _add_gap(
                    "exposure_ratio",
                    "收入敞口比例缺失或为零",
                    required_kind="financial_segment",
                )
            if not exposure.revenue_base.is_complete():
                self._check_input_completeness(
                    exposure.revenue_base, "revenue_base", gaps, driver.driver_id
                )

        if request.demand is None:
            _add_gap("demand_model", "缺少需求模型")
        else:
            d = request.demand
            for attr in ["market_size", "per_unit_usage", "market_growth_delta_input"]:
                item = getattr(d, attr)
                if not item.is_complete():
                    self._check_input_completeness(item, attr, gaps, driver.driver_id)
            if d.market_growth_delta_kind not in {"rate", "absolute"}:
                _add_gap(
                    "market_growth_delta_kind",
                    "市场变化语义必须显式声明 rate 或 absolute",
                )

        if request.capacity is None:
            _add_gap("capacity_model", "缺少产能模型")
        else:
            c = request.capacity
            if not c.effective_capacity.is_complete() and not c.nameplate_capacity.is_complete():
                _add_gap(
                    "effective_capacity",
                    "缺少有效产能或名义产能数据",
                    required_kind="company_disclosure",
                    task="补全公司产能披露",
                )
            for attr in ["utilization_rate", "yield_rate"]:
                item = getattr(c, attr)
                if not item.is_complete():
                    self._check_input_completeness(item, attr, gaps, driver.driver_id)

        if request.pricing is None:
            _add_gap("pricing_model", "缺少定价模型")
        else:
            p = request.pricing
            if not p.asp.is_complete():
                self._check_input_completeness(p.asp, "asp", gaps, driver.driver_id)

        if request.margin is None:
            _add_gap("margin_model", "缺少利润率模型")
        else:
            m = request.margin
            for attr in ["gross_margin", "expense_ratio", "tax_rate"]:
                item = getattr(m, attr)
                if not item.is_complete():
                    self._check_input_completeness(item, attr, gaps, driver.driver_id)

        if request.market_share is None:
            _add_gap(
                "market_share",
                "缺少公司市场份额，收入敞口比例不能替代市场份额",
            )
        elif not request.market_share.is_complete():
            self._check_input_completeness(
                request.market_share, "market_share", gaps, driver.driver_id
            )

        if not request.capital_versions:
            _add_gap(
                "shares_outstanding",
                "缺少股本版本数据",
                required_kind="company_disclosure",
                task="补全公司财报披露的总股本",
            )

    def _check_input_completeness(
        self,
        item: ImpactAssumption,
        field_name: str,
        gaps: list[InputGap],
        driver_id: str,
        severity: str = "blocker",
    ) -> None:
        """检查单项输入完整性，补充缺口。"""
        if not item.has_source():
            gaps.append(
                InputGap(
                    driver_id=driver_id,
                    field_name=field_name,
                    reason=f"{field_name} 缺少来源 Claim/Evidence",
                    required_source_kind="financial_segment/company_disclosure",
                    severity=severity,
                    suggested_task=f"为 {field_name} 补充可追溯的证据来源",
                )
            )
        if item.unit == UnitKind.UNKNOWN.value:
            gaps.append(
                InputGap(
                    driver_id=driver_id,
                    field_name=f"{field_name}.unit",
                    reason=f"{field_name} 缺少单位",
                    severity="warning",
                )
            )
        if not item.period:
            gaps.append(
                InputGap(
                    driver_id=driver_id,
                    field_name=f"{field_name}.period",
                    reason=f"{field_name} 缺少适用期间",
                    severity="warning",
                )
            )
        if not item.as_of:
            gaps.append(
                InputGap(
                    driver_id=driver_id,
                    field_name=f"{field_name}.as_of",
                    reason=f"{field_name} 缺少数据生效时点",
                    severity="warning",
                )
            )

    def _select_latest_capital(
        self, capitals: list[ShareCapitalVersion]
    ) -> ShareCapitalVersion | None:
        """选取最新（as_of 最大）的股本版本。"""
        if not capitals:
            return None
        sorted_capitals = sorted(
            capitals, key=lambda c: c.as_of or c.published_at or c.retrieved_at
        )
        return sorted_capitals[-1]

    def _compute_scenario(
        self,
        request: EarningsImpactRequest,
        exposure: RevenueExposure | None,
        capital: ShareCapitalVersion,
        kind: str,
        as_of: str,
    ) -> EarningsScenario:
        """计算单情景结果。"""
        trace: dict[str, Any] = {}
        scenario = EarningsScenario(kind=kind)

        demand = request.demand
        if demand is None:
            return scenario

        # 新增需求 = 市场规模变化 × 单机用量
        market_delta = self._scenario_point(
            demand.market_growth_delta_input.value, kind, "market_growth_delta"
        )
        per_unit = self._scenario_point(demand.per_unit_usage.value, kind, "per_unit_usage")
        market_size = self._scenario_point(demand.market_size.value, kind, "market_size")

        if demand.market_growth_delta_kind == "rate":
            new_demand = market_size * market_delta * per_unit
        elif demand.market_growth_delta_kind == "absolute":
            new_demand = market_delta * per_unit
        else:
            return scenario
        new_demand = new_demand.clamp(floor=0.0)
        trace["new_demand_formula"] = "市场规模变化 × 单机用量"
        trace["new_demand"] = new_demand.to_dict()
        scenario.demand_growth = market_delta

        # 公司份额
        if request.market_share is not None and request.market_share.is_complete():
            share = self._scenario_point(request.market_share.value, kind, "market_share")
            share = share.clamp(floor=0.0, ceil=1.0)
            scenario.company_share = share
            trace["market_share"] = share.to_dict()
        else:
            share = Interval.point(0.0)

        demand_volume = new_demand * share

        # 产能约束
        capacity = request.capacity
        if capacity is not None:
            deliverable, cap_trace = CapacityConstraint.apply(
                demand_volume, capacity, as_of, None
            )
            trace.update(cap_trace)
            scenario.capacity = cap_trace["capacity_volume"]
            scenario.utilization = cap_trace["utilization_rate"]
            scenario.yield_rate = cap_trace["yield_rate"]
        else:
            deliverable = demand_volume
            trace["capacity_model_missing"] = True

        deliverable = deliverable.clamp(floor=0.0)
        trace["deliverable_volume"] = deliverable.to_dict()
        trace["deliverable_formula"] = "min(新增需求 × 公司份额, 有效产能 × 利用率 × 良率 × 爬坡进度)"
        scenario.incremental_revenue = Interval.point(0.0)

        # 新增收入
        pricing = request.pricing
        if pricing is not None:
            asp = self._scenario_point(pricing.asp.value, kind, "asp").clamp(floor=0.0)
            scenario.asp = asp
            lag = pricing.revenue_recognition_lag.clamp(floor=0.0, ceil=1.0)
            recognized_ratio = Interval.point(1.0) - lag
            new_revenue = deliverable * asp * recognized_ratio
            trace["asp"] = asp.to_dict()
            trace["revenue_recognition_lag"] = lag.to_dict()
            trace["recognized_ratio"] = recognized_ratio.to_dict()
        else:
            new_revenue = Interval.point(0.0)
            trace["asp"] = "pricing_model_missing"
        new_revenue = new_revenue.clamp(floor=0.0)
        trace["new_revenue"] = new_revenue.to_dict()
        trace["new_revenue_formula"] = "可兑现销量 × ASP × (1 - 收入确认滞后)"
        scenario.incremental_revenue = new_revenue

        # 新增毛利与净利润
        margin = request.margin
        if margin is not None:
            gross = self._scenario_point(margin.gross_margin.value, kind, "gross_margin").clamp(
                floor=0.0, ceil=1.0
            )
            new_gross_profit = new_revenue * gross
            trace["gross_margin"] = gross.to_dict()
            scenario.gross_margin = gross

            expense = self._scenario_point(
                margin.expense_ratio.value, kind, "expense_ratio"
            ).clamp(floor=0.0, ceil=1.0)
            tax = self._scenario_point(margin.tax_rate.value, kind, "tax_rate").clamp(
                floor=0.0, ceil=1.0
            )

            new_expense = new_revenue * expense
            pre_tax_profit = new_gross_profit - new_expense
            tax_amount = Interval.point(0.0)
            if pre_tax_profit.low > 0:
                tax_amount = pre_tax_profit * tax
            elif pre_tax_profit.high > 0:
                # 区间跨零：保守只对正部分征税，这里简化为 0
                tax_amount = Interval.point(0.0)
            new_net_profit = pre_tax_profit - tax_amount
            new_net_profit = new_net_profit.clamp(floor=None)

            scenario.operating_expense = expense
            scenario.tax_rate = tax
            trace["expense_ratio"] = expense.to_dict()
            trace["tax_rate"] = tax.to_dict()
            trace["pre_tax_profit"] = pre_tax_profit.to_dict()
            trace["new_net_profit"] = new_net_profit.to_dict()
            trace["new_net_profit_formula"] = "新增毛利 - 增量费用 - 税费"
        else:
            new_gross_profit = Interval.point(0.0)
            new_net_profit = Interval.point(0.0)
            trace["margin_model_missing"] = True

        scenario.incremental_gross_profit = new_gross_profit
        scenario.incremental_net_profit = new_net_profit
        trace["new_gross_profit"] = new_gross_profit.to_dict()
        trace["new_gross_profit_formula"] = "新增收入 × 增量毛利率"

        # EPS
        shares = capital.shares_outstanding.value
        if shares.low > 0 or shares.high > 0:
            shares_int = (
                Interval.point(math.floor(shares.low))
                if shares.is_point
                else Interval(math.floor(shares.low), math.floor(shares.high))
            )
            if shares_int.low > 0:
                eps_contribution = new_net_profit / shares_int
            else:
                eps_contribution = Interval.point(0.0)
                trace["eps_warning"] = "股本为零或无效"
        else:
            eps_contribution = Interval.point(0.0)
            trace["eps_warning"] = "股本数据缺失"
        trace["shares_outstanding"] = shares.to_dict()
        trace["eps_contribution"] = eps_contribution.to_dict()
        trace["eps_formula"] = "新增净利润 / point-in-time 总股本"
        scenario.eps_impact = eps_contribution

        scenario.formula_trace = trace
        scenario.key_assumptions = [a.assumption_id for a in self._collect_assumptions(request, exposure)]
        return scenario

    def _compute_sensitivity(
        self,
        request: EarningsImpactRequest,
        exposure: RevenueExposure | None,
        capital: ShareCapitalVersion,
        as_of: str,
    ) -> list[SensitivityItem]:
        """逐项真实重算 ±10% 扰动后的 EPS，不复用基准区间伪装敏感性。"""
        base_eps = self._compute_scenario(
            request, exposure, capital, EarningsScenarioKind.BASELINE.value, as_of
        ).eps_impact.mid
        if base_eps == 0:
            return []

        paths: list[tuple[str, str, str]] = [
            ("market_size", "demand", "market_size"),
            ("per_unit_usage", "demand", "per_unit_usage"),
            ("market_growth_delta", "demand", "market_growth_delta_input"),
            ("market_share", "request", "market_share"),
            ("effective_capacity", "capacity", "effective_capacity"),
            ("utilization_rate", "capacity", "utilization_rate"),
            ("yield_rate", "capacity", "yield_rate"),
            ("asp", "pricing", "asp"),
            ("gross_margin", "margin", "gross_margin"),
            ("expense_ratio", "margin", "expense_ratio"),
            ("tax_rate", "margin", "tax_rate"),
        ]
        result: list[SensitivityItem] = []
        perturbation = 0.10
        for name, owner_name, attr_name in paths:
            probe = copy.deepcopy(request)
            owner = probe if owner_name == "request" else getattr(probe, owner_name, None)
            item = getattr(owner, attr_name, None) if owner is not None else None
            if not isinstance(item, ImpactAssumption) or not item.is_complete() or item.value.mid == 0:
                continue
            base_value = item.value.mid
            low_value = base_value * (1.0 - perturbation)
            high_value = base_value * (1.0 + perturbation)
            item.value = Interval.point(low_value)
            eps_low = self._compute_scenario(
                probe, exposure, capital, EarningsScenarioKind.BASELINE.value, as_of
            ).eps_impact.mid
            item.value = Interval.point(high_value)
            eps_high = self._compute_scenario(
                probe, exposure, capital, EarningsScenarioKind.BASELINE.value, as_of
            ).eps_impact.mid
            elasticity = ((eps_high - eps_low) / base_eps) / (2.0 * perturbation)
            result.append(
                SensitivityItem(
                    input_name=name,
                    base_value=round(base_value, 6),
                    low_value=round(low_value, 6),
                    high_value=round(high_value, 6),
                    eps_impact_low=round(eps_low, 6),
                    eps_impact_high=round(eps_high, 6),
                    elasticity=round(elasticity, 6),
                )
            )
        result.sort(key=lambda item: abs(item.elasticity), reverse=True)
        return result

    @staticmethod
    def _snapshot_inputs(request: EarningsImpactRequest) -> dict[str, Any]:
        """生成基准输入快照，用于追溯。"""
        snapshot: dict[str, Any] = {
            "driver_id": request.driver.driver_id,
            "as_of": request.as_of,
            "historical": request.historical,
        }
        if request.exposure is not None:
            snapshot["exposure"] = request.exposure.to_dict()
        if request.demand is not None:
            snapshot["demand"] = request.demand.to_dict()
        if request.capacity is not None:
            snapshot["capacity"] = request.capacity.to_dict()
        if request.pricing is not None:
            snapshot["pricing"] = request.pricing.to_dict()
        if request.margin is not None:
            snapshot["margin"] = request.margin.to_dict()
        if request.market_share is not None:
            snapshot["market_share"] = request.market_share.to_dict()
        if request.capital_versions:
            snapshot["capital_versions"] = [c.to_dict() for c in request.capital_versions]
        return snapshot


# ============================================================================
# P1.4：三表联动盈利预测引擎
# ============================================================================

# 三表勾稽检查名
CHECK_BALANCE_IDENTITY = "balance_sheet_identity"
CHECK_CASH_RECONCILIATION = "cash_flow_reconciliation"
CHECK_RETAINED_EARNINGS = "retained_earnings_linkage"
CHECK_DIVIDEND_PAIDOUT = "dividend_payout_linkage"

# ForecastBuildRequest 接受的 balance/cashflow 假设 variable_name 列表
BALANCE_INPUT_VARIABLES = (
    "dso_days",                # 应收周转天数
    "dio_days",                # 存货周转天数
    "dpo_days",                # 应付周转天数
    "capex_ratio",             # 资本开支 / 收入
    "depreciation_ratio",      # 折旧 / 期初固定资产
    "interest_expense",        # 利息费用
    "financial_expense",      # 财务费用（替代 interest_expense 时用）
    "tax_rate",                # 实际税率
    "minority_ratio",         # 少数股东损益 / 净利润
    "dividend_payout_ratio",  # 分红率
    "share_change",            # 股本变动（股数）
    "debt_change",            # 有息负债变动
)


@dataclass
class ForecastBuildRequest:
    """三表联动预测构建请求。

    Attributes:
        symbol: 公司代码。
        as_of: 测算时点（PIT）。
        period: 预测期间（如 2026Q1 / 2026FY）。
        horizon: 周期粒度（quarterly / annual）。
        scenario: 情景类型（bearish / baseline / bullish）。
        segment_impacts: 各产品/分部的 EarningsImpact 列表（含单 driver 增量）。
        balance_inputs: 资产负债表与现金流量表假设（DSO/DIO/capex_ratio 等）。
        opening_balance: 期初资产负债表 key-value，key 为 financials 字段名。
        share_capital: PIT 可见的股本版本。
        capital_versions: 全部股本版本（用于 PIT 过滤）。
        historical: 是否历史模式（True=严格 PIT）。
        model_version: 模型版本号。
        forecast_version: 预测版本号（递增）。
        currency: 币种。
        unit: 金额单位。
    """

    symbol: str
    as_of: str
    period: str
    horizon: str = "annual"
    scenario: str = "baseline"
    segment_impacts: list[EarningsImpact] = field(default_factory=list)
    balance_inputs: dict[str, ImpactAssumption] = field(default_factory=dict)
    opening_balance: dict[str, float] = field(default_factory=dict)
    share_capital: ShareCapitalVersion | None = None
    capital_versions: list[ShareCapitalVersion] = field(default_factory=list)
    historical: bool = True
    model_version: str = "integrated_forecast_v1"
    forecast_version: int = 1
    currency: str = "CNY"
    unit: str = "yuan"


class IntegratedForecastEngine:
    """三表联动盈利预测引擎。

    实施步骤：
    1. 从产品/分部收入推导公司收入（Σ segment_impacts.incremental_revenue）。
    2. 从收入和成本推导毛利、营业利润、税前利润和归母利润。
    3. 将营运资本、资本开支、折旧、融资和股本变化接入资产负债表与现金流量表。
    4. 输出季度、年度以及压力/基准/乐观研究情景。
    5. 所有情景保存完整输入快照和公式轨迹。
    6. 预测不得覆盖历史预测版本（由 repository 强制）。

    硬门禁：
    - 缺输入返回 insufficient_data + gaps，不产生伪精确。
    - 现金不凭空产生：closing_cash = opening_cash + OCF + ICF + FCF。
    - 产能约束销量：超出产能部分被截断。
    - 股本变化影响 EPS：使用 PIT 可见股本计算。
    - 情景单调性只在适用变量上成立（按 beneficial direction）。
    - 重复收入阻断：使用 DuplicateRevenueDetector 检测。
    - PIT 防泄漏：使用 filter_inputs_as_of 过滤所有输入。
    - 相同输入幂等、不同版本并存：input_snapshot 哈希一致时返回相同 forecast_id。
    """

    # 利润表字段：受 scenario 偏好方向影响
    # True=正向（越大越好）；False=反向（越小越好）
    _BENEFICIAL_VARS: dict[str, bool] = {
        "revenue": True,
        "gross_profit": True,
        "operating_profit": True,
        "net_profit": True,
        "capex_ratio": False,
        "depreciation_ratio": False,
        "interest_expense": False,
        "financial_expense": False,
        "tax_rate": False,
        "dso_days": False,
        "dio_days": False,
        "dpo_days": True,
        "dividend_payout_ratio": False,
        "minority_ratio": False,
    }

    @classmethod
    def build_forecast(cls, request: ForecastBuildRequest) -> IntegratedForecast:
        """构建三表联动预测。

        Args:
            request: 预测构建请求。

        Returns:
            IntegratedForecast 实例，缺输入时返回带 gaps 的结果。
        """
        gaps: list[InputGap] = []
        trace: dict[str, Any] = {}
        assumptions: list[ImpactAssumption] = []

        # 0. historical=true 时缺 as_of 阻断
        if request.historical and not request.as_of:
            gaps.append(InputGap(
                field_name="as_of",
                reason="historical=true 时必须提供 as_of 时点，禁止回退当前时间",
                required_source_kind="point_in_time_marker",
                severity="blocker",
                suggested_task="补全测算时点 as_of（ISO 8601）",
            ))
            return IntegratedForecast(
                symbol=request.symbol,
                as_of="",
                period=request.period,
                horizon=request.horizon,
                scenario=request.scenario,
                data_gaps=gaps,
                data_version="historical_missing_as_of",
            )
        as_of = request.as_of or utc_now()

        # 1. 收集并 PIT 过滤所有假设
        all_inputs: list[ImpactAssumption] = []
        for var_name, asm in request.balance_inputs.items():
            if asm.variable_name == "":
                asm.variable_name = var_name
            all_inputs.append(asm)
        if request.share_capital is not None:
            all_inputs.append(request.share_capital.shares_outstanding)

        if request.historical and as_of:
            visible_inputs, blocked_count = filter_inputs_as_of(all_inputs, as_of, historical=True)
            visible_capitals, _ = filter_capitals_as_of(
                request.capital_versions or ([request.share_capital] if request.share_capital else []),
                as_of,
                historical=True,
            )
            # filter_inputs_as_of 只返回 blocked 数量；这里重建被拦截的假设列表用于 gap 定位
            visible_ids = {asm.assumption_id for asm in visible_inputs}
            for asm in all_inputs:
                if asm.assumption_id not in visible_ids:
                    gaps.append(InputGap(
                        field_name=asm.variable_name or asm.assumption_id,
                        reason=f"{asm.variable_name} 在 as_of 时点不可见",
                        required_source_kind=asm.source_kind or "company_disclosure",
                        severity="blocker",
                        suggested_task="使用 as_of 时点已公开且已获取的数据版本",
                    ))
        else:
            visible_inputs = all_inputs
            visible_capitals = list(request.capital_versions) or (
                [request.share_capital] if request.share_capital else []
            )

        # 构建 variable_name -> ImpactAssumption 映射
        balance_inputs_visible: dict[str, ImpactAssumption] = {
            asm.variable_name: asm for asm in visible_inputs if asm.variable_name
        }
        assumptions = list(visible_inputs)
        trace["visible_assumptions"] = [a.assumption_id for a in assumptions]

        # 2. 从 segment_impacts 汇总公司收入
        segment_summary, dup_gaps = cls._aggregate_segment_revenue(
            request.segment_impacts, request.symbol, request.period,
        )
        gaps.extend(dup_gaps)
        trace["segment_summary"] = segment_summary

        # 3. 检查必备输入
        required_balance_inputs = ("dso_days", "dio_days", "capex_ratio", "depreciation_ratio", "tax_rate")
        for var_name in required_balance_inputs:
            if var_name not in balance_inputs_visible or not balance_inputs_visible[var_name].is_complete():
                gaps.append(InputGap(
                    field_name=var_name,
                    reason=f"缺少资产负债表/现金流量表假设：{var_name}",
                    required_source_kind="company_disclosure",
                    severity="blocker",
                    suggested_task=f"补全 {var_name} 假设（带来源与 PIT 时点）",
                ))

        # 4. 选取 PIT 可见最新股本
        capital = cls._select_latest_capital(visible_capitals)
        if capital is None or not capital.shares_outstanding.is_complete():
            gaps.append(InputGap(
                field_name="shares_outstanding",
                reason="无 PIT 可见的股本版本或股本数据不完整",
                required_source_kind="company_disclosure",
                severity="blocker",
                suggested_task="补全公司财报披露的总股本",
            ))
            shares_outstanding_value = 0.0
        else:
            shares_outstanding_value = max(capital.shares_outstanding.value.mid, 0.0)

        # 5. 有 blocker 直接返回
        if any(g.severity == "blocker" for g in gaps):
            return cls._assemble_forecast(
                request=request,
                as_of=as_of,
                assumptions=assumptions,
                gaps=gaps,
                trace=trace,
                segment_summary=segment_summary,
                shares_outstanding=shares_outstanding_value,
            )

        # 6. 选取情景对应的端点值
        scenario = request.scenario
        revenue = sum(item["revenue"] for item in segment_summary)
        gross_profit = sum(item["gross_profit"] for item in segment_summary)

        # 7. 推导利润表
        income_stmt = cls._build_income_statement(
            revenue=revenue,
            gross_profit=gross_profit,
            balance_inputs=balance_inputs_visible,
            scenario=scenario,
            trace=trace,
        )

        # 8. 推导资产负债表
        balance_sheet = cls._build_balance_sheet(
            income_stmt=income_stmt,
            opening_balance=request.opening_balance,
            balance_inputs=balance_inputs_visible,
            scenario=scenario,
            trace=trace,
            shares_outstanding=shares_outstanding_value,
        )

        # 9. 推导现金流量表
        cash_flow = cls._build_cash_flow(
            income_stmt=income_stmt,
            balance_sheet=balance_sheet,
            opening_balance=request.opening_balance,
            balance_inputs=balance_inputs_visible,
            scenario=scenario,
            trace=trace,
        )

        # 10. 组装 ForecastFinancials
        financials = cls._assemble_financials(
            period=request.period,
            currency=request.currency,
            unit=request.unit,
            income_stmt=income_stmt,
            balance_sheet=balance_sheet,
            cash_flow=cash_flow,
            shares_outstanding=shares_outstanding_value,
        )

        # 11. 三表勾稽检查（传入 dividend 用于留存收益勾稽）
        reconciliation = cls._reconcile(
            financials=financials,
            opening_balance=request.opening_balance,
            dividend=balance_sheet["dividend"],
        )

        # 12. 计算预测不确定性（用 assumption confidence 加权）
        if assumptions:
            conf_values = [a.confidence.mid for a in assumptions if a.confidence]
            avg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.0
            uncertainty = Interval.point(round(1.0 - avg_conf, 4))
        else:
            uncertainty = Interval.point(0.0)

        return cls._assemble_forecast(
            request=request,
            as_of=as_of,
            assumptions=assumptions,
            gaps=gaps,
            trace=trace,
            segment_summary=segment_summary,
            shares_outstanding=shares_outstanding_value,
            financials=financials,
            reconciliation=reconciliation,
            uncertainty=uncertainty,
        )

    # ---- 步骤 1：从产品/分部收入汇总公司收入 ----

    @classmethod
    def _aggregate_segment_revenue(
        cls,
        segment_impacts: list[EarningsImpact],
        symbol: str,
        period: str,
    ) -> tuple[list[dict[str, Any]], list[InputGap]]:
        """汇总各 segment 的收入、毛利到公司级，并检测重复收入基数。

        重复收入检测：使用 DuplicateRevenueDetector 检测多个 segment 是否对同一公司-期间-收入基数重复计算。
        但由于 segment_impacts 是已计算完成的 EarningsImpact，这里改为按 product_name 检测：
        若同一产品在多份 impact 中出现且共享同一 revenue_base（as_of），标记为重复风险。
        """
        gaps: list[InputGap] = []
        summary: list[dict[str, Any]] = []
        seen_keys: dict[str, list[str]] = {}
        for impact in segment_impacts:
            if impact.status not in {EarningsStatus.OK.value, EarningsStatus.DEGRADED.value, EarningsStatus.PARTIAL.value}:
                continue
            # 取基准情景
            base = next((s for s in impact.scenarios if s.kind == EarningsScenarioKind.BASELINE.value), None)
            if base is None:
                continue
            revenue = base.incremental_revenue.mid
            gross_profit = base.incremental_gross_profit.mid
            net_profit = base.incremental_net_profit.mid
            entry = {
                "product": impact.product,
                "segment": impact.segment,
                "period": impact.period,
                "revenue": round(revenue, 4),
                "gross_profit": round(gross_profit, 4),
                "net_profit": round(net_profit, 4),
                "impact_id": impact.impact_id,
                "driver_id": impact.driver_id,
            }
            summary.append(entry)
            # 重复收入检测：按 (symbol, period, product) 去重；同一 (symbol, period) 不同 product 是允许的
            dup_key = f"{symbol}|{period}|{impact.product}|{impact.driver_id}"
            seen_keys.setdefault(dup_key, []).append(impact.impact_id)
        # 同一产品同一期间在多个 impact 中出现 -> 重复收入风险
        for key, ids in seen_keys.items():
            if len(ids) > 1:
                gaps.append(InputGap(
                    field_name="segment_revenue.duplicate",
                    reason=f"公司 {symbol} 在 {period} 的产品在多个 impact 中重复计算: impact_ids={ids}",
                    required_source_kind="manual_audit",
                    severity="warning",
                    suggested_task="明确各产品收入边界或合并计算",
                ))
        return summary, gaps

    # ---- 步骤 2：构建利润表 ----

    @classmethod
    def _build_income_statement(
        cls,
        revenue: float,
        gross_profit: float,
        balance_inputs: dict[str, ImpactAssumption],
        scenario: str,
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        """推导利润表：revenue / operating_cost / gross_profit / operating_profit / pre_tax / net_profit / net_profit_parent。"""
        operating_cost = max(revenue - gross_profit, 0.0)

        # 营业利润 = 毛利 - 财务费用（简化：把财务费用作为期间费用代表）
        financial_expense = cls._scenario_value(balance_inputs.get("financial_expense"), scenario, default=0.0)
        if financial_expense == 0.0:
            financial_expense = cls._scenario_value(balance_inputs.get("interest_expense"), scenario, default=0.0)
        operating_profit = gross_profit - financial_expense

        # 利润总额 = 营业利润（不考虑营业外收支简化）
        pre_tax_profit = operating_profit
        tax_rate = cls._scenario_value(balance_inputs.get("tax_rate"), scenario, default=0.25)
        tax_rate = max(0.0, min(1.0, tax_rate))
        income_tax = max(pre_tax_profit, 0.0) * tax_rate
        net_profit = pre_tax_profit - income_tax

        # 归母利润 = 净利润 - 少数股东损益
        minority_ratio = cls._scenario_value(balance_inputs.get("minority_ratio"), scenario, default=0.0)
        minority_ratio = max(0.0, min(1.0, minority_ratio))
        minority_interest = net_profit * minority_ratio
        net_profit_parent = net_profit - minority_interest

        trace["income_statement"] = {
            "revenue": revenue,
            "operating_cost": operating_cost,
            "gross_profit": gross_profit,
            "financial_expense": financial_expense,
            "operating_profit": operating_profit,
            "pre_tax_profit": pre_tax_profit,
            "tax_rate": tax_rate,
            "income_tax": income_tax,
            "net_profit": net_profit,
            "minority_ratio": minority_ratio,
            "minority_interest": minority_interest,
            "net_profit_parent": net_profit_parent,
        }
        return {
            "revenue": revenue,
            "operating_cost": operating_cost,
            "gross_profit": gross_profit,
            "financial_expense": financial_expense,
            "operating_profit": operating_profit,
            "pre_tax_profit": pre_tax_profit,
            "income_tax": income_tax,
            "net_profit": net_profit,
            "minority_interest": minority_interest,
            "net_profit_parent": net_profit_parent,
        }

    # ---- 步骤 3：构建资产负债表 ----

    @classmethod
    def _build_balance_sheet(
        cls,
        income_stmt: dict[str, Any],
        opening_balance: dict[str, float],
        balance_inputs: dict[str, ImpactAssumption],
        scenario: str,
        trace: dict[str, Any],
        shares_outstanding: float,
    ) -> dict[str, Any]:
        """推导资产负债表：cash / AR / inventory / fixed_assets / debt / equity。"""
        revenue = income_stmt["revenue"]
        operating_cost = income_stmt["operating_cost"]
        net_profit_parent = income_stmt["net_profit_parent"]
        capex_ratio = cls._scenario_value(balance_inputs.get("capex_ratio"), scenario, default=0.0)
        capex = revenue * capex_ratio

        # 营运资本：DSO / DIO / DPO
        dso_days = cls._scenario_value(balance_inputs.get("dso_days"), scenario, default=0.0)
        dio_days = cls._scenario_value(balance_inputs.get("dio_days"), scenario, default=0.0)
        # AR = revenue × DSO / 365；Inventory = operating_cost × DIO / 365
        ar = revenue * dso_days / 365.0 if dso_days > 0 else opening_balance.get("accounts_receivable", 0.0)
        inventory = operating_cost * dio_days / 365.0 if dio_days > 0 else opening_balance.get("inventory", 0.0)

        # 折旧 = 期初固定资产 × depreciation_ratio
        opening_fixed = opening_balance.get("fixed_assets", 0.0)
        depreciation_ratio = cls._scenario_value(balance_inputs.get("depreciation_ratio"), scenario, default=0.0)
        depreciation = opening_fixed * depreciation_ratio
        fixed_assets = opening_fixed + capex - depreciation

        # 有息负债变动
        debt_change = cls._scenario_value(balance_inputs.get("debt_change"), scenario, default=0.0)
        opening_short_debt = opening_balance.get("short_term_debt", 0.0)
        opening_long_debt = opening_balance.get("long_term_debt", 0.0)
        short_term_debt = max(opening_short_debt + debt_change, 0.0)
        long_term_debt = opening_long_debt

        # 期末权益 = 期初权益 + 净利润（含少数股东损益） - 归母分红
        # 注：total_equity 包含归母权益和少数股东权益，故用 net_profit 而非 net_profit_parent
        opening_equity = opening_balance.get("total_equity", 0.0)
        opening_equity_parent = opening_balance.get("equity_parent", opening_equity)
        dividend_payout_ratio = cls._scenario_value(balance_inputs.get("dividend_payout_ratio"), scenario, default=0.0)
        dividend_payout_ratio = max(0.0, min(1.0, dividend_payout_ratio))
        dividend = net_profit_parent * dividend_payout_ratio
        total_equity = opening_equity + income_stmt["net_profit"] - dividend
        equity_parent = opening_equity_parent + net_profit_parent - dividend

        # 负债 = 短期借款 + 长期借款 + 其他负债（简化）
        opening_other_liabilities = max(
            opening_balance.get("total_liabilities", 0.0) - opening_short_debt - opening_long_debt,
            0.0,
        )
        total_liabilities = short_term_debt + long_term_debt + opening_other_liabilities

        # 现金将在 cash_flow 步骤中确定；这里先用期初+利润-分红-AR变动-存货变动+负债变动近似
        opening_cash = opening_balance.get("cash", 0.0)
        # 期末现金先用占位，由 cash_flow 计算后填回
        cash = opening_cash  # placeholder

        # 总资产：cash + AR + inventory + fixed_assets（简化模型，不含其他资产）
        # 这里采用「资产 = 负债 + 权益」的勾稽方式倒挤其他资产
        # 但需保证不凭空产生现金，所以 cash 单独由 cash_flow 表确定
        # 简化：保留 opening_balance 的其他资产
        opening_other_assets = max(
            opening_balance.get("total_assets", 0.0) - opening_cash - opening_balance.get("accounts_receivable", 0.0)
            - opening_balance.get("inventory", 0.0) - opening_fixed,
            0.0,
        )
        total_assets = cash + ar + inventory + fixed_assets + opening_other_assets

        trace["balance_sheet"] = {
            "capex_ratio": capex_ratio,
            "capex": capex,
            "dso_days": dso_days,
            "dio_days": dio_days,
            "accounts_receivable": ar,
            "inventory": inventory,
            "depreciation_ratio": depreciation_ratio,
            "depreciation": depreciation,
            "fixed_assets": fixed_assets,
            "short_term_debt": short_term_debt,
            "long_term_debt": long_term_debt,
            "total_liabilities": total_liabilities,
            "dividend_payout_ratio": dividend_payout_ratio,
            "dividend": dividend,
            "total_equity": total_equity,
            "equity_parent": equity_parent,
            "opening_other_assets": opening_other_assets,
            "opening_other_liabilities": opening_other_liabilities,
            "total_assets": total_assets,
        }
        return {
            "opening_cash": opening_cash,
            "cash": cash,  # placeholder, fixed in cash_flow
            "accounts_receivable": ar,
            "inventory": inventory,
            "fixed_assets": fixed_assets,
            "short_term_debt": short_term_debt,
            "long_term_debt": long_term_debt,
            "total_liabilities": total_liabilities,
            "total_equity": total_equity,
            "equity_parent": equity_parent,
            "capex": capex,
            "depreciation": depreciation,
            "dividend": dividend,
            "total_assets": total_assets,
        }

    # ---- 步骤 4：构建现金流量表 ----

    @classmethod
    def _build_cash_flow(
        cls,
        income_stmt: dict[str, Any],
        balance_sheet: dict[str, Any],
        opening_balance: dict[str, float],
        balance_inputs: dict[str, ImpactAssumption],
        scenario: str,
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        """推导现金流量表：OCF = net_profit + depreciation - WC change; ICF = -capex; FCF = share_change + debt_change - dividend。"""
        net_profit = income_stmt["net_profit"]
        depreciation = balance_sheet["depreciation"]
        capex = balance_sheet["capex"]
        dividend = balance_sheet["dividend"]

        # 营运资本变动 = (ΔAR + ΔInventory - ΔAP)；这里简化忽略 AP
        opening_ar = opening_balance.get("accounts_receivable", 0.0)
        opening_inv = opening_balance.get("inventory", 0.0)
        ar_change = balance_sheet["accounts_receivable"] - opening_ar
        inv_change = balance_sheet["inventory"] - opening_inv
        working_capital_change = ar_change + inv_change  # 正=占用现金

        operating_cash_flow = net_profit + depreciation - working_capital_change
        investing_cash_flow = -capex
        share_change = cls._scenario_value(balance_inputs.get("share_change"), scenario, default=0.0)
        debt_change = cls._scenario_value(balance_inputs.get("debt_change"), scenario, default=0.0)
        financing_cash_flow = share_change + debt_change - dividend

        opening_cash = opening_balance.get("cash", 0.0)
        closing_cash = opening_cash + operating_cash_flow + investing_cash_flow + financing_cash_flow

        trace["cash_flow"] = {
            "net_profit": net_profit,
            "depreciation": depreciation,
            "ar_change": ar_change,
            "inv_change": inv_change,
            "working_capital_change": working_capital_change,
            "operating_cash_flow": operating_cash_flow,
            "capex": capex,
            "investing_cash_flow": investing_cash_flow,
            "share_change": share_change,
            "debt_change": debt_change,
            "dividend": dividend,
            "financing_cash_flow": financing_cash_flow,
            "opening_cash": opening_cash,
            "closing_cash": closing_cash,
        }
        return {
            "operating_cash_flow": operating_cash_flow,
            "investing_cash_flow": investing_cash_flow,
            "financing_cash_flow": financing_cash_flow,
            "working_capital_change": working_capital_change,
            "closing_cash": closing_cash,
        }

    # ---- 步骤 5：组装 ForecastFinancials ----

    @classmethod
    def _assemble_financials(
        cls,
        period: str,
        currency: str,
        unit: str,
        income_stmt: dict[str, Any],
        balance_sheet: dict[str, Any],
        cash_flow: dict[str, Any],
        shares_outstanding: float,
    ) -> ForecastFinancials:
        """组装 ForecastFinancials。"""
        # 用 cash_flow 的 closing_cash 回填 balance_sheet.cash 并重算 total_assets
        cash = cash_flow["closing_cash"]
        # 重新计算 total_assets（cash 占位被替换）
        opening_other_assets = balance_sheet.get("opening_other_assets", 0.0) if "opening_other_assets" in balance_sheet else 0.0
        # 注意：balance_sheet 中已包含 opening_other_assets，重新计算 total_assets
        total_assets = (
            cash
            + balance_sheet["accounts_receivable"]
            + balance_sheet["inventory"]
            + balance_sheet["fixed_assets"]
        ) + opening_other_assets

        net_profit_parent = income_stmt["net_profit_parent"]
        eps = (net_profit_parent / shares_outstanding) if shares_outstanding > 0 else 0.0

        return ForecastFinancials(
            period=period,
            currency=currency,
            unit=unit,
            # 利润表
            revenue=round(income_stmt["revenue"], 6),
            operating_cost=round(income_stmt["operating_cost"], 6),
            gross_profit=round(income_stmt["gross_profit"], 6),
            operating_profit=round(income_stmt["operating_profit"], 6),
            pre_tax_profit=round(income_stmt["pre_tax_profit"], 6),
            income_tax=round(income_stmt["income_tax"], 6),
            net_profit=round(income_stmt["net_profit"], 6),
            net_profit_parent=round(income_stmt["net_profit_parent"], 6),
            minority_interest=round(income_stmt["minority_interest"], 6),
            # 资产负债表
            total_assets=round(total_assets, 6),
            total_liabilities=round(balance_sheet["total_liabilities"], 6),
            total_equity=round(balance_sheet["total_equity"], 6),
            equity_parent=round(balance_sheet["equity_parent"], 6),
            cash=round(cash, 6),
            accounts_receivable=round(balance_sheet["accounts_receivable"], 6),
            inventory=round(balance_sheet["inventory"], 6),
            fixed_assets=round(balance_sheet["fixed_assets"], 6),
            short_term_debt=round(balance_sheet["short_term_debt"], 6),
            long_term_debt=round(balance_sheet["long_term_debt"], 6),
            # 现金流量表
            opening_cash=round(balance_sheet["opening_cash"], 6),
            operating_cash_flow=round(cash_flow["operating_cash_flow"], 6),
            investing_cash_flow=round(cash_flow["investing_cash_flow"], 6),
            financing_cash_flow=round(cash_flow["financing_cash_flow"], 6),
            closing_cash=round(cash, 6),
            capex=round(balance_sheet["capex"], 6),
            depreciation=round(balance_sheet["depreciation"], 6),
            working_capital_change=round(cash_flow["working_capital_change"], 6),
            # 每股
            shares_outstanding=shares_outstanding,
            eps=round(eps, 6),
        )

    # ---- 步骤 6：三表勾稽 ----

    @classmethod
    def _reconcile(
        cls,
        financials: ForecastFinancials,
        opening_balance: dict[str, float],
        dividend: float = 0.0,
    ) -> list[ReconciliationCheck]:
        """三表勾稽：资产=负债+权益、现金勾稽、留存收益勾稽。"""
        checks: list[ReconciliationCheck] = []

        # 1. 资产 = 负债 + 权益
        expected_a = financials.total_liabilities + financials.total_equity
        actual_a = financials.total_assets
        diff_a = actual_a - expected_a
        checks.append(ReconciliationCheck(
            name=CHECK_BALANCE_IDENTITY,
            expected=round(expected_a, 6),
            actual=round(actual_a, 6),
            difference=round(diff_a, 6),
            tolerance=RECONCILIATION_TOLERANCE,
            status=ReconciliationStatus.OK.value if abs(diff_a) <= RECONCILIATION_TOLERANCE else ReconciliationStatus.OUT_OF_TOLERANCE.value,
            note="total_assets - (total_liabilities + total_equity) = 0",
        ))

        # 2. 现金流勾稽：closing_cash - opening_cash - OCF - ICF - FCF = 0
        expected_c = financials.opening_cash + financials.operating_cash_flow + financials.investing_cash_flow + financials.financing_cash_flow
        actual_c = financials.closing_cash
        diff_c = actual_c - expected_c
        checks.append(ReconciliationCheck(
            name=CHECK_CASH_RECONCILIATION,
            expected=round(expected_c, 6),
            actual=round(actual_c, 6),
            difference=round(diff_c, 6),
            tolerance=RECONCILIATION_TOLERANCE,
            status=ReconciliationStatus.OK.value if abs(diff_c) <= RECONCILIATION_TOLERANCE else ReconciliationStatus.OUT_OF_TOLERANCE.value,
            note="closing_cash = opening_cash + OCF + ICF + FCF",
        ))

        # 3. 留存收益勾稽：total_equity = opening_equity + net_profit - dividend
        # 注：total_equity 包含少数股东权益，故用 net_profit（含少数股东损益）
        opening_equity = opening_balance.get("total_equity", 0.0)
        expected_e = opening_equity + financials.net_profit - dividend
        actual_e = financials.total_equity
        diff_e = actual_e - expected_e
        checks.append(ReconciliationCheck(
            name=CHECK_RETAINED_EARNINGS,
            expected=round(expected_e, 6),
            actual=round(actual_e, 6),
            difference=round(diff_e, 6),
            tolerance=RECONCILIATION_TOLERANCE,
            status=ReconciliationStatus.OK.value if abs(diff_e) <= RECONCILIATION_TOLERANCE else ReconciliationStatus.OUT_OF_TOLERANCE.value,
            note="total_equity = opening_equity + net_profit - dividend (retained earnings linkage)",
        ))

        return checks

    # ---- 步骤 7：组装 IntegratedForecast ----

    @classmethod
    def _assemble_forecast(
        cls,
        request: ForecastBuildRequest,
        as_of: str,
        assumptions: list[ImpactAssumption],
        gaps: list[InputGap],
        trace: dict[str, Any],
        segment_summary: list[dict[str, Any]],
        shares_outstanding: float,
        financials: ForecastFinancials | None = None,
        reconciliation: list[ReconciliationCheck] | None = None,
        uncertainty: Interval | None = None,
    ) -> IntegratedForecast:
        """组装 IntegratedForecast。"""
        return IntegratedForecast(
            symbol=request.symbol,
            as_of=as_of,
            period=request.period,
            horizon=request.horizon,
            scenario=request.scenario,
            financials=financials or ForecastFinancials(period=request.period, currency=request.currency, unit=request.unit),
            segment_forecasts=segment_summary,
            assumptions=assumptions,
            reconciliation_checks=reconciliation or [],
            forecast_uncertainty=uncertainty or Interval.point(0.0),
            model_version=request.model_version,
            forecast_version=request.forecast_version,
            data_gaps=gaps,
            formula_trace=trace,
            input_snapshot=cls._snapshot_forecast_inputs(request),
            driver_ids=[item.get("driver_id", "") for item in segment_summary if item.get("driver_id")],
        )

    @staticmethod
    def _select_latest_capital(
        capitals: list[ShareCapitalVersion],
    ) -> ShareCapitalVersion | None:
        """选取 PIT 可见的最新股本版本。"""
        if not capitals:
            return None
        sorted_capitals = sorted(
            capitals, key=lambda c: c.as_of or c.published_at or c.retrieved_at
        )
        return sorted_capitals[-1]

    @classmethod
    def _scenario_value(
        cls,
        assumption: ImpactAssumption | None,
        scenario: str,
        default: float = 0.0,
    ) -> float:
        """按情景选取端点值。

        - baseline: 取中点。
        - bearish: 对 beneficial 变量取下界，对 reverse 变量取上界。
        - bullish: 反之。
        """
        if assumption is None or assumption.value is None:
            return default
        var_name = assumption.variable_name
        beneficial = cls._BENEFICIAL_VARS.get(var_name, True)
        v = assumption.value
        if scenario == EarningsScenarioKind.BASELINE.value:
            return v.mid
        if scenario == EarningsScenarioKind.BEARISH.value:
            return v.low if beneficial else v.high
        if scenario == EarningsScenarioKind.BULLISH.value:
            return v.high if beneficial else v.low
        return v.mid

    @staticmethod
    def _snapshot_forecast_inputs(request: ForecastBuildRequest) -> dict[str, Any]:
        """生成预测输入快照（用于幂等追溯）。"""
        return {
            "symbol": request.symbol,
            "as_of": request.as_of,
            "period": request.period,
            "horizon": request.horizon,
            "scenario": request.scenario,
            "historical": request.historical,
            "model_version": request.model_version,
            "forecast_version": request.forecast_version,
            "currency": request.currency,
            "unit": request.unit,
            "segment_impact_ids": [imp.impact_id for imp in request.segment_impacts],
            "balance_inputs": {
                k: v.to_dict() for k, v in request.balance_inputs.items()
            },
            "opening_balance": dict(request.opening_balance),
            "share_capital": request.share_capital.to_dict() if request.share_capital else None,
            "capital_versions": [c.to_dict() for c in request.capital_versions],
        }
