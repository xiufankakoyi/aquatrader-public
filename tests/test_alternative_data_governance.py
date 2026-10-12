"""P3.3 另类数据治理与接入测试。

覆盖：
- 契约校验（授权、信号分层、覆盖变化）
- 治理状态转移（active → suspended → deactivated）
- 无授权数据源拒绝
- MNPI 隔离
- 质量监控阈值与自动停用
- 标准化器与覆盖变化检测
- 数据质量 dashboard
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.research.alternative_data.contracts import (
    AlternativeDataError,
    AlternativeDataSignal,
    AlternativeDataSourceContract,
    AlternativeDataSourceStatus,
    AlternativeDataSourceType,
    AuthorizationScope,
    CoverageChange,
    CoverageChangeError,
    MnpiIsolationConfig,
    QualityMetric,
    QualityMonitoringConfig,
    RetentionPolicy,
    SignalTier,
    assert_authorized_source,
    assert_coverage_change_not_operational,
    assert_mnpi_isolated,
    assert_quality_within_thresholds,
    assert_signal_not_fact,
    is_status_transition_allowed,
)
from core.research.alternative_data.governance import (
    AlternativeDataGovernance,
    AlternativeDataRegistry,
    AlternativeDataRegistryError,
    QualityDashboard,
)
from core.research.alternative_data.normalizer import (
    AlternativeDataNormalizer,
    CoverageChangeDetector,
    EntityMapper,
    NormalizerError,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_authorization(
    status: str = "authorized",
    license_ref: str = "LIC-2026-001",
) -> AuthorizationScope:
    """构建授权范围。"""
    return AuthorizationScope(
        authorization_status=status,
        license_reference=license_ref,
        usage_scope=["research_only", "internal_model"],
        authorized_personnel=["analyst-A", "analyst-B"],
        expires_at="2027-12-31T23:59:59+00:00",
    )


def _make_retention() -> RetentionPolicy:
    """构建留存政策。"""
    return RetentionPolicy(
        fetch_method="api",
        retention_days=365,
        deletion_rule="到期后自动删除",
        pii_handling="脱敏后存储",
    )


def _make_quality_config(
    min_coverage: float = 0.5,
    max_freshness_hours: int = 72,
    max_error_rate: float = 0.1,
    min_completeness: float = 0.8,
    auto_suspend: bool = True,
) -> QualityMonitoringConfig:
    """构建质量监控配置。"""
    return QualityMonitoringConfig(
        min_coverage=min_coverage,
        max_freshness_hours=max_freshness_hours,
        max_error_rate=max_error_rate,
        min_completeness=min_completeness,
        auto_suspend_on_breach=auto_suspend,
    )


def _make_mnpi(
    may_contain: bool = False,
    isolation_process: str = "",
    storage_path: str = "",
    reviewers: list[str] | None = None,
) -> MnpiIsolationConfig:
    """构建 MNPI 隔离配置。"""
    return MnpiIsolationConfig(
        may_contain_mnpi=may_contain,
        isolation_process=isolation_process,
        isolated_storage_path=storage_path,
        authorized_reviewers=reviewers or [],
    )


def _make_source(
    source_id: str = "src-001",
    source_name: str = "某招标数据平台",
    source_type: AlternativeDataSourceType = AlternativeDataSourceType.BID,
    auth_status: str = "authorized",
    status: AlternativeDataSourceStatus = AlternativeDataSourceStatus.ACTIVE,
    mnpi: MnpiIsolationConfig | None = None,
    quality: QualityMonitoringConfig | None = None,
    entity_mapping: str = "direct",
) -> AlternativeDataSourceContract:
    """构建数据源契约。"""
    return AlternativeDataSourceContract(
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        authorization_scope=_make_authorization(status=auth_status),
        retention_policy=_make_retention(),
        update_frequency="daily",
        known_biases=["仅覆盖大中型企业", "北方地区覆盖偏多"],
        entity_mapping_method=entity_mapping,
        timestamp_semantics="event_time",
        quality_monitoring=quality or _make_quality_config(),
        mnpi_isolation=mnpi or _make_mnpi(),
        status=status,
        coverage_scope=["600519.SH", "000858.SZ"],
    )


def _make_signal(
    signal_id: str = "sig-001",
    source_id: str = "src-001",
    tier: SignalTier = SignalTier.OBSERVATION,
) -> AlternativeDataSignal:
    """构建另类数据信号。"""
    return AlternativeDataSignal(
        signal_id=signal_id,
        source_id=source_id,
        entity_id="600519.SH",
        metric_name="bid_count",
        metric_value=42.0,
        timestamp="2026-07-22T00:00:00+00:00",
        tier=tier,
    )


def _make_quality_metric(
    source_id: str = "src-001",
    coverage: float = 0.8,
    freshness_hours: int = 24,
    error_rate: float = 0.05,
    completeness: float = 0.9,
) -> QualityMetric:
    """构建质量指标快照。"""
    return QualityMetric(
        source_id=source_id,
        coverage=coverage,
        freshness_hours=freshness_hours,
        error_rate=error_rate,
        completeness=completeness,
    )


# ---------------------------------------------------------------------------
# 契约校验测试
# ---------------------------------------------------------------------------


class TestContracts:
    """数据源契约校验测试。"""

    def test_authorized_source_passes(self) -> None:
        """已授权数据源通过校验。"""
        source = _make_source(auth_status="authorized")
        assert_authorized_source(source)  # 不抛异常

    def test_unauthorized_source_rejected(self) -> None:
        """未授权数据源被拒绝。"""
        source = _make_source(auth_status="pending")
        with pytest.raises(AlternativeDataError, match="未授权"):
            assert_authorized_source(source)

    def test_unclear_source_rejected(self) -> None:
        """来源不清（source_id 为空）被拒绝。"""
        source = _make_source(source_id="", source_name="")
        with pytest.raises(AlternativeDataError, match="来源不清"):
            assert_authorized_source(source)

    def test_unknown_authorization_rejected(self) -> None:
        """授权状态为 unknown 被拒绝。"""
        source = _make_source(auth_status="unknown")
        with pytest.raises(AlternativeDataError, match="未授权"):
            assert_authorized_source(source)

    def test_signal_fact_rejected(self) -> None:
        """另类信号直接设为 FACT 被拒绝。"""
        signal = _make_signal(tier=SignalTier.FACT)
        with pytest.raises(AlternativeDataError, match="不得直接升级为 FACT"):
            assert_signal_not_fact(signal)

    def test_signal_observation_allowed(self) -> None:
        """信号设为 OBSERVATION 通过。"""
        signal = _make_signal(tier=SignalTier.OBSERVATION)
        assert_signal_not_fact(signal)  # 不抛异常

    def test_signal_hypothesis_allowed(self) -> None:
        """信号设为 HYPOTHESIS 通过。"""
        signal = _make_signal(tier=SignalTier.HYPOTHESIS)
        assert_signal_not_fact(signal)  # 不抛异常

    def test_coverage_change_with_operational_word_rejected(self) -> None:
        """覆盖变化 note 含经营变化措辞被拒绝。"""
        change = CoverageChange(
            source_id="src-001",
            change_type="added",
            affected_entities=["600519.SH"],
            detected_at="2026-07-22T00:00:00+00:00",
            note="新增覆盖导致收入增长",  # 包含「收入」「增长」
        )
        with pytest.raises(CoverageChangeError, match="经营变化"):
            assert_coverage_change_not_operational(change)

    def test_coverage_change_neutral_allowed(self) -> None:
        """中性覆盖变化通过校验。"""
        change = CoverageChange(
            source_id="src-001",
            change_type="added",
            affected_entities=["600519.SH"],
            detected_at="2026-07-22T00:00:00+00:00",
            note="数据源覆盖范围变化，不代表公司经营变化",
        )
        assert_coverage_change_not_operational(change)  # 不抛异常


# ---------------------------------------------------------------------------
# 状态机测试
# ---------------------------------------------------------------------------


class TestStateMachine:
    """数据源状态机测试。"""

    @pytest.mark.parametrize(
        "current,target,allowed",
        [
            (AlternativeDataSourceStatus.ACTIVE, AlternativeDataSourceStatus.SUSPENDED, True),
            (AlternativeDataSourceStatus.SUSPENDED, AlternativeDataSourceStatus.ACTIVE, True),
            (AlternativeDataSourceStatus.SUSPENDED, AlternativeDataSourceStatus.DEACTIVATED, True),
            (AlternativeDataSourceStatus.ACTIVE, AlternativeDataSourceStatus.DEACTIVATED, True),
            # 非法转移
            (AlternativeDataSourceStatus.DEACTIVATED, AlternativeDataSourceStatus.ACTIVE, False),
            (AlternativeDataSourceStatus.DEACTIVATED, AlternativeDataSourceStatus.SUSPENDED, False),
        ],
    )
    def test_state_machine_transitions(
        self,
        current: AlternativeDataSourceStatus,
        target: AlternativeDataSourceStatus,
        allowed: bool,
    ) -> None:
        """状态转移合法性。"""
        assert is_status_transition_allowed(current, target) is allowed

    def test_same_status_allowed(self) -> None:
        """相同状态视为允许（幂等）。"""
        assert is_status_transition_allowed(
            AlternativeDataSourceStatus.ACTIVE, AlternativeDataSourceStatus.ACTIVE
        )

    def test_deactivated_is_terminal(self) -> None:
        """deactivated 为终态，不可转移到任何状态。"""
        for target in AlternativeDataSourceStatus:
            if target == AlternativeDataSourceStatus.DEACTIVATED:
                continue
            assert not is_status_transition_allowed(
                AlternativeDataSourceStatus.DEACTIVATED, target
            )


# ---------------------------------------------------------------------------
# MNPI 隔离测试
# ---------------------------------------------------------------------------


class TestMnpiIsolation:
    """MNPI 隔离校验测试。"""

    def test_non_mnpi_source_passes(self) -> None:
        """不含 MNPI 的数据源通过。"""
        source = _make_source(mnpi=_make_mnpi(may_contain=False))
        assert_mnpi_isolated(source)  # 不抛异常

    def test_mnpi_without_isolation_process_rejected(self) -> None:
        """含 MNPI 但无隔离流程被拒绝。"""
        source = _make_source(
            mnpi=_make_mnpi(
                may_contain=True,
                isolation_process="",
                storage_path="/isolated/mnpi",
                reviewers=["compliance-A"],
            )
        )
        with pytest.raises(AlternativeDataError, match="隔离流程"):
            assert_mnpi_isolated(source)

    def test_mnpi_without_storage_path_rejected(self) -> None:
        """含 MNPI 但无隔离存储路径被拒绝。"""
        source = _make_source(
            mnpi=_make_mnpi(
                may_contain=True,
                isolation_process="双人复核+隔离存储",
                storage_path="",
                reviewers=["compliance-A"],
            )
        )
        with pytest.raises(AlternativeDataError, match="隔离存储路径"):
            assert_mnpi_isolated(source)

    def test_mnpi_without_reviewers_rejected(self) -> None:
        """含 MNPI 但无授权审核人员被拒绝。"""
        source = _make_source(
            mnpi=_make_mnpi(
                may_contain=True,
                isolation_process="双人复核+隔离存储",
                storage_path="/isolated/mnpi",
                reviewers=[],
            )
        )
        with pytest.raises(AlternativeDataError, match="授权审核人员"):
            assert_mnpi_isolated(source)

    def test_mnpi_full_isolation_passes(self) -> None:
        """含 MNPI 且隔离流程完整通过。"""
        source = _make_source(
            mnpi=_make_mnpi(
                may_contain=True,
                isolation_process="双人复核+隔离存储+访问审计",
                storage_path="/isolated/mnpi/src-001",
                reviewers=["compliance-A", "compliance-B"],
            )
        )
        assert_mnpi_isolated(source)  # 不抛异常


# ---------------------------------------------------------------------------
# 质量监控测试
# ---------------------------------------------------------------------------


class TestQualityMonitoring:
    """质量监控阈值测试。"""

    def test_quality_within_thresholds(self) -> None:
        """质量指标全部在阈值内通过。"""
        metric = _make_quality_metric(
            coverage=0.8, freshness_hours=24, error_rate=0.05, completeness=0.9
        )
        config = _make_quality_config()
        assert_quality_within_thresholds(metric, config)  # 不抛异常

    def test_quality_coverage_breach(self) -> None:
        """覆盖率低于阈值被拒绝。"""
        metric = _make_quality_metric(coverage=0.3)
        config = _make_quality_config(min_coverage=0.5)
        with pytest.raises(AlternativeDataError, match="coverage"):
            assert_quality_within_thresholds(metric, config)

    def test_quality_freshness_breach(self) -> None:
        """新鲜度超过阈值被拒绝。"""
        metric = _make_quality_metric(freshness_hours=100)
        config = _make_quality_config(max_freshness_hours=72)
        with pytest.raises(AlternativeDataError, match="freshness_hours"):
            assert_quality_within_thresholds(metric, config)

    def test_quality_error_rate_breach(self) -> None:
        """错误率超过阈值被拒绝。"""
        metric = _make_quality_metric(error_rate=0.2)
        config = _make_quality_config(max_error_rate=0.1)
        with pytest.raises(AlternativeDataError, match="error_rate"):
            assert_quality_within_thresholds(metric, config)

    def test_quality_completeness_breach(self) -> None:
        """完整性低于阈值被拒绝。"""
        metric = _make_quality_metric(completeness=0.6)
        config = _make_quality_config(min_completeness=0.8)
        with pytest.raises(AlternativeDataError, match="completeness"):
            assert_quality_within_thresholds(metric, config)


# ---------------------------------------------------------------------------
# 治理工作流测试
# ---------------------------------------------------------------------------


class TestGovernance:
    """治理工作流与生命周期测试。"""

    @pytest.fixture
    def governance(self, tmp_path: Path) -> AlternativeDataGovernance:
        """构建使用临时路径的治理工作流。"""
        return AlternativeDataGovernance(
            AlternativeDataRegistry(tmp_path / "alt_data_registry.json")
        )

    def test_register_authorized_source(self, governance: AlternativeDataGovernance) -> None:
        """登记已授权数据源成功。"""
        source = _make_source(source_id="src-A")
        governance.register_source(source)
        fetched = governance.registry.get("src-A")
        assert fetched is not None
        assert fetched.source_id == "src-A"

    def test_register_unauthorized_source_rejected(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """登记未授权数据源被拒绝。"""
        source = _make_source(source_id="src-B", auth_status="pending")
        with pytest.raises(AlternativeDataError, match="未授权"):
            governance.register_source(source)

    def test_register_duplicate_rejected(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """重复登记被拒绝。"""
        source = _make_source(source_id="src-C")
        governance.register_source(source)
        with pytest.raises(AlternativeDataRegistryError, match="已存在"):
            governance.register_source(source)

    def test_full_lifecycle_active_suspended_deactivated(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """完整生命周期：active → suspended → deactivated。"""
        source = _make_source(source_id="src-life")
        governance.register_source(source)

        # active → suspended
        suspended = governance.suspend("src-life", "admin-A", "数据质量下降")
        assert suspended.status is AlternativeDataSourceStatus.SUSPENDED
        assert suspended.suspension_reason == "数据质量下降"

        # suspended → deactivated
        deactivated = governance.deactivate("src-life", "admin-A", "授权到期")
        assert deactivated.status is AlternativeDataSourceStatus.DEACTIVATED
        assert deactivated.deactivation_reason == "授权到期"

    def test_reactivate_from_suspended(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """从 suspended 恢复到 active。"""
        source = _make_source(source_id="src-react")
        governance.register_source(source)
        governance.suspend("src-react", "admin-A", "临时暂停")
        reactivated = governance.reactivate("src-react", "admin-A")
        assert reactivated.status is AlternativeDataSourceStatus.ACTIVE

    def test_reactivate_from_deactivated_fails(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """deactivated 不可恢复。"""
        source = _make_source(source_id="src-dead")
        governance.register_source(source)
        governance.suspend("src-dead", "admin-A", "暂停")
        governance.deactivate("src-dead", "admin-A", "永久停用")
        with pytest.raises(AlternativeDataError, match="要求 suspended"):
            governance.reactivate("src-dead", "admin-A")

    def test_suspend_requires_reason(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """暂停必须记录原因。"""
        source = _make_source(source_id="src-reason")
        governance.register_source(source)
        with pytest.raises(AlternativeDataError, match="原因"):
            governance.suspend("src-reason", "admin-A", "")

    def test_deactivate_requires_reason(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """停用必须记录原因。"""
        source = _make_source(source_id="src-deact")
        governance.register_source(source)
        with pytest.raises(AlternativeDataError, match="原因"):
            governance.deactivate("src-deact", "admin-A", "")

    def test_suspend_non_active_fails(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """暂停非 active 状态失败。"""
        source = _make_source(source_id="src-nonact")
        governance.register_source(source)
        governance.suspend("src-nonact", "admin-A", "第一次暂停")
        with pytest.raises(AlternativeDataError, match="要求 active"):
            governance.suspend("src-nonact", "admin-A", "第二次暂停")

    def test_is_source_usable_for_formal_model(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """检查数据源是否可用于正式模型。"""
        source = _make_source(source_id="src-usable")
        governance.register_source(source)
        usable, reason = governance.is_source_usable_for_formal_model("src-usable")
        assert usable is True
        assert "可用" in reason

        # 暂停后不可用
        governance.suspend("src-usable", "admin-A", "测试")
        usable, reason = governance.is_source_usable_for_formal_model("src-usable")
        assert usable is False
        assert "active" in reason

    def test_auto_suspend_on_quality_breach(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """质量指标突破阈值时自动停用。"""
        source = _make_source(
            source_id="src-auto",
            quality=_make_quality_config(min_coverage=0.5, auto_suspend=True),
        )
        governance.register_source(source)
        # 记录一个突破阈值的指标
        bad_metric = _make_quality_metric(
            source_id="src-auto", coverage=0.2  # 低于 0.5
        )
        result = governance.record_quality(bad_metric)
        assert result is not None
        assert result.status is AlternativeDataSourceStatus.SUSPENDED
        # 验证数据源已被自动暂停
        usable, _ = governance.is_source_usable_for_formal_model("src-auto")
        assert usable is False

    def test_no_auto_suspend_when_disabled(
        self, governance: AlternativeDataGovernance
    ) -> None:
        """auto_suspend_on_breach=False 时不自动停用。"""
        source = _make_source(
            source_id="src-noauto",
            quality=_make_quality_config(min_coverage=0.5, auto_suspend=False),
        )
        governance.register_source(source)
        bad_metric = _make_quality_metric(source_id="src-noauto", coverage=0.2)
        result = governance.record_quality(bad_metric)
        assert result is None  # 未触发自动停用
        # 数据源仍 active
        usable, _ = governance.is_source_usable_for_formal_model("src-noauto")
        assert usable is True

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        """数据持久化：新实例能读回旧数据。"""
        path = tmp_path / "registry.json"
        gov1 = AlternativeDataGovernance(AlternativeDataRegistry(path))
        gov1.register_source(_make_source(source_id="src-persist"))
        gov1.suspend("src-persist", "admin-A", "持久化测试")

        gov2 = AlternativeDataGovernance(AlternativeDataRegistry(path))
        source = gov2.registry.get("src-persist")
        assert source is not None
        assert source.status is AlternativeDataSourceStatus.SUSPENDED
        assert source.suspension_reason == "持久化测试"


# ---------------------------------------------------------------------------
# 标准化器测试
# ---------------------------------------------------------------------------


    def test_malformed_source_record_raises_registry_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        path.write_text(
            '{"sources":{"src-invalid":["not-a-record"]}}',
            encoding="utf-8",
        )
        with pytest.raises(AlternativeDataRegistryError):
            AlternativeDataRegistry(path)

    def test_update_field_cannot_bypass_status_transition(
        self, governance: AlternativeDataGovernance
    ) -> None:
        governance.register_source(_make_source(source_id="src-status"))
        with pytest.raises(AlternativeDataRegistryError):
            governance.registry.update_field("src-status", "status", "deactivated")
        source = governance.registry.get("src-status")
        assert source is not None
        assert source.status is AlternativeDataSourceStatus.ACTIVE

    def test_register_rolls_back_memory_when_save_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        registry = AlternativeDataRegistry(tmp_path / "registry.json")

        def fail_save() -> None:
            raise AlternativeDataRegistryError("write failed")

        monkeypatch.setattr(registry, "_save", fail_save)
        with pytest.raises(AlternativeDataRegistryError, match="write failed"):
            registry.register(_make_source(source_id="src-fail"))
        assert registry.get("src-fail") is None


class TestNormalizer:
    """标准化器与覆盖变化检测测试。"""

    def test_normalize_signal_default_observation(self) -> None:
        """标准化信号默认 tier=OBSERVATION。"""
        source = _make_source(source_id="src-norm")
        normalizer = AlternativeDataNormalizer(source)
        signal = normalizer.normalize_signal(
            signal_id="sig-001",
            raw_entity_id="600519.SH",
            metric_name="bid_count",
            metric_value=42.0,
            timestamp="2026-07-22T00:00:00+00:00",
        )
        assert signal.tier is SignalTier.OBSERVATION
        assert signal.entity_id == "600519.SH"
        assert signal.source_id == "src-norm"

    def test_normalize_signal_fact_rejected(self) -> None:
        """标准化信号不允许直接设为 FACT。"""
        source = _make_source(source_id="src-fact")
        normalizer = AlternativeDataNormalizer(source)
        with pytest.raises(AlternativeDataError, match="不得直接升级为 FACT"):
            normalizer.normalize_signal(
                signal_id="sig-002",
                raw_entity_id="600519.SH",
                metric_name="bid_count",
                metric_value=42.0,
                timestamp="2026-07-22T00:00:00+00:00",
                tier=SignalTier.FACT,
            )

    def test_normalize_signal_suspended_source_fails(self) -> None:
        """suspended 状态的数据源不能标准化信号。"""
        source = _make_source(
            source_id="src-susp", status=AlternativeDataSourceStatus.SUSPENDED
        )
        with pytest.raises(NormalizerError, match="active"):
            AlternativeDataNormalizer(source)

    def test_normalize_signal_requires_authorization_and_normalizes_tier(self) -> None:
        with pytest.raises(NormalizerError, match="未授权"):
            AlternativeDataNormalizer(_make_source(source_id="src-pending", auth_status="pending"))
        normalizer = AlternativeDataNormalizer(_make_source(source_id="src-tier"))
        with pytest.raises(AlternativeDataError, match="不得直接升级为 FACT"):
            normalizer.normalize_signal(
                signal_id="sig-tier", raw_entity_id="600519.SH", metric_name="bid_count",
                metric_value=1, timestamp="2026-07-22T00:00:00+00:00", tier="fact",
            )

    def test_entity_mapper_direct(self) -> None:
        """direct 映射方法：原样返回。"""
        mapper = EntityMapper("direct")
        assert mapper.map_entity("600519.SH") == "600519.SH"

    def test_entity_mapper_alias_table(self) -> None:
        """alias_table 映射方法：通过别名表映射。"""
        mapper = EntityMapper("alias_table")
        mapper.set_alias_table({"茅台": "600519.SH"})
        assert mapper.map_entity("茅台") == "600519.SH"
        with pytest.raises(NormalizerError, match="未找到"):
            mapper.map_entity("未知公司")

    def test_coverage_change_detection(self) -> None:
        """覆盖变化检测：新增和移除实体。"""
        detector = CoverageChangeDetector("src-001")
        detector.set_baseline(["600519.SH", "000858.SZ"])
        changes = detector.update_coverage(["600519.SH", "000858.SZ", "300750.SZ"])
        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert "300750.SZ" in changes[0].affected_entities
        # 再次更新：移除一个
        changes = detector.update_coverage(["600519.SH"])
        assert len(changes) == 1
        assert changes[0].change_type == "removed"
        assert "000858.SZ" in changes[0].affected_entities

    def test_coverage_change_no_change(self) -> None:
        """覆盖范围未变化时不产生 CoverageChange。"""
        detector = CoverageChangeDetector("src-001")
        detector.set_baseline(["600519.SH"])
        changes = detector.update_coverage(["600519.SH"])
        assert len(changes) == 0


# ---------------------------------------------------------------------------
# Dashboard 测试
# ---------------------------------------------------------------------------


class TestDashboard:
    """数据质量 dashboard 测试。"""

    def test_dashboard_overview_no_quality_data(self, tmp_path: Path) -> None:
        """无质量数据时 dashboard 正常返回。"""
        registry = AlternativeDataRegistry(tmp_path / "reg.json")
        registry.register(_make_source(source_id="src-dash"))
        dashboard = QualityDashboard(registry)
        overview = dashboard.get_source_overview("src-dash")
        assert overview["source_id"] == "src-dash"
        assert overview["status"] == "active"
        assert overview["suspension_switch"] == "active"
        assert overview["latest_quality"] is None
        assert overview["quality_breaches"] == []

    def test_dashboard_overview_with_quality_data(self, tmp_path: Path) -> None:
        """有质量数据时 dashboard 标记阈值突破项。"""
        registry = AlternativeDataRegistry(tmp_path / "reg.json")
        source = _make_source(
            source_id="src-dash2",
            quality=_make_quality_config(min_coverage=0.5),
        )
        registry.register(source)
        registry.record_quality_metric(
            _make_quality_metric(source_id="src-dash2", coverage=0.2)
        )
        dashboard = QualityDashboard(registry)
        overview = dashboard.get_source_overview("src-dash2")
        assert overview["latest_quality"] is not None
        assert overview["latest_quality"]["coverage"] == 0.2
        assert len(overview["quality_breaches"]) == 1
        assert "coverage" in overview["quality_breaches"][0]

    def test_dashboard_all_overviews(self, tmp_path: Path) -> None:
        """获取所有数据源 dashboard 概览。"""
        registry = AlternativeDataRegistry(tmp_path / "reg.json")
        registry.register(_make_source(source_id="src-a"))
        registry.register(_make_source(source_id="src-b"))
        dashboard = QualityDashboard(registry)
        overviews = dashboard.get_all_overviews()
        assert len(overviews) == 2
        ids = {o["source_id"] for o in overviews}
        assert ids == {"src-a", "src-b"}


# ---------------------------------------------------------------------------
# 指纹与契约完整性测试
# ---------------------------------------------------------------------------


class TestFingerprint:
    """数据源指纹测试。"""

    def test_fingerprint_stable(self) -> None:
        """相同契约 → 相同指纹。"""
        s1 = _make_source(source_id="src-fp")
        s2 = _make_source(source_id="src-fp")
        assert s1.compute_fingerprint() == s2.compute_fingerprint()

    def test_fingerprint_changes_on_field_change(self) -> None:
        """字段变化 → 指纹变化。"""
        s1 = _make_source(source_id="src-fp")
        s2 = _make_source(source_id="src-fp", source_name="另一个名字")
        assert s1.compute_fingerprint() != s2.compute_fingerprint()
