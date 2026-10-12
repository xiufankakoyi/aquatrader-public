from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dashboard import app
from scripts.build_research_cockpit import build_cockpit


def test_build_research_cockpit_contains_audit_views() -> None:
    data = build_cockpit(Path("data/research_runs/cockpit_real_probe"))

    assert data["summary"]["canonical_evidence_count"] >= 1
    assert data["baseline_comparison"]["kpis"]["raw_documents"] >= 1
    assert data["baseline_comparison"]["kpis"]["candidate_concepts"] >= 1
    assert data["baseline_comparison"]["kpis"]["claims"] >= 1
    assert data["baseline_comparison"]["kpis"]["causal_chains"] >= 1
    assert data["baseline_comparison"]["kpis"]["llm_calls"] >= 1
    assert data["system_flow"]["lineage"]
    assert data["system_flow"]["architecture"]
    assert data["system_flow"]["code_modules"]
    assert data["target_judgment"]["conclusion_level"] == "INSUFFICIENT_EVIDENCE"


def test_dashboard_exposes_research_contribution_endpoints() -> None:
    client = TestClient(app)

    assert client.get("/api/system-flow").status_code == 200
    assert client.get("/api/baseline-comparison").status_code == 200
    assert client.get("/api/target-judgment").status_code == 200
