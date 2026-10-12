"""另类数据契约数值边界回归测试。"""

from __future__ import annotations

import math

import pytest

from core.research.alternative_data.contracts import (
    AlternativeDataError,
    QualityMetric,
    QualityMonitoringConfig,
    assert_quality_within_thresholds,
)


@pytest.mark.parametrize(
    "metric",
    [
        QualityMetric("src", math.nan, 1, 0.01, 0.9),
        QualityMetric("src", 0.8, 1, math.inf, 0.9),
        QualityMetric("src", 1.2, 1, 0.01, 0.9),
        QualityMetric("src", "bad", 1, 0.01, 0.9),  # type: ignore[arg-type]
    ],
)
def test_invalid_quality_metric_is_rejected(metric: QualityMetric) -> None:
    with pytest.raises(AlternativeDataError):
        assert_quality_within_thresholds(metric, QualityMonitoringConfig())


def test_invalid_quality_threshold_is_rejected() -> None:
    metric = QualityMetric("src", 0.8, 1, 0.01, 0.9)
    config = QualityMonitoringConfig(min_coverage=math.nan)

    with pytest.raises(AlternativeDataError):
        assert_quality_within_thresholds(metric, config)
