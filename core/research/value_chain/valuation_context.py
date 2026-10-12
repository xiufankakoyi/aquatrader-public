from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        for fmt in ("%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _not_computable(as_of: str, gap: str) -> dict[str, Any]:
    return {
        "status": "not_computable",
        "as_of": as_of,
        "current": {},
        "history": {},
        "forward_multiples": [],
        "gaps": [gap],
    }


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def summarize_valuation_history(
    records: list[dict[str, Any]],
    *,
    structured_consensus: dict[str, Any],
    as_of: str,
) -> dict[str, Any]:
    if not records:
        return _not_computable(as_of, "valuation_history_missing")
    if not any(isinstance(record, dict) and "trade_date" in record for record in records):
        return _not_computable(as_of, "valuation_trade_date_missing")

    as_of_date = _parse_date(as_of)
    if as_of_date is None:
        return _not_computable(as_of, "valuation_as_of_invalid")

    visible: list[tuple[date, int, dict[str, Any]]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        trade_date = _parse_date(record.get("trade_date"))
        if trade_date is not None and trade_date <= as_of_date:
            visible.append((trade_date, index, record))
    visible.sort(key=lambda item: (item[0], item[1]))
    if not visible:
        return _not_computable(as_of, "valuation_history_not_visible")

    latest_date, _, latest = visible[-1]
    rows = [item[2] for item in visible]
    current: dict[str, Any] = {"trade_date": latest_date.isoformat()}
    history: dict[str, Any] = {}
    for metric in ("pe_ttm", "pb", "ps_ttm"):
        values = [
            number
            for row in rows
            if (number := _number(row.get(metric))) is not None and number > 0
        ]
        current_value = _number(latest.get(metric))
        if not values or current_value is None or current_value <= 0:
            continue
        history[metric] = {
            "low": round(_quantile(values, 0.25), 6),
            "median": round(_quantile(values, 0.5), 6),
            "high": round(_quantile(values, 0.75), 6),
            "percentile": round(sum(value <= current_value for value in values) / len(values), 6),
            "sample_count": len(values),
        }
        current[metric] = current_value

    market_cap = _number(latest.get("total_market_cap"))
    if market_cap is not None and market_cap > 0:
        current["total_market_cap"] = market_cap

    forward: list[dict[str, Any]] = []
    consensus = structured_consensus if isinstance(structured_consensus, dict) else {}
    if market_cap is not None and market_cap > 0 and consensus.get("status") == "verified":
        for period, estimates in dict(consensus.get("periods") or {}).items():
            period_text = str(period or "")
            period_year = period_text[:4] if period_text[:4].isdigit() else ""
            if not period_text.endswith("Q4") or not period_year or period_year < as_of[:4]:
                continue
            estimate = estimates if isinstance(estimates, dict) else {}
            profit_summary = estimate.get("net_profit_yuan")
            profit_summary = profit_summary if isinstance(profit_summary, dict) else {}
            net_profit = _number(profit_summary.get("median"))
            if net_profit is None or net_profit <= 0:
                continue
            forward.append(
                {
                    "period": period_text,
                    "source_count": int(estimate.get("source_count") or 0),
                    "forward_pe": round(market_cap / net_profit, 6),
                    "model_kind": "market_cap_over_consensus_profit",
                    "is_company_guidance": False,
                }
            )
    return {
        "status": "ok" if history else "partial",
        "as_of": as_of,
        "current": current,
        "history": history,
        "history_start": visible[0][0].isoformat(),
        "history_end": visible[-1][0].isoformat(),
        "record_count": len(visible),
        "forward_multiples": forward,
        "gaps": [] if history else ["valuation_metrics_missing"],
    }
