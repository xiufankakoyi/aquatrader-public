"""Strict learning bar reader backed by forward observation lineage."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from core.research.pit_certification.forward_cohort import read_observation_refs

from .learning_bridge import AsOfDailyBar, _finite_float


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return parsed
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp must be a timezone-aware ISO datetime") from exc


class ForwardObservationBarReader:
    """Join UnifiedDataQuery prices to immutable forward observation refs."""

    def __init__(self, cohort_root: str | Path, *, query: Any | None = None) -> None:
        self.cohort_root = Path(cohort_root)
        if query is None:
            from data_svc.unified_data_query import UnifiedDataQuery

            query = UnifiedDataQuery()
        self.query = query

    def first_bar_after(
        self, symbol: str, *, after_date: str, as_of: str
    ) -> AsOfDailyBar | None:
        symbol = symbol.strip() if isinstance(symbol, str) else ""
        if not symbol:
            return None
        try:
            cutoff = _instant(as_of)
            lower = date.fromisoformat(after_date)
        except (TypeError, ValueError):
            return None
        try:
            refs = read_observation_refs(self.cohort_root)
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(refs, Mapping):
            return None
        dates: list[str] = []
        for trade_date in refs:
            try:
                parsed_date = date.fromisoformat(trade_date)
            except (TypeError, ValueError):
                continue
            if lower < parsed_date <= cutoff.date():
                dates.append(trade_date)
        dates.sort()
        if not dates:
            return None
        for trade_date in dates:
            ref = refs.get(trade_date)
            if not isinstance(ref, Mapping):
                continue
            available_at = str(ref.get("available_at") or "")
            record_hash = str(ref.get("record_hash") or "")
            benchmark_fingerprint = str(ref.get("benchmark_fingerprint") or "")
            benchmark_return = ref.get("benchmark_return")
            if (
                not available_at
                or not _SHA256.fullmatch(record_hash)
                or not _SHA256.fullmatch(benchmark_fingerprint)
                or _finite_float(benchmark_return) is None
            ):
                continue
            try:
                if _instant(available_at) > cutoff:
                    continue
            except ValueError:
                continue
            try:
                frame = self.query.get_stock_history(
                    symbol,
                    trade_date,
                    trade_date,
                    columns=["trade_date", "close", "prev_close"],
                )
            except Exception:
                return None
            rows = self._records(frame)
            row = next(
                (
                    item
                    for item in rows
                    if str(item.get("trade_date") or "")[:10] == trade_date
                ),
                None,
            )
            if row is None:
                continue
            close = _finite_float(row.get("close"))
            previous_close = _finite_float(row.get("prev_close"))
            if close is None or previous_close is None:
                continue
            if close <= 0 or previous_close <= 0:
                continue
            observation_ref = f"forward_observation:{record_hash}"
            benchmark_ref = f"forward_benchmark:{benchmark_fingerprint}"
            return AsOfDailyBar(
                symbol=symbol,
                trade_date=trade_date,
                available_at=available_at,
                close=close,
                previous_close=previous_close,
                source_ref=(
                    f"unified_data_query:daily_ohlcv:{symbol}:{trade_date}:"
                    f"{record_hash}"
                ),
                price_available_at_ref=observation_ref,
                benchmark_return=_finite_float(benchmark_return),
                benchmark_source_ref=benchmark_ref,
                benchmark_available_at=available_at,
                benchmark_available_at_ref=observation_ref,
            )
        return None

    @staticmethod
    def _records(frame: Any) -> list[dict[str, Any]]:
        if frame is None:
            return []
        if isinstance(frame, (list, tuple)):
            return [dict(item) for item in frame if isinstance(item, Mapping)]
        try:
            to_dicts = getattr(frame, "to_dicts", None)
            if callable(to_dicts):
                records = to_dicts()
            else:
                records = frame.to_dict("records")
        except (AttributeError, TypeError, ValueError):
            return []
        if not isinstance(records, (list, tuple)):
            return []
        return [dict(item) for item in records if isinstance(item, Mapping)]
