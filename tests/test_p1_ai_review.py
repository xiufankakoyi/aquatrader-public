from __future__ import annotations

from copy import deepcopy

from core.research.integration.p1_ai_review import audit_p1_ai_evidence
from scripts.audit_p1_ai_review import _row_value_present


def _valid_evidence() -> dict:
    return {
        "schema_version": "p1_ai_review_v1",
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "as_of": "2026-07-23",
        "companies": [
            {
                "symbol": "600519.SH",
                "annual_report": {
                    "period": "2025-12-31",
                    "published_at": "2026-04-17",
                    "pdf_sha256": "a" * 64,
                },
                "statements": {
                    "period": "2025-12-31",
                    "balance": {
                        "page": 57,
                        "assets": 100,
                        "liabilities": 40,
                        "equity": 60,
                        "equation_verified": True,
                    },
                    "income": {
                        "page": 61,
                        "revenue": 80,
                        "net_profit": 20,
                        "applicable_equations_verified": True,
                    },
                    "cash_flow": {
                        "page": 64,
                        "cash_begin": 10,
                        "cash_end": 15,
                        "cash_change": 5,
                        "cfo": 12,
                        "cfi": -4,
                        "cff": -2,
                        "fx_effect": -1,
                        "equation_verified": True,
                    },
                    "currency": "CNY",
                    "unit": "元",
                    "scope": "consolidated",
                    "pdf_rows_verified": True,
                },
                "operating_model": {
                    "period": "2025-12-31",
                    "segment_rows_verified": True,
                    "forward_plan_anchor_verified": True,
                    "qualitative_plan_numeric_fabrication": False,
                },
                "valuation": {
                    "as_of": "2026-07-23",
                    "unsupported_assumptions_present": False,
                    "method_applicability_verified": True,
                    "investment_advice_present": False,
                },
                "peers": {
                    "status": "not_generated_missing_business_structure_evidence",
                    "rows": [],
                    "basis_verified": True,
                },
                "primary_review": {"status": "passed", "reviewer_kind": "ai"},
                "adversarial_review": {"status": "passed", "reviewer_kind": "ai"},
            }
        ],
    }


def test_valid_ai_only_internal_evidence_can_pass_without_human_identity():
    report = audit_p1_ai_evidence(_valid_evidence(), expected_symbols={"600519.SH"})

    assert report["overall_passed"] is True
    assert report["status"] == "P1_AI_CERTIFIED"
    assert report["human_independent_review"] is False
    assert report["identity_independent"] is False


def test_mixed_statement_and_operating_model_periods_fail_closed():
    evidence = _valid_evidence()
    evidence["companies"][0]["statements"]["period"] = "2024-12-31"

    report = audit_p1_ai_evidence(evidence, expected_symbols={"600519.SH"})

    assert report["overall_passed"] is False
    assert "period_mismatch" in report["companies"][0]["failures"]


def test_missing_statement_field_fails_closed():
    evidence = _valid_evidence()
    del evidence["companies"][0]["statements"]["cash_flow"]["cash_end"]

    report = audit_p1_ai_evidence(evidence, expected_symbols={"600519.SH"})

    assert report["overall_passed"] is False
    assert "cash_flow_fields_missing" in report["companies"][0]["failures"]


def test_report_published_after_review_as_of_fails_closed():
    evidence = _valid_evidence()
    evidence["as_of"] = "2026-04-01"

    report = audit_p1_ai_evidence(evidence, expected_symbols={"600519.SH"})

    assert report["overall_passed"] is False
    assert "annual_report_not_pit_available" in report["companies"][0]["failures"]


def test_empty_peers_only_pass_with_explicit_no_conclusion_status():
    evidence = _valid_evidence()
    evidence["companies"][0]["peers"]["status"] = "qualified_peers"

    report = audit_p1_ai_evidence(evidence, expected_symbols={"600519.SH"})

    assert report["overall_passed"] is False
    assert "peer_evidence_invalid" in report["companies"][0]["failures"]


def test_primary_or_adversarial_failure_cannot_be_offset():
    evidence = _valid_evidence()
    evidence["companies"][0]["adversarial_review"]["status"] = "failed"

    report = audit_p1_ai_evidence(evidence, expected_symbols={"600519.SH"})

    assert report["overall_passed"] is False
    assert "adversarial_review_not_passed" in report["companies"][0]["failures"]


def test_missing_expected_company_fails_the_bundle():
    evidence = deepcopy(_valid_evidence())

    report = audit_p1_ai_evidence(
        evidence, expected_symbols={"600519.SH", "300750.SZ"}
    )

    assert report["overall_passed"] is False
    assert report["bundle_failures"] == ["expected_company_set_mismatch"]


def test_malformed_bundle_fails_closed_without_raising():
    report = audit_p1_ai_evidence(None, expected_symbols=None)
    assert report["overall_passed"] is False
    assert "review_mode_contract_invalid" in report["bundle_failures"]


def test_pdf_value_must_be_bound_to_its_row_anchor():
    text = "资产总计 100 90\n其他项目 40 30\n负债合计 20 10"

    assert _row_value_present(text, ("资产总计",), 100) is True
    assert _row_value_present(text, ("资产总计",), 40) is False


def test_pdf_row_binding_supports_wrapped_labels():
    text = "经营活动产生的现金流 61,522,204,989.35\n量净额"

    assert _row_value_present(
        text, ("经营活动产生的现金流",), 61522204989.35
    ) is True
