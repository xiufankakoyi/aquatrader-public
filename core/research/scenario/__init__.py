"""P3.4 跨域因果与宏观情景系统。

按 P3 计划 Task P3.4：
- 宏观变量通过显式传导路径影响行业和公司驱动
- 每条传导路径包含方向、时滞、适用范围和证据
- 相关性不能自动声明因果；缺乏证据的路径默认 ``correlation_only=True``
- 输出财务、现金流、估值和风险变化，不输出交易动作

模块构成：
- ``contracts``: MacroVariable / TransmissionPath / CompanyStressScenario 等契约
- ``macro_transmission``: 传导路径注册表 + 默认 5 类宏观变量模板路径 + 传导引擎
- ``company_stress``: 公司压力测试引擎（财务 / 现金流 / 估值 / 风险 delta）
"""
from __future__ import annotations

from .contracts import (
    # 合规
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    NOTICE,
    utc_now,
    # 宏观变量类别
    MACRO_CATEGORIES,
    MACRO_CATEGORY_COMMODITY_ENERGY,
    MACRO_CATEGORY_DEMAND_CAPEX,
    MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL,
    MACRO_CATEGORY_LABELS,
    MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT,
    MACRO_CATEGORY_TRADE_POLICY_SUPPLY,
    # 方向
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
    DIRECTION_POSITIVE,
    DIRECTION_SIGNS,
    DIRECTIONS,
    # 目标驱动
    TARGET_DRIVERS,
    TARGET_DRIVER_CAPEX,
    TARGET_DRIVER_DISCOUNT_RATE,
    TARGET_DRIVER_FCF_CONVERSION,
    TARGET_DRIVER_GROSS_MARGIN,
    TARGET_DRIVER_LABELS,
    TARGET_DRIVER_OPEX_RATIO,
    TARGET_DRIVER_REVENUE,
    TARGET_DRIVER_VALUATION_MULTIPLE,
    # 风险等级
    RISK_LEVEL_EXTREME,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_SCORES,
    RISK_LEVELS,
    # 错误
    CorrelationCausationError,
    ScenarioContractError,
    # 数据契约
    CompanyStressScenario,
    DriverImpact,
    ForecastSnapshot,
    MacroShock,
    MacroVariable,
    RiskDelta,
    TransmissionPath,
    # 校验
    assert_causal_path,
    assert_no_trade_action,
)
from .company_stress import (
    CompanyStressEngine,
    build_default_stress_engine,
)
from .macro_transmission import (
    MacroTransmissionEngine,
    MacroTransmissionRegistry,
    TransmissionRegistryError,
    build_default_engine,
    build_default_registry,
)

__all__ = [
    # 合规
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    "utc_now",
    # 宏观变量类别
    "MACRO_CATEGORIES",
    "MACRO_CATEGORY_COMMODITY_ENERGY",
    "MACRO_CATEGORY_DEMAND_CAPEX",
    "MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL",
    "MACRO_CATEGORY_LABELS",
    "MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT",
    "MACRO_CATEGORY_TRADE_POLICY_SUPPLY",
    # 方向
    "DIRECTION_NEGATIVE",
    "DIRECTION_NEUTRAL",
    "DIRECTION_POSITIVE",
    "DIRECTION_SIGNS",
    "DIRECTIONS",
    # 目标驱动
    "TARGET_DRIVERS",
    "TARGET_DRIVER_CAPEX",
    "TARGET_DRIVER_DISCOUNT_RATE",
    "TARGET_DRIVER_FCF_CONVERSION",
    "TARGET_DRIVER_GROSS_MARGIN",
    "TARGET_DRIVER_LABELS",
    "TARGET_DRIVER_OPEX_RATIO",
    "TARGET_DRIVER_REVENUE",
    "TARGET_DRIVER_VALUATION_MULTIPLE",
    # 风险等级
    "RISK_LEVEL_EXTREME",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
    "RISK_LEVEL_SCORES",
    "RISK_LEVELS",
    # 错误
    "CorrelationCausationError",
    "ScenarioContractError",
    "TransmissionRegistryError",
    # 数据契约
    "CompanyStressScenario",
    "DriverImpact",
    "ForecastSnapshot",
    "MacroShock",
    "MacroVariable",
    "RiskDelta",
    "TransmissionPath",
    # 校验
    "assert_causal_path",
    "assert_no_trade_action",
    # 引擎
    "MacroTransmissionEngine",
    "MacroTransmissionRegistry",
    "build_default_engine",
    "build_default_registry",
    "CompanyStressEngine",
    "build_default_stress_engine",
]
