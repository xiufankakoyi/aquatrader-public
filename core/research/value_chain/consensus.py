from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any


_METRIC_COLUMNS = {
    "net_profit_yuan": "net_profit_forecast_yuan",
    "revenue_yuan": "revenue_forecast_yuan",
    "eps": "eps_forecast",
    "pe": "pe_forecast",
}
_NULL_PERIODS = {"none", "nan", "nat", "null"}


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


def _coerce_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _distribution(records: list[dict[str, Any]], column: str) -> dict[str, float]:
    values = [
        number
        for record in records
        if (number := _coerce_number(record.get(column))) is not None
    ]
    if not values:
        return {}
    return {
        "low": round(_quantile(values, 0.25), 6),
        "median": round(_quantile(values, 0.5), 6),
        "high": round(_quantile(values, 0.75), 6),
    }


def summarize_consensus(
    records: list[dict[str, Any]], *, as_of: str, minimum_sources: int = 3
) -> dict[str, Any]:
    if not records:
        return {"status": "missing", "as_of": as_of, "periods": {}, "gaps": ["consensus_rows_missing"]}

    required = {"org_name", "report_date", "forecast_period"}
    if not all(isinstance(record, dict) for record in records) or not all(
        required.issubset(record) for record in records if isinstance(record, dict)
    ):
        return {"status": "missing", "as_of": as_of, "periods": {}, "gaps": ["consensus_schema_incomplete"]}

    as_of_date = _parse_date(as_of)
    if as_of_date is None:
        return {"status": "missing", "as_of": as_of, "periods": {}, "gaps": ["consensus_as_of_invalid"]}

    normalized: list[tuple[date, int, str, str, dict[str, Any]]] = []
    for index, record in enumerate(records):
        report_date = _parse_date(record.get("report_date"))
        organization = str(record.get("org_name") or "").strip()
        period = str(record.get("forecast_period") or "").strip()
        if (
            report_date is None
            or report_date > as_of_date
            or not organization
            or not period
            or period.lower() in _NULL_PERIODS
        ):
            continue
        normalized.append((report_date, index, period, organization, record))

    latest: dict[tuple[str, str], tuple[date, int, str, str, dict[str, Any]]] = {}
    for item in normalized:
        key = (item[2], item[3])
        previous = latest.get(key)
        if previous is None or (item[0], item[1]) >= (previous[0], previous[1]):
            latest[key] = item

    periods: dict[str, Any] = {}
    grouped: dict[str, list[tuple[date, int, str, str, dict[str, Any]]]] = {}
    for item in sorted(latest.values(), key=lambda value: (value[0], value[1])):
        grouped.setdefault(item[2], []).append(item)

    for period, items in grouped.items():
        rows = [item[4] for item in items]
        lineage = [
            {
                "organization": item[3],
                "report_date": item[0].isoformat(),
                "report_title": str(item[4].get("report_title") or ""),
                "provider": str(item[4].get("provider") or ""),
            }
            for item in items
        ]
        periods[period] = {
            "source_count": len(lineage),
            **{
                output: _distribution(rows, column)
                for output, column in _METRIC_COLUMNS.items()
            },
            "lineage": lineage,
        }

    sufficient = any(item["source_count"] >= minimum_sources for item in periods.values())
    return {
        "status": "verified" if sufficient else "insufficient_sources",
        "as_of": as_of,
        "minimum_sources": minimum_sources,
        "periods": periods,
        "gaps": [] if sufficient else ["consensus_independent_sources_insufficient"],
    }
