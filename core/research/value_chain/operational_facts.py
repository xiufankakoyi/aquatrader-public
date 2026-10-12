from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_VALUE = r"([\d,]+(?:\.\d+)?)"
_TABLE_PATTERN = re.compile(
    r"(?P<quote>"
    r"产品\s*名称\s*本报告期\s*上年同期\s*"
    r"产能\s*产量\s*销量\s*营业收入\s*毛利率\s*"
    r"产能\s*产量\s*销量\s*营业收入\s*毛利率\s*"
    r"(?P<product>[^\d]{2,80}?)\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*"
    + _VALUE + r"%\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*万只\s*"
    + _VALUE + r"\s*"
    + _VALUE + r"%"
    r")",
    flags=re.DOTALL,
)


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _periods(text: str) -> tuple[str, str]:
    match = re.search(r"(20\d{2})\s*年\s*年度报告", text)
    if not match:
        return "", ""
    year = int(match.group(1))
    return f"{year}1231", f"{year - 1}1231"


def parse_operating_tables(text: str) -> list[dict[str, Any]]:
    """Parse only the fixed operating table disclosed in a Chinese annual report.

    Values are observations for the reporting periods.  They are explicitly
    marked ineligible for forward earnings assumptions.
    """
    source = str(text or "")
    current_period, previous_period = _periods(source)
    if not current_period:
        return []
    output: list[dict[str, Any]] = []
    for match in _TABLE_PATTERN.finditer(source):
        values = [_number(value) for value in match.groups()[2:]]
        product = re.sub(r"\s+", "", match.group("product")).strip("：:")
        current = {
            "capacity": values[0] * 10_000,
            "production": values[1] * 10_000,
            "sales_volume": values[2] * 10_000,
            "revenue": values[3],
            "gross_margin": values[4] / 100.0,
        }
        previous = {
            "capacity": values[5] * 10_000,
            "production": values[6] * 10_000,
            "sales_volume": values[7] * 10_000,
            "revenue": values[8],
            "gross_margin": values[9] / 100.0,
        }
        output.append({
            "product_name": product,
            "current_period": current_period,
            "previous_period": previous_period,
            "current": current,
            "previous": previous,
            "observation_kind": "historical_disclosed",
            "eligible_as_forward_assumption": False,
            "quote": match.group("quote"),
        })
    return output


def flatten_operating_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units = {
        "capacity": "pieces",
        "production": "pieces",
        "sales_volume": "pieces",
        "revenue": "yuan",
        "gross_margin": "ratio",
    }
    output: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        for side, period_key in (("current", "current_period"), ("previous", "previous_period")):
            values = table.get(side)
            if not isinstance(values, Mapping):
                continue
            for metric, value in values.items():
                output.append({
                    "product_name": table.get("product_name", ""),
                    "period": table.get(period_key, ""),
                    "metric": metric,
                    "value": value,
                    "unit": units.get(metric, "unknown"),
                    "observation_kind": "historical_disclosed",
                    "eligible_as_forward_assumption": False,
                    "quote": table.get("quote", ""),
                })
    return output


_FORWARD_PLAN = re.compile(
    r"(?P<year>20\d{2})\s*年度工作计划\s*"
    r"(?P<quote>1、继续加大\s*(?P<products>[^。；\n]{2,80}?)\s*"
    r"等高端产品的交付能力和出货量[^。]{0,200}。)",
    flags=re.DOTALL,
)


def parse_forward_plan_indicators(text: str) -> list[dict[str, Any]]:
    """Extract explicit management directions while refusing to invent magnitude."""
    output: list[dict[str, Any]] = []
    for match in _FORWARD_PLAN.finditer(str(text or "")):
        products = [
            item.strip() for item in re.split(r"[、,，/]+", match.group("products"))
            if item.strip()
        ]
        output.append({
            "period": match.group("year"),
            "products": products,
            "metric": "delivery_and_shipment_direction",
            "direction": "increase",
            "value": None,
            "unit": "qualitative_direction",
            "observation_kind": "company_forward_plan_qualitative",
            "eligible_as_forward_assumption": False,
            "reason_ineligible": "magnitude_not_disclosed",
            "quote": match.group("quote"),
        })
    return output
