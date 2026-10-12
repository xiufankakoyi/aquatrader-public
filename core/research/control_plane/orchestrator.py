"""Perilla 统一检索控制面。所有层级结果均先入统一 Evidence/Claim 评估。"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from .contracts import (
    Claim, ClaimStatus, EvidenceRecord, EvidenceStance, ResearchTask, ReviewItem, StateTransition,
    TaskBudget, TaskStatus, _as_mapping, _safe_bool, _safe_float, _safe_int,
    _safe_text,
    _text_list, utc_now,
)
from .policies import ClaimPolicy, SufficiencyResult
from .repository import ResearchRepository
from core.research_graph.tiered_evidence_searcher import build_tiered_queries

logger = logging.getLogger(__name__)


class Retriever(Protocol):
    def search(self, query: str, *, tier: str, limit: int) -> list[Any]: ...


class Extractor(Protocol):
    def extract(self, document: Any, claim: Claim, *, query_purpose: str) -> list[EvidenceRecord]: ...


def _as_documents(value: Any) -> list[Any]:
    """Normalize retriever/extractor outputs without treating mappings as rows."""
    if isinstance(value, Mapping):
        value = value.get("documents", [])
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


# Phase 3: 结构化停止原因码
# 替代纯字符串 stopped_reason，提供可机读的原因分类
STOP_REASON_CODES: dict[str, str] = {
    "local_sufficient": "本地已有合格证据，跳过网络检索",
    "budget_exhausted": "全局预算耗尽",
    "budget_insufficient": "全局预算不足，分配最低保障",
    "first_party_unavailable": "第一方专线不可用",
    "first_party_degraded": "第一方专线部分降级",
    "claim_sufficient": "Claim 已满足统一充分性条件",
    "marginal_low": "边际检索价值过低",
    "all_tiers_done": "所有层级完成，证据仍不充分",
    "claim_not_found": "目标 Claim 不存在",
    "exception": "执行异常",
}


@dataclass
class SearchTier:
    name: str
    limit: int
    source_types: tuple[str, ...] = ()
    max_queries: int = 5
    query_suffixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.name = _safe_text(self.name)
        self.limit = _safe_int(self.limit, 0, minimum=0)
        self.max_queries = _safe_int(self.max_queries, 0, minimum=0)
        self.source_types = tuple(_text_list(self.source_types))
        self.query_suffixes = tuple(_text_list(self.query_suffixes))


@dataclass
class ExecutionSummary:
    task_id: str
    claim_id: str
    status: str
    tiers_executed: list[str] = field(default_factory=list)
    requests_used: int = 0
    documents_seen: int = 0
    evidence_saved: int = 0
    stopped_reason: str = ""
    # Phase 3: 结构化停止原因码（local_sufficient/budget_exhausted/...）
    stopped_reason_code: str = ""
    sufficiency: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # Phase 3: 本地检索短路信息
    local_retrieval_hit: bool = False
    # Phase 3: attempt 信息
    attempt_id: str = ""
    attempt_number: int = 0
    # Phase 3: 已完成的检索阶段（local/first_party/second_hand/web）
    completed_stages: list[str] = field(default_factory=list)
    degradation_codes: list[str] = field(default_factory=list)
    first_party_status: dict[str, Any] = field(default_factory=dict)
    budget_snapshot: dict[str, Any] = field(default_factory=dict)
    skipped_stages: list[str] = field(default_factory=list)
    market_documents_ignored: int = 0
    snippet_documents_ignored: int = 0
    duplicate_documents_skipped: int = 0


class BudgetMeter:
    def __init__(self, budget: TaskBudget) -> None:
        self.budget = budget if isinstance(budget, TaskBudget) else TaskBudget.from_dict(
            _as_mapping(budget)
        )
        self.started = time.monotonic()
        self.requests = 0
        self.documents = 0
        self.tokens = 0
        self.cost = 0.0

    def can_request(self) -> bool:
        return (
            self.requests < self.budget.max_requests
            and self.documents < self.budget.max_documents
            and self.tokens < self.budget.max_tokens
            and self.cost < self.budget.max_cost
            and time.monotonic() - self.started < self.budget.max_seconds
        )

    def reason(self) -> str:
        if self.requests >= self.budget.max_requests: return "请求预算耗尽"
        if self.documents >= self.budget.max_documents: return "文档预算耗尽"
        if self.tokens >= self.budget.max_tokens: return "Token 预算耗尽"
        if self.cost >= self.budget.max_cost: return "费用预算耗尽"
        if time.monotonic() - self.started >= self.budget.max_seconds: return "时间预算耗尽"
        return ""


class ResearchOrchestrator:
    POLICY_VERSION = ClaimPolicy.VERSION
    # 默认三层配置：高/中/低质量来源
    TIER_SUFFIXES = {
        "high": ("公告", "财报", "官方", "年报", "半年报"),
        "medium": ("新闻", "报道", "供应链", "最新消息"),
        "low": ("论坛", "博客", "讨论", "分析"),
        "second_hand": ("新闻", "报道", "券商研报", "行业分析"),
        "web": ("官网", "公告", "新闻", "报道"),
    }
    DEFAULT_TIERS = (
        SearchTier("high", 5, max_queries=5, query_suffixes=TIER_SUFFIXES["high"]),
        SearchTier("medium", 5, max_queries=4, query_suffixes=TIER_SUFFIXES["medium"]),
        SearchTier("low", 3, max_queries=3, query_suffixes=TIER_SUFFIXES["low"]),
    )
    # 下一层检索成本：低成本 tier 可继续，高成本低质量 tier 应谨慎
    TIER_COST = {"high": 0.2, "medium": 0.5, "low": 0.9}

    def __init__(
        self,
        repository: ResearchRepository,
        retriever: Retriever,
        extractor: Extractor,
        *,
        cache: Any | None = None,
        policy: ClaimPolicy | None = None,
        tiers: tuple[SearchTier, ...] | None = None,
        fact_outcome_store: Any | None = None,
        feedback_controller: Any | None = None,
        # Phase 3: 本地检索短路器
        local_retriever: Any | None = None,
        # Phase 3: 第一方专线检索器
        first_party_retriever: Any | None = None,
        # Phase 3: 全局预算管理器
        global_budget_manager: Any | None = None,
        # Phase 3: 跨日续跑恢复器
        task_resumer: Any | None = None,
    ) -> None:
        """初始化 Orchestrator。

        Args:
            repository: 研究仓库。
            retriever: 检索器。
            extractor: 证据抽取器。
            cache: 搜索缓存适配器。
            policy: Claim 充分性策略。
            tiers: 搜索层级配置。
            fact_outcome_store: 事实结果存储，用于晋升 hook。
            feedback_controller: 反馈控制器，用于检索指标反馈。
            local_retriever: Phase 3 本地证据检索短路器。
            first_party_retriever: Phase 3 第一方专线检索器。
            global_budget_manager: Phase 3 全局预算管理器。
            task_resumer: Phase 3 跨日续跑恢复器。
        """
        self.repository = repository
        self.retriever = retriever
        self.extractor = extractor
        self.cache = cache
        self.policy = policy or ClaimPolicy()
        configured_tiers = tuple(
            tier for tier in (tiers or self.DEFAULT_TIERS)
            if isinstance(tier, SearchTier) and tier.name
        )
        self.tiers = configured_tiers or self.DEFAULT_TIERS
        self.fact_outcome_store = fact_outcome_store
        self.feedback_controller = feedback_controller
        # Phase 3 组件（可选，向后兼容）
        self.local_retriever = local_retriever
        self.first_party_retriever = first_party_retriever
        self.global_budget_manager = global_budget_manager
        self.task_resumer = task_resumer

    def execute(self, task: ResearchTask) -> ExecutionSummary:
        if not isinstance(task, ResearchTask):
            raise ValueError("research task type is invalid")
        task.params = _as_mapping(task.params)
        claim = self.repository.get_claim(task.target_claim_id)
        if claim is None:
            task.status = TaskStatus.FAILED.value
            task.failure = {"code": "CLAIM_NOT_FOUND", "message": "目标 Claim 不存在"}
            task.finished_at = utc_now()
            self.repository.save_task(task)
            return ExecutionSummary(
                task.task_id, task.target_claim_id, task.status,
                stopped_reason="目标 Claim 不存在",
                stopped_reason_code="claim_not_found",
            )

        # Each execute call owns exactly one real attempt. With the Phase 3
        # resumer, claiming the task and creating that attempt are one SQLite
        # transaction so two workers cannot execute the same queued snapshot.
        claim_attempt = (
            getattr(self.task_resumer, "claim_attempt", None)
            if self.task_resumer is not None else None
        )
        if callable(claim_attempt):
            attempt = claim_attempt(task)
            if attempt is None:
                current = self.repository.get_task(task.task_id)
                return ExecutionSummary(
                    task.task_id,
                    task.target_claim_id,
                    current.status if current is not None else task.status,
                    stopped_reason="任务已被其他执行器领取",
                    stopped_reason_code="task_already_running",
                    attempt_id=current.attempt_id if current is not None else "",
                    attempt_number=(
                        current.attempt_number if current is not None
                        else task.attempt_number
                    ),
                )
        else:
            resume_pending = _safe_bool(task.params.pop("_resume_pending", False))
            legacy_placeholder = _safe_bool(
                task.params.get("_legacy_resume_placeholder")
            )
            if not resume_pending and not legacy_placeholder:
                task.attempt_number = task.attempt_number + 1
            if not legacy_placeholder:
                from .contracts import new_id
                task.attempt_id = new_id("att")
            task.attempt_started_at = utc_now()
            task.attempt_finished_at = ""
            task.status = TaskStatus.RUNNING.value
            task.started_at = task.started_at or utc_now()
            task.progress = {"percent": 1, "message": "开始统一检索"}
            if self.task_resumer is not None and callable(
                getattr(self.task_resumer, "start_attempt", None)
            ):
                self.task_resumer.start_attempt(task)
            if legacy_placeholder:
                task.params.pop("_legacy_resume_placeholder", None)
            self.repository.save_task(task)

        summary = ExecutionSummary(
            task.task_id, claim.claim_id, task.status,
            attempt_id=task.attempt_id,
            attempt_number=task.attempt_number,
        )

        # Phase 3: 已完成的检索阶段（用于 attempt checkpoint）
        checkpoint = _as_mapping(task.checkpoint)
        completed_stages: list[str] = _text_list(checkpoint.get("completed_stages"))
        seen_document_ids: set[str] = set(_text_list(checkpoint.get("seen_document_ids")))
        saved_evidence_ids: set[str] = set(_text_list(checkpoint.get("saved_evidence_ids")))
        completed_query_keys: set[str] = set(
            _text_list(checkpoint.get("completed_query_keys"))
        )
        seen_document_fingerprints: set[str] = set(
            _text_list(checkpoint.get("seen_document_fingerprints"))
        )
        budget_by_date_payload = _as_mapping(checkpoint.get("budget_by_date"))
        initial_budget_by_date: dict[str, dict[str, Any]] = {
            str(day): _as_mapping(values)
            for day, values in budget_by_date_payload.items()
            if isinstance(values, Mapping)
        }
        existing_evidence_signatures: set[str] = set()
        try:
            existing_rows = self.repository.list_evidence(
                claim.claim_id, limit=1000
            ) or []
            if not isinstance(existing_rows, (list, tuple, set)):
                existing_rows = []
            for existing in existing_rows:
                if not isinstance(existing, EvidenceRecord):
                    continue
                if existing.evidence_id:
                    saved_evidence_ids.add(existing.evidence_id)
                existing_evidence_signatures.add(
                    self._evidence_signature(existing)
                )
        except Exception:
            # A read-only deduplication aid must never block research.
            existing_evidence_signatures = set()

        def _add_stage(name: str) -> None:
            if name and name not in completed_stages:
                completed_stages.append(name)

        def _persist_checkpoint(
            current_stage: str,
            *,
            next_action: str = "continue",
            failure_type: str = "",
        ) -> None:
            budget_date = ""
            day_getter = getattr(self.global_budget_manager, "_budget_day", None)
            if callable(day_getter):
                try:
                    budget_date = str(day_getter())
                except Exception:
                    budget_date = ""
            if not budget_date:
                budget_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
            consumed_budget = {
                "requests": summary.requests_used,
                "documents": summary.documents_seen,
            }
            budget_by_date = {
                day: dict(values) for day, values in initial_budget_by_date.items()
            }
            previous_day = dict(initial_budget_by_date.get(budget_date, {}))
            merged_day = dict(previous_day)
            for key, value in consumed_budget.items():
                previous_value = previous_day.get(key, 0)
                if isinstance(value, (int, float)) and isinstance(
                    previous_value, (int, float)
                ):
                    merged_day[key] = previous_value + value
                else:
                    merged_day[key] = value
            budget_by_date[budget_date] = merged_day
            checkpoint.update({
                "checkpoint_version": 1,
                "current_stage": current_stage,
                "completed_stages": list(completed_stages),
                "completed_query_keys": sorted(completed_query_keys),
                "seen_document_ids": sorted(seen_document_ids),
                "seen_document_fingerprints": sorted(seen_document_fingerprints),
                "saved_evidence_ids": sorted(saved_evidence_ids),
                "consumed_budget": consumed_budget,
                "budget_by_date": budget_by_date,
                "source_status": dict(summary.first_party_status),
                "retrieval_stats": {
                    "documents_seen": summary.documents_seen,
                    "evidence_saved": summary.evidence_saved,
                    "snippet_documents_ignored": summary.snippet_documents_ignored,
                    "market_documents_ignored": summary.market_documents_ignored,
                    "duplicate_documents_skipped": summary.duplicate_documents_skipped,
                },
                "degradation_codes": list(dict.fromkeys(summary.degradation_codes)),
                "failure_type": failure_type,
                "next_action": next_action,
            })
            task.checkpoint = checkpoint
            updater = getattr(self.task_resumer, "update_checkpoint", None)
            if callable(updater):
                updater(
                    task,
                    current_stage=current_stage,
                    completed_stages=list(completed_stages),
                    completed_query_keys=sorted(completed_query_keys),
                    seen_document_ids=sorted(seen_document_ids),
                    seen_document_fingerprints=sorted(seen_document_fingerprints),
                    saved_evidence_ids=sorted(saved_evidence_ids),
                    consumed_budget=consumed_budget,
                    budget_by_date=budget_by_date,
                    source_status=dict(summary.first_party_status),
                    retrieval_stats=dict(checkpoint["retrieval_stats"]),
                    degradation_codes=list(checkpoint["degradation_codes"]),
                    failure_type=failure_type,
                    next_action=next_action,
                )
            else:
                self.repository.save_task(task)

        # Phase 3: 本地检索短路
        # 在进入网络检索前先查本地是否已有合格证据
        if self.local_retriever is not None and "local" not in completed_stages:
            try:
                local_result = self.local_retriever.query(claim)
                _add_stage("local")
                if local_result.hit:
                    # 本地已有合格证据，跳过所有网络检索
                    summary.local_retrieval_hit = True
                    summary.skipped_stages = _text_list(
                        getattr(local_result, "skipped_stages", [])
                    )
                    summary.stopped_reason = STOP_REASON_CODES["local_sufficient"]
                    summary.stopped_reason_code = "local_sufficient"
                    summary.completed_stages = completed_stages
                    summary.sufficiency = {
                        "independent_sources": local_result.independent_sources,
                        "first_party_sources": local_result.first_party_sources,
                        "support_count": local_result.support_count,
                        "counter_count": local_result.counter_count,
                        "sufficient": True,
                    }
                    _persist_checkpoint("local", next_action="none")
                    task.status = TaskStatus.SUCCEEDED.value
                    task.finished_at = utc_now()
                    task.progress = {"percent": 100, "message": summary.stopped_reason}
                    self.repository.save_task(task)
                    self._finalize_attempt(
                        task, status="succeeded",
                        completed_stages=completed_stages,
                        seen_document_ids=list(seen_document_ids),
                        saved_evidence_ids=list(saved_evidence_ids),
                        consumed_budget={"requests": 0, "documents": 0},
                        next_action="none",
                    )
                    summary.status = task.status
                    snapshot = getattr(self.global_budget_manager, "snapshot", None)
                    if callable(snapshot):
                        try:
                            value = snapshot()
                            summary.budget_snapshot = (
                                value.to_dict()
                                if hasattr(value, "to_dict")
                                else dict(value)
                                if isinstance(value, dict)
                                else {}
                            )
                        except Exception:
                            summary.budget_snapshot = {}
                    return summary
                _persist_checkpoint("local", next_action="continue_first_party")
            except Exception as exc:
                summary.errors.append({"type": type(exc).__name__, "message": f"local_retriever: {exc}"})
                _persist_checkpoint(
                    "local", next_action="continue_first_party", failure_type="local_query_failed"
                )
                # 本地检索失败不阻断主流程，继续网络检索

        # Reserve network budget only after the local short-circuit.  A local
        # sufficient hit therefore has zero reservation/consumption, even
        # when a global budget manager is configured.
        requested_budget = TaskBudget.from_dict(task.budget)
        allocated_budget = requested_budget
        budget_denied_reason = ""
        reservation_id = ""
        if self.global_budget_manager is not None:
            from .budget_manager import BudgetAllocationResult
            reserve = getattr(self.global_budget_manager, "reserve", None)
            if callable(reserve):
                allocation: BudgetAllocationResult = reserve(
                    requested_budget,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    action_id="research_network",
                )
            else:
                allocation = self.global_budget_manager.allocate(requested_budget)
            reservation_id = str(getattr(allocation, "reservation_id", "") or "")
            if not allocation.granted:
                _persist_checkpoint(
                    checkpoint.get("current_stage") or "local",
                    next_action="retry_later",
                    failure_type=allocation.reason_code,
                )
                task.status = TaskStatus.FAILED.value
                task.failure = {
                    "code": allocation.reason_code,
                    "message": allocation.reason_detail,
                }
                task.finished_at = utc_now()
                task.progress = {"percent": 0, "message": allocation.reason_detail}
                self.repository.save_task(task)
                self._finalize_attempt(
                    task,
                    status="failed",
                    completed_stages=completed_stages,
                    seen_document_ids=list(seen_document_ids),
                    saved_evidence_ids=list(saved_evidence_ids),
                    consumed_budget={
                        "requests": summary.requests_used,
                        "documents": summary.documents_seen,
                    },
                    failure_type=allocation.reason_code,
                    failure_detail=allocation.reason_detail,
                    next_action="retry_later",
                )
                snapshot = getattr(self.global_budget_manager, "snapshot", None)
                if callable(snapshot):
                    try:
                        value = snapshot()
                        denied_snapshot = (
                            value.to_dict() if hasattr(value, "to_dict")
                            else dict(value) if isinstance(value, dict) else {}
                        )
                    except Exception:
                        denied_snapshot = {}
                else:
                    denied_snapshot = {}
                return ExecutionSummary(
                    task.task_id,
                    claim.claim_id,
                    task.status,
                    stopped_reason=allocation.reason_detail,
                    stopped_reason_code=allocation.reason_code,
                    attempt_id=task.attempt_id,
                    attempt_number=task.attempt_number,
                    budget_snapshot=denied_snapshot,
                )
            allocated_budget = allocation.allocated
            budget_denied_reason = allocation.reason_detail

        budget = allocated_budget
        meter = BudgetMeter(budget)

        def _document_value(document: Any, name: str, default: Any = "") -> Any:
            if isinstance(document, Mapping):
                return document.get(name, default)
            return getattr(document, name, default)

        def _document_metadata(document: Any) -> dict[str, Any]:
            value = _document_value(document, "metadata", {})
            return dict(value) if isinstance(value, Mapping) else {}

        def _document_key(document: Any) -> str:
            metadata = _document_metadata(document)
            raw = (
                _document_value(document, "document_id")
                or _document_value(document, "url")
                or metadata.get("content_hash")
                or _document_value(document, "text_hash")
            )
            if raw:
                return str(raw)
            body = str(
                _document_value(document, "content_text")
                or _document_value(document, "content")
                or _document_value(document, "text")
                or ""
            )
            return hashlib.sha1(body.encode("utf-8")).hexdigest() if body else ""

        def _document_fingerprint(document: Any) -> str:
            metadata = _document_metadata(document)
            explicit = (
                metadata.get("content_hash")
                or _document_value(document, "text_hash")
            )
            if explicit:
                return str(explicit)
            body = str(
                _document_value(document, "content_text")
                or _document_value(document, "content")
                or _document_value(document, "text")
                or ""
            ).strip()
            return hashlib.sha256(body.encode("utf-8")).hexdigest() if body else ""

        def _is_market_document(document: Any) -> bool:
            source_type = str(_document_value(document, "source_type") or "").lower()
            metadata = _document_metadata(document)
            data_domain = str(metadata.get("data_domain") or "").lower()
            return (
                data_domain in {"market", "market_features", "quote", "kline"}
                or source_type in {
                    "kline", "quote", "market_data", "market_snapshot", "daily_bar",
                    "dragon_tiger", "dragon_tiger_list", "fund_flow", "money_flow",
                    "price_movement", "volume", "turnover", "limit_up",
                }
            )

        def _is_snippet_document(document: Any) -> bool:
            source_type = str(_document_value(document, "source_type") or "").lower()
            metadata = _document_metadata(document)
            body = str(
                _document_value(document, "content_text")
                or _document_value(document, "content")
                or _document_value(document, "text")
                or ""
            ).strip()
            return bool(
                _safe_bool(metadata.get("snippet_only"))
                or _safe_text(metadata.get("content_status")).lower() == "snippet"
                or source_type in {"search_result", "web_search_snippet"}
                or not body
            )

        def _process_documents(
            documents: list[Any],
            *,
            query: str,
            purpose: str,
            tier: SearchTier,
            is_counter: bool = False,
        ) -> tuple[int, bool, int]:
            """Run admissible documents through the shared evidence gate."""
            nonlocal counter_evidence_saved
            nonlocal counter_extractor_invoked
            documents = _as_documents(documents)
            processed = 0
            counter_saved = 0
            for document in documents[: tier.limit]:
                if meter.documents >= budget.max_documents:
                    break
                doc_id = _document_key(document)
                fingerprint = _document_fingerprint(document)
                if (
                    (doc_id and doc_id in seen_document_ids)
                    or (
                        fingerprint
                        and fingerprint in seen_document_fingerprints
                    )
                ):
                    summary.duplicate_documents_skipped += 1
                    continue
                if doc_id:
                    seen_document_ids.add(doc_id)
                if fingerprint:
                    seen_document_fingerprints.add(fingerprint)
                meter.documents += 1
                summary.documents_seen += 1
                processed += 1
                if _is_market_document(document):
                    summary.market_documents_ignored += 1
                    continue
                if _is_snippet_document(document):
                    summary.snippet_documents_ignored += 1
                    if "snippet_only" not in summary.degradation_codes:
                        summary.degradation_codes.append("snippet_only")
                    continue
                if is_counter:
                    counter_docs_seen += 1
                    counter_extractor_invoked = True
                try:
                    extracted = self.extractor.extract(
                        document, claim, query_purpose=purpose
                    )
                except Exception as exc:
                    summary.errors.append({
                        "type": "extractor_error",
                        "document_id": doc_id,
                        "message": str(exc),
                    })
                    if "extractor_error" not in summary.degradation_codes:
                        summary.degradation_codes.append("extractor_error")
                    continue
                extracted_items = (
                    [extracted] if isinstance(extracted, EvidenceRecord)
                    else _as_documents(extracted)
                )
                for item in extracted_items:
                    if not isinstance(item, EvidenceRecord):
                        continue
                    item.quote = _safe_text(item.quote)
                    item.grounding_status = _safe_text(item.grounding_status)
                    if not item.quote or item.grounding_status not in {
                        "exact", "partial", "structured",
                    }:
                        continue
                    item.claim_id = claim.claim_id
                    if not (
                        item.source_trace_id
                        or item.document_version_id
                        or item.lineage_id
                    ):
                        source_raw = str(
                            _document_value(document, "url")
                            or _document_value(document, "document_id")
                            or repr(document)
                        )
                        source_hash = hashlib.sha1(
                            source_raw.encode("utf-8")
                        ).hexdigest()[:20]
                        item.source_trace_id = f"src_inline_{source_hash[:16]}"
                        item.lineage_id = source_hash
                    evidence_signature = self._evidence_signature(item)
                    if (
                        item.evidence_id in saved_evidence_ids
                        or evidence_signature in existing_evidence_signatures
                    ):
                        summary.duplicate_documents_skipped += 1
                        continue
                    item_meta = _as_mapping(item.metadata)
                    item_meta.update({
                        "query_id": query,
                        "purpose": purpose,
                        "evaluation_status": "evaluated",
                        "evaluated_stance": item.stance,
                    })
                    item.metadata = item_meta
                    try:
                        saved = self.repository.save_evidence(item)
                    except Exception as exc:
                        summary.errors.append({
                            "type": "evidence_save_error",
                            "evidence_id": item.evidence_id,
                            "message": str(exc),
                        })
                        if "evidence_save_error" not in summary.degradation_codes:
                            summary.degradation_codes.append("evidence_save_error")
                        continue
                    if saved:
                        summary.evidence_saved += 1
                        saved_evidence_ids.add(item.evidence_id)
                        existing_evidence_signatures.add(evidence_signature)
                        if is_counter and item.stance == EvidenceStance.COUNTER.value:
                            counter_evidence_saved += 1
                            counter_saved += 1
            return processed, bool(counter_extractor_invoked), counter_saved

        counter_evidence_saved = 0
        counter_docs_seen = 0
        counter_extractor_invoked = False
        stop_after_first_party = False

        try:
            # Fixed order: local (above) -> first_party -> legacy
            # second-hand/web tiers.  The legacy path remains intact for old
            # retrievers and APIs.
            if (
                self.first_party_retriever is not None
                and "first_party" not in completed_stages
            ):
                if not meter.can_request():
                    summary.skipped_stages.append("first_party")
                    summary.degradation_codes.append("budget_exhausted:first_party")
                else:
                    first_party_queries = (
                        self._tiered_queries(claim, self.tiers[0])
                        if self.tiers else []
                    )
                    first_party_query = next(
                        (query for query, purpose in first_party_queries if purpose == "support"),
                        first_party_queries[0][0] if first_party_queries else (
                            f"{claim.subject_id} {claim.object_name}".strip()
                        ),
                    )
                    first_party_query_key = (
                        f"first_party|support|{first_party_query}"
                    )
                    if first_party_query_key not in completed_query_keys:
                        context = dict(task.params or {})
                        context.setdefault("as_of", task.params.get("as_of", ""))
                        result = self.first_party_retriever.search(
                            first_party_query,
                            tier="first_party",
                            limit=self.tiers[0].limit if self.tiers else 5,
                            context=context,
                        )
                        meter.requests += 1
                        summary.requests_used += 1
                        completed_query_keys.add(first_party_query_key)
                        _add_stage("first_party")
                        documents = _as_documents(getattr(result, "documents", result))
                        statuses = getattr(result, "source_status", {}) or {}
                        statuses = statuses if isinstance(statuses, Mapping) else {}
                        summary.first_party_status = {
                            str(name): (
                                value.__dict__.copy()
                                if hasattr(value, "__dict__") else dict(value)
                                if isinstance(value, Mapping) else str(value)
                            )
                            for name, value in statuses.items()
                        }
                        summary.degradation_codes.extend(_text_list(
                            getattr(result, "degradation_codes", [])
                        ))
                        reason_code = str(getattr(result, "reason_code", "") or "")
                        if reason_code and reason_code != "first_party_ok":
                            summary.degradation_codes.append(reason_code)
                        _process_documents(
                            documents,
                            query=first_party_query,
                            purpose="support",
                            tier=self.tiers[0] if self.tiers else SearchTier("first_party", 5),
                        )
                    else:
                        _add_stage("first_party")
                        summary.first_party_status = _as_mapping(
                            checkpoint.get("source_status")
                        )
                    _persist_checkpoint("first_party", next_action="continue_second_hand")

                    # First-party output is a complete retrieval stage: run
                    # the same ClaimPolicy gate before spending any budget on
                    # second-hand or web tiers.  This also lets a resumed task
                    # stop immediately when the first-party checkpoint filled
                    # the remaining claim gap.
                    first_party_tier = SearchTier(
                        "first_party",
                        self.tiers[0].limit if self.tiers else 5,
                        max_queries=1,
                    )
                    first_party_decision = self._evaluate_and_transition(
                        claim, first_party_tier
                    )
                    summary.sufficiency = first_party_decision.__dict__
                    if first_party_decision.sufficient:
                        summary.stopped_reason = STOP_REASON_CODES["claim_sufficient"]
                        summary.stopped_reason_code = "claim_sufficient"
                        stop_after_first_party = True
                    _persist_checkpoint(
                        "first_party",
                        next_action=(
                            "none" if stop_after_first_party else "continue_second_hand"
                        ),
                    )

            tiers_to_run = () if stop_after_first_party else self.tiers
            for tier_index, tier in enumerate(tiers_to_run):
                if not meter.can_request():
                    summary.stopped_reason = meter.reason()
                    summary.stopped_reason_code = "budget_exhausted"
                    break
                summary.tiers_executed.append(tier.name)
                tier_queries = self._tiered_queries(claim, tier)
                counter_evidence_saved = 0
                # 反证归因（P0 修复）：仅当反证查询"实际执行"且"结果完成有效评估"
                # 才置位 counter_search_completed / counter_evidence_evaluated。
                # "有效评估"= 零命中 或 至少一条反证证据通过 grounding；
                # "检索到但未形成有效抽取"不视为评估完成。
                # 零命中记录 executed_zero_results，不得与未执行混淆。
                counter_query_executed = False
                counter_query_zero_results = False
                counter_docs_seen = 0
                counter_extractor_invoked = False
                for query, purpose in tier_queries:
                    query_key = f"{tier.name}|{purpose}|{query}"
                    if query_key in completed_query_keys:
                        summary.duplicate_documents_skipped += 1
                        continue
                    if not meter.can_request():
                        summary.stopped_reason = meter.reason()
                        summary.stopped_reason_code = "budget_exhausted"
                        break
                    documents = self._retrieve_cached(claim, query, purpose, tier, task)
                    meter.requests += 1
                    summary.requests_used += 1
                    completed_query_keys.add(query_key)
                    is_counter = purpose == "counter"
                    if is_counter:
                        counter_query_executed = True
                        if not documents:
                            counter_query_zero_results = True
                    for document in documents[: tier.limit]:
                        if meter.documents >= budget.max_documents:
                            break
                        # Phase 3: 记录已见文档 ID
                        doc_id = _document_key(document)
                        fingerprint = _document_fingerprint(document)
                        duplicate_document = (
                            (doc_id and doc_id in seen_document_ids)
                            or (
                                fingerprint
                                and fingerprint in seen_document_fingerprints
                            )
                        )
                        if duplicate_document:
                            # A counter query may return a document already
                            # extracted by an earlier support query.  Do not
                            # extract or bill it twice, but count the counter
                            # query as evaluated against admissible content.
                            if (
                                is_counter
                                and not _is_market_document(document)
                                and not _is_snippet_document(document)
                            ):
                                counter_extractor_invoked = True
                            summary.duplicate_documents_skipped += 1
                            continue
                        if doc_id:
                            seen_document_ids.add(doc_id)
                        if fingerprint:
                            seen_document_fingerprints.add(fingerprint)
                        meter.documents += 1
                        summary.documents_seen += 1
                        # Search snippets and market-only documents are kept
                        # as retrieval diagnostics, never sent to Extractor or
                        # persisted as fact Evidence.
                        if _is_market_document(document):
                            summary.market_documents_ignored += 1
                            continue
                        if _is_snippet_document(document):
                            summary.snippet_documents_ignored += 1
                            if "snippet_only" not in summary.degradation_codes:
                                summary.degradation_codes.append("snippet_only")
                            continue
                        if is_counter:
                            counter_docs_seen += 1
                            counter_extractor_invoked = True
                        try:
                            extracted = self.extractor.extract(
                                document, claim, query_purpose=purpose
                            )
                        except Exception as exc:
                            summary.errors.append({
                                "type": "extractor_error",
                                "document_id": doc_id,
                                "message": str(exc),
                            })
                            if "extractor_error" not in summary.degradation_codes:
                                summary.degradation_codes.append("extractor_error")
                            continue
                        extracted_items = (
                            [extracted] if isinstance(extracted, EvidenceRecord)
                            else _as_documents(extracted)
                        )
                        for item in extracted_items:
                            if not isinstance(item, EvidenceRecord):
                                continue
                            # 搜索摘要/网页不是证据；必须有可定位 quote 且完成 grounding。
                            item.quote = _safe_text(item.quote)
                            item.grounding_status = _safe_text(item.grounding_status)
                            if not item.quote or item.grounding_status not in {"exact", "partial", "structured"}:
                                continue
                            item.claim_id = claim.claim_id
                            if not (
                                item.source_trace_id
                                or item.document_version_id
                                or item.lineage_id
                            ):
                                # A retriever document is still a real source
                                # object even when a legacy extractor omitted
                                # its IDs.  Derive an auditable content identity
                                # rather than silently dropping the evidence.
                                source_raw = str(
                                    getattr(document, "url", "")
                                    or getattr(document, "document_id", "")
                                    or (document.get("url") if isinstance(document, Mapping) else "")
                                    or (document.get("text") if isinstance(document, Mapping) else "")
                                    or repr(document)
                                )
                                source_hash = hashlib.sha1(
                                    source_raw.encode("utf-8")
                                ).hexdigest()[:20]
                                item.source_trace_id = f"src_inline_{source_hash[:16]}"
                                item.lineage_id = source_hash
                            evidence_signature = self._evidence_signature(item)
                            if (
                                item.evidence_id in saved_evidence_ids
                                or evidence_signature in existing_evidence_signatures
                            ):
                                summary.duplicate_documents_skipped += 1
                                continue
                            # 保留反证归因：把 query_id/purpose/evaluation_status/evaluated_stance 写入 evidence metadata。
                            item_meta = _as_mapping(item.metadata)
                            item_meta.update({
                                "query_id": query,
                                "purpose": purpose,
                                "evaluation_status": "evaluated",
                                "evaluated_stance": item.stance,
                            })
                            item.metadata = item_meta
                            try:
                                saved = self.repository.save_evidence(item)
                            except Exception as exc:
                                summary.errors.append({
                                    "type": "evidence_save_error",
                                    "evidence_id": item.evidence_id,
                                    "message": str(exc),
                                })
                                if "evidence_save_error" not in summary.degradation_codes:
                                    summary.degradation_codes.append("evidence_save_error")
                                continue
                            if saved:
                                summary.evidence_saved += 1
                                saved_evidence_ids.add(item.evidence_id)
                                existing_evidence_signatures.add(evidence_signature)
                                if is_counter and item.stance == EvidenceStance.COUNTER.value:
                                    counter_evidence_saved += 1

                # 反证查询已实际执行且结果经有效评估（零命中、或文档已送入 extractor、
                # 或至少一条反证证据通过 grounding），才标记
                # counter_evidence_evaluated / counter_search_completed。
                # "检索到文档但未送入 extractor"（如预算耗尽导致文档被跳过）→ 未完成评估。
                _add_stage(tier.name)
                _persist_checkpoint(tier.name, next_action="continue")
                if counter_query_executed:
                    claim.counter_search_executed = True
                    counter_effectively_evaluated = (
                        counter_query_zero_results
                        or counter_extractor_invoked
                        or counter_evidence_saved > 0
                    )
                    if counter_effectively_evaluated:
                        claim.counter_evidence_evaluated = True
                        claim.counter_search_completed = True
                        if counter_query_zero_results and counter_evidence_saved == 0:
                            claim.executed_zero_results = True
                        elif counter_evidence_saved > 0:
                            claim.executed_zero_results = False
                    self.repository.save_claim(claim)
                decision = self._evaluate_and_transition(claim, tier)
                summary.sufficiency = decision.__dict__
                if decision.sufficient:
                    summary.stopped_reason = "Claim 已满足统一充分性条件"
                    summary.stopped_reason_code = "claim_sufficient"
                    break
                # 边际价值过低时提前停止，即使未达 ACTIVE
                if tier_index >= 1 and decision.marginal_value < 0.15:
                    summary.stopped_reason = (
                        f"{tier.name} 层后边际检索价值过低 ({decision.marginal_value:.2f})，停止继续搜索"
                    )
                    summary.stopped_reason_code = "marginal_low"
                    break
                task.progress = {
                    "percent": min(90, int((tier_index + 1) / len(self.tiers) * 90)),
                    "message": f"{tier.name} 层完成，继续检索",
                }
                self.repository.save_task(task)
                # 记录检索指标反馈到 Retrieval 域
                try:
                    feedback_evidence = self.repository.list_evidence(
                        claim.claim_id, 500
                    ) or []
                except Exception:
                    feedback_evidence = []
                if not isinstance(feedback_evidence, (list, tuple, set)):
                    feedback_evidence = []
                self._record_retrieval_feedback(
                    claim=claim,
                    tier=tier,
                    documents_found=summary.documents_seen,
                    evidence_saved=summary.evidence_saved,
                    independent_sources=len({
                        ev.lineage_id
                        for ev in feedback_evidence
                        if isinstance(ev, EvidenceRecord) and ev.lineage_id
                    }),
                    trace_id=task.trace_id,
                )

            if not summary.stopped_reason:
                summary.stopped_reason = "所有层级完成，证据仍不充分"
                summary.stopped_reason_code = "all_tiers_done"
            task.status = TaskStatus.SUCCEEDED.value if summary.sufficiency.get("sufficient") else TaskStatus.PARTIAL.value
            if task.status == TaskStatus.PARTIAL.value:
                reason_codes = list(summary.sufficiency.get("reason_codes") or [])
                if not reason_codes and summary.stopped_reason_code:
                    reason_codes = [summary.stopped_reason_code]
                task.failure = {
                    "code": "EVIDENCE_INSUFFICIENT",
                    "reason_codes": reason_codes,
                    "stopped_reason_code": summary.stopped_reason_code,
                    "message": summary.stopped_reason,
                }
        except Exception as exc:
            summary.errors.append({"type": type(exc).__name__, "message": str(exc)})
            summary.stopped_reason_code = "exception"
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = TaskStatus.RETRYING.value
                task.failure = summary.errors[-1]
            else:
                task.status = TaskStatus.FAILED.value
                task.failure = summary.errors[-1]
        task.finished_at = utc_now() if task.status not in {TaskStatus.RETRYING.value} else ""
        task.progress = {"percent": 100 if task.finished_at else 90, "message": summary.stopped_reason}
        self.repository.save_task(task)

        # Phase 3: 释放全局预算
        if self.global_budget_manager is not None:
            consumed = TaskBudget(
                max_requests=summary.requests_used,
                max_seconds=0.0,  # 简化：不精确追踪时间
                max_tokens=0,
                max_cost=0.0,
                max_documents=summary.documents_seen,
            )
            consume = getattr(self.global_budget_manager, "consume", None)
            try:
                if reservation_id and callable(consume):
                    consume(reservation_id, consumed)
                    self.global_budget_manager.release(reservation_id)
                else:
                    self.global_budget_manager.release(consumed)
            except Exception as exc:
                summary.errors.append({
                    "type": "budget_release_error",
                    "message": str(exc),
                })
                summary.degradation_codes.append("budget_release_error")
            snapshot = getattr(self.global_budget_manager, "snapshot", None)
            if callable(snapshot):
                try:
                    value = snapshot()
                    summary.budget_snapshot = (
                        value.to_dict() if hasattr(value, "to_dict")
                        else dict(value) if isinstance(value, dict)
                        else {}
                    )
                except Exception:
                    summary.budget_snapshot = {}

        # Phase 3: 完成 attempt 记录
        for stage in summary.tiers_executed:
            _add_stage(stage)
        summary.completed_stages = completed_stages
        attempt_status = "succeeded" if task.status == TaskStatus.SUCCEEDED.value else (
            "partial" if task.status == TaskStatus.PARTIAL.value else "failed"
        )
        failure_type = "" if attempt_status == "succeeded" else summary.stopped_reason_code
        next_action = "none" if attempt_status == "succeeded" else (
            "resume_first_party" if "first_party" in summary.stopped_reason_code else "retry_later"
        )
        self._finalize_attempt(
            task,
            status=attempt_status,
            completed_stages=completed_stages,
            seen_document_ids=list(seen_document_ids),
            saved_evidence_ids=list(saved_evidence_ids),
            consumed_budget={
                "requests": summary.requests_used,
                "documents": summary.documents_seen,
            },
            failure_type=failure_type,
            failure_detail=summary.stopped_reason,
            next_action=next_action,
        )

        summary.status = task.status
        return summary

    def _finalize_attempt(
        self,
        task: ResearchTask,
        *,
        status: str,
        completed_stages: list[str] | None = None,
        seen_document_ids: list[str] | None = None,
        saved_evidence_ids: list[str] | None = None,
        consumed_budget: dict[str, Any] | None = None,
        failure_type: str = "",
        failure_detail: str = "",
        next_action: str = "",
    ) -> None:
        """完成 attempt 记录（委托给 TaskResumer）。

        Args:
            task: 关联的 ResearchTask。
            status: attempt 最终状态。
            completed_stages: 已完成的检索阶段。
            seen_document_ids: 已处理的文档 ID 集合。
            saved_evidence_ids: 已保存的证据 ID 集合。
            consumed_budget: 已消耗的预算快照。
            failure_type: 失败类型。
            failure_detail: 失败详情。
            next_action: 下一步动作建议。
        """
        if self.task_resumer is None:
            return
        try:
            self.task_resumer.finalize_attempt(
                task,
                status=status,
                completed_stages=list(completed_stages or []),
                seen_document_ids=list(seen_document_ids or []),
                saved_evidence_ids=list(saved_evidence_ids or []),
                consumed_budget=dict(consumed_budget or {}),
                failure_type=failure_type,
                failure_detail=failure_detail,
                next_action=next_action,
            )
        except Exception as exc:
            # attempt 记录失败不阻断主流程
            import logging
            logging.getLogger(__name__).warning(
                "finalize_attempt 失败 task=%s: %s", task.task_id, exc
            )

    def _tiered_queries(self, claim: Claim, tier: SearchTier) -> list[tuple[str, str]]:
        """构造当前质量层的查询列表。

        查询构造已抽取到 build_tiered_queries，与 TieredEvidenceSearcher 共享 suffix 与反证模式。
        反证词优先从 claim.counter_terms 读取，使 Claim 级反证配置生效。
        """
        terms: list[str] = []
        support_terms = _text_list(claim.support_terms)
        counter_terms = _text_list(claim.counter_terms)
        if support_terms:
            terms.extend(support_terms)
        else:
            subject = _safe_text(claim.subject_name) or _safe_text(claim.subject_id)
            obj = _safe_text(claim.object_name) or _safe_text(claim.object_id)
            relation = _safe_text(claim.relation)
            base = " ".join(part for part in (subject, relation, obj) if part)
            if base:
                terms.append(base)
        suffixes = tier.query_suffixes or self.TIER_SUFFIXES.get(tier.name, ())
        return build_tiered_queries(
            terms,
            tier_name=tier.name,
            query_suffixes=suffixes,
            max_queries=tier.max_queries,
            counter_terms=counter_terms or None,
        )

    def _retrieve_cached(self, claim: Claim, query: str, purpose: str, tier: SearchTier, task: ResearchTask) -> list[Any]:
        task_params = _as_mapping(getattr(task, "params", {}))
        as_of = _safe_text(task_params.get("as_of"))
        key_payload = {
            "candidate": claim.subject_id, "claim": claim.claim_id, "query": query,
            "purpose": purpose, "tier": tier.name, "engine": type(self.retriever).__name__,
            "content_version": task_params.get("content_version", "latest"),
            "policy": self.POLICY_VERSION, "date": as_of[:10] or utc_now()[:10],
        }
        if self.cache is not None:
            try:
                cached = getattr(self.cache, "get", lambda _key: None)(key_payload)
            except Exception:
                cached = None
            if cached is not None:
                return _as_documents(cached)
        result = self.retriever.search(query, tier=tier.name, limit=tier.limit)
        result = _as_documents(result)
        if self.cache is not None:
            try:
                getattr(self.cache, "set", lambda _key, _value: None)(key_payload, result)
            except Exception:
                pass
        return result

    @staticmethod
    def _normalize_importance(value: Any) -> float:
        """把 importance 归一化为 [0,1] 的浮点数。"""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = float(value)
            if math.isfinite(parsed):
                return max(0.0, min(1.0, parsed))
        mapping = {"high": 0.9, "medium": 0.5, "low": 0.2}
        return mapping.get(str(value).lower(), 0.5)

    @staticmethod
    def _evidence_signature(item: Any) -> str:
        """Stable identity for idempotent Evidence persistence."""
        raw = "|".join(
            (
                str(getattr(item, "claim_id", "") or ""),
                str(getattr(item, "lineage_id", "") or ""),
                str(getattr(item, "source_trace_id", "") or ""),
                str(getattr(item, "stance", "") or ""),
                str(getattr(item, "quote", "") or "").strip(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _evaluate_and_transition(self, claim: Claim, tier: SearchTier) -> SufficiencyResult:
        evidence = self.repository.list_evidence(claim.claim_id, limit=1000) or []
        if not isinstance(evidence, (list, tuple, set)):
            evidence = []
        evidence = [item for item in evidence if isinstance(item, EvidenceRecord)]
        claim.attributes = _as_mapping(claim.attributes)
        node_importance = self._normalize_importance(
            claim.attributes.get("importance", 0.5)
        )
        tier_cost = self.TIER_COST.get(tier.name, 0.5)
        result = self.policy.evaluate(
            claim, evidence,
            tier_cost=tier_cost, node_importance=node_importance,
        )
        old = claim.research_status
        progression = (
            ClaimStatus.DISCOVERED.value.lower(),
            ClaimStatus.CANDIDATE.value.lower(),
            ClaimStatus.SUPPORTED.value.lower(),
            ClaimStatus.ACTIVE.value.lower(),
            ClaimStatus.TRUSTED.value.lower(),
        )
        old_normalized = str(old or "").lower()
        target_normalized = str(result.target_status or "").lower()
        if (
            old_normalized in progression
            and target_normalized in progression
            and progression.index(target_normalized)
            < progression.index(old_normalized)
        ):
            result.target_status = old
            result.sufficient = old_normalized in {
                ClaimStatus.ACTIVE.value.lower(),
                ClaimStatus.TRUSTED.value.lower(),
            }
            result.reason_codes.append("monotonic_status_preserved")
            result.reasons.append(
                f"recheck_target={target_normalized}; "
                f"preserved_status={old_normalized}"
            )
        claim.evidence_confidence = result.confidence
        claim.conflict_status = "unresolved" if result.target_status == ClaimStatus.CONFLICTED.value else "none"
        claim.research_status = result.target_status
        # save_claim 自动校验状态迁移并创建基础 StateTransition 审计记录
        self.repository.save_claim(claim)
        if old != claim.research_status:
            # 查询 save_claim 自动创建的最新 transition，补充 rich context
            transitions = self.repository.list_transitions(claim.claim_id, limit=1) or []
            if not isinstance(transitions, (list, tuple)):
                transitions = []
            transition_id = ""
            if transitions:
                transition = transitions[0]
                transition_id = transition.transition_id
                # 补充 orchestrator 决策上下文
                evidence_ids = [item.evidence_id for item in evidence if item.can_affect_confidence]
                transition.reason = "；".join(result.reasons) or "满足统一 Claim 充分性策略"
                transition.reason_codes = list(result.reason_codes)
                transition.evidence_ids = evidence_ids
                transition.decision_score = result.decision_score
                transition.threshold_version = self.POLICY_VERSION
                transition.strategy_version = self.POLICY_VERSION
                transition.evidence_snapshot = result.__dict__
                transition.policy_version = self.POLICY_VERSION
                self.repository.save_transition(transition)
            self._on_claim_transition(claim, old, claim.research_status, evidence, result, transition_id)
        if (
            claim.research_status == ClaimStatus.CONFLICTED.value
            or (
                _safe_float(claim.mapping_confidence) < 0.6
                and _safe_text(claim.object_id)
            )
        ):
            self._ensure_review(claim, "高影响冲突或实体映射置信度不足")
        return result

    def request_trusted(self, claim_id: str, operator: str) -> ReviewItem:
        claim = self.repository.get_claim(claim_id)
        if claim is None or claim.research_status != ClaimStatus.ACTIVE.value:
            raise ValueError("只有 ACTIVE Claim 可以申请 TRUSTED")
        return self._ensure_review(claim, "申请升级至 TRUSTED", priority="high")

    # ---- Claim 晋升 hook ----

    _PROMOTION_ORDER: dict[str, int] = {
        "discovered": 0, "candidate": 1, "supported": 2,
        "active": 3, "trusted": 4,
    }

    def _is_promotion(self, old_status: str, new_status: str) -> bool:
        """判断状态迁移是否为晋升（朝向 trusted 方向）。"""
        old_status = _safe_text(old_status).lower()
        new_status = _safe_text(new_status).lower()
        return self._PROMOTION_ORDER.get(new_status, -1) > self._PROMOTION_ORDER.get(old_status, -1)

    def _on_claim_transition(
        self,
        claim: Claim,
        old_status: str,
        new_status: str,
        evidence: list[EvidenceRecord],
        decision: SufficiencyResult,
        transition_id: str,
    ) -> None:
        """Claim 状态迁移 hook — 晋升时自动记录 FactOutcomeRecord。

        只在晋升时调用 FactOutcomeStore.record_promotion，保存完整快照。
        fact_label 只能来源于事实确认，不来源于股价涨跌。

        Args:
            claim: 发生迁移的 Claim。
            old_status: 迁移前状态。
            new_status: 迁移后状态。
            evidence: 参与决策的证据列表。
            decision: 充分性评估结果。
            transition_id: StateTransition ID。
        """
        if not self._is_promotion(old_status, new_status):
            return
        if self.fact_outcome_store is None:
            return
        source_types: list[str] = []
        for item in evidence:
            source_types.append(
                _safe_text(
                    _as_mapping(item.metadata).get("source_type"), "unknown"
                )
            )
        try:
            self.fact_outcome_store.record_promotion(
            transition_id=transition_id,
            claim_id=claim.claim_id,
            candidate_id="",
            promotion_type="claim",
            from_status=old_status,
            to_status=new_status,
            evidence_ids=[item.evidence_id for item in evidence if item.can_affect_confidence],
            source_types=source_types,
            evidence_snapshot={
                "marginal_value": getattr(decision, "marginal_value", 0.0),
                "reason_codes": list(getattr(decision, "reason_codes", [])),
            },
            decision_score=decision.decision_score,
            confidence=decision.confidence,
            independent_sources=decision.independent_sources,
            policy_version=self.POLICY_VERSION,
            promoted_at=utc_now(),
            as_of=_as_mapping(claim.attributes).get("as_of", ""),
            )
        except Exception as exc:
            logger.warning("fact outcome hook failed claim=%s: %s", claim.claim_id, exc)

    def _record_retrieval_feedback(
        self,
        claim: Claim,
        tier: SearchTier,
        documents_found: int,
        evidence_saved: int,
        independent_sources: int,
        trace_id: str = "",
    ) -> None:
        """记录检索指标反馈到 Retrieval 域。

        Args:
            claim: 关联的 Claim。
            tier: 当前搜索层级。
            documents_found: 找到的文档数。
            evidence_saved: 保存的证据数。
            independent_sources: 独立来源数。
            trace_id: 追踪 ID。
        """
        if self.feedback_controller is None:
            return
        from .contracts import FeedbackDomain, FeedbackEvent
        event = FeedbackEvent(
            domain=FeedbackDomain.RETRIEVAL.value,
            event_type="retrieval_metric",
            event_kind="tier_completed",
            target_id=claim.claim_id,
            outcome="neutral",
            metrics={
                "tier": tier.name,
                "documents_found": documents_found,
                "evidence_saved": evidence_saved,
                "independent_sources": independent_sources,
                "claim_status": claim.research_status,
            },
        )
        try:
            self.feedback_controller.record(event, trace_id=trace_id, generate_task=False)
        except Exception as exc:
            logger.warning("retrieval feedback failed claim=%s: %s", claim.claim_id, exc)

    def _ensure_review(self, claim: Claim, reason: str, priority: str = "high") -> ReviewItem:
        try:
            rows = self.repository.list_reviews("pending", 1000) or []
        except Exception:
            rows = []
        if not isinstance(rows, (list, tuple, set)):
            rows = []
        existing = [
            item for item in rows
            if isinstance(item, ReviewItem)
            and item.claim_id == claim.claim_id
            and item.reason == reason
        ]
        if existing:
            return existing[0]
        item = ReviewItem(claim_id=claim.claim_id, reason=reason, priority=priority)
        self.repository.save_review(item)
        return item
