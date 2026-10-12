"""Perilla Discovery 与 SAG KnowledgeBase 之间的桥接层。

桥接层是状态转换层，不是报告层或检索层：
- 将 Perilla 发现的 ConceptSignal / BottleneckHypothesis / EarlySignal
  转成标准 EventClaim，写入 SAG event graph。
- 将 AlphaDiscoveryEngine 输出的 evidence gap / path gap 转成
  PerillaSearchTask，驱动下一轮外部搜索。
- 将高置信 EventClaim / EventEdge 转成 PromotionCandidate，
  交给 ClaimPromotionEngine 做显式晋升判断。

禁止在桥接层生成 mock 数据、固定示例数据，或直接将事件写入稳定图。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    NodeType,
    SourceType,
)
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.bridge.alpha_gap_feedback_emitter import (
    AlphaGapFeedbackEmitter,
)
from core.research.perilla.bridge.models import (
    EventClaimDraft,
    PerillaSearchTask,
    normalize_name,
    now_iso,
    stable_id,
)
from core.research.perilla.bridge.promotion_candidate_builder import (
    PromotionCandidateBuilder,
)
from core.research.perilla.discovery.models import (
    BottleneckHypothesis,
    ConceptSignal,
    EarlySignal,
    PerillaDiscoveryReport,
)
from core.research.perilla.event.event_graph_schema import (
    EventClaim,
    EventClaimStatus,
    EventType,
    PromotionCandidate,
)
from core.research.perilla.shared.identity.entity_resolver import EntityResolver, ResolvedEntity
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

logger = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(0.0, min(1.0, number))


def _confidence_label_to_float(label: str) -> float:
    """将 high/medium/low 标签转成置信度数值。"""
    return {"high": 0.8, "medium": 0.55, "low": 0.3}.get(_text(label).lower(), 0.3)


class PerillaSignalNormalizer:
    """将 Perilla 信号统一标准化为 EventClaimDraft。"""

    def __init__(
        self,
        entity_resolver: EntityResolver | None = None,
        ontology_registry: OntologyRegistry | None = None,
    ) -> None:
        """初始化。

        Args:
            entity_resolver: 实体解析器，None 则自动新建。
            ontology_registry: Ontology 注册表，None 则使用默认值。
        """
        self.entity_resolver = entity_resolver or EntityResolver()
        self.ontology_registry = ontology_registry or OntologyRegistry.default()

    def normalize_concept_signal(
        self,
        signal: ConceptSignal,
        source_document: dict[str, Any] | None = None,
    ) -> EventClaimDraft:
        """将 ConceptSignal 转为 EventClaimDraft。

        Args:
            signal: 概念信号。
            source_document: 可选来源文档负载。

        Returns:
            标准化后的事件草案。
        """
        signal_name = _text(_field(signal, "name"))
        concept_id = _text(_field(signal, "concept_id"))
        subject = normalize_name(signal_name)
        evidence = _text(_field(signal, "notes")) or f"概念信号: {signal_name}"
        source_document = _mapping(source_document)
        if source_document:
            evidence = _text(source_document.get("content_text"), evidence) or evidence

        source_types = _items(_field(signal, "source_types"))
        try:
            source_type = self.ontology_registry.perilla_source_to_sag_source(
                _text(source_types[0]) if source_types else "local_evidence"
            )
            event_type = self.ontology_registry.signal_type_to_event_type("concept_signal")
        except (AttributeError, KeyError, TypeError, ValueError):
            source_type = SourceType.WEB_SEARCH.value
            event_type = EventType.CONCEPT_SIGNAL.value
        return EventClaimDraft(
            draft_id=stable_id(f"concept|{concept_id}|{signal_name}"),
            event_type=event_type,
            raw_event_type="concept_signal",
            subject=subject,
            object="",
            event_time=_text(_field(signal, "last_seen_at")) or _text(_field(signal, "first_seen_at")) or now_iso(),
            source_time=_text(_field(signal, "last_seen_at")) or _text(_field(signal, "first_seen_at")) or now_iso(),
            evidence=evidence[:1000],
            source_type=source_type,
            source_title=f"ConceptSignal: {signal_name}",
            source_url="",
            confidence=_confidence_label_to_float(_field(signal, "confidence")),
            source_id=concept_id,
            linked_node_ids=[],
            raw_payload={"concept_signal": self._dataclass_to_dict(signal)},
        )

    def normalize_bottleneck_hypothesis(
        self,
        hypothesis: BottleneckHypothesis,
        source_document: dict[str, Any] | None = None,
    ) -> EventClaimDraft:
        """将 BottleneckHypothesis 转为 EventClaimDraft。"""
        node_name = _text(_field(hypothesis, "node_name"))
        hypothesis_id = _text(_field(hypothesis, "hypothesis_id"))
        concept_id = _text(_field(hypothesis, "concept_id"))
        probability = _score(_field(hypothesis, "perilla_probability"))
        subject = normalize_name(node_name)
        evidence = (
            f"瓶颈假设: {node_name}, "
            f"perilla_probability={probability:.2f}, "
            f"six_question_scores={_field(hypothesis, 'six_question_scores', {})}"
        )
        source_document = _mapping(source_document)
        if source_document:
            evidence = _text(source_document.get("content_text"), evidence) or evidence

        try:
            source_type = self.ontology_registry.perilla_source_to_sag_source("bottleneck_hypothesis")
            event_type = self.ontology_registry.signal_type_to_event_type("bottleneck_hypothesis")
        except (AttributeError, KeyError, TypeError, ValueError):
            source_type = SourceType.WEB_SEARCH.value
            event_type = EventType.BOTTLENECK_HYPOTHESIS.value
        return EventClaimDraft(
            draft_id=stable_id(f"bottleneck|{hypothesis_id}|{node_name}"),
            event_type=event_type,
            raw_event_type="bottleneck_hypothesis",
            subject=subject,
            object=normalize_name(concept_id),
            event_time=now_iso(),
            source_time=now_iso(),
            evidence=evidence[:1000],
            source_type=source_type,
            source_title=f"BottleneckHypothesis: {hypothesis.node_name}",
            source_url="",
            confidence=probability,
            source_id=hypothesis_id,
            linked_node_ids=[],
            raw_payload={"bottleneck_hypothesis": self._dataclass_to_dict(hypothesis)},
        )

    def normalize_early_signal(
        self,
        signal: EarlySignal,
        concept_name: str = "",
    ) -> EventClaimDraft:
        """将 EarlySignal 转为 EventClaimDraft。"""
        signal_type = _text(_field(signal, "signal_type"))
        signal_id = _text(_field(signal, "signal_id"))
        anchor_companies = [_text(item) for item in _items(_field(signal, "anchor_companies")) if _text(item)]
        mentioned_companies = [_text(item) for item in _items(_field(signal, "mentioned_companies")) if _text(item)]
        try:
            event_type = self.ontology_registry.signal_type_to_event_type(signal_type)
        except (AttributeError, KeyError, TypeError, ValueError):
            event_type = EventType.OTHER.value
        subject = normalize_name(
            anchor_companies[0]
            if anchor_companies
            else mentioned_companies[0]
            if mentioned_companies
            else concept_name
        )
        object_entity = normalize_name(_text(concept_name)) if concept_name else ""
        if event_type == EventType.ORDER_DISCLOSED.value and mentioned_companies:
            # subject=公司, object=概念/产品
            subject = normalize_name(mentioned_companies[0])
            object_entity = normalize_name(_text(concept_name)) if concept_name else ""

        try:
            source_type = self.ontology_registry.perilla_source_to_sag_source(signal_type)
        except (AttributeError, KeyError, TypeError, ValueError):
            source_type = SourceType.WEB_SEARCH.value
        return EventClaimDraft(
            draft_id=stable_id(f"early|{signal_id}|{subject}|{event_type}"),
            event_type=event_type,
            raw_event_type=signal_type,
            subject=subject,
            object=object_entity,
            event_time=now_iso(),
            source_time=now_iso(),
            evidence=(_text(_field(signal, "evidence_text")) or _text(_field(signal, "claim")))[:1000],
            source_type=source_type,
            source_title=_text(_field(signal, "claim")) or f"EarlySignal: {signal_type}",
            source_url="",
            confidence=_confidence_label_to_float(_field(signal, "confidence")),
            source_id=_text(_field(signal, "source_document_id")) or signal_id,
            linked_node_ids=[],
            raw_payload={"early_signal": self._dataclass_to_dict(signal)},
        )

    @staticmethod
    def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
        """安全地将 dataclass 实例转为 dict。"""
        if hasattr(obj, "__dataclass_fields__"):
            return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
        return {}


class EventClaimIngestor:
    """将 EventClaimDraft 写入 SAG event graph，并维护 EventEdge。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        ontology_registry: OntologyRegistry | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            entity_resolver: 实体解析器。
            ontology_registry: Ontology 注册表，None 则使用默认值。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.ontology_registry = ontology_registry or OntologyRegistry.default()

    def ingest(self, draft: EventClaimDraft) -> EventClaim | None:
        """将单个 EventClaimDraft 写入事件图。

        Args:
            draft: 标准化后的事件草案。

        Returns:
            写入后的 EventClaim，参数无效则返回 None。
        """
        if not isinstance(draft, EventClaimDraft):
            return None
        subject = normalize_name(_text(draft.subject))
        object_entity = normalize_name(_text(draft.object))
        event_type = _text(draft.event_type)
        evidence = _text(draft.evidence)

        if not subject or not event_type or not evidence:
            return None
        try:
            event_type_valid = self.ontology_registry.is_valid_event_type(event_type)
        except (AttributeError, TypeError, ValueError):
            event_type_valid = False
        if not event_type_valid:
            logger.warning("未知事件类型: %s", event_type)
            return None

        # 实体解析并绑定稳定图节点
        try:
            subject_resolved = self.entity_resolver.resolve(subject)
        except (AttributeError, KeyError, TypeError, ValueError):
            subject_resolved = ResolvedEntity(canonical_name=subject, node_type=NodeType.GENERIC.value)
        subject = _text(getattr(subject_resolved, "canonical_name", "")) or subject
        linked_node_ids: list[str] = []
        self._ensure_stable_node(subject, subject_resolved)
        linked_node_ids.append(subject)

        if object_entity:
            try:
                object_resolved = self.entity_resolver.resolve(object_entity)
            except (AttributeError, KeyError, TypeError, ValueError):
                object_resolved = ResolvedEntity(canonical_name=object_entity, node_type=NodeType.GENERIC.value)
            object_entity = _text(getattr(object_resolved, "canonical_name", "")) or object_entity
            self._ensure_stable_node(object_entity, object_resolved)
            linked_node_ids.append(object_entity)

        # 生成稳定 event_id
        event_time = _text(draft.event_time)
        source_time = _text(draft.source_time)
        source_title = _text(draft.source_title)
        confidence = _score(draft.confidence)
        event_id = stable_id(f"{subject}|{event_type}|{object_entity}|{event_time}|{evidence}|{source_title}")

        # 相同事件 claim 已存在时更新置信度（取高者）
        event_claims = getattr(self.kb, "event_claims", {})
        event_claims = event_claims if isinstance(event_claims, Mapping) else {}
        existing = event_claims.get(event_id)
        if existing:
            existing.confidence = max(_score(existing.confidence), confidence)
            existing.score = max(_score(existing.score), confidence)
            existing.source_time = source_time or existing.source_time
            existing.evidence = evidence or existing.evidence
            claim = existing
        else:
            claim = EventClaim(
                event_id=event_id,
                event_type=event_type,
                raw_event_type=draft.raw_event_type,
                subject=subject,
                object=object_entity,
                time=event_time,
                source_time=source_time or event_time,
                evidence=evidence,
                source_type=draft.source_type,
                source_title=source_title,
                source_url=_text(draft.source_url),
                confidence=confidence,
                score=confidence,
                status=EventClaimStatus.ACTIVE.value,
                source_id=_text(draft.source_id),
                linked_node_ids=linked_node_ids,
                raw_payload=dict(_mapping(draft.raw_payload)),
            )
            self.kb.event_claims[event_id] = claim

        self.kb._unindex_event_claim(claim.event_id)
        self.kb._index_event_claim(claim)
        # 更新事件边
        self.kb._update_event_edge(claim)
        self.kb._recompute_event_edge_links((claim.subject, claim.event_type, claim.object))
        self.kb.update_event_edge_confidence((claim.subject, claim.event_type, claim.object))
        return claim

    def _ensure_stable_node(
        self,
        name: str,
        resolved: ResolvedEntity,
    ) -> None:
        """确保被事件引用的实体在稳定图中存在节点。"""
        node_type = _text(getattr(resolved, "node_type", ""), NodeType.GENERIC.value) or NodeType.GENERIC.value
        self.kb._ensure_node(name, node_type=node_type, source_type="event_graph")


class PerillaSAGBridge:
    """Perilla Discovery 与 SAG KnowledgeBase 之间的状态转换协调器。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        ontology_registry: OntologyRegistry | None = None,
        normalizer: PerillaSignalNormalizer | None = None,
        ingestor: EventClaimIngestor | None = None,
        promotion_builder: PromotionCandidateBuilder | None = None,
        gap_emitter: AlphaGapFeedbackEmitter | None = None,
    ) -> None:
        """初始化桥接层。

        Args:
            kb: SAG 知识库。
            entity_resolver: 实体解析器。
            ontology_registry: Ontology 注册表，None 则使用默认值。
            normalizer: 信号标准化器。
            ingestor: 事件摄入器。
            promotion_builder: 晋升候选构造器。
            gap_emitter: 缺口反馈发射器。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.ontology_registry = ontology_registry or OntologyRegistry.default()
        self.normalizer = normalizer or PerillaSignalNormalizer(
            self.entity_resolver, self.ontology_registry
        )
        self.ingestor = ingestor or EventClaimIngestor(
            kb, self.entity_resolver, self.ontology_registry
        )
        self.promotion_builder = promotion_builder or PromotionCandidateBuilder(
            kb, self.entity_resolver, self.ontology_registry,
            min_source_count=1,
        )
        self.gap_emitter = gap_emitter or AlphaGapFeedbackEmitter(self.entity_resolver)
        self.pending_search_tasks: list[PerillaSearchTask] = []

    def ingest_concept_signal(
        self,
        signal: ConceptSignal,
        source_document: dict[str, Any] | None = None,
    ) -> EventClaim | None:
        """摄入单个 ConceptSignal 到事件图。"""
        draft = self.normalizer.normalize_concept_signal(signal, source_document)
        return self.ingestor.ingest(draft)

    def ingest_bottleneck_hypothesis(
        self,
        hypothesis: BottleneckHypothesis,
        source_document: dict[str, Any] | None = None,
    ) -> EventClaim | None:
        """摄入单个 BottleneckHypothesis 到事件图。"""
        draft = self.normalizer.normalize_bottleneck_hypothesis(hypothesis, source_document)
        return self.ingestor.ingest(draft)

    def ingest_early_signal(
        self,
        signal: EarlySignal,
        concept_name: str = "",
    ) -> EventClaim | None:
        """摄入单个 EarlySignal 到事件图。"""
        draft = self.normalizer.normalize_early_signal(signal, concept_name)
        return self.ingestor.ingest(draft)

    def ingest_perilla_report(
        self,
        report: PerillaDiscoveryReport,
    ) -> dict[str, list[EventClaim | None]]:
        """将 PerillaDiscoveryReport 中的信号批量摄入事件图。

        Args:
            report: 紫苏发现报告。

        Returns:
            按类型分组的事件 claim 结果。
        """
        results: dict[str, list[EventClaim | None]] = {
            "concept_signals": [],
            "bottlenecks": [],
            "early_signals": [],
        }
        if not isinstance(report, PerillaDiscoveryReport):
            return results
        for signal in _items(_field(report, "concept_signals")):
            try:
                results["concept_signals"].append(self.ingest_concept_signal(signal))
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning("概念信号摄入失败", exc_info=True)

        for hypothesis in _items(_field(report, "bottleneck_hypotheses")):
            try:
                results["bottlenecks"].append(self.ingest_bottleneck_hypothesis(hypothesis))
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning("瓶颈假设摄入失败", exc_info=True)

        return results

    def emit_search_tasks_from_alpha_result(
        self,
        alpha_result: dict[str, Any],
    ) -> list[PerillaSearchTask]:
        """从 AlphaDiscoveryEngine 结果中提取证据缺口任务。

        同时兼容旧的 path_data 缺口格式与新的 EvidenceGap 结构化输出。

        Args:
            alpha_result: discover() 返回的结果字典。

        Returns:
            新增的 PerillaSearchTask 列表。
        """
        alpha_result = _mapping(alpha_result)
        tasks: list[PerillaSearchTask] = []
        seen: set[str] = set()

        # 1. 从 top_paths 的 path_evidence_gap 生成任务（旧格式兼容）
        for path_data in _items(alpha_result.get("top_paths")):
            for task in self.gap_emitter.emit_from_path_gap(path_data):
                if task.task_id in seen:
                    continue
                tasks.append(task)
                seen.add(task.task_id)

        # 2. 从结构化的 evidence_gaps 生成任务（新格式）
        for gap in _items(alpha_result.get("evidence_gaps")):
            task = self.gap_emitter.emit_from_evidence_gap(gap)
            if task and task.task_id not in seen:
                tasks.append(task)
                seen.add(task.task_id)

        existing_ids = {task.task_id for task in self.pending_search_tasks if isinstance(task, PerillaSearchTask)}
        self.pending_search_tasks.extend(task for task in tasks if task.task_id not in existing_ids)
        return tasks

    def get_pending_search_tasks(self) -> list[PerillaSearchTask]:
        """获取待执行的搜索任务。"""
        return [t for t in self.pending_search_tasks if isinstance(t, PerillaSearchTask) and t.status == "pending"]

    def mark_search_task_done(self, task_id: str) -> None:
        """标记搜索任务已完成。"""
        task_id = _text(task_id)
        for task in self.pending_search_tasks:
            if isinstance(task, PerillaSearchTask) and task.task_id == task_id:
                task.status = "completed"

    def build_promotion_candidates(
        self,
        max_candidates: int = 50,
    ) -> list[PromotionCandidate]:
        """构建稳定图晋升候选。"""
        return self.promotion_builder.build_candidates(max_candidates)

    def apply_promotion_candidates(
        self,
        candidates: list[PromotionCandidate],
        promotion_engine: Any | None = None,
    ) -> list[dict[str, Any]]:
        """将 approved 的 PromotionCandidate 写入稳定图并触发晋升。

        Args:
            candidates: 晋升候选列表。
            promotion_engine: ClaimPromotionEngine 实例，可选。

        Returns:
            晋升结果列表。
        """
        results: list[dict[str, Any]] = []
        for candidate in _items(candidates):
            if not isinstance(candidate, PromotionCandidate) or _text(candidate.status) != "approved":
                continue
            try:
                claim = self.kb.add_claim(
                    head=normalize_name(_text(candidate.proposed_head)),
                    relation=_text(candidate.proposed_relation),
                    tail=normalize_name(_text(candidate.proposed_tail)),
                    evidence=_text(candidate.evidence_summary),
                    source_type=_text(candidate.source_types[0]) if _items(candidate.source_types) else SourceType.LLM_INFERENCE.value,
                    source_title=f"promoted_from_event:{_text(candidate.candidate_id)}",
                    confidence=_score(candidate.promotion_score),
                    metadata={"promotion_candidate_id": _text(candidate.candidate_id)},
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning("晋升候选写入稳定图失败", exc_info=True)
                continue
            if claim and promotion_engine:
                try:
                    promotion_engine.process_new_claim(claim)
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("晋升候选后处理失败: %s", claim.claim_id, exc_info=True)
            if claim:
                # 将相关 event claim 标记为 promoted
                for eid in _items(candidate.event_claim_ids):
                    try:
                        self.kb.update_event_claim_status(
                            _text(eid),
                            EventClaimStatus.PROMOTED.value,
                            f"promoted to stable claim {claim.claim_id}",
                            changed_by="PerillaSAGBridge",
                        )
                    except (AttributeError, KeyError, TypeError, ValueError):
                        logger.warning("事件 claim 状态更新失败: %s", eid, exc_info=True)
                results.append({
                    "candidate_id": candidate.candidate_id,
                    "claim_id": claim.claim_id,
                    "edge_key": candidate.event_edge_key,
                })
        return results

    def get_event_subgraph_for_concept(
        self,
        concept_name: str,
        max_depth: int = 3,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """获取某概念在事件图中的子图。"""
        return self.kb.get_event_subgraph(
            concept_name,
            max_depth=max_depth,
            min_confidence=min_confidence,
        )

    def export_event_features_by_company(self) -> dict[str, dict[str, float]]:
        """导出按公司聚合的 SAG 事件特征。

        遍历 SAG 事件图中所有事件节点，按公司(事件 subject)分组聚合事件特征，
        输出格式与 selector._event_score 消费格式对齐。

        Returns:
            {"by_company": {code_or_name: {"event_intensity": float, "event_freshness": float, ...}}}
        """
        grouped: dict[str, list[EventClaim]] = {}
        event_claims = getattr(self.kb, "event_claims", {})
        event_claims = event_claims.values() if isinstance(event_claims, Mapping) else _items(event_claims)
        for claim in event_claims:
            if not isinstance(claim, EventClaim):
                continue
            subject = _text(claim.subject)
            if not subject:
                continue
            grouped.setdefault(subject, []).append(claim)

        by_company: dict[str, dict[str, float]] = {}
        for company, claims in grouped.items():
            if not claims:
                continue
            # event_intensity: 置信度均值，confidence 已归一化到 [0,1]
            confidences = [_score(c.confidence) for c in claims]
            event_intensity = sum(confidences) / len(confidences)

            # event_freshness: 优先取预计算 freshness_score 的最大值；
            # 若均为 0 则按 source_time/time 计算 exponential decay。
            event_freshness = self._compute_event_freshness(claims)

            event_count = float(len(claims))

            by_company[company] = {
                "event_intensity": round(event_intensity, 4),
                "event_freshness": round(event_freshness, 4),
                "event_count": event_count,
            }

        return {"by_company": by_company}

    @staticmethod
    def _compute_event_freshness(claims: list[EventClaim]) -> float:
        """从事件列表中计算最新事件的时间新鲜度。

        优先使用事件的 freshness_score；若均为 0，则按 source_time/time
        计算 exponential decay（半衰期 30 天），取最新值。

        Args:
            claims: 同一公司的事件列表。

        Returns:
            新鲜度得分 [0, 1]。
        """
        from datetime import datetime, timezone

        freshness_scores = [_score(_field(c, "freshness_score")) for c in claims if _score(_field(c, "freshness_score")) > 0]
        if freshness_scores:
            return max(freshness_scores)

        now = datetime.now(timezone.utc)
        half_life_days = 30.0
        best = 0.0
        for claim in claims:
            time_str = _text(_field(claim, "source_time")) or _text(_field(claim, "time"))
            if not time_str:
                continue
            try:
                # 兼容 ISO 8601（含 Z 后缀）与无时区时间
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                elapsed_days = max(0.0, (now - dt).total_seconds() / 86400.0)
                freshness = 0.5 ** (elapsed_days / half_life_days)
                best = max(best, freshness)
            except (ValueError, TypeError):
                continue
        return best

    def save(self) -> bool:
        """持久化 SAG，返回是否成功。"""
        return self.kb.save()
