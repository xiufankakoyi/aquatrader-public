"""P3.4 跨域因果与宏观情景系统契约。

按 P3 计划 Task P3.4：
- 宏观变量通过显式传导路径影响行业和公司驱动
- 每条传导路径包含方向、时滞、适用范围和证据
- 相关性不能自动声明因果；缺乏证据的路径不得进入压力测试
- 输出财务、现金流、估值和风险变化，不输出交易动作

契约字段（按 P3.4 §「契约字段」）：
- MacroVariable: name, value, unit, as_of, source_ref
- TransmissionPath: source_variable, target_driver, direction (+/-),
                    lag_months, applicable_industries, evidence_refs, confidence
- CompanyStressScenario: scenario_id, symbol, macro_shocks,
                         driver_impacts, forecast_before, forecast_after, risk_delta

边界：
- 不输出买入 / 卖出 / 仓位 / 止盈 / 止损等交易动作
- 不凭空编造公司业务或证据来源；证据为空时必须降级
- 相关性观察必须显式标记 correlation_only，不能升级为因果路径
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# AGENTS.md 合规：禁止出现的交易动作关键词
# ---------------------------------------------------------------------------

FORBIDDEN_TRADE_ACTION_KEYWORDS: tuple[str, ...] = (
    "买入",
    "卖出",
    "止盈",
    "止损",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "调仓",
)

NOTICE: str = (
    "宏观情景系统仅输出财务、现金流、估值和风险变化，"
    "不输出仓位或交易动作建议。"
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 宏观变量类别（按 P3.4 §「情景变量」5 类）
# ---------------------------------------------------------------------------

MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT: str = "rate_fx_inflation_credit"
MACRO_CATEGORY_COMMODITY_ENERGY: str = "commodity_energy"
MACRO_CATEGORY_DEMAND_CAPEX: str = "demand_capex"
MACRO_CATEGORY_TRADE_POLICY_SUPPLY: str = "trade_policy_supply"
MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL: str = "industry_price_inventory_util"

MACRO_CATEGORIES: tuple[str, ...] = (
    MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT,
    MACRO_CATEGORY_COMMODITY_ENERGY,
    MACRO_CATEGORY_DEMAND_CAPEX,
    MACRO_CATEGORY_TRADE_POLICY_SUPPLY,
    MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL,
)

MACRO_CATEGORY_LABELS: dict[str, str] = {
    MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT: "利率、汇率、通胀和信用条件",
    MACRO_CATEGORY_COMMODITY_ENERGY: "商品价格和能源成本",
    MACRO_CATEGORY_DEMAND_CAPEX: "终端需求和资本开支",
    MACRO_CATEGORY_TRADE_POLICY_SUPPLY: "贸易、关税、政策和供应链限制",
    MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL: "行业价格、库存和利用率",
}


# ---------------------------------------------------------------------------
# 传导方向
# ---------------------------------------------------------------------------

DIRECTION_POSITIVE: str = "+"
DIRECTION_NEGATIVE: str = "-"
DIRECTION_NEUTRAL: str = "0"

DIRECTIONS: tuple[str, ...] = (DIRECTION_POSITIVE, DIRECTION_NEGATIVE, DIRECTION_NEUTRAL)

DIRECTION_SIGNS: dict[str, float] = {
    DIRECTION_POSITIVE: 1.0,
    DIRECTION_NEGATIVE: -1.0,
    DIRECTION_NEUTRAL: 0.0,
}


# ---------------------------------------------------------------------------
# 目标驱动（公司经营驱动；传导路径终点）
# ---------------------------------------------------------------------------

TARGET_DRIVER_REVENUE: str = "revenue"
TARGET_DRIVER_GROSS_MARGIN: str = "gross_margin"
TARGET_DRIVER_OPEX_RATIO: str = "opex_ratio"
TARGET_DRIVER_FCF_CONVERSION: str = "fcf_conversion"
TARGET_DRIVER_DISCOUNT_RATE: str = "discount_rate"
TARGET_DRIVER_VALUATION_MULTIPLE: str = "valuation_multiple"
TARGET_DRIVER_CAPEX: str = "capex"

TARGET_DRIVERS: tuple[str, ...] = (
    TARGET_DRIVER_REVENUE,
    TARGET_DRIVER_GROSS_MARGIN,
    TARGET_DRIVER_OPEX_RATIO,
    TARGET_DRIVER_FCF_CONVERSION,
    TARGET_DRIVER_DISCOUNT_RATE,
    TARGET_DRIVER_VALUATION_MULTIPLE,
    TARGET_DRIVER_CAPEX,
)

TARGET_DRIVER_LABELS: dict[str, str] = {
    TARGET_DRIVER_REVENUE: "营收",
    TARGET_DRIVER_GROSS_MARGIN: "毛利率",
    TARGET_DRIVER_OPEX_RATIO: "运营费用率",
    TARGET_DRIVER_FCF_CONVERSION: "自由现金流转化率",
    TARGET_DRIVER_DISCOUNT_RATE: "折现率",
    TARGET_DRIVER_VALUATION_MULTIPLE: "估值倍数",
    TARGET_DRIVER_CAPEX: "资本开支",
}


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ScenarioContractError(ValueError):
    """情景契约违规。"""


class CorrelationCausationError(ValueError):
    """相关性被错误声明为因果。"""


# ---------------------------------------------------------------------------
# 宏观变量
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroVariable:
    """宏观变量观测值。

    按 P3.4 §「契约字段」：name, value, unit, as_of, source_ref。

    Attributes:
        name: 变量名（如 ``policy_rate``、``cny_usd``）
        category: 所属类别（MACRO_CATEGORIES 之一）
        value: 观测值（绝对水平，如 5.25 表示 5.25%）
        unit: 单位（如 ``%``、``CNY/USD``、``USD/oz``）
        as_of: PIT 时点（YYYY-MM-DD）
        source_ref: 来源引用（数据源 ID 或文档链接）
    """

    name: str
    category: str
    value: float
    unit: str
    as_of: str
    source_ref: str

    def __post_init__(self) -> None:
        """校验必填字段。"""
        if not self.name:
            raise ScenarioContractError("MacroVariable.name 必填")
        if self.category not in MACRO_CATEGORIES:
            raise ScenarioContractError(
                f"MacroVariable.category 非法：{self.category}"
                f"（合法：{', '.join(MACRO_CATEGORIES)}）"
            )
        if not self.unit:
            raise ScenarioContractError("MacroVariable.unit 必填")
        if not self.as_of:
            raise ScenarioContractError("MacroVariable.as_of 必填")
        if not self.source_ref:
            raise ScenarioContractError(
                "MacroVariable.source_ref 必填；不得凭空编造来源"
            )
        try:
            parsed_value = _safe_float(self.value, default=math.nan)
            if not math.isfinite(parsed_value):
                raise ValueError("non-finite")
        except (TypeError, ValueError) as exc:
            raise ScenarioContractError(
                f"MacroVariable.value 必须为数值：{self.value}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "category": self.category,
            "category_label": MACRO_CATEGORY_LABELS.get(self.category, ""),
            "value": self.value,
            "unit": self.unit,
            "as_of": self.as_of,
            "source_ref": self.source_ref,
        }


# ---------------------------------------------------------------------------
# 宏观冲击（macro_shocks 字典的 value 类型）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MacroShock:
    """对宏观变量施加的冲击。

    shock_value 表示宏观变量的「变化量」（delta），而非绝对水平。
    例如政策利率 +50bp 记为 shock_value=0.50，单位 ``%``；
    人民币对美元贬值 5% 记为 shock_value=0.05，单位 ``fraction``。

    单位约定：
    - 当 unit 为 ``%`` 时，shock_value 直接以百分点表达（如 0.50 = 50bp）
    - 当 unit 为 ``fraction`` 时，shock_value 以小数表达（如 0.05 = 5%）
    - 传导计算时统一转换为「相对变化率」与 target_driver 对齐

    Attributes:
        variable: 被冲击的宏观变量
        shock_value: 冲击大小（delta）
        shock_unit: 冲击单位（``%`` / ``fraction`` / ``absolute``）
        rationale: 冲击依据（来源、假设或情景假设说明）
    """

    variable: MacroVariable
    shock_value: float
    shock_unit: str
    rationale: str = ""

    def __post_init__(self) -> None:
        """校验冲击单位。"""
        if not isinstance(self.variable, MacroVariable):
            raise ScenarioContractError("MacroShock.variable 必须是 MacroVariable")
        if not math.isfinite(_safe_float(self.shock_value, default=math.nan)):
            raise ScenarioContractError("MacroShock.shock_value 必须是有限数值")
        if self.shock_unit not in ("%", "fraction", "absolute"):
            raise ScenarioContractError(
                f"MacroShock.shock_unit 非法：{self.shock_unit}"
                "（合法：% / fraction / absolute）"
            )
        if any(kw in self.rationale for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
            raise ScenarioContractError(
                "MacroShock.rationale 含交易动作关键词"
                f"（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )

    def to_fractional_change(self) -> float:
        """将 shock 转换为对宏观变量的相对变化率。

        - ``%``：shock_value 以百分点表达；fractional = shock_value / variable.value
          （如政策利率 +50bp，variable.value=5.0，shock_value=0.50 → fractional=0.10）
        - ``fraction``：shock_value 已为相对变化率；直接返回
          （如人民币贬值 5%，shock_value=0.05 → fractional=0.05）
        - ``absolute``：shock_value 为绝对量（与 variable 同单位）；
          fractional = shock_value / variable.value

        Returns:
            float: 相对变化率（如 0.05 表示 +5%）
        """
        if self.shock_unit == "fraction":
            return _safe_float(self.shock_value)
        # % 与 absolute 都需要除以基线值（绝对水平）转为相对变化率
        base = max(abs(self.variable.value), 1e-9)
        return _safe_float(self.shock_value) / base

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "variable": self.variable.to_dict(),
            "shock_value": self.shock_value,
            "shock_unit": self.shock_unit,
            "rationale": self.rationale,
            "fractional_change": self.to_fractional_change(),
        }


# ---------------------------------------------------------------------------
# 传导路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransmissionPath:
    """宏观变量到公司驱动的传导路径。

    按 P3.4 §「契约字段」：
    - source_variable: 源宏观变量名（对应 MacroVariable.name）
    - target_driver: 目标驱动（TARGET_DRIVERS 之一）
    - direction: 方向（``+`` / ``-`` / ``0``）
    - lag_months: 时滞（月）；0 表示当期生效
    - applicable_industries: 适用行业列表；空列表表示全行业适用
    - evidence_refs: 证据 ID 列表（不得为空，否则视为相关性观察）
    - confidence: 置信度 [0, 1]

    相关性 ≠ 因果：
    - evidence_refs 为空时 correlation_only=True，禁止进入压力测试
    - confidence < CORRELATION_CONFIDENCE_THRESHOLD 时同样视为相关性观察
    - 调用方必须通过 ``assert_causal_path`` 显式校验

    Attributes:
        path_id: 路径 ID（确定性派生）
        description: 路径描述（可选）
        sensitivity: 灵敏度；target_driver 相对变化率 / 宏观变量相对变化率；
                     默认 1.0 表示单位弹性
        correlation_only: 是否仅为相关性观察（无证据或低置信度）
    """

    source_variable: str
    target_driver: str
    direction: str
    lag_months: int
    applicable_industries: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    description: str = ""
    sensitivity: float = 1.0
    path_id: str = ""
    correlation_only: bool = False

    #: 置信度阈值；低于此值视为相关性观察
    CORRELATION_CONFIDENCE_THRESHOLD: float = 0.40

    def __post_init__(self) -> None:
        """校验字段并派生 path_id / correlation_only。"""
        object.__setattr__(self, "applicable_industries", _string_tuple(self.applicable_industries))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))
        object.__setattr__(self, "lag_months", _safe_int(self.lag_months, default=-1))
        object.__setattr__(self, "confidence", _safe_float(self.confidence, default=math.nan))
        object.__setattr__(self, "sensitivity", _safe_float(self.sensitivity, default=math.nan))
        if not self.source_variable:
            raise ScenarioContractError("TransmissionPath.source_variable 必填")
        if self.target_driver not in TARGET_DRIVERS:
            raise ScenarioContractError(
                f"TransmissionPath.target_driver 非法：{self.target_driver}"
                f"（合法：{', '.join(TARGET_DRIVERS)}）"
            )
        if self.direction not in DIRECTIONS:
            raise ScenarioContractError(
                f"TransmissionPath.direction 非法：{self.direction}"
                f"（合法：{', '.join(DIRECTIONS)}）"
            )
        if self.lag_months < 0:
            raise ScenarioContractError(
                f"TransmissionPath.lag_months 不能为负：{self.lag_months}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ScenarioContractError(
                f"TransmissionPath.confidence 越界 [0,1]：{self.confidence}"
            )
        if self.sensitivity <= 0:
            raise ScenarioContractError(
                f"TransmissionPath.sensitivity 必须为正：{self.sensitivity}"
            )
        if any(
            kw in self.description for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        ):
            raise ScenarioContractError(
                "TransmissionPath.description 含交易动作关键词"
                f"（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )
        # 相关性观察判定：无证据 OR 置信度过低
        correlation_only = (
            len(self.evidence_refs) == 0
            or self.confidence < self.CORRELATION_CONFIDENCE_THRESHOLD
        )
        object.__setattr__(self, "correlation_only", correlation_only)
        if not self.path_id:
            object.__setattr__(
                self, "path_id", self._derive_path_id()
            )

    def _derive_path_id(self) -> str:
        """确定性路径 ID。"""
        raw = (
            f"{self.source_variable}|{self.target_driver}|{self.direction}|"
            f"{self.lag_months}|{','.join(sorted(self.applicable_industries))}|"
            f"{self.confidence:.4f}|{self.sensitivity:.4f}"
        )
        return f"path_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"

    def applies_to_industry(self, industry: str) -> bool:
        """判断路径是否适用于某行业。

        Args:
            industry: 行业名称

        Returns:
            bool: True 表示适用（空 applicable_industries 表示全行业适用）
        """
        if not self.applicable_industries:
            return True
        return industry in self.applicable_industries

    def sign(self) -> float:
        """返回方向符号（+1.0 / -1.0 / 0.0）。"""
        return DIRECTION_SIGNS[self.direction]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "path_id": self.path_id,
            "source_variable": self.source_variable,
            "target_driver": self.target_driver,
            "target_driver_label": TARGET_DRIVER_LABELS.get(self.target_driver, ""),
            "direction": self.direction,
            "lag_months": self.lag_months,
            "applicable_industries": list(self.applicable_industries),
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "description": self.description,
            "sensitivity": self.sensitivity,
            "correlation_only": self.correlation_only,
        }


# ---------------------------------------------------------------------------
# 驱动影响（driver_impacts 字典的 value 类型）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverImpact:
    """单一驱动的冲击影响。

    Attributes:
        driver: 目标驱动名（TARGET_DRIVERS 之一）
        path_id: 传导路径 ID
        source_variable: 源宏观变量名
        shock_fractional_change: 宏观变量相对变化率
        impact_pct: 驱动相对变化率（百分比；负值表示下降）
        lag_months: 路径时滞（月）
        confidence: 路径置信度
        evidence_refs: 证据引用
        note: 备注
    """

    driver: str
    path_id: str
    source_variable: str
    shock_fractional_change: float
    impact_pct: float
    lag_months: int
    confidence: float
    evidence_refs: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "shock_fractional_change", _safe_float(self.shock_fractional_change))
        object.__setattr__(self, "impact_pct", _safe_float(self.impact_pct))
        object.__setattr__(self, "lag_months", max(0, _safe_int(self.lag_months)))
        object.__setattr__(self, "confidence", max(0.0, min(1.0, _safe_float(self.confidence))))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "driver": self.driver,
            "driver_label": TARGET_DRIVER_LABELS.get(self.driver, ""),
            "path_id": self.path_id,
            "source_variable": self.source_variable,
            "shock_fractional_change": self.shock_fractional_change,
            "impact_pct": self.impact_pct,
            "lag_months": self.lag_months,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 财务快照（forecast_before / forecast_after）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastSnapshot:
    """财务预测快照。

    用于情景前 / 后对比。所有金额单位统一为「万元」，比率单位为小数。

    Attributes:
        revenue: 营收（万元）
        gross_margin: 毛利率（小数；如 0.35）
        opex_ratio: 运营费用率（小数；如 0.20）
        net_profit: 净利润（万元）
        fcf: 自由现金流（万元）
        discount_rate: 折现率（小数；如 0.10）
        valuation_multiple: 估值倍数（如 PE 20 倍）
        enterprise_value: 企业价值（万元）
    """

    revenue: float = 0.0
    gross_margin: float = 0.0
    opex_ratio: float = 0.0
    net_profit: float = 0.0
    fcf: float = 0.0
    discount_rate: float = 0.0
    valuation_multiple: float = 0.0
    enterprise_value: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "revenue", "gross_margin", "opex_ratio", "net_profit", "fcf",
            "discount_rate", "valuation_multiple", "enterprise_value",
        ):
            object.__setattr__(self, name, _safe_float(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "revenue": self.revenue,
            "gross_margin": self.gross_margin,
            "opex_ratio": self.opex_ratio,
            "net_profit": self.net_profit,
            "fcf": self.fcf,
            "discount_rate": self.discount_rate,
            "valuation_multiple": self.valuation_multiple,
            "enterprise_value": self.enterprise_value,
        }


# ---------------------------------------------------------------------------
# 风险变化（risk_delta）
# ---------------------------------------------------------------------------

RISK_LEVEL_LOW: str = "low"
RISK_LEVEL_MEDIUM: str = "medium"
RISK_LEVEL_HIGH: str = "high"
RISK_LEVEL_EXTREME: str = "extreme"

RISK_LEVELS: tuple[str, ...] = (
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_EXTREME,
)

RISK_LEVEL_SCORES: dict[str, float] = {
    RISK_LEVEL_LOW: 0.25,
    RISK_LEVEL_MEDIUM: 0.50,
    RISK_LEVEL_HIGH: 0.75,
    RISK_LEVEL_EXTREME: 1.00,
}


@dataclass(frozen=True)
class RiskDelta:
    """情景前后风险变化。

    Attributes:
        risk_level_before: 情景前风险等级
        risk_level_after: 情景后风险等级
        risk_score_before: 情景前风险分数 [0,1]
        risk_score_after: 情景后风险分数 [0,1]
        score_delta: 风险分数变化（after - before）
        escalated_drivers: 风险升级的驱动列表
        new_risk_factors: 情景新增的风险因素
    """

    risk_level_before: str
    risk_level_after: str
    risk_score_before: float
    risk_score_after: float
    score_delta: float
    escalated_drivers: tuple[str, ...] = ()
    new_risk_factors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验风险等级。"""
        object.__setattr__(self, "risk_score_before", max(0.0, min(1.0, _safe_float(self.risk_score_before))))
        object.__setattr__(self, "risk_score_after", max(0.0, min(1.0, _safe_float(self.risk_score_after))))
        object.__setattr__(self, "score_delta", _safe_float(self.score_delta))
        object.__setattr__(self, "escalated_drivers", _string_tuple(self.escalated_drivers))
        object.__setattr__(self, "new_risk_factors", _string_tuple(self.new_risk_factors))
        if self.risk_level_before not in RISK_LEVELS:
            raise ScenarioContractError(
                f"risk_level_before 非法：{self.risk_level_before}"
            )
        if self.risk_level_after not in RISK_LEVELS:
            raise ScenarioContractError(
                f"risk_level_after 非法：{self.risk_level_after}"
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "risk_level_before": self.risk_level_before,
            "risk_level_after": self.risk_level_after,
            "risk_score_before": self.risk_score_before,
            "risk_score_after": self.risk_score_after,
            "score_delta": self.score_delta,
            "escalated_drivers": list(self.escalated_drivers),
            "new_risk_factors": list(self.new_risk_factors),
        }


# ---------------------------------------------------------------------------
# 公司压力情景（最终输出）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyStressScenario:
    """公司压力测试情景。

    按 P3.4 §「契约字段」：
    - scenario_id: 情景 ID
    - symbol: 公司代码
    - macro_shocks: 宏观冲击列表
    - driver_impacts: 驱动影响列表
    - forecast_before: 情景前财务快照
    - forecast_after: 情景后财务快照
    - risk_delta: 风险变化

    Attributes:
        scenario_id: 情景 ID
        symbol: 公司代码
        industry: 公司行业
        as_of: PIT 时点
        scenario_name: 情景名称
        scenario_description: 情景描述
        macro_shocks: 宏观冲击列表（MacroShock）
        driver_impacts: 驱动影响列表（DriverImpact）
        forecast_before: 情景前财务快照
        forecast_after: 情景后财务快照
        revenue_impact_pct: 营收变化百分比
        profit_impact_pct: 净利润变化百分比
        cashflow_impact_pct: 自由现金流变化百分比
        valuation_impact_pct: 估值变化百分比
        risk_delta: 风险变化
        correlation_only_paths: 未能升级为因果的相关性观察路径 ID
        skipped_no_path: 未匹配到传导路径的宏观变量名
        evidence_refs: 涉及的证据汇总
        model_version: 模型版本
        created_at: 创建时间
        notice: 合规声明
    """

    scenario_id: str
    symbol: str
    industry: str
    as_of: str
    scenario_name: str
    scenario_description: str
    macro_shocks: tuple[MacroShock, ...]
    driver_impacts: tuple[DriverImpact, ...]
    forecast_before: ForecastSnapshot
    forecast_after: ForecastSnapshot
    revenue_impact_pct: float
    profit_impact_pct: float
    cashflow_impact_pct: float
    valuation_impact_pct: float
    risk_delta: RiskDelta
    correlation_only_paths: tuple[str, ...] = ()
    skipped_no_path: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    model_version: str = "v1"
    created_at: str = ""
    notice: str = NOTICE

    def __post_init__(self) -> None:
        """校验合规并填充默认值。"""
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now())
        object.__setattr__(self, "macro_shocks", tuple(item for item in self.macro_shocks if isinstance(item, MacroShock)))
        object.__setattr__(self, "driver_impacts", tuple(item for item in self.driver_impacts if isinstance(item, DriverImpact)))
        object.__setattr__(self, "correlation_only_paths", _string_tuple(self.correlation_only_paths))
        object.__setattr__(self, "skipped_no_path", _string_tuple(self.skipped_no_path))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))
        for name in ("revenue_impact_pct", "profit_impact_pct", "cashflow_impact_pct", "valuation_impact_pct"):
            object.__setattr__(self, name, _safe_float(getattr(self, name)))
        if any(
            kw in self.scenario_name for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        ) or any(
            kw in self.scenario_description
            for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        ):
            raise ScenarioContractError(
                "scenario_name / scenario_description 含交易动作关键词"
                f"（{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）"
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "scenario_id": self.scenario_id,
            "symbol": self.symbol,
            "industry": self.industry,
            "as_of": self.as_of,
            "scenario_name": self.scenario_name,
            "scenario_description": self.scenario_description,
            "macro_shocks": [s.to_dict() for s in self.macro_shocks],
            "driver_impacts": [d.to_dict() for d in self.driver_impacts],
            "forecast_before": self.forecast_before.to_dict(),
            "forecast_after": self.forecast_after.to_dict(),
            "revenue_impact_pct": self.revenue_impact_pct,
            "profit_impact_pct": self.profit_impact_pct,
            "cashflow_impact_pct": self.cashflow_impact_pct,
            "valuation_impact_pct": self.valuation_impact_pct,
            "risk_delta": self.risk_delta.to_dict(),
            "correlation_only_paths": list(self.correlation_only_paths),
            "skipped_no_path": list(self.skipped_no_path),
            "evidence_refs": list(self.evidence_refs),
            "model_version": self.model_version,
            "created_at": self.created_at,
            "notice": self.notice,
        }


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------


def assert_causal_path(path: TransmissionPath) -> None:
    """校验传导路径满足因果声明条件。

    相关性 ≠ 因果：路径必须同时满足
    - 至少一条证据
    - 置信度 ≥ CORRELATION_CONFIDENCE_THRESHOLD

    Args:
        path: 待校验的传导路径

    Raises:
        CorrelationCausationError: 路径仅为相关性观察，不能用于因果传导
    """
    if path.correlation_only:
        reasons: list[str] = []
        if not path.evidence_refs:
            reasons.append("evidence_refs 为空")
        if path.confidence < TransmissionPath.CORRELATION_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"confidence={path.confidence:.2f} 低于阈值 "
                f"{TransmissionPath.CORRELATION_CONFIDENCE_THRESHOLD:.2f}"
            )
        raise CorrelationCausationError(
            f"相关性不能自动声明因果（path_id={path.path_id}, "
            f"source={path.source_variable}, target={path.target_driver}）："
            f"{'；'.join(reasons)}"
        )


def assert_no_trade_action(text: str) -> None:
    """校验文本不含交易动作关键词。

    Args:
        text: 待校验文本

    Raises:
        ScenarioContractError: 含交易动作关键词
    """
    if not text:
        return
    matched = [kw for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS if kw in text]
    if matched:
        raise ScenarioContractError(
            f"文本含交易动作关键词（{', '.join(matched)}）"
        )


__all__ = [
    # 合规
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    "utc_now",
    # 宏观变量类别
    "MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT",
    "MACRO_CATEGORY_COMMODITY_ENERGY",
    "MACRO_CATEGORY_DEMAND_CAPEX",
    "MACRO_CATEGORY_TRADE_POLICY_SUPPLY",
    "MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL",
    "MACRO_CATEGORIES",
    "MACRO_CATEGORY_LABELS",
    # 方向
    "DIRECTION_POSITIVE",
    "DIRECTION_NEGATIVE",
    "DIRECTION_NEUTRAL",
    "DIRECTIONS",
    "DIRECTION_SIGNS",
    # 目标驱动
    "TARGET_DRIVER_REVENUE",
    "TARGET_DRIVER_GROSS_MARGIN",
    "TARGET_DRIVER_OPEX_RATIO",
    "TARGET_DRIVER_FCF_CONVERSION",
    "TARGET_DRIVER_DISCOUNT_RATE",
    "TARGET_DRIVER_VALUATION_MULTIPLE",
    "TARGET_DRIVER_CAPEX",
    "TARGET_DRIVERS",
    "TARGET_DRIVER_LABELS",
    # 风险等级
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_EXTREME",
    "RISK_LEVELS",
    "RISK_LEVEL_SCORES",
    # 错误
    "ScenarioContractError",
    "CorrelationCausationError",
    # 数据契约
    "MacroVariable",
    "MacroShock",
    "TransmissionPath",
    "DriverImpact",
    "ForecastSnapshot",
    "RiskDelta",
    "CompanyStressScenario",
    # 校验
    "assert_causal_path",
    "assert_no_trade_action",
]
