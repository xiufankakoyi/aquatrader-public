"""Compatibility facade for the current LanceDB-backed unified query layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd
import polars as pl
from loguru import logger

from data_svc.unified_data_query import UnifiedDataQuery


@dataclass
class WriteResult:
    success: bool
    symbol: str
    rows: int = 0
    version: Optional[int] = None
    error: Optional[str] = None
    elapsed_ms: float = 0.0


class UnifiedDataManager:
    """Keep legacy readers working while delegating to ``UnifiedDataQuery``."""

    HOT_DATA_YEARS = 2

    def __init__(
        self,
        db_path: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._query = UnifiedDataQuery(db_path=db_path)
        self.progress_callback = progress_callback
        self._memory_cache: Dict[str, Dict[str, pl.DataFrame]] = {
            "daily": {},
            "stock_daily": {},
        }
        self._cache_loaded = False
        self._preloaded_date_range: Optional[tuple[str, str]] = None

    def read(
        self,
        library: str,
        symbol: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        use_cache: bool = True,
        fields: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        if library not in {"daily", "stock_daily"}:
            logger.warning("[UnifiedDataManager] unsupported library: {}", library)
            return pl.DataFrame()

        cache_key = f"daily_{start_date}_{end_date}"
        if use_cache:
            cached = self._memory_cache["daily"].get(cache_key)
            if cached is not None:
                return cached.clone()

        frame = self._query._read_polars(
            symbol=symbol,
            start=start_date,
            end=end_date,
            columns=fields,
            table_name="daily_ohlcv",
        )
        if use_cache and not frame.is_empty():
            self._memory_cache["daily"][cache_key] = frame.clone()
            self._memory_cache["stock_daily"][cache_key] = frame.clone()
        return frame

    def preload_to_memory(
        self,
        years: Optional[float] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pl.DataFrame]:
        period_years = years if years is not None else self.HOT_DATA_YEARS
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = start_date or (
            datetime.now() - timedelta(days=max(1, int(period_years * 365)))
        ).strftime("%Y-%m-%d")

        frame = self.read(
            "daily",
            start_date=start,
            end_date=end,
            use_cache=False,
            fields=fields,
        )
        cache_key = f"daily_{start}_{end}"
        if not frame.is_empty():
            self._memory_cache["daily"][cache_key] = frame.clone()
            self._memory_cache["stock_daily"][cache_key] = frame.clone()
        self._preloaded_date_range = (start, end)
        self._cache_loaded = True
        return {"daily": frame, "stock_daily": frame}

    def get_preloaded_data(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, pl.DataFrame]:
        frames = list(self._memory_cache["daily"].values())
        if not frames:
            return {}
        frame = frames[-1].clone()
        date_col = "trade_date" if "trade_date" in frame.columns else "date"
        if date_col in frame.columns and (start_date or end_date):
            normalized = pl.col(date_col).cast(pl.String)
            if start_date:
                frame = frame.filter(normalized >= start_date)
            if end_date:
                frame = frame.filter(normalized <= end_date)
        return {"daily": frame, "stock_daily": frame}

    def get_trading_dates(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[str]:
        start = start_date or "1900-01-01"
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        return self._query.get_trading_dates(start, end)

    def get_stock_pool(
        self,
        date_str: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> pl.DataFrame:
        frame = self._query.get_stock_pool_pl(date_str)
        for column, condition in (filters or {}).items():
            if column not in frame.columns:
                continue
            if isinstance(condition, tuple):
                frame = frame.filter(pl.col(column).is_between(*condition))
            elif isinstance(condition, list):
                frame = frame.filter(pl.col(column).is_in(condition))
            else:
                frame = frame.filter(pl.col(column) == condition)
        return frame

    def get_stock_pool_at_time(
        self,
        timestamp: Union[str, datetime, pd.Timestamp],
    ) -> pd.DataFrame:
        date_str = timestamp.strftime("%Y-%m-%d") if hasattr(timestamp, "strftime") else str(timestamp)[:10]
        frame = self.get_stock_pool(date_str)
        return frame.to_pandas() if not frame.is_empty() else pd.DataFrame()

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self._query.get_db_stats(),
            "cache_loaded": self._cache_loaded,
            "preloaded_date_range": self._preloaded_date_range,
        }

    def clear_memory_cache(self) -> None:
        self._memory_cache = {"daily": {}, "stock_daily": {}}
        self._cache_loaded = False
        self._preloaded_date_range = None


_global_manager: Optional[UnifiedDataManager] = None


def get_unified_manager(**kwargs: Any) -> UnifiedDataManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = UnifiedDataManager(**kwargs)
    return _global_manager
