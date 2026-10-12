"""Read-only performance comparison for a PIT research-universe gate."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from math import isfinite
from statistics import mean, median
from typing import Any


def _metrics(values: list[float]) -> dict[str, float | int]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return {
        "observations": len(values),
        "total_return": equity - 1.0,
        "mean_daily_return": mean(values) if values else 0.0,
        "median_daily_return": median(values) if values else 0.0,
        "win_rate": (
            sum(value > 0 for value in values) / len(values) if values else 0.0
        ),
        "max_drawdown": max_drawdown,
    }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _index_bars(
    bars_by_symbol: Mapping[str, Any],
) -> dict[str, dict[str, Mapping[str, Any]]]:
    indexed: dict[str, dict[str, Mapping[str, Any]]] = {}
    for symbol, rows in bars_by_symbol.items():
        if not isinstance(symbol, str) or not isinstance(rows, (list, tuple)):
            continue
        symbol_rows: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            trade_date = str(row.get("trade_date", "")).strip()[:10]
            if trade_date:
                symbol_rows[trade_date] = row
        indexed[symbol] = symbol_rows
    return indexed


def evaluate_research_gate(
    *,
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    all_symbols: list[str],
    gated_symbols: list[str],
    trading_dates: list[str] | None = None,
    top_k: int = 3,
    evaluation_days: int = 20,
    momentum_lookback: int = 5,
) -> dict[str, Any]:
    """Compare the same momentum rule with and without a research gate.

    Signal for entry day D is computed at D-1 from closes no later than D-1.
    The simulated holding return is close(D) to open(D+1).
    """
    if not isinstance(bars_by_symbol, Mapping):
        return {
            "overall_valid": False,
            "failure": "bars_by_symbol must be a mapping",
            "daily_results": [],
            "unresolved_execution_count": 0,
        }
    indexed = _index_bars(bars_by_symbol)
    dates = (
        sorted({str(value)[:10] for value in trading_dates if value})
        if trading_dates is not None
        else sorted({date_value for rows in indexed.values() for date_value in rows})
    )
    required_dates = evaluation_days + momentum_lookback + 2
    if len(dates) < required_dates:
        return {
            "overall_valid": False,
            "failure": f"交易日期不足：{len(dates)} < {required_dates}",
            "daily_results": [],
            "unresolved_execution_count": 0,
        }

    entry_start = len(dates) - evaluation_days - 1
    daily_results: list[dict[str, Any]] = []
    unresolved = 0

    def rank(symbols: list[str], signal_index: int) -> list[str]:
        signal_date = dates[signal_index]
        start_date = dates[signal_index - momentum_lookback]
        scored: list[tuple[float, str]] = []
        for symbol in sorted(set(symbols)):
            signal_row = indexed.get(symbol, {}).get(signal_date)
            start_row = indexed.get(symbol, {}).get(start_date)
            if not signal_row or not start_row:
                continue
            start_close = _finite_number(start_row.get("close"))
            signal_close = _finite_number(signal_row.get("close"))
            if (
                start_close is not None
                and signal_close is not None
                and start_close > 0
                and signal_close > 0
            ):
                scored.append((signal_close / start_close - 1.0, symbol))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [symbol for _, symbol in scored[:top_k]]

    def holding_result(
        symbols: list[str], entry_date: str, exit_date: str
    ) -> tuple[float | None, list[str]]:
        returns: list[float] = []
        missing: list[str] = []
        for symbol in symbols:
            entry = indexed.get(symbol, {}).get(entry_date)
            exit_row = indexed.get(symbol, {}).get(exit_date)
            entry_close = _finite_number((entry or {}).get("close"))
            exit_open = _finite_number((exit_row or {}).get("open"))
            if (
                entry_close is None
                or exit_open is None
                or entry_close <= 0
                or exit_open <= 0
            ):
                missing.append(symbol)
                continue
            returns.append(exit_open / entry_close - 1.0)
        if missing or not returns:
            return None, missing
        return mean(returns), []

    for entry_index in range(entry_start, len(dates) - 1):
        signal_index = entry_index - 1
        signal_date = dates[signal_index]
        entry_date = dates[entry_index]
        exit_date = dates[entry_index + 1]
        gated = rank(gated_symbols, signal_index)
        active = rank(all_symbols, signal_index)
        passive = sorted(set(all_symbols))
        gated_return, gated_missing = holding_result(gated, entry_date, exit_date)
        active_return, active_missing = holding_result(active, entry_date, exit_date)
        passive_return, passive_missing = holding_result(passive, entry_date, exit_date)
        missing = sorted(set(gated_missing + active_missing + passive_missing))
        unresolved += len(missing)
        daily_results.append({
            "signal_date": signal_date,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "research_gated": {"symbols": gated, "return": gated_return},
            "active_baseline": {"symbols": active, "return": active_return},
            "passive_equal_weight": {"symbols": passive, "return": passive_return},
            "missing_execution_symbols": missing,
            "leakage_detected": signal_date >= entry_date,
        })

    leakage_count = sum(
        int(item["leakage_detected"]) for item in daily_results
    )
    report: dict[str, Any] = {
        "schema_version": "p2-research-gate-performance-v1",
        "scope": "historical_reconstruction_not_trade_advice",
        "overall_valid": unresolved == 0 and leakage_count == 0,
        "method": {
            "signal": f"{momentum_lookback}-trading-day close momentum at D-1",
            "entry": "D close",
            "exit": "D+1 open",
            "weighting": "equal weight",
            "top_k": top_k,
            "costs_and_slippage": 0.0,
        },
        "all_company_count": len(set(all_symbols)),
        "research_gated_company_count": len(set(gated_symbols)),
        "evaluation_days": len(daily_results),
        "unresolved_execution_count": unresolved,
        "leakage_count": leakage_count,
        "daily_results": daily_results,
    }
    for key in ("research_gated", "active_baseline", "passive_equal_weight"):
        values = [
            float(item[key]["return"])
            for item in daily_results
            if item[key]["return"] is not None
        ]
        report[key] = _metrics(values)
    unsigned = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report["evidence_sha256"] = hashlib.sha256(unsigned).hexdigest()
    return report
