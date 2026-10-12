"""Local-only mock broker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .base import BrokerBridge


class MockBrokerBridge(BrokerBridge):
    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.orders: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.cash = 1_000_000.0

    def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)

    def get_cash(self) -> float:
        return self.cash

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        record = {
            **order,
            "order_id": f"mock-{uuid4().hex[:12]}",
            "status": "recorded",
            "dry_run": True,
            "source": "mock_broker",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.orders.append(record)
        self._write_log()
        return record

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        for order in self.orders:
            if order["order_id"] == order_id:
                order["status"] = "cancelled"
                self._write_log()
                return order
        return {
            "order_id": order_id,
            "status": "not_found",
            "dry_run": True,
            "source": "mock_broker",
        }

    def get_orders(self) -> list[dict[str, Any]]:
        return list(self.orders)

    def get_trades(self) -> list[dict[str, Any]]:
        return list(self.trades)

    def _write_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "dry_run": True,
            "real_broker_connected": False,
            "source": "mock_broker",
            "orders": self.orders,
            "trades": self.trades,
        }
        self.log_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
