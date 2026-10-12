"""EarningsImpactEngine 盈利映射层。

独立领域模型，不把财务模型塞进候选图。
所有定量输入必须包含值/区间、单位、币种、期间、as_of、数据版本、
来源 Claim/Evidence、抽取方式和置信区间；缺输入返回 insufficient_data + gaps。
"""
from __future__ import annotations

from .contracts import (
    AssumptionSourceType,
    CapacityModel,
    DemandModel,
    DriverType,
    EarningsDriver,
    EarningsImpact,
    EarningsScenario,
    EarningsScenarioKind,
    EarningsStatus,
    ImpactAssumption,
    InputGap,
    Interval,
    MarginModel,
    PricingModel,
    RevenueExposure,
    RevenueExposureSource,
    SensitivityItem,
    ShareCapitalVersion,
    UnitKind,
)
from .engine import EarningsImpactEngine, EarningsImpactRequest
from .pit import filter_capitals_as_of, filter_inputs_as_of, input_visible_as_of
from .repository import EarningsRepository
from .revalidation import (
    AccuracyReport,
    CaseCategory,
    DISCLAIMER,
    ForecastRevalidator,
    InputDocument,
    MIN_SAMPLE_FOR_ACCURACY,
    MIN_SAMPLE_FOR_PROPAGATION,
    PredictionType,
    PriceReaction,
    PropagationReport,
    RevalidationCase,
    RevalidationResult,
    SAGClaimReaction,
    SAGPropagationRevalidator,
)

__all__ = [
    "AssumptionSourceType",
    "CapacityModel",
    "DemandModel",
    "DriverType",
    "EarningsDriver",
    "EarningsImpact",
    "EarningsImpactEngine",
    "EarningsImpactRequest",
    "EarningsRepository",
    "EarningsScenario",
    "EarningsScenarioKind",
    "EarningsStatus",
    "ImpactAssumption",
    "InputGap",
    "Interval",
    "MarginModel",
    "PricingModel",
    "RevenueExposure",
    "RevenueExposureSource",
    "SensitivityItem",
    "ShareCapitalVersion",
    "UnitKind",
    "filter_capitals_as_of",
    "filter_inputs_as_of",
    "input_visible_as_of",
    # Stage 7 - 重验框架
    "AccuracyReport",
    "CaseCategory",
    "DISCLAIMER",
    "ForecastRevalidator",
    "InputDocument",
    "MIN_SAMPLE_FOR_ACCURACY",
    "MIN_SAMPLE_FOR_PROPAGATION",
    "PredictionType",
    "PriceReaction",
    "PropagationReport",
    "RevalidationCase",
    "RevalidationResult",
    "SAGClaimReaction",
    "SAGPropagationRevalidator",
]
