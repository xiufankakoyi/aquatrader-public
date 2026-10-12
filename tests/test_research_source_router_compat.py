from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

from data_svc.providers.source_router import SourceRouter


class _FakeRegistry:
    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        return {
            "source": "fake_semantic",
            "query": query,
            "results": [
                {
                    "股票代码": "601179",
                    "股票简称": "中国西电",
                    "title": "中国西电 特高压 公开资料",
                    "summary": "用于 smoke 的本地假数据。",
                }
            ],
        }

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        return [{"title": f"{code} 公告", "date": "2026-07-04"}]


def test_source_router_query_returns_research_engine_shape() -> None:
    router = SourceRouter(registry=_FakeRegistry())  # type: ignore[arg-type]

    result = router.query("semantic_search", query="中国西电 特高压", limit=2).to_dict()

    assert result["status"] == "ok"
    assert result["events"][0]["symbol"] == "601179"
    assert result["selected_provider"] == "fake_semantic"
    assert result["source_chain"] == ["semantic_search"]


def test_source_router_supports_announcement_capability() -> None:
    router = SourceRouter(registry=_FakeRegistry())  # type: ignore[arg-type]

    result = router.query("announcements", symbols=["601179"], limit=2).to_dict()

    assert result["status"] == "ok"
    assert result["events"][0]["title"] == "601179 公告"


def test_source_router_status_lists_legacy_market_routes() -> None:
    router = SourceRouter(registry=_FakeRegistry())  # type: ignore[arg-type]

    routes = router.status(check_health=False)["routes"]

    assert routes["kline"] == ["unified_data_query.daily"]
    assert routes["quote"] == ["provider_registry.realtime"]


def test_source_router_daily_queries_are_bounded_and_keep_symbol_order() -> None:
    class _FakeQuery:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def get_stock_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.01)
                return pd.DataFrame({"close": [1.0], "symbol": [symbol]})
            finally:
                with self.lock:
                    self.active -= 1

    router = SourceRouter(registry=_FakeRegistry())
    query = _FakeQuery()
    router.query_engine = query  # type: ignore[assignment]

    result = router.route("daily", symbols=["A", "B", "C"])

    assert result["status"] == "ok"
    assert result["data"]["symbol"].tolist() == ["A", "B", "C"]
    assert 1 < query.max_active <= 3

