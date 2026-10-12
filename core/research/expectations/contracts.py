"""Stage 3 contracts for market expectations, expectation gaps, and falsifiable theses.

扩展版本：
- MarketExpectation 统一对象，保留来源和置信度，不同来源禁止混并
- ExpectationSource 6 级优先级枚举（P1.7 扩展为 7 级，新增 AQUATRADE_FORECAST）
- ExpectationGap 增加 relative_gap / gap_confidence / catalyst_window / valuation_level 等
- InvestmentThesis 增加 sector_id / thesis_summary / confidence / uncertainty / version 等
- ThesisStatus 增加 EVIDENCE_PENDING / HIGH_CONVICTION / CONFLICTED / ARCHIVED
- ImpliedExpectationInput 用于反推隐含预期
- P1.7 新增 AquatradeForecast：独立第四类预期，与公司指引/一致预期/隐含预期并列
- P1.7 新增 ConsensusSummary：聚合一致预期统计（中位数/分位数/来源数/分歧度/修正方向）
- P1.7 ConsensusEstimate 增加 institution_id / analyst_id 字段，支持分机构版本序列
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from core.research.earnings.interval import Interval


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


def _as_dict(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _dict_items(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ExpectationKind(str, Enum):
    """预期类型（兼容旧值，P1.7 扩展为 4 类独立域）。"""
    COMPANY_GUIDANCE = "company_guidance"
    CONSENSUS = "consensus"
    IMPLIED = "implied"
    AQUATRADE_FORECAST = "aquatrade_forecast"


class ExpectationSource(str, Enum):
    """预期来源 7 级优先级（P1.7 扩展）。

    优先级从高到低：
    1. 公司正式指引
    2. 公开一致预期
    3. 券商盈利预测
    4. 市场价格隐含预期
    5. AquaTrade 独立研究预测
    6. 用户显式输入
    7. 系统推断
    """
    COMPANY_GUIDANCE = "company_guidance"
    CONSENSUS = "consensus"
    ANALYST_FORECAST = "analyst_forecast"
    IMPLIED = "implied"
    AQUATRADE_FORECAST = "aquatrade_forecast"
    USER_INPUT = "user_input"
    SYSTEM_INFERRED = "system_inferred"

    @classmethod
    def priority(cls, source: str) -> int:
        """返回来源优先级，数字越小优先级越高。"""
        order = {
            cls.COMPANY_GUIDANCE.value: 1,
            cls.CONSENSUS.value: 2,
            cls.ANALYST_FORECAST.value: 3,
            cls.IMPLIED.value: 4,
            cls.AQUATRADE_FORECAST.value: 5,
            cls.USER_INPUT.value: 6,
            cls.SYSTEM_INFERRED.value: 7,
        }
        return order.get(source, 99)

    @classmethod
    def four_independent_domains(cls) -> tuple[str, ...]:
        """返回 P1.7 要求的四类独立预期来源。

        公司指引、第三方一致预期、市场隐含预期和 AquaTrade 预测互相独立，
        不得互相覆盖。系统推断与用户输入不属于四类独立预期。
        """
        return (
            cls.COMPANY_GUIDANCE.value,
            cls.CONSENSUS.value,
            cls.IMPLIED.value,
            cls.AQUATRADE_FORECAST.value,
        )


class ThesisStatus(str, Enum):
    """投资观点状态。"""
    DRAFT = "draft"
    EVIDENCE_PENDING = "evidence_pending"
    ACTIVE = "active"
    HIGH_CONVICTION = "high_conviction"
    CONFLICTED = "conflicted"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    ARCHIVED = "archived"

    @classmethod
    def invalid_set(cls) -> set[str]:
        """返回使观点失效的 Claim 状态集合。"""
        return {"CONFLICTED", "REJECTED", "DORMANT", "EXPIRED"}

    @classmethod
    def active_set(cls) -> set[str]:
        """返回活跃状态集合。"""
        return {cls.ACTIVE.value, cls.HIGH_CONVICTION.value}


class GapStatus(str, Enum):
    """预期差状态。"""
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    EXPECTATION_DATA_MISSING = "expectation_data_missing"
    DEGRADED = "degraded"


# ---------------------------------------------------------------------------
# 预期值与来源对象
# ---------------------------------------------------------------------------

@dataclass
class ExpectationValue:
    """预期值原子单元，保留来源和置信度。"""
    metric: str = ""
    value: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    unit: str = ""
    currency: str = ""
    period: str = ""
    as_of: str = ""
    data_version: str = ""
    source_claim_id: str = ""
    source_evidence_id: str = ""
    extraction_method: str = ""
    confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    published_at: str = ""
    retrieved_at: str = ""

    def is_complete(self) -> bool:
        """检查所有必填字段是否已填写。"""
        return all((self.metric, self.unit, self.period, self.as_of, self.data_version,
                    self.source_claim_id, self.source_evidence_id, self.extraction_method,
                    self.published_at, self.retrieved_at))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExpectationValue":
        data = _as_dict(raw)
        data["value"] = Interval.from_dict(data.get("value", {})) or Interval(0.0, 0.0)
        data["confidence"] = Interval.from_dict(data.get("confidence", {})) or Interval(0.0, 0.0)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompanyGuidance:
    """公司正式指引。"""
    guidance_id: str = field(default_factory=lambda: new_id("guid"))
    company_symbol: str = ""
    values: list[ExpectationValue] = field(default_factory=list)
    revision_of: str = ""
    revision_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    kind: str = ExpectationKind.COMPANY_GUIDANCE.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CompanyGuidance":
        data = _as_dict(raw)
        data["values"] = [ExpectationValue.from_dict(v) for v in _dict_items(data.get("values"))]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ConsensusEstimate:
    """公开一致预期（P1.7 扩展：支持分机构/分发布日期/分预测期间版本序列）。

    每条记录代表一家机构在某发布日期对某预测期间的预期。
    通过 institution_id + ExpectationValue.published_at + ExpectationValue.period
    组合形成版本序列；revision_of 链接前序修订。
    """
    consensus_id: str = field(default_factory=lambda: new_id("cons"))
    company_symbol: str = ""
    values: list[ExpectationValue] = field(default_factory=list)
    provider: str = ""
    institution_id: str = ""
    analyst_id: str = ""
    sample_size: int = 0
    dispersion: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    revision_of: str = ""
    created_at: str = field(default_factory=utc_now)
    kind: str = ExpectationKind.CONSENSUS.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConsensusEstimate":
        data = _as_dict(raw)
        data["values"] = [ExpectationValue.from_dict(v) for v in _dict_items(data.get("values"))]
        data["dispersion"] = Interval.from_dict(data.get("dispersion", {})) or Interval(0.0, 0.0)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ImpliedExpectation:
    """价格隐含预期。"""
    implied_id: str = field(default_factory=lambda: new_id("impl"))
    company_symbol: str = ""
    values: list[ExpectationValue] = field(default_factory=list)
    model_name: str = ""
    model_version: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    market_data_version: str = ""
    created_at: str = field(default_factory=utc_now)
    kind: str = ExpectationKind.IMPLIED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ImpliedExpectation":
        data = _as_dict(raw)
        data["values"] = [ExpectationValue.from_dict(v) for v in _dict_items(data.get("values"))]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AquatradeForecast:
    """AquaTrade 独立研究预测（P1.7 第四类独立预期）。

    与公司指引、第三方一致预期、市场隐含预期并列存储，互不覆盖。
    可关联 P1.4 的 IntegratedForecast.forecast_id，但保留独立版本与发布记录。
    不得冒充公司指引或一致预期。
    """
    forecast_id: str = field(default_factory=lambda: new_id("aqf"))
    company_symbol: str = ""
    values: list[ExpectationValue] = field(default_factory=list)
    integrated_forecast_id: str = ""
    model_version: str = ""
    scenario: str = ""
    as_of: str = ""
    forecast_period: str = ""
    assumptions_snapshot: dict[str, Any] = field(default_factory=dict)
    formula_trace: list[str] = field(default_factory=list)
    revision_of: str = ""
    revision_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    kind: str = ExpectationKind.AQUATRADE_FORECAST.value
    expectation_source: str = ExpectationSource.AQUATRADE_FORECAST.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AquatradeForecast":
        data = _as_dict(raw)
        data["values"] = [ExpectationValue.from_dict(v) for v in _dict_items(data.get("values"))]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ConsensusSummary:
    """一致预期聚合统计（P1.7）。

    从多家机构 ConsensusEstimate 聚合得到：
    - 中位数/均值/分位数/极值
    - 来源数（参与机构数）
    - 分歧度（标准差/极差）
    - 修正方向（与上一期相比 up/down/flat/unknown）

    来源数不足时 status="insufficient_sources"，预期差不可计算。
    """
    summary_id: str = field(default_factory=lambda: new_id("csum"))
    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    as_of: str = ""
    median: float = 0.0
    mean: float = 0.0
    p25: float = 0.0
    p75: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0
    source_count: int = 0
    dispersion_std: float = 0.0
    dispersion_range: float = 0.0
    revision_direction: str = "unknown"
    revision_magnitude: float = 0.0
    previous_summary_id: str = ""
    contributing_estimate_ids: list[str] = field(default_factory=list)
    contributing_providers: list[str] = field(default_factory=list)
    status: str = "ok"
    min_sources_threshold: int = 3
    notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConsensusSummary":
        data = _as_dict(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class MarketExpectation:
    """统一市场预期对象。

    不同来源禁止混成一个数字，每个实例对应单一来源。
    保留来源类型、来源数量、置信度和证据 ID。
    """
    expectation_id: str = field(default_factory=lambda: new_id("mkt"))
    symbol: str = ""
    expected_revenue: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    expected_net_profit: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    expected_EPS: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    valuation_multiple: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    expectation_source: str = ExpectationSource.CONSENSUS.value
    source_count: int = 0
    available_at: str = ""
    forecast_period: str = ""
    confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    evidence_ids: list[str] = field(default_factory=list)
    version: str = "v1"
    currency: str = "CNY"
    published_at: str = ""
    retrieved_at: str = ""
    created_at: str = field(default_factory=utc_now)

    @property
    def source_priority(self) -> int:
        """来源优先级，数字越小优先级越高。"""
        return ExpectationSource.priority(self.expectation_source)

    @property
    def is_implied(self) -> bool:
        """是否为反推隐含预期。"""
        return self.expectation_source == ExpectationSource.IMPLIED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketExpectation":
        data = _as_dict(raw)
        for key in ("expected_revenue", "expected_net_profit", "expected_EPS",
                    "valuation_multiple", "confidence"):
            data[key] = Interval.from_dict(data.get(key, {})) or Interval(0.0, 0.0)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ImpliedExpectationInput:
    """反推隐含预期所需的市场数据输入。

    用于从当前估值反推市场隐含的增长率、利润率等假设。
    反推结果必须标记为 implied，不得标记为 consensus。
    """
    symbol: str = ""
    current_price: float = 0.0
    shares_outstanding: float = 0.0
    trailing_EPS: float = 0.0
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    growth_rate_assumed: float = 0.0
    margin_assumed: float = 0.0
    market_data_version: str = ""
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValuationSnapshot:
    """估值快照（当前估值/历史分位/拥挤度），不混入事实置信度。"""
    snapshot_id: str = field(default_factory=lambda: new_id("val"))
    company_symbol: str = ""
    as_of: str = ""
    current_valuation: dict[str, float] = field(default_factory=dict)
    historical_percentile: dict[str, float] = field(default_factory=dict)
    crowding: dict[str, float] = field(default_factory=dict)
    market_data_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 预期差
# ---------------------------------------------------------------------------

@dataclass
class ExpectationGap:
    """预期差对象。

    earnings_gap = AquaResearch 基准情景盈利预测 - 市场预期盈利
    relative_gap = earnings_gap / abs(市场预期盈利)
    """
    gap_id: str = field(default_factory=lambda: new_id("gap"))
    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    research_value: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    expectation_value: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    gap: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    relative_gap: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    expectation_kind: str = ""
    expectation_source: str = ""
    source_priority: int = 99
    earnings_result_id: str = ""
    earnings_data_version: str = ""
    expectation_id: str = ""
    market_data_version: str = ""
    as_of: str = ""
    status: str = GapStatus.OK.value
    gap_confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    catalyst_window: str = ""
    valuation_level: str = ""
    crowding_available: bool = False
    revision_speed: str = ""
    data_expires_at: str = ""
    gaps: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExpectationGap":
        data = _as_dict(raw)
        for key in ("research_value", "expectation_value", "gap", "relative_gap", "gap_confidence"):
            data[key] = Interval.from_dict(data.get(key, {})) or Interval(0.0, 0.0)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# 催化剂与审计
# ---------------------------------------------------------------------------

@dataclass
class Catalyst:
    """催化剂对象。"""
    description: str = ""
    window_start: str = ""
    window_end: str = ""
    claim_id: str = ""
    evidence_id: str = ""
    probability: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Catalyst":
        data = _as_dict(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def is_expired(self, as_of: str = "") -> bool:
        """判断催化剂窗口是否已过期。"""
        if not self.window_end:
            return False
        from .engine import _time
        end = _time(self.window_end)
        cutoff = _time(as_of) if as_of else None
        if end is None:
            return False
        if cutoff is None:
            from datetime import datetime, timezone
            cutoff = datetime.now(timezone.utc)
        return end < cutoff


@dataclass
class ThesisAudit:
    """观点状态迁移审计。"""
    audit_id: str = field(default_factory=lambda: new_id("tha"))
    thesis_id: str = ""
    from_status: str = ""
    to_status: str = ""
    reason: str = ""
    operator: str = "system"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewTask:
    """复核任务。"""
    review_id: str = field(default_factory=lambda: new_id("review"))
    thesis_id: str = ""
    reason: str = ""
    status: str = "pending"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 投资观点
# ---------------------------------------------------------------------------

@dataclass
class InvestmentThesis:
    """可验证投资观点。

    必须是多个 Claim 组成的可验证结构，不能只保存自然语言报告。
    支持状态机：DRAFT → EVIDENCE_PENDING → ACTIVE → HIGH_CONVICTION
                            ↓               ↓           ↓
                        CONFLICTED     CONFLICTED   CONFLICTED
                            ↓               ↓           ↓
                        INVALIDATED   INVALIDATED  INVALIDATED
                                                         ↓
                                                     EXPIRED → ARCHIVED
    """
    thesis_id: str = field(default_factory=lambda: new_id("thesis"))
    company_symbol: str = ""
    sector_id: str = ""
    title: str = ""
    thesis_summary: str = ""
    core_assumptions: list[str] = field(default_factory=list)
    required_claim_ids: list[str] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradictory_evidence_ids: list[str] = field(default_factory=list)
    earnings_result_ids: list[str] = field(default_factory=list)
    expectation_gap_ids: list[str] = field(default_factory=list)
    catalysts: list[Catalyst] = field(default_factory=list)
    catalyst_window: str = ""
    invalidation_conditions: list[str] = field(default_factory=list)
    weakest_assumption: str = ""
    next_evidence_required: str = ""
    confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    uncertainty: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    valid_from: str = ""
    valid_until: str = ""
    review_at: str = ""
    next_review_at: str = ""
    status: str = ThesisStatus.DRAFT.value
    version: int = 1
    audits: list[ThesisAudit] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InvestmentThesis":
        data = _as_dict(raw)
        data["catalysts"] = [Catalyst.from_dict(c) for c in _dict_items(data.get("catalysts"))]
        data["audits"] = [ThesisAudit(**dict(a)) if isinstance(a, Mapping) else a
                          for a in _dict_items(data.get("audits"))]
        data["confidence"] = Interval.from_dict(data.get("confidence", {})) or Interval(0.0, 0.0)
        data["uncertainty"] = Interval.from_dict(data.get("uncertainty", {})) or Interval(0.0, 0.0)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
