"""P2.8 组合快照只读适配器。

按 P2 计划 §281-299 Task P2.8：
- 修改 core/quant/portfolio/ 中一个只读适配文件
- 从 PositionManager 拉取持仓数据，构造 PortfolioSnapshot
- 不修改 PositionManager 任何方法（保持职责边界）
- 不输出仓位建议，仅用于风险敞口分析

设计要点：
- PortfolioSnapshotAdapter 注入 PositionManager 实例
- 调用 get_all_positions() + calculate_analysis() 拼装只读快照
- 调用 calculate_analysis 前深拷贝 positions，规避其原地副作用
- calculate_analysis 在无 price_data 时优雅降级（仅返回基础持仓）
"""
from __future__ import annotations

import copy
from typing import Any

from core.research.risk import (
    Holding,
    PortfolioSnapshot,
    compute_concentration_hhi,
    compute_industry_exposure,
    utc_now,
)


class PortfolioSnapshotAdapter:
    """组合快照只读适配器。

    从 PositionManager 适配出只读 PortfolioSnapshot。

    Attributes:
        position_manager: PositionManager 实例（注入）
    """

    def __init__(self, position_manager: Any | None = None) -> None:
        """初始化适配器。

        Args:
            position_manager: PositionManager 实例（可延迟注入）
        """
        self._position_manager = position_manager

    def set_position_manager(self, position_manager: Any) -> None:
        """设置 PositionManager 实例。"""
        self._position_manager = position_manager

    def build_snapshot(
        self,
        *,
        as_of: str = "",
        active_only: bool = True,
    ) -> PortfolioSnapshot:
        """构建只读组合快照。

        Args:
            as_of: PIT 时点（默认取当前时间）
            active_only: 仅包含有效持仓

        Returns:
            PortfolioSnapshot

        Raises:
            RuntimeError: PositionManager 未注入
        """
        if self._position_manager is None:
            raise RuntimeError("PositionManager 未注入，请先调用 set_position_manager()")

        if not as_of:
            as_of = utc_now()

        # 拉取持仓
        positions = self._position_manager.get_all_positions(active_only=active_only)
        if not positions:
            return PortfolioSnapshot(as_of=as_of, holdings=[], total_market_value=0.0)

        # 深拷贝 positions，避免 calculate_analysis 原地副作用
        positions_copy = copy.deepcopy(positions)

        # 尝试调用 calculate_analysis 拉取权重
        # 若失败（如无 price_data），降级为均等权重
        holdings: list[Holding] = []
        try:
            analysis = self._position_manager.calculate_analysis(positions_copy, None)
            total_value = float(analysis.get("summary", {}).get("total_market_value", 0.0))
            for p in positions_copy:
                weight = float(getattr(p, "weight", 0.0)) / 100.0  # 百分比 → 0-1
                holdings.append(Holding(
                    stock_code=str(getattr(p, "stock_code", "")),
                    stock_name=str(getattr(p, "stock_name", "")),
                    weight=weight,
                    industry=str(getattr(p, "industry", "")),
                    market_value=float(getattr(p, "market_value", 0.0)),
                ))
        except Exception:
            # 降级：均等权重
            n = len(positions)
            equal_weight = 1.0 / n if n > 0 else 0.0
            for p in positions:
                holdings.append(Holding(
                    stock_code=str(getattr(p, "stock_code", "")),
                    stock_name=str(getattr(p, "stock_name", "")),
                    weight=equal_weight,
                    industry=str(getattr(p, "industry", "")),
                    market_value=0.0,
                ))
            total_value = 0.0

        # 计算行业暴露
        holdings_weights = {h.stock_code: h.weight for h in holdings}
        holdings_industries = {h.stock_code: h.industry for h in holdings}
        industry_exposure = compute_industry_exposure(holdings_weights, holdings_industries)

        # 计算集中度 HHI
        weights = [h.weight for h in holdings]
        concentration_hhi = compute_concentration_hhi(weights)

        # Top 10 持仓
        top_holdings = sorted(holdings, key=lambda h: h.weight, reverse=True)[:10]

        return PortfolioSnapshot(
            as_of=as_of,
            holdings=holdings,
            total_market_value=total_value,
            industry_exposure=industry_exposure,
            concentration_hhi=concentration_hhi,
            top_holdings=top_holdings,
        )

    def build_snapshot_from_holdings(
        self,
        *,
        holdings: list[Holding],
        as_of: str = "",
    ) -> PortfolioSnapshot:
        """从显式 holdings 构建只读快照（无需 PositionManager）。

        用于测试或自定义场景。

        Args:
            holdings: 持仓列表
            as_of: PIT 时点

        Returns:
            PortfolioSnapshot
        """
        if not as_of:
            as_of = utc_now()

        holdings_weights = {h.stock_code: h.weight for h in holdings}
        holdings_industries = {h.stock_code: h.industry for h in holdings}
        industry_exposure = compute_industry_exposure(holdings_weights, holdings_industries)
        weights = [h.weight for h in holdings]
        concentration_hhi = compute_concentration_hhi(weights)
        top_holdings = sorted(holdings, key=lambda h: h.weight, reverse=True)[:10]
        total_value = sum(h.market_value for h in holdings)

        return PortfolioSnapshot(
            as_of=as_of,
            holdings=list(holdings),
            total_market_value=total_value,
            industry_exposure=industry_exposure,
            concentration_hhi=concentration_hhi,
            top_holdings=top_holdings,
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_portfolio_snapshot_adapter(
    position_manager: Any | None = None,
) -> PortfolioSnapshotAdapter:
    """构造默认 PortfolioSnapshotAdapter。

    Args:
        position_manager: 可选的 PositionManager 实例

    Returns:
        PortfolioSnapshotAdapter
    """
    return PortfolioSnapshotAdapter(position_manager=position_manager)


__all__ = [
    "PortfolioSnapshotAdapter",
    "build_portfolio_snapshot_adapter",
]
