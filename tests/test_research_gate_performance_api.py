from __future__ import annotations

import json

from flask import Flask

import server.routes.research_model_routes as routes


def test_workbench_serves_research_gate_performance_report(
    tmp_path, monkeypatch
) -> None:
    report_path = tmp_path / "performance.json"
    report_path.write_text(json.dumps({
        "schema_version": "p2-research-gate-performance-v1",
        "scope": "historical_reconstruction_not_trade_advice",
        "overall_valid": True,
        "evaluation_days": 20,
        "leakage_count": 0,
        "selection_timestamp_bias": True,
        "research_gated": {"total_return": -0.05},
        "active_baseline": {"total_return": -0.07},
        "passive_equal_weight": {"total_return": -0.04},
    }), encoding="utf-8")
    monkeypatch.setattr(routes, "RESEARCH_GATE_PERFORMANCE_PATH", report_path)
    app = Flask(__name__)
    app.register_blueprint(routes.research_model_bp)

    response = app.test_client().get(
        "/api/research/models/workbench/research-gate-performance"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["performance"]["evaluation_days"] == 20
    assert payload["data"]["performance"]["selection_timestamp_bias"] is True


def test_workbench_performance_endpoint_degrades_when_report_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        routes, "RESEARCH_GATE_PERFORMANCE_PATH", tmp_path / "missing.json"
    )
    app = Flask(__name__)
    app.register_blueprint(routes.research_model_bp)

    response = app.test_client().get(
        "/api/research/models/workbench/research-gate-performance"
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["degraded"] is True
