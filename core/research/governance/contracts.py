"""P1.6 管理层、治理和资本配置台账领域契约。

按 P1 计划 §5 Task P1.6 覆盖 6 类内容，独立领域模型。
所有结构化条目必须带来源证据；缺任一项则 grounding_status="missing"。

硬门禁：
- 管理层承诺按"原文—时间—目标—兑现—偏差"保存（5 项齐全）
- 人员评价不能由情绪新闻直接生成（is_from_emotional_news=True 触发 blocker）
- 治理风险与财务预测影响分开表达（potential_financial_impact 仅描述性字段，不进入预测引擎）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的 ID。"""
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# 股东变更类型
SHAREHOLDER_CHANGE_CONTROLLER: str = "controller"  # 实际控制人变更
SHAREHOLDER_CHANGE_SUBSTANTIAL_HOLDER: str = "substantial_holder"  # 5% 以上股东
SHAREHOLDER_CHANGE_DIRECTOR: str = "director"  # 董事变更
SHAREHOLDER_CHANGE_OFFICER: str = "officer"  # 高管变更
SHAREHOLDER_CHANGE_SUPERVISOR: str = "supervisor"  # 监事变更
SHAREHOLDER_CHANGE_TYPES: tuple[str, ...] = (
    SHAREHOLDER_CHANGE_CONTROLLER,
    SHAREHOLDER_CHANGE_SUBSTANTIAL_HOLDER,
    SHAREHOLDER_CHANGE_DIRECTOR,
    SHAREHOLDER_CHANGE_OFFICER,
    SHAREHOLDER_CHANGE_SUPERVISOR,
)

# 股权激励类型
EQUITY_INCENTIVE_OPTION: str = "option"  # 期权
EQUITY_INCENTIVE_RSU: str = "rsu"  # 限制性股票单位
EQUITY_INCENTIVE_RESTRICTED_STOCK: str = "restricted_stock"  # 限制性股票
EQUITY_INCENTIVE_TYPES: tuple[str, ...] = (
    EQUITY_INCENTIVE_OPTION,
    EQUITY_INCENTIVE_RSU,
    EQUITY_INCENTIVE_RESTRICTED_STOCK,
)

# 关联交易类型
RELATED_TX_PURCHASE: str = "purchase"
RELATED_TX_SALE: str = "sale"
RELATED_TX_LOAN: str = "loan"
RELATED_TX_GUARANTEE: str = "guarantee"
RELATED_TX_FUND_TRANSFER: str = "fund_transfer"
RELATED_TX_TYPES: tuple[str, ...] = (
    RELATED_TX_PURCHASE,
    RELATED_TX_SALE,
    RELATED_TX_LOAN,
    RELATED_TX_GUARANTEE,
    RELATED_TX_FUND_TRANSFER,
)

# 关联交易定价依据
PRICING_MARKET: str = "market"
PRICING_AGREEMENT: str = "agreement"
PRICING_EVALUATION: str = "evaluation"
PRICING_BASES: tuple[str, ...] = (PRICING_MARKET, PRICING_AGREEMENT, PRICING_EVALUATION)

# 风险事件类型
RISK_EVENT_PLEDGE: str = "pledge"  # 质押
RISK_EVENT_GUARANTEE: str = "guarantee"  # 担保
RISK_EVENT_REGULATORY_PENALTY: str = "regulatory_penalty"  # 监管处罚
RISK_EVENT_TYPES: tuple[str, ...] = (
    RISK_EVENT_PLEDGE,
    RISK_EVENT_GUARANTEE,
    RISK_EVENT_REGULATORY_PENALTY,
)

# 风险严重度
RISK_SEVERITY_INFO: str = "info"
RISK_SEVERITY_WARNING: str = "warning"
RISK_SEVERITY_SEVERE: str = "severe"
RISK_SEVERITIES: tuple[str, ...] = (
    RISK_SEVERITY_INFO,
    RISK_SEVERITY_WARNING,
    RISK_SEVERITY_SEVERE,
)

# 资本动作类型
CAPITAL_ACTION_DIVIDEND: str = "dividend"  # 分红
CAPITAL_ACTION_BUYBACK: str = "buyback"  # 回购
CAPITAL_ACTION_FINANCING: str = "financing"  # 融资
CAPITAL_ACTION_MA: str = "ma"  # 并购
CAPITAL_ACTION_CAPACITY_EXPANSION: str = "capacity_expansion"  # 扩产
CAPITAL_ACTION_ASSET_DISPOSAL: str = "asset_disposal"  # 资产处置
CAPITAL_ACTION_TYPES: tuple[str, ...] = (
    CAPITAL_ACTION_DIVIDEND,
    CAPITAL_ACTION_BUYBACK,
    CAPITAL_ACTION_FINANCING,
    CAPITAL_ACTION_MA,
    CAPITAL_ACTION_CAPACITY_EXPANSION,
    CAPITAL_ACTION_ASSET_DISPOSAL,
)

# 承诺兑现状态
COMMITMENT_FULFILLED: str = "fulfilled"
COMMITMENT_PARTIALLY_FULFILLED: str = "partially_fulfilled"
COMMITMENT_UNFULFILLED: str = "unfulfilled"
COMMITMENT_PENDING: str = "pending"
COMMITMENT_STATUSES: tuple[str, ...] = (
    COMMITMENT_FULFILLED,
    COMMITMENT_PARTIALLY_FULFILLED,
    COMMITMENT_UNFULFILLED,
    COMMITMENT_PENDING,
)

# 治理风险类型
GOVERNANCE_RISK_CONTROLLER_CHANGE: str = "controller_change"
GOVERNANCE_RISK_RELATED_PARTY: str = "related_party"
GOVERNANCE_RISK_PLEDGE: str = "pledge"
GOVERNANCE_RISK_REGULATORY: str = "regulatory"
GOVERNANCE_RISK_GOVERNANCE_STRUCTURE: str = "governance_structure"
GOVERNANCE_RISK_TYPES: tuple[str, ...] = (
    GOVERNANCE_RISK_CONTROLLER_CHANGE,
    GOVERNANCE_RISK_RELATED_PARTY,
    GOVERNANCE_RISK_PLEDGE,
    GOVERNANCE_RISK_REGULATORY,
    GOVERNANCE_RISK_GOVERNANCE_STRUCTURE,
)

# Gap 严重度
GAP_SEVERITY_BLOCKER: str = "blocker"
GAP_SEVERITY_WARNING: str = "warning"
GAP_SEVERITIES: tuple[str, ...] = (GAP_SEVERITY_BLOCKER, GAP_SEVERITY_WARNING)

# Gap 类型
GAP_KIND_MISSING: str = "missing"
GAP_KIND_COMMITMENT_INCOMPLETE: str = "commitment_incomplete"
GAP_KIND_EMOTIONAL_NEWS_ASSESSMENT: str = "emotional_news_assessment"
GAP_KIND_RISK_FINANCIAL_IMPACT_MIXED: str = "risk_financial_impact_mixed"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ManagementPerson:
    """管理人员（董监高）。

    Attributes:
        person_id: 人员 ID
        name: 姓名
        title: 职位（董事长/CEO/CFO/独立董事/监事等）
        appointment_date: 任职日期 YYYY-MM-DD
        departure_date: 离任日期（在任为空）
        background: 履历摘要
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    person_id: str = ""
    name: str = ""
    title: str = ""
    appointment_date: str = ""
    departure_date: str = ""
    background: str = ""
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "person_id": self.person_id,
            "name": self.name,
            "title": self.title,
            "appointment_date": self.appointment_date,
            "departure_date": self.departure_date,
            "background": self.background,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class ShareholderChange:
    """股东/董监高变更。

    Attributes:
        change_id: 变更 ID
        change_type: 变更类型（SHAREHOLDER_CHANGE_TYPES 之一）
        person: 涉及的人员（如有）
        change_date: 变更日期 YYYY-MM-DD
        before_state: 变更前状态
        after_state: 变更后状态
        source_ref: 来源证据 ID
        available_at: 可见时点
        note: 备注
    """

    change_id: str = ""
    change_type: str = ""
    person: ManagementPerson | None = None
    change_date: str = ""
    before_state: str = ""
    after_state: str = ""
    source_ref: str = ""
    available_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "person": self.person.to_dict() if self.person else None,
            "change_date": self.change_date,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
            "note": self.note,
        }


@dataclass
class EquityIncentive:
    """股权激励与考核条件。

    Attributes:
        plan_id: 计划 ID
        plan_name: 计划名
        plan_type: 计划类型（EQUITY_INCENTIVE_TYPES 之一）
        grant_date: 授予日期 YYYY-MM-DD
        vesting_period_months: 解锁期（月）
        total_shares: 授予总股数
        exercise_price: 行权价（期权类有，限制性股票类为空）
        performance_conditions: 考核条件列表（按原始披露文本）
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    plan_id: str = ""
    plan_name: str = ""
    plan_type: str = ""
    grant_date: str = ""
    vesting_period_months: int | None = None
    total_shares: float | None = None
    exercise_price: float | None = None
    performance_conditions: list[str] = field(default_factory=list)
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "plan_type": self.plan_type,
            "grant_date": self.grant_date,
            "vesting_period_months": self.vesting_period_months,
            "total_shares": self.total_shares,
            "exercise_price": self.exercise_price,
            "performance_conditions": list(self.performance_conditions),
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class RelatedPartyTransaction:
    """关联交易。

    Attributes:
        transaction_id: 交易 ID
        transaction_type: 交易类型（RELATED_TX_TYPES 之一）
        counterparty: 关联方
        amount: 金额
        currency: 币种
        period: 期间 YYYY-MM-DD
        pricing_basis: 定价依据（PRICING_BASES 之一）
        source_ref: 来源证据 ID
        available_at: 可见时点
        note: 备注
    """

    transaction_id: str = ""
    transaction_type: str = ""
    counterparty: str = ""
    amount: float | None = None
    currency: str = ""
    period: str = ""
    pricing_basis: str = ""
    source_ref: str = ""
    available_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "transaction_id": self.transaction_id,
            "transaction_type": self.transaction_type,
            "counterparty": self.counterparty,
            "amount": self.amount,
            "currency": self.currency,
            "period": self.period,
            "pricing_basis": self.pricing_basis,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
            "note": self.note,
        }


@dataclass
class RiskEvent:
    """风险事件（质押、担保、监管处罚）。

    Attributes:
        event_id: 事件 ID
        event_type: 事件类型（RISK_EVENT_TYPES 之一）
        severity: 严重度（RISK_SEVERITIES 之一）
        event_date: 事件日期 YYYY-MM-DD
        description: 事件描述
        amount: 涉及金额
        currency: 币种
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    event_id: str = ""
    event_type: str = ""
    severity: str = ""
    event_date: str = ""
    description: str = ""
    amount: float | None = None
    currency: str = ""
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "event_date": self.event_date,
            "description": self.description,
            "amount": self.amount,
            "currency": self.currency,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class CapitalAction:
    """资本动作（分红、回购、融资、并购、扩产、资产处置）。

    Attributes:
        action_id: 动作 ID
        action_type: 动作类型（CAPITAL_ACTION_TYPES 之一）
        announcement_date: 公告日期 YYYY-MM-DD
        amount: 金额
        currency: 币种
        description: 描述
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    action_id: str = ""
    action_type: str = ""
    announcement_date: str = ""
    amount: float | None = None
    currency: str = ""
    description: str = ""
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "announcement_date": self.announcement_date,
            "amount": self.amount,
            "currency": self.currency,
            "description": self.description,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class ManagementCommitment:
    """管理层公开承诺。

    硬门禁：按"原文—时间—目标—兑现—偏差"5 项齐全保存。
    任何一项缺失即触发 blocker gap。

    Attributes:
        commitment_id: 承诺 ID
        original_text: 原文（强制非空）
        announced_date: 承诺时间（强制非空）
        target: 目标（强制非空）
        fulfillment_status: 兑现状态（COMMITMENT_STATUSES 之一）
        fulfillment_evidence_id: 兑现证据 ID（pending 状态可空）
        deviation: 偏差描述（强制非空；fulfilled 时可为"无偏差"）
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    commitment_id: str = ""
    original_text: str = ""
    announced_date: str = ""
    target: str = ""
    fulfillment_status: str = ""
    fulfillment_evidence_id: str = ""
    deviation: str = ""
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "commitment_id": self.commitment_id,
            "original_text": self.original_text,
            "announced_date": self.announced_date,
            "target": self.target,
            "fulfillment_status": self.fulfillment_status,
            "fulfillment_evidence_id": self.fulfillment_evidence_id,
            "deviation": self.deviation,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }

    def required_fields_complete(self) -> tuple[bool, list[str]]:
        """检查"原文—时间—目标—兑现—偏差"5 项是否齐全。

        Returns:
            (是否齐全, 缺失字段列表)
        """
        missing: list[str] = []
        if not self.original_text:
            missing.append("original_text")
        if not self.announced_date:
            missing.append("announced_date")
        if not self.target:
            missing.append("target")
        if not self.fulfillment_status:
            missing.append("fulfillment_status")
        if not self.deviation:
            missing.append("deviation")
        return (len(missing) == 0, missing)


@dataclass
class PersonnelAssessment:
    """人员评价。

    硬门禁：不能由情绪新闻直接生成。
    is_from_emotional_news=True 时触发 blocker，评价不进入正式结果。

    Attributes:
        assessment_id: 评价 ID
        person_name: 被评价人姓名
        assessment: 评价内容
        evidence_basis: 证据基础（必须是结构化证据，非情绪新闻）
        is_from_emotional_news: 是否来自情绪新闻（True 时阻断）
        emotional_news_detected: 检测到的情绪词列表
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    assessment_id: str = ""
    person_name: str = ""
    assessment: str = ""
    evidence_basis: str = ""
    is_from_emotional_news: bool = False
    emotional_news_detected: list[str] = field(default_factory=list)
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "assessment_id": self.assessment_id,
            "person_name": self.person_name,
            "assessment": self.assessment,
            "evidence_basis": self.evidence_basis,
            "is_from_emotional_news": self.is_from_emotional_news,
            "emotional_news_detected": list(self.emotional_news_detected),
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class GovernanceRisk:
    """治理风险。

    硬门禁：与财务预测影响分开表达。
    potential_financial_impact 仅为描述性字段，不进入预测引擎；
    若 risk_financial_impact_mixed=True 表示与预测引擎混淆，触发 blocker。

    Attributes:
        risk_id: 风险 ID
        risk_type: 风险类型（GOVERNANCE_RISK_TYPES 之一）
        severity: 严重度（RISK_SEVERITIES 之一）
        description: 风险描述
        potential_financial_impact: 潜在财务影响（仅描述性，不参与预测）
        risk_financial_impact_mixed: 是否与预测引擎混淆（True 触发 blocker）
        source_ref: 来源证据 ID
        available_at: 可见时点
    """

    risk_id: str = ""
    risk_type: str = ""
    severity: str = ""
    description: str = ""
    potential_financial_impact: str = ""
    risk_financial_impact_mixed: bool = False
    source_ref: str = ""
    available_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "risk_id": self.risk_id,
            "risk_type": self.risk_type,
            "severity": self.severity,
            "description": self.description,
            "potential_financial_impact": self.potential_financial_impact,
            "risk_financial_impact_mixed": self.risk_financial_impact_mixed,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
        }


@dataclass
class CapitalAllocationImpact:
    """资本配置对 ROIC/每股价值/财务风险的影响。

    Attributes:
        impact_id: 影响 ID
        action_ref: 关联的 CapitalAction.action_id
        roic_impact_bp: ROIC 影响（基点；如 +50bp / -30bp）
        per_share_value_impact: 每股价值影响（元）
        financial_risk_impact: 财务风险影响描述（与预测引擎分开）
        source_ref: 来源证据 ID
        available_at: 可见时点
        note: 备注
    """

    impact_id: str = ""
    action_ref: str = ""
    roic_impact_bp: float | None = None
    per_share_value_impact: float | None = None
    financial_risk_impact: str = ""
    source_ref: str = ""
    available_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "impact_id": self.impact_id,
            "action_ref": self.action_ref,
            "roic_impact_bp": self.roic_impact_bp,
            "per_share_value_impact": self.per_share_value_impact,
            "financial_risk_impact": self.financial_risk_impact,
            "source_ref": self.source_ref,
            "available_at": self.available_at,
            "note": self.note,
        }


@dataclass
class GovernanceGap:
    """缺 blocker 输入时的 gap 记录。

    Attributes:
        field_name: 缺失字段名
        reason: 缺失原因
        blocker_kind: blocker 类型
        severity: 严重度（GAP_SEVERITIES 之一）
        note: 备注
    """

    field_name: str = ""
    reason: str = ""
    blocker_kind: str = GAP_KIND_MISSING
    severity: str = GAP_SEVERITY_BLOCKER
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "field_name": self.field_name,
            "reason": self.reason,
            "blocker_kind": self.blocker_kind,
            "severity": self.severity,
            "note": self.note,
        }


@dataclass
class GovernanceLedger:
    """公司治理台账（顶层聚合）。

    Attributes:
        symbol: 公司股票代码
        company_name: 公司名称
        period: 台账基准期间 YYYY-MM-DD
        available_at: 台账整体可见时间 YYYY-MM-DD，不得用报告期代替
        shareholder_changes: 股东/董监高变更列表
        management_persons: 管理人员列表
        equity_incentives: 股权激励计划列表
        related_party_transactions: 关联交易列表
        risk_events: 风险事件列表
        capital_actions: 资本动作列表
        management_commitments: 管理层承诺列表
        personnel_assessments: 人员评价列表
        governance_risks: 治理风险列表
        capital_allocation_impacts: 资本配置影响列表
        gaps: gap 列表
        source_trace_id: 来源追溯 ID
        schema_version: 契约版本
        created_at: 创建时间
    """

    symbol: str = ""
    company_name: str = ""
    period: str = ""
    available_at: str = ""
    shareholder_changes: list[ShareholderChange] = field(default_factory=list)
    management_persons: list[ManagementPerson] = field(default_factory=list)
    equity_incentives: list[EquityIncentive] = field(default_factory=list)
    related_party_transactions: list[RelatedPartyTransaction] = field(default_factory=list)
    risk_events: list[RiskEvent] = field(default_factory=list)
    capital_actions: list[CapitalAction] = field(default_factory=list)
    management_commitments: list[ManagementCommitment] = field(default_factory=list)
    personnel_assessments: list[PersonnelAssessment] = field(default_factory=list)
    governance_risks: list[GovernanceRisk] = field(default_factory=list)
    capital_allocation_impacts: list[CapitalAllocationImpact] = field(default_factory=list)
    gaps: list[GovernanceGap] = field(default_factory=list)
    source_trace_id: str = ""
    schema_version: str = "governance_v1"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "period": self.period,
            "available_at": self.available_at,
            "shareholder_changes": [c.to_dict() for c in self.shareholder_changes],
            "management_persons": [p.to_dict() for p in self.management_persons],
            "equity_incentives": [e.to_dict() for e in self.equity_incentives],
            "related_party_transactions": [
                t.to_dict() for t in self.related_party_transactions
            ],
            "risk_events": [r.to_dict() for r in self.risk_events],
            "capital_actions": [a.to_dict() for a in self.capital_actions],
            "management_commitments": [
                c.to_dict() for c in self.management_commitments
            ],
            "personnel_assessments": [
                a.to_dict() for a in self.personnel_assessments
            ],
            "governance_risks": [r.to_dict() for r in self.governance_risks],
            "capital_allocation_impacts": [
                i.to_dict() for i in self.capital_allocation_impacts
            ],
            "gaps": [g.to_dict() for g in self.gaps],
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 情绪新闻检测
# ---------------------------------------------------------------------------

# 情绪词黑名单：含这些词的评价视为情绪新闻，不应直接生成人员评价
EMOTIONAL_NEWS_KEYWORDS: tuple[str, ...] = (
    "震惊", "意外", "突发", "惊现", "传奇", "天才",
    "庸才", "无能", "昏招", "败家", "暴富", "血洗",
    "惨败", "崩盘", "大跳水", "暴跌", "狂飙", "飙升",
)


def detect_emotional_news(text: str) -> list[str]:
    """检测文本中是否含情绪新闻关键词。

    Args:
        text: 待检测文本

    Returns:
        检测到的情绪词列表（空列表表示无）
    """
    if not isinstance(text, str) or not text:
        return []
    return [word for word in EMOTIONAL_NEWS_KEYWORDS if word in text]


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------


def validate_shareholder_change_type(change_type: str) -> None:
    """校验股东变更类型合法性。

    Args:
        change_type: 类型字符串

    Raises:
        ValueError: 当 change_type 不在 SHAREHOLDER_CHANGE_TYPES 中
    """
    if change_type and change_type not in SHAREHOLDER_CHANGE_TYPES:
        raise ValueError(
            f"非法 shareholder change type: {change_type}; "
            f"允许值: {SHAREHOLDER_CHANGE_TYPES}"
        )


def validate_equity_incentive_type(plan_type: str) -> None:
    """校验股权激励类型合法性。"""
    if plan_type and plan_type not in EQUITY_INCENTIVE_TYPES:
        raise ValueError(
            f"非法 equity incentive type: {plan_type}; "
            f"允许值: {EQUITY_INCENTIVE_TYPES}"
        )


def validate_related_tx_type(tx_type: str) -> None:
    """校验关联交易类型合法性。"""
    if tx_type and tx_type not in RELATED_TX_TYPES:
        raise ValueError(
            f"非法 related transaction type: {tx_type}; "
            f"允许值: {RELATED_TX_TYPES}"
        )


def validate_pricing_basis(basis: str) -> None:
    """校验关联交易定价依据合法性。"""
    if basis and basis not in PRICING_BASES:
        raise ValueError(
            f"非法 pricing basis: {basis}; 允许值: {PRICING_BASES}"
        )


def validate_risk_event_type(event_type: str) -> None:
    """校验风险事件类型合法性。"""
    if event_type and event_type not in RISK_EVENT_TYPES:
        raise ValueError(
            f"非法 risk event type: {event_type}; "
            f"允许值: {RISK_EVENT_TYPES}"
        )


def validate_risk_severity(severity: str) -> None:
    """校验风险严重度合法性。"""
    if severity and severity not in RISK_SEVERITIES:
        raise ValueError(
            f"非法 risk severity: {severity}; 允许值: {RISK_SEVERITIES}"
        )


def validate_capital_action_type(action_type: str) -> None:
    """校验资本动作类型合法性。"""
    if action_type and action_type not in CAPITAL_ACTION_TYPES:
        raise ValueError(
            f"非法 capital action type: {action_type}; "
            f"允许值: {CAPITAL_ACTION_TYPES}"
        )


def validate_commitment_status(status: str) -> None:
    """校验承诺兑现状态合法性。"""
    if status and status not in COMMITMENT_STATUSES:
        raise ValueError(
            f"非法 commitment status: {status}; "
            f"允许值: {COMMITMENT_STATUSES}"
        )


def validate_governance_risk_type(risk_type: str) -> None:
    """校验治理风险类型合法性。"""
    if risk_type and risk_type not in GOVERNANCE_RISK_TYPES:
        raise ValueError(
            f"非法 governance risk type: {risk_type}; "
            f"允许值: {GOVERNANCE_RISK_TYPES}"
        )
