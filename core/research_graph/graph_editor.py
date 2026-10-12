from __future__ import annotations

from typing import Any

from core.research_graph.models import HyperNode, utc_now
from core.research_graph.schema import EdgeStatus, MutationAction, NodeSource
from core.research_graph.store import GraphStore


class GraphEditor:
    """Manual seed graph editing primitives with mutation logging."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def merge_node(
        self,
        source_node_id: str,
        target_node_id: str,
        *,
        reason: str,
        operator: str = "human",
    ) -> None:
        self.merge_nodes([source_node_id], target_node_id, reason=reason, operator=operator)

    def merge_nodes(
        self,
        source_node_ids: list[str],
        target_node_id: str,
        *,
        reason: str,
        operator: str = "human",
    ) -> None:
        self._require_nodes([*source_node_ids, target_node_id])
        target = self.store.nodes[target_node_id]
        before = {
            "sources": {
                node_id: self.store.nodes[node_id].to_dict()
                for node_id in source_node_ids
            },
            "target": target.to_dict(),
            "edges": {
                node_id: self._incident_edges(node_id)
                for node_id in source_node_ids
            },
        }
        for source_node_id in source_node_ids:
            source = self.store.nodes[source_node_id]
            self._replace_node_in_edges(source_node_id, target_node_id)
            source.status = EdgeStatus.DELETED.value
            source.attributes["merged_into"] = target_node_id
            source.updated_at = utc_now()
            target.aliases = sorted(set(target.aliases + [source.name, *source.aliases]))
        target.updated_at = utc_now()
        self.store.mutations.record(
            MutationAction.MERGE_NODE.value,
            target_node_id,
            before,
            {
                "sources": {
                    node_id: self.store.nodes[node_id].to_dict()
                    for node_id in source_node_ids
                },
                "target": target.to_dict(),
                "edges": self._incident_edges(target_node_id),
            },
            reason,
            operator=operator,
        )

    def split_node(
        self,
        source_node_id: str,
        new_nodes: list[HyperNode],
        *,
        reason: str,
        operator: str = "human",
    ) -> list[HyperNode]:
        self._require_nodes([source_node_id])
        source = self.store.nodes[source_node_id]
        before = source.to_dict()
        source.attributes["split_into"] = [node.node_id for node in new_nodes]
        source.attributes["too_coarse"] = True
        source.attributes["split_reason"] = reason
        source.status = EdgeStatus.CANDIDATE.value
        source.updated_at = utc_now()
        added: list[HyperNode] = []
        for node in new_nodes:
            initial_status = (
                EdgeStatus.UNVERIFIED_SEED.value
                if node.source == NodeSource.SEED_GRAPH.value
                else EdgeStatus.CANDIDATE.value
            )
            added.append(
                self.store.add_node(
                    node,
                    operator=operator,
                    initial_status=initial_status,
                )
            )
        self.store.mutations.record(
            MutationAction.SPLIT_NODE.value,
            source_node_id,
            before,
            {
                "source": source.to_dict(),
                "new_nodes": [node.to_dict() for node in added],
            },
            reason,
            operator=operator,
        )
        return added

    def delete_node(
        self,
        node_id: str,
        *,
        reason: str,
        operator: str = "human",
    ) -> None:
        self._require_nodes([node_id])
        node = self.store.nodes[node_id]
        before = {
            "node": node.to_dict(),
            "edges": self._incident_edges(node_id),
        }
        node.status = EdgeStatus.DELETED.value
        node.updated_at = utc_now()
        for edge in self.store.edges.values():
            if node_id in edge.subjects or node_id in edge.objects:
                edge.status = EdgeStatus.DELETED.value
                edge.qualifiers["broken_by_delete_node"] = node_id
                edge.qualifiers["broken_reason"] = reason
                edge.updated_at = utc_now()
        self.store.mutations.record(
            MutationAction.DELETE_NODE.value,
            node_id,
            before,
            {
                "node": node.to_dict(),
                "edges": self._incident_edges(node_id),
            },
            reason,
            operator=operator,
        )

    def rename_node(
        self,
        node_id: str,
        new_name: str,
        *,
        reason: str,
        operator: str = "human",
    ) -> None:
        self._require_nodes([node_id])
        node = self.store.nodes[node_id]
        before = node.to_dict()
        if node.name not in node.aliases:
            node.aliases.append(node.name)
        node.name = new_name
        node.updated_at = utc_now()
        self.store.mutations.record(
            MutationAction.RENAME_NODE.value,
            node_id,
            before,
            node.to_dict(),
            reason,
            operator=operator,
        )

    def rewire_edge(
        self,
        edge_id: str,
        *,
        subjects: list[str] | None = None,
        objects: list[str] | None = None,
        reason: str,
        operator: str = "human",
    ) -> None:
        self._require_edges([edge_id])
        if subjects is not None:
            self._require_nodes(subjects)
        if objects is not None:
            self._require_nodes(objects)
        edge = self.store.edges[edge_id]
        before = edge.to_dict()
        if subjects is not None:
            edge.subjects = list(subjects)
        if objects is not None:
            edge.objects = list(objects)
        edge.updated_at = utc_now()
        result = self.store.verifier.verify_edge(edge, self.store.nodes, self.store.evidence)
        edge.status = result.status
        edge.confidence = result.confidence
        self.store.mutations.record(
            MutationAction.REWIRE_EDGE.value,
            edge_id,
            before,
            {"edge": edge.to_dict(), "verification_messages": result.messages},
            reason,
            operator=operator,
        )

    def mark_node_too_coarse(
        self,
        node_id: str,
        *,
        reason: str,
        suggested_action: str = "Split into PROCESS / MATERIAL / EQUIPMENT / PRODUCT nodes",
        operator: str = "human",
    ) -> None:
        self._require_nodes([node_id])
        node = self.store.nodes[node_id]
        before = node.to_dict()
        node.attributes["too_coarse"] = True
        node.attributes["too_coarse_reason"] = reason
        node.attributes["suggested_action"] = suggested_action
        node.updated_at = utc_now()
        self.store.mutations.record(
            MutationAction.MARK_NODE_TOO_COARSE.value,
            node_id,
            before,
            node.to_dict(),
            reason,
            operator=operator,
        )

    def _require_nodes(self, node_ids: list[str]) -> None:
        missing = [node_id for node_id in node_ids if node_id not in self.store.nodes]
        if missing:
            raise ValueError(f"missing_nodes: {', '.join(missing)}")

    def _require_edges(self, edge_ids: list[str]) -> None:
        missing = [edge_id for edge_id in edge_ids if edge_id not in self.store.edges]
        if missing:
            raise ValueError(f"missing_edges: {', '.join(missing)}")

    def _replace_node_in_edges(self, source_node_id: str, target_node_id: str) -> None:
        for edge in self.store.edges.values():
            if source_node_id in edge.subjects:
                edge.subjects = [target_node_id if item == source_node_id else item for item in edge.subjects]
                edge.updated_at = utc_now()
            if source_node_id in edge.objects:
                edge.objects = [target_node_id if item == source_node_id else item for item in edge.objects]
                edge.updated_at = utc_now()

    def _incident_edges(self, node_id: str) -> dict[str, dict[str, Any]]:
        return {
            edge_id: edge.to_dict()
            for edge_id, edge in self.store.edges.items()
            if node_id in edge.subjects or node_id in edge.objects
        }
