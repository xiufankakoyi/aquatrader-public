"""P3.3 另类数据治理与停用。

按 P3 计划 Task P3.3 硬规则：
- 无授权或来源不清的数据不得进入正式模型
- 另类信号必须先成为观察或假设，不能直接升级事实
- 覆盖变化不会被误判为公司经营变化
- 每个源有数据质量 dashboard 和停用开关

本模块提供：
- AlternativeDataRegistry：数据源注册表（持久化到 data/research_control/）
- AlternativeDataGovernance：治理工作流（注册、停用、恢复、退役、质量监控）
- QualityDashboard：数据质量 dashboard 视图
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from core.research.alternative_data.contracts import (
    AlternativeDataError,
    AlternativeDataSourceContract,
    AlternativeDataSourceStatus,
    AlternativeDataSourceType,
    AuthorizationScope,
    MnpiIsolationConfig,
    QualityMetric,
    QualityMonitoringConfig,
    RetentionPolicy,
    assert_authorized_source,
    assert_mnpi_isolated,
    assert_quality_within_thresholds,
    is_status_transition_allowed,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REGISTRY_PATH = (
    PROJECT_ROOT / "data" / "research_control" / "p3_alternative_data_registry.json"
)


class AlternativeDataRegistryError(ValueError):
    """另类数据注册表操作错误。"""


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _finite_number(value: Any, default: float | int) -> float | int:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"无效数值: {value!r}") from exc
    if not isfinite(number):
        raise ValueError(f"无效数值: {value!r}")
    if isinstance(default, int):
        return int(number)
    return number


_MUTABLE_SOURCE_FIELDS = frozenset(
    {
        "source_name",
        "update_frequency",
        "known_biases",
        "entity_mapping_method",
        "timestamp_semantics",
        "coverage_scope",
        "last_transition_by",
        "suspension_reason",
        "deactivation_reason",
    }
)


class AlternativeDataRegistry:
    """另类数据源注册表。

    按 ``source_id`` 索引所有数据源；保留所有历史记录，不删除停用记录。
    持久化到 ``data/research_control/p3_alternative_data_registry.json``。
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        """初始化注册表。

        Args:
            registry_path: 注册表文件路径；
                默认 data/research_control/p3_alternative_data_registry.json
        """
        self._path = registry_path or REGISTRY_PATH
        self._records: dict[str, dict[str, Any]] = {}
        self._quality_history: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘加载注册表；文件不存在则初始化空表。"""
        if not self._path.exists():
            self._records = {}
            self._quality_history = {}
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AlternativeDataRegistryError(
                f"注册表读取失败：{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "sources" not in payload:
            raise AlternativeDataRegistryError("注册表格式错误：缺少 sources")
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            raise AlternativeDataRegistryError("注册表格式错误：sources 必须是对象")
        normalized_sources: dict[str, dict[str, Any]] = {}
        for source_id, record in sources.items():
            source_key = str(source_id)
            if not isinstance(record, dict):
                raise AlternativeDataRegistryError(
                    f"注册表格式错误：source_id={source_key} 必须是对象"
                )
            try:
                _dict_to_source(record)
            except (AttributeError, TypeError, ValueError, KeyError, OverflowError) as exc:
                raise AlternativeDataRegistryError(
                    f"注册表格式错误：source_id={source_key}: {exc}"
                ) from exc
            normalized_sources[source_key] = dict(record)
        quality = payload.get("quality_history", {})
        if not isinstance(quality, dict):
            raise AlternativeDataRegistryError(
                "注册表格式错误：quality_history 必须是对象"
            )
        normalized_quality: dict[str, list[dict[str, Any]]] = {}
        for source_id, history in quality.items():
            source_key = str(source_id)
            if not isinstance(history, list) or any(
                not isinstance(metric, dict) for metric in history
            ):
                raise AlternativeDataRegistryError(
                    f"注册表格式错误：source_id={source_key} 的 quality_history 必须是对象列表"
                )
            for metric in history:
                try:
                    _dict_to_quality_metric(metric)
                except (TypeError, ValueError, KeyError, OverflowError) as exc:
                    raise AlternativeDataRegistryError(
                        f"注册表格式错误：source_id={source_key} 的质量指标无效: {exc}"
                    ) from exc
            normalized_quality[source_key] = list(history)
        self._records = normalized_sources
        self._quality_history = normalized_quality

    def _save(self) -> None:
        """持久化到磁盘；确保父目录存在。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": self._records,
            "quality_history": self._quality_history,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(
                    payload, handle, ensure_ascii=False, indent=2, allow_nan=False
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AlternativeDataRegistryError(
                f"注册表写入失败：{type(exc).__name__}: {exc}"
            ) from exc

    def register(self, source: AlternativeDataSourceContract) -> None:
        """登记新数据源。

        按 P3.3 硬规则：登记时强制校验授权与 MNPI 隔离。

        Args:
            source: 待登记的数据源契约

        Raises:
            AlternativeDataRegistryError: source_id 已存在
            AlternativeDataError: 授权不通过或 MNPI 隔离不完整
        """
        if source.source_id in self._records:
            raise AlternativeDataRegistryError(
                f"数据源已存在：source_id={source.source_id}"
            )
        # 登记时强制校验授权与 MNPI 隔离
        assert_authorized_source(source)
        assert_mnpi_isolated(source)
        self._records[source.source_id] = source.to_dict()
        try:
            self._save()
        except AlternativeDataRegistryError:
            self._records.pop(source.source_id, None)
            raise

    def get(self, source_id: str) -> AlternativeDataSourceContract | None:
        """获取数据源；不存在返回 None。"""
        record = self._records.get(source_id)
        if record is None:
            return None
        return _dict_to_source(record)

    def list_all(self) -> list[AlternativeDataSourceContract]:
        """列出所有数据源。"""
        return [_dict_to_source(rec) for rec in self._records.values()]

    def list_by_status(
        self, status: AlternativeDataSourceStatus
    ) -> list[AlternativeDataSourceContract]:
        """按状态列出数据源。"""
        return [
            _dict_to_source(rec)
            for rec in self._records.values()
            if rec.get("status") == status.value
        ]

    def list_by_type(
        self, source_type: Any
    ) -> list[AlternativeDataSourceContract]:
        """按数据源类型列出。"""
        type_value = (
            source_type.value
            if hasattr(source_type, "value")
            else str(source_type)
        )
        return [
            _dict_to_source(rec)
            for rec in self._records.values()
            if rec.get("source_type") == type_value
        ]

    def update_status(
        self,
        source_id: str,
        new_status: AlternativeDataSourceStatus,
        actor: str,
        reason: str = "",
    ) -> AlternativeDataSourceContract:
        """更新数据源状态。

        Args:
            source_id: 数据源 ID
            new_status: 目标状态
            actor: 操作人 ID
            reason: 状态变更原因（suspended/deactivated 必填）

        Returns:
            更新后的 AlternativeDataSourceContract

        Raises:
            AlternativeDataRegistryError: 数据源不存在或状态转移非法
        """
        if not isinstance(new_status, AlternativeDataSourceStatus):
            try:
                new_status = AlternativeDataSourceStatus(new_status)
            except (TypeError, ValueError) as exc:
                raise AlternativeDataRegistryError(
                    f"无效目标状态: {new_status!r}"
                ) from exc
        record = self._records.get(source_id)
        if record is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        current_status = AlternativeDataSourceStatus(record.get("status", "active"))
        if not is_status_transition_allowed(current_status, new_status):
            raise AlternativeDataRegistryError(
                f"非法状态转移：{current_status.value} → {new_status.value}"
                f"（source_id={source_id}）"
            )
        if new_status in {
            AlternativeDataSourceStatus.SUSPENDED,
            AlternativeDataSourceStatus.DEACTIVATED,
        } and not reason:
            raise AlternativeDataRegistryError(
                f"{new_status.value} 必须记录原因（source_id={source_id}）"
            )
        previous_record = dict(record)
        record["status"] = new_status.value
        record["last_transition_at"] = datetime.now(timezone.utc).isoformat()
        record["last_transition_by"] = actor
        if new_status is AlternativeDataSourceStatus.SUSPENDED:
            record["suspension_reason"] = reason
        elif new_status is AlternativeDataSourceStatus.DEACTIVATED:
            record["deactivation_reason"] = reason
        self._records[source_id] = record
        try:
            self._save()
        except AlternativeDataRegistryError:
            self._records[source_id] = previous_record
            raise
        return _dict_to_source(record)

    def update_field(
        self, source_id: str, field_name: str, value: Any
    ) -> None:
        """直接更新记录中的字段（绕过 status 转移）。

        Args:
            source_id: 数据源 ID
            field_name: 字段名
            value: 字段值

        Raises:
            AlternativeDataRegistryError: 数据源不存在
        """
        record = self._records.get(source_id)
        if record is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        if field_name == "status":
            raise AlternativeDataRegistryError("status 字段必须通过 update_status 修改")
        if field_name not in _MUTABLE_SOURCE_FIELDS:
            raise AlternativeDataRegistryError(f"不允许直接更新字段：{field_name}")
        previous_record = dict(record)
        record[field_name] = value
        record["last_transition_at"] = datetime.now(timezone.utc).isoformat()
        self._records[source_id] = record
        try:
            self._save()
        except AlternativeDataRegistryError:
            self._records[source_id] = previous_record
            raise

    def record_quality_metric(self, metric: QualityMetric) -> None:
        """记录数据源质量指标快照。

        Args:
            metric: 质量指标快照

        Raises:
            AlternativeDataRegistryError: 数据源不存在
        """
        if metric.source_id not in self._records:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={metric.source_id}"
            )
        history = self._quality_history.setdefault(metric.source_id, [])
        history.append(metric.to_dict())
        try:
            self._save()
        except AlternativeDataRegistryError:
            history.pop()
            if not history:
                self._quality_history.pop(metric.source_id, None)
            raise

    def get_quality_history(
        self, source_id: str
    ) -> list[QualityMetric]:
        """获取数据源质量指标历史。"""
        records = self._quality_history.get(source_id, [])
        return [_dict_to_quality_metric(rec) for rec in records]

    def all_sources(self) -> dict[str, dict[str, Any]]:
        """返回全部数据源记录的深拷贝。"""
        return json.loads(json.dumps(self._records))

    def all_quality_history(self) -> dict[str, list[dict[str, Any]]]:
        """返回全部质量历史的深拷贝。"""
        return json.loads(json.dumps(self._quality_history))


class QualityDashboard:
    """数据质量 dashboard 视图。

    按 P3.3 硬规则「每个源有数据质量 dashboard 和停用开关」：
    - 聚合每个数据源的最新质量指标
    - 标记阈值突破项
    - 提供停用开关状态
    """

    def __init__(self, registry: AlternativeDataRegistry) -> None:
        """初始化 dashboard。

        Args:
            registry: 数据源注册表
        """
        self._registry = registry

    def get_source_overview(self, source_id: str) -> dict[str, Any]:
        """获取单个数据源的 dashboard 概览。

        Args:
            source_id: 数据源 ID

        Returns:
            包含 source 信息、最新质量指标、阈值突破项的字典

        Raises:
            AlternativeDataRegistryError: 数据源不存在
        """
        source = self._registry.get(source_id)
        if source is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        history = self._registry.get_quality_history(source_id)
        latest_metric = history[-1] if history else None
        breaches: list[str] = []
        if latest_metric is not None:
            try:
                assert_quality_within_thresholds(
                    latest_metric, source.quality_monitoring
                )
            except AlternativeDataError as exc:
                breaches.append(str(exc))
        return {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "source_type": source.source_type.value,
            "status": source.status.value,
            "suspension_switch": source.status.value,
            "latest_quality": latest_metric.to_dict() if latest_metric else None,
            "quality_breaches": breaches,
            "quality_history_count": len(history),
            "quality_monitoring_config": source.quality_monitoring.to_dict(),
            "mnpi_isolation": source.mnpi_isolation.to_dict(),
        }

    def get_all_overviews(self) -> list[dict[str, Any]]:
        """获取所有数据源的 dashboard 概览。"""
        return [
            self.get_source_overview(source.source_id)
            for source in self._registry.list_all()
        ]


class AlternativeDataGovernance:
    """另类数据治理工作流。

    封装状态转移规则与硬约束校验，所有写操作通过
    ``AlternativeDataRegistry`` 持久化。
    """

    def __init__(self, registry: AlternativeDataRegistry | None = None) -> None:
        """初始化治理工作流。

        Args:
            registry: 数据源注册表；默认使用 ``AlternativeDataRegistry()``
        """
        self._registry = registry or AlternativeDataRegistry()

    @property
    def registry(self) -> AlternativeDataRegistry:
        """暴露注册表（只读视图）。"""
        return self._registry

    @property
    def dashboard(self) -> QualityDashboard:
        """数据质量 dashboard。"""
        return QualityDashboard(self._registry)

    def register_source(self, source: AlternativeDataSourceContract) -> None:
        """登记新数据源。

        按 P3.3 硬规则：登记时强制校验授权与 MNPI 隔离。

        Args:
            source: 待登记的数据源契约

        Raises:
            AlternativeDataError: 授权不通过或 MNPI 隔离不完整
            AlternativeDataRegistryError: source_id 已存在
        """
        self._registry.register(source)

    def suspend(
        self, source_id: str, actor: str, reason: str
    ) -> AlternativeDataSourceContract:
        """暂停数据源（active → suspended）；必须记录原因。

        Args:
            source_id: 数据源 ID
            actor: 操作人 ID
            reason: 暂停原因

        Returns:
            更新后的 AlternativeDataSourceContract
        """
        source = self._registry.get(source_id)
        if source is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        if source.status is not AlternativeDataSourceStatus.ACTIVE:
            raise AlternativeDataError(
                f"暂停要求 active 状态（当前 {source.status.value}）"
            )
        if not reason:
            raise AlternativeDataError("暂停必须记录原因")
        return self._registry.update_status(
            source_id, AlternativeDataSourceStatus.SUSPENDED, actor, reason
        )

    def reactivate(
        self, source_id: str, actor: str
    ) -> AlternativeDataSourceContract:
        """恢复数据源（suspended → active）。

        Args:
            source_id: 数据源 ID
            actor: 操作人 ID

        Returns:
            更新后的 AlternativeDataSourceContract
        """
        source = self._registry.get(source_id)
        if source is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        if source.status is not AlternativeDataSourceStatus.SUSPENDED:
            raise AlternativeDataError(
                f"恢复要求 suspended 状态（当前 {source.status.value}）"
            )
        return self._registry.update_status(
            source_id, AlternativeDataSourceStatus.ACTIVE, actor
        )

    def deactivate(
        self, source_id: str, actor: str, reason: str
    ) -> AlternativeDataSourceContract:
        """永久停用数据源（active/suspended → deactivated）。

        停用后不可恢复，但历史记录保留。

        Args:
            source_id: 数据源 ID
            actor: 操作人 ID
            reason: 停用原因

        Returns:
            更新后的 AlternativeDataSourceContract
        """
        source = self._registry.get(source_id)
        if source is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={source_id}"
            )
        if source.status is AlternativeDataSourceStatus.DEACTIVATED:
            raise AlternativeDataError(
                f"数据源已停用，不可重复停用（source_id={source_id}）"
            )
        if not reason:
            raise AlternativeDataError("停用必须记录原因")
        return self._registry.update_status(
            source_id, AlternativeDataSourceStatus.DEACTIVATED, actor, reason
        )

    def record_quality(
        self, metric: QualityMetric
    ) -> AlternativeDataSourceContract | None:
        """记录质量指标并在阈值突破时自动停用。

        按 P3.3 硬规则「每个源有数据质量 dashboard 和停用开关」：
        - 当 auto_suspend_on_breach=True 且指标突破阈值时自动暂停 active 数据源

        Args:
            metric: 质量指标快照

        Returns:
            若触发自动停用则返回更新后的契约；否则返回 None
        """
        source = self._registry.get(metric.source_id)
        if source is None:
            raise AlternativeDataRegistryError(
                f"数据源不存在：source_id={metric.source_id}"
            )
        self._registry.record_quality_metric(metric)
        # 检查阈值
        try:
            assert_quality_within_thresholds(
                metric, source.quality_monitoring
            )
        except AlternativeDataError:
            if (
                source.quality_monitoring.auto_suspend_on_breach
                and source.status is AlternativeDataSourceStatus.ACTIVE
            ):
                return self.suspend(
                    metric.source_id,
                    actor="quality_monitor",
                    reason="质量指标突破阈值，自动停用",
                )
            return None
        return None

    def is_source_usable_for_formal_model(
        self, source_id: str
    ) -> tuple[bool, str]:
        """检查数据源是否可用于正式模型。

        按 P3.3 硬规则「无授权或来源不清的数据不得进入正式模型」：
        - 必须已授权（authorization_status=authorized）
        - 必须 active 状态
        - MNPI 隔离必须完整

        Args:
            source_id: 数据源 ID

        Returns:
            (是否可用, 原因说明)
        """
        source = self._registry.get(source_id)
        if source is None:
            return False, f"数据源不存在：{source_id}"
        if source.status is not AlternativeDataSourceStatus.ACTIVE:
            return False, f"数据源状态非 active：{source.status.value}"
        if not source.authorization_scope.is_authorized:
            return False, "数据源未授权"
        try:
            assert_mnpi_isolated(source)
        except AlternativeDataError as exc:
            return False, str(exc)
        return True, "数据源可用于正式模型"


# ---------------------------------------------------------------------------
# dict ↔ dataclass 转换工具
# ---------------------------------------------------------------------------


def _dict_to_source(record: dict[str, Any]) -> AlternativeDataSourceContract:
    """将 dict 转换为 AlternativeDataSourceContract；兼容旧记录缺失字段。"""
    auth_data = record.get("authorization_scope") or {}
    retention_data = record.get("retention_policy") or {}
    quality_data = record.get("quality_monitoring") or {}
    mnpi_data = record.get("mnpi_isolation") or {}
    return AlternativeDataSourceContract(
        source_id=_text(record.get("source_id")),
        source_name=_text(record.get("source_name")),
        source_type=AlternativeDataSourceType(
            record.get("source_type", "other")
        ),
        authorization_scope=AuthorizationScope(
            authorization_status=_text(
                auth_data.get("authorization_status"), "unknown"
            ),
            license_reference=_text(auth_data.get("license_reference")),
            usage_scope=_text_list(auth_data.get("usage_scope")),
            authorized_personnel=_text_list(auth_data.get("authorized_personnel")),
            expires_at=_text(auth_data.get("expires_at")),
        ),
        retention_policy=RetentionPolicy(
            fetch_method=_text(retention_data.get("fetch_method")),
            retention_days=_finite_number(
                retention_data.get("retention_days"), -1
            ),
            deletion_rule=_text(retention_data.get("deletion_rule")),
            pii_handling=_text(retention_data.get("pii_handling")),
        ),
        update_frequency=_text(record.get("update_frequency")),
        known_biases=_text_list(record.get("known_biases")),
        entity_mapping_method=_text(record.get("entity_mapping_method")),
        timestamp_semantics=_text(record.get("timestamp_semantics")),
        quality_monitoring=QualityMonitoringConfig(
            min_coverage=_finite_number(
                quality_data.get("min_coverage"), 0.5
            ),
            max_freshness_hours=_finite_number(
                quality_data.get("max_freshness_hours"), 72
            ),
            max_error_rate=_finite_number(
                quality_data.get("max_error_rate"), 0.1
            ),
            min_completeness=_finite_number(
                quality_data.get("min_completeness"), 0.8
            ),
            auto_suspend_on_breach=_safe_bool(
                quality_data.get("auto_suspend_on_breach"), True
            ),
        ),
        mnpi_isolation=MnpiIsolationConfig(
            may_contain_mnpi=_safe_bool(mnpi_data.get("may_contain_mnpi")),
            isolation_process=_text(mnpi_data.get("isolation_process")),
            isolated_storage_path=_text(mnpi_data.get("isolated_storage_path")),
            authorized_reviewers=_text_list(mnpi_data.get("authorized_reviewers")),
        ),
        status=AlternativeDataSourceStatus(record.get("status", "active")),
        coverage_scope=_text_list(record.get("coverage_scope")),
        created_at=_text(record.get("created_at")),
        last_transition_at=_text(record.get("last_transition_at")),
        last_transition_by=_text(record.get("last_transition_by")),
        suspension_reason=_text(record.get("suspension_reason")),
        deactivation_reason=_text(record.get("deactivation_reason")),
    )


def _dict_to_quality_metric(record: dict[str, Any]) -> QualityMetric:
    """将 dict 转换为 QualityMetric。"""
    return QualityMetric(
        source_id=_text(record.get("source_id")),
        coverage=_finite_number(record.get("coverage"), 0.0),
        freshness_hours=_finite_number(record.get("freshness_hours"), 0),
        error_rate=_finite_number(record.get("error_rate"), 0.0),
        completeness=_finite_number(record.get("completeness"), 0.0),
        measured_at=_text(record.get("measured_at")),
    )
