"""Market expectations, expectation gaps, implied expectation reverse-engineering,
and falsifiable thesis lifecycle with full state machine.

P2.5 扩展：
- RevisionEngine：1/3/6 个月修正序列 + 来源数 + 分歧度 + 公司指引 vs 共识差 + 来源集中风险
- ImpliedExpectationEngine：当前市值隐含的增长率/利润率/持续年限/资本回报
"""
from .contracts import *  # noqa: F401,F403
from .engine import (
    AquatradeForecastBuilder,
    ConsensusSummaryEngine,
    ExpectationGapEngine,
    ImpliedExpectationEngine,
    ThesisLifecycle,
    latest_visible_consensus,
    visible_as_of,
)
from .implied_expectation_engine import (
    IMPLIED_EXPECTATION_SOURCE,
    IMPLIED_METRIC_DURATION,
    IMPLIED_METRIC_GROWTH,
    IMPLIED_METRIC_KINDS,
    IMPLIED_METRIC_MARGIN,
    IMPLIED_METRIC_RETURN_ON_CAPITAL,
    ImpliedExpectationEngine as P25ImpliedExpectationEngine,
    ImpliedExpectationSummary,
    ImpliedMetric,
    build_implied_expectation_engine,
)
from .repository import ExpectationRepository
from .revision_engine import (
    DEFAULT_CONCENTRATION_HHI_THRESHOLD,
    DEFAULT_MIN_SOURCES,
    REVISION_WINDOW_1M,
    REVISION_WINDOW_3M,
    REVISION_WINDOW_6M,
    REVISION_WINDOWS,
    STATUS_DEGRADED,
    STATUS_INSUFFICIENT_SOURCES,
    STATUS_NO_DATA,
    STATUS_NOT_COMPUTABLE,
    STATUS_OK,
    ConsensusDiversity,
    GuidanceConsensusGap,
    RevisionEngine,
    RevisionPoint,
    RevisionSequence,
    SourceConcentrationRisk,
    build_revision_engine,
)

__all__ = [
    # P1.x 原有
    "AquatradeForecastBuilder",
    "ConsensusSummaryEngine",
    "ExpectationGapEngine",
    "ImpliedExpectationEngine",
    "ThesisLifecycle",
    "latest_visible_consensus",
    "visible_as_of",
    "ExpectationRepository",
    # P2.5 修正引擎
    "RevisionEngine",
    "RevisionPoint",
    "RevisionSequence",
    "ConsensusDiversity",
    "GuidanceConsensusGap",
    "SourceConcentrationRisk",
    "REVISION_WINDOW_1M",
    "REVISION_WINDOW_3M",
    "REVISION_WINDOW_6M",
    "REVISION_WINDOWS",
    "STATUS_OK",
    "STATUS_INSUFFICIENT_SOURCES",
    "STATUS_NOT_COMPUTABLE",
    "STATUS_NO_DATA",
    "STATUS_DEGRADED",
    "DEFAULT_MIN_SOURCES",
    "DEFAULT_CONCENTRATION_HHI_THRESHOLD",
    "build_revision_engine",
    # P2.5 市场隐含预期
    "P25ImpliedExpectationEngine",
    "ImpliedMetric",
    "ImpliedExpectationSummary",
    "IMPLIED_EXPECTATION_SOURCE",
    "IMPLIED_METRIC_KINDS",
    "IMPLIED_METRIC_GROWTH",
    "IMPLIED_METRIC_MARGIN",
    "IMPLIED_METRIC_DURATION",
    "IMPLIED_METRIC_RETURN_ON_CAPITAL",
    "build_implied_expectation_engine",
]
