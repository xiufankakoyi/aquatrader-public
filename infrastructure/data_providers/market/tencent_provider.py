"""
腾讯财经数据提供者。

使用 qt.gtimg.cn 接口获取实时行情，包含 PE/PB/市值/换手率/流通市值。
支持批量查询。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    MARKET_SNAPSHOT_COLUMNS,
    STOCK_BASIC_INFO_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    to_numeric_series,
)

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


def _auto_prefix(code: str) -> str:
    """根据股票代码自动判断市场前缀。

    6xx→sh, 0xx/3xx→sz, 5xx→sh

    Args:
        code: 纯数字股票代码

    Returns:
        带前缀的代码，如 sh600036
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith("0") or code.startswith("3"):
        return f"sz{code}"
    if code.startswith("5"):
        return f"sh{code}"
    return f"sz{code}"


def _parse_tencent_fields(f: list[str]) -> dict[str, Any] | None:
    """解析腾讯财经返回的字段数组。

    字段索引:
        1=名称, 2=代码, 3=现价, 4=昨收, 5=开盘,
        6=成交量(手), 7=成交额, 31=涨跌额, 32=涨跌幅,
        33=最高, 34=最低, 38=换手率%, 39=PE,
        44=流通市值, 45=总市值

    Args:
        f: 以 ~ 分隔的字段数组

    Returns:
        解析后的字典，字段不足时返回 None
    """
    if len(f) < 46:
        return None
    try:
        last_close = float(f[4]) if f[4] else 0
        change_pct = float(f[32]) if f[32] else 0
        return {
            "symbol": f[2],
            "stock_name": f[1],
            "price": float(f[3]) if f[3] else 0,
            "open": float(f[5]) if f[5] else 0,
            "high": float(f[33]) if f[33] else 0,
            "low": float(f[34]) if f[34] else 0,
            "close": float(f[3]) if f[3] else 0,
            "pre_close": last_close,
            "pct_chg": round(change_pct, 2),
            "amount": float(f[7]) if f[7] else 0,
            "volume": int(float(f[6])) if f[6] else 0,
            "turnover_rate": float(f[38]) if f[38] else pd.NA,
            "pe": float(f[39]) if f[39] else pd.NA,
            "market_cap": float(f[45]) if f[45] else pd.NA,
            "float_market_cap": float(f[44]) if f[44] else pd.NA,
            # Exchange snapshot time from Tencent; ``updated_at`` is only the
            # local fetch time and must not be used as a quote-freshness proxy.
            "market_timestamp": f[30] if len(f) > 30 else "",
        }
    except (ValueError, IndexError):
        return None


class TencentProvider(BaseMarketDataProvider):
    """腾讯财经数据提供者，提供实时行情与基础财务数据。"""

    name = "tencent"

    def is_available(self) -> bool:
        """requests 库可用即视为可用。"""
        return requests is not None

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["realtime_quotes", "stock_basic_info"],
        }

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取实时行情快照，包含 PE/PB/市值/换手率/流通市值。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        try:
            qcodes = [_auto_prefix(s) for s in symbols]
            url = f"https://qt.gtimg.cn/q={','.join(qcodes)}"
            resp = requests.get(  # type: ignore[union-attr]
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            rows = []
            for match in re.finditer(r"\"(.*?)\"", resp.text):
                f = match.group(1).split("~")
                parsed = _parse_tencent_fields(f)
                if parsed:
                    parsed["provider"] = self.name
                    parsed["updated_at"] = now_text()
                    rows.append(parsed)

            if not rows:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            return ensure_columns(pd.DataFrame(rows), MARKET_SNAPSHOT_COLUMNS)
        except Exception as exc:
            logger.warning("腾讯财经 get_realtime_quotes 失败: %s", exc)
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取股票基础信息（PE、PB、市值、换手率等）。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            qcodes = [_auto_prefix(s) for s in symbols]
            url = f"https://qt.gtimg.cn/q={','.join(qcodes)}"
            resp = requests.get(  # type: ignore[union-attr]
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )

            rows = []
            for match in re.finditer(r"\"(.*?)\"", resp.text):
                f = match.group(1).split("~")
                parsed = _parse_tencent_fields(f)
                if parsed:
                    rows.append({
                        "symbol": parsed["symbol"],
                        "stock_name": parsed["stock_name"],
                        "industry": "",
                        "area": "",
                        "list_date": "",
                        "business_scope": "",
                        "main_business": "",
                        "provider": self.name,
                        "updated_at": now_text(),
                    })

            if not rows:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            return ensure_columns(pd.DataFrame(rows), STOCK_BASIC_INFO_COLUMNS)
        except Exception as exc:
            logger.warning("腾讯财经 get_stock_basic_info 失败: %s", exc)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)
