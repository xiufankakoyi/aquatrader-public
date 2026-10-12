"""
米筐 (RiceQuant) 数据提供者。

使用 rqdatac 库获取日K线数据。
需要 RQ_USER / RQ_PASS 环境变量。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    DAILY_BARS_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    to_numeric_series,
)

logger = logging.getLogger(__name__)

# 尝试导入 rqdatac
_rq: Any = None
try:
    import rqdatac as _rq
except ImportError:
    pass


def _to_rq_code(code: str) -> str:
    """将股票代码转换为米筐格式（如 600036.XSHG）。

    Args:
        code: 纯数字股票代码

    Returns:
        米筐格式代码
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6"):
        return f"{code}.XSHG"
    return f"{code}.XSHE"


class RicequantProvider(BaseMarketDataProvider):
    """米筐数据提供者，提供日K线数据。"""

    name = "ricequant"

    def __init__(self) -> None:
        self._user = os.environ.get("RQ_USER", "")
        self._password = os.environ.get("RQ_PASS", "")
        self._initialized = False

    def _init(self) -> bool:
        """初始化米筐连接，成功返回 True。"""
        if _rq is None or not self._user or not self._password:
            return False
        if self._initialized:
            return True
        try:
            _rq.init(self._user, self._password)
            self._initialized = True
            return True
        except Exception as exc:
            logger.warning("米筐初始化失败: %s", exc)
            return False

    def is_available(self) -> bool:
        """检查 rqdatac 是否已安装且凭证已配置。"""
        return _rq is not None and bool(self._user) and bool(self._password)

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["daily_bars"],
        }

    # ----------------------------------------------------------
    # 日K线
    # ----------------------------------------------------------

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 get_price() 获取日K线。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(DAILY_BARS_COLUMNS)

        if not self._init():
            return empty_frame(DAILY_BARS_COLUMNS)

        try:
            rq_codes = [_to_rq_code(s) for s in symbols]
            df = _rq.get_price(
                rq_codes,
                start_date=start_date,
                end_date=end_date,
                frequency="1d",
                fields=["open", "close", "high", "low", "volume", "total_turnover"],
            )
            if df is None or df.empty:
                return empty_frame(DAILY_BARS_COLUMNS)

            result = pd.DataFrame(index=df.index)
            # 米筐返回的 index 为 DatetimeIndex
            result["trade_date"] = df.index.strftime("%Y-%m-%d") if hasattr(df.index, "strftime") else df.index.astype(str)
            # 从 order_book_id 列提取纯数字代码
            code_col = df["order_book_id"] if "order_book_id" in df.columns else pd.Series("", index=df.index)
            result["symbol"] = code_col.astype(str).str.replace(r"\.(XSHG|XSHE)$", "", regex=True)
            result["stock_name"] = ""
            result["open"] = to_numeric_series(df["open"])
            result["high"] = to_numeric_series(df["high"])
            result["low"] = to_numeric_series(df["low"])
            result["close"] = to_numeric_series(df["close"])
            result["pre_close"] = pd.NA
            result["pct_chg"] = pd.NA
            result["amount"] = to_numeric_series(df["total_turnover"]) if "total_turnover" in df.columns else pd.NA
            result["volume"] = to_numeric_series(df["volume"])
            result["turnover_rate"] = pd.NA
            result["volume_ratio"] = pd.NA
            result["total_market_cap"] = pd.NA
            result["float_market_cap"] = pd.NA
            result["provider"] = self.name
            result["updated_at"] = now_text()

            # 计算 pct_chg
            result = result.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
            pre_close = result.groupby("symbol")["close"].shift(1)
            result["pre_close"] = pre_close
            close = to_numeric_series(result["close"])
            pre_close_num = to_numeric_series(result["pre_close"])
            result["pct_chg"] = ((close - pre_close_num) / pre_close_num * 100).round(2)

            return ensure_columns(result, DAILY_BARS_COLUMNS)
        except Exception as exc:
            logger.warning("米筐 get_daily_bars 失败: %s", exc)
            return empty_frame(DAILY_BARS_COLUMNS)
