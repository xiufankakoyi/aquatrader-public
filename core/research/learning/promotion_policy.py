"""P3.5 升级策略与跨域防火墙。

按 P3.5 §「验收」：
- 每次参数变化可解释、可重放、可回滚
- 不同领域独立校准，不能跨域传播权重
- 小样本不允许正式升级
- 失败校准不会污染 active 模型
"""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .contracts import (
    CalibrationOutcome,
    CalibrationParameter,
    CalibrationStatus,
    LearningGovernanceError,
    assert_domain_is_calibratable,
)


# 默认最小样本量
DEFAULT_MINIMUM_SAMPLE_SIZE: int = 30

# 各领域独立的最小样本量；不同领域可独立调整，互不影响
DOMAIN_MINIMUM_SAMPLE_SIZE: dict[str, int] = {
    "source_validity": 30,
    "retrieval_hit_rate": 50,  # 检索样本需更多
    "prediction_interval": 60,  # 区间覆盖需要更多样本
    "industry_model_error": 40,
    "event_driver_probability": 40,
    "research_budget_priority": 20,
}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


class CrossDomainFirewall:
    """跨域权重传播防火墙。

    硬规则：不同领域独立校准，不能跨域传播权重。
    任何显式跨域权重传播调用都会被拒绝并记录。
    """

    def __init__(self) -> None:
        """初始化跨域防火墙。"""
        self._rejected_attempts: list[dict[str, Any]] = []

    def assert_no_propagation(
        self,
        source_domain: str,
        target_domain: str,
        *,
        weight: float | None = None,
    ) -> None:
        """断言不会从 source_domain 向 target_domain 传播权重。

        Args:
            source_domain: 源领域
            target_domain: 目标领域
            weight: 待传播的权重（如有）

        Raises:
            LearningGovernanceError: 当 source_domain != target_domain 时（跨域传播一律拒绝）
        """
        if source_domain == target_domain:
            return  # 同域内允许
        attempt = {
            "source_domain": source_domain,
            "target_domain": target_domain,
            "weight": weight,
        }
        self._rejected_attempts.append(attempt)
        raise LearningGovernanceError(
            f"学习闭环治理违规：禁止跨域权重传播 "
            f"（source={source_domain}, target={target_domain}, weight={weight}）；"
            "不同领域必须独立校准"
        )

    @property
    def rejected_attempts(self) -> list[dict[str, Any]]:
        """返回已拒绝的跨域传播尝试列表（副本）。"""
        return [dict(a) for a in self._rejected_attempts]


class PromotionPolicy:
    """升级策略。

    决定一个候选 CalibrationParameter 是否可以从 monitoring 升级为 promoted。
    """

    def __init__(
        self,
        domain_minimum_sample_size: dict[str, int] | None = None,
    ) -> None:
        """初始化升级策略。

        Args:
            domain_minimum_sample_size: 覆盖各领域最小样本量
        """
        self._minimum_sample_size: dict[str, int] = dict(DOMAIN_MINIMUM_SAMPLE_SIZE)
        if isinstance(domain_minimum_sample_size, Mapping):
            self._minimum_sample_size.update(
                {
                    domain: minimum
                    for domain, minimum in domain_minimum_sample_size.items()
                    if isinstance(domain, str)
                    and isinstance(minimum, int)
                    and not isinstance(minimum, bool)
                    and minimum > 0
                }
            )
        self._firewall = CrossDomainFirewall()

    @property
    def firewall(self) -> CrossDomainFirewall:
        """跨域防火墙实例。"""
        return self._firewall

    def minimum_sample_size_for(self, domain: str) -> int:
        """查询指定领域的最小样本量。

        Args:
            domain: 校准领域

        Returns:
            int: 最小样本量
        """
        if not isinstance(domain, str):
            return DEFAULT_MINIMUM_SAMPLE_SIZE
        minimum = self._minimum_sample_size.get(domain, DEFAULT_MINIMUM_SAMPLE_SIZE)
        return (
            minimum
            if isinstance(minimum, int) and not isinstance(minimum, bool) and minimum > 0
            else DEFAULT_MINIMUM_SAMPLE_SIZE
        )

    def validate_candidate(self, parameter: CalibrationParameter) -> tuple[bool, list[str]]:
        """校验候选参数合法性。

        校验项：
        1. 领域必须允许校准
        2. bounds 必须合法 (lo <= hi)
        3. candidate_value 必须落在 bounds 内
        4. minimum_sample_size 不得低于领域最低要求
        5. 必须有证据引用

        Args:
            parameter: 待校验的候选参数

        Returns:
            (是否合法, 拒绝原因列表)
        """
        reasons: list[str] = []
        if not isinstance(parameter, CalibrationParameter):
            return False, ["parameter must be a CalibrationParameter"]
        # 1. 领域必须允许
        try:
            assert_domain_is_calibratable(parameter.domain)
        except LearningGovernanceError as exc:
            reasons.append(str(exc))
        # 2. bounds 必须合法
        if not isinstance(parameter.bounds, (tuple, list)) or len(parameter.bounds) != 2:
            reasons.append("bounds must contain exactly two numeric values")
            return False, reasons
        lo, hi = parameter.bounds
        lo_number = _finite_number(lo)
        hi_number = _finite_number(hi)
        candidate_number = _finite_number(parameter.candidate_value)
        if lo_number is None or hi_number is None or candidate_number is None:
            reasons.append("bounds and candidate_value must be finite numbers")
            return False, reasons
        if lo > hi:
            reasons.append(f"bounds 非法：lo={lo} > hi={hi}")
        # 3. candidate_value 必须落在 bounds 内
        if not (lo_number <= candidate_number <= hi_number):
            reasons.append(
                f"candidate_value={parameter.candidate_value} 越界 "
                f"bounds=({lo}, {hi})"
            )
        # 4. 声明的最小样本量门槛不得低于领域最低要求
        min_size = self.minimum_sample_size_for(parameter.domain)
        if (
            isinstance(parameter.minimum_sample_size, bool)
            or not isinstance(parameter.minimum_sample_size, int)
            or parameter.minimum_sample_size < min_size
        ):
            reasons.append(
                f"minimum_sample_size={parameter.minimum_sample_size} 低于领域最低要求 {min_size}"
            )
        # 5. 必须有证据引用
        if not isinstance(parameter.evidence_refs, (list, tuple)) or not parameter.evidence_refs:
            reasons.append("evidence_refs 为空；候选参数必须带证据")
        return (len(reasons) == 0, reasons)

    def can_promote(
        self,
        parameter: CalibrationParameter,
        outcome: CalibrationOutcome | None = None,
    ) -> tuple[bool, list[str]]:
        """评估参数是否可升级为 promoted。

        升级硬门禁：
        1. 必须处于 monitoring 状态（前置阶段全部完成）
        2. sample_size >= minimum_sample_size_for(domain)
        3. outcome.final_decision 不为 rolled_back
        4. outcome.null_control_result.passed 必须为 True

        Args:
            parameter: 待评估的参数
            outcome: 对应的校准结果（如有）

        Returns:
            (是否可升级, 拒绝原因列表)
        """
        reasons: list[str] = []
        if not isinstance(parameter, CalibrationParameter):
            return False, ["parameter must be a CalibrationParameter"]
        # 1. 状态门禁
        if not isinstance(parameter.status, CalibrationStatus):
            reasons.append("parameter status must be a CalibrationStatus")
        if (
            isinstance(parameter.status, CalibrationStatus)
            and parameter.status is not CalibrationStatus.MONITORING
        ):
            reasons.append(
                f"状态必须为 monitoring 才能升级，当前为 {parameter.status.value}"
            )
        # 2. 小样本门禁
        min_size = self.minimum_sample_size_for(parameter.domain)
        if (
            isinstance(parameter.sample_size, bool)
            or not isinstance(parameter.sample_size, int)
            or parameter.sample_size < min_size
        ):
            reasons.append(
                f"小样本拒绝升级：sample_size={parameter.sample_size} < "
                f"minimum={min_size}（domain={parameter.domain}）"
            )
        # 3. outcome 检查
        if outcome is not None and not isinstance(outcome, CalibrationOutcome):
            reasons.append("outcome must be a CalibrationOutcome")
        elif isinstance(outcome, CalibrationOutcome):
            if outcome.final_decision == "rolled_back":
                reasons.append("outcome.final_decision=rolled_back；不可升级")
            null_passed = (
                isinstance(outcome.null_control_result, Mapping)
                and outcome.null_control_result.get("passed") is True
            )
            if not null_passed:
                reasons.append("null_control_result.passed=False；不可升级")
        return (len(reasons) == 0, reasons)

    def assert_domain_isolated(
        self,
        parameter: CalibrationParameter,
        other_domain: str,
    ) -> None:
        """断言参数与另一领域无权重传播。

        Args:
            parameter: 待检查的参数
            other_domain: 另一领域

        Raises:
            LearningGovernanceError: 当跨域传播被检测
        """
        self._firewall.assert_no_propagation(parameter.domain, other_domain)
