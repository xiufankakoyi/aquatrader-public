from __future__ import annotations

from argparse import Namespace

import pandas as pd

from scripts.research_data import execute


class _Router:
    def status(self, check_health: bool = False):
        return {"routes": {"kline": ["local"]}, "health_checked": check_health}

    def route(self, data_type, symbols, start_date=None, end_date=None):
        assert data_type == "daily"
        return {
            "status": "ok",
            "data": pd.DataFrame([
                {"symbol": symbols[0], "trade_date": "2026-07-23", "close": 12.3}
            ]),
        }

    def query(self, capability, **kwargs):
        return type("Result", (), {
            "to_dict": lambda self: {
                "status": "ok",
                "events": [{"event_type": capability}],
                "selected_provider": "fake",
                "source_chain": [capability],
                "errors": [],
            }
        })()


def test_status_has_stable_top_level_contract():
    result = execute(Namespace(command="status", no_health=True), _Router())

    assert result["status"] == "ok"
    assert result["selected_provider"] == ""
    assert result["attempts"] == []
    assert result["events"][0]["routes"] == {"kline": ["local"]}


def test_kline_serializes_dataframe_and_records_attempt():
    args = Namespace(
        command="kline",
        symbols=["000001.SZ"],
        start_date="2026-07-01",
        end_date="2026-07-23",
        limit=20,
        keyword="",
        query="",
    )

    result = execute(args, _Router())

    assert result["status"] == "ok"
    assert result["selected_provider"] == "unified_data_query"
    assert result["attempts"] == ["unified_data_query.daily"]
    assert result["events"][0]["trade_date"] == "2026-07-23"


def test_research_capability_preserves_router_errors_without_crashing():
    args = Namespace(
        command="announce",
        symbols=["600000.SH"],
        start_date="",
        end_date="",
        limit=10,
        keyword="",
        query="",
    )

    result = execute(args, _Router())

    assert result["status"] == "ok"
    assert result["selected_provider"] == "fake"
    assert result["attempts"] == ["announcements"]

