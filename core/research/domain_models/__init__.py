"""P3.2 行业专用模型包。"""
from __future__ import annotations

from core.research.domain_models.base import (
    CompanyProfile,
    DomainModelBase,
    DomainModelError,
    InputRequirement,
    IntegratedForecast,
    ResearchRisk,
    ValidationReport,
)
from core.research.domain_models.registry import (
    DomainModelRegistry,
    get_default_registry,
    register_default_models,
)

__all__ = [
    "CompanyProfile",
    "DomainModelBase",
    "DomainModelError",
    "DomainModelRegistry",
    "InputRequirement",
    "IntegratedForecast",
    "ResearchRisk",
    "ValidationReport",
    "get_default_registry",
    "register_default_models",
]
