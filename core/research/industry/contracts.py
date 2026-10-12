"""P1.5 产业经济与竞争格局领域契约。

独立领域模型，按 P1 计划 §5 Task P1.5 至少覆盖 10 类内容。
所有结构化条目必须带：来源证据、期间、可见时点；缺任一项则 grounding_status="missing"。

硬门禁：
- member_of_theme 不进入市场份额、产品和竞争结论（CompetitionEngine 过滤）
- 赛道阶段必须有时间和证据（LifecycleStage 缺一即 gap）
- 新闻热度不能单独证明需求兑现（DemandDriver.heat_only=True 降级为 warning）
- 公司映射证据为空显示"暂无本地证据"（CompanyMapping.has_local_evidence=False 返回 NO_LOCAL_EVIDENCE 常量）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 验收点 4：公司映射证据为空时显示的固定字符串
NO_LOCAL_EVIDENCE: str = "暂无本地证据"

# 行业代码体系
INDUSTRY_CODE_SW: str = "sw"  # 申万
INDUSTRY_CODE_ZX: str = "zx"  # 中信
INDUSTRY_CODE_GICS: str = "gics"
INDUSTRY_CODE_SYSTEMS: tuple[str, ...] = (
    INDUSTRY_CODE_SW,
    INDUSTRY_CODE_ZX,
    INDUSTRY_CODE_GICS,
)

# TAM/SAM/SOM 测算方法
MARKET_SIZING_METHOD_TOP_DOWN: str = "top_down"
MARKET_SIZING_METHOD_BOTTOM_UP: str = "bottom_up"
MARKET_SIZING_METHOD_SUPPLY_DEMAND: str = "supply_demand"
MARKET_SIZING_METHODS: tuple[str, ...] = (
    MARKET_SIZING_METHOD_TOP_DOWN,
    MARKET_SIZING_METHOD_BOTTOM_UP,
    MARKET_SIZING_METHOD_SUPPLY_DEMAND,
)

# 需求驱动方向
DEMAND_DIRECTION_POSITIVE: str = "positive"
DEMAND_DIRECTION_NEGATIVE: str = "negative"
DEMAND_DIRECTION_NEUTRAL: str = "neutral"
DEMAND_DIRECTIONS: tuple[str, ...] = (
    DEMAND_DIRECTION_POSITIVE,
    DEMAND_DIRECTION_NEGATIVE,
    DEMAND_DIRECTION_NEUTRAL,
)

# 供给扩产周期阶段
SUPPLY_PHASE_EXPANSION: str = "expansion"
SUPPLY_PHASE_STABILIZATION: str = "stabilization"
SUPPLY_PHASE_CONTRACTION: str = "contraction"
SUPPLY_PHASES: tuple[str, ...] = (
    SUPPLY_PHASE_EXPANSION,
    SUPPLY_PHASE_STABILIZATION,
    SUPPLY_PHASE_CONTRACTION,
)

# 价格曲线趋势
PRICE_TREND_UP: str = "up"
PRICE_TREND_DOWN: str = "down"
PRICE_TREND_FLAT: str = "flat"
PRICE_TREND_VOLATILE: str = "volatile"
PRICE_TRENDS: tuple[str, ...] = (
    PRICE_TREND_UP,
    PRICE_TREND_DOWN,
    PRICE_TREND_FLAT,
    PRICE_TREND_VOLATILE,
)

# 库存周期状态
INVENTORY_TREND_ACCUMULATING: str = "accumulating"
INVENTORY_TREND_DEPLETING: str = "depleting"
INVENTORY_TREND_STABLE: str = "stable"
INVENTORY_TRENDS: tuple[str, ...] = (
    INVENTORY_TREND_ACCUMULATING,
    INVENTORY_TREND_DEPLETING,
    INVENTORY_TREND_STABLE,
)

# 份额计算基础
SHARE_BASIS_REVENUE: str = "revenue"
SHARE_BASIS_VOLUME: str = "volume"
SHARE_BASIS_CAPACITY: str = "capacity"
SHARE_BASIS_NONE: str = "none"
SHARE_BASES: tuple[str, ...] = (
    SHARE_BASIS_REVENUE,
    SHARE_BASIS_VOLUME,
    SHARE_BASIS_CAPACITY,
    SHARE_BASIS_NONE,
)

# 集中度指标类型
CONCENTRATION_METRIC_CR3: str = "cr3"
CONCENTRATION_METRIC_CR5: str = "cr5"
CONCENTRATION_METRIC_CR10: str = "cr10"
CONCENTRATION_METRIC_HHI: str = "hhi"
CONCENTRATION_METRICS: tuple[str, ...] = (
    CONCENTRATION_METRIC_CR3,
    CONCENTRATION_METRIC_CR5,
    CONCENTRATION_METRIC_CR10,
    CONCENTRATION_METRIC_HHI,
)

# 技术路线状态
TECH_STATUS_EMERGING: str = "emerging"
TECH_STATUS_MAINSTREAM: str = "mainstream"
TECH_STATUS_DECLINING: str = "declining"
TECH_STATUS_EXPERIMENTAL: str = "experimental"
TECH_STATUSES: tuple[str, ...] = (
    TECH_STATUS_EMERGING,
    TECH_STATUS_MAINSTREAM,
    TECH_STATUS_DECLINING,
    TECH_STATUS_EXPERIMENTAL,
)

# 议价能力方向
BARGAINING_DIRECTION_UPSTREAM: str = "upstream"
BARGAINING_DIRECTION_DOWNSTREAM: str = "downstream"
BARGAINING_DIRECTIONS: tuple[str, ...] = (
    BARGAINING_DIRECTION_UPSTREAM,
    BARGAINING_DIRECTION_DOWNSTREAM,
)

# 议价能力等级
BARGAINING_LEVEL_STRONG: str = "strong"
BARGAINING_LEVEL_MODERATE: str = "moderate"
BARGAINING_LEVEL_WEAK: str = "weak"
BARGAINING_LEVELS: tuple[str, ...] = (
    BARGAINING_LEVEL_STRONG,
    BARGAINING_LEVEL_MODERATE,
    BARGAINING_LEVEL_WEAK,
)

# 资本开支周期阶段
CAPEX_PHASE_INVESTMENT: str = "investment"
CAPEX_PHASE_HARVEST: str = "harvest"
CAPEX_PHASE_DECLINE: str = "decline"
CAPEX_PHASES: tuple[str, ...] = (
    CAPEX_PHASE_INVESTMENT,
    CAPEX_PHASE_HARVEST,
    CAPEX_PHASE_DECLINE,
)

# 赛道生命周期阶段
LIFECYCLE_STAGE_EMBRYONIC: str = "embryonic"
LIFECYCLE_STAGE_GROWTH: str = "growth"
LIFECYCLE_STAGE_MATURE: str = "mature"
LIFECYCLE_STAGE_DECLINE: str = "decline"
LIFECYCLE_STAGES: tuple[str, ...] = (
    LIFECYCLE_STAGE_EMBRYONIC,
    LIFECYCLE_STAGE_GROWTH,
    LIFECYCLE_STAGE_MATURE,
    LIFECYCLE_STAGE_DECLINE,
)

# 公司映射角色
COMPANY_ROLE_LEADER: str = "leader"
COMPANY_ROLE_CHALLENGER: str = "challenger"
COMPANY_ROLE_FOLLOWER: str = "follower"
COMPANY_ROLE_NICHE: str = "niche"
COMPANY_ROLES: tuple[str, ...] = (
    COMPANY_ROLE_LEADER,
    COMPANY_ROLE_CHALLENGER,
    COMPANY_ROLE_FOLLOWER,
    COMPANY_ROLE_NICHE,
)

# Gap 严重度
GAP_SEVERITY_BLOCKER: str = "blocker"
GAP_SEVERITY_WARNING: str = "warning"
GAP_SEVERITIES: tuple[str, ...] = (GAP_SEVERITY_BLOCKER, GAP_SEVERITY_WARNING)

# Gap 类型
GAP_KIND_MISSING: str = "missing"
GAP_KIND_HEAT_ONLY: str = "heat_only"
GAP_KIND_QUALITATIVE_ONLY: str = "qualitative_only"
GAP_KIND_STAGE_NO_EVIDENCE: str = "stage_no_evidence"
GAP_KIND_STAGE_NO_TIME: str = "stage_no_time"
GAP_KIND_MEMBER_OF_THEME_LEAK: str = "member_of_theme_leak"


def _items(value: Any) -> tuple[Any, ...]:
    """Return sequence-like contract values without treating strings as lists."""
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _text_items(value: Any) -> list[str]:
    """Keep only non-empty string items from a contract list."""
    return [item.strip() for item in _items(value) if isinstance(item, str) and item.strip()]


def _point_items(value: Any) -> list[list[Any]]:
    """Serialize only valid ``(period, value)`` points."""
    return [list(point[:2]) for point in _items(value)
            if isinstance(point, (list, tuple)) and len(point) >= 2]


def _nested_dicts(value: Any) -> list[dict[str, Any]]:
    """Serialize typed child objects and skip malformed entries."""
    result: list[dict[str, Any]] = []
    for item in _items(value):
        to_dict = getattr(item, "to_dict", None)
        if not callable(to_dict):
            continue
        payload = to_dict()
        if isinstance(payload, dict):
            result.append(payload)
    return result


def _nested_dict(value: Any) -> dict[str, Any] | None:
    """Serialize one typed child object, or omit malformed optional input."""
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return None
    payload = to_dict()
    return payload if isinstance(payload, dict) else None


def _validate_choice(value: Any, name: str, choices: tuple[str, ...]) -> None:
    """Validate optional enum-like fields without leaking type errors."""
    if value and (not isinstance(value, str) or value not in choices):
        raise ValueError(f"非法 {name}: {value}; 允许值: {choices}")


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class MarketSizing:
    """TAM/SAM/SOM 口径。

    Attributes:
        tam: 全市场（Total Addressable Market）
        sam: 可服务市场（Serviceable Addressable Market）
        som: 公司可获取（Serviceable Obtainable Market）
        unit: 单位（如 "亿元"、"万台"）
        currency: 币种（金额类必填）
        period: 期间 YYYY-MM-DD
        method: 测算方法（MARKET_SIZING_METHODS 之一）
        source_refs: 来源证据 ID 列表
        available_at: 可见时点（PIT）
        grounding_status: 可追溯状态
        note: 备注
    """

    tam: float | None = None
    sam: float | None = None
    som: float | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    method: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "tam": self.tam,
            "sam": self.sam,
            "som": self.som,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "method": self.method,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class DemandDriver:
    """需求驱动和渗透率。

    硬门禁：新闻热度不能单独证明需求兑现。
    若 heat_only=True，必须降级为 warning，不作为有效需求证据。

    Attributes:
        name: 驱动名（如 "EV 销量"）
        direction: 方向（DEMAND_DIRECTIONS 之一）
        magnitude: 数值幅度（如增速百分比）
        magnitude_unit: 幅度单位
        penetration_rate: 渗透率（0-1）
        period: 期间 YYYY-MM-DD
        source_refs: 来源证据 ID 列表
        available_at: 可见时点
        heat_only: 仅新闻热度（True 时降级为 warning）
        grounding_status: 可追溯状态
        note: 备注
    """

    name: str = ""
    direction: str = DEMAND_DIRECTION_NEUTRAL
    magnitude: float | None = None
    magnitude_unit: str = ""
    penetration_rate: float | None = None
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    heat_only: bool = False
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "magnitude_unit": self.magnitude_unit,
            "penetration_rate": self.penetration_rate,
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "heat_only": self.heat_only,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class SupplyProfile:
    """供给、产能和扩产周期。

    Attributes:
        industry_capacity: 行业总产能
        capacity_unit: 产能单位
        utilization_rate: 行业产能利用率（0-1）
        expansion_lead_time_months: 扩产周期（月）
        expansion_phase: 扩产阶段（SUPPLY_PHASES 之一）
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    industry_capacity: float | None = None
    capacity_unit: str = ""
    utilization_rate: float | None = None
    expansion_lead_time_months: int | None = None
    expansion_phase: str = ""
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "industry_capacity": self.industry_capacity,
            "capacity_unit": self.capacity_unit,
            "utilization_rate": self.utilization_rate,
            "expansion_lead_time_months": self.expansion_lead_time_months,
            "expansion_phase": self.expansion_phase,
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class PriceCostCurve:
    """价格与成本曲线。

    Attributes:
        price_points: 价格点列表 [(period, value), ...]
        cost_points: 成本点列表 [(period, value), ...]
        unit: 单位
        currency: 币种
        trend: 趋势（PRICE_TRENDS 之一）
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    price_points: list[tuple[str, float]] = field(default_factory=list)
    cost_points: list[tuple[str, float]] = field(default_factory=list)
    unit: str = ""
    currency: str = ""
    trend: str = ""
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "price_points": _point_items(self.price_points),
            "cost_points": _point_items(self.cost_points),
            "unit": self.unit,
            "currency": self.currency,
            "trend": self.trend,
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class InventoryCycle:
    """库存和交付周期。

    Attributes:
        inventory_days: 库存周转天数
        delivery_lead_time_days: 交付周期（天）
        inventory_trend: 库存趋势（INVENTORY_TRENDS 之一）
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    inventory_days: float | None = None
    delivery_lead_time_days: int | None = None
    inventory_trend: str = ""
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "inventory_days": self.inventory_days,
            "delivery_lead_time_days": self.delivery_lead_time_days,
            "inventory_trend": self.inventory_trend,
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class Participant:
    """行业参与者。

    Attributes:
        name: 公司名称
        symbol: 公司股票代码
        market_share: 市场份额（0-1）
        share_basis: 份额计算基础（SHARE_BASES 之一）
        share_source_ref: 份额来源证据 ID
        share_period: 份额对应期间
        role: 公司角色（COMPANY_ROLES 之一）
    """

    name: str = ""
    symbol: str = ""
    market_share: float | None = None
    share_basis: str = SHARE_BASIS_NONE
    share_source_ref: str = ""
    share_period: str = ""
    role: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "symbol": self.symbol,
            "market_share": self.market_share,
            "share_basis": self.share_basis,
            "share_source_ref": self.share_source_ref,
            "share_period": self.share_period,
            "role": self.role,
        }


@dataclass
class CompetitiveLandscape:
    """行业集中度、主要参与者和份额来源。

    硬门禁：member_of_theme 不进入市场份额、产品和竞争结论。
    若 member_of_theme_excluded=False，表示过滤不完整，调用方应阻断。

    Attributes:
        concentration_metric: 集中度指标类型（CONCENTRATION_METRICS 之一）
        concentration_value: 集中度值
        hhi: HHI 指数
        participants: 主要参与者列表
        share_source_refs: 份额来源证据 ID
        period: 期间
        member_of_theme_excluded: 是否已过滤 member_of_theme 来源
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    concentration_metric: str = ""
    concentration_value: float | None = None
    hhi: float | None = None
    participants: list[Participant] = field(default_factory=list)
    share_source_refs: list[str] = field(default_factory=list)
    period: str = ""
    member_of_theme_excluded: bool = True
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "concentration_metric": self.concentration_metric,
            "concentration_value": self.concentration_value,
            "hhi": self.hhi,
            "participants": _nested_dicts(self.participants),
            "share_source_refs": _text_items(self.share_source_refs),
            "period": self.period,
            "member_of_theme_excluded": self.member_of_theme_excluded,
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class TechnologyRoute:
    """技术路线、替代关系和认证壁垒。

    Attributes:
        name: 技术路线名（如 "TOPCon 电池"）
        status: 状态（TECH_STATUSES 之一）
        adoption_rate: 渗透率（0-1）
        alternatives: 替代技术列表
        certification_barriers: 认证壁垒列表
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    name: str = ""
    status: str = ""
    adoption_rate: float | None = None
    alternatives: list[str] = field(default_factory=list)
    certification_barriers: list[str] = field(default_factory=list)
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "status": self.status,
            "adoption_rate": self.adoption_rate,
            "alternatives": _text_items(self.alternatives),
            "certification_barriers": _text_items(self.certification_barriers),
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class BargainingPower:
    """上下游议价能力。

    Attributes:
        direction: 方向（BARGAINING_DIRECTIONS 之一）
        level: 等级（BARGAINING_LEVELS 之一）
        factors: 关键因素列表
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    direction: str = ""
    level: str = ""
    factors: list[str] = field(default_factory=list)
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "direction": self.direction,
            "level": self.level,
            "factors": _text_items(self.factors),
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class CapexCycle:
    """资本开支和自由现金流周期。

    Attributes:
        industry_capex: 行业资本开支
        capex_unit: 单位
        capex_currency: 币种
        fcf_cycle_phase: FCF 周期阶段（CAPEX_PHASES 之一）
        capex_to_revenue_ratio: 资本开支/收入比
        cycle_period_years: 周期长度（年）
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    industry_capex: float | None = None
    capex_unit: str = ""
    capex_currency: str = ""
    fcf_cycle_phase: str = ""
    capex_to_revenue_ratio: float | None = None
    cycle_period_years: int | None = None
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "industry_capex": self.industry_capex,
            "capex_unit": self.capex_unit,
            "capex_currency": self.capex_currency,
            "fcf_cycle_phase": self.fcf_cycle_phase,
            "capex_to_revenue_ratio": self.capex_to_revenue_ratio,
            "cycle_period_years": self.cycle_period_years,
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class LifecycleStage:
    """赛道生命周期和失效条件。

    硬门禁：赛道阶段必须有时间和证据。
    stage_evidence_id 和 stage_dated_at 缺一即触发 gap。

    Attributes:
        stage: 阶段（LIFECYCLE_STAGES 之一）
        stage_evidence_id: 阶段判定证据 ID（强制非空）
        stage_dated_at: 阶段判定时间 YYYY-MM-DD（强制非空）
        failure_conditions: 失效条件列表
        period: 期间
        source_refs: 来源证据
        available_at: 可见时点
        grounding_status: 可追溯状态
        note: 备注
    """

    stage: str = ""
    stage_evidence_id: str = ""
    stage_dated_at: str = ""
    failure_conditions: list[str] = field(default_factory=list)
    period: str = ""
    source_refs: list[str] = field(default_factory=list)
    available_at: str = ""
    grounding_status: str = "missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "stage": self.stage,
            "stage_evidence_id": self.stage_evidence_id,
            "stage_dated_at": self.stage_dated_at,
            "failure_conditions": _text_items(self.failure_conditions),
            "period": self.period,
            "source_refs": _text_items(self.source_refs),
            "available_at": self.available_at,
            "grounding_status": self.grounding_status,
            "note": self.note,
        }


@dataclass
class CompanyMapping:
    """公司映射证据。

    硬门禁：公司映射证据为空时显示"暂无本地证据"。
    通过 has_local_evidence 标记 + local_evidence_display() 方法访问。

    Attributes:
        symbol: 公司股票代码
        industry_role: 公司角色（COMPANY_ROLES 之一）
        exposure_to_industry: 行业敞口（0-1）
        evidence_ids: 本地证据 ID 列表
        has_local_evidence: 是否有本地证据
        period: 期间
        note: 备注
    """

    symbol: str = ""
    industry_role: str = ""
    exposure_to_industry: float | None = None
    evidence_ids: list[str] = field(default_factory=list)
    has_local_evidence: bool = False
    period: str = ""
    note: str = ""

    def local_evidence_display(self) -> str:
        """返回本地证据展示字符串。

        若 has_local_evidence=False 或 evidence_ids 为空，返回 NO_LOCAL_EVIDENCE 常量。

        Returns:
            本地证据展示字符串或 NO_LOCAL_EVIDENCE
        """
        evidence_ids = _text_items(self.evidence_ids)
        if not self.has_local_evidence or not evidence_ids:
            return NO_LOCAL_EVIDENCE
        return ", ".join(evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "industry_role": self.industry_role,
            "exposure_to_industry": self.exposure_to_industry,
            "evidence_ids": _text_items(self.evidence_ids),
            "has_local_evidence": self.has_local_evidence,
            "local_evidence_display": self.local_evidence_display(),
            "period": self.period,
            "note": self.note,
        }


@dataclass
class IndustryGap:
    """缺 blocker 输入时的 gap 记录。

    Attributes:
        field_name: 缺失字段名
        reason: 缺失原因
        blocker_kind: blocker 类型（GAP_KIND_* 之一）
        severity: 严重度（GAP_SEVERITIES 之一）
        note: 备注
    """

    field_name: str = ""
    reason: str = ""
    blocker_kind: str = GAP_KIND_MISSING
    severity: str = GAP_SEVERITY_BLOCKER
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "field_name": self.field_name,
            "reason": self.reason,
            "blocker_kind": self.blocker_kind,
            "severity": self.severity,
            "note": self.note,
        }


@dataclass
class IndustryProfile:
    """行业画像（顶层聚合）。

    Attributes:
        industry_name: 行业名（如 "光伏电池"）
        industry_code: 行业代码
        code_system: 代码体系（INDUSTRY_CODE_SYSTEMS 之一）
        period: 画像基准期间 YYYY-MM-DD
        market_sizing: TAM/SAM/SOM
        demand_drivers: 需求驱动列表
        supply_profile: 供给画像
        price_cost_curve: 价格成本曲线
        inventory_cycle: 库存周期
        competitive_landscape: 竞争格局
        technology_routes: 技术路线列表
        bargaining_powers: 议价能力列表
        capex_cycle: 资本开支周期
        lifecycle_stage: 生命周期阶段
        company_mappings: 公司映射列表
        gaps: gap 列表
        source_trace_id: 来源追溯 ID
        schema_version: 契约版本
    """

    industry_name: str = ""
    industry_code: str = ""
    code_system: str = INDUSTRY_CODE_SW
    period: str = ""
    market_sizing: MarketSizing | None = None
    demand_drivers: list[DemandDriver] = field(default_factory=list)
    supply_profile: SupplyProfile | None = None
    price_cost_curve: PriceCostCurve | None = None
    inventory_cycle: InventoryCycle | None = None
    competitive_landscape: CompetitiveLandscape | None = None
    technology_routes: list[TechnologyRoute] = field(default_factory=list)
    bargaining_powers: list[BargainingPower] = field(default_factory=list)
    capex_cycle: CapexCycle | None = None
    lifecycle_stage: LifecycleStage | None = None
    company_mappings: list[CompanyMapping] = field(default_factory=list)
    gaps: list[IndustryGap] = field(default_factory=list)
    source_trace_id: str = ""
    schema_version: str = "industry_v1"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "industry_name": self.industry_name,
            "industry_code": self.industry_code,
            "code_system": self.code_system,
            "period": self.period,
            "market_sizing": _nested_dict(self.market_sizing),
            "demand_drivers": _nested_dicts(self.demand_drivers),
            "supply_profile": _nested_dict(self.supply_profile),
            "price_cost_curve": _nested_dict(self.price_cost_curve),
            "inventory_cycle": _nested_dict(self.inventory_cycle),
            "competitive_landscape": _nested_dict(self.competitive_landscape),
            "technology_routes": _nested_dicts(self.technology_routes),
            "bargaining_powers": _nested_dicts(self.bargaining_powers),
            "capex_cycle": _nested_dict(self.capex_cycle),
            "lifecycle_stage": _nested_dict(self.lifecycle_stage),
            "company_mappings": _nested_dicts(self.company_mappings),
            "gaps": _nested_dicts(self.gaps),
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------


def validate_market_sizing_method(method: str) -> None:
    """校验市场测算方法合法性。

    Args:
        method: 方法字符串

    Raises:
        ValueError: 当 method 不在 MARKET_SIZING_METHODS 中
    """
    _validate_choice(method, "market_sizing method", MARKET_SIZING_METHODS)


def validate_demand_direction(direction: str) -> None:
    """校验需求驱动方向合法性。

    Args:
        direction: 方向字符串

    Raises:
        ValueError: 当 direction 不在 DEMAND_DIRECTIONS 中
    """
    _validate_choice(direction, "demand direction", DEMAND_DIRECTIONS)


def validate_supply_phase(phase: str) -> None:
    """校验供给扩产阶段合法性。

    Args:
        phase: 阶段字符串

    Raises:
        ValueError: 当 phase 不在 SUPPLY_PHASES 中
    """
    _validate_choice(phase, "supply phase", SUPPLY_PHASES)


def validate_price_trend(trend: str) -> None:
    """校验价格趋势合法性。

    Args:
        trend: 趋势字符串

    Raises:
        ValueError: 当 trend 不在 PRICE_TRENDS 中
    """
    _validate_choice(trend, "price trend", PRICE_TRENDS)


def validate_inventory_trend(trend: str) -> None:
    """校验库存趋势合法性。

    Args:
        trend: 趋势字符串

    Raises:
        ValueError: 当 trend 不在 INVENTORY_TRENDS 中
    """
    _validate_choice(trend, "inventory trend", INVENTORY_TRENDS)


def validate_share_basis(basis: str) -> None:
    """校验份额基础合法性。

    Args:
        basis: 基础字符串

    Raises:
        ValueError: 当 basis 不在 SHARE_BASES 中
    """
    _validate_choice(basis, "share basis", SHARE_BASES)


def validate_concentration_metric(metric: str) -> None:
    """校验集中度指标合法性。

    Args:
        metric: 指标字符串

    Raises:
        ValueError: 当 metric 不在 CONCENTRATION_METRICS 中
    """
    _validate_choice(metric, "concentration metric", CONCENTRATION_METRICS)


def validate_tech_status(status: str) -> None:
    """校验技术状态合法性。

    Args:
        status: 状态字符串

    Raises:
        ValueError: 当 status 不在 TECH_STATUSES 中
    """
    _validate_choice(status, "tech status", TECH_STATUSES)


def validate_bargaining_direction(direction: str) -> None:
    """校验议价方向合法性。

    Args:
        direction: 方向字符串

    Raises:
        ValueError: 当 direction 不在 BARGAINING_DIRECTIONS 中
    """
    _validate_choice(direction, "bargaining direction", BARGAINING_DIRECTIONS)


def validate_bargaining_level(level: str) -> None:
    """校验议价等级合法性。

    Args:
        level: 等级字符串

    Raises:
        ValueError: 当 level 不在 BARGAINING_LEVELS 中
    """
    _validate_choice(level, "bargaining level", BARGAINING_LEVELS)


def validate_capex_phase(phase: str) -> None:
    """校验资本开支阶段合法性。

    Args:
        phase: 阶段字符串

    Raises:
        ValueError: 当 phase 不在 CAPEX_PHASES 中
    """
    _validate_choice(phase, "capex phase", CAPEX_PHASES)


def validate_lifecycle_stage(stage: str) -> None:
    """校验生命周期阶段合法性。

    Args:
        stage: 阶段字符串

    Raises:
        ValueError: 当 stage 不在 LIFECYCLE_STAGES 中
    """
    _validate_choice(stage, "lifecycle stage", LIFECYCLE_STAGES)


def validate_company_role(role: str) -> None:
    """校验公司角色合法性。

    Args:
        role: 角色字符串

    Raises:
        ValueError: 当 role 不在 COMPANY_ROLES 中
    """
    _validate_choice(role, "company role", COMPANY_ROLES)


def validate_industry_code_system(system: str) -> None:
    """校验行业代码体系合法性。

    Args:
        system: 体系字符串

    Raises:
        ValueError: 当 system 不在 INDUSTRY_CODE_SYSTEMS 中
    """
    _validate_choice(system, "industry code system", INDUSTRY_CODE_SYSTEMS)
