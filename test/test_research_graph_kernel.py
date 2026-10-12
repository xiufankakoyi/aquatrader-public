from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import Evidence, EvidenceLedger, GraphQuery, GraphStore, HyperEdge, HyperNode
from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeType, RelationType
from core.research_graph.seed_graph import SeedGraphBuilder, build_hbm_golden_seed


def _node(node_id: str, node_type: str, name: str) -> HyperNode:
    return HyperNode(node_id=node_id, type=node_type, name=name)


def _build_hbm_kernel_graph() -> tuple[GraphStore, EvidenceLedger]:
    store = GraphStore()
    ledger = EvidenceLedger(store)
    for node in (
        _node("theme:hbm", NodeType.THEME.value, "HBM"),
        _node("product:tsv", NodeType.PRODUCT.value, "TSV"),
        _node("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
        _node("material:plating_solution", NodeType.MATERIAL.value, "电镀液"),
        _node("company:A", NodeType.COMPANY.value, "公司A"),
    ):
        store.add_node(node)

    for edge in (
        HyperEdge(
            edge_id="edge_seed_hbm_tsv",
            relation=RelationType.PART_OF.value,
            subjects=["product:tsv"],
            objects=["theme:hbm"],
            source="seed_graph",
        ),
        HyperEdge(
            edge_id="edge_seed_tsv_plating",
            relation=RelationType.REQUIRES.value,
            subjects=["product:tsv"],
            objects=["process:tsv_cu_plating"],
            source="seed_graph",
        ),
        HyperEdge(
            edge_id="edge_seed_plating_solution",
            relation=RelationType.REQUIRES.value,
            subjects=["process:tsv_cu_plating"],
            objects=["material:plating_solution"],
            source="seed_graph",
        ),
    ):
        stored, _ = store.add_edge(edge, operator="seed_graph")
        assert stored.status == EdgeStatus.UNVERIFIED_SEED.value

    return store, ledger


def test_acceptance_1_coarse_company_theme_edge_cannot_enter_active() -> None:
    store, _ = _build_hbm_kernel_graph()

    for relation in (
        RelationType.BENEFITS_FROM.value,
        RelationType.PRODUCES.value,
        RelationType.SUPPLIES_TO.value,
        RelationType.VALIDATED_BY.value,
    ):
        coarse_edge, result = store.add_edge(
            HyperEdge(
                edge_id=f"edge_company_a_hbm_coarse_{relation.lower()}",
                relation=relation,
                subjects=["company:A"],
                objects=["theme:hbm"],
                qualifiers={"exposure_type": "direct"},
            )
        )

        assert coarse_edge.status == EdgeStatus.CANDIDATE.value
        assert coarse_edge.status != EdgeStatus.ACTIVE.value
        assert "company_theme_requires_specific_path" in result.messages

    mention_edge, mention_result = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_hbm_mention",
            relation=RelationType.MENTIONS.value,
            subjects=["company:A"],
            objects=["theme:hbm"],
        )
    )
    assert mention_edge.status == EdgeStatus.CANDIDATE.value
    assert "缺少具体工艺/材料/设备/产品节点" not in "；".join(mention_result.messages)


def test_acceptance_2_and_6_evidence_promotes_specific_relation() -> None:
    store, ledger = _build_hbm_kernel_graph()

    edge, result = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_plating_benefit",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["process:tsv_cu_plating"],
            qualifiers={"exposure_type": "indirect", "chain_position": "material_supplier"},
        )
    )
    assert edge.status == EdgeStatus.CANDIDATE.value
    assert "active_requires_evidence_id" in result.messages

    ledger.support_edge(
        edge.edge_id,
        Evidence(
            evidence_id="ev_annual_report_plating",
            source_type="annual_report",
            source_title="公司A 2025年年度报告",
            source_id="src_annual_report_plating",
            publish_date="2026-04-20",
            quote="公司先进封装电镀液产品已用于客户验证。",
            summary="该证据支持公司A存在先进封装电镀液业务。",
            evidence_level=EvidenceLevel.PROVEN.value,
        ),
    )

    promoted = store.edges[edge.edge_id]
    assert promoted.status == EdgeStatus.ACTIVE.value
    assert promoted.confidence in {EvidenceLevel.LIKELY.value, EvidenceLevel.PROVEN.value}
    assert promoted.evidence_ids == ["ev_annual_report_plating"]
    assert store.edge_mutation_history(edge.edge_id)


def test_acceptance_3_and_4_counter_evidence_marks_disproven_or_conflicted() -> None:
    store, ledger = _build_hbm_kernel_graph()
    store.add_node(_node("product:hbm_stack", NodeType.PRODUCT.value, "HBM堆叠"))

    direct_edge, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_direct_hbm_supplier",
            relation=RelationType.SUPPLIES_TO.value,
            subjects=["company:A"],
            objects=["product:hbm_stack"],
            qualifiers={"exposure_type": "direct"},
        )
    )
    ledger.support_edge(
        direct_edge.edge_id,
        Evidence(
            evidence_id="ev_report_claim_supplier",
            source_type="research_report",
            source_title="产业研报",
            source_id="src_report_claim_supplier",
            quote="研报称公司A是HBM供应商。",
            summary="单一研报声称公司A直接供应HBM。",
            evidence_level=EvidenceLevel.LIKELY.value,
        ),
    )
    ledger.contradict_edge(
        direct_edge.edge_id,
        Evidence(
            evidence_id="ev_company_denies_direct_hbm",
            source_type="announcement",
            source_title="公司互动回复",
            source_id="src_company_denies_direct_hbm",
            quote="公司回复称暂无 HBM 直接客户。",
            summary="该证据反驳公司A直接供应HBM。",
            evidence_level=EvidenceLevel.DISPROVEN.value,
        ),
    )
    assert store.edges[direct_edge.edge_id].status == EdgeStatus.DISPROVEN.value
    assert store.edges[direct_edge.edge_id].confidence == EvidenceLevel.DISPROVEN.value

    indirect_edge, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_plating_indirect",
            relation=RelationType.BENEFITS_FROM.value,
            subjects=["company:A"],
            objects=["process:tsv_cu_plating"],
            qualifiers={"exposure_type": "indirect"},
        )
    )
    ledger.support_edge(
        indirect_edge.edge_id,
        Evidence(
            evidence_id="ev_annual_report_indirect",
            source_type="annual_report",
            source_title="公司A 年报",
            source_id="src_annual_report_indirect",
            quote="公司先进封装电镀液产品已形成销售。",
            summary="该证据支持间接受益 TSV 铜电镀填充路径。",
            evidence_level=EvidenceLevel.PROVEN.value,
        ),
    )
    ledger.contradict_edge(
        indirect_edge.edge_id,
        Evidence(
            evidence_id="ev_low_revenue_exposure",
            source_type="announcement",
            source_title="公司公告",
            source_id="src_low_revenue_exposure",
            quote="相关业务收入占比极低。",
            summary="该证据削弱间接受益路径强度。",
            evidence_level=EvidenceLevel.DISPROVEN.value,
        ),
    )
    assert store.edges[indirect_edge.edge_id].status == EdgeStatus.CONFLICTED.value
    assert store.edges[indirect_edge.edge_id].counter_evidence_ids == ["ev_low_revenue_exposure"]
    assert any(
        item["action"] == "MARK_CONFLICT"
        for item in store.edge_mutation_history(indirect_edge.edge_id)
    )


def test_acceptance_5_and_8_path_query_returns_evidence_and_mutation_history() -> None:
    store, ledger = _build_hbm_kernel_graph()
    for edge_id, relation, subjects, objects in (
        ("edge_verified_hbm_tsv", RelationType.PART_OF.value, ["product:tsv"], ["theme:hbm"]),
        ("edge_verified_tsv_plating", RelationType.REQUIRES.value, ["product:tsv"], ["process:tsv_cu_plating"]),
        ("edge_verified_plating_solution", RelationType.REQUIRES.value, ["process:tsv_cu_plating"], ["material:plating_solution"]),
    ):
        store.add_edge(
            HyperEdge(
                edge_id=edge_id,
                relation=relation,
                subjects=subjects,
                objects=objects,
            )
        )
        ledger.support_edge(
            edge_id,
            Evidence(
                evidence_id=f"ev_{edge_id}",
                source_type="industry_report",
                source_title=f"{edge_id} 产业链资料",
                source_id=f"src_{edge_id}",
                publish_date="2026-01-01",
                quote="产业链资料支持该层级关系。",
                summary="该证据将种子假设升级为可复核链路边。",
                evidence_level=EvidenceLevel.LIKELY.value,
            ),
        )
    edge, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_company_a_produces_plating_solution",
            relation=RelationType.PRODUCES.value,
            subjects=["company:A"],
            objects=["material:plating_solution"],
            qualifiers={"time_scope": "2026Q2", "chain_position": "material_supplier"},
        )
    )
    ledger.support_edge(
        edge.edge_id,
        Evidence(
            evidence_id="ev_annual_report_solution",
            source_type="annual_report",
            source_title="公司A 2025年年度报告",
            source_id="src_annual_report_solution",
            publish_date="2026-04-20",
            quote="公司先进封装电镀液产品已用于客户验证。",
            summary="该证据支持公司A生产先进封装电镀液。",
            evidence_level=EvidenceLevel.PROVEN.value,
        ),
    )

    query = GraphQuery(store)
    paths = query.benefit_paths("theme:hbm", "company:A")
    assert paths
    rendered = paths[0]
    assert [node["name"] for node in rendered["nodes"]] == [
        "HBM",
        "TSV",
        "TSV铜电镀填充",
        "电镀液",
        "公司A",
    ]
    assert all(edge["status"] == EdgeStatus.ACTIVE.value for edge in rendered["edges"])
    assert all(edge["evidence_ids"] for edge in rendered["edges"])
    assert rendered["edges"][-1]["confidence"] == EvidenceLevel.PROVEN.value
    assert rendered["edges"][-1]["evidence_ids"] == ["ev_annual_report_solution"]

    target_query = query.related_targets("theme:hbm")
    assert target_query["result_type"] == "benefit_paths"
    assert target_query["mode"] == "verified"
    assert "companies" not in target_query
    assert target_query["paths"]

    ledger.contradict_edge(
        edge.edge_id,
        Evidence(
            evidence_id="ev_customer_unknown",
            source_type="news",
            source_title="客户信息核验",
            source_id="src_customer_unknown",
            quote="客户名称未披露。",
            summary="该证据提示客户不明，需要继续验证。",
            evidence_level=EvidenceLevel.SPECULATIVE.value,
        ),
    )
    assert query.benefit_paths("theme:hbm", "company:A") == []

    audit_paths = query.benefit_paths(
        "theme:hbm",
        "company:A",
        include_audit_edges=True,
    )
    assert audit_paths
    audit_rendered = audit_paths[0]
    assert audit_rendered["edges"][-1]["confidence"] == EvidenceLevel.CONFLICTED.value
    assert audit_rendered["edges"][-1]["status"] == EdgeStatus.CONFLICTED.value
    assert audit_rendered["edges"][-1]["evidence_ids"] == ["ev_annual_report_solution"]
    assert audit_rendered["edges"][-1]["counter_evidence_ids"] == ["ev_customer_unknown"]
    assert audit_rendered["edges"][-1]["mutation_history"]

    audit_target_query = query.related_targets("theme:hbm", include_audit_edges=True)
    assert audit_target_query["result_type"] == "audit_benefit_paths"
    assert audit_target_query["mode"] == "audit"
    assert "companies" not in audit_target_query
    assert audit_target_query["paths"]


def test_hyperedge_dimension_fields_are_first_class_and_backward_compatible() -> None:
    edge = HyperEdge(
        edge_id="edge_dimension_backfill",
        relation=RelationType.REQUIRES.value,
        subjects=["a"],
        objects=["b"],
        qualifiers={"dimension": "合成树/工艺树", "route_layer": "process", "time_stage": "2026Q1"},
    )

    assert edge.dimensions == ["合成树/工艺树"]
    assert edge.primary_dimension == "合成树/工艺树"
    assert edge.route_layer == "process"
    assert edge.time_stage == "2026Q1"

    restored = HyperEdge.from_dict({
        "edge_id": "edge_old_json",
        "relation": RelationType.REQUIRES.value,
        "subjects": ["a"],
        "objects": ["b"],
        "qualifiers": {"dimension": "产业链流"},
    })
    assert restored.dimensions == ["产业链流"]
    assert restored.primary_dimension == "产业链流"


def test_seed_graph_writes_dimension_as_first_class_edge_fields() -> None:
    store = GraphStore()
    summary = SeedGraphBuilder(store).build(build_hbm_golden_seed())

    assert summary["edges"]
    edge = store.edges[summary["edges"][0]["edge_id"]]
    assert edge.qualifiers["dimension"]
    assert edge.dimensions == [edge.qualifiers["dimension"]]
    assert edge.primary_dimension == edge.qualifiers["dimension"]
    assert edge.route_layer == edge.qualifiers["dimension"]


def test_graph_query_filters_and_projects_by_first_class_dimension() -> None:
    store, ledger = _build_hbm_kernel_graph()
    store.add_edge(
        HyperEdge(
            edge_id="edge_dim_hbm_tsv",
            relation=RelationType.PART_OF.value,
            subjects=["product:tsv"],
            objects=["theme:hbm"],
            dimensions=["产业链流"],
            primary_dimension="产业链流",
            route_layer="industry",
            time_stage="2026",
        )
    )
    ledger.support_edge(
        "edge_dim_hbm_tsv",
        Evidence(
            evidence_id="ev_edge_dim_hbm_tsv",
            source_type="industry_report",
            source_title="产业链资料",
            source_id="src_edge_dim_hbm_tsv",
            quote="产业链资料支持该层级关系。",
            evidence_level=EvidenceLevel.LIKELY.value,
        ),
    )
    store.add_edge(
        HyperEdge(
            edge_id="edge_dim_tsv_plating",
            relation=RelationType.REQUIRES.value,
            subjects=["product:tsv"],
            objects=["process:tsv_cu_plating"],
            dimensions=["合成树/工艺树"],
            primary_dimension="合成树/工艺树",
            route_layer="process",
            time_stage="2026",
        )
    )
    ledger.support_edge(
        "edge_dim_tsv_plating",
        Evidence(
            evidence_id="ev_edge_dim_tsv_plating",
            source_type="industry_report",
            source_title="工艺资料",
            source_id="src_edge_dim_tsv_plating",
            quote="工艺资料支持该层级关系。",
            evidence_level=EvidenceLevel.LIKELY.value,
        ),
    )

    query = GraphQuery(store)
    industry_projection = query.dimension_projection("产业链流")
    assert [edge["edge_id"] for edge in industry_projection["edges"]] == ["edge_dim_hbm_tsv"]
    assert industry_projection["mode"] == "verified"

    process_paths = query.find_paths(
        "theme:hbm",
        "process:tsv_cu_plating",
        include_audit_edges=True,
        dimension="合成树/工艺树",
    )
    assert process_paths == []

    all_dimension_paths = query.find_paths(
        "theme:hbm",
        "process:tsv_cu_plating",
        include_audit_edges=True,
    )
    assert all_dimension_paths
    rendered_edge = all_dimension_paths[0]["edges"][0]
    assert "dimensions" in rendered_edge
    assert "primary_dimension" in rendered_edge


def test_default_query_hides_unverified_seed_candidate_and_conflicted_edges() -> None:
    store, ledger = _build_hbm_kernel_graph()
    store.add_node(_node("company:B", NodeType.COMPANY.value, "公司B"))
    store.add_edge(
        HyperEdge(
            edge_id="edge_candidate_company_b",
            relation=RelationType.PRODUCES.value,
            subjects=["company:B"],
            objects=["material:plating_solution"],
        )
    )
    edge, _ = store.add_edge(
        HyperEdge(
            edge_id="edge_conflicted_company_a",
            relation=RelationType.PRODUCES.value,
            subjects=["company:A"],
            objects=["material:plating_solution"],
            evidence_ids=["ev_conflict_support"],
            counter_evidence_ids=["ev_conflict_counter"],
        )
    )
    store.add_evidence(
        Evidence(
            evidence_id="ev_conflict_support",
            source_type="annual_report",
            source_title="公司A 年报",
            source_id="src_conflict_support",
            quote="公司先进封装电镀液产品已形成销售。",
            summary="支持。",
            evidence_level=EvidenceLevel.PROVEN.value,
            supports=[edge.edge_id],
        )
    )
    ledger.contradict_edge(
        edge.edge_id,
        Evidence(
            evidence_id="ev_conflict_counter",
            source_type="announcement",
            source_title="公司A 公告",
            source_id="src_conflict_counter",
            quote="相关业务收入占比极低。",
            summary="反证。",
            evidence_level=EvidenceLevel.DISPROVEN.value,
        ),
    )

    query = GraphQuery(store)
    assert query.related_targets("theme:hbm")["paths"] == []

    audit = query.related_targets("theme:hbm", include_audit_edges=True)
    assert audit["paths"]
    edge_statuses = {
        edge["status"]
        for path in audit["paths"]
        for edge in path["edges"]
    }
    assert EdgeStatus.UNVERIFIED_SEED.value in edge_statuses
    assert EdgeStatus.CANDIDATE.value in edge_statuses or EdgeStatus.CONFLICTED.value in edge_statuses
