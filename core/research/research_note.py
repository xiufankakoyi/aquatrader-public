"""Small, file-backed research note helper.

The class deliberately keeps the original public API, while treating note
filenames and user-provided metadata as untrusted input.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from config.logger import get_logger


logger = get_logger(__name__)


class ResearchNote:
    """Store a small research note as JSON and Markdown."""

    _DEFAULT_METADATA: dict[str, Any] = {
        "hypothesis": "",
        "experiment_design": "",
        "data_sources": [],
        "parameters": {},
        "key_findings": [],
        "learnings": [],
        "next_steps": [],
        "backtest_id": "",
        "strategy_version": "",
    }

    def __init__(self, note_dir: str | Path = "data/research_notes") -> None:
        self.note_dir = Path(note_dir)
        self.note_dir.mkdir(parents=True, exist_ok=True)
        self.metadata = {
            **self._DEFAULT_METADATA,
            "data_sources": [],
            "parameters": {},
            "key_findings": [],
            "learnings": [],
            "next_steps": [],
            "git_commit": self._get_git_commit(),
            "timestamp": datetime.now().isoformat(),
        }

    def _get_git_commit(self) -> str:
        """Return the current repository commit, or ``unknown`` outside Git."""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.note_dir,
                stderr=subprocess.DEVNULL,
                timeout=3,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def update_metadata(self, **kwargs: Any) -> None:
        """Update known metadata fields and ignore unknown fields."""
        for key, value in kwargs.items():
            if key in self.metadata:
                self.metadata[key] = value
            else:
                logger.warning("Unknown research note metadata field: %s", key)

    def _path_for(self, filename: str | Path | None, suffix: str) -> Path:
        if filename is None or str(filename).strip() == "":
            filename = f"note_{datetime.now():%Y%m%d_%H%M%S}{suffix}"
        raw_name = str(filename)
        candidate = Path(raw_name)
        if (
            candidate.is_absolute()
            or candidate.name != raw_name
            or candidate.name in {".", ".."}
        ):
            raise ValueError("filename must be a single file name inside note_dir")
        directory = self.note_dir.resolve()
        target = (directory / candidate.name).resolve()
        if target.parent != directory:
            raise ValueError("filename must stay inside note_dir")
        return target

    def save(self, filename: str | Path | None = None) -> str:
        """Save the note as UTF-8 JSON and return its path."""
        file_path = self._path_for(filename, ".json")
        payload = json.dumps(
            self.metadata,
            indent=4,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
        file_path.write_text(payload + "\n", encoding="utf-8")
        logger.info("Research note saved: %s", file_path)
        return str(file_path)

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value)

    @classmethod
    def _lines(cls, value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [cls._text(item) for item in value if item is not None]
        text = cls._text(value).strip()
        return [text] if text else []

    @staticmethod
    def _parameters_json(value: Any) -> str:
        if not isinstance(value, Mapping):
            value = {}
        try:
            return json.dumps(
                value,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
                default=str,
            )
        except (TypeError, ValueError):
            return json.dumps(str(value), ensure_ascii=False)

    def to_markdown(self) -> str:
        """Render the current note as Markdown without assuming field shapes."""
        metadata = self.metadata
        markdown = [
            f"# Research Note - {self._text(metadata.get('timestamp'))}",
            "",
            f"**Git Commit**: `{self._text(metadata.get('git_commit'))}`",
            f"**Backtest ID**: `{self._text(metadata.get('backtest_id'))}`",
            f"**Strategy Version**: `{self._text(metadata.get('strategy_version'))}`",
            "",
            "## 1. Hypothesis",
            self._text(metadata.get("hypothesis")) or "Not recorded",
            "",
            "## 2. Experiment Design",
            self._text(metadata.get("experiment_design")) or "Not recorded",
            "",
            "## 3. Parameters",
            "```json",
            self._parameters_json(metadata.get("parameters")),
            "```",
            "",
            "## 4. Key Findings",
        ]
        findings = self._lines(metadata.get("key_findings"))
        markdown.extend([f"- {item}" for item in findings] or ["Not recorded"])
        markdown.extend(["", "## 5. Learnings and Next Steps", "### Learnings"])
        learnings = self._lines(metadata.get("learnings"))
        markdown.extend([f"- {item}" for item in learnings] or ["Not recorded"])
        markdown.extend(["", "### Next Steps"])
        next_steps = self._lines(metadata.get("next_steps"))
        markdown.extend([f"- {item}" for item in next_steps] or ["Not recorded"])
        return "\n".join(markdown) + "\n"

    def save_markdown(self, filename: str | Path | None = None) -> str:
        """Save the note as UTF-8 Markdown and return its path."""
        file_path = self._path_for(filename, ".md")
        file_path.write_text(self.to_markdown(), encoding="utf-8")
        logger.info("Research note Markdown saved: %s", file_path)
        return str(file_path)
