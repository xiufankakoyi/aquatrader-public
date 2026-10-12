"""P2.6 差异化观点对象契约。

按 P2 计划 §208-227 Task P2.6：
- VariantPerception：市场观点 vs 内部模型 vs 差异变量
- 强制规则：
  1. market_view 不可由系统臆测，必须来自共识或市场隐含模型
  2. internal_view 必须来自 P1 可审计模型
  3. 差异必须落到具体经营驱动，而不是「我们更乐观」
  4. 催化只描述验证事件和时间，不输出交易动作

验收点：
- 任一观点可定位市场输入、内部模型和差异变量
- 缺市场观点或内部模型时不能形成正式 VariantPerception
- 观点版本不可覆盖
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# MarketView 来源类型（验收点：market_view 不可由系统臆测）
MARKET_VIEW_SOURCE_CONSENSUS: str = "consensus"
MARKET_VIEW_SOURCE_IMPLIED: str = "implied"
MARKET_VIEW_SOURCE_GUIDANCE: str = "company_guidance"

MARKET_VIEW_SOURCES: tuple[str, ...] = (
    MARKET_VIEW_SOURCE_CONSENSUS,
    MARKET_VIEW_SOURCE_IMPLIED,
    MARKET_VIEW_SOURCE_GUIDANCE,
)

# InternalView 来源类型（验收点：internal_view 必须来自 P1 可审计模型）
INTERNAL_VIEW_SOURCE_P1: str = "p1_model"

# 观点状态
THESIS_STATUS_DRAFT: str = "draft"
THESIS_STATUS_ACTIVE: str = "active"
THESIS_STATUS_INVALIDATED: str = "invalidated"
THESIS_STATUS_SUPERSEDED: str = "superseded"
THESIS_STATUS_EXPIRED: str = "expired"

THESIS_STATUSES: tuple[str, ...] = (
    THESIS_STATUS_DRAFT,
    THESIS_STATUS_ACTIVE,
    THESIS_STATUS_INVALIDATED,
    THESIS_STATUS_SUPERSEDED,
    THESIS_STATUS_EXPIRED,
)

# 验证类型（催化剂）
VALIDATION_TYPE_EARNINGS_RELEASE: str = "earnings_release"
VALIDATION_TYPE_POLICY_ANNOUNCEMENT: str = "policy_announcement"
VALIDATION_TYPE_PRODUCT_LAUNCH: str = "product_launch"
VALIDATION_TYPE_INDUSTRY_DATA: str = "industry_data"
VALIDATION_TYPE_MANAGEMENT_UPDATE: str = "management_update"

VALIDATION_TYPES: tuple[str, ...] = (
    VALIDATION_TYPE_EARNINGS_RELEASE,
    VALIDATION_TYPE_POLICY_ANNOUNCEMENT,
    VALIDATION_TYPE_PRODUCT_LAUNCH,
    VALIDATION_TYPE_INDUSTRY_DATA,
    VALIDATION_TYPE_MANAGEMENT_UPDATE,
)

# 估值含义方向
VALUATION_DIRECTION_HIGHER: str = "higher"
VALUATION_DIRECTION_LOWER: str = "lower"
VALUATION_DIRECTION_NEUTRAL: str = "neutral"

VALUATION_DIRECTIONS: tuple[str, ...] = (
    VALUATION_DIRECTION_HIGHER,
    VALUATION_DIRECTION_LOWER,
    VALUATION_DIRECTION_NEUTRAL,
)

VALUATION_MAGNITUDE_SMALL: str = "small"
VALUATION_MAGNITUDE_MEDIUM: str = "medium"
VALUATION_MAGNITUDE_LARGE: str = "large"

VALUATION_MAGNITUDES: tuple[str, ...] = (
    VALUATION_MAGNITUDE_SMALL,
    VALUATION_MAGNITUDE_MEDIUM,
    VALUATION_MAGNITUDE_LARGE,
)

# 复核频率
REVIEW_FREQUENCY_QUARTERLY: str = "quarterly"
REVIEW_FREQUENCY_EVENT_DRIVEN: str = "event_driven"
REVIEW_FREQUENCY_MONTHLY: str = "monthly"

# 拒绝原因码（验收点 2）
REJECT_MISSING_MARKET_VIEW: str = "missing_market_view"
REJECT_MISSING_INTERNAL_VIEW: str = "missing_internal_view"
REJECT_MISSING_DRIVER_DIFFERENCES: str = "missing_driver_differences"
REJECT_GENERIC_DIFFERENCE: str = "generic_difference_no_driver"
REJECT_CATALYST_TRADE_ACTION: str = "catalyst_contains_trade_action"
REJECT_INVALIDATION_GENERIC: str = "invalidation_too_generic"

# AGENTS.md 禁词（催化剂 / 估值含义 / 证伪条件不允许出现交易动作词）
FORBIDDEN_TRADE_ACTION_KEYWORDS: tuple[str, ...] = (
    "买入",
    "卖出",
    "止盈",
    "止损",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "调仓",
)

# 通用「整体更乐观/悲观」类无具体驱动的关键词（验收点：差异必须落到具体经营驱动）
GENERIC_DIFFERENCE_KEYWORDS: tuple[str, ...] = (
    "我们更乐观",
    "我们更悲观",
    "整体更乐观",
    "整体更悲观",
    "更看好",
    "更看淡",
)


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 市场观点 / 内部观点
# ---------------------------------------------------------------------------


@dataclass
class MarketView:
    """市场观点。

    强制规则 1：market_view 不可由系统臆测，必须来自共识或市场隐含模型。

    Attributes:
        source_type: 来源类型（MARKET_VIEW_SOURCES 之一）
        source_ref: 来源引用 ID（如 consensus_summary_id / implied_summary_id / guidance_id）
        as_of: PIT 时点
        summary: 观点简述（自然语言）
        metric_values: 关键指标值（如 {"revenue_2027e": {"median": 100.0, "p25": 95, "p75": 105}}）
        retrieved_at: 检索时间
    """

    source_type: str = ""
    source_ref: str = ""
    as_of: str = ""
    summary: str = ""
    metric_values: dict[str, Any] = field(default_factory=dict)
    retrieved_at: str = ""

    def is_valid(self) -> bool:
        """校验市场观点是否可定位（验收点 1）。"""
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (self.source_ref, self.as_of)
        ) and self.source_type in MARKET_VIEW_SOURCES

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "as_of": self.as_of,
            "summary": self.summary,
            "metric_values": dict(self.metric_values)
            if isinstance(self.metric_values, Mapping)
            else {},
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class InternalView:
    """内部观点。

    强制规则 2：internal_view 必须来自 P1 可审计模型。

    Attributes:
        model_name: P1 模型名（如 operating_driver_builder / valuation_engine / fundamental_quality_engine）
        model_version: 模型版本
        source_ref: 来源引用 ID（如 operating_model_id / valuation_case_id / quality_score_id）
        as_of: PIT 时点
        summary: 观点简述
        metric_values: 关键指标值
        computed_at: 计算时间
    """

    model_name: str = ""
    model_version: str = ""
    source_ref: str = ""
    as_of: str = ""
    summary: str = ""
    metric_values: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""

    def is_valid(self) -> bool:
        """校验内部观点是否可定位（验收点 1）。"""
        return all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.model_name,
                self.model_version,
                self.source_ref,
                self.as_of,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "source_ref": self.source_ref,
            "as_of": self.as_of,
            "summary": self.summary,
            "metric_values": dict(self.metric_values)
            if isinstance(self.metric_values, Mapping)
            else {},
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# 差异变量
# ---------------------------------------------------------------------------


@dataclass
class DriverDifference:
    """单一经营驱动差异。

    强制规则 3：差异必须落到具体经营驱动，而不是「我们更乐观」。

    Attributes:
        driver_name: 驱动名（必须非空，对应 P1.3 OperatingDriver.name）
        market_value: 市场观点中的值（None 表示市场未覆盖）
        internal_value: 内部模型中的值
        delta: 差值（internal - market）
        relative_delta: 相对差值（delta / abs(market)）
        unit: 单位
        period: 期间
        reasoning: 差异推理（必须非空，说明差异来源）
        evidence_refs: 支撑证据 ID 列表
    """

    driver_name: str = ""
    market_value: float | None = None
    internal_value: float | None = None
    delta: float | None = None
    relative_delta: float | None = None
    unit: str = ""
    period: str = ""
    reasoning: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        """校验差异是否具体可定位。"""
        if not isinstance(self.driver_name, str) or not self.driver_name.strip():
            return False
        if not isinstance(self.reasoning, str) or not self.reasoning.strip():
            return False
        # market_value 与 internal_value 至少一个非空
        if self.market_value is None and self.internal_value is None:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "driver_name": self.driver_name,
            "market_value": self.market_value,
            "internal_value": self.internal_value,
            "delta": self.delta,
            "relative_delta": self.relative_delta,
            "unit": self.unit,
            "period": self.period,
            "reasoning": self.reasoning,
            "evidence_refs": list(self.evidence_refs)
            if isinstance(self.evidence_refs, (list, tuple))
            else [],
        }


@dataclass
class ValuationImplication:
    """估值含义。

    Attributes:
        direction: higher / lower / neutral（内部估值 vs 市场估值）
        magnitude_band: small / medium / large
        reasoning: 推理（必须非空）
        evidence_refs: 证据 ID 列表
    """

    direction: str = VALUATION_DIRECTION_NEUTRAL
    magnitude_band: str = VALUATION_MAGNITUDE_SMALL
    reasoning: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        """校验估值含义。"""
        return (
            self.direction in VALUATION_DIRECTIONS
            and self.magnitude_band in VALUATION_MAGNITUDES
            and bool(self.reasoning)
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "direction": self.direction,
            "magnitude_band": self.magnitude_band,
            "reasoning": self.reasoning,
            "evidence_refs": list(self.evidence_refs)
            if isinstance(self.evidence_refs, (list, tuple))
            else [],
        }


# ---------------------------------------------------------------------------
# 催化剂 / 验证时间表 / 证伪条件
# ---------------------------------------------------------------------------


@dataclass
class ThesisCatalyst:
    """观点催化剂。

    强制规则 4：催化只描述验证事件和时间，不输出交易动作。

    Attributes:
        event_description: 事件描述（仅描述验证事件，不含交易动作词）
        expected_date: 预期日期 ISO
        validation_type: 验证类型（VALIDATION_TYPES 之一）
        evidence_refs: 证据 ID 列表
    """

    event_description: str = ""
    expected_date: str = ""
    validation_type: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def contains_trade_action(self) -> bool:
        """检查事件描述是否含交易动作关键词。"""
        if not self.event_description:
            return False
        return any(kw in self.event_description for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS)

    def is_valid(self) -> bool:
        """校验催化剂。"""
        if not self.event_description:
            return False
        if not self.expected_date:
            return False
        if self.contains_trade_action():
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "event_description": self.event_description,
            "expected_date": self.expected_date,
            "validation_type": self.validation_type,
            "evidence_refs": list(self.evidence_refs)
            if isinstance(self.evidence_refs, (list, tuple))
            else [],
        }


@dataclass
class ValidationSchedule:
    """验证时间表。

    Attributes:
        next_review_date: 下次复核日期 ISO
        frequency: 复核频率（quarterly / event_driven / monthly）
        triggers: 触发条件列表（如 ["季报发布", "行业政策变化"]）
    """

    next_review_date: str = ""
    frequency: str = REVIEW_FREQUENCY_QUARTERLY
    triggers: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        """校验时间表。"""
        return (
            isinstance(self.next_review_date, str)
            and bool(self.next_review_date.strip())
            and isinstance(self.frequency, str)
            and self.frequency
            in {
                REVIEW_FREQUENCY_QUARTERLY,
                REVIEW_FREQUENCY_EVENT_DRIVEN,
                REVIEW_FREQUENCY_MONTHLY,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "next_review_date": self.next_review_date,
            "frequency": self.frequency,
            "triggers": list(self.triggers)
            if isinstance(self.triggers, (list, tuple))
            else [],
        }


@dataclass
class InvalidationCondition:
    """证伪条件。

    Attributes:
        condition_description: 条件描述（必须具体可验证，不接受「业绩不及预期」类空泛描述）
        threshold: 阈值（None 表示定性条件）
        monitor_metric: 监控指标
        monitor_source: 监控数据源
    """

    condition_description: str = ""
    threshold: float | None = None
    monitor_metric: str = ""
    monitor_source: str = ""

    def is_valid(self) -> bool:
        """校验证伪条件具体可验证。"""
        if (
            not isinstance(self.condition_description, str)
            or not self.condition_description.strip()
        ):
            return False
        # 不接受「业绩不及预期」「市场波动」类空泛描述
        vague_terms = ("不及预期", "市场波动", "宏观变化", "其他因素")
        if any(term in self.condition_description for term in vague_terms):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "condition_description": self.condition_description,
            "threshold": self.threshold,
            "monitor_metric": self.monitor_metric,
            "monitor_source": self.monitor_source,
        }


# ---------------------------------------------------------------------------
# VariantPerception 主对象
# ---------------------------------------------------------------------------


def _safe_uncertainty(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not isfinite(number):
        return 0.0
    return round(min(max(number, 0.0), 1.0), 4)


@dataclass
class VariantPerception:
    """差异化观点对象。

    P2.6 §217-227 必须包含字段：
    thesis_id, symbol, as_of,
    market_view, internal_view, difference,
    driver_differences, valuation_implication,
    supporting_fact_ids, counter_evidence_ids,
    catalysts, validation_schedule,
    invalidation_conditions, uncertainty,
    owner, reviewer, model_versions, status

    Attributes:
        thesis_id: 观点唯一 ID（由业务键 + version 派生，确定性）
        symbol: 公司代码
        as_of: PIT 时点
        market_view: 市场观点（必须来自共识/隐含/指引）
        internal_view: 内部观点（必须来自 P1 可审计模型）
        difference: 差异总结（自然语言）
        driver_differences: 经营驱动差异列表（必须非空且每条具体可定位）
        valuation_implication: 估值含义
        supporting_fact_ids: 支撑事实 ID 列表
        counter_evidence_ids: 反证 ID 列表
        catalysts: 催化剂列表（仅描述验证事件和时间）
        validation_schedule: 验证时间表
        invalidation_conditions: 证伪条件列表
        uncertainty: 不确定性（0-1）
        owner: 主分析师
        reviewer: 复核人
        model_versions: 模型版本字典（如 {"operating_driver_builder": "v1.3", "valuation_engine": "v2.1"}）
        status: 状态（draft / active / invalidated / superseded / expired）
        version: 版本号（从 1 开始）
        revision_of: 旧版本 thesis_id（版本链）
        created_at: 创建时间
        rejection_reason: 拒绝原因码（当无法从 draft 提升为 active 时填充）
        rejection_details: 拒绝详情
    """

    thesis_id: str = ""
    symbol: str = ""
    as_of: str = ""

    market_view: MarketView = field(default_factory=MarketView)
    internal_view: InternalView = field(default_factory=InternalView)
    difference: str = ""

    driver_differences: list[DriverDifference] = field(default_factory=list)
    valuation_implication: ValuationImplication = field(default_factory=ValuationImplication)

    supporting_fact_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)

    catalysts: list[ThesisCatalyst] = field(default_factory=list)
    validation_schedule: ValidationSchedule = field(default_factory=ValidationSchedule)
    invalidation_conditions: list[InvalidationCondition] = field(default_factory=list)

    uncertainty: float = 0.0

    owner: str = ""
    reviewer: str = ""
    model_versions: dict[str, str] = field(default_factory=dict)
    status: str = THESIS_STATUS_DRAFT

    version: int = 1
    revision_of: str = ""
    created_at: str = ""

    rejection_reason: str = ""
    rejection_details: str = ""

    def __post_init__(self) -> None:
        """派生 thesis_id（如果未提供）+ 默认 created_at。"""
        if not isinstance(self.market_view, MarketView):
            self.market_view = MarketView()
        if not isinstance(self.internal_view, InternalView):
            self.internal_view = InternalView()
        if self.valuation_implication is None:
            self.valuation_implication = ValuationImplication()
        if self.driver_differences is None:
            self.driver_differences = []
        if self.catalysts is None:
            self.catalysts = []
        if self.invalidation_conditions is None:
            self.invalidation_conditions = []
        if self.supporting_fact_ids is None:
            self.supporting_fact_ids = []
        if self.counter_evidence_ids is None:
            self.counter_evidence_ids = []
        if not self.created_at:
            self.created_at = utc_now()
        if not self.thesis_id:
            self.thesis_id = _derive_thesis_id(
                symbol=self.symbol,
                as_of=self.as_of,
                market_source_ref=self.market_view.source_ref,
                internal_source_ref=self.internal_view.source_ref,
                version=self.version,
            )

    # ------------------------------------------------------------------
    # 校验：是否可提升为 active（验收点 2）
    # ------------------------------------------------------------------

    def can_promote_to_active(self) -> tuple[bool, str, str]:
        """检查是否可以从 draft 提升为 active。

        验收点 2：缺市场观点或内部模型时不能形成正式 VariantPerception。

        Returns:
            (can_promote, reason_code, details)
            - can_promote=True 时 reason_code/details 为空
            - can_promote=False 时填充拒绝原因码与详情
        """
        # 1. 市场观点必须可定位
        if not isinstance(self.market_view, MarketView) or not self.market_view.is_valid():
            return (
                False,
                REJECT_MISSING_MARKET_VIEW,
                "market_view 缺失或不可定位（source_type/source_ref/as_of 必填）",
            )

        # 2. 内部观点必须可定位
        if not isinstance(self.internal_view, InternalView) or not self.internal_view.is_valid():
            return (
                False,
                REJECT_MISSING_INTERNAL_VIEW,
                "internal_view 缺失或不可定位（model_name/model_version/source_ref/as_of 必填）",
            )

        # 3. 驱动差异必须非空且每条具体可定位
        if not self.driver_differences:
            return (
                False,
                REJECT_MISSING_DRIVER_DIFFERENCES,
                "driver_differences 为空（差异必须落到具体经营驱动）",
            )
        for i, diff in enumerate(self.driver_differences):
            if not isinstance(diff, DriverDifference) or not diff.is_valid():
                return (
                    False,
                    REJECT_GENERIC_DIFFERENCE,
                    f"driver_differences[{i}] 不具体可定位（driver_name/reasoning 必填，"
                    f"market_value/internal_value 至少一个非空）",
                )

        # 4. 差异总结不能仅含「整体更乐观/悲观」类无驱动描述
        if self.difference:
            if any(kw in self.difference for kw in GENERIC_DIFFERENCE_KEYWORDS):
                return (
                    False,
                    REJECT_GENERIC_DIFFERENCE,
                    "difference 仅含定性描述（如「我们更乐观」），未落到具体经营驱动",
                )

        # 5. 催化剂不能含交易动作词
        for i, cat in enumerate(self.catalysts):
            if not isinstance(cat, ThesisCatalyst):
                return False, REJECT_CATALYST_TRADE_ACTION, f"catalysts[{i}] 格式无效"
            if cat.contains_trade_action():
                return (
                    False,
                    REJECT_CATALYST_TRADE_ACTION,
                    f"catalysts[{i}] 含交易动作关键词（"
                    f"{', '.join(FORBIDDEN_TRADE_ACTION_KEYWORDS)}）",
                )

        # 6. 证伪条件不能过于空泛
        for i, cond in enumerate(self.invalidation_conditions):
            if not isinstance(cond, InvalidationCondition) or not cond.is_valid():
                return (
                    False,
                    REJECT_INVALIDATION_GENERIC,
                    f"invalidation_conditions[{i}] 过于空泛（不接受「业绩不及预期」类描述）",
                )

        return True, "", ""

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "thesis_id": self.thesis_id,
            "symbol": self.symbol,
            "as_of": self.as_of,
            "market_view": self.market_view.to_dict(),
            "internal_view": self.internal_view.to_dict(),
            "difference": self.difference,
            "driver_differences": [
                d.to_dict()
                for d in self.driver_differences
                if isinstance(d, DriverDifference)
            ],
            "valuation_implication": (
                self.valuation_implication.to_dict()
                if isinstance(self.valuation_implication, ValuationImplication)
                else ValuationImplication().to_dict()
            ),
            "supporting_fact_ids": list(self.supporting_fact_ids)
            if isinstance(self.supporting_fact_ids, (list, tuple))
            else [],
            "counter_evidence_ids": list(self.counter_evidence_ids)
            if isinstance(self.counter_evidence_ids, (list, tuple))
            else [],
            "catalysts": [c.to_dict() for c in self.catalysts if isinstance(c, ThesisCatalyst)],
            "validation_schedule": (
                self.validation_schedule.to_dict()
                if isinstance(self.validation_schedule, ValidationSchedule)
                else ValidationSchedule().to_dict()
            ),
            "invalidation_conditions": [
                c.to_dict()
                for c in self.invalidation_conditions
                if isinstance(c, InvalidationCondition)
            ],
            "uncertainty": _safe_uncertainty(self.uncertainty),
            "owner": self.owner,
            "reviewer": self.reviewer,
            "model_versions": dict(self.model_versions)
            if isinstance(self.model_versions, Mapping)
            else {},
            "status": self.status,
            "version": self.version,
            "revision_of": self.revision_of,
            "created_at": self.created_at,
            "rejection_reason": self.rejection_reason,
            "rejection_details": self.rejection_details,
        }


def _derive_thesis_id(
    *,
    symbol: str,
    as_of: str,
    market_source_ref: str,
    internal_source_ref: str,
    version: int,
) -> str:
    """确定性派生 thesis_id。

    相同 (symbol, as_of, market_source_ref, internal_source_ref, version) → 相同 thesis_id。
    """
    raw = f"{symbol}|{as_of}|{market_source_ref}|{internal_source_ref}|{version}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"vp_{symbol}_{as_of}_{h}"
