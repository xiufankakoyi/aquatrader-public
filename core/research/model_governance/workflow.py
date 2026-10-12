"""P3.1 研究模型生命周期工作流。

按 P3 计划 Task P3.1 硬规则：
- 模型作者不能完成独立验证签署
- 配置或权重变化必须生成新版本
- active 模型必须有回滚版本
- 输入数据契约变化自动暂停相关模型
- 退役模型历史产物仍可重放
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.research.model_governance.contracts import (
    ModelGovernanceError,
    ModelStatus,
    ModelVersion,
    assert_active_has_rollback,
    assert_config_change_creates_new_version,
    assert_reviewer_independent,
)
from core.research.model_governance.registry import ModelRegistry


class ModelWorkflowError(ValueError):
    """工作流操作错误。"""


class ModelWorkflow:
    """研究模型生命周期工作流。

    封装状态转移规则与硬约束校验，所有写操作通过 ``ModelRegistry`` 持久化。
    """

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        """初始化工作流。

        Args:
            registry: 模型注册表；默认使用 ``ModelRegistry()``
        """
        self._registry = registry or ModelRegistry()

    @property
    def registry(self) -> ModelRegistry:
        """暴露注册表（只读视图）。"""
        return self._registry

    def submit_draft(self, model: ModelVersion) -> None:
        """登记新草稿模型。

        Args:
            model: 待登记的模型版本；status 必须为 draft

        Raises:
            ModelWorkflowError: status 不是 draft
        """
        if model.status is not ModelStatus.DRAFT:
            raise ModelWorkflowError(
                f"新登记模型必须是 draft 状态（当前 {model.status.value}）"
            )
        self._registry.register(model)

    def unit_validate(
        self, model_id: str, version: str, actor: str, report: dict[str, Any]
    ) -> ModelVersion:
        """提交单元验证报告并将模型状态从 draft 提升至 unit_validated。

        Args:
            model_id: 模型 ID
            version: 版本号
            actor: 操作人 ID（必须 == model.owner）
            report: 单元验证报告

        Returns:
            更新后的 ModelVersion
        """
        model = self._require_version(model_id, version)
        if actor != model.owner:
            raise ModelWorkflowError(
                f"单元验证只能由作者本人提交（owner={model.owner}, actor={actor}）"
            )
        if not report:
            raise ModelWorkflowError("单元验证报告不能为空")
        # 直接修改记录中的 validation_report 字段
        self._update_validation_report(model_id, version, "unit_validation", report)
        return self._registry.update_status(
            model_id, version, ModelStatus.UNIT_VALIDATED, actor
        )

    def start_shadow(
        self, model_id: str, version: str, actor: str
    ) -> ModelVersion:
        """将模型从 unit_validated 提升至 shadow。"""
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.UNIT_VALIDATED:
            raise ModelWorkflowError(
                f"shadow 启动要求 unit_validated 状态（当前 {model.status.value}）"
            )
        return self._registry.update_status(
            model_id, version, ModelStatus.SHADOW, actor
        )

    def independent_validate(
        self,
        model_id: str,
        version: str,
        reviewer: str,
        report: dict[str, Any],
    ) -> ModelVersion:
        """独立验证签署；模型作者不能完成此步骤。

        Args:
            model_id: 模型 ID
            version: 版本号
            reviewer: 独立验证人 ID（必须 != model.owner）
            report: 独立验证报告

        Returns:
            更新后的 ModelVersion
        """
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.SHADOW:
            raise ModelWorkflowError(
                f"独立验证要求 shadow 状态（当前 {model.status.value}）"
            )
        assert_reviewer_independent(model.owner, reviewer)
        if not report:
            raise ModelWorkflowError("独立验证报告不能为空")
        self._update_validation_report(
            model_id, version, "independent_validation", report
        )
        # 同时更新 reviewer 字段
        self._update_field(model_id, version, "reviewer", reviewer)
        return self._registry.update_status(
            model_id, version, ModelStatus.INDEPENDENT_VALIDATED, reviewer
        )

    def approve(
        self, model_id: str, version: str, actor: str
    ) -> ModelVersion:
        """将模型从 independent_validated 提升至 approved。"""
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.INDEPENDENT_VALIDATED:
            raise ModelWorkflowError(
                f"审批要求 independent_validated 状态（当前 {model.status.value}）"
            )
        return self._registry.update_status(
            model_id, version, ModelStatus.APPROVED, actor
        )

    def activate(
        self,
        model_id: str,
        version: str,
        actor: str,
        rollback_version: str,
    ) -> ModelVersion:
        """将模型从 approved 提升至 active；必须指定 rollback_version。

        按 P3.1 硬规则：active 模型必须有回滚版本。

        Args:
            model_id: 模型 ID
            version: 版本号
            actor: 操作人 ID
            rollback_version: 回滚版本号；必须是已登记的版本

        Returns:
            更新后的 ModelVersion
        """
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.APPROVED:
            raise ModelWorkflowError(
                f"激活要求 approved 状态（当前 {model.status.value}）"
            )
        if not rollback_version:
            raise ModelWorkflowError("active 模型必须指定 rollback_version")
        rollback_model = self._registry.get_version(model_id, rollback_version)
        if rollback_model is None:
            raise ModelWorkflowError(
                f"rollback_version 不存在：{rollback_version}"
            )
        self._update_field(model_id, version, "rollback_version", rollback_version)
        updated = self._registry.update_status(
            model_id, version, ModelStatus.ACTIVE, actor
        )
        assert_active_has_rollback(updated)
        return updated

    def suspend(
        self,
        model_id: str,
        version: str,
        actor: str,
        reason: str,
    ) -> ModelVersion:
        """暂停 active 模型；必须记录原因。

        Args:
            model_id: 模型 ID
            version: 版本号
            actor: 操作人 ID
            reason: 暂停原因

        Returns:
            更新后的 ModelVersion
        """
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.ACTIVE:
            raise ModelWorkflowError(
                f"暂停要求 active 状态（当前 {model.status.value}）"
            )
        if not reason:
            raise ModelWorkflowError("暂停必须记录原因")
        self._update_field(
            model_id, version, "suspension_reason", reason
        )
        return self._registry.update_status(
            model_id, version, ModelStatus.SUSPENDED, actor
        )

    def resume(
        self, model_id: str, version: str, actor: str
    ) -> ModelVersion:
        """从 suspended 恢复到 active。"""
        model = self._require_version(model_id, version)
        if model.status is not ModelStatus.SUSPENDED:
            raise ModelWorkflowError(
                f"恢复要求 suspended 状态（当前 {model.status.value}）"
            )
        return self._registry.update_status(
            model_id, version, ModelStatus.ACTIVE, actor
        )

    def retire(
        self, model_id: str, version: str, actor: str, reason: str
    ) -> ModelVersion:
        """退役模型；历史产物仍可重放（不删除记录）。

        Args:
            model_id: 模型 ID
            version: 版本号
            actor: 操作人 ID
            reason: 退役原因
        """
        model = self._require_version(model_id, version)
        if model.status not in {ModelStatus.ACTIVE, ModelStatus.SUSPENDED}:
            raise ModelWorkflowError(
                f"退役要求 active 或 suspended 状态（当前 {model.status.value}）"
            )
        if not reason:
            raise ModelWorkflowError("退役必须记录原因")
        self._update_field(model_id, version, "retirement_reason", reason)
        return self._registry.update_status(
            model_id, version, ModelStatus.RETIRED, actor
        )

    def rollback(
        self, model_id: str, from_version: str, actor: str, reason: str
    ) -> ModelVersion:
        """回滚到 rollback_version 指定的版本。

        Args:
            model_id: 模型 ID
            from_version: 当前 active 版本
            actor: 操作人 ID
            reason: 回滚原因

        Returns:
            回滚后的 active ModelVersion
        """
        model = self._require_version(model_id, from_version)
        if model.status is not ModelStatus.ACTIVE:
            raise ModelWorkflowError(
                f"回滚要求 active 状态（当前 {model.status.value}）"
            )
        if not model.rollback_version:
            raise ModelWorkflowError(
                f"当前版本未指定 rollback_version，无法回滚"
            )
        if not reason:
            raise ModelWorkflowError("回滚必须记录原因")
        # 先暂停当前版本
        self._update_field(
            model_id, from_version, "rollback_reason", reason
        )
        self._registry.update_status(
            model_id, from_version, ModelStatus.SUSPENDED, actor
        )
        # 再将 rollback_version 激活（绕过 approve 路径，直接 suspended → active）
        rollback_model = self._require_version(model_id, model.rollback_version)
        if rollback_model.status is not ModelStatus.SUSPENDED:
            # 如果回滚版本不是 suspended，可能本身就是之前被替换的 active
            # 此时需要先 suspend 它，但状态机不允许 retired → suspended
            # 安全做法：要求 rollback_version 必须是 suspended 状态
            raise ModelWorkflowError(
                f"rollback_version 必须是 suspended 状态"
                f"（当前 {rollback_model.status.value}）"
            )
        return self._registry.update_status(
            model_id, model.rollback_version, ModelStatus.ACTIVE, actor
        )

    def register_new_version(
        self, new_model: ModelVersion, actor: str
    ) -> ModelVersion:
        """登记新版本；配置或权重变化必须生成新版本。

        Args:
            new_model: 新模型版本
            actor: 操作人 ID

        Returns:
            登记后的 ModelVersion
        """
        existing_versions = self._registry.list_versions(new_model.model_id)
        if existing_versions:
            latest = existing_versions[-1]
            assert_config_change_creates_new_version(
                latest.config_fingerprint,
                new_model.config_fingerprint,
                latest.version,
                new_model.version,
            )
        self._registry.register(new_model)
        return new_model

    def _require_version(
        self, model_id: str, version: str
    ) -> ModelVersion:
        """获取版本或抛异常。"""
        model = self._registry.get_version(model_id, version)
        if model is None:
            raise ModelWorkflowError(
                f"模型版本不存在：model_id={model_id}, version={version}"
            )
        return model

    def _update_field(
        self, model_id: str, version: str, field_name: str, value: Any
    ) -> None:
        """直接更新记录中的字段（绕过 status 转移）。"""
        try:
            self._registry.update_fields(
                model_id,
                version,
                {
                    field_name: value,
                    "last_transition_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
            )
        except ModelRegistryError as exc:
            raise ModelWorkflowError(str(exc)) from exc

    def _update_validation_report(
        self,
        model_id: str,
        version: str,
        stage: str,
        report: dict[str, Any],
    ) -> None:
        """合并更新 validation_report 字段。"""
        model = self._registry.get_version(model_id, version)
        if model is None:
            raise ModelWorkflowError(
                f"模型版本不存在：model_id={model_id}, version={version}"
            )
        validation_report = dict(model.validation_report)
        validation_report[stage] = {
            "report": report,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._registry.update_fields(
                model_id,
                version,
                {
                    "validation_report": validation_report,
                    "last_transition_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
            )
        except ModelRegistryError as exc:
            raise ModelWorkflowError(str(exc)) from exc


def suspend_models_on_input_contract_change(
    workflow: ModelWorkflow,
    old_contract_hash: str,
    new_contract_hash: str,
    actor: str,
    reason: str = "输入数据契约变化",
) -> list[ModelVersion]:
    """输入数据契约变化自动暂停相关 active 模型。

    Args:
        workflow: 模型工作流
        old_contract_hash: 旧输入契约哈希
        new_contract_hash: 新输入契约哈希
        actor: 操作人 ID
        reason: 暂停原因

    Returns:
        被暂停的 ModelVersion 列表
    """
    if old_contract_hash == new_contract_hash:
        return []
    active_models = workflow.registry.list_by_status(ModelStatus.ACTIVE)
    suspended: list[ModelVersion] = []
    for model in active_models:
        if model.input_contract.contract_hash == old_contract_hash:
            try:
                suspended.append(
                    workflow.suspend(model.model_id, model.version, actor, reason)
                )
            except ModelWorkflowError:
                # 单个模型暂停失败不应阻断其他模型
                continue
    return suspended
