from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import Evidence, GraphEditor, GraphStore, HyperEdge, HyperNode
from core.research_graph.audit import build_audit_snapshot
from core.research_graph.schema import EdgeStatus, EvidenceLevel, MutationAction, NodeSource, NodeType, RelationType


def _base_store() -> GraphStore:
    store = GraphStore()
    for node in (
        HyperNode("theme:ai", NodeType.THEME.value, "AI server"),
        HyperNode("company:a", NodeType.COMPANY.value, "Company A"),
        HyperNode("company:a_dup", NodeType.COMPANY.value, "Company A old name"),
        HyperNode("material:x", NodeType.MATERIAL.value, "Material X"),
        HyperNode("process:advanced_packaging", NodeType.PROCESS.value, "Advanced packaging"),
        HyperNode("process:tsv", NodeType.PROCESS.value, "TSV"),
    ):
        store.add_node(node)
    store.add_evidence(
        Evidence(
            evidence_id="ev_material_x",
            source_type="annual_report",
            source_title="Company A annual report",
            quote="Company A produces Material X.",
            summary="Supports Company A producing Material X.",
            evidence_level=EvidenceLevel.PROVEN.value,
            supports=["edge_company_material"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_company_material",
            relation=RelationType.PRODUCES.value,
            subjects=["company:a"],
            objects=["material:x"],
            evidence_ids=["ev_material_x"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_dup_material",
            relation=RelationType.PRODUCES.value,
            subjects=["company:a_dup"],
            objects=["material:x"],
        )
    )
    return store


def _last_mutation(store: GraphStore, action: str):
    return [item for item in store.mutations.items if item.action == action][-1]


def test_rename_node_keeps_alias_and_records_before_after() -> None:
    store = _base_store()
    GraphEditor(store).rename_node("material:x", "High purity Material X", reason="normalize material name")

    node = store.nodes["material:x"]
    mutation = _last_mutation(store, MutationAction.RENAME_NODE.value)

    assert node.name == "High purity Material X"
    assert "Material X" in node.aliases
    assert mutation.before["name"] == "Material X"
    assert mutation.after["name"] == "High purity Material X"
    assert mutation.reason == "normalize material name"


def test_merge_nodes_rewires_edges_tombstones_sources_and_avoids_dangling_edges() -> None:
    store = _base_store()
    GraphEditor(store).merge_nodes(["company:a_dup"], "company:a", reason="merge duplicate company nodes")

    assert store.nodes["company:a_dup"].status == EdgeStatus.DELETED.value
    assert store.nodes["company:a_dup"].attributes["merged_into"] == "company:a"
    assert store.edges["edge_dup_material"].subjects == ["company:a"]
    assert build_audit_snapshot(store).dangling_edges == []

    mutation = _last_mutation(store, MutationAction.MERGE_NODE.value)
    assert "company:a_dup" in mutation.before["sources"]
    assert mutation.after["sources"]["company:a_dup"]["attributes"]["merged_into"] == "company:a"


def test_split_node_marks_source_and_new_nodes_are_not_active() -> None:
    store = _base_store()
    added = GraphEditor(store).split_node(
        "process:advanced_packaging",
        [
            HyperNode("process:temporary_bonding", NodeType.PROCESS.value, "Temporary bonding"),
            HyperNode("process:wafer_thinning", NodeType.PROCESS.value, "Wafer thinning", source=NodeSource.SEED_GRAPH.value),
        ],
        reason="advanced packaging is too coarse",
    )

    assert store.nodes["process:advanced_packaging"].status == EdgeStatus.CANDIDATE.value
    assert store.nodes["process:advanced_packaging"].attributes["too_coarse"] is True
    assert store.nodes["process:advanced_packaging"].attributes["split_into"] == [
        "process:temporary_bonding",
        "process:wafer_thinning",
    ]
    assert {node.status for node in added} == {
        EdgeStatus.CANDIDATE.value,
        EdgeStatus.UNVERIFIED_SEED.value,
    }
    mutation = _last_mutation(store, MutationAction.SPLIT_NODE.value)
    assert mutation.after["source"]["attributes"]["split_into"]


def test_delete_node_tombstones_node_and_deletes_related_edges_without_dangling_edges() -> None:
    store = _base_store()
    GraphEditor(store).delete_node("material:x", reason="remove wrong material placeholder")

    assert store.nodes["material:x"].status == EdgeStatus.DELETED.value
    assert store.edges["edge_company_material"].status == EdgeStatus.DELETED.value
    assert store.edges["edge_company_material"].qualifiers["broken_by_delete_node"] == "material:x"
    assert build_audit_snapshot(store).dangling_edges == []
    assert _last_mutation(store, MutationAction.DELETE_NODE.value).reason == "remove wrong material placeholder"


def test_rewire_edge_validates_nodes_and_blocks_coarse_company_theme_active_relation() -> None:
    store = _base_store()
    edge = store.edges["edge_company_material"]
    assert edge.status == EdgeStatus.ACTIVE.value

    GraphEditor(store).rewire_edge(
        "edge_company_material",
        objects=["theme:ai"],
        reason="rewire to theme should be blocked by verifier",
    )

    rewired = store.edges["edge_company_material"]
    mutation = _last_mutation(store, MutationAction.REWIRE_EDGE.value)
    assert rewired.objects == ["theme:ai"]
    assert rewired.status == EdgeStatus.CANDIDATE.value
    assert rewired.status != EdgeStatus.ACTIVE.value
    assert mutation.before["objects"] == ["material:x"]
    assert mutation.after["edge"]["objects"] == ["theme:ai"]
    assert mutation.after["verification_messages"]


def test_mark_node_too_coarse_is_visible_to_audit_and_growth_agent_source() -> None:
    store = _base_store()
    GraphEditor(store).mark_node_too_coarse(
        "process:advanced_packaging",
        reason="advanced packaging is too coarse",
        suggested_action="Split into TSV / temporary bonding / wafer thinning / micro bump",
    )

    node = store.nodes["process:advanced_packaging"]
    snapshot = build_audit_snapshot(store)
    assert node.attributes["too_coarse"] is True
    assert node.attributes["suggested_action"] == "Split into TSV / temporary bonding / wafer thinning / micro bump"
    assert "process:advanced_packaging" in snapshot.nodes_marked_too_coarse
    assert _last_mutation(store, MutationAction.MARK_NODE_TOO_COARSE.value).before["attributes"] == {}
