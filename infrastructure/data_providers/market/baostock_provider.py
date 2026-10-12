"""
宝塔数据 (BaoStock) 数据提供者。

使用 baostock 库获取日K线和股票基础信息。
免费开源，无需注册。
"""

from __future__ import annotations

import logging
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
)

logger = logging.getLogger(__name__)

# 尝试导入 baostock
_bs: Any = None
try:
    import baostock as _bs
except ImportError:
    pass


def _to_bs_code(code: str) -> str:
    """将股票代码转换为 BaoStock 格式（如 sh.600036）。

    Args:
        code: 纯数字股票代码

    Returns:
        BaoStock 格式代码
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6"):
        return f"sh.{code}"
    return f"sz.{code}"


class BaostockProvider(BaseMarketDataProvider):
    """宝塔数据提供者，提供日K线和股票基础信息。"""

    name = "baostock"

    def is_available(self) -> bool:
        """检查 baostock 是否已安装。"""
        return _bs is not None

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
        """通过 bs.query_history_k_data_plus() 获取日K线。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(DAILY_BARS_COLUMNS)

        try:
            _bs.login()
        except Exception as exc:
            logger.warning("BaoStock login 失败: %s", exc)
            return empty_frame(DAILY_BARS_COLUMNS)

        try:
            all_rows: list[pd.DataFrame] = []
            for sym in symbols:
                bs_code = _to_bs_code(sym)
                try:
                    rs = _bs.query_history_k_data_plus(
                        code=bs_code,
                        fields="date,code,open,high,low,close,preclose,pctChg,turn,volume,amount",
                        start_date=start_date.replace("-", ""),
                        end_date=end_date.replace("-", ""),
                        frequency="d",
                        adjustflag="2",
                    )

                    data = []
                    while rs.error_code == "0" and rs.next():
                        data.append(rs.get_row_data())

                    if not data:
                        continue

                    fields = rs.fields
                    df = pd.DataFrame(data, columns=fields)

                    result = pd.DataFrame(index=df.index)
                    result["trade_date"] = df["date"].astype(str) if "date" in df.columns else ""
                    result["symbol"] = df["code"].astype(str).str.replace(r"^(sh|sz)\.", "", regex=True) if "code" in df.columns else sym
                    result["stock_name"] = ""
                    result["open"] = to_numeric_series(df["open"]) if "open" in df.columns else pd.NA
                    result["high"] = to_numeric_series(df["high"]) if "high" in df.columns else pd.NA
                    result["low"] = to_numeric_series(df["low"]) if "low" in df.columns else pd.NA
                    result["close"] = to_numeric_series(df["close"]) if "close" in df.columns else pd.NA
                    result["pre_close"] = to_numeric_series(df["preclose"]) if "preclose" in df.columns else pd.NA
                    result["pct_chg"] = to_numeric_series(df["pctChg"]) if "pctChg" in df.columns else pd.NA
                    result["amount"] = to_numeric_series(df["amount"]) if "amount" in df.columns else pd.NA
                    result["volume"] = to_numeric_series(df["volume"]) if "volume" in df.columns else pd.NA
                    result["turnover_rate"] = to_numeric_series(df["turn"]) if "turn" in df.columns else pd.NA
                    result["volume_ratio"] = pd.NA
                    result["total_market_cap"] = pd.NA
                    result["float_market_cap"] = pd.NA
                    result["provider"] = self.name
                    result["updated_at"] = now_text()
                    all_rows.append(ensure_columns(result, DAILY_BARS_COLUMNS))
                except Exception as exc:
                    logger.warning("BaoStock get_daily_bars(%s) 失败: %s", bs_code, exc)
                    continue

            if not all_rows:
                return empty_frame(DAILY_BARS_COLUMNS)
            return pd.concat(all_rows, ignore_index=True)
        finally:
            try:
                _bs.logout()
            except Exception:
                pass

    # ----------------------------------------------------------
    # 基础信息
    # ----------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 bs.query_stock_basic() 获取股票基础信息。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        if not self.is_available():
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            _bs.login()
        except Exception as exc:
            logger.warning("BaoStock login 失败: %s", exc)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            rs = _bs.query_stock_basic(
                code="",
                code_name="",
            )

            data = []
            while rs.error_code == "0" and rs.next():
                data.append(rs.get_row_data())

            if not data:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            fields = rs.fields
            df = pd.DataFrame(data, columns=fields)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = df["code"].astype(str).str.replace(r"^(sh|sz)\.", "", regex=True) if "code" in df.columns else ""
            result["stock_name"] = df["code_name"].astype(str) if "code_name" in df.columns else ""
            result["industry"] = ""
            result["area"] = ""
            result["list_date"] = df["ipoDate"].astype(str) if "ipoDate" in df.columns else ""
            result["business_scope"] = ""
            result["main_business"] = ""
            result["provider"] = self.name
            result["updated_at"] = now_text()

            if symbols:
                symbol_set = set(normalize_symbols(symbols))
                result = result[result["symbol"].isin(symbol_set)]

            return ensure_columns(result, STOCK_BASIC_INFO_COLUMNS)
        except Exception as exc:
            logger.warning("BaoStock get_stock_basic_info 失败: %s", exc)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)
        finally:
            try:
                _bs.logout()
            except Exception:
                pass
