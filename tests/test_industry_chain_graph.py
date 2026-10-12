"""P3 产业链图谱与搜索任务测试。

覆盖 SubTask 12.1-12.10：
- 12.1: GB200 节点能生成
- 12.2: LPO 节点能生成
- 12.3: GB200 缺少 PCB 下钻边时能生成 missing_edge
- 12.4: LPO 缺少光模块/光芯片下钻边时能生成 missing_edge
- 12.5: missing_edge 能生成 search_task
- 12.6: 不允许生成 supplies
- 12.7: 不允许生成 confirmed_supplier
- 12.8: industry_chain_graph_latest.json 能生成（在 tmp_path 下）
- 12.9: evidence_edges / graph_edges 的 confidence_level 不含 probable / verified
- 12.10: search_tasks 输出到 search_tasks_from_missing_edges_latest.json，含 task_source / generated_by
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.research.perilla.discovery.industry_chain_graph_builder import (
    build_allowed_edges,
    build_nodes,
    detect_missing_edges,
    export_industry_chain_graph,
    load_inputs,
    run_industry_chain_graph,
)
from core.research.perilla.discovery.search_task_compiler import (
    compile_search_tasks,
    run_search_task_compiler,
)


# ============================================================================
# 测试辅助函数
# ============================================================================


def _make_concept(
    name: str, term_type: str, root_theme: str = "",
    final_score: float = 0.5, doc_ids: List[str] = None,
) -> Dict[str, Any]:
    """创建测试用 candidate_concept。"""
    return {
        "concept_id": f"concept_{name}",
        "concept_name": name,
        "normalized_name": name,
        "term_type": term_type,
        "root_theme": root_theme,
        "evidence_document_ids": doc_ids or ["doc_1"],
        "final_score": final_score,
    }


def _make_evidence_edge(
    source: str, relation: str, target: str,
    confidence_level: str = "hypothesis",
) -> Dict[str, Any]:
    """创建测试用 evidence_edge。"""
    return {
        "edge_id": f"edge_{source}_{target}",
        "source_node": source,
        "source_type": "concept",
        "relation": relation,
        "target_node": target,
        "target_type": "theme",
        "confidence_level": confidence_level,
        "evidence_document_ids": ["doc_1"],
        "evidence_quotes": [],
        "status": "pending",
    }


def _make_causal_chain(
    cause: str, beneficiary: str, doc_id: str = "doc_1",
) -> Dict[str, Any]:
    """创建测试用 causal_chain。"""
    return {
        "chain_id": f"chain_{cause}_{beneficiary}",
        "claim_id": "claim_1",
        "document_id": doc_id,
        "cause": cause,
        "mechanism": "未明确",
        "effect": "未明确",
        "beneficiary_node": beneficiary,
        "evidence_quote": "测试引用",
        "confidence": "low",
        "status": "pending",
    }


def _setup_p3_data(tmp_path: Path) -> Path:
    """在 tmp_path 下创建 P3 测试输入文件。"""
    concepts = [
        _make_concept("GB200", "product_platform", "算", 0.8, ["doc_1", "doc_2"]),
        _make_concept("LPO", "technology_route", "光", 0.7, ["doc_1"]),
        _make_concept("PCB", "component", "电", 0.6, ["doc_1"]),
        _make_concept("PTFE", "material", "光", 0.5, ["doc_2"]),
    ]
    evidence_edges = [
        _make_evidence_edge("GB200", "belongs_to", "算"),
        _make_evidence_edge("LPO", "belongs_to", "光"),
        _make_evidence_edge("GB200", "co_occurs_with", "PCB"),
    ]
    causal_chains = [
        _make_causal_chain("GB200", "PCB"),
        _make_causal_chain("LPO", "光模块"),
    ]
    concept_scores = [
        {"concept_name": "GB200", "final_score": 0.8},
        {"concept_name": "LPO", "final_score": 0.7},
    ]

    (tmp_path / "candidate_concepts_latest.json").write_text(
        json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "evidence_edges_latest.json").write_text(
        json.dumps(evidence_edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "causal_chains_latest.json").write_text(
        json.dumps(causal_chains, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "concept_scores_latest.json").write_text(
        json.dumps(concept_scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


# ============================================================================
# 测试类
# ============================================================================


class TestNodeGeneration:
    """测试节点生成。"""

    def test_gb200_node_generated(self):
        """SubTask 12.1: GB200 节点能生成。"""
        concepts = [_make_concept("GB200", "product_platform", "算", 0.8)]
        nodes = build_nodes(concepts)
        names = [n["node_name"] for n in nodes]
        assert "GB200" in names, f"应生成 GB200 节点，实际: {names}"
        gb200 = next(n for n in nodes if n["node_name"] == "GB200")
        assert gb200["node_type"] == "product_platform"

    def test_lpo_node_generated(self):
        """SubTask 12.2: LPO 节点能生成。"""
        concepts = [_make_concept("LPO", "technology_route", "光", 0.7)]
        nodes = build_nodes(concepts)
        names = [n["node_name"] for n in nodes]
        assert "LPO" in names, f"应生成 LPO 节点，实际: {names}"
        lpo = next(n for n in nodes if n["node_name"] == "LPO")
        assert lpo["node_type"] == "technology_route"


class TestMissingEdges:
    """测试缺失边检测。"""

    def test_gb200_missing_pcb_edge(self):
        """SubTask 12.3: GB200 缺少 PCB 下钻边时能生成 missing_edge。"""
        concepts = [_make_concept("GB200", "product_platform", "算", 0.8)]
        nodes = build_nodes(concepts)
        # 不提供 GB200→PCB 的边
        edges: List[Dict[str, Any]] = []
        missing = detect_missing_edges(nodes, edges)
        # GB200 是 product_platform，应检测 PCB 下钻边
        targets = [m["target_hint"] for m in missing if m["source_node"] == "GB200"]
        assert "PCB" in targets, f"GB200 应缺少 PCB 下钻边，实际 targets: {targets}"

    def test_lpo_missing_optical_module_edge(self):
        """SubTask 12.4: LPO 缺少光模块/光芯片下钻边时能生成 missing_edge。"""
        concepts = [_make_concept("LPO", "technology_route", "光", 0.7)]
        nodes = build_nodes(concepts)
        edges: List[Dict[str, Any]] = []
        missing = detect_missing_edges(nodes, edges)
        targets = [m["target_hint"] for m in missing if m["source_node"] == "LPO"]
        assert "光模块" in targets, f"LPO 应缺少光模块下钻边，实际: {targets}"
        assert "光芯片" in targets, f"LPO 应缺少光芯片下钻边，实际: {targets}"


class TestSearchTasks:
    """测试搜索任务编译。"""

    def test_missing_edge_generates_search_task(self):
        """SubTask 12.5: missing_edge 能生成 search_task。"""
        missing_edges = [{
            "missing_edge_id": "missing_1",
            "source_node": "GB200",
            "source_type": "product_platform",
            "target_hint": "PCB",
            "reason": "测试",
            "priority_score": 0.8,
            "status": "pending",
        }]
        tasks = compile_search_tasks(missing_edges)
        assert len(tasks) >= 3, f"每条 missing_edge 应生成 3-5 条 query，实际: {len(tasks)}"
        for task in tasks:
            assert task["related_node"] == "GB200"
            assert task["related_missing_edge_id"] == "missing_1"

    def test_search_task_has_source_and_generated_by(self):
        """SubTask 12.10: search_task 含 task_source / generated_by 字段。"""
        missing_edges = [{
            "missing_edge_id": "missing_1",
            "source_node": "GB200",
            "source_type": "product_platform",
            "target_hint": "PCB",
            "reason": "测试",
            "priority_score": 0.8,
            "status": "pending",
        }]
        tasks = compile_search_tasks(missing_edges)
        for task in tasks:
            assert task["task_source"] == "missing_edge", "task_source 应为 missing_edge"
            assert task["generated_by"] == "industry_chain_graph_builder", (
                "generated_by 应为 industry_chain_graph_builder"
            )


class TestForbiddenRelations:
    """测试禁止的关系。"""

    def test_no_supplies_relation(self):
        """SubTask 12.6: 不允许生成 supplies。"""
        concepts = [_make_concept("GB200", "product_platform", "算")]
        # 提供一个 supplies 边，应被过滤
        evidence_edges = [_make_evidence_edge("GB200", "supplies", "PCB")]
        causal_chains: List[Dict[str, Any]] = []
        edges = build_allowed_edges(concepts, evidence_edges, causal_chains)
        relations = [e["relation"] for e in edges]
        assert "supplies" not in relations, f"不应生成 supplies 关系，实际: {relations}"

    def test_no_confirmed_supplier_relation(self):
        """SubTask 12.7: 不允许生成 confirmed_supplier。"""
        concepts = [_make_concept("GB200", "product_platform", "算")]
        evidence_edges = [_make_evidence_edge("GB200", "confirmed_supplier", "公司A")]
        causal_chains: List[Dict[str, Any]] = []
        edges = build_allowed_edges(concepts, evidence_edges, causal_chains)
        relations = [e["relation"] for e in edges]
        assert "confirmed_supplier" not in relations, (
            f"不应生成 confirmed_supplier 关系，实际: {relations}"
        )


class TestConfidenceLevel:
    """测试 confidence_level 约束。"""

    def test_no_probable_or_verified_in_edges(self):
        """SubTask 12.9: confidence_level 不含 probable / verified。"""
        concepts = [_make_concept("GB200", "product_platform", "算")]
        # 提供一个 probable 边，应被降级为 hypothesis
        evidence_edges = [
            _make_evidence_edge("GB200", "belongs_to", "算", "probable"),
            _make_evidence_edge("GB200", "co_occurs_with", "PCB", "verified"),
        ]
        causal_chains: List[Dict[str, Any]] = []
        edges = build_allowed_edges(concepts, evidence_edges, causal_chains)
        for edge in edges:
            assert edge["confidence_level"] not in ("probable", "verified"), (
                f"confidence_level 不应为 probable/verified，"
                f"实际: {edge['confidence_level']}"
            )


class TestJsonGeneration:
    """测试 JSON 文件生成。"""

    def test_industry_chain_graph_json_generated(self, tmp_path: Path):
        """SubTask 12.8: industry_chain_graph_latest.json 能生成（在 tmp_path 下）。"""
        _setup_p3_data(tmp_path)
        summary = run_industry_chain_graph(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"], f"运行失败: {summary.get('error')}"
        assert (tmp_path / "industry_chain_graph_latest.json").exists()
        assert (tmp_path / "missing_edges_latest.json").exists()
        assert (tmp_path / "edge_validation_history_latest.json").exists()

    def test_search_tasks_file_name(self, tmp_path: Path):
        """SubTask 12.10: search_tasks 输出到 search_tasks_from_missing_edges_latest.json。"""
        _setup_p3_data(tmp_path)
        # 先运行图谱构建
        graph_summary = run_industry_chain_graph(input_dir=tmp_path, output_dir=tmp_path)
        assert graph_summary["success"]
        # 再运行搜索任务编译
        task_summary = run_search_task_compiler(input_dir=tmp_path, output_dir=tmp_path)
        assert task_summary["success"]
        # 验证文件名
        assert (tmp_path / "search_tasks_from_missing_edges_latest.json").exists()
        # 验证不直接写 search_tasks_latest.json
        # （P0 可能已存在 search_tasks_latest.json，但 P3 不应覆盖它）

    def test_does_not_touch_real_data_dir(self, tmp_path: Path):
        """测试不触碰真实 data/reports/perilla_discovery/ 目录。"""
        _setup_p3_data(tmp_path)
        summary = run_industry_chain_graph(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"]
        for path in summary["outputs"]:
            assert str(tmp_path) in path, f"输出应指向 tmp_path: {path}"
