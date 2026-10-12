"""Flow-based path pruning for Path-based Alpha Discovery.

借鉴 PathRAG 的 flow-based pruning 思想：路径不是按单一 score 排序，
而是根据 query intent、axis weight、edge confidence、claim strength、
source reliability、event overlay、path length 等计算 path_flow_score，
再剪枝冗余路径并保留多样化 top-k。
"""
from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.research.perilla.alpha.graph_schema import StructuralAxis
from core.research.perilla.alpha.path_discovery_schema import PathCandidate
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite(value, default)))


def _limit(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _mapping_get(store: Any, key: Any) -> Any:
    if not isinstance(store, Mapping):
        return None
    try:
        return store.get(key)
    except (TypeError, ValueError):
        return None


@dataclass
class PrunedPath:
    """剪枝后的路径项，附带 flow score 与剪枝原因。"""

    path_candidate: PathCandidate
    flow_score: float = 0.0
    pruning_reason: str = ""
    component_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path_candidate": self.path_candidate.to_dict() if isinstance(self.path_candidate, PathCandidate) else {},
            "flow_score": _score(self.flow_score),
            "pruning_reason": str(self.pruning_reason or ""),
            "component_scores": {
                str(key): _score(value) for key, value in self.component_scores.items()
            } if isinstance(self.component_scores, Mapping) else {},
        }


class PathFlowScorer:
    """计算单条 PathCandidate 的 flow score。"""

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
        """
        self.kb = kb

    def score(
        self,
        candidate: PathCandidate,
        query: str = "",
        axis_weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """计算路径流分数。

        Args:
            candidate: 路径候选。
            query: 查询文本，用于 root relevance。
            axis_weights: 结构轴权重覆盖。

        Returns:
            (flow_score, component_scores)
        """
        if not isinstance(candidate, PathCandidate):
            return 0.0, {}
        axis_weights = dict(axis_weights) if isinstance(axis_weights, Mapping) else {}
        path = [str(node) for node in candidate.nodes if node is not None] if isinstance(candidate.nodes, (list, tuple)) else []
        edges = candidate.edges if isinstance(candidate.edges, (list, tuple)) else []

        # root relevance：路径包含 query 相关实体（简单子串匹配）
        query = query if isinstance(query, str) else ""
        query_terms = {q.strip() for q in query.split() if len(q.strip()) > 1}
        root_relevance = 0.0
        if query_terms:
            matched = sum(1 for n in path if any(t in n for t in query_terms))
            root_relevance = min(1.0, matched / max(1, len(query_terms)))
        else:
            root_relevance = 1.0

        # axis alignment：路径覆盖的轴与权重的匹配度
        axis_alignment = 0.0
        axes = [axis for axis in candidate.axes if isinstance(axis, str)] if isinstance(candidate.axes, (list, tuple)) else []
        if axes:
            weights = []
            for axis in axes:
                weights.append(_score(axis_weights.get(axis, self._default_axis_weight(axis))))
            axis_alignment = min(1.0, sum(weights) / len(weights))
        else:
            axis_alignment = 0.3

        # relation reliability & edge confidence
        edge_confidences: list[float] = []
        source_weights: list[float] = []
        kb_edges = getattr(self.kb, "edges", {})
        kb_claims = getattr(self.kb, "claims", {})
        for edge_key in edges:
            edge = _mapping_get(kb_edges, edge_key)
            if edge is None:
                edge_confidences.append(0.0)
                continue
            edge_confidences.append(_score(_field(edge, "confidence", 0.0)))
            source_values = _field(edge, "source_weights", {})
            if isinstance(source_values, Mapping) and source_values:
                source_weights.append(sum(_score(value) for value in source_values.values()) / len(source_values))
            else:
                source_weights.append(0.0)

        relation_reliability = min(1.0, sum(edge_confidences) / max(1, len(edge_confidences)))
        edge_confidence_score = relation_reliability
        source_reliability = min(1.0, sum(source_weights) / max(1, len(source_weights)))

        # claim support：路径上稳定 claim 的平均 score
        claim_scores: list[float] = []
        for edge_key in edges:
            edge = _mapping_get(kb_edges, edge_key)
            if not edge:
                continue
            for cid in _string_list(_field(edge, "claim_ids", [])):
                claim = _mapping_get(kb_claims, cid)
                if claim:
                    claim_scores.append(_score(
                        _field(claim, "claim_score", 0.0)
                        or _field(claim, "confidence", 0.0)
                    ))
        claim_support = sum(claim_scores) / max(1, len(claim_scores)) if claim_scores else 0.0

        # evidence availability：每条边是否有 claim
        evidence_availability = sum(
            1 for e in edges if _field(_mapping_get(kb_edges, e), "claim_ids", [])
        ) / max(1, len(edges))

        # event activation：本模块不直接读取 event graph，由调用方通过 axis_weights 传入
        event_activation = _score(axis_weights.get("_event_activation", 0.0))

        # path length penalty
        length_penalty = 1.0
        hop_count = _limit(candidate.hop_count, 0, 1000)
        if hop_count == 1:
            length_penalty = 0.85
        elif hop_count == 2:
            length_penalty = 1.0
        elif hop_count == 3:
            length_penalty = 0.95
        else:
            length_penalty = 0.8

        component_scores = {
            "root_relevance": round(root_relevance, 3),
            "axis_alignment": round(axis_alignment, 3),
            "relation_reliability": round(relation_reliability, 3),
            "edge_confidence": round(edge_confidence_score, 3),
            "claim_support": round(claim_support, 3),
            "evidence_availability": round(evidence_availability, 3),
            "event_activation": round(event_activation, 3),
            "source_reliability": round(source_reliability, 3),
            "length_penalty": round(length_penalty, 3),
        }

        # 事件热度不应覆盖结构置信度：event_activation 权重较低
        flow_score = (
            root_relevance * 0.10
            + axis_alignment * 0.20
            + relation_reliability * 0.15
            + edge_confidence_score * 0.15
            + claim_support * 0.15
            + evidence_availability * 0.10
            + event_activation * 0.05
            + source_reliability * 0.10
        ) * length_penalty

        return round(max(0.0, min(1.0, flow_score)), 3), component_scores

    @staticmethod
    def _default_axis_weight(axis: str) -> float:
        """默认结构轴权重。"""
        preferred = {
            StructuralAxis.MATERIAL.value: 0.9,
            StructuralAxis.EQUIPMENT.value: 0.9,
            StructuralAxis.PROCESS.value: 0.85,
            StructuralAxis.CERTIFICATION.value: 0.85,
            StructuralAxis.SUPPLIER.value: 0.75,
            StructuralAxis.DOMESTIC_SUBSTITUTION.value: 0.8,
            StructuralAxis.PERFORMANCE.value: 0.6,
            StructuralAxis.COST.value: 0.5,
            StructuralAxis.VALUATION.value: 0.3,
        }
        return preferred.get(axis, 0.5)


class PathDiversitySelector:
    """在 top-k 选择中保证路径多样化。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        min_axes: int = 3,
        max_same_path_type: int = 3,
        max_same_target: int = 2,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            min_axes: 至少覆盖多少个不同结构轴。
            max_same_path_type: 同一路径类型最多保留几条。
            max_same_target: 同一目标实体最多保留几条。
        """
        self.kb = kb
        self.min_axes = _limit(min_axes, 3, 100)
        self.max_same_path_type = _limit(max_same_path_type, 3, 100)
        self.max_same_target = _limit(max_same_target, 2, 100)

    def select(
        self,
        scored_paths: list[PrunedPath],
        top_k: int = 20,
    ) -> list[PrunedPath]:
        """选择高分且多样化的路径。

        Args:
            scored_paths: 已按 flow_score 降序排列的路径。
            top_k: 最多保留数量。

        Returns:
            选中的 PrunedPath 列表。
        """
        top_k = _limit(top_k, 20)
        selected: list[PrunedPath] = []
        type_counter: dict[str, int] = {}
        target_counter: dict[str, int] = {}
        covered_axes: set[str] = set()

        # 第一轮：优先保证轴覆盖
        for pp in scored_paths if isinstance(scored_paths, (list, tuple)) else ():
            if not isinstance(pp, PrunedPath) or not isinstance(pp.path_candidate, PathCandidate):
                continue
            if len(selected) >= top_k:
                break
            path_type = str(pp.path_candidate.path_type or "unknown")
            target = str(pp.path_candidate.target_entity or "")

            if type_counter.get(path_type, 0) >= self.max_same_path_type:
                continue
            if target_counter.get(target, 0) >= self.max_same_target:
                continue

            selected.append(pp)
            type_counter[path_type] = type_counter.get(path_type, 0) + 1
            target_counter[target] = target_counter.get(target, 0) + 1
            candidate_axes = pp.path_candidate.axes if isinstance(pp.path_candidate.axes, (list, tuple)) else []
            covered_axes.update(axis for axis in candidate_axes if isinstance(axis, str))

        # 第二轮：如果轴覆盖不足，放宽 type/target 限制补充
        if len(covered_axes) < self.min_axes:
            for pp in scored_paths if isinstance(scored_paths, (list, tuple)) else ():
                if not isinstance(pp, PrunedPath) or not isinstance(pp.path_candidate, PathCandidate):
                    continue
                if len(selected) >= top_k:
                    break
                if pp in selected:
                    continue
                candidate_axes = pp.path_candidate.axes if isinstance(pp.path_candidate.axes, (list, tuple)) else []
                new_axes = {axis for axis in candidate_axes if isinstance(axis, str)} - covered_axes
                if new_axes:
                    selected.append(pp)
                    covered_axes.update(new_axes)

        return selected


class FlowPathPruner:
    """路径流剪枝器：剪掉低 flow、冗余、噪声路径。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        min_flow_score: float = 0.15,
        redundancy_threshold: float = 0.85,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            min_flow_score: 最低 flow score，低于该值直接剪枝。
            redundancy_threshold: 路径节点集合 Jaccard 相似度阈值，超过视为冗余。
        """
        self.kb = kb
        self.min_flow_score = _score(min_flow_score, 0.15)
        self.redundancy_threshold = _score(redundancy_threshold, 0.85)
        self.flow_scorer = PathFlowScorer(kb=kb)
        self.diversity_selector = PathDiversitySelector(kb=kb)

    def prune(
        self,
        candidates: list[PathCandidate],
        query: str = "",
        axis_weights: dict[str, float] | None = None,
        top_k: int = 20,
    ) -> list[PrunedPath]:
        """对 PathCandidate 列表进行 flow-based 剪枝。

        Args:
            candidates: 路径候选。
            query: 查询文本。
            axis_weights: 结构轴/事件权重覆盖。
            top_k: 最多保留数量。

        Returns:
            剪枝后的 PrunedPath 列表。
        """
        top_k = _limit(top_k, 20)
        axis_weights = dict(axis_weights) if isinstance(axis_weights, Mapping) else {}

        # 1. 计算 flow score
        scored: list[PrunedPath] = []
        for candidate in candidates if isinstance(candidates, (list, tuple)) else ():
            if not isinstance(candidate, PathCandidate):
                continue
            flow_score, components = self.flow_scorer.score(candidate, query=query, axis_weights=axis_weights)
            if flow_score < self.min_flow_score:
                continue
            scored.append(PrunedPath(
                path_candidate=candidate,
                flow_score=flow_score,
                component_scores=components,
            ))

        scored.sort(key=lambda x: x.flow_score, reverse=True)

        # 2. 冗余剪枝：按节点集合 Jaccard 相似度去重
        pruned: list[PrunedPath] = []
        selected_node_sets: list[set[str]] = []
        for pp in scored:
            node_set = {
                str(node) for node in pp.path_candidate.nodes
                if node is not None
            } if isinstance(pp.path_candidate.nodes, (list, tuple)) else set()
            redundant = False
            for existing_set in selected_node_sets:
                intersection = len(node_set & existing_set)
                union = len(node_set | existing_set)
                if union > 0 and intersection / union >= self.redundancy_threshold:
                    redundant = True
                    break
            if redundant:
                pp.pruning_reason = "redundant"
                continue
            selected_node_sets.append(node_set)
            pruned.append(pp)

        # 3. 多样化选择
        selected = self.diversity_selector.select(pruned, top_k=top_k)

        # 标记未选中的原因
        selected_ids = {id(pp.path_candidate) for pp in selected}
        for pp in pruned:
            if id(pp.path_candidate) not in selected_ids and not pp.pruning_reason:
                pp.pruning_reason = "diversity_dropped"

        candidate_count = len(candidates) if isinstance(candidates, (list, tuple)) else 0
        logger.info(
            "FlowPathPruner: %d candidates -> %d after flow filter -> %d selected",
            candidate_count,
            len(pruned),
            len(selected),
        )
        return selected

    @staticmethod
    def _path_signature(path: list[str]) -> str:
        """生成路径签名，用于日志/去重。"""
        values = path if isinstance(path, (list, tuple)) else []
        return hashlib.sha256(
            "|".join(str(item) for item in values if item is not None).encode("utf-8")
        ).hexdigest()[:12]
