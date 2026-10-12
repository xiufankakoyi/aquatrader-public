from __future__ import annotations

from core.research.reporting.cfa_report_markdown import (
    _format_amount,
    _format_percent,
    _format_ratio,
    render_cfa_report_section_to_markdown,
    render_cfa_report_to_markdown,
    render_evidence_snapshot_to_markdown,
)


def test_non_finite_numbers_degrade_instead_of_rendering_fake_values() -> None:
    assert _format_amount(float("nan")) == "暂无数据"
    assert _format_percent(float("inf")) == "暂无数据"
    assert _format_ratio(float("-inf")) == "暂无数据"


def test_malformed_report_and_section_inputs_do_not_crash() -> None:
    assert render_cfa_report_section_to_markdown(object()) == ""
    assert "类型无效" in render_cfa_report_to_markdown(["bad"])


def test_malformed_snapshot_rows_degrade_without_crashing() -> None:
    markdown = render_evidence_snapshot_to_markdown({
        "meta": [],
        "key_metrics": [],
        "balance_check": [],
        "valuation_cases": [None, {"assumptions": [None]}],
        "consensus_estimates": [None],
        "data_gaps": [None],
        "review_checklist_hints": [None],
    })

    assert "A.2" in markdown
    assert "A.4" in markdown
    assert "A.6" in markdown
