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
    HyperNode,
    SeedGraphBuilder,
    ThemeSeedCompiler,
)
from core.research_graph.graph_diff import diff_stores
from core.research_graph.schema import (
    EdgeStatus,
    EvidenceLevel,
    HYPERGRAPH_DIMENSIONS,
    MutationAction,
    NodeType,
)


def _build_hbm_seed_store() -> GraphStore:
    store = GraphStore()
    seed = ThemeSeedCompiler().compile("HBM")
    SeedGraphBuilder(store).build(seed)
    return store


def _roundtrip_store(store: GraphStore, tmp_path: Path) -> GraphStore:
    path = tmp_path / "store.json"
    store.save_json(path)
    return GraphStore.load_json(path)


def test_hbm_theme_seed_compiler_generates_seven_dimension_golden_seed() -> None:
    seed = ThemeSeedCompiler().compile("HBM")
    node_names = {node.name for node in seed.nodes}
    dimensions = {edge.dimension for edge in seed.edges}

    assert set(HYPERGRAPH_DIMENSIONS).issubset(dimensions)
    assert {
        "HBM",
        "HBM3E",
        "HBM4",
        "DRAM Die 制造",
        "TSV刻蚀",
        "TSV铜电镀填充",
        "临时键合",
        "晶圆减薄",
        "微凸点",
        "堆叠封装",
        "电镀液",
        "封装基板",
        "相关材料占位",
        "刻蚀设备",
        "电镀设备",
        "检测设备",
        "先进封装产能",
        "良率",
        "客户验证周期",
        "替代路线占位",
        "公司链预留位",
    }.issubset(node_names)


def test_seed_graph_builder_keeps_seed_nodes_and_edges_unverified_with_required_qualifiers() -> None:
    store = _build_hbm_seed_store()

    assert all(node.source == "seed_graph" for node in store.nodes.values())
    assert all(node.status == EdgeStatus.UNVERIFIED_SEED.value for node in store.nodes.values())
    assert all(edge.source == "seed_graph" for edge in store.edges.values())
    assert all(edge.status == EdgeStatus.UNVERIFIED_SEED.value for edge in store.edges.values())
    for edge in store.edges.values():
        assert edge.qualifiers["dimension"] in HYPERGRAPH_DIMENSIONS
        assert edge.qualifiers["seed_notice"] == "Seed Graph 不是事实，只是待验证假设"
        assert edge.qualifiers["needs_verification"] is True
        assert edge.qualifiers["verification_target"]


def test_seed_paths_are_hidden_from_verified_query_visible_in_audit_and_not_ranked() -> None:
    store = _build_hbm_seed_store()
    query = GraphQuery(store)

    verified = query.related_targets("theme:hbm")
    assert verified["mode"] == "verified"
    assert verified["paths"] == []
    assert "companies" not in verified

    audit = query.related_targets("theme:hbm", include_audit_edges=True)
    assert audit["mode"] == "audit"
    assert audit["paths"]
    assert "companies" not in audit
    assert any(
        edge["status"] == EdgeStatus.UNVERIFIED_SEED.value
        for path in audit["paths"]
        for edge in path["edges"]
    )

    ranked = AlphaPathRanker().rank(audit["paths"])
    assert ranked["paths"] == []


def test_seed_edges_never_enter_active_even_if_evidence_is_attached() -> None:
    store = _build_hbm_seed_store()
    edge_id = "seed_hbm_requires_dram_die"
    store.add_evidence(
        Evidence(
            evidence_id="ev_seed_support",
            source_type="industry_report",
            source_title="HBM 工艺资料",
            quote="HBM 需要 DRAM Die 制造环节。",
            summary="支持 seed 假设，但不能让 seed 原地变成事实。",
            evidence_level=EvidenceLevel.LIKELY.value,
            supports=[edge_id],
        )
    )
    store.attach_evidence(edge_id, "ev_seed_support")

    assert store.edges[edge_id].source == "seed_graph"
    assert store.edges[edge_id].status == EdgeStatus.UNVERIFIED_SEED.value
    assert store.edges[edge_id].confidence == EvidenceLevel.SPECULATIVE.value


def test_graph_editing_primitives_write_mutation_log_and_graph_diff(tmp_path: Path) -> None:
    store = _build_hbm_seed_store()
    before = _roundtrip_store(store, tmp_path)
    editor = GraphEditor(store)

    editor.mark_node_too_coarse("theme:hbm", reason="主题节点需要继续拆到材料/设备")
    editor.rename_node("material:related_placeholder", "先进封装相关材料占位", reason="命名更明确")
    editor.split_node(
        "process:tsv_cu_plating",
        [
            HyperNode("process:barrier_seed_layer", NodeType.PROCESS.value, "阻挡层/种子层"),
            HyperNode("process:cu_electrofill", NodeType.PROCESS.value, "铜电镀填充"),
        ],
        reason="TSV 铜电镀填充仍然太粗",
    )
    editor.merge_node(
        "tech_route:substitute_placeholder",
        "tech_route:hbm4",
        reason="替代路线占位并入 HBM4 路线假设",
    )
    editor.rewire_edge(
        "seed_related_material_placeholder",
        objects=["material:package_substrate"],
        reason="将材料占位边重连到封装基板",
    )
    editor.delete_node("material:related_placeholder", reason="占位材料已被重连并删除")

    actions = [item.action for item in store.mutations.items]
    assert MutationAction.MARK_NODE_TOO_COARSE.value in actions
    assert MutationAction.RENAME_NODE.value in actions
    assert MutationAction.SPLIT_NODE.value in actions
    assert MutationAction.MERGE_NODE.value in actions
    assert MutationAction.REWIRE_EDGE.value in actions
    assert MutationAction.DELETE_NODE.value in actions

    diff = diff_stores(before, store)
    assert {
        "process:barrier_seed_layer",
        "process:cu_electrofill",
    }.issubset(set(diff["added_nodes"]))
    assert "material:related_placeholder" in diff["deleted_nodes"]
    assert "tech_route:substitute_placeholder" in diff["merged_nodes"]
    assert "seed_related_material_placeholder" in diff["rewired_edges"]
    assert "seed_related_material_placeholder" in diff["modified_edges"]
    assert "material:related_placeholder" in diff["status_changes"]["nodes"]
