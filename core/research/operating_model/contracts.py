"""P1.3 产品与分部经营驱动模型契约。

按 P1 计划 §5 Task P1.3 至少覆盖 8 类驱动变量：
1. 市场规模、渗透率、单位价值量 (market_size / penetration / unit_value)
2. 公司份额 (market_share)
3. 名义产能、有效产能、投产和爬坡日期 (nameplate_capacity / effective_capacity / ramp_up_date)
4. 利用率、良率、销量 (utilization_rate / yield_rate / sales_volume)
5. ASP、价格传导、单位成本 (asp / price_passthrough / unit_cost)
6. 产品收入、分部收入暴露 (product_revenue / segment_revenue_exposure)
7. 毛利率、费用率、税率 (gross_margin / expense_ratio / tax_rate)
8. 股本和稀释因素 (share_count / dilution_factor)

硬门禁：
- 历史观察值不得自动成为未来假设（observation_kind 区分 historical_disclosed / forward_assumption）
- 定性"提升/增长"不能转成数值幅度（ForwardAssumption 必须有 source_ref，禁止定性词转数值）
- 多产品共享收入基数必须检测重复计算（detect_duplicate_revenue_base）
- 单位、币种和期间不一致必须阻断（OperatingModel.validate_consistency）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 商业模式（P1 §5 Task P1.3 验收点：制造、软件/服务、周期品）
BUSINESS_MODEL_MANUFACTURING: str = "manufacturing"
BUSINESS_MODEL_SOFTWARE_SERVICE: str = "software_service"
BUSINESS_MODEL_CYCLICAL: str = "cyclical"

BUSINESS_MODELS: tuple[str, ...] = (
    BUSINESS_MODEL_MANUFACTURING,
    BUSINESS_MODEL_SOFTWARE_SERVICE,
    BUSINESS_MODEL_CYCLICAL,
)


# 驱动变量类型（8 类，覆盖 P1 计划要求）
DRIVER_KIND_MARKET_SIZE: str = "market_size"
DRIVER_KIND_PENETRATION: str = "penetration"
DRIVER_KIND_UNIT_VALUE: str = "unit_value"
DRIVER_KIND_MARKET_SHARE: str = "market_share"
DRIVER_KIND_NAMEPLATE_CAPACITY: str = "nameplate_capacity"
DRIVER_KIND_EFFECTIVE_CAPACITY: str = "effective_capacity"
DRIVER_KIND_RAMP_UP_DATE: str = "ramp_up_date"
DRIVER_KIND_UTILIZATION_RATE: str = "utilization_rate"
DRIVER_KIND_YIELD_RATE: str = "yield_rate"
DRIVER_KIND_SALES_VOLUME: str = "sales_volume"
DRIVER_KIND_ASP: str = "asp"
DRIVER_KIND_PRICE_PASSTHROUGH: str = "price_passthrough"
DRIVER_KIND_UNIT_COST: str = "unit_cost"
DRIVER_KIND_PRODUCT_REVENUE: str = "product_revenue"
DRIVER_KIND_SEGMENT_REVENUE_EXPOSURE: str = "segment_revenue_exposure"
DRIVER_KIND_GROSS_MARGIN: str = "gross_margin"
DRIVER_KIND_EXPENSE_RATIO: str = "expense_ratio"
DRIVER_KIND_TAX_RATE: str = "tax_rate"
DRIVER_KIND_SHARE_COUNT: str = "share_count"
DRIVER_KIND_DILUTION_FACTOR: str = "dilution_factor"

DRIVER_KINDS: tuple[str, ...] = (
    DRIVER_KIND_MARKET_SIZE,
    DRIVER_KIND_PENETRATION,
    DRIVER_KIND_UNIT_VALUE,
    DRIVER_KIND_MARKET_SHARE,
    DRIVER_KIND_NAMEPLATE_CAPACITY,
    DRIVER_KIND_EFFECTIVE_CAPACITY,
    DRIVER_KIND_RAMP_UP_DATE,
    DRIVER_KIND_UTILIZATION_RATE,
    DRIVER_KIND_YIELD_RATE,
    DRIVER_KIND_SALES_VOLUME,
    DRIVER_KIND_ASP,
    DRIVER_KIND_PRICE_PASSTHROUGH,
    DRIVER_KIND_UNIT_COST,
    DRIVER_KIND_PRODUCT_REVENUE,
    DRIVER_KIND_SEGMENT_REVENUE_EXPOSURE,
    DRIVER_KIND_GROSS_MARGIN,
    DRIVER_KIND_EXPENSE_RATIO,
    DRIVER_KIND_TAX_RATE,
    DRIVER_KIND_SHARE_COUNT,
    DRIVER_KIND_DILUTION_FACTOR,
)

# 别名：兼容 DriverKind 类型表达
DriverKind = str


# 观察种类：历史事实 vs 前瞻假设
OBSERVATION_KIND_HISTORICAL: str = "historical_disclosed"
OBSERVATION_KIND_FORWARD: str = "forward_assumption"

OBSERVATION_KINDS: tuple[str, ...] = (
    OBSERVATION_KIND_HISTORICAL,
    OBSERVATION_KIND_FORWARD,
)


# 定性词黑名单：禁止从定性词转数值幅度
QUALITATIVE_WORDS_FORBIDDEN: tuple[str, ...] = (
    "提升", "增长", "提高", "扩大", "加速", "改善", "下滑", "减少",
    "显著", "大幅", "适度", "稳健", "继续",
)


@dataclass
class OperatingDriver:
    """单一驱动变量。

    每个驱动必须带 name/kind/value/unit/currency/period/source_ref；
    缺任一项则 grounding_status="missing"，调用方必须阻断使用。

    Attributes:
        name: 驱动名（如 "EV 电池销量"）
        kind: 驱动类型（DRIVER_KINDS 之一）
        value: 数值；缺失为 None
        unit: 单位（如 "GWh"、"元/吨"、"%"）
        currency: 币种（金额类必填，比率类为空）
        period: 期间 YYYY-MM-DD
        source_ref: 来源引用（claim_id 或 evidence_id）
        observation_kind: historical_disclosed / forward_assumption
        grounding_status: 可追溯状态（与 P0 contracts 对齐）
        reported_value: 原始披露值（未标准化）
        segment_name: 所属分部名（产品/业务线）
        note: 备注
    """

    name: str = ""
    kind: str = ""
    value: float | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    source_ref: str = ""
    observation_kind: str = OBSERVATION_KIND_HISTORICAL
    grounding_status: str = "missing"
    reported_value: float | None = None
    segment_name: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "source_ref": self.source_ref,
            "observation_kind": self.observation_kind,
            "grounding_status": self.grounding_status,
            "reported_value": self.reported_value,
            "segment_name": self.segment_name,
            "note": self.note,
        }


@dataclass
class ForwardAssumption:
    """前瞻假设条目。

    按硬门禁「历史观察值不得自动成为未来假设」与
    「定性"提升/增长"不能转成数值幅度」：
    - 必须有 source_ref（来源证据）
    - assumption_text 不得仅含定性词（如"显著提升"）
    - value 必须有明确数值或为 None（gap）

    Attributes:
        name: 假设名
        kind: 驱动类型（DRIVER_KINDS 之一）
        value: 假设数值；缺数据为 None（输出为 gap）
        unit: 单位
        currency: 币种
        period: 假设生效期间 YYYY-MM-DD
        source_ref: 来源引用（强制非空）
        assumption_text: 假设文本（不得仅含定性词）
        basis: 假设基础（disclosed_forward_plan / industry_research / management_guidance）
        note: 备注
    """

    name: str = ""
    kind: str = ""
    value: float | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    source_ref: str = ""
    assumption_text: str = ""
    basis: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "source_ref": self.source_ref,
            "assumption_text": self.assumption_text,
            "basis": self.basis,
            "note": self.note,
        }


@dataclass
class OperatingGap:
    """缺 blocker 输入时的 gap 记录。

    缺 blocker 时 driver_builder 必须输出 gap，不输出精确预测。

    Attributes:
        name: 缺失驱动名（如 "EV 电池销量 2025E"）
        kind: 缺失驱动类型
        period: 缺失期间
        reason: 缺失原因（如 "no_forward_evidence"）
        blocker_kind: blocker 类型（forward_evidence_missing / qualitative_only / unit_mismatch）
        note: 备注
    """

    name: str = ""
    kind: str = ""
    period: str = ""
    reason: str = ""
    blocker_kind: str = "forward_evidence_missing"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "period": self.period,
            "reason": self.reason,
            "blocker_kind": self.blocker_kind,
            "note": self.note,
        }


@dataclass
class Segment:
    """业务分部。

    Attributes:
        segment_name: 分部名（如 "动力电池"、"储能系统"）
        products: 产品列表
        drivers: 该分部下的驱动列表
    """

    segment_name: str = ""
    products: list[str] = field(default_factory=list)
    drivers: list[OperatingDriver] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "segment_name": self.segment_name,
            "products": list(self.products),
            "drivers": [d.to_dict() for d in self.drivers],
        }


@dataclass
class OperatingModel:
    """公司经营驱动模型。

    Attributes:
        symbol: 公司股票代码
        business_model: 商业模式（BUSINESS_MODELS 之一）
        period: 模型基准期间 YYYY-MM-DD
        currency: 模型统一币种
        unit: 模型统一单位（金额类）
        segments: 业务分部列表
        drivers: 公司级驱动列表（不分部的，如总股本）
        assumptions: 前瞻假设列表
        gaps: 缺 blocker 的 gap 列表
        duplicate_revenue_warnings: 多产品共享收入基数的告警
        consistency_errors: 单位/币种/期间不一致的错误
        source_trace_id: 模型来源追溯 ID
        schema_version: 契约版本
    """

    symbol: str = ""
    business_model: str = BUSINESS_MODEL_MANUFACTURING
    period: str = ""
    currency: str = "CNY"
    unit: str = "元"
    segments: list[Segment] = field(default_factory=list)
    drivers: list[OperatingDriver] = field(default_factory=list)
    assumptions: list[ForwardAssumption] = field(default_factory=list)
    gaps: list[OperatingGap] = field(default_factory=list)
    duplicate_revenue_warnings: list[str] = field(default_factory=list)
    consistency_errors: list[str] = field(default_factory=list)
    source_trace_id: str = ""
    schema_version: str = "operating_model_v1"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "business_model": self.business_model,
            "period": self.period,
            "currency": self.currency,
            "unit": self.unit,
            "segments": [s.to_dict() for s in self.segments],
            "drivers": [d.to_dict() for d in self.drivers],
            "assumptions": [a.to_dict() for a in self.assumptions],
            "gaps": [g.to_dict() for g in self.gaps],
            "duplicate_revenue_warnings": list(self.duplicate_revenue_warnings),
            "consistency_errors": list(self.consistency_errors),
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }

    def all_drivers(self) -> list[OperatingDriver]:
        """返回所有驱动（分部级 + 公司级）。"""
        result = list(self.drivers)
        for seg in self.segments:
            result.extend(seg.drivers)
        return result

    def validate_consistency(self) -> list[str]:
        """检查单位、币种、期间一致性。

        Returns:
            错误消息列表；空列表表示一致
        """
        errors: list[str] = []
        # 同 kind 同 period 不同 unit 视为不一致
        seen: dict[tuple[str, str], str] = {}
        for driver in self.all_drivers():
            key = (driver.kind, driver.period)
            if key in seen and seen[key] != driver.unit:
                errors.append(
                    f"驱动 {driver.name} ({key[0]}/{key[1]}) unit 不一致: "
                    f"{seen[key]} vs {driver.unit}"
                )
            else:
                seen[key] = driver.unit
            # 金额类必须有 currency
            if driver.kind in (
                "market_size", "unit_value", "asp", "unit_cost",
                "product_revenue", "segment_revenue_exposure",
            ) and driver.value is not None and not driver.currency:
                errors.append(
                    f"金额类驱动 {driver.name} 缺 currency"
                )
        return errors


def validate_driver_kind(kind: str) -> None:
    """校验驱动类型合法性。

    Args:
        kind: 驱动类型字符串

    Raises:
        ValueError: 当 kind 不在 DRIVER_KINDS 中
    """
    if kind not in DRIVER_KINDS:
        raise ValueError(f"非法驱动类型: {kind}; 允许值: {DRIVER_KINDS}")


def validate_business_model(model: str) -> None:
    """校验商业模式合法性。

    Args:
        model: 商业模式字符串

    Raises:
        ValueError: 当 model 不在 BUSINESS_MODELS 中
    """
    if model not in BUSINESS_MODELS:
        raise ValueError(f"非法商业模式: {model}; 允许值: {BUSINESS_MODELS}")


def validate_observation_kind(kind: str) -> None:
    """校验观察种类合法性。

    Args:
        kind: 观察种类字符串

    Raises:
        ValueError: 当 kind 不在 OBSERVATION_KINDS 中
    """
    if kind not in OBSERVATION_KINDS:
        raise ValueError(f"非法 observation_kind: {kind}; 允许值: {OBSERVATION_KINDS}")
