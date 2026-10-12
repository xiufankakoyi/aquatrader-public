from __future__ import annotations

import pytest

from core.research.evidence import Evidence, EvidenceCenter


def test_evidence_normalizes_lists_and_quality() -> None:
    evidence = Evidence(
        evidence_id="e1",
        trade_date="2026-07-31",
        entity_type="stock",
        entity_id="000001",
        entity_name="平安银行",
        source_type="local",
        source_name="test",
        title="title",
        summary="summary",
        symbols=("000001", 1),
        themes="invalid",
        industries=["银行", None],
        quality_score=float("nan"),
    )

    assert evidence.symbols == ["000001"]
    assert evidence.themes == []
    assert evidence.industries == ["银行"]
    assert evidence.quality_score == 0.0


def test_event_bus_subscription_is_idempotent(tmp_path) -> None:
    center = EvidenceCenter(tmp_path)

    class Bus:
        def __init__(self) -> None:
            self.subscriptions = []

        def subscribe(self, topic, handler) -> None:
            self.subscriptions.append((topic, handler))

    bus = Bus()
    center.collect_from_event_bus(bus)
    center.collect_from_event_bus(bus)
    center.collect_from_event_bus(object())

    assert len(bus.subscriptions) == 2


def test_collectors_skip_malformed_rows_and_export_is_confined(tmp_path) -> None:
    center = EvidenceCenter(tmp_path)

    center.collect_from_dragon_eye(
        {"evidence": "invalid", "limit_up_count": "not-a-number"}, "2026-07-31"
    )
    center.collect_from_stock_screener(
        {
            "no_candidates": False,
            "candidates": [None, {"symbol": "000001", "return_5d": "bad"}],
        },
        "2026-07-31",
    )
    center.collect_from_strategy_signals(
        {"buy": [None, {"stock_code": "000001", "signal_strength": "bad"}]},
        "2026-07-31",
    )

    assert len(center.evidences) == 2
    assert center.export("evidence.json")["total"] == 2
    with pytest.raises(ValueError):
        center.export("../outside.json")
