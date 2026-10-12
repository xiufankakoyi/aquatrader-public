"""Data source router for market data and research retrieval."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from data_svc.providers.provider_registry import ProviderRegistry
from data_svc.unified_data_query import UnifiedDataQuery


@dataclass
class SourceQueryResult:
    """Uniform result returned to ResearchEngine.query_source."""

    status: str
    events: list[dict[str, Any]] = field(default_factory=list)
    selected_provider: str = ""
    source_chain: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "events": self.events,
            "selected_provider": self.selected_provider,
            "source_chain": self.source_chain,
            "errors": self.errors,
        }


class SourceRouter:
    """Route logical data requests to the provider registry."""

    def __init__(self, registry: ProviderRegistry | None = None):
        self.registry = registry or ProviderRegistry()
        self.query_engine = UnifiedDataQuery()

    def route(
        self,
        data_type: str,
        symbols: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Route legacy market-data requests."""
        symbols = symbols or []
        data_type = (data_type or "").lower()

        if data_type == "daily":
            end = end_date or datetime.now().strftime("%Y-%m-%d")
            start = start_date or end
            def load_symbol(sym: str) -> pd.DataFrame | None:
                df = self.query_engine.get_stock_history(sym, start, end)
                if df is not None and not df.empty:
                    if "symbol" not in df.columns:
                        df["symbol"] = sym
                    return df
                return None

            if len(symbols) > 1:
                with ThreadPoolExecutor(
                    max_workers=min(8, len(symbols)),
                    thread_name_prefix="source-daily",
                ) as executor:
                    queried = executor.map(load_symbol, symbols)
                    frames = [frame for frame in queried if frame is not None]
            else:
                frames = [frame for sym in symbols if (frame := load_symbol(sym)) is not None]
            return {
                "status": "ok",
                "data": pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(),
            }

        if data_type == "realtime":
            try:
                return {"status": "ok", "data": self.registry.get_realtime_quotes(symbols)}
            except Exception as exc:
                return {"status": "error", "detail": str(exc)}

        return {"status": "error", "detail": f"Unknown data type: {data_type}"}

    def query(
        self,
        capability: str,
        symbols: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        keyword: str | None = None,
        query: str | None = None,
    ) -> SourceQueryResult:
        """Query a research capability through ProviderRegistry failover."""
        del start_date, end_date
        capability = (capability or "semantic_search").strip()
        symbols = symbols or []
        limit = max(1, int(limit or 20))
        errors: list[str] = []
        events: list[dict[str, Any]] = []
        selected_provider = ""

        try:
            if capability == "semantic_search":
                result = self.registry.semantic_search(query=query or keyword or "", count=limit)
                selected_provider = str(result.get("source") or "")
                events = [
                    self._event_from_mapping(item, capability, query=query or keyword or "")
                    for item in (result.get("results", []) or [])
                ]
                if not events and result.get("error"):
                    errors.append(str(result.get("error")))

            elif capability == "announcements":
                selected_provider = "provider_registry.announcements"
                for sym in symbols[:limit]:
                    for item in self.registry.get_announcements(code=sym, count=limit):
                        events.append(self._event_from_mapping(item, capability, symbol=sym))

            elif capability == "reports":
                selected_provider = "provider_registry.reports"
                for sym in symbols[:limit]:
                    for item in self.registry.get_research_reports(code=sym, count=limit):
                        events.append(self._event_from_mapping(item, capability, symbol=sym))

            elif capability == "periodic_reports":
                selected_provider = "provider_registry.periodic_reports"
                for sym in symbols[:limit]:
                    for item in self.registry.get_periodic_reports(code=sym, count=limit):
                        events.append(self._event_from_mapping(item, capability, symbol=sym))

            elif capability == "investor_interactions":
                selected_provider = "provider_registry.investor_interactions"
                for sym in symbols[:limit]:
                    for item in self.registry.get_investor_interactions(code=sym, count=limit):
                        events.append(self._event_from_mapping(item, capability, symbol=sym))

            elif capability == "news":
                search_text = query or " ".join([keyword or "", *symbols]).strip()
                result = self.registry.semantic_search(query=search_text, count=limit)
                selected_provider = str(result.get("source") or "semantic_news_fallback")
                events = [
                    self._event_from_mapping(item, capability, query=search_text)
                    for item in (result.get("results", []) or [])
                ]
                if not events and result.get("error"):
                    errors.append(str(result.get("error")))

            else:
                errors.append(f"unsupported capability: {capability}")

        except Exception as exc:
            errors.append(str(exc))

        return SourceQueryResult(
            status="ok" if events else ("empty" if not errors else "error"),
            events=events[:limit],
            selected_provider=selected_provider,
            source_chain=[capability],
            errors=errors,
        )

    def status(self, check_health: bool = False) -> dict[str, Any]:
        """Return logical research routes and optional provider health."""
        routes = {
            "kline": ["unified_data_query.daily"],
            "quote": ["provider_registry.realtime"],
            "semantic_search": ["wencai", "ifind", "akshare", "baidu", "duckduckgo"],
            "announcements": ["cninfo", "tushare", "akshare"],
            "reports": ["akshare"],
            "periodic_reports": ["cninfo", "akshare"],
            "investor_interactions": ["akshare"],
            "news": ["semantic_search"],
        }
        payload: dict[str, Any] = {"routes": routes}
        if check_health and hasattr(self.registry, "health_check_all"):
            payload["providers"] = {
                name: {
                    "enabled": health.enabled,
                    "status": health.status,
                    "capabilities": health.capabilities,
                    "last_error": health.last_error,
                }
                for name, health in self.registry.health_check_all().items()
            }
        return payload

    @staticmethod
    def _event_from_mapping(
        item: Any,
        capability: str,
        *,
        symbol: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            item = {"title": str(item)}
        title = (
            item.get("title")
            or item.get("公告标题")
            or item.get("研报标题")
            or item.get("股票简称")
            or item.get("鑲＄エ绠€绉?")
            or query
            or str(item)[:80]
        )
        summary = (
            item.get("digest")
            or item.get("summary")
            or item.get("snippet")
            or item.get("body")
            or item.get("answer")
            or ""
        )
        event_symbol = symbol or str(
            item.get("symbol")
            or item.get("stock_code")
            or item.get("股票代码")
            or item.get("鑲＄エ浠ｇ爜")
            or ""
        )
        return {
            "event_type": capability,
            "title": str(title),
            "summary": str(summary),
            "symbol": event_symbol,
            "stock_name": str(
                item.get("stock_name")
                or item.get("股票简称")
                or item.get("鑲＄エ绠€绉?")
                or ""
            ),
            "event_time": str(item.get("date") or item.get("time") or item.get("published_at") or ""),
            "source_url": str(item.get("url") or item.get("link") or item.get("pdf_url") or ""),
            "fetched_at": datetime.now().isoformat(),
            "payload": item,
        }


# Backward-compatible name used by ResearchEngine.
DataSourceRouter = SourceRouter
