"""Perilla 唯一分层调度器：计划、预算、检索、评估、停止/继续。"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from .repository import ResearchRepository
from .contracts import _safe_bool, _safe_float, _safe_int, _safe_text, _text_list
from .domain_calibrators import normalize_retrieval_feedback

TIER_ORDER = ("high", "medium", "low")
logger = logging.getLogger(__name__)


def _get(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _mapping(item: Any, name: str) -> Mapping[str, Any]:
    value = _get(item, name, {})
    return value if isinstance(value, Mapping) else {}


@dataclass
class TierPlan:
    allocations: dict[str, int]
    policy_version: str
    utilities: dict[str, float]


@dataclass
class CoverageDecision:
    complete: bool
    independent_sources: int
    first_party_sources: int
    counter_search_completed: bool
    unresolved_conflict: bool
    coverage_ratio: float
    reason: str


@dataclass
class TierExecution:
    runs: list[Any] = field(default_factory=list)
    documents: list[Any] = field(default_factory=list)
    tiers_executed: list[str] = field(default_factory=list)
    tiers_skipped: list[str] = field(default_factory=list)
    decision: CoverageDecision | None = None


class AdaptiveSearchBudgetPolicy:
    """从 Retrieval Outcome 学习边际效用；无标签时使用保守先验。"""
    VERSION = "retrieval-budget-v2"

    def __init__(self, repository: ResearchRepository | None = None) -> None:
        self.repository = repository

    def plan(self, grouped: dict[str, list[Any]], total_budget: int) -> TierPlan:
        grouped = grouped if isinstance(grouped, Mapping) else {}
        total_budget = max(0, _safe_int(total_budget, 0))
        utilities = {"high": 1.0, "medium": 0.65, "low": 0.35}
        if self.repository is not None:
            try:
                active_policy = self.repository.get_active_policy("retrieval")
            except Exception as exc:
                logger.warning("retrieval policy lookup failed: %s", exc)
                active_policy = None
            if active_policy is not None:
                configured = _mapping(active_policy, "parameters").get("tier_utilities", {})
                configured = configured if isinstance(configured, Mapping) else {}
                for tier in TIER_ORDER:
                    value = _safe_float(configured.get(tier), float("nan"))
                    if value == value:
                        utilities[tier] = value
            try:
                feedback_events = self.repository.list_feedback("retrieval", limit=1000) or []
            except Exception as exc:
                logger.warning("retrieval feedback lookup failed: %s", exc)
                feedback_events = []
            observed_by_tier: dict[str, list[float]] = defaultdict(list)
            for event in feedback_events:
                sample = normalize_retrieval_feedback(event)
                if sample is None:
                    continue
                tier = str(sample["tier"])
                effective_rate = float(sample["effective_evidence_rate"])
                independent_rate = float(sample["independent_source_rate"])
                unit_cost = float(sample["unit_cost"])
                observed = (0.6 * effective_rate + 0.4 * independent_rate) / unit_cost
                observed_by_tier[tier].append(observed)
            for tier, observed in observed_by_tier.items():
                utilities[tier] = 0.7 * utilities[tier] + 0.3 * (
                    sum(observed) / len(observed)
                )
        active = [tier for tier in TIER_ORDER if grouped.get(tier)]
        if not active or total_budget <= 0:
            return TierPlan({tier: 0 for tier in TIER_ORDER}, self.VERSION, utilities)
        scores = {tier: math.exp(max(-5.0, min(5.0, utilities[tier]))) for tier in active}
        total_score = sum(scores.values())
        allocations = {tier: 0 for tier in TIER_ORDER}
        if total_budget < len(active):
            for tier in sorted(active, key=lambda key: utilities[key], reverse=True)[:total_budget]:
                allocations[tier] = 1
            return TierPlan(allocations, self.VERSION, {k: round(v, 4) for k, v in utilities.items()})
        remaining = int(total_budget)
        for tier in active:
            allocations[tier] = max(1, round(remaining * scores[tier] / total_score))
        while sum(allocations.values()) > remaining:
            tier = min(active, key=lambda key: utilities[key] if allocations[key] > 1 else float("inf"))
            if allocations[tier] <= 1:
                break
            allocations[tier] -= 1
        return TierPlan(allocations, self.VERSION, {k: round(v, 4) for k, v in utilities.items()})

    def recommended_total_budget(self, default: int, *, expected_information_gain: float = 0.5) -> int:
        observed: list[float] = []
        if self.repository is not None:
            try:
                active_policy = self.repository.get_active_policy("retrieval")
            except Exception as exc:
                logger.warning("retrieval policy lookup failed: %s", exc)
                active_policy = None
            if active_policy is not None:
                configured = _mapping(active_policy, "parameters").get("recommended_budget")
                value = _safe_float(configured)
                if value > 0:
                    observed.append(value)
            try:
                feedback_events = self.repository.list_feedback("retrieval", limit=500) or []
            except Exception as exc:
                logger.warning("retrieval feedback lookup failed: %s", exc)
                feedback_events = []
            for event in feedback_events:
                value = _safe_float(_mapping(event, "metrics").get("recommended_budget"))
                if value > 0:
                    observed.append(value)
        baseline = sum(observed) / len(observed) if observed else _safe_float(default, 2.0)
        multiplier = 0.6 + 0.8 * max(0.0, min(1.0, _safe_float(expected_information_gain, 0.5)))
        return max(2, min(100, round(baseline * multiplier)))


class EvidenceCoverageEvaluator:
    COUNTER = re.compile(r"否认|澄清|未供货|不属实|终止|取消|counter|disproven", re.I)
    FIRST_PARTY = {"official", "announcement", "financial_report", "company_website", "公告", "财报"}

    def __init__(self, min_independent_sources: int = 2, require_counter_search: bool = True) -> None:
        self.min_independent_sources = max(2, _safe_int(min_independent_sources, 2))
        self.require_counter_search = _safe_bool(require_counter_search, True)

    def evaluate(
        self, documents: list[Any], *, counter_search_completed: bool,
        required_targets: set[str] | None = None, require_first_party: bool = False,
    ) -> CoverageDecision:
        lineages: dict[str, Any] = {}
        support = 0
        counter = 0
        first_party = 0
        covered: set[str] = set()
        for doc in documents if isinstance(documents, (list, tuple)) else []:
            text = str(_get(doc, "content_text", "") or _get(doc, "content", "") or _get(doc, "summary", "") or "")
            title = str(_get(doc, "title", "") or "")
            if not (text or title):
                continue
            metadata = dict(_mapping(doc, "metadata"))
            normalized = re.sub(r"\s+", "", f"{title}\n{text}").lower()
            lineage = str(
                metadata.get("source_lineage_id")
                or metadata.get("lineage_id")
                or metadata.get("original_source_id")
                or hashlib.sha1(normalized.encode()).hexdigest()[:20]
            )
            lineages.setdefault(lineage, doc)
        for doc in lineages.values():
            metadata = dict(_mapping(doc, "metadata"))
            text = " ".join(str(value or "") for value in (_get(doc, "title", ""), _get(doc, "content_text", ""), _get(doc, "content", ""), _get(doc, "summary", "")))
            stance = str(metadata.get("stance") or metadata.get("evidence_strength_hint") or "")
            if self.COUNTER.search(f"{stance} {text}"):
                counter += 1
            else:
                support += 1
            source_type = _safe_text(
                _get(doc, "source_type", "unknown"), "unknown"
            ).lower()
            if source_type in self.FIRST_PARTY:
                first_party += 1
            covered.update(_text_list(metadata.get("related_themes") or _get(doc, "related_themes", [])))
        targets = (
            set(required_targets)
            if isinstance(required_targets, (set, frozenset, list, tuple))
            else set()
        )
        counter_search_completed = _safe_bool(counter_search_completed)
        require_first_party = _safe_bool(require_first_party)
        coverage_ratio = len(covered & targets) / len(targets) if targets else (1.0 if support else 0.0)
        conflict = support > 0 and counter > 0
        complete = (
            support >= self.min_independent_sources
            and (counter_search_completed or not self.require_counter_search)
            and not conflict
            and coverage_ratio >= 1.0
            and (not require_first_party or first_party >= 1)
        )
        reasons = []
        if support < self.min_independent_sources: reasons.append(f"独立支持来源不足{self.min_independent_sources}个")
        if self.require_counter_search and not counter_search_completed: reasons.append("反证搜索未完成")
        if conflict: reasons.append("存在未解决冲突")
        if coverage_ratio < 1.0: reasons.append("Claim槽位/目标覆盖不完整")
        if require_first_party and not first_party: reasons.append("高影响关系缺少第一方来源")
        return CoverageDecision(complete, len(lineages), first_party, counter_search_completed, conflict, round(coverage_ratio, 4), "；".join(reasons) or "证据充分")


class PerillaOrchestrator:
    """KnowledgeLoop、AutonomousPerilla 共用的唯一 tier 控制流。"""

    def __init__(self, budget_policy: AdaptiveSearchBudgetPolicy | None = None, evaluator: EvidenceCoverageEvaluator | None = None) -> None:
        self.budget_policy = (
            budget_policy
            if budget_policy is not None
            else AdaptiveSearchBudgetPolicy()
        )
        if evaluator is None:
            min_sources = 2
            require_counter = True
            repository = getattr(self.budget_policy, "repository", None)
            if repository is not None:
                try:
                    active = repository.get_active_policy("retrieval")
                except Exception as exc:
                    logger.warning("retrieval policy lookup failed: %s", exc)
                    active = None
                if active is not None:
                    parameters = _mapping(active, "parameters")
                    min_sources = _safe_int(parameters.get("stop_min_independent_sources"), 2)
                    require_counter = _safe_bool(
                        parameters.get("require_counter_search"), True
                    )
            evaluator = EvidenceCoverageEvaluator(min_sources, require_counter)
        self.evaluator = evaluator

    def execute(
        self, grouped: dict[str, list[Any]], run_tier: Callable[[str, list[Any]], tuple[Any, ...]],
        *, total_budget: int, required_targets: set[str] | None = None,
        require_first_party: bool = False,
    ) -> TierExecution:
        grouped = grouped if isinstance(grouped, Mapping) else {}
        plan = self.budget_policy.plan(grouped, total_budget)
        output = TierExecution()
        counter_done = False
        previous_independent = 0
        for tier in TIER_ORDER:
            tasks = sorted(
                grouped.get(tier, []) if isinstance(grouped.get(tier, []), list) else [],
                key=lambda item: _safe_float(_get(item, "priority_score", _get(item, "priority", 0.0))),
                reverse=True,
            )
            allocation = plan.allocations.get(tier, 0)
            if not tasks or allocation <= 0:
                continue
            batch = tasks[:allocation]
            try:
                tier_result = run_tier(tier, batch)
            except Exception as exc:
                logger.warning("tier execution failed(%s): %s", tier, exc)
                output.tiers_skipped.append(tier)
                continue
            if not isinstance(tier_result, (tuple, list)) or len(tier_result) not in {2, 3}:
                output.tiers_skipped.append(tier)
                continue
            if len(tier_result) == 3:
                runs_raw, docs_raw, counter_raw = tier_result
                counter_succeeded = _safe_bool(counter_raw)
            else:
                runs_raw, docs_raw = tier_result
                counter_succeeded = False
            runs = list(runs_raw) if isinstance(runs_raw, (list, tuple)) else []
            docs = list(docs_raw) if isinstance(docs_raw, (list, tuple)) else []
            if len(tier_result) == 2:
                counter_succeeded = any(
                    str(_get(run, "query_purpose", _get(run, "purpose", ""))).lower() == "counter"
                    and str(_get(run, "status", "completed")).lower() not in {"failed", "error", "timeout"}
                    for run in runs
                )
            output.runs.extend(runs); output.documents.extend(docs); output.tiers_executed.append(tier)
            counter_done = counter_done or bool(counter_succeeded)
            try:
                decision = self.evaluator.evaluate(
                    output.documents,
                    counter_search_completed=counter_done,
                    required_targets=required_targets,
                    require_first_party=require_first_party,
                )
            except Exception as exc:
                logger.warning("tier coverage evaluation failed(%s): %s", tier, exc)
                output.tiers_skipped.append(tier)
                continue
            output.decision = decision
            repository = getattr(self.budget_policy, "repository", None)
            if repository is not None:
                from .contracts import FeedbackEvent
                from .feedback import FeedbackController
                docs_count = max(1, len(docs))
                marginal = max(0, decision.independent_sources - previous_independent) / docs_count
                try:
                    FeedbackController(repository).record(
                        FeedbackEvent(
                            domain="retrieval",
                            event_type="tier_completed",
                            metrics={
                                "tier": tier,
                                "effective_evidence_rate": round(decision.independent_sources / max(1, len(output.documents)), 4),
                                "independent_source_rate": round(decision.independent_sources / max(1, len(output.documents)), 4),
                                "unit_cost": float(len(batch)),
                                "marginal_information_gain": round(marginal, 4),
                                "recommended_budget": max(2, len(batch)),
                            },
                            attribution={"tier": tier, "policy_version": plan.policy_version},
                        ),
                        generate_task=False,
                    )
                except Exception as exc:
                    logger.warning("tier feedback persistence failed(%s): %s", tier, exc)
                previous_independent = decision.independent_sources
            if decision.complete:
                lower = TIER_ORDER[TIER_ORDER.index(tier) + 1:]
                output.tiers_skipped.extend(item for item in lower if grouped.get(item))
                for lower_tier in lower:
                    for task in grouped.get(lower_tier, []):
                        if isinstance(task, dict) and task.get("status") == "pending": task["status"] = "skipped_sufficient"
                        elif getattr(task, "status", None) == "pending": task.status = "skipped_sufficient"
                break
        return output
