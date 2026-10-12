from __future__ import annotations

from itertools import count
from typing import Any

from core.research_graph.models import MutationLog


class MutationLogger:
    def __init__(self) -> None:
        self._counter = count(1)
        self.items: list[MutationLog] = []

    def record(
        self,
        action: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        reason: str,
        operator: str = "human",
    ) -> MutationLog:
        mutation = MutationLog(
            mutation_id=f"mut_{next(self._counter):06d}",
            action=action,
            target_id=target_id,
            before=before or {},
            after=after or {},
            reason=reason,
            operator=operator,
        )
        self.items.append(mutation)
        return mutation

    def for_target(self, target_id: str) -> list[MutationLog]:
        return [item for item in self.items if item.target_id == target_id]

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items]

    @classmethod
    def from_list(cls, payload: list[dict[str, Any]]) -> "MutationLogger":
        logger = cls()
        logger.items = [MutationLog.from_dict(item) for item in payload]
        logger._counter = count(len(logger.items) + 1)
        return logger
