from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import AlphaPathRanker, GraphQuery, GraphStore, SeedGraphBuilder, ThemeSeedCompiler
from core.research_graph.schema import EdgeStatus, EvidenceLevel, HYPERGRAPH_DIMENSIONS, NodeType


SEED_NOTICE = "Seed Graph 不是事实，只是待验证假设"


def _build_store(topic: str) -> tuple[GraphStore, dict[str, object]]:
    store = GraphStore()
    seed = ThemeSeedCompiler().compile(topic)
    summary = SeedGraphBuilder(store).build(seed)
    return store, summary


def test_hbm_seed_graph_generates_structured_seven_dimension_summary() -> None:
    store, summary = _build_store("HBM")

    assert summary["topic"] == "HBM"
    assert summary["seed_name"] == "HBM Golden Seed"
    assert summary["seed_notice"] == SEED_NOTICE
    assert set(HYPERGRAPH_DIMENSIONS).issubset(set(summary["dimensions_covered"]))
    assert summary["assumptions"]
    assert summary["warnings"]
    assert summary["nodes"]
    assert summary["edges"]

    node_names = {node.name for node in store.nodes.values()}
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
        "测试",
        "硅片",
        "电镀液",
        "封装基板",
        "绝缘/阻挡层材料",
        "微凸点材料",
        "TSV 刻蚀设备",
        "电镀设备",
        "键合设备",
        "减薄设备",
        "测试设备",
        "HBM3E 路线",
        "HBM4 路线",
        "替代/旁路路线占位",
        "先进封装产能",
        "良率",
        "客户验证周期",
        "设备交期",
        "材料纯度",
        "年报证据占位",
        "公告证据占位",
        "研报证据占位",
        "论文/技术资料占位",
    }.issubset(node_names)

    node_types = {node.type for node in store.nodes.values()}
    assert {
        NodeType.THEME.value,
        NodeType.PRODUCT.value,
        NodeType.PROCESS.value,
        NodeType.MATERIAL.value,
        NodeType.EQUIPMENT.value,
        NodeType.CONSTRAINT.value,
        NodeType.DOCUMENT.value,
    }.issubset(node_types)


def test_seed_nodes_and_edges_are_unverified_hypotheses_only() -> None:
    store, _ = _build_store("HBM")

    assert all(node.source == "seed_graph" for node in store.nodes.values())
    assert all(node.status == EdgeStatus.UNVERIFIED_SEED.value for node in store.nodes.values())
    for node in store.nodes.values():
        assert node.attributes["seed_topic"] == "HBM"
        assert node.attributes["seed_notice"] == SEED_NOTICE
        assert node.attributes["needs_verification"] is True
        assert node.attributes["confidence"] == EvidenceLevel.SPECULATIVE.value

    assert all(edge.source == "seed_graph" for edge in store.edges.values())
    assert all(edge.status == EdgeStatus.UNVERIFIED_SEED.value for edge in store.edges.values())
    assert all(edge.confidence == EvidenceLevel.SPECULATIVE.value for edge in store.edges.values())
    assert all(edge.evidence_ids == [] for edge in store.edges.values())
    assert all(edge.counter_evidence_ids == [] for edge in store.edges.values())
    assert EdgeStatus.ACTIVE.value not in {edge.status for edge in store.edges.values()}
    for edge in store.edges.values():
        assert edge.qualifiers["dimension"] in HYPERGRAPH_DIMENSIONS
        assert edge.qualifiers["seed_notice"] == SEED_NOTICE
        assert edge.qualifiers["needs_verification"] is True
        assert edge.qualifiers["verification_target"]
        assert edge.qualifiers["seed_topic"] == "HBM"


def test_seed_paths_are_audit_only_and_not_ranked() -> None:
    store, _ = _build_store("HBM")
    query = GraphQuery(store)

    verified = query.related_targets("theme:hbm")
    assert verified["mode"] == "verified"
    assert verified["paths"] == []
    assert "companies" not in verified

    audit = query.related_targets("theme:hbm", include_audit_edges=True)
    assert audit["mode"] == "audit"
    assert audit["paths"]
    assert "companies" not in audit
    assert AlphaPathRanker().rank(audit["paths"])["paths"] == []


def test_non_hbm_topics_generate_seed_graphs_without_stock_lists() -> None:
    for topic in ("CPO", "800V HVDC", "数据中心电力"):
        store, summary = _build_store(topic)
        node_types = {node.type for node in store.nodes.values()}
        assert NodeType.THEME.value in node_types
        assert {NodeType.PRODUCT.value, NodeType.TECH_ROUTE.value} & node_types
        assert NodeType.PROCESS.value in node_types
        assert {NodeType.MATERIAL.value, NodeType.EQUIPMENT.value} & node_types
        assert NodeType.CONSTRAINT.value in node_types
        assert NodeType.DOCUMENT.value in node_types
        assert len(summary["dimensions_covered"]) >= 3
        assert all("ticker" not in node.attributes for node in store.nodes.values())
        assert all("stock_code" not in node.attributes for node in store.nodes.values())
        assert "companies" not in GraphQuery(store).related_targets(next(iter(store.nodes)))


def test_seed_builder_has_no_mutation_diff_audit_or_legacy_pipeline_dependency() -> None:
    source = (_PROJECT_ROOT / "core" / "research_graph" / "seed_graph.py").read_text(encoding="utf-8")
    forbidden_imports = (
        "graph_editor",
        "graph_diff",
        "mutation_log",
        "build_audit_snapshot",
        "research_routes",
        "QuantFlow",
        "quant_flow",
        "AlphaDiscoveryPipeline",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source
