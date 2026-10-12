import pandas as pd

from server.pattern_lab.pattern_scanner import EVENT_SEQUENCE_COLUMNS, _event_sequence


def test_event_sequence_keeps_dates_prices_and_event_flags() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-01-01",
                "close": 10.0,
                "change_pct": 2.5,
                EVENT_SEQUENCE_COLUMNS[0]: True,
            },
            {
                "trade_date": "2026-01-02",
                "close": 10.2,
                "change_pct": -1.0,
                EVENT_SEQUENCE_COLUMNS[1]: True,
            },
        ]
    )

    assert _event_sequence(frame, 0, 2) == [
        {"date": "2026-01-01", "close": 10.0, "change_pct": 2.5, "events": [EVENT_SEQUENCE_COLUMNS[0]]},
        {"date": "2026-01-02", "close": 10.2, "change_pct": -1.0, "events": [EVENT_SEQUENCE_COLUMNS[1]]},
    ]
