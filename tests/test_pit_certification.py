"""P3.9 大规模历史 PIT 认证工具测试。

测试内容：
- PITCertificationScope 和 PITCertificationResult 数据契约
- universe_builder 能构建至少 300 家公司、20 个行业、5 年的样本（mock 数据）
- pit_runner 的断点续跑（临时目录和 mock 数据）
- leakage_checker 能检测出未来泄漏
- error_attribution 的五类错误归因
- report_generator 同时显示成功与失败
- report_generator 在样本不足时标注「不包装成有效能力」
- market_state_classifier 的三类分类
- 独立复核人签署字段

所有测试用 mock 数据，不依赖真实数据库。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.research.pit_certification.contracts import (
    CertificationStatus,
    CertificationUniverse,
    CompanyCertificationMember,
    IndependentSignOff,
    MarketState,
    PITCertificationReport,
    PITCertificationResult,
    PITCertificationScope,
    PITErrorAttribution,
    PITLeakageCheck,
    compute_results_hash,
)
from core.research.pit_certification.error_attribution import (
    ErrorAttributionResult,
    attribute_error,
)
from core.research.pit_certification.leakage_checker import check_future_leakage
from core.research.pit_certification.market_state_classifier import (
    classify_market_state,
    classify_market_state_by_return,
)
from core.research.pit_certification.pit_runner import run_pit_certification
from core.research.pit_certification.report_generator import (
    generate_certification_report,
    sign_certification_report,
)
from core.research.pit_certification.universe_builder import (
    build_certification_universe,
    compute_available_years,
    validate_universe_scope,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_member(
    symbol: str = "600519.SH",
    company_name: str = "贵州茅台",
    industry: str = "食品饮料",
    sub_industry: str = "白酒",
    listed_at: str = "2001-08-27",
    pit_start: str = "2019-01-01",
    pit_end: str = "2024-12-31",
    available_years: float = 6.0,
    market_states: frozenset[MarketState] | None = None,
) -> CompanyCertificationMember:
    """创建测试用公司成员。"""
    return CompanyCertificationMember(
        symbol=symbol,
        company_name=company_name,
        industry=industry,
        sub_industry=sub_industry,
        listed_at=listed_at,
        pit_start=pit_start,
        pit_end=pit_end,
        available_years=available_years,
        market_states_covered=market_states
        or frozenset({MarketState.UP, MarketState.DOWN, MarketState.SIDEWAYS}),
    )


def _make_result(
    symbol: str = "600519.SH",
    status: CertificationStatus = CertificationStatus.SUCCESS,
    prediction: float = 100.0,
    actual: float = 105.0,
    interval_low: float = 90.0,
    interval_high: float = 110.0,
    consensus: float = 102.0,
    industry: str = "食品饮料",
    pit_time: str = "2023-06-30",
    percentage_error: float | None = None,
    interval_covered: bool | None = None,
) -> PITCertificationResult:
    """创建测试用认证结果。"""
    abs_error = abs(prediction - actual) if prediction is not None and actual is not None else None
    if percentage_error is None and actual and abs_error is not None:
        percentage_error = abs_error / abs(actual)
    if interval_covered is None and interval_low is not None and interval_high is not None and actual is not None:
        interval_covered = interval_low <= actual <= interval_high
    return PITCertificationResult(
        symbol=symbol,
        company_name=f"公司_{symbol}",
        industry=industry,
        pit_time=pit_time,
        status=status,
        prediction_value=prediction,
        prediction_interval_low=interval_low,
        prediction_interval_high=interval_high,
        actual_value=actual,
        consensus_value=consensus,
        absolute_error=abs_error,
        percentage_error=percentage_error,
        interval_covered=interval_covered,
        model_version="test_model_v1",
        policy_version="test_policy_v1",
    )


def _make_large_mock_members(
    num_companies: int = 300,
    num_industries: int = 20,
) -> list[CompanyCertificationMember]:
    """生成大规模 mock 公司列表。"""
    members: list[CompanyCertificationMember] = []
    for i in range(num_companies):
        industry = f"行业_{i % num_industries}"
        states = frozenset({
            MarketState.UP if i % 3 == 0 else MarketState.DOWN if i % 3 == 1 else MarketState.SIDEWAYS,
            MarketState.SIDEWAYS,
        })
        members.append(_make_member(
            symbol=f"{i:06d}.SH",
            company_name=f"公司_{i}",
            industry=industry,
            listed_at="2010-01-01",
            pit_start="2019-01-01",
            pit_end="2024-12-31",
            available_years=6.0,
            market_states=states,
        ))
    return members


# ---------------------------------------------------------------------------
# 数据契约测试
# ---------------------------------------------------------------------------


class TestContracts:
    """PITCertificationScope / Result / Report 数据契约测试。"""

    def test_scope_defaults(self) -> None:
        """默认 scope 满足 P3.9 最低要求：300/20/5。"""
        scope = PITCertificationScope()
        assert scope.min_companies == 300
        assert scope.min_industries == 20
        assert scope.min_years == 5
        assert MarketState.UP in scope.require_market_states
        assert MarketState.DOWN in scope.require_market_states
        assert MarketState.SIDEWAYS in scope.require_market_states

    def test_scope_to_dict_serializable(self) -> None:
        """scope 可序列化为字典。"""
        scope = PITCertificationScope()
        d = scope.to_dict()
        assert d["min_companies"] == 300
        assert "up" in d["require_market_states"]
        assert "down" in d["require_market_states"]
        assert "sideways" in d["require_market_states"]

    def test_result_success_property(self) -> None:
        """is_success 属性正确。"""
        result = _make_result(status=CertificationStatus.SUCCESS)
        assert result.is_success is True
        assert result.is_failure is False

    def test_result_failure_property(self) -> None:
        """is_failure 属性正确。"""
        result = _make_result(status=CertificationStatus.FAILURE)
        assert result.is_success is False
        assert result.is_failure is True

    def test_result_to_dict_serializable(self) -> None:
        """result 可序列化为字典。"""
        result = _make_result()
        d = result.to_dict()
        assert d["symbol"] == "600519.SH"
        assert d["status"] == "success"
        assert d["prediction_value"] == 100.0
        assert d["actual_value"] == 105.0

    def test_report_shows_both_success_and_failure(self) -> None:
        """report 同时显示成功与失败。"""
        report = PITCertificationReport(
            total_success=10,
            total_failure=5,
        )
        assert report.shows_both_success_and_failure is True

    def test_report_significance_warning(self) -> None:
        """report 样本不足警告。"""
        report = PITCertificationReport(
            statistical_significance={"insufficient_sample": True},
        )
        assert report.has_significance_warning is True

    def test_leakage_check_defaults(self) -> None:
        """PITLeakageCheck 默认无泄漏。"""
        check = PITLeakageCheck()
        assert check.has_leakage is False
        assert check.blocked_count == 0

    def test_compute_results_hash_deterministic(self) -> None:
        """相同结果产生相同哈希。"""
        results = [_make_result(symbol="A"), _make_result(symbol="B")]
        hash1 = compute_results_hash(results)
        hash2 = compute_results_hash(results)
        assert hash1 == hash2

    def test_compute_results_hash_changes_on_modification(self) -> None:
        """修改结果后哈希变化。"""
        results = [_make_result(symbol="A")]
        hash1 = compute_results_hash(results)
        results.append(_make_result(symbol="B"))
        hash2 = compute_results_hash(results)
        assert hash1 != hash2

    def test_independent_sign_off_defaults(self) -> None:
        """IndependentSignOff 默认未签署。"""
        sign_off = IndependentSignOff()
        assert sign_off.is_signed is False
        assert sign_off.signer == ""


# ---------------------------------------------------------------------------
# 市场状态分类器测试
# ---------------------------------------------------------------------------


class TestMarketStateClassifier:
    """market_state_classifier 三类分类测试。"""

    def test_classify_up_market(self) -> None:
        """涨幅 >= 10% 分类为 UP。"""
        data = [("2023-01-01", 3000.0), ("2023-12-31", 3500.0)]
        state = classify_market_state(data)
        assert state is MarketState.UP

    def test_classify_down_market(self) -> None:
        """跌幅 <= -10% 分类为 DOWN。"""
        data = [("2023-01-01", 3500.0), ("2023-12-31", 3000.0)]
        state = classify_market_state(data)
        assert state is MarketState.DOWN

    def test_classify_sideways_market(self) -> None:
        """涨跌幅在阈值内分类为 SIDEWAYS。"""
        data = [("2023-01-01", 3000.0), ("2023-12-31", 3100.0)]
        state = classify_market_state(data)
        assert state is MarketState.SIDEWAYS

    def test_classify_with_time_window(self) -> None:
        """时间窗口过滤后正确分类。"""
        data = [
            ("2022-01-01", 3000.0),
            ("2023-06-30", 3500.0),
            ("2023-12-31", 3400.0),
        ]
        state = classify_market_state(data, time_window=("2023-01-01", "2023-12-31"))
        # 窗口内 3500 -> 3400，跌幅约 2.86%，SIDEWAYS
        assert state is MarketState.SIDEWAYS

    def test_classify_empty_raises(self) -> None:
        """空数据抛 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            classify_market_state([])

    def test_classify_single_point_sideways(self) -> None:
        """单点数据降级为 SIDEWAYS。"""
        state = classify_market_state([("2023-01-01", 3000.0)])
        assert state is MarketState.SIDEWAYS

    def test_classify_by_return_up(self) -> None:
        """按收益率分类 UP。"""
        assert classify_market_state_by_return(0.15) is MarketState.UP

    def test_classify_by_return_down(self) -> None:
        """按收益率分类 DOWN。"""
        assert classify_market_state_by_return(-0.15) is MarketState.DOWN

    def test_classify_by_return_sideways(self) -> None:
        """按收益率分类 SIDEWAYS。"""
        assert classify_market_state_by_return(0.05) is MarketState.SIDEWAYS

    def test_classify_by_return_nan(self) -> None:
        """NaN 降级为 SIDEWAYS。"""
        import math
        assert classify_market_state_by_return(math.nan) is MarketState.SIDEWAYS


# ---------------------------------------------------------------------------
# 认证样本构建器测试
# ---------------------------------------------------------------------------


class TestUniverseBuilder:
    """universe_builder 测试。"""

    def test_build_with_mock_300_companies(self) -> None:
        """能构建至少 300 家公司、20 个行业的样本。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(300, 20)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        assert universe.total_companies == 300
        assert universe.total_industries == 20

    def test_build_industry_distribution(self) -> None:
        """行业分布正确统计。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(300, 20)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        # 300 / 20 = 15 每行业
        for count in universe.industry_distribution.values():
            assert count == 15

    def test_build_market_state_distribution(self) -> None:
        """市场状态分布正确统计。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(300, 20)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        assert universe.market_state_distribution["up"] > 0
        assert universe.market_state_distribution["down"] > 0
        assert universe.market_state_distribution["sideways"] > 0

    def test_build_pit_window(self) -> None:
        """PIT 时间窗口正确计算。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(10, 3)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        assert universe.pit_window_start == "2019-01-01"
        assert universe.pit_window_end == "2024-12-31"

    def test_build_empty_when_no_provider(self) -> None:
        """无 provider 时返回空样本。"""
        scope = PITCertificationScope()
        universe = build_certification_universe(scope, company_provider=None)
        assert universe.total_companies == 0

    def test_validate_scope_satisfied(self) -> None:
        """满足范围要求时 all_satisfied=True。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(300, 20)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        validation = validate_universe_scope(universe, scope)
        assert validation["companies_satisfied"] is True
        assert validation["industries_satisfied"] is True
        assert validation["market_states_satisfied"] is True

    def test_validate_scope_not_satisfied(self) -> None:
        """不满足范围要求时 all_satisfied=False。"""
        scope = PITCertificationScope()
        members = _make_large_mock_members(10, 3)
        universe = build_certification_universe(scope, company_provider=lambda: members)
        validation = validate_universe_scope(universe, scope)
        assert validation["companies_satisfied"] is False
        assert validation["all_satisfied"] is False

    def test_compute_available_years(self) -> None:
        """可用年数计算正确。"""
        years = compute_available_years("2019-01-01", "2024-12-31")
        assert years == 6.0

    def test_compute_available_years_empty(self) -> None:
        """空日期返回 0。"""
        assert compute_available_years("", "2024-12-31") == 0.0


# ---------------------------------------------------------------------------
# 未来泄漏检查器测试
# ---------------------------------------------------------------------------


class TestLeakageChecker:
    """leakage_checker 测试。"""

    def test_detect_financial_report_leakage(self) -> None:
        """检测财报发布日期晚于 PIT 时点。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={
                "financial_reports": [
                    {"published_at": "2023-08-15", "period_end": "2023-06-30"},
                ],
            },
        )
        assert result.has_leakage is True
        assert result.blocked_count == 1
        assert result.leakage_items[0]["category"] == "financial_report"

    def test_no_leakage_when_all_before_pit(self) -> None:
        """所有输入早于 PIT 时点时无泄漏。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={
                "financial_reports": [
                    {"published_at": "2023-04-30", "period_end": "2023-03-31"},
                ],
                "analyst_expectations": [
                    {"snapshot_date": "2023-05-15"},
                ],
                "industry_data": [
                    {"available_at": "2023-04-01"},
                ],
            },
        )
        assert result.has_leakage is False
        assert result.blocked_count == 0

    def test_detect_analyst_expectation_leakage(self) -> None:
        """检测分析师预期快照日期晚于 PIT 时点。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={
                "analyst_expectations": [
                    {"snapshot_date": "2023-07-01"},
                ],
            },
        )
        assert result.has_leakage is True
        assert result.leakage_items[0]["category"] == "analyst_expectation"

    def test_detect_industry_data_leakage(self) -> None:
        """检测行业数据可用日期晚于 PIT 时点。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={
                "industry_data": [
                    {"available_at": "2023-08-01"},
                ],
            },
        )
        assert result.has_leakage is True
        assert result.leakage_items[0]["category"] == "industry_data"

    def test_detect_model_version_not_yet_effective(self) -> None:
        """检测模型版本尚未生效。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={},
            model_version_info={
                "version": "v2.0",
                "effective_from": "2023-09-01",
                "effective_to": "",
            },
        )
        assert result.has_leakage is True
        assert result.leakage_items[0]["category"] == "model_version"

    def test_detect_model_version_expired(self) -> None:
        """检测模型版本已过期。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={},
            model_version_info={
                "version": "v1.0",
                "effective_from": "2020-01-01",
                "effective_to": "2023-05-01",
            },
        )
        assert result.has_leakage is True

    def test_detect_missing_timestamp(self) -> None:
        """缺少时间戳视为泄漏（时间未知数据不得进入历史测算）。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={
                "financial_reports": [
                    {"period_end": "2023-03-31"},  # 无 published_at
                ],
            },
        )
        assert result.has_leakage is True

    def test_no_leakage_with_valid_model_version(self) -> None:
        """模型版本在有效期内无泄漏。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="2023-06-30",
            inputs={},
            model_version_info={
                "version": "v1.0",
                "effective_from": "2020-01-01",
                "effective_to": "",
            },
        )
        assert result.has_leakage is False

    def test_invalid_pit_time_returns_no_leakage(self) -> None:
        """无效 PIT 时点返回无泄漏（降级处理）。"""
        result = check_future_leakage(
            symbol="600519.SH",
            pit_time="invalid-date",
            inputs={"financial_reports": [{"published_at": "2023-08-15"}]},
        )
        assert result.has_leakage is False
        assert result.checked_count == 0


# ---------------------------------------------------------------------------
# 错误归因器测试
# ---------------------------------------------------------------------------


class TestErrorAttribution:
    """error_attribution 五类错误归因测试。"""

    def test_timing_error_attribution(self) -> None:
        """检测到泄漏时归因为 TIMING。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            leakage_detected=True,
        )
        assert result.attribution is PITErrorAttribution.TIMING
        assert result.confidence == 0.95

    def test_data_error_attribution(self) -> None:
        """有数据质量问题时归因为 DATA。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            data_quality_issues=["营收数据缺失", "利润数据陈旧"],
        )
        assert result.attribution is PITErrorAttribution.DATA
        assert "营收数据缺失" in result.detail

    def test_mapping_error_attribution(self) -> None:
        """有概念映射错误时归因为 MAPPING。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            mapping_errors=["行业归属错误：应为消费而非科技"],
        )
        assert result.attribution is PITErrorAttribution.MAPPING

    def test_assumption_error_attribution(self) -> None:
        """有假设错误时归因为 ASSUMPTION。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            assumption_errors=["假设 ASP=100，实际 ASP=85"],
        )
        assert result.attribution is PITErrorAttribution.ASSUMPTION

    def test_model_error_attribution(self) -> None:
        """排除其他类型后归因为 MODEL。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
        )
        assert result.attribution is PITErrorAttribution.MODEL
        assert "已排除" in result.detail

    def test_no_error_no_attribution(self) -> None:
        """无预测误差时不归因。"""
        result = attribute_error(
            prediction=100.0,
            actual=100.0,
            model_version="v1.0",
        )
        assert result.confidence == 0.0

    def test_attribution_priority_timing_over_data(self) -> None:
        """TIMING 优先于 DATA。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            leakage_detected=True,
            data_quality_issues=["数据缺失"],
        )
        assert result.attribution is PITErrorAttribution.TIMING

    def test_attribution_priority_data_over_mapping(self) -> None:
        """DATA 优先于 MAPPING。"""
        result = attribute_error(
            prediction=100.0,
            actual=90.0,
            model_version="v1.0",
            data_quality_issues=["数据缺失"],
            mapping_errors=["映射错误"],
        )
        assert result.attribution is PITErrorAttribution.DATA


# ---------------------------------------------------------------------------
# PIT 认证执行器测试
# ---------------------------------------------------------------------------


class TestPITRunner:
    """pit_runner 断点续跑和并行测试。"""

    def test_run_basic(self, tmp_path: Path) -> None:
        """基本运行能产出结果。"""
        members = [
            _make_member(symbol="000001.SH"),
            _make_member(symbol="000002.SH"),
        ]
        pit_times = ["2023-06-30"]

        def runner(member: CompanyCertificationMember, pit_time: str) -> PITCertificationResult:
            return _make_result(symbol=member.symbol, pit_time=pit_time)

        results = run_pit_certification(
            scope=PITCertificationScope(min_companies=2, min_industries=1, min_years=1),
            checkpoint_dir=tmp_path / "checkpoint",
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            resume=False,
        )
        assert len(results) == 2

    def test_checkpoint_resume(self, tmp_path: Path) -> None:
        """断点续跑：第二次运行跳过已完成任务。"""
        members = [
            _make_member(symbol="000001.SH"),
            _make_member(symbol="000002.SH"),
            _make_member(symbol="000003.SH"),
        ]
        pit_times = ["2023-06-30"]

        call_count = 0

        def runner(member: CompanyCertificationMember, pit_time: str) -> PITCertificationResult:
            nonlocal call_count
            call_count += 1
            return _make_result(symbol=member.symbol, pit_time=pit_time)

        checkpoint_dir = tmp_path / "checkpoint"

        # 第一次运行
        results1 = run_pit_certification(
            scope=PITCertificationScope(min_companies=3, min_industries=1, min_years=1),
            checkpoint_dir=checkpoint_dir,
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            resume=False,
        )
        assert len(results1) == 3
        assert call_count == 3

        # 第二次运行（resume=True），应跳过已完成任务
        results2 = run_pit_certification(
            scope=PITCertificationScope(min_companies=3, min_industries=1, min_years=1),
            checkpoint_dir=checkpoint_dir,
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            resume=True,
        )
        assert len(results2) == 3
        assert call_count == 3  # 没有新的调用

    def test_no_selective_deletion(self, tmp_path: Path) -> None:
        """失败样本不被删除。"""
        members = [
            _make_member(symbol="SUCCESS.SH"),
            _make_member(symbol="FAILURE.SH"),
        ]
        pit_times = ["2023-06-30"]

        def runner(member: CompanyCertificationMember, pit_time: str) -> PITCertificationResult:
            if member.symbol == "FAILURE.SH":
                return _make_result(
                    symbol=member.symbol,
                    pit_time=pit_time,
                    status=CertificationStatus.FAILURE,
                    prediction=100.0,
                    actual=200.0,
                )
            return _make_result(symbol=member.symbol, pit_time=pit_time)

        results = run_pit_certification(
            scope=PITCertificationScope(min_companies=2, min_industries=1, min_years=1),
            checkpoint_dir=tmp_path / "checkpoint",
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            resume=False,
        )
        # 失败样本仍然保留
        symbols = [r.symbol for r in results]
        assert "SUCCESS.SH" in symbols
        assert "FAILURE.SH" in symbols

    def test_leakage_detected_skips_prediction(self, tmp_path: Path) -> None:
        """检测到泄漏时不执行预测。"""
        members = [_make_member(symbol="000001.SH")]
        pit_times = ["2023-06-30"]

        runner_called = False

        def runner(member: CompanyCertificationMember, pit_time: str) -> PITCertificationResult:
            nonlocal runner_called
            runner_called = True
            return _make_result(symbol=member.symbol, pit_time=pit_time)

        def leakage_provider(
            member: CompanyCertificationMember, pit_time: str,
        ) -> dict:
            return {
                "financial_reports": [
                    {"published_at": "2023-08-15"},  # 晚于 pit_time
                ],
            }

        results = run_pit_certification(
            scope=PITCertificationScope(min_companies=1, min_industries=1, min_years=1),
            checkpoint_dir=tmp_path / "checkpoint",
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            leakage_inputs_provider=leakage_provider,
            resume=False,
        )
        assert len(results) == 1
        assert results[0].status is CertificationStatus.LEAKAGE_DETECTED
        assert runner_called is False

    def test_parallel_execution(self, tmp_path: Path) -> None:
        """并行执行能产出正确结果数。"""
        members = [_make_member(symbol=f"{i:06d}.SH") for i in range(20)]
        pit_times = ["2023-06-30", "2023-12-31"]

        def runner(member: CompanyCertificationMember, pit_time: str) -> PITCertificationResult:
            return _make_result(symbol=member.symbol, pit_time=pit_time)

        results = run_pit_certification(
            scope=PITCertificationScope(min_companies=20, min_industries=1, min_years=1),
            checkpoint_dir=tmp_path / "checkpoint",
            members=members,
            pit_times=pit_times,
            prediction_runner=runner,
            max_workers=4,
            resume=False,
        )
        assert len(results) == 40  # 20 companies × 2 times


# ---------------------------------------------------------------------------
# 报告生成器测试
# ---------------------------------------------------------------------------


class TestReportGenerator:
    """report_generator 测试。"""

    def test_shows_both_success_and_failure(self) -> None:
        """报告同时显示成功与失败。"""
        results = [
            _make_result(symbol="A", status=CertificationStatus.SUCCESS),
            _make_result(symbol="B", status=CertificationStatus.FAILURE, actual=200.0),
        ]
        report = generate_certification_report(results)
        assert report.total_success == 1
        assert report.total_failure == 1
        assert report.shows_both_success_and_failure is True

    def test_insufficient_sample_warning(self) -> None:
        """样本不足时标注「不包装成有效能力」。"""
        results = [
            _make_result(symbol="A", status=CertificationStatus.SUCCESS),
        ]
        scope = PITCertificationScope(min_sample_for_significance=30)
        report = generate_certification_report(results, scope)
        assert report.has_significance_warning is True
        assert "不包装成有效能力" in report.statistical_significance["warning"]

    def test_sufficient_sample_no_warning(self) -> None:
        """样本充足时不标注警告。"""
        results = [
            _make_result(symbol=f"C{i}", status=CertificationStatus.SUCCESS)
            for i in range(35)
        ] + [
            _make_result(symbol=f"F{i}", status=CertificationStatus.FAILURE, actual=200.0)
            for i in range(5)
        ]
        scope = PITCertificationScope(min_sample_for_significance=30)
        report = generate_certification_report(results, scope)
        assert report.has_significance_warning is False

    def test_report_contains_all_eight_dimensions(self) -> None:
        """报告包含全部 8 个认证维度。"""
        results = [_make_result(symbol="A")]
        report = generate_certification_report(results)
        assert "total_symbols" in report.data_coverage
        assert "leakage_in_financials" in report.statement_adjustment
        assert "mape" in report.forecast_error
        assert "avg_consensus_relative_error" in report.consensus_error
        assert "industry_breakdown" in report.industry_model_applicability
        assert "total_companies_in_funnel" in report.funnel_stability
        assert "total_vp_formed" in report.vp_analysis
        assert "distribution" in report.error_attribution_distribution

    def test_error_attribution_distribution(self) -> None:
        """错误归因分布正确统计。"""
        results = [
            _make_result(
                symbol="A", status=CertificationStatus.FAILURE, actual=200.0,
            ),
            _make_result(
                symbol="B", status=CertificationStatus.FAILURE, actual=300.0,
            ),
        ]
        # 手动设置错误归因
        results[0].error_attribution = PITErrorAttribution.MODEL
        results[1].error_attribution = PITErrorAttribution.DATA
        report = generate_certification_report(results)
        dist = report.error_attribution_distribution["distribution"]
        assert dist.get("model") == 1
        assert dist.get("data") == 1

    def test_hard_gates(self) -> None:
        """硬门槛检查正确。"""
        results = [
            _make_result(symbol="A", status=CertificationStatus.SUCCESS),
            _make_result(symbol="B", status=CertificationStatus.FAILURE, actual=200.0),
        ]
        report = generate_certification_report(results)
        assert report.hard_gates["no_known_leakage"] is True
        assert report.hard_gates["shows_both_success_and_failure"] is True
        assert report.hard_gates["no_selective_deletion"] is True


# ---------------------------------------------------------------------------
# 独立复核人签署测试
# ---------------------------------------------------------------------------


class TestIndependentSignOff:
    """独立复核人签署字段测试。"""

    def test_sign_report_success(self) -> None:
        """独立复核人成功签署。"""
        results = [
            _make_result(symbol="A", status=CertificationStatus.SUCCESS),
            _make_result(symbol="B", status=CertificationStatus.FAILURE, actual=200.0),
        ]
        report = generate_certification_report(results)
        signed = sign_certification_report(
            report=report,
            results=results,
            signer="reviewer-A",
            model_author="analyst-B",
        )
        assert signed.sign_off.is_signed is True
        assert signed.sign_off.signer == "reviewer-A"
        assert signed.sign_off.signature_hash != ""
        assert signed.sign_off.sign_time != ""

    def test_sign_rejected_same_person(self) -> None:
        """签署人与模型作者相同时被拒绝。"""
        results = [_make_result(symbol="A")]
        report = generate_certification_report(results)
        with pytest.raises(ValueError, match="不能为同一人"):
            sign_certification_report(
                report=report,
                results=results,
                signer="analyst-A",
                model_author="analyst-A",
            )

    def test_sign_rejected_empty_signer(self) -> None:
        """签署人为空时被拒绝。"""
        results = [_make_result(symbol="A")]
        report = generate_certification_report(results)
        with pytest.raises(ValueError, match="不能为空"):
            sign_certification_report(
                report=report,
                results=results,
                signer="",
                model_author="analyst-A",
            )

    def test_signature_hash_covers_all_results(self) -> None:
        """签署哈希覆盖全部结果。"""
        results = [
            _make_result(symbol="A", status=CertificationStatus.SUCCESS),
            _make_result(symbol="B", status=CertificationStatus.FAILURE, actual=200.0),
        ]
        report = generate_certification_report(results)
        signed = sign_certification_report(
            report=report,
            results=results,
            signer="reviewer-A",
            model_author="analyst-B",
        )
        expected_hash = compute_results_hash(results)
        assert signed.sign_off.signature_hash == expected_hash

    def test_signature_hash_changes_on_result_modification(self) -> None:
        """修改结果后签署哈希变化。"""
        results = [_make_result(symbol="A")]
        report1 = generate_certification_report(results)
        signed1 = sign_certification_report(
            report=report1, results=results,
            signer="reviewer-A", model_author="analyst-B",
        )

        results.append(_make_result(symbol="B"))
        report2 = generate_certification_report(results)
        signed2 = sign_certification_report(
            report=report2, results=results,
            signer="reviewer-A", model_author="analyst-B",
        )
        assert signed1.sign_off.signature_hash != signed2.sign_off.signature_hash

    def test_unsigned_report_default(self) -> None:
        """未签署的报告默认 is_signed=False。"""
        results = [_make_result(symbol="A")]
        report = generate_certification_report(results)
        assert report.sign_off.is_signed is False
        assert report.sign_off.signer == ""
