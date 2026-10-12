"""Perilla Discovery Engine v1.25 - 数据模型定义.

定义自主紫苏发现引擎的所有核心数据结构，包括概念信号、需求锚点、
供应链节点/边、瓶颈假设、公司候选及最终发现报告。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.research.perilla.shared.common.confidence import (
    CONFIDENCE_LEVELS,
    CREDIBILITY_HINTS,
    EVIDENCE_LEVELS,
    EVIDENCE_LEVEL_CLAIM_ONLY,
    EVIDENCE_LEVEL_CONFIRMED,
    EVIDENCE_LEVEL_EARLY_SIGNAL,
    EVIDENCE_LEVEL_HYPOTHESIS,
    EVIDENCE_LEVEL_INSUFFICIENT,
    EVIDENCE_LEVEL_MAIN_ANCHOR,
    EVIDENCE_LEVEL_NON_CORE,
    EVIDENCE_LEVEL_PROBABLE,
    EVIDENCE_LEVEL_RUMOR,
    REVIEW_STATUSES,
)
from core.research.perilla.shared.common.temporal import now_iso


# ---------------------------------------------------------------------------
# 常量：枚举型字符串字段的可选值集合
# ---------------------------------------------------------------------------

ANCHOR_TYPES: frozenset[str] = frozenset({
    "terminal_product", "application_scenario", "technology_route",
    "policy_theme", "industry_event", "unknown",
})


SOURCE_TYPES: frozenset[str] = frozenset({
    "index_page", "concept_page", "sector_page", "search_result",
    "rss", "change_event", "official_site", "recruitment", "expo",
    "tender", "news", "report", "announcement", "forum", "social",
    "meeting_note", "rumor", "manual", "local_evidence",
})

SIGNAL_TYPES: frozenset[str] = frozenset({
    "supplier", "certification", "sampling", "capacity", "recruitment",
    "expo", "tender", "partner", "customer", "technical_route",
    "rumor", "concept_claim", "other",
})

SUPPLY_CHAIN_LAYERS: frozenset[str] = frozenset({
    "terminal", "module", "component", "material", "equipment",
    "testing", "consumable", "service", "software", "unknown",
})

EDGE_RELATION_TYPES: frozenset[str] = frozenset({
    "upstream_of", "downstream_of", "depends_on", "supplies",
    "tests", "enables", "substitutes", "unknown",
})

CHAIN_CONSTRUCTION_METHODS: frozenset[str] = frozenset({
    "llm_dynamic", "hybrid", "fallback_seed",
})

BOTTLENECK_STATUSES: frozenset[str] = frozenset({
    "STRONG_BOTTLENECK", "POSSIBLE_BOTTLENECK",
    "WEAK_BOTTLENECK", "HYPOTHESIS_ONLY",
})

SIX_QUESTION_KEYS: frozenset[str] = frozenset({
    "attach_rate", "bom_share", "supplier_concentration",
    "capacity_expansion", "switching_cost", "pricing_state",
})

MARKETS: frozenset[str] = frozenset({
    "A_SHARE", "US", "HK", "JP", "TW", "KR", "EU", "OTHER", "UNKNOWN",
})

DISCOVERY_CLASSIFICATIONS: frozenset[str] = frozenset({
    "PERILLA_CANDIDATE", "CONCEPT_CLAIM_ONLY", "EARLY_SIGNAL_WATCH",
    "RUMOR_WATCH", "HYPOTHESIS_ONLY", "MAIN_ANCHOR",
    "NON_CORE_MARKET", "NEEDS_HUMAN_REVIEW",
})

REPORT_MODES: frozenset[str] = frozenset({"auto", "topic"})


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def new_id(prefix: str = "") -> str:
    """生成基于 UUID 的唯一标识符，可带可选前缀.

    Args:
        prefix: ID 前缀，例如 "cs_" 表示 ConceptSignal。

    Returns:
        格式为 "{prefix}{uuid4_hex}" 的字符串。
    """
    return f"{prefix}{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ConceptSignal:
    """概念信号 —— 从多源数据中检测到的概念/主题.

    记录一个概念的基本信息、热度、新颖度及置信度，
    是整个发现流程的起点。

    Attributes:
        concept_id: 概念唯一标识。
        name: 概念名称。
        aliases: 概念别名列表。
        detected_from: 检测来源标识列表。
        source_types: 来源类型列表。
        first_seen_at: 首次发现时间。
        last_seen_at: 最近发现时间。
        heat_score: 热度评分 [0, 1]。
        novelty_score: 新颖度评分 [0, 1]。
        confidence: 置信度 (high / medium / low)。
        raw_evidence_ids: 原始证据 ID 列表。
        notes: 备注信息。
    """

    concept_id: str = ""
    name: str = ""
    aliases: List[str] = field(default_factory=list)
    detected_from: List[str] = field(default_factory=list)
    source_types: List[str] = field(default_factory=list)
    first_seen_at: str = ""
    last_seen_at: str = ""
    heat_score: float = 0.0
    novelty_score: float = 0.0
    confidence: str = "low"
    raw_evidence_ids: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class DemandAnchor:
    """需求锚点 —— 概念对应的终端需求或应用场景.

    将抽象概念锚定到可验证的需求端，例如终端产品、
    应用场景、技术路线、政策主题或行业事件。

    Attributes:
        anchor_id: 锚点唯一标识。
        concept_id: 关联概念 ID。
        name: 锚点名称。
        anchor_type: 锚点类型。
        description: 锚点描述。
        evidence_ids: 支撑证据 ID 列表。
        confidence: 置信度 (high / medium / low)。
    """

    anchor_id: str = ""
    concept_id: str = ""
    name: str = ""
    anchor_type: str = "unknown"
    description: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    confidence: str = "low"


@dataclass
class SourceDocument:
    """来源文档 —— 爬取或采集的原始文档.

    记录文档的来源、内容、元数据及可信度提示，
    作为所有证据的原始载体。

    Attributes:
        document_id: 文档唯一标识。
        source_type: 来源类型。
        url: 文档 URL。
        title: 文档标题。
        site_name: 站点名称。
        published_at: 发布时间（可选）。
        fetched_at: 采集时间。
        content_text: 纯文本内容。
        raw_html_path: 原始 HTML 存储路径（可选）。
        text_hash: 文本内容哈希值。
        metadata: 附加元数据字典。
        credibility_hint: 可信度提示。
    """

    document_id: str = ""
    source_type: str = "local_evidence"
    url: str = ""
    title: str = ""
    site_name: str = ""
    published_at: Optional[str] = None
    fetched_at: str = ""
    content_text: str = ""
    raw_html_path: Optional[str] = None
    text_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    credibility_hint: str = "unknown"


@dataclass
class EarlySignal:
    """早期信号 —— 从来源文档中提取的供应链/概念信号.

    捕获关于公司-概念关系的早期线索，包括供应商关系、
    认证、产能、招聘、展会、招标等多种信号类型。

    Attributes:
        signal_id: 信号唯一标识。
        concept_id: 关联概念 ID。
        source_document_id: 来源文档 ID。
        signal_type: 信号类型。
        mentioned_companies: 文中提及的公司列表。
        anchor_companies: 被锚定为关键的公司列表。
        claim: 信号声明/断言。
        evidence_text: 证据原文片段。
        confidence: 置信度 (high / medium / low)。
        evidence_level: 证据等级。
        needs_human_review: 是否需要人工审核。
    """

    signal_id: str = ""
    concept_id: str = ""
    source_document_id: str = ""
    signal_type: str = "other"
    mentioned_companies: List[str] = field(default_factory=list)
    anchor_companies: List[str] = field(default_factory=list)
    claim: str = ""
    evidence_text: str = ""
    confidence: str = "low"
    evidence_level: str = "INSUFFICIENT_EVIDENCE"
    needs_human_review: bool = False


@dataclass
class SupplyChainNode:
    """供应链节点 —— 供应链中的一个环节/层级.

    表示供应链中的终端产品、模块、组件、材料、设备等节点，
    可标记为锚点或潜在瓶颈。

    Attributes:
        node_id: 节点唯一标识。
        chain_id: 所属供应链 ID。
        name: 节点名称。
        layer: 节点层级。
        description: 节点描述。
        is_anchor: 是否为需求锚点节点。
        is_potential_bottleneck: 是否为潜在瓶颈节点。
        evidence_ids: 支撑证据 ID 列表。
        hypothesis_reason: 假设推理理由。
        confidence: 置信度 (high / medium / low)。
    """

    node_id: str = ""
    chain_id: str = ""
    name: str = ""
    layer: str = "unknown"
    description: str = ""
    is_anchor: bool = False
    is_potential_bottleneck: bool = False
    evidence_ids: List[str] = field(default_factory=list)
    hypothesis_reason: str = ""
    confidence: str = "low"


@dataclass
class SupplyChainEdge:
    """供应链边 —— 两个节点之间的上下游/依赖关系.

    Attributes:
        source_node_id: 源节点 ID。
        target_node_id: 目标节点 ID。
        relation_type: 关系类型。
        evidence_ids: 支撑证据 ID 列表。
        confidence: 置信度 (high / medium / low)。
    """

    source_node_id: str = ""
    target_node_id: str = ""
    relation_type: str = "unknown"
    evidence_ids: List[str] = field(default_factory=list)
    confidence: str = "low"


@dataclass
class DynamicSupplyChain:
    """动态供应链 —— 围绕需求锚点构建的完整供应链图谱.

    由 LLM 动态生成或混合构建，包含节点和边的集合，
    描述从终端需求到上游材料的完整链路。

    Attributes:
        chain_id: 供应链唯一标识。
        concept_id: 关联概念 ID。
        demand_anchor_id: 关联需求锚点 ID。
        nodes: 供应链节点列表。
        edges: 供应链边列表。
        construction_method: 构建方法。
        source_document_ids: 来源文档 ID 列表。
        confidence: 置信度 (high / medium / low)。
    """

    chain_id: str = ""
    concept_id: str = ""
    demand_anchor_id: str = ""
    nodes: List[SupplyChainNode] = field(default_factory=list)
    edges: List[SupplyChainEdge] = field(default_factory=list)
    construction_method: str = "fallback_seed"
    source_document_ids: List[str] = field(default_factory=list)
    confidence: str = "low"


@dataclass
class BottleneckHypothesis:
    """瓶颈假设 —— 对供应链中潜在瓶颈节点的六问评估.

    通过六个维度（attach_rate, bom_share, supplier_concentration,
    capacity_expansion, switching_cost, pricing_state）量化评估
    节点成为紫苏（关键瓶颈）的概率。

    Attributes:
        hypothesis_id: 假设唯一标识。
        concept_id: 关联概念 ID。
        chain_id: 关联供应链 ID。
        node_id: 关联节点 ID。
        node_name: 节点名称。
        bottleneck_signals: 瓶颈信号列表。
        six_question_scores: 六问评分字典，每项 0-10 整数。
        total_score: 六问总分。
        perilla_probability: 紫苏概率 [0, 1]。
        evidence_ids: 支撑证据 ID 列表。
        is_hypothesis: 是否为假设（尚未充分验证）。
        needs_verification: 是否需要进一步验证。
        status: 瓶颈状态等级。
    """

    hypothesis_id: str = ""
    concept_id: str = ""
    chain_id: str = ""
    node_id: str = ""
    node_name: str = ""
    bottleneck_signals: List[str] = field(default_factory=list)
    six_question_scores: Dict[str, int] = field(default_factory=lambda: {
        "attach_rate": 0,
        "bom_share": 0,
        "supplier_concentration": 0,
        "capacity_expansion": 0,
        "switching_cost": 0,
        "pricing_state": 0,
    })
    total_score: int = 0
    perilla_probability: float = 0.0
    evidence_ids: List[str] = field(default_factory=list)
    is_hypothesis: bool = True
    needs_verification: bool = True
    status: str = "HYPOTHESIS_ONLY"


@dataclass
class CompanyCandidate:
    """公司候选 —— 与概念/供应链节点关联的候选公司.

    记录公司在特定概念中的映射来源、证据等级及声明摘要，
    用于后续筛选和人工审核。

    Attributes:
        candidate_id: 候选唯一标识。
        company_name: 公司名称。
        symbol: 证券代码（可选）。
        market: 所属市场。
        market_warning: 市场风险提示（可选，如非 A 股提醒）。
        concept_id: 关联概念 ID。
        chain_node_id: 关联供应链节点 ID。
        bottleneck_hypothesis_id: 关联瓶颈假设 ID。
        mapping_sources: 映射来源列表。
        evidence_level: 证据等级。
        evidence_ids: 支撑证据 ID 列表。
        claim_summary: 声明摘要。
        needs_human_review: 是否需要人工审核。
    """

    candidate_id: str = ""
    company_name: str = ""
    symbol: Optional[str] = None
    market: str = "UNKNOWN"
    market_warning: Optional[str] = None
    concept_id: str = ""
    chain_node_id: str = ""
    bottleneck_hypothesis_id: str = ""
    mapping_sources: List[str] = field(default_factory=list)
    evidence_level: str = "INSUFFICIENT_EVIDENCE"
    evidence_ids: List[str] = field(default_factory=list)
    claim_summary: str = ""
    needs_human_review: bool = False


@dataclass
class PerillaDiscoveryItem:
    """紫苏发现条目 —— 最终输出的单条发现结果.

    融合概念、需求锚点、供应链节点、公司信息及瓶颈逻辑，
    形成面向用户/审核人员的完整发现条目。

    Attributes:
        item_id: 条目唯一标识。
        concept: 概念名称。
        demand_anchor: 需求锚点名称。
        chain_node: 供应链节点名称。
        company_name: 公司名称。
        symbol: 证券代码（可选）。
        market: 所属市场。
        classification: 分类标签。
        evidence_level: 证据等级。
        perilla_probability: 紫苏概率 [0, 1]。
        bottleneck_logic: 瓶颈逻辑说明。
        evidence_summary: 证据摘要。
        source_urls: 来源 URL 列表。
        risk_flags: 风险标记列表。
        review_status: 审核状态。
    """

    item_id: str = ""
    concept: str = ""
    demand_anchor: str = ""
    chain_node: str = ""
    company_name: str = ""
    symbol: Optional[str] = None
    market: str = "UNKNOWN"
    classification: str = "NEEDS_HUMAN_REVIEW"
    evidence_level: str = "INSUFFICIENT_EVIDENCE"
    perilla_probability: float = 0.0
    bottleneck_logic: str = ""
    evidence_summary: str = ""
    source_urls: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    review_status: str = "pending_human_review"


@dataclass
class PerillaDiscoveryReport:
    """紫苏发现报告 —— 一次完整发现流程的输出汇总.

    包含本次运行的所有概念信号、需求锚点、动态供应链、
    瓶颈假设、公司候选及最终发现条目，以及审核队列和统计信息。

    Attributes:
        run_id: 运行唯一标识。
        mode: 运行模式 (auto / topic)。
        input_topic: 输入主题（topic 模式时使用）。
        concept_signals: 概念信号列表。
        demand_anchors: 需求锚点列表。
        dynamic_chains: 动态供应链列表。
        bottleneck_hypotheses: 瓶颈假设列表。
        company_candidates: 公司候选列表。
        discovery_items: 发现条目列表。
        review_queue: 待审核队列（字典列表）。
        source_stats: 来源统计信息。
        generated_at: 报告生成时间。
        disclaimer: 免责声明。
    """

    run_id: str = ""
    mode: str = "auto"
    input_topic: Optional[str] = None
    concept_signals: List[ConceptSignal] = field(default_factory=list)
    demand_anchors: List[DemandAnchor] = field(default_factory=list)
    dynamic_chains: List[DynamicSupplyChain] = field(default_factory=list)
    bottleneck_hypotheses: List[BottleneckHypothesis] = field(default_factory=list)
    company_candidates: List[CompanyCandidate] = field(default_factory=list)
    discovery_items: List[PerillaDiscoveryItem] = field(default_factory=list)
    review_queue: List[Dict[str, Any]] = field(default_factory=list)
    source_stats: Dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    disclaimer: str = (
        "本报告由 Perilla Discovery Engine 自动生成，"
        "仅供研究参考，不构成任何投资建议。"
        "数据来源于公开信息，可能存在遗漏或错误，"
        "请以官方披露为准。"
    )


# ══════════════════════════════════════════════════════════
# v1.26: Research Inbox + Discovery Watchlist
# ══════════════════════════════════════════════════════════

INBOX_STATUSES: frozenset[str] = frozenset({
    "NEW", "REVIEWING", "WATCHING", "DEEP_RESEARCH_READY",
    "DISMISSED", "STALE",
})

RESEARCH_PRIORITY_LEVELS: frozenset[str] = frozenset({
    "HIGH_PRIORITY_RESEARCH", "MEDIUM_PRIORITY_RESEARCH",
    "LOW_PRIORITY_RESEARCH", "WATCH_ONLY",
})

EVIDENCE_CHANGE_TYPES: frozenset[str] = frozenset({
    "UPGRADE", "DOWNGRADE", "NEW_EVIDENCE", "EVIDENCE_STALE",
    "CLASSIFICATION_CHANGE", "NO_CHANGE",
})


@dataclass
class EvidenceChangeLog:
    """证据变更日志 —— 记录候选证据等级的变化.

    Attributes:
        change_id: 变更唯一标识。
        item_id: 关联的 ResearchInboxItem ID。
        company_name: 公司名称。
        symbol: 证券代码。
        chain_node: 供应链节点。
        change_type: 变更类型（UPGRADE/DOWNGRADE/NEW_EVIDENCE 等）。
        old_evidence_level: 变更前证据等级。
        new_evidence_level: 变更后证据等级。
        old_classification: 变更前分类。
        new_classification: 变更后分类。
        reason: 变更原因。
        detected_at: 检测时间。
        run_id: 触发变更的运行 ID。
    """

    change_id: str = ""
    item_id: str = ""
    company_name: str = ""
    symbol: str = ""
    chain_node: str = ""
    change_type: str = "NO_CHANGE"
    old_evidence_level: str = ""
    new_evidence_level: str = ""
    old_classification: str = ""
    new_classification: str = ""
    reason: str = ""
    detected_at: str = ""
    run_id: str = ""


@dataclass
class ResearchInboxItem:
    """研究收件箱条目 —— 每次发现的候选进入收件箱.

    Attributes:
        item_id: 条目唯一标识。
        company_name: 公司名称。
        symbol: 证券代码。
        market: 所属市场。
        chain_node: 供应链节点（紫苏叶环节）。
        concept: 概念/主题。
        main_dish: 主菜。
        evidence_level: 当前证据等级。
        classification: 分类（紫苏叶/主链锚点/相关/概念蹭热/早期信号）。
        total_score: 六问总分。
        bottleneck_logic: 一句话瓶颈逻辑。
        inbox_status: 收件箱状态（NEW/REVIEWING/WATCHING/DEEP_RESEARCH_READY/DISMISSED/STALE）。
        research_priority_score: 研究优先级分数。
        research_priority_level: 研究优先级等级。
        evidence_links: 证据链接列表。
        red_team_risks: 蓝军风险列表。
        risk_level: 风险等级。
        first_seen_at: 首次发现时间。
        last_updated_at: 最近更新时间。
        last_run_id: 最近一次运行 ID。
        evidence_history: 证据变更历史（change_id 列表）。
        next_verification_queries: 下一步验证搜索查询。
        notes: 备注。
    """

    item_id: str = ""
    company_name: str = ""
    symbol: str = ""
    market: str = "UNKNOWN"
    chain_node: str = ""
    concept: str = ""
    main_dish: str = ""
    evidence_level: str = "INSUFFICIENT_EVIDENCE"
    classification: str = "相关"
    total_score: int = 0
    bottleneck_logic: str = ""
    inbox_status: str = "NEW"
    research_priority_score: float = 0.0
    research_priority_level: str = "WATCH_ONLY"
    evidence_links: List[str] = field(default_factory=list)
    red_team_risks: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    first_seen_at: str = ""
    last_updated_at: str = ""
    last_run_id: str = ""
    evidence_history: List[str] = field(default_factory=list)
    next_verification_queries: List[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class DailyDiscoveryBrief:
    """每日发现简报 —— 每日输出的研究工作台摘要.

    Attributes:
        brief_id: 简报唯一标识。
        date: 日期（YYYY-MM-DD）。
        new_items: 今日新增紫苏叶线索。
        upgraded_items: 证据升级线索。
        downgraded_items: 证据降级线索。
        high_priority_watch: 高优先级观察池。
        needs_human_confirmation: 需要人工确认的问题。
        total_inbox: 收件箱总数。
        total_watching: 观察池总数。
        generated_at: 生成时间。
    """

    brief_id: str = ""
    date: str = ""
    new_items: List[ResearchInboxItem] = field(default_factory=list)
    upgraded_items: List[ResearchInboxItem] = field(default_factory=list)
    downgraded_items: List[ResearchInboxItem] = field(default_factory=list)
    high_priority_watch: List[ResearchInboxItem] = field(default_factory=list)
    needs_human_confirmation: List[ResearchInboxItem] = field(default_factory=list)
    total_inbox: int = 0
    total_watching: int = 0
    generated_at: str = ""
