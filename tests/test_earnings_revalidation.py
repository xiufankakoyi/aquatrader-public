"""Phase 7 - 盈利预测与 SAG 产业链传播 PIT 重验测试。

覆盖：
- 正向案例（预测方向正确 + 区间覆盖）
- 负向案例（预测方向错误）
- 无显著案例（实际结果落在区间内但无显著变化）
- PIT 检测拦截未来财报和修订
- 样本不足输出 insufficient_sample
- SAG 传播 vs 股价反应分开评价
- 不含合成数据宣称业务通过

约束（AGENTS.md）：
- 不输出投资建议（disclaimer 必须出现在每份报告）
- 样本不足时禁止用合成数据宣称业务通过
- PIT 安全：未来财报和修订版本在历史 as_of 下不可见
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.earnings.revalidation import (
    DISCLAIMER,
    AccuracyReport,
    CaseCategory,
    ForecastRevalidator,
    InputDocument,
    MIN_SAMPLE_FOR_ACCURACY,
    MIN_SAMPLE_FOR_PROPAGATION,
    PredictionType,
    PriceReaction,
    PropagationReport,
    RevalidationCase,
    RevalidationResult,
    SAGClaimReaction,
    SAGPropagationRevalidator,
)


# ---------- 测试常量 ----------

_AS_OF = "2026-01-01T00:00:00+00:00"
_PAST_PUBLISHED = "2025-12-01T00:00:00+00:00"
_PAST_RETRIEVED = "2025-12-15T00:00:00+00:00"
_FUTURE_PUBLISHED = "2026-06-01T00:00:00+00:00"
_FUTURE_RETRIEVED = "2026-06-15T00:00:00+00:00"
_ACTUAL_DATE = "2026-04-15T00:00:00+00:00"


# ---------- 辅助函数 ----------


def _make_input_document(
    doc_id: str,
    *,
    published_at: str = _PAST_PUBLISHED,
    retrieved_at: str = _PAST_RETRIEVED,
    title: str = "doc",
) -> InputDocument:
    """构造 PIT 可见的输入文档（默认时间早于 as_of）。"""
    return InputDocument(
        document_id=doc_id,
        title=title,
        published_at=published_at,
        retrieved_at=retrieved_at,
        source_kind="company_disclosure",
    )


def _make_positive_case(
    *,
    actual_result: float | None = 0.08,
    actual_result_date: str = _ACTUAL_DATE,
    symbol: str = "600000.SH",
) -> RevalidationCase:
    """构造正向预测案例，默认实际结果落在区间内。"""
    rv = ForecastRevalidator()
    case = rv.create_case(
        symbol=symbol,
        theme_id="theme_test",
        as_of=_AS_OF,
        prediction_type=PredictionType.EARNINGS_FORECAST.value,
        input_documents=[_make_input_document("doc1")],
        prediction_horizon="2026Q1",
        assumptions=["demand_growth_up", "asp_stable"],
        prediction_value=0.10,
        prediction_interval_low=0.05,
        prediction_interval_high=0.15,
        case_category=CaseCategory.POSITIVE.value,
        selection_reason="Strong demand signal from 2025-Q4 reports",
    )
    if actual_result is not None:
        rv.resolve_case(
            case,
            actual_result=actual_result,
            actual_result_date=actual_result_date,
        )
    return case


# =====================================================================
# 1. 正向 / 负向 / 无显著案例
# =====================================================================


class TestCaseDirections:
    """测试三种 case_category 的方向判定与区间覆盖逻辑。"""

    def test_positive_case_direction_and_interval_correct(self):
        """正向案例：预测方向正确 + 实际值落在区间内。"""
        case = _make_positive_case(actual_result=0.08)
        rv = ForecastRevalidator()
        result = rv._compute_metrics(case)

        assert result.direction_correct is True
        assert result.interval_covered is True
        assert result.is_failure is False
        assert result.error_magnitude == pytest.approx(0.02)

    def test_positive_case_direction_correct_interval_miss(self):
        """正向案例：方向对但实际值超出区间上界（部分正确，不算失败）。"""
        case = _make_positive_case(actual_result=0.20)  # > 0.15
        rv = ForecastRevalidator()
        result = rv._compute_metrics(case)

        assert result.direction_correct is True
        assert result.interval_covered is False
        # 方向对了就不算失败
        assert result.is_failure is False

    def test_negative_case_direction_wrong(self):
        """负向案例：预测向下但实际向上 -> 方向错误 + 失败。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600001.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            input_documents=[_make_input_document("doc_neg")],
            prediction_value=-0.05,
            prediction_interval_low=-0.10,
            prediction_interval_high=-0.02,
            case_category=CaseCategory.NEGATIVE.value,
            selection_reason="Margin compression signal",
        )
        rv.resolve_case(
            case,
            actual_result=0.03,  # 实际向上，与预测相反
            actual_result_date=_ACTUAL_DATE,
        )
        result = rv._compute_metrics(case)

        assert result.direction_correct is False
        assert result.interval_covered is False
        assert result.is_failure is True

    def test_negative_case_direction_correct(self):
        """负向案例：方向对 + 区间覆盖。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600001.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            prediction_value=-0.05,
            prediction_interval_low=-0.10,
            prediction_interval_high=-0.02,
            case_category=CaseCategory.NEGATIVE.value,
        )
        rv.resolve_case(
            case,
            actual_result=-0.06,
            actual_result_date=_ACTUAL_DATE,
        )
        result = rv._compute_metrics(case)

        assert result.direction_correct is True
        assert result.interval_covered is True
        assert result.is_failure is False

    def test_no_significant_case_within_interval(self):
        """无显著案例：实际落在区间内 -> 方向正确 + 区间覆盖。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600002.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            prediction_value=0.01,
            prediction_interval_low=-0.005,
            prediction_interval_high=0.015,
            case_category=CaseCategory.NO_SIGNIFICANT.value,
            selection_reason="Predicted change within noise band",
        )
        rv.resolve_case(
            case,
            actual_result=0.005,
            actual_result_date=_ACTUAL_DATE,
        )
        result = rv._compute_metrics(case)

        assert result.direction_correct is True
        assert result.interval_covered is True
        assert result.is_failure is False

    def test_no_significant_case_outside_interval(self):
        """无显著案例：实际跳出噪声区间 -> 方向错误 + 失败。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600002.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            prediction_value=0.01,
            prediction_interval_low=-0.005,
            prediction_interval_high=0.015,
            case_category=CaseCategory.NO_SIGNIFICANT.value,
        )
        rv.resolve_case(
            case,
            actual_result=0.05,
            actual_result_date=_ACTUAL_DATE,
        )
        result = rv._compute_metrics(case)

        assert result.direction_correct is False
        assert result.interval_covered is False
        assert result.is_failure is True


# =====================================================================
# 2. PIT 检测
# =====================================================================


class TestPITDetection:
    """测试 PIT 过滤拦截未来财报和修订版本。"""

    def test_future_documents_are_blocked(self):
        """PIT 过滤：published_at 晚于 as_of 的文档被拦截。"""
        rv = ForecastRevalidator()
        past_doc = _make_input_document("past_doc")
        future_doc = _make_input_document(
            "future_doc",
            published_at=_FUTURE_PUBLISHED,
            retrieved_at=_FUTURE_RETRIEVED,
            title="future_q1_report",
        )
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            input_documents=[past_doc, future_doc],
            prediction_value=0.10,
            prediction_interval_low=0.05,
            prediction_interval_high=0.15,
            case_category=CaseCategory.POSITIVE.value,
        )

        assert len(case.input_documents) == 1
        assert case.input_documents[0].document_id == "past_doc"
        assert case.pit_blocked_documents == 1

    def test_revision_blocked_when_later_than_as_of(self):
        """PIT 过滤：修订版本（v2）晚于 as_of 时被拦截，仅保留原始 v1。"""
        rv = ForecastRevalidator()
        original = _make_input_document("original_v1")
        revision = _make_input_document(
            "revision_v2",
            published_at=_FUTURE_PUBLISHED,
            retrieved_at=_FUTURE_RETRIEVED,
            title="revised_consensus_v2",
        )
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            input_documents=[original, revision],
            case_category=CaseCategory.POSITIVE.value,
        )

        assert {d.document_id for d in case.input_documents} == {"original_v1"}
        assert case.pit_blocked_documents == 1

    def test_empty_documents_allowed(self):
        """PIT 过滤：空文档列表允许（纯模型案例）。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            input_documents=[],
            case_category=CaseCategory.POSITIVE.value,
        )

        assert case.input_documents == []
        assert case.pit_blocked_documents == 0

    def test_non_finite_prediction_is_rejected(self):
        rv = ForecastRevalidator()
        with pytest.raises(ValueError, match="finite"):
            rv.create_case(
                symbol="600000.SH",
                theme_id="theme_test",
                as_of=_AS_OF,
                prediction_value=float("nan"),
            )

    def test_non_finite_actual_is_rejected(self):
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
        )
        with pytest.raises(ValueError, match="finite"):
            rv.resolve_case(
                case,
                actual_result=float("inf"),
                actual_result_date=_ACTUAL_DATE,
            )

    def test_malformed_document_entries_are_ignored(self):
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            input_documents=[None, _make_input_document("valid")],
        )
        assert [doc.document_id for doc in case.input_documents] == ["valid"]

    def test_invalid_as_of_raises(self):
        """防御性：as_of 不是合法 ISO 时间戳时 raise ValueError。"""
        rv = ForecastRevalidator()
        with pytest.raises(ValueError, match="as_of"):
            rv.create_case(
                symbol="600000.SH",
                theme_id="theme_test",
                as_of="not-a-timestamp",
                case_category=CaseCategory.POSITIVE.value,
            )

    def test_missing_symbol_raises(self):
        """防御性：symbol 缺失时 raise ValueError。"""
        rv = ForecastRevalidator()
        with pytest.raises(ValueError, match="symbol"):
            rv.create_case(
                symbol="",
                theme_id="theme_test",
                as_of=_AS_OF,
                case_category=CaseCategory.POSITIVE.value,
            )

    def test_actual_date_before_as_of_raises(self):
        """PIT 时序防御：actual_result_date 不得早于 case.as_of。"""
        rv = ForecastRevalidator()
        case = rv.create_case(
            symbol="600000.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            case_category=CaseCategory.POSITIVE.value,
        )
        with pytest.raises(ValueError, match="actual_result_date"):
            rv.resolve_case(
                case,
                actual_result=0.05,
                actual_result_date="2025-12-01T00:00:00+00:00",  # 早于 as_of
            )


# =====================================================================
# 3. evaluate_accuracy + 样本不足
# =====================================================================


class TestEvaluateAccuracy:
    """测试 evaluate_accuracy 聚合评估与 insufficient_sample 处理。"""

    def test_insufficient_sample_returns_flag(self):
        """已 resolve 案例数 < MIN_SAMPLE_FOR_ACCURACY -> insufficient_sample=True。"""
        rv = ForecastRevalidator()
        # 只有 2 个案例，低于阈值 5
        cases = [_make_positive_case(actual_result=0.08) for _ in range(2)]
        report = rv.evaluate_accuracy(cases)

        assert report.insufficient_sample is True
        assert report.total_cases == 2
        # 指标全部归零，禁止用小样本宣称结论
        assert report.direction_accuracy == 0.0
        assert report.interval_coverage_rate == 0.0
        assert report.mean_error == 0.0
        assert report.failure_cases == []
        assert report.disclaimer == DISCLAIMER

    def test_no_synthetic_data_claimed_business_passed(self):
        """空输入不得伪造"业务通过"指标。"""
        rv = ForecastRevalidator()
        report = rv.evaluate_accuracy([])

        assert report.insufficient_sample is True
        assert report.total_cases == 0
        # 所有指标必须为 0，不允许默认 1.0 之类的"假装通过"
        assert report.direction_accuracy == 0.0
        assert report.interval_coverage_rate == 0.0
        assert report.mean_error == 0.0
        assert report.failure_cases == []
        # disclaimer 必须存在（不输出投资建议）
        assert report.disclaimer == DISCLAIMER
        assert report.disclaimer == "模拟评价，不构成投资建议"

    def test_malformed_cases_are_skipped(self):
        rv = ForecastRevalidator()
        malformed_case = _make_positive_case(actual_result=0.08)
        malformed_case.actual_result = "bad"
        report = rv.evaluate_accuracy([None, object(), malformed_case])
        assert report.total_cases == 0
        assert report.insufficient_sample is True

    def test_sufficient_sample_computes_metrics(self):
        """样本达到阈值 -> 真实计算指标。"""
        rv = ForecastRevalidator()
        # 5 个正向案例，全部方向正确 + 区间覆盖
        cases = [_make_positive_case(actual_result=0.08) for _ in range(5)]
        report = rv.evaluate_accuracy(cases)

        assert report.insufficient_sample is False
        assert report.total_cases == 5
        assert report.direction_accuracy == 1.0
        assert report.interval_coverage_rate == 1.0
        assert report.mean_error == pytest.approx(0.02)
        assert report.failure_cases == []

    def test_failure_cases_collected(self):
        """失败案例 case_id 必须出现在 failure_cases 中。"""
        rv = ForecastRevalidator()
        # 4 个正确案例 + 1 个方向错误案例
        cases = [
            _make_positive_case(actual_result=0.08, symbol=f"60000{i}.SH")
            for i in range(4)
        ]
        bad_case = rv.create_case(
            symbol="600999.SH",
            theme_id="theme_test",
            as_of=_AS_OF,
            prediction_value=0.10,
            prediction_interval_low=0.05,
            prediction_interval_high=0.15,
            case_category=CaseCategory.POSITIVE.value,
        )
        rv.resolve_case(
            bad_case,
            actual_result=-0.05,  # 方向错 + 区间外
            actual_result_date=_ACTUAL_DATE,
        )
        cases.append(bad_case)

        report = rv.evaluate_accuracy(cases)

        assert report.insufficient_sample is False
        assert report.total_cases == 5
        assert report.direction_accuracy == pytest.approx(4 / 5)
        assert bad_case.case_id in report.failure_cases


# =====================================================================
# 4. SAG 传播 vs 股价反应（分开评价）
# =====================================================================


class TestSAGPropagation:
    """测试 SAG 传播准确率与股价反应相关性分开计算。"""

    def test_separate_propagation_and_price_metrics(self):
        """传播全对，但股价只有部分对齐 -> 两个指标分开体现。"""
        revalidator = SAGPropagationRevalidator()
        sag_claims = [
            SAGClaimReaction(
                claim_id=f"c_{i}",
                expected_direction=1,
                propagation_verified=True,
                verified_at="2026-04-01T00:00:00+00:00",
            )
            for i in range(5)
        ]
        # 5 个 claim 全部传播验证通过；但只有 3 个价格反应方向对齐
        price_reactions = [
            PriceReaction(claim_id="c_0", price_change=0.05, window="20d"),
            PriceReaction(claim_id="c_1", price_change=0.03, window="20d"),
            PriceReaction(claim_id="c_2", price_change=-0.02, window="20d"),  # 反向
            PriceReaction(claim_id="c_3", price_change=0.04, window="20d"),
            PriceReaction(claim_id="c_4", price_change=-0.01, window="20d"),  # 反向
        ]

        report = revalidator.evaluate_sag_vs_price(
            sag_claims=sag_claims, price_reactions=price_reactions
        )

        assert report.insufficient_sample is False
        assert report.total_claims == 5
        # 传播准确率：5/5 = 1.0
        assert report.propagation_accuracy == 1.0
        # 股价反应相关性：3/5 = 0.6
        assert report.price_reaction_correlation == pytest.approx(3 / 5)
        # 失败：传播对了但股价反向 -> c_2, c_4
        assert set(report.failure_cases) == {"c_2", "c_4"}
        assert report.disclaimer == DISCLAIMER

    def test_insufficient_claims_returns_flag(self):
        """claim 数 < MIN_SAMPLE_FOR_PROPAGATION -> insufficient_sample=True。"""
        revalidator = SAGPropagationRevalidator()
        report = revalidator.evaluate_sag_vs_price(
            sag_claims=[], price_reactions=[]
        )

        assert report.insufficient_sample is True
        assert report.total_claims == 0
        assert report.propagation_accuracy == 0.0
        assert report.price_reaction_correlation == 0.0
        assert report.failure_cases == []
        assert report.disclaimer == DISCLAIMER

    def test_malformed_claims_and_reactions_are_skipped(self):
        revalidator = SAGPropagationRevalidator()
        valid_claims = [
            SAGClaimReaction(claim_id=f"c_{i}", expected_direction=1, propagation_verified=True)
            for i in range(5)
        ]
        report = revalidator.evaluate_sag_vs_price(
            sag_claims=[None, *valid_claims],
            price_reactions=[None],
        )
        assert report.total_claims == 5
        assert report.propagation_accuracy == 1.0
        assert report.price_reaction_correlation == 0.0

    def test_unverified_claim_alone_is_failure(self):
        """传播未通过的 claim 即使有股价数据也算失败。"""
        revalidator = SAGPropagationRevalidator()
        sag_claims = [
            SAGClaimReaction(
                claim_id=f"c_{i}",
                expected_direction=1,
                propagation_verified=(i < 4),  # 4 个通过，1 个未通过
            )
            for i in range(5)
        ]
        price_reactions = [
            PriceReaction(claim_id=f"c_{i}", price_change=0.02, window="20d")
            for i in range(5)
        ]

        report = revalidator.evaluate_sag_vs_price(
            sag_claims=sag_claims, price_reactions=price_reactions
        )

        assert report.propagation_accuracy == pytest.approx(4 / 5)
        assert "c_4" in report.failure_cases

    def test_no_price_reactions_reports_zero_correlation(self):
        """无股价数据时相关性为 0.0（不得 NaN）。"""
        revalidator = SAGPropagationRevalidator()
        sag_claims = [
            SAGClaimReaction(
                claim_id=f"c_{i}",
                expected_direction=1,
                propagation_verified=True,
            )
            for i in range(5)
        ]
        report = revalidator.evaluate_sag_vs_price(
            sag_claims=sag_claims, price_reactions=[]
        )

        assert report.total_claims == 5
        assert report.propagation_accuracy == 1.0
        assert report.price_reaction_correlation == 0.0
        # 全部传播通过 -> 无失败案例
        assert report.failure_cases == []

    def test_disclaimer_always_present(self):
        """每份 PropagationReport 必须附 disclaimer。"""
        revalidator = SAGPropagationRevalidator()
        report = revalidator.evaluate_sag_vs_price(
            sag_claims=[], price_reactions=[]
        )
        assert report.disclaimer == DISCLAIMER
        assert report.disclaimer == "模拟评价，不构成投资建议"


# =====================================================================
# 5. 预测准确性 vs Alpha 分开评价（集成 sanity check）
# =====================================================================


class TestSeparateEvaluationAxes:
    """验证 ForecastRevalidator 与 SAGPropagationRevalidator 互相独立。"""

    def test_forecast_revalidator_does_not_touch_sag(self):
        """ForecastRevalidator 不计算 SAG 传播指标。"""
        rv = ForecastRevalidator(sag_kb=object())  # 即使传入 sag_kb 也不使用
        cases = [_make_positive_case(actual_result=0.08) for _ in range(5)]
        report = rv.evaluate_accuracy(cases)

        # AccuracyReport 字段不含 propagation_accuracy / price_reaction_correlation
        assert not hasattr(report, "propagation_accuracy")
        assert not hasattr(report, "price_reaction_correlation")
        assert isinstance(report, AccuracyReport)

    def test_sag_revalidator_does_not_touch_forecast(self):
        """SAGPropagationRevalidator 不计算盈利预测指标。"""
        revalidator = SAGPropagationRevalidator()
        sag_claims = [
            SAGClaimReaction(
                claim_id=f"c_{i}",
                expected_direction=1,
                propagation_verified=True,
            )
            for i in range(5)
        ]
        price_reactions = [
            PriceReaction(claim_id=f"c_{i}", price_change=0.02, window="20d")
            for i in range(5)
        ]
        report = revalidator.evaluate_sag_vs_price(
            sag_claims=sag_claims, price_reactions=price_reactions
        )

        # PropagationReport 字段不含 direction_accuracy / interval_coverage_rate
        assert not hasattr(report, "direction_accuracy")
        assert not hasattr(report, "interval_coverage_rate")
        assert isinstance(report, PropagationReport)

    def test_min_sample_constants_exist(self):
        """常量存在，便于上层文档化阈值。"""
        assert MIN_SAMPLE_FOR_ACCURACY >= 1
        assert MIN_SAMPLE_FOR_PROPAGATION >= 1
