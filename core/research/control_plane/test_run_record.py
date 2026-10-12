"""持久化的测试运行记录，由 CI 或本地测试运行器写入。

服务端根据 test_run_id + code_version 查询持久化记录，
不信任 API 传入的 test_passed。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    Serializable,
    _as_mapping,
    _safe_bool,
    _safe_text,
    new_id,
    utc_now,
)


@dataclass
class TestRunRecord(Serializable):
    """持久化的测试运行记录。

    由测试运行器写入，API 无法伪造 passed 字段。
    CalibrationController.activate 通过 test_run_id 查询此记录，
    验证测试确实通过且 code_version 匹配。

    P2.5: 新增 data_as_of 字段，激活时核对数据时点（防止用旧数据激活新策略）。
    新增 policy_version 字段，激活时核对测试确实针对目标 policy_version。
    """

    test_run_id: str = field(default_factory=lambda: new_id("testrun"))
    code_version: str = ""            # git commit hash
    test_suite: str = ""              # 如 "tests/test_feedback_loop_integration.py"
    passed: bool = False
    started_at: str = ""
    finished_at: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    # P2.5: 数据时点（YYYYMMDD），激活时必须核对，防止用过期数据激活
    data_as_of: str = ""
    # P2.5: 关联的 policy_version，激活时必须核对（防止用其它 policy 的测试结果激活本 policy）
    policy_version: str = ""

    __test__ = False

    def __post_init__(self) -> None:
        self.test_run_id = _safe_text(self.test_run_id) or new_id("testrun")
        self.code_version = _safe_text(self.code_version)
        self.test_suite = _safe_text(self.test_suite)
        self.passed = _safe_bool(self.passed)
        self.started_at = _safe_text(self.started_at)
        self.finished_at = _safe_text(self.finished_at)
        self.metrics = _as_mapping(self.metrics)
        self.created_at = _safe_text(self.created_at) or utc_now()
        self.data_as_of = _safe_text(self.data_as_of)
        self.policy_version = _safe_text(self.policy_version)


class TestRunStore:
    """测试运行记录的持久化。"""

    __test__ = False

    def __init__(self, repository: Any) -> None:
        """初始化。

        Args:
            repository: ResearchRepository 实例。
        """
        self.repository = repository

    def record_run(
        self,
        *,
        code_version: str,
        test_suite: str,
        passed: bool,
        started_at: str = "",
        finished_at: str = "",
        metrics: dict[str, Any] | None = None,
        data_as_of: str = "",
        policy_version: str = "",
    ) -> TestRunRecord:
        """记录一次测试运行。

        Args:
            code_version: git commit hash。
            test_suite: 测试套件路径。
            passed: 是否全部通过。
            started_at: 开始时间。
            finished_at: 结束时间。
            metrics: {"passed": 14, "failed": 0, "skipped": 0}。
            data_as_of: P2.5 数据时点（YYYYMMDD），用于激活时数据新鲜度核对。
            policy_version: P2.5 关联的 policy_version，激活时核对目标策略匹配。

        Returns:
            TestRunRecord 实例。
        """
        record = TestRunRecord(
            code_version=code_version,
            test_suite=test_suite,
            passed=passed,
            started_at=started_at,
            finished_at=_safe_text(finished_at) or utc_now(),
            metrics=_as_mapping(metrics),
            data_as_of=data_as_of,
            policy_version=policy_version,
        )
        self.repository.save_test_run(record)
        return record

    def get_run(self, test_run_id: str) -> TestRunRecord | None:
        """查询测试运行记录。

        Args:
            test_run_id: 测试运行 ID。

        Returns:
            TestRunRecord 或 None。
        """
        return self.repository.get_test_run(test_run_id)

    def verify(
        self,
        test_run_id: str,
        expected_code_version: str,
        required_suite: str = "",
    ) -> bool:
        """验证测试运行记录是否满足激活条件。

        Args:
            test_run_id: 测试运行 ID。
            expected_code_version: 期望的 git commit hash。
            required_suite: 必须包含的测试套件路径。

        Returns:
            True 如果测试通过、代码版本匹配、测试套件包含 required_suite。
        """
        record = self.get_run(test_run_id)
        if record is None:
            return False
        if not record.passed:
            return False
        expected_code_version = _safe_text(expected_code_version)
        if not expected_code_version or not record.code_version:
            return False
        if record.code_version != expected_code_version:
            return False
        required_suite = _safe_text(required_suite)
        if required_suite and required_suite not in record.test_suite:
            return False
        return True
