"""AlphaDiscoveryEngine：Graph-first 产业链 Alpha 发现引擎。

流程：
1. 从 SAG 查询 root 已有 subgraph。
2. 若稀疏，用 StructureNavigator 轻量 BFS 补骨架。
3. 用 FrontierScorer 枚举路径并评分。
4. 用 priority queue 选择 path_alpha_score 高但证据缺口大的路径。
5. 用 EvidenceProbe 补证据。
6. 迭代直到满足停止条件。
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    GENERIC_BLACKLIST,
    NodeType,
    RelationType,
    StructuralAxis,
)
from core.research.perilla.alpha.claim_promotion_engine import ClaimPromotionEngine
from core.research.perilla.alpha.contradiction_detector import ContradictionDetector
from core.research.perilla.event.dynamic_event_propagation import DynamicEventPropagationEngine
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.evidence.evidence_probe import EvidenceProbe
from core.research.perilla.alpha.flow_path_pruner import FlowPathPruner
from core.research.perilla.alpha.frontier_scorer import FrontierScorer
from core.research.perilla.discovery.julang_report_provider import JulangReportProvider
from core.research.perilla.alpha.llm_path_verifier import LLMPathVerifier
from core.research.perilla.alpha.path_confidence_propagator import PathConfidencePropagator
from core.research.perilla.alpha.path_discovery_schema import (
    AlphaPathResult,
    PathCandidate,
    PathEvidenceBundle,
    PathQualityReport,
    PathVerificationResult,
)
from core.research.perilla.alpha.path_quality_evaluator import PathQualityEvaluator
from core.research.perilla.bridge.perilla_sag_bridge import PerillaSAGBridge, PerillaSearchTask
from core.research.perilla.alpha.report_graph_builder import ReportGraphBuilder
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.alpha.structure_navigator import StructureNavigator
from core.research.alpha.active_learning_planner import ActiveLearningPlanner
from core.research_graph.scenario_simulator import ScenarioSimulator

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_get(value: Any, key: Any, default: Any = None) -> Any:
    if not isinstance(value, Mapping):
        return default
    try:
        return value.get(key, default)
    except (TypeError, ValueError):
        return default


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    number = _finite(value, default)
    return max(0, min(maximum, int(number)))


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


@dataclass(order=True)
class PrioritizedPath:
    """优先级队列项。"""

    priority: float
    iteration: int
    path_data: dict[str, Any] = field(compare=False)


@dataclass
class EvidenceGap:
    """Alpha 路径上的证据缺口，用于驱动 Perilla 下一轮搜索。

    Attributes:
        gap_id: 缺口唯一标识。
        path: 所属路径节点列表。
        missing_relation: 缺失的关系类型。
        missing_entity: 缺失的实体（head / tail）。
        missing_source_type: 推荐补充的数据源类型。
        reason: 缺口原因。
        priority: 优先级 [0, 1]。
    """

    gap_id: str
    path: list[str]
    missing_relation: str
    missing_entity: str
    missing_source_type: str
    reason: str
    priority: float

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "gap_id": _text(self.gap_id),
            "path": [_text(node) for node in _items(self.path) if _text(node)],
            "missing_relation": _text(self.missing_relation),
            "missing_entity": _text(self.missing_entity),
            "missing_source_type": _text(self.missing_source_type),
            "reason": _text(self.reason),
            "priority": _score(self.priority),
        }


class AlphaDiscoveryEngine:
    """产业链 Alpha 发现引擎。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase | None = None,
        storage_path: str | Path | None = None,
        max_iterations: int = 10,
        top_k: int = 10,
        min_path_alpha_score: float = 0.2,
        bridge: PerillaSAGBridge | None = None,
        event_engine: DynamicEventPropagationEngine | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库，None 则按 storage_path 新建
            storage_path: 知识库持久化路径
            max_iterations: 最大补证据迭代次数
            top_k: 输出 Top K 路径
            min_path_alpha_score: 路径入选最低分
            bridge: Perilla-SAG 桥接层，用于输出 evidence gap 任务
            event_engine: 动态事件传播引擎，None 则自动创建
        """
        if kb is None:
            kb = SAGKnowledgeBase(storage_path=storage_path)
        self.kb = kb
        self.entity_resolver = EntityResolver()
        self.event_engine = event_engine or DynamicEventPropagationEngine(
            kb=kb, entity_resolver=self.entity_resolver
        )
        self.navigator = StructureNavigator(kb=kb)
        self.scorer = FrontierScorer(kb=kb, entity_resolver=self.entity_resolver, event_engine=self.event_engine)
        self.graph_builder = ReportGraphBuilder(kb=kb)
        self.probe = EvidenceProbe(kb=kb, graph_builder=self.graph_builder, entity_resolver=self.entity_resolver)
        self.promotion_engine = ClaimPromotionEngine(kb=kb)
        self.contradiction_detector = ContradictionDetector(kb=kb, entity_resolver=self.entity_resolver)
        self.path_quality_evaluator = PathQualityEvaluator(entity_resolver=self.entity_resolver)
        self.path_propagator = PathConfidencePropagator(kb=kb, entity_resolver=self.entity_resolver, event_engine=self.event_engine)
        self.flow_pruner = FlowPathPruner(kb=kb)
        self.llm_path_verifier = LLMPathVerifier()
        self.julang_provider = JulangReportProvider()
        self.bridge = bridge or PerillaSAGBridge(kb=self.kb)
        self.max_iterations = _bounded_int(max_iterations, 10, 1000)
        self.top_k = _bounded_int(top_k, 10, 1000)
        self.min_path_alpha_score = _score(min_path_alpha_score, 0.2)
        self.iteration_logs: list[dict[str, Any]] = []
        self._processed_claim_ids: set[str] = set()
        self._learning_planner = ActiveLearningPlanner()

    def _process_event_driven_paths(
        self,
        paths: list[dict[str, Any]],
        root: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """处理事件驱动路径：晋升候选、冲突检测、证据探测。

        Args:
            paths: FrontierScorer 输出的路径列表（含 path_category）。
            root: 当前根节点。

        Returns:
            (promotion_results, contradiction_records, event_gaps)
        """
        promotion_results: list[dict[str, Any]] = []
        contradiction_records: list[dict[str, Any]] = []
        event_gaps: list[dict[str, Any]] = []

        promotion_candidates: list[Any] = []
        contradiction_candidates: list[Any] = []
        high_priority_probe_paths: list[dict[str, Any]] = []

        for raw_path_data in _items(paths):
            path_data = _mapping(raw_path_data)
            category = _text(path_data.get("path_category"), "normal") or "normal"
            if category == "promotion_candidate_path":
                promotion_candidates.extend(_items(path_data.get("related_event_claims")))
            elif category == "contradiction_candidate_path":
                contradiction_candidates.extend(_items(path_data.get("related_event_claims")))
            elif category == "high_priority_probe_path":
                high_priority_probe_paths.append(path_data)

        # 去重 event claim id
        promo_ids = sorted({_text(item) for item in promotion_candidates if _text(item)})
        contra_ids = sorted({_text(item) for item in contradiction_candidates if _text(item)})

        if promo_ids:
            event_claims = getattr(self.kb, "event_claims", {})
            event_claims = event_claims if isinstance(event_claims, Mapping) else {}
            promo_claims = [_mapping_get(event_claims, eid) for eid in promo_ids]
            promo_claims = [claim for claim in promo_claims if claim is not None]
            promotion_results = self.promotion_engine.process_promotion_candidates(promo_claims)
            self._process_unprocessed_claims()

        for eid in contra_ids:
            event_claims = getattr(self.kb, "event_claims", {})
            event_claims = event_claims if isinstance(event_claims, Mapping) else {}
            claim = _mapping_get(event_claims, eid)
            if claim is None:
                continue
            records: list[Any] = []
            records.extend(self.contradiction_detector.detect_event_vs_stable(claim))
            records.extend(self.contradiction_detector.detect_event_vs_event(claim))
            if records:
                self.contradiction_detector.apply_event_conflict_records(records)
                contradiction_records.extend([r.to_dict() for r in records])

        if high_priority_probe_paths:
            event_gaps = self.probe.probe_event_driven_paths(high_priority_probe_paths, root)

        return promotion_results, contradiction_records, event_gaps

    def _enrich_paths_with_event_overlay(
        self,
        paths: list[dict[str, Any]],
        event_result: Any,
    ) -> list[dict[str, Any]]:
        """把事件传播结果中的 overlay 字段附加到路径上。

        Args:
            paths: FrontierScorer 输出的路径列表。
            event_result: DynamicEventPropagationEngine 传播结果。

        Returns:
            附加事件字段后的路径列表。
        """
        overlays = _mapping(getattr(event_result, "path_overlays", {})) if event_result else {}
        for raw_path_data in _items(paths):
            if not isinstance(raw_path_data, Mapping):
                continue
            path_data = raw_path_data
            path = [_text(node) for node in _items(path_data.get("path"))]
            path_id = "->".join(path)
            overlay = _mapping(_mapping_get(overlays, path_id, {}))
            related_ids = [_text(item) for item in _items(overlay.get("related_event_claim_ids")) if _text(item)]

            # final_score 优先使用事件调整后的分数
            final_score = _score(path_data.get("event_adjusted_score", path_data.get("path_alpha_score", 0.0)))

            path_data["event_boost_score"] = _score(overlay.get("event_boost_score", 0.0))
            path_data["event_risk_score"] = _score(overlay.get("event_risk_score", 0.0))
            path_data["freshness_score"] = _score(overlay.get("freshness_score", 0.0))
            path_data["promotion_pressure"] = _score(overlay.get("promotion_pressure", 0.0))
            path_data["contradiction_pressure"] = _score(overlay.get("contradiction_pressure", 0.0))
            path_data["ppr_activation_score"] = _score(path_data.get("ppr_activation_score", 0.0))
            path_data["final_score"] = final_score
            path_data["related_event_claims"] = related_ids
            path_data["event_overlay"] = overlay

        return paths

    def _identify_axes(self, paths: list[dict[str, Any]]) -> list[str]:
        """从路径中识别至少 3 条结构轴。"""
        axes_counter: dict[str, int] = {}
        for p in _items(paths):
            p = _mapping(p)
            for ns in _items(p.get("node_scores")):
                ns = _mapping(ns)
                for axis in _items(ns.get("axes")):
                    axis_name = _text(axis)
                    if axis_name:
                        axes_counter[axis_name] = axes_counter.get(axis_name, 0) + 1
        return sorted(axes_counter, key=lambda k: axes_counter[k], reverse=True)[:6]

    def _is_generic_path(self, path: list[str]) -> bool:
        """判断路径是否包含泛概念。"""
        return any(str(n).strip().lower() in GENERIC_BLACKLIST for n in path)

    def _explain_bottleneck(self, path_data: dict[str, Any]) -> dict[str, Any]:
        """为 Top path 生成卡脖子解释和证据缺口分析。"""
        path_data = _mapping(path_data)
        path = [_text(node) for node in _items(path_data.get("path"))]
        gaps = [gap for gap in _items(path_data.get("gaps")) if isinstance(gap, Mapping)]
        node_scores = [score for score in _items(path_data.get("node_scores")) if isinstance(score, Mapping)]

        # 关键瓶颈节点
        bottleneck_node = None
        max_bottleneck = 0.0
        for ns in node_scores:
            b = _score(_mapping(ns.get("dimensions")).get("bottleneck_score", 0))
            if b > max_bottleneck:
                max_bottleneck = b
                bottleneck_node = ns

        # 选取最相关 evidence，遍历所有已知关系类型
        evidences: list[str] = []
        evidence_sources: set[str] = set()
        for i in range(len(path) - 1):
            head, tail = path[i], path[i + 1]
            edge_claims: list[Any] = []
            try:
                relations = self.kb.ontology_registry.all_relations()
            except (AttributeError, TypeError, ValueError):
                relations = []
            for relation in _items(relations):
                try:
                    edge_claims.extend(_items(self.kb.get_claims_for_edge(head, relation, tail)))
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            for c in edge_claims[:2]:
                source_type = _text(getattr(c, "source_type", ""))
                evidence = _text(getattr(c, "evidence", ""))
                if evidence:
                    evidences.append(f"[{source_type}] {evidence}")
                if source_type:
                    evidence_sources.add(source_type)

        # 下一步数据源
        next_sources: list[str] = []
        if gaps:
            for g in gaps[:2]:
                if g.get("missing") == "多源证据":
                    next_sources.extend(["年报", "公告", "官网"])
                if g.get("relation") in (
                    RelationType.BOTTLENECK_OF.value,
                    RelationType.CONSTRAINED_BY.value,
                ):
                    next_sources.extend(["研报", "专利", "白皮书"])
        next_sources = list(dict.fromkeys(next_sources)) or ["问财", "东财", "网页搜索"]

        nodes = getattr(self.kb, "nodes", {})
        node_types: list[str] = []
        for node_name in path:
            node = _mapping_get(nodes, node_name)
            node_types.append(_text(_field(node, "node_type"), NodeType.GENERIC.value) or NodeType.GENERIC.value)
        bottleneck_dimensions = _mapping(bottleneck_node.get("dimensions")) if bottleneck_node else {}
        bottleneck_name = _text(bottleneck_node.get("node_name")) if bottleneck_node else ""
        return {
            "path": path,
            "path_alpha_score": _score(path_data.get("path_alpha_score", 0)),
            "path_confidence": _score(path_data.get("path_confidence", 0.0)),
            "path_noise_score": _score(path_data.get("path_noise_score", 0.0)),
            "path_evidence_gap": _items(path_data.get("path_evidence_gap")),
            "node_types": node_types,
            "bottleneck_node": bottleneck_name or (path[-1] if path else ""),
            "bottleneck_score": max_bottleneck,
            "why_bottleneck": (
                f"{bottleneck_name or '该节点'} 处于产业链上游关键位置，"
                f"bottleneck_score={max_bottleneck:.2f}，"
                f"且 evidence_score={_score(bottleneck_dimensions.get('evidence_score', 0)):.2f}"
                if bottleneck_node
                else "路径尾部节点可能是关键瓶颈。"
            ),
            "evidences": evidences[:3],
            "evidence_source_count": len(evidence_sources),
            "evidence_sources": sorted(evidence_sources),
            "missing_evidence": [
                f"{_text(g.get('head'))} → {_text(g.get('tail'))} 缺 {_text(g.get('missing'), '证据')}"
                for g in gaps[:3]
            ],
            "next_sources": next_sources[:4],
        }

    def _extract_evidence_gaps(
        self,
        path_data: dict[str, Any],
    ) -> list[EvidenceGap]:
        """从单条路径数据中提取结构化 EvidenceGap。

        优先使用 path_propagator 输出的 path_evidence_gap；
        若不存在，则回退到 frontier_scorer 的 gaps。
        """
        path_data = _mapping(path_data)
        path = [_text(node) for node in _items(path_data.get("path"))]
        if len(path) < 2:
            return []

        gaps = (
            path_data.get("evidence_gaps", [])
            or path_data.get("path_evidence_gap", [])
            or path_data.get("gaps", [])
        )
        gaps = [gap for gap in _items(gaps) if isinstance(gap, Mapping)]
        base_priority = _score(path_data.get("path_alpha_score", 0.5), 0.5)
        path_confidence = _score(path_data.get("path_confidence", 1.0), 1.0)
        # 置信度越低，缺口优先级越高
        confidence_penalty = max(0.0, 0.5 - path_confidence)

        results: list[EvidenceGap] = []
        seen: set[str] = set()
        for gap in gaps:
            head = _text(gap.get("head"))
            tail = _text(gap.get("tail"))
            relation = _text(gap.get("relation"))
            missing = _text(gap.get("missing"), "证据")
            if not head or not tail:
                continue

            gap_id = f"{head}|{relation}|{tail}|{missing}"
            if gap_id in seen:
                continue
            seen.add(gap_id)

            missing_entity = tail if self.entity_resolver.is_company(tail) else head
            missing_source_type = self._preferred_source_for_gap(missing, relation)
            priority = min(1.0, base_priority + confidence_penalty + 0.05 * len(gaps))
            results.append(EvidenceGap(
                gap_id=gap_id,
                path=path,
                missing_relation=relation or "evidence",
                missing_entity=missing_entity,
                missing_source_type=missing_source_type,
                reason=f"路径证据缺口: {head} → {tail} 缺 {missing}",
                priority=priority,
            ))

        # 对整体弱路径额外生成一个 gap
        if not gaps and path_confidence < 0.5:
            gap_id = f"weak|{'|'.join(path)}"
            if gap_id not in seen:
                results.append(EvidenceGap(
                    gap_id=gap_id,
                    path=path,
                    missing_relation="path_evidence",
                    missing_entity=path[-1] if path else "",
                    missing_source_type="研报",
                    reason=f"弱路径置信度不足，需要多源证据: {' → '.join(path)}",
                    priority=base_priority,
                ))

        return results

    @staticmethod
    def _preferred_source_for_gap(missing: str, relation: str) -> str:
        """根据缺口类型推荐数据源。"""
        if "供应商" in missing or relation == RelationType.SUPPLIED_BY.value:
            return "公告"
        if relation in (
            RelationType.BOTTLENECK_OF.value,
            RelationType.CONSTRAINED_BY.value,
            RelationType.IMPORT_DEPENDENT_ON.value,
        ):
            return "研报"
        if relation == RelationType.CERTIFIED_BY.value:
            return "公告"
        return "网页搜索"

    def emit_search_tasks(
        self,
        alpha_result: dict[str, Any],
    ) -> list[PerillaSearchTask]:
        """通过 Bridge 将 evidence gap 转成 PerillaSearchTask。

        Args:
            alpha_result: discover() 返回结果。

        Returns:
            新增搜索任务列表。
        """
        return self.bridge.emit_search_tasks_from_alpha_result(alpha_result)

    def _process_unprocessed_claims(self) -> list[str]:
        """Process all unprocessed claims through the promotion engine."""
        newly_processed: list[str] = []
        claims = getattr(self.kb, "claims", {})
        claims = claims.items() if isinstance(claims, Mapping) else []
        for claim_id, claim in claims:
            claim_id = _text(claim_id)
            if claim_id in self._processed_claim_ids:
                continue
            try:
                self.promotion_engine.process_new_claim(claim)
                self._processed_claim_ids.add(claim_id)
                newly_processed.append(claim_id)
            except Exception as e:
                logger.warning("处理 claim %s 失败: %s", claim_id, e)
        return newly_processed

    def _get_status_distribution(self) -> dict[str, int]:
        """Return the distribution of claim statuses in the KB."""
        distribution: dict[str, int] = {}
        claims = getattr(self.kb, "claims", {})
        claims = claims.values() if isinstance(claims, Mapping) else []
        for claim in claims:
            status = _text(getattr(claim, "status", "unknown"), "unknown")
            distribution[status] = distribution.get(status, 0) + 1
        return distribution

    def _get_trusted_edges(self) -> list[dict[str, Any]]:
        """Return edges with at least one trusted claim."""
        trusted: list[dict[str, Any]] = []
        edges = getattr(self.kb, "edges", {})
        claims = getattr(self.kb, "claims", {})
        edges = edges.values() if isinstance(edges, Mapping) else []
        claims = claims if isinstance(claims, Mapping) else {}
        for edge in edges:
            has_trusted = any(
                _text(_field(_mapping_get(claims, cid), "status")) == "trusted"
                for cid in _items(_field(edge, "claim_ids"))
            )
            if has_trusted:
                to_dict = getattr(edge, "to_dict", None)
                if callable(to_dict):
                    try:
                        trusted.append(_mapping(to_dict()))
                    except (AttributeError, TypeError, ValueError):
                        continue
                elif isinstance(edge, Mapping):
                    trusted.append(dict(edge))
        return trusted

    def _get_conflicted_edges(self) -> list[dict[str, Any]]:
        """Return edges with conflict status != none."""
        conflicted: list[dict[str, Any]] = []
        edges = getattr(self.kb, "edges", {})
        edges = edges.values() if isinstance(edges, Mapping) else []
        for edge in edges:
            if _text(_field(edge, "conflict_status"), "none") != "none" or _items(_field(edge, "contradicting_claim_ids")):
                to_dict = getattr(edge, "to_dict", None)
                if callable(to_dict):
                    try:
                        conflicted.append(_mapping(to_dict()))
                    except (AttributeError, TypeError, ValueError):
                        continue
                elif isinstance(edge, Mapping):
                    conflicted.append(dict(edge))
        return conflicted

    def _enrich_path_with_confidence(
        self,
        path_data: dict[str, Any],
        path_records: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach path_confidence and evidence_gap from path propagator."""
        path_data = _mapping(path_data)
        path = [_text(node) for node in _items(path_data.get("path"))]
        path_id = PathConfidencePropagator._path_id(path)
        record = _mapping_get(path_records, path_id)
        if record:
            path_data["path_confidence"] = _score(_field(record, "path_confidence"))
            path_data["path_noise_score"] = _score(_field(record, "path_noise_score"))
            path_data["path_evidence_gap"] = _items(_field(record, "path_evidence_gap"))
        else:
            path_data["path_confidence"] = 0.0
            path_data["path_noise_score"] = 0.0
            path_data["path_evidence_gap"] = []
        return path_data

    def discover(self, root: str) -> dict[str, Any]:
        """对 root 执行 Path-based Alpha Discovery。

        新流程：
        StructureNavigator -> PathCandidate -> FlowPathPruner -> FrontierScorer
        -> PathEvidenceBundle -> LLMPathVerifier -> PathQualityEvaluator
        -> ClaimPromotionEngine -> ContradictionDetector -> PathConfidencePropagator
        -> AlphaPathResult

        Args:
            root: 根概念

        Returns:
            {"root": str, "axes": list, "top_paths": list,
             "alpha_path_results": list, "evidence_gaps": list, ...}
        """
        root = _text(root)
        if not root:
            return {"root": "", "axes": [], "top_paths": [], "alpha_path_results": [], "evidence_gaps": []}
        self.iteration_logs = []
        logger.info("AlphaDiscoveryEngine 开始发现: %s", root)
        start_time = datetime.now().isoformat()

        # 0. 优先接入巨浪研报
        julang_added = 0
        if self.julang_provider.is_available():
            julang_added = self.julang_provider.ingest_to_kb(
                self.kb, query=root, root_concept=root, graph_builder=self.graph_builder
            )
            self.iteration_logs.append({
                "step": "julang_ingest",
                "added_claims": julang_added,
                "nodes": self.kb.node_count(),
                "edges": self.kb.edge_count(),
            })
        else:
            self.iteration_logs.append({"step": "julang_ingest", "added_claims": 0, "available": False})

        self._process_unprocessed_claims()

        # 1. 构建/丰富 subgraph
        nav_result = self.navigator.build_or_enrich(root, max_breadth=5)
        self.iteration_logs.append({
            "step": "navigate",
            "was_sparse": nav_result["was_sparse"],
            "added_claims": nav_result["added_claims"],
            "nodes": self.kb.node_count(),
            "edges": self.kb.edge_count(),
        })
        self._process_unprocessed_claims()

        # 2. 事件传播 overlay
        event_result = self.event_engine.propagate_for_root(root) if self.event_engine else None
        raw_event_overlays = getattr(event_result, "path_overlays", {}) if event_result else {}

        # 3. 生成 PathCandidate
        candidates = self.navigator.generate_path_candidates(root, max_depth=3, max_paths=500)

        # 4. Flow-based pruning
        pruned_paths = self.flow_pruner.prune(candidates, query=root, top_k=100)

        # 5. 事件 overlay 按 path_id 对齐
        event_overlay_by_path_id = self._map_event_overlays(pruned_paths, raw_event_overlays)

        # 6. FrontierScorer 初步排序
        ranked = self.scorer.rank_pruned_paths(pruned_paths, event_overlays=event_overlay_by_path_id)

        # 7. 迭代补证据
        for iteration in range(self.max_iterations):
            probe_targets = [p for p in ranked if p.get("evidence_gap_score", 0) > 0][:5]
            if not probe_targets:
                break

            added_total = 0
            for path_data in probe_targets:
                candidate = _mapping_get(path_data, "_candidate")
                if not isinstance(candidate, PathCandidate):
                    continue
                for gap in _items(_mapping_get(path_data, "evidence_gaps"))[:2]:
                    try:
                        added_total += len(_items(self.probe.probe_gap(gap)))
                    except (AttributeError, KeyError, TypeError, ValueError):
                        logger.warning("failed to probe path evidence gap", exc_info=True)
                tail = _text(candidate.target_entity)
                nodes = getattr(self.kb, "nodes", {})
                node = _mapping_get(nodes, tail)
                if tail and node is not None:
                    try:
                        added_total += len(_items(self.probe.probe_node(tail, axes=_items(_field(node, "axes")))))
                    except (AttributeError, KeyError, TypeError, ValueError):
                        logger.warning("failed to probe path tail node %s", tail, exc_info=True)

            self.iteration_logs.append({
                "iteration": iteration + 1,
                "probe_path_ids": [p.get("path_id") for p in probe_targets],
                "added_claims": added_total,
                "nodes": self.kb.node_count(),
                "edges": self.kb.edge_count(),
            })

            if added_total == 0:
                break

            self._process_unprocessed_claims()

            # 重新生成路径并排序
            candidates = self.navigator.generate_path_candidates(root, max_depth=3, max_paths=500)
            pruned_paths = self.flow_pruner.prune(candidates, query=root, top_k=100)
            event_overlay_by_path_id = self._map_event_overlays(pruned_paths, raw_event_overlays)
            ranked = self.scorer.rank_pruned_paths(pruned_paths, event_overlays=event_overlay_by_path_id)

        # 8. 路径级审查流水线
        alpha_path_results: list[AlphaPathResult] = []
        for path_data in ranked[: self.top_k]:
            candidate = path_data.get("_candidate")
            if not isinstance(candidate, PathCandidate):
                continue
            overlay = _mapping_get(event_overlay_by_path_id, _text(candidate.path_id), {})
            bundle = self.probe.bind_path_evidence(candidate, include_event_claims=True)
            verification = self.llm_path_verifier.verify(bundle, query=root)
            quality_report = self.path_quality_evaluator.evaluate_path_candidate(
                candidate, bundle, verification, overlay
            )
            promotion_decision = self.promotion_engine.promote_from_path_evidence(
                bundle, verification, quality_report
            )
            contradiction_records = self.contradiction_detector.detect_path_contradictions(bundle)
            path_record = self.path_propagator.propagate_path(
                candidate, bundle, verification, quality_report, overlay
            )

            alpha_path_results.append(
                self._build_alpha_path_result(
                    path_data,
                    candidate,
                    bundle,
                    verification,
                    quality_report,
                    promotion_decision,
                    contradiction_records,
                    path_record,
                )
            )

        # 9. 结构轴识别
        axes = self._identify_axes(ranked[: self.top_k])

        # 10. 提取 EvidenceGap
        evidence_gaps: list[EvidenceGap] = []
        for path_data in ranked[: self.top_k]:
            evidence_gaps.extend(self._extract_evidence_gaps(path_data))

        # 11. 统计与输出
        status_distribution = self._get_status_distribution()
        trusted_edges = self._get_trusted_edges()
        conflicted_edges = self._get_conflicted_edges()
        path_quality_report = self._aggregate_quality_report(alpha_path_results)

        result = {
            "root": root,
            "start_time": start_time,
            "end_time": datetime.now().isoformat(),
            "axes": axes,
            "top_paths": [apr.to_dict() for apr in alpha_path_results],
            "alpha_path_results": [apr.to_dict() for apr in alpha_path_results],
            "evidence_gaps": [g.to_dict() for g in evidence_gaps],
            "kb_stats": {
                "nodes": self.kb.node_count(),
                "edges": self.kb.edge_count(),
                "claims": self.kb.claim_count(),
                "event_edges": self.kb.event_edge_count(),
                "event_claims": self.kb.event_claim_count(),
            },
            "iterations": self.iteration_logs,
            "entity_resolver_status": self.entity_resolver.data_source_status(),
            "path_quality_report": path_quality_report,
            "status_distribution": status_distribution,
            "trusted_edges": trusted_edges,
            "conflicted_edges": conflicted_edges,
        }

        # 12. 路径情景压力测试(差距 3:让 scenario_simulator 出报告)
        path_scenarios = self._run_path_scenarios(alpha_path_results)
        result["path_scenarios"] = path_scenarios

        # 13. 主动学习计划(差距 5:把 EvidenceGap 变成高价值学习任务)
        learning_plan = self._build_learning_plan(result)
        result["learning_plan"] = [p.to_dict() for p in learning_plan]

        # 14. 通过 Bridge 输出搜索任务（Alpha -> Perilla）
        search_tasks = self.emit_search_tasks(result)
        result["search_tasks"] = [t.to_dict() for t in search_tasks]

        logger.info(
            "AlphaDiscoveryEngine 完成 %s: %d nodes, %d edges, %d top_paths, "
            "trusted=%d, conflicted=%d, event_edges=%d, event_claims=%d, search_tasks=%d",
            root,
            result["kb_stats"]["nodes"],
            result["kb_stats"]["edges"],
            len(alpha_path_results),
            len(trusted_edges),
            len(conflicted_edges),
            result["kb_stats"].get("event_edges", 0),
            result["kb_stats"].get("event_claims", 0),
            len(search_tasks),
        )
        return result

    def _build_learning_plan(
        self,
        alpha_result: dict[str, Any],
    ) -> list[Any]:
        """从 Alpha 结果生成主动学习计划(差距 5)。"""
        try:
            return self._learning_planner.plan_from_alpha_result(
                alpha_result,
                reference_date=alpha_result.get("end_time", "")[:10],
            )
        except Exception as exc:
            logger.warning("生成主动学习计划失败: %s", exc)
            return []

    def _run_path_scenarios(
        self,
        alpha_path_results: list[Any],
    ) -> dict[str, Any]:
        """对 Top 路径构造内存神经元图并跑情景压力测试。

        把每条路径的节点串成链,对路径首节点(root)施加冲击,
        观察下游节点置信度变化,输出情景报告。
        """
        scenarios = [
            ("demand_boom", 0.15, 2),
            ("supply_shock", -0.15, 2),
            ("price_pressure", -0.20, 2),
        ]
        all_reports: dict[str, Any] = {}

        class _FakeNeuron:
            def __init__(
                self, neuron_id: str, canonical_name: str, node_type: str, confidence: float
            ):
                self.neuron_id = neuron_id
                self.canonical_name = canonical_name
                self.node_type = node_type
                self.confidence = confidence
                self.is_tombstone = False

        class _FakeSynapse:
            def __init__(
                self,
                synapse_id: str,
                source_neuron_id: str,
                target_neuron_id: str,
                strength: float,
            ):
                self.synapse_id = synapse_id
                self.source_neuron_id = source_neuron_id
                self.target_neuron_id = target_neuron_id
                self.target_kind = "EXISTING_NEURON"
                self.strength = strength
                self.direction = "DOWNSTREAM_APPLICATION"
                self.relation = "DOWNSTREAM_OF"
                self.status = "OPEN"

        for apr in alpha_path_results[:3]:
            path_nodes = [_text(node) for node in _items(_field(apr, "path")) if _text(node)]
            if not path_nodes or len(path_nodes) < 2:
                continue
            path_id = _text(_field(apr, "path_id")) or "->".join(path_nodes)

            neurons: dict[str, Any] = {}
            synapses: dict[str, Any] = {}
            edge_confidences = (
                _items(_field(apr, "edge_confidences"))
                if _items(_field(apr, "edge_confidences"))
                else [0.7] * (len(path_nodes) - 1)
            )

            for i, node_name in enumerate(path_nodes):
                nid = f"node_{i}_{node_name}"
                neurons[nid] = _FakeNeuron(
                    neuron_id=nid,
                    canonical_name=node_name,
                    node_type="CONCEPT",
                    confidence=0.5,
                )

            for i in range(len(path_nodes) - 1):
                src_id = f"node_{i}_{path_nodes[i]}"
                tgt_id = f"node_{i + 1}_{path_nodes[i + 1]}"
                strength = _score(edge_confidences[i]) if i < len(edge_confidences) else 0.7
                syn_id = f"syn_{src_id}_{tgt_id}"
                synapses[syn_id] = _FakeSynapse(
                    synapse_id=syn_id,
                    source_neuron_id=src_id,
                    target_neuron_id=tgt_id,
                    strength=max(0.1, min(1.0, strength)),
                )

            try:
                simulator = ScenarioSimulator.from_neurons_and_synapses(neurons, synapses)
                root_id = f"node_0_{path_nodes[0]}"
                path_reports: dict[str, Any] = {}
                for label, shock, steps in scenarios:
                    report = simulator.simulate_intervention(root_id, shock, steps)
                    to_dict = getattr(report, "to_dict", None)
                    path_reports[label] = to_dict() if callable(to_dict) else {}
                all_reports[path_id] = path_reports
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning("failed to simulate path scenarios for %s", path_id, exc_info=True)

        return all_reports

    def _map_event_overlays(
        self,
        pruned_paths: list[Any],
        event_overlays: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """将事件 overlay 从 path string 映射到 path_id。"""
        result: dict[str, dict[str, Any]] = {}
        overlays = _mapping(event_overlays)
        for pp in _items(pruned_paths):
            candidate = getattr(pp, "path_candidate", None)
            if not isinstance(candidate, PathCandidate):
                continue
            path_str = "->".join(_text(node) for node in _items(candidate.nodes))
            overlay = _mapping(
                _mapping_get(overlays, path_str)
                or _mapping_get(overlays, _text(candidate.path_id))
            )
            if overlay:
                result[_text(candidate.path_id)] = dict(overlay)
        return result

    def _build_alpha_path_result(
        self,
        path_data: dict[str, Any],
        candidate: PathCandidate,
        bundle: PathEvidenceBundle,
        verification: PathVerificationResult,
        quality_report: PathQualityReport,
        promotion_decision: Any,
        contradiction_records: list[Any],
        path_record: Any,
    ) -> AlphaPathResult:
        """从路径审查结果构建 AlphaPathResult。"""
        path_data = _mapping(path_data)
        contradiction_score = _score(path_data.get("contradiction_risk_score", 0.0))
        contradiction_records = _items(contradiction_records)
        if contradiction_records:
            high = sum(1 for r in contradiction_records if _text(_field(r, "severity")) == "high")
            contradiction_score = max(contradiction_score, min(1.0, high * 0.3))

        path_confidence = _score(_field(path_record, "final_confidence"))
        final_score = (
            path_confidence * 0.5
            + _score(path_data.get("final_score", 0.0)) * 0.3
            + _score(_field(quality_report, "final_quality_score")) * 0.2
        )
        final_score = _score(final_score)

        next_probe_tasks: list[dict[str, Any]] = []
        for item in _items(_field(bundle, "missing_evidence")):
            if not isinstance(item, Mapping):
                continue
            next_probe_tasks.append({
                "edge_key": item.get("edge_key"),
                "reason": item.get("reason"),
                "preferred_sources": _items(item.get("preferred_sources")),
            })

        path_nodes = [_text(node) for node in _items(_field(candidate, "nodes")) if _text(node)]
        path_id = _text(_field(candidate, "path_id"))
        path_type = _text(_field(candidate, "path_type"), "unknown") or "unknown"
        quality_label = _text(_field(quality_report, "quality_label"), "UNKNOWN") or "UNKNOWN"
        promoted_claims = _items(_field(promotion_decision, "promotable_claims"))
        rejected_claims = _items(_field(promotion_decision, "rejected_claims"))
        explanation = (
            f"路径 {' -> '.join(path_nodes)} 为 {path_type}，"
            f"质量标签 {quality_label}，"
            f"最终置信度 {final_score:.2f}。"
        )
        if promoted_claims:
            explanation += f" 晋升 {len(promoted_claims)} 条 claim。"
        if contradiction_records:
            explanation += f" 检测到 {len(contradiction_records)} 条路径级冲突。"

        return AlphaPathResult(
            path_id=path_id,
            path=path_nodes,
            path_type=path_type,
            stable_score=round(_score(path_data.get("structural_score", 0.0)), 3),
            event_score=round(_score(path_data.get("event_adjusted_score", 0.0)), 3),
            evidence_score=round(_score(_field(quality_report, "evidence_score")), 3),
            verification_score=round(_score(_field(verification, "validity_score")), 3),
            contradiction_score=round(contradiction_score, 3),
            final_score=round(final_score, 3),
            evidence_chunks=_items(_field(bundle, "evidence_chunks")),
            promoted_claims=promoted_claims,
            rejected_claims=rejected_claims,
            missing_evidence=_items(_field(bundle, "missing_evidence")),
            next_probe_tasks=next_probe_tasks,
            explanation=explanation,
            quality_label=quality_label,
        )

    def _aggregate_quality_report(
        self,
        alpha_path_results: list[AlphaPathResult],
    ) -> dict[str, Any]:
        """汇总路径质量报告。"""
        if not alpha_path_results:
            return {"label_distribution": {}, "average_final_quality_score": 0.0}
        label_distribution: dict[str, int] = {}
        total_score = 0.0
        for apr in _items(alpha_path_results):
            if not isinstance(apr, AlphaPathResult):
                continue
            label = _text(apr.quality_label, "UNKNOWN")
            label_distribution[label] = label_distribution.get(label, 0) + 1
            total_score += _score(apr.final_score)
        if not label_distribution:
            return {"label_distribution": {}, "average_final_quality_score": 0.0}
        return {
            "label_distribution": label_distribution,
            "average_final_quality_score": round(total_score / sum(label_distribution.values()), 3),
        }

    def discover_multi(
        self,
        roots: list[str],
        output_dir: str | Path = "data/reports/perilla_discovery",
    ) -> dict[str, Any]:
        """对多个 root 执行发现，并生成汇总报告。

        Args:
            roots: 根概念列表
            output_dir: 报告输出目录

        Returns:
            {"roots": list, "results": dict, "summary": dict}
        """
        roots = [_text(root) for root in _items(roots) if _text(root)]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        all_results: dict[str, Any] = {}
        for root in roots:
            result = self.discover(root)
            all_results[root] = result

            # 保存单个 root 报告
            safe_root = _text(root).replace("/", "_").replace("\\", "_")
            report_path = output_dir / f"alpha_discovery_{safe_root}.json"
            try:
                report_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except (OSError, TypeError, ValueError):
                logger.warning("保存 root 报告失败: %s", report_path, exc_info=True)

        # 汇总
        generic_count = 0
        material_process_equipment_count = 0
        total_top_paths = 0
        multi_source_paths = 0

        for root, result in all_results.items():
            for raw_path in _items(_mapping(result).get("top_paths")):
                p = _mapping(raw_path)
                total_top_paths += 1
                path = [_text(n) for n in _items(p.get("path"))]
                if any(n.lower() in GENERIC_BLACKLIST for n in path):
                    generic_count += 1
                # 检查是否落在 material/process/equipment/testing/consumable 方向
                if any(t in (
                    NodeType.MATERIAL.value,
                    NodeType.PROCESS.value,
                    NodeType.EQUIPMENT.value,
                    NodeType.TESTING.value,
                    NodeType.CONSUMABLE.value,
                ) for t in _items(p.get("node_types"))):
                    material_process_equipment_count += 1
                # 检查是否至少 2 个不同来源 claim
                if _finite(p.get("evidence_source_count")) >= 2:
                    multi_source_paths += 1

        summary = {
            "total_roots": len(roots),
            "total_top_paths": total_top_paths,
            "generic_paths": generic_count,
            "material_process_equipment_paths": material_process_equipment_count,
            "multi_source_paths": multi_source_paths,
            "roots": roots,
        }

        # 保存汇总
        summary_path = output_dir / "alpha_discovery_summary.json"
        try:
            summary_path.write_text(
                json.dumps({"results": all_results, "summary": summary}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            logger.warning("保存 Alpha 汇总报告失败: %s", summary_path, exc_info=True)

        return {"roots": roots, "results": all_results, "summary": summary}
