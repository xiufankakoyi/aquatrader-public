"""Objective technical reference levels for research reports."""

from __future__ import annotations

from typing import Any

import pandas as pd


def calculate_technical_context(
    history: pd.DataFrame | None, *, as_of: str
) -> dict[str, Any]:
    """Calculate MA/previous-high references without emitting trade actions."""
    empty = {
        "ma5": None,
        "ma10": None,
        "previous_high": None,
        "support_label": "暂无数据",
        "support_price": None,
        "volume_vs_ma5": None,
        "volume_anomaly": False,
    }
    if history is None or history.empty or "close" not in history.columns:
        return empty

    frame = history.copy()
    if "trade_date" in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame = frame[frame["trade_date"] <= pd.Timestamp(as_of[:10])]
        frame = frame.sort_values("trade_date")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"])
    if frame.empty:
        return empty

    close = frame["close"]
    current = float(close.iloc[-1])
    ma5 = float(close.tail(5).mean()) if len(close) >= 5 else None
    ma10 = float(close.tail(10).mean()) if len(close) >= 10 else None
    previous_high = None
    if "high" in frame.columns and len(frame) >= 2:
        high = pd.to_numeric(frame["high"], errors="coerce")
        value = high.iloc[:-1].tail(20).max()
        if pd.notna(value):
            previous_high = float(value)

    candidates = [("MA5", ma5), ("MA10", ma10), ("前高", previous_high)]
    below = [(label, value) for label, value in candidates if value and value <= current]
    support_label, support_price = max(below, key=lambda item: item[1]) if below else (
        "暂无数据",
        None,
    )

    volume_vs_ma5 = None
    if "volume" in frame.columns and len(frame) >= 6:
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        base = volume.iloc[-6:-1].mean()
        if pd.notna(base) and base > 0 and pd.notna(volume.iloc[-1]):
            volume_vs_ma5 = float(volume.iloc[-1]) / float(base)

    return {
        "ma5": round(ma5, 2) if ma5 is not None else None,
        "ma10": round(ma10, 2) if ma10 is not None else None,
        "previous_high": round(previous_high, 2) if previous_high is not None else None,
        "support_label": support_label,
        "support_price": round(support_price, 2) if support_price is not None else None,
        "volume_vs_ma5": round(volume_vs_ma5, 2) if volume_vs_ma5 is not None else None,
        "volume_anomaly": bool(volume_vs_ma5 is not None and volume_vs_ma5 >= 1.8),
    }

