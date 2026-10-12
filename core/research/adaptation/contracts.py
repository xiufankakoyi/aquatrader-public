"""Stage 4 domain-isolated evaluation and attribution contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Mapping
from math import isfinite
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if isfinite(parsed) else default


def _count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _numbers(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _number(item) for key, item in value.items() if str(key).strip()}


def _texts(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class LearningDomain(str, Enum):
    KNOWLEDGE = "knowledge"
    RESEARCH = "research"
    ALPHA = "alpha"
    EXECUTION = "execution"


@dataclass
class DomainFeedback:
    feedback_id: str = field(default_factory=lambda: ident("fb"))
    domain: str = ""
    attribution_type: str = ""
    entity_id: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    as_of: str = ""
    data_version: str = ""
    created_at: str = field(default_factory=now)

    def __post_init__(self) -> None:
        self.feedback_id = _text(self.feedback_id) or ident("fb")
        self.domain = _text(self.domain)
        self.attribution_type = _text(self.attribution_type)
        self.entity_id = _text(self.entity_id)
        self.metrics = _numbers(self.metrics)
        self.as_of = _text(self.as_of)
        self.data_version = _text(self.data_version)
        self.created_at = _text(self.created_at) or now()


@dataclass
class AttributionReviewTask:
    task_id: str = field(default_factory=lambda: ident("atr"))
    source_domain: str = ""
    attribution_type: str = ""
    entity_id: str = ""
    reason: str = ""
    status: str = "pending"
    created_at: str = field(default_factory=now)

    def __post_init__(self) -> None:
        self.task_id = _text(self.task_id) or ident("atr")
        self.source_domain = _text(self.source_domain)
        self.attribution_type = _text(self.attribution_type)
        self.entity_id = _text(self.entity_id)
        self.reason = _text(self.reason)
        self.status = _text(self.status, "pending")
        self.created_at = _text(self.created_at) or now()


@dataclass
class CalibrationVersion:
    calibration_id: str = field(default_factory=lambda: ident("cal"))
    domain: str = ""
    parameters: dict[str, float] = field(default_factory=dict)
    baseline_parameters: dict[str, float] = field(default_factory=dict)
    bounds: dict[str, tuple[float, float]] = field(default_factory=dict)
    sample_size: int = 0
    minimum_sample_size: int = 30
    shadow_metrics: dict[str, float] = field(default_factory=dict)
    status: str = "shadow"
    approved_by: str = ""
    rollback_of: str = ""
    created_at: str = field(default_factory=now)

    def __post_init__(self) -> None:
        self.calibration_id = _text(self.calibration_id) or ident("cal")
        self.domain = _text(self.domain)
        self.parameters = _numbers(self.parameters)
        self.baseline_parameters = _numbers(self.baseline_parameters)
        raw_bounds = self.bounds if isinstance(self.bounds, Mapping) else {}
        self.bounds = {}
        for key, raw in raw_bounds.items():
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                continue
            self.bounds[str(key)] = (_number(raw[0]), _number(raw[1]))
        self.sample_size = _count(self.sample_size)
        self.minimum_sample_size = _count(self.minimum_sample_size, 30)
        self.shadow_metrics = _numbers(self.shadow_metrics)
        self.status = _text(self.status, "shadow")
        self.approved_by = _text(self.approved_by)
        self.rollback_of = _text(self.rollback_of)
        self.created_at = _text(self.created_at) or now()


@dataclass
class PortfolioDiagnostics:
    as_of: str = ""
    industry_exposure: dict[str, float] = field(default_factory=dict)
    style_exposure: dict[str, float] = field(default_factory=dict)
    beta_exposure: float = 0.0
    size_exposure: float = 0.0
    concentration_hhi: float = 0.0
    liquidity_utilization: float = 0.0
    capacity_utilization: float = 0.0
    violations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.as_of = _text(self.as_of)
        self.industry_exposure = _numbers(self.industry_exposure)
        self.style_exposure = _numbers(self.style_exposure)
        self.beta_exposure = _number(self.beta_exposure)
        self.size_exposure = _number(self.size_exposure)
        self.concentration_hhi = _number(self.concentration_hhi)
        self.liquidity_utilization = _number(self.liquidity_utilization)
        self.capacity_utilization = _number(self.capacity_utilization)
        self.violations = _texts(self.violations)


@dataclass
class FourDomainReport:
    report_id: str = field(default_factory=lambda: ident("report"))
    as_of: str = ""
    pit_blocked_count: int = 0
    ic: float | None = None
    rank_ic: float | None = None
    grouped_returns: dict[str, float] = field(default_factory=dict)
    diagnostics: PortfolioDiagnostics = field(default_factory=PortfolioDiagnostics)
    cost_attribution: dict[str, float] = field(default_factory=dict)
    feedback_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    walk_forward_windows: list[dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=now)

    def __post_init__(self) -> None:
        self.report_id = _text(self.report_id) or ident("report")
        self.as_of = _text(self.as_of)
        self.pit_blocked_count = _count(self.pit_blocked_count)
        self.ic = None if self.ic is None else _number(self.ic)
        self.rank_ic = None if self.rank_ic is None else _number(self.rank_ic)
        self.grouped_returns = _numbers(self.grouped_returns)
        self.diagnostics = (
            self.diagnostics
            if isinstance(self.diagnostics, PortfolioDiagnostics)
            else PortfolioDiagnostics()
        )
        self.cost_attribution = _numbers(self.cost_attribution)
        self.feedback_stats = {
            str(domain): _numbers(metrics)
            for domain, metrics in (
                self.feedback_stats.items()
                if isinstance(self.feedback_stats, Mapping) else []
            )
        }
        self.walk_forward_windows = [
            dict(item) for item in self.walk_forward_windows
            if isinstance(item, Mapping)
        ] if isinstance(self.walk_forward_windows, (list, tuple)) else []
        self.created_at = _text(self.created_at) or now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
