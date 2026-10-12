"""另类数据标准化器边界回归测试。"""

from __future__ import annotations

import pytest

from core.research.alternative_data.contracts import (
    AlternativeDataSourceStatus,
    AlternativeDataSourceType,
    AuthorizationScope,
    MnpiIsolationConfig,
    QualityMonitoringConfig,
    RetentionPolicy,
    SignalTier,
    AlternativeDataSourceContract,
)
from core.research.alternative_data.normalizer import (
    AlternativeDataNormalizer,
    CoverageChangeDetector,
    EntityMapper,
    NormalizerError,
)


def _source() -> AlternativeDataSourceContract:
    return AlternativeDataSourceContract(
        source_id="src",
        source_name="source",
        source_type=AlternativeDataSourceType.OTHER,
        authorization_scope=AuthorizationScope("authorized", "lic", [], []),
        retention_policy=RetentionPolicy("api", 30, "delete", "mask"),
        update_frequency="daily",
        known_biases=[],
        entity_mapping_method="alias_table",
        timestamp_semantics="event_time",
        quality_monitoring=QualityMonitoringConfig(),
        mnpi_isolation=MnpiIsolationConfig(False, "", "", []),
        status=AlternativeDataSourceStatus.ACTIVE,
    )


def test_alias_mapping_strips_lookup_input() -> None:
    mapper = EntityMapper("alias_table")
    mapper.set_alias_table({"茅台": "600519.SH"})

    assert mapper.map_entity(" 茅台 ") == "600519.SH"


def test_coverage_detector_normalizes_current_entities() -> None:
    detector = CoverageChangeDetector("src")
    detector.set_baseline(["600519.SH"])

    assert detector.update_coverage([" 600519.SH "]) == []


def test_normalize_signal_strips_identity_fields_and_requires_timestamp() -> None:
    normalizer = AlternativeDataNormalizer(_source())
    normalizer.set_alias_table({"茅台": "600519.SH"})

    signal = normalizer.normalize_signal(
        " sig ", "茅台", " bid_count ", 1, " 2026-07-22 ", SignalTier.OBSERVATION
    )
    assert signal.signal_id == "sig"
    assert signal.metric_name == "bid_count"
    assert signal.timestamp == "2026-07-22"

    with pytest.raises(NormalizerError):
        normalizer.normalize_signal("sig", "茅台", "bid_count", 1, " ")
