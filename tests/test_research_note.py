from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.research.research_note import ResearchNote


def test_note_round_trips_json_and_markdown(tmp_path: Path) -> None:
    note = ResearchNote(tmp_path)
    note.update_metadata(
        hypothesis="test",
        parameters={"lookback": 5},
        key_findings=["finding"],
    )

    json_path = Path(note.save("note.json"))
    markdown_path = Path(note.save_markdown("note.md"))

    assert json.loads(json_path.read_text(encoding="utf-8"))["hypothesis"] == "test"
    assert "- finding" in markdown_path.read_text(encoding="utf-8")


def test_note_rejects_paths_outside_note_directory(tmp_path: Path) -> None:
    note = ResearchNote(tmp_path / "notes")

    with pytest.raises(ValueError):
        note.save("../outside.json")
    with pytest.raises(ValueError):
        note.save(str(tmp_path / "outside.json"))


def test_malformed_metadata_degrades_in_markdown(tmp_path: Path) -> None:
    note = ResearchNote(tmp_path)
    note.metadata["key_findings"] = None
    note.metadata["parameters"] = None

    markdown = note.to_markdown()

    assert "Not recorded" in markdown
    assert "```json" in markdown
