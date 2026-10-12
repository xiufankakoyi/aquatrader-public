"""Ontology models shared across Perilla domains.

Defines canonical enums, value sets, and structural constants used by the
alpha graph, event graph, entity resolver, and evidence modules. Keeping these
types in one place avoids cross-domain circular imports and provides a single
source of truth for node/relation/event/status vocabularies.

TODO: Migrate additional axis-specific constants and competency question
templates here as the ontology evolves.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class RelationType(str, Enum):
    """产业链关系类型。"""

    CONTAINS = "contains"
    MADE_OF = "made_of"
    REQUIRES = "requires"
    ENABLED_BY = "enabled_by"
    CONSTRAINED_BY = "constrained_by"
    BOTTLENECK_OF = "bottleneck_of"
    PROCESSED_BY = "processed_by"
    TESTED_BY = "tested_by"
    SUPPLIED_BY = "supplied_by"
    SUBSTITUTE_FOR = "substitute_for"
    CERTIFIED_BY = "certified_by"
    IMPORT_DEPENDENT_ON = "import_dependent_on"
    COST_DRIVER_OF = "cost_driver_of"
    PERFORMANCE_DRIVER_OF = "performance_driver_of"


RELATION_TYPES: FrozenSet[str] = frozenset(r.value for r in RelationType)


RELATION_LABELS: dict[str, str] = {
    RelationType.CONTAINS.value: "包含",
    RelationType.MADE_OF.value: "由...制成",
    RelationType.REQUIRES.value: "需要",
    RelationType.ENABLED_BY.value: "由... enabling",
    RelationType.CONSTRAINED_BY.value: "受...约束",
    RelationType.BOTTLENECK_OF.value: "瓶颈于",
    RelationType.PROCESSED_BY.value: "由...加工",
    RelationType.TESTED_BY.value: "由...测试",
    RelationType.SUPPLIED_BY.value: "由...供应",
    RelationType.SUBSTITUTE_FOR.value: "替代",
    RelationType.CERTIFIED_BY.value: "由...认证",
    RelationType.IMPORT_DEPENDENT_ON.value: "进口依赖",
    RelationType.COST_DRIVER_OF.value: "成本驱动",
    RelationType.PERFORMANCE_DRIVER_OF.value: "性能驱动",
}


class ClaimStatus(str, Enum):
    """Claim 在 SAG 中的可信状态。"""

    RAW = "raw"
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    TRUSTED = "trusted"
    PENDING_VALIDATION = "pending_validation"
    PENDING_DOWNGRADE = "pending_downgrade"
    CONTRADICTED = "contradicted"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


CLAIM_STATUS_SET: FrozenSet[str] = frozenset(s.value for s in ClaimStatus)


class StructuralAxis(str, Enum):
    """产业链结构轴。"""

    MATERIAL = "material_axis"
    EQUIPMENT = "equipment_axis"
    PROCESS = "process_axis"
    PERFORMANCE = "performance_axis"
    SUPPLIER = "supplier_axis"
    CERTIFICATION = "certification_axis"
    DOMESTIC_SUBSTITUTION = "domestic_substitution_axis"
    COST = "cost_axis"
    VALUATION = "valuation_axis"


STRUCTURAL_AXES: FrozenSet[str] = frozenset(a.value for a in StructuralAxis)


AXIS_LABELS: dict[str, str] = {
    StructuralAxis.MATERIAL.value: "材料轴",
    StructuralAxis.EQUIPMENT.value: "设备轴",
    StructuralAxis.PROCESS.value: "工艺轴",
    StructuralAxis.PERFORMANCE.value: "性能轴",
    StructuralAxis.SUPPLIER.value: "供应商轴",
    StructuralAxis.CERTIFICATION.value: "认证轴",
    StructuralAxis.DOMESTIC_SUBSTITUTION.value: "国产替代轴",
    StructuralAxis.COST.value: "成本轴",
    StructuralAxis.VALUATION.value: "估值轴",
}


class NodeType(str, Enum):
    """节点类型。"""

    ROOT = "root"
    PRODUCT = "product"
    COMPONENT = "component"
    MATERIAL = "material"
    PROCESS = "process"
    EQUIPMENT = "equipment"
    TESTING = "testing"
    CONSUMABLE = "consumable"
    SUPPLIER = "supplier"
    COMPANY = "company"
    CERTIFICATION = "certification"
    MARKET = "market"
    GENERIC = "generic"


NODE_TYPES: FrozenSet[str] = frozenset(n.value for n in NodeType)


class SourceType(str, Enum):
    """数据来源类型，影响可信度权重。"""

    TDX_REPORT = "tdx_report"
    JULANG_REPORT = "julang_report"
    BROKER_REPORT = "broker_report"
    INDUSTRY_WHITEPAPER = "industry_whitepaper"
    ANNOUNCEMENT = "announcement"
    ANNUAL_REPORT = "annual_report"
    PROSPECTUS = "prospectus"
    OFFICIAL_WEBSITE = "official_website"
    PATENT = "patent"
    STANDARD = "standard"
    CNINFO_INTERACTION = "cninfo_interaction"
    INVESTOR_RELATION = "investor_relation"
    WENCAI = "wencai"
    EASTMONEY = "eastmoney"
    NEWS = "news"
    EXHIBITION = "exhibition"
    MEETING_MINUTES = "meeting_minutes"
    WEB_SEARCH_SNIPPET = "web_search_snippet"
    WEB_SEARCH = "web_search"
    FORUM = "forum"
    SOCIAL_MEDIA = "social_media"
    CONFERENCE = "conference"
    WHITEPAPER = "whitepaper"
    INTERACTION_EASY = "interaction_easy"
    MARKET_DATA = "market_data"
    LLM_INFERENCE = "llm_inference"


SOURCE_TYPES: FrozenSet[str] = frozenset(s.value for s in SourceType)


class EventType(str, Enum):
    """事件类型。"""

    PRICE_INCREASE = "price_increase"
    CAPACITY_EXPANSION = "capacity_expansion"
    CUSTOMER_CERTIFIED = "customer_certified"
    ORDER_DISCLOSED = "order_disclosed"
    PRODUCT_LAUNCHED = "product_launched"
    SUPPLY_SHORTAGE = "supply_shortage"
    DOMESTIC_SUBSTITUTION_PROGRESS = "domestic_substitution_progress"
    POLICY_SIGNAL = "policy_signal"
    IMPORT_DEPENDENCY_SIGNAL = "import_dependency_signal"
    CERTIFICATION_PROGRESS = "certification_progress"
    BOTTLENECK_HYPOTHESIS = "bottleneck_hypothesis"
    CONCEPT_SIGNAL = "concept_signal"
    OTHER = "other"


EVENT_TYPES: FrozenSet[str] = frozenset(e.value for e in EventType)


class EventClaimStatus(str, Enum):
    """EventClaim 在事件图中的生命周期状态。"""

    ACTIVE = "active"
    STALE = "stale"
    RETRACTED = "retracted"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    EXPIRED = "expired"
    PROMOTION_CANDIDATE = "promotion_candidate"
    PROMOTED = "promoted"


EVENT_CLAIM_STATUSES: FrozenSet[str] = frozenset(s.value for s in EventClaimStatus)
