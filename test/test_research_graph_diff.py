from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import (
    AlphaPathRanker,
    Evidence,
    GraphEditor,
    GraphQuery,
    GraphStore,
    HyperEdge,
    HyperNode,
)
from core.research_graph.audit import build_audit_snapshot
from core.research_graph.graph_diff import diff_stores
from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeType, RelationType


def _roundtrip(store: GraphStore, tmp_path: Path, name: str) -> GraphStore:
    path = tmp_path / f"{name}.json"
    store.save_json(path)
    return GraphStore.load_json(path)


def _store_for_diff() -> GraphStore:
    store = GraphStore()
    for node in (
        HyperNode("theme:ai", NodeType.THEME.value, "AI server"),
        HyperNode("company:a", NodeType.COMPANY.value, "Company A"),
        HyperNode("company:b", NodeType.COMPANY.value, "Company B"),
        HyperNode("material:x", NodeType.MATERIAL.value, "Material X"),
        HyperNode("material:y", NodeType.MATERIAL.value, "Material Y"),
        HyperNode("process:packaging", NodeType.PROCESS.value, "Advanced packaging"),
    ):
        store.add_node(node)
    store.add_evidence(
        Evidence(
            evidence_id="ev_x",
            source_type="annual_report",
            source_title="Company A annual report",
            quote="Company A produces Material X.",
            summary="Supports Company A producing Material X.",
            evidence_level=EvidenceLevel.PROVEN.value,
            supports=["edge_a_x"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_a_x",
            relation=RelationType.PRODUCES.value,
            subjects=["company:a"],
            objects=["material:x"],
            evidence_ids=["ev_x"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_b_y",
            relation=RelationType.PRODUCES.value,
            subjects=["company:b"],
            objects=["material:y"],
        )
    )
    return store


def test_graph_diff_reports_structured_changes(tmp_path: Path) -> None:
    store = _store_for_diff()
    before = _roundtrip(store, tmp_path, "before")
    editor = GraphEditor(store)

    editor.rename_node("material:x", "High purity Material X", reason="normalize name")
    editor.split_node(
        "process:packaging",
        [HyperNode("process:tsv", NodeType.PROCESS.value, "TSV")],
        reason="split coarse node",
    )
    editor.rewire_edge("edge_a_x", objects=["material:y"], reason="rewire material path")
    store.add_evidence(
        Evidence(
            evidence_id="ev_counter",
            source_type="announcement",
            source_title="Company A announcement",
            quote="Related revenue exposure is very low.",
            summary="Counter evidence.",
            evidence_level=EvidenceLevel.DISPROVEN.value,
            contradicts=["edge_a_x"],
        )
    )
    store.attach_evidence("edge_a_x", "ev_counter", as_counter=True)
    editor.delete_node("material:x", reason="delete old material node")

    diff = diff_stores(before, store)

    assert diff["added_nodes"] == ["process:tsv"]
    assert "material:x" in diff["removed_or_deleted_nodes"]
    assert diff["renamed_nodes"]["material:x"] == {"before": "Material X", "after": "High purity Material X"}
    assert diff["split_nodes"]["process:packaging"] == ["process:tsv"]
    assert "edge_a_x" in diff["rewired_edges"]
    assert "edge_a_x" in diff["confidence_changes"]
    assert "edge_a_x" in diff["status_changes"]["edges"]
    assert "edge_a_x" in diff["evidence_changes"]
    assert "edge_a_x" in diff["conflicted_changes"]


def test_audit_snapshot_counts_quality_issues_and_audit_only_edges() -> None:
    store = _store_for_diff()
    store.add_edge(
        HyperEdge(
            edge_id="edge_a_theme_mentions",
            relation=RelationType.MENTIONS.value,
            subjects=["company:a"],
            objects=["theme:ai"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_a_theme_coarse",
            relation=RelationType.PRODUCES.value,
            subjects=["company:a"],
            objects=["theme:ai"],
            evidence_ids=["ev_x"],
        )
    )
    store.edges["edge_b_y"].subjects = ["company:missing"]
    GraphEditor(store).mark_node_too_coarse("process:packaging", reason="too coarse")

    snapshot = build_audit_snapshot(store).to_dict()

    assert snapshot["node_count"] == len(store.nodes)
    assert snapshot["edge_count"] == len(store.edges)
    assert snapshot["evidence_count"] == len(store.evidence)
    assert snapshot["active_edge_count"] >= 1
    assert snapshot["candidate_edge_count"] >= 1
    assert "process:packaging" in snapshot["nodes_marked_too_coarse"]
    assert "edge_b_y" in snapshot["edges_without_evidence"]
    assert "edge_b_y" in snapshot["dangling_edges"]
    assert "edge_a_theme_coarse" in snapshot["coarse_company_theme_edges"]
    assert "edge_a_theme_mentions" in snapshot["audit_only_edges"]


def test_verified_output_rules_survive_mutations() -> None:
    store = _store_for_diff()
    store.add_node(HyperNode("theme:materials", NodeType.THEME.value, "Materials theme"))
    store.add_evidence(
        Evidence(
            evidence_id="ev_theme_material",
            source_type="industry_report",
            source_title="Material industry source",
            quote="Materials theme includes Material X.",
            summary="Supports theme to material path.",
            evidence_level=EvidenceLevel.LIKELY.value,
            supports=["edge_theme_material"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_theme_material",
            relation=RelationType.PART_OF.value,
            subjects=["material:x"],
            objects=["theme:materials"],
            evidence_ids=["ev_theme_material"],
        )
    )
    query = GraphQuery(store)
    verified_before = query.benefit_paths("theme:materials", "company:a")
    assert verified_before
    assert AlphaPathRanker().rank(verified_before)["paths"]

    GraphEditor(store).delete_node("material:x", reason="delete material node")

    assert query.benefit_paths("theme:materials", "company:a") == []
    audit_paths = query.benefit_paths("theme:materials", "company:a", include_audit_edges=True)
    assert audit_paths
    assert AlphaPathRanker().rank(audit_paths)["paths"] == []
