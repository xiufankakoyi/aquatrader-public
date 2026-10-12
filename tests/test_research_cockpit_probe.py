from __future__ import annotations

import json

from scripts.run_research_cockpit_probe import prepare_probe_inputs


def test_prepare_probe_inputs_uses_selected_real_documents(tmp_path) -> None:
    source = tmp_path / "source"
    run_dir = tmp_path / "probe"
    source.mkdir()
    documents = [
        {"document_id": "doc_keep", "title": "保留"},
        {"document_id": "doc_skip", "title": "跳过"},
    ]
    (source / "raw_documents_latest.json").write_text(
        json.dumps(documents, ensure_ascii=False), encoding="utf-8"
    )
    (source / "selected_new_term_latest.json").write_text(
        json.dumps(
            {"normalized_name": "真实主题", "evidence_document_ids": ["doc_keep"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for name in (
        "candidate_concepts_latest.json",
        "evidence_edges_latest.json",
        "search_tasks_latest.json",
        "search_task_runs_latest.json",
    ):
        (source / name).write_text("[]", encoding="utf-8")

    result = prepare_probe_inputs(source, run_dir)

    focus_documents = json.loads(
        (run_dir / "focus_p1" / "raw_documents_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"source_documents": 2, "focus_documents": 1}
    assert focus_documents == [{"document_id": "doc_keep", "title": "保留"}]
    assert (run_dir / "p0" / "selected_new_term_latest.json").exists()
