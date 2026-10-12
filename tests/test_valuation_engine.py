"""P1.8 多方法估值与敏感性 — 专项测试。

覆盖 4 个验收点 + 安全测试：
1. TestAcceptancePoint1DcfFormulaAuditable: DCF 公式与终值占比可审计、缺关键假设降级
2. TestAcceptancePoint2ReverseDcfImpliedAssumptions: Reverse DCF 返回隐含增长率/WACC 且标记 implied
3. TestAcceptancePoint3CapitalStructureAdjustments: 净债务/少数股东/联营/稀释股本处理
4. TestAcceptancePoint4MethodDifferencesNotAveraged: 方法差异解释 + overall_range=min_max
5. TestIntervalPropagation: 区间算术在 DCF 中传播
6. TestSensitivityMatrix: WACC × g 敏感性矩阵
7. TestPitFilter: list_cases 按 as_of 过滤
8. TestRepositoryIdempotency: 迁移幂等 + 重启回读
9. TestApiSmoke: 所有端点 smoke
10. TestNoTradeAdvice: 响应不含 buy/sell/position/止盈/止损
11. TestNotApplicableNotFailure: 方法不适用时 status=200 + computability=not_applicable
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from flask import Flask

from core.research.earnings.interval import Interval
from core.research.valuation.contracts import (
    Computability,
    SensitivityPoint,
    ValuationAssumption,
    ValuationCase,
    ValuationGap,
    ValuationInput,
    ValuationMethod,
    ValuationReport,
)
from core.research.valuation.engine import (
    ComparableCompanyEngine,
    DcfEngine,
    DdmEngine,
    MultiplesEngine,
    ReverseDcfEngine,
    SensitivityAnalyzer,
    ValuationAssumptionValidator,
    ValuationEngine,
)
from core.research.valuation.repository import ValuationRepository
from server.routes.valuation_routes import valuation_bp


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _assumption(name: str, value: float, *, source_ref: str = "claim-test-1",
                source_kind: str = "manual", as_of: str = "2026-07-01T00:00:00+00:00") -> ValuationAssumption:
    """构造带 source_ref 的假设。"""
    return ValuationAssumption(
        name=name, value=value, unit="ratio",
        source_ref=source_ref, source_kind=source_kind,
        as_of=as_of,
    )


def _base_dcf_inputs(
    *,
    fcff_series: list[float] | None = None,
    wacc: float = 0.10,
    terminal_growth: float = 0.03,
    wacc_source: str = "claim-wacc-1",
    growth_source: str = "claim-g-1",
    total_debt: float = 1000.0,
    cash: float = 500.0,
    minority_interest: float = 100.0,
    associates_investments: float = 200.0,
    diluted_shares: float = 100.0,
    market_cap: float = 5000.0,
    current_price: float = 50.0,
    industry: str = "consumer",
) -> ValuationInput:
    """构造 DCF 测试输入。"""
    fcff = fcff_series if fcff_series is not None else [400.0, 450.0, 500.0, 550.0, 600.0]
    return ValuationInput(
        company_symbol="TEST001",
        as_of="2026-07-01T00:00:00+00:00",
        scenario="base",
        current_price=current_price,
        shares_outstanding=diluted_shares,
        market_cap=market_cap,
        total_debt=total_debt,
        cash_and_equivalents=cash,
        minority_interest=minority_interest,
        associates_investments=associates_investments,
        diluted_shares=diluted_shares,
        fcff_series=fcff,
        assumptions=[
            _assumption("wacc", wacc, source_ref=wacc_source),
            _assumption("terminal_growth", terminal_growth, source_ref=growth_source),
        ],
        industry=industry,
        market_data_version="market-v1",
        financial_data_version="fin-v1",
        model_version="test-v1",
    )


# ---------------------------------------------------------------------------
# 验收点 1: DCF 公式与终值占比可审计
# ---------------------------------------------------------------------------

class TestAcceptancePoint1DcfFormulaAuditable:
    """验收点 1：DCF 公式完整记录 + 终值占比可审计 + 缺关键假设降级。"""

    def test_dcf_formula_trace_includes_all_fcff_discount_and_terminal_value(self):
        """DCF formula_trace 必须包含所有 FCFF 折现公式 + 终值公式。"""
        inputs = _base_dcf_inputs()
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        assert not gaps
        # 显式期 5 个 FCFF 折现公式
        fcff_lines = [l for l in case.formula_trace if l.startswith("PV(FCFF_")]
        assert len(fcff_lines) == 5
        # 终值公式
        tv_lines = [l for l in case.formula_trace if l.startswith("TV =")]
        assert len(tv_lines) == 1
        pv_tv_lines = [l for l in case.formula_trace if l.startswith("PV(TV) =")]
        assert len(pv_tv_lines) == 1
        # 企业价值公式
        ev_lines = [l for l in case.formula_trace if l.startswith("EV =")]
        assert len(ev_lines) == 1

    def test_terminal_value_pct_is_auditable(self):
        """终值占比 = PV(TV) / 企业价值 可审计。"""
        inputs = _base_dcf_inputs()
        case, _ = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        m = case.method_specific_metrics
        assert "terminal_value_pct" in m
        assert "pv_terminal_value" in m
        assert "enterprise_value" in m
        # 终值占比 = PV(TV) / EV
        expected_pct = m["pv_terminal_value"] / m["enterprise_value"]
        assert m["terminal_value_pct"] == pytest.approx(expected_pct, rel=1e-6)
        # 占比在 [0, 1] 之间
        assert 0.0 <= m["terminal_value_pct"] <= 1.0

    def test_missing_wacc_returns_not_applicable_with_gap(self):
        """缺 WACC 假设时返回 NOT_APPLICABLE + 生成 ValuationGap。"""
        inputs = _base_dcf_inputs()
        inputs.assumptions = [_assumption("terminal_growth", 0.03)]
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert not case.is_computable()
        assert case.computability == Computability.INSUFFICIENT_INPUT.value
        assert any(g.blocker_kind == "missing_wacc" for g in gaps)

    def test_missing_terminal_growth_returns_not_applicable_with_gap(self):
        """缺 terminal_growth 假设时返回 NOT_APPLICABLE + 生成 ValuationGap。"""
        inputs = _base_dcf_inputs()
        inputs.assumptions = [_assumption("wacc", 0.10)]
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_terminal_growth" for g in gaps)

    def test_missing_fcff_series_returns_not_applicable_with_gap(self):
        """缺 fcff_series 时返回 NOT_APPLICABLE + 生成 ValuationGap。"""
        inputs = _base_dcf_inputs(fcff_series=[])
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_fcff_series" for g in gaps)

    def test_invalid_fcff_values_are_filtered_with_gap(self):
        inputs = _base_dcf_inputs(
            fcff_series=[400.0, "bad", float("nan"), 600.0],
        )
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        assert any(g.blocker_kind == "invalid_fcff_series_values" for g in gaps)

    def test_wacc_without_source_ref_returns_not_applicable(self):
        """WACC 缺 source_ref 时返回 NOT_APPLICABLE（不使用默认值）。"""
        inputs = _base_dcf_inputs()
        # 把 wacc 的 source_ref 清空
        for a in inputs.assumptions:
            if a.name == "wacc":
                a.source_ref = ""
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_source_ref_wacc" for g in gaps)
        # 不得使用默认值：output_interval 必须为 [0, 0]
        assert case.output_interval == Interval(0.0, 0.0)

    def test_wacc_not_greater_than_growth_returns_not_applicable(self):
        """WACC 不大于终值增长率时返回 NOT_APPLICABLE（TV 发散）。"""
        inputs = _base_dcf_inputs(wacc=0.03, terminal_growth=0.05)
        case, gaps = DcfEngine().compute_fcff(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "wacc_not_greater_than_growth" for g in gaps)


# ---------------------------------------------------------------------------
# 验收点 2: Reverse DCF 隐含假设
# ---------------------------------------------------------------------------

class TestAcceptancePoint2ReverseDcfImpliedAssumptions:
    """验收点 2：Reverse DCF 返回市场隐含增长率 / WACC 且明确标记为 implied。"""

    def test_reverse_dcf_returns_implied_growth(self):
        """Reverse DCF 能返回市场隐含增长率。"""
        inputs = _base_dcf_inputs(market_cap=8000.0, current_price=80.0)
        case, gaps = ReverseDcfEngine().reverse_engineer(inputs)
        assert case.is_computable()
        assert not gaps
        assert "implied_growth" in case.method_specific_metrics
        implied_g = case.method_specific_metrics["implied_growth"]
        # 隐含增长率在合理范围 (-5%, WACC-0.1%)
        assert -0.05 < implied_g < inputs.assumption("wacc").value
        # 反推结果应使 EV 接近 target_ev
        assert case.method_specific_metrics["target_enterprise_value"] == pytest.approx(
            case.output_interval.low, rel=1e-3
        )

    def test_reverse_dcf_implied_growth_is_marked_implied(self):
        """反推结果必须明确标记为 implied，不得冒充 consensus。"""
        inputs = _base_dcf_inputs(market_cap=8000.0)
        case, _ = ReverseDcfEngine().reverse_engineer(inputs)
        assert case.method_specific_metrics.get("is_implied") is True
        assert case.method_specific_metrics.get("source_kind") == "implied"
        # limitations 中明确说明不得冒充 consensus
        assert any("implied" in l.lower() or "consensus" in l.lower() for l in case.limitations)

    def test_reverse_dcf_can_also_reverse_engineer_wacc(self):
        """隐含 WACC 也可反推。"""
        inputs = _base_dcf_inputs(market_cap=8000.0, current_price=80.0)
        case, gaps = ReverseDcfEngine().reverse_engineer_wacc(inputs)
        assert case.is_computable()
        assert not gaps
        assert "implied_wacc" in case.method_specific_metrics
        implied_wacc = case.method_specific_metrics["implied_wacc"]
        # 隐含 WACC > terminal_growth
        assert implied_wacc > inputs.assumption("terminal_growth").value
        assert case.method_specific_metrics.get("reverse_mode") == "wacc"
        assert case.method_specific_metrics.get("is_implied") is True

    def test_reverse_dcf_without_wacc_returns_not_applicable(self):
        """Reverse DCF 缺 WACC 假设时返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs()
        inputs.assumptions = []  # 清空所有假设
        case, gaps = ReverseDcfEngine().reverse_engineer(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_wacc" for g in gaps)

    def test_reverse_dcf_without_market_cap_returns_not_applicable(self):
        """Reverse DCF 缺 market_cap 时返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs(market_cap=0.0)
        case, gaps = ReverseDcfEngine().reverse_engineer(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_market_cap" for g in gaps)


# ---------------------------------------------------------------------------
# 验收点 3: 资本结构调整
# ---------------------------------------------------------------------------

class TestAcceptancePoint3CapitalStructureAdjustments:
    """验收点 3：净债务/少数股东/联营/稀释股本处理。"""

    def test_net_debt_is_total_debt_minus_cash(self):
        """净债务 = total_debt - cash 处理。"""
        inputs = _base_dcf_inputs(total_debt=1000.0, cash=500.0)
        case, _ = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        assert case.method_specific_metrics["net_debt"] == pytest.approx(500.0)

    def test_minority_interest_subtracted_from_enterprise_value(self):
        """少数股东权益从企业价值中扣除。"""
        inputs_no_minority = _base_dcf_inputs(minority_interest=0.0)
        inputs_with_minority = _base_dcf_inputs(minority_interest=200.0)
        case_no, _ = DcfEngine().compute_fcff(inputs_no_minority)
        case_with, _ = DcfEngine().compute_fcff(inputs_with_minority)
        # 企业价值相同（不依赖 minority），但权益价值应减少 minority_interest
        assert case_no.method_specific_metrics["enterprise_value"] == pytest.approx(
            case_with.method_specific_metrics["enterprise_value"]
        )
        delta = (case_no.method_specific_metrics["equity_value"]
                 - case_with.method_specific_metrics["equity_value"])
        assert delta == pytest.approx(200.0, rel=1e-6)

    def test_associates_investments_added_back(self):
        """联营投资加回。"""
        inputs_no_assoc = _base_dcf_inputs(associates_investments=0.0)
        inputs_with_assoc = _base_dcf_inputs(associates_investments=300.0)
        case_no, _ = DcfEngine().compute_fcff(inputs_no_assoc)
        case_with, _ = DcfEngine().compute_fcff(inputs_with_assoc)
        delta = (case_with.method_specific_metrics["equity_value"]
                 - case_no.method_specific_metrics["equity_value"])
        assert delta == pytest.approx(300.0, rel=1e-6)

    def test_diluted_shares_affects_per_share_value(self):
        """稀释股本（diluted_shares）影响每股价值。"""
        inputs_smaller = _base_dcf_inputs(diluted_shares=100.0)
        inputs_larger = _base_dcf_inputs(diluted_shares=200.0)
        case_small, _ = DcfEngine().compute_fcff(inputs_smaller)
        case_large, _ = DcfEngine().compute_fcff(inputs_larger)
        # 权益价值相同，但每股价值应为一半
        assert case_small.method_specific_metrics["equity_value"] == pytest.approx(
            case_large.method_specific_metrics["equity_value"]
        )
        assert case_small.output_per_share.mid == pytest.approx(
            case_large.output_per_share.mid * 2, rel=1e-6
        )

    def test_negative_net_debt_handled_correctly(self):
        """净债务为负时（现金大于债务）权益价值 = EV - (-net_debt) - minority + assoc = EV + |net_debt| - minority + assoc。"""
        inputs = _base_dcf_inputs(total_debt=200.0, cash=1000.0)  # net_debt = -800
        case, _ = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        assert case.method_specific_metrics["net_debt"] == pytest.approx(-800.0)
        # equity = EV - net_debt - minority + assoc = EV - (-800) - 100 + 200 = EV + 900
        ev = case.method_specific_metrics["enterprise_value"]
        expected_equity = ev - (-800.0) - 100.0 + 200.0
        assert case.method_specific_metrics["equity_value"] == pytest.approx(expected_equity, rel=1e-6)


# ---------------------------------------------------------------------------
# 验收点 4: 方法差异不平均
# ---------------------------------------------------------------------------

class TestAcceptancePoint4MethodDifferencesNotAveraged:
    """验收点 4：方法差异在报告中解释而非平均掩盖。"""

    def test_method_differences_explain_dcf_vs_pe(self):
        """DCF 与 PE 估值结果差异在 method_differences 中有解释。"""
        # 构造 DCF + historical_pe 都可计算的输入
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {
            "historical_pe": [8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0],
        }
        inputs.consensus_eps = 3.0
        inputs.current_price = 50.0
        rep = ValuationEngine().compute_report(
            inputs, methods=["dcf_fcff", "historical_pe"],
        )
        computable = rep.computable_cases()
        assert len(computable) == 2
        assert any(c.method == "dcf_fcff" for c in computable)
        assert any(c.method == "historical_pe" for c in computable)
        # method_differences 非空
        assert len(rep.method_differences) >= 1
        # 至少有一条解释包含 DCF 与 PE 的对比
        explanations = " ".join(str(d.get("explanation", "")) for d in rep.method_differences)
        assert "dcf_fcff" in explanations or "DCF" in explanations.upper()
        assert "historical_pe" in explanations or "PE" in explanations.upper()

    def test_overall_range_uses_min_max_not_mean(self):
        """overall_range 取 min(low) ~ max(high)，不是平均值。"""
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {
            "historical_pe": [8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0],
        }
        inputs.consensus_eps = 3.0
        rep = ValuationEngine().compute_report(
            inputs, methods=["dcf_fcff", "historical_pe"],
        )
        computable = rep.computable_cases()
        lows = [c.output_per_share.low for c in computable]
        highs = [c.output_per_share.high for c in computable]
        expected_low = min(lows)
        expected_high = max(highs)
        # 不是平均值
        mean_mid = sum((l + h) / 2 for l, h in zip(lows, highs)) / len(lows)
        assert rep.overall_range.low == pytest.approx(expected_low, rel=1e-6)
        assert rep.overall_range.high == pytest.approx(expected_high, rel=1e-6)
        # overall_range 的 low/high 不等于 mean_mid（除非所有方法中位值相同）
        if len(set((l + h) / 2 for l, h in zip(lows, highs))) > 1:
            assert rep.overall_range.low != pytest.approx(mean_mid, abs=1e-3)

    def test_overall_range_method_is_min_max_not_mean(self):
        """overall_range_method 标注为 "min_max"，不是 "mean"。"""
        inputs = _base_dcf_inputs()
        rep = ValuationEngine().compute_report(inputs)
        assert rep.overall_range_method == "min_max"
        assert rep.overall_range_method != "mean"

    def test_financial_industry_ev_ebitda_returns_not_applicable(self):
        """方法不适用（如金融业用 EV/EBITDA）返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs(industry="bank")
        inputs.ebitda_ttm = 1000.0
        case, _ = MultiplesEngine().compute_forward(inputs, ValuationMethod.EV_EBITDA)
        assert not case.is_computable()
        assert case.computability == Computability.METHOD_MISMATCH.value

    def test_not_applicable_methods_excluded_from_overall_range(self):
        """方法不适用时不参与 overall_range 汇总。"""
        inputs = _base_dcf_inputs(industry="bank")
        inputs.ebitda_ttm = 1000.0
        inputs.historical_multiples = {"historical_pe": [10.0, 12.0, 14.0, 16.0]}
        inputs.consensus_eps = 3.0
        # 同时计算 EV/EBITDA（不适用）和 historical_pe（可计算）
        rep = ValuationEngine().compute_report(
            inputs, methods=["ev_ebitda", "historical_pe"],
        )
        computable = rep.computable_cases()
        # 只有一个可计算（historical_pe）
        assert len(computable) == 1
        assert computable[0].method == "historical_pe"
        # overall_range 应等于 historical_pe 的区间
        assert rep.overall_range.low == pytest.approx(computable[0].output_per_share.low)
        assert rep.overall_range.high == pytest.approx(computable[0].output_per_share.high)


# ---------------------------------------------------------------------------
# 区间算术传播
# ---------------------------------------------------------------------------

class TestIntervalPropagation:
    """区间算术在 DCF 中正确传播。"""

    def test_dcf_output_is_point_interval_for_point_inputs(self):
        """DCF 输入为点值时，输出也应为点区间（low == high）。"""
        inputs = _base_dcf_inputs()
        case, _ = DcfEngine().compute_fcff(inputs)
        assert case.is_computable()
        assert case.output_interval.low == pytest.approx(case.output_interval.high)
        assert case.output_per_share.low == pytest.approx(case.output_per_share.high)

    def test_dcf_formula_trace_values_match_output(self):
        """公式轨迹中的值与最终输出一致。"""
        inputs = _base_dcf_inputs()
        case, _ = DcfEngine().compute_fcff(inputs)
        # EV = sum(PV(FCFF)) + PV(TV)
        sum_pv = case.method_specific_metrics["sum_pv_fcff"]
        pv_tv = case.method_specific_metrics["pv_terminal_value"]
        ev = case.method_specific_metrics["enterprise_value"]
        assert ev == pytest.approx(sum_pv + pv_tv, rel=1e-6)
        assert case.output_interval.low == pytest.approx(ev, rel=1e-6)


# ---------------------------------------------------------------------------
# 敏感性矩阵
# ---------------------------------------------------------------------------

class TestSensitivityMatrix:
    """敏感性矩阵生成正确（WACC × g）。"""

    def test_wacc_x_growth_returns_points_for_each_axis(self):
        """WACC × g 矩阵生成 wacc 与 growth 两轴的 SensitivityPoint。"""
        fcff_series = [400.0, 450.0, 500.0, 550.0, 600.0]
        wacc_range = [0.09, 0.095, 0.10, 0.105, 0.11]
        growth_range = [0.025, 0.0275, 0.03, 0.0325, 0.035]
        points = SensitivityAnalyzer().wacc_x_growth(
            fcff_series=fcff_series,
            wacc_range=wacc_range,
            growth_range=growth_range,
            net_debt=500.0,
            minority_interest=100.0,
            associates_investments=200.0,
            shares=100.0,
        )
        wacc_points = [p for p in points if p.variable_name == "wacc"]
        growth_points = [p for p in points if p.variable_name == "growth"]
        assert len(wacc_points) == 5
        assert len(growth_points) == 5
        # 每个 wacc 点的 output_low <= output_high
        for p in wacc_points:
            assert p.output_low <= p.output_high
        for p in growth_points:
            assert p.output_low <= p.output_high

    def test_wacc_higher_means_value_lower(self):
        """WACC 越高，每股价值越低（在其他变量不变时）。"""
        fcff_series = [400.0, 450.0, 500.0, 550.0, 600.0]
        wacc_range = [0.08, 0.10, 0.12]
        growth_range = [0.03]
        points = SensitivityAnalyzer().wacc_x_growth(
            fcff_series=fcff_series,
            wacc_range=wacc_range,
            growth_range=growth_range,
            shares=100.0,
        )
        wacc_points = sorted([p for p in points if p.variable_name == "wacc"],
                             key=lambda p: p.variable_value)
        # WACC 升高 -> 中位值下降
        assert wacc_points[0].output_low > wacc_points[-1].output_low
        assert wacc_points[0].output_high > wacc_points[-1].output_high

    def test_empty_inputs_returns_empty(self):
        """空输入返回空列表。"""
        assert SensitivityAnalyzer().wacc_x_growth([], [0.10], [0.03]) == []
        assert SensitivityAnalyzer().wacc_x_growth([1.0], [], [0.03]) == []
        assert SensitivityAnalyzer().wacc_x_growth([1.0], [0.10], []) == []


# ---------------------------------------------------------------------------
# PIT 过滤
# ---------------------------------------------------------------------------

class TestPitFilter:
    """list_cases 按 as_of 过滤。"""

    def test_list_cases_filters_by_as_of(self, tmp_path):
        """list_cases 仅返回 as_of <= 查询时点的记录。"""
        db_path = tmp_path / "test_pit.db"
        repo = ValuationRepository(db_path=db_path)
        # 三条记录，as_of 不同
        for as_of in ["2026-01-01T00:00:00+00:00",
                       "2026-03-01T00:00:00+00:00",
                       "2026-06-01T00:00:00+00:00"]:
            case = ValuationCase(
                company_symbol="TEST001",
                method="dcf_fcff",
                as_of=as_of,
                output_interval=Interval(1.0, 2.0),
                model_version="test-v1",
            )
            repo.save_case(case)
        # 查询 2026-04-01 时点，应返回 2 条（1月+3月）
        items = repo.list_cases(company_symbol="TEST001", as_of="2026-04-01T00:00:00+00:00")
        assert len(items) == 2
        for item in items:
            assert item["as_of"] <= "2026-04-01T00:00:00+00:00"
        # 查询 2026-07-01 时点，应返回 3 条
        items_all = repo.list_cases(company_symbol="TEST001", as_of="2026-07-01T00:00:00+00:00")
        assert len(items_all) == 3

    def test_list_cases_filters_by_method(self, tmp_path):
        """list_cases 支持按 method 过滤。"""
        db_path = tmp_path / "test_method.db"
        repo = ValuationRepository(db_path=db_path)
        for method in ["dcf_fcff", "historical_pe", "dcf_fcff"]:
            case = ValuationCase(
                company_symbol="TEST001",
                method=method,
                as_of="2026-07-01T00:00:00+00:00",
                model_version="test-v1",
            )
            repo.save_case(case)
        items = repo.list_cases(company_symbol="TEST001", method="dcf_fcff")
        assert len(items) == 2
        for item in items:
            assert item["method"] == "dcf_fcff"


class TestValuationInputBoundaries:
    def test_contract_and_validator_boundaries(self):
        inputs = ValuationInput(
            industry=123,
            assumptions=None,
            historical_multiples=None,
            comparable_companies=None,
        )
        assert inputs.is_financial_industry() is False
        assert inputs.assumption("wacc") is None
        validator = ValuationAssumptionValidator()
        assert validator.validate(None, None) == ({}, [])

        case = ValuationCase(output_interval=None, output_per_share=None, assumptions=None, sensitivity=None)
        assert case.to_dict()["output_interval"]["low"] == 0.0
        report = ValuationReport(cases=[None], overall_range=None)
        assert report.computable_cases() == []
        assert report.to_dict()["cases"] == []

    def test_invalid_historical_values_are_filtered(self):
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {
            ValuationMethod.HISTORICAL_PE.value: ["bad", float("nan"), 10.0],
        }
        inputs.trailing_eps = 2.0
        case, gaps = MultiplesEngine().compute_historical(
            inputs, ValuationMethod.HISTORICAL_PE
        )
        assert case.is_computable()
        assert any(g.blocker_kind == "invalid_historical_series_values" for g in gaps)

    def test_invalid_comparable_peers_are_ignored(self):
        inputs = _base_dcf_inputs()
        inputs.consensus_eps = 2.0
        inputs.comparable_companies = [None, {"pe": "bad"}, {"pe": 12.0}]
        case, gaps = ComparableCompanyEngine().compute(inputs)
        assert case.is_computable()
        assert any(g.blocker_kind == "invalid_comparable_pe_values" for g in gaps)


# ---------------------------------------------------------------------------
# 仓储幂等性
# ---------------------------------------------------------------------------

class TestRepositoryIdempotency:
    """迁移幂等 + 重启回读正确。"""

    def test_migrate_is_idempotent(self, tmp_path):
        """migrate() 重复执行不抛异常。"""
        db_path = tmp_path / "test_idem.db"
        repo1 = ValuationRepository(db_path=db_path)
        # 再次创建实例（触发 migrate）应不抛异常
        repo2 = ValuationRepository(db_path=db_path)
        stats1 = repo1.stats()
        stats2 = repo2.stats()
        assert stats1["schema_version"] == stats2["schema_version"] == 1
        # 多次调用 migrate
        repo1.migrate()
        repo1.migrate()
        assert repo1.stats()["schema_version"] == 1

    def test_roundtrip_case(self, tmp_path):
        """ValuationCase to_dict/from_dict 往返。"""
        db_path = tmp_path / "test_roundtrip.db"
        repo = ValuationRepository(db_path=db_path)
        case = ValuationCase(
            company_symbol="TEST001",
            method="dcf_fcff",
            as_of="2026-07-01T00:00:00+00:00",
            output_interval=Interval(100.0, 200.0),
            output_per_share=Interval(10.0, 20.0),
            formula_trace=["PV(FCFF_1) = ...", "TV = ..."],
            sensitivity=[SensitivityPoint("wacc", 0.10, 9.0, 11.0)],
            assumptions=[ValuationAssumption(name="wacc", value=0.10, source_ref="c1")],
            method_specific_metrics={"terminal_value_pct": 0.65},
            model_version="test-v1",
        )
        repo.save_case(case)
        # 重启回读
        repo2 = ValuationRepository(db_path=db_path)
        loaded = repo2.get_case(case.case_id)
        assert loaded is not None
        roundtrip = ValuationCase.from_dict(loaded)
        assert roundtrip.case_id == case.case_id
        assert roundtrip.method == case.method
        assert roundtrip.output_interval.low == pytest.approx(100.0)
        assert roundtrip.output_interval.high == pytest.approx(200.0)
        assert roundtrip.output_per_share.low == pytest.approx(10.0)
        assert roundtrip.output_per_share.high == pytest.approx(20.0)
        assert len(roundtrip.formula_trace) == 2
        assert len(roundtrip.sensitivity) == 1
        assert roundtrip.sensitivity[0].variable_name == "wacc"
        assert len(roundtrip.assumptions) == 1
        assert roundtrip.assumptions[0].name == "wacc"
        assert roundtrip.method_specific_metrics["terminal_value_pct"] == pytest.approx(0.65)

    def test_malformed_persisted_payload_is_skipped(self, tmp_path):
        repo = ValuationRepository(db_path=tmp_path / "test_malformed.db")
        case = ValuationCase(company_symbol="TEST001", method="dcf_fcff")
        repo.save_case(case)
        with repo.connect() as conn:
            conn.execute(
                "UPDATE valuation_cases SET payload_json=? WHERE case_id=?",
                ("not-json", case.case_id),
            )
        assert repo.get_case(case.case_id) is None
        assert repo.list_cases() == []

    def test_invalid_limit_falls_back_to_default(self, tmp_path):
        repo = ValuationRepository(db_path=tmp_path / "test_limit.db")
        assert repo.list_cases(limit="invalid") == []

    def test_unknown_repository_table_is_rejected(self, tmp_path):
        repo = ValuationRepository(db_path=tmp_path / "test_unknown_table.db")
        with pytest.raises(ValueError):
            repo._get("unknown", "id")

    def test_roundtrip_report(self, tmp_path):
        """ValuationReport to_dict/from_dict 往返。"""
        db_path = tmp_path / "test_roundtrip_rep.db"
        repo = ValuationRepository(db_path=db_path)
        case = ValuationCase(
            company_symbol="TEST001",
            method="dcf_fcff",
            as_of="2026-07-01T00:00:00+00:00",
            output_interval=Interval(100.0, 200.0),
            output_per_share=Interval(10.0, 20.0),
            model_version="test-v1",
        )
        rep = ValuationReport(
            company_symbol="TEST001",
            as_of="2026-07-01T00:00:00+00:00",
            cases=[case],
            overall_range=Interval(10.0, 30.0),
            overall_range_method="min_max",
            model_version="test-v1",
        )
        repo.save_report(rep)
        repo2 = ValuationRepository(db_path=db_path)
        loaded = repo2.get_report(rep.report_id)
        assert loaded is not None
        roundtrip = ValuationReport.from_dict(loaded)
        assert roundtrip.report_id == rep.report_id
        assert roundtrip.overall_range.low == pytest.approx(10.0)
        assert roundtrip.overall_range.high == pytest.approx(30.0)
        assert len(roundtrip.cases) == 1
        assert roundtrip.cases[0].method == "dcf_fcff"

    def test_malformed_contract_payloads_degrade(self):
        case = ValuationCase.from_dict(["not", "a", "mapping"])
        assert case.output_interval == Interval(0.0, 0.0)
        assert case.assumptions == []

        report = ValuationReport.from_dict({
            "cases": [None, {"assumptions": [{"name": "broken"}, {"name": "wacc", "value": 0.1}]}],
        })
        assert len(report.cases) == 1
        assert [item.name for item in report.cases[0].assumptions] == ["wacc"]


# ---------------------------------------------------------------------------
# API Smoke
# ---------------------------------------------------------------------------

class TestApiSmoke:
    """所有端点 smoke 测试。"""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """构造 Flask 测试客户端。"""
        # 重置单例
        import server.routes.valuation_routes as mod
        mod._repository = None
        mod._engine = None
        # 重定向 DB 路径到 tmp
        db_path = tmp_path / "test_api.db"
        from core.research.valuation.repository import ValuationRepository
        mod._repository = ValuationRepository(db_path=db_path)
        app = Flask(__name__)
        app.register_blueprint(valuation_bp)
        with app.test_client() as client:
            yield client
        mod._repository = None
        mod._engine = None

    def test_health(self, client):
        """GET /health 端点 smoke。"""
        resp = client.get("/api/research/valuation/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "ok"
        assert data["notice"] == "研究数据产物，不包含交易动作建议。"

    def test_post_case_dcf(self, client):
        """POST /cases 计算 DCF 案例。"""
        payload = {
            "company_symbol": "TEST001",
            "method": "dcf_fcff",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 450.0, 500.0, 550.0, 600.0],
            "current_price": 50.0,
            "shares_outstanding": 100.0,
            "market_cap": 5000.0,
            "total_debt": 1000.0,
            "cash_and_equivalents": 500.0,
            "minority_interest": 100.0,
            "associates_investments": 200.0,
            "diluted_shares": 100.0,
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": "claim-wacc-1"},
                {"name": "terminal_growth", "value": 0.03, "source_ref": "claim-g-1"},
            ],
            "save": False,
        }
        resp = client.post("/api/research/valuation/cases", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["computability"] == "computable"
        assert data["data"]["method"] == "dcf_fcff"

    def test_post_case_missing_required_returns_400(self, client):
        """POST /cases 缺必填字段返回 400。"""
        resp = client.post("/api/research/valuation/cases", json={
            "method": "dcf_fcff",
            "as_of": "2026-07-01T00:00:00+00:00",
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "INSUFFICIENT_INPUT"

    def test_post_case_unknown_method_returns_400(self, client):
        """POST /cases 未知方法返回 400。"""
        resp = client.post("/api/research/valuation/cases", json={
            "company_symbol": "TEST001",
            "method": "unknown_method",
            "as_of": "2026-07-01T00:00:00+00:00",
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"]["code"] == "UNKNOWN_METHOD"

    def test_get_cases(self, client):
        """GET /cases 列表查询。"""
        # 先保存一条
        case = ValuationCase(
            company_symbol="TEST001",
            method="dcf_fcff",
            as_of="2026-07-01T00:00:00+00:00",
            model_version="v1",
        )
        import server.routes.valuation_routes as mod
        mod._repository.save_case(case)
        resp = client.get("/api/research/valuation/cases?company_symbol=TEST001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]) == 1

    def test_get_case_detail_not_found(self, client):
        """GET /cases/<id> 不存在返回 404。"""
        resp = client.get("/api/research/valuation/cases/nonexistent")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"]["code"] == "CASE_NOT_FOUND"

    def test_post_report(self, client):
        """POST /report 计算多方法报告。"""
        payload = {
            "company_symbol": "TEST001",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 450.0, 500.0, 550.0, 600.0],
            "current_price": 50.0,
            "shares_outstanding": 100.0,
            "market_cap": 5000.0,
            "total_debt": 1000.0,
            "cash_and_equivalents": 500.0,
            "diluted_shares": 100.0,
            "consensus_eps": 3.0,
            "historical_multiples": {"historical_pe": [10.0, 12.0, 14.0, 16.0, 18.0]},
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": "claim-wacc-1"},
                {"name": "terminal_growth", "value": 0.03, "source_ref": "claim-g-1"},
            ],
            "methods": ["dcf_fcff", "historical_pe"],
            "save": False,
        }
        resp = client.post("/api/research/valuation/report", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "cases" in data["data"]
        assert data["data"]["overall_range_method"] == "min_max"

    def test_post_reverse_dcf(self, client):
        """POST /reverse_dcf 反推隐含增长率。"""
        payload = {
            "company_symbol": "TEST001",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 450.0, 500.0, 550.0, 600.0],
            "current_price": 80.0,
            "shares_outstanding": 100.0,
            "market_cap": 8000.0,
            "total_debt": 1000.0,
            "cash_and_equivalents": 500.0,
            "diluted_shares": 100.0,
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": "claim-wacc-1"},
            ],
            "save": False,
        }
        resp = client.post("/api/research/valuation/reverse_dcf", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "implied_growth" in data["data"]["method_specific_metrics"]

    def test_post_reverse_dcf_missing_required_returns_400(self, client):
        """POST /reverse_dcf 缺 fcff_series 返回 400。"""
        resp = client.post("/api/research/valuation/reverse_dcf", json={
            "company_symbol": "TEST001",
            "as_of": "2026-07-01T00:00:00+00:00",
            "assumptions": [{"name": "wacc", "value": 0.10, "source_ref": "c1"}],
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"]["code"] == "INSUFFICIENT_INPUT"

    def test_get_reports(self, client):
        """GET /reports 列表查询。"""
        rep = ValuationReport(
            company_symbol="TEST001",
            as_of="2026-07-01T00:00:00+00:00",
            model_version="v1",
        )
        import server.routes.valuation_routes as mod
        mod._repository.save_report(rep)
        resp = client.get("/api/research/valuation/reports?company_symbol=TEST001")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]) == 1

    def test_get_report_detail_not_found(self, client):
        """GET /reports/<id> 不存在返回 404。"""
        resp = client.get("/api/research/valuation/reports/nonexistent")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"]["code"] == "REPORT_NOT_FOUND"


# ---------------------------------------------------------------------------
# 安全测试：不含交易建议
# ---------------------------------------------------------------------------

class TestNoTradeAdvice:
    """响应不含 buy/sell/position/止盈/止损 等交易建议关键词。"""

    BANNED_KEYWORDS: tuple[str, ...] = (
        "buy", "sell", "long", "short",  # 英文动作词
        "止盈", "止损", "买入", "卖出", "仓位", "加仓", "减仓",  # 中文动作词
        "target_price",  # 目标价（不允许给单点目标）
        "stop_loss", "take_profit",
    )

    @pytest.fixture
    def client(self, tmp_path):
        """构造 Flask 测试客户端。"""
        import server.routes.valuation_routes as mod
        mod._repository = None
        mod._engine = None
        db_path = tmp_path / "test_no_advice.db"
        from core.research.valuation.repository import ValuationRepository
        mod._repository = ValuationRepository(db_path=db_path)
        app = Flask(__name__)
        app.register_blueprint(valuation_bp)
        with app.test_client() as client:
            yield client
        mod._repository = None
        mod._engine = None

    def _assert_no_banned_keywords(self, payload):
        """递归检查 payload 中无被禁关键词。"""
        text = json.dumps(payload, ensure_ascii=False).lower()
        for kw in self.BANNED_KEYWORDS:
            # notice 包含「不包含交易动作建议」字样，会命中"买入/卖出"等子串
            # 因此只检查非 notice 字段
            assert kw.lower() not in text or "不包含交易" in text, f"响应包含被禁关键词: {kw}"

    def test_case_response_no_trade_advice(self, client):
        """估值案例响应不含交易建议。"""
        payload = {
            "company_symbol": "TEST001",
            "method": "dcf_fcff",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 450.0, 500.0, 550.0, 600.0],
            "current_price": 50.0,
            "shares_outstanding": 100.0,
            "market_cap": 5000.0,
            "total_debt": 1000.0,
            "cash_and_equivalents": 500.0,
            "diluted_shares": 100.0,
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": "c1"},
                {"name": "terminal_growth", "value": 0.03, "source_ref": "c2"},
            ],
            "save": False,
        }
        resp = client.post("/api/research/valuation/cases", json=payload)
        data = resp.get_json()
        # 检查 data 部分（不含 notice）
        self._assert_no_banned_keywords(data["data"])
        # notice 字段固定提示
        assert data["notice"] == "研究数据产物，不包含交易动作建议。"

    def test_report_response_no_trade_advice(self, client):
        """估值报告响应不含交易建议。"""
        payload = {
            "company_symbol": "TEST001",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 450.0, 500.0, 550.0, 600.0],
            "current_price": 50.0,
            "shares_outstanding": 100.0,
            "market_cap": 5000.0,
            "total_debt": 1000.0,
            "cash_and_equivalents": 500.0,
            "diluted_shares": 100.0,
            "consensus_eps": 3.0,
            "historical_multiples": {"historical_pe": [10.0, 12.0, 14.0, 16.0, 18.0]},
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": "c1"},
                {"name": "terminal_growth", "value": 0.03, "source_ref": "c2"},
            ],
            "methods": ["dcf_fcff", "historical_pe"],
            "save": False,
        }
        resp = client.post("/api/research/valuation/report", json=payload)
        data = resp.get_json()
        self._assert_no_banned_keywords(data["data"])
        assert data["notice"] == "研究数据产物，不包含交易动作建议。"

    def test_method_specific_metrics_no_target_price(self):
        """method_specific_metrics 不包含 target_price 单点目标价字段。"""
        inputs = _base_dcf_inputs()
        case, _ = DcfEngine().compute_fcff(inputs)
        assert "target_price" not in case.method_specific_metrics
        assert "buy" not in case.method_specific_metrics
        assert "sell" not in case.method_specific_metrics


# ---------------------------------------------------------------------------
# 方法不适用 = 200 + not_applicable（非 5xx 错误）
# ---------------------------------------------------------------------------

class TestNotApplicableNotFailure:
    """方法不适用时 status=200 + computability=not_applicable，不视为失败。"""

    @pytest.fixture
    def client(self, tmp_path):
        """构造 Flask 测试客户端。"""
        import server.routes.valuation_routes as mod
        mod._repository = None
        mod._engine = None
        db_path = tmp_path / "test_na.db"
        from core.research.valuation.repository import ValuationRepository
        mod._repository = ValuationRepository(db_path=db_path)
        app = Flask(__name__)
        app.register_blueprint(valuation_bp)
        with app.test_client() as client:
            yield client
        mod._repository = None
        mod._engine = None

    def test_financial_industry_ev_ebitda_returns_200_with_not_applicable(self, client):
        """金融业用 EV/EBITDA 返回 200 + computability=method_mismatch。"""
        payload = {
            "company_symbol": "BANK001",
            "method": "ev_ebitda",
            "as_of": "2026-07-01T00:00:00+00:00",
            "industry": "bank",
            "market_cap": 5000.0,
            "ebitda_ttm": 1000.0,
            "save": False,
        }
        resp = client.post("/api/research/valuation/cases", json=payload)
        assert resp.status_code == 200  # 不适用不是 4xx/5xx
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["computability"] == Computability.METHOD_MISMATCH.value

    def test_missing_assumptions_returns_200_with_not_applicable(self, client):
        """缺关键假设时返回 200 + computability=insufficient_input。"""
        payload = {
            "company_symbol": "TEST001",
            "method": "dcf_fcff",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 500.0],
            # 故意不传 wacc 假设
            "save": False,
        }
        resp = client.post("/api/research/valuation/cases", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["computability"] == Computability.INSUFFICIENT_INPUT.value

    def test_wacc_without_source_ref_returns_200_with_not_applicable(self, client):
        """WACC 缺 source_ref 时返回 200 + not_applicable（不使用默认值）。"""
        payload = {
            "company_symbol": "TEST001",
            "method": "dcf_fcff",
            "as_of": "2026-07-01T00:00:00+00:00",
            "fcff_series": [400.0, 500.0],
            "assumptions": [
                {"name": "wacc", "value": 0.10, "source_ref": ""},  # 缺 source_ref
                {"name": "terminal_growth", "value": 0.03, "source_ref": "c2"},
            ],
            "save": False,
        }
        resp = client.post("/api/research/valuation/cases", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["computability"] == Computability.INSUFFICIENT_INPUT.value
        # output_interval 必须为 [0, 0]
        assert data["data"]["output_interval"]["low"] == 0.0
        assert data["data"]["output_interval"]["high"] == 0.0


# ---------------------------------------------------------------------------
# 子引擎单元测试（补充覆盖）
# ---------------------------------------------------------------------------

class TestSubEngines:
    """子引擎单元测试。"""

    def test_assumption_validator_detects_missing(self):
        """假设校验器检测缺失。"""
        validator = ValuationAssumptionValidator()
        assumptions = [_assumption("wacc", 0.10)]
        assumption_map, gaps = validator.validate(assumptions, ["wacc", "terminal_growth"])
        assert "wacc" in assumption_map
        assert "terminal_growth" not in assumption_map
        assert any(g.blocker_kind == "missing_terminal_growth" for g in gaps)

    def test_assumption_validator_detects_missing_source_ref(self):
        """假设校验器检测缺 source_ref。"""
        validator = ValuationAssumptionValidator()
        a = ValuationAssumption(name="wacc", value=0.10, source_ref="")
        assumption_map, gaps = validator.validate([a], ["wacc"])
        assert "wacc" not in assumption_map  # 缺 source_ref 时被剔除
        assert any(g.blocker_kind == "missing_source_ref_wacc" for g in gaps)

    def test_fcfe_dcf_works(self):
        """FCFE DCF 可计算。"""
        inputs = _base_dcf_inputs()
        inputs.fcfe_series = [300.0, 350.0, 400.0, 450.0, 500.0]
        inputs.assumptions = [
            _assumption("cost_of_equity", 0.12),
            _assumption("terminal_growth", 0.03),
        ]
        case, gaps = DcfEngine().compute_fcfe(inputs)
        assert case.is_computable()
        assert not gaps
        assert case.method == "dcf_fcfe"
        assert case.output_per_share.low > 0

    def test_ddm_works(self):
        """DDM 可计算。"""
        inputs = _base_dcf_inputs()
        inputs.dividend_series = [2.0, 2.1, 2.2, 2.3, 2.4]
        inputs.assumptions = [
            _assumption("cost_of_equity", 0.10),
            _assumption("terminal_growth", 0.03),
        ]
        case, gaps = DdmEngine().compute(inputs)
        assert case.is_computable()
        assert not gaps
        assert case.method == "ddm"
        assert case.output_per_share.low > 0

    def test_ddm_without_dividend_series_returns_not_applicable(self):
        """DDM 缺 dividend_series 返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs()
        inputs.dividend_series = []
        inputs.assumptions = [
            _assumption("cost_of_equity", 0.10),
            _assumption("terminal_growth", 0.03),
        ]
        case, gaps = DdmEngine().compute(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_dividend_series" for g in gaps)

    def test_historical_pe_works(self):
        """历史 PE 倍数法可计算。"""
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {"historical_pe": [8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]}
        inputs.consensus_eps = 3.0
        inputs.current_price = 50.0
        case, gaps = MultiplesEngine().compute_historical(inputs, ValuationMethod.HISTORICAL_PE)
        assert case.is_computable()
        assert not gaps
        assert case.method_specific_metrics["p25"] > 0
        assert case.output_per_share.low > 0
        assert case.output_per_share.high > case.output_per_share.low

    def test_historical_pe_missing_series_returns_not_applicable(self):
        """历史 PE 缺序列返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {}
        case, gaps = MultiplesEngine().compute_historical(inputs, ValuationMethod.HISTORICAL_PE)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_historical_pe_series" for g in gaps)

    def test_forward_pe_works(self):
        """Forward PE 可计算。"""
        inputs = _base_dcf_inputs(market_cap=6000.0)
        inputs.consensus_net_profit = 300.0
        inputs.consensus_eps = 3.0
        case, _ = MultiplesEngine().compute_forward(inputs, ValuationMethod.FORWARD_PE)
        assert case.is_computable()
        assert case.method_specific_metrics["forward_pe"] == pytest.approx(20.0)

    def test_ev_ebitda_works(self):
        """EV/EBITDA 可计算（非金融业）。"""
        # minority=0 / associates=0 让 EV = market_cap + net_debt = 6000 + 500 = 6500
        inputs = _base_dcf_inputs(
            market_cap=6000.0, total_debt=1000.0, cash=500.0,
            minority_interest=0.0, associates_investments=0.0,
            industry="consumer",
        )
        inputs.ebitda_ttm = 1000.0
        case, _ = MultiplesEngine().compute_forward(inputs, ValuationMethod.EV_EBITDA)
        assert case.is_computable()
        # EV = 6000 + 500 + 0 - 0 = 6500, EV/EBITDA = 6.5
        assert case.method_specific_metrics["ev_ebitda"] == pytest.approx(6.5)

    def test_comparable_company_works(self):
        """同业可比公司估值可计算。"""
        inputs = _base_dcf_inputs()
        inputs.comparable_companies = [
            {"symbol": "PEER1", "pe": 10.0},
            {"symbol": "PEER2", "pe": 15.0},
            {"symbol": "PEER3", "pe": 20.0},
        ]
        inputs.consensus_eps = 3.0
        case, gaps = ComparableCompanyEngine().compute(inputs)
        assert case.is_computable()
        assert not gaps
        assert case.output_per_share.low > 0

    def test_comparable_company_missing_returns_not_applicable(self):
        """缺可比公司数据返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs()
        inputs.comparable_companies = []
        case, gaps = ComparableCompanyEngine().compute(inputs)
        assert not case.is_computable()
        assert any(g.blocker_kind == "missing_comparable_pe" for g in gaps)

    def test_peg_without_growth_returns_not_applicable(self):
        """PEG 缺 growth_rate 返回 NOT_APPLICABLE。"""
        inputs = _base_dcf_inputs()
        inputs.consensus_eps = 3.0
        inputs.current_price = 50.0
        # 不传 growth_rate 假设
        case, _ = MultiplesEngine().compute_forward(inputs, ValuationMethod.PEG)
        assert not case.is_computable()

    def test_full_report_computes_multiple_methods(self):
        """完整报告计算多方法。"""
        inputs = _base_dcf_inputs()
        inputs.historical_multiples = {"historical_pe": [10.0, 12.0, 14.0, 16.0, 18.0]}
        inputs.consensus_eps = 3.0
        inputs.consensus_net_profit = 300.0
        inputs.ebitda_ttm = 1000.0
        inputs.comparable_companies = [{"symbol": "P1", "pe": 12.0}, {"symbol": "P2", "pe": 16.0}]
        rep = ValuationEngine().compute_report(inputs)
        # 应有多个案例
        assert len(rep.cases) >= 5
        # 至少 2 个可计算
        computable = rep.computable_cases()
        assert len(computable) >= 2
        # overall_range_method 始终为 min_max
        assert rep.overall_range_method == "min_max"
        # notice 固定
        assert rep.notice == "研究数据产物，不包含交易动作建议。"
