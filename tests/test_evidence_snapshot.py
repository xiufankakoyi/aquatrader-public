"""P1.10 evidence snapshot 与 Markdown 渲染器专项测试。

覆盖 2 个测试类：
1. TestBuildCompanyEvidenceSnapshot: 字段快照构建器
   - 三表快照含完整 income_statement / balance_sheet / cash_flow_statement
   - key_metrics 6 个字段都有值
   - balance_check 计算正确（asset = liability + equity 时 balanced=true）
   - balance_check 缺字段时 diff=null
   - valuation_cases 列表每个含 assumptions + source_refs
   - consensus_estimates 含 institution_id / analyst_id / values
   - cfa_report_summary 含 10 章节状态
   - data_gaps 列表
   - review_checklist_hints 含 5 项
2. TestRenderCfaReportToMarkdown: Markdown 渲染器
   - Markdown 含公司元数据 + notice
   - 10 章节都有标题
   - 章节状态标记 ✓ / ⚠ / ✗
   - 数值格式：百分比 2 位、金额用 万/亿
   - 缺字段显示"暂无数据"
   - 末尾含「人工复核 Checklist 提示」章节
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.research.reporting.contracts import (
    NOTICE,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    ReportSection,
    ReportSectionContent,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_TITLES,
    CfaReport,
    empty_section,
)
from core.research.reporting.evidence_snapshot import (
    build_company_evidence_snapshot,
)
from core.research.reporting.cfa_report_markdown import (
    render_cfa_report_to_markdown,
    render_cfa_report_section_to_markdown,
    render_evidence_snapshot_to_markdown,
)


# ---------------------------------------------------------------------------
# Mock 数据：含完整三表
# ---------------------------------------------------------------------------

@dataclass
class _MockStatement:
    """模拟 NormalizedFinancialStatement，含完整三表行数据。"""
    symbol: str = ""
    period: str = "2024-12-31"
    published_at: str = "2025-03-28"
    available_at: str = "2025-03-28"
    currency: str = "CNY"
    unit: str = "元"
    income_statement: list[dict[str, Any]] = field(default_factory=list)
    balance_sheet: list[dict[str, Any]] = field(default_factory=list)
    cash_flow_statement: list[dict[str, Any]] = field(default_factory=list)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    accounting_policy_refs: list[str] = field(default_factory=list)
    restatement_version: int = 1
    source_trace_id: str = "trace-stmt-001"
    schema_version: str = "normalized_statement_v1"

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "currency": self.currency,
            "unit": self.unit,
            "income_statement": list(self.income_statement),
            "balance_sheet": list(self.balance_sheet),
            "cash_flow_statement": list(self.cash_flow_statement),
            "adjustments": list(self.adjustments),
            "accounting_policy_refs": list(self.accounting_policy_refs),
            "restatement_version": self.restatement_version,
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }


def _make_full_statement(symbol: str = "600519.SH") -> _MockStatement:
    """构造含完整三表行数据的 statement（资产=负债+权益 平衡）。"""
    return _MockStatement(
        symbol=symbol,
        period="2024-12-31",
        income_statement=[
            {"metric_name": "revenue", "value": 150_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-rev-1",
             "grounding_status": "grounding_ok", "reported_value": 150_000_000_000.0},
            {"metric_name": "net_profit", "value": 75_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-np-1",
             "grounding_status": "grounding_ok", "reported_value": 75_000_000_000.0},
        ],
        balance_sheet=[
            {"metric_name": "total_assets", "value": 200_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-ta-1",
             "grounding_status": "grounding_ok", "reported_value": 200_000_000_000.0},
            {"metric_name": "total_liabilities", "value": 50_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-tl-1",
             "grounding_status": "grounding_ok", "reported_value": 50_000_000_000.0},
            {"metric_name": "total_equity", "value": 150_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-te-1",
             "grounding_status": "grounding_ok", "reported_value": 150_000_000_000.0},
        ],
        cash_flow_statement=[
            {"metric_name": "operating_cash_flow", "value": 80_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-ocf-1",
             "grounding_status": "grounding_ok", "reported_value": 80_000_000_000.0},
            {"metric_name": "cash_begin", "value": 100_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-cb-1",
             "grounding_status": "grounding_ok", "reported_value": 100_000_000_000.0},
            {"metric_name": "cash_end", "value": 180_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-ce-1",
             "grounding_status": "grounding_ok", "reported_value": 180_000_000_000.0},
            {"metric_name": "net_increase_in_cash", "value": 80_000_000_000.0, "unit": "元",
             "currency": "CNY", "source_ref": "src-ni-1",
             "grounding_status": "grounding_ok", "reported_value": 80_000_000_000.0},
        ],
        accounting_policy_refs=["policy-1"],
        source_trace_id="trace-stmt-001",
    )


def _make_unbalanced_statement(symbol: str = "300750.SZ") -> _MockStatement:
    """构造资产≠负债+权益 的 statement（用于测不平衡场景）。"""
    stmt = _make_full_statement(symbol)
    # 把权益改成 100 亿，导致 diff = 200 - 50 - 100 = 50（不平衡）
    stmt.balance_sheet = [
        {"metric_name": "total_assets", "value": 200_000_000_000.0, "unit": "元",
         "currency": "CNY", "source_ref": "src-ta-1",
         "grounding_status": "grounding_ok", "reported_value": 200_000_000_000.0},
        {"metric_name": "total_liabilities", "value": 50_000_000_000.0, "unit": "元",
         "currency": "CNY", "source_ref": "src-tl-1",
         "grounding_status": "grounding_ok", "reported_value": 50_000_000_000.0},
        {"metric_name": "total_equity", "value": 100_000_000_000.0, "unit": "元",
         "currency": "CNY", "source_ref": "src-te-1",
         "grounding_status": "grounding_ok", "reported_value": 100_000_000_000.0},
    ]
    return stmt


def _make_partial_statement(symbol: str = "600276.SH") -> _MockStatement:
    """构造缺 total_equity 字段的 statement（用于测缺字段场景）。"""
    stmt = _make_full_statement(symbol)
    stmt.balance_sheet = [
        {"metric_name": "total_assets", "value": 100_000_000_000.0, "unit": "元",
         "currency": "CNY", "source_ref": "src-ta-1",
         "grounding_status": "grounding_ok", "reported_value": 100_000_000_000.0},
        # 缺 total_liabilities 与 total_equity
    ]
    return stmt


# ---------------------------------------------------------------------------
# Mock 仓储
# ---------------------------------------------------------------------------

class _MockFundamentalsRepo:
    """模拟 StatementRepository。"""

    def __init__(self, statements: dict[str, Any] | None = None) -> None:
        self.statements = statements or {}

    def get_latest_statement(self, symbol: str, as_of: str = "") -> Any:
        """取最新 statement。"""
        return self.statements.get(symbol)

    def get_latest(self, symbol: str, period: str, as_of: str = "") -> Any:
        """兼容旧签名。"""
        return self.statements.get(symbol)


class _MockExpectationRepo:
    """模拟 ExpectationRepository。"""

    def __init__(self, consensus: list[dict[str, Any]] | None = None) -> None:
        self.consensus = consensus or []

    def list(self, table: str, *, index_value: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """查询 list。"""
        if table != "consensus_estimates":
            return []
        items = self.consensus
        if index_value:
            items = [c for c in items if c.get("company_symbol") == index_value]
        return items[:limit]


class _MockValuationRepo:
    """模拟 ValuationRepository。"""

    def __init__(self, cases: list[dict[str, Any]] | None = None) -> None:
        self.cases = cases or []

    def list_cases(
        self,
        *,
        company_symbol: str = "",
        method: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询 cases。"""
        items = self.cases
        if company_symbol:
            items = [c for c in items if c.get("company_symbol") == company_symbol]
        return items[:limit]


# ---------------------------------------------------------------------------
# Mock CfaReport 构造
# ---------------------------------------------------------------------------

def _make_full_cfa_report(symbol: str = "600519.SH", name: str = "贵州茅台") -> CfaReport:
    """构造含 10 章节的完整 CfaReport。"""
    sections: list[ReportSectionContent] = []
    for sec_value in ReportSection.ordered_values():
        sections.append(ReportSectionContent(
            section=sec_value,
            title=SECTION_TITLES[sec_value],
            content={"test_key": "test_value", "amount_field": 1_500_000_000.0},
            limitations=["test_limitation"] if sec_value == ReportSection.FINANCIAL_QUALITY.value else [],
            source_refs=["src-test-1"] if sec_value != ReportSection.SCOPE_AND_AS_OF.value else [],
            section_status=SECTION_STATUS_OK,
        ))
    return CfaReport(
        company_symbol=symbol,
        company_name=name,
        as_of="2024-12-31",
        lookback_days=180,
        sections=sections,
        overall_status=OVERALL_STATUS_OK,
        notice=NOTICE,
        model_version="v1",
    )


def _make_partial_cfa_report(symbol: str = "300750.SZ", name: str = "宁德时代") -> CfaReport:
    """构造含 missing/partial 章节的 CfaReport。"""
    sections: list[ReportSectionContent] = []
    for idx, sec_value in enumerate(ReportSection.ordered_values()):
        if idx == 0:
            status = SECTION_STATUS_OK
        elif idx == 1:
            status = SECTION_STATUS_PARTIAL
        else:
            status = SECTION_STATUS_MISSING
        sections.append(ReportSectionContent(
            section=sec_value,
            title=SECTION_TITLES[sec_value],
            content={"wacc": 0.085, "terminal_growth": 0.025, "revenue": 150_000_000_000.0},
            limitations=["limitation"] if status != SECTION_STATUS_OK else [],
            source_refs=["src-test-1"] if status != SECTION_STATUS_MISSING else [],
            section_status=status,
        ))
    return CfaReport(
        company_symbol=symbol,
        company_name=name,
        as_of="2024-12-31",
        lookback_days=180,
        sections=sections,
        overall_status=OVERALL_STATUS_PARTIAL,
        degradation_reasons=[
            "section_partial:strategy_business_model",
            "section_missing:industry_competition",
            "section_missing:financial_quality",
        ],
        notice=NOTICE,
        model_version="v1",
    )


# ---------------------------------------------------------------------------
# 测试类 1: TestBuildCompanyEvidenceSnapshot
# ---------------------------------------------------------------------------

class TestBuildCompanyEvidenceSnapshot:
    """测试 build_company_evidence_snapshot 函数。"""

    def test_snapshot_contains_all_top_level_keys(self) -> None:
        """快照包含所有顶层键。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台", "industry": "白酒"},
        )
        for key in ("meta", "fundamentals_latest", "key_metrics", "balance_check",
                    "valuation_cases", "consensus_estimates", "cfa_report_summary",
                    "data_gaps", "review_checklist_hints"):
            assert key in snapshot, f"missing top-level key: {key}"

    def test_fundamentals_latest_contains_three_statements(self) -> None:
        """三表快照含完整 income_statement / balance_sheet / cash_flow_statement。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台", "industry": "白酒"},
        )
        fund = snapshot["fundamentals_latest"]
        assert fund is not None
        assert "income_statement" in fund
        assert "balance_sheet" in fund
        assert "cash_flow_statement" in fund
        assert len(fund["income_statement"]) >= 1
        assert len(fund["balance_sheet"]) >= 3  # total_assets + total_liabilities + total_equity
        assert len(fund["cash_flow_statement"]) >= 1

    def test_key_metrics_all_six_have_values(self) -> None:
        """6 个关键指标都有值。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台", "industry": "白酒"},
        )
        key_metrics = snapshot["key_metrics"]
        expected_keys = ("revenue", "net_profit", "total_assets",
                         "total_liabilities", "equity", "operating_cash_flow")
        for key in expected_keys:
            assert key in key_metrics, f"missing key_metric: {key}"
            m = key_metrics[key]
            assert m["value"] is not None, f"key_metric {key} value is None"
            assert m["unit"] == "元"
            assert m["currency"] == "CNY"
            assert m["period"] == "2024-12-31"
            assert m["source_trace_id"]  # 非空

    def test_balance_check_balanced_when_assets_equal_liab_plus_equity(self) -> None:
        """资产 = 负债 + 权益 时 balanced=true。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        bc = snapshot["balance_check"]
        # 200 = 50 + 150，diff=0
        assert bc["asset_liability_equity_diff"] is not None
        assert abs(bc["asset_liability_equity_diff"]) < 1.0
        assert bc["asset_liability_equity_balanced"] is True
        # 现金流勾稽：180 - 100 - 80 = 0，平衡
        assert bc["cash_reconciliation_diff"] is not None
        assert abs(bc["cash_reconciliation_diff"]) < 1.0
        assert bc["cash_reconciliation_explained"] is True

    def test_review_hint_does_not_pass_when_cash_reconciliation_is_missing(self) -> None:
        """现金勾稽缺字段时，三表复核提示不能显示通过。"""
        statement = _make_full_statement()
        statement.cash_flow_statement = [statement.cash_flow_statement[0]]
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": statement}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )

        hint = snapshot["review_checklist_hints"][0]
        assert hint["computed_result"] == "无法计算（现金流勾稽缺字段）"
        assert hint["data_availability"] == "partial"

    def test_cash_reconciliation_accepts_p1_loader_metric_names(self) -> None:
        """勾稽器识别 P1DataLoader 实际保存的现金流字段名。"""
        statement = _make_full_statement()
        statement.cash_flow_statement = [
            {"metric_name": "cash_change", "value": 80.0},
            {"metric_name": "cash_and_equivalents_begin", "value": 100.0},
            {"metric_name": "cash_and_equivalents_end", "value": 180.0},
        ]
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": statement}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )

        assert snapshot["balance_check"]["cash_reconciliation_explained"] is True
        assert snapshot["review_checklist_hints"][0]["computed_result"] == "通过（diff < 1.0）"

    def test_balance_check_unbalanced_when_diff_nonzero(self) -> None:
        """资产≠负债+权益 时 balanced=false。"""
        fund_repo = _MockFundamentalsRepo({"300750.SZ": _make_unbalanced_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="300750.SZ", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "宁德时代"},
        )
        bc = snapshot["balance_check"]
        # 200 - 50 - 100 = 50，不平衡
        assert bc["asset_liability_equity_diff"] == 50_000_000_000.0
        assert bc["asset_liability_equity_balanced"] is False

    def test_balance_check_diff_null_when_missing_field(self) -> None:
        """缺字段时 diff=null, balanced=null。"""
        fund_repo = _MockFundamentalsRepo({"600276.SH": _make_partial_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600276.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "恒瑞医药"},
        )
        bc = snapshot["balance_check"]
        assert bc["asset_liability_equity_diff"] is None
        assert bc["asset_liability_equity_balanced"] is None

    def test_balance_check_diff_null_when_no_fundamentals(self) -> None:
        """无 fundamentals 时 diff=null。"""
        fund_repo = _MockFundamentalsRepo({})
        snapshot = build_company_evidence_snapshot(
            symbol="000001.SZ", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
        )
        bc = snapshot["balance_check"]
        assert bc["asset_liability_equity_diff"] is None
        assert bc["asset_liability_equity_balanced"] is None
        assert snapshot["fundamentals_latest"] is None

    def test_valuation_cases_contain_assumptions_and_source_refs(self) -> None:
        """valuation_cases 列表每个含 assumptions + source_refs。"""
        cases = [
            {
                "case_id": "valc_001", "company_symbol": "600519.SH",
                "method": "dcf_fcff", "as_of": "2024-12-31", "scenario": "base",
                "output_interval": {"low": 1500.0, "high": 1800.0},
                "output_per_share": {"low": 1500.0, "high": 1800.0},
                "assumptions": [
                    {"name": "wacc", "value": 0.085, "unit": "percent",
                     "source_ref": "claim-wacc-1", "as_of": "2024-12-31"},
                    {"name": "terminal_growth", "value": 0.025, "unit": "percent",
                     "source_ref": "claim-g-1", "as_of": "2024-12-31"},
                ],
                "source_refs": ["claim-wacc-1", "claim-g-1"],
                "formula_trace": ["EV = sum(FCFF / (1+wacc)^t) + TV"],
                "computability": "computable",
                "limitations": [],
                "model_version": "v1",
                "inputs_snapshot": {
                    "comparable_companies": [
                        {"symbol": "000858.SZ", "pe": 25.0, "pb": 5.0},
                    ],
                },
                "sensitivity": [],
                "created_at": "2025-01-01T00:00:00+00:00",
            },
        ]
        val_repo = _MockValuationRepo(cases=cases)
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=val_repo,
            company_meta={"name": "贵州茅台"},
        )
        val_cases = snapshot["valuation_cases"]
        assert len(val_cases) == 1
        case = val_cases[0]
        assert case["case_id"] == "valc_001"
        assert len(case["assumptions"]) == 2
        assert len(case["source_refs"]) == 2
        assert case["comparable_companies"]  # 含可比公司
        assert case["formula_trace"]

    def test_consensus_estimates_contain_institution_analyst_values(self) -> None:
        """consensus_estimates 含 institution_id / analyst_id / values。"""
        consensus = [
            {
                "consensus_id": "cons_001", "company_symbol": "600519.SH",
                "provider": "wind", "institution_id": "inst_wind_001",
                "analyst_id": "analyst_zhang", "sample_size": 5,
                "revision_of": "", "created_at": "2025-01-01T00:00:00+00:00",
                "values": [
                    {"metric": "net_profit", "value": {"low": 75e9, "high": 80e9},
                     "period": "2025", "published_at": "2025-01-01",
                     "provider": "wind"},
                ],
            },
            {
                "consensus_id": "cons_002", "company_symbol": "000858.SZ",
                "provider": "choice", "institution_id": "inst_choice_001",
                "analyst_id": "analyst_li",
                "values": [{"metric": "revenue", "value": {"low": 80e9, "high": 90e9}}],
            },
        ]
        exp_repo = _MockExpectationRepo(consensus=consensus)
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=exp_repo,
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        ce = snapshot["consensus_estimates"]
        assert len(ce) == 1  # 只过滤 600519.SH
        assert ce[0]["consensus_id"] == "cons_001"
        assert ce[0]["institution_id"] == "inst_wind_001"
        assert ce[0]["analyst_id"] == "analyst_zhang"
        assert len(ce[0]["values"]) == 1

    def test_cfa_report_summary_contains_ten_sections(self) -> None:
        """cfa_report_summary 含 10 章节状态。"""
        cfa_report = _make_full_cfa_report()
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
            cfa_report=cfa_report,
        )
        summary = snapshot["cfa_report_summary"]
        assert summary is not None
        assert summary["overall_status"] == OVERALL_STATUS_OK
        assert len(summary["sections"]) == 10
        # 每个章节摘要含 section / title / section_status / source_refs_count
        for sec in summary["sections"]:
            assert "section" in sec
            assert "title" in sec
            assert "section_status" in sec
            assert "source_refs_count" in sec

    def test_cfa_report_summary_none_when_not_passed(self) -> None:
        """未传 cfa_report 时 cfa_report_summary=None。"""
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
        )
        assert snapshot["cfa_report_summary"] is None

    def test_data_gaps_listed_when_missing_data(self) -> None:
        """缺数据时 data_gaps 列表非空。"""
        snapshot = build_company_evidence_snapshot(
            symbol="000001.SZ", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
        )
        gaps = snapshot["data_gaps"]
        assert len(gaps) > 0
        gap_fields = [g["field"] for g in gaps]
        assert "fundamentals_latest" in gap_fields
        assert "valuation_cases" in gap_fields
        assert "consensus_estimates" in gap_fields

    def test_review_checklist_hints_contains_five_items(self) -> None:
        """review_checklist_hints 含 5 项。"""
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        hints = snapshot["review_checklist_hints"]
        assert len(hints) == 5
        items = [h["item"] for h in hints]
        assert "1_三表勾稽" in items
        assert "2_分部收入加总" in items
        assert "3_前瞻假设" in items
        assert "4_估值关键假设" in items
        assert "5_同业可比口径" in items
        for hint in hints:
            assert "field_location" in hint
            assert "pass_criteria" in hint
            assert "computed_result" in hint
            assert "data_availability" in hint

    def test_meta_contains_required_fields(self) -> None:
        """meta 含 symbol/name/industry/as_of/generated_at/data_source/notice。"""
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo(),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台", "industry": "白酒"},
        )
        meta = snapshot["meta"]
        assert meta["symbol"] == "600519.SH"
        assert meta["name"] == "贵州茅台"
        assert meta["industry"] == "白酒"
        assert meta["as_of"] == "2024-12-31"
        assert meta["generated_at"]
        assert meta["data_source"] == "tushare"
        assert "研究数据产物" in meta["notice"]

    def test_load_result_backward_compatible(self) -> None:
        """load_result 向后兼容字段。"""
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo(),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            load_result={"symbol": "600519.SH", "success": True,
                         "statements_loaded": 8, "consensus_loaded": 226,
                         "valuation_loaded": 3, "errors": [], "data_source": "tushare"},
        )
        assert snapshot["load_result"]["statements_loaded"] == 8
        assert snapshot["load_result"]["consensus_loaded"] == 226

    def test_no_exception_when_all_repos_none(self) -> None:
        """所有 repo 都为 None 时不抛异常。"""
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=None,
            expectation_repo=None,
            valuation_repo=None,
        )
        assert snapshot["fundamentals_latest"] is None
        assert snapshot["valuation_cases"] == []
        assert snapshot["consensus_estimates"] == []
        assert len(snapshot["data_gaps"]) > 0


# ---------------------------------------------------------------------------
# 测试类 2: TestRenderCfaReportToMarkdown
# ---------------------------------------------------------------------------

class TestRenderCfaReportToMarkdown:
    """测试 render_cfa_report_to_markdown 函数。"""

    def test_markdown_contains_company_metadata(self) -> None:
        """Markdown 含公司元数据。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "600519.SH" in md
        assert "贵州茅台" in md
        assert "## 0. 公司元数据" in md
        assert "symbol" in md
        assert "as_of" in md
        assert "2024-12-31" in md

    def test_markdown_contains_notice(self) -> None:
        """Markdown 含 notice 提示。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "研究数据产物" in md
        assert "不包含交易动作建议" in md

    def test_markdown_contains_all_ten_section_titles(self) -> None:
        """10 章节都有标题。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        for sec_value in ReportSection.ordered_values():
            title = SECTION_TITLES[sec_value]
            assert title in md, f"missing section title: {title}"

    def test_markdown_contains_section_status_icons(self) -> None:
        """章节状态标记 ✓ / ⚠ / ✗。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "✓" in md  # ok 章节
        report_partial = _make_partial_cfa_report()
        md_partial = render_cfa_report_to_markdown(report_partial)
        assert "⚠" in md_partial  # partial 章节
        assert "✗" in md_partial  # missing 章节

    def test_markdown_formats_amount_in_yi_wan(self) -> None:
        """金额用 万/亿 单位。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        # 1_500_000_000.0 元 = 15.00 亿元
        assert "15.00 亿元" in md or "亿元" in md

    def test_markdown_formats_percent_two_decimal(self) -> None:
        """百分比 2 位小数（wacc=0.085 → 8.50%）。"""
        report = _make_partial_cfa_report()
        md = render_cfa_report_to_markdown(report)
        # 章节 5 content 含 wacc=0.085，应渲染为 8.50%
        assert "8.50%" in md

    def test_markdown_shows_no_data_when_field_missing(self) -> None:
        """缺字段显示「暂无数据」。"""
        # 构造空 content 章节
        section = ReportSectionContent(
            section=ReportSection.SCOPE_AND_AS_OF.value,
            title=SECTION_TITLES[ReportSection.SCOPE_AND_AS_OF.value],
            content={},
            limitations=[],
            source_refs=[],
            section_status=SECTION_STATUS_MISSING,
        )
        md = render_cfa_report_section_to_markdown(section)
        assert "暂无数据" in md

    def test_markdown_contains_review_checklist_section(self) -> None:
        """末尾含「人工复核 Checklist 提示」章节。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "人工复核 Checklist 提示" in md
        # 5 项复核
        assert "三表勾稽" in md
        assert "分部收入加总" in md
        assert "前瞻假设" in md
        assert "估值关键假设" in md
        assert "同业可比口径" in md

    def test_markdown_contains_field_lookup_table(self) -> None:
        """含字段位置速查表。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "字段位置速查表" in md

    def test_markdown_contains_review_conclusion_table(self) -> None:
        """含复核结论表格（业务人员填写）。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "复核结论" in md
        assert "复核人签名" in md

    def test_markdown_none_report_returns_no_data(self) -> None:
        """report=None 时返回「暂无数据」。"""
        md = render_cfa_report_to_markdown(None)
        assert "暂无数据" in md

    def test_markdown_dict_input_supported(self) -> None:
        """支持 dict 输入（兼容序列化报告）。"""
        report = _make_full_cfa_report()
        report_dict = report.to_dict()
        md = render_cfa_report_to_markdown(report_dict)
        assert "600519.SH" in md
        assert "贵州茅台" in md

    def test_markdown_overall_status_label(self) -> None:
        """整体状态标签显示。"""
        report = _make_full_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "完整" in md  # OVERALL_STATUS_OK → "完整"

    def test_markdown_contains_degradation_reasons(self) -> None:
        """含降级原因列表。"""
        report = _make_partial_cfa_report()
        md = render_cfa_report_to_markdown(report)
        assert "降级原因" in md


# ---------------------------------------------------------------------------
# 测试类 3: TestRenderEvidenceSnapshotToMarkdown
# ---------------------------------------------------------------------------

class TestRenderEvidenceSnapshotToMarkdown:
    """测试 render_evidence_snapshot_to_markdown 函数。"""

    def test_appendix_contains_key_metrics_table(self) -> None:
        """附录含 key_metrics 表格。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "附录 A" in md
        assert "A.2 关键指标" in md
        assert "revenue" in md
        assert "亿元" in md  # 金额格式化

    def test_appendix_contains_balance_check(self) -> None:
        """附录含 balance_check 章节。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "A.3 三表勾稽" in md
        assert "通过" in md

    def test_appendix_contains_valuation_cases(self) -> None:
        """附录含 valuation_cases 列表。"""
        cases = [{
            "case_id": "valc_001", "company_symbol": "600519.SH",
            "method": "dcf_fcff", "as_of": "2024-12-31", "scenario": "base",
            "output_interval": {"low": 1500.0, "high": 1800.0},
            "output_per_share": {"low": 1500.0, "high": 1800.0},
            "assumptions": [{"name": "wacc", "value": 0.085, "unit": "percent",
                             "source_ref": "claim-wacc-1", "as_of": "2024-12-31"}],
            "source_refs": ["claim-wacc-1"],
            "comparable_companies": [],
            "computability": "computable", "limitations": [],
            "model_version": "v1", "inputs_snapshot": {},
            "formula_trace": [], "sensitivity": [],
            "created_at": "",
        }]
        val_repo = _MockValuationRepo(cases=cases)
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=val_repo,
            company_meta={"name": "贵州茅台"},
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "A.4 估值案例" in md
        assert "valc_001" in md
        assert "wacc" in md
        assert "8.50%" in md  # 0.085 → 8.50%

    def test_appendix_contains_data_gaps(self) -> None:
        """附录含 data_gaps 章节。"""
        snapshot = build_company_evidence_snapshot(
            symbol="000001.SZ", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({}),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "A.6 数据缺口" in md
        assert "no_fundamentals" in md or "no_valuation_cases" in md

    def test_appendix_contains_review_checklist_hints(self) -> None:
        """附录含 review_checklist_hints 表格。"""
        fund_repo = _MockFundamentalsRepo({"600519.SH": _make_full_statement()})
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=fund_repo,
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "A.7 人工复核 Checklist 提示" in md
        assert "1_三表勾稽" in md
        assert "5_同业可比口径" in md

    def test_appendix_consensus_estimates_count_displayed(self) -> None:
        """附录显示 consensus_estimates 数量。"""
        consensus = [
            {"consensus_id": f"cons_{i:03d}", "company_symbol": "600519.SH",
             "provider": "wind", "institution_id": "inst_001",
             "analyst_id": "analyst_001", "values": [{"metric": "net_profit"}]}
            for i in range(5)
        ]
        exp_repo = _MockExpectationRepo(consensus=consensus)
        snapshot = build_company_evidence_snapshot(
            symbol="600519.SH", as_of="2024-12-31",
            fundamentals_repo=_MockFundamentalsRepo({"600519.SH": _make_full_statement()}),
            expectation_repo=exp_repo,
            valuation_repo=_MockValuationRepo(),
            company_meta={"name": "贵州茅台"},
        )
        md = render_evidence_snapshot_to_markdown(snapshot)
        assert "A.5 一致预期" in md
        assert "共 5 条" in md

    def test_appendix_empty_snapshot_returns_empty_string(self) -> None:
        """空 snapshot 返回空字符串。"""
        md = render_evidence_snapshot_to_markdown(None)
        assert md == ""
