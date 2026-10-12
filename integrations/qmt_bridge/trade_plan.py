"""Structured signal-to-plan representation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TradePlan:
    symbol: str
    side: str
    quantity: int
    price_type: str
    reason: str
    strategy_id: str
    risk_checks: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
