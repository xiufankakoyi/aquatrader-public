"""Alpha gap feedback emitter.

Converts evidence gaps produced by ``AlphaDiscoveryEngine`` into executable
``PerillaSearchTask`` objects, closing the loop from stable-path discovery back
to open discovery. 同时提供 ``emit_as_research_tasks`` 将 Perilla 任务转为
统一 ResearchTask，进入控制面任务队列。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.control_plane.contracts import ResearchTask, TaskBudget, TaskStatus
from core.research.perilla.alpha.graph_schema import SourceType
from core.research.perilla.bridge.models import (
    PerillaSearchTask,
    normalize_name,
    now_iso,
    stable_id,
)
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.shared.ontology.ontology_models import RelationType

logger = logging.getLogger(__name__)

# Alpha gap 反馈任务默认 TTL（小时）
ALPHA_GAP_TASK_TTL_HOURS = 48


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(0.0, min(1.0, number))


class AlphaGapFeedbackEmitter:
    """将 AlphaDiscoveryEngine 的 evidence gap 转成 PerillaSearchTask。"""

    def __init__(self, entity_resolver: EntityResolver | None = None) -> None:
        """初始化。"""
        self.entity_resolver = entity_resolver or EntityResolver()

    def emit_from_path_gap(
        self,
        path_data: dict[str, Any],
        priority_boost: float = 0.0,
    ) -> list[PerillaSearchTask]:
        """从单条路径的 evidence gap 生成搜索任务。

        Args:
            path_data: Alpha 路径数据，含 path / path_evidence_gap / gaps。
            priority_boost: 优先级加成。

        Returns:
            PerillaSearchTask 列表。
        """
        path_data = _mapping(path_data)
        path = [_text(node) for node in _items(path_data.get("path")) if _text(node)]
        gaps = path_data.get("path_evidence_gap", []) or path_data.get("gaps", [])
        gaps = _items(gaps)
        if not path or len(path) < 2:
            return []

        tasks: list[PerillaSearchTask] = []
        seen: set[str] = set()
        base_priority = _score(path_data.get("path_alpha_score", 0.5), 0.5) + _score(priority_boost)

        for gap in gaps:
            task = self._gap_to_task(gap, path, base_priority)
            if task and task.task_id not in seen:
                tasks.append(task)
                seen.add(task.task_id)

        # 对弱路径/缺实体路径也生成任务
        if not gaps and _score(path_data.get("path_confidence", 1.0), 1.0) < 0.5:
            task = self._weak_path_task(path, base_priority)
            if task and task.task_id not in seen:
                tasks.append(task)
                seen.add(task.task_id)

        return tasks

    def emit_from_evidence_gap(
        self,
        gap: dict[str, Any],
    ) -> PerillaSearchTask | None:
        """从结构化的 EvidenceGap dict 生成搜索任务。

        Args:
            gap: EvidenceGap.to_dict() 输出，含 gap_id / path / missing_relation /
                 missing_entity / missing_source_type / reason / priority。

        Returns:
            PerillaSearchTask 或 None。
        """
        gap = _mapping(gap)
        path = [_text(node) for node in _items(gap.get("path")) if _text(node)]
        if len(path) < 2:
            return None

        missing_relation = _text(gap.get("missing_relation"))
        missing_entity = normalize_name(_text(gap.get("missing_entity")))
        missing_source_type = _text(gap.get("missing_source_type"))
        reason = _text(gap.get("reason"))
        priority = _score(gap.get("priority", 0.5), 0.5)
        if not missing_entity:
            return None

        target = missing_entity
        query_keywords = path[:3] + [missing_relation] if missing_relation else path[:3]
        preferred_sources = self._preferred_sources_for_evidence_gap(
            missing_source_type, missing_relation
        )
        capability_hint = self._capability_hint_for_gap(missing_source_type, missing_relation)
        task_id = stable_id(f"evgap|{gap.get('gap_id', '')}")
        return PerillaSearchTask(
            task_id=task_id,
            target_entity=target,
            missing_relation=missing_relation or "evidence",
            query_keywords=query_keywords,
            preferred_sources=preferred_sources,
            priority=priority,
            reason=reason,
            origin_path=path,
            origin_gap=gap,
            capability_hint=capability_hint,
            created_at=now_iso(),
        )

    def _gap_to_task(
        self,
        gap: dict[str, Any],
        path: list[str],
        base_priority: float,
    ) -> PerillaSearchTask | None:
        """将单个 evidence gap 转为搜索任务。"""
        gap = _mapping(gap)
        head = normalize_name(_text(gap.get("head")))
        tail = normalize_name(_text(gap.get("tail")))
        relation = _text(gap.get("relation"))
        missing = _text(gap.get("missing"), "证据") or "证据"
        if not head or not tail:
            return None

        try:
            is_company = self.entity_resolver.is_company(tail)
        except (AttributeError, TypeError, ValueError):
            is_company = False
        target = tail if is_company else head
        query_keywords = [head, tail, relation] if relation else [head, tail]
        preferred_sources = self._preferred_sources_for_gap(missing, relation)
        capability_hint = self._capability_hint_for_gap(missing, relation)

        task_id = stable_id(f"gap|{head}|{relation}|{tail}")
        return PerillaSearchTask(
            task_id=task_id,
            target_entity=target,
            missing_relation=relation or "evidence",
            query_keywords=query_keywords,
            preferred_sources=preferred_sources,
            priority=_score(base_priority + 0.05),
            reason=f"路径证据缺口: {head} → {tail} 缺 {missing}",
            origin_path=path,
            origin_gap=gap,
            capability_hint=capability_hint,
            created_at=now_iso(),
        )

    def _weak_path_task(
        self,
        path: list[str],
        base_priority: float,
    ) -> PerillaSearchTask | None:
        """为整体弱路径生成搜索任务。"""
        path = [_text(node) for node in _items(path) if _text(node)]
        target = path[-1] if path else ""
        if not target:
            return None
        task_id = stable_id(f"weakpath|{'|'.join(path)}")
        return PerillaSearchTask(
            task_id=task_id,
            target_entity=target,
            missing_relation="path_evidence",
            query_keywords=path,
            preferred_sources=[
                SourceType.BROKER_REPORT.value,
                SourceType.ANNUAL_REPORT.value,
                SourceType.NEWS.value,
            ],
            priority=_score(base_priority),
            reason=f"弱路径置信度不足，需要多源证据: {' → '.join(path)}",
            origin_path=path,
            capability_hint="semantic_search",
            created_at=now_iso(),
        )

    @staticmethod
    def _preferred_sources_for_gap(missing: str, relation: str) -> list[str]:
        """根据缺口类型推荐数据源。"""
        if "供应商" in missing or relation in (RelationType.SUPPLIED_BY.value,):
            return [
                SourceType.ANNOUNCEMENT.value,
                SourceType.ANNUAL_REPORT.value,
                SourceType.WENCAI.value,
                SourceType.OFFICIAL_WEBSITE.value,
            ]
        if relation in (
            RelationType.BOTTLENECK_OF.value,
            RelationType.CONSTRAINED_BY.value,
            RelationType.IMPORT_DEPENDENT_ON.value,
        ):
            return [
                SourceType.BROKER_REPORT.value,
                SourceType.INDUSTRY_WHITEPAPER.value,
                SourceType.PATENT.value,
                SourceType.STANDARD.value,
            ]
        if relation in (RelationType.CERTIFIED_BY.value,):
            return [
                SourceType.ANNOUNCEMENT.value,
                SourceType.OFFICIAL_WEBSITE.value,
                SourceType.STANDARD.value,
            ]
        return [
            SourceType.BROKER_REPORT.value,
            SourceType.NEWS.value,
            SourceType.WEB_SEARCH_SNIPPET.value,
        ]

    @staticmethod
    def _preferred_sources_for_evidence_gap(
        missing_source_type: str,
        relation: str,
    ) -> list[str]:
        """根据 EvidenceGap 推荐数据源。

        Args:
            missing_source_type: EvidenceGap.missing_source_type 中的简写标签。
            relation: 缺失关系。

        Returns:
            SourceType 值列表。
        """
        label = _text(missing_source_type).lower()
        if label == "公告":
            return [
                SourceType.ANNOUNCEMENT.value,
                SourceType.ANNUAL_REPORT.value,
                SourceType.OFFICIAL_WEBSITE.value,
            ]
        if label == "研报":
            return [
                SourceType.BROKER_REPORT.value,
                SourceType.INDUSTRY_WHITEPAPER.value,
                SourceType.PATENT.value,
            ]
        if label == "网页搜索":
            return [
                SourceType.WEB_SEARCH_SNIPPET.value,
                SourceType.NEWS.value,
                SourceType.BROKER_REPORT.value,
            ]
        # 回退到按关系判断
        return AlphaGapFeedbackEmitter._preferred_sources_for_gap("", relation)

    @staticmethod
    def _capability_hint_for_gap(missing: str, relation: str) -> str:
        """根据缺口类型推荐搜索能力。"""
        if relation in (RelationType.SUPPLIED_BY.value, RelationType.CERTIFIED_BY.value):
            return "announcements"
        if relation in (RelationType.BOTTLENECK_OF.value, RelationType.CONSTRAINED_BY.value):
            return "reports"
        if "估值" in missing or "市场" in missing:
            return "market_data"
        return "semantic_search"

    def emit_as_research_tasks(
        self,
        path_data: dict[str, Any],
        *,
        parent_task_id: str = "",
        trace_id: str = "",
        feedback_depth: int = 0,
        budget: TaskBudget | None = None,
    ) -> list[ResearchTask]:
        """将 Alpha 路径缺口转为统一 ResearchTask，进入控制面任务队列。

        每条 ResearchTask 保存来源、原因、优先级、预算和过期时间，
        由 FeedbackController 进行去重和限流后入队。

        Args:
            path_data: Alpha 路径数据，含 path / path_evidence_gap / gaps。
            parent_task_id: 父任务 ID，用于防循环追踪。
            trace_id: 追踪链 ID。
            feedback_depth: 反馈深度，超过 MAX_FEEDBACK_DEPTH 时不再生成。
            budget: 任务预算，为 None 时使用默认值。

        Returns:
            ResearchTask 列表，task_type="alpha_gap_search"。
        """
        path_data = _mapping(path_data)
        perilla_tasks = self.emit_from_path_gap(path_data)
        if not perilla_tasks:
            return []
        research_tasks: list[ResearchTask] = []
        task_budget = budget if isinstance(budget, TaskBudget) else TaskBudget()
        expires_at = self._compute_expiry()
        path = [_text(node) for node in _items(path_data.get("path")) if _text(node)]
        for pt in perilla_tasks:
            rt = ResearchTask(
                task_id=pt.task_id,
                task_type="alpha_gap_search",
                trigger_source="alpha_gap_feedback",
                target_claim_id="",
                priority=min(1.0, pt.priority),
                budget=task_budget.to_dict(),
                parent_task_id=parent_task_id,
                trace_id=trace_id,
                expires_at=expires_at,
                status=TaskStatus.QUEUED.value,
                params={
                    "target_entity": pt.target_entity,
                    "missing_relation": pt.missing_relation,
                    "query_keywords": pt.query_keywords,
                    "preferred_sources": pt.preferred_sources,
                    "reason": pt.reason,
                    "origin_path": path,
                    "capability_hint": pt.capability_hint,
                    "feedback_depth": feedback_depth,
                },
            )
            research_tasks.append(rt)
        return research_tasks

    @staticmethod
    def _compute_expiry() -> str:
        """计算任务过期时间（当前时间 + ALPHA_GAP_TASK_TTL_HOURS）。"""
        from datetime import datetime, timedelta, timezone
        expiry = datetime.now(timezone.utc) + timedelta(hours=ALPHA_GAP_TASK_TTL_HOURS)
        return expiry.isoformat()
