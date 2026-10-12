from __future__ import annotations

import math
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .contracts import SourceSnapshot


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_clean(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return value


def _parse_visible_date(value: Any) -> date | None:
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


class ResearchSourceGateway:
    """Thin adapter around the existing failover registry with audit snapshots."""

    def __init__(
        self,
        registry: Any | None = None,
        *,
        cache_dir: str | Path | None = None,
    ) -> None:
        if registry is None:
            from infrastructure.data_providers.provider_registry import ProviderRegistry

            registry = ProviderRegistry()
        self.registry = registry
        self.cache_dir = Path(
            cache_dir
            or os.getenv("RESEARCH_SOURCE_CACHE_DIR", "data/research_cache/source_snapshots")
        )

    def _cache_path(self, symbol: str, capability: str) -> Path:
        safe_symbol = "".join(char if char.isalnum() else "_" for char in symbol.upper())
        return self.cache_dir / safe_symbol / f"{capability}.json"

    def _save_snapshot(self, symbol: str, snapshot: SourceSnapshot) -> None:
        if snapshot.status != "ok" or not snapshot.records:
            return
        path = self._cache_path(symbol, snapshot.capability)
        payload = {
            "schema_version": "research_source_snapshot_v1",
            "symbol": symbol.upper(),
            "capability": snapshot.capability,
            "provider": snapshot.provider,
            "source_as_of": snapshot.as_of,
            "cached_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "records": snapshot.records,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError:
            return

    def _fallback_to_cache(
        self,
        symbol: str,
        snapshot: SourceSnapshot,
        *,
        visible_time_fields: tuple[str, ...],
    ) -> SourceSnapshot:
        if snapshot.status == "ok":
            self._save_snapshot(symbol, snapshot)
            return snapshot
        path = self._cache_path(symbol, snapshot.capability)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return snapshot
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            return snapshot
        if (
            payload.get("schema_version") != "research_source_snapshot_v1"
            or str(payload.get("symbol") or "").upper() != symbol.upper()
            or payload.get("capability") != snapshot.capability
        ):
            return snapshot
        cached = self._capture(
            snapshot.capability,
            snapshot.as_of,
            lambda: payload["records"],
            visible_time_fields=visible_time_fields,
        )
        if cached.status != "ok":
            return snapshot
        cached.provider = f"local_snapshot_cache:{payload.get('provider') or 'unknown'}"
        cached.attempts = [*snapshot.attempts, {
            "provider_used": "local_snapshot_cache",
            "success": True,
            "source_as_of": payload.get("source_as_of"),
            "cached_at": payload.get("cached_at"),
        }]
        cached.degradation_reason = "live_source_unavailable_using_point_in_time_cache"
        return cached

    @staticmethod
    def _date(value: str) -> str:
        datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return str(value)[:10]

    def _capture(
        self,
        capability: str,
        as_of: str,
        call: Callable[[], Any],
        *,
        visible_time_fields: tuple[str, ...] = (),
    ) -> SourceSnapshot:
        logs = getattr(self.registry, "logs", [])
        before = len(logs)
        try:
            raw = call()
            if isinstance(raw, pd.DataFrame):
                records = raw.to_dict(orient="records")
            elif isinstance(raw, list):
                records = [dict(item) if isinstance(item, dict) else {"value": item} for item in raw]
            elif isinstance(raw, dict):
                records = [] if raw.get("error") else [raw]
            else:
                records = []
            records = _clean(records)
            excluded_future = 0
            excluded_unknown = 0
            if visible_time_fields:
                cutoff = datetime.strptime(as_of, "%Y-%m-%d").date()
                historical = cutoff < date.today()
                visible_records: list[dict[str, Any]] = []
                for record in records:
                    raw_time = next(
                        (record.get(field) for field in visible_time_fields if record.get(field)),
                        None,
                    )
                    if raw_time is None:
                        if historical:
                            excluded_unknown += 1
                            continue
                        visible_records.append(record)
                        continue
                    try:
                        visible_date = _parse_visible_date(raw_time)
                    except (TypeError, ValueError):
                        visible_date = None
                    if visible_date is None:
                        if historical:
                            excluded_unknown += 1
                            continue
                        visible_records.append(record)
                        continue
                    if visible_date > cutoff:
                        excluded_future += 1
                        continue
                    visible_records.append(record)
                records = visible_records
            attempts = _clean(list(logs[before:]))
            provider = ""
            if records:
                provider = str(records[0].get("provider") or records[0].get("source") or "")
            if not provider and attempts:
                successful = [item for item in attempts if item.get("success")]
                provider = str((successful[-1] if successful else attempts[-1]).get("provider_used") or "")
            return SourceSnapshot(
                capability=capability,
                status="ok" if records else "empty",
                provider=provider,
                as_of=as_of,
                records=records,
                attempts=attempts,
                degradation_reason=(
                    "" if records else
                    "point_in_time_filtered" if excluded_future or excluded_unknown else
                    "no_records"
                ),
                excluded_future_records=excluded_future,
                excluded_unknown_time_records=excluded_unknown,
            )
        except Exception as exc:
            return SourceSnapshot(
                capability=capability,
                status="failed",
                as_of=as_of,
                attempts=_clean(list(logs[before:])),
                degradation_reason=f"{type(exc).__name__}: {exc}",
            )

    def collect(self, symbol: str, *, as_of: str, lookback_days: int = 120) -> dict[str, SourceSnapshot]:
        as_of_date = self._date(as_of)
        start = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=max(30, lookback_days))).strftime("%Y-%m-%d")
        finance_method = getattr(self.registry, "get_finance_snapshot", None)
        consensus_method = getattr(self.registry, "get_consensus_estimates", None)
        valuation_history_method = getattr(self.registry, "get_valuation_history", None)
        snapshots = {
            "profile": self._capture(
                "company_profile", as_of_date,
                lambda: self.registry.get_stock_basic_info(symbols=[symbol]),
            ),
            "daily_bars": self._capture(
                "daily_bars", as_of_date,
                lambda: self.registry.get_daily_bars(start_date=start, end_date=as_of_date, symbols=[symbol]),
                visible_time_fields=("trade_date", "date", "datetime"),
            ),
            "earnings_forecast": self._capture(
                "earnings_forecast", as_of_date,
                lambda: self.registry.get_earnings_forecasts(start_date=start, end_date=as_of_date, symbols=[symbol]),
                visible_time_fields=("announcement_date", "ann_date", "published_at", "publish_date", "date"),
            ),
            "announcements": self._capture(
                "announcements", as_of_date,
                lambda: self.registry.get_announcements(code=symbol.split(".")[0], count=20),
                visible_time_fields=("published_at", "publish_time", "announcement_date", "ann_date", "date"),
            ),
            "valuation": self._capture(
                "valuation", as_of_date,
                lambda: self.registry.smart_quote(symbol),
                visible_time_fields=("trade_date", "date", "datetime", "timestamp"),
            ),
        }
        if consensus_method is None:
            snapshots["consensus"] = SourceSnapshot(
                capability="consensus_estimates", status="empty", as_of=as_of_date,
                degradation_reason="capability_not_available",
            )
        else:
            snapshots["consensus"] = self._capture(
                "consensus_estimates", as_of_date,
                lambda: consensus_method(start_date=start, end_date=as_of_date, symbols=[symbol]),
                visible_time_fields=("report_date",),
            )
            snapshots["consensus"] = self._fallback_to_cache(
                symbol,
                snapshots["consensus"],
                visible_time_fields=("report_date",),
            )
        if valuation_history_method is None:
            snapshots["valuation_history"] = SourceSnapshot(
                capability="valuation_history", status="empty", as_of=as_of_date,
                degradation_reason="capability_not_available",
            )
        else:
            snapshots["valuation_history"] = self._capture(
                "valuation_history", as_of_date,
                lambda: valuation_history_method(
                    start_date=start, end_date=as_of_date, symbols=[symbol]
                ),
                visible_time_fields=("trade_date",),
            )
        if finance_method is None:
            snapshots["finance"] = SourceSnapshot(
                capability="finance_snapshot", status="empty", as_of=as_of_date,
                degradation_reason="capability_not_available",
            )
        else:
            snapshots["finance"] = self._capture(
                "finance_snapshot", as_of_date,
                lambda: finance_method(symbols=[symbol], as_of=as_of_date),
                visible_time_fields=("ann_date", "published_at", "publish_date", "date"),
            )
        return snapshots
