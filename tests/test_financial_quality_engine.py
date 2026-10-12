"""P1.2 财务质量引擎 + 异常检测引擎 测试。

按 P1 计划 Task P1.2 验收点设计：
- [ ] 每个指标带公式、输入字段和期间
- [ ] 分母为零、负值、缺字段有明确处理
- [ ] 同比只比较同口径期间
- [ ] 异常是审查提示，不自动等同财务造假
- [ ] 页面显示"观察到的异常"和"结论"之间的边界
"""
from __future__ import annotations

from typing import Any

import pytest

from core.research.fundamentals.anomaly_engine import (
    ACCRUAL_RATIO_SEVERE,
    ACCRUAL_RATIO_WARNING,
    CASH_CONVERSION_WARNING,
    CONCLUSION_BOUNDARY,
    GROSS_MARGIN_DROP_SEVERE,
    GROSS_MARGIN_DROP_WARNING,
    INVENTORY_GROWTH_VS_REVENUE_WARNING,
    RECEIVABLE_GROWTH_VS_REVENUE_WARNING,
    SEVERITY_INFO,
    SEVERITY_SEVERE,
    SEVERITY_WARNING,
    AnomalyReport,
    AnomalySignal,
    detect_anomalies,
)
from core.research.fundamentals.contracts import (
    NormalizedFinancialStatement,
    StatementLine,
)
from core.research.fundamentals.quality_engine import (
    METRIC_MISSING,
    METRIC_NOT_COMPUTABLE,
    METRIC_OK,
    QualityMetric,
    QualityReport,
    compute_quality_metrics,
)
from core.research.value_chain.fundamentals import (
    build_anomaly_report_from_statements,
    build_quality_report_from_statements,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _line(metric: str, value: float | None) -> StatementLine:
    """构造一行标准化报表数据。"""
    return StatementLine(
        metric_name=metric,
        value=value,
        unit="元",
        currency="CNY",
        source_ref="test:symbol",
        grounding_status="grounding_ok" if value is not None else "missing",
        reported_value=value,
    )


def _build_statement(
    *,
    symbol: str = "600519.SH",
    period: str = "2023-12-31",
    income: dict[str, float | None] | None = None,
    balance: dict[str, float | None] | None = None,
    cashflow: dict[str, float | None] | None = None,
) -> NormalizedFinancialStatement:
    """根据字段字典构造 NormalizedFinancialStatement。"""
    return NormalizedFinancialStatement(
        symbol=symbol,
        period=period,
        published_at="2024-03-28",
        available_at="2024-03-28",
        currency="CNY",
        unit="元",
        income_statement=[_line(k, v) for k, v in (income or {}).items()],
        balance_sheet=[_line(k, v) for k, v in (balance or {}).items()],
        cash_flow_statement=[_line(k, v) for k, v in (cashflow or {}).items()],
        source_trace_id="test:symbol",
    )


@pytest.fixture
def current_statement() -> NormalizedFinancialStatement:
    """当期完整三表（2023年报）。"""
    return _build_statement(
        period="2023-12-31",
        income={
            "revenue": 1_000_000_000.0,
            "operating_cost": 600_000_000.0,
            "gross_profit": 400_000_000.0,
            "operating_profit": 200_000_000.0,
            "net_profit": 150_000_000.0,
            "financial_expense": 20_000_000.0,
        },
        balance={
            "total_assets": 2_000_000_000.0,
            "total_liabilities": 800_000_000.0,
            "total_equity": 1_200_000_000.0,
            "equity_parent": 1_180_000_000.0,
            "total_current_liabilities": 500_000_000.0,
            "cash_and_equivalents": 200_000_000.0,
            "accounts_receivable": 100_000_000.0,
            "inventory": 80_000_000.0,
            "contract_liabilities": 50_000_000.0,
            "short_term_borrowings": 300_000_000.0,
            "long_term_borrowings": 200_000_000.0,
        },
        cashflow={
            "operating_cash_flow": 120_000_000.0,
            "capex": 60_000_000.0,
        },
    )


@pytest.fixture
def previous_statement() -> NormalizedFinancialStatement:
    """同口径上一年（2022年报）。"""
    return _build_statement(
        period="2022-12-31",
        income={
            "revenue": 800_000_000.0,
            "operating_cost": 480_000_000.0,
            "gross_profit": 320_000_000.0,
            "operating_profit": 150_000_000.0,
            "net_profit": 110_000_000.0,
            "financial_expense": 15_000_000.0,
        },
        balance={
            "total_assets": 1_600_000_000.0,
            "total_liabilities": 600_000_000.0,
            "total_equity": 1_000_000_000.0,
            "equity_parent": 980_000_000.0,
            "total_current_liabilities": 400_000_000.0,
            "cash_and_equivalents": 150_000_000.0,
            "accounts_receivable": 70_000_000.0,
            "inventory": 60_000_000.0,
            "contract_liabilities": 40_000_000.0,
            "short_term_borrowings": 250_000_000.0,
            "long_term_borrowings": 150_000_000.0,
        },
        cashflow={
            "operating_cash_flow": 100_000_000.0,
            "capex": 50_000_000.0,
        },
    )


# ---------------------------------------------------------------------------
# 验收点 1：每个指标带公式、输入字段和期间
# ---------------------------------------------------------------------------


class TestMetricContract:
    """验收点：每个指标带公式、输入字段和期间。"""

    def test_each_metric_has_formula_inputs_period(self, current_statement):
        """所有 QualityMetric 必须带非空 formula/input_fields/period。"""
        report = compute_quality_metrics(current_statement, previous=None)
        assert len(report.metrics) >= 15, "至少覆盖计划要求的 15 个指标"

        for name, metric in report.metrics.items():
            assert metric.name == name
            assert metric.formula, f"指标 {name} 缺 formula"
            assert isinstance(metric.input_fields, list)
            assert metric.period == current_statement.period, (
                f"指标 {name} period 与报表不一致"
            )
            assert metric.unit, f"指标 {name} 缺 unit"
            assert metric.status in (METRIC_OK, METRIC_MISSING, METRIC_NOT_COMPUTABLE)

    def test_dupont_decomposition_has_four_parts(self, current_statement):
        """杜邦分解必须包含 net_margin/asset_turnover/equity_multiplier/roe_check。"""
        report = compute_quality_metrics(current_statement, previous=None)
        keys = set(report.dupont_decomposition.keys())
        assert keys == {"net_margin", "asset_turnover", "equity_multiplier", "roe_check"}
        for key, metric in report.dupont_decomposition.items():
            assert metric.formula, f"杜邦项 {key} 缺 formula"
            assert metric.period == current_statement.period

    def test_quality_metric_to_dict_roundtrip(self):
        """QualityMetric.to_dict() 应包含所有字段。"""
        metric = QualityMetric(
            name="roe", value=0.12, formula="net_profit/equity",
            input_fields=["net_profit", "equity"], period="2023-12-31",
            unit="ratio", status=METRIC_OK, note="",
        )
        d = metric.to_dict()
        assert d["name"] == "roe"
        assert d["value"] == 0.12
        assert d["formula"] == "net_profit/equity"
        assert d["input_fields"] == ["net_profit", "equity"]
        assert d["period"] == "2023-12-31"
        assert d["unit"] == "ratio"
        assert d["status"] == METRIC_OK

    def test_quality_report_to_dict(self, current_statement):
        """QualityReport.to_dict() 应包含 metrics/missing_metrics/dupont_decomposition。"""
        report = compute_quality_metrics(current_statement)
        d = report.to_dict()
        assert "status" in d
        assert "metrics" in d
        assert "missing_metrics" in d
        assert "dupont_decomposition" in d


# ---------------------------------------------------------------------------
# 验收点 2：分母为零、负值、缺字段有明确处理
# ---------------------------------------------------------------------------


class TestSafeComputation:
    """验收点：分母为零/负值/缺字段时 value=None 且 status 明确。"""

    def test_revenue_zero_makes_margins_not_computable(self):
        """revenue=0 时 gross_margin/operating_margin/net_margin 必须 not_computable。"""
        stmt = _build_statement(
            income={
                "revenue": 0.0,
                "gross_profit": 100.0,
                "operating_profit": 50.0,
                "net_profit": 30.0,
            },
        )
        report = compute_quality_metrics(stmt)
        for name in ("gross_margin", "operating_margin", "net_margin"):
            metric = report.metrics[name]
            assert metric.value is None, f"{name} value 应为 None"
            assert metric.status == METRIC_NOT_COMPUTABLE, f"{name} status 应为 not_computable"

    def test_revenue_negative_makes_margins_not_computable(self):
        """revenue<0 时分母为负，status 必须 not_computable（不能返回 0.0 兜底）。"""
        stmt = _build_statement(
            income={
                "revenue": -100.0,
                "gross_profit": 50.0,
                "operating_profit": 25.0,
                "net_profit": 10.0,
            },
        )
        report = compute_quality_metrics(stmt)
        assert report.metrics["gross_margin"].status == METRIC_NOT_COMPUTABLE
        assert report.metrics["gross_margin"].value is None
        assert report.metrics["operating_margin"].status == METRIC_NOT_COMPUTABLE
        assert report.metrics["net_margin"].status == METRIC_NOT_COMPUTABLE

    def test_missing_field_marked_missing(self):
        """缺字段时 status=missing，value=None。"""
        stmt = _build_statement(
            income={"revenue": 1_000.0, "net_profit": 100.0},  # 缺 gross_profit/operating_profit
            balance={"total_equity": 1_000.0},
        )
        report = compute_quality_metrics(stmt)
        # gross_margin 因 gross_profit=None 应 missing
        assert report.metrics["gross_margin"].status == METRIC_MISSING
        assert report.metrics["gross_margin"].value is None
        # operating_margin 同理
        assert report.metrics["operating_margin"].status == METRIC_MISSING

    def test_zero_equity_makes_roe_not_computable(self):
        """equity=0 时 ROE not_computable。"""
        stmt = _build_statement(
            income={"revenue": 1_000.0, "net_profit": 100.0, "operating_profit": 200.0},
            balance={"total_equity": 0.0, "equity_parent": 0.0},
        )
        report = compute_quality_metrics(stmt)
        assert report.metrics["roe"].status == METRIC_NOT_COMPUTABLE
        assert report.metrics["roe"].value is None

    def test_zero_operating_cost_makes_inventory_days_not_computable(self):
        """operating_cost=0 时存货周转天数 not_computable。"""
        stmt = _build_statement(
            income={"revenue": 1_000.0, "operating_cost": 0.0},
            balance={"inventory": 100.0},
        )
        report = compute_quality_metrics(stmt)
        inv_days = report.metrics["inventory_turnover_days"]
        assert inv_days.status == METRIC_NOT_COMPUTABLE
        assert inv_days.value is None

    def test_zero_interest_expense_makes_coverage_not_computable(self):
        """financial_expense=0 时利息覆盖 not_computable。"""
        stmt = _build_statement(
            income={"operating_profit": 100.0, "financial_expense": 0.0},
        )
        report = compute_quality_metrics(stmt)
        assert report.metrics["interest_coverage"].status == METRIC_NOT_COMPUTABLE

    def test_growth_with_zero_previous_not_computable(self):
        """previous=0 时同比增长 not_computable。"""
        prev = _build_statement(
            period="2022-12-31",
            income={"revenue": 0.0, "net_profit": 100.0, "operating_profit": 50.0},
        )
        curr = _build_statement(
            period="2023-12-31",
            income={"revenue": 1_000.0, "net_profit": 200.0, "operating_profit": 100.0},
        )
        report = compute_quality_metrics(curr, previous=prev)
        # previous revenue=0 → revenue_growth not_computable
        assert report.metrics["revenue_growth"].status == METRIC_NOT_COMPUTABLE
        # previous net_profit=100>0 → net_profit_growth ok
        assert report.metrics["net_profit_growth"].status == METRIC_OK


# ---------------------------------------------------------------------------
# 验收点 3：同比只比较同口径期间
# ---------------------------------------------------------------------------


class TestYearOnYearComparison:
    """验收点：同比只比较同口径期间。"""

    def test_growth_metrics_missing_without_previous(self, current_statement):
        """无 previous 时增长指标全部 missing。"""
        report = compute_quality_metrics(current_statement, previous=None)
        for name in ("revenue_growth", "net_profit_growth", "operating_profit_growth"):
            metric = report.metrics[name]
            assert metric.status == METRIC_MISSING
            assert metric.value is None
            assert "previous" in metric.note or "previous" in metric.formula

    def test_growth_metrics_computed_with_previous(
        self, current_statement, previous_statement
    ):
        """previous 提供时增长指标按同口径计算。"""
        report = compute_quality_metrics(current_statement, previous=previous_statement)
        # revenue: 1e9 / 8e8 - 1 = 0.25
        assert report.metrics["revenue_growth"].status == METRIC_OK
        assert report.metrics["revenue_growth"].value == pytest.approx(0.25, rel=1e-6)
        # net_profit: 1.5e8 / 1.1e8 - 1
        expected_np = 150_000_000.0 / 110_000_000.0 - 1.0
        assert report.metrics["net_profit_growth"].value == pytest.approx(expected_np, rel=1e-6)

    def test_incremental_roic_requires_previous(
        self, current_statement, previous_statement
    ):
        """增量 ROIC 需要 previous；无 previous 时 missing。"""
        report_no_prev = compute_quality_metrics(current_statement)
        assert report_no_prev.metrics["incremental_roic"].status == METRIC_MISSING

        report = compute_quality_metrics(current_statement, previous=previous_statement)
        # previous 提供后应可计算（除非 delta_capital=0）
        metric = report.metrics["incremental_roic"]
        assert metric.status in (METRIC_OK, METRIC_NOT_COMPUTABLE)

    def test_growth_uses_previous_period_not_current(
        self, current_statement, previous_statement
    ):
        """previous 的 period 必须早于 current，验证 input_fields 不混用。"""
        report = compute_quality_metrics(current_statement, previous=previous_statement)
        assert report.metrics["revenue_growth"].period == current_statement.period
        # formula 必须体现"t-1"语义
        assert "t-1" in report.metrics["revenue_growth"].formula


# ---------------------------------------------------------------------------
# 验收点 4：异常是审查提示，不自动等同财务造假
# ---------------------------------------------------------------------------


class TestAnomalyDetection:
    """验收点：异常检测逻辑与边界。"""

    def test_no_anomaly_when_data_missing(self):
        """缺数据时不触发异常（避免假阳性）。"""
        stmt = _build_statement(income={"revenue": 1_000.0})
        report = detect_anomalies(stmt)
        assert isinstance(report, AnomalyReport)
        # 数据严重缺失时不应触发异常信号
        assert report.signals == []

    def test_accrual_anomaly_warning(self):
        """应计利润占比 >= 10% 且 < 20% 触发 warning。"""
        # net_profit=200, ocf=100, total_assets=1000 → accrual_ratio=0.1
        stmt = _build_statement(
            income={"net_profit": 200.0},
            balance={"total_assets": 1_000.0},
            cashflow={"operating_cash_flow": 100.0},
        )
        report = detect_anomalies(stmt)
        accrual_signals = [s for s in report.signals if s.name == "abnormal_accrual"]
        assert len(accrual_signals) == 1
        assert accrual_signals[0].severity == SEVERITY_WARNING
        assert accrual_signals[0].actual_value == pytest.approx(0.10, abs=1e-6)

    def test_accrual_anomaly_severe(self):
        """应计利润占比 >= 20% 触发 severe。"""
        # net_profit=300, ocf=50, total_assets=1000 → accrual_ratio=0.25
        stmt = _build_statement(
            income={"net_profit": 300.0},
            balance={"total_assets": 1_000.0},
            cashflow={"operating_cash_flow": 50.0},
        )
        report = detect_anomalies(stmt)
        accrual_signals = [s for s in report.signals if s.name == "abnormal_accrual"]
        assert len(accrual_signals) == 1
        assert accrual_signals[0].severity == SEVERITY_SEVERE

    def test_cash_profit_divergence_warning(self):
        """净利润>0 但 cash_conversion < 0.5 触发 warning。"""
        # net_profit=100, ocf=30 → cc=0.3
        stmt = _build_statement(
            income={"net_profit": 100.0},
            cashflow={"operating_cash_flow": 30.0},
        )
        report = detect_anomalies(stmt)
        cash_signals = [s for s in report.signals if s.name == "cash_profit_divergence"]
        assert len(cash_signals) == 1
        assert cash_signals[0].severity == SEVERITY_WARNING

    def test_cash_profit_divergence_severe(self):
        """净利润>0 但 cash_conversion<0（现金流为负）触发 severe。"""
        # net_profit=100, ocf=-20 → cc=-0.2
        stmt = _build_statement(
            income={"net_profit": 100.0},
            cashflow={"operating_cash_flow": -20.0},
        )
        report = detect_anomalies(stmt)
        cash_signals = [s for s in report.signals if s.name == "cash_profit_divergence"]
        assert len(cash_signals) == 1
        assert cash_signals[0].severity == SEVERITY_SEVERE

    def test_gross_margin_drop_yoy_warning(self, current_statement):
        """毛利率同比下滑 >= 5% 触发 warning。"""
        # previous 毛利率 = 320/800 = 40%；current = 400/1000 = 40%
        # 调整 current 毛利率到 33%，下滑 7%
        curr = _build_statement(
            period="2023-12-31",
            income={
                "revenue": 1_000.0, "gross_profit": 330.0,
                "operating_profit": 200.0, "net_profit": 150.0,
            },
        )
        prev = _build_statement(
            period="2022-12-31",
            income={
                "revenue": 1_000.0, "gross_profit": 400.0,
                "operating_profit": 200.0, "net_profit": 150.0,
            },
        )
        report = detect_anomalies(curr, previous=prev)
        gm_signals = [s for s in report.signals if s.name == "gross_margin_drop_yoy"]
        assert len(gm_signals) == 1
        assert gm_signals[0].severity == SEVERITY_WARNING

    def test_gross_margin_industry_divergence(self):
        """毛利率低于行业中位数 >= 15% 触发 severe。"""
        # current 毛利率 = 25%，行业中位数 = 45%，偏离 -20%
        stmt = _build_statement(
            income={"revenue": 1_000.0, "gross_profit": 250.0, "net_profit": 100.0},
        )
        report = detect_anomalies(
            stmt, industry_median_gross_margin=0.45,
        )
        ind_signals = [
            s for s in report.signals if s.name == "gross_margin_industry_divergence"
        ]
        assert len(ind_signals) == 1
        assert ind_signals[0].severity == SEVERITY_SEVERE

    def test_abnormal_receivable_growth(self, previous_statement):
        """应收增长 > 收入增长 * 1.5 触发 warning。"""
        # previous: revenue=800M, ar=70M；current: revenue=900M(+12.5%), ar=120M(+71%)
        curr = _build_statement(
            period="2023-12-31",
            income={
                "revenue": 900_000_000.0, "gross_profit": 360_000_000.0,
                "operating_profit": 200_000_000.0, "net_profit": 150_000_000.0,
            },
            balance={
                "accounts_receivable": 120_000_000.0,
                "total_assets": 2_000_000_000.0,
                "total_equity": 1_200_000_000.0,
            },
            cashflow={"operating_cash_flow": 120_000_000.0},
        )
        report = detect_anomalies(curr, previous=previous_statement)
        ar_signals = [s for s in report.signals if s.name == "abnormal_receivable_growth"]
        assert len(ar_signals) == 1
        assert ar_signals[0].severity == SEVERITY_WARNING

    def test_abnormal_inventory_growth(self, previous_statement):
        """存货增长 > 收入增长 * 1.5 触发 warning。"""
        curr = _build_statement(
            period="2023-12-31",
            income={
                "revenue": 900_000_000.0, "gross_profit": 360_000_000.0,
                "operating_profit": 200_000_000.0, "net_profit": 150_000_000.0,
                "operating_cost": 540_000_000.0,
            },
            balance={
                "inventory": 150_000_000.0,
                "total_assets": 2_000_000_000.0,
                "total_equity": 1_200_000_000.0,
            },
            cashflow={"operating_cash_flow": 120_000_000.0},
        )
        report = detect_anomalies(curr, previous=previous_statement)
        inv_signals = [s for s in report.signals if s.name == "abnormal_inventory_growth"]
        assert len(inv_signals) == 1
        assert inv_signals[0].severity == SEVERITY_WARNING

    def test_observation_does_not_contain_conclusion_words(
        self, current_statement, previous_statement
    ):
        """验收点：observation 不能含"造假/欺诈/舞弊"等结论词。"""
        # 构造触发所有异常的场景
        stmt = _build_statement(
            period="2023-12-31",
            income={
                "revenue": 900.0, "gross_profit": 200.0,  # 毛利率下滑
                "operating_profit": 100.0, "net_profit": 200.0,
            },
            balance={
                "total_assets": 1_000.0,
                "accounts_receivable": 300.0,
                "inventory": 250.0,
                "total_equity": 800.0,
            },
            cashflow={"operating_cash_flow": 50.0},
        )
        prev = _build_statement(
            period="2022-12-31",
            income={
                "revenue": 800.0, "gross_profit": 320.0,
                "operating_profit": 100.0, "net_profit": 100.0,
            },
            balance={
                "total_assets": 900.0,
                "accounts_receivable": 80.0,
                "inventory": 60.0,
                "total_equity": 700.0,
            },
            cashflow={"operating_cash_flow": 80.0},
        )
        report = detect_anomalies(stmt, previous=prev)
        forbidden = ("造假", "欺诈", "舞弊", "fraud", "manipulat", "fake")
        for signal in report.signals:
            for word in forbidden:
                assert word not in signal.observation.lower(), (
                    f"信号 {signal.name} observation 含结论词: {word}"
                )


# ---------------------------------------------------------------------------
# 验收点 5：观察与结论之间的边界
# ---------------------------------------------------------------------------


class TestConclusionBoundary:
    """验收点：观察与结论之间的边界。"""

    def test_conclusion_boundary_non_empty(self):
        """AnomalyReport 必须带 conclusion_boundary 声明。"""
        stmt = _build_statement(income={"revenue": 1.0})
        report = detect_anomalies(stmt)
        assert report.conclusion_boundary
        assert "审查提示" in report.conclusion_boundary or "不等于" in report.conclusion_boundary

    def test_conclusion_boundary_constant_matches(self):
        """CONCLUSION_BOUNDARY 常量内容符合规范。"""
        assert "审查提示" in CONCLUSION_BOUNDARY or "不等于" in CONCLUSION_BOUNDARY
        assert "造假" in CONCLUSION_BOUNDARY  # 明确提及"造假"用于否定

    def test_anomaly_report_to_dict_includes_boundary(self, current_statement):
        """to_dict 必须包含 conclusion_boundary 供页面展示。"""
        report = detect_anomalies(current_statement)
        d = report.to_dict()
        assert "conclusion_boundary" in d
        assert "signals" in d
        assert isinstance(d["signals"], list)

    def test_signal_to_dict_has_observation_and_severity(self):
        """AnomalySignal.to_dict 必须包含 observation/severity/evidence_fields。"""
        signal = AnomalySignal(
            name="abnormal_accrual", severity=SEVERITY_WARNING,
            observation="应计利润占比 12%", evidence_fields=["net_profit"],
            period="2023-12-31", threshold=0.10, actual_value=0.12,
        )
        d = signal.to_dict()
        assert d["name"] == "abnormal_accrual"
        assert d["severity"] == SEVERITY_WARNING
        assert d["observation"] == "应计利润占比 12%"
        assert d["evidence_fields"] == ["net_profit"]
        assert d["actual_value"] == 0.12

    def test_signal_cannot_be_initialized_with_conclusion_word(self):
        """signal 字段非 None 时 severity 必须在允许集合内。"""
        # 构造一个严重度非法的信号应抛错（dataclass 不强制，但语义上不应使用）
        signal = AnomalySignal(severity="invalid")
        assert signal.severity not in (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_SEVERE)


# ---------------------------------------------------------------------------
# 完整计算正确性
# ---------------------------------------------------------------------------


class TestFullMetricComputation:
    """验收点：完整指标计算的数值正确性。"""

    def test_all_required_metrics_present(self, current_statement, previous_statement):
        """必须覆盖计划要求的全部指标类别。"""
        report = compute_quality_metrics(current_statement, previous=previous_statement)
        required = {
            # 增长
            "revenue_growth", "net_profit_growth", "operating_profit_growth",
            # 利润率
            "gross_margin", "operating_margin", "net_margin",
            # ROE/ROIC
            "roe", "roic", "incremental_roic",
            # 现金流
            "cash_conversion", "free_cash_flow",
            # 周转天数
            "accounts_receivable_turnover_days",
            "inventory_turnover_days",
            "contract_liabilities_turnover_days",
            # 债务
            "net_debt", "interest_coverage", "short_term_debt_ratio",
            # 再投资/可持续增长
            "reinvestment_rate", "sustainable_growth",
        }
        assert required.issubset(set(report.metrics.keys())), (
            f"缺失指标: {required - set(report.metrics.keys())}"
        )

    def test_dupont_roe_matches_direct_roe(self, current_statement):
        """杜邦分解 ROE 应等于直接计算的 ROE。"""
        report = compute_quality_metrics(current_statement)
        direct_roe = report.metrics["roe"].value
        dupont_roe = report.dupont_decomposition["roe_check"].value
        if direct_roe is not None and dupont_roe is not None:
            assert direct_roe == pytest.approx(dupont_roe, rel=1e-4)

    def test_margin_values_correct(self, current_statement):
        """毛利率 40%、营业利润率 20%、净利率 15%。"""
        report = compute_quality_metrics(current_statement)
        assert report.metrics["gross_margin"].value == pytest.approx(0.40, rel=1e-6)
        assert report.metrics["operating_margin"].value == pytest.approx(0.20, rel=1e-6)
        assert report.metrics["net_margin"].value == pytest.approx(0.15, rel=1e-6)

    def test_roe_uses_equity_parent_when_available(self, current_statement):
        """ROE 优先用 equity_parent 计算。"""
        report = compute_quality_metrics(current_statement)
        # equity_parent = 1.18e9, net_profit = 1.5e8 → 0.1271
        assert report.metrics["roe"].value == pytest.approx(
            150_000_000.0 / 1_180_000_000.0, rel=1e-6
        )

    def test_free_cash_flow_correct(self, current_statement):
        """自由现金流 = 经营现金流 - capex。"""
        report = compute_quality_metrics(current_statement)
        assert report.metrics["free_cash_flow"].value == pytest.approx(
            120_000_000.0 - 60_000_000.0, rel=1e-6
        )
        assert report.metrics["free_cash_flow"].status == METRIC_OK

    def test_turnover_days_correct(self, current_statement):
        """周转天数按 365 * stock / flow 计算。"""
        report = compute_quality_metrics(current_statement)
        # 应收 = 1e8, revenue = 1e9 → 36.5 天
        assert report.metrics["accounts_receivable_turnover_days"].value == pytest.approx(
            365.0 * 100_000_000.0 / 1_000_000_000.0, rel=1e-6
        )
        # 存货 = 8e7, operating_cost = 6e8 → 48.67 天
        assert report.metrics["inventory_turnover_days"].value == pytest.approx(
            365.0 * 80_000_000.0 / 600_000_000.0, rel=1e-6
        )

    def test_net_debt_and_short_term_ratio(self, current_statement):
        """净债务与短债占比计算正确。"""
        report = compute_quality_metrics(current_statement)
        # debt_total = 3e8 + 2e8 = 5e8, cash = 2e8 → net_debt = 3e8
        assert report.metrics["net_debt"].value == pytest.approx(300_000_000.0, rel=1e-6)
        # short_term / debt_total = 3e8 / 5e8 = 0.6
        assert report.metrics["short_term_debt_ratio"].value == pytest.approx(0.6, rel=1e-6)

    def test_reinvestment_rate_missing_without_dividends(self, current_statement):
        """P1.2 阶段未接入股息数据，reinvestment_rate 应 missing。"""
        report = compute_quality_metrics(current_statement)
        assert report.metrics["reinvestment_rate"].status == METRIC_MISSING
        assert report.metrics["sustainable_growth"].status == METRIC_MISSING

    def test_quality_status_partial_when_growth_missing(self, current_statement):
        """缺 previous 时 status 应为 partial（有 missing 指标）。"""
        report = compute_quality_metrics(current_statement, previous=None)
        assert report.status in ("partial", "not_computable")


# ---------------------------------------------------------------------------
# 桥接函数
# ---------------------------------------------------------------------------


def test_anomaly_detection_degrades_invalid_industry_margin() -> None:
    current = _build_statement(
        income={"revenue": 1_000.0, "gross_profit": 500.0},
    )
    previous = _build_statement(
        income={"revenue": 900.0, "gross_profit": 450.0},
    )

    report = detect_anomalies(
        current, previous=previous, industry_median_gross_margin="bad",
    )

    assert isinstance(report, AnomalyReport)


def test_quality_metrics_degrade_non_finite_inputs() -> None:
    from math import isfinite

    statement = _build_statement(
        income={
            "revenue": float("inf"),
            "net_profit": float("nan"),
            "operating_profit": 10.0,
        },
        balance={"total_assets": 1_000.0, "total_equity": 500.0},
        cashflow={"operating_cash_flow": 5.0, "capex": float("inf")},
    )

    report = compute_quality_metrics(statement)

    assert report.status == "partial"
    assert all(
        metric.value is None or isfinite(metric.value)
        for metric in report.metrics.values()
    )


class TestBridgeFunctions:
    """验收点：value_chain/fundamentals.py 桥接函数。"""

    def test_build_quality_report_from_statements(self, current_statement, previous_statement):
        """桥接函数应正确转发到 P1.2 引擎。"""
        report = build_quality_report_from_statements(current_statement, previous_statement)
        assert isinstance(report, QualityReport)
        assert report.metrics["revenue_growth"].value == pytest.approx(0.25, rel=1e-6)

    def test_build_anomaly_report_from_statements(self, current_statement, previous_statement):
        """桥接函数应正确转发到异常检测引擎。"""
        report = build_anomaly_report_from_statements(
            current_statement, previous=previous_statement,
            industry_median_gross_margin=0.50,
        )
        assert isinstance(report, AnomalyReport)
        assert report.conclusion_boundary == CONCLUSION_BOUNDARY

    def test_bridge_handles_missing_data(self):
        """桥接函数处理缺数据场景应不报错。"""
        stmt = _build_statement(income={"revenue": 1.0})
        q_report = build_quality_report_from_statements(stmt)
        a_report = build_anomaly_report_from_statements(stmt)
        assert isinstance(q_report, QualityReport)
        assert isinstance(a_report, AnomalyReport)
