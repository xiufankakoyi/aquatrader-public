from __future__ import annotations

from collections import deque
from typing import Any

from core.research_graph.schema import AUDIT_THEME_RELATION_TYPES, EdgeStatus, NodeType
from core.research_graph.store import GraphStore


class GraphQuery:
    def __init__(self, store: GraphStore, candidate_store: Any | None = None) -> None:
        self.store = store
        self.candidate_store = candidate_store

    def find_paths(
        self,
        start_node_id: str,
        end_node_id: str,
        *,
        max_depth: int = 5,
        include_audit_edges: bool = False,
        dimension: str = "",
        route_layer: str = "",
        time_stage: str = "",
    ) -> list[dict[str, Any]]:
        """Find paths.

        Default mode is verified output: every traversed edge must be ACTIVE
        and carry evidence_ids. Set include_audit_edges=True only for
        debug/audit views that intentionally display candidate, seed, or
        conflicted relations.
        """

        if start_node_id not in self.store.nodes or end_node_id not in self.store.nodes:
            return []
        queue = deque([(start_node_id, [], {start_node_id})])
        paths: list[dict[str, Any]] = []
        while queue:
            current, edge_ids, seen = queue.popleft()
            if len(edge_ids) >= max_depth:
                continue
            for edge_id, next_node in self._neighbors(
                current,
                include_audit_edges=include_audit_edges,
                dimension=dimension,
                route_layer=route_layer,
                time_stage=time_stage,
            ):
                if next_node in seen:
                    continue
                next_edges = edge_ids + [edge_id]
                if next_node == end_node_id:
                    paths.append(self._render_path(start_node_id, end_node_id, next_edges))
                else:
                    queue.append((next_node, next_edges, seen | {next_node}))
        return paths

    def benefit_paths(
        self,
        theme_id: str,
        company_id: str,
        *,
        include_audit_edges: bool = False,
        dimension: str = "",
        route_layer: str = "",
        time_stage: str = "",
    ) -> list[dict[str, Any]]:
        return self.find_paths(
            theme_id,
            company_id,
            max_depth=6,
            include_audit_edges=include_audit_edges,
            dimension=dimension,
            route_layer=route_layer,
            time_stage=time_stage,
        )

    def related_targets(
        self,
        theme_id: str,
        *,
        include_audit_edges: bool = False,
        include_candidates: bool = False,
        dimension: str = "",
        route_layer: str = "",
        time_stage: str = "",
    ) -> dict[str, Any]:
        """Return paths only; never emit a naked stock/company list.

        Args:
            include_candidates: True 时在 exploration_view 中包含 CandidateGraph 的
                DISCOVERED/CANDIDATE/SUPPORTED 节点和边;默认 False(research_view 仅 ACTIVE)。
        """

        company_ids = [
            node_id
            for node_id, node in self.store.nodes.items()
            if node.type in {NodeType.COMPANY.value, NodeType.SUBSIDIARY.value}
        ]
        paths: list[dict[str, Any]] = []
        for company_id in company_ids:
            paths.extend(
                self.benefit_paths(
                    theme_id,
                    company_id,
                    include_audit_edges=include_audit_edges,
                    dimension=dimension,
                    route_layer=route_layer,
                    time_stage=time_stage,
                )
            )

        candidate_paths: list[dict[str, Any]] = []
        if include_candidates and self.candidate_store is not None:
            candidate_paths = self._candidate_paths_from(theme_id)

        return {
            "query": theme_id,
            "result_type": "audit_benefit_paths" if include_audit_edges else "benefit_paths",
            "mode": "exploration" if include_candidates else ("audit" if include_audit_edges else "verified"),
            "dimension": dimension,
            "route_layer": route_layer,
            "time_stage": time_stage,
            "paths": paths,
            "candidate_paths": candidate_paths if include_candidates else [],
        }

    def dimension_projection(
        self,
        dimension: str,
        *,
        include_audit_edges: bool = False,
    ) -> dict[str, Any]:
        edges = [
            edge
            for edge_id, edge in self.store.edges.items()
            if self._edge_visible(edge_id, include_audit_edges=include_audit_edges)
            and self._edge_matches_filters(edge, dimension=dimension)
        ]
        node_ids = sorted(
            {
                node_id
                for edge in edges
                for node_id in [*edge.subjects, *edge.objects]
                if node_id in self.store.nodes
            }
        )
        return {
            "dimension": dimension,
            "mode": "audit" if include_audit_edges else "verified",
            "nodes": [self.store.nodes[node_id].to_dict() for node_id in node_ids],
            "edges": [edge.to_dict() for edge in sorted(edges, key=lambda item: item.edge_id)],
        }

    def _neighbors(
        self,
        node_id: str,
        *,
        include_audit_edges: bool,
        dimension: str = "",
        route_layer: str = "",
        time_stage: str = "",
    ) -> list[tuple[str, str]]:
        neighbors: list[tuple[str, str]] = []
        for edge_id, edge in self.store.edges.items():
            if not self._edge_visible(edge_id, include_audit_edges=include_audit_edges):
                continue
            if not self._edge_matches_filters(
                edge,
                dimension=dimension,
                route_layer=route_layer,
                time_stage=time_stage,
            ):
                continue
            for subject in edge.subjects:
                if subject == node_id:
                    neighbors.extend((edge_id, obj) for obj in edge.objects)
            for obj in edge.objects:
                if obj == node_id:
                    neighbors.extend((edge_id, subject) for subject in edge.subjects)
        return neighbors

    def _edge_visible(self, edge_id: str, *, include_audit_edges: bool) -> bool:
        edge = self.store.edges[edge_id]
        if include_audit_edges:
            return True
        return (
            edge.status == EdgeStatus.ACTIVE.value
            and bool(edge.evidence_ids)
            and edge.relation not in AUDIT_THEME_RELATION_TYPES
        )

    def _edge_matches_filters(
        self,
        edge: Any,
        *,
        dimension: str = "",
        route_layer: str = "",
        time_stage: str = "",
    ) -> bool:
        if dimension:
            edge_dimensions = set(getattr(edge, "dimensions", []) or [])
            primary = getattr(edge, "primary_dimension", "")
            qualifier_dimension = (getattr(edge, "qualifiers", {}) or {}).get("dimension", "")
            if dimension not in edge_dimensions and dimension not in {primary, qualifier_dimension}:
                return False
        if route_layer and route_layer != getattr(edge, "route_layer", ""):
            return False
        if time_stage and time_stage != getattr(edge, "time_stage", ""):
            return False
        return True

    def _render_path(self, start_node_id: str, end_node_id: str, edge_ids: list[str]) -> dict[str, Any]:
        node_ids = [start_node_id]
        current = start_node_id
        edge_payloads: list[dict[str, Any]] = []
        for edge_id in edge_ids:
            edge = self.store.edges[edge_id]
            candidates = list(edge.subjects) + list(edge.objects)
            next_nodes = [item for item in candidates if item != current]
            next_node = next_nodes[0] if next_nodes else end_node_id
            node_ids.append(next_node)
            current = next_node
            edge_payloads.append({
                "edge_id": edge.edge_id,
                "relation": edge.relation,
                "qualifiers": dict(edge.qualifiers),
                "dimensions": list(edge.dimensions),
                "primary_dimension": edge.primary_dimension,
                "route_layer": edge.route_layer,
                "time_stage": edge.time_stage,
                "confidence": edge.confidence,
                "status": edge.status,
                "evidence_ids": list(edge.evidence_ids),
                "counter_evidence_ids": list(edge.counter_evidence_ids),
                "updated_at": edge.updated_at,
                "mutation_history": self.store.edge_mutation_history(edge.edge_id),
            })
        return {
            "node_ids": node_ids,
            "nodes": [self.store.nodes[node_id].to_dict() for node_id in node_ids],
            "edges": edge_payloads,
        }

    def exploration_projection(
        self,
        *,
        view: str = "exploration",
        node_type: str = "",
    ) -> dict[str, Any]:
        """返回 CandidateGraph 的投影视图。

        Args:
            view: "exploration"(含 DISCOVERED/CANDIDATE/SUPPORTED) 或 "research"(仅 ACTIVE)。
            node_type: 可选按节点类型过滤。

        Returns:
            {"nodes": [...], "edges": [...], "mode": view}
        """
        if self.candidate_store is None:
            return {"nodes": [], "edges": [], "mode": view}
        cand_nodes = self.candidate_store.list_candidates(view=view, node_type=node_type)
        visible_ids = {n.candidate_id for n in cand_nodes}
        cand_edges = self.candidate_store.list_edges(view=view)
        # 只返回两端节点都可见的边
        visible_edges = [
            e for e in cand_edges
            if e.subject_id in visible_ids and e.object_id in visible_ids
        ]
        return {
            "nodes": [
                {
                    "candidate_id": n.candidate_id,
                    "name": n.name,
                    "node_type": n.node_type,
                    "status": n.status,
                    "confidence": n.confidence,
                    "activation_score": n.activation_score,
                    "evidence_count": len(n.evidence_slots),
                    "first_discovered_by": n.first_discovered_by,
                    "last_seen_at": n.last_seen_at,
                    "aliases": list(n.aliases),
                }
                for n in cand_nodes
            ],
            "edges": [
                {
                    "candidate_id": e.candidate_id,
                    "subject_id": e.subject_id,
                    "object_id": e.object_id,
                    "relation": e.relation,
                    "status": e.status,
                    "confidence": e.confidence,
                    "activation_score": e.activation_score,
                    "evidence_count": len(e.evidence_slots),
                }
                for e in visible_edges
            ],
            "mode": view,
        }

    def _candidate_paths_from(self, seed_node_id: str) -> list[dict[str, Any]]:
        """从 seed 节点出发,在 CandidateGraph 中找 1 跳候选路径。

        用于 exploration_view 展示候选扩展方向。仅返回 1 跳路径,
        避免候选图无限扩散。
        """
        if self.candidate_store is None:
            return []
        paths: list[dict[str, Any]] = []
        neighbors = self.candidate_store.neighbors(seed_node_id, view="exploration")
        for edge, other_node in neighbors:
            if other_node is None:
                continue
            path = {
                "node_ids": [edge.subject_id, edge.object_id],
                "nodes": [
                    {
                        "candidate_id": other_node.candidate_id,
                        "name": other_node.name,
                        "node_type": other_node.node_type,
                        "status": other_node.status,
                    }
                ],
                "edges": [
                    {
                        "candidate_id": edge.candidate_id,
                        "relation": edge.relation,
                        "status": edge.status,
                        "confidence": edge.confidence,
                        "is_candidate": True,
                    }
                ],
                "is_candidate_path": True,
            }
            # 如果 subject 是 seed,把 object 作为 next_node;反之亦然
            seed_node = self.store.nodes.get(seed_node_id)
            if seed_node is not None:
                path["nodes"] = [
                    {"node_id": seed_node.node_id, "name": seed_node.name, "type": seed_node.type},
                    {"candidate_id": other_node.candidate_id, "name": other_node.name, "node_type": other_node.node_type, "status": other_node.status},
                ]
            paths.append(path)
        return paths
