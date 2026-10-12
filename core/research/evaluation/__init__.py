"""P2.9 PIT 历史检验与研究记分车子包。

按 P2 计划 §301-323 Task P2.9：
- 预测误差评估（收入 / 利润 / EPS）
- 预测区间覆盖率
- 共识 vs 独立预测相对误差
- 变化事件 → 有效事实转化率
- 来源命中率与单位成本
- 5 类错误归因（模型 / 映射 / 数据 / 时点 / 假设）
- 行业与市场环境稳定性
- 3 条强制规则：不反向修改研究记录 / 冻结当时输入 / 不以股价涨跌单独定义正确性

导出：
- 契约：FrozenForecastInput / ActualOutcome / ErrorAttribution / ForecastScorecard /
  SourceMetrics / EventConversionStat / IndustryStabilityScore
- 引擎：ForecastScorecardEngine / DiscoveryEvaluator
- 仓储：EvaluationRepository
- 常量：METRIC_* / FORECAST_SOURCE_* / MARKET_REGIME_* / ERROR_ATTRIBUTION_* 等
"""
from __future__ import annotations

from .contracts import (
    ACTUAL_OUTCOME_STATUS_REPORTED,
    ACTUAL_OUTCOME_STATUS_PRELIMINARY,
    ACTUAL_OUTCOME_STATUSES,
    ERROR_ATTRIBUTION_ASSUMPTION,
    ERROR_ATTRIBUTION_DATA,
    ERROR_ATTRIBUTION_LABELS,
    ERROR_ATTRIBUTION_MAPPING,
    ERROR_ATTRIBUTION_MODEL,
    ERROR_ATTRIBUTION_TIMING,
    ERROR_ATTRIBUTIONS,
    EVENT_STATUS_EVIDENCE_SUPPORTED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_MODEL_ELIGIBLE,
    EVENT_STATUS_OBSERVED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUSES,
    FORECAST_SOURCE_CONSENSUS,
    FORECAST_SOURCE_GUIDANCE,
    FORECAST_SOURCE_IMPLIED,
    FORECAST_SOURCE_INTERNAL,
    FORECAST_SOURCES,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    MARKET_REGIME_BEAR,
    MARKET_REGIME_BULL,
    MARKET_REGIME_RANGE,
    MARKET_REGIMES,
    METRIC_EPS,
    METRIC_NET_PROFIT,
    METRIC_REVENUE,
    METRICS,
    NOTICE,
    ActualOutcome,
    ErrorAttribution,
    EventConversionStat,
    ForecastScorecard,
    FrozenForecastInput,
    IndustryStabilityScore,
    SourceMetrics,
    compute_frozen_input_hash,
    utc_now,
)
from .discovery_evaluator import (
    DiscoveryEvaluator,
    build_discovery_evaluator,
)
from .forecast_scorecard import (
    ERR_ACTUAL_BEFORE_FORECAST,
    ERR_ATTRIBUTION_TOTAL_NOT_100,
    ERR_INVALID_ATTRIBUTION,
    ERR_INVALID_INTERVAL,
    ERR_INVALID_METRIC,
    ERR_INVALID_REGIME,
    ERR_INVALID_SOURCE,
    ERR_MISSING_ACTUAL,
    ERR_MISSING_FORECAST,
    ERR_NOT_REPORTED,
    ForecastScorecardEngine,
    build_forecast_scorecard_engine,
)
from .repository import (
    ERR_FROZEN_INPUT_TAMPERED,
    ERR_FUTURE_LEAKAGE,
    ERR_SCORECARD_EXISTS,
    EvaluationRepository,
)

__all__ = [
    # 常量
    "METRIC_REVENUE",
    "METRIC_NET_PROFIT",
    "METRIC_EPS",
    "METRICS",
    "FORECAST_SOURCE_CONSENSUS",
    "FORECAST_SOURCE_INTERNAL",
    "FORECAST_SOURCE_GUIDANCE",
    "FORECAST_SOURCE_IMPLIED",
    "FORECAST_SOURCES",
    "MARKET_REGIME_BULL",
    "MARKET_REGIME_BEAR",
    "MARKET_REGIME_RANGE",
    "MARKET_REGIMES",
    "ERROR_ATTRIBUTION_MODEL",
    "ERROR_ATTRIBUTION_MAPPING",
    "ERROR_ATTRIBUTION_DATA",
    "ERROR_ATTRIBUTION_TIMING",
    "ERROR_ATTRIBUTION_ASSUMPTION",
    "ERROR_ATTRIBUTIONS",
    "ERROR_ATTRIBUTION_LABELS",
    "EVENT_STATUS_OBSERVED",
    "EVENT_STATUS_EVIDENCE_SUPPORTED",
    "EVENT_STATUS_MODEL_ELIGIBLE",
    "EVENT_STATUS_REJECTED",
    "EVENT_STATUS_EXPIRED",
    "EVENT_STATUSES",
    "ACTUAL_OUTCOME_STATUS_REPORTED",
    "ACTUAL_OUTCOME_STATUS_PRELIMINARY",
    "ACTUAL_OUTCOME_STATUSES",
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    # 契约
    "FrozenForecastInput",
    "ActualOutcome",
    "ErrorAttribution",
    "ForecastScorecard",
    "SourceMetrics",
    "EventConversionStat",
    "IndustryStabilityScore",
    "compute_frozen_input_hash",
    "utc_now",
    # 引擎
    "ForecastScorecardEngine",
    "DiscoveryEvaluator",
    "build_forecast_scorecard_engine",
    "build_discovery_evaluator",
    # 仓储
    "EvaluationRepository",
    # 错误码
    "ERR_MISSING_FORECAST",
    "ERR_MISSING_ACTUAL",
    "ERR_NOT_REPORTED",
    "ERR_INVALID_METRIC",
    "ERR_INVALID_SOURCE",
    "ERR_INVALID_REGIME",
    "ERR_INVALID_ATTRIBUTION",
    "ERR_INVALID_INTERVAL",
    "ERR_ATTRIBUTION_TOTAL_NOT_100",
    "ERR_ACTUAL_BEFORE_FORECAST",
    "ERR_SCORECARD_EXISTS",
    "ERR_FUTURE_LEAKAGE",
    "ERR_FROZEN_INPUT_TAMPERED",
]
