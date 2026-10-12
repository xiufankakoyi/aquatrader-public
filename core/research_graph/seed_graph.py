from __future__ import annotations

from dataclasses import dataclass, field

from core.research_graph.models import HyperEdge, HyperNode
from core.research_graph.schema import (
    EdgeStatus,
    EvidenceLevel,
    HyperGraphDimension,
    NodeSource,
    NodeType,
    RelationType,
)
from core.research_graph.store import GraphStore


SEED_NOTICE = "Seed Graph 不是事实，只是待验证假设"


def add_seed_node(store: GraphStore, node: HyperNode) -> HyperNode:
    node.source = NodeSource.SEED_GRAPH.value
    node.status = EdgeStatus.UNVERIFIED_SEED.value
    return store.add_node(node, operator=NodeSource.SEED_GRAPH.value)


def add_seed_edge(store: GraphStore, edge: HyperEdge) -> HyperEdge:
    edge.source = NodeSource.SEED_GRAPH.value
    edge.status = EdgeStatus.UNVERIFIED_SEED.value
    stored, _ = store.add_edge(edge, operator=NodeSource.SEED_GRAPH.value)
    return stored


@dataclass
class SeedNodeSpec:
    node_id: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass
class SeedEdgeSpec:
    edge_id: str
    relation: str
    subjects: list[str]
    objects: list[str]
    dimension: str
    qualifiers: dict[str, object] = field(default_factory=dict)
    verification_target: str = ""


@dataclass
class IndustryChainSeed:
    name: str
    nodes: list[SeedNodeSpec]
    edges: list[SeedEdgeSpec]
    topic: str = ""
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seed_notice: str = SEED_NOTICE

    @property
    def seed_name(self) -> str:
        return self.name

    @property
    def dimensions_covered(self) -> list[str]:
        return sorted({edge.dimension for edge in self.edges})


class SeedGraphBuilder:
    """Build an initial industry-chain graph as unverifiable hypotheses."""

    allowed_dimensions = {item.value for item in HyperGraphDimension}

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def build(self, seed: IndustryChainSeed) -> dict[str, object]:
        added_nodes: list[str] = []
        added_edges: list[str] = []
        warnings: list[str] = list(seed.warnings)
        topic = seed.topic or seed.name

        for node_spec in seed.nodes:
            attributes = dict(node_spec.attributes)
            attributes.update({
                "seed_topic": topic,
                "seed_notice": seed.seed_notice,
                "needs_verification": True,
                "confidence": EvidenceLevel.SPECULATIVE.value,
            })
            node = HyperNode(
                node_id=node_spec.node_id,
                type=node_spec.type,
                name=node_spec.name,
                aliases=list(node_spec.aliases),
                description=node_spec.description,
                attributes=attributes,
                source=NodeSource.SEED_GRAPH.value,
            )
            add_seed_node(self.store, node)
            added_nodes.append(node.node_id)

        for edge_spec in seed.edges:
            qualifiers = dict(edge_spec.qualifiers)
            if edge_spec.dimension not in self.allowed_dimensions:
                warnings.append(f"{edge_spec.edge_id}: 未知七维超图维度 {edge_spec.dimension}")
            qualifiers["dimension"] = edge_spec.dimension
            qualifiers["seed_notice"] = seed.seed_notice
            qualifiers["needs_verification"] = True
            qualifiers["verification_target"] = (
                edge_spec.verification_target
                or f"{edge_spec.relation}:{','.join(edge_spec.subjects)}->{','.join(edge_spec.objects)}"
            )
            qualifiers["seed_topic"] = topic
            edge = HyperEdge(
                edge_id=edge_spec.edge_id,
                relation=edge_spec.relation,
                subjects=list(edge_spec.subjects),
                objects=list(edge_spec.objects),
                qualifiers=qualifiers,
                dimensions=[edge_spec.dimension],
                primary_dimension=edge_spec.dimension,
                route_layer=edge_spec.dimension,
                time_stage=str(qualifiers.get("time_scope", "")),
                confidence=EvidenceLevel.SPECULATIVE.value,
                evidence_ids=[],
                counter_evidence_ids=[],
                source=NodeSource.SEED_GRAPH.value,
            )
            add_seed_edge(self.store, edge)
            added_edges.append(edge.edge_id)

        return {
            "topic": topic,
            "seed_name": seed.name,
            "nodes": [self.store.nodes[node_id].to_dict() for node_id in added_nodes],
            "edges": [self.store.edges[edge_id].to_dict() for edge_id in added_edges],
            "dimensions_covered": seed.dimensions_covered,
            "assumptions": list(seed.assumptions),
            "warnings": warnings,
            "seed_notice": seed.seed_notice,
            "added_nodes": added_nodes,
            "added_edges": added_edges,
            "invariant": seed.seed_notice,
        }


class ThemeSeedCompiler:
    """Compile a theme keyword into a seven-dimension seed graph."""

    def compile(self, theme: str) -> IndustryChainSeed:
        normalized = " ".join(theme.strip().split()).upper()
        aliases = {
            "HBM": build_hbm_golden_seed,
            "CPO": build_cpo_seed,
            "800V HVDC": build_800v_hvdc_seed,
            "数据中心电力": build_data_center_power_seed,
            "玻璃基板": build_glass_substrate_seed,
            "硅光": build_silicon_photonics_seed,
        }
        builder = aliases.get(normalized) or aliases.get(theme.strip())
        if builder is None:
            return build_generic_theme_seed(theme.strip())
        return builder()


def build_hbm_golden_seed() -> IndustryChainSeed:
    nodes = [
        SeedNodeSpec("theme:hbm", NodeType.THEME.value, "HBM"),
        SeedNodeSpec("product:hbm3e", NodeType.PRODUCT.value, "HBM3E"),
        SeedNodeSpec("product:hbm4", NodeType.PRODUCT.value, "HBM4"),
        SeedNodeSpec("process:dram_die_fabrication", NodeType.PROCESS.value, "DRAM Die 制造"),
        SeedNodeSpec("process:tsv_etching", NodeType.PROCESS.value, "TSV刻蚀"),
        SeedNodeSpec("process:tsv_cu_plating", NodeType.PROCESS.value, "TSV铜电镀填充"),
        SeedNodeSpec("process:temporary_bonding", NodeType.PROCESS.value, "临时键合"),
        SeedNodeSpec("process:wafer_thinning", NodeType.PROCESS.value, "晶圆减薄"),
        SeedNodeSpec("process:micro_bump", NodeType.PROCESS.value, "微凸点"),
        SeedNodeSpec("process:stacked_packaging", NodeType.PROCESS.value, "堆叠封装"),
        SeedNodeSpec("process:test", NodeType.PROCESS.value, "测试"),
        SeedNodeSpec("material:silicon_wafer", NodeType.MATERIAL.value, "硅片"),
        SeedNodeSpec("material:plating_solution", NodeType.MATERIAL.value, "电镀液"),
        SeedNodeSpec("material:package_substrate", NodeType.MATERIAL.value, "封装基板"),
        SeedNodeSpec("material:insulation_barrier", NodeType.MATERIAL.value, "绝缘/阻挡层材料"),
        SeedNodeSpec("material:micro_bump_material", NodeType.MATERIAL.value, "微凸点材料"),
        SeedNodeSpec("material:related_placeholder", NodeType.MATERIAL.value, "相关材料占位"),
        SeedNodeSpec("equipment:etching", NodeType.EQUIPMENT.value, "刻蚀设备"),
        SeedNodeSpec("equipment:tsv_etching", NodeType.EQUIPMENT.value, "TSV 刻蚀设备"),
        SeedNodeSpec("equipment:plating", NodeType.EQUIPMENT.value, "电镀设备"),
        SeedNodeSpec("equipment:bonding", NodeType.EQUIPMENT.value, "键合设备"),
        SeedNodeSpec("equipment:thinning", NodeType.EQUIPMENT.value, "减薄设备"),
        SeedNodeSpec("equipment:inspection", NodeType.EQUIPMENT.value, "检测设备"),
        SeedNodeSpec("equipment:test", NodeType.EQUIPMENT.value, "测试设备"),
        SeedNodeSpec("constraint:advanced_packaging_capacity", NodeType.CONSTRAINT.value, "先进封装产能"),
        SeedNodeSpec("constraint:yield_rate", NodeType.CONSTRAINT.value, "良率"),
        SeedNodeSpec("constraint:customer_validation_cycle", NodeType.CONSTRAINT.value, "客户验证周期"),
        SeedNodeSpec("constraint:equipment_lead_time", NodeType.CONSTRAINT.value, "设备交期"),
        SeedNodeSpec("constraint:material_purity", NodeType.CONSTRAINT.value, "材料纯度"),
        SeedNodeSpec("tech_route:hbm3e", NodeType.TECH_ROUTE.value, "HBM3E 路线"),
        SeedNodeSpec("tech_route:hbm4", NodeType.TECH_ROUTE.value, "HBM4 路线"),
        SeedNodeSpec("tech_route:substitute_placeholder", NodeType.TECH_ROUTE.value, "替代路线占位"),
        SeedNodeSpec("tech_route:bypass_placeholder", NodeType.TECH_ROUTE.value, "替代/旁路路线占位"),
        SeedNodeSpec("supplier:placeholder", NodeType.SUPPLIER.value, "供应商预留位", attributes={"placeholder": True}),
        SeedNodeSpec("company:placeholder", NodeType.COMPANY.value, "公司链预留位", attributes={"placeholder": True}),
        SeedNodeSpec("document:evidence_placeholder", NodeType.DOCUMENT.value, "证据维度占位", attributes={"placeholder": True}),
        SeedNodeSpec("document:annual_report_placeholder", NodeType.DOCUMENT.value, "年报证据占位", attributes={"placeholder": True}),
        SeedNodeSpec("document:announcement_placeholder", NodeType.DOCUMENT.value, "公告证据占位", attributes={"placeholder": True}),
        SeedNodeSpec("document:research_report_placeholder", NodeType.DOCUMENT.value, "研报证据占位", attributes={"placeholder": True}),
        SeedNodeSpec("document:paper_technical_placeholder", NodeType.DOCUMENT.value, "论文/技术资料占位", attributes={"placeholder": True}),
        SeedNodeSpec("event:lab_stage", NodeType.EVENT.value, "实验室阶段", attributes={"placeholder": True}),
        SeedNodeSpec("event:customer_validation", NodeType.EVENT.value, "客户验证阶段", attributes={"placeholder": True}),
        SeedNodeSpec("event:mass_production", NodeType.EVENT.value, "量产阶段", attributes={"placeholder": True}),
        SeedNodeSpec("event:2026_customer_validation", NodeType.EVENT.value, "2026 客户验证阶段", attributes={"placeholder": True}),
    ]

    flow = HyperGraphDimension.INDUSTRY_CHAIN_FLOW.value
    substitute = HyperGraphDimension.SUBSTITUTE_BRANCH.value
    process_tree = HyperGraphDimension.SYNTHESIS_PROCESS_TREE.value
    equity = HyperGraphDimension.EQUITY_OWNERSHIP_CHAIN.value
    time_stage = HyperGraphDimension.TIME_STAGE.value
    bottleneck = HyperGraphDimension.BOTTLENECK_CONSTRAINT.value
    evidence = HyperGraphDimension.EVIDENCE_DISCIPLINE.value

    edges = [
        SeedEdgeSpec("seed_hbm3e_part_of_hbm", RelationType.PART_OF.value, ["product:hbm3e"], ["theme:hbm"], flow),
        SeedEdgeSpec("seed_hbm4_part_of_hbm", RelationType.PART_OF.value, ["product:hbm4"], ["theme:hbm"], flow),
        SeedEdgeSpec("seed_hbm3e_route_mentions_product", RelationType.MENTIONS.value, ["tech_route:hbm3e"], ["product:hbm3e"], flow),
        SeedEdgeSpec("seed_hbm4_route_mentions_product", RelationType.MENTIONS.value, ["tech_route:hbm4"], ["product:hbm4"], flow),
        SeedEdgeSpec("seed_hbm3e_to_hbm4_substitute", RelationType.SUBSTITUTES.value, ["tech_route:hbm4"], ["tech_route:hbm3e"], substitute),
        SeedEdgeSpec("seed_substitute_placeholder", RelationType.SUBSTITUTES.value, ["tech_route:substitute_placeholder"], ["theme:hbm"], substitute),
        SeedEdgeSpec("seed_bypass_placeholder", RelationType.SUBSTITUTES.value, ["tech_route:bypass_placeholder"], ["theme:hbm"], substitute),
        SeedEdgeSpec("seed_hbm_requires_dram_die", RelationType.REQUIRES.value, ["theme:hbm"], ["process:dram_die_fabrication"], process_tree),
        SeedEdgeSpec("seed_dram_die_requires_wafer", RelationType.REQUIRES.value, ["process:dram_die_fabrication"], ["material:silicon_wafer"], process_tree),
        SeedEdgeSpec("seed_dram_die_requires_tsv_etching", RelationType.REQUIRES.value, ["process:dram_die_fabrication"], ["process:tsv_etching"], process_tree),
        SeedEdgeSpec("seed_tsv_etching_requires_plating", RelationType.REQUIRES.value, ["process:tsv_etching"], ["process:tsv_cu_plating"], process_tree),
        SeedEdgeSpec("seed_plating_requires_temp_bonding", RelationType.REQUIRES.value, ["process:tsv_cu_plating"], ["process:temporary_bonding"], process_tree),
        SeedEdgeSpec("seed_temp_bonding_requires_thinning", RelationType.REQUIRES.value, ["process:temporary_bonding"], ["process:wafer_thinning"], process_tree),
        SeedEdgeSpec("seed_thinning_requires_micro_bump", RelationType.REQUIRES.value, ["process:wafer_thinning"], ["process:micro_bump"], process_tree),
        SeedEdgeSpec("seed_micro_bump_requires_stacking", RelationType.REQUIRES.value, ["process:micro_bump"], ["process:stacked_packaging"], process_tree),
        SeedEdgeSpec("seed_stacking_requires_test", RelationType.REQUIRES.value, ["process:stacked_packaging"], ["process:test"], process_tree),
        SeedEdgeSpec("seed_plating_requires_solution", RelationType.REQUIRES.value, ["process:tsv_cu_plating"], ["material:plating_solution"], process_tree),
        SeedEdgeSpec("seed_tsv_requires_barrier_material", RelationType.REQUIRES.value, ["process:tsv_cu_plating"], ["material:insulation_barrier"], process_tree),
        SeedEdgeSpec("seed_micro_bump_requires_material", RelationType.REQUIRES.value, ["process:micro_bump"], ["material:micro_bump_material"], process_tree),
        SeedEdgeSpec("seed_stacking_requires_substrate", RelationType.REQUIRES.value, ["process:stacked_packaging"], ["material:package_substrate"], process_tree),
        SeedEdgeSpec("seed_related_material_placeholder", RelationType.REQUIRES.value, ["process:stacked_packaging"], ["material:related_placeholder"], process_tree),
        SeedEdgeSpec("seed_tsv_etching_requires_equipment", RelationType.REQUIRES.value, ["process:tsv_etching"], ["equipment:etching"], process_tree),
        SeedEdgeSpec("seed_tsv_etching_requires_tsv_equipment", RelationType.REQUIRES.value, ["process:tsv_etching"], ["equipment:tsv_etching"], process_tree),
        SeedEdgeSpec("seed_plating_requires_equipment", RelationType.REQUIRES.value, ["process:tsv_cu_plating"], ["equipment:plating"], process_tree),
        SeedEdgeSpec("seed_bonding_requires_equipment", RelationType.REQUIRES.value, ["process:temporary_bonding"], ["equipment:bonding"], process_tree),
        SeedEdgeSpec("seed_thinning_requires_equipment", RelationType.REQUIRES.value, ["process:wafer_thinning"], ["equipment:thinning"], process_tree),
        SeedEdgeSpec("seed_stacking_requires_inspection", RelationType.REQUIRES.value, ["process:stacked_packaging"], ["equipment:inspection"], process_tree),
        SeedEdgeSpec("seed_test_requires_equipment", RelationType.REQUIRES.value, ["process:test"], ["equipment:test"], process_tree),
        SeedEdgeSpec("seed_supplier_chain_placeholder", RelationType.SUPPLIES_TO.value, ["supplier:placeholder"], ["material:package_substrate"], equity),
        SeedEdgeSpec("seed_company_chain_placeholder", RelationType.MENTIONS.value, ["company:placeholder"], ["material:plating_solution"], equity),
        SeedEdgeSpec("seed_hbm_lab_stage", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["event:lab_stage"], time_stage),
        SeedEdgeSpec("seed_hbm_customer_validation_stage", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["event:customer_validation"], time_stage),
        SeedEdgeSpec("seed_hbm_mass_production_stage", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["event:mass_production"], time_stage),
        SeedEdgeSpec("seed_hbm_time_stage", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["event:2026_customer_validation"], time_stage, {"time_scope": "2026"}),
        SeedEdgeSpec("seed_capacity_bottleneck", RelationType.CONSTRAINED_BY.value, ["process:stacked_packaging"], ["constraint:advanced_packaging_capacity"], bottleneck),
        SeedEdgeSpec("seed_yield_bottleneck", RelationType.CONSTRAINED_BY.value, ["process:tsv_cu_plating"], ["constraint:yield_rate"], bottleneck),
        SeedEdgeSpec("seed_validation_cycle_bottleneck", RelationType.CONSTRAINED_BY.value, ["material:plating_solution"], ["constraint:customer_validation_cycle"], bottleneck),
        SeedEdgeSpec("seed_equipment_lead_time_bottleneck", RelationType.CONSTRAINED_BY.value, ["equipment:tsv_etching"], ["constraint:equipment_lead_time"], bottleneck),
        SeedEdgeSpec("seed_material_purity_bottleneck", RelationType.CONSTRAINED_BY.value, ["material:plating_solution"], ["constraint:material_purity"], bottleneck),
        SeedEdgeSpec("seed_evidence_placeholder", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["document:evidence_placeholder"], evidence),
        SeedEdgeSpec("seed_annual_report_evidence_placeholder", RelationType.VALIDATED_BY.value, ["company:placeholder"], ["document:annual_report_placeholder"], evidence),
        SeedEdgeSpec("seed_announcement_evidence_placeholder", RelationType.VALIDATED_BY.value, ["company:placeholder"], ["document:announcement_placeholder"], evidence),
        SeedEdgeSpec("seed_research_report_evidence_placeholder", RelationType.VALIDATED_BY.value, ["theme:hbm"], ["document:research_report_placeholder"], evidence),
        SeedEdgeSpec("seed_paper_evidence_placeholder", RelationType.VALIDATED_BY.value, ["process:tsv_etching"], ["document:paper_technical_placeholder"], evidence),
    ]
    return IndustryChainSeed(
        name="HBM Golden Seed",
        nodes=nodes,
        edges=edges,
        topic="HBM",
        assumptions=[
            "HBM seed graph 仅用于提出产业链拆解假设。",
            "公司、供应商、证据和阶段节点均为待填充占位，不代表事实归属。",
        ],
        warnings=[
            "不包含真实公司清单，不输出候选股。",
            "所有关系必须后续由公告、年报、研报或技术资料验证。",
        ],
    )


def build_cpo_seed() -> IndustryChainSeed:
    return _build_compact_theme_seed(
        topic="CPO",
        seed_name="CPO Seed",
        product_name="CPO 光模块",
        route_name="光电共封装路线",
        process_name="光电共封装工艺",
        material_name="光芯片/封装材料占位",
        equipment_name="光耦合与测试设备占位",
        constraint_name="耦合良率",
        extra_constraint_name="客户验证周期",
    )


def build_800v_hvdc_seed() -> IndustryChainSeed:
    return _build_compact_theme_seed(
        topic="800V HVDC",
        seed_name="800V HVDC Seed",
        product_name="800V HVDC 电源模块",
        route_name="高压直流供电路线",
        process_name="高压直流配电设计",
        material_name="绝缘材料占位",
        equipment_name="高压电源测试设备占位",
        constraint_name="认证周期",
        extra_constraint_name="设备交期",
    )


def build_data_center_power_seed() -> IndustryChainSeed:
    return _build_compact_theme_seed(
        topic="数据中心电力",
        seed_name="数据中心电力 Seed",
        product_name="数据中心供配电系统",
        route_name="高密度供电路线",
        process_name="配电架构设计",
        material_name="电力连接材料占位",
        equipment_name="UPS/配电设备占位",
        constraint_name="供电可靠性",
        extra_constraint_name="交付周期",
    )


def build_glass_substrate_seed() -> IndustryChainSeed:
    return _build_compact_theme_seed(
        topic="玻璃基板",
        seed_name="玻璃基板 Seed",
        product_name="先进封装玻璃基板",
        route_name="玻璃核心基板路线",
        process_name="玻璃通孔加工",
        material_name="玻璃基材占位",
        equipment_name="激光加工设备占位",
        constraint_name="良率",
        extra_constraint_name="客户验证周期",
    )


def build_silicon_photonics_seed() -> IndustryChainSeed:
    return _build_compact_theme_seed(
        topic="硅光",
        seed_name="硅光 Seed",
        product_name="硅光芯片",
        route_name="硅光集成路线",
        process_name="硅光器件制造",
        material_name="硅光材料占位",
        equipment_name="光刻与测试设备占位",
        constraint_name="封装耦合良率",
        extra_constraint_name="客户验证周期",
    )


def build_generic_theme_seed(topic: str) -> IndustryChainSeed:
    normalized = topic or "未命名主题"
    return _build_compact_theme_seed(
        topic=normalized,
        seed_name=f"{normalized} Seed",
        product_name=f"{normalized} 产品占位",
        route_name=f"{normalized} 技术路线占位",
        process_name=f"{normalized} 工艺占位",
        material_name=f"{normalized} 材料占位",
        equipment_name=f"{normalized} 设备占位",
        constraint_name="待验证瓶颈约束",
        extra_constraint_name="客户验证周期",
        generic_warning="该主题未命中内置 fixture，仅生成最小通用 seed graph。",
    )


def _build_compact_theme_seed(
    *,
    topic: str,
    seed_name: str,
    product_name: str,
    route_name: str,
    process_name: str,
    material_name: str,
    equipment_name: str,
    constraint_name: str,
    extra_constraint_name: str,
    generic_warning: str | None = None,
) -> IndustryChainSeed:
    slug = _slug(topic)
    theme_id = f"theme:{slug}"
    product_id = f"product:{slug}:primary"
    route_id = f"tech_route:{slug}:primary"
    future_route_id = f"tech_route:{slug}:future_placeholder"
    process_id = f"process:{slug}:primary"
    material_id = f"material:{slug}:placeholder"
    equipment_id = f"equipment:{slug}:placeholder"
    supplier_id = f"supplier:{slug}:placeholder"
    company_id = f"company:{slug}:placeholder"
    event_id = f"event:{slug}:validation"
    constraint_id = f"constraint:{slug}:primary"
    extra_constraint_id = f"constraint:{slug}:secondary"
    document_id = f"document:{slug}:placeholder"

    nodes = [
        SeedNodeSpec(theme_id, NodeType.THEME.value, topic),
        SeedNodeSpec(product_id, NodeType.PRODUCT.value, product_name),
        SeedNodeSpec(route_id, NodeType.TECH_ROUTE.value, route_name),
        SeedNodeSpec(future_route_id, NodeType.TECH_ROUTE.value, "潜在未来路线占位"),
        SeedNodeSpec(process_id, NodeType.PROCESS.value, process_name),
        SeedNodeSpec(material_id, NodeType.MATERIAL.value, material_name, attributes={"placeholder": True}),
        SeedNodeSpec(equipment_id, NodeType.EQUIPMENT.value, equipment_name, attributes={"placeholder": True}),
        SeedNodeSpec(supplier_id, NodeType.SUPPLIER.value, "供应商预留位", attributes={"placeholder": True}),
        SeedNodeSpec(company_id, NodeType.COMPANY.value, "公司链预留位", attributes={"placeholder": True}),
        SeedNodeSpec(event_id, NodeType.EVENT.value, "验证/送样阶段占位", attributes={"placeholder": True}),
        SeedNodeSpec(constraint_id, NodeType.CONSTRAINT.value, constraint_name),
        SeedNodeSpec(extra_constraint_id, NodeType.CONSTRAINT.value, extra_constraint_name),
        SeedNodeSpec(document_id, NodeType.DOCUMENT.value, "证据占位", attributes={"placeholder": True}),
    ]

    flow = HyperGraphDimension.INDUSTRY_CHAIN_FLOW.value
    substitute = HyperGraphDimension.SUBSTITUTE_BRANCH.value
    process_tree = HyperGraphDimension.SYNTHESIS_PROCESS_TREE.value
    equity = HyperGraphDimension.EQUITY_OWNERSHIP_CHAIN.value
    time_stage = HyperGraphDimension.TIME_STAGE.value
    bottleneck = HyperGraphDimension.BOTTLENECK_CONSTRAINT.value
    evidence = HyperGraphDimension.EVIDENCE_DISCIPLINE.value

    edges = [
        SeedEdgeSpec(f"seed_{slug}_product_part_of_theme", RelationType.PART_OF.value, [product_id], [theme_id], flow),
        SeedEdgeSpec(f"seed_{slug}_route_mentions_product", RelationType.MENTIONS.value, [route_id], [product_id], flow),
        SeedEdgeSpec(f"seed_{slug}_future_route_substitutes", RelationType.SUBSTITUTES.value, [future_route_id], [route_id], substitute),
        SeedEdgeSpec(f"seed_{slug}_product_requires_process", RelationType.REQUIRES.value, [product_id], [process_id], process_tree),
        SeedEdgeSpec(f"seed_{slug}_process_requires_material", RelationType.REQUIRES.value, [process_id], [material_id], process_tree),
        SeedEdgeSpec(f"seed_{slug}_process_requires_equipment", RelationType.REQUIRES.value, [process_id], [equipment_id], process_tree),
        SeedEdgeSpec(f"seed_{slug}_supplier_placeholder", RelationType.SUPPLIES_TO.value, [supplier_id], [material_id], equity),
        SeedEdgeSpec(f"seed_{slug}_company_placeholder", RelationType.MENTIONS.value, [company_id], [process_id], equity),
        SeedEdgeSpec(f"seed_{slug}_validation_stage", RelationType.VALIDATED_BY.value, [theme_id], [event_id], time_stage),
        SeedEdgeSpec(f"seed_{slug}_primary_constraint", RelationType.CONSTRAINED_BY.value, [process_id], [constraint_id], bottleneck),
        SeedEdgeSpec(f"seed_{slug}_secondary_constraint", RelationType.CONSTRAINED_BY.value, [equipment_id], [extra_constraint_id], bottleneck),
        SeedEdgeSpec(f"seed_{slug}_document_placeholder", RelationType.VALIDATED_BY.value, [theme_id], [document_id], evidence),
    ]

    warnings = [
        "该 seed graph 只表达待验证产业链假设。",
        "不包含真实公司清单，不输出候选股。",
    ]
    if generic_warning:
        warnings.append(generic_warning)

    return IndustryChainSeed(
        name=seed_name,
        nodes=nodes,
        edges=edges,
        topic=topic,
        assumptions=[
            f"{topic} 的产业链、工艺、约束和证据占位均需后续验证。",
            "占位公司和供应商只表示待补充结构位置，不表示真实主体。",
        ],
        warnings=warnings,
    )


def _slug(topic: str) -> str:
    predefined = {
        "CPO": "cpo",
        "800V HVDC": "800v_hvdc",
        "数据中心电力": "data_center_power",
        "玻璃基板": "glass_substrate",
        "硅光": "silicon_photonics",
    }
    if topic in predefined:
        return predefined[topic]
    return "generic_" + "_".join(f"{ord(char):x}" for char in topic if not char.isspace())
