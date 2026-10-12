from __future__ import annotations

from datetime import date

import lancedb
import polars as pl
import pyarrow as pa

from data_svc.storage.lancedb_reader import LanceDBDataReader


def test_read_all_returns_filtered_polars_frame_for_factor_jobs(tmp_path) -> None:
    db = lancedb.connect(str(tmp_path / "lancedb"))
    db.create_table(
        "daily_ohlcv",
        pa.table(
            {
                "stock_code": ["000001.SZ", "000001.SZ"],
                "trade_date": [date(2026, 7, 13), date(2026, 7, 14)],
                "close": [10.0, 10.5],
            }
        ),
    )

    frame = LanceDBDataReader(str(tmp_path / "lancedb")).read_all(
        start_date="2026-07-14",
        end_date="2026-07-14",
        fields=["stock_code", "trade_date", "close"],
    )

    assert isinstance(frame, pl.DataFrame)
    assert frame.get_column("trade_date").to_list() == ["2026-07-14"]
    assert frame.get_column("close").to_list() == [10.5]
