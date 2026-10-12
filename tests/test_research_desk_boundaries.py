from __future__ import annotations

import json

from core.research.desk import agents
from core.research.desk.discussion_protocol import AgentArgument, AgentDebate, build_arguments_from_agent_outputs


def _write_report(tmp_path, filename: str, payload) -> None:
    (tmp_path / filename).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_read_json_rejects_non_object_and_path_traversal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agents, "REPORT_DIR", tmp_path)
    _write_report(tmp_path, "list.json", [1, 2, 3])

    assert agents._read_json("list.json") == {}
    assert agents._read_json("../list.json") == {}


def test_dataops_handles_malformed_counts_and_stages(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agents, "REPORT_DIR", tmp_path)
    _write_report(
        tmp_path,
        "quant_flow_latest.json",
        {
            "global_latest_trade_date": "2026-07-31",
            "stages": [None, "invalid", {"stage": "factor", "data_date": "2026-07-30"}],
        },
    )
    _write_report(tmp_path, "data_health_latest.json", {"status": "ok"})
    _write_report(
        tmp_path,
        "evidence_center_latest.json",
        {"total_evidence": "3", "stale_evidence": "9"},
    )

    result = agents.DataOpsAgent().run()

    assert result.decision == "BLOCK"
    assert result.details["total_evidence"] == 3
    assert result.details["stale_evidence"] == 3
    assert result.details["fresh_evidence"] == 0


def test_research_and_quant_agents_skip_malformed_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agents, "REPORT_DIR", tmp_path)
    _write_report(
        tmp_path,
        "quant_flow_latest.json",
        {
            "global_latest_trade_date": "2026-07-31",
            "stages": [
                None,
                {"stage": "dragon_eye_summary", "data": {"evidence": "invalid"}},
                {
                    "stage": "stock_screener_candidates",
                    "data": {"no_candidates": False, "candidates": [None, {"symbol": "000001"}]},
                },
                {
                    "stage": "strategy_signal_scan",
                    "data": {"buy": [None], "watch": [{"stock_code": "000001"}]},
                },
            ],
        },
    )
    _write_report(
        tmp_path,
        "evidence_center_latest.json",
        {
            "evidences": [
                None,
                {"evidence_id": "e1", "symbols": "000001", "themes": "broken"},
                {"evidence_id": "e2", "symbols": ["000001"], "themes": ["主题"]},
            ]
        },
    )

    research = agents.ResearchAgent().run()
    quant = agents.QuantValidationAgent().run()

    assert research.decision == "PASS"
    assert quant.details["validation_results"][0]["symbol"] == "000001"


def test_discussion_protocol_normalizes_bad_arguments() -> None:
    argument = AgentArgument(
        "agent",
        "stock",
        "000001",
        "support",
        "claim",
        "high",
        evidence_ids="invalid",
        risk_flags=["risk", 1],
    )

    result = AgentDebate({"agent": "invalid"}).debate(
        "stock", "000001", [argument, None]
    )

    assert argument.to_dict()["evidence_ids"] == []
    assert argument.to_dict()["risk_flags"] == ["risk"]
    assert result.to_dict()["support_score"] == 1.0
    assert build_arguments_from_agent_outputs(
        None, None, None, None, theme_candidates=[None, {"name": "主题"}]
    )
