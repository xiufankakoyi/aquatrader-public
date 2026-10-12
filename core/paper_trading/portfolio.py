"""In-memory + JSON persisted paper portfolio.

Tracks cash, positions, trade records and daily NAV. Each trade is recorded
with execution price, quantity, fees and a research snapshot so we can audit
why the trade happened.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.research_graph.models import utc_now

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """A single simulated trade."""

    date: str
    code: str
    name: str
    side: str  # "buy" or "sell"
    price: float
    quantity: int
    amount: float
    commission: float
    tax: float
    reason: str
    research_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "code": self.code,
            "name": self.name,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "amount": self.amount,
            "commission": self.commission,
            "tax": self.tax,
            "reason": self.reason,
            "research_snapshot": self.research_snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeRecord":
        return cls(
            date=data["date"],
            code=data["code"],
            name=data["name"],
            side=data["side"],
            price=float(data["price"]),
            quantity=int(data["quantity"]),
            amount=float(data["amount"]),
            commission=float(data.get("commission", 0)),
            tax=float(data.get("tax", 0)),
            reason=data.get("reason", ""),
            research_snapshot=data.get("research_snapshot", {}),
        )


@dataclass
class Position:
    """Current holding of a single stock."""

    code: str
    name: str
    quantity: int
    avg_cost: float
    total_cost: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "total_cost": self.total_cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(
            code=data["code"],
            name=data["name"],
            quantity=int(data["quantity"]),
            avg_cost=float(data["avg_cost"]),
            total_cost=float(data["total_cost"]),
        )


class Portfolio:
    """Paper trading portfolio with JSON persistence."""

    COMMISSION_RATE = 0.0003  # 0.03%
    TAX_RATE = 0.001  # 0.1% on sell only
    MIN_COMMISSION = 5.0
    LOT_SIZE = 100

    def __init__(
        self,
        initial_cash: float = 1_000_000.0,
        state_path: str | Path | None = None,
        max_position_pct: float = 0.20,
    ) -> None:
        """Initialize portfolio.

        Args:
            initial_cash: Starting cash in CNY.
            state_path: Path to JSON state file. Loads existing state if present.
            max_position_pct: Max single-position weight at entry (0.2 = 20%).
        """
        self.initial_cash = initial_cash
        self.state_path = Path(state_path) if state_path else None
        self.max_position_pct = max_position_pct

        self.cash: float = initial_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []
        self.nav_history: list[dict[str, Any]] = []

        if self.state_path and self.state_path.exists():
            self.load()

    def buy(
        self,
        code: str,
        name: str,
        price: float,
        trade_date: str | None = None,
        reason: str = "",
        research_snapshot: dict[str, Any] | None = None,
        max_quantity: int | None = None,
    ) -> int:
        """Buy a stock at the given price, respecting position limits.

        Args:
            code: Normalized 6-digit A-share code.
            name: Stock name for display.
            price: Execution price.
            trade_date: Date string, defaults to today.
            reason: Human-readable trade reason.
            research_snapshot: Optional research metadata for audit.

        Returns:
            Number of shares bought (0 if skipped).
        """
        code = str(code).strip()[-6:]
        if price <= 0 or self.cash <= 0:
            return 0

        date = trade_date or utc_now()[:10]
        max_amount = min(self.cash, self.initial_cash * self.max_position_pct)
        if code in self.positions:
            # do not add to an existing position if already at limit
            existing_value = self.positions[code].quantity * price
            max_amount = min(max_amount, self.initial_cash * self.max_position_pct - existing_value)
            if max_amount <= 0:
                logger.info("Skip %s: already at position limit", code)
                return 0

        raw_quantity = int(max_amount / price)
        quantity = (raw_quantity // self.LOT_SIZE) * self.LOT_SIZE
        if max_quantity is not None:
            quantity = min(quantity, (max(0, int(max_quantity)) // self.LOT_SIZE) * self.LOT_SIZE)
        if quantity <= 0:
            logger.info("Skip %s: not enough cash for one lot", code)
            return 0

        amount = quantity * price
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        total_cost = amount + commission
        if total_cost > self.cash:
            # reduce by one lot and retry
            quantity -= self.LOT_SIZE
            if quantity <= 0:
                return 0
            amount = quantity * price
            commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
            total_cost = amount + commission

        self.cash -= total_cost
        if code in self.positions:
            pos = self.positions[code]
            new_total_cost = pos.total_cost + total_cost
            new_quantity = pos.quantity + quantity
            pos.avg_cost = new_total_cost / new_quantity
            pos.quantity = new_quantity
            pos.total_cost = new_total_cost
        else:
            self.positions[code] = Position(
                code=code,
                name=name,
                quantity=quantity,
                avg_cost=total_cost / quantity,
                total_cost=total_cost,
            )

        trade = TradeRecord(
            date=date,
            code=code,
            name=name,
            side="buy",
            price=price,
            quantity=quantity,
            amount=amount,
            commission=commission,
            tax=0.0,
            reason=reason,
            research_snapshot=research_snapshot or {},
        )
        self.trades.append(trade)
        self.save()
        logger.info("BUY %s %s @ %.2f qty=%d cost=%.2f", date, code, price, quantity, total_cost)
        return quantity

    def sell(
        self,
        code: str,
        price: float,
        quantity: int | None = None,
        trade_date: str | None = None,
        reason: str = "",
    ) -> int:
        """Sell a stock at the given price."""
        code = str(code).strip()[-6:]
        pos = self.positions.get(code)
        if pos is None or pos.quantity <= 0:
            return 0

        sell_qty = quantity if quantity is not None else pos.quantity
        sell_qty = (sell_qty // self.LOT_SIZE) * self.LOT_SIZE
        sell_qty = min(sell_qty, pos.quantity)
        if sell_qty <= 0:
            return 0

        amount = sell_qty * price
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        tax = amount * self.TAX_RATE
        net_proceeds = amount - commission - tax

        self.cash += net_proceeds
        pos.quantity -= sell_qty
        pos.total_cost = pos.avg_cost * pos.quantity
        if pos.quantity <= 0:
            del self.positions[code]

        trade = TradeRecord(
            date=trade_date or utc_now()[:10],
            code=code,
            name=pos.name,
            side="sell",
            price=price,
            quantity=sell_qty,
            amount=amount,
            commission=commission,
            tax=tax,
            reason=reason,
        )
        self.trades.append(trade)
        self.save()
        return sell_qty

    def current_nav(self, prices: dict[str, float]) -> dict[str, Any]:
        """Calculate current NAV using provided market prices."""
        position_value = 0.0
        position_details: list[dict[str, Any]] = []
        for code, pos in self.positions.items():
            price = prices.get(code, pos.avg_cost)
            value = pos.quantity * price
            position_value += value
            position_details.append({
                "code": code,
                "name": pos.name,
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "price": price,
                "value": value,
                "pnl": value - pos.total_cost,
            })

        total = self.cash + position_value
        nav = {
            "date": utc_now()[:10],
            "cash": self.cash,
            "position_value": position_value,
            "total_value": total,
            "positions": position_details,
        }
        self.nav_history.append(nav)
        return nav

    def rebalance(
        self,
        targets: list[dict[str, Any]],
        prices: dict[str, float],
        trade_date: str | None = None,
        reason_template: str = "投研选股再平衡",
        max_quantity_by_code: dict[str, int] | None = None,
        *,
        constructor: Any | None = None,
        execution_model: Any | None = None,
    ) -> dict[str, Any]:
        """Rebalance portfolio to match a new target stock list.

        Sells positions no longer in the target list, then buys/sells each
        target stock to reach the allocation produced by ``constructor``
        (equal-weight by default, preserving historical behavior).

        权重构造与订单执行已解耦到 ``constructor`` / ``execution_model``，
        二者为 None 时回退到默认实现，保证向后兼容。

        Args:
            targets: List of dicts with keys 'code', 'name', 'score'.
            prices: Dict mapping code -> execution price.
            trade_date: Trade date string; defaults to today.
            reason_template: Prefix used in trade records.
            max_quantity_by_code: Per-code volume cap applied to buys.
            constructor: Optional target-weight constructor. Defaults to
                EqualWeightConstructor for backward compatibility.
            execution_model: Optional execution model. Defaults to
                DefaultExecutionModel for backward compatibility.

        Returns:
            Dict with executed_buys, executed_sells and skipped codes.
        """
        # 延迟导入避免循环依赖：execution_model 在 TYPE_CHECKING 下引用 Portfolio。
        if constructor is None:
            from core.paper_trading.portfolio_constructor import EqualWeightConstructor
            constructor = EqualWeightConstructor()
        if execution_model is None:
            from core.paper_trading.execution_model import DefaultExecutionModel
            execution_model = DefaultExecutionModel()
        return execution_model.execute(
            portfolio=self,
            targets=targets,
            prices=prices,
            max_quantity_by_code=max_quantity_by_code or {},
            constructor=constructor,
            trade_date=trade_date,
            reason_template=reason_template,
        )

    def save(self) -> None:
        """Persist portfolio state to JSON."""
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "max_position_pct": self.max_position_pct,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "trades": [t.to_dict() for t in self.trades],
            "nav_history": self.nav_history,
            "updated_at": utc_now(),
        }
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> None:
        """Load portfolio state from JSON."""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.initial_cash = float(data.get("initial_cash", self.initial_cash))
            self.cash = float(data.get("cash", self.initial_cash))
            self.max_position_pct = float(data.get("max_position_pct", self.max_position_pct))
            self.positions = {
                k: Position.from_dict(v) for k, v in data.get("positions", {}).items()
            }
            self.trades = [TradeRecord.from_dict(t) for t in data.get("trades", [])]
            self.nav_history = data.get("nav_history", [])
        except Exception as exc:
            logger.warning("Failed to load portfolio state: %s", exc)
