from __future__ import annotations

import json
from types import SimpleNamespace

from core.runtime.research_monitor import ResearchMonitor


def test_theme_quant_uses_injected_production_repository(tmp_path, monkeypatch):
    quant_repository = object()
    captured: dict[str, object] = {}

    class FakeQuantPipeline:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_with_themes(self, packets, **kwargs):
            captured["packets"] = packets
            captured.update(kwargs)
            return "persisted-result"

    monkeypatch.setattr("core.quant.pipeline.QuantPipeline", FakeQuantPipeline)
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=object(),
        data_query=object(),
        quant_repository=quant_repository,
    )

    errors: list[str] = []
    result = monitor._run_theme_quant(
        [object()],
        daily_data_map={},
        as_of="2026-07-23T07:45:00+08:00",
        errors=errors,
    )

    assert result == "persisted-result"
    assert captured["repository"] is quant_repository
    assert errors == []


def test_previous_close_reader_finds_archived_daily_output(tmp_path):
    previous_dir = tmp_path / "2026-07-22"
    previous_dir.mkdir()
    expected = {
        "as_of": "2026-07-22T15:05:00+08:00",
        "close_watchlist": [{"symbol": "000001.SZ"}],
    }
    (previous_dir / "close_close_20260722_abcd1234.json").write_text(
        json.dumps(expected),
        encoding="utf-8",
    )
    monitor = ResearchMonitor(
        output_dir=tmp_path,
        research_repository=object(),
        data_query=object(),
    )

    assert monitor._read_previous_close_payload("2026-07-23") == expected


def test_quant_payload_exposes_persisted_signal_count():
    payload = ResearchMonitor._quant_payload(
        SimpleNamespace(
            signals=[],
            theme_screening_results=[],
            admission_results={},
            errors=[],
            degraded=False,
            saved_count=3,
        )
    )

    assert payload["saved_count"] == 3
