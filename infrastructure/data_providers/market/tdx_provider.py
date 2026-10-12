"""
通达信 (TDX) 数据提供者。

使用 pytdx 库获取实时行情、K线数据。
支持多服务器轮询和限速保护。
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    DAILY_BARS_COLUMNS,
    MARKET_SNAPSHOT_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    to_numeric_series,
)

logger = logging.getLogger(__name__)

# 尝试导入 pytdx
_TdxAPI: type | None = None
try:
    from pytdx.hq import TdxHq_API as _TdxAPI
except ImportError:
    pass

# TDX 服务器列表 (名称, IP, 端口)
TDX_HOSTS: list[tuple[str, str, int]] = [
    ("上证云成都电信一", "218.6.170.47", 7709),
    ("上证云北京联通一", "123.125.108.14", 7709),
    ("上海电信主站Z1", "180.153.18.170", 7709),
    ("杭州电信主站J1", "60.191.117.167", 7709),
]


def _tdx_market(code: str) -> int:
    """判断市场代码: 0=深圳, 1=上海。

    Args:
        code: 股票代码（支持 sh/sz 前缀和 .SH/.SZ 后缀）

    Returns:
        市场编号 0 或 1
    """
    code = re.sub(r"[^0-9]", "", code)
    if code.startswith("6") or code.startswith("9"):
        return 1  # 上海
    if code.startswith("0") or code.startswith("3") or code.startswith("2"):
        return 0  # 深圳
    if code.startswith("5"):
        return 0
    return 1


class TdxProvider(BaseMarketDataProvider):
    """通达信数据提供者，支持实时行情与K线查询。"""

    name = "tdx"

    def __init__(self) -> None:
        self._host_idx = 0
        self._last_call_time: float = 0.0
        self._operational: bool | None = None
        self._last_error: str = ""
        self._last_connect_success_at: str = ""

    def is_available(self) -> bool:
        """检查 pytdx 是否已安装。"""
        return _TdxAPI is not None

    def health(self) -> dict[str, Any]:
        """返回运行健康状态；未知连接状态不会伪装成健康。"""
        dependency_available = self.is_available()
        if not dependency_available:
            status = "unavailable"
        elif self._operational is True:
            status = "active"
        elif self._operational is False:
            status = "failed"
        else:
            status = "unknown"
        return {
            "name": self.name,
            "available": dependency_available and self._operational is True,
            "dependency_available": dependency_available,
            "status": status,
            "last_error": self._last_error,
            "last_connect_success_at": self._last_connect_success_at,
            "capabilities": ["realtime_quotes", "kline", "daily_bars"],
        }

    # ----------------------------------------------------------
    # 限速 & 连接
    # ----------------------------------------------------------

    def _throttle(self) -> None:
        """全局TDX限速：每次调用至少间隔0.5秒，防止被封IP。"""
        elapsed = time.time() - self._last_call_time
        if elapsed < 0.5:
            time.sleep(round(0.5 - elapsed + random.random() * 0.2, 3))
        self._last_call_time = time.time()

    def _connect(self) -> Any:
        """连接TDX服务器（限速+轮询多IP）。

        Returns:
            已连接的 TdxHq_API 实例，连接失败返回 None
        """
        if _TdxAPI is None:
            return None
        self._throttle()
        for _ in range(len(TDX_HOSTS)):
            _, ip, port = TDX_HOSTS[self._host_idx % len(TDX_HOSTS)]
            self._host_idx += 1
            api = _TdxAPI()
            connected = False
            try:
                connected = api.connect(ip, port, time_out=3)
                if connected:
                    self._operational = True
                    self._last_error = ""
                    self._last_connect_success_at = now_text()
                    return api
                self._last_error = f"connect returned false: {ip}:{port}"
            except Exception as exc:
                self._last_error = str(exc)
            finally:
                if not connected:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
        self._operational = False
        if not self._last_error:
            self._last_error = "all TDX hosts failed"
        return None

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 get_security_quotes 获取实时行情，包含5档盘口。

        Args:
            symbols: 股票代码列表，None 时返回空（TDX 需要具体代码）

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        api = self._connect()
        if api is None:
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        try:
            # 构造查询参数列表 [(market, code), ...]
            quote_params = []
            for sym in symbols:
                code_clean = re.sub(r"[^0-9]", "", sym)
                if code_clean:
                    quote_params.append((_tdx_market(code_clean), code_clean))

            if not quote_params:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            data = api.get_security_quotes(quote_params)
            if not data:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            rows = []
            for d in data:
                last_close = d.get("last_close", 0) or 0
                price = d.get("price", 0) or 0
                change_pct = ((price - last_close) / last_close * 100) if last_close else 0
                rows.append({
                    "symbol": str(d.get("code", "")),
                    "stock_name": "",
                    "price": price,
                    "open": d.get("open", 0),
                    "high": d.get("high", 0),
                    "low": d.get("low", 0),
                    "close": price,
                    "pre_close": last_close,
                    "pct_chg": round(change_pct, 2),
                    "amount": d.get("amount", 0),
                    "volume": d.get("vol", 0),
                    "turnover_rate": pd.NA,
                    "pe": pd.NA,
                    "market_cap": pd.NA,
                    "float_market_cap": pd.NA,
                    # 5档买卖盘口
                    "bid1": d.get("bid1", 0),
                    "ask1": d.get("ask1", 0),
                    "bid_vol1": d.get("bid_vol1", 0),
                    "ask_vol1": d.get("ask_vol1", 0),
                    "bid2": d.get("bid2", 0),
                    "ask2": d.get("ask2", 0),
                    "bid_vol2": d.get("bid_vol2", 0),
                    "ask_vol2": d.get("ask_vol2", 0),
                    "bid3": d.get("bid3", 0),
                    "ask3": d.get("ask3", 0),
                    "bid_vol3": d.get("bid_vol3", 0),
                    "ask_vol3": d.get("ask_vol3", 0),
                    "bid4": d.get("bid4", 0),
                    "ask4": d.get("ask4", 0),
                    "bid_vol4": d.get("bid_vol4", 0),
                    "ask_vol4": d.get("ask_vol4", 0),
                    "bid5": d.get("bid5", 0),
                    "ask5": d.get("ask5", 0),
                    "bid_vol5": d.get("bid_vol5", 0),
                    "ask_vol5": d.get("ask_vol5", 0),
                    "provider": self.name,
                    "updated_at": now_text(),
                })

            return ensure_columns(pd.DataFrame(rows), MARKET_SNAPSHOT_COLUMNS)
        except Exception as exc:
            logger.warning("TDX get_realtime_quotes 失败: %s", exc)
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def get_index_quotes(self, indices: list[tuple[str, int, str]] | None = None) -> list[dict[str, Any]]:
        """批量获取指数实时行情（1次连接查多个指数）。

        Args:
            indices: 指数配置列表 [(名称, 市场, 代码), ...]
                     默认为6大核心指数

        Returns:
            指数行情列表 [{name, price, change_pct}]
        """
        if not self.is_available():
            return []

        if indices is None:
            indices = [
                ("上证指数", 1, "000001"),
                ("深证成指", 0, "399001"),
                ("创业板指", 0, "399006"),
                ("科创50", 1, "000688"),
                ("上证50", 1, "000016"),
                ("沪深300", 1, "000300"),
            ]

        api = self._connect()
        if api is None:
            return []

        try:
            quote_params = [(mkt, code) for _, mkt, code in indices]
            data = api.get_security_quotes(quote_params)
            if not data:
                return []

            result: list[dict[str, Any]] = []
            for i, (name, _, _) in enumerate(indices):
                if i < len(data) and data[i]:
                    d = data[i]
                    last_close = d.get("last_close", 0) or 0
                    price = d.get("price", 0) or 0
                    change_pct = ((price - last_close) / last_close * 100) if last_close else 0
                    change_amount = price - last_close if last_close else 0.0
                    result.append({
                        "name": name,
                        "price": round(price, 2),
                        "change_pct": round(change_pct, 2),
                        "change_amount": round(change_amount, 2),
                        "last_close": round(last_close, 2),
                    })
            return result
        except Exception as exc:
            logger.warning("TDX get_index_quotes 失败: %s", exc)
            return []
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        count: int = 30,
    ) -> dict[str, Any]:
        """获取K线数据。

        Args:
            code: 股票代码
            period: 周期 daily/weekly/monthly/60min/30min/15min/5min
            count: 返回条数

        Returns:
            {source, code, period, bars: [{date,open,high,low,close,volume,amount}]}
        """
        if not self.is_available():
            return {"source": self.name, "code": code, "period": period, "bars": []}

        api = self._connect()
        if api is None:
            return {"source": self.name, "code": code, "period": period, "bars": []}

        try:
            m = _tdx_market(code)
            code_clean = re.sub(r"[^0-9]", "", code)
            cat_map = {
                "daily": 9,
                "weekly": 5,
                "monthly": 6,
                "60min": 3,
                "30min": 2,
                "15min": 1,
                "5min": 0,
            }
            cat = cat_map.get(period, 9)
            bars = api.get_security_bars(cat, m, code_clean, 0, count)
            if not bars:
                return {"source": self.name, "code": code_clean, "period": period, "bars": []}

            result = []
            for b in bars:
                result.append({
                    "date": str(b.get("datetime", "")),
                    "open": b.get("open"),
                    "high": b.get("high"),
                    "low": b.get("low"),
                    "close": b.get("close"),
                    "volume": b.get("vol"),
                    "amount": b.get("amount"),
                })
            return {"source": self.name, "code": code_clean, "period": period, "bars": result}
        except Exception as exc:
            logger.warning("TDX get_kline 失败: %s", exc)
            return {"source": self.name, "code": code, "period": period, "bars": []}
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def get_intraday_series(
        self,
        code: str,
        market: int | None = None,
    ) -> dict[str, Any]:
        """获取当日 1 分钟分时数据。

        使用 pytdx get_minute_time_data 接口，返回当日已开盘的 1 分钟价格序列。
        A 股交易时间 09:30-11:30、13:00-15:00，最多 240 条。

        Args:
            code: 股票代码（支持 sh/sz 前缀或纯数字）
            market: 市场代码 0=深圳, 1=上海；None 时自动判断

        Returns:
            {source, code, data: [{time, price, volume}]}
            失败或 pytdx 不可用时返回空 data 列表。
        """
        result: dict[str, Any] = {"source": self.name, "code": code, "data": []}
        if not self.is_available():
            return result

        code_clean = re.sub(r"[^0-9]", "", code)
        if not code_clean:
            return result

        m = market if market is not None else _tdx_market(code_clean)

        api = self._connect()
        if api is None:
            return result

        try:
            bars = api.get_minute_time_data(m, code_clean)
            if not bars:
                return {**result, "code": code_clean}
            return {"source": self.name, "code": code_clean, "data": self._format_minute_bars(bars)}
        except Exception as exc:
            logger.warning("TDX get_intraday_series 失败: %s", exc)
            return {**result, "code": code_clean}
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    def get_intraday_series_batch(
        self,
        codes: list[tuple[str, int]],
    ) -> dict[str, list[dict[str, Any]]]:
        """批量获取当日 1 分钟分时数据（复用单个 TDX 连接）。

        Args:
            codes: [(code, market), ...] 列表，market 0=深圳, 1=上海。

        Returns:
            {code: [{time, price, volume}, ...]}，失败项对应空列表。
        """
        result: dict[str, list[dict[str, Any]]] = {}
        if not self.is_available() or not codes:
            return result

        api = self._connect()
        if api is None:
            return result

        try:
            for code, market in codes:
                code_clean = re.sub(r"[^0-9]", "", code)
                if not code_clean:
                    result[code] = []
                    continue
                try:
                    bars = api.get_minute_time_data(market, code_clean)
                    result[code] = self._format_minute_bars(bars) if bars else []
                except Exception as exc:
                    logger.warning("TDX 批量分时 %s 失败: %s", code_clean, exc)
                    result[code] = []
            return result
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    @staticmethod
    def _format_minute_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 pytdx 分钟线原始数据格式化为统一结构。

        Args:
            bars: pytdx 返回的分钟线列表，每项含 price/vol。

        Returns:
            [{time, price, volume}, ...]
        """
        data: list[dict[str, Any]] = []
        base_time = datetime.strptime("09:30", "%H:%M")
        for i, bar in enumerate(bars):
            price = bar.get("price", 0)
            vol = bar.get("vol", 0)
            if price <= 0:
                continue
            cur = base_time + timedelta(minutes=i)
            # 跳过午间休市 11:30-13:00
            if cur.hour == 11 and cur.minute > 30:
                cur += timedelta(hours=1, minutes=30)
            if 11 < cur.hour < 13:
                cur += timedelta(hours=1, minutes=30)
            data.append({
                "time": cur.strftime("%H:%M"),
                "price": round(float(price), 2),
                "volume": int(vol),
            })
        return data

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """获取日K线数据，基于 get_security_bars 实现。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(DAILY_BARS_COLUMNS)

        all_rows: list[dict[str, Any]] = []
        for sym in symbols:
            kline = self.get_kline(sym, period="daily", count=300)
            for bar in kline.get("bars", []):
                bar_date = bar.get("date", "")[:10]
                if start_date <= bar_date <= end_date:
                    last_close = bar.get("close", 0)
                    # 简单估算 pct_chg（TDX K线不直接提供）
                    rows_item = {
                        "trade_date": bar_date,
                        "symbol": kline["code"],
                        "stock_name": "",
                        "open": bar.get("open"),
                        "high": bar.get("high"),
                        "low": bar.get("low"),
                        "close": bar.get("close"),
                        "pre_close": pd.NA,
                        "pct_chg": pd.NA,
                        "amount": bar.get("amount"),
                        "volume": bar.get("volume"),
                        "turnover_rate": pd.NA,
                        "volume_ratio": pd.NA,
                        "total_market_cap": pd.NA,
                        "float_market_cap": pd.NA,
                        "provider": self.name,
                        "updated_at": now_text(),
                    }
                    all_rows.append(rows_item)

        if not all_rows:
            return empty_frame(DAILY_BARS_COLUMNS)

        df = pd.DataFrame(all_rows)
        # 计算 pct_chg：当日 close 相对前日 close 的涨跌幅
        df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        df["pre_close"] = df.groupby("symbol")["close"].shift(1)
        pre_close = to_numeric_series(df["pre_close"])
        close = to_numeric_series(df["close"])
        df["pct_chg"] = ((close - pre_close) / pre_close * 100).round(2)
        return ensure_columns(df, DAILY_BARS_COLUMNS)
