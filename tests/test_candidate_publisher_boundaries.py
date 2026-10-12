"""ResearchCandidatePublisher 边界回归测试。"""
from __future__ import annotations

from types import SimpleNamespace

from core.research.candidate.publisher import ResearchCandidatePublisher
from core.research.expectations.contracts import InvestmentThesis


def _publisher(*, earnings_repo=None, claim_repo=None):
    return ResearchCandidatePublisher(
        expectations_repo=SimpleNamespace(),
        earnings_repo=earnings_repo or SimpleNamespace(),
        candidate_repo=SimpleNamespace(),
        claim_repo=claim_repo,
    )


def test_load_earnings_impact_skips_malformed_rows_and_keeps_valid_row():
    class EarningsRepo:
        def list_impacts(self, **_kwargs):
            return [
                object(),
                SimpleNamespace(
                    symbol="300308.SZ",
                    to_dict=lambda: {"impact_id": "impact_1", "status": "ok"},
                ),
            ]

    impact = _publisher(earnings_repo=EarningsRepo())._load_earnings_impact("300308.SZ")

    assert impact["impact_id"] == "impact_1"


def test_attach_claim_trace_treats_string_claim_ids_as_one_id():
    calls = []

    class ClaimRepo:
        def get_claim(self, claim_id):
            calls.append(claim_id)
            return SimpleNamespace(
                claim_id=claim_id,
                research_status="supported",
                mapping_confidence=1.5,
                subject_id="company_1",
                valid_from="2026-01-01",
                created_at="2026-01-02",
            )

    candidate = SimpleNamespace(
        candidate_id="candidate_1",
        claim_id="",
        claim_status="",
        mapping_confidence=0.0,
        company_id="",
        evidence_as_of="",
    )
    thesis = InvestmentThesis(required_claim_ids="claim_1")

    _publisher(claim_repo=ClaimRepo())._attach_claim_trace(candidate, thesis)

    assert calls == ["claim_1"]
    assert candidate.claim_id == "claim_1"
    assert candidate.mapping_confidence == 1.0


def test_invalid_structured_confidence_is_clamped_to_unit_interval():
    thesis = SimpleNamespace(
        attributes={
            "evidence_confidence": {"low": -0.5, "high": 1.5},
        },
    )

    confidence = _publisher()._derive_evidence_confidence(thesis)

    assert confidence.low == 0.0
    assert confidence.high == 1.0


def test_malformed_derived_inputs_degrade_without_crashing():
    publisher = _publisher()

    assert publisher._derive_company_exposure_score(InvestmentThesis(), []) is None
    assert publisher._derive_completeness("run_1", []) == "research_complete"
    assert publisher._collect_research_gaps(["bad"], [{"not": "hashable"}, "known"]) == ["known"]
