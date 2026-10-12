from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# P0 正式契约（docs/research/institutional/p0-contracts.md）
# ---------------------------------------------------------------------------

RESEARCH_FACT_REQUIRED_FIELDS: tuple[str, ...] = (
    "fact_id", "entity_id", "metric_or_relation", "value", "unit",
    "currency", "period", "published_at", "retrieved_at", "available_at",
    "valid_from", "valid_to", "source_trace_id", "evidence_ids",
    "grounding_status", "confidence", "observation_kind", "data_version",
)

RESEARCH_FACT_GROUNDING_STATUSES: tuple[str, ...] = (
    "grounding_ok", "grounding_partial", "missing",
)

RESEARCH_FACT_OBSERVATION_KINDS: tuple[str, ...] = (
    "verified_fact", "company_disclosed", "third_party_consensus",
    "market_implied", "analyst_assumption", "unknown",
)

RESEARCH_ASSUMPTION_REQUIRED_FIELDS: tuple[str, ...] = (
    "assumption_id", "symbol", "variable_name", "interval", "unit",
    "period", "source_type", "source_fact_ids", "source_evidence_ids",
    "created_at", "available_at", "expires_at", "owner", "model_version",
    "status", "falsification_condition",
)

RESEARCH_ASSUMPTION_SOURCE_TYPES: tuple[str, ...] = (
    "company_disclosed", "third_party_consensus",
    "market_implied", "analyst_assumption",
)

RESEARCH_ASSUMPTION_STATUSES: tuple[str, ...] = (
    "draft", "active", "falsified", "superseded",
)

RESEARCH_ASSUMPTION_VALID_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("draft", "active"),
    ("active", "falsified"),
    ("active", "superseded"),
    ("falsified", "superseded"),
)

RESEARCH_READINESS_DIMENSIONS: tuple[str, ...] = (
    "fact_completeness",
    "source_traceability",
    "source_independence",
    "pit_integrity",
    "forecast_input_completeness",
    "model_computability",
    "valuation_computability",
    "research_uncertainty",
)

RESEARCH_READINESS_REQUIRED_FIELDS: tuple[str, ...] = (
    "status", "score", "missing_items", "evidence_refs",
)

RESEARCH_READINESS_VALID_STATUSES: tuple[str, ...] = (
    "ok", "partial", "blocked", "not_computable",
)


@dataclass
class ConfidenceInterval:
    """事实或假设的置信度区间。

    缺失时 low 与 high 必须同时为 None；禁止返回 {0.0, 0.0} 等伪中性。
    """

    low: float | None = None
    high: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high}


@dataclass
class ResearchFact:
    """P0 正式事实契约（docs/research/institutional/p0-contracts.md §2）。

    缺失字段必须显式为 None，禁止用 0.5 / 行业均值 / 生成式文本兜底。
    """

    fact_id: str = ""
    entity_id: str = ""
    metric_or_relation: str = ""
    value: Any = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    available_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    source_trace_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    grounding_status: str = "missing"
    confidence: ConfidenceInterval = field(default_factory=ConfidenceInterval)
    observation_kind: str = "unknown"
    data_version: str = "research_fact_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "entity_id": self.entity_id,
            "metric_or_relation": self.metric_or_relation,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "available_at": self.available_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_trace_id": self.source_trace_id,
            "evidence_ids": list(self.evidence_ids),
            "grounding_status": self.grounding_status,
            "confidence": self.confidence.to_dict(),
            "observation_kind": self.observation_kind,
            "data_version": self.data_version,
        }


@dataclass
class ResearchAssumption:
    """P0 正式假设契约（docs/research/institutional/p0-contracts.md §3）。

    source_type 四类互斥；同一假设不得同时归为多种来源。
    """

    assumption_id: str = ""
    symbol: str = ""
    variable_name: str = ""
    interval: ConfidenceInterval = field(default_factory=ConfidenceInterval)
    unit: str = ""
    period: str = ""
    source_type: str = ""
    source_fact_ids: list[str] = field(default_factory=list)
    source_evidence_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    available_at: str = ""
    expires_at: str = ""
    owner: str = ""
    model_version: str = "research_assumption_v1"
    status: str = "draft"
    falsification_condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumption_id": self.assumption_id,
            "symbol": self.symbol,
            "variable_name": self.variable_name,
            "interval": self.interval.to_dict(),
            "unit": self.unit,
            "period": self.period,
            "source_type": self.source_type,
            "source_fact_ids": list(self.source_fact_ids),
            "source_evidence_ids": list(self.source_evidence_ids),
            "created_at": self.created_at,
            "available_at": self.available_at,
            "expires_at": self.expires_at,
            "owner": self.owner,
            "model_version": self.model_version,
            "status": self.status,
            "falsification_condition": self.falsification_condition,
        }


@dataclass
class ReadinessDimension:
    """P0 readiness 单维度（docs/research/institutional/p0-contracts.md §4）。

    score=None 表示不可计算；禁止返回中性分。
    """

    status: str = "not_computable"
    score: float | None = None
    missing_items: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "missing_items": list(self.missing_items),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass
class ResearchReadiness:
    """P0 八维 readiness 总览（docs/research/institutional/p0-contracts.md §4）。"""

    fact_completeness: ReadinessDimension = field(default_factory=ReadinessDimension)
    source_traceability: ReadinessDimension = field(default_factory=ReadinessDimension)
    source_independence: ReadinessDimension = field(default_factory=ReadinessDimension)
    pit_integrity: ReadinessDimension = field(default_factory=ReadinessDimension)
    forecast_input_completeness: ReadinessDimension = field(default_factory=ReadinessDimension)
    model_computability: ReadinessDimension = field(default_factory=ReadinessDimension)
    valuation_computability: ReadinessDimension = field(default_factory=ReadinessDimension)
    research_uncertainty: ReadinessDimension = field(default_factory=ReadinessDimension)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name).to_dict()
            for name in RESEARCH_READINESS_DIMENSIONS
        }


def validate_assumption_transition(from_status: str, to_status: str) -> None:
    """校验假设状态迁移合法性。

    Args:
        from_status: 起始状态。
        to_status: 目标状态。

    Raises:
        ValueError: 当迁移不在 RESEARCH_ASSUMPTION_VALID_TRANSITIONS 中。
    """
    if (from_status, to_status) not in RESEARCH_ASSUMPTION_VALID_TRANSITIONS:
        raise ValueError(
            f"非法假设状态迁移: {from_status} -> {to_status}"
        )


def validate_assumption_source_type(source_type: str) -> None:
    """校验假设 source_type 合法性。

    Args:
        source_type: 来源类型字符串。

    Raises:
        ValueError: 当 source_type 不在 RESEARCH_ASSUMPTION_SOURCE_TYPES 中。
    """
    if source_type not in RESEARCH_ASSUMPTION_SOURCE_TYPES:
        raise ValueError(
            f"非法假设 source_type: {source_type}; "
            f"允许值: {RESEARCH_ASSUMPTION_SOURCE_TYPES}"
        )


# ---------------------------------------------------------------------------
# 既有研究产物契约
# ---------------------------------------------------------------------------


@dataclass
class MetricValue:
    value: float
    source_fields: list[str] = field(default_factory=list)
    period: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialQuality:
    status: str = "not_computable"
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    missing_metrics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "metrics": {name: value.to_dict() for name, value in self.metrics.items()},
            "missing_metrics": list(self.missing_metrics),
        }


@dataclass
class ValuationState:
    status: str = "not_computable"
    metrics: dict[str, float] = field(default_factory=dict)
    as_of: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceSnapshot:
    capability: str
    status: str = "empty"
    provider: str = ""
    as_of: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    degradation_reason: str = ""
    excluded_future_records: int = 0
    excluded_unknown_time_records: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompanyDossier:
    symbol: str
    company_name: str = ""
    industry: str = ""
    as_of: str = ""
    company_profile: dict[str, Any] = field(default_factory=dict)
    financial_history: list[dict[str, Any]] = field(default_factory=list)
    financial_quality: FinancialQuality = field(default_factory=FinancialQuality)
    valuation: ValuationState = field(default_factory=ValuationState)
    valuation_context: dict[str, Any] = field(default_factory=dict)
    technical_context: dict[str, Any] = field(default_factory=dict)
    earnings_forecasts: list[dict[str, Any]] = field(default_factory=list)
    announcements: list[dict[str, Any]] = field(default_factory=list)
    industry_context: dict[str, Any] = field(default_factory=dict)
    profit_bridge: dict[str, Any] = field(default_factory=dict)
    expectation_context: dict[str, Any] = field(default_factory=dict)
    research_dimensions: dict[str, Any] = field(default_factory=dict)
    research_assessment: dict[str, Any] = field(default_factory=dict)
    source_snapshots: dict[str, SourceSnapshot] = field(default_factory=dict)
    degradation_reasons: list[str] = field(default_factory=list)
    research_readiness: ResearchReadiness = field(default_factory=ResearchReadiness)
    research_facts: list[ResearchFact] = field(default_factory=list)
    research_assumptions: list[ResearchAssumption] = field(default_factory=list)
    schema_version: str = "company_dossier_v8"

    def to_dict(self) -> dict[str, Any]:
        # dataclasses.asdict recursively copies nested mappings/lists, so a
        # caller cannot mutate the dossier by changing the serialized payload.
        return asdict(self)
