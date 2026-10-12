"""Tests for annual-report smoke evidence binding."""

import os
import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_p1_annual_report_models import (
    _peer_basis_audit,
    _reported_number_is_present,
    _segment_evidence_checks,
)


def test_reported_number_rejects_substring_inside_another_token() -> None:
    assert not _reported_number_is_present("x1234y", 123)


def test_reported_decimal_survives_concatenated_pdf_columns() -> None:
    assert _reported_number_is_present("收入 16,671,093,834.941,086,375,839.14", 16671093834.94)


def test_row_evidence_requires_label_and_value_on_same_line() -> None:
    evidence = {
        "segments": [
            {"name": "动力电池系统", "revenue": 316506369},
            {
                "name": "手机部件、组装及其他产品",
                "pdf_label": "日用电子器件制",
                "revenue": 155236528000,
            },
        ]
    }
    page = (
        "动力电池系统 316,506,369\n"
        "日用电子器件制 155,236,528,000.00 19.31%\n"
    )

    assert _segment_evidence_checks(page, evidence) == [True, True]


def test_row_evidence_does_not_combine_unrelated_page_hits() -> None:
    evidence = {"segments": [{"name": "动力电池系统", "revenue": 316506369}]}
    page = "动力电池系统\n其他表格 316,506,369\n"

    assert _segment_evidence_checks(page, evidence) == [False]


def test_columnar_evidence_binds_labels_to_ordered_total_row() -> None:
    evidence = {
        "segment_evidence_mode": "columnar_row",
        "segment_value_row_anchor": "营业收入合计",
        "segments": [
            {"name": "寿险及健康险", "pdf_label_parts": ["寿险及", "健康险"], "revenue": 502171},
            {"name": "财产保险", "revenue": 353272},
            {
                "name": "其他业务及合并抵销",
                "pdf_label_parts": ["其他业务及", "合并抵销"],
                "revenue": -37083,
            },
        ],
    }
    page = (
        "寿险及 其他业务及\n健康险 财产保险 合并抵销 合计\n"
        "营业收入合计 502, 171 353, 272 (37, 083) 818,360\n"
    )

    assert _segment_evidence_checks(page, evidence) == [True, True, True]


def test_columnar_evidence_rejects_values_in_wrong_order() -> None:
    evidence = {
        "segment_evidence_mode": "columnar_row",
        "segment_value_row_anchor": "Operating income",
        "segments": [
            {"name": "批发金融业务", "pdf_label": "Wholesale finance business", "revenue": 137939},
            {"name": "零售金融业务", "pdf_label": "Retail finance business", "revenue": 190988},
        ],
    }
    page = (
        "Wholesale\nfinance business Retail finance business\n"
        "Operating income 190,988 137,939\n"
    )

    assert _segment_evidence_checks(page, evidence) == [False, False]


def test_peer_audit_accepts_honest_empty_degradation_without_structure_evidence() -> None:
    result = _peer_basis_audit([], {"industry": "行业甲"}, "2026-07-22", requested=True)

    assert result == {
        "peer_basis_verified": True,
        "peer_comparison_status": "not_generated_missing_business_structure_evidence",
    }


def test_peer_audit_rejects_unsourced_comparable_rows() -> None:
    target = {
        "industry": "行业甲",
        "business_structure": "结构甲",
        "business_structure_source_ref": "annual_report:target",
    }
    peer = {
        "industry": "行业甲",
        "business_structure": "结构甲",
        "scale_ratio": 1.2,
        "as_of": "2026-07-21",
        "multiple_type": "pe_ttm",
        "currency": "CNY",
        "accounting_standard": "PRC_ASBE",
        "selection_basis": "same_industry_business_structure_and_market_cap_0.25x_to_4x",
        "source_ref": "tushare:valuation_history:600002.SH:2026-07-21",
    }

    assert not _peer_basis_audit([peer], target, "2026-07-22", requested=True)["peer_basis_verified"]


def test_peer_audit_rejects_invalid_scale_without_crashing() -> None:
    target = {
        "industry": "行业甲",
        "business_structure": "结构甲",
        "business_structure_source_ref": "annual_report:target",
    }
    peer = {
        "industry": "行业甲",
        "business_structure": "结构甲",
        "business_structure_source_ref": "annual_report:peer",
        "target_business_structure_source_ref": "annual_report:target",
        "scale_ratio": "invalid",
    }

    assert not _peer_basis_audit([peer], target, "2026-07-22", requested=True)["peer_basis_verified"]


def test_smoke_script_help_runs_without_manual_pythonpath() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, "scripts/smoke_p1_annual_report_models.py", "--help"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_smoke_script_fails_closed_for_corrupt_manifest(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    (tmp_path / "operating_model_evidence.json").write_text("{not-json", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_p1_annual_report_models.py",
            "--root",
            str(tmp_path),
            "--output",
            str(tmp_path / "audit.json"),
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert audit["manifest_errors"] == ["manifest_unreadable"]
