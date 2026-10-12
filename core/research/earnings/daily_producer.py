"""每日盈利驱动生产入口。

主题成员资格、市场角色和技术特征都不是盈利披露。真实盈利驱动证据接入前，
该入口保持显式降级，不能为了填充表而生成代理记录。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .repository import EarningsRepository


def produce_daily_earnings_drivers(
    trade_date: str,
    *,
    research_repo: Any | None = None,
    earnings_repo: EarningsRepository | None = None,
    period: str = "",
) -> dict[str, Any]:
    """缺少真实盈利证据时不写入 driver 或 assumption。"""
    del trade_date, research_repo, earnings_repo, period
    return {
        "status": "skipped",
        "driver_count": 0,
        "assumption_count": 0,
        "reason": "missing_real_earnings_driver_evidence",
    }
