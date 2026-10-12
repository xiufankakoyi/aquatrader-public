"""统一研究数据契约。

契约只承载事实、任务和审计数据；研究置信度与交易相关性严格分离。
所有 ``from_dict`` 均忽略未知字段并为缺失字段提供默认值，兼容历史数据。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, TypeVar


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _safe_text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if isfinite(parsed) else default


def _safe_int(value: Any, default: int = 0, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, parsed) if minimum is not None else parsed


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClaimStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    ACTIVE = "ACTIVE"
    TRUSTED = "TRUSTED"
    CONFLICTED = "CONFLICTED"
    REJECTED = "REJECTED"
    DORMANT = "DORMANT"
    PRUNED = "PRUNED"


class EvidenceStance(str, Enum):
    SUPPORT = "support"
    COUNTER = "counter"
    NEUTRAL = "neutral"
    STALE = "stale"
    RETRACTED = "retracted"


class FeedbackDomain(str, Enum):
    KNOWLEDGE = "knowledge"
    RETRIEVAL = "retrieval"
    ALPHA = "alpha"


# 任务类型常量（与 ResearchTask.task_type 字段配套使用）。
# 现有默认类型为 evidence_completion；dynamic_theme_review 用于
# Research 侧处理 QUANT 上报的 ThemePacket v1 并产出增强后的 v2。
TASK_TYPE_EVIDENCE_COMPLETION = "evidence_completion"
TASK_TYPE_DYNAMIC_THEME_REVIEW = "dynamic_theme_review"


@dataclass
class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]):
        allowed = {item.name for item in fields(cls)}
        source = payload if isinstance(payload, Mapping) else {}
        return cls(**{key: value for key, value in source.items() if key in allowed})


@dataclass
class TaskBudget(Serializable):
    max_requests: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 20_000
    max_cost: float = 20.0
    max_documents: int = 30

    def __post_init__(self) -> None:
        self.max_requests = _safe_int(self.max_requests, 20, minimum=0)
        self.max_seconds = max(0.0, _safe_float(self.max_seconds, 120.0))
        self.max_tokens = _safe_int(self.max_tokens, 20_000, minimum=0)
        self.max_cost = max(0.0, _safe_float(self.max_cost, 20.0))
        self.max_documents = _safe_int(self.max_documents, 30, minimum=0)


@dataclass
class ResearchTask(Serializable):
    task_id: str = field(default_factory=lambda: new_id("rt"))
    task_type: str = "evidence_completion"
    trigger_source: str = "manual"
    target_claim_id: str = ""
    priority: float = 0.5
    deadline_at: str = ""
    max_retries: int = 2
    retry_count: int = 0
    budget: dict[str, Any] = field(default_factory=lambda: TaskBudget().to_dict())
    parent_task_id: str = ""
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    expires_at: str = ""               # 过期时间，TaskRunner 取任务时检查
    status: str = TaskStatus.QUEUED.value
    params: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=lambda: {"percent": 0, "message": "等待执行"})
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = field(default_factory=utc_now)
    # Phase 3: 跨日续跑支持
    # run_id 标识同一 Claim 的跨日执行链，避免重复创建 Claim
    run_id: str = field(default_factory=lambda: new_id("run"))
    # 当前 attempt 编号，每次执行递增
    attempt_number: int = 0
    # 当前 attempt 的 ID（独立于 task_id）
    attempt_id: str = ""
    # 当前 attempt 开始/结束时间
    attempt_started_at: str = ""
    attempt_finished_at: str = ""
    # checkpoint：保存跨日恢复所需的全部状态
    # completed_stages: 已完成的检索阶段（local/first_party/second_hand/web）
    # seen_document_ids: 已处理的文档 ID 集合（避免重复抓取）
    # saved_evidence_ids: 已保存的证据 ID 集合
    # consumed_budget: 已消耗的预算快照
    # failure_type: 失败类型（budget_exhausted/first_party_unavailable/exception）
    # next_action: 下一步动作（resume_first_party/continue_second_hand/...）
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.task_id = _safe_text(self.task_id) or new_id("rt")
        self.task_type = _safe_text(self.task_type, TASK_TYPE_EVIDENCE_COMPLETION)
        self.trace_id = _safe_text(self.trace_id) or new_id("trace")
        self.run_id = _safe_text(self.run_id) or new_id("run")
        self.budget = _as_mapping(self.budget)
        self.params = _as_mapping(self.params)
        self.failure = _as_mapping(self.failure)
        self.progress = _as_mapping(self.progress)
        self.checkpoint = _as_mapping(self.checkpoint)
        self.priority = _safe_float(self.priority, 0.5)
        self.max_retries = _safe_int(self.max_retries, 2, minimum=0)
        self.retry_count = _safe_int(self.retry_count, 0, minimum=0)
        self.attempt_number = _safe_int(self.attempt_number, 0, minimum=0)


@dataclass
class SourceTrace(Serializable):
    source_trace_id: str = field(default_factory=lambda: new_id("src"))
    original_source: str = ""
    canonical_url: str = ""
    lineage_id: str = ""
    repost_of: str = ""
    provider: str = "unknown"
    source_type: str = "unknown"
    license_policy: str = "unknown"
    robots_policy: str = "unknown"
    rate_limit_policy: str = "default"
    availability_status: str = "unknown"
    retrieved_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source_trace_id = _safe_text(self.source_trace_id) or new_id("src")
        self.original_source = _safe_text(self.original_source)
        self.canonical_url = _safe_text(self.canonical_url)
        self.lineage_id = _safe_text(self.lineage_id)
        self.metadata = _as_mapping(self.metadata)
        if not self.lineage_id:
            raw = self.canonical_url or self.original_source or self.source_trace_id
            self.lineage_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@dataclass
class DocumentVersion(Serializable):
    document_version_id: str = field(default_factory=lambda: new_id("dv"))
    document_id: str = field(default_factory=lambda: new_id("doc"))
    version: int = 1
    content_hash: str = ""
    title: str = ""
    content_text: str = ""
    source_trace_id: str = ""
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)
    content_available: bool = False
    parse_status: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.document_version_id = _safe_text(self.document_version_id) or new_id("dv")
        self.document_id = _safe_text(self.document_id) or new_id("doc")
        self.content_text = self.content_text if isinstance(self.content_text, str) else ""
        self.content_hash = _safe_text(self.content_hash)
        self.version = _safe_int(self.version, 1, minimum=1)
        self.metadata = _as_mapping(self.metadata)
        if not self.content_hash and self.content_text:
            self.content_hash = hashlib.sha256(self.content_text.encode("utf-8")).hexdigest()
        self.content_available = _safe_bool(self.content_available) or bool(self.content_text)


@dataclass
class Claim(Serializable):
    claim_id: str = field(default_factory=lambda: new_id("claim"))
    subject_id: str = ""
    subject_name: str = ""
    relation: str = "related_to"
    object_id: str = ""
    object_name: str = ""
    direction: str = "forward"
    valid_from: str = ""
    valid_to: str = ""
    research_status: str = ClaimStatus.DISCOVERED.value
    evidence_confidence: float = 0.0
    trading_relevance: float = 0.0
    mapping_confidence: float = 0.0
    conflict_status: str = "none"
    required_slots: list[str] = field(default_factory=lambda: ["identity", "relationship"])
    support_terms: list[str] = field(default_factory=list)
    counter_terms: list[str] = field(default_factory=list)
    counter_search_completed: bool = False
    counter_evidence_evaluated: bool = False
    counter_search_executed: bool = False
    executed_zero_results: bool = False
    high_impact: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.claim_id = _safe_text(self.claim_id) or new_id("claim")
        for name in (
            "subject_id", "subject_name", "relation", "object_id", "object_name",
            "direction", "valid_from", "valid_to", "research_status", "conflict_status",
        ):
            setattr(self, name, _safe_text(getattr(self, name, None)))
        self.evidence_confidence = _safe_float(self.evidence_confidence)
        self.trading_relevance = _safe_float(self.trading_relevance)
        self.mapping_confidence = _safe_float(self.mapping_confidence)
        self.counter_search_completed = _safe_bool(self.counter_search_completed)
        self.counter_evidence_evaluated = _safe_bool(self.counter_evidence_evaluated)
        self.counter_search_executed = _safe_bool(self.counter_search_executed)
        self.executed_zero_results = _safe_bool(self.executed_zero_results)
        self.high_impact = _safe_bool(self.high_impact)
        self.required_slots = _text_list(self.required_slots)
        self.support_terms = _text_list(self.support_terms)
        self.counter_terms = _text_list(self.counter_terms)
        self.attributes = _as_mapping(self.attributes)


@dataclass
class EvidenceRecord(Serializable):
    evidence_id: str = field(default_factory=lambda: new_id("ev"))
    claim_id: str = ""
    document_version_id: str = ""
    source_trace_id: str = ""
    lineage_id: str = ""
    stance: str = EvidenceStance.NEUTRAL.value
    evidence_kind: str = "document"
    quote: str = ""
    locator: dict[str, Any] = field(default_factory=dict)
    source_quality: float = 0.5
    match_score: float = 0.0
    correlation_group: str = ""
    published_at: str = ""
    retrieved_at: str = field(default_factory=utc_now)
    valid_from: str = ""
    valid_to: str = ""
    extraction_method: str = "unknown"
    grounding_status: str = "unknown"
    is_first_party: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.evidence_id = _safe_text(self.evidence_id) or new_id("ev")
        for name in (
            "claim_id", "document_version_id", "source_trace_id", "lineage_id", "stance",
            "evidence_kind", "quote", "correlation_group", "published_at", "retrieved_at",
            "valid_from", "valid_to", "extraction_method", "grounding_status",
        ):
            setattr(self, name, _safe_text(getattr(self, name, None)))
        self.locator = _as_mapping(self.locator)
        self.metadata = _as_mapping(self.metadata)
        self.source_quality = _safe_float(self.source_quality, 0.5)
        self.match_score = _safe_float(self.match_score)
        self.is_first_party = _safe_bool(self.is_first_party)

    @property
    def can_affect_confidence(self) -> bool:
        return not bool((self.metadata or {}).get("invalidated")) and self.evidence_kind not in {
            "generated", "pseudo_evidence", "retrieval_hypothesis", "query_prototype"
        } and self.stance in {EvidenceStance.SUPPORT.value, EvidenceStance.COUNTER.value}


@dataclass
class StateTransition(Serializable):
    transition_id: str = field(default_factory=lambda: new_id("tr"))
    claim_id: str = ""
    candidate_id: str = ""
    from_status: str = ""
    to_status: str = ""
    reason: str = ""
    reason_codes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    decision_score: float = 0.0
    threshold_version: str = ""
    strategy_version: str = ""
    evidence_snapshot: dict[str, Any] = field(default_factory=dict)
    policy_version: str = "research-policy-v1"
    operator: str = "system"
    changed_at: str = field(default_factory=utc_now)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.transition_id = _safe_text(self.transition_id) or new_id("tr")
        self.changed_at = _safe_text(self.changed_at, _safe_text(self.created_at) or utc_now())
        self.reason_codes = _text_list(self.reason_codes)
        self.evidence_ids = _text_list(self.evidence_ids)
        self.evidence_snapshot = _as_mapping(self.evidence_snapshot)
        self.decision_score = _safe_float(self.decision_score)


@dataclass
class ReviewItem(Serializable):
    review_id: str = field(default_factory=lambda: new_id("review"))
    claim_id: str = ""
    reason: str = ""
    priority: str = "medium"
    status: str = "pending"
    required_action: str = "人工复核"
    resolution: str = ""
    reviewer: str = ""
    created_at: str = field(default_factory=utc_now)
    resolved_at: str = ""

    def __post_init__(self) -> None:
        self.review_id = _safe_text(self.review_id) or new_id("review")
        for name, default in (
            ("claim_id", ""), ("reason", ""), ("priority", "medium"),
            ("status", "pending"), ("required_action", "人工复核"),
            ("resolution", ""), ("reviewer", ""), ("created_at", utc_now()),
            ("resolved_at", ""),
        ):
            setattr(self, name, _safe_text(getattr(self, name, None), default))


@dataclass
class FeedbackEvent(Serializable):
    feedback_id: str = field(default_factory=lambda: new_id("fb"))
    domain: str = FeedbackDomain.KNOWLEDGE.value
    event_type: str = ""           # 兼容旧字段，保持原有语义
    event_kind: str = ""           # 事件细分类别，如 node_activation / path_outcome
    target_id: str = ""
    outcome: str = ""              # 事件结果：positive / negative / neutral
    metrics: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)
    generated_task_id: str = ""
    # 防循环控制字段（持久化到 SQLite）
    trace_id: str = ""
    parent_task_id: str = ""
    feedback_depth: int = 0
    dedup_key: str = ""
    expires_at: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.feedback_id = _safe_text(self.feedback_id) or new_id("fb")
        self.domain = _safe_text(self.domain, FeedbackDomain.KNOWLEDGE.value)
        for name in (
            "event_type", "event_kind", "target_id", "outcome", "generated_task_id",
            "trace_id", "parent_task_id", "dedup_key", "expires_at", "created_at",
        ):
            setattr(self, name, _safe_text(getattr(self, name, None)))
        self.metrics = _as_mapping(self.metrics)
        self.attribution = _as_mapping(self.attribution)
        self.feedback_depth = _safe_int(self.feedback_depth, 0, minimum=0)


@dataclass
class CalibrationRun(Serializable):
    calibration_id: str = field(default_factory=lambda: new_id("cal"))
    domain: str = "source_quality"
    policy_version: str = ""
    status: str = "shadow"
    sample_size: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    bounds: dict[str, Any] = field(default_factory=dict)
    approved_by: str = ""
    created_at: str = field(default_factory=utc_now)
    activated_at: str = ""

    def __post_init__(self) -> None:
        self.calibration_id = _safe_text(self.calibration_id) or new_id("cal")
        self.domain = _safe_text(self.domain, "source_quality")
        self.policy_version = _safe_text(self.policy_version)
        self.status = _safe_text(self.status, "shadow")
        self.approved_by = _safe_text(self.approved_by)
        self.created_at = _safe_text(self.created_at) or utc_now()
        self.activated_at = _safe_text(self.activated_at)
        self.sample_size = _safe_int(self.sample_size, 0, minimum=0)
        self.metrics = _as_mapping(self.metrics)
        self.parameters = _as_mapping(self.parameters)
        self.bounds = _as_mapping(self.bounds)


@dataclass
class PolicyVersion(Serializable):
    """版本化策略参数，治理元数据与参数分离。

    parameters 只包含纯参数；code_version/test_run_id/approved_by 等治理
    元数据独立存放。metrics_snapshot 保留在 CalibrationRun，不在此重复。
    """
    policy_version: str = field(default_factory=lambda: new_id("policy"))
    domain: str = "research"
    status: str = "draft"               # draft / shadow / active / superseded / rolled_back
    parameters: dict[str, Any] = field(default_factory=dict)
    activation_criteria: dict[str, Any] = field(default_factory=dict)
    code_version: str = ""              # 激活时 git commit hash
    test_run_id: str = ""               # 关联的 TestRunRecord
    approved_by: str = ""
    approved_at: str = ""
    previous_version: str = ""
    created_at: str = field(default_factory=utc_now)
    activated_at: str = ""

    def __post_init__(self) -> None:
        self.policy_version = _safe_text(self.policy_version) or new_id("policy")
        self.domain = _safe_text(self.domain, "research")
        self.status = _safe_text(self.status, "draft")
        for name in (
            "code_version", "test_run_id", "approved_by", "approved_at",
            "previous_version", "created_at", "activated_at",
        ):
            setattr(self, name, _safe_text(getattr(self, name, None)))
        self.parameters = _as_mapping(self.parameters)
        self.activation_criteria = _as_mapping(self.activation_criteria)
