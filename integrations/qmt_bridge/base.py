"""Broker bridge contract.

Real QMT/QNT connectivity is intentionally not implemented in this project.
"""

from __future__ import annotations

from abc import ABC
from typing import Any


class BrokerBridge(ABC):
    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError("真实 broker adapter 未启用")

    def get_cash(self) -> float:
        raise NotImplementedError("真实 broker adapter 未启用")

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("真实下单被禁止")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError("真实撤单被禁止")

    def get_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError("真实 broker adapter 未启用")

    def get_trades(self) -> list[dict[str, Any]]:
        raise NotImplementedError("真实 broker adapter 未启用")
