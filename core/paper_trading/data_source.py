"""A-share market data source for paper trading.

Primary backend is baostock (stable in the current network environment).
akshare is kept as a fallback for redundancy. The interface is isolated so we
can swap to tushare later without touching portfolio logic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from core.research_graph.models import utc_now

logger = logging.getLogger(__name__)

# Optional backends; load lazily so missing packages do not crash import.
try:
    import baostock as bs
except Exception:  # pragma: no cover
    bs = None  # type: ignore[assignment]

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None  # type: ignore[assignment]


@dataclass
class DailyBar:
    """Single daily bar for a stock."""

    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    prev_close: float = 0.0
    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False


class AkSharePriceSource:
    """Fetch A-share daily prices via akshare.

    All prices are forward-adjusted by akshare by default. For paper trading we
    use the 'open' price as the execution price on the target date.
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        """Initialize the price source.

        Args:
            cache_dir: Optional directory to cache price DataFrames.
        """
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._baostock_logged_in = False

    @staticmethod
    def _normalize_code(code: str) -> str:
        """Return 6-digit numeric code without exchange prefix."""
        return str(code).strip().split(".")[0][-6:]

    @staticmethod
    def _to_baostock_code(code: str) -> str | None:
        """Map a 6-digit numeric code to baostock exchange prefix."""
        code = AkSharePriceSource._normalize_code(code)
        if len(code) != 6 or not code.isdigit():
            return None
        first = code[0]
        first_two = code[:2]
        if first in ("6", "5") or first_two in ("68", "69"):
            return f"sh.{code}"
        if first in ("0", "3") or first_two in ("00", "30"):
            return f"sz.{code}"
        # Beijing Stock Exchange is not supported by baostock.
        return None

    def get_open_price(self, code: str, trade_date: str | None = None) -> float | None:
        """Get the opening price for a stock on a given date.

        Args:
            code: Stock code, e.g. "000001" or "sh000001".
            trade_date: Date in "YYYYMMDD" or "YYYY-MM-DD". Defaults to today.

        Returns:
            Opening price or None if unavailable.
        """
        code = self._normalize_code(code)
        target_date = self._normalize_date(trade_date or utc_now()[:10])

        df = self._load_history(code)
        if df is None or df.empty:
            return None

        # akshare stock_zh_a_hist returns columns like: 日期,开盘,收盘,最高,最低,成交量,成交额...
        date_col = self._find_date_column(df)
        if date_col is None:
            logger.warning("No date column found for %s", code)
            return None

        df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
        row = df[df[date_col] == target_date]
        if row.empty:
            logger.warning("No price data for %s on %s", code, target_date)
            return None

        open_col = self._find_open_column(df)
        if open_col is None:
            logger.warning("No open column found for %s", code)
            return None

        return float(row.iloc[0][open_col])

    def get_daily_bar(self, code: str, trade_date: str | None = None) -> DailyBar | None:
        """Get the full daily bar for a stock on a given date."""
        code = self._normalize_code(code)
        target_date = self._normalize_date(trade_date or utc_now()[:10])

        df = self._load_history(code)
        if df is None or df.empty:
            return None

        date_col = self._find_date_column(df)
        if date_col is None:
            return None

        df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
        row = df[df[date_col] == target_date]
        if row.empty:
            return None

        def col(*names: str) -> str | None:
            for n in names:
                if n in df.columns:
                    return n
            return None

        prev_close_col = col("昨收", "preclose", "prev_close")
        prev_close = float(row.iloc[0][prev_close_col]) if prev_close_col else 0.0
        open_price = float(row.iloc[0][col("开盘", "open", "Open") or df.columns[1]])
        volume = float(row.iloc[0][col("成交量", "volume", "Volume") or df.columns[5]])
        trade_status_col = col("tradestatus", "交易状态")
        is_suspended = volume <= 0 or (trade_status_col is not None and str(row.iloc[0][trade_status_col]) in {"0", "停牌"})
        limit_ratio = 0.20 if code.startswith(("30", "68")) else 0.10
        return DailyBar(
            code=code,
            date=target_date,
            open=open_price,
            high=float(row.iloc[0][col("最高", "high", "High") or df.columns[2]]),
            low=float(row.iloc[0][col("最低", "low", "Low") or df.columns[3]]),
            close=float(row.iloc[0][col("收盘", "close", "Close") or df.columns[4]]),
            volume=volume,
            amount=float(row.iloc[0][col("成交额", "amount", "Amount") or df.columns[6]]),
            prev_close=prev_close,
            is_suspended=is_suspended,
            is_limit_up=bool(prev_close > 0 and open_price >= prev_close * (1 + limit_ratio - 0.002)),
            is_limit_down=bool(prev_close > 0 and open_price <= prev_close * (1 - limit_ratio + 0.002)),
        )

    def _load_history(self, code: str) -> pd.DataFrame | None:
        """Load historical daily data, using file cache if configured."""
        normalized = self._normalize_code(code)
        cache_path: Path | None = None
        if self._cache_dir:
            cache_path = self._cache_dir / f"{normalized}.parquet"
            if cache_path.exists():
                try:
                    return pd.read_parquet(cache_path)
                except Exception as exc:
                    logger.debug("Cache read failed for %s: %s", normalized, exc)

        # Try baostock first; it is stable in the current network environment.
        df = self._load_history_from_baostock(normalized)
        if df is not None and not df.empty:
            if cache_path:
                df.to_parquet(cache_path, index=False)
            return df

        # Fallback to akshare when baostock is unavailable.
        df = self._load_history_from_akshare(normalized)
        if df is not None and not df.empty:
            if cache_path:
                df.to_parquet(cache_path, index=False)
            return df
        return None

    def _load_history_from_baostock(self, code: str) -> pd.DataFrame | None:
        """Fetch daily bars from baostock."""
        if bs is None:
            return None
        bs_code = self._to_baostock_code(code)
        if bs_code is None:
            return None
        if not self._baostock_logged_in:
            login = bs.login()
            if login.error_code != "0":
                logger.warning("baostock login failed: %s", login.error_msg)
                return None
            self._baostock_logged_in = True

        try:
            end_date = datetime.now().strftime("%Y-%m-%d")
            # Use a wide start date so historical trade dates are covered once cached.
            start_date = "2015-01-01"
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,preclose,tradestatus,pctChg,isST",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",  # forward-adjusted
            )
            if rs.error_code != "0":
                logger.warning("baostock query failed for %s: %s", bs_code, rs.error_msg)
                return None
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None
            df = pd.DataFrame(
                rows,
                columns=["date", "open", "high", "low", "close", "volume", "amount", "preclose", "tradestatus", "pctChg", "isST"],
            )
            for col in ("open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as exc:
            logger.warning("baostock fetch failed for %s: %s", bs_code, exc)
            return None

    def _load_history_from_akshare(self, code: str) -> pd.DataFrame | None:
        """Fetch daily bars from akshare as a fallback."""
        if ak is None:
            return None
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - pd.Timedelta(days=90)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
            if df is None or df.empty:
                return None
            return df
        except Exception as exc:
            logger.warning("akshare price fetch failed for %s: %s", code, exc)
            return None

    @staticmethod
    def _normalize_date(date_str: str) -> str:
        """Normalize date string to 'YYYY-MM-DD'."""
        s = str(date_str).replace("-", "").replace("/", "")
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return date_str

    @staticmethod
    def _find_date_column(df: pd.DataFrame) -> str | None:
        for name in ("日期", "date", "Date", "trade_date", "datetime"):
            if name in df.columns:
                return name
        return None

    @staticmethod
    def _find_open_column(df: pd.DataFrame) -> str | None:
        for name in ("开盘", "open", "Open"):
            if name in df.columns:
                return name
        return None

    def get_volume_stats(
        self,
        code: str,
        as_of: str,
        window: int = 20,
    ) -> dict[str, float]:
        """返回截至 as_of 的成交量统计。

        利用已有历史数据计算多日均量，用于回测时设定成交量上限，
        避免使用单日全天量（未来函数）。

        Args:
            code: 股票代码。
            as_of: 截止日期（YYYY-MM-DD）。
            window: 均量窗口（默认 20 个交易日）。

        Returns:
            包含 avg_volume_20d/max_volume_20d/turnover_ratio 的字典。
        """
        empty = {"avg_volume_20d": 0.0, "max_volume_20d": 0.0, "turnover_ratio": 0.0}
        cutoff = self._normalize_date(as_of)
        df = self._load_history(code)
        if df is None or df.empty:
            return empty
        date_col = self._find_date_column(df)
        if date_col is None:
            return empty
        # 统一日期格式为 YYYY-MM-DD 便于字符串比较
        df = df.copy()
        df[date_col] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
        visible = df[df[date_col] <= cutoff]
        if visible.empty:
            return empty
        recent = visible.tail(window)
        # 兼容 baostock (英文列名) 与 akshare (中文列名) 两种来源
        vol_col = next(
            (name for name in ("成交量", "volume", "Volume") if name in recent.columns),
            None,
        )
        amt_col = next(
            (name for name in ("成交额", "amount", "Amount") if name in recent.columns),
            None,
        )
        volumes = (
            pd.to_numeric(recent[vol_col], errors="coerce").fillna(0.0).astype(float).tolist()
            if vol_col else []
        )
        amounts = (
            pd.to_numeric(recent[amt_col], errors="coerce").fillna(0.0).astype(float).tolist()
            if amt_col else []
        )
        avg_volume = sum(volumes) / len(volumes) if volumes else 0.0
        max_volume = max(volumes) if volumes else 0.0
        avg_amount = sum(amounts) / len(amounts) if amounts else 0.0
        turnover_ratio = avg_amount / (avg_volume * 100) if avg_volume > 0 else 0.0
        return {
            "avg_volume_20d": avg_volume,
            "max_volume_20d": max_volume,
            "turnover_ratio": turnover_ratio,
        }
