"""Fail-closed audit contract for the owner-authorized P1 AI-only review."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


_BALANCE_FIELDS = {"page", "assets", "liabilities", "equity", "equation_verified"}
_INCOME_FIELDS = {
    "page",
    "revenue",
    "net_profit",
    "applicable_equations_verified",
}
_CASH_FLOW_FIELDS = {
    "page",
    "cash_begin",
    "cash_end",
    "cash_change",
    "cfo",
    "cfi",
    "cff",
    "fx_effect",
    "equation_verified",
}
_EMPTY_PEER_STATUSES = {
    "not_generated_missing_business_structure_evidence",
    "no_qualified_peer",
    "not_requested",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _has_fields(value: object, required: set[str]) -> bool:
    row = _mapping(value)
    return required.issubset(row) and all(row[key] is not None for key in required)


def _review_passed(value: object) -> bool:
    row = _mapping(value)
    return row.get("status") == "passed" and row.get("reviewer_kind") == "ai"


def _audit_company(company: Mapping[str, Any], review_as_of: str) -> dict[str, Any]:
    failures: list[str] = []
    annual = _mapping(company.get("annual_report"))
    statements = _mapping(company.get("statements"))
    operating = _mapping(company.get("operating_model"))
    valuation = _mapping(company.get("valuation"))
    peers = _mapping(company.get("peers"))

    period_values = [annual.get("period"), statements.get("period"), operating.get("period")]
    if not all(isinstance(value, str) and value for value in period_values) or len(set(period_values)) != 1:
        failures.append("period_mismatch")

    published_at = annual.get("published_at")
    pdf_sha256 = annual.get("pdf_sha256")
    if (
        not isinstance(published_at, str)
        or not published_at
        or not isinstance(pdf_sha256, str)
        or len(pdf_sha256) != 64
    ):
        failures.append("annual_report_trace_invalid")
    elif not review_as_of or published_at[:10] > review_as_of[:10]:
        failures.append("annual_report_not_pit_available")

    if not _has_fields(statements.get("balance"), _BALANCE_FIELDS):
        failures.append("balance_fields_missing")
    if not _has_fields(statements.get("income"), _INCOME_FIELDS):
        failures.append("income_fields_missing")
    if not _has_fields(statements.get("cash_flow"), _CASH_FLOW_FIELDS):
        failures.append("cash_flow_fields_missing")
    if (
        statements.get("currency") != "CNY"
        or not statements.get("unit")
        or statements.get("scope") != "consolidated"
        or statements.get("pdf_rows_verified") is not True
    ):
        failures.append("statement_basis_invalid")

    balance = _mapping(statements.get("balance"))
    income = _mapping(statements.get("income"))
    cash_flow = _mapping(statements.get("cash_flow"))
    if balance.get("equation_verified") is not True:
        failures.append("balance_equation_not_verified")
    if income.get("applicable_equations_verified") is not True:
        failures.append("income_equations_not_verified")
    if cash_flow.get("equation_verified") is not True:
        failures.append("cash_flow_equation_not_verified")

    if (
        operating.get("segment_rows_verified") is not True
        or operating.get("forward_plan_anchor_verified") is not True
        or operating.get("qualitative_plan_numeric_fabrication") is not False
    ):
        failures.append("operating_model_evidence_invalid")

    if (
        valuation.get("unsupported_assumptions_present") is not False
        or valuation.get("method_applicability_verified") is not True
        or valuation.get("investment_advice_present") is not False
    ):
        failures.append("valuation_evidence_invalid")

    peer_rows = peers.get("rows")
    peer_status = peers.get("status")
    peer_valid = (
        isinstance(peer_rows, list)
        and peers.get("basis_verified") is True
        and (
            (bool(peer_rows) and peer_status == "qualified_peers")
            or (not peer_rows and peer_status in _EMPTY_PEER_STATUSES)
        )
    )
    if not peer_valid:
        failures.append("peer_evidence_invalid")

    if not _review_passed(company.get("primary_review")):
        failures.append("primary_review_not_passed")
    if not _review_passed(company.get("adversarial_review")):
        failures.append("adversarial_review_not_passed")

    return {
        "symbol": str(company.get("symbol") or ""),
        "passed": not failures,
        "failures": failures,
    }


def audit_p1_ai_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_symbols: Iterable[str],
) -> dict[str, Any]:
    """Audit a P1 AI evidence bundle without treating AI as a human signer."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    bundle_failures: list[str] = []
    companies = evidence.get("companies")
    company_rows = companies if isinstance(companies, list) else []
    expected = {str(symbol) for symbol in expected_symbols if isinstance(symbol, (str, int))} if expected_symbols is not None else set()
    actual = {
        str(row.get("symbol") or "")
        for row in company_rows
        if isinstance(row, Mapping)
    }
    if actual != expected:
        bundle_failures.append("expected_company_set_mismatch")
    if (
        evidence.get("schema_version") != "p1_ai_review_v1"
        or evidence.get("review_mode") != "ai_only_internal"
        or evidence.get("human_independent_review") is not False
        or evidence.get("identity_independent") is not False
    ):
        bundle_failures.append("review_mode_contract_invalid")

    audited = [
        _audit_company(row, str(evidence.get("as_of") or ""))
        for row in company_rows
        if isinstance(row, Mapping)
    ]
    passed = not bundle_failures and len(audited) == len(expected) and all(
        row["passed"] for row in audited
    )
    return {
        "status": "P1_AI_CERTIFIED" if passed else "P1_AI_REVIEW_FAILED",
        "overall_passed": passed,
        "review_mode": "ai_only_internal",
        "human_independent_review": False,
        "identity_independent": False,
        "bundle_failures": bundle_failures,
        "companies": audited,
    }
