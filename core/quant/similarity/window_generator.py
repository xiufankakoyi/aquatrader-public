"""
滑动窗口批量生成模块

对多只股票的历史K线数据按固定窗口大小生成滑动窗口片段，
供相似度匹配引擎使用。使用NumPy strided操作优化性能。
"""

import numpy as np
import polars as pl
from loguru import logger

from .normalizer import normalize_kline


def generate_sliding_windows(
    df: pl.DataFrame,
    window_size: int,
    symbol_col: str = "stock_code",
    date_col: str = "trade_date",
    close_col: str = "close",
    volume_col: str = "volume",
) -> list[dict]:
    """
    对Polars DataFrame生成滑动窗口片段

    按股票分组，对每只股票按日期排序后生成滑动窗口，
    每个窗口包含归一化收盘价序列和元数据。

    Args:
        df: 包含多只股票K线数据的Polars DataFrame
        window_size: 滑动窗口大小
        symbol_col: 股票代码列名
        date_col: 交易日期列名
        close_col: 收盘价列名
        volume_col: 成交量列名

    Returns:
        窗口字典列表，每个dict包含:
        - "normalized": 归一化收盘价序列 (np.ndarray)
        - "close": 原始收盘价序列 (np.ndarray)
        - "volume": 成交量序列 (np.ndarray)
        - "metadata": {"stock_code", "start_date", "end_date"}
    """
    if window_size < 2:
        raise ValueError(f"window_size must be >= 2, got {window_size}")

    if df.is_empty():
        return []

    required_cols = {symbol_col, date_col, close_col, volume_col}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    symbols = df[symbol_col].unique().to_list()
    windows = []

    for symbol in symbols:
        symbol_df = df.filter(pl.col(symbol_col) == symbol).sort(date_col)

        n_rows = symbol_df.height
        if n_rows < window_size:
            continue

        close_arr = symbol_df[close_col].cast(pl.Float64).to_numpy()
        volume_arr = symbol_df[volume_col].cast(pl.Float64).to_numpy()
        date_arr = symbol_df[date_col].to_numpy()

        n_windows = n_rows - window_size + 1

        close_strided = _sliding_window_view(close_arr, window_size)
        volume_strided = _sliding_window_view(volume_arr, window_size)

        for i in range(n_windows):
            window_close = close_strided[i]
            window_volume = volume_strided[i]
            normalized = normalize_kline(window_close)

            windows.append({
                "normalized": normalized,
                "close": window_close.copy(),
                "volume": window_volume.copy(),
                "metadata": {
                    "stock_code": symbol,
                    "start_date": str(date_arr[i]),
                    "end_date": str(date_arr[i + window_size - 1]),
                },
            })

    logger.debug(
        f"Generated {len(windows)} sliding windows "
        f"(window_size={window_size}, symbols={len(symbols)})"
    )

    return windows


def _sliding_window_view(arr: np.ndarray, window_size: int) -> np.ndarray:
    """
    使用NumPy strided操作生成滑动窗口视图

    Args:
        arr: 一维数组
        window_size: 窗口大小

    Returns:
        (n_windows, window_size) 形状的二维数组视图
    """
    return np.lib.stride_tricks.sliding_window_view(arr, window_size)
