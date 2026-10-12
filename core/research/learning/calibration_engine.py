"""P3.5 受治理的自校准引擎。

按 P3.5 §「上线流程」实现状态机推进：
    candidate → offline_evaluated → null_control_passed → shadow
              → independent_reviewed → bounded_rollout → monitoring
              → promoted
    任意非终态阶段 → rolled_back（失败回滚）

按 P3.5 §「绝对禁止自动修改」：
    引擎不会写入任何禁止目标；任何通过 attempt_forbidden_mutation 的尝试
    都会被拒绝并记录为 ForbiddenMutationAttempt。

按 P3.5 §「验收」：
    - 每次参数变化可解释、可重放、可回滚：所有状态转移写入历史
    - 不同领域独立校准：list_parameters 按 domain 隔离
    - 小样本不允许正式升级：promote 调用 PromotionPolicy.can_promote
    - 失败校准不会污染 active 模型：rollback 不修改 current_value
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    CalibrationOutcome,
    CalibrationParameter,
    CalibrationStatus,
    FORBIDDEN_MUTATION_TARGETS,
    ForbiddenMutationAttempt,
    LearningGovernanceError,
    assert_domain_is_calibratable,
    is_status_transition_allowed,
    utc_now,
)
from .promotion_policy import PromotionPolicy


class CalibrationEngine:
    """受治理的自校准引擎。

    维护候选参数与终态结果的内存存储（按域隔离），驱动状态机推进，
    并拒绝任何对禁止目标的修改尝试。

    Attributes:
        policy: 升级策略
    """

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        """初始化校准引擎。

        Args:
            policy: 自定义升级策略；默认使用 PromotionPolicy()
        """
        self.policy: PromotionPolicy = policy if policy is not None else PromotionPolicy()
        self._parameters: dict[str, CalibrationParameter] = {}
        self._outcomes: dict[str, CalibrationOutcome] = {}
        self._history: list[dict[str, Any]] = []
        self._forbidden_attempts: list[ForbiddenMutationAttempt] = []

    # ------------------------------------------------------------------
    # 候选提交
    # ------------------------------------------------------------------

    def submit_candidate(self, parameter: CalibrationParameter) -> str:
        """提交候选参数。

        Args:
            parameter: 候选参数（status 必须为 CANDIDATE）

        Returns:
            param_id

        Raises:
            LearningGovernanceError: 领域不允许、bounds 越界、状态非 candidate、缺少证据
        """
        if not isinstance(parameter, CalibrationParameter):
            raise LearningGovernanceError("parameter must be a CalibrationParameter")
        assert_domain_is_calibratable(parameter.domain)
        if not isinstance(parameter.status, CalibrationStatus):
            raise LearningGovernanceError("parameter status must be a CalibrationStatus")
        if parameter.param_id in self._parameters:
            raise LearningGovernanceError(
                f"param_id already exists: {parameter.param_id}"
            )
        if parameter.status is not CalibrationStatus.CANDIDATE:
            raise LearningGovernanceError(
                f"提交候选参数 status 必须为 candidate，当前为 {parameter.status.value}"
            )
        ok, reasons = self.policy.validate_candidate(parameter)
        if not ok:
            raise LearningGovernanceError(
                "候选参数校验失败：" + "; ".join(reasons)
            )
        # 基线值兜底：未显式设置时以 current_value 作为基线
        if parameter.baseline_value == 0.0 and parameter.current_value != 0.0:
            parameter.baseline_value = parameter.current_value
        self._parameters[parameter.param_id] = parameter
        self._record_history(parameter, "submit_candidate", "")
        return parameter.param_id

    # ------------------------------------------------------------------
    # 状态机推进
    # ------------------------------------------------------------------

    def _assert_transition(
        self, parameter: CalibrationParameter, target: CalibrationStatus
    ) -> None:
        """断言状态转移合法。

        Args:
            parameter: 待转移的参数
            target: 目标状态

        Raises:
            LearningGovernanceError: 非法转移
        """
        if not is_status_transition_allowed(parameter.status, target):
            raise LearningGovernanceError(
                f"非法状态转移：{parameter.status.value} → {target.value} "
                f"（param_id={parameter.param_id}）"
            )

    def _advance(
        self,
        param_id: str,
        target: CalibrationStatus,
        actor: str,
        metrics: dict[str, float] | None = None,
        note: str = "",
    ) -> CalibrationParameter:
        """推进参数到下一状态并记录历史。

        Args:
            param_id: 参数 ID
            target: 目标状态
            actor: 执行人
            metrics: 阶段指标（可选）
            note: 备注

        Returns:
            更新后的参数

        Raises:
            LearningGovernanceError: 参数不存在或非法转移
        """
        parameter = self._require_parameter(param_id)
        self._assert_transition(parameter, target)
        safe_metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
        parameter.status = target
        parameter.last_transition_at = utc_now()
        parameter.last_transition_by = actor
        self._record_history(
            parameter, f"advance_to_{target.value}", actor, safe_metrics, note
        )
        return parameter

    def run_offline_evaluation(
        self,
        param_id: str,
        metrics: dict[str, float],
        actor: str = "",
    ) -> CalibrationParameter:
        """执行离线评估。

        Args:
            param_id: 参数 ID
            metrics: 离线评估指标
            actor: 执行人

        Returns:
            更新后的参数
        """
        self._require_parameter(param_id)
        if not isinstance(metrics, Mapping):
            raise LearningGovernanceError("metrics must be a mapping")
        outcome = self._get_or_create_outcome(param_id)
        outcome.evaluation_metrics.update(metrics)
        return self._advance(
            param_id, CalibrationStatus.OFFLINE_EVALUATED, actor, metrics,
            note="offline_evaluation",
        )

    def run_null_control_test(
        self,
        param_id: str,
        null_metrics: dict[str, float],
        actor: str = "",
    ) -> CalibrationParameter:
        """执行空白/阴性对照测试。

        只有 passed=True 才能推进到 null_control_passed。
        失败则自动触发回滚。

        Args:
            param_id: 参数 ID
            null_metrics: 对照测试指标，必须包含 passed 键
            actor: 执行人

        Returns:
            更新后的参数

        Raises:
            LearningGovernanceError: 对照测试未通过（已自动回滚）
        """
        self._require_parameter(param_id)
        if not isinstance(null_metrics, Mapping):
            raise LearningGovernanceError("null_metrics must be a mapping")
        outcome = self._get_or_create_outcome(param_id)
        outcome.null_control_result = dict(null_metrics)
        passed = null_metrics.get("passed") is True
        if not passed:
            parameter = self._require_parameter(param_id)
            self._record_history(
                parameter, "null_control_failed", actor, null_metrics,
                "null control test failed; auto-rollback",
            )
            self.rollback(param_id, actor, reason="null_control_failed")
            raise LearningGovernanceError(
                f"空白/阴性对照测试未通过（param_id={param_id}）；已自动回滚"
            )
        return self._advance(
            param_id, CalibrationStatus.NULL_CONTROL_PASSED, actor, null_metrics,
            note="null_control_passed",
        )

    def start_shadow(
        self,
        param_id: str,
        shadow_metrics: dict[str, float],
        actor: str = "",
    ) -> CalibrationParameter:
        """进入 shadow 阶段。

        Args:
            param_id: 参数 ID
            shadow_metrics: shadow 阶段观察指标
            actor: 执行人

        Returns:
            更新后的参数
        """
        self._require_parameter(param_id)
        if not isinstance(shadow_metrics, Mapping):
            raise LearningGovernanceError("shadow_metrics must be a mapping")
        outcome = self._get_or_create_outcome(param_id)
        outcome.shadow_result = dict(shadow_metrics)
        return self._advance(
            param_id, CalibrationStatus.SHADOW, actor, shadow_metrics,
            note="shadow_started",
        )

    def independent_review(
        self,
        param_id: str,
        reviewer: str,
        decision: str,
        note: str = "",
    ) -> CalibrationParameter:
        """独立审查。

        按治理要求，审查人不能与候选参数 owner 为同一人。
        审查决定为 rejected 时自动触发回滚。

        Args:
            param_id: 参数 ID
            reviewer: 审查人 ID
            decision: 审查决定（"approved" / "rejected"）
            note: 审查备注

        Returns:
            更新后的参数

        Raises:
            LearningGovernanceError: 审查人与 owner 相同；或决定为 rejected（已自动回滚）
        """
        parameter = self._require_parameter(param_id)
        if parameter.owner and reviewer and parameter.owner == reviewer:
            raise LearningGovernanceError(
                f"独立审查违规：审查人不能与候选参数 owner 为同一人 "
                f"（owner={parameter.owner}, reviewer={reviewer}）"
            )
        outcome = self._get_or_create_outcome(param_id)
        outcome.review_record = {
            "reviewer": reviewer,
            "decision": decision,
            "note": note,
            "reviewed_at": utc_now(),
        }
        if decision != "approved":
            self.rollback(param_id, reviewer, reason=f"review_rejected: {note}")
            raise LearningGovernanceError(
                f"独立审查未通过（decision={decision}）；已自动回滚"
            )
        return self._advance(
            param_id, CalibrationStatus.INDEPENDENT_REVIEWED, reviewer,
            note=f"independent_review_approved: {note}",
        )

    def bounded_rollout(
        self,
        param_id: str,
        rollout_metrics: dict[str, float],
        actor: str = "",
    ) -> CalibrationParameter:
        """有限启用（bounded rollout）。

        Args:
            param_id: 参数 ID
            rollout_metrics: rollout 阶段指标
            actor: 执行人

        Returns:
            更新后的参数
        """
        return self._advance(
            param_id, CalibrationStatus.BOUNDED_ROLLOUT, actor, rollout_metrics,
            note="bounded_rollout",
        )

    def monitor(
        self,
        param_id: str,
        monitor_metrics: dict[str, float],
        actor: str = "",
    ) -> CalibrationParameter:
        """监控阶段。

        Args:
            param_id: 参数 ID
            monitor_metrics: 监控指标
            actor: 执行人

        Returns:
            更新后的参数
        """
        return self._advance(
            param_id, CalibrationStatus.MONITORING, actor, monitor_metrics,
            note="monitoring",
        )

    def promote(self, param_id: str, actor: str = "") -> CalibrationParameter:
        """正式升级。

        升级前调用 PromotionPolicy.can_promote 检查所有门禁。
        升级后 candidate_value 提交为 current_value，baseline_value 保留为升级前的 current_value，
        并生成终态 CalibrationOutcome。

        Args:
            param_id: 参数 ID
            actor: 执行人

        Returns:
            更新后的参数

        Raises:
            LearningGovernanceError: 升级门禁未通过
        """
        parameter = self._require_parameter(param_id)
        outcome = self._get_or_create_outcome(param_id)
        ok, reasons = self.policy.can_promote(parameter, outcome)
        if not ok:
            raise LearningGovernanceError(
                f"升级门禁未通过（param_id={param_id}）：" + "; ".join(reasons)
            )
        # 将 candidate_value 提交为 current_value；baseline_value 保留升级前的 current_value
        parameter.baseline_value = parameter.current_value
        parameter.current_value = parameter.candidate_value
        outcome.final_decision = "promoted"
        self._advance(
            param_id, CalibrationStatus.PROMOTED, actor, note="promoted",
        )
        self._outcomes[param_id] = outcome
        return parameter

    def rollback(
        self,
        param_id: str,
        actor: str = "",
        reason: str = "",
    ) -> CalibrationParameter:
        """回滚。

        回滚不修改 current_value（保证失败校准不污染 active 模型）；
        状态置为 rolled_back，candidate_value 被丢弃。

        Args:
            param_id: 参数 ID
            actor: 执行人
            reason: 回滚原因

        Returns:
            更新后的参数

        Raises:
            LearningGovernanceError: 参数已处于终态（promoted/rolled_back）
        """
        parameter = self._require_parameter(param_id)
        if parameter.status in (CalibrationStatus.ROLLED_BACK, CalibrationStatus.PROMOTED):
            raise LearningGovernanceError(
                f"参数已处于终态 {parameter.status.value}，不能回滚"
            )
        # 不修改 current_value：失败校准不污染 active 模型
        parameter.status = CalibrationStatus.ROLLED_BACK
        parameter.last_transition_at = utc_now()
        parameter.last_transition_by = actor
        outcome = self._get_or_create_outcome(param_id)
        outcome.final_decision = "rolled_back"
        outcome.rollback_reason = reason
        self._outcomes[param_id] = outcome
        self._record_history(
            parameter, "rollback", actor, note=f"reason={reason}",
        )
        return parameter

    # ------------------------------------------------------------------
    # 禁止修改保护
    # ------------------------------------------------------------------

    def attempt_forbidden_mutation(
        self, target: str, attempted_by: str = ""
    ) -> None:
        """尝试修改禁止目标；若目标在禁止列表中则记录并抛出异常。

        作为引擎对所有写入操作的统一保护入口。调用方在执行任何"修改"前
        应先调用此方法；若返回则表示目标不在禁止列表中，可继续。

        Args:
            target: 修改目标标识
            attempted_by: 尝试执行人

        Raises:
            LearningGovernanceError: 当 target 在 FORBIDDEN_MUTATION_TARGETS 中
        """
        if target in FORBIDDEN_MUTATION_TARGETS:
            attempt = ForbiddenMutationAttempt(
                target=target,
                attempted_by=attempted_by,
                reason=f"target {target!r} 在禁止自动修改列表中",
            )
            self._forbidden_attempts.append(attempt)
            raise LearningGovernanceError(
                f"学习闭环治理违规：目标 {target!r} 绝对禁止自动修改"
            )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_parameter(self, param_id: str) -> CalibrationParameter | None:
        """查询参数。

        Args:
            param_id: 参数 ID

        Returns:
            参数对象；不存在返回 None
        """
        return self._parameters.get(param_id)

    def list_parameters(
        self, domain: str | None = None
    ) -> list[CalibrationParameter]:
        """列出参数；按 domain 隔离。

        Args:
            domain: 校准领域；None 表示全部

        Returns:
            参数列表
        """
        if domain is None:
            return list(self._parameters.values())
        return [p for p in self._parameters.values() if p.domain == domain]

    def get_outcome(self, param_id: str) -> CalibrationOutcome | None:
        """查询校准结果。

        Args:
            param_id: 参数 ID

        Returns:
            校准结果；不存在返回 None
        """
        return self._outcomes.get(param_id)

    def list_forbidden_attempts(self) -> list[ForbiddenMutationAttempt]:
        """列出禁止修改尝试记录。

        Returns:
            禁止修改尝试列表（副本）
        """
        return list(self._forbidden_attempts)

    def history(self, param_id: str | None = None) -> list[dict[str, Any]]:
        """查询状态转移历史（可重放）。

        Args:
            param_id: 参数 ID；None 表示全部

        Returns:
            历史记录列表
        """
        if param_id is None:
            return [dict(h) for h in self._history]
        return [dict(h) for h in self._history if h.get("param_id") == param_id]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _require_parameter(self, param_id: str) -> CalibrationParameter:
        """获取参数；不存在则抛异常。

        Args:
            param_id: 参数 ID

        Returns:
            参数对象

        Raises:
            LearningGovernanceError: 参数不存在
        """
        parameter = self._parameters.get(param_id)
        if parameter is None:
            raise LearningGovernanceError(
                f"参数不存在：param_id={param_id}"
            )
        return parameter

    def _get_or_create_outcome(self, param_id: str) -> CalibrationOutcome:
        """获取或创建 outcome。

        Args:
            param_id: 参数 ID

        Returns:
            CalibrationOutcome
        """
        if param_id not in self._outcomes:
            self._outcomes[param_id] = CalibrationOutcome(param_id=param_id)
        return self._outcomes[param_id]

    def _record_history(
        self,
        parameter: CalibrationParameter,
        action: str,
        actor: str,
        metrics: dict[str, float] | None = None,
        note: str = "",
    ) -> None:
        """记录状态转移历史。

        Args:
            parameter: 参数对象
            action: 动作名
            actor: 执行人
            metrics: 阶段指标
            note: 备注
        """
        self._history.append({
            "param_id": parameter.param_id,
            "domain": parameter.domain,
            "action": action,
            "actor": actor,
            "status": parameter.status.value,
            "current_value": parameter.current_value,
            "candidate_value": parameter.candidate_value,
            "baseline_value": parameter.baseline_value,
            "metrics": dict(metrics) if isinstance(metrics, Mapping) else {},
            "note": note,
            "recorded_at": utc_now(),
        })
