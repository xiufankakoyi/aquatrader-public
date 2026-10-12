"""版本化 shadow 校准守卫，不直接训练具体模型。

激活流程：
1. 从当前 active policy 读取门槛（无 active policy 时用 bootstrap）；
2. 服务端验证 TestRunRecord（test_run_id + code_version），不信任 API 传入的 test_passed；
3. 新待激活参数不能自行降低验收标准。
"""
from __future__ import annotations

from .contracts import CalibrationRun, PolicyVersion, _safe_float, _safe_int, _safe_text, utc_now
from .repository import ResearchRepository


class CalibrationController:
    """版本化 shadow→active 校准控制器。"""

    #: 版本化 bootstrap 门槛，仅在没有 active policy 时使用
    BOOTSTRAP_CRITERIA: dict[str, dict[str, float]] = {
        "knowledge": {"min_samples": 50, "min_walk_forward": 0.55, "max_false_promotion": 0.10},
        "retrieval": {"min_samples": 30, "min_walk_forward": 0.55, "max_false_promotion": 0.15},
        "alpha": {"min_samples": 20, "min_walk_forward": 0.55, "max_false_promotion": 0.10},
        # 历史域名兼容
        "source_quality": {"min_samples": 30, "min_walk_forward": 0.55, "max_false_promotion": 0.15},
        "claim_threshold": {"min_samples": 50, "min_walk_forward": 0.55, "max_false_promotion": 0.10},
        "retrieval_budget": {"min_samples": 30, "min_walk_forward": 0.55, "max_false_promotion": 0.15},
    }

    #: 反馈环测试套件
    REQUIRED_TEST_SUITE = "tests/test_feedback_loop_integration.py"

    def __init__(self, repository: ResearchRepository) -> None:
        """初始化校准控制器。

        Args:
            repository: ResearchRepository 实例。
        """
        self.repository = repository

    def _get_activation_criteria(self, domain: str) -> dict[str, float]:
        """从当前 active policy 读取门槛；无 active policy 时用 bootstrap。

        新待激活参数不能自行降低验收标准：activation_criteria 只能从
        当前 active policy 或 bootstrap 读取。
        """
        active = self.repository.get_active_policy(domain)
        if active and active.activation_criteria:
            return active.activation_criteria
        return self.BOOTSTRAP_CRITERIA.get(domain, self.BOOTSTRAP_CRITERIA["knowledge"])

    def create_shadow(self, run: CalibrationRun) -> CalibrationRun:
        """创建 shadow 校准运行。"""
        if not isinstance(run, CalibrationRun):
            raise ValueError("校准运行类型无效")
        run.status = "shadow"
        self._clamp(run)
        self.repository.save_calibration(run)
        return run

    def activate(
        self,
        calibration_id: str,
        approver: str,
        *,
        test_run_id: str = "",
        code_version: str = "",
    ) -> CalibrationRun:
        """激活校准参数。

        服务端验证流程：
        1. 从当前 active policy 读取门槛（无 active 时用 bootstrap）；
        2. 验证 sample_size / walk_forward_score / false_promotion_rate；
        3. 服务端查询 TestRunRecord，不信任 API 传入的 test_passed；
        4. 验证 test_run_id 的 code_version 与参数 code_version 一致；
        5. 验证 test_suite 包含反馈环测试。

        Args:
            calibration_id: 校准运行 ID。
            approver: 审批人。
            test_run_id: 测试运行 ID（由测试运行器写入，非 API 伪造）。
            code_version: 当前代码版本（git commit hash）。

        Returns:
            激活后的 CalibrationRun。

        Raises:
            KeyError: calibration_id 不存在。
            ValueError: 门槛不满足或测试验证失败。
        """
        run = self.repository.get_calibration(calibration_id)
        if run is None:
            raise KeyError(calibration_id)
        criteria = self._get_activation_criteria(run.domain)
        min_samples = max(0, _safe_int(criteria.get("min_samples"), 50))
        min_wf = max(0.0, min(1.0, _safe_float(criteria.get("min_walk_forward"), 0.55)))
        max_fp = max(0.0, min(1.0, _safe_float(criteria.get("max_false_promotion"), 0.10)))
        if run.sample_size < min_samples:
            raise ValueError(f"样本不足：{run.sample_size} < {min_samples}")
        metrics = run.metrics if isinstance(run.metrics, dict) else {}
        walk_forward_score = _safe_float(metrics.get("walk_forward_score"), -1.0)
        false_promotion_rate = _safe_float(metrics.get("false_promotion_rate"), 1.0)
        if walk_forward_score < min_wf:
            raise ValueError(f"walk-forward 指标未达到 {min_wf}")
        if false_promotion_rate > max_fp:
            raise ValueError(f"误升级率超过 {max_fp}")
        approver = _safe_text(approver)
        if not approver:
            raise ValueError("启用校准必须记录审批人")
        # 强制要求服务端可信测试记录，不允许跳过
        test_run_id = _safe_text(test_run_id)
        code_version = _safe_text(code_version)
        if not test_run_id:
            raise ValueError("激活校准必须提供 test_run_id")
        if not code_version:
            raise ValueError("激活校准必须提供 code_version")
        test_record = self.repository.get_test_run(test_run_id)
        if test_record is None:
            raise ValueError(f"测试运行记录不存在: {test_run_id}")
        if not test_record.passed:
            raise ValueError("测试运行记录显示未通过")
        if test_record.code_version != code_version:
            raise ValueError(
                f"测试代码版本不匹配: {test_record.code_version} != {code_version}"
            )
        if self.REQUIRED_TEST_SUITE not in test_record.test_suite:
            raise ValueError(f"测试套件必须包含 {self.REQUIRED_TEST_SUITE}")
        run.status = "active"
        run.approved_by = approver
        run.activated_at = utc_now()
        run.policy_version = run.policy_version or f"{run.domain}-{run.calibration_id}"
        self.repository.save_calibration(run)
        # 每个域只允许一个运行时 active 策略，旧版本保留用于审计和回滚。
        previous_version = ""
        for previous in self.repository.list_policies(status="active", limit=1000):
            if previous.domain == run.domain and previous.policy_version != run.policy_version:
                previous.status = "superseded"
                self.repository.save_policy(previous)
                previous_version = previous.policy_version
        self.repository.save_policy(PolicyVersion(
            policy_version=run.policy_version,
            domain=run.domain,
            status="active",
            parameters=run.parameters,
            activation_criteria=criteria,
            code_version=code_version,
            test_run_id=test_run_id,
            approved_by=approver,
            approved_at=run.activated_at,
            previous_version=previous_version,
            activated_at=run.activated_at,
        ))
        return run

    def rollback(self, calibration_id: str, approver: str) -> CalibrationRun:
        """沿 previous_version 精确回滚到上一版本。

        流程：
        1. 获取当前 calibration_run 对应的 active policy；
        2. 读取其 previous_version 字段；
        3. 若 previous_version 为空，说明无更早版本可回滚，拒绝回滚；
        4. 标记当前 policy 为 rolled_back，标记 previous_version 指向的 policy 为 active。

        Args:
            calibration_id: 校准运行 ID。
            approver: 审批人。

        Returns:
            回滚后的 CalibrationRun。

        Raises:
            KeyError: calibration_id 不存在。
            ValueError: 无审批人或无更早版本可回滚。
        """
        run = self.repository.get_calibration(calibration_id)
        if run is None:
            raise KeyError(calibration_id)
        approver = _safe_text(approver)
        if not approver:
            raise ValueError("回滚必须记录审批人")
        if not run.policy_version:
            raise ValueError("calibration_run 缺少 policy_version，无法回滚")

        current_policy = self.repository.get_policy(run.policy_version)
        if current_policy is None:
            raise ValueError(f"policy_version {run.policy_version} 不存在")

        previous_version = current_policy.previous_version
        if not previous_version:
            raise ValueError(
                f"policy {run.policy_version} 无 previous_version，"
                "已是初始版本，没有更早版本可回滚"
            )

        previous_policy = self.repository.get_policy(previous_version)
        if previous_policy is None:
            raise ValueError(f"previous_version {previous_version} 不存在于策略表")

        # 标记当前版本为 rolled_back
        run.status = "rolled_back"
        run.approved_by = approver
        self.repository.save_calibration(run)
        current_policy.status = "rolled_back"
        current_policy.approved_by = approver
        current_policy.approved_at = utc_now()
        self.repository.save_policy(current_policy)

        # 恢复 previous_version 指向的版本为 active
        previous_policy.status = "active"
        previous_policy.approved_by = approver
        previous_policy.approved_at = utc_now()
        self.repository.save_policy(previous_policy)

        return run

    @staticmethod
    def _clamp(run: CalibrationRun) -> None:
        """参数上下限钳制。"""
        if not isinstance(run, CalibrationRun):
            return
        for key, value in list(run.parameters.items()):
            numeric = _safe_float(value)
            bound = run.bounds.get(key, {}) if isinstance(run.bounds, dict) else {}
            low = _safe_float(bound.get("min"), numeric) if isinstance(bound, dict) else numeric
            high = _safe_float(bound.get("max"), numeric) if isinstance(bound, dict) else numeric
            if low > high:
                low, high = high, low
            run.parameters[key] = max(low, min(high, numeric))
