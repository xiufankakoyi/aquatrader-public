from __future__ import annotations

import copy
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph.evidence_corpus import (
    CorpusValidationError,
    REQUIRED_RUN_FILES,
    validate_run_directory,
    write_jsonl,
)
from scripts.run_overnight_evidence_corpus import build_run


def test_overnight_evidence_corpus_run_outputs_required_files_and_validates(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, "overnight_evidence_20260629_231500")

    assert {path.name for path in run_dir.iterdir()}.issuperset(set(REQUIRED_RUN_FILES))
    summary = validate_run_directory(run_dir)
    assert summary["sources"] == 18
    assert summary["successful_sources"] == 12
    assert summary["failed_sources"] == 6
    assert summary["raw_evidence"] == 12
    assert summary["candidate_claims"] == 12
    assert summary["counter_evidence"] == 3
    assert summary["graph_hypotheses"] == 6


def test_validator_rejects_raw_evidence_without_known_source_id(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, "bad_source_id")
    rows = _read_jsonl(run_dir / "raw_evidence.jsonl")
    rows[0]["source_id"] = "missing_source"
    write_jsonl(run_dir / "raw_evidence.jsonl", rows)

    try:
        validate_run_directory(run_dir)
    except CorpusValidationError as exc:
        assert "unknown source_id" in str(exc)
    else:
        raise AssertionError("validator accepted evidence with missing source_id")


def test_validator_rejects_high_confidence_claim_without_evidence_id(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, "bad_high_confidence")
    rows = _read_jsonl(run_dir / "candidate_claims.jsonl")
    rows[0]["supporting_evidence_ids"] = []
    rows[0]["suggested_confidence"] = "LIKELY"
    write_jsonl(run_dir / "candidate_claims.jsonl", rows)

    try:
        validate_run_directory(run_dir)
    except CorpusValidationError as exc:
        assert "high confidence without evidence_id" in str(exc)
    else:
        raise AssertionError("validator accepted high-confidence claim without evidence")


def test_validator_rejects_company_direct_theme_claim(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, "bad_company_theme")
    rows = _read_jsonl(run_dir / "candidate_claims.jsonl")
    bad = copy.deepcopy(rows[0])
    bad["claim_id"] = "bad_company_benefits_from_theme"
    bad["subject"] = {"name": "公司A", "type": "COMPANY"}
    bad["relation"] = "BENEFITS_FROM"
    bad["object"] = {"name": "HBM", "type": "THEME"}
    bad["path_hint"] = ["HBM", "公司A"]
    rows.append(bad)
    write_jsonl(run_dir / "candidate_claims.jsonl", rows)

    try:
        validate_run_directory(run_dir)
    except CorpusValidationError as exc:
        assert "directly connects to THEME" in str(exc)
    else:
        raise AssertionError("validator accepted company-to-theme claim")


def test_validator_rejects_non_mainboard_claim_marked_eligible(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, "bad_mainboard")
    rows = _read_jsonl(run_dir / "candidate_claims.jsonl")
    rows[0]["main_board_relevance"] = "eligible"
    rows[0]["stock_code"] = "688001.SH"
    write_jsonl(run_dir / "candidate_claims.jsonl", rows)

    try:
        validate_run_directory(run_dir)
    except CorpusValidationError as exc:
        assert "non-mainboard claim marked eligible" in str(exc)
    else:
        raise AssertionError("validator accepted non-mainboard eligible claim")


def _read_jsonl(path: Path) -> list[dict]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
