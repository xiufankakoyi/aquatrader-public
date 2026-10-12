"""P1.5 产业经济引擎。

负责 MarketSizing / DemandDriver / SupplyProfile / PriceCostCurve / InventoryCycle /
CapexCycle / LifecycleStage 7 个经济侧模块的组装与校验。

硬门禁：
- 验收点 2：赛道阶段必须有时间和证据（LifecycleStage 缺一即触发 blocker gap）
- 验收点 3：新闻热度不能单独证明需求兑现（heat_only=True 降级为 warning）
- 缺 source_refs/period/available_at 任一项则 grounding_status="missing"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    BargainingPower,
    CAPEX_PHASES,
    CapexCycle,
    CompanyMapping,
    CompetitiveLandscape,
    DEMAND_DIRECTIONS,
    DemandDriver,
    GAP_KIND_HEAT_ONLY,
    GAP_KIND_MISSING,
    GAP_KIND_STAGE_NO_EVIDENCE,
    GAP_KIND_STAGE_NO_TIME,
    GAP_SEVERITY_BLOCKER,
    GAP_SEVERITY_WARNING,
    IndustryGap,
    IndustryProfile,
    InventoryCycle,
    LIFECYCLE_STAGES,
    LifecycleStage,
    MARKET_SIZING_METHODS,
    MarketSizing,
    PRICE_TRENDS,
    PriceCostCurve,
    SupplyProfile,
    TechnologyRoute,
    validate_capex_phase,
    validate_demand_direction,
    validate_lifecycle_stage,
    validate_market_sizing_method,
    validate_price_trend,
    validate_supply_phase,
)


# ---------------------------------------------------------------------------
# 请求与状态
# ---------------------------------------------------------------------------


@dataclass
class EconomicsRequest:
    """经济引擎请求。

    Attributes:
        industry_name: 行业名
        industry_code: 行业代码
        code_system: 代码体系
        period: 画像基准期间 YYYY-MM-DD
        market_sizing: TAM/SAM/SOM（可选）
        demand_drivers: 需求驱动列表
        supply_profile: 供给画像（可选）
        price_cost_curve: 价格成本曲线（可选）
        inventory_cycle: 库存周期（可选）
        capex_cycle: 资本开支周期（可选）
        lifecycle_stage: 生命周期阶段（可选）
        source_trace_id: 来源追溯 ID
    """

    industry_name: str = ""
    industry_code: str = ""
    code_system: str = "sw"
    period: str = ""
    market_sizing: MarketSizing | None = None
    demand_drivers: list[DemandDriver] = field(default_factory=list)
    supply_profile: SupplyProfile | None = None
    price_cost_curve: PriceCostCurve | None = None
    inventory_cycle: InventoryCycle | None = None
    capex_cycle: CapexCycle | None = None
    lifecycle_stage: LifecycleStage | None = None
    source_trace_id: str = ""


def _mark_grounding_status(
    item: Any,
    field_prefix: str,
    label: str,
    gaps: list[IndustryGap],
    evidence_field: str = "source_refs",
) -> bool:
    """Mark a module and emit one blocker for each missing grounding field."""
    missing = [
        name for name in (evidence_field, "period", "available_at")
        if not getattr(item, name, None)
    ]
    if not missing:
        item.grounding_status = "ok"
        return False
    item.grounding_status = "missing"
    for name in missing:
        gaps.append(
            IndustryGap(
                field_name=f"{field_prefix}.{name}",
                reason=f"{label} 缺{name}",
                blocker_kind=GAP_KIND_MISSING,
                severity=GAP_SEVERITY_BLOCKER,
            )
        )
    return True


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class EconomicsEngine:
    """产业经济引擎。

    组装经济侧 7 个模块（MarketSizing/DemandDriver/SupplyProfile/PriceCostCurve/
    InventoryCycle/CapexCycle/LifecycleStage）并校验：

    - 验收点 2：赛道阶段必须有时间和证据
    - 验收点 3：新闻热度不能单独证明需求兑现
    - 缺 source_refs/period/available_at 任一项则 grounding_status="missing"
    """

    def compute_industry_economics(self, request: EconomicsRequest) -> IndustryProfile:
        """计算产业经济画像。

        Args:
            request: 经济引擎请求

        Returns:
            IndustryProfile（含 gaps 与各子模块 grounding_status）
        """
        gaps: list[IndustryGap] = []

        # 校验 market_sizing
        market_sizing = self._validate_market_sizing(request.market_sizing, gaps)

        # 校验 demand_drivers（验收点 3）
        demand_drivers = self._validate_demand_drivers(request.demand_drivers, gaps)

        # 校验 supply_profile
        supply_profile = self._validate_supply_profile(request.supply_profile, gaps)

        # 校验 price_cost_curve
        price_cost_curve = self._validate_price_cost_curve(
            request.price_cost_curve, gaps
        )

        # 校验 inventory_cycle
        inventory_cycle = self._validate_inventory_cycle(request.inventory_cycle, gaps)

        # 校验 capex_cycle
        capex_cycle = self._validate_capex_cycle(request.capex_cycle, gaps)

        # 验收点 2：赛道阶段必须有时间和证据
        lifecycle_stage = self._validate_lifecycle_stage(request.lifecycle_stage, gaps)

        profile = IndustryProfile(
            industry_name=request.industry_name,
            industry_code=request.industry_code,
            code_system=request.code_system,
            period=request.period,
            market_sizing=market_sizing,
            demand_drivers=demand_drivers,
            supply_profile=supply_profile,
            price_cost_curve=price_cost_curve,
            inventory_cycle=inventory_cycle,
            capex_cycle=capex_cycle,
            lifecycle_stage=lifecycle_stage,
            technology_routes=[],
            bargaining_powers=[],
            company_mappings=[],
            gaps=gaps,
            source_trace_id=request.source_trace_id,
        )
        return profile

    # ----- 子模块校验 -----

    def _validate_market_sizing(
        self, sizing: MarketSizing | None, gaps: list[IndustryGap]
    ) -> MarketSizing | None:
        """校验 TAM/SAM/SOM 测算。

        缺 source_refs 或 period → missing；
        method 非法 → 抛 ValueError（不该发生）。
        """
        if sizing is None:
            return None
        if sizing.method:
            validate_market_sizing_method(sizing.method)
        _mark_grounding_status(sizing, "market_sizing", "TAM/SAM/SOM", gaps)
        return sizing

    def _validate_demand_drivers(
        self, drivers: list[DemandDriver], gaps: list[IndustryGap]
    ) -> list[DemandDriver]:
        """校验需求驱动列表。

        验收点 3：新闻热度不能单独证明需求兑现。
        heat_only=True 时降级为 warning，driver.grounding_status="degraded"。
        """
        result: list[DemandDriver] = []
        for idx, driver in enumerate(drivers if isinstance(drivers, (list, tuple)) else []):
            if not isinstance(driver, DemandDriver):
                continue
            if driver.direction:
                validate_demand_direction(driver.direction)
            if _mark_grounding_status(
                driver, f"demand_drivers[{idx}]", f"需求驱动 {driver.name or idx}", gaps
            ):
                result.append(driver)
                continue
            if driver.heat_only:
                # 验收点 3：新闻热度不能单独证明需求兑现
                driver.grounding_status = "degraded"
                gaps.append(
                    IndustryGap(
                        field_name=f"demand_drivers[{idx}].heat_only",
                        reason=(
                            f"需求驱动 {driver.name or idx} 仅有新闻热度，"
                            "不能单独证明需求兑现，降级为 warning"
                        ),
                        blocker_kind=GAP_KIND_HEAT_ONLY,
                        severity=GAP_SEVERITY_WARNING,
                    )
                )
            else:
                driver.grounding_status = "ok"
            result.append(driver)
        return result

    def _validate_supply_profile(
        self, supply: SupplyProfile | None, gaps: list[IndustryGap]
    ) -> SupplyProfile | None:
        """校验供给画像。"""
        if supply is None:
            return None
        if supply.expansion_phase:
            validate_supply_phase(supply.expansion_phase)
        _mark_grounding_status(supply, "supply_profile", "供给画像", gaps)
        return supply

    def _validate_price_cost_curve(
        self, curve: PriceCostCurve | None, gaps: list[IndustryGap]
    ) -> PriceCostCurve | None:
        """校验价格成本曲线。"""
        if curve is None:
            return None
        if curve.trend:
            validate_price_trend(curve.trend)
        _mark_grounding_status(curve, "price_cost_curve", "价格成本曲线", gaps)
        return curve

    def _validate_inventory_cycle(
        self, cycle: InventoryCycle | None, gaps: list[IndustryGap]
    ) -> InventoryCycle | None:
        """校验库存周期。"""
        if cycle is None:
            return None
        _mark_grounding_status(cycle, "inventory_cycle", "库存周期", gaps)
        return cycle

    def _validate_capex_cycle(
        self, capex: CapexCycle | None, gaps: list[IndustryGap]
    ) -> CapexCycle | None:
        """校验资本开支周期。"""
        if capex is None:
            return None
        if capex.fcf_cycle_phase:
            validate_capex_phase(capex.fcf_cycle_phase)
        _mark_grounding_status(capex, "capex_cycle", "资本开支周期", gaps)
        return capex

    def _validate_lifecycle_stage(
        self, stage: LifecycleStage | None, gaps: list[IndustryGap]
    ) -> LifecycleStage | None:
        """校验赛道生命周期阶段。

        验收点 2：赛道阶段必须有时间和证据。
        stage_evidence_id 缺失 → blocker gap (stage_no_evidence)
        stage_dated_at 缺失 → blocker gap (stage_no_time)
        """
        if stage is None:
            return None
        if stage.stage:
            validate_lifecycle_stage(stage.stage)
        has_evidence = bool(stage.stage_evidence_id)
        has_time = bool(stage.stage_dated_at)
        if not has_evidence:
            stage.grounding_status = "missing"
            gaps.append(
                IndustryGap(
                    field_name="lifecycle_stage.stage_evidence_id",
                    reason=(
                        f"赛道阶段 {stage.stage or '未知'} 缺阶段判定证据 ID"
                    ),
                    blocker_kind=GAP_KIND_STAGE_NO_EVIDENCE,
                    severity=GAP_SEVERITY_BLOCKER,
                )
            )
        if not has_time:
            gaps.append(
                IndustryGap(
                    field_name="lifecycle_stage.stage_dated_at",
                    reason=(
                        f"赛道阶段 {stage.stage or '未知'} 缺阶段判定时间"
                    ),
                    blocker_kind=GAP_KIND_STAGE_NO_TIME,
                    severity=GAP_SEVERITY_BLOCKER,
                )
            )
        common_missing = _mark_grounding_status(
            stage, "lifecycle_stage", "赛道阶段", gaps
        )
        if not has_evidence or not has_time or common_missing:
            stage.grounding_status = "missing"
        return stage


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def compute_economics(request: EconomicsRequest) -> IndustryProfile:
    """便捷入口：直接计算产业经济画像。

    Args:
        request: 经济引擎请求

    Returns:
        IndustryProfile
    """
    return EconomicsEngine().compute_industry_economics(request)
