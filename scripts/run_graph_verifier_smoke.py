from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "research_runs" / "graph_verifier_smoke_latest"


class DeterministicAdvisor:
    def review(self, request: LLMVerificationRequest) -> LLMVerificationOpinion:
        if request.edge.edge_id == "edge_llm_conflict":
            return LLMVerificationOpinion(
                status=EdgeStatus.CONFLICTED.value,
                confidence=EvidenceLevel.CONFLICTED.value,
                rationale="deterministic_review_flags_conflict",
                grounded_evidence_ids=[item.evidence_id for item in request.support_evidence],
                risk_flags=["customer_path_unclear"],
            )
        return LLMVerificationOpinion(
            status=EdgeStatus.ACTIVE.value,
            confidence=EvidenceLevel.LIKELY.value,
            rationale="deterministic_review_grounded",
            grounded_evidence_ids=[item.evidence_id for item in request.support_evidence],
            should_enter_active=True,
        )


def build_store() -> GraphStore:
    store = GraphStore(GraphVerifier(DeterministicAdvisor(), mode="hybrid", require_llm_for_active=True))
    for node in (
        HyperNode("theme:hbm", NodeType.THEME.value, "HBM"),
        HyperNode("company:a", NodeType.COMPANY.value, "Company A"),
        HyperNode("process:tsv", NodeType.PROCESS.value, "TSV process"),
        HyperNode("material:plating", NodeType.MATERIAL.value, "Plating material"),
    ):
        store.add_node(node)
    for evidence_id, summary in (
        ("ev_specific_path", "Specific process path evidence."),
        ("ev_conflict_path", "Evidence with unresolved customer path."),
        ("ev_coarse_theme", "Coarse theme mention."),
    ):
        store.add_evidence(Evidence(
            evidence_id=evidence_id,
            source_id=f"src_{evidence_id}",
            document_id=evidence_id,
            source_type="annual_report",
            source_title=f"{evidence_id} report",
            quote=summary,
            summary=summary,
            evidence_level=EvidenceLevel.PROVEN.value,
        ))
    store.add_edge(HyperEdge(
        edge_id="edge_specific_active",
        relation=RelationType.BENEFITS_FROM.value,
        subjects=["company:a"],
        objects=["process:tsv"],
        qualifiers={"exposure_type": "indirect"},
        evidence_ids=["ev_specific_path"],
    ))
    store.add_edge(HyperEdge(
        edge_id="edge_llm_conflict",
        relation=RelationType.PRODUCES.value,
        subjects=["company:a"],
        objects=["material:plating"],
        evidence_ids=["ev_conflict_path"],
    ))
    store.add_edge(HyperEdge(
        edge_id="edge_hard_blocked",
        relation=RelationType.BENEFITS_FROM.value,
        subjects=["company:a"],
        objects=["theme:hbm"],
        evidence_ids=["ev_coarse_theme"],
    ))
    return store


def run_smoke(output_dir: Path) -> dict:
    store = build_store()
    audit = store.verifier.audit_store(store.nodes, store.edges, store.evidence)
    summary = {
        "status": "passed" if _passes(store, audit) else "failed",
        "verification_mode": audit["verification_mode"],
        "edge_statuses": {edge_id: edge.status for edge_id, edge in store.edges.items()},
        "llm_reviewed_edges": audit["llm_reviewed_edges"],
        "llm_blocked_edges": audit["llm_blocked_edges"],
        "blocked_edges": audit["blocked_edges"],
        "conflicted_edges": audit["conflicted_edges"],
        "hard_blocked_edges": sorted(
            edge_id
            for edge_id, result in audit["edge_results"].items()
            if result["blocked_by_hard_rule"]
        ),
        "result_type": "graph_verifier_smoke_summary",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summary


def _passes(store: GraphStore, audit: dict) -> bool:
    return (
        audit["verification_mode"] == "hybrid"
        and store.edges["edge_specific_active"].status == EdgeStatus.ACTIVE.value
        and store.edges["edge_llm_conflict"].status == EdgeStatus.CONFLICTED.value
        and store.edges["edge_hard_blocked"].status == EdgeStatus.CANDIDATE.value
        and "edge_hard_blocked" not in audit["llm_reviewed_edges"]
        and "edge_llm_conflict" in audit["llm_blocked_edges"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ResearchGraph Graph Verifier smoke validation.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    summary = run_smoke(args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir), **summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
