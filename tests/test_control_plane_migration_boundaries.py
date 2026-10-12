from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from core.research.control_plane.migration import LazyResearchMigrator


def test_directory_path_degrades_to_report(tmp_path: Path) -> None:
    repository = MagicMock()

    report = LazyResearchMigrator(repository).migrate_jsonl(
        tmp_path, "document"
    )

    assert report.malformed_skipped == 0
    assert report.warnings
    repository.save_document.assert_not_called()


def test_non_object_json_does_not_abort_following_rows(tmp_path: Path) -> None:
    path = tmp_path / "claims.jsonl"
    path.write_text(
        "1\n" + json.dumps({"candidate_id": "candidate-1"}) + "\n",
        encoding="utf-8",
    )
    repository = MagicMock()

    report = LazyResearchMigrator(repository).migrate_jsonl(path, "claim")

    assert report.malformed_skipped == 1
    assert report.claims == 1
    repository.save_claim.assert_called_once()


def test_candidate_store_failure_and_bad_rows_are_isolated(tmp_path: Path) -> None:
    store = MagicMock()
    store.list_candidates.side_effect = RuntimeError("backend unavailable")

    report = LazyResearchMigrator(MagicMock()).migrate_candidate_store(store)

    assert report.malformed_skipped == 1
    assert report.warnings
