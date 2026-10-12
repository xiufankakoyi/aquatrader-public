"""P3.9 大规模历史 PIT 认证数据契约。

按 P3 计划 Task P3.9：
- 至少 300 家公司、20 个行业、5 年 PIT 可见历史
- 覆盖上行、下行和震荡市场状态
- 认证内容：数据覆盖、三表调整、预测误差、共识相对误差、
  行业模型适用性、漏斗稳定性、VP 失效原因、错误归因
- 硬门槛：无未来泄漏、无选择性删除、使用当时有效版本、
  同时显示成功与失败、样本不足时明确标注、独立复核人签署

设计原则：
- 契约只定义数据结构，不含业务逻辑
- 支持未来真实运行（不写死 mock 数据）
- 失败样本与成功样本同等保留
- 独立复核人签署字段不可由模型作者自行填写
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar


_EnumT = TypeVar("_EnumT", bound=Enum)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return converted if math.isfinite(converted) else default


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, converted)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _enum_value(value: Any, enum_type: type[_EnumT], default: _EnumT) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _market_states(value: Any) -> frozenset["MarketState"]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return frozenset()
    states = {
        _enum_value(item, MarketState, MarketState.SIDEWAYS)
        for item in value
    }
    return frozenset(states)


def utc_now() -> str:
    """返回 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MarketState(str, Enum):
    """市场状态分类。

    Attributes:
        UP: 上行市场（指数涨幅超过上行阈值）
        DOWN: 下行市场（指数跌幅超过下行阈值）
        SIDEWAYS: 震荡市场（涨跌幅在阈值区间内）
    """

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class PITErrorAttribution(str, Enum):
    """预测错误归因类型。

    五类错误来源：
        MODEL: 模型本身缺陷（结构、参数、校准问题）
        DATA: 输入数据错误（数据源错误、缺失、陈旧）
        MAPPING: 概念映射错误（行业归属、产品映射、分部归集）
        TIMING: 时点错误（使用了未来信息或时点对齐错误）
        ASSUMPTION: 假设错误（显式假设与实际偏离）
    """

    MODEL = "model"
    DATA = "data"
    MAPPING = "mapping"
    TIMING = "timing"
    ASSUMPTION = "assumption"


class CertificationStatus(str, Enum):
    """单公司认证结果状态。

    Attributes:
        SUCCESS: 预测成功兑现（误差在可接受区间内）
        FAILURE: 预测失败（误差超出可接受区间）
        INSUFFICIENT_DATA: 数据不足，无法完成认证
        LEAKAGE_DETECTED: 检测到未来信息泄漏
    """

    SUCCESS = "success"
    FAILURE = "failure"
    INSUFFICIENT_DATA = "insufficient_data"
    LEAKAGE_DETECTED = "leakage_detected"


@dataclass(frozen=True)
class PITCertificationScope:
    """PIT 认证范围定义。

    按 P3.9 §范围：
    - 至少 300 家公司
    - 至少 20 个行业
    - 至少 5 年 PIT 可见历史
    - 至少覆盖上行、下行和震荡市场状态

    Attributes:
        min_companies: 最少公司数（默认 300）
        min_industries: 最少行业数（默认 20）
        min_years: 最少 PIT 历史年数（默认 5）
        require_market_states: 需要覆盖的市场状态集合
        max_acceptable_mape: 可接受的最大 MAPE（默认 0.30）
        confidence_level: 区间覆盖率的声明置信度（默认 0.90）
        min_sample_for_significance: 统计显著性最小样本数（默认 30）
    """

    min_companies: int = 300
    min_industries: int = 20
    min_years: int = 5
    require_market_states: frozenset[MarketState] = field(
        default_factory=lambda: frozenset(
            {MarketState.UP, MarketState.DOWN, MarketState.SIDEWAYS}
        )
    )
    max_acceptable_mape: float = 0.30
    confidence_level: float = 0.90
    min_sample_for_significance: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_companies", _non_negative_int(self.min_companies, 300))
        object.__setattr__(self, "min_industries", _non_negative_int(self.min_industries, 20))
        object.__setattr__(self, "min_years", _non_negative_int(self.min_years, 5))
        states = _market_states(self.require_market_states)
        object.__setattr__(
            self,
            "require_market_states",
            states or frozenset(MarketState),
        )
        object.__setattr__(
            self,
            "max_acceptable_mape",
            max(0.0, _finite_float(self.max_acceptable_mape, 0.30) or 0.0),
        )
        confidence = _finite_float(self.confidence_level, 0.90) or 0.90
        object.__setattr__(self, "confidence_level", min(1.0, max(0.0, confidence)))
        object.__setattr__(
            self,
            "min_sample_for_significance",
            _non_negative_int(self.min_sample_for_significance, 30),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "min_companies": self.min_companies,
            "min_industries": self.min_industries,
            "min_years": self.min_years,
            "require_market_states": sorted(s.value for s in self.require_market_states),
            "max_acceptable_mape": self.max_acceptable_mape,
            "confidence_level": self.confidence_level,
            "min_sample_for_significance": self.min_sample_for_significance,
        }


@dataclass(frozen=True)
class CompanyCertificationMember:
    """认证样本中的单家公司成员。

    Attributes:
        symbol: 公司代码
        company_name: 公司名称
        industry: 行业名称
        sub_industry: 子行业
        listed_at: 上市日期 YYYY-MM-DD（不足 5 年按上市时间记录）
        pit_start: PIT 可见历史起始日期
        pit_end: PIT 可见历史结束日期
        available_years: 可用 PIT 年数（不足 5 年时按实际记录）
        market_states_covered: 该公司覆盖的市场状态
    """

    symbol: str
    company_name: str
    industry: str
    sub_industry: str = ""
    listed_at: str = ""
    pit_start: str = ""
    pit_end: str = ""
    available_years: float = 0.0
    market_states_covered: frozenset[MarketState] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("symbol", "company_name", "industry", "sub_industry", "listed_at", "pit_start", "pit_end"):
            object.__setattr__(self, name, _text(getattr(self, name)))
        object.__setattr__(
            self,
            "available_years",
            max(0.0, _finite_float(self.available_years, 0.0) or 0.0),
        )
        object.__setattr__(self, "market_states_covered", _market_states(self.market_states_covered))

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "industry": self.industry,
            "sub_industry": self.sub_industry,
            "listed_at": self.listed_at,
            "pit_start": self.pit_start,
            "pit_end": self.pit_end,
            "available_years": self.available_years,
            "market_states_covered": sorted(s.value for s in self.market_states_covered),
        }


@dataclass(frozen=True)
class CertificationUniverse:
    """认证样本全集。

    Attributes:
        members: 公司成员列表
        industry_distribution: 行业 -> 公司数
        market_state_distribution: 市场状态 -> 公司数
        pit_window_start: PIT 时间窗口起始
        pit_window_end: PIT 时间窗口结束
        total_companies: 公司总数
        total_industries: 行业总数
    """

    members: list[CompanyCertificationMember] = field(default_factory=list)
    industry_distribution: dict[str, int] = field(default_factory=dict)
    market_state_distribution: dict[str, int] = field(default_factory=dict)
    pit_window_start: str = ""
    pit_window_end: str = ""

    def __post_init__(self) -> None:
        members = [member for member in self.members if isinstance(member, CompanyCertificationMember)] if isinstance(self.members, (list, tuple)) else []
        object.__setattr__(self, "members", members)
        object.__setattr__(
            self,
            "industry_distribution",
            {str(key): _non_negative_int(value) for key, value in _mapping(self.industry_distribution).items()},
        )
        object.__setattr__(
            self,
            "market_state_distribution",
            {str(key): _non_negative_int(value) for key, value in _mapping(self.market_state_distribution).items()},
        )
        object.__setattr__(self, "pit_window_start", _text(self.pit_window_start))
        object.__setattr__(self, "pit_window_end", _text(self.pit_window_end))

    @property
    def total_companies(self) -> int:
        """公司总数。"""
        return len(self.members)

    @property
    def total_industries(self) -> int:
        """行业总数。"""
        return len(self.industry_distribution)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "members": [m.to_dict() for m in self.members],
            "industry_distribution": dict(self.industry_distribution),
            "market_state_distribution": dict(self.market_state_distribution),
            "pit_window_start": self.pit_window_start,
            "pit_window_end": self.pit_window_end,
            "total_companies": self.total_companies,
            "total_industries": self.total_industries,
        }


@dataclass
class PITLeakageCheck:
    """未来信息泄漏检查结果。

    按 P3.9 硬门槛「无已知未来泄漏」：
    - 财报数据使用发布日期而非期末日期
    - 分析师预期使用快照日期
    - 行业数据使用可用日期
    - 模型版本是否在当时有效

    Attributes:
        symbol: 公司代码
        pit_time: PIT 检查时点
        has_leakage: 是否检测到泄漏
        leakage_items: 泄漏项明细列表（每项含 field/source/expected/actual/reason）
        checked_count: 检查项总数
        blocked_count: 被拦截（泄漏）项数
    """

    symbol: str = ""
    pit_time: str = ""
    has_leakage: bool = False
    leakage_items: list[dict[str, Any]] = field(default_factory=list)
    checked_count: int = 0
    blocked_count: int = 0

    def __post_init__(self) -> None:
        self.symbol = _text(self.symbol)
        self.pit_time = _text(self.pit_time)
        self.has_leakage = bool(self.has_leakage)
        self.leakage_items = [
            dict(item) for item in self.leakage_items
            if isinstance(item, Mapping)
        ] if isinstance(self.leakage_items, (list, tuple)) else []
        self.checked_count = _non_negative_int(self.checked_count)
        self.blocked_count = _non_negative_int(self.blocked_count)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "pit_time": self.pit_time,
            "has_leakage": self.has_leakage,
            "leakage_items": list(self.leakage_items),
            "checked_count": self.checked_count,
            "blocked_count": self.blocked_count,
        }


@dataclass
class PITCertificationResult:
    """单公司 PIT 认证结果。

    同时记录成功和失败；不选择性删除失败样本。

    Attributes:
        symbol: 公司代码
        company_name: 公司名称
        industry: 行业
        pit_time: 认证时点
        status: 认证状态（success/failure/insufficient_data/leakage_detected）
        prediction_value: 预测值
        prediction_interval_low: 预测区间下界
        prediction_interval_high: 预测区间上界
        actual_value: 实际值
        consensus_value: 共识值（用于计算共识相对误差）
        absolute_error: 绝对误差
        percentage_error: 百分比误差
        interval_covered: 预测区间是否覆盖实际值
        consensus_relative_error: 相对共识的误差改进
        model_version: 使用的模型版本（当时有效版本）
        policy_version: 使用的 policy 版本（当时有效版本）
        error_attribution: 错误归因类型（失败时填充）
        error_attribution_detail: 错误归因详细说明
        leakage_check: 泄漏检查结果
        market_state: 该时点的市场状态
        vp_formed: 是否形成了 VariantPerception
        vp_invalidation_reason: VP 失效原因（若适用）
        notes: 备注
    """

    symbol: str = ""
    company_name: str = ""
    industry: str = ""
    pit_time: str = ""
    status: CertificationStatus = CertificationStatus.INSUFFICIENT_DATA
    prediction_value: float | None = None
    prediction_interval_low: float | None = None
    prediction_interval_high: float | None = None
    actual_value: float | None = None
    consensus_value: float | None = None
    absolute_error: float | None = None
    percentage_error: float | None = None
    interval_covered: bool | None = None
    consensus_relative_error: float | None = None
    model_version: str = ""
    policy_version: str = ""
    error_attribution: PITErrorAttribution | None = None
    error_attribution_detail: str = ""
    leakage_check: PITLeakageCheck | None = None
    market_state: MarketState = MarketState.SIDEWAYS
    vp_formed: bool = False
    vp_invalidation_reason: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("symbol", "company_name", "industry", "pit_time", "model_version", "policy_version", "error_attribution_detail", "vp_invalidation_reason", "notes"):
            setattr(self, name, _text(getattr(self, name)))
        self.status = _enum_value(
            self.status,
            CertificationStatus,
            CertificationStatus.INSUFFICIENT_DATA,
        )
        for name in (
            "prediction_value",
            "prediction_interval_low",
            "prediction_interval_high",
            "actual_value",
            "consensus_value",
            "absolute_error",
            "percentage_error",
            "consensus_relative_error",
        ):
            setattr(self, name, _finite_float(getattr(self, name)))
        if self.interval_covered is not None:
            self.interval_covered = bool(self.interval_covered)
        self.error_attribution = (
            _enum_value(self.error_attribution, PITErrorAttribution, PITErrorAttribution.MODEL)
            if self.error_attribution is not None else None
        )
        self.leakage_check = (
            self.leakage_check if isinstance(self.leakage_check, PITLeakageCheck) else None
        )
        self.market_state = _enum_value(self.market_state, MarketState, MarketState.SIDEWAYS)
        self.vp_formed = bool(self.vp_formed)

    @property
    def is_success(self) -> bool:
        """是否成功。"""
        return self.status is CertificationStatus.SUCCESS

    @property
    def is_failure(self) -> bool:
        """是否失败（不含 insufficient_data）。"""
        return self.status is CertificationStatus.FAILURE

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "industry": self.industry,
            "pit_time": self.pit_time,
            "status": self.status.value,
            "prediction_value": self.prediction_value,
            "prediction_interval_low": self.prediction_interval_low,
            "prediction_interval_high": self.prediction_interval_high,
            "actual_value": self.actual_value,
            "consensus_value": self.consensus_value,
            "absolute_error": self.absolute_error,
            "percentage_error": self.percentage_error,
            "interval_covered": self.interval_covered,
            "consensus_relative_error": self.consensus_relative_error,
            "model_version": self.model_version,
            "policy_version": self.policy_version,
            "error_attribution": (
                self.error_attribution.value if self.error_attribution else None
            ),
            "error_attribution_detail": self.error_attribution_detail,
            "leakage_check": self.leakage_check.to_dict() if self.leakage_check else None,
            "market_state": self.market_state.value,
            "vp_formed": self.vp_formed,
            "vp_invalidation_reason": self.vp_invalidation_reason,
            "notes": self.notes,
        }


@dataclass
class IndependentSignOff:
    """独立复核人签署记录。

    按 P3.9 硬门槛「独立复核人签署历史认证」：
    - 模型作者不能完成独立验证签署（与 P3.1 规则一致）
    - 签署内容哈希覆盖全部认证结果，防止事后篡改

    Attributes:
        signer: 签署人 ID（必须与模型作者不同）
        sign_time: 签署时间（UTC ISO）
        signature_hash: 签署内容哈希（覆盖全部结果的 SHA-256）
        scope_summary: 签署时的范围摘要
        is_signed: 是否已完成签署
    """

    signer: str = ""
    sign_time: str = ""
    signature_hash: str = ""
    scope_summary: str = ""
    is_signed: bool = False

    def __post_init__(self) -> None:
        self.signer = _text(self.signer)
        self.sign_time = _text(self.sign_time)
        self.signature_hash = _text(self.signature_hash)
        self.scope_summary = _text(self.scope_summary)
        self.is_signed = bool(self.is_signed)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "signer": self.signer,
            "sign_time": self.sign_time,
            "signature_hash": self.signature_hash,
            "scope_summary": self.scope_summary,
            "is_signed": self.is_signed,
        }


@dataclass
class PITCertificationReport:
    """PIT 认证汇总报告。

    按 P3.9 §认证内容，报告必须包含 8 个维度：
    1. 数据覆盖和来源偏差
    2. 三表与公司行为调整
    3. 预测误差和区间覆盖
    4. 共识相对误差
    5. 行业模型适用性
    6. 发现漏斗稳定性
    7. VariantPerception 的形成和失效原因
    8. 模型、数据、映射、时点和假设错误归因

    硬门槛：
    - 同时显示成功与失败
    - 样本不足时标注「不包装成有效能力」
    - 独立复核人签署

    Attributes:
        scope: 认证范围
        total_companies: 公司总数
        total_success: 成功数
        total_failure: 失败数
        total_insufficient: 数据不足数
        total_leakage: 泄漏数
        data_coverage: 数据覆盖和来源偏差
        statement_adjustment: 三表与公司行为调整
        forecast_error: 预测误差和区间覆盖
        consensus_error: 共识相对误差
        industry_model_applicability: 行业模型适用性
        funnel_stability: 发现漏斗稳定性
        vp_analysis: VP 形成和失效分析
        error_attribution_distribution: 错误归因分布
        statistical_significance: 统计显著性标注
        hard_gates: 硬门槛检查结果
        sign_off: 独立复核人签署
        generated_at: 报告生成时间
    """

    scope: PITCertificationScope = field(default_factory=PITCertificationScope)
    total_companies: int = 0
    total_success: int = 0
    total_failure: int = 0
    total_insufficient: int = 0
    total_leakage: int = 0
    data_coverage: dict[str, Any] = field(default_factory=dict)
    statement_adjustment: dict[str, Any] = field(default_factory=dict)
    forecast_error: dict[str, Any] = field(default_factory=dict)
    consensus_error: dict[str, Any] = field(default_factory=dict)
    industry_model_applicability: dict[str, Any] = field(default_factory=dict)
    funnel_stability: dict[str, Any] = field(default_factory=dict)
    vp_analysis: dict[str, Any] = field(default_factory=dict)
    error_attribution_distribution: dict[str, Any] = field(default_factory=dict)
    statistical_significance: dict[str, Any] = field(default_factory=dict)
    hard_gates: dict[str, bool] = field(default_factory=dict)
    sign_off: IndependentSignOff = field(default_factory=IndependentSignOff)
    generated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PITCertificationScope):
            self.scope = PITCertificationScope()
        for name in ("total_companies", "total_success", "total_failure", "total_insufficient", "total_leakage"):
            setattr(self, name, _non_negative_int(getattr(self, name)))
        for name in (
            "data_coverage", "statement_adjustment", "forecast_error", "consensus_error",
            "industry_model_applicability", "funnel_stability", "vp_analysis",
            "error_attribution_distribution", "statistical_significance",
        ):
            setattr(self, name, _mapping(getattr(self, name)))
        self.hard_gates = {
            str(key): bool(value) for key, value in _mapping(self.hard_gates).items()
        }
        if not isinstance(self.sign_off, IndependentSignOff):
            self.sign_off = IndependentSignOff()
        self.generated_at = _text(self.generated_at)

    @property
    def shows_both_success_and_failure(self) -> bool:
        """是否同时显示了成功与失败。"""
        return self.total_success > 0 and self.total_failure > 0

    @property
    def has_significance_warning(self) -> bool:
        """是否标注了样本不足警告。"""
        return bool(self.statistical_significance.get("insufficient_sample", False))

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "scope": self.scope.to_dict(),
            "total_companies": self.total_companies,
            "total_success": self.total_success,
            "total_failure": self.total_failure,
            "total_insufficient": self.total_insufficient,
            "total_leakage": self.total_leakage,
            "data_coverage": dict(self.data_coverage),
            "statement_adjustment": dict(self.statement_adjustment),
            "forecast_error": dict(self.forecast_error),
            "consensus_error": dict(self.consensus_error),
            "industry_model_applicability": dict(self.industry_model_applicability),
            "funnel_stability": dict(self.funnel_stability),
            "vp_analysis": dict(self.vp_analysis),
            "error_attribution_distribution": dict(self.error_attribution_distribution),
            "statistical_significance": dict(self.statistical_significance),
            "hard_gates": dict(self.hard_gates),
            "sign_off": self.sign_off.to_dict(),
            "generated_at": self.generated_at,
        }


def compute_results_hash(results: list[PITCertificationResult]) -> str:
    """计算认证结果列表的内容哈希。

    用于独立复核人签署；覆盖全部结果（含成功和失败），
    防止事后选择性删除或篡改。

    Args:
        results: 认证结果列表

    Returns:
        SHA-256 哈希字符串
    """
    valid_results = [
        result for result in results if isinstance(result, PITCertificationResult)
    ]
    payload = [
        result.to_dict()
        for result in sorted(valid_results, key=lambda r: (r.symbol, r.pit_time))
    ]

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
