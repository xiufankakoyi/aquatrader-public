from __future__ import annotations

from enum import Enum


class NodeType(str, Enum):
    THEME = "THEME"
    PRODUCT = "PRODUCT"
    PROCESS = "PROCESS"
    MATERIAL = "MATERIAL"
    EQUIPMENT = "EQUIPMENT"
    COMPANY = "COMPANY"
    SUBSIDIARY = "SUBSIDIARY"
    CUSTOMER = "CUSTOMER"
    SUPPLIER = "SUPPLIER"
    EVENT = "EVENT"
    DOCUMENT = "DOCUMENT"
    CONSTRAINT = "CONSTRAINT"
    TECH_ROUTE = "TECH_ROUTE"


class RelationType(str, Enum):
    UPSTREAM_OF = "UPSTREAM_OF"
    DOWNSTREAM_OF = "DOWNSTREAM_OF"
    PART_OF = "PART_OF"
    REQUIRES = "REQUIRES"
    SUBSTITUTES = "SUBSTITUTES"
    BOTTLENECK_OF = "BOTTLENECK_OF"
    PRODUCES = "PRODUCES"
    SUPPLIES_TO = "SUPPLIES_TO"
    CUSTOMER_OF = "CUSTOMER_OF"
    OWNS = "OWNS"
    INVESTS_IN = "INVESTS_IN"
    BENEFITS_FROM = "BENEFITS_FROM"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    VALIDATED_BY = "VALIDATED_BY"
    CONTRADICTED_BY = "CONTRADICTED_BY"
    MENTIONS = "MENTIONS"


class EvidenceLevel(str, Enum):
    PROVEN = "PROVEN"
    LIKELY = "LIKELY"
    SPECULATIVE = "SPECULATIVE"
    DISPROVEN = "DISPROVEN"
    CONFLICTED = "CONFLICTED"


class EdgeStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    UNVERIFIED_SEED = "UNVERIFIED_SEED"
    ACTIVE = "ACTIVE"
    DISPROVEN = "DISPROVEN"
    CONFLICTED = "CONFLICTED"
    DELETED = "DELETED"


class MutationAction(str, Enum):
    ADD_NODE = "ADD_NODE"
    ADD_EDGE = "ADD_EDGE"
    ADD_EVIDENCE = "ADD_EVIDENCE"
    UPDATE_EDGE = "UPDATE_EDGE"
    DOWNGRADE_CONFIDENCE = "DOWNGRADE_CONFIDENCE"
    MARK_CONFLICT = "MARK_CONFLICT"
    MERGE_NODE = "MERGE_NODE"
    SPLIT_NODE = "SPLIT_NODE"
    RENAME_NODE = "RENAME_NODE"
    REWIRE_EDGE = "REWIRE_EDGE"
    MARK_NODE_TOO_COARSE = "MARK_NODE_TOO_COARSE"
    DELETE_NODE = "DELETE_NODE"
    DELETE_EDGE = "DELETE_EDGE"


class NodeSource(str, Enum):
    SEED_GRAPH = "seed_graph"
    LLM = "llm"
    EVIDENCE = "evidence"
    MANUAL = "manual"


class HyperGraphDimension(str, Enum):
    INDUSTRY_CHAIN_FLOW = "产业链流"
    SUBSTITUTE_BRANCH = "替代分支"
    SYNTHESIS_PROCESS_TREE = "合成树/工艺树"
    EQUITY_OWNERSHIP_CHAIN = "公司股权链"
    TIME_STAGE = "时间阶段"
    BOTTLENECK_CONSTRAINT = "瓶颈约束"
    EVIDENCE_DISCIPLINE = "证据维度"


HYPERGRAPH_DIMENSIONS = tuple(item.value for item in HyperGraphDimension)


class CausalStatus(str, Enum):
    """因果声明强度枚举(Step 1)。

    用于 Evidence.causal_status 与 HyperEdge.qualifiers["causal_status"],
    让 Verifier 按因果类型差异化校验,避免把"相关性"当作"因果"写入 ACTIVE。
    """

    # 仅观测到共现,无方向性证据。
    OBSERVATIONAL_ASSOCIATION = "OBSERVATIONAL_ASSOCIATION"
    # 时序上 X 先于 Y 发生(Granger 类筛选结果)。
    TEMPORAL_PRECEDENCE = "TEMPORAL_PRECEDENCE"
    # 准实验归因(BSTS/CausalImpact 在已知干预时点下估计的效应)。
    QUASI_EXPERIMENTAL_EFFECT = "QUASI_EXPERIMENTAL_EFFECT"
    # 预测因果(时序模型稳定通过 bootstrap 验证)。
    PREDICTIVE_CAUSAL = "PREDICTIVE_CAUSAL"
    # 干预语义已确认(有反事实或实验证据支撑)。
    INTERVENTION_READY = "INTERVENTION_READY"


CAUSAL_STATUS_VALUES = frozenset(item.value for item in CausalStatus)


RESEARCH_BATTLES = (
    "第一战役：HyperGraph Kernel",
    "第二战役：Seed Graph Builder",
    "第三战役：Evidence Miner",
    "第四战役：Graph Verifier",
    "第五战役：Growth Agent",
    "第六战役：Alpha Path Ranker",
    "第七战役：ResearchRunManager",
)


OFFICIAL_SOURCE_TYPES = {
    "announcement",
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "prospectus",
    "contract",
    "customer_certification",
}

COMPANY_NODE_TYPES = {NodeType.COMPANY.value, NodeType.SUBSIDIARY.value}
CONCRETE_BENEFIT_NODE_TYPES = {
    NodeType.PRODUCT.value,
    NodeType.PROCESS.value,
    NodeType.MATERIAL.value,
    NodeType.EQUIPMENT.value,
}

AUDIT_THEME_RELATION_TYPES = {RelationType.MENTIONS.value}
