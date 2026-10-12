"""Post-match sample statistics for PatternRadar."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd


RETURN_HORIZONS = (1, 3, 5, 10)


def _safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_forward_stats(
    symbol_events: pd.DataFrame,
    match_pos: int,
    success_gain_5d_pct: float = 5.0,
) -> Dict[str, Any]:
    """Compute future returns after a matched event.

    Values are statistics for research samples only, not action signals.
    """
    if symbol_events.empty or match_pos < 0 or match_pos >= len(symbol_events):
        return _empty_forward_stats("missing_match_row")

    close0 = _safe_float(symbol_events.iloc[match_pos].get("close"))
    if close0 is None or close0 <= 0:
        return _empty_forward_stats("missing_match_close")

    stats: Dict[str, Any] = {}
    for horizon in RETURN_HORIZONS:
        future_pos = match_pos + horizon
        key = f"future_return_{horizon}d"
        if future_pos < len(symbol_events):
            close_n = _safe_float(symbol_events.iloc[future_pos].get("close"))
            stats[key] = ((close_n / close0 - 1.0) * 100.0) if close_n is not None else None
        else:
            stats[key] = None

    future_5d = symbol_events.iloc[match_pos + 1 : match_pos + 6]
    if future_5d.empty:
        stats["max_gain_5d"] = None
        stats["max_drawdown_5d"] = None
        stats["success_label"] = None
        stats["failure_reason"] = "no_future_data"
        return stats

    highs = pd.to_numeric(future_5d.get("high"), errors="coerce")
    lows = pd.to_numeric(future_5d.get("low"), errors="coerce")
    max_high = highs.max(skipna=True)
    min_low = lows.min(skipna=True)

    stats["max_gain_5d"] = ((float(max_high) / close0 - 1.0) * 100.0) if pd.notna(max_high) else None
    stats["max_drawdown_5d"] = ((float(min_low) / close0 - 1.0) * 100.0) if pd.notna(min_low) else None

    success = stats["max_gain_5d"] is not None and stats["max_gain_5d"] >= float(success_gain_5d_pct)
    stats["success_label"] = bool(success)
    if success:
        stats["failure_reason"] = None
    elif stats["max_drawdown_5d"] is not None and stats["max_drawdown_5d"] <= -5.0:
        stats["failure_reason"] = "5日内最大回撤较大"
    else:
        stats["failure_reason"] = "5日内未达到样本成功阈值"
    return stats


def _empty_forward_stats(reason: str) -> Dict[str, Any]:
    stats = {f"future_return_{horizon}d": None for horizon in RETURN_HORIZONS}
    stats.update(
        {
            "max_gain_5d": None,
            "max_drawdown_5d": None,
            "success_label": None,
            "failure_reason": reason,
        }
    )
    return stats
