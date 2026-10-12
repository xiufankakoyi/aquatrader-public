from __future__ import annotations

import json

from core.research.control_plane.learning_bar_reader import (
    ForwardObservationBarReader,
)


class StubQuery:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_stock_history(self, symbol, start, end, columns=None):
        self.calls.append((symbol, start, end, columns))
        return list(self.rows)


def _write(root, records):
    root.mkdir()
    (root / "observations.json").write_text(
        json.dumps(records), encoding="utf-8"
    )


def _record(
    trade_date="2026-07-02",
    available_at="2026-07-02T15:10:00+08:00",
    record_hash="a" * 64,
    benchmark_return=-0.01,
    benchmark_fingerprint="b" * 64,
):
    return {
        "trade_date": trade_date,
        "available_at": available_at,
        "record_hash": record_hash,
        "benchmark": {
            "return": benchmark_return,
            "fingerprint": benchmark_fingerprint,
        },
    }


def test_reads_earliest_eligible_trade_date_with_forward_lineage(tmp_path):
    root = tmp_path / "forward"
    _write(
        root,
        [
            _record(),
            _record(
                trade_date="2026-07-03",
                available_at="2026-07-03T15:10:00+08:00",
                record_hash="c" * 64,
                benchmark_fingerprint="d" * 64,
            ),
        ],
    )
    query = StubQuery(
        [
            {
                "trade_date": "2026-07-02",
                "close": 101.0,
                "prev_close": 100.0,
            }
        ]
    )

    bar = ForwardObservationBarReader(root, query=query).first_bar_after(
        "600519.SH",
        after_date="2026-07-01",
        as_of="2026-07-03T16:00:00+08:00",
    )

    assert bar is not None
    assert bar.trade_date == "2026-07-02"
    assert bar.available_at == "2026-07-02T15:10:00+08:00"
    assert bar.close == 101.0
    assert bar.previous_close == 100.0
    assert bar.benchmark_return == -0.01
    assert bar.price_available_at_ref == f"forward_observation:{'a' * 64}"
    assert bar.benchmark_source_ref == f"forward_benchmark:{'b' * 64}"
    assert query.calls[0][:3] == ("600519.SH", "2026-07-02", "2026-07-02")


def test_future_observation_is_not_eligible(tmp_path):
    root = tmp_path / "forward"
    _write(root, [_record(available_at="2026-07-04T15:10:00+08:00")])
    query = StubQuery(
        [{"trade_date": "2026-07-02", "close": 101.0, "prev_close": 100.0}]
    )

    bar = ForwardObservationBarReader(root, query=query).first_bar_after(
        "600519.SH",
        after_date="2026-07-01",
        as_of="2026-07-03T16:00:00+08:00",
    )

    assert bar is None
    assert query.calls == []


def test_missing_forward_available_at_is_never_inferred(tmp_path):
    root = tmp_path / "forward"
    _write(root, [_record(available_at="")])
    query = StubQuery(
        [{"trade_date": "2026-07-02", "close": 101.0, "prev_close": 100.0}]
    )

    bar = ForwardObservationBarReader(root, query=query).first_bar_after(
        "600519.SH",
        after_date="2026-07-01",
        as_of="2026-07-03T16:00:00+08:00",
    )

    assert bar is None
    assert query.calls == []


def test_missing_hash_benchmark_or_price_field_returns_none(tmp_path):
    cases = [
        (_record(record_hash=""), {"close": 101.0, "prev_close": 100.0}),
        (
            _record(benchmark_fingerprint=""),
            {"close": 101.0, "prev_close": 100.0},
        ),
        (_record(benchmark_return=None), {"close": 101.0, "prev_close": 100.0}),
        (_record(), {"close": 101.0}),
    ]
    for index, (record, prices) in enumerate(cases):
        root = tmp_path / f"forward-{index}"
        _write(root, [record])
        query = StubQuery([{"trade_date": "2026-07-02", **prices}])

        assert (
            ForwardObservationBarReader(root, query=query).first_bar_after(
                "600519.SH",
                after_date="2026-07-01",
                as_of="2026-07-03T16:00:00+08:00",
            )
            is None
        )
