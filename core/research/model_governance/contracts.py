"""P3.1 研究模型治理契约。

按 P3 计划 Task P3.1：
- 每个模型必须登记 19 个字段
- 状态机：draft → unit_validated → shadow → independent_validated
  → approved → active → suspended → retired
- 模型作者不能完成独立验证签署
- 配置或权重变化必须生成新版本
- active 模型必须有回滚版本
- 输入数据契约变化自动暂停相关模型
- 退役模型历史产物仍可重放
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ModelStatus(Enum):
    """研究模型生命周期状态。

    状态机：
        draft → unit_validated → shadow → independent_validated
              → approved → active → suspended → retired
    """

    DRAFT = "draft"
    UNIT_VALIDATED = "unit_validated"
    SHADOW = "shadow"
    INDEPENDENT_VALIDATED = "independent_validated"
    APPROVED = "approved"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ModelGovernanceError(ValueError):
    """模型治理违规。"""


# 允许的状态转移（单向图）
_ALLOWED_TRANSITIONS: dict[ModelStatus, frozenset[ModelStatus]] = {
    ModelStatus.DRAFT: frozenset({ModelStatus.UNIT_VALIDATED}),
    ModelStatus.UNIT_VALIDATED: frozenset({ModelStatus.SHADOW, ModelStatus.DRAFT}),
    ModelStatus.SHADOW: frozenset(
        {ModelStatus.INDEPENDENT_VALIDATED, ModelStatus.UNIT_VALIDATED}
    ),
    ModelStatus.INDEPENDENT_VALIDATED: frozenset(
        {ModelStatus.APPROVED, ModelStatus.SHADOW}
    ),
    ModelStatus.APPROVED: frozenset({ModelStatus.ACTIVE, ModelStatus.INDEPENDENT_VALIDATED}),
    ModelStatus.ACTIVE: frozenset({ModelStatus.SUSPENDED, ModelStatus.RETIRED}),
    ModelStatus.SUSPENDED: frozenset(
        {ModelStatus.ACTIVE, ModelStatus.RETIRED}
    ),
    ModelStatus.RETIRED: frozenset(),
}


@dataclass(frozen=True)
class InputContract:
    """模型输入契约；契约变化自动暂停相关 active 模型。

    Attributes:
        fields: 必填字段名 -> 字段类型描述
        data_source_refs: 引用的数据源 ID 列表
        contract_hash: 契约内容 SHA-256；任一字段变化必须重新计算
    """

    fields: dict[str, str]
    data_source_refs: list[str]
    contract_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_hash(self) -> str:
        """重新计算契约哈希。"""
        payload = {"fields": self.fields, "data_source_refs": sorted(self.data_source_refs)}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class OutputContract:
    """模型输出契约。

    Attributes:
        fields: 输出字段名 -> 字段类型描述
        validation_rules: 输出校验规则（如范围、单位）
        contract_hash: 契约内容 SHA-256
    """

    fields: dict[str, str]
    validation_rules: dict[str, str]
    contract_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_hash(self) -> str:
        payload = {"fields": self.fields, "validation_rules": self.validation_rules}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


@dataclass
class ModelVersion:
    """研究模型注册版本。

    按 P3.1 §「每个模型必须登记」的 19 个字段。
    """

    model_id: str
    model_family: str
    domain: str
    version: str
    owner: str
    reviewer: str
    training_or_calibration_window: str
    input_contract: InputContract
    output_contract: OutputContract
    applicable_universe: list[str]
    limitations: list[str]
    benchmark: dict[str, Any]
    validation_report: dict[str, Any]
    status: ModelStatus
    effective_from: str
    effective_to: str
    rollback_version: str
    code_fingerprint: str
    config_fingerprint: str
    data_fingerprint: str
    # 治理元数据
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_transition_at: str = ""
    last_transition_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_family": self.model_family,
            "domain": self.domain,
            "version": self.version,
            "owner": self.owner,
            "reviewer": self.reviewer,
            "training_or_calibration_window": self.training_or_calibration_window,
            "input_contract": self.input_contract.to_dict(),
            "output_contract": self.output_contract.to_dict(),
            "applicable_universe": list(self.applicable_universe),
            "limitations": list(self.limitations),
            "benchmark": dict(self.benchmark),
            "validation_report": dict(self.validation_report),
            "status": self.status.value,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "rollback_version": self.rollback_version,
            "code_fingerprint": self.code_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "data_fingerprint": self.data_fingerprint,
            "created_at": self.created_at,
            "last_transition_at": self.last_transition_at,
            "last_transition_by": self.last_transition_by,
        }


def is_transition_allowed(
    current: ModelStatus, target: ModelStatus
) -> bool:
    """查询状态转移是否允许。

    Args:
        current: 当前状态
        target: 目标状态

    Returns:
        bool: 是否允许转移
    """
    if current == target:
        return True
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_reviewer_independent(owner: str, reviewer: str) -> None:
    """模型作者不能完成独立验证签署。

    Args:
        owner: 模型作者 ID
        reviewer: 独立验证人 ID

    Raises:
        ModelGovernanceError: 当 owner == reviewer 时
    """
    if owner and reviewer and owner == reviewer:
        raise ModelGovernanceError(
            f"模型治理违规：作者与独立验证人不能为同一人（owner={owner}, reviewer={reviewer}）"
        )


def assert_active_has_rollback(model: ModelVersion) -> None:
    """active 模型必须有回滚版本。

    Args:
        model: 待激活的模型版本

    Raises:
        ModelGovernanceError: 当 status=active 且 rollback_version 为空时
    """
    if model.status is ModelStatus.ACTIVE and not model.rollback_version.strip():
        raise ModelGovernanceError(
            f"模型治理违规：active 模型必须指定 rollback_version（model_id={model.model_id}）"
        )


def assert_config_change_creates_new_version(
    old_fingerprint: str, new_fingerprint: str, old_version: str, new_version: str
) -> None:
    """配置或权重变化必须生成新版本。

    Args:
        old_fingerprint: 旧 config_fingerprint
        new_fingerprint: 新 config_fingerprint
        old_version: 旧 version
        new_version: 新 version

    Raises:
        ModelGovernanceError: 指纹变化但版本号未变
    """
    if old_fingerprint != new_fingerprint and old_version == new_version:
        raise ModelGovernanceError(
            "模型治理违规：config_fingerprint 变化但 version 未变"
            f"（old={old_version}, new={new_version}）"
        )
