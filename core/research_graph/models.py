from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.research_graph.schema import EdgeStatus, EvidenceLevel


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class HyperNode:
    node_id: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ACTIVE"
    source: str = "manual"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HyperNode":
        return cls(**payload)


@dataclass
class HyperEdge:
    edge_id: str
    relation: str
    subjects: list[str]
    objects: list[str]
    qualifiers: dict[str, Any] = field(default_factory=dict)
    dimensions: list[str] = field(default_factory=list)
    primary_dimension: str = ""
    route_layer: str = ""
    time_stage: str = ""
    confidence: str = EvidenceLevel.SPECULATIVE.value
    status: str = EdgeStatus.CANDIDATE.value
    evidence_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)
    source: str = "manual"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        dimension = str(self.qualifiers.get("dimension") or "")
        if dimension and dimension not in self.dimensions:
            self.dimensions.append(dimension)
        if not self.primary_dimension and self.dimensions:
            self.primary_dimension = self.dimensions[0]
        if not self.route_layer:
            self.route_layer = str(self.qualifiers.get("route_layer") or "")
        if not self.time_stage:
            self.time_stage = str(self.qualifiers.get("time_stage") or "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HyperEdge":
        qualifiers = dict(payload.get("qualifiers") or {})
        dimension = str(qualifiers.get("dimension") or "")
        dimensions = list(payload.get("dimensions") or ([] if not dimension else [dimension]))
        primary_dimension = str(payload.get("primary_dimension") or (dimensions[0] if dimensions else ""))
        route_layer = str(payload.get("route_layer") or qualifiers.get("route_layer") or "")
        time_stage = str(payload.get("time_stage") or qualifiers.get("time_stage") or "")
        payload = dict(payload)
        payload["qualifiers"] = qualifiers
        payload["dimensions"] = dimensions
        payload["primary_dimension"] = primary_dimension
        payload["route_layer"] = route_layer
        payload["time_stage"] = time_stage
        return cls(**payload)


@dataclass
class Evidence:
    evidence_id: str
    source_type: str
    source_title: str
    source_id: str = ""
    document_id: str = ""
    source_url: str = ""
    publish_date: str = ""
    quote: str = ""
    summary: str = ""
    evidence_level: str = EvidenceLevel.SPECULATIVE.value
    supports: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    freshness: str = "current"
    extracted_by: str = "human"
    verified_by: str = "rule_verifier"
    metadata: dict[str, Any] = field(default_factory=dict)
    # Step 1 新增:政策事件与因果类型承载字段。
    # event_date 用于政策/事件类证据(如 FOMC 声明发布时刻),空表示非事件证据。
    event_date: str = ""
    # causal_status 标注本证据对应的因果声明强度,空表示未标注(向后兼容)。
    # 取值见 schema.CausalStatus,如 OBSERVATIONAL_ASSOCIATION / TEMPORAL_PRECEDENCE /
    # QUASI_EXPERIMENTAL_EFFECT / PREDICTIVE_CAUSAL / INTERVENTION_READY。
    causal_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Evidence":
        # 兼容历史数据:缺失字段使用默认值,不抛错。
        payload = dict(payload)
        payload.setdefault("event_date", "")
        payload.setdefault("causal_status", "")
        return cls(**payload)


@dataclass
class MutationLog:
    mutation_id: str
    action: str
    target_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str
    operator: str = "human"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MutationLog":
        return cls(**payload)
