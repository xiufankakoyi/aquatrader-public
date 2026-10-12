from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from core.research_graph.models import Evidence, HyperEdge, HyperNode
from core.research_graph.schema import (
    AUDIT_THEME_RELATION_TYPES,
    CAUSAL_STATUS_VALUES,
    COMPANY_NODE_TYPES,
    CONCRETE_BENEFIT_NODE_TYPES,
    CausalStatus,
    EdgeStatus,
    EvidenceLevel,
    NodeSource,
    NodeType,
    OFFICIAL_SOURCE_TYPES,
    RelationType,
)


@dataclass
class VerificationResult:
    status: str
    confidence: str
    messages: list[str] = field(default_factory=list)
    can_enter_active: bool = False
    verification_mode: str = "rules"
    rule_status: str = ""
    rule_confidence: str = ""
    llm_status: str = ""
    llm_confidence: str = ""
    llm_messages: list[str] = field(default_factory=list)
    grounded_evidence_ids: list[str] = field(default_factory=list)
    blocked_by_hard_rule: bool = False
    requires_human_review: bool = False


@dataclass
class LLMVerificationRequest:
    edge: HyperEdge
    subject_nodes: list[HyperNode]
    object_nodes: list[HyperNode]
    support_evidence: list[Evidence]
    counter_evidence: list[Evidence]
    rule_result: VerificationResult


@dataclass
class LLMVerificationOpinion:
    status: str
    confidence: str
    rationale: str
    grounded_evidence_ids: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    should_enter_active: bool = False


class LLMVerificationAdvisor(Protocol):
    def review(self, request: LLMVerificationRequest) -> LLMVerificationOpinion:
        ...


class GraphVerifier:
    """Admission rules for HyperGraph edges with optional LLM cross-check."""

    def __init__(
        self,
        llm_advisor: LLMVerificationAdvisor | None = None,
        *,
        mode: str = "rules",
        require_llm_for_active: bool = False,
    ) -> None:
        if mode not in {"rules", "hybrid"}:
            raise ValueError("GraphVerifier mode must be rules or hybrid")
        self.llm_advisor = llm_advisor
        self.mode = mode
        self.require_llm_for_active = require_llm_for_active

    def verify_edge(
        self,
        edge: HyperEdge,
        nodes: dict[str, HyperNode],
        evidence: dict[str, Evidence],
    ) -> VerificationResult:
        subject_nodes = [nodes.get(node_id) for node_id in edge.subjects]
        object_nodes = [nodes.get(node_id) for node_id in edge.objects]
        result = self._verify_edge_rules(edge, evidence, subject_nodes, object_nodes)
        result.verification_mode = self.mode
        result.rule_status = result.status
        result.rule_confidence = result.confidence
        if self.mode != "hybrid":
            return result
        return self._merge_llm_review(edge, subject_nodes, object_nodes, evidence, result)

    def _verify_edge_rules(
        self,
        edge: HyperEdge,
        evidence: dict[str, Evidence],
        subject_nodes: list[HyperNode | None],
        object_nodes: list[HyperNode | None],
    ) -> VerificationResult:
        messages: list[str] = []
        status = (
            EdgeStatus.UNVERIFIED_SEED.value
            if edge.source == NodeSource.SEED_GRAPH.value
            else EdgeStatus.CANDIDATE.value
        )
        confidence = edge.confidence or EvidenceLevel.SPECULATIVE.value

        if any(node is None for node in subject_nodes + object_nodes):
            messages.append("missing_node")
            return VerificationResult(
                status=status,
                confidence=confidence,
                messages=messages,
                blocked_by_hard_rule=True,
                requires_human_review=True,
            )

        if self._is_forbidden_coarse_company_theme_edge(edge, subject_nodes, object_nodes):
            messages.append("company_theme_requires_specific_path")
            return VerificationResult(
                status=EdgeStatus.CANDIDATE.value,
                confidence=confidence,
                messages=messages,
                blocked_by_hard_rule=True,
                requires_human_review=True,
            )

        if edge.source == NodeSource.SEED_GRAPH.value:
            messages.append("seed_graph_cannot_enter_active")
            return VerificationResult(
                status=EdgeStatus.UNVERIFIED_SEED.value,
                confidence=EvidenceLevel.SPECULATIVE.value,
                messages=messages,
                blocked_by_hard_rule=True,
                requires_human_review=True,
            )

        support_evidence = [evidence[item] for item in edge.evidence_ids if item in evidence]
        counter_evidence = [evidence[item] for item in edge.counter_evidence_ids if item in evidence]

        if not support_evidence:
            messages.append("active_requires_evidence_id")
            return VerificationResult(
                status=status,
                confidence=confidence,
                messages=messages,
                blocked_by_hard_rule=True,
                requires_human_review=True,
            )

        if counter_evidence:
            if edge.qualifiers.get("exposure_type") == "direct":
                messages.append("direct_edge_counter_evidence_disproves")
                return VerificationResult(
                    status=EdgeStatus.DISPROVEN.value,
                    confidence=EvidenceLevel.DISPROVEN.value,
                    messages=messages,
                    requires_human_review=True,
                )
            messages.append("support_and_counter_evidence_conflict")
            return VerificationResult(
                status=EdgeStatus.CONFLICTED.value,
                confidence=EvidenceLevel.CONFLICTED.value,
                messages=messages,
                requires_human_review=True,
            )

        best_confidence = self._best_support_level(support_evidence)
        messages.append(f"rules_passed:{best_confidence}")

        # Step 9:因果类型差异化校验。
        # QUASI_EXPERIMENTAL_EFFECT 及以上要求 support evidence metadata 含
        # pre_period_rmse(由 temporal_causal_service 写入),否则降级为 CANDIDATE。
        causal_status = str(edge.qualifiers.get("causal_status", "") or "")
        if causal_status:
            if causal_status not in CAUSAL_STATUS_VALUES:
                messages.append(f"invalid_causal_status:{causal_status}")
                return VerificationResult(
                    status=EdgeStatus.CANDIDATE.value,
                    confidence=confidence,
                    messages=messages,
                    blocked_by_hard_rule=True,
                    requires_human_review=True,
                )
            if causal_status in (
                CausalStatus.QUASI_EXPERIMENTAL_EFFECT.value,
                CausalStatus.PREDICTIVE_CAUSAL.value,
                CausalStatus.INTERVENTION_READY.value,
            ):
                # QUASI_EXPERIMENTAL_EFFECT 及以上要求 support evidence
                # 含 pre_period_rmse(由 temporal_causal_service 写入),
                # 仅 item.causal_status 相同不足以证明准实验归因。
                has_quasi_evidence = any(
                    (item.metadata or {}).get("pre_period_rmse") is not None
                    for item in support_evidence
                )
                if not has_quasi_evidence:
                    messages.append(
                        f"causal_status_{causal_status}_requires_quasi_experimental_evidence"
                    )
                    return VerificationResult(
                        status=EdgeStatus.CANDIDATE.value,
                        confidence=confidence,
                        messages=messages,
                        requires_human_review=True,
                    )
            # OBSERVATIONAL_ASSOCIATION 不能单独进入 ACTIVE,需配合额外 support。
            if (
                causal_status == CausalStatus.OBSERVATIONAL_ASSOCIATION.value
                and len(support_evidence) < 2
            ):
                messages.append("observational_association_requires_more_support")
                return VerificationResult(
                    status=EdgeStatus.CANDIDATE.value,
                    confidence=confidence,
                    messages=messages,
                    requires_human_review=True,
                )

        return VerificationResult(
            status=EdgeStatus.ACTIVE.value,
            confidence=best_confidence,
            messages=messages,
            can_enter_active=True,
            grounded_evidence_ids=[item.evidence_id for item in support_evidence],
        )

    def _merge_llm_review(
        self,
        edge: HyperEdge,
        subject_nodes: list[HyperNode | None],
        object_nodes: list[HyperNode | None],
        evidence: dict[str, Evidence],
        rule_result: VerificationResult,
    ) -> VerificationResult:
        if rule_result.blocked_by_hard_rule:
            rule_result.messages.append("hybrid_hard_rule_blocks_llm_override")
            return rule_result

        support_evidence = [evidence[item] for item in edge.evidence_ids if item in evidence]
        counter_evidence = [evidence[item] for item in edge.counter_evidence_ids if item in evidence]
        if self.llm_advisor is None:
            if self.require_llm_for_active and rule_result.can_enter_active:
                return VerificationResult(
                    status=EdgeStatus.CANDIDATE.value,
                    confidence=rule_result.confidence,
                    messages=rule_result.messages + ["hybrid_missing_llm_review_blocks_active"],
                    verification_mode=self.mode,
                    rule_status=rule_result.status,
                    rule_confidence=rule_result.confidence,
                    grounded_evidence_ids=rule_result.grounded_evidence_ids,
                    requires_human_review=True,
                )
            rule_result.messages.append("hybrid_no_llm_advisor_rule_result_kept")
            return rule_result

        request = LLMVerificationRequest(
            edge=edge,
            subject_nodes=[node for node in subject_nodes if node is not None],
            object_nodes=[node for node in object_nodes if node is not None],
            support_evidence=support_evidence,
            counter_evidence=counter_evidence,
            rule_result=rule_result,
        )
        opinion = self.llm_advisor.review(request)
        valid_ids = set(edge.evidence_ids + edge.counter_evidence_ids)
        grounded = [item for item in opinion.grounded_evidence_ids if item in valid_ids]
        llm_messages = [opinion.rationale, *opinion.risk_flags]

        if opinion.status == EdgeStatus.DISPROVEN.value or opinion.confidence == EvidenceLevel.DISPROVEN.value:
            final_status = EdgeStatus.DISPROVEN.value
            final_confidence = EvidenceLevel.DISPROVEN.value
            can_enter_active = False
        elif opinion.status == EdgeStatus.CONFLICTED.value or opinion.confidence == EvidenceLevel.CONFLICTED.value:
            final_status = EdgeStatus.CONFLICTED.value
            final_confidence = EvidenceLevel.CONFLICTED.value
            can_enter_active = False
        elif rule_result.can_enter_active and opinion.should_enter_active and grounded:
            final_status = EdgeStatus.ACTIVE.value
            final_confidence = self._lower_confidence(rule_result.confidence, opinion.confidence)
            can_enter_active = True
        else:
            final_status = EdgeStatus.CANDIDATE.value
            final_confidence = self._lower_confidence(rule_result.confidence, opinion.confidence)
            can_enter_active = False
            llm_messages.append("llm_did_not_ground_active_approval")

        return VerificationResult(
            status=final_status,
            confidence=final_confidence,
            messages=rule_result.messages + [f"hybrid_llm_status:{opinion.status}", *llm_messages],
            can_enter_active=can_enter_active,
            verification_mode=self.mode,
            rule_status=rule_result.status,
            rule_confidence=rule_result.confidence,
            llm_status=opinion.status,
            llm_confidence=opinion.confidence,
            llm_messages=llm_messages,
            grounded_evidence_ids=grounded,
            requires_human_review=not can_enter_active,
        )

    def audit_store(
        self,
        nodes: dict[str, HyperNode],
        edges: dict[str, HyperEdge],
        evidence: dict[str, Evidence],
    ) -> dict[str, Any]:
        edge_results: dict[str, dict[str, Any]] = {}
        blocked: list[str] = []
        conflicted: list[str] = []
        for edge_id, edge in edges.items():
            result = self.verify_edge(edge, nodes, evidence)
            edge_results[edge_id] = {
                "status": result.status,
                "confidence": result.confidence,
                "messages": result.messages,
                "can_enter_active": result.can_enter_active,
                "verification_mode": result.verification_mode,
                "rule_status": result.rule_status,
                "rule_confidence": result.rule_confidence,
                "llm_status": result.llm_status,
                "llm_confidence": result.llm_confidence,
                "llm_messages": result.llm_messages,
                "grounded_evidence_ids": result.grounded_evidence_ids,
                "blocked_by_hard_rule": result.blocked_by_hard_rule,
                "requires_human_review": result.requires_human_review,
            }
            if not result.can_enter_active:
                blocked.append(edge_id)
            if result.status == EdgeStatus.CONFLICTED.value:
                conflicted.append(edge_id)
        return {
            "edge_results": edge_results,
            "blocked_edges": blocked,
            "conflicted_edges": conflicted,
            "verification_mode": self.mode,
            "llm_reviewed_edges": sorted(
                edge_id for edge_id, result in edge_results.items() if result["llm_status"]
            ),
            "llm_blocked_edges": sorted(
                edge_id
                for edge_id, result in edge_results.items()
                if result["llm_status"] and not result["can_enter_active"]
            ),
            "rule_summary": [
                "reject_coarse_company_theme_edges",
                "require_specific_path_node",
                "require_support_evidence",
                "detect_counter_evidence",
                "detect_conflicts",
                "hybrid_review_cannot_override_hard_rules",
            ],
        }

    @staticmethod
    def _is_forbidden_coarse_company_theme_edge(
        edge: HyperEdge,
        subject_nodes: list[HyperNode | None],
        object_nodes: list[HyperNode | None],
    ) -> bool:
        subject_types = {node.type for node in subject_nodes if node is not None}
        object_types = {node.type for node in object_nodes if node is not None}

        has_company = bool((subject_types | object_types) & COMPANY_NODE_TYPES)
        has_theme = NodeType.THEME.value in (subject_types | object_types)
        if (
            has_company
            and has_theme
            and edge.relation not in AUDIT_THEME_RELATION_TYPES
        ):
            return True

        if (
            edge.relation == RelationType.BENEFITS_FROM.value
            and subject_types & COMPANY_NODE_TYPES
            and not (object_types & CONCRETE_BENEFIT_NODE_TYPES)
        ):
            return True
        return False

    @staticmethod
    def _best_support_level(items: list[Evidence]) -> str:
        if any(item.evidence_level == EvidenceLevel.PROVEN.value for item in items):
            return EvidenceLevel.PROVEN.value
        if any(item.source_type in OFFICIAL_SOURCE_TYPES for item in items):
            return EvidenceLevel.PROVEN.value
        likely_count = sum(1 for item in items if item.evidence_level == EvidenceLevel.LIKELY.value)
        if likely_count or len(items) >= 2:
            return EvidenceLevel.LIKELY.value
        return EvidenceLevel.SPECULATIVE.value

    @staticmethod
    def _lower_confidence(rule_confidence: str, llm_confidence: str) -> str:
        order = {
            EvidenceLevel.DISPROVEN.value: 0,
            EvidenceLevel.CONFLICTED.value: 0,
            EvidenceLevel.SPECULATIVE.value: 1,
            EvidenceLevel.LIKELY.value: 2,
            EvidenceLevel.PROVEN.value: 3,
        }
        reverse = {
            0: EvidenceLevel.SPECULATIVE.value,
            1: EvidenceLevel.SPECULATIVE.value,
            2: EvidenceLevel.LIKELY.value,
            3: EvidenceLevel.PROVEN.value,
        }
        return reverse[min(order.get(rule_confidence, 1), order.get(llm_confidence, 1))]
