"""Candidate engine 边界回归测试。"""
from __future__ import annotations

from core.research.candidate.contracts import ResearchCandidate, ScoreComputability
from core.research.candidate.engine import CandidateBuilder, CandidateReportBuilder
from core.research.earnings.interval import Interval


def _candidate(**overrides):
    payload = {
        "thesis": {
            "company_symbol": "300308.SZ",
            "required_claim_ids": ["claim_1"],
            "status": "active",
        },
        "earnings_impact": {"status": "ok"},
        "expectation_gap": {
            "status": "ok",
            "gap_confidence": {"low": 0.2, "high": 0.4},
        },
        "valuation_snapshot": {"historical_percentile": {"pe": 0.5}},
        "evidence_confidence": Interval(0.2, 0.4),
        "industry_core_score": 0.5,
        "company_exposure_score": 0.5,
        "catalysts": [],
    }
    payload.update(overrides)
    return CandidateBuilder().build(**payload)


def test_invalid_scores_and_valuation_are_degraded_without_crashing():
    candidate = _candidate(
        industry_core_score=float("nan"),
        company_exposure_score=float("inf"),
        valuation_snapshot={"historical_percentile": {"pe": "bad"}},
        catalysts=[{"window_end": 123}],
    )

    assert candidate.industry_core_score.computability == ScoreComputability.NOT_COMPUTABLE.value
    assert candidate.company_exposure_score.computability == ScoreComputability.NOT_COMPUTABLE.value
    assert candidate.status == "degraded"


def test_naive_and_invalid_catalyst_times_do_not_break_scoring():
    candidate = _candidate(
        as_of="2026-07-01",
        catalysts=[
            {"window_end": "2026-08-01"},
            {"window_end": "not-a-time"},
            {"window_end": None},
        ],
    )

    assert candidate.catalyst_score.components["active"] == 2
    assert candidate.catalyst_window == "until 2026-08-01"


def test_report_degrades_malformed_numeric_sections_to_missing_data():
    candidate = _candidate(
        earnings_impact={
            "status": "ok",
            "eps_impact": [],
            "incremental_revenue": {"low": "bad", "high": 10},
            "incremental_net_profit": {"low": 1, "high": 2},
            "falsification_conditions": None,
        },
        expectation_gap={
            "status": "ok",
            "gap": [],
            "relative_gap": {"low": "bad", "high": 0.2},
            "gap_confidence": {"low": 0.2, "high": 0.4},
        },
    )

    report = CandidateReportBuilder().build(candidate)

    assert "新增收入" not in report.report_sections["earnings_impact"]
    assert "新增净利润" in report.report_sections["earnings_impact"]
    assert "缺失原因" in report.report_sections["expectation_gap"]
    assert report.report_sections["contradictions"] == "暂无"


def test_source_trace_ignores_malformed_claim_collection():
    candidate = _candidate(
        thesis={
            "thesis_id": "thesis_1",
            "required_claim_ids": "claim_as_string",
            "status": "active",
        },
    )

    assert candidate.source_trace == ["thesis:thesis_1"]


def test_report_builder_handles_malformed_candidate_fields():
    report = CandidateReportBuilder().build(
        ResearchCandidate(
            earnings_impact=[],
            expectation_gap=[],
            research_score=None,
            alpha_score=None,
            core_score=None,
            catalyst_score=None,
            source_trace=None,
            invalidation_conditions=None,
        )
    )

    assert report.report_sections["earnings_impact"] == "暂无盈利影响数据"
    assert report.report_sections["expectation_gap"] == "暂无预期差数据"
    assert report.report_sections["scores"].count("unknown") == 3
