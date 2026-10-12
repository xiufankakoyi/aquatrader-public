from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any


def _period(record: dict[str, Any]) -> str:
    if not isinstance(record, Mapping):
        return ""
    return "".join(
        character for character in str(record.get("period") or record.get("period_end") or "")
        if character.isdigit()
    )


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def build_profit_bridge(
    financial_history: list[dict[str, Any]],
    earnings_forecasts: list[dict[str, Any]],
    observed_operating_facts: list[dict[str, Any]] | None = None,
    forward_driver_assumptions: list[dict[str, Any]] | None = None,
    structured_consensus: dict[str, Any] | None = None,
    forward_disclosed_indicators: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build only observed profit and first-party guidance; never invent drivers."""
    financial_rows = [dict(record) for record in financial_history if isinstance(record, Mapping)]
    forecast_rows = [dict(record) for record in earnings_forecasts if isinstance(record, Mapping)]
    observed_rows = [
        dict(record) for record in (observed_operating_facts or []) if isinstance(record, Mapping)
    ]
    forward_driver_rows = [
        dict(record) for record in (forward_driver_assumptions or []) if isinstance(record, Mapping)
    ]
    annual = sorted(
        (record for record in financial_rows if _period(record).endswith("1231")),
        key=_period,
    )
    latest_annual = annual[-1] if annual else None
    latest_period = _period(latest_annual or {})
    baseline_profit = _number((latest_annual or {}).get("net_profit"))
    baseline_revenue = _number((latest_annual or {}).get("revenue"))
    forward: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for record in sorted(forecast_rows, key=_period):
        target_period = _period(record)
        item = {
            "period": target_period,
            "announced_at": record.get("announcement_date", ""),
            "net_profit_min": None,
            "net_profit_max": None,
            "forecast_type": record.get("forecast_type", ""),
            "change_reason": record.get("change_reason", ""),
            "provider": record.get("provider", ""),
        }
        minimum = _number(record.get("net_profit_min_wan"))
        maximum = _number(record.get("net_profit_max_wan"))
        item["net_profit_min"] = minimum * 10_000 if minimum is not None else None
        item["net_profit_max"] = maximum * 10_000 if maximum is not None else None
        (forward if target_period and target_period > latest_period else stale).append(item)
    growth_range: dict[str, float] = {}
    if forward and baseline_profit not in (None, 0):
        latest_guidance = forward[-1]
        if latest_guidance["net_profit_min"] is not None:
            growth_range["min"] = round(latest_guidance["net_profit_min"] / baseline_profit - 1.0, 6)
        if latest_guidance["net_profit_max"] is not None:
            growth_range["max"] = round(latest_guidance["net_profit_max"] / baseline_profit - 1.0, 6)
    observed = [
        item for item in observed_rows
        if item.get("observation_kind") == "historical_disclosed"
        and not bool(item.get("eligible_as_forward_assumption"))
    ]
    forward_drivers = [
        item for item in forward_driver_rows
        if bool(item.get("eligible_as_forward_assumption"))
    ]
    market_implied: list[dict[str, Any]] = []
    latest_observed_period = max(
        (str(item.get("period") or "") for item in observed), default=""
    )
    latest_facts = {
        str(item.get("metric") or ""): item
        for item in observed
        if str(item.get("period") or "") == latest_observed_period
    }
    capacity = _number((latest_facts.get("capacity") or {}).get("value"))
    production = _number((latest_facts.get("production") or {}).get("value"))
    sales_volume = _number((latest_facts.get("sales_volume") or {}).get("value"))
    product_revenue = _number((latest_facts.get("revenue") or {}).get("value"))
    utilization = production / capacity if capacity not in (None, 0) and production is not None else None
    sell_through = sales_volume / production if production not in (None, 0) and sales_volume is not None else None
    historical_asp = product_revenue / sales_volume if sales_volume not in (None, 0) and product_revenue is not None else None
    product_revenue_exposure = (
        product_revenue / baseline_revenue
        if product_revenue is not None and baseline_revenue not in (None, 0)
        else None
    )
    consensus = structured_consensus if isinstance(structured_consensus, dict) else {}
    if (
        consensus.get("status") == "verified"
        and historical_asp not in (None, 0)
        and product_revenue_exposure is not None
    ):
        for period, estimates in dict(consensus.get("periods") or {}).items():
            if not isinstance(estimates, Mapping):
                continue
            period_text = str(period or "")
            year_match = period_text[:4] if period_text[:4].isdigit() else ""
            if (
                not year_match or not latest_observed_period
                or year_match <= latest_observed_period[:4]
                or not period_text.endswith("Q4")
            ):
                continue
            revenue_estimate = _number((estimates.get("revenue_yuan") or {}).get("median"))
            profit_estimate = _number((estimates.get("net_profit_yuan") or {}).get("median"))
            if revenue_estimate in (None, 0):
                continue
            expected_product_revenue = revenue_estimate * product_revenue_exposure
            required_sales = expected_product_revenue / historical_asp
            efficiency = (
                utilization * sell_through
                if utilization not in (None, 0) and sell_through not in (None, 0)
                else None
            )
            required_capacity = required_sales / efficiency if efficiency not in (None, 0) else None
            try:
                source_count = int(estimates.get("source_count") or 0)
            except (TypeError, ValueError):
                source_count = 0
            market_implied.append({
                "period": period_text,
                "source_count": source_count,
                "consensus_revenue": revenue_estimate,
                "consensus_net_profit": profit_estimate,
                "revenue_growth_vs_baseline": (
                    round(revenue_estimate / baseline_revenue - 1.0, 6)
                    if baseline_revenue not in (None, 0) else None
                ),
                "net_profit_growth_vs_baseline": (
                    round(profit_estimate / baseline_profit - 1.0, 6)
                    if profit_estimate is not None and baseline_profit not in (None, 0) else None
                ),
                "historical_asp": round(historical_asp, 6),
                "historical_product_revenue_exposure": round(product_revenue_exposure, 6),
                "expected_product_revenue_if_flat_exposure": round(expected_product_revenue, 6),
                "historical_utilization": round(utilization, 6) if utilization is not None else None,
                "historical_sell_through": round(sell_through, 6) if sell_through is not None else None,
                "required_sales_if_flat_asp": round(required_sales, 6),
                "required_capacity_if_flat_efficiency": (
                    round(required_capacity, 6) if required_capacity is not None else None
                ),
                "capacity_change_required": (
                    round(required_capacity / capacity - 1.0, 6)
                    if required_capacity is not None and capacity not in (None, 0) else None
                ),
                "implied_net_margin": (
                    round(profit_estimate / revenue_estimate, 6)
                    if profit_estimate is not None else None
                ),
                "model_kind": "market_implied_constraint_scenario",
                "is_company_guidance": False,
                "updates_fact_confidence": False,
                "assumptions": [
                    "flat_product_revenue_exposure", "flat_historical_asp",
                    "flat_utilization", "flat_sell_through",
                ],
            })
    observed_metrics = {str(item.get("metric") or "") for item in observed}
    gaps = []
    if "capacity" not in observed_metrics:
        gaps.append("historical_capacity_observation_missing")
    if "sales_volume" not in observed_metrics:
        gaps.append("historical_sales_observation_missing")
    if not any(str(item.get("metric") or "") in {"price", "asp"} for item in forward_drivers):
        gaps.append("forward_price_driver_missing")
    if not any(str(item.get("metric") or "") == "capacity" for item in forward_drivers):
        gaps.append("forward_capacity_driver_missing")
    if not any(str(item.get("metric") or "") in {"sales_volume", "market_share"} for item in forward_drivers):
        gaps.append("forward_volume_or_share_driver_missing")
    if not forward:
        gaps.append("forward_forecast_missing")
    if product_revenue is not None and baseline_revenue in (None, 0):
        gaps.append("historical_product_revenue_exposure_missing")
    status = (
        "partial" if forward else "market_implied" if market_implied
        else "observed_only" if latest_annual else "not_computable"
    )
    return {
        "status": status,
        "baseline": {
            "period": latest_period,
            "revenue": baseline_revenue,
            "net_profit": baseline_profit,
            "provider": (latest_annual or {}).get("provider", ""),
            "published_at": (latest_annual or {}).get("published_at", ""),
        },
        "forward_guidance": forward,
        "stale_guidance": stale,
        "growth_range": growth_range,
        "observed_historical_drivers": observed,
        "forward_driver_assumptions": forward_drivers,
        "forward_disclosed_indicators": list(forward_disclosed_indicators or []),
        "market_implied_scenarios": market_implied,
        "gaps": gaps,
    }
