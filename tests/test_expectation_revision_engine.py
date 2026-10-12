"""P2.5 专项测试：一致预期修正与市场隐含预期。

覆盖 4 个验收点：
1. 修正序列按发布日期重建（RevisionSequence.revision_points 按 published_at 升序）
2. Reverse DCF 与一致预期使用独立标签（expectation_source="implied"）
3. 单一机构来源不能冒充市场共识（source_count < min_sources 时 status="insufficient_sources"）
4. 无可靠共识时输出不可计算（status="not_computable" / "no_data"）

附加覆盖：
- RevisionEngine.build_all_windows：1m / 3m / 6m 窗口同时构建
- RevisionEngine.compute_consensus_diversity：来源数与分歧度统计
- RevisionEngine.compute_guidance_consensus_gap：公司指引 vs 共识差
- RevisionEngine.compute_source_concentration_risk：HHI 集中度
- ImpliedExpectationEngine 四种反推：growth / margin / duration / ROIC
- ExpectationRepository 持久化与 PIT 查询
- valuation/engine.ReverseDcfEngine 输出含 expectation_source="implied" 标签
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core.research.earnings.interval import Interval
from core.research.expectations.contracts import (
    CompanyGuidance,
    ConsensusEstimate,
    ExpectationSource,
    ExpectationValue,
)
from core.research.expectations.implied_expectation_engine import (
    IMPLIED_EXPECTATION_SOURCE,
    IMPLIED_METRIC_DURATION,
    IMPLIED_METRIC_GROWTH,
    IMPLIED_METRIC_KINDS,
    IMPLIED_METRIC_MARGIN,
    IMPLIED_METRIC_RETURN_ON_CAPITAL,
    ImpliedExpectationEngine as P25ImpliedExpectationEngine,
    ImpliedExpectationSummary,
    ImpliedMetric,
)
from core.research.expectations.repository import ExpectationRepository
from core.research.expectations.revision_engine import (
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
from core.research.valuation.contracts import (
    Computability,
    ValuationAssumption,
    ValuationInput,
    ValuationMethod,
)
from core.research.valuation.engine import ReverseDcfEngine


# ---------------------------------------------------------------------------
# 工具与 fixtures
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """转 ISO 字符串（带时区）。"""
    return dt.isoformat()


@pytest.fixture
def as_of_dt() -> datetime:
    """PIT 时点：2027-01-15 UTC。"""
    return datetime(2027, 1, 15, tzinfo=timezone.utc)


@pytest.fixture
def as_of_str(as_of_dt: datetime) -> str:
    return _iso(as_of_dt)


def _make_value(
    *,
    metric: str = "net_profit",
    period: str = "2027",
    published_at: str,
    retrieved_at: str,
    low: float,
    high: float | None = None,
    unit: str = "CNY",
    source_claim_id: str = "",
) -> ExpectationValue:
    """构造 ExpectationValue 测试数据。"""
    return ExpectationValue(
        metric=metric,
        value=Interval(low, high if high is not None else low),
        unit=unit,
        currency="CNY",
        period=period,
        as_of=published_at,
        data_version="test_v1",
        source_claim_id=source_claim_id or f"claim_{published_at}",
        source_evidence_id=f"evid_{published_at}",
        extraction_method="test_fixture",
        confidence=Interval(0.7, 0.9),
        published_at=published_at,
        retrieved_at=retrieved_at,
    )


def _make_estimate(
    *,
    company_symbol: str = "TEST001",
    institution_id: str,
    provider: str = "",
    values: list[ExpectationValue],
) -> ConsensusEstimate:
    """构造 ConsensusEstimate 测试数据。"""
    return ConsensusEstimate(
        company_symbol=company_symbol,
        provider=provider or institution_id,
        institution_id=institution_id,
        analyst_id=f"{institution_id}_analyst_A",
        sample_size=1,
        dispersion=Interval(0.0, 0.0),
        values=values,
    )


@pytest.fixture
def three_institution_estimates(as_of_str: str) -> list[ConsensusEstimate]:
    """3 家机构在 3m 窗口内的修正序列。

    机构 A：
      - v1 (2026-08-01)：6m 内但 3m 外（应当被窗口过滤）
      - v2 (2026-12-01)：3m 内，1m 外
    机构 B：
      - v1 (2026-12-15)：1m 内
    机构 C：
      - v1 (2026-12-20)：1m 内
    """
    inst_a = _make_estimate(
        institution_id="inst_A",
        provider="provider_A",
        values=[
            _make_value(
                published_at="2026-08-01T00:00:00+00:00",
                retrieved_at="2026-08-02T00:00:00+00:00",
                low=9.5e8,
                high=9.7e8,
                source_claim_id="claim_A_v1",
            ),
            _make_value(
                published_at="2026-12-01T00:00:00+00:00",
                retrieved_at="2026-12-02T00:00:00+00:00",
                low=1.0e9,
                high=1.02e9,
                source_claim_id="claim_A_v2",
            ),
        ],
    )
    inst_b = _make_estimate(
        institution_id="inst_B",
        provider="provider_B",
        values=[
            _make_value(
                published_at="2026-12-15T00:00:00+00:00",
                retrieved_at="2026-12-16T00:00:00+00:00",
                low=1.05e9,
                high=1.07e9,
                source_claim_id="claim_B_v1",
            ),
        ],
    )
    inst_c = _make_estimate(
        institution_id="inst_C",
        provider="provider_C",
        values=[
            _make_value(
                published_at="2026-12-20T00:00:00+00:00",
                retrieved_at="2026-12-21T00:00:00+00:00",
                low=1.08e9,
                high=1.10e9,
                source_claim_id="claim_C_v1",
            ),
        ],
    )
    return [inst_a, inst_b, inst_c]


@pytest.fixture
def single_institution_estimates() -> list[ConsensusEstimate]:
    """只有 1 家机构（用于测试验收点 3）。"""
    return [
        _make_estimate(
            institution_id="only_inst",
            provider="only_provider",
            values=[
                _make_value(
                    published_at="2026-12-15T00:00:00+00:00",
                    retrieved_at="2026-12-16T00:00:00+00:00",
                    low=1.0e9,
                    high=1.05e9,
                ),
            ],
        ),
    ]


@pytest.fixture
def company_guidance(as_of_str: str) -> CompanyGuidance:
    """公司指引（高于共识中位数）。"""
    return CompanyGuidance(
        company_symbol="TEST001",
        values=[
            _make_value(
                metric="net_profit",
                period="2027",
                published_at="2026-11-15T00:00:00+00:00",
                retrieved_at="2026-11-16T00:00:00+00:00",
                low=1.20e9,
                high=1.25e9,
                source_claim_id="guidance_claim_1",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 验收点 1：修正序列按发布日期重建
# ---------------------------------------------------------------------------


class TestRevisionSequenceByPublishedAt:
    """验收点 1：修正序列按发布日期重建。"""

    def test_revision_points_sorted_by_published_at_ascending(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """revision_points 必须按 published_at 升序排列。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        assert seq.status == STATUS_OK
        published_dates = [p.published_at for p in seq.revision_points]
        assert published_dates == sorted(published_dates), (
            f"revision_points 未按 published_at 升序：{published_dates}"
        )

    def test_window_filtering_3m_excludes_august_data(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """3m 窗口应排除 2026-08-01 数据（窗口起点 2026-10-17）。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        # 3m 窗口内应有 3 家机构（A v2 / B / C）
        assert seq.source_count == 3
        # 不应包含 2026-08-01 的数据
        for p in seq.revision_points:
            assert "2026-08-01" not in p.published_at, (
                f"3m 窗口不应包含 2026-08-01 数据：{p.published_at}"
            )

    def test_window_filtering_1m_only_recent(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """1m 窗口（起点 2026-12-16）应仅含 inst_B 与 inst_C。

        1m 窗口起点 = as_of - 30 days = 2026-12-16
        - inst_B published 2026-12-15 < 12-16，不在窗口内
        - inst_C published 2026-12-20，在窗口内

        修正：实际只有 inst_C 在 1m 窗口内（inst_B 12-15 早于 12-16）。
        """
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_1M,
        )
        # 1m 窗口内只有 inst_C 一家机构 → insufficient_sources
        assert seq.source_count == 1
        assert seq.status == STATUS_INSUFFICIENT_SOURCES
        assert len(seq.revision_points) == 1
        assert "inst_C" in seq.revision_points[0].institution_id

    def test_institution_dedup_keeps_latest(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """同机构多条版本仅保留最新（按 published_at）。"""
        engine = build_revision_engine()
        seq_6m = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_6M,
        )
        # 6m 窗口应包含 inst_A v2（2026-12-01），不应包含 v1（2026-08-01）
        inst_a_points = [
            p for p in seq_6m.revision_points if p.institution_id == "inst_A"
        ]
        assert len(inst_a_points) == 1, "同机构多条版本未去重"
        assert "2026-12-01" in inst_a_points[0].published_at

    def test_build_all_windows(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """build_all_windows 应同时返回 1m / 3m / 6m 三个序列。"""
        engine = build_revision_engine()
        result = engine.build_all_windows(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
        )
        assert set(result.keys()) == set(REVISION_WINDOWS)
        # 1m 应为 insufficient_sources，3m 与 6m 应为 ok
        assert result[REVISION_WINDOW_1M].status == STATUS_INSUFFICIENT_SOURCES
        assert result[REVISION_WINDOW_3M].status == STATUS_OK
        assert result[REVISION_WINDOW_6M].status == STATUS_OK

    def test_revision_direction_net_up(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """整体修正方向应为 up（最新中位数 > 最早中位数）。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_6M,
        )
        # 最早 inst_A v2 mid = 1.01e9；最新 inst_C mid = 1.09e9 → up
        assert seq.revision_direction == "up"
        assert seq.net_revision > 0

    def test_sequence_id_derived_from_business_keys(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """sequence_id 应基于 (company_symbol, metric, period, as_of, window) 派生。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        assert seq.sequence_id == (
            f"revseq_TEST001_net_profit_2027_{as_of_str}_{REVISION_WINDOW_3M}"
        )


# ---------------------------------------------------------------------------
# 验收点 2：Reverse DCF 与一致预期使用独立标签
# ---------------------------------------------------------------------------


class TestImpliedExpectationIndependentTag:
    """验收点 2：Reverse DCF 与一致预期使用独立标签。"""

    def test_implied_metric_default_tag(self) -> None:
        """ImpliedMetric 默认 expectation_source='implied'。"""
        metric = ImpliedMetric(
            metric_kind=IMPLIED_METRIC_GROWTH,
            value=0.08,
            unit="ratio",
            as_of="2027-01-15T00:00:00+00:00",
            company_symbol="TEST001",
            reverse_model="reverse_dcf_gordon",
        )
        assert metric.expectation_source == IMPLIED_EXPECTATION_SOURCE
        assert metric.is_implied_tagged() is True

    def test_implied_metric_with_wrong_tag_detected(self) -> None:
        """非 implied 标签会被 is_implied_tagged 检测出来。"""
        metric = ImpliedMetric(
            metric_kind=IMPLIED_METRIC_GROWTH,
            value=0.08,
            expectation_source="consensus",  # 故意冒充一致预期
        )
        assert metric.is_implied_tagged() is False

    def test_implied_growth_carries_implied_tag(
        self, as_of_str: str
    ) -> None:
        """compute_implied_growth 输出必须带 expectation_source='implied'。"""
        engine = P25ImpliedExpectationEngine()
        metric = engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=0.08,
        )
        assert metric is not None
        assert metric.expectation_source == IMPLIED_EXPECTATION_SOURCE
        assert metric.is_implied_tagged() is True
        # 数值校验：g = (mc*wacc - fcff) / (mc + fcff)
        expected_g = (1.0e10 * 0.08 - 8.0e8) / (1.0e10 + 8.0e8)
        assert abs(metric.value - expected_g) < 1e-9

    def test_implied_margin_carries_implied_tag(
        self, as_of_str: str
    ) -> None:
        """compute_implied_margin 输出必须带 expectation_source='implied'。"""
        engine = P25ImpliedExpectationEngine()
        metric = engine.compute_implied_margin(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_revenue=5.0e9,
            required_return=0.08,
        )
        assert metric is not None
        assert metric.expectation_source == IMPLIED_EXPECTATION_SOURCE
        # margin = mc * required_return / revenue
        expected_margin = 1.0e10 * 0.08 / 5.0e9
        assert abs(metric.value - expected_margin) < 1e-9

    def test_implied_duration_carries_implied_tag(
        self, as_of_str: str
    ) -> None:
        """compute_implied_duration 输出必须带 expectation_source='implied'。

        反推约束：ratio = 1 - wacc*excess_value/EVA 必须落在 (0, 1)。
        - EVA = invested_capital * excess_return
        - excess_value = market_cap - invested_capital
        - 需 wacc * excess_value < EVA
        选 mc=1e10, ic=8e9, excess_return=0.08, wacc=0.08：
        - EVA = 8e9 * 0.08 = 6.4e8
        - excess_value = 1e10 - 8e9 = 2e9
        - ratio = 1 - 0.08 * 2e9 / 6.4e8 = 0.75 ∈ (0, 1) ✓
        """
        engine = P25ImpliedExpectationEngine()
        metric = engine.compute_implied_duration(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            invested_capital=8.0e9,
            excess_return=0.08,
            wacc=0.08,
        )
        assert metric is not None
        assert metric.expectation_source == IMPLIED_EXPECTATION_SOURCE
        assert metric.unit == "years"
        assert metric.value > 0

    def test_implied_roic_carries_implied_tag(
        self, as_of_str: str
    ) -> None:
        """compute_implied_roic 输出必须带 expectation_source='implied'。"""
        engine = P25ImpliedExpectationEngine()
        metric = engine.compute_implied_roic(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            invested_capital=5.0e9,
            wacc=0.08,
            duration_years=10.0,
            terminal_growth=0.02,
        )
        assert metric is not None
        assert metric.expectation_source == IMPLIED_EXPECTATION_SOURCE
        # 隐含 ROIC > WACC（市值大于投入资本）
        assert metric.value > 0.08

    def test_implied_growth_returns_none_on_invalid_input(self, as_of_str: str) -> None:
        """输入不完整时返回 None，不伪造。"""
        engine = P25ImpliedExpectationEngine()
        # market_cap <= 0
        assert engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=0.0,
            base_fcff=8.0e8,
            wacc=0.08,
        ) is None
        # wacc 越界
        assert engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=1.5,
        ) is None

    def test_summarize_all_implied_tagged(
        self, as_of_str: str
    ) -> None:
        """全部指标带 implied 标签时 all_implied_tagged=True。"""
        engine = P25ImpliedExpectationEngine()
        growth = engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=0.08,
        )
        margin = engine.compute_implied_margin(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_revenue=5.0e9,
            required_return=0.08,
        )
        summary = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[growth, margin],
        )
        assert summary.all_implied_tagged is True
        assert summary.status in ("ok", "partial")
        # 验收点 2：summary 不会写入 consensus_estimates 表
        # （通过 repository 测试进一步验证）

    def test_summarize_with_no_metrics_returns_no_data(
        self, as_of_str: str
    ) -> None:
        """无任何隐含指标时 status='no_data'。"""
        engine = P25ImpliedExpectationEngine()
        summary = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[None, None],
        )
        assert summary.status == "no_data"
        assert summary.all_implied_tagged is True  # 空集视为全部带标签
        assert len(summary.metrics) == 0

    def test_reverse_dcf_engine_carries_expectation_source_tag(self) -> None:
        """ReverseDcfEngine 输出的 method_specific_metrics 必须含 expectation_source='implied'。"""
        # 构造极简输入：fcff_series / wacc / market_cap
        inputs = ValuationInput(
            company_symbol="TEST001",
            as_of="2027-01-15T00:00:00+00:00",
            shares_outstanding=1.0e8,
            market_cap=1.0e10,
            fcff_series=[8.0e8, 8.5e8, 9.0e8],
            assumptions=[
                ValuationAssumption(
                    name="wacc",
                    value=0.08,
                    source_ref="test_wacc_source",
                ),
            ],
        )
        engine = ReverseDcfEngine()
        case, gaps = engine.reverse_engineer(inputs)
        assert not gaps, f"Reverse DCF 不应有 gap：{[g.reason for g in gaps]}"
        assert case.method == ValuationMethod.REVERSE_DCF.value
        # P2.5 验收点 2：必须含 expectation_source='implied'
        assert case.method_specific_metrics.get("expectation_source") == "implied"
        assert case.method_specific_metrics.get("is_implied") is True
        assert case.method_specific_metrics.get("source_kind") == "implied"

    def test_reverse_dcf_wacc_mode_carries_expectation_source_tag(self) -> None:
        """ReverseDcfEngine.reverse_engineer_wacc 也必须含 expectation_source='implied'。"""
        inputs = ValuationInput(
            company_symbol="TEST001",
            as_of="2027-01-15T00:00:00+00:00",
            shares_outstanding=1.0e8,
            market_cap=1.0e10,
            fcff_series=[8.0e8, 8.5e8, 9.0e8],
            assumptions=[
                ValuationAssumption(
                    name="terminal_growth",
                    value=0.02,
                    source_ref="test_g_source",
                ),
            ],
        )
        engine = ReverseDcfEngine()
        case, gaps = engine.reverse_engineer_wacc(inputs)
        assert not gaps, f"Reverse DCF (WACC) 不应有 gap：{[g.reason for g in gaps]}"
        assert case.method_specific_metrics.get("expectation_source") == "implied"
        assert case.method_specific_metrics.get("reverse_mode") == "wacc"


# ---------------------------------------------------------------------------
# 验收点 3：单一机构来源不能冒充市场共识
# ---------------------------------------------------------------------------


class TestSingleInstitutionNotMarketConsensus:
    """验收点 3：单一机构来源不能冒充市场共识。"""

    def test_single_institution_insufficient_sources(
        self,
        as_of_str: str,
        single_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """source_count < min_sources 时 status='insufficient_sources'。"""
        engine = build_revision_engine()  # 默认 min_sources=3
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=single_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        assert seq.source_count == 1
        assert seq.status == STATUS_INSUFFICIENT_SOURCES
        assert any("source_count=1" in note for note in seq.notes)

    def test_consensus_diversity_single_institution_insufficient(
        self,
        as_of_str: str,
        single_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """compute_consensus_diversity：单一机构 status='insufficient_sources'。"""
        engine = build_revision_engine()
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=single_institution_estimates,
        )
        assert diversity.source_count == 1
        assert diversity.status == STATUS_INSUFFICIENT_SOURCES
        # 仍计算统计量但标记不可靠
        assert diversity.median > 0

    def test_source_concentration_risk_single_institution(
        self,
        as_of_str: str,
        single_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """单一机构 HHI=1.0，top_source_share=1.0，is_concentrated=True。"""
        engine = build_revision_engine()
        risk = engine.compute_source_concentration_risk(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=single_institution_estimates,
        )
        assert risk.source_count == 1
        assert abs(risk.hhi - 1.0) < 1e-9
        assert abs(risk.top_source_share - 1.0) < 1e-9
        assert risk.is_concentrated is True
        assert risk.status == STATUS_INSUFFICIENT_SOURCES

    def test_min_sources_threshold_configurable(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """min_sources 可配置；设为 2 时 3 家机构应通过。"""
        engine = build_revision_engine(min_sources=2)
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        assert seq.source_count == 3
        assert seq.status == STATUS_OK

    def test_high_hhi_marks_concentration_risk(
        self,
        as_of_str: str,
    ) -> None:
        """HHI >= 阈值（但 source_count >= min_sources）时 status='degraded'。

        构造 5 家机构但其中 1 家占比 60%（HHI≈0.52>0.5）。
        """
        # 5 家机构，但 inst_dom 有 6 条记录，其它各 1 条 → inst_dom 占 6/10=0.6
        estimates: list[ConsensusEstimate] = []
        for i in range(6):
            estimates.append(_make_estimate(
                institution_id="inst_dom",
                values=[
                    _make_value(
                        published_at=f"2026-12-{10 + i:02d}T00:00:00+00:00",
                        retrieved_at=f"2026-12-{10 + i:02d}T12:00:00+00:00",
                        low=1.0e9 + i * 1e7,
                    ),
                ],
            ))
        for i in range(4):
            estimates.append(_make_estimate(
                institution_id=f"inst_minor_{i}",
                values=[
                    _make_value(
                        published_at="2026-12-15T00:00:00+00:00",
                        retrieved_at="2026-12-16T00:00:00+00:00",
                        low=1.0e9 + i * 1e7,
                    ),
                ],
            ))
        engine = build_revision_engine(min_sources=3, concentration_hhi_threshold=0.5)
        risk = engine.compute_source_concentration_risk(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=estimates,
        )
        assert risk.source_count == 5  # 5 家不同机构
        assert risk.top_source_id == "inst_dom"
        assert risk.top_source_share == 0.6
        # HHI = 0.6^2 + 4 * 0.1^2 = 0.36 + 0.04 = 0.40 < 0.5
        # 但 top_source_share=0.6 >= 0.5 → is_concentrated
        assert risk.is_concentrated is True
        assert risk.status == STATUS_DEGRADED


# ---------------------------------------------------------------------------
# 验收点 4：无可靠共识时输出不可计算
# ---------------------------------------------------------------------------


class TestNotComputableWhenNoReliableConsensus:
    """验收点 4：无可靠共识时输出不可计算。"""

    def test_no_estimates_returns_no_data(self, as_of_str: str) -> None:
        """空输入时 status='no_data'。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=[],
            window=REVISION_WINDOW_3M,
        )
        assert seq.status == STATUS_NO_DATA
        assert seq.source_count == 0
        assert len(seq.revision_points) == 0

    def test_invalid_as_of_returns_not_computable(
        self,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """as_of 非法时 status='not_computable'。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of="not-a-date",
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        assert seq.status == STATUS_NOT_COMPUTABLE
        assert "invalid_as_of" in seq.notes

    def test_guidance_consensus_gap_no_guidance(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """guidance=None 时 status='not_computable'。"""
        engine = build_revision_engine()
        gap = engine.compute_guidance_consensus_gap(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            guidance=None,
            estimates=three_institution_estimates,
        )
        assert gap.status == STATUS_NOT_COMPUTABLE
        assert "guidance_missing" in gap.notes

    def test_guidance_consensus_gap_no_consensus(
        self,
        as_of_str: str,
        company_guidance: CompanyGuidance,
    ) -> None:
        """consensus 数据空时 status='not_computable'。"""
        engine = build_revision_engine()
        gap = engine.compute_guidance_consensus_gap(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            guidance=company_guidance,
            estimates=[],
        )
        assert gap.status == STATUS_NOT_COMPUTABLE
        # 共识空 → diversity 返回 no_data → gap 标记 not_computable
        assert "consensus_no_data" in gap.notes or "consensus_insufficient_sources" in gap.notes

    def test_guidance_consensus_gap_with_consensus(
        self,
        as_of_str: str,
        company_guidance: CompanyGuidance,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """有公司指引 + 3 家机构共识 → gap 可计算。"""
        engine = build_revision_engine()
        gap = engine.compute_guidance_consensus_gap(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            guidance=company_guidance,
            estimates=three_institution_estimates,
        )
        assert gap.status == STATUS_OK
        # 公司指引 mid (1.225e9) > 共识中位数 → gap > 0
        assert gap.gap > 0
        assert gap.consensus_source_count == 3
        assert gap.guidance_source_count == 1

    def test_consensus_diversity_no_data(self, as_of_str: str) -> None:
        """compute_consensus_diversity 空输入时 status='no_data'。"""
        engine = build_revision_engine()
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=[],
        )
        assert diversity.status == STATUS_NO_DATA
        assert diversity.source_count == 0

    def test_source_concentration_risk_no_data(self, as_of_str: str) -> None:
        """compute_source_concentration_risk 空输入时 status='no_data'。"""
        engine = build_revision_engine()
        risk = engine.compute_source_concentration_risk(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=[],
        )
        assert risk.status == STATUS_NO_DATA
        assert risk.source_count == 0


# ---------------------------------------------------------------------------
# ConsensusDiversity 统计正确性
# ---------------------------------------------------------------------------


class TestConsensusDiversityStatistics:
    """ConsensusDiversity 分位数/标准差/CV 计算正确性。"""

    def test_diversity_statistics_with_3_institutions(
        self,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """3 家机构 diversity 统计正确。"""
        engine = build_revision_engine()
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
        )
        # inst_A v2 mid=1.01e9, inst_B mid=1.06e9, inst_C mid=1.09e9
        # 注意：inst_A 有 v1 与 v2，diversity 应仅保留 v2（retrieved_at 最新）
        assert diversity.source_count == 3
        assert diversity.institution_ids == ["inst_A", "inst_B", "inst_C"]
        # median = 1.06e9
        assert abs(diversity.median - 1.06e9) < 1.0
        # mean = (1.01 + 1.06 + 1.09) / 3 * 1e9
        expected_mean = (1.01e9 + 1.06e9 + 1.09e9) / 3
        assert abs(diversity.mean - expected_mean) < 1.0
        assert diversity.min_value == 1.01e9
        assert diversity.max_value == 1.09e9
        assert diversity.dispersion_range == 1.09e9 - 1.01e9
        assert diversity.dispersion_std > 0
        assert diversity.dispersion_cv > 0

    def test_diversity_pit_filter_excludes_future_publications(
        self,
        as_of_str: str,
    ) -> None:
        """PIT 过滤应排除 published_at > as_of 的预期。"""
        future_est = _make_estimate(
            institution_id="future_inst",
            values=[
                _make_value(
                    published_at="2027-02-01T00:00:00+00:00",  # 未来
                    retrieved_at="2027-02-02T00:00:00+00:00",
                    low=2.0e9,
                ),
            ],
        )
        engine = build_revision_engine()
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=[future_est],
            historical=True,
        )
        # 未来发布的预期不可见 → no_data
        assert diversity.status == STATUS_NO_DATA

    def test_diversity_historical_false_bypasses_pit(
        self,
        as_of_str: str,
    ) -> None:
        """historical=False 时不过滤 PIT（用于历史回测场景）。"""
        future_est = _make_estimate(
            institution_id="future_inst",
            values=[
                _make_value(
                    published_at="2027-02-01T00:00:00+00:00",
                    retrieved_at="2027-02-02T00:00:00+00:00",
                    low=2.0e9,
                ),
            ],
        )
        engine = build_revision_engine()
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=[future_est],
            historical=False,
        )
        # 不过滤 → 1 家机构但 status='insufficient_sources'
        assert diversity.source_count == 1
        assert diversity.status == STATUS_INSUFFICIENT_SOURCES


# ---------------------------------------------------------------------------
# Repository 持久化与 PIT 查询
# ---------------------------------------------------------------------------


class TestRepositoryPersistence:
    """ExpectationRepository 持久化与 PIT 查询。"""

    @pytest.fixture
    def repo(self, tmp_path) -> ExpectationRepository:
        """使用临时路径初始化 repository。"""
        return ExpectationRepository(db_path=tmp_path / "test_p25.db")

    def test_save_and_list_revision_sequences(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """save_revision_sequence + list_revision_sequences 基本流程。"""
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        repo.save_revision_sequence(seq)

        # 按 company_symbol 查询
        rows = repo.list_revision_sequences(company_symbol="TEST001")
        assert len(rows) == 1
        assert rows[0]["sequence_id"] == seq.sequence_id
        assert rows[0]["metric"] == "net_profit"
        assert rows[0]["window"] == REVISION_WINDOW_3M
        assert rows[0]["status"] == STATUS_OK
        assert int(rows[0]["source_count"]) == 3

    def test_list_revision_sequences_pit_filter(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """list_revision_sequences 的 as_of PIT 过滤生效。"""
        engine = build_revision_engine()
        # 保存两个不同 as_of 的序列
        for as_of_value in ["2027-01-15T00:00:00+00:00", "2027-02-15T00:00:00+00:00"]:
            seq = engine.build_revision_sequence(
                company_symbol="TEST001",
                metric="net_profit",
                period="2027",
                as_of=as_of_value,
                estimates=three_institution_estimates,
                window=REVISION_WINDOW_3M,
            )
            repo.save_revision_sequence(seq)

        # as_of='2027-01-20' 应只返回 2027-01-15 的序列
        rows = repo.list_revision_sequences(
            company_symbol="TEST001",
            as_of="2027-01-20T00:00:00+00:00",
        )
        assert len(rows) == 1
        assert "2027-01-15" in rows[0]["as_of"]

    def test_list_revision_sequences_status_filter(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
        three_institution_estimates: list[ConsensusEstimate],
        single_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """list_revision_sequences 的 status 过滤生效。

        注意：
        1. single_institution_estimates 的 published_at=2026-12-15，1m 窗口
           （起点 2026-12-16）会将其过滤掉，返回 NO_DATA 而非 INSUFFICIENT_SOURCES。
           因此使用 3m 窗口（起点 2026-10-17）使其可见但触发 INSUFFICIENT_SOURCES。
        2. sequence_id 派生规则为 `revseq_{symbol}_{metric}_{period}_{as_of}_{window}`，
           相同 (symbol, metric, period, as_of, window) 会冲突导致主键覆盖。
           因此两个序列使用不同 window（3m 与 6m）。
        """
        engine = build_revision_engine()
        seq_ok = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_6M,  # 6m 窗口，3 家机构 → ok
        )
        seq_insufficient = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=single_institution_estimates,
            window=REVISION_WINDOW_3M,  # 3m 窗口，1 家机构 → insufficient
        )
        assert seq_ok.status == STATUS_OK
        assert seq_insufficient.status == STATUS_INSUFFICIENT_SOURCES
        assert seq_ok.sequence_id != seq_insufficient.sequence_id, (
            "测试 setup 错误：两个序列主键相同会被覆盖"
        )
        repo.save_revision_sequence(seq_ok)
        repo.save_revision_sequence(seq_insufficient)

        rows_ok = repo.list_revision_sequences(
            company_symbol="TEST001", status=STATUS_OK,
        )
        assert len(rows_ok) == 1
        assert rows_ok[0]["window"] == REVISION_WINDOW_6M
        assert int(rows_ok[0]["source_count"]) == 3

        rows_insufficient = repo.list_revision_sequences(
            company_symbol="TEST001", status=STATUS_INSUFFICIENT_SOURCES,
        )
        assert len(rows_insufficient) == 1
        assert rows_insufficient[0]["window"] == REVISION_WINDOW_3M
        assert int(rows_insufficient[0]["source_count"]) == 1

    def test_save_and_list_implied_summaries(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
    ) -> None:
        """save_implied_expectation_summary + list_implied_expectation_summaries。"""
        engine = P25ImpliedExpectationEngine()
        growth = engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=0.08,
        )
        margin = engine.compute_implied_margin(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_revenue=5.0e9,
            required_return=0.08,
        )
        summary = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[growth, margin],
        )
        repo.save_implied_expectation_summary(summary)

        rows = repo.list_implied_expectation_summaries(company_symbol="TEST001")
        assert len(rows) == 1
        assert rows[0]["summary_id"] == summary.summary_id
        assert rows[0]["as_of"] == as_of_str
        assert rows[0]["status"] in ("ok", "partial")
        assert rows[0]["all_implied_tagged"] in (True, "1", 1)

    def test_list_implied_summaries_all_tagged_filter(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
    ) -> None:
        """all_implied_tagged 过滤生效。

        注意：summary_id 派生规则为 `impl_sum_{symbol}_{as_of}`，相同 (symbol, as_of)
        会冲突导致主键覆盖。因此两个 summary 使用不同 as_of。
        """
        engine = P25ImpliedExpectationEngine()
        as_of_a = as_of_str
        as_of_b = "2027-02-15T00:00:00+00:00"
        # 1) 全部带标签的 summary
        growth = engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_a,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=0.08,
        )
        summary_tagged = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_a,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[growth],
        )
        # 2) 不带标签的 summary（手工构造）
        bad_metric = ImpliedMetric(
            metric_kind=IMPLIED_METRIC_GROWTH,
            value=0.05,
            as_of=as_of_b,
            company_symbol="TEST001",
            expectation_source="consensus",  # 冒充一致预期
        )
        summary_bad = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_b,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[bad_metric],
        )
        assert summary_tagged.summary_id != summary_bad.summary_id, (
            "测试 setup 错误：两个 summary 主键相同会被覆盖"
        )
        repo.save_implied_expectation_summary(summary_tagged)
        repo.save_implied_expectation_summary(summary_bad)

        # 仅查 all_implied_tagged=True
        rows_tagged = repo.list_implied_expectation_summaries(
            company_symbol="TEST001", all_implied_tagged=True,
        )
        assert len(rows_tagged) == 1
        assert rows_tagged[0]["summary_id"] == summary_tagged.summary_id

        # 仅查 all_implied_tagged=False
        rows_bad = repo.list_implied_expectation_summaries(
            company_symbol="TEST001", all_implied_tagged=False,
        )
        assert len(rows_bad) == 1
        assert rows_bad[0]["summary_id"] == summary_bad.summary_id

    def test_schema_version_upgraded_to_4(self, repo: ExpectationRepository) -> None:
        """SCHEMA_VERSION 应为 4。"""
        stats = repo.stats()
        assert stats["schema_version"] == 4
        # 新表存在
        assert "revision_sequences" in stats["counts"]
        assert "implied_expectation_summaries" in stats["counts"]

    def test_implied_summary_not_in_consensus_table(
        self,
        repo: ExpectationRepository,
        as_of_str: str,
    ) -> None:
        """验收点 2：implied summary 不会写入 consensus_summaries 表。"""
        engine = P25ImpliedExpectationEngine()
        growth = engine.compute_implied_growth(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            base_fcff=8.0e8,
            wacc=0.08,
        )
        summary = engine.summarize(
            company_symbol="TEST001",
            as_of=as_of_str,
            market_cap=1.0e10,
            current_price=50.0,
            metrics=[growth],
        )
        repo.save_implied_expectation_summary(summary)

        # consensus_summaries 表不应含此记录
        consensus_rows = repo.list_consensus_summaries(company_symbol="TEST001")
        assert all(r.get("summary_id") != summary.summary_id for r in consensus_rows), (
            "implied summary 错误地写入了 consensus_summaries 表"
        )


# ---------------------------------------------------------------------------
# 集成：四类输出综合
# ---------------------------------------------------------------------------


class TestRevisionEngineIntegration:
    """RevisionEngine 四类输出综合集成。"""

    def test_all_four_outputs_consistent(
        self,
        as_of_str: str,
        company_guidance: CompanyGuidance,
        three_institution_estimates: list[ConsensusEstimate],
    ) -> None:
        """四类输出（修正序列 + diversity + gap + concentration）一致。

        注意：compute_source_concentration_risk 不按窗口过滤，仅 PIT 过滤。
        inst_A 在 PIT 内有 v1（2026-08-01）与 v2（2026-12-01）两条记录，
        因此 institution_counts={inst_A:2, inst_B:1, inst_C:1}，total=4，
        top_source_share=0.5 ≥ 默认阈值 0.5 → is_concentrated=True, status='degraded'。
        这是预期行为：HHI 集中度评估反映所有可见记录的来源分布。
        """
        engine = build_revision_engine()
        seq = engine.build_revision_sequence(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
            window=REVISION_WINDOW_3M,
        )
        diversity = engine.compute_consensus_diversity(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
        )
        gap = engine.compute_guidance_consensus_gap(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            guidance=company_guidance,
            estimates=three_institution_estimates,
        )
        risk = engine.compute_source_concentration_risk(
            company_symbol="TEST001",
            metric="net_profit",
            period="2027",
            as_of=as_of_str,
            estimates=three_institution_estimates,
        )
        # 修正序列与 diversity 按 institution_id 去重后 source_count=3
        assert seq.source_count == diversity.source_count == 3
        assert gap.consensus_source_count == 3
        # risk 按 institution_id 聚合但保留所有 PIT 可见记录，source_count=3
        assert risk.source_count == 3
        # 状态：seq/diversity/gap 为 ok（3 家机构满足 min_sources）
        assert seq.status == STATUS_OK
        assert diversity.status == STATUS_OK
        assert gap.status == STATUS_OK
        # risk：inst_A 有 2 条 PIT 内记录，top_share=0.5 触发 degraded
        # （HHI=0.375 < 0.5 但 top_share=0.5 ≥ 0.5）
        assert risk.status in (STATUS_OK, STATUS_DEGRADED), (
            f"risk.status 应为 ok 或 degraded，实际为 {risk.status}"
        )
        # source_count >= min_sources，不会是 insufficient_sources
        assert risk.status != STATUS_INSUFFICIENT_SOURCES


def test_revision_engine_degrades_malformed_source_and_retrieval_fields(as_of_str: str) -> None:
    value = _make_value(
        published_at=as_of_str,
        retrieved_at=123,
        low=10.0,
    )
    estimate = ConsensusEstimate(
        company_symbol="TEST001",
        institution_id={"bad": "source"},
        provider=["bad"],
        values=[value],
    )

    engine = RevisionEngine(min_sources=1)
    diversity = engine.compute_consensus_diversity(
        company_symbol="TEST001",
        metric="net_profit",
        period="2027",
        as_of=as_of_str,
        estimates=[estimate],
        historical=False,
    )
    risk = engine.compute_source_concentration_risk(
        company_symbol="TEST001",
        metric="net_profit",
        period="2027",
        as_of=as_of_str,
        estimates=[estimate],
        historical=False,
    )

    assert diversity.status == STATUS_OK
    assert diversity.institution_ids == ["unknown"]
    assert risk.source_count == 1
