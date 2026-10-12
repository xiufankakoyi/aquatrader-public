"""GraphRAG 双路/多路检索策略层。

SAGKnowledgeBase 是存储层，DualRetriever 是检索策略层。
固定检索顺序：
1. Entity Linking
2. Stable Graph Path Expansion
3. Axis Filtering
4. Meta-path Candidate Generation
5. Event Signal Overlay
6. Evidence Chunk Binding
7. LLM Verification（本层只准备 bundle，不调用 LLM）
8. EventClaim / StableClaim Update（由调用方完成）

禁止把检索退化成 query → vector chunks → LLM。
"""
from __future__ import annotations

import heapq
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import count
from statistics import harmonic_mean
from typing import Any

from core.research.perilla.alpha.graph_schema import get_source_weight
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.event.event_graph_schema import EventClaim, EventEdge
from core.research.perilla.alpha.sag_knowledge_base import Edge, SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _score(value: Any) -> float:
    return max(0.0, min(1.0, _finite_float(value)))


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


def _serialize(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, (list, tuple)):
        return []
    encoded: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            encoded.append(dict(item))
            continue
        serializer = getattr(item, "to_dict", None)
        if callable(serializer):
            try:
                value = serializer()
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(value, Mapping):
                encoded.append(dict(value))
    return encoded


@dataclass
class PathEvidenceBundle:
    """供 LLM verification 使用的路径-证据包。"""

    path: list[str]
    path_id: str
    stable_edges: list[Edge] = field(default_factory=list)
    event_edges: list[EventEdge] = field(default_factory=list)
    event_claims: list[EventClaim] = field(default_factory=list)
    evidence_chunks: list[dict[str, Any]] = field(default_factory=list)
    stable_path_score: float = 0.0
    event_signal_score: float = 0.0
    evidence_score: float = 0.0
    freshness_score: float = 0.0
    source_reliability_score: float = 0.0
    fused_score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "path": [str(item) for item in self.path if item is not None] if isinstance(self.path, (list, tuple)) else [],
            "path_id": str(self.path_id or ""),
            "stable_edges": _serialize(self.stable_edges),
            "event_edges": _serialize(self.event_edges),
            "event_claims": _serialize(self.event_claims),
            "evidence_chunks": _serialize(self.evidence_chunks),
            "stable_path_score": _score(self.stable_path_score),
            "event_signal_score": _score(self.event_signal_score),
            "evidence_score": _score(self.evidence_score),
            "freshness_score": _score(self.freshness_score),
            "source_reliability_score": _score(self.source_reliability_score),
            "fused_score": _score(self.fused_score),
            "reason": str(self.reason or ""),
        }


class StablePathRetriever:
    """只读稳定图，召回结构路径。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            entity_resolver: 实体解析器。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()

    def link_entities(self, query: str) -> list[str]:
        """从 query 中解析候选实体。

        Args:
            query: 查询字符串。

        Returns:
            归一化后的候选实体名列表。
        """
        # 简单策略：对 query 按常见分隔拆分并解析
        candidates: list[str] = []
        seen: set[str] = set()
        query = query if isinstance(query, str) else ""
        for part in query.replace("。", " ").replace("、", " ").split():
            try:
                resolved = self.entity_resolver.resolve(part)
            except (AttributeError, TypeError, ValueError):
                resolved = None
            name = _field(resolved, "canonical_name") or part
            if not name:
                continue
            norm = self._normalize(name)
            if norm in seen:
                continue
            seen.add(norm)
            candidates.append(norm)
        return candidates

    def retrieve(
        self,
        query: str,
        root_entity: str | None = None,
        axes: list[str] | None = None,
        max_depth: int = 5,
        max_paths: int = 50,
    ) -> list[dict[str, Any]]:
        """召回稳定图路径候选。

        Args:
            query: 查询字符串，用于实体链接。
            root_entity: 指定根实体，优先使用。
            axes: 结构轴过滤。
            max_depth: 最大路径深度。
            max_paths: 最大路径数。

        Returns:
            路径候选列表，每项包含 path / stable_edges / path_id / stable_path_score。
        """
        max_depth = _limit(max_depth, 5, 50)
        max_paths = _limit(max_paths, 50)
        roots = []
        if root_entity:
            try:
                resolved = self.entity_resolver.resolve(root_entity)
            except (AttributeError, TypeError, ValueError):
                resolved = None
            roots.append(_field(resolved, "canonical_name") or root_entity)
        roots.extend(self.link_entities(query))
        roots = list(dict.fromkeys(self._normalize(r) for r in roots if r and self._normalize(r)))
        if not roots:
            return []

        axis_filter = {axis for axis in axes if isinstance(axis, str)} if isinstance(axes, (list, tuple, set)) else set()
        results: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        for root in roots:
            paths = self._expand_paths(root, max_depth, max_paths, axis_filter)
            for path, edges in paths:
                path_key = "->".join(path)
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                score = self._score_path(edges, axis_filter)
                results.append({
                    "path": path,
                    "stable_edges": edges,
                    "path_id": path_key,
                    "stable_path_score": score,
                })

        results.sort(key=lambda x: _score(x.get("stable_path_score")), reverse=True)
        return results[:max_paths]

    def _expand_paths(
        self,
        root: str,
        max_depth: int,
        max_paths: int,
        axis_filter: set[str],
    ) -> list[tuple[list[str], list[Edge]]]:
        """从 root 出发 BFS 扩展路径。"""
        max_depth = _limit(max_depth, 5, 50)
        max_paths = _limit(max_paths, 50)
        results: list[tuple[list[str], list[Edge]]] = []
        if max_paths <= 0:
            return results
        sequence = count()
        pq: list[tuple[float, int, int, list[str], list[Edge]]] = [
            (0.0, 0, next(sequence), [root], [])
        ]

        while pq and len(results) < max_paths:
            _, depth, _, path, edges = heapq.heappop(pq)
            current = path[-1]
            if depth >= max_depth:
                results.append((path, edges))
                continue

            try:
                neighbors = self.kb.get_neighbors(current, direction="out")
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("无法读取节点邻居: %s", current, exc_info=True)
                neighbors = []
            expanded = False
            for edge in neighbors if isinstance(neighbors, (list, tuple)) else ():
                tail = _field(edge, "tail")
                if not isinstance(tail, str) or tail in path:
                    continue
                if axis_filter and not axis_filter.intersection(
                    self._edge_axes(edge) or set()
                ):
                    continue
                new_path = path + [tail]
                new_edges = edges + [edge]
                score = -self._score_path(new_edges, axis_filter)
                heapq.heappush(
                    pq,
                    (score, depth + 1, next(sequence), new_path, new_edges),
                )
                expanded = True

            if not expanded:
                results.append((path, edges))

        return results

    def _score_path(self, edges: list[Edge], axis_filter: set[str]) -> float:
        """根据边置信度和结构轴相关性评分。"""
        if not edges:
            return 0.0
        confidences = [_score(getattr(e, "confidence", 0.0)) for e in edges]
        hmean = harmonic_mean(confidences) if all(c > 0 for c in confidences) else 0.0
        axis_bonus = 0.0
        if axis_filter:
            matched = sum(
                1 for e in edges if axis_filter.intersection(self._edge_axes(e) or set())
            )
            axis_bonus = 0.1 * min(1.0, matched / max(1, len(edges)))
        return min(1.0, hmean + axis_bonus)

    def _edge_axes(self, edge: Edge) -> set[str]:
        """从边两端节点推断结构轴。"""
        axes: set[str] = set()
        for name in (_field(edge, "head", ""), _field(edge, "tail", "")):
            node = self.kb.nodes.get(name) if isinstance(getattr(self.kb, "nodes", None), Mapping) else None
            if node:
                axes.update(axis for axis in _field(node, "axes", []) or [] if isinstance(axis, str))
        return axes

    @staticmethod
    def _normalize(name: str) -> str:
        return str(name).strip().replace(" ", "").replace("　", "")


class EventSignalRetriever:
    """只读事件图，为稳定路径召回 event overlay。"""

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
        """
        self.kb = kb

    def retrieve_for_path(
        self,
        path: list[str],
        min_confidence: float = 0.0,
    ) -> tuple[list[EventEdge], list[EventClaim]]:
        """根据稳定路径上的节点召回相关事件边和事件 claim。

        Args:
            path: 稳定图路径节点列表。
            min_confidence: 事件边最低置信度。

        Returns:
            (event_edges, event_claims) 元组。
        """
        path_nodes = {node for node in path if isinstance(node, str)} if isinstance(path, (list, tuple)) else set()
        min_confidence = _score(min_confidence)
        event_edges: list[EventEdge] = []
        event_claim_ids: set[str] = set()

        event_edges_store = getattr(self.kb, "event_edges", {})
        for edge in event_edges_store.values() if isinstance(event_edges_store, Mapping) else ():
            if _score(_field(edge, "confidence", 0.0)) < min_confidence:
                continue
            if _field(edge, "subject", "") in path_nodes or _field(edge, "object", "") in path_nodes:
                event_edges.append(edge)
                event_claim_ids.update(
                    claim_id for claim_id in _string_list(_field(edge, "claim_ids", []))
                )

        event_claim_store = getattr(self.kb, "event_claims", {})
        event_claims = [
            event_claim_store[eid]
            for eid in event_claim_ids
            if isinstance(event_claim_store, Mapping) and eid in event_claim_store
        ]
        return event_edges, event_claims

    def retrieve_for_nodes(
        self,
        nodes: list[str],
        event_type: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[EventEdge]:
        """按节点召回事件边。"""
        node_set = {node for node in nodes if isinstance(node, str)} if isinstance(nodes, (list, tuple, set)) else set()
        min_confidence = _score(min_confidence)
        event_edges = getattr(self.kb, "event_edges", {})
        if not isinstance(event_edges, Mapping):
            return []
        return [
            edge
            for edge in event_edges.values()
            if _score(_field(edge, "confidence", 0.0)) >= min_confidence
            and (event_type is None or _field(edge, "event_type", "") == event_type)
            and (_field(edge, "subject", "") in node_set or _field(edge, "object", "") in node_set)
        ]


class EvidenceChunkRetriever:
    """根据稳定路径和事件 claim 召回原始证据 chunks。"""

    def __init__(self, kb: SAGKnowledgeBase) -> None:
        """初始化。"""
        self.kb = kb

    def retrieve(
        self,
        stable_edges: list[Edge],
        event_claims: list[EventClaim],
        query: str = "",
    ) -> list[dict[str, Any]]:
        """召回证据 chunks。

        优先级：
        1. 通过 stable edge 的 claim_ids 精确召回 stable claim evidence。
        2. 通过 event claim 的 source_id / evidence 召回。
        3. 可选：按 query 关键词做文本匹配补充（本实现用规则文本匹配，
           向量检索应在此接入作为补充，不能替代图路径召回）。

        Args:
            stable_edges: 稳定图边列表。
            event_claims: 事件 claim 列表。
            query: 查询字符串，用于文本匹配补充。

        Returns:
            证据 chunk 列表。
        """
        chunks: list[dict[str, Any]] = []
        seen: set[str] = set()
        query = query if isinstance(query, str) else ""

        # 1. Stable claim evidence
        claims = getattr(self.kb, "claims", {})
        for edge in stable_edges if isinstance(stable_edges, (list, tuple)) else ():
            for cid in _string_list(_field(edge, "claim_ids", [])):
                claim = claims.get(cid) if isinstance(claims, Mapping) else None
                evidence = _field(claim, "evidence", "")
                if not claim or not isinstance(evidence, str) or not evidence:
                    continue
                key = f"stable|{cid}"
                if key in seen:
                    continue
                seen.add(key)
                chunks.append({
                    "chunk_id": key,
                    "chunk_type": "stable_claim",
                    "source_type": _field(claim, "source_type", ""),
                    "source_title": _field(claim, "source_title", ""),
                    "evidence": evidence,
                    "claim_id": cid,
                    "edge_key": (_field(edge, "head", ""), _field(edge, "relation", ""), _field(edge, "tail", "")),
                    "retrieval_method": "claim_id_exact",
                })

        # 2. Event claim evidence
        for claim in event_claims if isinstance(event_claims, (list, tuple)) else ():
            if claim is None:
                continue
            event_id = _field(claim, "event_id", "")
            if not isinstance(event_id, str) or not event_id:
                continue
            key = f"event|{event_id}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append({
                "chunk_id": key,
                "chunk_type": "event_claim",
                "source_type": _field(claim, "source_type", ""),
                "source_title": _field(claim, "source_title", ""),
                "evidence": _field(claim, "evidence", ""),
                "event_id": event_id,
                "event_type": _field(claim, "event_type", ""),
                "retrieval_method": "event_claim_exact",
                "source_url": _field(claim, "source_url", ""),
            })

        # 3. 文本匹配补充（非向量，仅作为补充示例）
        if query:
            query_terms = [t for t in query.split() if len(t) >= 2]
            for claim in claims.values() if isinstance(claims, Mapping) else ():
                evidence = _field(claim, "evidence", "")
                if not isinstance(evidence, str) or not evidence:
                    continue
                claim_id = _field(claim, "claim_id", "")
                if not isinstance(claim_id, str) or not claim_id:
                    continue
                key = f"stable_text|{claim_id}"
                if key in seen:
                    continue
                if any(t in evidence for t in query_terms):
                    seen.add(key)
                    chunks.append({
                        "chunk_id": key,
                        "chunk_type": "stable_claim",
                        "source_type": _field(claim, "source_type", ""),
                        "source_title": _field(claim, "source_title", ""),
                        "evidence": evidence,
                        "claim_id": claim_id,
                        "retrieval_method": "text_match_fallback",
                    })

        return chunks


class RetrievalFusionRanker:
    """融合 stable path score、event signal score、evidence strength 等。"""

    def __init__(self) -> None:
        """初始化。"""
        pass

    def rank(
        self,
        bundles: list[PathEvidenceBundle],
    ) -> list[PathEvidenceBundle]:
        """对 bundle 排序。

        融合维度：
        - stable_path_score: 结构路径置信度
        - event_signal_score: 事件信号覆盖度
        - evidence_score: 证据 chunk 强度
        - freshness_score: 事件新鲜度
        - source_reliability_score: 来源可信度

        Args:
            bundles: 待排序 bundle 列表。

        Returns:
            按 fused_score 降序排列的 bundle 列表。
        """
        valid_bundles = [bundle for bundle in bundles if isinstance(bundle, PathEvidenceBundle)] if isinstance(bundles, (list, tuple)) else []
        for bundle in valid_bundles:
            bundle.evidence_score = self._evidence_score(bundle.evidence_chunks)
            bundle.event_signal_score = self._event_signal_score(
                bundle.event_edges, bundle.event_claims
            )
            bundle.freshness_score = self._freshness_score(bundle.event_claims)
            bundle.source_reliability_score = self._source_reliability_score(
                bundle.evidence_chunks, bundle.event_claims
            )
            stable_score = _score(bundle.stable_path_score)
            bundle.fused_score = (
                0.35 * stable_score
                + 0.25 * bundle.event_signal_score
                + 0.20 * bundle.evidence_score
                + 0.10 * bundle.freshness_score
                + 0.10 * bundle.source_reliability_score
            )
            bundle.reason = (
                f"stable={stable_score:.2f}, "
                f"event={bundle.event_signal_score:.2f}, "
                f"evidence={bundle.evidence_score:.2f}, "
                f"fresh={bundle.freshness_score:.2f}, "
                f"reliability={bundle.source_reliability_score:.2f}"
            )

        valid_bundles.sort(key=lambda b: _score(b.fused_score), reverse=True)
        return valid_bundles

    @staticmethod
    def _evidence_score(chunks: list[dict[str, Any]]) -> float:
        if not chunks:
            return 0.0
        if not isinstance(chunks, (list, tuple)):
            return 0.0
        valid_chunks = [c for c in chunks if isinstance(c, Mapping)]
        exact = sum(1 for c in valid_chunks if c.get("retrieval_method") == "claim_id_exact")
        return min(1.0, 0.3 + 0.1 * len(valid_chunks) + 0.2 * exact)

    @staticmethod
    def _event_signal_score(
        event_edges: list[EventEdge],
        event_claims: list[EventClaim],
    ) -> float:
        if not isinstance(event_edges, (list, tuple)):
            return 0.0
        confidences = [_score(_field(edge, "confidence", 0.0)) for edge in event_edges if edge is not None]
        if not confidences:
            return 0.0
        claim_count = len(event_claims) if isinstance(event_claims, (list, tuple)) else 0
        claim_bonus = min(0.3, 0.05 * claim_count)
        return min(1.0, sum(confidences) / len(confidences) + claim_bonus)

    @staticmethod
    def _freshness_score(event_claims: list[EventClaim]) -> float:
        if not isinstance(event_claims, (list, tuple)) or not event_claims:
            return 0.0
        # 简单策略：有 source_time 的事件越多，freshness 越高
        timed = sum(1 for c in event_claims if _field(c, "source_time", "") or _field(c, "time", ""))
        return min(1.0, 0.4 + 0.1 * timed)

    @staticmethod
    def _source_reliability_score(
        chunks: list[dict[str, Any]],
        event_claims: list[EventClaim],
    ) -> float:
        sources: list[str] = []
        if isinstance(chunks, (list, tuple)):
            sources.extend(c.get("source_type", "") for c in chunks if isinstance(c, Mapping))
        if isinstance(event_claims, (list, tuple)):
            sources.extend(_field(c, "source_type", "") for c in event_claims if c is not None)
        sources = [source for source in sources if isinstance(source, str)]
        if not sources:
            return 0.0
        avg_weight = sum(_score(get_source_weight(s)) for s in sources) / len(sources)
        return _score(avg_weight)


class DualRetriever:
    """GraphRAG 多路检索入口。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        entity_resolver: EntityResolver | None = None,
        stable_retriever: StablePathRetriever | None = None,
        event_retriever: EventSignalRetriever | None = None,
        chunk_retriever: EvidenceChunkRetriever | None = None,
        ranker: RetrievalFusionRanker | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库。
            entity_resolver: 实体解析器。
            stable_retriever: 稳定路径检索器。
            event_retriever: 事件信号检索器。
            chunk_retriever: 证据 chunk 检索器。
            ranker: 融合排序器。
        """
        self.kb = kb
        self.entity_resolver = entity_resolver or EntityResolver()
        self.stable_retriever = stable_retriever or StablePathRetriever(kb, self.entity_resolver)
        self.event_retriever = event_retriever or EventSignalRetriever(kb)
        self.chunk_retriever = chunk_retriever or EvidenceChunkRetriever(kb)
        self.ranker = ranker or RetrievalFusionRanker()

    def retrieve(
        self,
        query: str,
        root_entity: str | None = None,
        task_type: str = "alpha_discovery",
        axes: list[str] | None = None,
        max_depth: int = 5,
        max_paths: int = 20,
        min_confidence: float = 0.0,
        top_k: int = 10,
    ) -> list[PathEvidenceBundle]:
        """执行 GraphRAG 多路检索。

        Args:
            query: 查询字符串。
            root_entity: 根实体。
            task_type: 任务类型（仅作标记）。
            axes: 结构轴过滤。
            max_depth: 最大路径深度。
            max_paths: 最大路径候选数。
            min_confidence: 事件信号最低置信度。
            top_k: 返回 bundle 数量。

        Returns:
            排序后的 PathEvidenceBundle 列表。
        """
        # 1-4. 稳定图路径召回
        query = query if isinstance(query, str) else ""
        max_depth = _limit(max_depth, 5, 50)
        max_paths = _limit(max_paths, 20)
        top_k = _limit(top_k, 10)
        min_confidence = _score(min_confidence)
        path_candidates = self.stable_retriever.retrieve(
            query=query,
            root_entity=root_entity,
            axes=axes,
            max_depth=max_depth,
            max_paths=max_paths,
        )

        bundles: list[PathEvidenceBundle] = []
        for candidate in path_candidates if isinstance(path_candidates, (list, tuple)) else ():
            if not isinstance(candidate, Mapping):
                continue
            path = candidate.get("path", [])
            stable_edges = candidate.get("stable_edges", [])
            path_id = candidate.get("path_id", "")
            stable_score = candidate.get("stable_path_score", 0.0)
            if not isinstance(path, (list, tuple)):
                path = []

            # 5. 事件信号覆盖
            event_edges, event_claims = self.event_retriever.retrieve_for_path(
                path, min_confidence=min_confidence
            )

            # 6. 证据 chunk 绑定
            evidence_chunks = self.chunk_retriever.retrieve(
                stable_edges=stable_edges,
                event_claims=event_claims,
                query=query,
            )

            bundle = PathEvidenceBundle(
                path=path,
                path_id=path_id,
                stable_edges=stable_edges,
                event_edges=event_edges,
                event_claims=event_claims,
                evidence_chunks=evidence_chunks,
                stable_path_score=stable_score,
            )
            bundles.append(bundle)

        # 7. 融合排序
        ranked = self.ranker.rank(bundles)
        return ranked[:top_k]
