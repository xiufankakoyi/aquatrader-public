from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import (
    Evidence,
    GraphStore,
    GraphVerifier,
    HyperEdge,
    HyperNode,
    LLMVerificationOpinion,
    LLMVerificationRequest,
)
from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeType, RelationType


class FakeAdvisor:
    def __init__(self, opinion: LLMVerificationOpinion) -> None:
        self.opinion = opinion
        self.calls: list[LLMVerificationRequest] = []

    def review(self, request: LLMVerificationRequest) -> LLMVerificationOpinion:
        self.calls.append(request)
        return self.opinion


def _store_with_nodes(verifier: GraphVerifier) -> GraphStore:
    store = GraphStore(verifier=verifier)
    for node in (
        HyperNode("theme:hbm", NodeType.THEME.value, "HBM"),
        HyperNode("company:a", NodeType.COMPANY.value, "Company A"),
        HyperNode("process:tsv", NodeType.PROCESS.value, "TSV process"),
        HyperNode("material:plating", NodeType.MATERIAL.value, "Plating material"),
    ):
        store.add_node(node)
    return store


def _support(evidence_id: str = "ev_support") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        source_type="annual_report",
        source_title="Company A annual report",
        quote="Company A discloses plating material used in advanced packaging validation.",
        summary="Traceable support evidence for the specific process/material path.",
        evidence_level=EvidenceLevel.PROVEN.value,
        source_id="src_annual_report",
        document_id=evidence_id,
    )


def test_hybrid_verifier_does_not_let_llm_override_company_theme_hard_rule() -> None:
    advisor = FakeAdvisor(LLMVerificationOpinion(
        status=EdgeStatus.ACTIVE.value,
        confidence=EvidenceLevel.PROVEN.value,
        rationale="approve",
        grounded_evidence_ids=["ev_support"],
        should_enter_active=True,
    ))
    store = _store_with_nodes(GraphVerifier(advisor, mode="hybrid"))
    store.add_evidence(_support())

    edge, result = store.add_edge(HyperEdge(
        edge_id="edge_company_theme",
        relation=RelationType.BENEFITS_FROM.value,
        subjects=["company:a"],
        objects=["theme:hbm"],
        evidence_ids=["ev_support"],
    ))

    assert advisor.calls == []
    assert edge.status == EdgeStatus.CANDIDATE.value
    assert result.blocked_by_hard_rule is True
    assert "hybrid_hard_rule_blocks_llm_override" in result.messages


def test_hybrid_verifier_requires_grounded_llm_approval_for_active() -> None:
    advisor = FakeAdvisor(LLMVerificationOpinion(
        status=EdgeStatus.ACTIVE.value,
        confidence=EvidenceLevel.PROVEN.value,
        rationale="specific path is grounded",
        grounded_evidence_ids=["ev_support"],
        should_enter_active=True,
    ))
    store = _store_with_nodes(GraphVerifier(advisor, mode="hybrid", require_llm_for_active=True))
    store.add_evidence(_support())

    edge, result = store.add_edge(HyperEdge(
        edge_id="edge_company_process",
        relation=RelationType.BENEFITS_FROM.value,
        subjects=["company:a"],
        objects=["process:tsv"],
        qualifiers={"exposure_type": "indirect"},
        evidence_ids=["ev_support"],
    ))

    assert advisor.calls
    assert edge.status == EdgeStatus.ACTIVE.value
    assert result.verification_mode == "hybrid"
    assert result.rule_status == EdgeStatus.ACTIVE.value
    assert result.llm_status == EdgeStatus.ACTIVE.value
    assert result.grounded_evidence_ids == ["ev_support"]


def test_hybrid_verifier_blocks_ungrounded_llm_approval() -> None:
    advisor = FakeAdvisor(LLMVerificationOpinion(
        status=EdgeStatus.ACTIVE.value,
        confidence=EvidenceLevel.PROVEN.value,
        rationale="approve without valid citation",
        grounded_evidence_ids=["not_on_edge"],
        should_enter_active=True,
    ))
    store = _store_with_nodes(GraphVerifier(advisor, mode="hybrid", require_llm_for_active=True))
    store.add_evidence(_support())

    edge, result = store.add_edge(HyperEdge(
        edge_id="edge_ungrounded",
        relation=RelationType.PRODUCES.value,
        subjects=["company:a"],
        objects=["material:plating"],
        evidence_ids=["ev_support"],
    ))

    assert edge.status == EdgeStatus.CANDIDATE.value
    assert result.requires_human_review is True
    assert "llm_did_not_ground_active_approval" in result.llm_messages


def test_hybrid_verifier_llm_can_downgrade_rule_pass_to_conflicted() -> None:
    advisor = FakeAdvisor(LLMVerificationOpinion(
        status=EdgeStatus.CONFLICTED.value,
        confidence=EvidenceLevel.CONFLICTED.value,
        rationale="source says product exists but customer path is still unclear",
        grounded_evidence_ids=["ev_support"],
        risk_flags=["customer_path_unclear"],
        should_enter_active=False,
    ))
    store = _store_with_nodes(GraphVerifier(advisor, mode="hybrid"))
    store.add_evidence(_support())

    edge, result = store.add_edge(HyperEdge(
        edge_id="edge_conflicted_by_llm",
        relation=RelationType.PRODUCES.value,
        subjects=["company:a"],
        objects=["material:plating"],
        evidence_ids=["ev_support"],
    ))
    audit = store.verifier.audit_store(store.nodes, store.edges, store.evidence)

    assert edge.status == EdgeStatus.CONFLICTED.value
    assert result.confidence == EvidenceLevel.CONFLICTED.value
    assert "edge_conflicted_by_llm" in audit["conflicted_edges"]
    assert "edge_conflicted_by_llm" in audit["llm_reviewed_edges"]
    assert "edge_conflicted_by_llm" in audit["llm_blocked_edges"]
