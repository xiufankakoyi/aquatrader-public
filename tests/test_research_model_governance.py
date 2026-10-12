"""P3.1 研究模型治理测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.research.model_governance.contracts import (
    InputContract,
    ModelGovernanceError,
    ModelStatus,
    ModelVersion,
    OutputContract,
    assert_active_has_rollback,
    assert_config_change_creates_new_version,
    assert_reviewer_independent,
    is_transition_allowed,
)
from core.research.model_governance.registry import (
    ModelRegistry,
    ModelRegistryError,
)
from core.research.model_governance.workflow import (
    ModelWorkflow,
    ModelWorkflowError,
    suspend_models_on_input_contract_change,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_input_contract(fields: dict | None = None) -> InputContract:
    contract = InputContract(
        fields=fields or {"revenue": "float", "net_profit": "float"},
        data_source_refs=["tushare", "akshare"],
    )
    return InputContract(
        fields=contract.fields,
        data_source_refs=contract.data_source_refs,
        contract_hash=contract.compute_hash(),
    )


def _make_output_contract() -> OutputContract:
    contract = OutputContract(
        fields={"forecast_revenue": "float", "confidence": "float"},
        validation_rules={"confidence_range": "[0,1]"},
    )
    return OutputContract(
        fields=contract.fields,
        validation_rules=contract.validation_rules,
        contract_hash=contract.compute_hash(),
    )


def _make_model(
    model_id: str = "model-001",
    version: str = "1.0",
    owner: str = "analyst-A",
    status: ModelStatus = ModelStatus.DRAFT,
    config_fingerprint: str = "config-hash-001",
    rollback_version: str = "",
) -> ModelVersion:
    return ModelVersion(
        model_id=model_id,
        model_family="revenue_forecast",
        domain="consumer",
        version=version,
        owner=owner,
        reviewer="",
        training_or_calibration_window="2020-01-01:2024-12-31",
        input_contract=_make_input_contract(),
        output_contract=_make_output_contract(),
        applicable_universe=["600519.SH", "000858.SZ"],
        limitations=["仅适用于消费行业"],
        benchmark={"mape": 0.15},
        validation_report={},
        status=status,
        effective_from="2024-01-01",
        effective_to="",
        rollback_version=rollback_version,
        code_fingerprint="code-hash-001",
        config_fingerprint=config_fingerprint,
        data_fingerprint="data-hash-001",
    )


# ---------------------------------------------------------------------------
# 契约测试
# ---------------------------------------------------------------------------


class TestContracts:
    """契约、状态机、硬规则测试。"""

    def test_input_contract_hash_stable(self) -> None:
        """相同输入 → 相同哈希。"""
        c1 = _make_input_contract()
        c2 = _make_input_contract()
        assert c1.compute_hash() == c2.compute_hash()

    def test_input_contract_hash_changes_on_field_change(self) -> None:
        """字段变化 → 哈希变化。"""
        c1 = _make_input_contract()
        c2 = _make_input_contract({"revenue": "float", "eps": "float"})
        assert c1.compute_hash() != c2.compute_hash()

    @pytest.mark.parametrize(
        "current,target,allowed",
        [
            (ModelStatus.DRAFT, ModelStatus.UNIT_VALIDATED, True),
            (ModelStatus.UNIT_VALIDATED, ModelStatus.SHADOW, True),
            (ModelStatus.SHADOW, ModelStatus.INDEPENDENT_VALIDATED, True),
            (ModelStatus.INDEPENDENT_VALIDATED, ModelStatus.APPROVED, True),
            (ModelStatus.APPROVED, ModelStatus.ACTIVE, True),
            (ModelStatus.ACTIVE, ModelStatus.SUSPENDED, True),
            (ModelStatus.SUSPENDED, ModelStatus.ACTIVE, True),
            (ModelStatus.SUSPENDED, ModelStatus.RETIRED, True),
            # 非法转移
            (ModelStatus.DRAFT, ModelStatus.ACTIVE, False),
            (ModelStatus.DRAFT, ModelStatus.SHADOW, False),
            (ModelStatus.UNIT_VALIDATED, ModelStatus.ACTIVE, False),
            (ModelStatus.RETIRED, ModelStatus.ACTIVE, False),
            (ModelStatus.RETIRED, ModelStatus.DRAFT, False),
        ],
    )
    def test_state_machine_transitions(
        self, current: ModelStatus, target: ModelStatus, allowed: bool
    ) -> None:
        assert is_transition_allowed(current, target) is allowed

    def test_same_status_transition_allowed(self) -> None:
        """相同状态视为允许（幂等）。"""
        assert is_transition_allowed(ModelStatus.ACTIVE, ModelStatus.ACTIVE)

    def test_reviewer_independence_enforced(self) -> None:
        """作者与验证人不能为同一人。"""
        assert_reviewer_independent("analyst-A", "analyst-B")  # 通过
        with pytest.raises(ModelGovernanceError, match="不能为同一人"):
            assert_reviewer_independent("analyst-A", "analyst-A")

    def test_active_must_have_rollback(self) -> None:
        """active 模型必须有回滚版本。"""
        model_with = _make_model(status=ModelStatus.ACTIVE, rollback_version="0.9")
        assert_active_has_rollback(model_with)  # 通过
        model_without = _make_model(status=ModelStatus.ACTIVE, rollback_version="")
        with pytest.raises(ModelGovernanceError, match="rollback_version"):
            assert_active_has_rollback(model_without)
        model_with_whitespace = _make_model(
            status=ModelStatus.ACTIVE, rollback_version="   "
        )
        with pytest.raises(ModelGovernanceError, match="rollback_version"):
            assert_active_has_rollback(model_with_whitespace)

    def test_config_change_requires_new_version(self) -> None:
        """配置变化必须生成新版本。"""
        # 指纹变化 + 版本变化 → 通过
        assert_config_change_creates_new_version(
            "old-hash", "new-hash", "1.0", "1.1"
        )
        # 指纹相同 + 版本相同 → 通过（无变化）
        assert_config_change_creates_new_version(
            "same-hash", "same-hash", "1.0", "1.0"
        )
        # 指纹变化 + 版本相同 → 违规
        with pytest.raises(ModelGovernanceError, match="version 未变"):
            assert_config_change_creates_new_version(
                "old-hash", "new-hash", "1.0", "1.0"
            )


# ---------------------------------------------------------------------------
# 注册表测试
# ---------------------------------------------------------------------------


class TestRegistry:
    """模型注册表测试。"""

    def test_register_and_list(self, tmp_path: Path) -> None:
        """登记 + 列出版本。"""
        registry = ModelRegistry(tmp_path / "registry.json")
        registry.register(_make_model(version="1.0"))
        registry.register(_make_model(version="1.1", config_fingerprint="hash-1.1"))
        versions = registry.list_versions("model-001")
        assert len(versions) == 2
        assert {v.version for v in versions} == {"1.0", "1.1"}

    def test_duplicate_version_rejected(self, tmp_path: Path) -> None:
        """重复版本号 → 错误。"""
        registry = ModelRegistry(tmp_path / "registry.json")
        registry.register(_make_model(version="1.0"))
        with pytest.raises(ModelRegistryError, match="已存在"):
            registry.register(_make_model(version="1.0"))

    def test_get_version(self, tmp_path: Path) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")
        model = _make_model(version="1.0")
        registry.register(model)
        fetched = registry.get_version("model-001", "1.0")
        assert fetched is not None
        assert fetched.model_id == "model-001"
        assert registry.get_version("model-001", "2.0") is None

    def test_update_fields_persists_without_exposing_storage(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        registry = ModelRegistry(path)
        registry.register(_make_model(version="1.0"))

        updated = registry.update_fields(
            "model-001", "1.0", {"reviewer": "reviewer-B"}
        )

        assert updated.reviewer == "reviewer-B"
        restored = ModelRegistry(path).get_version("model-001", "1.0")
        assert restored is not None
        assert restored.reviewer == "reviewer-B"

    def test_list_by_status(self, tmp_path: Path) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")
        registry.register(_make_model(model_id="m1", version="1.0", status=ModelStatus.DRAFT))
        registry.register(
            _make_model(model_id="m2", version="1.0", status=ModelStatus.ACTIVE, rollback_version="0.9")
        )
        drafts = registry.list_by_status(ModelStatus.DRAFT)
        actives = registry.list_by_status(ModelStatus.ACTIVE)
        assert len(drafts) == 1
        assert len(actives) == 1

    def test_list_by_domain(self, tmp_path: Path) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")
        model = _make_model()
        registry.register(model)
        results = registry.list_by_domain("consumer")
        assert len(results) == 1
        assert results[0].model_id == "model-001"

    def test_update_status_invalid_transition(self, tmp_path: Path) -> None:
        """非法状态转移 → 错误。"""
        registry = ModelRegistry(tmp_path / "registry.json")
        registry.register(_make_model(status=ModelStatus.DRAFT))
        with pytest.raises(ModelRegistryError, match="非法状态转移"):
            registry.update_status("model-001", "1.0", ModelStatus.ACTIVE, "user")

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        """数据持久化：新实例能读回旧数据。"""
        path = tmp_path / "registry.json"
        registry1 = ModelRegistry(path)
        registry1.register(_make_model(version="1.0"))
        registry2 = ModelRegistry(path)
        versions = registry2.list_versions("model-001")
        assert len(versions) == 1
        assert versions[0].version == "1.0"

    def test_malformed_registry_raises(self, tmp_path: Path) -> None:
        """畸形注册表 → 错误。"""
        path = tmp_path / "registry.json"
        path.write_text("not a json", encoding="utf-8")
        with pytest.raises(ModelRegistryError, match="读取失败"):
            ModelRegistry(path)

    def test_malformed_model_record_raises_registry_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        path.write_text(
            '{"models":{"model-001":["not-a-record"]}}',
            encoding="utf-8",
        )
        with pytest.raises(ModelRegistryError):
            ModelRegistry(path)

    def test_non_list_model_versions_raise_registry_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        path.write_text(
            '{"models":{"model-001":{"version":"1.0"}}}',
            encoding="utf-8",
        )
        with pytest.raises(ModelRegistryError):
            ModelRegistry(path)

    def test_malformed_contract_fields_raise_registry_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        path.write_text(
            '{"models":{"model-001":[{"input_contract":{"fields":"bad"}}]}}',
            encoding="utf-8",
        )
        with pytest.raises(ModelRegistryError):
            ModelRegistry(path)

    def test_active_model_requires_rollback_version(
        self, tmp_path: Path
    ) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")
        with pytest.raises(ModelRegistryError):
            registry.register(_make_model(status=ModelStatus.ACTIVE))

    def test_only_one_active_version_is_allowed(
        self, tmp_path: Path
    ) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")
        registry.register(
            _make_model(
                version="1.0",
                status=ModelStatus.ACTIVE,
                rollback_version="0.9",
            )
        )
        with pytest.raises(ModelRegistryError):
            registry.register(
                _make_model(
                    version="1.1",
                    status=ModelStatus.ACTIVE,
                    rollback_version="1.0",
                )
            )

    def test_register_rolls_back_memory_when_save_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ModelRegistry(tmp_path / "registry.json")

        def fail_save() -> None:
            raise ModelRegistryError("save failed")

        monkeypatch.setattr(registry, "_save", fail_save)
        with pytest.raises(ModelRegistryError, match="save failed"):
            registry.register(_make_model())

        assert registry.get_version("model-001", "1.0") is None

    def test_bootstrap_registers_shadow_learning_model(
        self, tmp_path: Path
    ) -> None:
        """bootstrap 首次调用登记 shadow 学习模型族。"""
        from core.research.model_governance.bootstrap import (
            ensure_initial_models_registered,
        )

        registry = ModelRegistry(tmp_path / "registry.json")
        registered = ensure_initial_models_registered(registry)
        assert "research_learning_shadow_observations" in registered

        versions = registry.list_versions("research_learning_shadow_observations")
        assert len(versions) == 1
        model = versions[0]
        # 初始登记必须处于 SHADOW，不能直接 ACTIVE（无 rollback_version）
        assert model.status is ModelStatus.SHADOW
        assert model.model_family == "research_learning_shadow"
        assert model.domain == "forward_learning"
        assert model.version == "template_v1"
        # P3.1 硬规则：模型作者不能完成独立验证签署 → reviewer 必须为空
        assert model.reviewer == ""
        # shadow 阶段无需 rollback_version
        assert model.rollback_version == ""
        # 输入/输出契约哈希已计算
        assert model.input_contract.contract_hash
        assert model.output_contract.contract_hash
        # code/config fingerprint 已写入
        assert model.code_fingerprint
        assert model.config_fingerprint
        # limitations 包含 shadow 边界说明
        limitations_text = " ".join(model.limitations)
        assert "shadow" in limitations_text.lower()
        assert "sha256" not in limitations_text.lower()
        # 不输出交易动作措辞
        for banned in ("买入", "卖出", "仓位", "止盈", "止损"):
            assert banned not in limitations_text

    def test_bootstrap_idempotent(self, tmp_path: Path) -> None:
        """bootstrap 重复调用安全，不会重复登记。"""
        from core.research.model_governance.bootstrap import (
            ensure_initial_models_registered,
        )

        registry = ModelRegistry(tmp_path / "registry.json")
        first = ensure_initial_models_registered(registry)
        second = ensure_initial_models_registered(registry)
        assert len(first) == 1
        assert second == []
        versions = registry.list_versions("research_learning_shadow_observations")
        assert len(versions) == 1

    def test_bootstrap_does_not_swallow_registry_write_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.research.model_governance.bootstrap import (
            ensure_initial_models_registered,
        )

        registry = ModelRegistry(tmp_path / "registry.json")

        def fail_register(model: ModelVersion) -> None:
            raise ModelRegistryError("write failed")

        monkeypatch.setattr(registry, "register", fail_register)
        with pytest.raises(ModelRegistryError, match="write failed"):
            ensure_initial_models_registered(registry)


# ---------------------------------------------------------------------------
# 工作流测试
# ---------------------------------------------------------------------------


class TestWorkflow:
    """生命周期工作流测试。"""

    @pytest.fixture
    def workflow(self, tmp_path: Path) -> ModelWorkflow:
        return ModelWorkflow(ModelRegistry(tmp_path / "registry.json"))

    def test_full_lifecycle(self, workflow: ModelWorkflow) -> None:
        """完整生命周期：draft → unit_validated → shadow → independent_validated
        → approved → active → suspended → retired。"""
        model = _make_model(owner="analyst-A")
        workflow.submit_draft(model)

        # 单元验证（作者本人提交）
        workflow.unit_validate(
            "model-001", "1.0", "analyst-A",
            {"tests_passed": 50, "coverage": 0.92}
        )

        # shadow
        workflow.start_shadow("model-001", "1.0", "analyst-A")

        # 独立验证（不同人）
        workflow.independent_validate(
            "model-001", "1.0", "reviewer-B",
            {"adversarial_review": "passed", "errors": []}
        )

        # 审批
        workflow.approve("model-001", "1.0", "committee")

        # 先登记回滚版本（必须 active 或 suspended 才能作为 rollback）
        rollback_model = _make_model(
            version="0.9", owner="analyst-A",
            status=ModelStatus.SUSPENDED,
            config_fingerprint="hash-0.9",
        )
        workflow.registry.register(rollback_model)

        # 激活
        workflow.activate("model-001", "1.0", "committee", rollback_version="0.9")

        # 暂停
        workflow.suspend("model-001", "1.0", "committee", "数据异常")

        # 退役
        workflow.retire("model-001", "1.0", "committee", "生命周期结束")
        final = workflow.registry.get_version("model-001", "1.0")
        assert final is not None
        assert final.status is ModelStatus.RETIRED

    def test_independent_validate_rejects_same_person(
        self, workflow: ModelWorkflow
    ) -> None:
        """作者不能完成独立验证。"""
        model = _make_model(owner="analyst-A")
        workflow.submit_draft(model)
        workflow.unit_validate(
            "model-001", "1.0", "analyst-A", {"tests_passed": 50}
        )
        workflow.start_shadow("model-001", "1.0", "analyst-A")
        with pytest.raises(ModelGovernanceError, match="不能为同一人"):
            workflow.independent_validate(
                "model-001", "1.0", "analyst-A", {"review": "passed"}
            )

    def test_activate_requires_rollback(self, workflow: ModelWorkflow) -> None:
        """激活必须指定 rollback_version。"""
        model = _make_model(owner="analyst-A")
        workflow.submit_draft(model)
        workflow.unit_validate("model-001", "1.0", "analyst-A", {"tests": 1})
        workflow.start_shadow("model-001", "1.0", "analyst-A")
        workflow.independent_validate(
            "model-001", "1.0", "reviewer-B", {"review": "passed"}
        )
        workflow.approve("model-001", "1.0", "committee")
        with pytest.raises(ModelWorkflowError, match="rollback_version"):
            workflow.activate("model-001", "1.0", "committee", "")

    def test_rollback_flow(self, workflow: ModelWorkflow) -> None:
        """回滚流程：active → suspended, rollback_version → active。"""
        # 准备 v0.9（suspended 作为 rollback 目标）
        v09 = _make_model(
            version="0.9", owner="analyst-A",
            status=ModelStatus.SUSPENDED,
            config_fingerprint="hash-0.9",
        )
        workflow.registry.register(v09)
        # 准备 v1.0，直接登记为 approved（绕过 submit_draft，测试回滚路径）
        v10 = _make_model(
            version="1.0", owner="analyst-A",
            status=ModelStatus.APPROVED,
            config_fingerprint="hash-1.0",
        )
        workflow.registry.register(v10)
        workflow.activate("model-001", "1.0", "committee", rollback_version="0.9")

        # 回滚
        rolled_back = workflow.rollback(
            "model-001", "1.0", "committee", "v1.0 误差超限"
        )
        assert rolled_back.version == "0.9"
        assert rolled_back.status is ModelStatus.ACTIVE
        v10_after = workflow.registry.get_version("model-001", "1.0")
        assert v10_after is not None
        assert v10_after.status is ModelStatus.SUSPENDED

    def test_register_new_version_config_change(
        self, workflow: ModelWorkflow
    ) -> None:
        """配置变化必须生成新版本。"""
        workflow.submit_draft(_make_model(version="1.0", config_fingerprint="hash-1.0"))
        # 配置变化但版本号未变 → 违规
        with pytest.raises(ModelGovernanceError, match="version 未变"):
            workflow.register_new_version(
                _make_model(version="1.0", config_fingerprint="hash-2.0"),
                "analyst-A",
            )
        # 配置变化 + 版本号变化 → 通过
        workflow.register_new_version(
            _make_model(version="1.1", config_fingerprint="hash-1.1"),
            "analyst-A",
        )

    def test_suspend_requires_reason(self, workflow: ModelWorkflow) -> None:
        """暂停必须记录原因。"""
        workflow.registry.register(
            _make_model(version="1.0", status=ModelStatus.ACTIVE, rollback_version="0.9")
        )
        # 先登记 rollback_version
        workflow.registry.register(
            _make_model(version="0.9", status=ModelStatus.SUSPENDED, config_fingerprint="hash-0.9")
        )
        with pytest.raises(ModelWorkflowError, match="原因"):
            workflow.suspend("model-001", "1.0", "user", "")

    def test_suspend_models_on_input_contract_change(
        self, workflow: ModelWorkflow
    ) -> None:
        """输入契约变化自动暂停相关 active 模型。"""
        contract = _make_input_contract()
        model = ModelVersion(
            model_id="m-active",
            model_family="test",
            domain="consumer",
            version="1.0",
            owner="analyst-A",
            reviewer="",
            training_or_calibration_window="2020:2024",
            input_contract=contract,
            output_contract=_make_output_contract(),
            applicable_universe=["600519.SH"],
            limitations=[],
            benchmark={},
            validation_report={},
            status=ModelStatus.ACTIVE,
            effective_from="2024-01-01",
            effective_to="",
            rollback_version="0.9",
            code_fingerprint="c",
            config_fingerprint="cf",
            data_fingerprint="d",
        )
        # 先登记 rollback 版本
        rollback = ModelVersion(
            model_id="m-active",
            model_family="test",
            domain="consumer",
            version="0.9",
            owner="analyst-A",
            reviewer="",
            training_or_calibration_window="2020:2024",
            input_contract=contract,
            output_contract=_make_output_contract(),
            applicable_universe=["600519.SH"],
            limitations=[],
            benchmark={},
            validation_report={},
            status=ModelStatus.SUSPENDED,
            effective_from="2024-01-01",
            effective_to="",
            rollback_version="",
            code_fingerprint="c",
            config_fingerprint="cf-0.9",
            data_fingerprint="d",
        )
        workflow.registry.register(rollback)
        workflow.registry.register(model)

        # 契约不变 → 不暂停
        suspended = suspend_models_on_input_contract_change(
            workflow, contract.contract_hash, contract.contract_hash, "system"
        )
        assert suspended == []

        # 契约变化 → 暂停相关模型
        suspended = suspend_models_on_input_contract_change(
            workflow, contract.contract_hash, "new-hash", "system"
        )
        assert len(suspended) == 1
        assert suspended[0].status is ModelStatus.SUSPENDED
