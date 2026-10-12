"""P3.5 受治理的自校准学习闭环契约。

按 P3 计划 Task P3.5：
- 只允许校准 6 类目标（来源有效率、检索命中率、预测区间、行业模型误差、
  事件到经营驱动转化概率、研究预算优先级）
- 绝对禁止自动修改 6 类目标（原始事实、原始 Evidence、公司业务归属、
  历史预测版本、已冻结投委会记录、未经审查的正式估值方法）
- 上线流程状态机：
      candidate → offline_evaluated → null_control_passed → shadow
                → independent_reviewed → bounded_rollout → monitoring
                → promoted
      任意非终态阶段 → rolled_back（失败回滚）
- 不同领域独立校准，不能跨域传播权重
- 小样本不允许正式升级
- 失败校准不会污染 active 模型
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class LearningGovernanceError(ValueError):
    """学习闭环治理违规。"""


# ---------------------------------------------------------------------------
# 允许校准的领域
# ---------------------------------------------------------------------------

CALIBRATION_DOMAIN_SOURCE_VALIDITY: str = "source_validity"  # 来源有效率
CALIBRATION_DOMAIN_RETRIEVAL_HIT_RATE: str = "retrieval_hit_rate"  # 检索命中率
CALIBRATION_DOMAIN_PREDICTION_INTERVAL: str = "prediction_interval"  # 预测区间宽度和覆盖率
CALIBRATION_DOMAIN_INDUSTRY_MODEL_ERROR: str = "industry_model_error"  # 行业模型误差参数
CALIBRATION_DOMAIN_EVENT_DRIVER_PROBABILITY: str = "event_driver_probability"  # 事件到经营驱动的转化概率
CALIBRATION_DOMAIN_RESEARCH_BUDGET_PRIORITY: str = "research_budget_priority"  # 研究预算优先级

ALLOWED_CALIBRATION_DOMAINS: tuple[str, ...] = (
    CALIBRATION_DOMAIN_SOURCE_VALIDITY,
    CALIBRATION_DOMAIN_RETRIEVAL_HIT_RATE,
    CALIBRATION_DOMAIN_PREDICTION_INTERVAL,
    CALIBRATION_DOMAIN_INDUSTRY_MODEL_ERROR,
    CALIBRATION_DOMAIN_EVENT_DRIVER_PROBABILITY,
    CALIBRATION_DOMAIN_RESEARCH_BUDGET_PRIORITY,
)


# ---------------------------------------------------------------------------
# 绝对禁止自动修改的目标
# ---------------------------------------------------------------------------

FORBIDDEN_MUTATION_TARGET_RAW_FACT: str = "raw_fact"  # 原始事实
FORBIDDEN_MUTATION_TARGET_RAW_EVIDENCE: str = "raw_evidence"  # 原始 Evidence
FORBIDDEN_MUTATION_TARGET_BUSINESS_ATTRIBUTION: str = "business_attribution"  # 公司业务归属
FORBIDDEN_MUTATION_TARGET_HISTORICAL_FORECAST: str = "historical_forecast_version"  # 历史预测版本
FORBIDDEN_MUTATION_TARGET_FROZEN_COMMITTEE: str = "frozen_committee_record"  # 已冻结投委会记录
FORBIDDEN_MUTATION_TARGET_UNREVIEWED_VALUATION: str = "unreviewed_valuation_method"  # 未经审查的正式估值方法

FORBIDDEN_MUTATION_TARGETS: tuple[str, ...] = (
    FORBIDDEN_MUTATION_TARGET_RAW_FACT,
    FORBIDDEN_MUTATION_TARGET_RAW_EVIDENCE,
    FORBIDDEN_MUTATION_TARGET_BUSINESS_ATTRIBUTION,
    FORBIDDEN_MUTATION_TARGET_HISTORICAL_FORECAST,
    FORBIDDEN_MUTATION_TARGET_FROZEN_COMMITTEE,
    FORBIDDEN_MUTATION_TARGET_UNREVIEWED_VALUATION,
)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class CalibrationStatus(str, Enum):
    """校准参数生命周期状态。

    状态机：
        candidate → offline_evaluated → null_control_passed → shadow
                  → independent_reviewed → bounded_rollout → monitoring
                  → promoted
        任意非终态阶段 → rolled_back
    """

    CANDIDATE = "candidate"
    OFFLINE_EVALUATED = "offline_evaluated"
    NULL_CONTROL_PASSED = "null_control_passed"
    SHADOW = "shadow"
    INDEPENDENT_REVIEWED = "independent_reviewed"
    BOUNDED_ROLLOUT = "bounded_rollout"
    MONITORING = "monitoring"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


# 允许的状态转移图：candidate 单向推进；任意非终态可回滚
_ALLOWED_TRANSITIONS: dict[CalibrationStatus, frozenset[CalibrationStatus]] = {
    CalibrationStatus.CANDIDATE: frozenset({
        CalibrationStatus.OFFLINE_EVALUATED,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.OFFLINE_EVALUATED: frozenset({
        CalibrationStatus.NULL_CONTROL_PASSED,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.NULL_CONTROL_PASSED: frozenset({
        CalibrationStatus.SHADOW,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.SHADOW: frozenset({
        CalibrationStatus.INDEPENDENT_REVIEWED,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.INDEPENDENT_REVIEWED: frozenset({
        CalibrationStatus.BOUNDED_ROLLOUT,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.BOUNDED_ROLLOUT: frozenset({
        CalibrationStatus.MONITORING,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.MONITORING: frozenset({
        CalibrationStatus.PROMOTED,
        CalibrationStatus.ROLLED_BACK,
    }),
    CalibrationStatus.PROMOTED: frozenset(),  # 终态
    CalibrationStatus.ROLLED_BACK: frozenset(),  # 终态
}


def is_status_transition_allowed(
    current: CalibrationStatus, target: CalibrationStatus
) -> bool:
    """查询状态转移是否允许。

    Args:
        current: 当前状态
        target: 目标状态

    Returns:
        bool: 是否允许转移
    """
    if not isinstance(current, CalibrationStatus) or not isinstance(
        target, CalibrationStatus
    ):
        return False
    if current == target:
        return True
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_domain_is_calibratable(domain: str) -> None:
    """断言领域在允许校准列表中。

    Args:
        domain: 校准领域标识

    Raises:
        LearningGovernanceError: 当 domain 不在 ALLOWED_CALIBRATION_DOMAINS 中
    """
    if domain not in ALLOWED_CALIBRATION_DOMAINS:
        raise LearningGovernanceError(
            f"学习闭环治理违规：领域 {domain!r} 不在允许校准列表中；"
            f"允许：{ALLOWED_CALIBRATION_DOMAINS}"
        )


def assert_target_not_forbidden(target: str) -> None:
    """断言修改目标不在禁止自动修改列表中。

    Args:
        target: 修改目标标识

    Raises:
        LearningGovernanceError: 当 target 在 FORBIDDEN_MUTATION_TARGETS 中
    """
    if target in FORBIDDEN_MUTATION_TARGETS:
        raise LearningGovernanceError(
            f"学习闭环治理违规：目标 {target!r} 绝对禁止自动修改；"
            f"禁止列表：{FORBIDDEN_MUTATION_TARGETS}"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """返回 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。

    Args:
        prefix: ID 前缀

    Returns:
        形如 ``prefix_xxxxxxxxxxxx`` 的 ID
    """
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class CalibrationParameter:
    """校准参数（候选）。

    一个 CalibrationParameter 表示对某个允许校准领域的一个候选参数变化。
    不持有对其他领域参数的引用，以避免跨域权重传播。

    Attributes:
        param_id: 参数 ID
        domain: 校准领域（ALLOWED_CALIBRATION_DOMAINS 之一）
        name: 参数名（如 source_validity_decay_rate）
        current_value: 当前生效值（来自上一个 promoted 参数；初始为 baseline_value）
        candidate_value: 候选值（待评估）
        baseline_value: 基线值（用于失败回滚；promote 前的 current_value）
        evidence_refs: 支持候选值的证据 ID 列表
        sample_size: 候选评估使用的样本量
        minimum_sample_size: 候选参数声明的最小样本量门槛
        bounds: 参数合法范围 (lo, hi)；candidate_value 必须落在范围内
        status: 当前生命周期状态
        owner: 候选参数提交人
        provenance: 来源说明（数据版本、计算脚本指纹等）
        created_at: 创建时间
        last_transition_at: 最近状态转移时间
        last_transition_by: 最近状态转移执行人
    """

    param_id: str = field(default_factory=lambda: new_id("calparam"))
    domain: str = ""
    name: str = ""
    current_value: float = 0.0
    candidate_value: float = 0.0
    baseline_value: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    sample_size: int = 0
    minimum_sample_size: int = 30
    bounds: tuple[float, float] = (0.0, 0.0)
    status: CalibrationStatus = CalibrationStatus.CANDIDATE
    owner: str = ""
    provenance: str = ""
    created_at: str = field(default_factory=utc_now)
    last_transition_at: str = ""
    last_transition_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "param_id": self.param_id,
            "domain": self.domain,
            "name": self.name,
            "current_value": self.current_value,
            "candidate_value": self.candidate_value,
            "baseline_value": self.baseline_value,
            "evidence_refs": list(self.evidence_refs)
            if isinstance(self.evidence_refs, (list, tuple))
            else [],
            "sample_size": self.sample_size,
            "minimum_sample_size": self.minimum_sample_size,
            "bounds": list(self.bounds)
            if isinstance(self.bounds, (list, tuple))
            else [],
            "status": self.status.value
            if isinstance(self.status, CalibrationStatus)
            else str(self.status),
            "owner": self.owner,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "last_transition_at": self.last_transition_at,
            "last_transition_by": self.last_transition_by,
        }


@dataclass
class CalibrationOutcome:
    """校准结果（终态产物）。

    一个 CalibrationParameter 进入 promoted 或 rolled_back 终态后，
    必须生成对应的 CalibrationOutcome 用于审计与回放。

    Attributes:
        param_id: 关联的参数 ID
        evaluation_metrics: 离线评估指标
        null_control_result: 空白/阴性对照测试结果
        shadow_result: shadow 阶段观察结果
        review_record: 独立审查记录（审查人、决定、备注）
        final_decision: 最终决定（promoted / rolled_back）
        rollback_reason: 回滚原因（promoted 时为空）
        created_at: 创建时间
    """

    param_id: str = ""
    evaluation_metrics: dict[str, float] = field(default_factory=dict)
    null_control_result: dict[str, float] = field(default_factory=dict)
    shadow_result: dict[str, float] = field(default_factory=dict)
    review_record: dict[str, Any] = field(default_factory=dict)
    final_decision: str = ""
    rollback_reason: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "param_id": self.param_id,
            "evaluation_metrics": dict(self.evaluation_metrics)
            if isinstance(self.evaluation_metrics, Mapping)
            else {},
            "null_control_result": dict(self.null_control_result)
            if isinstance(self.null_control_result, Mapping)
            else {},
            "shadow_result": dict(self.shadow_result)
            if isinstance(self.shadow_result, Mapping)
            else {},
            "review_record": dict(self.review_record)
            if isinstance(self.review_record, Mapping)
            else {},
            "final_decision": self.final_decision,
            "rollback_reason": self.rollback_reason,
            "created_at": self.created_at,
        }


@dataclass
class ForbiddenMutationAttempt:
    """被拒绝的禁止修改尝试（用于审计）。

    Attributes:
        attempt_id: 尝试 ID
        target: 修改目标（FORBIDDEN_MUTATION_TARGETS 之一）
        attempted_by: 尝试执行人
        reason: 拒绝原因
        created_at: 创建时间
    """

    attempt_id: str = field(default_factory=lambda: new_id("forbid"))
    target: str = ""
    attempted_by: str = ""
    reason: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "attempt_id": self.attempt_id,
            "target": self.target,
            "attempted_by": self.attempted_by,
            "reason": self.reason,
            "created_at": self.created_at,
        }
