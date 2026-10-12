"""P2.9 PIT 历史检验与研究记分卡数据契约。

按 P2 计划 §301-323 Task P2.9：
- 预测误差评估（收入 / 利润 / EPS）
- 预测区间覆盖率
- 共识 vs 独立预测相对误差
- 变化事件 → 有效事实转化率
- 来源命中率与单位成本
- 5 类错误归因
- 行业与市场环境稳定性
- 3 条强制规则

设计要点：
- FrozenForecastInput：冻结的预测输入快照（不可变，含 input_hash 防篡改）
- ActualOutcome：实际结果（reported_at 严格 > forecast.as_of，无未来泄漏）
- ErrorAttribution：5 类错误归因（model / mapping / data / timing / assumption）
- ForecastScorecard：记分卡，引用 frozen input + actual，不修改原始研究记录
- SourceMetrics / EventConversionStat / IndustryStabilityScore：发现评估指标
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# 评估指标
# ---------------------------------------------------------------------------

METRIC_REVENUE: str = "revenue"
METRIC_NET_PROFIT: str = "net_profit"
METRIC_EPS: str = "eps"

METRICS: tuple[str, ...] = (
    METRIC_REVENUE,
    METRIC_NET_PROFIT,
    METRIC_EPS,
)

METRIC_LABELS: dict[str, str] = {
    METRIC_REVENUE: "营业收入",
    METRIC_NET_PROFIT: "归母净利润",
    METRIC_EPS: "每股收益",
}


# ---------------------------------------------------------------------------
# 预测来源
# ---------------------------------------------------------------------------

FORECAST_SOURCE_CONSENSUS: str = "consensus"
FORECAST_SOURCE_INTERNAL: str = "internal"
FORECAST_SOURCE_GUIDANCE: str = "guidance"
FORECAST_SOURCE_IMPLIED: str = "implied"

FORECAST_SOURCES: tuple[str, ...] = (
    FORECAST_SOURCE_CONSENSUS,
    FORECAST_SOURCE_INTERNAL,
    FORECAST_SOURCE_GUIDANCE,
    FORECAST_SOURCE_IMPLIED,
)

FORECAST_SOURCE_LABELS: dict[str, str] = {
    FORECAST_SOURCE_CONSENSUS: "市场一致预期",
    FORECAST_SOURCE_INTERNAL: "内部独立预测",
    FORECAST_SOURCE_GUIDANCE: "公司指引",
    FORECAST_SOURCE_IMPLIED: "市场隐含预期",
}


# ---------------------------------------------------------------------------
# 市场环境
# ---------------------------------------------------------------------------

MARKET_REGIME_BULL: str = "bull"
MARKET_REGIME_BEAR: str = "bear"
MARKET_REGIME_RANGE: str = "range"

MARKET_REGIMES: tuple[str, ...] = (
    MARKET_REGIME_BULL,
    MARKET_REGIME_BEAR,
    MARKET_REGIME_RANGE,
)

MARKET_REGIME_LABELS: dict[str, str] = {
    MARKET_REGIME_BULL: "牛市",
    MARKET_REGIME_BEAR: "熊市",
    MARKET_REGIME_RANGE: "震荡市",
}


# ---------------------------------------------------------------------------
# 错误归因 5 类
# ---------------------------------------------------------------------------

ERROR_ATTRIBUTION_MODEL: str = "model"
ERROR_ATTRIBUTION_MAPPING: str = "mapping"
ERROR_ATTRIBUTION_DATA: str = "data"
ERROR_ATTRIBUTION_TIMING: str = "timing"
ERROR_ATTRIBUTION_ASSUMPTION: str = "assumption"

ERROR_ATTRIBUTIONS: tuple[str, ...] = (
    ERROR_ATTRIBUTION_MODEL,
    ERROR_ATTRIBUTION_MAPPING,
    ERROR_ATTRIBUTION_DATA,
    ERROR_ATTRIBUTION_TIMING,
    ERROR_ATTRIBUTION_ASSUMPTION,
)

ERROR_ATTRIBUTION_LABELS: dict[str, str] = {
    ERROR_ATTRIBUTION_MODEL: "模型结构错误",
    ERROR_ATTRIBUTION_MAPPING: "概念映射错误",
    ERROR_ATTRIBUTION_DATA: "数据错误",
    ERROR_ATTRIBUTION_TIMING: "时点错误",
    ERROR_ATTRIBUTION_ASSUMPTION: "假设错误",
}


# ---------------------------------------------------------------------------
# 事件状态（与 P2.3 change_radar 对齐）
# ---------------------------------------------------------------------------

EVENT_STATUS_OBSERVED: str = "observed"
EVENT_STATUS_EVIDENCE_SUPPORTED: str = "evidence_supported"
EVENT_STATUS_MODEL_ELIGIBLE: str = "model_eligible"
EVENT_STATUS_REJECTED: str = "rejected"
EVENT_STATUS_EXPIRED: str = "expired"

EVENT_STATUSES: tuple[str, ...] = (
    EVENT_STATUS_OBSERVED,
    EVENT_STATUS_EVIDENCE_SUPPORTED,
    EVENT_STATUS_MODEL_ELIGIBLE,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_EXPIRED,
)


# ---------------------------------------------------------------------------
# 实际结果状态
# ---------------------------------------------------------------------------

ACTUAL_OUTCOME_STATUS_REPORTED: str = "reported"
ACTUAL_OUTCOME_STATUS_PRELIMINARY: str = "preliminary"

ACTUAL_OUTCOME_STATUSES: tuple[str, ...] = (
    ACTUAL_OUTCOME_STATUS_REPORTED,
    ACTUAL_OUTCOME_STATUS_PRELIMINARY,
)


# ---------------------------------------------------------------------------
# AGENTS.md 合规
# ---------------------------------------------------------------------------

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

NOTICE: str = (
    "P2.9 研究记分卡仅评估预测误差与归因，不反向修改研究记录，"
    "不以股价涨跌单独定义基本面研究正确性，不输出交易动作建议。"
)


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 冻结输入哈希
# ---------------------------------------------------------------------------


def compute_frozen_input_hash(
    *,
    forecast_id: str,
    company_symbol: str,
    as_of: str,
    metric: str,
    forecast_source: str,
    forecast_value: float,
    interval_lower: float | None,
    interval_upper: float | None,
    model_name: str,
    model_version: str,
    assumptions: dict[str, Any],
) -> str:
    """计算冻结输入的 SHA256 哈希（防篡改）。

    Args:
        forecast_id: 预测 ID
        company_symbol: 公司代码
        as_of: PIT 时点
        metric: 指标（METRICS 之一）
        forecast_source: 来源（FORECAST_SOURCES 之一）
        forecast_value: 预测值
        interval_lower: 区间下限
        interval_upper: 区间上限
        model_name: 模型名
        model_version: 模型版本
        assumptions: 假设字典

    Returns:
        SHA256[:16]
    """
    raw = json.dumps(
        {
            "forecast_id": forecast_id,
            "company_symbol": company_symbol,
            "as_of": as_of,
            "metric": metric,
            "forecast_source": forecast_source,
            "forecast_value": float(forecast_value),
            "interval_lower": (
                None if interval_lower is None else float(interval_lower)
            ),
            "interval_upper": (
                None if interval_upper is None else float(interval_upper)
            ),
            "model_name": model_name,
            "model_version": model_version,
            "assumptions": dict(assumptions or {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 冻结的预测输入
# ---------------------------------------------------------------------------


@dataclass
class FrozenForecastInput:
    """冻结的预测输入快照（不可变）。

    评估时使用冻结输入，确保历史评估可复现。
    任何字段修改都会导致 input_hash 不匹配（仓储校验）。

    Attributes:
        forecast_id: 预测 ID
        company_symbol: 公司代码
        as_of: PIT 时点（预测发布日期）
        period: 预测期间（如 "2027Q1" / "2027FY"）
        metric: 指标（METRICS 之一）
        forecast_source: 来源（FORECAST_SOURCES 之一）
        forecast_value: 预测值
        interval_lower: 区间下限（None 表示无区间）
        interval_upper: 区间上限
        model_name: 模型名
        model_version: 模型版本
        source_refs: 来源引用
        assumptions: 假设字典
        input_hash: 输入哈希（防篡改）
        frozen_at: 冻结时间
    """

    forecast_id: str = ""
    company_symbol: str = ""
    as_of: str = ""
    period: str = ""
    metric: str = METRIC_REVENUE
    forecast_source: str = FORECAST_SOURCE_INTERNAL
    forecast_value: float = 0.0
    interval_lower: float | None = None
    interval_upper: float | None = None
    model_name: str = ""
    model_version: str = ""
    source_refs: list[str] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    frozen_at: str = ""

    def __post_init__(self) -> None:
        """默认 frozen_at / input_hash。"""
        if not self.frozen_at:
            self.frozen_at = utc_now()
        if not self.input_hash:
            self.input_hash = self.recompute_hash()

    def recompute_hash(self) -> str:
        """重新计算 input_hash（用于防篡改校验）。"""
        return compute_frozen_input_hash(
            forecast_id=self.forecast_id,
            company_symbol=self.company_symbol,
            as_of=self.as_of,
            metric=self.metric,
            forecast_source=self.forecast_source,
            forecast_value=self.forecast_value,
            interval_lower=self.interval_lower,
            interval_upper=self.interval_upper,
            model_name=self.model_name,
            model_version=self.model_version,
            assumptions=self.assumptions,
        )

    def is_intact(self) -> bool:
        """校验冻结输入是否未被篡改。"""
        return self.input_hash == self.recompute_hash()

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "forecast_id": self.forecast_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "period": self.period,
            "metric": self.metric,
            "metric_label": METRIC_LABELS.get(self.metric, ""),
            "forecast_source": self.forecast_source,
            "forecast_source_label": FORECAST_SOURCE_LABELS.get(
                self.forecast_source, ""
            ),
            "forecast_value": self.forecast_value,
            "interval_lower": self.interval_lower,
            "interval_upper": self.interval_upper,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "source_refs": list(self.source_refs),
            "assumptions": dict(self.assumptions),
            "input_hash": self.input_hash,
            "frozen_at": self.frozen_at,
        }


# ---------------------------------------------------------------------------
# 实际结果
# ---------------------------------------------------------------------------


@dataclass
class ActualOutcome:
    """实际结果。

    Attributes:
        company_symbol: 公司代码
        period: 期间（与 forecast.period 对齐）
        metric: 指标
        actual_value: 实际值
        reported_at: 实际公布时间（必须 > forecast.as_of，无未来泄漏）
        status: 状态（reported / preliminary）
        source_refs: 来源引用
    """

    company_symbol: str = ""
    period: str = ""
    metric: str = METRIC_REVENUE
    actual_value: float = 0.0
    reported_at: str = ""
    status: str = ACTUAL_OUTCOME_STATUS_REPORTED
    source_refs: list[str] = field(default_factory=list)

    def is_reported(self) -> bool:
        """是否已公布。"""
        return self.status in ACTUAL_OUTCOME_STATUSES and bool(self.reported_at)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "company_symbol": self.company_symbol,
            "period": self.period,
            "metric": self.metric,
            "metric_label": METRIC_LABELS.get(self.metric, ""),
            "actual_value": self.actual_value,
            "reported_at": self.reported_at,
            "status": self.status,
            "source_refs": list(self.source_refs),
        }


# ---------------------------------------------------------------------------
# 错误归因
# ---------------------------------------------------------------------------


@dataclass
class ErrorAttribution:
    """错误归因（5 类，占比之和必须为 100%）。

    各字段值为百分比（0-100），表示该类错误占总误差的比例。
    总和必须为 100（容忍 ±0.5 浮点误差）。

    Attributes:
        model_error_pct: 模型结构错误占比
        mapping_error_pct: 概念映射错误占比
        data_error_pct: 数据错误占比
        timing_error_pct: 时点错误占比
        assumption_error_pct: 假设错误占比
        note: 备注
    """

    model_error_pct: float = 0.0
    mapping_error_pct: float = 0.0
    data_error_pct: float = 0.0
    timing_error_pct: float = 0.0
    assumption_error_pct: float = 0.0
    note: str = ""

    def total(self) -> float:
        """归因占比总和。"""
        return (
            self.model_error_pct
            + self.mapping_error_pct
            + self.data_error_pct
            + self.timing_error_pct
            + self.assumption_error_pct
        )

    def is_total_100(self, tolerance: float = 0.5) -> bool:
        """总和是否为 100（容忍浮点误差）。"""
        return abs(self.total() - 100.0) <= tolerance

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        return bool(self.note) and any(
            kw in self.note for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "model_error_pct": self.model_error_pct,
            "mapping_error_pct": self.mapping_error_pct,
            "data_error_pct": self.data_error_pct,
            "timing_error_pct": self.timing_error_pct,
            "assumption_error_pct": self.assumption_error_pct,
            "total_pct": self.total(),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 预测记分卡
# ---------------------------------------------------------------------------


@dataclass
class ForecastScorecard:
    """预测记分卡。

    评估某次预测的误差、区间覆盖、归因和稳定性。
    引用 FrozenForecastInput + ActualOutcome，不修改原始研究记录。

    Attributes:
        scorecard_id: 记分卡 ID
        forecast: 冻结的预测输入
        actual: 实际结果
        signed_error_pct: 有符号误差百分比（actual - forecast）/|forecast| × 100
        abs_error_pct: 绝对误差百分比
        interval_covered: 实际值是否落在预测区间内
        attribution: 错误归因
        industry: 行业
        market_regime: 市场环境
        stability_score: 稳定性分数（0-1，越高越稳定）
        consensus_error_pct: 同期共识预测的相对误差（None 表示无共识对比）
        evaluation_as_of: 评估时点
        evaluator: 评估人
        created_at: 创建时间
    """

    scorecard_id: str = ""
    forecast: FrozenForecastInput = field(default_factory=FrozenForecastInput)
    actual: ActualOutcome = field(default_factory=ActualOutcome)
    signed_error_pct: float = 0.0
    abs_error_pct: float = 0.0
    interval_covered: bool = False
    attribution: ErrorAttribution = field(default_factory=ErrorAttribution)
    industry: str = ""
    market_regime: str = MARKET_REGIME_RANGE
    stability_score: float = 0.5
    consensus_error_pct: float | None = None
    evaluation_as_of: str = ""
    evaluator: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        """默认 created_at / scorecard_id。"""
        if not self.created_at:
            self.created_at = utc_now()
        if not self.scorecard_id:
            self.scorecard_id = self._derive_scorecard_id()

    def _derive_scorecard_id(self) -> str:
        """确定性记分卡 ID。"""
        raw = (
            f"{self.forecast.forecast_id}|{self.actual.period}|"
            f"{self.actual.reported_at}|{self.evaluation_as_of}"
        )
        return f"scorecard_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"

    def contains_trade_action(self) -> bool:
        """是否含交易动作关键词。"""
        if self.attribution.contains_trade_action():
            return True
        return bool(self.industry) and any(
            kw in self.industry for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "scorecard_id": self.scorecard_id,
            "forecast": self.forecast.to_dict(),
            "actual": self.actual.to_dict(),
            "signed_error_pct": self.signed_error_pct,
            "abs_error_pct": self.abs_error_pct,
            "interval_covered": self.interval_covered,
            "attribution": self.attribution.to_dict(),
            "industry": self.industry,
            "market_regime": self.market_regime,
            "market_regime_label": MARKET_REGIME_LABELS.get(self.market_regime, ""),
            "stability_score": self.stability_score,
            "consensus_error_pct": self.consensus_error_pct,
            "evaluation_as_of": self.evaluation_as_of,
            "evaluator": self.evaluator,
            "created_at": self.created_at,
            "notice": NOTICE,
        }


# ---------------------------------------------------------------------------
# 来源指标
# ---------------------------------------------------------------------------


@dataclass
class SourceMetrics:
    """来源指标。

    Attributes:
        source_id: 来源 ID
        hit_count: 命中数（被采用为有效证据）
        total_queries: 总查询数
        total_cost: 总成本
        unit_cost: 单位成本（total_cost / total_queries）
        hit_rate: 命中率（hit_count / total_queries）
        as_of: PIT 时点
    """

    source_id: str = ""
    hit_count: int = 0
    total_queries: int = 0
    total_cost: float = 0.0
    unit_cost: float = 0.0
    hit_rate: float = 0.0
    as_of: str = ""

    def __post_init__(self) -> None:
        """默认派生字段。"""
        if self.total_queries > 0:
            if self.unit_cost == 0.0:
                self.unit_cost = self.total_cost / self.total_queries
            if self.hit_rate == 0.0:
                self.hit_rate = self.hit_count / self.total_queries

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "source_id": self.source_id,
            "hit_count": self.hit_count,
            "total_queries": self.total_queries,
            "total_cost": self.total_cost,
            "unit_cost": self.unit_cost,
            "hit_rate": self.hit_rate,
            "as_of": self.as_of,
        }


# ---------------------------------------------------------------------------
# 事件转化统计
# ---------------------------------------------------------------------------


@dataclass
class EventConversionStat:
    """变化事件 → 有效事实转化统计。

    Attributes:
        total_events: 总事件数
        effective_facts: 转化为有效事实的事件数
        rejected_events: 被拒绝的事件数
        expired_events: 过期的事件数
        conversion_rate: 转化率（effective_facts / total_events）
        as_of: PIT 时点
        period: 统计期间
    """

    total_events: int = 0
    effective_facts: int = 0
    rejected_events: int = 0
    expired_events: int = 0
    conversion_rate: float = 0.0
    as_of: str = ""
    period: str = ""

    def __post_init__(self) -> None:
        """默认派生字段。"""
        if self.total_events > 0 and self.conversion_rate == 0.0:
            self.conversion_rate = self.effective_facts / self.total_events

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "total_events": self.total_events,
            "effective_facts": self.effective_facts,
            "rejected_events": self.rejected_events,
            "expired_events": self.expired_events,
            "conversion_rate": self.conversion_rate,
            "as_of": self.as_of,
            "period": self.period,
        }


# ---------------------------------------------------------------------------
# 行业稳定性分数
# ---------------------------------------------------------------------------


@dataclass
class IndustryStabilityScore:
    """行业稳定性分数。

    Attributes:
        industry: 行业
        market_regime: 市场环境
        score: 稳定性分数（0-1，越高越稳定）
        sample_count: 样本数
        variance: 误差方差
        mean_abs_error_pct: 平均绝对误差百分比
    """

    industry: str = ""
    market_regime: str = MARKET_REGIME_RANGE
    score: float = 0.0
    sample_count: int = 0
    variance: float = 0.0
    mean_abs_error_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "industry": self.industry,
            "market_regime": self.market_regime,
            "market_regime_label": MARKET_REGIME_LABELS.get(self.market_regime, ""),
            "score": self.score,
            "sample_count": self.sample_count,
            "variance": self.variance,
            "mean_abs_error_pct": self.mean_abs_error_pct,
        }


__all__ = [
    # 指标
    "METRIC_REVENUE",
    "METRIC_NET_PROFIT",
    "METRIC_EPS",
    "METRICS",
    "METRIC_LABELS",
    # 来源
    "FORECAST_SOURCE_CONSENSUS",
    "FORECAST_SOURCE_INTERNAL",
    "FORECAST_SOURCE_GUIDANCE",
    "FORECAST_SOURCE_IMPLIED",
    "FORECAST_SOURCES",
    "FORECAST_SOURCE_LABELS",
    # 市场环境
    "MARKET_REGIME_BULL",
    "MARKET_REGIME_BEAR",
    "MARKET_REGIME_RANGE",
    "MARKET_REGIMES",
    "MARKET_REGIME_LABELS",
    # 错误归因
    "ERROR_ATTRIBUTION_MODEL",
    "ERROR_ATTRIBUTION_MAPPING",
    "ERROR_ATTRIBUTION_DATA",
    "ERROR_ATTRIBUTION_TIMING",
    "ERROR_ATTRIBUTION_ASSUMPTION",
    "ERROR_ATTRIBUTIONS",
    "ERROR_ATTRIBUTION_LABELS",
    # 事件状态
    "EVENT_STATUS_OBSERVED",
    "EVENT_STATUS_EVIDENCE_SUPPORTED",
    "EVENT_STATUS_MODEL_ELIGIBLE",
    "EVENT_STATUS_REJECTED",
    "EVENT_STATUS_EXPIRED",
    "EVENT_STATUSES",
    # 实际结果状态
    "ACTUAL_OUTCOME_STATUS_REPORTED",
    "ACTUAL_OUTCOME_STATUS_PRELIMINARY",
    "ACTUAL_OUTCOME_STATUSES",
    # 合规
    "FORBIDDEN_TRADE_ACTION_KEYWORDS",
    "NOTICE",
    "utc_now",
    # 工具
    "compute_frozen_input_hash",
    # 契约
    "FrozenForecastInput",
    "ActualOutcome",
    "ErrorAttribution",
    "ForecastScorecard",
    "SourceMetrics",
    "EventConversionStat",
    "IndustryStabilityScore",
]
