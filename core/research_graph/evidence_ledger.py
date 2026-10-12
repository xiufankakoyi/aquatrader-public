from __future__ import annotations

from core.research_graph.models import Evidence
from core.research_graph.schema import CAUSAL_STATUS_VALUES, EvidenceLevel
from core.research_graph.store import GraphStore


def validate_evidence_for_ledger(evidence: Evidence) -> None:
    """Validate canonical evidence before it enters GraphStore.

    Raises:
        ValueError: if the evidence is not traceable enough for the ledger.
    """
    if not evidence.evidence_id:
        raise ValueError("evidence_id_required")
    if not evidence.quote:
        raise ValueError("quote_required")
    if not (evidence.source_id or evidence.document_id):
        raise ValueError("source_id_or_document_id_required")
    if evidence.evidence_level not in {item.value for item in EvidenceLevel}:
        raise ValueError(f"invalid_evidence_level: {evidence.evidence_level}")
    if not evidence.source_type:
        raise ValueError("source_type_required")
    # Step 1:校验 causal_status 合法性。空字符串视为未标注(向后兼容)。
    if evidence.causal_status and evidence.causal_status not in CAUSAL_STATUS_VALUES:
        raise ValueError(f"invalid_causal_status: {evidence.causal_status}")
    # 政策事件类证据必须有 event_date,便于 Verifier 区分时序归因。
    if evidence.source_type == "policy_release" and not evidence.event_date:
        raise ValueError("policy_release_requires_event_date")
    if "llm" in (evidence.extracted_by or "").lower():
        metadata = evidence.metadata or {}
        if not (metadata.get("llm_trace_path") or metadata.get("extraction_trace_path")):
            raise ValueError("llm_trace_path_required")


class EvidenceLedger:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def add(self, evidence: Evidence, operator: str = "human") -> Evidence:
        validate_evidence_for_ledger(evidence)
        return self.store.add_evidence(evidence, operator=operator)

    def support_edge(
        self,
        edge_id: str,
        evidence: Evidence,
        operator: str = "rule_verifier",
    ) -> None:
        if edge_id not in evidence.supports:
            evidence.supports.append(edge_id)
        self.add(evidence, operator=operator)
        self.store.attach_evidence(edge_id, evidence.evidence_id, operator=operator)

    def contradict_edge(
        self,
        edge_id: str,
        evidence: Evidence,
        operator: str = "rule_verifier",
    ) -> None:
        if edge_id not in evidence.contradicts:
            evidence.contradicts.append(edge_id)
        self.add(evidence, operator=operator)
        self.store.attach_evidence(edge_id, evidence.evidence_id, as_counter=True, operator=operator)
