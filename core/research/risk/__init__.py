"""P2.8 风险与组合上下文子包。

按 P2 计划 §281-299 Task P2.8：
- 6 个风险维度（公司经营财务治理 / 行业供需价格技术替代 / 宏观变量 / 流动性停牌容量 /
  因子相关性 / 情景压力）
- 边界：只输出风险暴露和压力结果，不输出仓位或交易动作
- 适配 core/quant/portfolio 的只读快照（不改 PositionManager）

导出：
- 契约：RiskDriver / RiskDimension / ResearchRiskContext / ScenarioStressTest / Holding / PortfolioSnapshot
- 引擎：ResearchRiskEngine / ScenarioStressEngine
- 适配器：PortfolioSnapshotAdapter（从 core.quant.portfolio.portfolio_snapshot_adapter 导入）
- 常量：RISK_DIMENSIONS / RISK_LEVELS / SCENARIO_TYPES / FORBIDDEN_TRADE_ACTION_KEYWORDS 等
"""
from __future__ import annotations

from .contracts import (
    DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
    DIMENSION_FACTOR_CORRELATION,
    DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH,
    DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY,
    DIMENSION_MACRO_FX_RATE_COMMODITY,
    DIMENSION_SCENARIO_STRESS,
    DRIVER_DIRECTION_DOWNSIDE,
    DRIVER_DIRECTION_NEUTRAL,
    DRIVER_DIRECTION_UPSIDE,
    DRIVER_DIRECTIONS,
    DRIVER_MAGNITUDE_LARGE,
    DRIVER_MAGNITUDE_MEDIUM,
    DRIVER_MAGNITUDE_SMALL,
    DRIVER_MAGNITUDES,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    Holding,
    NOTICE,
    PortfolioSnapshot,
    RISK_DIMENSIONS,
    RISK_DIMENSION_LABELS,
    RISK_LEVEL_EXTREME,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_SCORES,
    RISK_LEVELS,
    ResearchRiskContext,
    RiskDimension,
    RiskDriver,
    SCENARIO_BASE_CASE,
    SCENARIO_BEAR_CASE,
    SCENARIO_BULL_CASE,
    SCENARIO_CUSTOM,
    SCENARIO_TYPE_LABELS,
    SCENARIO_TYPES,
    ScenarioStressTest,
    utc_now,
)
from .research_risk import (
    ERR_INVALID_ASSUMPTIONS,
    ERR_INVALID_DIRECTION,
    ERR_INVALID_LEVEL,
    ERR_INVALID_MAGNITUDE,
    ERR_MISSING_DIMENSION,
    ERR_TRADE_ACTION_KEYWORD,
    ResearchRiskEngine,
    aggregate_overall_risk_level,
    build_research_risk_engine,
    compute_concentration_hhi,
    compute_industry_exposure,
)
from .scenario_stress import (
    ERR_INVALID_SCENARIO_TYPE,
    ERR_INVALID_SHOCK,
    ERR_MISSING_BASELINE,
    STANDARD_SCENARIO_TEMPLATES,
    ScenarioStressEngine,
    build_scenario_stress_engine,
)


__all__ = [
    # 维度常量
    "DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE",
    "DIMENSION_FACTOR_CORRELATION",
    "DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH",
    "DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY",
    "DIMENSION_MACRO_FX_RATE_COMMODITY",
    "DIMENSION_SCENARIO_STRESS",
    "RISK_DIMENSIONS",
    "RISK_DIMENSION_LABELS",
    # 等级常量
    "RISK_LEVEL_EXTREME",
    "RISK_LEVEL_HIGH",
    "RISK_LEVEL_LOW",
    "RISK_LEVEL_MEDIUM",
    "RISK_LEVEL_SCORES",
    "RISK_LEVELS",
    # 驱动方向与幅度
    "DRIVER_DIRECTION_DOWNSIDE",
    "DRIVER_DIRECTION_NEUTRAL",
    "DRIVER_DIRECTION_UPSIDE",
    "DRIVER_DIRECTIONS",
    "DRIVER_MAGNITUDE_LARGE",
    "DRIVER_MAGNITUDE_MEDIUM",
    "DRIVER_MAGNITUDE_SMALL",
    "DRIVER_MAGNITUDES",
    # 情景类型
    "SCENARIO_BASE_CASE",
    "SCENARIO_BEAR_CASE",
    "SCENARIO_BULL_CASE",
    "SCENARIO_CUSTOM",
    "SCENARIO_TYPE_LABELS",
    "SCENARIO_TYPES",
    "STANDARD_SCENARIO_TEMPLATES",
    # 合规
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    "utc_now",
    # 数据契约
    "Holding",
    "PortfolioSnapshot",
    "ResearchRiskContext",
    "RiskDimension",
    "RiskDriver",
    "ScenarioStressTest",
    # 引擎
    "ResearchRiskEngine",
    "ScenarioStressEngine",
    "aggregate_overall_risk_level",
    "build_research_risk_engine",
    "build_scenario_stress_engine",
    "compute_concentration_hhi",
    "compute_industry_exposure",
    # 错误码
    "ERR_INVALID_ASSUMPTIONS",
    "ERR_INVALID_DIRECTION",
    "ERR_INVALID_LEVEL",
    "ERR_INVALID_MAGNITUDE",
    "ERR_INVALID_SCENARIO_TYPE",
    "ERR_INVALID_SHOCK",
    "ERR_MISSING_BASELINE",
    "ERR_MISSING_DIMENSION",
    "ERR_TRADE_ACTION_KEYWORD",
]
