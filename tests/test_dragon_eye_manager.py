from __future__ import annotations

import pandas as pd

from core.research.dragon_eye.manager import (
    DragonEyeManager,
    _FallbackDataManager,
    _frame_is_empty,
)


def test_fallback_upsert_uses_polars_for_parquet_merge(
    tmp_path, monkeypatch
) -> None:
    manager = _FallbackDataManager(str(tmp_path), table_name="dragon_stock")
    manager.upsert_daily_data(
        pd.DataFrame(
            [
                {"_id": "a", "trade_date": "2026-07-01", "value": 1},
                {"_id": "b", "trade_date": "2026-07-02", "value": 2},
            ]
        )
    )

    def pandas_path_removed(*args, **kwargs):
        raise AssertionError("fallback should not merge through pandas")

    monkeypatch.setattr(pd, "concat", pandas_path_removed)
    monkeypatch.setattr(pd, "read_parquet", pandas_path_removed)
    manager.upsert_daily_data(
        pd.DataFrame([{"_id": "b", "trade_date": "2026-07-02", "value": 3}])
    )

    loaded = manager.load_to_polars(
        start_date="2026-07-02", end_date="2026-07-02"
    )
    assert loaded.height == 1
    assert loaded.item(0, "_id") == "b"
    assert loaded.item(0, "value") == 3


def test_manager_degrades_invalid_or_incomplete_frames(tmp_path) -> None:
    assert _frame_is_empty(None)
    manager = DragonEyeManager.__new__(DragonEyeManager)
    manager.lancedb_mgr = None
    manager.stock_mgr = _FallbackDataManager(str(tmp_path), table_name="dragon_stock")
    manager.sentiment_mgr = _FallbackDataManager(
        str(tmp_path), table_name="market_sentiment"
    )

    manager.upsert_stocks(pd.DataFrame([{"value": 1}]))
    manager.upsert_sentiment(pd.DataFrame([{"value": 1}]))

    assert not (tmp_path / "dragon_stock.parquet").exists()
    assert not (tmp_path / "market_sentiment.parquet").exists()
