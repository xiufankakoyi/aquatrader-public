from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research_graph.growth_mining import (  # noqa: E402
    GrowthArtifactValidator,
    GrowthMiningEngine,
    GrowthMiningRequest,
    sha256_text,
)


PROFILE_PATH = PROJECT_ROOT / "data" / "config" / "research_graph_growth_acceptance.json"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _base_profile() -> dict:
    payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile = payload["defaults"].copy()
    profile.update(payload["profiles"][0])
    profile["all_forbidden_fixture_terms"] = payload["all_forbidden_fixture_terms"]
    profile["forbidden_trading_terms"] = payload["forbidden_trading_terms"]
    return profile


def _valid_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    raw_dir = run_dir / "raw_documents"
    raw_dir.mkdir(parents=True)
    texts = [
        "PVD physical vapor deposition is used in DRAM memory manufacturing steps.",
        "NAND memory process flows include physical vapor deposition, also called PVD.",
        "HBM memory manufacturing relies on PVD metallization in semiconductor process modules.",
    ]
    sources = []
    evidence = []
    claims = []
    hypotheses = []
    for idx, text in enumerate(texts):
        source_id = f"src_{idx}"
        doc_id = f"doc_{idx}"
        raw_path = raw_dir / f"{source_id}.txt"
        raw_path.write_text(text, encoding="utf-8")
        norm_path = raw_dir / f"{source_id}.normalized.txt"
        norm_path.write_text(text, encoding="utf-8")
        quote = text
        sources.append({
            "source_id": source_id,
            "document_id": doc_id,
            "provider_name": "FocusedCrawler",
            "provider_type": "crawler",
            "query_or_request": "fixture query",
            "request_id": f"req_{idx}",
            "retrieved_at": "2026-07-01T00:00:00+00:00",
            "url": f"https://example{idx}.com/doc",
            "domain": f"example{idx}.com",
            "publisher": f"example{idx}.com",
            "raw_document_path": str(raw_path),
            "normalized_document_path": str(norm_path),
            "content_hash": sha256_text(text),
            "is_live": idx < 2,
            "metadata": {"content_origin": "crawler_text"},
        })
        evidence_id = f"ev_{idx}"
        evidence.append({
            "evidence_id": evidence_id,
            "source_id": source_id,
            "document_id": doc_id,
            "raw_document_path": str(raw_path),
            "normalized_document_path": str(norm_path),
            "quote": quote,
            "span_start": 0,
            "span_end": len(quote),
            "stance": "support",
            "status": "CANDIDATE",
            "extraction_reason": "anchor_window_match",
            "anchor_window": {
                "primary_anchor": "PVD",
                "secondary_anchor": "memory",
                "distance_chars": 40,
                "same_paragraph": True,
                "paragraph_chars": len(quote),
            },
            "path_hint": ["fixture root", "fixture node"],
            "path_hint_source": "crawler_text",
            "content_origin": "crawler_text",
            "evidence_strength": "STRONG",
            "evidence_kind": "crawler_text",
        })
        claim_id = f"claim_{idx}"
        claims.append({
            "claim_id": claim_id,
            "status": "CANDIDATE",
            "subject": {"name": "node", "type": "PROCESS"},
            "relation": "RELATED_TO",
            "object": {"name": "object", "type": "PROCESS"},
            "source_id": source_id,
            "document_id": doc_id,
            "evidence_ids": [evidence_id],
            "quote": quote,
            "span_start": 0,
            "span_end": len(quote),
            "extraction_reason": "anchor_window_match",
            "path_hint": ["fixture root", "fixture node"],
            "path_hint_source": "crawler_text",
            "normalization_source": "crawler_text",
            "normalization_evidence_id": evidence_id,
            "evidence_strength": "STRONG",
            "evidence_kind": "crawler_text",
        })
        hypotheses.append({
            "hypothesis_id": f"gh_{idx}",
            "status": "CANDIDATE",
            "claim_ids": [claim_id],
            "evidence_ids": [evidence_id],
            "subject": {"name": "node", "type": "PROCESS"},
            "relation": "RELATED_TO",
            "object": {"name": "object", "type": "PROCESS"},
            "path_hint": ["fixture root", "fixture node"],
            "path_hint_source": "crawler_text",
            "normalization_source": "crawler_text",
        })
    _write_json(run_dir / "source_manifest.json", sources)
    _write_json(run_dir / "raw_evidence.json", evidence)
    _write_json(run_dir / "candidate_claims.json", claims)
    _write_json(run_dir / "counter_evidence.json", [])
    _write_json(run_dir / "graph_hypotheses.json", hypotheses)
    _write_json(run_dir / "run_options.json", {
        "run_status": "CANDIDATE",
        "apply_candidate": False,
        "staging_only": True,
    })
    (run_dir / "README.md").write_text("# run\n", encoding="utf-8")
    return run_dir


def test_growth_artifact_validator_accepts_traceable_live_staging_run(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    assert summary["status"] == "PASSED"
    assert summary["support_evidence_count"] == 3
    assert all(item["passed"] for item in summary["checks"])


def test_growth_artifact_validator_rejects_quote_not_found_and_active_status(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    raw_evidence = json.loads((run_dir / "raw_evidence.json").read_text(encoding="utf-8"))
    raw_evidence[0]["quote"] = "fabricated quote"
    _write_json(run_dir / "raw_evidence.json", raw_evidence)
    claims = json.loads((run_dir / "candidate_claims.json").read_text(encoding="utf-8"))
    claims[0]["status"] = "ACTIVE"
    _write_json(run_dir / "candidate_claims.json", claims)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    assert summary["status"] == "FAILED"
    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["quote_grounding_check"]["passed"] is False
    assert checks["candidate_only_graph_check"]["passed"] is False


def test_growth_artifact_validator_rejects_company_theme_direct_and_missing_path(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    hypotheses = json.loads((run_dir / "graph_hypotheses.json").read_text(encoding="utf-8"))
    hypotheses[0]["subject"] = {"name": "company", "type": "COMPANY"}
    hypotheses[0]["object"] = {"name": "theme", "type": "THEME"}
    hypotheses[0]["path_hint"] = []
    _write_json(run_dir / "graph_hypotheses.json", hypotheses)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["no_company_theme_direct_check"]["passed"] is False


def test_growth_artifact_validator_detects_fixture_hardcode_absence_in_core_files(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["anti_hardcode_check"]["passed"] is True
    assert checks["anti_hardcode_check"]["hits"] == []


def test_growth_artifact_validator_rejects_candidate_when_support_and_counter_conflict(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    raw_path = run_dir / "raw_documents" / "src_0.txt"
    conflict_text = "PVD physical vapor deposition appears near memory, but the company says no product and no plan."
    raw_path.write_text(conflict_text, encoding="utf-8")
    counter = [{
        "evidence_id": "ev_counter",
        "source_id": "src_0",
        "document_id": "doc_0",
        "raw_document_path": str(raw_path),
        "normalized_document_path": str(run_dir / "raw_documents" / "src_0.normalized.txt"),
        "quote": conflict_text,
        "span_start": 0,
        "span_end": len(conflict_text),
        "stance": "counter",
        "status": "CANDIDATE",
        "extraction_reason": "counter_anchor_window_match",
        "counter_anchor": "no product",
        "anchor_window": {
            "primary_anchor": "PVD",
            "secondary_anchor": "memory",
            "distance_chars": 38,
            "same_paragraph": True,
            "paragraph_chars": len(conflict_text),
        },
        "path_hint": ["fixture root", "fixture node"],
        "path_hint_source": "crawler_text",
        "content_origin": "crawler_text",
        "evidence_strength": "STRONG",
        "evidence_kind": "crawler_text",
    }]
    _write_json(run_dir / "counter_evidence.json", counter)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["counter_evidence_routing_check"]["passed"] is True
    assert checks["conflict_risk_check"]["passed"] is False


def test_growth_artifact_validator_rejects_ungraded_api_snippet_evidence(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    sources = json.loads((run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    sources[0]["provider_type"] = "api"
    sources[0]["metadata"] = {"content_origin": "search_snippet"}
    _write_json(run_dir / "source_manifest.json", sources)
    raw_evidence = json.loads((run_dir / "raw_evidence.json").read_text(encoding="utf-8"))
    raw_evidence[0]["evidence_strength"] = "STRONG"
    raw_evidence[0]["evidence_kind"] = "crawler_text"
    _write_json(run_dir / "raw_evidence.json", raw_evidence)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["snippet_evidence_grading_check"]["passed"] is False


def test_growth_artifact_validator_requires_llm_trace_when_llm_used(tmp_path: Path) -> None:
    run_dir = _valid_run_dir(tmp_path)
    options = json.loads((run_dir / "run_options.json").read_text(encoding="utf-8"))
    options["allow_llm_assist"] = True
    options["llm_assist_used"] = True
    options["llm_trace_path"] = str(run_dir / "missing_llm_trace.jsonl")
    _write_json(run_dir / "run_options.json", options)
    request = GrowthMiningRequest(min_evidence=3, live_mode=True, allow_llm_assist=True)

    summary = GrowthArtifactValidator(run_dir, request, _base_profile()).validate()

    checks = {item["name"]: item for item in summary["checks"]}
    assert checks["llm_trace_check"]["passed"] is False


def test_growth_engine_maps_company_from_local_structured_evidence(tmp_path: Path) -> None:
    request = GrowthMiningRequest(
        topic="光",
        node="indium_phosphide",
        company="有研新材",
        symbol="600206.SH",
        min_evidence=1,
        live_mode=True,
        use_system_search=False,
        use_vector_memory=False,
        acceptance_profile_path=PROFILE_PATH,
        run_root=tmp_path,
        run_name="company_mapping",
    )

    result = GrowthMiningEngine(request).run()

    assert result.validation_summary["status"] == "PASSED"
    claims = json.loads((result.run_dir / "candidate_claims.json").read_text(encoding="utf-8"))
    assert claims
    assert claims[0]["status"] == "CANDIDATE"
    assert claims[0]["path_hint"] == ["光", "optical_communication", "indium_phosphide"]
    assert claims[0]["relation"] == "MAPS_TO"
    assert claims[0]["normalization_source"] == "local_structured_record"
    assert claims[0]["path_hint_source"] == "local_structured_record"
    manifest = json.loads((result.run_dir / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["provider_type"] == "local_cache"
    assert manifest[0]["local_path"]
    assert manifest[0]["row_id"]
