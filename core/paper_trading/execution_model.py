"""订单执行模型。

将买卖执行逻辑从 Portfolio.rebalance 中解耦，支持不同的执行策略
（如 TWAP、VWAP、限价等）。DefaultExecutionModel 严格复刻原 rebalance
的执行时序与字段处理，保证重构后行为不变。

执行时序（与原 rebalance 一致）：
1. 卖出不在 targets 中的持仓（调出）
2. 计算总市值（在调出之后，含手续费/税后净值）
3. 通过 constructor 构造目标权重
4. 卖出超配（减仓到 target_qty）
5. 买入低配（含现金不足按手递减、成交量上限 max_quantity_by_code）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from core.research_graph.models import utc_now

if TYPE_CHECKING:
    from core.paper_trading.portfolio import Portfolio
    from core.paper_trading.portfolio_constructor import PortfolioConstructor


class ExecutionModel(ABC):
    """订单执行模型抽象基类。

    负责根据目标持仓和当前持仓执行调仓操作，返回与原 rebalance
    一致的 {"buys", "sells", "skipped"} 结构。
    """

    @abstractmethod
    def execute(
        self,
        portfolio: "Portfolio",
        targets: list[dict[str, Any]],
        prices: dict[str, float],
        max_quantity_by_code: dict[str, int],
        constructor: "PortfolioConstructor",
        *,
        trade_date: str | None = None,
        reason_template: str = "投研选股再平衡",
    ) -> dict[str, Any]:
        """执行调仓。

        Args:
            portfolio: 当前持仓组合。
            targets: 目标股票列表（dict，含 code/name/score 等键）。
            prices: {code: price} 当前价格。
            max_quantity_by_code: {code: max_qty} 成交量上限。
            constructor: 目标权重构造器，用于将 targets 转为目标价值。
            trade_date: 交易日期字符串，默认当天。
            reason_template: 成交记录原因前缀。

        Returns:
            {"buys": [...], "sells": [...], "skipped": [...]} 成交与跳过记录。
        """
        ...


class DefaultExecutionModel(ExecutionModel):
    """默认执行模型。

    从 Portfolio.rebalance 原样抽取的逻辑，保证重构后行为不变：
    1. 卖出不在 targets 中的持仓（调出）
    2. 卖出超配（减仓到 target_qty）
    3. 买入低配（含现金不足时按手递减、成交量上限）
    """

    def execute(
        self,
        portfolio: "Portfolio",
        targets: list[dict[str, Any]],
        prices: dict[str, float],
        max_quantity_by_code: dict[str, int],
        constructor: "PortfolioConstructor",
        *,
        trade_date: str | None = None,
        reason_template: str = "投研选股再平衡",
    ) -> dict[str, Any]:
        """执行默认调仓逻辑。

        Args:
            portfolio: 当前持仓组合。
            targets: 目标股票列表（dict，含 code/name/score 等键）。
            prices: 当前价格字典。
            max_quantity_by_code: 成交量上限字典。
            constructor: 目标权重构造器。
            trade_date: 交易日期字符串。
            reason_template: 成交记录原因前缀。

        Returns:
            {"buys": [...], "sells": [...], "skipped": [...]} 成交与跳过记录。
        """
        date = trade_date or utc_now()[:10]
        target_codes = {str(t["code"]).strip()[-6:] for t in targets}

        # 1. 卖出不在 targets 中的持仓（调出）。
        sells: list[dict[str, Any]] = []
        for code in list(portfolio.positions.keys()):
            if code in target_codes:
                continue
            price = prices.get(code)
            if price is None or price <= 0:
                continue
            pos_name = portfolio.positions[code].name
            qty = portfolio.sell(
                code, price,
                reason=f"{reason_template}: 调出 {code}",
                trade_date=date,
            )
            if qty > 0:
                sells.append({"code": code, "name": pos_name, "quantity": qty, "price": price})

        # 无目标时只完成调出，直接返回。
        if not targets:
            return {"buys": [], "sells": sells, "skipped": []}

        # 2. 计算总市值（在调出之后，与原 rebalance 时序一致，含手续费/税后净值）。
        total_value = portfolio.cash + sum(
            portfolio.positions[c].quantity * prices.get(c, portfolio.positions[c].avg_cost)
            for c in portfolio.positions
        )

        # 3. 通过 constructor 构造目标权重。
        target_values = constructor.construct(targets, total_value)

        buys: list[dict[str, Any]] = []
        skipped: list[str] = []

        # 4. 第一轮：卖出超配（减仓到 target_qty）。
        for target in targets:
            code = str(target["code"]).strip()[-6:]
            price = prices.get(code)
            if price is None or price <= 0:
                skipped.append(code)
                continue
            current_qty = (
                portfolio.positions[code].quantity
                if code in portfolio.positions
                else 0
            )
            target_qty = (int(target_values.get(code, 0.0) / price) // portfolio.LOT_SIZE) * portfolio.LOT_SIZE
            if current_qty > target_qty:
                excess = current_qty - target_qty
                pos_name = portfolio.positions[code].name
                sold = portfolio.sell(
                    code, price,
                    quantity=excess,
                    reason=f"{reason_template}: 减仓 {code}",
                    trade_date=date,
                )
                if sold > 0:
                    sells.append({"code": code, "name": pos_name, "quantity": sold, "price": price})

        # 5. 第二轮：买入低配（含现金不足按手递减、成交量上限）。
        for target in targets:
            code = str(target["code"]).strip()[-6:]
            name = target.get("name", "")
            price = prices.get(code)
            if price is None or price <= 0:
                skipped.append(code)
                continue
            current_qty = (
                portfolio.positions[code].quantity
                if code in portfolio.positions
                else 0
            )
            target_qty = (int(target_values.get(code, 0.0) / price) // portfolio.LOT_SIZE) * portfolio.LOT_SIZE
            if target_qty <= current_qty:
                continue
            quantity = target_qty - current_qty
            amount = quantity * price
            commission = max(amount * portfolio.COMMISSION_RATE, portfolio.MIN_COMMISSION)
            total_cost = amount + commission
            if total_cost > portfolio.cash:
                # 现金不足时按手递减，直到可负担。
                while quantity > 0 and total_cost > portfolio.cash:
                    quantity -= portfolio.LOT_SIZE
                    amount = quantity * price
                    commission = max(amount * portfolio.COMMISSION_RATE, portfolio.MIN_COMMISSION)
                    total_cost = amount + commission
                if quantity <= 0:
                    skipped.append(code)
                    continue

            snapshot = {
                "score": target.get("score"),
                "node_id": target.get("node_id"),
                "node_type": target.get("node_type"),
                "status": target.get("status"),
            }
            volume_limit = max_quantity_by_code.get(code)
            max_quantity = min(quantity, volume_limit) if volume_limit is not None else quantity
            bought = portfolio.buy(
                code, name, price,
                trade_date=date,
                reason=f"{reason_template}: 买入 {code}",
                research_snapshot=snapshot,
                max_quantity=max_quantity,
            )
            if bought > 0:
                buys.append({
                    "code": code,
                    "name": name,
                    "quantity": bought,
                    "price": price,
                    "score": target.get("score"),
                })
            else:
                skipped.append(code)

        return {"buys": buys, "sells": sells, "skipped": skipped}
