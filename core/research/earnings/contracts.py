"""盈利映射层领域契约。

独立领域模型，不把财务模型塞进候选图。
所有假设必须包含：值/区间、单位、币种、期间、生效时点、数据版本、
来源 Claim/Evidence ID、抽取方式、置信区间、来源类型。
缺输入返回 insufficient_data + gaps，严禁默认比例或伪精确。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .interval import Interval


def utc_now() -> str:
    """UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的 ID。"""
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Serializable:
    """可序列化基类。"""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            result[f.name] = _serialize(value)
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None):
        allowed = {item.name for item in fields(cls)}  # type: ignore[arg-type]
        kwargs: dict[str, Any] = {}
        if not isinstance(payload, Mapping):
            payload = {}
        for key, value in payload.items():
            if key not in allowed:
                continue
            kwargs[key] = _deserialize(value, cls, key)
        return cls(**kwargs)


def _serialize(value: Any) -> Any:
    if isinstance(value, Interval):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _serialize(v) for k, v in value.items()}
    return value


# 嵌套类型注册表：字段名 → 目标 Serializable 子类
# 用于 from_dict 时把 dict 还原为对应的 dataclass 实例
_NESTED_TYPES: dict[str, type] = {}


def _register_nested(name: str, cls: type) -> None:
    _NESTED_TYPES[name] = cls


def _deserialize(value: Any, cls: type, field_name: str) -> Any:
    annotations = getattr(cls, "__annotations__", {})
    annotation = str(annotations.get(field_name, ""))

    # Interval 字段
    if "Interval" in annotation and "list" not in annotation.lower():
        if isinstance(value, Mapping):
            return Interval.from_dict(value)
        if isinstance(value, (int, float)):
            return Interval.point(float(value))
        return Interval.point(0.0)

    # 嵌套 Serializable 字段（ImpactAssumption 等）
    nested_cls = _NESTED_TYPES.get(field_name)
    if nested_cls is not None and isinstance(value, Mapping):
        return nested_cls.from_dict(value)

    # 列表字段
    if isinstance(value, list):
        result: list[Any] = []
        for v in value:
            if isinstance(v, Mapping):
                # 尝试根据字段名推断列表元素类型
                item_cls = _NESTED_TYPES.get(f"{field_name}[]")
                if item_cls is not None:
                    result.append(item_cls.from_dict(v))
                else:
                    result.append(v)
            else:
                result.append(v)
        return result

    return value


class EarningsStatus(str, Enum):
    """盈利测算结果状态。"""

    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    DEGRADED = "degraded"  # 部分输入缺失但仍有部分结果
    PARTIAL = "partial"  # 有占位符未计算


class DriverType(str, Enum):
    """盈利驱动类型。"""

    DEMAND_EXPANSION = "demand_expansion"  # 需求扩张
    PRICE_INCREASE = "price_increase"  # 提价
    CAPACITY_EXPANSION = "capacity_expansion"  # 产能扩张
    COST_CHANGE = "cost_change"  # 成本变动
    MARKET_SHARE_GAIN = "market_share_gain"  # 份额提升
    PRODUCT_MIX = "product_mix"  # 产品结构升级


class EarningsScenarioKind(str, Enum):
    """情景类型。"""

    BEARISH = "bearish"
    BASELINE = "baseline"
    BULLISH = "bullish"


class RevenueExposureSource(str, Enum):
    """收入敞口来源类型，只接受以下三种。"""

    FINANCIAL_SEGMENT = "financial_segment"  # 财报分部
    COMPANY_DISCLOSURE = "company_disclosure"  # 公司披露
    MANUAL_AUDIT = "manual_audit"  # 人工审核


class AssumptionSourceType(str, Enum):
    """假设来源类型。"""

    DISCLOSED = "disclosed"  # 公司/官方披露
    INFERRED = "inferred"  # 推断值
    USER_PROVIDED = "user_provided"  # 用户手工输入
    THIRD_PARTY = "third_party"  # 第三方数据


class UnitKind(str, Enum):
    """单位种类，用于单位换算。"""

    PIECES = "pieces"  # 件/个/台
    TONS = "tons"  # 吨
    KG = "kg"  # 千克
    WAN_PIECES = "wan_pieces"  # 万件
    YI_YUAN = "yi_yuan"  # 亿元
    WAN_YUAN = "wan_yuan"  # 万元
    YUAN = "yuan"  # 元
    PERCENT = "percent"  # 百分比
    RATIO = "ratio"  # 比例（0-1）
    UNKNOWN = "unknown"


# 单位换算系数表：(from_unit, to_unit) -> 乘数
# 只登记同维度换算，不做隐式跨维度换算
_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    (UnitKind.TONS.value, UnitKind.KG.value): 1000.0,
    (UnitKind.WAN_PIECES.value, UnitKind.PIECES.value): 10000.0,
    (UnitKind.YI_YUAN.value, UnitKind.WAN_YUAN.value): 10000.0,
    (UnitKind.WAN_YUAN.value, UnitKind.YUAN.value): 10000.0,
    (UnitKind.PERCENT.value, UnitKind.RATIO.value): 0.01,
}


def convert_unit(value: Interval, from_unit: str, to_unit: str) -> tuple[Interval, bool]:
    """单位换算，返回 (换算结果, 是否成功)。

    不做隐式跨维度换算；只支持同维度换算。
    """
    if from_unit == to_unit:
        return value, True
    key = (from_unit, to_unit)
    if key in _UNIT_CONVERSIONS:
        return value * _UNIT_CONVERSIONS[key], True
    reverse = (to_unit, from_unit)
    if reverse in _UNIT_CONVERSIONS:
        return value * (1.0 / _UNIT_CONVERSIONS[reverse]), True
    return value, False


@dataclass
class ImpactAssumption(Serializable):
    """可追溯的计算假设。

    Attributes:
        assumption_id: 假设 ID。
        variable_name: 变量名（用于敏感性矩阵和缺口定位）。
        value: 数值区间。
        unit: 单位（UnitKind）。
        currency: 币种（CNY/USD/HKD 等）。
        period: 适用期间（如 2026Q1 / 2026FY）。
        as_of: 数据生效时点（ISO）。
        data_version: 数据版本。
        source_claim_id: 来源 Claim ID。
        source_evidence_id: 来源 Evidence ID。
        extraction_method: 抽取方式（manual / regex / llm / structured_parse）。
        confidence: 置信区间 0-1。
        published_at: 来源发布时间（PIT 用）。
        retrieved_at: 采集时间（PIT 用）。
        source_kind: 来源种类。
        source_type: 来源类型（disclosed / inferred / user_provided / third_party）。
        available_at: 假设生效时点（与 as_of 同步）。
        expires_at: 假设过期时点（ISO，可选）。
        is_user_provided: 是否用户手工输入。
        is_inferred: 是否推断值。
        notes: 备注。
    """

    assumption_id: str = field(default_factory=lambda: new_id("asm"))
    name: str = ""  # 兼容旧 QuantitativeInput 命名；优先同步到 variable_name
    variable_name: str = ""
    value: Interval = field(default_factory=lambda: Interval.point(0.0))
    unit: str = UnitKind.UNKNOWN.value
    currency: str = "CNY"
    period: str = ""
    as_of: str = ""
    data_version: str = ""
    source_claim_id: str = ""
    source_evidence_id: str = ""
    extraction_method: str = "unknown"
    confidence: Interval = field(default_factory=lambda: Interval.point(0.5))
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)
    source_kind: str = "unknown"
    source_type: str = AssumptionSourceType.DISCLOSED.value
    available_at: str = ""
    expires_at: str = ""
    is_user_provided: bool = False
    is_inferred: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        self.assumption_id = self.assumption_id or new_id("asm")
        if self.name and not self.variable_name:
            self.variable_name = self.name
        if self.variable_name and not self.name:
            self.name = self.variable_name
        if self.available_at and not self.as_of:
            self.as_of = self.available_at
        if not self.available_at and self.as_of:
            self.available_at = self.as_of

    def is_complete(self) -> bool:
        """是否完整（非零值、有来源、有期间、有生效时点）。"""
        return (
            self.value is not None
            and bool(self.unit)
            and self.unit != UnitKind.UNKNOWN.value
            and bool(self.period)
            and bool(self.as_of)
            and bool(self.data_version)
            and bool(self.source_claim_id or self.source_evidence_id)
            and self.extraction_method != "unknown"
        )

    def has_source(self) -> bool:
        """是否有来源 Claim/Evidence。"""
        return bool(self.source_claim_id or self.source_evidence_id)


@dataclass
class EarningsDriver(Serializable):
    """盈利驱动：产品/业务、公司、期间、驱动类型和证据引用。

    Attributes:
        driver_id: 驱动 ID。
        company_symbol: 公司代码。
        company_name: 公司名称。
        product_name: 产品/业务名称。
        period: 适用期间。
        driver_type: 驱动类型（DriverType）。
        description: 驱动描述（来自证据，不含投资建议）。
        evidence_ids: 关联 Evidence ID 列表。
        claim_ids: 关联 Claim ID 列表。
        created_at: 创建时间。
    """

    driver_id: str = field(default_factory=lambda: new_id("drv"))
    company_symbol: str = ""
    company_name: str = ""
    product_name: str = ""
    period: str = ""
    driver_type: str = DriverType.DEMAND_EXPANSION.value
    description: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.driver_id = self.driver_id or new_id("drv")


@dataclass
class RevenueExposure(Serializable):
    """产品到公司收入敞口。

    只接受财报分部、公司披露或人工审核数据；
    valuation_bridge 的启发式结果只能标为 exploration_hypothesis，禁止进入本结构。

    Attributes:
        exposure_id: 敞口 ID。
        driver_id: 关联驱动 ID。
        company_symbol: 公司代码。
        product_name: 产品/业务名称。
        exposure_ratio: 收入敞口比例区间（0-1）。
        revenue_base: 该产品对应的公司收入基数（ImpactAssumption）。
        source_kind: 来源类型（必须为 financial_segment/company_disclosure/manual_audit）。
        source_claim_id: 来源 Claim ID。
        source_evidence_id: 来源 Evidence ID。
        as_of: 数据生效时点。
        period: 适用期间。
        is_exploration_hypothesis: 是否为探索假设（True 时禁止进入正式测算）。
    """

    exposure_id: str = field(default_factory=lambda: new_id("exp"))
    driver_id: str = ""
    company_symbol: str = ""
    product_name: str = ""
    exposure_ratio: Interval = field(default_factory=lambda: Interval.point(0.0))
    revenue_base: ImpactAssumption = field(default_factory=ImpactAssumption)
    source_kind: str = RevenueExposureSource.FINANCIAL_SEGMENT.value
    source_claim_id: str = ""
    source_evidence_id: str = ""
    as_of: str = ""
    period: str = ""
    is_exploration_hypothesis: bool = False
    data_version: str = ""
    confidence: Interval = field(default_factory=lambda: Interval.point(0.5))
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.exposure_id = self.exposure_id or new_id("exp")

    def is_eligible_for_formal(self) -> bool:
        """是否可用于正式盈利测算。"""
        if self.is_exploration_hypothesis:
            return False
        if self.source_kind not in {
            RevenueExposureSource.FINANCIAL_SEGMENT.value,
            RevenueExposureSource.COMPANY_DISCLOSURE.value,
            RevenueExposureSource.MANUAL_AUDIT.value,
        }:
            return False
        if not (self.source_claim_id or self.source_evidence_id):
            return False
        if not self.data_version or not self.as_of:
            return False
        return True


@dataclass
class DemandModel(Serializable):
    """市场需求模型：市场规模变化 × 单机用量/渗透率。

    Attributes:
        model_id: 模型 ID。
        driver_id: 关联驱动 ID。
        market_size: 市场规模（ImpactAssumption）。
        per_unit_usage: 单机用量或渗透率（ImpactAssumption）。
        market_growth_delta: 市场规模变化量（区间），可为负。
        market_growth_delta_input: 市场增长变化假设。
        market_growth_delta_kind: 必须显式声明 rate/absolute。
        new_demand: 新增需求（计算结果）。
    """

    model_id: str = field(default_factory=lambda: new_id("dm"))
    driver_id: str = ""
    market_size: ImpactAssumption = field(default_factory=ImpactAssumption)
    per_unit_usage: ImpactAssumption = field(default_factory=ImpactAssumption)
    market_growth_delta: Interval = field(default_factory=lambda: Interval.point(0.0))
    market_growth_delta_input: ImpactAssumption = field(default_factory=ImpactAssumption)
    # 必须显式声明 rate/absolute，禁止通过数值大小猜测语义。
    market_growth_delta_kind: str = ""
    new_demand: Interval = field(default_factory=lambda: Interval.point(0.0))

    def __post_init__(self) -> None:
        self.model_id = self.model_id or new_id("dm")


@dataclass
class CapacityModel(Serializable):
    """产能模型：名义产能、有效产能、利用率、良率、投产时间。

    Attributes:
        model_id: 模型 ID。
        driver_id: 关联驱动 ID。
        nameplate_capacity: 名义产能（ImpactAssumption）。
        effective_capacity: 有效产能（ImpactAssumption，可由名义×利用率推导）。
        utilization_rate: 产能利用率（ImpactAssumption，0-1）。
        yield_rate: 良率（ImpactAssumption，0-1）。
        ramp_date: 投产/达产时间。
        ramp_progress: 爬坡进度（0-1）。
    """

    model_id: str = field(default_factory=lambda: new_id("cm"))
    driver_id: str = ""
    nameplate_capacity: ImpactAssumption = field(default_factory=ImpactAssumption)
    effective_capacity: ImpactAssumption = field(default_factory=ImpactAssumption)
    utilization_rate: ImpactAssumption = field(default_factory=ImpactAssumption)
    yield_rate: ImpactAssumption = field(default_factory=ImpactAssumption)
    ramp_date: str = ""
    ramp_progress: Interval = field(default_factory=lambda: Interval.point(1.0))

    def __post_init__(self) -> None:
        self.model_id = self.model_id or new_id("cm")


@dataclass
class PricingModel(Serializable):
    """定价模型：ASP、价格传导、成本和适用期间。

    Attributes:
        model_id: 模型 ID。
        driver_id: 关联驱动 ID。
        asp: 平均售价（ImpactAssumption）。
        price_pass_through: 价格传导比例（ImpactAssumption，0-1，1=完全传导）。
        unit_cost: 单位成本（ImpactAssumption）。
        period: 适用期间。
        revenue_recognition_lag: 收入确认滞后（季度）。
    """

    model_id: str = field(default_factory=lambda: new_id("pm"))
    driver_id: str = ""
    asp: ImpactAssumption = field(default_factory=ImpactAssumption)
    price_pass_through: ImpactAssumption = field(default_factory=ImpactAssumption)
    unit_cost: ImpactAssumption = field(default_factory=ImpactAssumption)
    period: str = ""
    revenue_recognition_lag: Interval = field(default_factory=lambda: Interval.point(0.0))

    def __post_init__(self) -> None:
        self.model_id = self.model_id or new_id("pm")


@dataclass
class MarginModel(Serializable):
    """利润率模型：毛利率、费用率、税率和净利率。

    Attributes:
        model_id: 模型 ID。
        driver_id: 关联驱动 ID。
        gross_margin: 增量毛利率（ImpactAssumption，0-1）。
        expense_ratio: 增量费用率（ImpactAssumption，0-1）。
        tax_rate: 税率（ImpactAssumption，0-1）。
        net_margin: 净利率（计算结果）。
    """

    model_id: str = field(default_factory=lambda: new_id("mm"))
    driver_id: str = ""
    gross_margin: ImpactAssumption = field(default_factory=ImpactAssumption)
    expense_ratio: ImpactAssumption = field(default_factory=ImpactAssumption)
    tax_rate: ImpactAssumption = field(default_factory=ImpactAssumption)
    net_margin: Interval = field(default_factory=lambda: Interval.point(0.0))

    def __post_init__(self) -> None:
        self.model_id = self.model_id or new_id("mm")


@dataclass
class ShareCapitalVersion(Serializable):
    """股本版本：用于 point-in-time EPS 计算。

    未来股本版本在历史 as_of 下不可见。

    Attributes:
        capital_id: 股本版本 ID。
        company_symbol: 公司代码。
        period: 适用期间。
        shares_outstanding: 总股本（股，ImpactAssumption）。
        as_of: 生效时点。
        source_kind: 来源类型。
        source_claim_id: 来源 Claim ID。
        source_evidence_id: 来源 Evidence ID。
        published_at: 发布时间（PIT 用）。
        retrieved_at: 采集时间（PIT 用）。
    """

    capital_id: str = field(default_factory=lambda: new_id("cap"))
    company_symbol: str = ""
    period: str = ""
    shares_outstanding: ImpactAssumption = field(default_factory=ImpactAssumption)
    as_of: str = ""
    source_kind: str = "company_disclosure"
    source_claim_id: str = ""
    source_evidence_id: str = ""
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.capital_id = self.capital_id or new_id("cap")


@dataclass
class InputGap(Serializable):
    """输入缺口。

    Attributes:
        gap_id: 缺口 ID。
        driver_id: 关联驱动 ID。
        field_name: 缺失字段名。
        reason: 缺失原因。
        required_source_kind: 期望来源类型。
        severity: 严重程度（blocker / warning）。
        suggested_task: 建议的补全任务。
    """

    gap_id: str = field(default_factory=lambda: new_id("gap"))
    driver_id: str = ""
    field_name: str = ""
    reason: str = ""
    required_source_kind: str = ""
    severity: str = "blocker"
    suggested_task: str = ""

    def __post_init__(self) -> None:
        self.gap_id = self.gap_id or new_id("gap")


@dataclass
class SensitivityItem(Serializable):
    """敏感性矩阵单项。

    Attributes:
        input_name: 输入名称。
        base_value: 基准值（中点）。
        low_value: 悲观值。
        high_value: 乐观值。
        eps_impact_low: 对 EPS 的下界影响。
        eps_impact_high: 对 EPS 的上界影响。
        elasticity: 弹性系数（输入变化 1% 时 EPS 变化比例）。
    """

    input_name: str = ""
    base_value: float = 0.0
    low_value: float = 0.0
    high_value: float = 0.0
    eps_impact_low: float = 0.0
    eps_impact_high: float = 0.0
    elasticity: float = 0.0


@dataclass
class EarningsScenario(Serializable):
    """单情景计算结果，包含全部中间变量。

    Attributes:
        scenario_id: 情景 ID。
        kind: 情景类型（bearish/base/bullish）。
        demand_growth: 市场需求变化。
        company_share: 公司份额。
        capacity: 有效产能。
        utilization: 产能利用率。
        yield_rate: 良率。
        asp: 平均售价。
        unit_cost: 单位成本。
        gross_margin: 增量毛利率。
        operating_expense: 增量费用。
        tax_rate: 税率。
        realization_probability: 兑现概率。
        realization_period: 兑现周期（季度）。
        incremental_revenue: 新增收入。
        incremental_gross_profit: 新增毛利。
        incremental_net_profit: 新增净利润。
        eps_impact: EPS 影响。
        input_overrides: 本情景相对基准的输入差异记录。
        formula_trace: 公式追踪（每步计算明细）。
        key_assumptions: 本情景使用的 assumption_id 列表。
    """

    scenario_id: str = field(default_factory=lambda: new_id("scn"))
    kind: str = EarningsScenarioKind.BASELINE.value
    demand_growth: Interval = field(default_factory=lambda: Interval.point(0.0))
    company_share: Interval = field(default_factory=lambda: Interval.point(0.0))
    capacity: Interval = field(default_factory=lambda: Interval.point(0.0))
    utilization: Interval = field(default_factory=lambda: Interval.point(0.0))
    yield_rate: Interval = field(default_factory=lambda: Interval.point(0.0))
    asp: Interval = field(default_factory=lambda: Interval.point(0.0))
    unit_cost: Interval = field(default_factory=lambda: Interval.point(0.0))
    gross_margin: Interval = field(default_factory=lambda: Interval.point(0.0))
    operating_expense: Interval = field(default_factory=lambda: Interval.point(0.0))
    tax_rate: Interval = field(default_factory=lambda: Interval.point(0.0))
    realization_probability: Interval = field(default_factory=lambda: Interval.point(1.0))
    realization_period: Interval = field(default_factory=lambda: Interval.point(0.0))
    incremental_revenue: Interval = field(default_factory=lambda: Interval.point(0.0))
    incremental_gross_profit: Interval = field(default_factory=lambda: Interval.point(0.0))
    incremental_net_profit: Interval = field(default_factory=lambda: Interval.point(0.0))
    eps_impact: Interval = field(default_factory=lambda: Interval.point(0.0))
    input_overrides: dict[str, Any] = field(default_factory=dict)
    formula_trace: dict[str, Any] = field(default_factory=dict)
    key_assumptions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.scenario_id = self.scenario_id or new_id("scn")


@dataclass
class EarningsImpact(Serializable):
    """盈利影响结果：新增收入、毛利、净利润、EPS 影响的区间与敏感性。

    Attributes:
        impact_id: 结果 ID。
        driver_id: 关联驱动 ID。
        symbol: 公司代码。
        company_name: 公司名称。
        product: 产品/业务名称。
        segment: 分部。
        period: 适用期间。
        as_of: 测算时点（PIT）。
        status: 状态（ok / insufficient_data / degraded / partial）。
        currency: 币种。
        incremental_revenue: 新增收入。
        incremental_gross_profit: 新增毛利。
        incremental_net_profit: 新增净利润。
        eps_impact: EPS 影响。
        contribution_to_existing_profit: 对现有利润的贡献占比。
        confidence_interval: 综合置信区间。
        scenarios: 三情景结果列表。
        key_assumptions: 主要假设列表。
        sensitivity: 敏感性矩阵。
        evidence_trace: 证据溯源链。
        available_at: 结果可用时点。
        model_version: 模型版本。
        falsification_conditions: 关键证伪条件。
        data_gaps: 数据缺口列表。
        placeholders: 已占位但未计算的模块。
        formula: 公式说明文本。
        base_inputs_snapshot: 基准输入快照（用于追溯）。
        created_at: 创建时间。
    """

    impact_id: str = field(default_factory=lambda: new_id("imp"))
    driver_id: str = ""
    symbol: str = ""
    company_name: str = ""
    product: str = ""
    segment: str = ""
    period: str = ""
    as_of: str = ""
    status: str = EarningsStatus.INSUFFICIENT_DATA.value
    currency: str = "CNY"
    incremental_revenue: Interval = field(default_factory=lambda: Interval.point(0.0))
    incremental_gross_profit: Interval = field(default_factory=lambda: Interval.point(0.0))
    incremental_net_profit: Interval = field(default_factory=lambda: Interval.point(0.0))
    eps_impact: Interval = field(default_factory=lambda: Interval.point(0.0))
    contribution_to_existing_profit: Interval = field(default_factory=lambda: Interval.point(0.0))
    confidence_interval: Interval = field(default_factory=lambda: Interval.point(0.0))
    scenarios: list[EarningsScenario] = field(default_factory=list)
    key_assumptions: list[ImpactAssumption] = field(default_factory=list)
    sensitivity: list[SensitivityItem] = field(default_factory=list)
    evidence_trace: list[dict[str, Any]] = field(default_factory=list)
    available_at: str = ""
    model_version: str = "earnings_impact_v1"
    data_version: str = ""  # 结果版本/审计标记，如 historical_missing_as_of
    falsification_conditions: list[str] = field(default_factory=list)
    data_gaps: list[InputGap] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    formula: str = ""
    base_inputs_snapshot: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.impact_id = self.impact_id or new_id("imp")

    @property
    def gaps(self) -> list[InputGap]:
        """兼容旧名：测试与 API 使用 gaps 访问 data_gaps。"""
        return self.data_gaps


# 注册嵌套类型，供 from_dict 反序列化使用
_register_nested("revenue_base", ImpactAssumption)
_register_nested("market_size", ImpactAssumption)
_register_nested("per_unit_usage", ImpactAssumption)
_register_nested("market_growth_delta_input", ImpactAssumption)
_register_nested("nameplate_capacity", ImpactAssumption)
_register_nested("effective_capacity", ImpactAssumption)
_register_nested("utilization_rate", ImpactAssumption)
_register_nested("yield_rate", ImpactAssumption)
_register_nested("asp", ImpactAssumption)
_register_nested("price_pass_through", ImpactAssumption)
_register_nested("unit_cost", ImpactAssumption)
_register_nested("gross_margin", ImpactAssumption)
_register_nested("expense_ratio", ImpactAssumption)
_register_nested("tax_rate", ImpactAssumption)
_register_nested("shares_outstanding", ImpactAssumption)
_register_nested("scenarios[]", EarningsScenario)
_register_nested("key_assumptions[]", ImpactAssumption)
_register_nested("sensitivity[]", SensitivityItem)
_register_nested("data_gaps[]", InputGap)


# 兼容别名：测试与 API 文档使用 Result 后缀命名，内部早期实现使用短名。
EarningsImpactResult = EarningsImpact
ScenarioResult = EarningsScenario
# QuantitativeInput 已统一为 ImpactAssumption；保留旧名以兼容 repository/测试。
QuantitativeInput = ImpactAssumption


# ============================================================================
# P1.4：三表联动盈利预测契约（IntegratedForecast）
# ============================================================================

class ForecastHorizon(str, Enum):
    """预测周期粒度。"""

    QUARTERLY = "quarterly"  # 季度
    ANNUAL = "annual"        # 年度


class ReconciliationStatus(str, Enum):
    """勾稽检查状态。"""

    OK = "ok"
    OUT_OF_TOLERANCE = "out_of_tolerance"
    NOT_APPLICABLE = "not_applicable"


# 三表勾稽容差：绝对差值低于此阈值视为平衡
RECONCILIATION_TOLERANCE: float = 0.01


@dataclass
class ReconciliationCheck(Serializable):
    """三表勾稽检查项。

    Attributes:
        check_id: 检查项 ID。
        name: 检查名（如 balance_sheet_identity / cash_flow_reconciliation / retained_earnings_linkage）。
        expected: 期望值（如 负债+权益）。
        actual: 实际值（如 资产）。
        difference: actual - expected 差额。
        tolerance: 容差；|difference| <= tolerance 视为通过。
        status: 状态（ok / out_of_tolerance / not_applicable）。
        note: 备注（公式或失败原因）。
    """

    check_id: str = field(default_factory=lambda: new_id("rc"))
    name: str = ""
    expected: float = 0.0
    actual: float = 0.0
    difference: float = 0.0
    tolerance: float = RECONCILIATION_TOLERANCE
    status: str = ReconciliationStatus.OK.value
    note: str = ""


@dataclass
class ForecastFinancials(Serializable):
    """单情景三表预测结果。

    包含利润表、资产负债表、现金流量表核心科目及每股指标。
    所有金额统一为 `unit` 指定单位；币种由 `currency` 指定。

    Attributes:
        period: 期间（如 2026Q1 / 2026FY）。
        currency: 币种。
        unit: 金额单位。
        revenue: 营业收入。
        operating_cost: 营业成本。
        gross_profit: 毛利。
        operating_profit: 营业利润。
        pre_tax_profit: 利润总额（税前利润）。
        income_tax: 所得税费用。
        net_profit: 净利润（含少数股东）。
        net_profit_parent: 归母净利润。
        minority_interest: 少数股东损益。
        total_assets: 资产总计。
        total_liabilities: 负债合计。
        total_equity: 权益合计。
        equity_parent: 归母权益。
        cash: 货币资金（期末）。
        accounts_receivable: 应收账款。
        inventory: 存货。
        fixed_assets: 固定资产净值。
        short_term_debt: 短期借款。
        long_term_debt: 长期借款。
        opening_cash: 期初现金。
        operating_cash_flow: 经营活动现金流净额。
        investing_cash_flow: 投资活动现金流净额。
        financing_cash_flow: 筹资活动现金流净额。
        closing_cash: 期末现金。
        capex: 资本开支。
        depreciation: 折旧与摊销。
        working_capital_change: 营运资本变动（正=净流出）。
        shares_outstanding: 总股本（股）。
        eps: 每股收益（归母）。
    """

    period: str = ""
    currency: str = "CNY"
    unit: str = "yuan"
    # 利润表
    revenue: float = 0.0
    operating_cost: float = 0.0
    gross_profit: float = 0.0
    operating_profit: float = 0.0
    pre_tax_profit: float = 0.0
    income_tax: float = 0.0
    net_profit: float = 0.0
    net_profit_parent: float = 0.0
    minority_interest: float = 0.0
    # 资产负债表
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    total_equity: float = 0.0
    equity_parent: float = 0.0
    cash: float = 0.0
    accounts_receivable: float = 0.0
    inventory: float = 0.0
    fixed_assets: float = 0.0
    short_term_debt: float = 0.0
    long_term_debt: float = 0.0
    # 现金流量表
    opening_cash: float = 0.0
    operating_cash_flow: float = 0.0
    investing_cash_flow: float = 0.0
    financing_cash_flow: float = 0.0
    closing_cash: float = 0.0
    capex: float = 0.0
    depreciation: float = 0.0
    working_capital_change: float = 0.0
    # 每股指标
    shares_outstanding: float = 0.0
    eps: float = 0.0


@dataclass
class IntegratedForecast(Serializable):
    """三表联动盈利预测结果（P1 §4.3 契约）。

    覆盖契约字段：
    forecast_id / symbol / as_of / horizon / scenario /
    income_statement / balance_sheet / cash_flow_statement /
    segment_forecasts / assumptions / reconciliation_checks /
    forecast_uncertainty / model_version / created_at

    Attributes:
        forecast_id: 预测 ID。
        symbol: 公司代码。
        as_of: 测算时点（PIT）。
        period: 预测期间（2026Q1 / 2026FY）。
        horizon: 周期粒度（quarterly / annual）。
        scenario: 情景类型（bearish / baseline / bullish）。
        financials: 三表数据。
        segment_forecasts: 各产品/分部明细（每个 dict 含 product/period/revenue/gross_profit 等）。
        assumptions: 关键假设列表（带来源追溯）。
        reconciliation_checks: 三表勾稽检查项。
        forecast_uncertainty: 预测不确定性区间。
        model_version: 模型版本号。
        forecast_version: 预测版本号（同 model_version 下递增；历史版本不可覆盖）。
        data_version: 数据版本标记。
        data_gaps: 缺口列表。
        formula_trace: 公式轨迹（含每步计算明细）。
        input_snapshot: 完整输入快照（用于幂等追溯）。
        driver_ids: 关联的 driver_id 列表。
        created_at: 创建时间。
    """

    forecast_id: str = field(default_factory=lambda: new_id("fcst"))
    symbol: str = ""
    as_of: str = ""
    period: str = ""
    horizon: str = ForecastHorizon.ANNUAL.value
    scenario: str = EarningsScenarioKind.BASELINE.value
    financials: ForecastFinancials = field(default_factory=ForecastFinancials)
    segment_forecasts: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[ImpactAssumption] = field(default_factory=list)
    reconciliation_checks: list[ReconciliationCheck] = field(default_factory=list)
    forecast_uncertainty: Interval = field(default_factory=lambda: Interval.point(0.0))
    model_version: str = "integrated_forecast_v1"
    forecast_version: int = 1
    data_version: str = ""
    data_gaps: list[InputGap] = field(default_factory=list)
    formula_trace: dict[str, Any] = field(default_factory=dict)
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    driver_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)


# 注册嵌套类型，供 from_dict 反序列化使用
_register_nested("financials", ForecastFinancials)
_register_nested("reconciliation_checks[]", ReconciliationCheck)
_register_nested("assumptions[]", ImpactAssumption)
_register_nested("data_gaps[]", InputGap)
