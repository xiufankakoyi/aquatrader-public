#!/usr/bin/env python
"""Build the research cockpit probe from existing real research artifacts."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.perilla.discovery.report_causal_extractor import (
    run_report_causal_extractor,
)
from scripts.build_research_cockpit import (
    BASELINE_DIR,
    DEFAULT_RUN_DIR,
    LATEST_COCKPIT_DIR,
    build_cockpit,
)


P0_FILES = (
    "raw_documents_latest.json",
    "candidate_concepts_latest.json",
    "evidence_edges_latest.json",
    "search_tasks_latest.json",
    "search_task_runs_latest.json",
    "selected_new_term_latest.json",
)


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_probe_inputs(source_dir: Path, run_dir: Path) -> dict[str, int]:
    """Copy P0 audit files and select only the chosen term's source documents."""
    p0_dir = run_dir / "p0"
    focus_dir = run_dir / "focus_p1"
    p0_dir.mkdir(parents=True, exist_ok=True)
    focus_dir.mkdir(parents=True, exist_ok=True)

    for name in P0_FILES:
        source = source_dir / name
        if not source.exists():
            raise FileNotFoundError(f"required real artifact missing: {source}")
        shutil.copy2(source, p0_dir / name)

    documents = _load_json(source_dir / "raw_documents_latest.json")
    selected = _load_json(source_dir / "selected_new_term_latest.json")
    selected_ids = {str(value) for value in selected.get("evidence_document_ids", [])}
    focus_documents = [
        document
        for document in documents
        if str(document.get("document_id", "")) in selected_ids
    ]
    if not focus_documents:
        raise RuntimeError("selected term has no matching real source documents")

    (focus_dir / "raw_documents_latest.json").write_text(
        json.dumps(focus_documents, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "source_documents": len(documents),
        "focus_documents": len(focus_documents),
    }


def run_probe(
    source_dir: Path = BASELINE_DIR,
    run_dir: Path = DEFAULT_RUN_DIR,
    *,
    use_llm: bool = True,
) -> dict:
    prepared = prepare_probe_inputs(source_dir, run_dir)
    focus_dir = run_dir / "focus_p1"
    p1_summary = run_report_causal_extractor(
        input_dir=focus_dir,
        output_dir=focus_dir,
        use_llm=use_llm,
    )
    if not p1_summary.get("success"):
        raise RuntimeError(str(p1_summary.get("error") or "P1 extractor failed"))
    (focus_dir / "p1_summary.json").write_text(
        json.dumps(p1_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cockpit = build_cockpit(run_dir)
    output = LATEST_COCKPIT_DIR / "cockpit_data.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cockpit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        **prepared,
        "evidence": cockpit["summary"]["canonical_evidence_count"],
        "llm_calls": cockpit["baseline_comparison"]["kpis"]["llm_calls"],
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行真实 Research cockpit 验收探针")
    parser.add_argument("--source-dir", default=str(BASELINE_DIR))
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    result = run_probe(
        Path(args.source_dir), Path(args.run_dir), use_llm=not args.no_llm
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["evidence"] >= 1 and (args.no_llm or result["llm_calls"] >= 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
