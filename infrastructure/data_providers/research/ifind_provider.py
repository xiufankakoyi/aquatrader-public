"""
同花顺 iFinD 数据提供者。

使用 iFinD HTTP API (https://quantapi.51ifind.com/api/v1) 获取专业行情。
配置从 tonghuashun/ifind_config.json 读取，支持自动 token 刷新。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    MARKET_SNAPSHOT_COLUMNS,
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

# iFinD 配置文件路径
_IFIND_CONFIG_PATH = Path(__file__).resolve().parents[2] / "tonghuashun" / "ifind_config.json"
_IFIND_URL = "https://quantapi.51ifind.com/api/v1"


def _parse_code(c: str) -> tuple[str, int]:
    """解析股票代码，返回 (code, market)。

    market: 0=深, 1=沪, 2=港, 3=美

    Args:
        c: 股票代码

    Returns:
        (纯数字代码, 市场编号)
    """
    c = c.strip().upper().replace("SH", "").replace("SZ", "").replace("HK", "").replace(".", "")
    if c.startswith("6"):
        return c, 1
    if c.startswith("0") or c.startswith("3"):
        return c, 0
    if c.startswith("5"):
        return c, 1
    return c, 0


def _to_ifind_code(c: str) -> str:
    """将股票代码转换为 iFinD 格式（如 600036.SH）。

    Args:
        c: 股票代码

    Returns:
        iFinD 格式代码
    """
    code, market = _parse_code(c)
    suffix = {1: ".SH", 0: ".SZ", 2: ".HK", 3: ".US"}.get(market, "")
    return f"{code}{suffix}"


def _simplify_ifind_fields(d: dict[str, Any]) -> dict[str, Any]:
    """简化 iFinD 返回字段名。

    Args:
        d: iFinD 原始字段字典

    Returns:
        简化后的字段字典
    """
    mapping = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "preClose": "pre_close",
        "change": "change",
        "changeRatio": "pct_chg",
        "volume": "volume",
        "amount": "amount",
        "pe_ttm": "pe",
        "turnoverRatio": "turnover_rate",
        "totalCapital": "market_cap",
        "amplitude": "amplitude",
        "pe_static": "pe_static",
        "pb": "pb",
    }
    return {mapping.get(k, k): v for k, v in d.items() if v is not None}


class IfindProvider(BaseMarketDataProvider):
    """同花顺 iFinD 数据提供者，提供专业行情与热点题材。"""

    name = "ifind"

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or _IFIND_CONFIG_PATH
        self._config: dict[str, str] = {}
        self._load_config()

    def _load_config(self) -> None:
        """从配置文件或 config.setting 加载 token。"""
        # 优先从 config.setting 读取
        try:
            from config.setting import Setting
            if Setting.IFIND_REFRESH_TOKEN:
                self._config["refresh_token"] = Setting.IFIND_REFRESH_TOKEN
            if Setting.IFIND_ACCESS_TOKEN:
                self._config["access_token"] = Setting.IFIND_ACCESS_TOKEN
        except Exception:
            pass
        # 配置文件作为补充
        if self._config_path.exists():
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    file_config = json.load(f)
                    # 只在 setting 没有配置时使用文件配置
                    if "refresh_token" not in self._config and "refresh_token" in file_config:
                        self._config["refresh_token"] = file_config["refresh_token"]
                    if "access_token" not in self._config and "access_token" in file_config:
                        self._config["access_token"] = file_config["access_token"]
            except Exception as exc:
                logger.warning("iFinD 配置文件加载失败: %s", exc)

    @property
    def _access_token(self) -> str:
        """获取当前 access_token。"""
        return self._config.get("access_token", "")

    @property
    def _refresh_token(self) -> str:
        """获取当前 refresh_token。"""
        return self._config.get("refresh_token", "")

    def is_available(self) -> bool:
        """检查 iFinD token 是否已配置且 requests 可用。"""
        return requests is not None and bool(self._access_token or self._refresh_token)

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["realtime_quotes", "hot_themes"],
        }

    # ----------------------------------------------------------
    # iFinD API 调用
    # ----------------------------------------------------------

    def _api_call(self, endpoint: str, params: dict[str, Any], retry: bool = True) -> dict[str, Any]:
        """调用 iFinD HTTP API，token 过期时自动刷新。

        Args:
            endpoint: API 端点名称
            params: 请求参数
            retry: 是否允许重试（刷新 token 后）

        Returns:
            API 响应字典
        """
        at = self._access_token
        if not at:
            return {"errorcode": -9999, "errmsg": "未配置iFinD token"}

        try:
            r = requests.post(  # type: ignore[union-attr]
                f"{_IFIND_URL}/{endpoint}",
                headers={"Content-Type": "application/json", "access_token": at},
                json=params,
                timeout=30,
            )
            d = r.json()

            # token 过期时自动刷新
            if d.get("errorcode") in (-1010, -1300, -1302) and retry and self._refresh_token:
                r2 = requests.post(  # type: ignore[union-attr]
                    f"{_IFIND_URL}/get_access_token",
                    headers={"Content-Type": "application/json"},
                    json={"refresh_token": self._refresh_token},
                    timeout=15,
                )
                d2 = r2.json()
                if d2.get("errorcode") == 0:
                    self._config["access_token"] = d2.get("access_token", "")
                    # 持久化刷新后的 token
                    try:
                        with open(self._config_path, "w", encoding="utf-8") as f:
                            json.dump(self._config, f, ensure_ascii=False, indent=2)
                    except Exception as exc:
                        logger.warning("iFinD token 持久化失败: %s", exc)
                    return self._api_call(endpoint, params, retry=False)

            return d
        except Exception as exc:
            return {"errorcode": -9998, "errmsg": str(exc)}

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取专业行情，包含 PE/PB/股息率等。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        if not self.is_available():
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        try:
            ifind_codes = ",".join(_to_ifind_code(s) for s in symbols)
            ds = self._api_call("real_time_quotation", {
                "codes": ifind_codes,
                "indicators": "open,high,low,close,preClose,change,changeRatio,volume,amount,pe_ttm,turnoverRatio,totalCapital,amplitude,pe_static,pb",
            })

            if ds.get("errorcode") != 0:
                logger.warning("iFinD 行情查询失败: %s", ds.get("errmsg", ""))
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            rows = []
            for sym in symbols:
                ic = _to_ifind_code(sym)
                dd = ds.get(ic, {})
                if dd:
                    simplified = _simplify_ifind_fields(dd)
                    simplified["symbol"] = re.sub(r"[^0-9]", "", sym)
                    simplified["stock_name"] = ""
                    simplified["price"] = simplified.get("close", pd.NA)
                    simplified["market_cap"] = simplified.pop("market_cap", pd.NA)
                    simplified["float_market_cap"] = pd.NA
                    simplified["provider"] = self.name
                    simplified["updated_at"] = now_text()
                    rows.append(simplified)

            if not rows:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            df = pd.DataFrame(rows)
            return ensure_columns(df, MARKET_SNAPSHOT_COLUMNS)
        except Exception as exc:
            logger.warning("iFinD get_realtime_quotes 失败: %s", exc)
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    # ----------------------------------------------------------
    # 热点题材
    # ----------------------------------------------------------

    def get_hot_themes(self) -> dict[str, Any]:
        """获取行业+概念板块排行。

        Returns:
            {source, industry: [...], concept: [...]}
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"source": self.name, "industry": [], "concept": [], "error": "缺少 beautifulsoup4"}

        def _scrap_page(url_name: str, label: str) -> list[dict[str, str]]:
            try:
                url = f"https://q.10jqka.com.cn/{url_name}/"
                resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})  # type: ignore[union-attr]
                soup = BeautifulSoup(resp.text, "html.parser")
                table = soup.find("table")
                if not table:
                    return []
                rows_html = table.find_all("tr")
                if len(rows_html) < 2:
                    return []
                headers = [th.get_text(strip=True) for th in rows_html[0].find_all(["th", "td"])]
                data = []
                for tr in rows_html[1:21]:
                    cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                    if cells:
                        data.append(dict(zip(headers, cells)))
                return data
            except Exception as exc:
                logger.warning("iFinD 板块页面抓取失败(%s): %s", label, exc)
                return []

        industry = _scrap_page("thshy", "行业板块")
        concept = _scrap_page("thybk", "概念板块")
        return {"source": self.name, "industry": industry[:10], "concept": concept[:10]}

    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        """iFinD HTTP API 不支持语义搜索。

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
            "error": "iFinD HTTP API 不支持语义搜索，请使用问财(Wencai)数据源",
        }
