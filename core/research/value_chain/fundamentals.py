from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from .contracts import FinancialQuality, MetricValue

# P1.2 引入新引擎（延迟导入避免循环依赖）
try:
    from core.research.fundamentals.contracts import NormalizedFinancialStatement
    from core.research.fundamentals.quality_engine import (
        QualityReport,
        compute_quality_metrics,
    )
    from core.research.fundamentals.anomaly_engine import (
        AnomalyReport,
        detect_anomalies,
    )
    _P12_AVAILABLE = True
except ImportError:  # pragma: no cover
    _P12_AVAILABLE = False


_ALIASES = {
    "revenue": ("revenue", "total_revenue", "营业收入"),
    "net_profit": ("net_profit", "net_profit_parent", "归母净利润", "净利润"),
    "operating_cash_flow": ("operating_cash_flow", "net_operate_cash_flow", "经营活动现金流净额"),
    "gross_margin": ("gross_margin", "gross_profit_margin", "毛利率"),
    "net_margin": ("net_margin", "net_profit_margin", "净利率"),
    "roe": ("roe", "roe_weighted", "净资产收益率"),
    "roic": ("roic", "投入资本回报率"),
    "debt_ratio": ("debt_ratio", "debt_to_assets", "资产负债率"),
}


def _number(row: dict[str, Any], name: str) -> tuple[float | None, str]:
    for key in _ALIASES[name]:
        value = row.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, bool):
            continue
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(result):
            return result, key
    return None, ""


def _growth(latest: dict[str, Any], previous: dict[str, Any], name: str) -> MetricValue | None:
    current, current_key = _number(latest, name)
    base, base_key = _number(previous, name)
    if current is None or base in (None, 0):
        return None
    growth = current / base - 1.0
    if not isfinite(growth):
        return None
    return MetricValue(
        value=round(growth, 6),
        source_fields=[base_key, current_key],
        period=str(latest.get("period") or latest.get("report_period") or ""),
    )


def _comparison_period(
    ordered: list[dict[str, Any]], latest: dict[str, Any]
) -> dict[str, Any] | None:
    period = str(latest.get("period") or latest.get("report_period") or latest.get("end_date") or "")
    digits = "".join(character for character in period if character.isdigit())
    expected = ""
    if len(digits) == 8:
        expected = f"{int(digits[:4]) - 1:04d}{digits[4:]}"
    elif len(digits) == 4:
        expected = str(int(digits) - 1)
    if expected:
        for candidate in reversed(ordered[:-1]):
            candidate_period = str(
                candidate.get("period") or candidate.get("report_period") or candidate.get("end_date") or ""
            )
            if "".join(character for character in candidate_period if character.isdigit()) == expected:
                return candidate
        return None
    return ordered[-2] if len(ordered) >= 2 else None


def compute_financial_quality(records: list[dict[str, Any]]) -> FinancialQuality:
    """Derive only metrics supported by supplied financial statement fields."""
    ordered = sorted(
        (dict(item) for item in records if isinstance(item, Mapping)),
        key=lambda item: str(item.get("period") or item.get("report_period") or item.get("end_date") or ""),
    )
    metrics: dict[str, MetricValue] = {}
    previous = _comparison_period(ordered, ordered[-1]) if ordered else None
    if previous is not None:
        for output, source in (("revenue_growth", "revenue"), ("net_profit_growth", "net_profit")):
            value = _growth(ordered[-1], previous, source)
            if value is not None:
                metrics[output] = value

    if ordered:
        latest = ordered[-1]
        period = str(latest.get("period") or latest.get("report_period") or "")
        for name in ("gross_margin", "net_margin", "roe", "roic", "debt_ratio"):
            value, key = _number(latest, name)
            if value is not None:
                metrics[name] = MetricValue(value=round(value, 6), source_fields=[key], period=period)
        cash, cash_key = _number(latest, "operating_cash_flow")
        profit, profit_key = _number(latest, "net_profit")
        if cash is not None and profit not in (None, 0):
            cash_conversion = cash / profit
            if not isfinite(cash_conversion):
                cash_conversion = None
            if cash_conversion is None:
                cash = None
        if cash is not None and profit not in (None, 0):
            metrics["cash_conversion"] = MetricValue(
                value=round(cash_conversion, 6), source_fields=[cash_key, profit_key], period=period
            )

    expected = [
        "revenue_growth", "net_profit_growth", "gross_margin", "net_margin",
        "cash_conversion", "roe", "roic", "debt_ratio",
    ]
    missing = [name for name in expected if name not in metrics]
    if not metrics:
        status = "not_computable"
    elif missing:
        status = "partial"
    else:
        status = "ok"
    return FinancialQuality(status=status, metrics=metrics, missing_metrics=missing)


# ---------------------------------------------------------------------------
# P1.2 桥接：从 NormalizedFinancialStatement 调用新引擎
# ---------------------------------------------------------------------------


def build_quality_report_from_statements(
    current: "NormalizedFinancialStatement",
    previous: "NormalizedFinancialStatement | None" = None,
) -> "QualityReport":
    """从 NormalizedFinancialStatement 调用 P1.2 质量引擎。

    Args:
        current: 当期标准化三表
        previous: 同口径上一期（可选）

    Returns:
        QualityReport 实例

    Raises:
        RuntimeError: 当 P1.2 引擎不可用（导入失败）
    """
    if not _P12_AVAILABLE:
        raise RuntimeError("P1.2 引擎不可用，请检查 core/research/fundamentals 包")
    return compute_quality_metrics(current, previous)


def build_anomaly_report_from_statements(
    current: "NormalizedFinancialStatement",
    previous: "NormalizedFinancialStatement | None" = None,
    industry_median_gross_margin: float | None = None,
) -> "AnomalyReport":
    """从 NormalizedFinancialStatement 调用 P1.2 异常引擎。

    Args:
        current: 当期标准化三表
        previous: 同口径上一期（可选）
        industry_median_gross_margin: 行业毛利率中位数（可选）

    Returns:
        AnomalyReport 实例

    Raises:
        RuntimeError: 当 P1.2 引擎不可用（导入失败）
    """
    if not _P12_AVAILABLE:
        raise RuntimeError("P1.2 引擎不可用，请检查 core/research/fundamentals 包")
    return detect_anomalies(current, previous, industry_median_gross_margin)
