"""P3.1 研究模型注册表。

按 P3 计划 Task P3.1：
- 模型版本持久化到 ``data/research_control/p3_model_registry.json``
- 按 ``model_id`` 索引所有版本
- 支持按 status / domain / model_family 查询
- 退役模型历史产物仍可重放（不删除记录）
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.research.model_governance.contracts import (
    InputContract,
    ModelGovernanceError,
    ModelStatus,
    ModelVersion,
    OutputContract,
    assert_active_has_rollback,
    is_transition_allowed,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REGISTRY_PATH = (
    PROJECT_ROOT / "data" / "research_control" / "p3_model_registry.json"
)


class ModelRegistryError(ValueError):
    """模型注册表操作错误。"""


class ModelRegistryConflictError(ModelRegistryError):
    """登记目标已被另一个版本或 worker 占用。"""


class ModelRegistry:
    """研究模型注册表。

    按 ``model_id`` 索引所有版本；保留所有历史版本，不删除退役记录。
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        """初始化注册表。

        Args:
            registry_path: 注册表文件路径；默认 data/research_control/p3_model_registry.json
        """
        self._path = registry_path or REGISTRY_PATH
        self._records: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘加载注册表；文件不存在则初始化空表。"""
        if not self._path.exists():
            self._records = {}
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRegistryError(
                f"注册表读取失败：{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "models" not in payload:
            raise ModelRegistryError("注册表格式错误：缺少 models")
        records = payload.get("models")
        if not isinstance(records, dict):
            raise ModelRegistryError("注册表格式错误：models 必须是对象")
        normalized: dict[str, list[dict[str, Any]]] = {}
        for model_id, versions in records.items():
            model_key = str(model_id)
            if not isinstance(versions, list):
                raise ModelRegistryError(
                    f"注册表格式错误：{model_key}.versions 必须是列表"
                )
            normalized[model_key] = []
            active_count = 0
            for index, record in enumerate(versions):
                _validate_record_shape(record, model_key, index)
                if record.get("status") == ModelStatus.ACTIVE.value:
                    active_count += 1
                normalized[model_key].append(record)
            if active_count > 1:
                raise ModelRegistryError(
                    f"注册表格式错误：model_id={model_key} 只能有一个 active 版本"
                )
        self._records = normalized

    def _save(self) -> None:
        """持久化到磁盘；确保父目录存在。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "models": self._records,
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
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise ModelRegistryError(
                f"注册表写入失败：{type(exc).__name__}: {exc}"
            ) from exc

    def register(self, model: ModelVersion) -> None:
        """登记新模型版本。

        Args:
            model: 待登记的模型版本

        Raises:
            ModelRegistryError: 同 model_id + version 已存在
        """
        if not model.model_id or not model.version:
            raise ModelRegistryError("模型注册失败：model_id 和 version 不能为空")
        record = model.to_dict()
        _validate_record_shape(record, model.model_id, 0)
        if model.status is ModelStatus.ACTIVE:
            try:
                assert_active_has_rollback(model)
            except ModelGovernanceError as exc:
                raise ModelRegistryError(str(exc)) from exc
        versions = self._records.setdefault(model.model_id, [])
        for existing in versions:
            if existing.get("version") == model.version:
                raise ModelRegistryConflictError(
                    f"模型版本已存在：model_id={model.model_id}, version={model.version}"
                )
            if (
                model.status is ModelStatus.ACTIVE
                and existing.get("status") == ModelStatus.ACTIVE.value
            ):
                raise ModelRegistryConflictError(
                    f"模型只能有一个 active 版本：model_id={model.model_id}"
                )
        versions.append(record)
        try:
            self._save()
        except ModelRegistryError:
            versions.pop()
            if not versions:
                self._records.pop(model.model_id, None)
            raise

    def list_versions(self, model_id: str) -> list[ModelVersion]:
        """列出指定 model_id 的所有版本（按 created_at 升序）。"""
        records = self._records.get(model_id, [])
        sorted_records = sorted(records, key=lambda r: str(r.get("created_at", "")))
        return [_dict_to_model(record) for record in sorted_records]

    def get_version(self, model_id: str, version: str) -> ModelVersion | None:
        """获取指定版本；不存在返回 None。"""
        for record in self._records.get(model_id, []):
            if record.get("version") == version:
                return _dict_to_model(record)
        return None

    def get_active(self, model_id: str) -> ModelVersion | None:
        """获取指定 model_id 当前 active 版本；不存在返回 None。"""
        for record in self._records.get(model_id, []):
            if record.get("status") == ModelStatus.ACTIVE.value:
                return _dict_to_model(record)
        return None

    def list_by_status(self, status: ModelStatus) -> list[ModelVersion]:
        """按状态列出所有模型版本。"""
        result: list[ModelVersion] = []
        for records in self._records.values():
            for record in records:
                if record.get("status") == status.value:
                    result.append(_dict_to_model(record))
        return result

    def list_by_domain(self, domain: str) -> list[ModelVersion]:
        """按领域列出所有模型版本。"""
        result: list[ModelVersion] = []
        for records in self._records.values():
            for record in records:
                if record.get("domain") == domain:
                    result.append(_dict_to_model(record))
        return result

    def update_status(
        self,
        model_id: str,
        version: str,
        new_status: ModelStatus,
        actor: str,
    ) -> ModelVersion:
        """更新模型版本状态。

        Args:
            model_id: 模型 ID
            version: 版本号
            new_status: 目标状态
            actor: 操作人 ID

        Returns:
            更新后的 ModelVersion

        Raises:
            ModelRegistryError: 版本不存在或状态转移非法
        """
        records = self._records.get(model_id, [])
        target_index = -1
        for index, record in enumerate(records):
            if record.get("version") == version:
                target_index = index
                break
        if target_index < 0:
            raise ModelRegistryError(
                f"模型版本不存在：model_id={model_id}, version={version}"
            )
        record = records[target_index]
        current_status = ModelStatus(record.get("status", "draft"))

        if not isinstance(new_status, ModelStatus):
            raise ModelRegistryError(f"无效模型状态：{new_status}")
        if not is_transition_allowed(current_status, new_status):
            raise ModelRegistryError(
                f"非法状态转移：{current_status.value} → {new_status.value}"
                f"（model_id={model_id}, version={version}）"
            )
        if new_status is ModelStatus.ACTIVE:
            if (
                current_status is ModelStatus.APPROVED
                and not str(record.get("rollback_version", "")).strip()
            ):
                raise ModelRegistryError("active 模型必须指定 rollback_version")
            if any(
                index != target_index
                and candidate.get("status") == ModelStatus.ACTIVE.value
                for index, candidate in enumerate(records)
            ):
                raise ModelRegistryError(
                    f"模型只能有一个 active 版本：model_id={model_id}"
                )
        previous_record = dict(record)
        record["status"] = new_status.value
        record["last_transition_at"] = datetime.now(timezone.utc).isoformat()
        record["last_transition_by"] = actor
        records[target_index] = record
        try:
            self._save()
        except ModelRegistryError:
            records[target_index] = previous_record
            raise
        return _dict_to_model(record)

    def update_fields(
        self,
        model_id: str,
        version: str,
        updates: dict[str, Any],
    ) -> ModelVersion:
        """Update non-status fields through the registry persistence boundary."""
        if "status" in updates:
            raise ModelRegistryError("状态字段必须通过 update_status 修改")
        records = self._records.get(model_id, [])
        for index, record in enumerate(records):
            if record.get("version") != version:
                continue
            previous_record = dict(record)
            record.update(updates)
            _validate_record_shape(record, model_id, index)
            records[index] = record
            try:
                self._save()
            except ModelRegistryError:
                records[index] = previous_record
                raise
            return _dict_to_model(record)
        raise ModelRegistryError(
            f"模型版本不存在：model_id={model_id}, version={version}"
        )

    def all_models(self) -> dict[str, list[dict[str, Any]]]:
        """返回全部模型记录的深拷贝。"""
        return json.loads(json.dumps(self._records))


def _dict_to_model(record: dict[str, Any]) -> ModelVersion:
    """将 dict 转换为 ModelVersion；兼容旧记录缺失字段。"""
    _validate_record_shape(record, str(record.get("model_id", "")), 0)
    input_data = record.get("input_contract") or {}
    output_data = record.get("output_contract") or {}
    input_contract = InputContract(
        fields=dict(input_data.get("fields") or {}),
        data_source_refs=list(input_data.get("data_source_refs") or []),
        contract_hash=str(input_data.get("contract_hash", "")),
    )
    output_contract = OutputContract(
        fields=dict(output_data.get("fields") or {}),
        validation_rules=dict(output_data.get("validation_rules") or {}),
        contract_hash=str(output_data.get("contract_hash", "")),
    )
    return ModelVersion(
        model_id=str(record.get("model_id", "")),
        model_family=str(record.get("model_family", "")),
        domain=str(record.get("domain", "")),
        version=str(record.get("version", "")),
        owner=str(record.get("owner", "")),
        reviewer=str(record.get("reviewer", "")),
        training_or_calibration_window=str(record.get("training_or_calibration_window", "")),
        input_contract=input_contract,
        output_contract=output_contract,
        applicable_universe=list(record.get("applicable_universe") or []),
        limitations=list(record.get("limitations") or []),
        benchmark=dict(record.get("benchmark") or {}),
        validation_report=dict(record.get("validation_report") or {}),
        status=ModelStatus(record.get("status", "draft")),
        effective_from=str(record.get("effective_from", "")),
        effective_to=str(record.get("effective_to", "")),
        rollback_version=str(record.get("rollback_version", "")),
        code_fingerprint=str(record.get("code_fingerprint", "")),
        config_fingerprint=str(record.get("config_fingerprint", "")),
        data_fingerprint=str(record.get("data_fingerprint", "")),
        created_at=str(record.get("created_at", "")),
        last_transition_at=str(record.get("last_transition_at", "")),
        last_transition_by=str(record.get("last_transition_by", "")),
    )


def _validate_record_shape(
    record: Any, model_id: str, index: int
) -> None:
    """Validate persisted shape before a query can expose a raw TypeError."""
    if not isinstance(record, dict):
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 必须是对象"
        )
    for field_name in ("input_contract", "output_contract"):
        value = record.get(field_name)
        if value is not None and not isinstance(value, dict):
            raise ModelRegistryError(
                f"注册表格式错误：model_id={model_id}, index={index} 的 {field_name} 必须是对象"
            )
    input_contract = record.get("input_contract") or {}
    if not isinstance(input_contract.get("fields", {}), dict):
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 的 input_contract.fields 必须是对象"
        )
    if not isinstance(input_contract.get("data_source_refs", []), list):
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 的 input_contract.data_source_refs 必须是列表"
        )
    output_contract = record.get("output_contract") or {}
    if not isinstance(output_contract.get("fields", {}), dict):
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 的 output_contract.fields 必须是对象"
        )
    if not isinstance(output_contract.get("validation_rules", {}), dict):
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 的 output_contract.validation_rules 必须是对象"
        )
    for field_name in ("applicable_universe", "limitations"):
        value = record.get(field_name)
        if value is not None and not isinstance(value, list):
            raise ModelRegistryError(
                f"注册表格式错误：model_id={model_id}, index={index} 的 {field_name} 必须是列表"
            )
    for field_name in ("benchmark", "validation_report"):
        value = record.get(field_name)
        if value is not None and not isinstance(value, dict):
            raise ModelRegistryError(
                f"注册表格式错误：model_id={model_id}, index={index} 的 {field_name} 必须是对象"
            )
    try:
        ModelStatus(record.get("status", ModelStatus.DRAFT.value))
    except (TypeError, ValueError) as exc:
        raise ModelRegistryError(
            f"注册表格式错误：model_id={model_id}, index={index} 的 status 无效"
        ) from exc
