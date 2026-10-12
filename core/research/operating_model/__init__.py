"""P1.3 产品与分部经营驱动模型子包导出。"""
from __future__ import annotations

from .contracts import (
    BUSINESS_MODELS,
    BUSINESS_MODEL_MANUFACTURING,
    BUSINESS_MODEL_SOFTWARE_SERVICE,
    BUSINESS_MODEL_CYCLICAL,
    DRIVER_KINDS,
    DriverKind,
    ForwardAssumption,
    OperatingDriver,
    OperatingGap,
    OperatingModel,
    Segment,
    validate_business_model,
    validate_driver_kind,
    validate_observation_kind,
)
from .driver_builder import (
    DUPLICATE_REVENUE_BASE_THRESHOLD,
    build_operating_model,
    detect_duplicate_revenue_base,
)
from .repository import OperatingModelRepository

__all__ = [
    "BUSINESS_MODELS",
    "BUSINESS_MODEL_MANUFACTURING",
    "BUSINESS_MODEL_SOFTWARE_SERVICE",
    "BUSINESS_MODEL_CYCLICAL",
    "DRIVER_KINDS",
    "DUPLICATE_REVENUE_BASE_THRESHOLD",
    "DriverKind",
    "ForwardAssumption",
    "OperatingDriver",
    "OperatingGap",
    "OperatingModel",
    "OperatingModelRepository",
    "Segment",
    "build_operating_model",
    "detect_duplicate_revenue_base",
    "validate_business_model",
    "validate_driver_kind",
    "validate_observation_kind",
]
