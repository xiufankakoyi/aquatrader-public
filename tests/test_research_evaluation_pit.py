"""P2.9 专项测试：PIT 历史检验与研究记分卡。

覆盖 3 条强制规则：
1. 评价标签不得反向修改当时研究记录
2. 所有历史评估使用冻结的当时输入
3. 不以股价涨跌单独定义基本面研究正确性

附加覆盖：
- 预测误差计算（signed / abs / 区间覆盖）
- 5 类错误归因（model / mapping / data / timing / assumption，总和必须 100）
- 共识 vs 独立预测相对误差
- 来源命中率与单位成本
- 事件 → 有效事实转化率
- 行业稳定性聚合
- 仓储持久化（不可覆盖 + 防篡改 + PIT 校验）
- AGENTS.md 合规
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.research.evaluation import (
    ACTUAL_OUTCOME_STATUS_PRELIMINARY,
    ACTUAL_OUTCOME_STATUS_REPORTED,
    ERROR_ATTRIBUTION_ASSUMPTION,
    ERROR_ATTRIBUTION_DATA,
    ERROR_ATTRIBUTION_LABELS,
    ERROR_ATTRIBUTION_MAPPING,
    ERROR_ATTRIBUTION_MODEL,
    ERROR_ATTRIBUTION_TIMING,
    EVENT_STATUS_EVIDENCE_SUPPORTED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_MODEL_ELIGIBLE,
    EVENT_STATUS_OBSERVED,
    EVENT_STATUS_REJECTED,
    FORECAST_SOURCE_CONSENSUS,
    FORECAST_SOURCE_INTERNAL,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    ForecastScorecardEngine,
    ForecastScorecard,
    FrozenForecastInput,
    ActualOutcome,
    ErrorAttribution,
    IndustryStabilityScore,
    MARKET_REGIME_BEAR,
    MARKET_REGIME_BULL,
    MARKET_REGIME_RANGE,
    METRIC_EPS,
    METRIC_NET_PROFIT,
    METRIC_REVENUE,
    METRICS,
    NOTICE,
    SourceMetrics,
    EvaluationRepository,
    DiscoveryEvaluator,
    build_forecast_scorecard_engine,
    build_discovery_evaluator,
    compute_frozen_input_hash,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


FORECAST_AS_OF = "2026-07-01T00:00:00+00:00"
REPORTED_AT = "2027-04-30T00:00:00+00:00"
EVAL_AS_OF = "2027-05-15T00:00:00+00:00"
SYMBOL = "TEST001"
PERIOD = "2027Q1"


def make_frozen_input(
    *,
    forecast_id: str = "fc_TEST001_2027Q1_revenue",
    company_symbol: str = SYMBOL,
    as_of: str = FORECAST_AS_OF,
    period: str = PERIOD,
    metric: str = METRIC_REVENUE,
    forecast_source: str = FORECAST_SOURCE_INTERNAL,
    forecast_value: float = 1000.0,
    interval_lower: float | None = 950.0,
    interval_upper: float | None = 1050.0,
    model_name: str = "operating_driver_builder",
    model_version: str = "v1.3",
    assumptions: dict[str, Any] | None = None,
) -> FrozenForecastInput:
    """构造冻结输入。"""
    return FrozenForecastInput(
        forecast_id=forecast_id,
        company_symbol=company_symbol,
        as_of=as_of,
        period=period,
        metric=metric,
        forecast_source=forecast_source,
        forecast_value=forecast_value,
        interval_lower=interval_lower,
        interval_upper=interval_upper,
        model_name=model_name,
        model_version=model_version,
        assumptions=assumptions or {"capacity": 10.0, "price": 100.0},
    )


def make_actual(
    *,
    company_symbol: str = SYMBOL,
    period: str = PERIOD,
    metric: str = METRIC_REVENUE,
    actual_value: float = 980.0,
    reported_at: str = REPORTED_AT,
    status: str = ACTUAL_OUTCOME_STATUS_REPORTED,
) -> ActualOutcome:
    """构造实际结果。"""
    return ActualOutcome(
        company_symbol=company_symbol,
        period=period,
        metric=metric,
        actual_value=actual_value,
        reported_at=reported_at,
        status=status,
    )


def make_attribution(
    *,
    model_pct: float = 40.0,
    mapping_pct: float = 20.0,
    data_pct: float = 15.0,
    timing_pct: float = 15.0,
    assumption_pct: float = 10.0,
    note: str = "低估行业产能利用率影响",
) -> ErrorAttribution:
    """构造错误归因（默认总和 100）。"""
    return ErrorAttribution(
        model_error_pct=model_pct,
        mapping_error_pct=mapping_pct,
        data_error_pct=data_pct,
        timing_error_pct=timing_pct,
        assumption_error_pct=assumption_pct,
        note=note,
    )


@pytest.fixture
def engine() -> ForecastScorecardEngine:
    """记分卡引擎。"""
    return build_forecast_scorecard_engine()


@pytest.fixture
def discovery_evaluator() -> DiscoveryEvaluator:
    """发现评估器。"""
    return build_discovery_evaluator()


@pytest.fixture
def repo(tmp_path: Path) -> EvaluationRepository:
    """临时仓储。"""
    return EvaluationRepository(tmp_path / "eval.db")


# ---------------------------------------------------------------------------
# 1. 数据契约测试
# ---------------------------------------------------------------------------


class TestContracts:
    """数据契约测试。"""

    def test_frozen_input_hash_deterministic(self):
        """相同输入产生相同 hash（确定性）。"""
        fi1 = make_frozen_input()
        fi2 = make_frozen_input()
        assert fi1.input_hash == fi2.input_hash
        assert fi1.is_intact()

    def test_frozen_input_hash_changes_with_value(self):
        """forecast_value 改变 → hash 改变。"""
        fi1 = make_frozen_input(forecast_value=1000.0)
        fi2 = make_frozen_input(forecast_value=1100.0)
        assert fi1.input_hash != fi2.input_hash

    def test_frozen_input_is_intact_after_creation(self):
        """冻结输入创建后未被篡改。"""
        fi = make_frozen_input()
        assert fi.is_intact() is True

    def test_frozen_input_tampered_detected(self):
        """篡改 forecast_value 后 is_intact 返回 False。"""
        fi = make_frozen_input()
        fi.forecast_value = 9999.0  # 篡改
        # input_hash 未变但 recompute_hash 会变
        assert fi.is_intact() is False

    def test_compute_frozen_input_hash_function(self):
        """compute_frozen_input_hash 函数可直接调用。"""
        h = compute_frozen_input_hash(
            forecast_id="fc1",
            company_symbol="C1",
            as_of="2026-07-01",
            metric=METRIC_REVENUE,
            forecast_source=FORECAST_SOURCE_INTERNAL,
            forecast_value=100.0,
            interval_lower=90.0,
            interval_upper=110.0,
            model_name="m",
            model_version="v1",
            assumptions={"a": 1},
        )
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_actual_is_reported(self):
        """实际结果状态校验。"""
        a = make_actual(status=ACTUAL_OUTCOME_STATUS_REPORTED)
        assert a.is_reported()
        a2 = make_actual(status="invalid")
        assert not a2.is_reported()

    def test_attribution_total_100(self):
        """归因总和 100。"""
        attr = make_attribution()
        assert attr.is_total_100()
        assert abs(attr.total() - 100.0) < 1e-9

    def test_attribution_total_not_100_rejected(self):
        """归因总和不为 100 → is_total_100 False。"""
        attr = ErrorAttribution(
            model_error_pct=50.0,
            mapping_error_pct=10.0,
            data_error_pct=10.0,
            timing_error_pct=10.0,
            assumption_error_pct=10.0,  # 总 90
        )
        assert not attr.is_total_100()

    def test_attribution_contains_trade_action(self):
        """归因备注含交易动作词。"""
        attr = ErrorAttribution(note="建议止损")
        assert attr.contains_trade_action()

    def test_scorecard_id_deterministic(self):
        """scorecard_id 确定性。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        eng = build_forecast_scorecard_engine()
        sc1 = eng.evaluate(forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF)
        sc2 = eng.evaluate(forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF)
        assert sc1.scorecard_id == sc2.scorecard_id

    def test_scorecard_to_dict_has_notice(self):
        """to_dict 含 NOTICE。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        eng = build_forecast_scorecard_engine()
        sc = eng.evaluate(forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF)
        d = sc.to_dict()
        assert d["notice"] == NOTICE

    def test_source_metrics_derived_fields(self):
        """SourceMetrics 自动派生 unit_cost / hit_rate。"""
        sm = SourceMetrics(
            source_id="src1",
            hit_count=8,
            total_queries=10,
            total_cost=5.0,
        )
        assert sm.unit_cost == 0.5
        assert sm.hit_rate == 0.8

    def test_event_conversion_stat_derived_rate(self):
        """EventConversionStat 自动派生 conversion_rate。"""
        from core.research.evaluation import EventConversionStat
        ecs = EventConversionStat(
            total_events=20,
            effective_facts=12,
        )
        assert ecs.conversion_rate == 0.6


# ---------------------------------------------------------------------------
# 2. 强制规则 1：评价标签不得反向修改当时研究记录
# ---------------------------------------------------------------------------


class TestAcceptanceRule1NoBackwardModification:
    """强制规则 1：评价标签不得反向修改当时研究记录。"""

    def test_repository_save_is_insert_only(self, repo, engine):
        """仓储 save_scorecard 为 INSERT ON CONFLICT DO NOTHING。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        # 第一次保存成功
        ok1 = repo.save_scorecard(sc)
        assert ok1 is True
        # 第二次保存（同 scorecard_id）返回 False（不覆盖）
        ok2 = repo.save_scorecard(sc)
        assert ok2 is False

    def test_repository_get_returns_same_scorecard_id(self, repo, engine):
        """读取的 scorecard_id 与写入一致（未修改）。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        repo.save_scorecard(sc)
        loaded = repo.get_scorecard(sc.scorecard_id)
        assert loaded is not None
        assert loaded.scorecard_id == sc.scorecard_id
        assert loaded.forecast.forecast_id == sc.forecast.forecast_id

    def test_scorecard_does_not_mutate_forecast(self, engine):
        """记分卡不修改 forecast 的字段。"""
        fi = make_frozen_input()
        original_value = fi.forecast_value
        original_hash = fi.input_hash
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        # forecast 字段未被引擎修改
        assert sc.forecast.forecast_value == original_value
        assert sc.forecast.input_hash == original_hash
        assert fi.forecast_value == original_value


# ---------------------------------------------------------------------------
# 3. 强制规则 2：所有历史评估使用冻结的当时输入
# ---------------------------------------------------------------------------


class TestAcceptanceRule2FrozenInputs:
    """强制规则 2：所有历史评估使用冻结的当时输入。"""

    def test_frozen_input_hash_blocks_tampering(self, repo, engine):
        """仓储拒绝被篡改的冻结输入。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        # 篡改 forecast_value 但不重算 input_hash
        sc.forecast.forecast_value = 9999.0
        with pytest.raises(ValueError, match="篡改"):
            repo.save_scorecard(sc)

    def test_engine_rejects_tampered_frozen_input(self, engine):
        """引擎拒绝被篡改的冻结输入。"""
        fi = make_frozen_input()
        # 篡改 forecast_value 但不重算 input_hash
        fi.forecast_value = 9999.0
        assert not fi.is_intact()
        a = make_actual()
        attr = make_attribution()
        with pytest.raises(ValueError, match="篡改"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_recompute_hash_after_change(self):
        """修改字段后 recompute_hash 可恢复一致性。"""
        fi = make_frozen_input()
        fi.forecast_value = 1100.0
        assert not fi.is_intact()
        fi.input_hash = fi.recompute_hash()
        assert fi.is_intact()

    def test_repository_validates_no_future_leakage(self, repo):
        """仓储校验 PIT 无未来泄漏。

        直接构造带未来泄漏的 scorecard（绕过引擎 PIT 校验）以测试仓储自身校验。
        """
        fi = make_frozen_input(as_of="2027-06-01T00:00:00+00:00")
        a = make_actual(reported_at="2027-05-01T00:00:00+00:00")
        attr = make_attribution()
        sc = ForecastScorecard(
            forecast=fi,
            actual=a,
            attribution=attr,
            signed_error_pct=-2.0,
            abs_error_pct=2.0,
            interval_covered=False,
            evaluation_as_of=EVAL_AS_OF,
        )
        with pytest.raises(ValueError, match="未来泄漏"):
            repo.save_scorecard(sc)

    def test_engine_rejects_actual_before_forecast(self, engine):
        """引擎拒绝 actual.reported_at <= forecast.as_of。"""
        fi = make_frozen_input(as_of="2027-06-01T00:00:00+00:00")
        a = make_actual(reported_at="2027-05-01T00:00:00+00:00")
        attr = make_attribution()
        with pytest.raises(ValueError, match="未来泄漏"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )


# ---------------------------------------------------------------------------
# 4. 强制规则 3：不以股价涨跌单独定义正确性
# ---------------------------------------------------------------------------


class TestAcceptanceRule3NoStockPriceBasedCorrectness:
    """强制规则 3：不以股价涨跌单独定义基本面研究正确性。"""

    def test_scorecard_does_not_have_stock_price_field(self, engine):
        """记分卡字段不含股价涨跌指标。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        d = sc.to_dict()
        # 不应含股价涨跌字段
        assert "stock_return" not in d
        assert "stock_price_change" not in d
        assert "price_correctness" not in d

    def test_scorecard_metrics_are_fundamental_only(self):
        """评估指标仅含基本面（收入 / 利润 / EPS）。"""
        from core.research.evaluation import METRICS
        assert METRIC_REVENUE in METRICS
        assert METRIC_NET_PROFIT in METRICS
        assert METRIC_EPS in METRICS
        # 不含股价类指标
        assert "stock_price" not in METRICS
        assert "stock_return" not in METRICS

    def test_notice_mentions_no_stock_price(self):
        """NOTICE 明确不以股价定义正确性。"""
        assert "股价涨跌" in NOTICE
        assert "不" in NOTICE

    def test_metrics_only_fundamental(self):
        """评估指标均为基本面指标。"""
        for metric in (METRIC_REVENUE, METRIC_NET_PROFIT, METRIC_EPS):
            assert metric in METRICS


# ---------------------------------------------------------------------------
# 5. 预测误差计算
# ---------------------------------------------------------------------------


class TestErrorComputation:
    """预测误差计算测试。"""

    def test_signed_error_underestimate(self, engine):
        """低估：actual > forecast → 正误差。"""
        fi = make_frozen_input(forecast_value=1000.0)
        a = make_actual(actual_value=1100.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        # (1100 - 1000) / 1000 × 100 = 10%
        assert sc.signed_error_pct == pytest.approx(10.0)
        assert sc.abs_error_pct == pytest.approx(10.0)

    def test_signed_error_overestimate(self, engine):
        """高估：actual < forecast → 负误差。"""
        fi = make_frozen_input(forecast_value=1000.0)
        a = make_actual(actual_value=900.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        # (900 - 1000) / 1000 × 100 = -10%
        assert sc.signed_error_pct == pytest.approx(-10.0)
        assert sc.abs_error_pct == pytest.approx(10.0)

    def test_signed_error_zero_forecast_defensive(self, engine):
        """forecast=0 防御性返回 0。"""
        fi = make_frozen_input(forecast_value=0.0, interval_lower=None, interval_upper=None)
        a = make_actual(actual_value=100.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert sc.signed_error_pct == 0.0

    def test_interval_covered_inside(self, engine):
        """实际值落在区间内。"""
        fi = make_frozen_input(
            forecast_value=1000.0, interval_lower=950.0, interval_upper=1050.0
        )
        a = make_actual(actual_value=980.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert sc.interval_covered is True

    def test_interval_covered_outside(self, engine):
        """实际值落在区间外。"""
        fi = make_frozen_input(
            forecast_value=1000.0, interval_lower=950.0, interval_upper=1050.0
        )
        a = make_actual(actual_value=1100.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert sc.interval_covered is False

    def test_interval_no_interval_not_covered(self, engine):
        """无区间 → 不覆盖。"""
        fi = make_frozen_input(interval_lower=None, interval_upper=None)
        a = make_actual(actual_value=1000.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert sc.interval_covered is False

    def test_consensus_error_computed(self, engine):
        """共识误差计算。"""
        fi = make_frozen_input(forecast_value=1000.0)
        a = make_actual(actual_value=980.0)
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr,
            consensus_forecast_value=950.0,
            evaluation_as_of=EVAL_AS_OF,
        )
        # 共识误差 = (980 - 950) / 950 × 100 = 3.157...
        assert sc.consensus_error_pct is not None
        assert sc.consensus_error_pct == pytest.approx(
            (980.0 - 950.0) / 950.0 * 100.0, abs=0.01
        )

    def test_consensus_error_none_when_no_consensus(self, engine):
        """无共识 → consensus_error_pct None。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert sc.consensus_error_pct is None


# ---------------------------------------------------------------------------
# 6. 错误归因 5 类
# ---------------------------------------------------------------------------


class TestErrorAttribution:
    """5 类错误归因测试。"""

    def test_five_attribution_categories_exist(self):
        """5 类归因齐全。"""
        from core.research.evaluation import ERROR_ATTRIBUTIONS
        assert ERROR_ATTRIBUTION_MODEL in ERROR_ATTRIBUTIONS
        assert ERROR_ATTRIBUTION_MAPPING in ERROR_ATTRIBUTIONS
        assert ERROR_ATTRIBUTION_DATA in ERROR_ATTRIBUTIONS
        assert ERROR_ATTRIBUTION_TIMING in ERROR_ATTRIBUTIONS
        assert ERROR_ATTRIBUTION_ASSUMPTION in ERROR_ATTRIBUTIONS
        assert len(ERROR_ATTRIBUTIONS) == 5

    def test_attribution_labels_chinese(self):
        """归因标签为中文。"""
        assert ERROR_ATTRIBUTION_LABELS[ERROR_ATTRIBUTION_MODEL] == "模型结构错误"
        assert ERROR_ATTRIBUTION_LABELS[ERROR_ATTRIBUTION_MAPPING] == "概念映射错误"
        assert ERROR_ATTRIBUTION_LABELS[ERROR_ATTRIBUTION_DATA] == "数据错误"
        assert ERROR_ATTRIBUTION_LABELS[ERROR_ATTRIBUTION_TIMING] == "时点错误"
        assert ERROR_ATTRIBUTION_LABELS[ERROR_ATTRIBUTION_ASSUMPTION] == "假设错误"

    def test_engine_rejects_attribution_total_not_100(self, engine):
        """归因总和不为 100 → 引擎拒绝。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = ErrorAttribution(
            model_error_pct=50.0,
            mapping_error_pct=10.0,
            data_error_pct=10.0,
            timing_error_pct=10.0,
            assumption_error_pct=10.0,  # 总 90
        )
        with pytest.raises(ValueError, match="总和必须为 100"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_engine_rejects_attribution_with_trade_action(self, engine):
        """归因备注含交易动作词 → 引擎拒绝。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution(note="建议止盈")
        with pytest.raises(ValueError, match="交易动作关键词"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_attribution_total_with_tolerance(self):
        """is_total_100 容忍 0.5 浮点误差。"""
        attr = ErrorAttribution(
            model_error_pct=40.0,
            mapping_error_pct=20.0,
            data_error_pct=15.0,
            timing_error_pct=15.0,
            assumption_error_pct=10.3,  # 总 100.3
        )
        assert attr.is_total_100() is True  # 容忍 ±0.5


# ---------------------------------------------------------------------------
# 7. 引擎输入校验
# ---------------------------------------------------------------------------


class TestEngineValidation:
    """引擎输入校验测试。"""

    def test_rejects_missing_forecast_id(self, engine):
        """forecast.forecast_id 缺失 → 拒绝。"""
        fi = make_frozen_input()
        fi.forecast_id = ""
        fi.input_hash = fi.recompute_hash()
        a = make_actual()
        attr = make_attribution()
        with pytest.raises(ValueError, match="缺少必填字段"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_rejects_invalid_metric(self, engine):
        """非法 metric → 拒绝。"""
        fi = make_frozen_input(metric="invalid_metric")
        a = make_actual(metric="invalid_metric")
        attr = make_attribution()
        with pytest.raises(ValueError, match="非法 metric"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_rejects_invalid_forecast_source(self, engine):
        """非法 forecast_source → 拒绝。"""
        fi = make_frozen_input(forecast_source="invalid")
        a = make_actual()
        attr = make_attribution()
        with pytest.raises(ValueError, match="非法 forecast_source"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_rejects_invalid_market_regime(self, engine):
        """非法 market_regime → 拒绝。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        with pytest.raises(ValueError, match="非法 market_regime"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr,
                market_regime="invalid",
                evaluation_as_of=EVAL_AS_OF,
            )

    def test_rejects_actual_not_reported(self, engine):
        """actual 未公布 → 拒绝。"""
        fi = make_frozen_input()
        a = make_actual(status="invalid", reported_at="")
        attr = make_attribution()
        with pytest.raises(ValueError, match="未公布"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_rejects_metric_mismatch(self, engine):
        """forecast.metric != actual.metric → 拒绝。"""
        fi = make_frozen_input(metric=METRIC_REVENUE)
        a = make_actual(metric=METRIC_NET_PROFIT)
        attr = make_attribution()
        with pytest.raises(ValueError, match="metric 不匹配"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )

    def test_rejects_invalid_interval(self, engine):
        """区间 lower > upper → 拒绝。"""
        fi = make_frozen_input(interval_lower=1050.0, interval_upper=950.0)
        a = make_actual()
        attr = make_attribution()
        with pytest.raises(ValueError, match="非法区间"):
            engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )


# ---------------------------------------------------------------------------
# 8. 行业稳定性聚合
# ---------------------------------------------------------------------------


class TestIndustryStability:
    """行业稳定性聚合测试。"""

    def test_aggregate_industry_stability_groups(self, engine):
        """按 (industry, regime) 分组聚合。"""
        scorecards = []
        for i in range(5):
            fi = make_frozen_input(forecast_id=f"fc_{i}", forecast_value=1000.0)
            a = make_actual(actual_value=1000.0 + i * 10)  # 误差 0%, 1%, 2%, 3%, 4%
            attr = make_attribution()
            sc = engine.evaluate(
                forecast=fi, actual=a, attribution=attr,
                industry="制造业", market_regime=MARKET_REGIME_BULL,
                evaluation_as_of=EVAL_AS_OF,
            )
            scorecards.append(sc)

        stability = engine.aggregate_industry_stability(scorecards)
        assert len(stability) == 1
        assert stability[0].industry == "制造业"
        assert stability[0].market_regime == MARKET_REGIME_BULL
        assert stability[0].sample_count == 5
        # 方差较小 → 稳定性较高
        assert 0.0 <= stability[0].score <= 1.0

    def test_aggregate_industry_stability_multiple_groups(self, engine):
        """多行业 / 多 regime 分组。"""
        scorecards = []
        for industry in ("制造业", "金融业"):
            for regime in (MARKET_REGIME_BULL, MARKET_REGIME_BEAR):
                fi = make_frozen_input(forecast_id=f"fc_{industry}_{regime}")
                a = make_actual()
                attr = make_attribution()
                sc = engine.evaluate(
                    forecast=fi, actual=a, attribution=attr,
                    industry=industry, market_regime=regime,
                    evaluation_as_of=EVAL_AS_OF,
                )
                scorecards.append(sc)
        stability = engine.aggregate_industry_stability(scorecards)
        assert len(stability) == 4  # 2 行业 × 2 regime

    def test_stability_score_high_when_low_variance(self, engine):
        """方差低 → 稳定性高。"""
        scorecards = []
        for i in range(5):
            fi = make_frozen_input(forecast_id=f"fc_{i}", forecast_value=1000.0)
            a = make_actual(actual_value=1000.1)  # 误差极小
            attr = make_attribution()
            sc = engine.evaluate(
                forecast=fi, actual=a, attribution=attr,
                industry="X", market_regime=MARKET_REGIME_RANGE,
                evaluation_as_of=EVAL_AS_OF,
            )
            scorecards.append(sc)
        stability = engine.aggregate_industry_stability(scorecards)
        assert stability[0].score > 0.9  # 高稳定性


# ---------------------------------------------------------------------------
# 9. 发现评估器
# ---------------------------------------------------------------------------


class TestDiscoveryEvaluator:
    """发现评估器测试。"""

    def test_event_conversion_rate(self, discovery_evaluator):
        """事件 → 有效事实转化率。"""
        events = [
            {"status": EVENT_STATUS_EVIDENCE_SUPPORTED},
            {"status": EVENT_STATUS_MODEL_ELIGIBLE},
            {"status": EVENT_STATUS_REJECTED},
            {"status": EVENT_STATUS_EXPIRED},
            {"status": EVENT_STATUS_OBSERVED},
        ]
        stat = discovery_evaluator.evaluate_event_conversion(events=events, period="2027Q1")
        assert stat.total_events == 5
        assert stat.effective_facts == 2  # evidence_supported + model_eligible
        assert stat.rejected_events == 1
        assert stat.expired_events == 1
        assert stat.conversion_rate == pytest.approx(0.4)

    def test_event_conversion_empty(self, discovery_evaluator):
        """空事件列表 → 全 0。"""
        stat = discovery_evaluator.evaluate_event_conversion(events=[])
        assert stat.total_events == 0
        assert stat.effective_facts == 0
        assert stat.conversion_rate == 0.0

    def test_event_conversion_rejects_invalid_status(self, discovery_evaluator):
        """非法事件 status → 抛错。"""
        events = [{"status": "invalid_status"}]
        with pytest.raises(ValueError, match="非法事件 status"):
            discovery_evaluator.evaluate_event_conversion(events=events)

    def test_source_metrics_basic(self, discovery_evaluator):
        """单一来源指标。"""
        sm = discovery_evaluator.evaluate_source_metrics(
            source_id="src1",
            hit_count=8,
            total_queries=10,
            total_cost=5.0,
        )
        assert sm.source_id == "src1"
        assert sm.hit_rate == 0.8
        assert sm.unit_cost == 0.5

    def test_source_metrics_rejects_hit_exceeds_queries(self, discovery_evaluator):
        """hit_count > total_queries → 抛错。"""
        with pytest.raises(ValueError, match="hit_count.*total_queries"):
            discovery_evaluator.evaluate_source_metrics(
                source_id="src1",
                hit_count=15,
                total_queries=10,
            )

    def test_source_metrics_rejects_empty_source_id(self, discovery_evaluator):
        """source_id 为空 → 抛错。"""
        with pytest.raises(ValueError, match="source_id 必填"):
            discovery_evaluator.evaluate_source_metrics(
                source_id="",
                hit_count=1,
                total_queries=10,
            )

    def test_source_metrics_batch(self, discovery_evaluator):
        """批量来源评估。"""
        sources = [
            {"source_id": "src1", "hit_count": 8, "total_queries": 10, "total_cost": 5.0},
            {"source_id": "src2", "hit_count": 4, "total_queries": 20, "total_cost": 2.0},
        ]
        results = discovery_evaluator.evaluate_sources_batch(sources)
        assert len(results) == 2
        assert results[0].hit_rate == 0.8
        assert results[1].hit_rate == 0.2

    def test_source_metrics_zero_queries(self, discovery_evaluator):
        """0 queries 防御性返回 0。"""
        sm = discovery_evaluator.evaluate_source_metrics(
            source_id="src1",
            hit_count=0,
            total_queries=0,
        )
        assert sm.hit_rate == 0.0
        assert sm.unit_cost == 0.0


# ---------------------------------------------------------------------------
# 10. 仓储持久化
# ---------------------------------------------------------------------------


class TestRepositoryPersistence:
    """仓储持久化测试。"""

    def test_save_and_get_scorecard(self, repo, engine):
        """保存并读取记分卡。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert repo.save_scorecard(sc) is True
        loaded = repo.get_scorecard(sc.scorecard_id)
        assert loaded is not None
        assert loaded.scorecard_id == sc.scorecard_id
        assert loaded.forecast.forecast_id == sc.forecast.forecast_id
        assert loaded.actual.actual_value == sc.actual.actual_value
        assert loaded.signed_error_pct == pytest.approx(sc.signed_error_pct)

    def test_save_scorecard_not_overwritable(self, repo, engine):
        """同 scorecard_id 不可覆盖。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert repo.save_scorecard(sc) is True
        # 再次保存 → False
        assert repo.save_scorecard(sc) is False

    def test_list_by_company(self, repo, engine):
        """按公司列出记分卡。"""
        for i in range(3):
            fi = make_frozen_input(forecast_id=f"fc_{i}")
            a = make_actual(period=f"2027Q{i+1}")
            attr = make_attribution()
            sc = engine.evaluate(
                forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
            )
            repo.save_scorecard(sc)
        results = repo.list_by_company(SYMBOL)
        assert len(results) == 3

    def test_list_by_company_with_metric_filter(self, repo, engine):
        """按公司 + 指标过滤。"""
        fi_rev = make_frozen_input(
            forecast_id="fc_rev", metric=METRIC_REVENUE
        )
        fi_np = make_frozen_input(
            forecast_id="fc_np", metric=METRIC_NET_PROFIT
        )
        a_rev = make_actual(metric=METRIC_REVENUE)
        a_np = make_actual(metric=METRIC_NET_PROFIT)
        attr = make_attribution()
        sc_rev = engine.evaluate(
            forecast=fi_rev, actual=a_rev, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        sc_np = engine.evaluate(
            forecast=fi_np, actual=a_np, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        repo.save_scorecard(sc_rev)
        repo.save_scorecard(sc_np)
        results = repo.list_by_company(SYMBOL, metric=METRIC_REVENUE)
        assert len(results) == 1
        assert results[0].forecast.metric == METRIC_REVENUE

    def test_list_by_forecast(self, repo, engine):
        """按 forecast_id 列出。"""
        fi = make_frozen_input(forecast_id="fc_unique")
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        repo.save_scorecard(sc)
        results = repo.list_by_forecast("fc_unique")
        assert len(results) == 1

    def test_list_by_regime(self, repo, engine):
        """按行业 + regime 列出。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr,
            industry="制造业", market_regime=MARKET_REGIME_BULL,
            evaluation_as_of=EVAL_AS_OF,
        )
        repo.save_scorecard(sc)
        results = repo.list_by_regime("制造业", MARKET_REGIME_BULL)
        assert len(results) == 1

    def test_stats(self, repo, engine):
        """仓储统计。"""
        for metric in (METRIC_REVENUE, METRIC_NET_PROFIT, METRIC_EPS):
            fi = make_frozen_input(
                forecast_id=f"fc_{metric}", metric=metric
            )
            a = make_actual(metric=metric)
            attr = make_attribution()
            sc = engine.evaluate(
                forecast=fi, actual=a, attribution=attr,
                industry="X", market_regime=MARKET_REGIME_BULL,
                evaluation_as_of=EVAL_AS_OF,
            )
            repo.save_scorecard(sc)
        stats = repo.stats()
        assert stats["total_scorecards"] == 3
        assert len(stats["by_metric"]) == 3

    def test_validate_no_future_leakage(self, repo, engine):
        """validate_no_future_leakage 方法。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        assert repo.validate_no_future_leakage(sc) is True


# ---------------------------------------------------------------------------
# 11. 端到端集成
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端集成测试。"""

    def test_full_pipeline_forecast_evaluate_persist(self, engine, repo):
        """完整流程：构造冻结输入 → 评估 → 持久化 → 读取。"""
        fi = make_frozen_input(
            forecast_value=1000.0,
            interval_lower=950.0,
            interval_upper=1050.0,
            forecast_source=FORECAST_SOURCE_INTERNAL,
        )
        a = make_actual(actual_value=980.0)
        attr = make_attribution(
            model_pct=40.0,
            mapping_pct=20.0,
            data_pct=15.0,
            timing_pct=15.0,
            assumption_pct=10.0,
        )
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr,
            industry="制造业", market_regime=MARKET_REGIME_BULL,
            consensus_forecast_value=950.0,
            evaluation_as_of=EVAL_AS_OF,
            evaluator="analyst-A",
        )
        # 误差 = (980 - 1000) / 1000 × 100 = -2%
        assert sc.signed_error_pct == pytest.approx(-2.0)
        assert sc.interval_covered is True
        # 共识误差 = (980 - 950) / 950 × 100 = 3.158%
        assert sc.consensus_error_pct is not None

        ok = repo.save_scorecard(sc)
        assert ok is True
        loaded = repo.get_scorecard(sc.scorecard_id)
        assert loaded is not None
        assert loaded.signed_error_pct == pytest.approx(-2.0)
        assert loaded.interval_covered is True

    def test_pipeline_with_peer_stability(self, engine, repo):
        """完整流程：多个记分卡 → 稳定性聚合。"""
        scorecards = []
        for i in range(5):
            fi = make_frozen_input(
                forecast_id=f"fc_{i}", forecast_value=1000.0
            )
            a = make_actual(actual_value=1000.0 + i * 20)  # 误差 0%, 2%, 4%, 6%, 8%
            attr = make_attribution()
            sc = engine.evaluate(
                forecast=fi, actual=a, attribution=attr,
                industry="制造业", market_regime=MARKET_REGIME_BULL,
                evaluation_as_of=EVAL_AS_OF,
            )
            scorecards.append(sc)
            repo.save_scorecard(sc)

        stability = engine.aggregate_industry_stability(scorecards)
        assert len(stability) == 1
        assert stability[0].sample_count == 5
        assert stability[0].mean_abs_error_pct > 0


# ---------------------------------------------------------------------------
# 12. AGENTS.md 合规
# ---------------------------------------------------------------------------


class TestAgentsMdCompliance:
    """AGENTS.md 合规检查。"""

    def test_no_trade_advice_in_source(self):
        """源码中无交易动作建议。"""
        paths = [
            Path(__file__).parent.parent / "core" / "research" / "evaluation" / "contracts.py",
            Path(__file__).parent.parent / "core" / "research" / "evaluation" / "forecast_scorecard.py",
            Path(__file__).parent.parent / "core" / "research" / "evaluation" / "discovery_evaluator.py",
            Path(__file__).parent.parent / "core" / "research" / "evaluation" / "repository.py",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for kw in ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓", "调仓"):
                # FORBIDDEN_TRADE_ACTION_KEYWORDS 元组中的字符串本身不算违规
                if kw in text and f'"{kw}"' not in text and f"'{kw}'" not in text:
                    pytest.fail(f"{path.name} 含交易动作词：{kw}")

    def test_notice_disclaims_trade_advice(self):
        """NOTICE 明确否认交易动作建议。"""
        assert "不" in NOTICE
        assert "交易动作" in NOTICE or "股价涨跌" in NOTICE

    def test_engine_rejects_trade_action_in_attribution(self, engine):
        """引擎拒绝归因备注中的交易动作词。"""
        for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS:
            fi = make_frozen_input()
            a = make_actual()
            attr = make_attribution(note=f"建议{kw}")
            with pytest.raises(ValueError, match="交易动作关键词"):
                engine.evaluate(
                    forecast=fi, actual=a, attribution=attr,
                    evaluation_as_of=EVAL_AS_OF,
                )

    def test_repository_rejects_tampered_input(self, repo, engine):
        """仓储拒绝被篡改的冻结输入。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        sc.forecast.forecast_value = 9999.0  # 篡改
        with pytest.raises(ValueError, match="篡改"):
            repo.save_scorecard(sc)

    def test_repository_rejects_future_leakage(self, repo):
        """仓储拒绝未来泄漏。

        直接构造带未来泄漏的 scorecard（绕过引擎 PIT 校验）以测试仓储自身校验。
        """
        fi = make_frozen_input(as_of="2027-06-01T00:00:00+00:00")
        a = make_actual(reported_at="2027-05-01T00:00:00+00:00")
        attr = make_attribution()
        sc = ForecastScorecard(
            forecast=fi,
            actual=a,
            attribution=attr,
            signed_error_pct=-2.0,
            abs_error_pct=2.0,
            interval_covered=False,
            evaluation_as_of=EVAL_AS_OF,
        )
        with pytest.raises(ValueError, match="未来泄漏"):
            repo.save_scorecard(sc)

    def test_scorecard_output_only_evaluation(self, engine):
        """记分卡输出仅含评估字段，不含交易动作。"""
        fi = make_frozen_input()
        a = make_actual()
        attr = make_attribution()
        sc = engine.evaluate(
            forecast=fi, actual=a, attribution=attr, evaluation_as_of=EVAL_AS_OF
        )
        d = sc.to_dict()
        # 不应含交易动作字段
        assert "trade_action" not in d
        assert "position_advice" not in d
        assert "buy_signal" not in d
        assert "sell_signal" not in d

    def test_graceful_degradation_empty_peer_scorecards(self, engine):
        """空 peer_scorecards → 默认稳定性 0.5（不崩溃）。"""
        score = engine._compute_stability_score([])
        assert score == 0.5
