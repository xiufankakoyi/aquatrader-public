from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.research_graph.models import utc_now


@dataclass
class ResearchRunStep:
    name: str
    status: str = "pending"
    started_at: str = ""
    finished_at: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchRunManager:
    """Checkpoint and progress manager for real research actions."""

    def __init__(self, run_id: str, checkpoint_path: Path | None = None) -> None:
        self.run_id = run_id
        self.checkpoint_path = checkpoint_path
        self.steps: list[ResearchRunStep] = []
        self._handlers: dict[str, Callable[[], dict[str, Any]]] = {}

    def register_step(self, name: str, handler: Callable[[], dict[str, Any]]) -> None:
        if name not in {step.name for step in self.steps}:
            self.steps.append(ResearchRunStep(name=name))
        self._handlers[name] = handler

    def run(self, stop_after: str | None = None) -> dict[str, Any]:
        for step in self.steps:
            if step.status == "completed":
                continue
            handler = self._handlers.get(step.name)
            if handler is None:
                step.status = "skipped"
                step.error = "missing_handler"
                self._save()
                continue
            step.status = "running"
            step.started_at = step.started_at or utc_now()
            try:
                step.output = handler() or {}
                step.status = "completed"
                step.finished_at = utc_now()
            except Exception as exc:
                step.status = "failed"
                step.error = str(exc)
                step.finished_at = utc_now()
                self._save()
                raise
            self._save()
            if stop_after and step.name == stop_after:
                break
        return self.state()

    def state(self) -> dict[str, Any]:
        completed = sum(1 for step in self.steps if step.status == "completed")
        total = len(self.steps)
        return {
            "run_id": self.run_id,
            "progress": round(completed / total, 4) if total else 0.0,
            "completed_steps": completed,
            "total_steps": total,
            "steps": [step.to_dict() for step in self.steps],
        }

    def _save(self) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(
            json.dumps(self.state(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(
        cls,
        checkpoint_path: Path,
        handlers: dict[str, Callable[[], dict[str, Any]]] | None = None,
    ) -> "ResearchRunManager":
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        manager = cls(run_id=payload["run_id"], checkpoint_path=checkpoint_path)
        manager.steps = [
            ResearchRunStep(**item)
            for item in payload.get("steps", [])
        ]
        for name, handler in (handlers or {}).items():
            manager._handlers[name] = handler
        return manager
