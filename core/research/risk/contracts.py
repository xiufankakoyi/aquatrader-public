"""P2.8 风险与组合上下文契约。

按 P2 计划 §281-299 Task P2.8：
- 6 个风险维度（公司经营财务治理 / 行业供需价格技术替代 / 宏观变量 / 流动性停牌容量 /
  因子相关性 / 情景压力）
- 边界：只输出风险暴露和压力结果，不输出仓位或交易动作
- 适配 core/quant/portfolio 的只读快照（不改 PositionManager）

设计要点：
- RiskDimension：单一维度，含 level / score / drivers / evidence / mitigants
- ResearchRiskContext：6 维度聚合 + 集中度 HHI + 因子暴露 + 相关性备注
- ScenarioStressTest：情景压力下收入 / 利润 / 现金流 / 估值变化
- PortfolioSnapshot：只读组合快照（holdings + industry_exposure + HHI）
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# 6 个风险维度
# ---------------------------------------------------------------------------

DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE: str = "corporate_financial_governance"
DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH: str = "industry_supply_demand_tech"
DIMENSION_MACRO_FX_RATE_COMMODITY: str = "macro_fx_rate_commodity"
DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY: str = "liquidity_suspension_capacity"
DIMENSION_FACTOR_CORRELATION: str = "factor_correlation"
DIMENSION_SCENARIO_STRESS: str = "scenario_stress"

RISK_DIMENSIONS: tuple[str, ...] = (
    DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
    DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH,
    DIMENSION_MACRO_FX_RATE_COMMODITY,
    DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY,
    DIMENSION_FACTOR_CORRELATION,
    DIMENSION_SCENARIO_STRESS,
)

RISK_DIMENSION_LABELS: dict[str, str] = {
    DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE: "公司经营、财务和治理",
    DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH: "行业供需、价格和技术替代",
    DIMENSION_MACRO_FX_RATE_COMMODITY: "宏观变量与汇率、利率、商品价格",
    DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY: "流动性、停牌和容量",
    DIMENSION_FACTOR_CORRELATION: "因子和相关性暴露",
    DIMENSION_SCENARIO_STRESS: "情景压力下收入、利润、现金流和估值变化",
}


# ---------------------------------------------------------------------------
# 风险等级
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

# level → 数值映射（用于聚合）
RISK_LEVEL_SCORES: dict[str, float] = {
    RISK_LEVEL_LOW: 0.25,
    RISK_LEVEL_MEDIUM: 0.5,
    RISK_LEVEL_HIGH: 0.75,
    RISK_LEVEL_EXTREME: 1.0,
}


# ---------------------------------------------------------------------------
# 风险驱动方向与幅度
# ---------------------------------------------------------------------------

DRIVER_DIRECTION_UPSIDE: str = "upside"
DRIVER_DIRECTION_DOWNSIDE: str = "downside"
DRIVER_DIRECTION_NEUTRAL: str = "neutral"

DRIVER_DIRECTIONS: tuple[str, ...] = (
    DRIVER_DIRECTION_UPSIDE,
    DRIVER_DIRECTION_DOWNSIDE,
    DRIVER_DIRECTION_NEUTRAL,
)

DRIVER_MAGNITUDE_SMALL: str = "small"
DRIVER_MAGNITUDE_MEDIUM: str = "medium"
DRIVER_MAGNITUDE_LARGE: str = "large"

DRIVER_MAGNITUDES: tuple[str, ...] = (
    DRIVER_MAGNITUDE_SMALL,
    DRIVER_MAGNITUDE_MEDIUM,
    DRIVER_MAGNITUDE_LARGE,
)


# ---------------------------------------------------------------------------
# 情景类型
# ---------------------------------------------------------------------------

SCENARIO_BEAR_CASE: str = "bear_case"
SCENARIO_BASE_CASE: str = "base_case"
SCENARIO_BULL_CASE: str = "bull_case"
SCENARIO_CUSTOM: str = "custom"

SCENARIO_TYPES: tuple[str, ...] = (
    SCENARIO_BEAR_CASE,
    SCENARIO_BASE_CASE,
    SCENARIO_BULL_CASE,
    SCENARIO_CUSTOM,
)

SCENARIO_TYPE_LABELS: dict[str, str] = {
    SCENARIO_BEAR_CASE: "悲观情景",
    SCENARIO_BASE_CASE: "基准情景",
    SCENARIO_BULL_CASE: "乐观情景",
    SCENARIO_CUSTOM: "自定义情景",
}


# ---------------------------------------------------------------------------
# AGENTS.md 合规
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

NOTICE: str = "风险与组合上下文仅输出风险暴露和压力结果，不输出仓位或交易动作建议。"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _bounded_score(value: Any) -> float:
    return max(0.0, min(1.0, _safe_float(value)))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _float_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _safe_float(item) for key, item in value.items() if str(key).strip()}


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 风险驱动
# ---------------------------------------------------------------------------


@dataclass
class RiskDriver:
    """风险驱动。

    单一风险维度下的具体驱动因素。

    Attributes:
        driver_id: 驱动 ID
        name: 驱动名称
        description: 描述
        direction: 方向（upside / downside / neutral）
        magnitude: 幅度（small / medium / large）
        evidence_refs: 证据 ID 列表
        source_refs: 来源引用
    """

    driver_id: str = ""
    name: str = ""
    description: str = ""
    direction: str = DRIVER_DIRECTION_NEUTRAL
    magnitude: str = DRIVER_MAGNITUDE_MEDIUM
    evidence_refs: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.evidence_refs = _string_list(self.evidence_refs)
        self.source_refs = _string_list(self.source_refs)

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词（AGENTS.md）。"""
        for field_name in ("name", "description"):
            value = getattr(self, field_name)
            if value and any(kw in value for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "description": self.description,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "evidence_refs": list(self.evidence_refs),
            "source_refs": list(self.source_refs),
        }


# ---------------------------------------------------------------------------
# 风险维度
# ---------------------------------------------------------------------------


@dataclass
class RiskDimension:
    """单一风险维度。

    Attributes:
        dimension: 维度 key（RISK_DIMENSIONS 之一）
        level: 风险等级（RISK_LEVELS 之一）
        score: 0-1 数值（越高风险越大）
        drivers: 风险驱动列表
        evidence_refs: 证据 ID 列表
        mitigants: 风险缓释因素
        source_refs: 来源引用
        as_of: PIT 时点
        note: 备注
    """

    dimension: str = ""
    level: str = RISK_LEVEL_LOW
    score: float = 0.0
    drivers: list[RiskDriver] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    mitigants: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    as_of: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        """默认 score 由 level 推导（如未显式提供）。"""
        self.score = _bounded_score(self.score)
        self.drivers = [item for item in self.drivers if isinstance(item, RiskDriver)]
        self.evidence_refs = _string_list(self.evidence_refs)
        self.mitigants = _string_list(self.mitigants)
        self.source_refs = _string_list(self.source_refs)
        if self.score == 0.0 and self.level in RISK_LEVEL_SCORES:
            self.score = RISK_LEVEL_SCORES[self.level]

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        if self.note and any(kw in self.note for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
            return True
        if any(
            kw in m for m in self.mitigants for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        ):
            return True
        return any(d.contains_trade_action() for d in self.drivers)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "dimension": self.dimension,
            "dimension_label": RISK_DIMENSION_LABELS.get(self.dimension, ""),
            "level": self.level,
            "score": self.score,
            "drivers": [d.to_dict() for d in self.drivers],
            "evidence_refs": list(self.evidence_refs),
            "mitigants": list(self.mitigants),
            "source_refs": list(self.source_refs),
            "as_of": self.as_of,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 研究风险上下文
# ---------------------------------------------------------------------------


@dataclass
class ResearchRiskContext:
    """研究风险上下文（公司级聚合）。

    聚合 6 个风险维度 + 集中度 HHI + 因子暴露 + 相关性备注。

    Attributes:
        context_id: 上下文 ID
        company_symbol: 公司代码
        as_of: PIT 时点
        thesis_id: 关联 P2.6 VariantPerception.thesis_id（可选）
        dimensions: 6 个风险维度
        overall_risk_level: 综合风险等级（由维度聚合）
        overall_risk_score: 综合风险分数
        concentration_hhi: 集中度 HHI 指数（0-1，1 = 单一持仓）
        factor_exposures: 因子暴露字典（factor_name → exposure）
        correlation_notes: 相关性备注列表
        data_version: 数据版本
        model_version: 模型版本
        created_at: 创建时间
    """

    context_id: str = ""
    company_symbol: str = ""
    as_of: str = ""
    thesis_id: str = ""
    dimensions: list[RiskDimension] = field(default_factory=list)
    overall_risk_level: str = RISK_LEVEL_LOW
    overall_risk_score: float = 0.0
    concentration_hhi: float = 0.0
    factor_exposures: dict[str, float] = field(default_factory=dict)
    correlation_notes: list[str] = field(default_factory=list)
    data_version: str = ""
    model_version: str = "v1"
    created_at: str = ""

    def __post_init__(self) -> None:
        """默认 created_at。"""
        if not self.created_at:
            self.created_at = utc_now()
        self.dimensions = [item for item in self.dimensions if isinstance(item, RiskDimension)]
        self.overall_risk_score = _bounded_score(self.overall_risk_score)
        self.concentration_hhi = _bounded_score(self.concentration_hhi)
        self.factor_exposures = _float_mapping(self.factor_exposures)
        self.correlation_notes = _string_list(self.correlation_notes)
        if not self.context_id:
            self.context_id = self._derive_context_id()

    def _derive_context_id(self) -> str:
        """确定性上下文 ID。"""
        raw = f"{self.company_symbol}|{self.as_of}|{self.thesis_id}|{self.model_version}"
        return f"risk_ctx_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def get_dimension(self, dimension: str) -> RiskDimension | None:
        """获取指定维度。"""
        for d in self.dimensions:
            if d.dimension == dimension:
                return d
        return None

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        if any(
            kw in n for n in self.correlation_notes for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        ):
            return True
        return any(d.contains_trade_action() for d in self.dimensions)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "context_id": self.context_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "thesis_id": self.thesis_id,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "overall_risk_level": self.overall_risk_level,
            "overall_risk_score": self.overall_risk_score,
            "concentration_hhi": self.concentration_hhi,
            "factor_exposures": dict(self.factor_exposures),
            "correlation_notes": list(self.correlation_notes),
            "data_version": self.data_version,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "notice": NOTICE,
        }


# ---------------------------------------------------------------------------
# 情景压力测试
# ---------------------------------------------------------------------------


@dataclass
class ScenarioStressTest:
    """情景压力测试。

    模拟某情景下公司收入、利润、现金流和估值的变化。

    Attributes:
        test_id: 测试 ID
        company_symbol: 公司代码
        as_of: PIT 时点
        scenario_type: 情景类型（SCENARIO_TYPES 之一）
        scenario_name: 情景名称
        scenario_description: 情景描述
        assumptions: 假设参数（shock 参数）
        revenue_impact_pct: 收入影响百分比（负值表示下降）
        profit_impact_pct: 利润影响百分比
        cashflow_impact_pct: 现金流影响百分比
        valuation_impact_pct: 估值影响百分比
        evidence_refs: 证据 ID 列表
        source_refs: 来源引用
        model_version: 模型版本
        created_at: 创建时间
    """

    test_id: str = ""
    company_symbol: str = ""
    as_of: str = ""
    scenario_type: str = SCENARIO_BASE_CASE
    scenario_name: str = ""
    scenario_description: str = ""
    assumptions: dict[str, Any] = field(default_factory=dict)
    revenue_impact_pct: float = 0.0
    profit_impact_pct: float = 0.0
    cashflow_impact_pct: float = 0.0
    valuation_impact_pct: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    model_version: str = "v1"
    created_at: str = ""

    def __post_init__(self) -> None:
        """默认 test_id / created_at。"""
        if not self.created_at:
            self.created_at = utc_now()
        self.assumptions = dict(self.assumptions) if isinstance(self.assumptions, dict) else {}
        self.revenue_impact_pct = _safe_float(self.revenue_impact_pct)
        self.profit_impact_pct = _safe_float(self.profit_impact_pct)
        self.cashflow_impact_pct = _safe_float(self.cashflow_impact_pct)
        self.valuation_impact_pct = _safe_float(self.valuation_impact_pct)
        self.evidence_refs = _string_list(self.evidence_refs)
        self.source_refs = _string_list(self.source_refs)
        if not self.test_id:
            self.test_id = self._derive_test_id()

    def _derive_test_id(self) -> str:
        """确定性测试 ID。"""
        raw = (
            f"{self.company_symbol}|{self.as_of}|{self.scenario_type}|"
            f"{self.scenario_name}|{self.model_version}"
        )
        return f"stress_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        for field_name in ("scenario_name", "scenario_description"):
            value = getattr(self, field_name)
            if value and any(kw in value for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "test_id": self.test_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "scenario_type": self.scenario_type,
            "scenario_type_label": SCENARIO_TYPE_LABELS.get(self.scenario_type, ""),
            "scenario_name": self.scenario_name,
            "scenario_description": self.scenario_description,
            "assumptions": dict(self.assumptions),
            "revenue_impact_pct": self.revenue_impact_pct,
            "profit_impact_pct": self.profit_impact_pct,
            "cashflow_impact_pct": self.cashflow_impact_pct,
            "valuation_impact_pct": self.valuation_impact_pct,
            "evidence_refs": list(self.evidence_refs),
            "source_refs": list(self.source_refs),
            "model_version": self.model_version,
            "created_at": self.created_at,
            "notice": NOTICE,
        }


# ---------------------------------------------------------------------------
# 组合快照（只读 view）
# ---------------------------------------------------------------------------


@dataclass
class Holding:
    """单一持仓（只读 view）。

    Attributes:
        stock_code: 股票代码
        stock_name: 股票名称
        weight: 权重（0-1）
        industry: 行业
        market_value: 市值
    """

    stock_code: str = ""
    stock_name: str = ""
    weight: float = 0.0
    industry: str = ""
    market_value: float = 0.0

    def __post_init__(self) -> None:
        self.weight = _safe_float(self.weight)
        self.market_value = _safe_float(self.market_value)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "weight": self.weight,
            "industry": self.industry,
            "market_value": self.market_value,
        }


@dataclass
class PortfolioSnapshot:
    """组合快照（只读 view）。

    从 PositionManager 适配而来的只读快照，用于风险上下文计算。
    不输出仓位建议，仅用于风险敞口分析（集中度、行业暴露）。

    Attributes:
        snapshot_id: 快照 ID
        as_of: PIT 时点
        holdings: 持仓列表
        total_market_value: 总市值
        industry_exposure: 行业暴露字典（industry → weight）
        concentration_hhi: 集中度 HHI（0-1）
        top_holdings: 前十大持仓
        created_at: 创建时间
    """

    snapshot_id: str = ""
    as_of: str = ""
    holdings: list[Holding] = field(default_factory=list)
    total_market_value: float = 0.0
    industry_exposure: dict[str, float] = field(default_factory=dict)
    concentration_hhi: float = 0.0
    top_holdings: list[Holding] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        """默认 created_at / snapshot_id。"""
        if not self.created_at:
            self.created_at = utc_now()
        self.holdings = [item for item in self.holdings if isinstance(item, Holding)]
        self.top_holdings = [item for item in self.top_holdings if isinstance(item, Holding)]
        self.total_market_value = _safe_float(self.total_market_value)
        self.industry_exposure = _float_mapping(self.industry_exposure)
        self.concentration_hhi = _bounded_score(self.concentration_hhi)
        if not self.snapshot_id:
            self.snapshot_id = self._derive_snapshot_id()

    def _derive_snapshot_id(self) -> str:
        """确定性快照 ID。"""
        codes = ",".join(sorted(h.stock_code for h in self.holdings))
        raw = f"{self.as_of}|{codes}|{self.total_market_value}"
        return f"snap_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "holdings": [h.to_dict() for h in self.holdings],
            "total_market_value": self.total_market_value,
            "industry_exposure": dict(self.industry_exposure),
            "concentration_hhi": self.concentration_hhi,
            "top_holdings": [h.to_dict() for h in self.top_holdings],
            "created_at": self.created_at,
            "notice": NOTICE,
        }


__all__ = [
    # 维度常量
    "DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE",
    "DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH",
    "DIMENSION_MACRO_FX_RATE_COMMODITY",
    "DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY",
    "DIMENSION_FACTOR_CORRELATION",
    "DIMENSION_SCENARIO_STRESS",
    "RISK_DIMENSIONS",
    "RISK_DIMENSION_LABELS",
    # 等级常量
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_EXTREME",
    "RISK_LEVELS",
    "RISK_LEVEL_SCORES",
    # 驱动方向与幅度
    "DRIVER_DIRECTION_UPSIDE",
    "DRIVER_DIRECTION_DOWNSIDE",
    "DRIVER_DIRECTION_NEUTRAL",
    "DRIVER_DIRECTIONS",
    "DRIVER_MAGNITUDE_SMALL",
    "DRIVER_MAGNITUDE_MEDIUM",
    "DRIVER_MAGNITUDE_LARGE",
    "DRIVER_MAGNITUDES",
    # 情景类型
    "SCENARIO_BEAR_CASE",
    "SCENARIO_BASE_CASE",
    "SCENARIO_BULL_CASE",
    "SCENARIO_CUSTOM",
    "SCENARIO_TYPES",
    "SCENARIO_TYPE_LABELS",
    # 合规
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    "utc_now",
    # 数据契约
    "RiskDriver",
    "RiskDimension",
    "ResearchRiskContext",
    "ScenarioStressTest",
    "Holding",
    "PortfolioSnapshot",
]
