import pandas as pd

from config.config import Config
from core.quant.portfolio.position_history_manager import (
    PositionHistory,
    PositionHistoryManager,
)
from core.quant.portfolio.position_manager import Position, PositionManager


def test_add_position_appends_one_record_and_keeps_legacy_columns(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Config, "PARQUET_DIR", str(tmp_path))
    manager = PositionManager()
    existing = pd.DataFrame(
        [{"id": 1, "stock_code": "000001", "stock_name": "旧记录", "is_active": 1}]
    )
    saved = {}
    manager._get_positions_df = lambda: existing.copy()
    manager._save_positions_df = lambda frame: saved.setdefault("frame", frame.copy())

    new_id = manager.add_position(
        Position(stock_code="600000", stock_name="新记录", quantity=100, cost=1234)
    )

    result = saved["frame"]
    assert new_id == 2
    assert len(result) == 2
    assert set(result["stock_code"]) == {"000001", "600000"}
    assert "quantity" in result.columns


def test_add_history_appends_one_record_and_keeps_legacy_columns(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Config, "PARQUET_DIR", str(tmp_path))
    manager = PositionHistoryManager()
    existing = pd.DataFrame(
        [{"id": 1, "position_id": 10, "stock_code": "000001", "action": "buy"}]
    )
    saved = {}
    manager._get_history_df = lambda: existing.copy()
    manager._save_history_df = lambda frame: saved.setdefault("frame", frame.copy())

    new_id = manager.add_history(
        PositionHistory(position_id=11, stock_code="600000", action="add", amount=50)
    )

    result = saved["frame"]
    assert new_id == 2
    assert len(result) == 2
    assert set(result["stock_code"]) == {"000001", "600000"}
    assert "created_at" in result.columns
