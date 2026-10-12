from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.research_graph.schema import (
    AUDIT_THEME_RELATION_TYPES,
    COMPANY_NODE_TYPES,
    EdgeStatus,
    NodeType,
)
from core.research_graph.store import GraphStore


@dataclass
class GraphAuditSnapshot:
    node_count: int
    edge_count: int
    evidence_count: int
    active_edge_count: int
    candidate_edge_count: int
    unverified_seed_edge_count: int
    conflicted_edge_count: int
    disproven_edge_count: int
    deleted_edge_count: int
    nodes_marked_too_coarse: list[str]
    edges_without_evidence: list[str]
    candidate_edges_with_evidence: list[str]
    active_edges_with_counter_evidence: list[str]
    dangling_edges: list[str]
    coarse_company_theme_edges: list[str]
    audit_only_edges: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_audit_snapshot(store: GraphStore) -> GraphAuditSnapshot:
    edges = store.edges
    return GraphAuditSnapshot(
        node_count=len(store.nodes),
        edge_count=len(edges),
        evidence_count=len(store.evidence),
        active_edge_count=_count_edges(edges, EdgeStatus.ACTIVE.value),
        candidate_edge_count=_count_edges(edges, EdgeStatus.CANDIDATE.value),
        unverified_seed_edge_count=_count_edges(edges, EdgeStatus.UNVERIFIED_SEED.value),
        conflicted_edge_count=_count_edges(edges, EdgeStatus.CONFLICTED.value),
        disproven_edge_count=_count_edges(edges, EdgeStatus.DISPROVEN.value),
        deleted_edge_count=_count_edges(edges, EdgeStatus.DELETED.value),
        nodes_marked_too_coarse=sorted(
            node_id
            for node_id, node in store.nodes.items()
            if node.attributes.get("too_coarse")
        ),
        edges_without_evidence=sorted(
            edge_id
            for edge_id, edge in edges.items()
            if edge.status != EdgeStatus.DELETED.value and not edge.evidence_ids
        ),
        candidate_edges_with_evidence=sorted(
            edge_id
            for edge_id, edge in edges.items()
            if edge.status == EdgeStatus.CANDIDATE.value and edge.evidence_ids
        ),
        active_edges_with_counter_evidence=sorted(
            edge_id
            for edge_id, edge in edges.items()
            if edge.status == EdgeStatus.ACTIVE.value and edge.counter_evidence_ids
        ),
        dangling_edges=sorted(
            edge_id
            for edge_id, edge in edges.items()
            if edge.status != EdgeStatus.DELETED.value
            and _edge_has_missing_or_deleted_node(store, edge_id)
        ),
        coarse_company_theme_edges=sorted(
            edge_id
            for edge_id in edges
            if _is_coarse_company_theme_edge(store, edge_id)
        ),
        audit_only_edges=sorted(
            edge_id
            for edge_id, edge in edges.items()
            if edge.relation in AUDIT_THEME_RELATION_TYPES
            or edge.status
            in {
                EdgeStatus.CANDIDATE.value,
                EdgeStatus.UNVERIFIED_SEED.value,
                EdgeStatus.CONFLICTED.value,
            }
        ),
    )


def _count_edges(edges: dict[str, Any], status: str) -> int:
    return sum(1 for edge in edges.values() if edge.status == status)


def _edge_has_missing_or_deleted_node(store: GraphStore, edge_id: str) -> bool:
    edge = store.edges[edge_id]
    for node_id in [*edge.subjects, *edge.objects]:
        node = store.nodes.get(node_id)
        if node is None or node.status == EdgeStatus.DELETED.value:
            return True
    return False


def _is_coarse_company_theme_edge(store: GraphStore, edge_id: str) -> bool:
    edge = store.edges[edge_id]
    if edge.relation in AUDIT_THEME_RELATION_TYPES:
        return False
    node_types = {
        store.nodes[node_id].type
        for node_id in [*edge.subjects, *edge.objects]
        if node_id in store.nodes
    }
    return bool(node_types & COMPANY_NODE_TYPES) and NodeType.THEME.value in node_types
