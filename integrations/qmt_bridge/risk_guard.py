"""Fail-closed pre-trade checks for dry-run plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .trade_plan import TradePlan


@dataclass
class RiskLimits:
    max_single_position_ratio: float = 0.20
    max_total_position_ratio: float = 0.80


class RiskGuard:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self, plan: TradePlan, context: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        checks: list[dict[str, Any]] = []

        def add(name: str, passed: bool, reason: str) -> None:
            checks.append({"check": name, "passed": passed, "reason": reason})

        required = {
            "single_position_ratio",
            "total_position_ratio",
            "is_st",
            "is_suspended",
            "is_limit_up",
            "is_limit_down",
            "data_complete",
            "is_trading_day",
        }
        missing = sorted(required - set(context))
        add("required_data", not missing, "字段完整" if not missing else f"缺少字段: {', '.join(missing)}")
        if missing:
            plan.risk_checks = checks
            plan.status = "rejected"
            return False, checks

        add(
            "single_position_limit",
            float(context["single_position_ratio"]) <= self.limits.max_single_position_ratio,
            "单标的占比检查",
        )
        add(
            "total_position_limit",
            float(context["total_position_ratio"]) <= self.limits.max_total_position_ratio,
            "总占比检查",
        )
        add("st_block", not bool(context["is_st"]), "ST 标记检查")
        add("suspension_block", not bool(context["is_suspended"]), "停牌检查")
        add(
            "limit_up_block",
            not (plan.side.lower() == "buy" and bool(context["is_limit_up"])),
            "涨停方向检查",
        )
        add(
            "limit_down_block",
            not (plan.side.lower() == "sell" and bool(context["is_limit_down"])),
            "跌停方向检查",
        )
        add("data_complete", bool(context["data_complete"]), "行情与风控数据完整性")
        add("trading_day", bool(context["is_trading_day"]), "交易日检查")

        approved = all(item["passed"] for item in checks)
        plan.risk_checks = checks
        plan.status = "approved" if approved else "rejected"
        return approved, checks
