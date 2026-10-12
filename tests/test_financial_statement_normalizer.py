"""P1.1 标准化三表 normalizer + repository 测试。

按 P1 计划 Task P1.1 验收点设计：
- [ ] 三表期间、币种和单位统一
- [ ] 资产 = 负债 + 权益 差异在数据精度允许范围内，否则标记不平
- [ ] 现金流变化与现金余额能够勾稽或明确解释
- [ ] 重述前后版本可比较
- [ ] 未来财报不会进入历史 as_of
"""
from __future__ import annotations

import pytest

from core.research.fundamentals.contracts import (
    ADJUSTMENT_KINDS,
    BalanceCheck,
    NormalizedFinancialStatement,
    StatementLine,
    validate_adjustment_kind,
)
from core.research.fundamentals.normalizer import (
    add_adjustment,
    apply_pit_filter,
    check_balance,
    normalize_statement,
)
from core.research.fundamentals.repository import StatementRepository


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def income_row() -> dict:
    """利润表原始行（tushare 风格）。"""
    return {
        "ts_code": "600519.SH",
        "ann_date": "20240328",
        "end_date": "20231231",
        "revenue": 1.5e11,
        "operating_cost": 1.0e11,
        "gross_profit": 5.0e10,
        "operating_profit": 7.5e10,
        "n_income_attr_p": 6.0e10,
        "income_tax_expense": 1.5e10,
    }


@pytest.fixture
def balance_row() -> dict:
    """资产负债表原始行。"""
    return {
        "ts_code": "600519.SH",
        "ann_date": "20240328",
        "end_date": "20231231",
        "total_assets": 1.0e11,
        "total_liabilities": 3.0e10,
        "total_equity": 7.0e10,  # 资产=负债+权益，平
    }


@pytest.fixture
def cashflow_row() -> dict:
    """现金流量表原始行（勾稽通过）。"""
    return {
        "ts_code": "600519.SH",
        "ann_date": "20240328",
        "end_date": "20231231",
        "n_cashflow_act": 5.0e10,
        "n_cashflow_inv_act": -2.0e10,
        "n_cashflow_fin_act": -1.0e10,
        "n_cash_flows_act": 2.0e10,  # 净增加
        "c_cash_equ_bgn_period": 1.0e10,  # 期初
        "c_cash_equ_end_period": 3.0e10,  # 期末，期末-期初=2e10=净增加
    }


@pytest.fixture
def statement(income_row, balance_row, cashflow_row) -> NormalizedFinancialStatement:
    """标准化三表（2023年报，2024-03-28 披露）。"""
    return normalize_statement(
        symbol="600519.SH",
        period="20231231",
        published_at="2024-03-28",
        available_at="2024-03-28",
        provider="tushare",
        income_row=income_row,
        balance_row=balance_row,
        cashflow_row=cashflow_row,
    )


# ---------------------------------------------------------------------------
# normalize_statement
# ---------------------------------------------------------------------------


class TestNormalizeStatement:
    """验收：三表期间、币种和单位统一。"""

    def test_period_normalized_to_iso(self, statement):
        """期间应标准化为 YYYY-MM-DD。"""
        assert statement.period == "2023-12-31"

    def test_currency_and_unit_unified(self, statement):
        """币种与单位在所有三表中统一。"""
        for line in statement.income_statement + statement.balance_sheet + statement.cash_flow_statement:
            assert line.currency == "CNY"
            assert line.unit == "元"

    def test_source_trace_id_propagated_to_lines(self, statement):
        """所有非空行必须带 source_ref。"""
        for line in statement.income_statement:
            if line.value is not None:
                assert line.source_ref == statement.source_trace_id
                assert line.grounding_status == "grounding_ok"

    def test_missing_value_marked_as_missing(self, statement):
        """缺失字段的 grounding_status 必须为 'missing'。"""
        rd_line = statement.get_line("income", "rd_expense")
        assert rd_line is not None
        assert rd_line.value is None
        assert rd_line.grounding_status == "missing"

    def test_reported_value_preserved(self, statement):
        """reported_value 保留原始披露值便于审计。"""
        revenue = statement.get_line("income", "revenue")
        assert revenue is not None
        assert revenue.reported_value == 1.5e11
        assert revenue.value == 1.5e11


# ---------------------------------------------------------------------------
# check_balance
# ---------------------------------------------------------------------------


class TestCheckBalance:
    """验收：资产=负债+权益；现金流变化与现金余额勾稽。"""

    def test_balance_sheet_balanced(self, statement):
        """资产=负债+权益 平衡。"""
        check = check_balance(statement, tolerance=1.0)
        assert isinstance(check, BalanceCheck)
        assert check.asset_liability_equity_balanced is True
        assert check.asset_liability_equity_diff == 0.0
        assert check.notes == []

    def test_balance_sheet_imbalance_marked(self, statement):
        """资产 != 负债+权益 时应标记不平并附备注。"""
        # 构造不平的资产负债表
        bad = NormalizedFinancialStatement(
            symbol=statement.symbol,
            period=statement.period,
            balance_sheet=[
                StatementLine(metric_name="total_assets", value=1.0e11, unit="元", currency="CNY"),
                StatementLine(metric_name="total_liabilities", value=3.0e10, unit="元", currency="CNY"),
                StatementLine(metric_name="total_equity", value=6.0e10, unit="元", currency="CNY"),  # 差 1e10
            ],
        )
        check = check_balance(bad, tolerance=1.0)
        assert check.asset_liability_equity_balanced is False
        assert abs(check.asset_liability_equity_diff - 1.0e10) < 1.0
        assert any("差异" in note for note in check.notes)

    def test_cash_flow_reconciled(self, statement):
        """现金流净增加 = 期末 - 期初。"""
        check = check_balance(statement, tolerance=1.0)
        assert check.cash_reconciliation_explained is True
        assert check.cash_reconciliation_diff == 0.0

    def test_cash_flow_imbalance_marked(self, statement):
        """现金流不勾稽时标记并附备注。"""
        bad = NormalizedFinancialStatement(
            symbol=statement.symbol,
            period=statement.period,
            cash_flow_statement=[
                StatementLine(metric_name="cash_and_equivalents_begin", value=1.0e10, unit="元", currency="CNY"),
                StatementLine(metric_name="cash_and_equivalents_end", value=3.0e10, unit="元", currency="CNY"),
                StatementLine(metric_name="net_cash_increase", value=5.0e10, unit="元", currency="CNY"),  # 差 3e10
            ],
        )
        check = check_balance(bad, tolerance=1.0)
        assert check.cash_reconciliation_explained is False
        assert any("现金流" in note for note in check.notes)

    def test_missing_data_returns_none(self, statement):
        """缺数据时 diff/balanced 均为 None。"""
        empty = NormalizedFinancialStatement(symbol="000001", period="2023-12-31")
        check = check_balance(empty)
        assert check.asset_liability_equity_diff is None
        assert check.asset_liability_equity_balanced is None
        assert check.cash_reconciliation_diff is None
        assert check.cash_reconciliation_explained is None


# ---------------------------------------------------------------------------
# apply_pit_filter
# ---------------------------------------------------------------------------


class TestPitFilter:
    """验收：未来财报不会进入历史 as_of。"""

    def test_future_records_excluded(self, statement):
        """available_at > as_of 的报表应被排除。"""
        future = NormalizedFinancialStatement(
            symbol="600519.SH",
            period="2024-06-30",
            published_at="2024-08-28",
            available_at="2024-08-28",
        )
        result = apply_pit_filter([statement, future], as_of="2024-04-01")
        periods = [s.period for s in result]
        assert "2023-12-31" in periods
        assert "2024-06-30" not in periods

    def test_multiple_periods_filtered(self, statement):
        """同时存在多个期间时只保留 <= as_of 的。"""
        s1 = statement
        s2 = NormalizedFinancialStatement(
            symbol="600519.SH", period="2024-06-30", available_at="2024-08-28"
        )
        s3 = NormalizedFinancialStatement(
            symbol="600519.SH", period="2024-12-31", available_at="2025-03-28"
        )
        result = apply_pit_filter([s1, s2, s3], as_of="2024-09-01")
        periods = [s.period for s in result]
        assert set(periods) == {"2023-12-31", "2024-06-30"}

    def test_latest_version_kept_for_same_period(self, statement):
        """同 period 多版本只保留最新 restatement_version。"""
        s_v1 = statement
        s_v2 = NormalizedFinancialStatement(
            symbol="600519.SH",
            period="2023-12-31",
            available_at="2024-05-01",
            restatement_version=2,
        )
        result = apply_pit_filter([s_v1, s_v2], as_of="2024-09-01")
        assert len(result) == 1
        assert result[0].restatement_version == 2


# ---------------------------------------------------------------------------
# add_adjustment
# ---------------------------------------------------------------------------


class TestAdjustments:
    """验收：每项调整必须有来源；无来源只能创建 gap。"""

    def test_add_valid_adjustment(self, statement):
        """合法调整追加成功。"""
        new = add_adjustment(
            statement,
            kind="government_grant",
            amount=5.0e8,
            source_ref="evidence:gov_grant_2023",
            note="2023 年度政府补助调整",
        )
        assert len(new.adjustments) == 1
        assert new.adjustments[0].kind == "government_grant"
        assert new.adjustments[0].amount == 5.0e8
        # 原对象不可变
        assert len(statement.adjustments) == 0

    def test_invalid_kind_rejected(self, statement):
        """非法 kind 抛 ValueError。"""
        with pytest.raises(ValueError):
            add_adjustment(
                statement,
                kind="fake_kind",
                amount=1.0,
                source_ref="ref",
            )

    def test_missing_source_ref_rejected(self, statement):
        """无 source_ref 抛 ValueError。"""
        with pytest.raises(ValueError):
            add_adjustment(
                statement,
                kind="impairment",
                amount=1.0,
                source_ref="",
            )

    def test_all_adjustment_kinds_accepted(self, statement):
        """所有 ADJUSTMENT_KINDS 应可被接受。"""
        for i, kind in enumerate(ADJUSTMENT_KINDS):
            new = add_adjustment(
                statement,
                kind=kind,
                amount=1.0,
                source_ref=f"ref:{i}",
            )
            assert new.adjustments[-1].kind == kind

    def test_validate_adjustment_kind_helper(self):
        """validate_adjustment_kind 辅助函数工作。"""
        validate_adjustment_kind("impairment")
        with pytest.raises(ValueError):
            validate_adjustment_kind("nonexistent")


# ---------------------------------------------------------------------------
# StatementRepository
# ---------------------------------------------------------------------------


class TestStatementRepository:
    """验收：重述前后版本可比较；旧版本不可覆盖。"""

    def test_save_and_retrieve(self, statement):
        """保存后可按 symbol+period 查询。"""
        repo = StatementRepository()
        repo.save(statement)
        latest = repo.get_latest("600519.SH", "2023-12-31")
        assert latest is not None
        assert latest.symbol == "600519.SH"
        assert latest.period == "2023-12-31"

    def test_duplicate_save_rejected(self, statement):
        """同 (symbol, period, version) 重复保存应抛异常。"""
        repo = StatementRepository()
        repo.save(statement)
        with pytest.raises(ValueError, match="不可覆盖"):
            repo.save(statement)

    def test_restatement_version_strictly_increasing(self, statement):
        """重述版本号必须严格递增。"""
        repo = StatementRepository()
        repo.save(statement)

        def factory(previous: NormalizedFinancialStatement) -> NormalizedFinancialStatement:
            return NormalizedFinancialStatement(
                symbol=previous.symbol,
                period=previous.period,
                published_at=previous.published_at,
                available_at="2024-05-01",
                restatement_version=2,
            )

        new_stmt = repo.save_restatement(
            "600519.SH", "2023-12-31", new_version=2, factory=factory
        )
        assert new_stmt.restatement_version == 2
        assert repo.list_versions("600519.SH", "2023-12-31") == [1, 2]

    def test_restatement_version_not_increasing_rejected(self, statement):
        """重述版本号 <= 当前最大版本号应抛异常。"""
        repo = StatementRepository()
        repo.save(statement)

        def factory(previous):
            return NormalizedFinancialStatement(
                symbol=previous.symbol,
                period=previous.period,
                restatement_version=1,
            )

        with pytest.raises(ValueError, match="严格递增"):
            repo.save_restatement(
                "600519.SH", "2023-12-31", new_version=1, factory=factory
            )

    def test_pit_filter_in_repository(self, statement):
        """repository.get_latest 应遵守 as_of PIT 限制。"""
        repo = StatementRepository()
        repo.save(statement)

        # as_of 早于 available_at 应查不到
        assert repo.get_latest("600519.SH", "2023-12-31", as_of="2024-01-01") is None
        # as_of 晚于 available_at 应能查到
        assert repo.get_latest("600519.SH", "2023-12-31", as_of="2024-04-01") is not None

    def test_list_periods_respects_pit(self, statement):
        """list_periods 也应遵守 PIT 限制。"""
        repo = StatementRepository()
        repo.save(statement)
        early = NormalizedFinancialStatement(
            symbol="600519.SH",
            period="2022-12-31",
            published_at="2023-03-28",
            available_at="2023-03-28",
        )
        repo.save(early)

        periods_before_apr_2024 = repo.list_periods("600519.SH", as_of="2024-04-01")
        assert "2023-12-31" in periods_before_apr_2024
        periods_before_jan_2024 = repo.list_periods("600519.SH", as_of="2024-01-01")
        assert "2023-12-31" not in periods_before_jan_2024
        assert "2022-12-31" in periods_before_jan_2024

    def test_restatement_versions_comparable(self, statement):
        """重述前后版本可比较（保留旧版本数据）。"""
        repo = StatementRepository()
        repo.save(statement)
        original_revenue = 1.5e11

        def factory(previous: NormalizedFinancialStatement) -> NormalizedFinancialStatement:
            # 模拟重述：营收上修 5%
            new_lines = [
                StatementLine(
                    metric_name="revenue",
                    value=previous_revenue * 1.05,
                    unit="元",
                    currency="CNY",
                    source_ref=previous.source_trace_id,
                    grounding_status="grounding_ok",
                    reported_value=previous_revenue * 1.05,
                )
                for previous_revenue in [
                    line.value for line in previous.income_statement if line.metric_name == "revenue"
                ]
            ]
            return NormalizedFinancialStatement(
                symbol=previous.symbol,
                period=previous.period,
                published_at=previous.published_at,
                available_at="2024-05-01",
                income_statement=new_lines,
                restatement_version=2,
            )

        repo.save_restatement("600519.SH", "2023-12-31", 2, factory)
        # 旧版本仍可查
        versions = repo.list_versions("600519.SH", "2023-12-31")
        assert versions == [1, 2]
        # 最新版本营收上修
        latest = repo.get_latest("600519.SH", "2023-12-31")
        assert latest is not None
        latest_revenue = latest.get_line("income", "revenue")
        assert latest_revenue is not None
        assert abs(latest_revenue.value - original_revenue * 1.05) < 1.0
