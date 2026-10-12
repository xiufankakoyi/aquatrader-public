from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.research_graph.models import Evidence, HyperEdge, HyperNode, utc_now
from core.research_graph.mutation_log import MutationLogger
from core.research_graph.schema import EdgeStatus, MutationAction, NodeSource
from core.research_graph.verifier import GraphVerifier, VerificationResult


class GraphStore:
    def __init__(self, verifier: GraphVerifier | None = None) -> None:
        self.nodes: dict[str, HyperNode] = {}
        self.edges: dict[str, HyperEdge] = {}
        self.evidence: dict[str, Evidence] = {}
        self.mutations = MutationLogger()
        self.verifier = verifier or GraphVerifier()

    def add_node(
        self,
        node: HyperNode,
        operator: str = "human",
        *,
        initial_status: str | None = None,
    ) -> HyperNode:
        before = self.nodes.get(node.node_id).to_dict() if node.node_id in self.nodes else {}
        if initial_status is not None:
            node.status = initial_status
        elif node.source == NodeSource.SEED_GRAPH.value:
            node.status = EdgeStatus.UNVERIFIED_SEED.value
        elif node.source == NodeSource.MANUAL.value:
            node.status = EdgeStatus.ACTIVE.value
        else:
            # Evidence/LLM/未知来源都必须从候选态开始；未知 source 不能沿用
            # HyperNode 的 ACTIVE 默认值绕过研究状态门禁。
            node.status = EdgeStatus.CANDIDATE.value
        self.nodes[node.node_id] = node
        self.mutations.record(
            MutationAction.ADD_NODE.value,
            node.node_id,
            before,
            node.to_dict(),
            "新增或更新超图节点",
            operator=operator,
        )
        return node

    def add_edge(self, edge: HyperEdge, operator: str = "human") -> tuple[HyperEdge, VerificationResult]:
        before = self.edges.get(edge.edge_id).to_dict() if edge.edge_id in self.edges else {}
        result = self.verifier.verify_edge(edge, self.nodes, self.evidence)
        edge.status = result.status
        edge.confidence = result.confidence
        self.edges[edge.edge_id] = edge
        self.mutations.record(
            MutationAction.ADD_EDGE.value,
            edge.edge_id,
            before,
            edge.to_dict(),
            "；".join(result.messages) or "新增超图关系",
            operator=operator,
        )
        return edge, result

    def add_evidence(self, evidence: Evidence, operator: str = "human") -> Evidence:
        before = self.evidence.get(evidence.evidence_id).to_dict() if evidence.evidence_id in self.evidence else {}
        self.evidence[evidence.evidence_id] = evidence
        self.mutations.record(
            MutationAction.ADD_EVIDENCE.value,
            evidence.evidence_id,
            before,
            evidence.to_dict(),
            "新增证据账本记录",
            operator=operator,
        )
        return evidence

    def attach_evidence(
        self,
        edge_id: str,
        evidence_id: str,
        *,
        as_counter: bool = False,
        operator: str = "rule_verifier",
    ) -> tuple[HyperEdge, VerificationResult]:
        edge = self.edges[edge_id]
        before = edge.to_dict()
        if as_counter:
            if evidence_id not in edge.counter_evidence_ids:
                edge.counter_evidence_ids.append(evidence_id)
        elif evidence_id not in edge.evidence_ids:
            edge.evidence_ids.append(evidence_id)
        result = self.reevaluate_edge(edge_id, operator=operator, before=before)
        return self.edges[edge_id], result

    def reevaluate_edge(
        self,
        edge_id: str,
        *,
        operator: str = "rule_verifier",
        before: dict[str, Any] | None = None,
    ) -> VerificationResult:
        edge = self.edges[edge_id]
        before_payload = before or edge.to_dict()
        result = self.verifier.verify_edge(edge, self.nodes, self.evidence)
        old_confidence = edge.confidence
        old_status = edge.status
        edge.status = result.status
        edge.confidence = result.confidence
        edge.updated_at = utc_now()
        action = MutationAction.UPDATE_EDGE.value
        if result.status == EdgeStatus.CONFLICTED.value:
            action = MutationAction.MARK_CONFLICT.value
        elif result.status == EdgeStatus.DISPROVEN.value or result.confidence == "DISPROVEN":
            action = MutationAction.DOWNGRADE_CONFIDENCE.value
        self.mutations.record(
            action,
            edge_id,
            before_payload,
            edge.to_dict(),
            "；".join(result.messages) or "验证器重新评估关系",
            operator=operator,
        )
        return result

    def delete_edge(self, edge_id: str, reason: str, operator: str = "human") -> None:
        edge = self.edges[edge_id]
        before = edge.to_dict()
        edge.status = EdgeStatus.DELETED.value
        edge.updated_at = utc_now()
        self.mutations.record(
            MutationAction.DELETE_EDGE.value,
            edge_id,
            before,
            edge.to_dict(),
            reason,
            operator=operator,
        )

    def edge_mutation_history(self, edge_id: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.mutations.for_target(edge_id)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {key: value.to_dict() for key, value in self.nodes.items()},
            "edges": {key: value.to_dict() for key, value in self.edges.items()},
            "evidence": {key: value.to_dict() for key, value in self.evidence.items()},
            "mutations": self.mutations.to_list(),
        }

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "GraphStore":
        payload = json.loads(path.read_text(encoding="utf-8"))
        store = cls()
        store.nodes = {key: HyperNode.from_dict(value) for key, value in payload.get("nodes", {}).items()}
        store.edges = {key: HyperEdge.from_dict(value) for key, value in payload.get("edges", {}).items()}
        store.evidence = {key: Evidence.from_dict(value) for key, value in payload.get("evidence", {}).items()}
        store.mutations = MutationLogger.from_list(payload.get("mutations", []))
        return store
