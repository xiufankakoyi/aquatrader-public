"""共享成交策略：ExecutionPolicy / FillDecision。

从现有 BacktestConfig + BacktestExecutionPolicyAdapter 抽取统一抽象，
供回测引擎和模拟盘共享，不增加第三套成交模型。

修复的问题：
1. 卖出容量约束缺失：旧实现卖出时不检查 volume_cap_ratio
2. ST 过滤缺失：模拟盘 DailyBar 无 is_st 字段，adapter 也不检查
3. 冲击成本：新增可选的市场冲击成本模型

设计原则：
- 回测和模拟盘共享同一套 ExecutionPolicy/FillDecision
- 不修改现有 BacktestConfig，通过组合而非继承扩展
- 缺字段安全降级
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RejectReason(str, Enum):
    """成交拒绝原因。"""

    SUSPENDED = "suspended"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    ST_FILTERED = "st_filtered"
    INVALID_PRICE = "invalid_price"
    VOLUME_CAP = "volume_cap"
    INSUFFICIENT_CASH = "insufficient_cash"
    MIN_TRADE_AMOUNT = "min_trade_amount"


@dataclass
class ExecutionPolicy:
    """统一成交策略配置。

    从 BacktestConfig 抽取成交相关参数，增加卖出容量约束和 ST 过滤。
    回测引擎和模拟盘共享同一实例。

    Attributes:
        slippage_rate: 滑点比例（单边）。
        commission_rate: 佣金费率。
        min_commission: 最小佣金（元）。
        sell_tax: 印花税率（卖出）。
        volume_cap_ratio: 单笔成交不超过日成交量的比例。
        exclude_st: 是否过滤 ST 股票。
        min_trade_amount: 最小可交易金额（元）。
        impact_cost_rate: 市场冲击成本率（可选，默认 0）。
    """

    slippage_rate: float = 0.001
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    sell_tax: float = 0.001
    volume_cap_ratio: float = 0.05
    exclude_st: bool = True
    min_trade_amount: float = 1000.0
    impact_cost_rate: float = 0.0  # 默认无冲击成本

    @classmethod
    def from_backtest_config(cls, config: Any) -> "ExecutionPolicy":
        """从 BacktestConfig 构造 ExecutionPolicy。

        Args:
            config: BacktestConfig 实例。

        Returns:
            ExecutionPolicy 实例。
        """
        return cls(
            slippage_rate=float(getattr(config, "slippage_rate", 0.001)),
            commission_rate=float(getattr(config, "commission_rate", 0.0003)),
            min_commission=float(getattr(config, "min_commission", 5.0)),
            sell_tax=float(getattr(config, "sell_tax", 0.001)),
            volume_cap_ratio=float(getattr(config, "volume_cap_ratio", 0.05)),
            exclude_st=bool(getattr(config, "exclude_st", True)),
            min_trade_amount=float(getattr(config, "min_trade_amount", 1000.0)),
            impact_cost_rate=0.0,
        )

    def compute_slippage(self, price: float, side: str) -> float:
        """计算滑点后的成交价。

        Args:
            price: 原始价格。
            side: "buy" 或 "sell"。

        Returns:
            滑点后价格。
        """
        direction = 1.0 if side == "buy" else -1.0
        return price * (1.0 + direction * self.slippage_rate)

    def compute_impact_cost(self, quantity: int, price: float, avg_volume: float) -> float:
        """计算市场冲击成本。

        冲击成本 = quantity * price * impact_cost_rate * (quantity / avg_volume)

        Args:
            quantity: 成交数量。
            price: 成交价格。
            avg_volume: 平均日成交量。

        Returns:
            冲击成本金额。
        """
        if self.impact_cost_rate <= 0 or avg_volume <= 0:
            return 0.0
        participation_rate = min(quantity / avg_volume, 1.0)
        return quantity * price * self.impact_cost_rate * participation_rate

    def compute_commission(self, amount: float, side: str) -> float:
        """计算佣金。

        Args:
            amount: 成交金额。
            side: "buy" 或 "sell"。

        Returns:
            佣金金额。
        """
        commission = max(amount * self.commission_rate, self.min_commission)
        if side == "sell":
            commission += amount * self.sell_tax
        return commission


@dataclass
class FillDecision:
    """成交决策结果。

    由 ExecutionPolicy.evaluate() 产出，回测引擎和模拟盘共享。
    """

    allowed: bool = False
    fill_price: float = 0.0
    max_quantity: int = 0
    commission: float = 0.0
    impact_cost: float = 0.0
    rejection_reason: str = ""
    reject_reason_enum: RejectReason | None = None


class SharedExecutionPolicy:
    """共享成交策略评估器。

    回测引擎和模拟盘共用此类，确保成交行为一致。
    修复卖出容量约束缺失和 ST 过滤缺失问题。
    """

    def __init__(self, policy: ExecutionPolicy | None = None) -> None:
        """初始化共享成交策略。

        Args:
            policy: 成交策略配置；None 时使用默认值。
        """
        self.policy = policy or ExecutionPolicy()

    def evaluate(
        self,
        bar: Any,
        *,
        side: str,
        quantity: int = 0,
        average_volume: float = 0.0,
        cash_available: float = 0.0,
    ) -> FillDecision:
        """评估单笔成交是否允许，并计算成交参数。

        Args:
            bar: 行情数据（需有 open/close/volume/is_st/is_suspended/is_limit_up/is_limit_down 属性）。
            side: "buy" 或 "sell"。
            quantity: 请求成交数量（0 表示未知，仅计算 max_quantity）。
            average_volume: 平均日成交量（用于容量约束和冲击成本）。
            cash_available: 可用资金（买入时检查）。

        Returns:
            FillDecision 成交决策。
        """
        if bar is None:
            return FillDecision(rejection_reason="price_unavailable", reject_reason_enum=None)

        # 1. 停牌检查
        if bool(getattr(bar, "is_suspended", False)):
            return FillDecision(rejection_reason="suspended", reject_reason_enum=RejectReason.SUSPENDED)

        # 2. 涨跌停检查
        if side == "buy" and bool(getattr(bar, "is_limit_up", False)):
            return FillDecision(rejection_reason="limit_up", reject_reason_enum=RejectReason.LIMIT_UP)
        if side == "sell" and bool(getattr(bar, "is_limit_down", False)):
            return FillDecision(rejection_reason="limit_down", reject_reason_enum=RejectReason.LIMIT_DOWN)

        # 3. ST 过滤（买入时检查，卖出时也检查确保一致性）
        if self.policy.exclude_st and bool(getattr(bar, "is_st", False)):
            return FillDecision(rejection_reason="st_filtered", reject_reason_enum=RejectReason.ST_FILTERED)

        # 4. 价格有效性
        raw_price = float(getattr(bar, "open", 0.0) or getattr(bar, "close", 0.0) or 0.0)
        if raw_price <= 0:
            return FillDecision(rejection_reason="invalid_price", reject_reason_enum=RejectReason.INVALID_PRICE)

        # 5. 滑点
        fill_price = self.policy.compute_slippage(raw_price, side)

        # 6. 容量约束（买入和卖出都检查）
        volume = float(average_volume or getattr(bar, "volume", 0.0) or 0.0)
        max_quantity = max(0, int(volume * self.policy.volume_cap_ratio))

        # 7. 请求数量检查
        if quantity > 0 and max_quantity > 0 and quantity > max_quantity:
            # 超过容量上限，拒绝（而非截断，由调用方决定截断或拒绝）
            return FillDecision(
                fill_price=fill_price,
                max_quantity=max_quantity,
                rejection_reason="volume_cap",
                reject_reason_enum=RejectReason.VOLUME_CAP,
            )

        # 8. 买入资金检查
        actual_quantity = quantity if quantity > 0 else max_quantity
        if side == "buy" and actual_quantity > 0 and cash_available > 0:
            amount = actual_quantity * fill_price
            if amount > cash_available:
                return FillDecision(
                    fill_price=fill_price,
                    max_quantity=max_quantity,
                    rejection_reason="insufficient_cash",
                    reject_reason_enum=RejectReason.INSUFFICIENT_CASH,
                )

        # 9. 最小交易金额检查
        if side == "buy" and actual_quantity > 0:
            amount = actual_quantity * fill_price
            if amount < self.policy.min_trade_amount:
                return FillDecision(
                    fill_price=fill_price,
                    max_quantity=max_quantity,
                    rejection_reason="min_trade_amount",
                    reject_reason_enum=RejectReason.MIN_TRADE_AMOUNT,
                )

        # 10. 佣金和冲击成本
        amount = actual_quantity * fill_price
        commission = self.policy.compute_commission(amount, side)
        impact_cost = self.policy.compute_impact_cost(actual_quantity, fill_price, volume)

        return FillDecision(
            allowed=True,
            fill_price=fill_price,
            max_quantity=max_quantity,
            commission=commission,
            impact_cost=impact_cost,
        )
