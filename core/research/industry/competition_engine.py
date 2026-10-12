"""P1.5 产业竞争引擎。

负责 CompetitiveLandscape / TechnologyRoute / BargainingPower / CompanyMapping
4 个竞争侧模块的组装与校验，并通过 EconomicsEngine 处理经济部分，最终合并为
完整 IndustryProfile。

硬门禁：
- 验收点 1：member_of_theme 不进入市场份额、产品和竞争结论
  - CompetitiveLandscape.member_of_theme_excluded 必须为 True
  - 若发现 member_of_theme 来源的 participant，过滤并生成 blocker gap
- 验收点 4：公司映射证据为空显示"暂无本地证据"
  - CompanyMapping.local_evidence_display() 返回 NO_LOCAL_EVIDENCE 常量
  - has_local_evidence=False 时不阻断，但生成 warning gap
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from .contracts import (
    BargainingPower,
    CapexCycle,
    CompanyMapping,
    CompetitiveLandscape,
    DemandDriver,
    GAP_KIND_MEMBER_OF_THEME_LEAK,
    GAP_KIND_MISSING,
    GAP_SEVERITY_BLOCKER,
    GAP_SEVERITY_WARNING,
    IndustryGap,
    IndustryProfile,
    InventoryCycle,
    LifecycleStage,
    MarketSizing,
    NO_LOCAL_EVIDENCE,
    Participant,
    PriceCostCurve,
    SupplyProfile,
    TechnologyRoute,
    validate_bargaining_direction,
    validate_bargaining_level,
    validate_company_role,
    validate_concentration_metric,
    validate_share_basis,
    validate_tech_status,
)
from .economics_engine import (
    EconomicsEngine,
    EconomicsRequest,
    _mark_grounding_status,
)


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


@dataclass
class CompetitionRequest:
    """竞争引擎请求（顶层入口，包含经济与竞争两部分输入）。

    Attributes:
        industry_name: 行业名
        industry_code: 行业代码
        code_system: 代码体系
        period: 画像基准期间 YYYY-MM-DD
        market_sizing: TAM/SAM/SOM（经济部分）
        demand_drivers: 需求驱动列表（经济部分）
        supply_profile: 供给画像（经济部分）
        price_cost_curve: 价格成本曲线（经济部分）
        inventory_cycle: 库存周期（经济部分）
        capex_cycle: 资本开支周期（经济部分）
        lifecycle_stage: 生命周期阶段（经济部分）
        competitive_landscape: 竞争格局（竞争部分）
        technology_routes: 技术路线列表（竞争部分）
        bargaining_powers: 议价能力列表（竞争部分）
        company_mappings: 公司映射列表（竞争部分）
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
    competitive_landscape: CompetitiveLandscape | None = None
    technology_routes: list[TechnologyRoute] = field(default_factory=list)
    bargaining_powers: list[BargainingPower] = field(default_factory=list)
    company_mappings: list[CompanyMapping] = field(default_factory=list)
    source_trace_id: str = ""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


class CompetitionEngine:
    """产业竞争引擎。

    组装竞争侧 4 个模块（CompetitiveLandscape/TechnologyRoute/BargainingPower/
    CompanyMapping）并校验，同时通过 EconomicsEngine 处理经济侧 7 个模块，
    最终合并为完整 IndustryProfile。

    硬门禁：
    - 验收点 1：member_of_theme 不进入市场份额、产品和竞争结论
    - 验收点 4：公司映射证据为空显示"暂无本地证据"
    """

    def __init__(self) -> None:
        self._economics = EconomicsEngine()

    def compute_industry_profile(
        self, request: CompetitionRequest
    ) -> IndustryProfile:
        """计算完整产业画像（经济 + 竞争）。

        Args:
            request: 竞争引擎请求（顶层）

        Returns:
            IndustryProfile（含全部 10 个模块 + gaps）
        """
        # 1. 用 EconomicsEngine 处理经济部分
        eco_request = EconomicsRequest(
            industry_name=request.industry_name,
            industry_code=request.industry_code,
            code_system=request.code_system,
            period=request.period,
            market_sizing=request.market_sizing,
            demand_drivers=request.demand_drivers,
            supply_profile=request.supply_profile,
            price_cost_curve=request.price_cost_curve,
            inventory_cycle=request.inventory_cycle,
            capex_cycle=request.capex_cycle,
            lifecycle_stage=request.lifecycle_stage,
            source_trace_id=request.source_trace_id,
        )
        profile = self._economics.compute_industry_economics(eco_request)
        gaps: list[IndustryGap] = list(profile.gaps)

        # 2. 处理竞争部分
        competitive_landscape = self._validate_competitive_landscape(
            request.competitive_landscape, gaps
        )
        technology_routes = self._validate_technology_routes(
            request.technology_routes, gaps
        )
        bargaining_powers = self._validate_bargaining_powers(
            request.bargaining_powers, gaps
        )
        company_mappings = self._validate_company_mappings(
            request.company_mappings, gaps
        )

        # 3. 合并到 profile
        profile.competitive_landscape = competitive_landscape
        profile.technology_routes = technology_routes
        profile.bargaining_powers = bargaining_powers
        profile.company_mappings = company_mappings
        profile.gaps = gaps
        return profile

    # ----- 子模块校验 -----

    def _validate_competitive_landscape(
        self, landscape: CompetitiveLandscape | None, gaps: list[IndustryGap]
    ) -> CompetitiveLandscape | None:
        """校验竞争格局。

        验收点 1：member_of_theme 不进入市场份额、产品和竞争结论。
        若 member_of_theme_excluded=False，生成 blocker gap。
        过滤 share_source_refs 中的 member_of_theme 来源。
        """
        if landscape is None:
            return None
        gap_start = len(gaps)
        if landscape.concentration_metric:
            validate_concentration_metric(landscape.concentration_metric)
        # 验收点 1：member_of_theme 必须被过滤
        if not landscape.member_of_theme_excluded:
            gaps.append(
                IndustryGap(
                    field_name="competitive_landscape.member_of_theme_excluded",
                    reason=(
                        "竞争格局未过滤 member_of_theme 关系，"
                        "member_of_theme 不得进入市场份额、产品和竞争结论"
                    ),
                    blocker_kind=GAP_KIND_MEMBER_OF_THEME_LEAK,
                    severity=GAP_SEVERITY_BLOCKER,
                )
            )
            landscape.grounding_status = "missing"
        # 校验参与者
        participants = (
            landscape.participants
            if isinstance(landscape.participants, (list, tuple))
            else []
        )
        for idx, p in enumerate(participants):
            if not isinstance(p, Participant):
                continue
            if p.share_basis:
                validate_share_basis(p.share_basis)
            if p.role:
                validate_company_role(p.role)
            share = _finite(p.market_share)
            if p.market_share is not None and (share is None or not 0.0 <= share <= 1.0):
                gaps.append(
                    IndustryGap(
                        field_name=f"competitive_landscape.participants[{idx}].market_share",
                        reason=(
                            f"参与者 {p.name or idx} 市场份额 {p.market_share} 超出 [0,1] 区间"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_WARNING,
                    )
                )
            if p.market_share is not None and not p.share_source_ref:
                gaps.append(
                    IndustryGap(
                        field_name=f"competitive_landscape.participants[{idx}].share_source_ref",
                        reason=(
                            f"参与者 {p.name or idx} 缺份额来源证据，"
                            "份额不得从 member_of_theme 推导"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_BLOCKER,
                    )
                )
        # 集中度校验
        concentration = _finite(landscape.concentration_value)
        if (
            landscape.concentration_value is not None
            and (concentration is None or not 0.0 <= concentration <= 1.0)
            and landscape.concentration_metric != "hhi"
        ):
            gaps.append(
                IndustryGap(
                    field_name="competitive_landscape.concentration_value",
                    reason=(
                        f"集中度值 {landscape.concentration_value} 超出 [0,1] "
                        "（hhi 除外）"
                    ),
                    blocker_kind=GAP_KIND_MISSING,
                    severity=GAP_SEVERITY_WARNING,
                )
            )
        common_missing = _mark_grounding_status(
            landscape,
            "competitive_landscape",
            "竞争格局",
            gaps,
            evidence_field="share_source_refs",
        )
        has_blocker = any(
            gap.severity == GAP_SEVERITY_BLOCKER for gap in gaps[gap_start:]
        )
        if (
            landscape.member_of_theme_excluded
            and (landscape.share_source_refs or participants)
            and not common_missing
            and not has_blocker
        ):
            landscape.grounding_status = "ok"
        else:
            landscape.grounding_status = "missing"
        return landscape

    def _validate_technology_routes(
        self, routes: list[TechnologyRoute], gaps: list[IndustryGap]
    ) -> list[TechnologyRoute]:
        """校验技术路线列表。"""
        result: list[TechnologyRoute] = []
        for idx, route in enumerate(routes if isinstance(routes, (list, tuple)) else []):
            if not isinstance(route, TechnologyRoute):
                continue
            if route.status:
                validate_tech_status(route.status)
            _mark_grounding_status(
                route, f"technology_routes[{idx}]", f"技术路线 {route.name or idx}", gaps
            )
            result.append(route)
        return result

    def _validate_bargaining_powers(
        self, powers: list[BargainingPower], gaps: list[IndustryGap]
    ) -> list[BargainingPower]:
        """校验议价能力列表。"""
        result: list[BargainingPower] = []
        for idx, power in enumerate(powers if isinstance(powers, (list, tuple)) else []):
            if not isinstance(power, BargainingPower):
                continue
            if power.direction:
                validate_bargaining_direction(power.direction)
            if power.level:
                validate_bargaining_level(power.level)
            _mark_grounding_status(
                power, f"bargaining_powers[{idx}]", f"议价能力 {idx}", gaps
            )
            result.append(power)
        return result

    def _validate_company_mappings(
        self, mappings: list[CompanyMapping], gaps: list[IndustryGap]
    ) -> list[CompanyMapping]:
        """校验公司映射列表。

        验收点 4：公司映射证据为空显示"暂无本地证据"。
        has_local_evidence=False 时不阻断，但生成 warning gap；
        调用方通过 local_evidence_display() 获取固定字符串 NO_LOCAL_EVIDENCE。
        """
        result: list[CompanyMapping] = []
        for idx, mapping in enumerate(mappings if isinstance(mappings, (list, tuple)) else []):
            if not isinstance(mapping, CompanyMapping):
                continue
            if mapping.industry_role:
                validate_company_role(mapping.industry_role)
            has_evidence = mapping.local_evidence_display() != NO_LOCAL_EVIDENCE
            if not has_evidence:
                # 验收点 4：证据为空显示"暂无本地证据"
                gaps.append(
                    IndustryGap(
                        field_name=f"company_mappings[{idx}].evidence_ids",
                        reason=(
                            f"公司映射 {mapping.symbol or idx} 缺本地证据，"
                            f"页面应显示 '{NO_LOCAL_EVIDENCE}'"
                        ),
                        blocker_kind=GAP_KIND_MISSING,
                        severity=GAP_SEVERITY_WARNING,
                        note=f"local_evidence_display='{mapping.local_evidence_display()}'",
                    )
                )
            result.append(mapping)
        return result


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def compute_industry_profile(request: CompetitionRequest) -> IndustryProfile:
    """便捷入口：计算完整产业画像（经济 + 竞争）。

    Args:
        request: 竞争引擎请求（顶层）

    Returns:
        IndustryProfile
    """
    return CompetitionEngine().compute_industry_profile(request)
