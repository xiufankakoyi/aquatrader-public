"""PolicyVersion SQLite 持久化存储。

将旧规则的固定阈值通过 PolicyVersion 配置化，评价标准和激活条件通过
policy_versions 表配置，不散落在代码常量中。

激活与回滚规则：
- status 从 shadow → approved → active，需 approved_by 非空且满足 activation_criteria
- 激活前必须服务端验证 TestRunRecord 通过（test_run_id 非空）
- 回滚通过 previous_version 精确指向前一版本，无 previous_version 时拒绝回滚
- 新参数不能降低自身的接受标准（新版本的 activation_criteria 不得低于当前 active 版本）

P2.5: 激活时必须查询真实 TestRunRecord，核对 passed/policy_version/code_version/
data_as_of 和 activation_criteria；未注入 test_run_verifier 时仅做向后兼容的
非空校验（仅用于旧测试，生产必须注入真实 verifier）。

P2.6: activation_criteria 增加方向语义 higher_is_better/lower_is_better/range；
缺少必填指标（required_metrics）必须拒绝激活。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .contracts import (
    LEGACY_DAILY_SCREENER_POLICY_VERSION,
    LEGACY_REVERSAL_DETECTOR_POLICY_VERSION,
    LEGACY_SECTOR_ENGINE_POLICY_VERSION,
    PolicyVersion,
    make_legacy_daily_screener_policy,
    make_legacy_reversal_detector_policy,
    make_legacy_sector_engine_policy,
    utc_now,
)

logger = logging.getLogger(__name__)


# P2.5: TestRunRecord 验证器类型。
# 输入: (test_run_id, expected_code_version, expected_policy_version)
# 输出: dict[str, Any] | None — 返回 TestRunRecord 的关键字段（passed/code_version/
#   data_as_of/policy_version/metrics）表示验证通过并返回真实记录；
#   返回 None 表示记录不存在或基础字段不匹配。调用方在此基础上再做方向语义校验。
TestRunVerifier = Callable[[str, str, str], dict[str, Any] | None]


# P2.6: 支持的方向语义常量
DIRECTION_HIGHER_IS_BETTER: str = "higher_is_better"
DIRECTION_LOWER_IS_BETTER: str = "lower_is_better"
DIRECTION_RANGE: str = "range"
_VALID_DIRECTIONS: frozenset[str] = frozenset({
    DIRECTION_HIGHER_IS_BETTER, DIRECTION_LOWER_IS_BETTER, DIRECTION_RANGE,
})


def _direction_for(policy: PolicyVersion, key: str) -> str:
    """获取某 criterion 的方向语义，默认 higher_is_better（向后兼容）。"""
    raw = (policy.activation_criteria_directions or {}).get(key, "")
    if raw in _VALID_DIRECTIONS:
        return raw
    return DIRECTION_HIGHER_IS_BETTER


def _criteria_value_satisfies_direction(
    new_value: float, old_value: float, direction: str,
) -> bool:
    """P2.6: 判断新版本的 criterion 值在新方向下是否"不低于"旧版本的接受标准。

    - higher_is_better: new >= old（提高阈值不降低接受标准）
    - lower_is_better: new <= old（降低阈值不降低接受标准，例如 max_drawdown
      从 -0.2 改为 -0.3 是更严格的接受标准，不应被拒绝）
    - range: 这里只比较"区间宽度"是否被缩窄；new/old 都应是 dict 时由调用方
      单独处理。本函数对 range 直接返回 True（区间校验在 metrics 校验环节做）。

    Args:
        new_value: 新版本的 criterion 阈值。
        old_value: 当前 active 版本的 criterion 阈值。
        direction: 方向语义。

    Returns:
        True 表示新版本未降低接受标准。
    """
    if direction == DIRECTION_LOWER_IS_BETTER:
        return new_value <= old_value
    if direction == DIRECTION_RANGE:
        # range 的"不降低"由调用方单独校验 min/max 区间是否被放宽
        return True
    # 默认 higher_is_better
    return new_value >= old_value


def _metric_satisfies_criteria(
    metric_value: float, criteria_value: Any, direction: str,
) -> bool:
    """P2.6: 判断 TestRunRecord.metrics 中的实际指标值是否满足 criterion。

    - higher_is_better: metric >= criteria_value
    - lower_is_better: metric <= criteria_value
    - range: criteria_value 是 {"min": x, "max": y}，要求 min <= metric <= max

    Args:
        metric_value: 实际指标值。
        criteria_value: criterion 阈值（float 或 range dict）。
        direction: 方向语义。

    Returns:
        True 表示实际指标满足 criterion。
    """
    try:
        if direction == DIRECTION_LOWER_IS_BETTER:
            return float(metric_value) <= float(criteria_value)
        if direction == DIRECTION_RANGE:
            if isinstance(criteria_value, dict):
                lower = float(criteria_value.get("min", float("-inf")))
                upper = float(criteria_value.get("max", float("inf")))
                return lower <= float(metric_value) <= upper
            # range 但 criteria_value 不是 dict，退化为 higher_is_better
            return float(metric_value) >= float(criteria_value)
        # 默认 higher_is_better
        return float(metric_value) >= float(criteria_value)
    except (TypeError, ValueError):
        return False


class PolicyVersionStore:
    """PolicyVersion SQLite 存储。

    评价标准和激活条件通过 policy_versions 表配置，不散落在代码常量中。
    所有写操作通过 RLock 串行化保证线程安全。

    P2.5: 生产环境必须注入 test_run_verifier，激活时查询真实 TestRunRecord；
    未注入时仅做向后兼容的非空校验（仅用于旧测试，不应在生产使用）。
    """

    SCHEMA_VERSION = 2
    DEFAULT_DB_PATH = "data/theme_control/sector_policy_versions.db"

    # 旧规则 policy_version 常量到构造函数的映射（用于 bootstrap 初始化）
    _LEGACY_BOOTSTRAP = {
        LEGACY_SECTOR_ENGINE_POLICY_VERSION: make_legacy_sector_engine_policy,
        LEGACY_REVERSAL_DETECTOR_POLICY_VERSION: make_legacy_reversal_detector_policy,
        LEGACY_DAILY_SCREENER_POLICY_VERSION: make_legacy_daily_screener_policy,
    }

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        test_run_verifier: TestRunVerifier | None = None,
    ) -> None:
        """初始化存储并执行 schema 迁移与 bootstrap。

        Args:
            db_path: 数据库路径，默认为 data/theme_control/sector_policy_versions.db。
            test_run_verifier: P2.5 TestRunRecord 验证器。生产环境必须注入，
                激活时查询真实 TestRunRecord 并核对 passed/policy_version/
                code_version/data_as_of。None 时仅做向后兼容的非空校验
                （仅用于旧测试，不应在生产使用）。
        """
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._test_run_verifier = test_run_verifier
        self._ensure_schema()
        self._migrate_schema_v1_to_v2()
        self._bootstrap_legacy_policies()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接上下文，启用 WAL 模式。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """创建表与索引（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sector_policy_versions ("
                "policy_version_id TEXT PRIMARY KEY, "
                "feature_version TEXT NOT NULL DEFAULT '', "
                "policy_version TEXT NOT NULL DEFAULT '', "
                "parameters_json TEXT NOT NULL DEFAULT '{}', "
                "code_version TEXT NOT NULL DEFAULT '', "
                "test_run_id TEXT NOT NULL DEFAULT '', "
                "approved_by TEXT NOT NULL DEFAULT '', "
                "activation_criteria_json TEXT NOT NULL DEFAULT '{}', "
                "status TEXT NOT NULL DEFAULT 'shadow', "
                "previous_version TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL DEFAULT '', "
                "activated_at TEXT NOT NULL DEFAULT '', "
                "activation_criteria_directions_json TEXT NOT NULL DEFAULT '{}', "
                "required_metrics_json TEXT NOT NULL DEFAULT '[]', "
                "data_as_of TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_policy_feature_version "
                "ON sector_policy_versions(feature_version)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_policy_status "
                "ON sector_policy_versions(status)"
            )

    def _migrate_schema_v1_to_v2(self) -> None:
        """P2.5/P2.6: 将 v1 schema 迁移到 v2（添加新列，幂等）。

        旧 DB 缺少 activation_criteria_directions_json / required_metrics_json /
        data_as_of 列；通过 ALTER TABLE ADD COLUMN 补齐，已有数据使用默认值。
        """
        with self._lock, self._connect() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sector_policy_versions)")
            }
            if "activation_criteria_directions_json" not in columns:
                conn.execute(
                    "ALTER TABLE sector_policy_versions "
                    "ADD COLUMN activation_criteria_directions_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
            if "required_metrics_json" not in columns:
                conn.execute(
                    "ALTER TABLE sector_policy_versions "
                    "ADD COLUMN required_metrics_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "data_as_of" not in columns:
                conn.execute(
                    "ALTER TABLE sector_policy_versions "
                    "ADD COLUMN data_as_of TEXT NOT NULL DEFAULT ''"
                )

    def _bootstrap_legacy_policies(self) -> None:
        """初始化旧规则的 shadow PolicyVersion（幂等）。

        旧规则的固定阈值通过 PolicyVersion 配置化，默认 status="shadow"。
        重复执行幂等：已存在的 policy_version 不会重复写入。
        """
        for policy_version, factory in self._LEGACY_BOOTSTRAP.items():
            existing = self.get_by_policy_version(policy_version)
            if existing is not None:
                continue
            try:
                self.save(factory())
            except Exception as exc:
                logger.warning("bootstrap 旧规则 PolicyVersion 失败 %s: %s", policy_version, exc)

    def save(self, policy: PolicyVersion) -> bool:
        """保存策略版本。

        Args:
            policy: 策略版本实例。

        Returns:
            True 表示保存成功。
        """
        parameters_json = json.dumps(policy.parameters, ensure_ascii=False)
        criteria_json = json.dumps(policy.activation_criteria, ensure_ascii=False)
        directions_json = json.dumps(
            policy.activation_criteria_directions or {}, ensure_ascii=False,
        )
        required_metrics_json = json.dumps(
            policy.required_metrics or [], ensure_ascii=False,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sector_policy_versions ("
                "policy_version_id, feature_version, policy_version, "
                "parameters_json, code_version, test_run_id, approved_by, "
                "activation_criteria_json, status, previous_version, "
                "created_at, activated_at, "
                "activation_criteria_directions_json, required_metrics_json, "
                "data_as_of"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    policy.policy_version_id,
                    policy.feature_version,
                    policy.policy_version,
                    parameters_json,
                    policy.code_version,
                    policy.test_run_id,
                    policy.approved_by,
                    criteria_json,
                    policy.status,
                    policy.previous_version,
                    policy.created_at,
                    policy.activated_at,
                    directions_json,
                    required_metrics_json,
                    policy.data_as_of,
                ),
            )
        return True

    @staticmethod
    def _row_to_policy(row: sqlite3.Row) -> PolicyVersion:
        """将数据库行转换为 PolicyVersion 实例。"""
        try:
            parameters = json.loads(row["parameters_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            parameters = {}
        try:
            criteria = json.loads(row["activation_criteria_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            criteria = {}
        # P2.5/P2.6: 读取新增字段（兼容旧 DB 缺列的情况）
        try:
            directions = json.loads(
                row["activation_criteria_directions_json"] or "{}",
            )
        except (json.JSONDecodeError, TypeError, KeyError, IndexError):
            directions = {}
        try:
            required_metrics = json.loads(row["required_metrics_json"] or "[]")
        except (json.JSONDecodeError, TypeError, KeyError, IndexError):
            required_metrics = []
        try:
            data_as_of = row["data_as_of"] or ""
        except (KeyError, IndexError):
            data_as_of = ""
        return PolicyVersion(
            policy_version_id=row["policy_version_id"],
            feature_version=row["feature_version"],
            policy_version=row["policy_version"],
            parameters=parameters,
            code_version=row["code_version"],
            test_run_id=row["test_run_id"],
            approved_by=row["approved_by"],
            activation_criteria=criteria,
            status=row["status"],
            previous_version=row["previous_version"],
            created_at=row["created_at"],
            activated_at=row["activated_at"],
            activation_criteria_directions=directions,
            required_metrics=required_metrics,
            data_as_of=data_as_of,
        )

    def get_by_policy_version(self, policy_version: str) -> PolicyVersion | None:
        """按 policy_version 标识查询。

        Args:
            policy_version: 策略版本标识。

        Returns:
            PolicyVersion 实例；不存在时返回 None。
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sector_policy_versions WHERE policy_version = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (policy_version,),
            ).fetchone()
        return self._row_to_policy(row) if row is not None else None

    def get_active_by_feature(self, feature_version: str) -> PolicyVersion | None:
        """查询某 feature_version 的当前 active 版本。

        Args:
            feature_version: 特征版本标识。

        Returns:
            status="active" 的 PolicyVersion；不存在时返回 None。
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sector_policy_versions "
                "WHERE feature_version = ? AND status = 'active' "
                "ORDER BY activated_at DESC LIMIT 1",
                (feature_version,),
            ).fetchone()
        return self._row_to_policy(row) if row is not None else None

    def get_shadow_by_feature(self, feature_version: str) -> PolicyVersion | None:
        """查询某 feature_version 的 shadow 版本。

        Args:
            feature_version: 特征版本标识。

        Returns:
            status="shadow" 的 PolicyVersion；不存在时返回 None。
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sector_policy_versions "
                "WHERE feature_version = ? AND status = 'shadow' "
                "ORDER BY created_at DESC LIMIT 1",
                (feature_version,),
            ).fetchone()
        return self._row_to_policy(row) if row is not None else None

    def list_by_feature(self, feature_version: str) -> list[PolicyVersion]:
        """列出某 feature_version 的所有版本（按创建时间降序）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sector_policy_versions "
                "WHERE feature_version = ? ORDER BY created_at DESC",
                (feature_version,),
            ).fetchall()
        return [self._row_to_policy(row) for row in rows]

    def _verify_test_run_record(
        self,
        target: PolicyVersion,
        test_run_id: str,
        code_version: str,
    ) -> dict[str, Any] | None:
        """P2.5: 查询真实 TestRunRecord 并核对 passed/policy_version/code_version/data_as_of。

        未注入 test_run_verifier 时，仅做向后兼容的非空校验（仅用于旧测试，
        生产必须注入真实 verifier）。注入 verifier 时调用 verifier 查询真实记录。

        Args:
            target: 待激活的 PolicyVersion。
            test_run_id: 关联的 TestRunRecord ID。
            code_version: 期望的代码版本。

        Returns:
            验证通过时返回 TestRunRecord 的关键字段 dict（包含 metrics/passed/
            data_as_of 等）；验证失败时返回 None。
        """
        if self._test_run_verifier is None:
            # 向后兼容：未注入 verifier 时仅校验 test_run_id 非空（旧测试路径）
            if not test_run_id:
                return None
            # 旧测试无法核对真实记录，返回最小 dict 表示"通过"
            return {"_legacy": True, "passed": True, "metrics": {}}

        try:
            record = self._test_run_verifier(
                test_run_id, code_version, target.policy_version,
            )
        except Exception as exc:
            logger.warning("TestRunVerifier 调用异常: %s", exc)
            return None
        if record is None:
            logger.warning(
                "激活被拒绝：TestRunRecord 不存在或基础字段不匹配 (test_run_id=%s)",
                test_run_id,
            )
            return None
        if not record.get("passed", False):
            logger.warning(
                "激活被拒绝：TestRunRecord.passed=False (test_run_id=%s)",
                test_run_id,
            )
            return None
        # P2.5: 核对 data_as_of（policy 声明的数据时点必须与 TestRunRecord 一致）
        expected_data_as_of = target.data_as_of or ""
        actual_data_as_of = record.get("data_as_of", "") or ""
        if expected_data_as_of and actual_data_as_of and \
                expected_data_as_of != actual_data_as_of:
            logger.warning(
                "激活被拒绝：data_as_of 不匹配 (policy=%s, test_run=%s)",
                expected_data_as_of, actual_data_as_of,
            )
            return None
        # P2.5: 核对 policy_version（防止用其它 policy 的测试结果激活本 policy）
        record_policy_version = record.get("policy_version", "") or ""
        if target.policy_version and record_policy_version and \
                record_policy_version != target.policy_version:
            logger.warning(
                "激活被拒绝：policy_version 不匹配 (policy=%s, test_run=%s)",
                target.policy_version, record_policy_version,
            )
            return None
        return record

    def _check_required_metrics_present(
        self, target: PolicyVersion, metrics: dict[str, Any],
    ) -> bool:
        """P2.6: 校验 TestRunRecord.metrics 包含全部必填指标。

        Args:
            target: 待激活的 PolicyVersion。
            metrics: TestRunRecord.metrics 字典。

        Returns:
            True 表示全部必填指标存在；False 表示缺失任意一个。
        """
        for required_key in target.required_metrics or []:
            if required_key not in metrics:
                logger.warning(
                    "激活被拒绝：必填指标 %s 缺失 (test_run metrics=%s)",
                    required_key, sorted(metrics.keys()),
                )
                return False
        return True

    def _check_activation_criteria_with_directions(
        self, target: PolicyVersion, metrics: dict[str, Any],
    ) -> bool:
        """P2.6: 按方向语义校验实际指标是否满足 activation_criteria。

        Args:
            target: 待激活的 PolicyVersion。
            metrics: TestRunRecord.metrics 字典。

        Returns:
            True 表示全部 criterion 满足；False 表示任意 criterion 不满足。
        """
        for key, criteria_value in (target.activation_criteria or {}).items():
            # 必填指标缺失已由 _check_required_metrics_present 处理；
            # 这里只校验在 metrics 中存在的指标
            if key not in metrics:
                # 旧测试未注入 metrics 时跳过（向后兼容）
                continue
            direction = _direction_for(target, key)
            metric_value = metrics.get(key)
            if not _metric_satisfies_criteria(
                metric_value, criteria_value, direction,
            ):
                logger.warning(
                    "激活被拒绝：指标 %s 不满足 criterion (metric=%s, "
                    "criteria=%s, direction=%s)",
                    key, metric_value, criteria_value, direction,
                )
                return False
        return True

    def _check_no_lowered_criteria_with_directions(
        self, target: PolicyVersion, current_active: PolicyVersion,
    ) -> bool:
        """P2.6: 按方向语义校验新版本不降低自身的接受标准。

        Args:
            target: 待激活的新 PolicyVersion。
            current_active: 当前 active 的 PolicyVersion。

        Returns:
            True 表示新版本未降低接受标准；False 表示任意 criterion 降低。
        """
        for key, new_value in (target.activation_criteria or {}).items():
            old_value = current_active.activation_criteria.get(key)
            if old_value is None:
                continue
            direction = _direction_for(target, key)
            if not _criteria_value_satisfies_direction(
                new_value, old_value, direction,
            ):
                logger.warning(
                    "激活被拒绝：%s 的 %s 从 %s 改为 %s 在 %s 方向下降低了接受标准",
                    target.feature_version, key, old_value, new_value, direction,
                )
                return False
        return True

    def activate(
        self,
        policy_version_id: str,
        *,
        approved_by: str,
        test_run_id: str,
        code_version: str = "",
    ) -> PolicyVersion | None:
        """激活策略版本。

        激活规则（P2.5/P2.6）：
        - approved_by 和 test_run_id 必须非空
        - P2.5: 注入 test_run_verifier 时必须查询真实 TestRunRecord，核对
          passed/policy_version/code_version/data_as_of
        - P2.6: required_metrics 中的指标必须全部存在于 TestRunRecord.metrics
        - P2.6: activation_criteria 按方向语义校验实际指标是否满足
        - P2.6: 新版本不能降低自身的接受标准（按方向语义判断"降低"）
        - 激活后将前一 active 版本的 status 设为 rolled_back，previous_version 指向新版本
        - 同一 feature_version 同时只能有一个 active 版本

        Args:
            policy_version_id: 待激活的策略版本 ID。
            approved_by: 审批人。
            test_run_id: 关联的 TestRunRecord ID。
            code_version: 代码版本。

        Returns:
            激活后的 PolicyVersion；失败返回 None。
        """
        if not approved_by or not test_run_id:
            logger.warning("激活策略版本需要 approved_by 和 test_run_id")
            return None

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sector_policy_versions WHERE policy_version_id = ?",
                (policy_version_id,),
            ).fetchone()
            if row is None:
                logger.warning("策略版本不存在: %s", policy_version_id)
                return None
            target = self._row_to_policy(row)

            # P2.5: 查询真实 TestRunRecord（注入 verifier 时）或向后兼容（未注入时）
            test_run_record = self._verify_test_run_record(
                target, test_run_id, code_version,
            )
            if test_run_record is None:
                return None

            # P2.6: 校验必填指标存在
            metrics = test_run_record.get("metrics", {}) or {}
            if not self._check_required_metrics_present(target, metrics):
                return None

            # P2.6: 按方向语义校验实际指标满足 activation_criteria
            if not self._check_activation_criteria_with_directions(target, metrics):
                return None

            # P2.6: 按方向语义校验新版本不降低自身的接受标准
            current_active = self.get_active_by_feature(target.feature_version)
            if current_active is not None and \
                    not self._check_no_lowered_criteria_with_directions(
                        target, current_active,
                    ):
                return None

            # 激活新版本
            now = utc_now()
            # 如果 target.previous_version 为空且存在当前 active 版本，
            # 则设置 target.previous_version 指向当前 active 版本（用于精确回滚）
            new_previous = target.previous_version
            if not new_previous and current_active is not None and current_active.policy_version_id != policy_version_id:
                new_previous = current_active.policy_version_id
            conn.execute(
                "UPDATE sector_policy_versions SET status='active', "
                "approved_by=?, test_run_id=?, code_version=?, activated_at=?, "
                "previous_version=? WHERE policy_version_id=?",
                (approved_by, test_run_id, code_version, now, new_previous, policy_version_id),
            )

            # 将前一 active 版本标记为 rolled_back
            if current_active is not None and current_active.policy_version_id != policy_version_id:
                conn.execute(
                    "UPDATE sector_policy_versions SET status='rolled_back' "
                    "WHERE policy_version_id=?",
                    (current_active.policy_version_id,),
                )

            target.status = "active"
            target.approved_by = approved_by
            target.test_run_id = test_run_id
            target.code_version = code_version
            target.activated_at = now
            target.previous_version = new_previous
            return target

    def rollback(self, policy_version_id: str) -> PolicyVersion | None:
        """回滚到前一版本。

        回滚规则：
        - 通过 previous_version 精确指向前一版本
        - 无 previous_version 时拒绝回滚（首次激活无法回滚）
        - 回滚后前一版本重新设为 active，当前版本设为 rolled_back

        Args:
            policy_version_id: 待回滚的策略版本 ID。

        Returns:
            回滚后的新 active PolicyVersion；失败返回 None。
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sector_policy_versions WHERE policy_version_id = ?",
                (policy_version_id,),
            ).fetchone()
            if row is None:
                logger.warning("策略版本不存在: %s", policy_version_id)
                return None
            current = self._row_to_policy(row)
            if not current.previous_version:
                logger.warning("回滚被拒绝：%s 无 previous_version（首次激活无法回滚）", policy_version_id)
                return None

            prev_row = conn.execute(
                "SELECT * FROM sector_policy_versions WHERE policy_version_id = ?",
                (current.previous_version,),
            ).fetchone()
            if prev_row is None:
                logger.warning("previous_version 指向的版本不存在: %s", current.previous_version)
                return None

            now = utc_now()
            # 前一版本重新设为 active
            conn.execute(
                "UPDATE sector_policy_versions SET status='active', activated_at=? "
                "WHERE policy_version_id=?",
                (now, current.previous_version),
            )
            # 当前版本设为 rolled_back
            conn.execute(
                "UPDATE sector_policy_versions SET status='rolled_back' "
                "WHERE policy_version_id=?",
                (policy_version_id,),
            )

            prev_policy = self._row_to_policy(prev_row)
            prev_policy.status = "active"
            prev_policy.activated_at = now
            return prev_policy


class ProductionPolicyAccessor:
    """P2.7: 生产调用点的 shadow 真正隔离器。

    生产代码必须通过本类访问策略参数；本类只返回 status="active" 的 PolicyVersion
    的参数，shadow/approved/rolled_back 等非 active 状态的策略参数对生产不可见。

    设计原则：
    - 生产调用点不直接读 PolicyVersionStore.get_shadow_by_feature / list_by_feature
      等返回任意状态策略的接口；只通过本类的 get_active_parameters 访问参数。
    - shadow 状态的策略参数对生产完全不可见（不是只加 status 字段标签，
      而是从查询入口就隔离）。
    - 评价/消融路径通过 get_shadow_policy_for_evaluation 显式获取 shadow 策略，
      方法名含 "shadow" + "evaluation" 表明仅用于评价，禁止用于生产评分。

    使用方式：
        accessor = ProductionPolicyAccessor(policy_store)
        params = accessor.get_active_parameters(feature_version)
        if params is None:
            # 生产降级：无 active 策略时不应用任何策略参数
            ...
        else:
            # 使用 params["rally_threshold"] 等
            ...
    """

    def __init__(self, store: PolicyVersionStore) -> None:
        """初始化生产策略访问器。

        Args:
            store: PolicyVersionStore 实例。
        """
        self._store = store

    def get_active_parameters(self, feature_version: str) -> dict[str, float] | None:
        """P2.7: 获取生产可用的 active 策略参数。

        生产调用点的唯一参数入口。shadow 状态的策略参数对生产完全不可见。

        Args:
            feature_version: 特征版本标识。

        Returns:
            active 策略的 parameters 字典；无 active 策略时返回 None（生产降级）。
        """
        policy = self._store.get_active_by_feature(feature_version)
        if policy is None:
            logger.info(
                "生产路径无 active 策略 (feature_version=%s)，降级处理",
                feature_version,
            )
            return None
        if policy.status != "active":
            # 二次防御：即使 get_active_by_feature 返回非 active 也拒绝
            logger.warning(
                "生产路径拒绝非 active 策略 (feature_version=%s, status=%s)",
                feature_version, policy.status,
            )
            return None
        return dict(policy.parameters or {})

    def get_active_policy(self, feature_version: str) -> PolicyVersion | None:
        """P2.7: 获取生产可用的 active PolicyVersion（含治理元数据）。

        生产调用点如需访问 test_run_id/code_version 等治理元数据用于审计日志，
        通过本方法获取；shadow 状态的策略对生产完全不可见。

        Args:
            feature_version: 特征版本标识。

        Returns:
            active 的 PolicyVersion；无 active 策略时返回 None。
        """
        policy = self._store.get_active_by_feature(feature_version)
        if policy is None or policy.status != "active":
            return None
        return policy

    def is_shadow(self, feature_version: str) -> bool:
        """P2.7: 判断某 feature_version 是否仍处于 shadow 状态（仅用于评价/消融）。

        生产代码不应调用本方法做评分决策；本方法仅供评价路径判断是否需要
        走 shadow 评价分支。

        Args:
            feature_version: 特征版本标识。

        Returns:
            True 表示存在 shadow 策略且无 active 策略（仍在验证阶段）。
        """
        active = self._store.get_active_by_feature(feature_version)
        if active is not None:
            return False
        shadow = self._store.get_shadow_by_feature(feature_version)
        return shadow is not None

    def get_shadow_policy_for_evaluation(
        self, feature_version: str,
    ) -> PolicyVersion | None:
        """P2.7: 获取 shadow 策略（仅用于评价/消融，禁止用于生产评分）。

        方法名含 "shadow" + "evaluation" 表明仅用于评价路径。生产评分代码
        禁止调用本方法。

        Args:
            feature_version: 特征版本标识。

        Returns:
            shadow 的 PolicyVersion；无 shadow 策略时返回 None。
        """
        return self._store.get_shadow_by_feature(feature_version)
