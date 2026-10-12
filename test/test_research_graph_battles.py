from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import (
    AlphaPathRanker,
    Claim,
    Evidence,
    EvidenceMiner,
    GraphStore,
    GraphQuery,
    GrowthAgent,
    HyperEdge,
    HyperNode,
    IndustryChainSeed,
    ResearchRunManager,
    SeedEdgeSpec,
    SeedGraphBuilder,
    SeedNodeSpec,
    SourceDocument,
)
from core.research_graph.schema import (
    EdgeStatus,
    EvidenceLevel,
    HYPERGRAPH_DIMENSIONS,
    NodeType,
    RESEARCH_BATTLES,
    RelationType,
)


def test_battle_plan_and_seven_dimensions_are_fixed() -> None:
    assert RESEARCH_BATTLES == (
        "第一战役：HyperGraph Kernel",
        "第二战役：Seed Graph Builder",
        "第三战役：Evidence Miner",
        "第四战役：Graph Verifier",
        "第五战役：Growth Agent",
        "第六战役：Alpha Path Ranker",
        "第七战役：ResearchRunManager",
    )
    assert HYPERGRAPH_DIMENSIONS == (
        "产业链流",
        "替代分支",
        "合成树/工艺树",
        "公司股权链",
        "时间阶段",
        "瓶颈约束",
        "证据维度",
    )


def test_second_battle_seed_graph_builder_marks_everything_unverified_seed() -> None:
    store = GraphStore()
    seed = IndustryChainSeed(
        name="HBM TSV seed",
        nodes=[
            SeedNodeSpec("theme:hbm", NodeType.THEME.value, "HBM"),
            SeedNodeSpec("product:tsv", NodeType.PRODUCT.value, "TSV"),
            SeedNodeSpec("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
            SeedNodeSpec("material:plating_solution", NodeType.MATERIAL.value, "电镀液"),
        ],
        edges=[
            SeedEdgeSpec(
                "seed_hbm_tsv",
                RelationType.PART_OF.value,
                ["product:tsv"],
                ["theme:hbm"],
                "产业链流",
            ),
            SeedEdgeSpec(
                "seed_tsv_plating",
                RelationType.REQUIRES.value,
                ["product:tsv"],
                ["process:tsv_cu_plating"],
                "合成树/工艺树",
            ),
            SeedEdgeSpec(
                "seed_plating_solution",
                RelationType.REQUIRES.value,
                ["process:tsv_cu_plating"],
                ["material:plating_solution"],
                "瓶颈约束",
                {"constraint_reason": "待验证材料卡点"},
            ),
        ],
    )

    result = SeedGraphBuilder(store).build(seed)

    assert result["invariant"] == "Seed Graph 不是事实，只是待验证假设"
    assert set(result["added_nodes"]) == set(store.nodes)
    assert set(result["added_edges"]) == set(store.edges)
    assert all(node.status == EdgeStatus.UNVERIFIED_SEED.value for node in store.nodes.values())
    assert all(edge.status == EdgeStatus.UNVERIFIED_SEED.value for edge in store.edges.values())
    assert all(edge.evidence_ids == [] for edge in store.edges.values())
    assert store.edges["seed_tsv_plating"].qualifiers["dimension"] == "合成树/工艺树"


def test_third_battle_evidence_miner_extracts_support_counter_and_updates_graph() -> None:
    store = GraphStore()
    for node in (
        HyperNode("company:A", NodeType.COMPANY.value, "公司A"),
        HyperNode("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
    ):
        store.add_node(node)
    edge, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_tsv_plating",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["process:tsv_cu_plating"],
            qualifiers={"exposure_type": "indirect"},
        )
    )

    miner = EvidenceMiner()
    result = miner.mine(
        Claim(
            claim_id="claim_a_tsv_plating",
            edge_id=edge.edge_id,
            claim_text="公司A 受益 TSV铜电镀填充",
            support_terms=["先进封装电镀液", "TSV铜电镀填充"],
            counter_terms=["收入占比极低"],
        ),
        [
            SourceDocument(
                document_id="annual_report",
                source_type="annual_report",
                title="公司A 2025年年度报告",
                content="公司先进封装电镀液产品已用于客户验证。",
                publish_date="2026-04-20",
            ),
            SourceDocument(
                document_id="risk_notice",
                source_type="announcement",
                title="公司A 风险提示公告",
                content="公司提示相关业务收入占比极低。",
                publish_date="2026-05-01",
            ),
        ],
    )

    assert result.should_update_graph is True
    assert len(result.support_evidence) == 1
    assert len(result.counter_evidence) == 1
    assert result.suggested_confidence == EvidenceLevel.CONFLICTED.value

    miner.apply_to_store(store, result)
    updated = store.edges[edge.edge_id]
    assert updated.status == EdgeStatus.CONFLICTED.value
    assert updated.evidence_ids == [result.support_evidence[0].evidence_id]
    assert updated.counter_evidence_ids == [result.counter_evidence[0].evidence_id]


def test_fourth_battle_graph_verifier_audit_reports_blocked_and_conflicted_edges() -> None:
    store = GraphStore()
    for node in (
        HyperNode("theme:hbm", NodeType.THEME.value, "HBM"),
        HyperNode("company:A", NodeType.COMPANY.value, "公司A"),
        HyperNode("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
    ):
        store.add_node(node)
    coarse, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_coarse_company_theme",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["theme:hbm"],
        )
    )
    specific, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_specific_company_process",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["process:tsv_cu_plating"],
            qualifiers={"exposure_type": "indirect"},
        )
    )
    miner = EvidenceMiner()
    result = miner.mine(
        Claim(
            claim_id="claim_conflict",
            edge_id=specific.edge_id,
            claim_text="公司A 受益 TSV铜电镀填充",
            support_terms=["先进封装电镀液"],
            counter_terms=["收入占比极低"],
        ),
        [
            SourceDocument(
                "report",
                "annual_report",
                "公司A 年报",
                "公司先进封装电镀液产品已形成销售。",
            ),
            SourceDocument(
                "notice",
                "announcement",
                "公司A 公告",
                "相关业务收入占比极低。",
            ),
        ],
    )
    miner.apply_to_store(store, result)

    audit = store.verifier.audit_store(store.nodes, store.edges, store.evidence)

    assert coarse.edge_id in audit["blocked_edges"]
    assert specific.edge_id in audit["conflicted_edges"]
    assert audit["edge_results"][coarse.edge_id]["status"] == EdgeStatus.CANDIDATE.value
    assert "reject_coarse_company_theme_edges" in audit["rule_summary"]
    assert "require_specific_path_node" in audit["rule_summary"]


def test_fifth_battle_growth_agent_finds_daily_completion_tasks() -> None:
    store = GraphStore()
    for node in (
        HyperNode("theme:hbm", NodeType.THEME.value, "HBM"),
        HyperNode("process:rough_packaging", NodeType.PROCESS.value, "先进封装"),
        HyperNode("company:A", NodeType.COMPANY.value, "公司A"),
        HyperNode("material:plating_solution", NodeType.MATERIAL.value, "电镀液"),
    ):
        store.add_node(node)
    store.add_evidence(
        Evidence(
            evidence_id="ev_stale_weak",
            source_type="forum",
            source_title="互动问答",
            quote="市场讨论称公司A有相关材料。",
            summary="单一弱来源。",
            evidence_level=EvidenceLevel.SPECULATIVE.value,
            supports=["edge_speculative"],
            freshness="stale",
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_speculative",
            relation=RelationType.PRODUCES.value,
            subjects=["company:A"],
            objects=["material:plating_solution"],
            evidence_ids=["ev_stale_weak"],
        )
    )
    store.add_evidence(
        Evidence(
            evidence_id="ev_support",
            source_type="annual_report",
            source_title="公司A 年报",
            quote="公司先进封装电镀液产品已形成销售。",
            summary="支持间接受益路径。",
            evidence_level=EvidenceLevel.PROVEN.value,
            supports=["edge_conflict"],
        )
    )
    store.add_evidence(
        Evidence(
            evidence_id="ev_counter",
            source_type="announcement",
            source_title="公司A 公告",
            quote="相关业务收入占比极低。",
            summary="反证。",
            evidence_level=EvidenceLevel.DISPROVEN.value,
            contradicts=["edge_conflict"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_conflict",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["process:rough_packaging"],
            qualifiers={"exposure_type": "indirect"},
            evidence_ids=["ev_support"],
            counter_evidence_ids=["ev_counter"],
        )
    )

    tasks = GrowthAgent(store).discover_tasks()
    issue_types = {task.issue_type for task in tasks}

    assert "节点太粗" in issue_types
    assert "缺少材料级拆解" in issue_types
    assert "缺少 A 股映射" in issue_types
    assert "缺少股权穿透" in issue_types
    assert "只有 SPECULATIVE 证据" in issue_types
    assert "证据过期" in issue_types
    assert "存在冲突证据" in issue_types


def test_sixth_battle_alpha_path_ranker_scores_paths_not_naked_stocks() -> None:
    store = GraphStore()
    for node in (
        HyperNode("theme:hbm", NodeType.THEME.value, "HBM"),
        HyperNode("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
        HyperNode("material:plating_solution", NodeType.MATERIAL.value, "电镀液"),
        HyperNode("company:A", NodeType.COMPANY.value, "公司A", attributes={"ticker": "300001.SZ"}),
    ):
        store.add_node(node)
    store.add_edge(
        HyperEdge(
            edge_id="edge_theme_process",
            relation=RelationType.PART_OF.value,
            subjects=["process:tsv_cu_plating"],
            objects=["theme:hbm"],
        )
    )
    store.add_evidence(
        Evidence(
            evidence_id="ev_theme_process",
            source_type="industry_report",
            source_title="HBM TSV 产业链资料",
            quote="TSV 是 HBM 先进封装关键环节。",
            summary="支持 HBM 到 TSV 铜电镀填充工艺路径。",
            evidence_level=EvidenceLevel.LIKELY.value,
            supports=["edge_theme_process"],
        )
    )
    store.attach_evidence("edge_theme_process", "ev_theme_process")
    store.add_edge(
        HyperEdge(
            edge_id="edge_process_material",
            relation=RelationType.REQUIRES.value,
            subjects=["process:tsv_cu_plating"],
            objects=["material:plating_solution"],
            qualifiers={"constraint_reason": "电镀液客户验证周期长"},
        )
    )
    store.add_evidence(
        Evidence(
            evidence_id="ev_process_material",
            source_type="industry_report",
            source_title="TSV 电镀液资料",
            quote="TSV 铜填充需要电镀液材料配套。",
            summary="支持工艺到材料路径。",
            evidence_level=EvidenceLevel.LIKELY.value,
            supports=["edge_process_material"],
        )
    )
    store.attach_evidence("edge_process_material", "ev_process_material")
    store.add_evidence(
        Evidence(
            evidence_id="ev_company_material",
            source_type="annual_report",
            source_title="公司A 年报",
            quote="公司先进封装电镀液产品已用于客户验证。",
            summary="支持公司A生产电镀液。",
            evidence_level=EvidenceLevel.PROVEN.value,
            supports=["edge_company_material"],
        )
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_company_material",
            relation=RelationType.PRODUCES.value,
            subjects=["company:A"],
            objects=["material:plating_solution"],
            evidence_ids=["ev_company_material"],
        )
    )

    paths = GraphQuery(store).benefit_paths("theme:hbm", "company:A")
    ranked = AlphaPathRanker().rank(paths)

    assert ranked["result_type"] == "ranked_benefit_paths"
    assert ranked["ranking_object"] == "主题 → 瓶颈 → 工艺/材料/设备 → 公司 → 证据"
    assert "companies" not in ranked
    assert ranked["paths"][0]["score"] > 0
    assert ranked["paths"][0]["score_breakdown"]["bottleneck"] == 1.0

    unsafe_paths = [
        {
            "nodes": [],
            "edges": [
                {"status": EdgeStatus.CANDIDATE.value, "evidence_ids": ["ev_x"]},
            ],
        },
        {
            "nodes": [],
            "edges": [
                {"status": EdgeStatus.UNVERIFIED_SEED.value, "evidence_ids": ["ev_x"]},
            ],
        },
        {
            "nodes": [],
            "edges": [
                {"status": EdgeStatus.CONFLICTED.value, "evidence_ids": ["ev_x"]},
            ],
        },
        {
            "nodes": [],
            "edges": [
                {"status": EdgeStatus.ACTIVE.value, "evidence_ids": []},
            ],
        },
    ]
    unsafe_ranked = AlphaPathRanker().rank(unsafe_paths)
    assert unsafe_ranked["paths"] == []


def test_seventh_battle_research_run_manager_checkpoints_real_steps(tmp_path: Path) -> None:
    checkpoint = tmp_path / "research_run.json"
    calls: list[str] = []

    manager = ResearchRunManager("run_hbm_001", checkpoint)
    manager.register_step(
        "第二战役：Seed Graph Builder",
        lambda: calls.append("seed") or {"added_edges": 3},
    )
    manager.register_step(
        "第三战役：Evidence Miner",
        lambda: calls.append("mine") or {"support_evidence": 1},
    )

    partial = manager.run(stop_after="第二战役：Seed Graph Builder")
    assert partial["progress"] == 0.5
    assert calls == ["seed"]
    assert checkpoint.exists()

    resumed = ResearchRunManager.load(
        checkpoint,
        handlers={
            "第二战役：Seed Graph Builder": lambda: calls.append("seed_again") or {},
            "第三战役：Evidence Miner": lambda: calls.append("mine") or {"support_evidence": 1},
        },
    )
    final_state = resumed.run()

    assert final_state["progress"] == 1.0
    assert calls == ["seed", "mine"]
    assert [step["status"] for step in final_state["steps"]] == ["completed", "completed"]
