"""P3.3 另类数据治理与接入包。

按 P3 计划 Task P3.3：
- 每个数据源必须登记 10 个字段
- 状态机：active → suspended → deactivated
- 无授权或来源不清的数据不得进入正式模型
- 另类信号必须先成为观察或假设，不能直接升级事实
- 覆盖变化不会被误判为公司经营变化
- 每个源有数据质量 dashboard 和停用开关
"""
from __future__ import annotations

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

__all__ = [
    # 契约
    "AlternativeDataError",
    "AlternativeDataSignal",
    "AlternativeDataSourceContract",
    "AlternativeDataSourceStatus",
    "AlternativeDataSourceType",
    "AuthorizationScope",
    "CoverageChange",
    "CoverageChangeError",
    "MnpiIsolationConfig",
    "QualityMetric",
    "QualityMonitoringConfig",
    "RetentionPolicy",
    "SignalTier",
    # 校验函数
    "assert_authorized_source",
    "assert_coverage_change_not_operational",
    "assert_mnpi_isolated",
    "assert_quality_within_thresholds",
    "assert_signal_not_fact",
    "is_status_transition_allowed",
    # 治理
    "AlternativeDataGovernance",
    "AlternativeDataRegistry",
    "AlternativeDataRegistryError",
    "QualityDashboard",
    # 标准化器
    "AlternativeDataNormalizer",
    "CoverageChangeDetector",
    "EntityMapper",
    "NormalizerError",
]
