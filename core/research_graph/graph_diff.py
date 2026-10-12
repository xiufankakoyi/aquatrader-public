from __future__ import annotations

from typing import Any

from core.research_graph.store import GraphStore


def diff_stores(before: GraphStore, after: GraphStore) -> dict[str, Any]:
    before_nodes = set(before.nodes)
    after_nodes = set(after.nodes)
    before_edges = set(before.edges)
    after_edges = set(after.edges)
    common_nodes = before_nodes & after_nodes
    common_edges = before_edges & after_edges
    changed_edges = sorted(
        edge_id
        for edge_id in common_edges
        if before.edges[edge_id].to_dict() != after.edges[edge_id].to_dict()
    )
    renamed_nodes = {
        node_id: {
            "before": before.nodes[node_id].name,
            "after": after.nodes[node_id].name,
        }
        for node_id in common_nodes
        if before.nodes[node_id].name != after.nodes[node_id].name
    }
    removed_or_deleted_nodes = sorted(
        (before_nodes - after_nodes)
        | {
            node_id
            for node_id in common_nodes
            if before.nodes[node_id].status != "DELETED"
            and after.nodes[node_id].status == "DELETED"
        }
    )
    removed_or_deleted_edges = sorted(
        (before_edges - after_edges)
        | {
            edge_id
            for edge_id in common_edges
            if before.edges[edge_id].status != "DELETED"
            and after.edges[edge_id].status == "DELETED"
        }
    )
    split_nodes = {
        node_id: list(after.nodes[node_id].attributes.get("split_into") or [])
        for node_id in common_nodes
        if after.nodes[node_id].attributes.get("split_into")
    }
    confidence_changes = {
        edge_id: {
            "before": before.edges[edge_id].confidence,
            "after": after.edges[edge_id].confidence,
        }
        for edge_id in common_edges
        if before.edges[edge_id].confidence != after.edges[edge_id].confidence
    }
    evidence_changes = {
        edge_id: {
            "evidence_ids": {
                "before": list(before.edges[edge_id].evidence_ids),
                "after": list(after.edges[edge_id].evidence_ids),
            },
            "counter_evidence_ids": {
                "before": list(before.edges[edge_id].counter_evidence_ids),
                "after": list(after.edges[edge_id].counter_evidence_ids),
            },
        }
        for edge_id in common_edges
        if before.edges[edge_id].evidence_ids != after.edges[edge_id].evidence_ids
        or before.edges[edge_id].counter_evidence_ids != after.edges[edge_id].counter_evidence_ids
    }
    conflicted_changes = {
        edge_id: {
            "before": before.edges[edge_id].status,
            "after": after.edges[edge_id].status,
        }
        for edge_id in common_edges
        if before.edges[edge_id].status != "CONFLICTED"
        and after.edges[edge_id].status == "CONFLICTED"
    }
    return {
        "added_nodes": sorted(after_nodes - before_nodes),
        "removed_nodes": sorted(before_nodes - after_nodes),
        "removed_or_deleted_nodes": removed_or_deleted_nodes,
        "deleted_nodes": sorted(
            node_id
            for node_id in common_nodes
            if before.nodes[node_id].status != "DELETED"
            and after.nodes[node_id].status == "DELETED"
        ),
        "merged_nodes": sorted(
            node_id
            for node_id in common_nodes
            if after.nodes[node_id].attributes.get("merged_into")
        ),
        "split_nodes": split_nodes,
        "renamed_nodes": renamed_nodes,
        "added_edges": sorted(after_edges - before_edges),
        "removed_edges": sorted(before_edges - after_edges),
        "removed_or_deleted_edges": removed_or_deleted_edges,
        "changed_edges": changed_edges,
        "modified_edges": changed_edges,
        "rewired_edges": sorted(
            edge_id
            for edge_id in common_edges
            if before.edges[edge_id].subjects != after.edges[edge_id].subjects
            or before.edges[edge_id].objects != after.edges[edge_id].objects
        ),
        "status_changes": {
            "nodes": {
                node_id: {
                    "before": before.nodes[node_id].status,
                    "after": after.nodes[node_id].status,
                }
                for node_id in sorted(common_nodes)
                if before.nodes[node_id].status != after.nodes[node_id].status
            },
            "edges": {
                edge_id: {
                    "before": before.edges[edge_id].status,
                    "after": after.edges[edge_id].status,
                }
                for edge_id in sorted(common_edges)
                if before.edges[edge_id].status != after.edges[edge_id].status
            },
        },
        "confidence_changes": confidence_changes,
        "evidence_changes": evidence_changes,
        "conflicted_changes": conflicted_changes,
    }
