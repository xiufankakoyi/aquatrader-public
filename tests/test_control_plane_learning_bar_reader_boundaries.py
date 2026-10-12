from __future__ import annotations

import json

from core.research.control_plane.learning_bar_reader import ForwardObservationBarReader


class _Query:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def get_stock_history(self, symbol, start, end, columns=None):
        self.calls.append((symbol, start, end))
        return self.frame


class _PolarsLikeFrame:
    def to_dicts(self):
        return [{"trade_date": "2026-07-02", "close": 101, "prev_close": 100}]


def _write(root, records):
    root.mkdir()
    (root / "observations.json").write_text(json.dumps(records), encoding="utf-8")


def _record(trade_date, record_hash):
    return {
        "trade_date": trade_date,
        "available_at": f"{trade_date}T15:10:00+08:00",
        "record_hash": record_hash,
        "benchmark": {"return": 0.01, "fingerprint": "b" * 64},
    }


def test_invalid_earliest_reference_is_skipped_for_next_valid_date(tmp_path):
    root = tmp_path / "forward"
    _write(root, [_record("2026-07-02", "bad"), _record("2026-07-03", "a" * 64)])

    bar = ForwardObservationBarReader(
        root,
        query=_Query([{"trade_date": "2026-07-03", "close": 101, "prev_close": 100}]),
    ).first_bar_after(
        "600519.SH", after_date="2026-07-01", as_of="2026-07-04T16:00:00+08:00"
    )

    assert bar is not None
    assert bar.trade_date == "2026-07-03"


def test_malformed_reference_and_symbol_degrade_without_query_call(tmp_path):
    root = tmp_path / "forward"
    _write(root, [{"trade_date": "2026-07-02", "available_at": "bad"}, {"bad": True}])
    query = _Query([])

    assert ForwardObservationBarReader(root, query=query).first_bar_after(
        None, after_date="2026-07-01", as_of="2026-07-03T16:00:00+08:00"
    ) is None
    assert query.calls == []


def test_polars_style_to_dicts_frame_is_supported(tmp_path):
    root = tmp_path / "forward"
    _write(root, [_record("2026-07-02", "a" * 64)])

    bar = ForwardObservationBarReader(
        root, query=_Query(_PolarsLikeFrame())
    ).first_bar_after(
        "600519.SH", after_date="2026-07-01", as_of="2026-07-03T16:00:00+08:00"
    )

    assert bar is not None
    assert bar.close == 101.0
