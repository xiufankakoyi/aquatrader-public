"""P1.9 CFA 风格研究报告与页面 — 专项测试。

覆盖 8 个验收点：
1. TestReportStructure: 报告固定 10 章节 + 章节字段齐全 + section_status 合法
2. TestSectionContentBoundaries: 章节内容边界（异常≠造假/历史 vs 前瞻/四类预期分离/
   区间不单点/情绪新闻不入评价/反证不下买卖结论）
3. TestDegradationHandling: 缺数据降级 + 多数 missing -> degraded + 不崩溃
4. TestNoTradeAdvice: 响应不含 buy/sell/long/short/止盈/止损/买入/卖出/仓位 等关键词
5. TestPitFilter: get_latest_report 按 as_of 过滤；未来数据不进入历史报告
6. TestSourcesAndVersions: 章节 10 收集所有 source_refs + model_versions + 审计链路
7. TestApiSmoke: 所有 4 个端点 smoke + 错误响应统一
8. TestRepositoryIdempotency: 迁移幂等 + 重启回读正确
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from core.research.reporting.contracts import (
    NOTICE,
    OVERALL_STATUS_DEGRADED,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    ReportSection,
    ReportSectionContent,
    SECTION_STATUSES,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_NOT_APPLICABLE,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_TITLES,
    CfaReport,
    compute_overall_status,
    empty_section,
)
from core.research.reporting.cfa_report_builder import (
    CfaReportBuilder,
    ReportRepository,
)
from server.routes.reporting_routes import reporting_bp, set_builder, set_repository


# ---------------------------------------------------------------------------
# 测试辅助：Mock 仓储
# ---------------------------------------------------------------------------

@dataclass
class _MockDriver:
    """模拟 OperatingDriver。"""
    name: str = ""
    kind: str = ""
    value: float | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    source_ref: str = ""
    observation_kind: str = "historical_disclosed"
    grounding_status: str = "grounding_ok"
    reported_value: float | None = None
    segment_name: str = ""
    note: str = ""


@dataclass
class _MockForwardAssumption:
    """模拟 ForwardAssumption。"""
    name: str = ""
    kind: str = ""
    value: float | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    source_ref: str = ""
    assumption_text: str = ""
    basis: str = ""
    note: str = ""


@dataclass
class _MockSegment:
    """模拟 Segment。"""
    segment_name: str = ""
    products: list[str] = field(default_factory=list)
    drivers: list[_MockDriver] = field(default_factory=list)


@dataclass
class _MockOperatingModel:
    """模拟 OperatingModel。"""
    symbol: str = ""
    business_model: str = "manufacturing"
    period: str = ""
    currency: str = "CNY"
    unit: str = "元"
    segments: list[_MockSegment] = field(default_factory=list)
    drivers: list[_MockDriver] = field(default_factory=list)
    assumptions: list[_MockForwardAssumption] = field(default_factory=list)
    gaps: list[Any] = field(default_factory=list)
    duplicate_revenue_warnings: list[str] = field(default_factory=list)
    consistency_errors: list[str] = field(default_factory=list)
    source_trace_id: str = "trace-operating-1"
    schema_version: str = "operating_model_v1"

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "symbol": self.symbol,
            "business_model": self.business_model,
            "period": self.period,
            "currency": self.currency,
            "unit": self.unit,
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }


@dataclass
class _MockStatement:
    """模拟 NormalizedFinancialStatement。"""
    symbol: str = ""
    period: str = ""
    restatement_version: int = 1
    source_trace_id: str = "trace-stmt-1"
    schema_version: str = "financial_v1"
    company_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "symbol": self.symbol,
            "period": self.period,
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }


@dataclass
class _MockQualityMetric:
    """模拟 QualityMetric。"""
    name: str = ""
    value: float | None = None
    formula: str = ""
    input_fields: list[str] = field(default_factory=list)
    period: str = ""
    unit: str = ""
    status: str = "ok"
    note: str = ""


@dataclass
class _MockQualityReport:
    """模拟 QualityReport。"""
    status: str = "ok"
    metrics: dict[str, _MockQualityMetric] = field(default_factory=dict)
    missing_metrics: list[str] = field(default_factory=list)
    dupont_decomposition: dict[str, _MockQualityMetric] = field(default_factory=dict)


@dataclass
class _MockAnomalySignal:
    """模拟 AnomalySignal。"""
    name: str = "abnormal_accrual"
    severity: str = "warning"
    observation: str = "应计利润 / 总资产 = 12% > 10% 阈值"
    evidence_fields: list[str] = field(default_factory=list)
    period: str = ""
    threshold: float | None = None
    actual_value: float | None = None


@dataclass
class _MockAnomalyReport:
    """模拟 AnomalyReport。"""
    signals: list[_MockAnomalySignal] = field(default_factory=list)
    conclusion_boundary: str = "异常是审查提示，不等于财务造假"
    status: str = "ok"


@dataclass
class _MockLedger:
    """模拟 GovernanceLedger。"""
    symbol: str = ""
    company_name: str = "测试公司"
    period: str = ""
    available_at: str = ""
    shareholder_changes: list[Any] = field(default_factory=list)
    management_persons: list[Any] = field(default_factory=list)
    equity_incentives: list[Any] = field(default_factory=list)
    related_party_transactions: list[Any] = field(default_factory=list)
    risk_events: list[Any] = field(default_factory=list)
    capital_actions: list[Any] = field(default_factory=list)
    management_commitments: list[Any] = field(default_factory=list)
    personnel_assessments: list[Any] = field(default_factory=list)
    governance_risks: list[Any] = field(default_factory=list)
    capital_allocation_impacts: list[Any] = field(default_factory=list)
    gaps: list[Any] = field(default_factory=list)
    source_trace_id: str = "trace-gov-1"
    schema_version: str = "governance_v1"

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "source_trace_id": self.source_trace_id,
            "schema_version": self.schema_version,
        }


@dataclass
class _MockValuationCase:
    """模拟 ValuationCase。"""
    case_id: str = "valc_test1"
    company_symbol: str = ""
    method: str = "dcf_fcff"
    as_of: str = ""
    scenario: str = "base"
    output_interval: dict[str, float] = field(
        default_factory=lambda: {"low": 50.0, "high": 70.0}
    )
    output_per_share: dict[str, float] = field(
        default_factory=lambda: {"low": 50.0, "high": 70.0}
    )
    computability: str = "computable"
    sensitivity: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=lambda: ["claim-wacc-1", "claim-g-1"])
    formula_trace: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    method_specific_metrics: dict[str, Any] = field(default_factory=dict)
    model_version: str = "v1"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "case_id": self.case_id,
            "company_symbol": self.company_symbol,
            "method": self.method,
            "as_of": self.as_of,
            "output_interval": self.output_interval,
            "output_per_share": self.output_per_share,
            "computability": self.computability,
            "sensitivity": self.sensitivity,
            "source_refs": self.source_refs,
            "formula_trace": self.formula_trace,
            "limitations": self.limitations,
            "method_specific_metrics": self.method_specific_metrics,
            "model_version": self.model_version,
            "created_at": self.created_at,
        }


@dataclass
class _MockValuationReport:
    """模拟 ValuationReport。"""
    report_id: str = "valr_test1"
    company_symbol: str = ""
    as_of: str = ""
    scenario: str = "base"
    cases: list[_MockValuationCase] = field(default_factory=list)
    method_differences: list[dict[str, Any]] = field(default_factory=list)
    overall_range: dict[str, float] = field(
        default_factory=lambda: {"low": 50.0, "high": 70.0}
    )
    overall_range_method: str = "min_max"
    limitations: list[str] = field(default_factory=list)
    notice: str = NOTICE
    model_version: str = "v1"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "report_id": self.report_id,
            "company_symbol": self.company_symbol,
            "as_of": self.as_of,
            "overall_range": self.overall_range,
            "overall_range_method": self.overall_range_method,
            "method_differences": self.method_differences,
            "limitations": self.limitations,
            "notice": self.notice,
            "model_version": self.model_version,
            "created_at": self.created_at,
        }


@dataclass
class _MockIndustryProfile:
    """模拟 IndustryProfile。"""
    industry_name: str = "测试行业"
    industry_code: str = "T001"
    code_system: str = "sw"
    period: str = ""
    market_sizing: Any = None
    demand_drivers: list[Any] = field(default_factory=list)
    supply_profile: Any = None
    price_cost_curve: Any = None
    inventory_cycle: Any = None
    competitive_landscape: Any = None
    technology_routes: list[Any] = field(default_factory=list)
    bargaining_powers: list[Any] = field(default_factory=list)
    capex_cycle: Any = None
    lifecycle_stage: Any = None
    company_mappings: list[Any] = field(default_factory=list)
    gaps: list[Any] = field(default_factory=list)
    source_trace_id: str = "trace-ind-1"
    schema_version: str = "industry_v1"


# ---------------------------------------------------------------------------
# Mock 仓储实现
# ---------------------------------------------------------------------------

class _MockFundamentalsRepo:
    """模拟 fundamentals 仓储。"""

    def __init__(self) -> None:
        self.statements: dict[str, _MockStatement] = {}
        self.quality_reports: dict[str, _MockQualityReport] = {}
        self.anomaly_reports: dict[str, _MockAnomalyReport] = {}

    def get_latest_statement(self, symbol: str, as_of: str) -> Any:
        """取最新 statement。"""
        return self.statements.get(symbol)

    def get_latest(self, symbol: str, as_of: str) -> Any:
        """兼容别名。"""
        return self.statements.get(symbol)

    def get_latest_quality_report(self, symbol: str, as_of: str) -> Any:
        """取最新质量报告。"""
        return self.quality_reports.get(symbol)

    def get_latest_anomaly_report(self, symbol: str, as_of: str) -> Any:
        """取最新异常报告。"""
        return self.anomaly_reports.get(symbol)


class _MockOperatingRepo:
    """模拟 operating_model 仓储。"""

    def __init__(self) -> None:
        self.models: dict[str, _MockOperatingModel] = {}

    def get_latest(self, symbol: str, as_of: str = "") -> Any:
        """取最新模型。"""
        return self.models.get(symbol)


class _MockEarningsRepo:
    """模拟 earnings 仓储。"""

    def __init__(self) -> None:
        self.forecasts: dict[str, Any] = {}

    def get_latest_forecast(self, symbol: str, as_of: str) -> Any:
        """取最新 forecast。"""
        return self.forecasts.get(symbol)


class _MockIndustryRepo:
    """模拟 industry 仓储。"""

    def __init__(self) -> None:
        self.profiles: dict[str, _MockIndustryProfile] = {}

    def get_latest_profile(self, symbol: str, as_of: str) -> Any:
        """取最新 industry profile。"""
        return self.profiles.get(symbol)


class _MockGovernanceRepo:
    """模拟 governance 仓储。"""

    def __init__(self) -> None:
        self.ledgers: dict[str, _MockLedger] = {}

    def get_latest_ledger(self, symbol: str, as_of: str) -> Any:
        """取最新 ledger。"""
        return self.ledgers.get(symbol)


class _MockExpectationRepo:
    """模拟 expectations 仓储。"""

    def __init__(self) -> None:
        self.guidance: list[dict[str, Any]] = []
        self.consensus: list[dict[str, Any]] = []
        self.implied: list[dict[str, Any]] = []
        self.aquatrade: list[dict[str, Any]] = []
        self.summaries: dict[str, Any] = {}
        self.gaps: list[dict[str, Any]] = []

    def list_company_guidance(self, *, company_symbol: str = "",
                              as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 company guidance。"""
        return [g for g in self.guidance if g.get("company_symbol") == company_symbol][:limit]

    def list_consensus_estimates(self, *, company_symbol: str = "",
                                 as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 consensus estimates。"""
        return [c for c in self.consensus if c.get("company_symbol") == company_symbol][:limit]

    def list_implied_expectations(self, *, company_symbol: str = "",
                                  as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 implied expectations。"""
        return [i for i in self.implied if i.get("company_symbol") == company_symbol][:limit]

    def list_aquatrade_forecasts(self, *, company_symbol: str = "",
                                 as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 aquatrade forecasts。"""
        return [a for a in self.aquatrade if a.get("company_symbol") == company_symbol][:limit]

    def get_latest_consensus_summary(self, company_symbol: str, as_of: str) -> Any:
        """取最新 consensus summary。"""
        return self.summaries.get(company_symbol)

    def list_expectation_gaps(self, *, company_symbol: str = "",
                              as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 expectation gaps。"""
        return [g for g in self.gaps if g.get("company_symbol") == company_symbol][:limit]


class _MockValuationRepo:
    """模拟 valuation 仓储。"""

    def __init__(self) -> None:
        self.reports: dict[str, list[_MockValuationReport]] = {}
        self.cases: dict[str, list[_MockValuationCase]] = {}

    def list_reports(self, *, company_symbol: str = "", as_of: str = "",
                     limit: int = 100) -> list[Any]:
        """查询 reports。"""
        items = self.reports.get(company_symbol, [])
        return items[:limit]

    def list_cases(self, *, company_symbol: str = "", method: str = "",
                   as_of: str = "", limit: int = 100) -> list[Any]:
        """查询 cases。"""
        items = self.cases.get(company_symbol, [])
        return items[:limit]


def _full_mock_repos() -> tuple:
    """构造满数据的 mock 仓储集合。"""
    fundamentals = _MockFundamentalsRepo()
    fundamentals.statements["600519.SH"] = _MockStatement(
        symbol="600519.SH", period="2025-12-31",
        company_name="贵州茅台",
    )
    fundamentals.quality_reports["600519.SH"] = _MockQualityReport(
        status="ok",
        metrics={"roe": _MockQualityMetric(name="roe", value=0.30, status="ok")},
    )
    fundamentals.anomaly_reports["600519.SH"] = _MockAnomalyReport(
        signals=[_MockAnomalySignal()],
        status="ok",
    )

    operating = _MockOperatingRepo()
    operating.models["600519.SH"] = _MockOperatingModel(
        symbol="600519.SH",
        business_model="manufacturing",
        period="2025-12-31",
        segments=[_MockSegment(
            segment_name="白酒",
            products=["飞天茅台"],
            drivers=[_MockDriver(
                name="销量", kind="sales_volume", value=2.0,
                observation_kind="historical_disclosed",
                source_ref="claim-driver-1",
            )],
        )],
        assumptions=[_MockForwardAssumption(
            name="销量增长", kind="sales_volume", value=0.05,
            source_ref="claim-asm-1",
            assumption_text="2026 年销量增长 5%",
        )],
    )

    earnings = _MockEarningsRepo()
    earnings.forecasts["600519.SH"] = {
        "forecast_id": "fcst-1",
        "company_symbol": "600519.SH",
        "scenario": "baseline",
        "as_of": "2026-07-01",
        "source_refs": ["claim-fcst-1"],
    }

    industry = _MockIndustryRepo()
    industry.profiles["600519.SH"] = _MockIndustryProfile(
        industry_name="白酒",
        period="2025-12-31",
    )

    governance = _MockGovernanceRepo()
    governance.ledgers["600519.SH"] = _MockLedger(
        symbol="600519.SH",
        company_name="贵州茅台",
        period="2025-12-31",
        management_commitments=[{"commitment_id": "c1"}],
        management_persons=[{"person_id": "p1"}],
        governance_risks=[{"description": "关联交易占比偏高", "source_ref": "claim-risk-1"}],
        capital_actions=[{"action_id": "a1"}],
        risk_events=[{"description": "股东质押比例 5%", "source_ref": "claim-evt-1"}],
    )

    expectation = _MockExpectationRepo()
    expectation.guidance = [{"company_symbol": "600519.SH", "guidance_id": "g1"}]
    expectation.consensus = [{"company_symbol": "600519.SH", "consensus_id": "c1"}]
    expectation.implied = [{"company_symbol": "600519.SH", "implied_id": "i1"}]
    expectation.aquatrade = [{"company_symbol": "600519.SH", "forecast_id": "a1"}]
    expectation.summaries["600519.SH"] = {"summary_id": "s1", "status": "ok"}
    expectation.gaps = [{"company_symbol": "600519.SH", "gap_id": "eg1"}]

    valuation = _MockValuationRepo()
    valuation.reports["600519.SH"] = [_MockValuationReport(
        report_id="valr_test1",
        company_symbol="600519.SH",
        as_of="2026-07-01",
        cases=[_MockValuationCase(
            company_symbol="600519.SH",
            as_of="2026-07-01",
            sensitivity=[
                {"variable_name": "wacc", "variable_value": 0.10,
                 "output_low": 50.0, "output_high": 70.0},
            ],
        )],
    )]
    valuation.cases["600519.SH"] = [_MockValuationCase(
        company_symbol="600519.SH",
        as_of="2026-07-01",
    )]

    return (fundamentals, operating, earnings, industry,
            governance, expectation, valuation)


def _make_builder_with_full_data() -> CfaReportBuilder:
    """构造满数据的 builder。"""
    (fundamentals, operating, earnings, industry,
     governance, expectation, valuation) = _full_mock_repos()
    return CfaReportBuilder(
        fundamentals_repo=fundamentals,
        operating_repo=operating,
        earnings_repo=earnings,
        industry_repo=industry,
        governance_repo=governance,
        expectation_repo=expectation,
        valuation_repo=valuation,
    )


def _make_empty_builder() -> CfaReportBuilder:
    """构造无数据的 builder。"""
    return CfaReportBuilder(
        fundamentals_repo=_MockFundamentalsRepo(),
        operating_repo=_MockOperatingRepo(),
        earnings_repo=_MockEarningsRepo(),
        industry_repo=_MockIndustryRepo(),
        governance_repo=_MockGovernanceRepo(),
        expectation_repo=_MockExpectationRepo(),
        valuation_repo=_MockValuationRepo(),
    )


# ---------------------------------------------------------------------------
# 验收点 1: TestReportStructure
# ---------------------------------------------------------------------------

class TestReportStructure:
    """验收点 1：报告结构固定 10 章节。"""

    def test_report_has_exactly_10_sections(self):
        """报告必须固定含 10 章节。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        assert len(report.sections) == 10

    def test_scope_uses_explicit_company_metadata_when_domain_repos_lack_it(self):
        builder = _make_empty_builder()
        builder.company_meta = {
            "600438.SH": {"name": "通威股份", "industry": "新能源"},
        }

        report = builder.build("600438.SH", "2024-12-31")
        scope = report.sections[0]

        assert report.company_name == "通威股份"
        assert scope.content["company_name"] == "通威股份"
        assert scope.content["industry"] == "新能源"
        assert "company_name_missing" not in scope.limitations
        assert "industry_missing" not in scope.limitations

    def test_section_order_matches_enum(self):
        """章节顺序与 ReportSection 枚举一致。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        for idx, section_value in enumerate(ReportSection.ordered_values()):
            assert report.sections[idx].section == section_value

    def test_each_section_has_required_fields(self):
        """每章节含 title / content / section_status / limitations / source_refs。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        for section in report.sections:
            assert section.title, f"section {section.section} title 为空"
            assert isinstance(section.content, dict)
            assert section.section_status in SECTION_STATUSES
            assert isinstance(section.limitations, list)
            assert isinstance(section.source_refs, list)

    def test_section_titles_are_chinese(self):
        """章节 title 为中文。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        for section in report.sections:
            assert SECTION_TITLES[section.section] == section.title

    def test_report_has_required_metadata(self):
        """报告含 report_id / company_symbol / as_of / notice / model_version。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        assert report.report_id.startswith("cfar_")
        assert report.company_symbol == "600519.SH"
        assert report.as_of == "2026-07-01"
        assert report.notice == NOTICE
        assert report.model_version == "v1"
        assert report.created_at

    def test_get_section_method(self):
        """get_section 方法按 section value 获取章节。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.SCOPE_AND_AS_OF.value)
        assert section is not None
        assert section.section == ReportSection.SCOPE_AND_AS_OF.value
        # 不存在的 section 返回 None
        assert report.get_section("nonexistent") is None


# ---------------------------------------------------------------------------
# 验收点 2: TestSectionContentBoundaries
# ---------------------------------------------------------------------------

class TestSectionContentBoundaries:
    """验收点 2：章节内容边界。"""

    def test_financial_quality_section_has_audit_boundary(self):
        """章节 4 含异常审查边界声明（不等于造假）。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.FINANCIAL_QUALITY.value)
        assert section is not None
        audit_text = section.content.get("audit_boundary", "")
        assert "审查提示" in audit_text
        assert "不等于" in audit_text or "不下结论" in audit_text

    def test_drivers_section_separates_historical_and_forward(self):
        """章节 5 区分历史观察 vs 前瞻假设。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.DRIVERS_AND_FORECAST.value)
        assert section is not None
        historical = section.content.get("historical_drivers", [])
        forward = section.content.get("forward_assumptions", [])
        assert isinstance(historical, list)
        assert isinstance(forward, list)
        # 必须有说明字段
        assert "observation_vs_assumption_note" in section.content
        note = section.content["observation_vs_assumption_note"]
        assert "historical_disclosed" in note or "历史观察" in note
        assert "ForwardAssumption" in note or "前瞻假设" in note

    def test_expectations_section_separates_four_domains(self):
        """章节 6 严格分离四类预期（不互相覆盖）。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.EXPECTATION_DIFFERENCES.value)
        assert section is not None
        # 四类独立字段
        assert "company_guidance" in section.content
        assert "consensus_estimates" in section.content
        assert "implied_expectations" in section.content
        assert "aquatrade_forecasts" in section.content
        # 四类必须互不重叠（这里通过字段独立性验证）
        keys = {
            "company_guidance", "consensus_estimates",
            "implied_expectations", "aquatrade_forecasts",
        }
        content_keys = set(section.content.keys())
        assert keys.issubset(content_keys)
        note = section.content.get("separation_note", "")
        assert "独立" in note or "互相覆盖" in note

    def test_valuation_section_outputs_range_not_single_point(self):
        """章节 7 输出区间，不输出单点动作。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.VALUATION_SENSITIVITY.value)
        assert section is not None
        # range_note 必须提到区间
        note = section.content.get("range_note", "")
        assert "区间" in note
        # no_action_note 必须提到不输出交易动作
        no_action = section.content.get("no_action_note", "")
        assert "买入" in no_action or "卖出" in no_action
        # valuation_report 必须含 overall_range（区间）
        vreport = section.content.get("valuation_report") or {}
        if vreport:
            assert "overall_range" in vreport

    def test_governance_section_excludes_emotional_news(self):
        """章节 8 情绪新闻不进入正式人员评价。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.GOVERNANCE_CAPITAL.value)
        assert section is not None
        note = section.content.get("emotional_news_note", "")
        assert "情绪新闻" in note
        assert "is_from_emotional_news" in note or "不进入正式" in note

    def test_risks_section_has_counter_evidence_and_no_trade_advice(self):
        """章节 9 含反证证据，不下买卖结论。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.RISKS_COUNTEREVIDENCE.value)
        assert section is not None
        counter = section.content.get("counter_evidence", [])
        assert isinstance(counter, list)
        # 反证证据应来自 governance 风险事件 + anomaly signals
        assert len(counter) > 0
        # no_trade_advice_note 必须提到不下买卖结论
        no_trade = section.content.get("no_trade_advice_note", "")
        assert "买入" in no_trade or "卖出" in no_trade
        # 模型失效条件
        failures = section.content.get("model_failure_conditions", [])
        assert len(failures) >= 3

    def test_industry_section_member_of_theme_filter_note(self):
        """章节 3 含 member_of_theme 严格过滤说明。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.INDUSTRY_COMPETITION.value)
        assert section is not None
        filter_note = section.content.get("member_of_theme_filter", "")
        assert "member_of_theme" in filter_note


# ---------------------------------------------------------------------------
# 验收点 3: TestDegradationHandling
# ---------------------------------------------------------------------------

class TestDegradationHandling:
    """验收点 3：降级处理。"""

    def test_empty_repos_produce_degraded_report(self):
        """无数据仓储构建出 degraded 报告。"""
        builder = _make_empty_builder()
        report = builder.build("600519.SH", "2026-07-01")
        assert report.overall_status == OVERALL_STATUS_DEGRADED
        assert len(report.degradation_reasons) > 0

    def test_degraded_report_still_has_10_sections(self):
        """degraded 报告仍含 10 章节（全 missing）。"""
        builder = _make_empty_builder()
        report = builder.build("600519.SH", "2026-07-01")
        assert len(report.sections) == 10
        for section in report.sections:
            # 章节 1 始终 ok/partial（有公司基本信息），其他应为 missing
            if section.section != ReportSection.SCOPE_AND_AS_OF.value:
                assert section.section_status in (
                    SECTION_STATUS_MISSING, SECTION_STATUS_PARTIAL,
                )

    def test_no_500_errors_on_invalid_symbol(self):
        """非法 symbol 不抛异常，返回 degraded。"""
        builder = _make_builder_with_full_data()
        report = builder.build("BAD_SYMBOL", "2026-07-01")
        assert report.overall_status == OVERALL_STATUS_DEGRADED
        assert any("invalid_symbol" in r for r in report.degradation_reasons)
        assert len(report.sections) == 10

    def test_partial_data_returns_partial(self):
        """部分数据返回 partial。"""
        # 构造仅 fundamentals 有数据、其他全空的 builder
        fundamentals = _MockFundamentalsRepo()
        fundamentals.statements["600519.SH"] = _MockStatement(
            symbol="600519.SH", period="2025-12-31",
            company_name="贵州茅台",
        )
        fundamentals.quality_reports["600519.SH"] = _MockQualityReport()
        fundamentals.anomaly_reports["600519.SH"] = _MockAnomalyReport(
            signals=[_MockAnomalySignal()],
        )
        builder = CfaReportBuilder(
            fundamentals_repo=fundamentals,
            operating_repo=_MockOperatingRepo(),
            earnings_repo=_MockEarningsRepo(),
            industry_repo=_MockIndustryRepo(),
            governance_repo=_MockGovernanceRepo(),
            expectation_repo=_MockExpectationRepo(),
            valuation_repo=_MockValuationRepo(),
        )
        report = builder.build("600519.SH", "2026-07-01")
        # 多数 missing -> degraded
        assert report.overall_status in (
            OVERALL_STATUS_DEGRADED, OVERALL_STATUS_PARTIAL,
        )

    def test_builder_does_not_raise_on_repo_exceptions(self):
        """仓储抛异常时 builder 不传播异常，章节降级 missing。"""
        class _ExplodingRepo:
            def get_latest(self, *args, **kwargs):
                raise RuntimeError("boom")
            def get_latest_statement(self, *args, **kwargs):
                raise RuntimeError("boom")
        builder = CfaReportBuilder(
            fundamentals_repo=_ExplodingRepo(),
            operating_repo=_ExplodingRepo(),
        )
        # 不应抛异常
        report = builder.build("600519.SH", "2026-07-01")
        assert len(report.sections) == 10


# ---------------------------------------------------------------------------
# 验收点 4: TestNoTradeAdvice
# ---------------------------------------------------------------------------

class TestNoTradeAdvice:
    """验收点 4：响应不含交易动作建议关键词。"""

    FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "buy", "sell", "long", "short",
        "止盈", "止损", "买入", "卖出", "仓位",
        "加仓", "减仓", "target_price", "stop_loss", "take_profit",
    )

    def _scan_text(self, obj: Any) -> str:
        """递归扫描对象所有字符串字段，返回拼接文本。"""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, dict):
            return " ".join(self._scan_text(v) for v in obj.values())
        if isinstance(obj, (list, tuple)):
            return " ".join(self._scan_text(v) for v in obj)
        return ""

    def test_notice_field_fixed_text(self):
        """notice 字段固定提示文本。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        assert report.notice == NOTICE
        assert "不包含交易动作建议" in report.notice

    def test_no_forbidden_keywords_in_report(self):
        """报告整体不含交易建议关键词。

        允许出现"买入/卖出/仓位/止盈/止损"等词仅限于
        「不输出买入/卖出/仓位/止盈/止损等」这类否定句说明中
        （如 no_action_note / no_trade_advice_note）。
        本测试扫描除 notice / no_action_note / no_trade_advice_note /
        range_note 之外的字符串字段。
        """
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        data = report.to_dict()

        # 排除字段：固定否定说明
        excluded_keys = {"notice", "no_action_note", "no_trade_advice_note"}

        def _scan(obj: Any, path: str = "") -> list[tuple[str, str]]:
            """递归扫描，返回 (path, text) 列表。"""
            results: list[tuple[str, str]] = []
            if isinstance(obj, str):
                if path.split(".")[-1] in excluded_keys:
                    return results
                results.append((path, obj))
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    new_path = f"{path}.{k}" if path else k
                    results.extend(_scan(v, new_path))
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    new_path = f"{path}[{i}]"
                    results.extend(_scan(v, new_path))
            return results

        for path, text in _scan(data):
            for kw in self.FORBIDDEN_KEYWORDS:
                # 排除否定说明字段中的关键词
                if path.endswith("no_action_note") or path.endswith("no_trade_advice_note"):
                    continue
                # range_note 中可以提到"不输出买入/卖出"
                if path.endswith("range_note"):
                    continue
                # no_trade_advice_note 字段被排除
                if "no_trade_advice_note" in path or "no_action_note" in path:
                    continue
                # 关键词必须不是出现在「不下买卖结论」「不输出买入/卖出」等否定句中
                if kw in text.lower() if kw.isalpha() else kw in text:
                    # 检查前后是否有「不」「未」等否定词
                    pos = text.lower().find(kw) if kw.isalpha() else text.find(kw)
                    if pos > 0:
                        prev_char = text[max(0, pos - 3):pos]
                        if any(neg in prev_char for neg in
                               ("不", "未", "无", "禁止", "no_", "not_")):
                            continue
                    # 跳过 model_failure_conditions 中的否定说明
                    if "model_failure_conditions" in path:
                        continue
                    pytest.fail(
                        f"路径 {path} 含禁用关键词 '{kw}'，文本: {text}"
                    )


# ---------------------------------------------------------------------------
# 验收点 5: TestPitFilter
# ---------------------------------------------------------------------------

class TestPitFilter:
    """验收点 5：PIT 过滤。"""

    def test_get_latest_report_filters_by_as_of(self, tmp_path: Path):
        """get_latest_report 按 as_of 过滤，未来数据不进入历史报告。"""
        repo = ReportRepository(tmp_path / "test_pit.db")
        # 创建 3 个不同 as_of 的报告
        for as_of in ("2026-01-01", "2026-04-01", "2026-07-01"):
            report = CfaReport(
                company_symbol="600519.SH",
                as_of=as_of,
                sections=[empty_section(ReportSection.SCOPE_AND_AS_OF.value)],
            )
            repo.save_report(report)

        # PIT 查询：2026-03-01 只能看到 1月
        latest = repo.get_latest_report("600519.SH", as_of="2026-03-01")
        assert latest is not None
        assert latest["as_of"] == "2026-01-01"

        # PIT 查询：2026-05-01 可看到 1月+4月，取最新
        latest = repo.get_latest_report("600519.SH", as_of="2026-05-01")
        assert latest is not None
        assert latest["as_of"] == "2026-04-01"

        # PIT 查询：2026-08-01 可看到全部，取最新
        latest = repo.get_latest_report("600519.SH", as_of="2026-08-01")
        assert latest is not None
        assert latest["as_of"] == "2026-07-01"

        # 不传 as_of 不限时间
        latest = repo.get_latest_report("600519.SH", as_of="")
        assert latest is not None
        assert latest["as_of"] == "2026-07-01"

    def test_future_data_not_in_history(self, tmp_path: Path):
        """未来报告不进入历史 PIT 查询。"""
        repo = ReportRepository(tmp_path / "test_future.db")
        future_report = CfaReport(
            company_symbol="600519.SH",
            as_of="2026-12-31",
            sections=[],
        )
        repo.save_report(future_report)
        # 2026-06-01 查询不应看到 12月 的报告
        latest = repo.get_latest_report("600519.SH", as_of="2026-06-01")
        assert latest is None


# ---------------------------------------------------------------------------
# 验收点 6: TestSourcesAndVersions
# ---------------------------------------------------------------------------

class TestSourcesAndVersions:
    """验收点 6：来源、模型版本和审计链路。"""

    def test_sources_section_collects_refs(self):
        """章节 10 收集所有章节 source_refs。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.SOURCES_AND_VERSIONS.value)
        assert section is not None
        refs = section.content.get("source_refs", [])
        assert isinstance(refs, list)
        # 满数据场景应有至少 1 个 source_ref（来自各 mock 仓储）
        assert len(refs) >= 1
        # report_id 字段应存在
        assert "report_id" in section.content

    def test_sources_section_has_model_versions(self):
        """章节 10 含 model_versions 字典。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.SOURCES_AND_VERSIONS.value)
        assert section is not None
        versions = section.content.get("model_versions", {})
        assert isinstance(versions, dict)
        # 必须含 cfa_report_builder 版本
        assert "cfa_report_builder" in versions
        assert versions["cfa_report_builder"] == "v1"

    def test_sources_section_audit_chain(self):
        """章节 10 含审计链路说明。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.SOURCES_AND_VERSIONS.value)
        assert section is not None
        note = section.content.get("audit_chain_note", "")
        assert "审计" in note or "audit" in note.lower()

    def test_empty_repos_sources_section_still_works(self):
        """无数据时章节 10 仍可生成（partial 状态）。"""
        builder = _make_empty_builder()
        report = builder.build("600519.SH", "2026-07-01")
        section = report.get_section(ReportSection.SOURCES_AND_VERSIONS.value)
        assert section is not None
        assert section.section_status in (
            SECTION_STATUS_OK, SECTION_STATUS_PARTIAL,
        )
        # 无 source_refs 时仍应输出空列表
        assert isinstance(section.content.get("source_refs", []), list)


# ---------------------------------------------------------------------------
# 验收点 7: TestApiSmoke
# ---------------------------------------------------------------------------

class TestApiSmoke:
    """验收点 7：API 端点 smoke。"""

    @pytest.fixture
    def app(self, tmp_path: Path):
        """构造 Flask app + 注入 mock builder 与 tmp 仓储。"""
        repo = ReportRepository(tmp_path / "test_api.db")
        builder = _make_builder_with_full_data()
        set_builder(builder)
        set_repository(repo)
        app = Flask(__name__)
        app.register_blueprint(reporting_bp)
        app.config["TESTING"] = True
        yield app
        # 重置单例
        import server.routes.reporting_routes as mod
        mod._builder = None
        mod._repository = None

    @pytest.fixture
    def client(self, app):
        """Flask test client。"""
        return app.test_client()

    def test_health_endpoint(self, client):
        """/health 返回 ok。"""
        resp = client.get("/api/research/reports/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["data"]["status"] == "ok"
        assert body["notice"] == NOTICE
        assert "trace_id" in body

    def test_post_cfa_builds_report(self, client):
        """POST /cfa 构建报告。"""
        resp = client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
            "lookback_days": 120,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["notice"] == NOTICE
        data = body["data"]
        assert data["company_symbol"] == "600519.SH"
        assert len(data["sections"]) == 10
        # 满数据场景 overall_status 不应是 degraded
        assert data["overall_status"] in (
            OVERALL_STATUS_OK, OVERALL_STATUS_PARTIAL,
        )

    def test_post_cfa_missing_fields_returns_400(self, client):
        """缺必填字段返回 400 INVALID_INPUT。"""
        resp = client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_INPUT"
        assert "trace_id" in body["error"]
        assert body["notice"] == NOTICE

    def test_post_cfa_invalid_lookback_returns_400(self, client):
        """lookback_days 越界返回 400。"""
        resp = client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
            "lookback_days": 10,
        })
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_get_cfa_list(self, client):
        """GET /cfa 查询列表。"""
        # 先构建一份
        client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
        })
        resp = client.get("/api/research/reports/cfa?company_symbol=600519.SH")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 1
        assert body["total"] >= 1

    def test_get_cfa_by_id(self, client):
        """GET /cfa/<report_id> 查询详情。"""
        build_resp = client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
        })
        report_id = build_resp.get_json()["data"]["report_id"]
        resp = client.get(f"/api/research/reports/cfa/{report_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["data"]["report_id"] == report_id

    def test_get_cfa_unknown_id_returns_404(self, client):
        """未知 report_id 返回 404 REPORT_NOT_FOUND。"""
        resp = client.get("/api/research/reports/cfa/nonexistent_id")
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"]["code"] == "REPORT_NOT_FOUND"

    def test_get_cfa_latest(self, client):
        """GET /cfa/latest 按 PIT 取最新。"""
        # 构建两份不同 as_of
        client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-01-01",
        })
        client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
        })
        # 不限 as_of 取最新
        resp = client.get("/api/research/reports/cfa/latest?company_symbol=600519.SH")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["data"]["as_of"] == "2026-07-01"

    def test_get_cfa_latest_with_pit_filter(self, client):
        """GET /cfa/latest PIT 过滤。"""
        client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-01-01",
        })
        client.post("/api/research/reports/cfa", json={
            "company_symbol": "600519.SH",
            "as_of": "2026-07-01",
        })
        # 2026-03-01 查询只看到 1月
        resp = client.get(
            "/api/research/reports/cfa/latest"
            "?company_symbol=600519.SH&as_of=2026-03-01"
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["data"]["as_of"] == "2026-01-01"

    def test_get_cfa_latest_missing_symbol_returns_degraded(self, client):
        """GET /cfa/latest 缺数据返回 200 + degraded=true。"""
        resp = client.get(
            "/api/research/reports/cfa/latest?company_symbol=000000.SZ"
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body.get("degraded") is True
        assert "degradation_reason" in body

    def test_get_cfa_latest_missing_company_returns_400(self, client):
        """GET /cfa/latest 缺 company_symbol 返回 400。"""
        resp = client.get("/api/research/reports/cfa/latest")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"]["code"] == "INVALID_INPUT"

    def test_error_response_format_unified(self, client):
        """错误响应格式统一 {ok, error:{code, message, trace_id}, notice}。"""
        resp = client.post("/api/research/reports/cfa", json={})
        body = resp.get_json()
        assert "ok" in body
        assert body["ok"] is False
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert "trace_id" in body["error"]
        assert body["notice"] == NOTICE


# ---------------------------------------------------------------------------
# 验收点 8: TestRepositoryIdempotency
# ---------------------------------------------------------------------------

class TestRepositoryIdempotency:
    """验收点 8：仓储幂等性 + 重启回读。"""

    def test_migrate_idempotent(self, tmp_path: Path):
        """migrate 多次执行不抛异常。"""
        db_path = tmp_path / "test_idem.db"
        repo1 = ReportRepository(db_path)
        # 重复创建
        repo2 = ReportRepository(db_path)
        # 重复 migrate
        repo1.migrate()
        repo2.migrate()
        # schema_version 仍为 1
        assert repo1.stats()["schema_version"] == 1
        assert repo2.stats()["schema_version"] == 1

    def test_save_and_get_roundtrip(self, tmp_path: Path):
        """保存后能完整读回。"""
        repo = ReportRepository(tmp_path / "test_roundtrip.db")
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        repo.save_report(report)
        loaded = repo.get_report(report.report_id)
        assert loaded is not None
        assert loaded["report_id"] == report.report_id
        assert loaded["company_symbol"] == report.company_symbol
        assert loaded["as_of"] == report.as_of
        assert len(loaded["sections"]) == 10
        assert loaded["notice"] == NOTICE

    def test_restart_roundtrip(self, tmp_path: Path):
        """模拟重启：新仓储实例能读回旧数据。"""
        db_path = tmp_path / "test_restart.db"
        repo1 = ReportRepository(db_path)
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        repo1.save_report(report)
        # 模拟重启
        repo2 = ReportRepository(db_path)
        loaded = repo2.get_report(report.report_id)
        assert loaded is not None
        assert loaded["company_symbol"] == report.company_symbol
        assert loaded["as_of"] == report.as_of

    def test_save_same_report_id_updates(self, tmp_path: Path):
        """同 report_id 重复保存视为更新。"""
        repo = ReportRepository(tmp_path / "test_update.db")
        report = CfaReport(
            report_id="cfar_test_id",
            company_symbol="600519.SH",
            as_of="2026-07-01",
            sections=[empty_section(ReportSection.SCOPE_AND_AS_OF.value)],
        )
        repo.save_report(report)
        # 修改后重新保存
        report.company_name = "新名称"
        repo.save_report(report)
        loaded = repo.get_report("cfar_test_id")
        assert loaded is not None
        assert loaded["company_name"] == "新名称"

    def test_list_reports_filters(self, tmp_path: Path):
        """list_reports 支持多种过滤。"""
        repo = ReportRepository(tmp_path / "test_list.db")
        for as_of, status in (
            ("2026-01-01", OVERALL_STATUS_PARTIAL),
            ("2026-04-01", OVERALL_STATUS_OK),
            ("2026-07-01", OVERALL_STATUS_DEGRADED),
        ):
            report = CfaReport(
                company_symbol="600519.SH",
                as_of=as_of,
                overall_status=status,
                sections=[empty_section(ReportSection.SCOPE_AND_AS_OF.value)],
            )
            repo.save_report(report)

        # symbol 过滤
        items = repo.list_reports(company_symbol="600519.SH")
        assert len(items) == 3

        # as_of PIT 过滤
        items = repo.list_reports(
            company_symbol="600519.SH", as_of="2026-05-01",
        )
        assert len(items) == 2  # 1月 + 4月

        # status 过滤
        items = repo.list_reports(
            company_symbol="600519.SH", status=OVERALL_STATUS_OK,
        )
        assert len(items) == 1
        assert items[0]["overall_status"] == OVERALL_STATUS_OK


# ---------------------------------------------------------------------------
# 序列化往返
# ---------------------------------------------------------------------------

class TestSerialization:
    """序列化往返测试。"""

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict / from_dict 往返保持数据。"""
        builder = _make_builder_with_full_data()
        report = builder.build("600519.SH", "2026-07-01")
        data = report.to_dict()
        restored = CfaReport.from_dict(data)
        assert restored.report_id == report.report_id
        assert restored.company_symbol == report.company_symbol
        assert restored.as_of == report.as_of
        assert restored.notice == report.notice
        assert len(restored.sections) == len(report.sections)
        for orig, rest in zip(report.sections, restored.sections):
            assert orig.section == rest.section
            assert orig.title == rest.title
            assert orig.section_status == rest.section_status
            assert orig.limitations == rest.limitations
            assert orig.source_refs == rest.source_refs

    def test_compute_overall_status_pure_ok(self):
        """全部 ok -> overall=ok。"""
        sections = [
            ReportSectionContent(
                section=member.value,
                title=SECTION_TITLES[member.value],
                section_status=SECTION_STATUS_OK,
            )
            for member in ReportSection
        ]
        status, reasons = compute_overall_status(sections)
        assert status == OVERALL_STATUS_OK
        assert reasons == []

    def test_compute_overall_status_some_missing(self):
        """部分 missing -> overall=partial。"""
        sections = [
            ReportSectionContent(
                section=member.value,
                title=SECTION_TITLES[member.value],
                section_status=(
                    SECTION_STATUS_MISSING if i == 2 else SECTION_STATUS_OK
                ),
            )
            for i, member in enumerate(ReportSection)
        ]
        status, reasons = compute_overall_status(sections)
        assert status == OVERALL_STATUS_PARTIAL
        assert len(reasons) == 1

    def test_compute_overall_status_majority_missing(self):
        """多数 missing -> overall=degraded。"""
        sections = [
            ReportSectionContent(
                section=member.value,
                title=SECTION_TITLES[member.value],
                section_status=(
                    SECTION_STATUS_OK if i < 2 else SECTION_STATUS_MISSING
                ),
            )
            for i, member in enumerate(ReportSection)
        ]
        status, reasons = compute_overall_status(sections)
        assert status == OVERALL_STATUS_DEGRADED
        assert len(reasons) >= 5
