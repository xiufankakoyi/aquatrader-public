"""Reuse backtest execution configuration and market constraints in paper trading.

P0-6 接入：内部委托 SharedExecutionPolicy，补齐卖出 volume_cap 和 ST 过滤，
保持 ExecutionDecision 接口不变，向后兼容 orchestrator.py 调用方。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.quant.backtest.models import BacktestConfig
from core.quant.execution_policy import (
    ExecutionPolicy,
    FillDecision,
    SharedExecutionPolicy,
)


@dataclass
class ExecutionDecision:
    """模拟盘成交决策（向后兼容接口）。

    Attributes:
        allowed: 是否允许成交。
        price: 成交价（含滑点）。
        max_quantity: 最大可成交数量。
        rejection_reason: 拒绝原因字符串。
        commission: 佣金（P0-6 新增，可选）。
        impact_cost: 冲击成本（P0-6 新增，可选）。
    """

    allowed: bool
    price: float = 0.0
    max_quantity: int = 0
    rejection_reason: str = ""
    commission: float = 0.0
    impact_cost: float = 0.0


class BacktestExecutionPolicyAdapter:
    """Apply the same BacktestConfig semantics via SharedExecutionPolicy.

    P0-6：改为内部委托 SharedExecutionPolicy，统一成交规则源：
    - 补齐卖出 volume_cap 检查（旧版仅买入检查）
    - 补齐 ST 过滤（旧版完全缺失）
    - 补齐冲击成本计算（可选，默认 0）

    保持 decide() 接口不变，orchestrator.py 调用方无需修改。
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        """初始化适配器。

        Args:
            config: 回测配置；None 时用默认 BacktestConfig。
        """
        self.config = config or BacktestConfig()
        # 构造共享成交策略实例
        self._policy = ExecutionPolicy.from_backtest_config(self.config)
        self._shared = SharedExecutionPolicy(self._policy)

    def decide(
        self,
        bar: Any,
        *,
        side: str,
        average_volume: float = 0.0,
        quantity: int = 0,
        cash_available: float = 0.0,
    ) -> ExecutionDecision:
        """评估单笔成交。

        P0-6：委托 SharedExecutionPolicy.evaluate()，补齐 ST/卖出 volume_cap/冲击成本。

        Args:
            bar: 行情数据（需有 open/close/volume/is_st/is_suspended/is_limit_up/is_limit_down）。
            side: "buy" 或 "sell"。
            average_volume: 平均日成交量（用于容量约束和冲击成本）。
            quantity: 请求成交数量（0 表示未知，仅计算 max_quantity）。
            cash_available: 可用资金（买入时检查）。

        Returns:
            ExecutionDecision 决策结果。
        """
        fill = self._shared.evaluate(
            bar,
            side=side,
            quantity=quantity,
            average_volume=average_volume,
            cash_available=cash_available,
        )
        return ExecutionDecision(
            allowed=fill.allowed,
            price=fill.fill_price,
            max_quantity=fill.max_quantity,
            rejection_reason=fill.rejection_reason,
            commission=fill.commission,
            impact_cost=fill.impact_cost,
        )
