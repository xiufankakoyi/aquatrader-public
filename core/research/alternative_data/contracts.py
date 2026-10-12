"""P3.3 另类数据治理契约。

按 P3 计划 Task P3.3：
- 每个数据源必须登记 10 个字段
- 状态机：active → suspended → deactivated（suspended 可恢复 active）
- 无授权或来源不清的数据不得进入正式模型
- 另类信号必须先成为观察或假设，不能直接升级事实
- 覆盖变化不会被误判为公司经营变化
- 每个源有数据质量 dashboard 和停用开关
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any


class AlternativeDataSourceType(Enum):
    """另类数据源类型。

    覆盖招标、招聘、专利、价格、产能、流量等产业信号。
    """

    BID = "bid"                # 招标、订单和中标
    RECRUITMENT = "recruitment"  # 招聘和岗位结构
    PATENT = "patent"          # 专利、标准和认证
    PRICE = "price"           # 产品价格、库存和交付周期
    CAPACITY = "capacity"      # 产能、开工率、运输或渠道信息
    TRAFFIC = "traffic"        # 合法授权的流量、门店、应用或供应链数据
    ORDER = "order"           # 订单与交付
    INVENTORY = "inventory"    # 库存与周转
    OTHER = "other"           # 其他合法授权来源


class AlternativeDataSourceStatus(Enum):
    """另类数据源治理状态。

    状态机：
        active → suspended → deactivated
        suspended → active（恢复）
        active → deactivated（授权失效等直接停用）
        deactivated 为终态，不可恢复
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class SignalTier(Enum):
    """另类信号分层。

    按 P3.3 硬规则「另类信号必须先成为观察或假设，不能直接升级事实」：
    - 另类数据源产生的信号默认只能是 OBSERVATION 或 HYPOTHESIS
    - 升级为 FACT 必须经过独立证据治理流程（不在本模块自动完成）
    """

    OBSERVATION = "observation"  # 观察：单源、未交叉验证
    HYPOTHESIS = "hypothesis"    # 假设：有待验证的推断
    FACT = "fact"                # 事实：经独立证据治理流程确认


class AlternativeDataError(ValueError):
    """另类数据治理违规。"""


class CoverageChangeError(AlternativeDataError):
    """覆盖变化被误判为公司经营变化时抛出。"""


# 允许的状态转移（单向图）
_ALLOWED_STATUS_TRANSITIONS: dict[
    AlternativeDataSourceStatus, frozenset[AlternativeDataSourceStatus]
] = {
    AlternativeDataSourceStatus.ACTIVE: frozenset(
        {AlternativeDataSourceStatus.SUSPENDED, AlternativeDataSourceStatus.DEACTIVATED}
    ),
    AlternativeDataSourceStatus.SUSPENDED: frozenset(
        {AlternativeDataSourceStatus.ACTIVE, AlternativeDataSourceStatus.DEACTIVATED}
    ),
    AlternativeDataSourceStatus.DEACTIVATED: frozenset(),
}


@dataclass(frozen=True)
class AuthorizationScope:
    """数据源授权与使用范围。

    按 P3.3 硬规则「无授权或来源不清的数据不得进入正式模型」：
    - authorization_status 必须为 authorized 才能进入正式模型
    - usage_scope 限定数据可用场景
    - license_reference 留存授权凭证

    Attributes:
        authorization_status: 授权状态（authorized/pending/unknown）
        license_reference: 授权凭证或合同编号
        usage_scope: 允许使用场景列表（如 research_only, internal_model）
        authorized_personnel: 授权访问人员 ID 列表
        expires_at: 授权到期时间（ISO 格式；空表示无到期）
    """

    authorization_status: str  # authorized / pending / unknown
    license_reference: str
    usage_scope: list[str]
    authorized_personnel: list[str]
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_authorized(self) -> bool:
        """是否已获授权。"""
        return self.authorization_status == "authorized"


@dataclass(frozen=True)
class RetentionPolicy:
    """数据抓取和留存政策。

    Attributes:
        fetch_method: 抓取方式（api/scrape/manual/push）
        retention_days: 数据留存天数（-1 表示永久）
        deletion_rule: 删除规则描述
        pii_handling: 个人信息处理方式
    """

    fetch_method: str
    retention_days: int
    deletion_rule: str
    pii_handling: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityMonitoringConfig:
    """质量监控和停用条件配置。

    按 P3.3 硬规则「每个源有数据质量 dashboard 和停用开关」：
    - 当质量指标突破阈值时自动触发停用
    - 阈值可配置，覆盖覆盖率、新鲜度、错误率、完整性

    Attributes:
        min_coverage: 最低覆盖率（0-1），低于则停用
        max_freshness_hours: 最大新鲜度（小时），超过则停用
        max_error_rate: 最大错误率（0-1），超过则停用
        min_completeness: 最低完整性（0-1），低于则停用
        auto_suspend_on_breach: 是否在阈值突破时自动停用
    """

    min_coverage: float = 0.5
    max_freshness_hours: int = 72
    max_error_rate: float = 0.1
    min_completeness: float = 0.8
    auto_suspend_on_breach: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MnpiIsolationConfig:
    """重大非公开信息（MNPI）隔离配置。

    按 P3.3「是否可能包含重大非公开信息及隔离流程」：
    - may_contain_mnpi 标记数据源是否可能含 MNPI
    - 含 MNPI 的数据必须走隔离流程，不得直接进入正式研究

    Attributes:
        may_contain_mnpi: 是否可能包含 MNPI
        isolation_process: 隔离流程描述
        isolated_storage_path: 隔离存储路径
        authorized_reviewers: 授权审核人员 ID 列表
    """

    may_contain_mnpi: bool
    isolation_process: str
    isolated_storage_path: str
    authorized_reviewers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlternativeDataSourceContract:
    """另类数据源契约。

    按 P3.3 §「每个数据源必须登记」的 10 个字段。
    """

    source_id: str
    source_name: str
    source_type: AlternativeDataSourceType
    authorization_scope: AuthorizationScope
    retention_policy: RetentionPolicy
    update_frequency: str
    known_biases: list[str]
    entity_mapping_method: str
    timestamp_semantics: str
    quality_monitoring: QualityMonitoringConfig
    mnpi_isolation: MnpiIsolationConfig
    status: AlternativeDataSourceStatus
    # 治理元数据
    coverage_scope: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_transition_at: str = ""
    last_transition_by: str = ""
    suspension_reason: str = ""
    deactivation_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": self.source_type.value,
            "authorization_scope": self.authorization_scope.to_dict(),
            "retention_policy": self.retention_policy.to_dict(),
            "update_frequency": self.update_frequency,
            "known_biases": list(self.known_biases),
            "entity_mapping_method": self.entity_mapping_method,
            "timestamp_semantics": self.timestamp_semantics,
            "quality_monitoring": self.quality_monitoring.to_dict(),
            "mnpi_isolation": self.mnpi_isolation.to_dict(),
            "status": self.status.value,
            "coverage_scope": list(self.coverage_scope),
            "created_at": self.created_at,
            "last_transition_at": self.last_transition_at,
            "last_transition_by": self.last_transition_by,
            "suspension_reason": self.suspension_reason,
            "deactivation_reason": self.deactivation_reason,
        }

    def compute_fingerprint(self) -> str:
        """计算数据源指纹（用于检测契约变化）。"""
        payload = {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": self.source_type.value,
            "authorization_scope": self.authorization_scope.to_dict(),
            "retention_policy": self.retention_policy.to_dict(),
            "update_frequency": self.update_frequency,
            "known_biases": sorted(self.known_biases),
            "entity_mapping_method": self.entity_mapping_method,
            "timestamp_semantics": self.timestamp_semantics,
            "quality_monitoring": self.quality_monitoring.to_dict(),
            "mnpi_isolation": self.mnpi_isolation.to_dict(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class AlternativeDataSignal:
    """另类数据信号。

    按 P3.3 硬规则「另类信号必须先成为观察或假设，不能直接升级事实」：
    - 来自另类数据源的信号默认 tier=OBSERVATION
    - 不允许在创建时直接设置 tier=FACT

    Attributes:
        signal_id: 信号唯一 ID
        source_id: 来源数据源 ID
        entity_id: 实体 ID（如股票代码）
        metric_name: 指标名（如 job_openings, bid_count）
        metric_value: 指标值
        timestamp: 信号时间戳（ISO 格式）
        tier: 信号分层（observation/hypothesis/fact）
        raw_payload: 原始数据负载
    """

    signal_id: str
    source_id: str
    entity_id: str
    metric_name: str
    metric_value: float
    timestamp: str
    tier: SignalTier = SignalTier.OBSERVATION
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "timestamp": self.timestamp,
            "tier": self.tier.value,
            "raw_payload": dict(self.raw_payload),
        }


@dataclass(frozen=True)
class CoverageChange:
    """覆盖变化标记。

    按 P3.3 硬规则「覆盖变化不会被误判为公司经营变化」：
    - 当数据源覆盖范围变化（新增/减少公司）时，必须标记为 CoverageChange
    - CoverageChange 不得被解释为公司经营变化
    - 仅描述数据源覆盖范围变化，不涉及公司经营

    Attributes:
        source_id: 数据源 ID
        change_type: 变化类型（added/removed）
        affected_entities: 受影响实体 ID 列表
        detected_at: 检测时间（ISO 格式）
        note: 说明（仅描述覆盖变化，不解释为经营变化）
    """

    source_id: str
    change_type: str  # added / removed
    affected_entities: list[str]
    detected_at: str
    note: str = "数据源覆盖范围变化，不代表公司经营变化"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "change_type": self.change_type,
            "affected_entities": list(self.affected_entities),
            "detected_at": self.detected_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class QualityMetric:
    """数据源质量指标快照。

    Attributes:
        source_id: 数据源 ID
        coverage: 覆盖率（0-1）
        freshness_hours: 新鲜度（小时）
        error_rate: 错误率（0-1）
        completeness: 完整性（0-1）
        measured_at: 测量时间（ISO 格式）
    """

    source_id: str
    coverage: float
    freshness_hours: int
    error_rate: float
    completeness: float
    measured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "coverage": self.coverage,
            "freshness_hours": self.freshness_hours,
            "error_rate": self.error_rate,
            "completeness": self.completeness,
            "measured_at": self.measured_at,
        }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


# ---------------------------------------------------------------------------
# 状态机与硬规则校验函数
# ---------------------------------------------------------------------------


def is_status_transition_allowed(
    current: AlternativeDataSourceStatus, target: AlternativeDataSourceStatus
) -> bool:
    """查询数据源状态转移是否允许。

    Args:
        current: 当前状态
        target: 目标状态

    Returns:
        bool: 是否允许转移
    """
    if current == target:
        return True
    return target in _ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())


def assert_authorized_source(source: AlternativeDataSourceContract) -> None:
    """无授权或来源不清的数据不得进入正式模型。

    按 P3.3 硬规则：authorization_status 必须为 authorized，
    且 source_id、source_name 不得为空。

    Args:
        source: 待校验的数据源契约

    Raises:
        AlternativeDataError: 当数据源未授权或来源不清时
    """
    if not source.source_id or not source.source_name:
        raise AlternativeDataError(
            "另类数据治理违规：来源不清（source_id 或 source_name 为空）"
        )
    if not source.authorization_scope.is_authorized:
        raise AlternativeDataError(
            f"另类数据治理违规：数据源未授权，不得进入正式模型"
            f"（source_id={source.source_id}, "
            f"authorization_status={source.authorization_scope.authorization_status}）"
        )


def assert_signal_not_fact(signal: AlternativeDataSignal) -> None:
    """另类信号必须先成为观察或假设，不能直接升级事实。

    按 P3.3 硬规则：来自另类数据源的信号在创建时不得直接设置为 FACT。
    FACT 必须经过独立证据治理流程确认（不在本模块自动完成）。

    Args:
        signal: 待校验的另类数据信号

    Raises:
        AlternativeDataError: 当信号 tier 为 FACT 时
    """
    if signal.tier is SignalTier.FACT or signal.tier == SignalTier.FACT.value:
        raise AlternativeDataError(
            f"另类数据治理违规：另类信号不得直接升级为 FACT"
            f"（signal_id={signal.signal_id}, source_id={signal.source_id}）"
        )


def assert_coverage_change_not_operational(change: CoverageChange) -> None:
    """覆盖变化不会被误判为公司经营变化。

    按 P3.3 硬规则：CoverageChange 的 note 必须明确声明「不代表公司经营变化」，
    不得包含任何经营变化措辞。

    Args:
        change: 覆盖变化标记

    Raises:
        CoverageChangeError: 当 note 包含经营变化措辞时
    """
    forbidden_keywords = ("业绩", "收入", "利润", "增长", "下滑", "经营变化")
    note_lower = change.note.lower()
    for keyword in forbidden_keywords:
        if keyword in change.note and "不代表" not in note_lower and "不涉及" not in note_lower:
            raise CoverageChangeError(
                f"覆盖变化违规：note 不得将覆盖变化解释为公司经营变化"
                f"（source_id={change.source_id}, keyword={keyword}）"
            )


def assert_mnpi_isolated(source: AlternativeDataSourceContract) -> None:
    """含 MNPI 的数据源必须走隔离流程。

    按 P3.3「是否可能包含重大非公开信息及隔离流程」：
    - 若 may_contain_mnpi=True，则必须有隔离流程、隔离存储路径和授权审核人员
    - 含 MNPI 的数据源不得直接进入正式研究

    Args:
        source: 待校验的数据源契约

    Raises:
        AlternativeDataError: 当含 MNPI 但隔离流程不完整时
    """
    mnpi = source.mnpi_isolation
    if not mnpi.may_contain_mnpi:
        return
    if not mnpi.isolation_process:
        raise AlternativeDataError(
            f"MNPI 隔离违规：含 MNPI 的数据源必须指定隔离流程"
            f"（source_id={source.source_id}）"
        )
    if not mnpi.isolated_storage_path:
        raise AlternativeDataError(
            f"MNPI 隔离违规：含 MNPI 的数据源必须指定隔离存储路径"
            f"（source_id={source.source_id}）"
        )
    if not mnpi.authorized_reviewers:
        raise AlternativeDataError(
            f"MNPI 隔离违规：含 MNPI 的数据源必须指定授权审核人员"
            f"（source_id={source.source_id}）"
        )


def assert_quality_within_thresholds(
    metric: QualityMetric, config: QualityMonitoringConfig
) -> None:
    """检查质量指标是否在阈值范围内。

    Args:
        metric: 质量指标快照
        config: 质量监控配置

    Raises:
        AlternativeDataError: 当任一指标突破阈值时
    """
    breaches: list[str] = []
    coverage = _finite(metric.coverage)
    min_coverage = _finite(config.min_coverage)
    freshness_hours = _finite(metric.freshness_hours)
    max_freshness_hours = _finite(config.max_freshness_hours)
    error_rate = _finite(metric.error_rate)
    max_error_rate = _finite(config.max_error_rate)
    completeness = _finite(metric.completeness)
    min_completeness = _finite(config.min_completeness)

    for name, value in (
        ("coverage", coverage),
        ("freshness_hours", freshness_hours),
        ("error_rate", error_rate),
        ("completeness", completeness),
        ("min_coverage", min_coverage),
        ("max_freshness_hours", max_freshness_hours),
        ("max_error_rate", max_error_rate),
        ("min_completeness", min_completeness),
    ):
        if value is None:
            breaches.append(f"{name}=invalid")

    if coverage is not None and not 0.0 <= coverage <= 1.0:
        breaches.append(f"coverage={coverage:.2f} out of [0,1]")
    if error_rate is not None and not 0.0 <= error_rate <= 1.0:
        breaches.append(f"error_rate={error_rate:.2f} out of [0,1]")
    if completeness is not None and not 0.0 <= completeness <= 1.0:
        breaches.append(f"completeness={completeness:.2f} out of [0,1]")
    if freshness_hours is not None and freshness_hours < 0:
        breaches.append(f"freshness_hours={freshness_hours:.2f} < 0")
    if coverage is not None and min_coverage is not None and coverage < min_coverage:
        breaches.append(
            f"coverage={coverage:.2f} < min_coverage={min_coverage:.2f}"
        )
    if (
        freshness_hours is not None
        and max_freshness_hours is not None
        and freshness_hours > max_freshness_hours
    ):
        breaches.append(
            f"freshness_hours={freshness_hours:g} > "
            f"max_freshness_hours={max_freshness_hours:g}"
        )
    if error_rate is not None and max_error_rate is not None and error_rate > max_error_rate:
        breaches.append(
            f"error_rate={error_rate:.2f} > "
            f"max_error_rate={max_error_rate:.2f}"
        )
    if (
        completeness is not None
        and min_completeness is not None
        and completeness < min_completeness
    ):
        breaches.append(
            f"completeness={completeness:.2f} < "
            f"min_completeness={min_completeness:.2f}"
        )
    if breaches:
        raise AlternativeDataError(
            f"质量指标突破阈值（source_id={metric.source_id}）: "
            + "; ".join(breaches)
        )
