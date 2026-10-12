"""
聚宽 (JQData) 数据提供者。

使用 jqdatasdk 库获取日K线、股票基础信息等。
需要 JQ_USER / JQ_PASS 环境变量。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    DAILY_BARS_COLUMNS,
    STOCK_BASIC_INFO_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    normalize_symbols,
    to_numeric_series,
    pick_column,
)

logger = logging.getLogger(__name__)

# 尝试导入 jqdatasdk
_jq: Any = None
try:
    import jqdatasdk as _jq
except ImportError:
    pass


def _to_jq_code(code: str) -> str:
    """将股票代码转换为聚宽格式（如 600036.XSHG）。

    Args:
        code: 纯数字股票代码

    Returns:
        聚宽格式代码
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6"):
        return f"{code}.XSHG"
    return f"{code}.XSHE"


class JqdataProvider(BaseMarketDataProvider):
    """聚宽数据提供者，提供日K线和股票基础信息。"""

    name = "jqdata"

    def __init__(self) -> None:
        self._user = os.environ.get("JQ_USER", "")
        self._password = os.environ.get("JQ_PASS", "")
        self._authenticated = False

    def _auth(self) -> bool:
        """登录聚宽，成功返回 True。"""
        if _jq is None or not self._user or not self._password:
            return False
        if self._authenticated:
            return True
        try:
            _jq.auth(self._user, self._password)
            self._authenticated = _jq.is_auth()
            return self._authenticated
        except Exception as exc:
            logger.warning("聚宽登录失败: %s", exc)
            return False

    def is_available(self) -> bool:
        """检查 jqdatasdk 是否已安装且凭证已配置。"""
        return _jq is not None and bool(self._user) and bool(self._password)

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["daily_bars", "stock_basic_info"],
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

        if not self._auth():
            return empty_frame(DAILY_BARS_COLUMNS)

        try:
            jq_codes = [_to_jq_code(s) for s in symbols]
            df = _jq.get_price(
                jq_codes,
                start_date=start_date,
                end_date=end_date,
                frequency="daily",
                fields=["open", "close", "high", "low", "volume", "money"],
            )
            if df is None or df.empty:
                return empty_frame(DAILY_BARS_COLUMNS)

            # 聚宽返回的 index 为 DatetimeIndex，code 列为代码
            result = pd.DataFrame(index=df.index)
            result["trade_date"] = df.index.strftime("%Y-%m-%d") if hasattr(df.index, "strftime") else df.index.astype(str)
            # 从 code 列提取纯数字代码
            code_col = df["code"] if "code" in df.columns else pd.Series("", index=df.index)
            result["symbol"] = code_col.astype(str).str.replace(r"\.(XSHG|XSHE)$", "", regex=True)
            result["stock_name"] = ""
            result["open"] = to_numeric_series(df["open"])
            result["high"] = to_numeric_series(df["high"])
            result["low"] = to_numeric_series(df["low"])
            result["close"] = to_numeric_series(df["close"])
            result["pre_close"] = pd.NA
            result["pct_chg"] = pd.NA
            result["amount"] = to_numeric_series(df["money"])
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
            logger.warning("聚宽 get_daily_bars 失败: %s", exc)
            return empty_frame(DAILY_BARS_COLUMNS)

    # ----------------------------------------------------------
    # 基础信息
    # ----------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 get_fundamentals() 获取股票基础信息。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        if not self.is_available():
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        if not self._auth():
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            from jqdatasdk import query, valuation

            if not symbols:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            jq_codes = [_to_jq_code(s) for s in symbols]
            q = query(valuation).filter(valuation.code.in_(jq_codes))
            df = _jq.get_fundamentals(q)
            if df is None or df.empty:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = df["code"].astype(str).str.replace(r"\.(XSHG|XSHE)$", "", regex=True) if "code" in df.columns else ""
            result["stock_name"] = ""
            result["industry"] = ""
            result["area"] = ""
            result["list_date"] = ""
            result["business_scope"] = ""
            result["main_business"] = ""
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, STOCK_BASIC_INFO_COLUMNS)
        except Exception as exc:
            logger.warning("聚宽 get_stock_basic_info 失败: %s", exc)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        """聚宽不支持语义搜索。

        Args:
            query: 自然语言查询
            count: 返回条数

        Returns:
            错误提示
        """
        return {
            "source": self.name,
            "query": query,
            "results": [],
            "error": "聚宽(JQData)不支持语义搜索",
        }
