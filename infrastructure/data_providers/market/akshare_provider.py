"""
AKShare 数据提供者。

使用 akshare 库获取实时行情、日K线、概念板块、资金流向、研报、公告等。
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from io import StringIO
import time
import re
from datetime import datetime
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
    ALL_A_SPOT_COLUMNS,
    BOARD_FUND_FLOW_COLUMNS,
    CONCEPT_BOARD_MEMBERS_COLUMNS,
    CONCEPT_BOARDS_COLUMNS,
    DAILY_BARS_COLUMNS,
    LIMIT_UP_POOL_COLUMNS,
    MARKET_OVERVIEW_COLUMNS,
    MARKET_SNAPSHOT_COLUMNS,
    STOCK_BELONG_BOARDS_COLUMNS,
    STOCK_BASIC_INFO_COLUMNS,
    STOCK_FUND_FLOW_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    now_text,
    normalize_symbols,
    to_numeric_series,
    pick_column,
)

logger = logging.getLogger(__name__)

# 尝试导入 akshare
_ak: Any = None
try:
    import akshare as _ak
except ImportError:
    pass

_requests: Any = None
try:
    import requests as _requests
except ImportError:
    pass


_EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_FUND_FLOW_TIMEOUT_SECONDS = 8.0


def _load_ths_industry_fund_flow() -> pd.DataFrame:
    """Work around AKShare 1.18.x's stale THS column assignment."""
    from akshare.stock_feature.stock_fund_flow import (
        _get_file_content_ths,
        py_mini_racer,
    )

    js = py_mini_racer.MiniRacer()
    js.eval(_get_file_content_ths("ths.js"))
    headers = {
        "hexin-v": js.call("v"),
        "Referer": "http://data.10jqka.com.cn/funds/hyzjl/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    url = "http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{}/ajax/1/free/1/"
    first = _requests.get(url.format(1), headers=headers, timeout=_FUND_FLOW_TIMEOUT_SECONDS)
    first.raise_for_status()
    match = re.search(r'class=["\']page_info["\'][^>]*>\s*\d+/(\d+)', first.text)
    pages = int(match.group(1)) if match else 1
    frames = [pd.read_html(StringIO(first.text))[0]]
    for page in range(2, pages + 1):
        response = _requests.get(
            url.format(page), headers=headers, timeout=_FUND_FLOW_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        frames.append(pd.read_html(StringIO(response.text))[0])
    return pd.concat(frames, ignore_index=True)
_EASTMONEY_FIELDS = (
    "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152,f45"
)
_EASTMONEY_BOARD_FIELDS = (
    "f2,f3,f4,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,f25,f22,f33,"
    "f11,f62,f128,f124,f107,f104,f105,f136"
)


def _clean_code(code: str) -> str:
    """去除代码中的市场后缀 (.SH/.SZ)。"""
    return re.sub(r"\.(SH|SZ)$", "", str(code))


def _eastmoney_session() -> Any:
    if _requests is None:
        raise RuntimeError("requests not installed")
    session = _requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/center/boardlist.html",
            "Connection": "close",
        }
    )
    return session


def _eastmoney_fetch_clist(params: dict[str, Any], host: str, timeout: int = 15) -> list[dict[str, Any]]:
    hosts = [host, "29", "79", "48"]
    url_candidates = []
    for item in hosts:
        url_candidates.append(f"https://{item}.push2.eastmoney.com/api/qt/clist/get")
        url_candidates.append(f"http://{item}.push2.eastmoney.com/api/qt/clist/get")
    url_candidates.extend(
        [
            "https://push2.eastmoney.com/api/qt/clist/get",
            "http://push2.eastmoney.com/api/qt/clist/get",
        ]
    )
    url_candidates = list(dict.fromkeys(url_candidates))
    last_exc: Exception | None = None
    for url in url_candidates:
        try:
            page_params = dict(params)
            page_params.setdefault("pn", "1")
            page_params.setdefault("pz", "100")
            resp = _eastmoney_get(url, page_params, timeout)
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") or {}
            rows = list(data.get("diff") or [])
            total = int(data.get("total") or len(rows))
            page_size = max(1, int(page_params.get("pz") or len(rows) or 1))
            total_pages = max(1, math.ceil(total / page_size))
            for page in range(2, total_pages + 1):
                page_params["pn"] = str(page)
                resp = _eastmoney_get(url, page_params, timeout)
                resp.raise_for_status()
                payload = resp.json()
                rows.extend((payload.get("data") or {}).get("diff") or [])
            return rows
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    return []


def _eastmoney_get(url: str, params: dict[str, Any], timeout: int) -> Any:
    last_exc: Exception | None = None
    for attempt in range(3):
        request_session = _eastmoney_session()
        try:
            return request_session.get(url, params=params, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            time.sleep(0.3 * (attempt + 1))
        finally:
            request_session.close()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("eastmoney request failed")


def _eastmoney_concept_board_code(symbol: str) -> tuple[str, str]:
    query = str(symbol or "").strip()
    if re.match(r"^BK\d+", query):
        return query, query

    suggested = _eastmoney_suggest_concept_board(query)
    if suggested:
        return suggested

    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": _EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:90 t:3 f:!50",
        "fields": _EASTMONEY_BOARD_FIELDS,
    }
    rows = _eastmoney_fetch_clist(params, host="79")
    if not rows:
        raise RuntimeError("eastmoney concept board list empty")

    exact = [row for row in rows if str(row.get("f14") or "").strip() == query]
    preferred = [row for row in rows if str(row.get("f14") or "").strip() == f"{query}概念"]
    contains = [row for row in rows if query and query in str(row.get("f14") or "")]
    match = (exact or preferred or contains)[0] if (exact or preferred or contains) else None
    if not match:
        raise RuntimeError(f"eastmoney concept board not found: {query}")
    return str(match.get("f12") or "").strip(), str(match.get("f14") or "").strip()


def _eastmoney_suggest_concept_board(query: str) -> tuple[str, str] | None:
    if not query:
        return None
    params = {
        "input": query,
        "type": "14",
        "count": "10",
    }
    url_candidates = [
        "https://searchapi.eastmoney.com/api/suggest/get",
        "http://searchapi.eastmoney.com/api/suggest/get",
    ]
    for url in url_candidates:
        try:
            resp = _eastmoney_get(url, params, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
            rows = ((payload.get("QuotationCodeTable") or {}).get("Data") or [])
            for row in rows:
                code = str(row.get("Code") or row.get("UnifiedCode") or "").strip()
                name = str(row.get("Name") or "").strip()
                classify = str(row.get("Classify") or "").strip().upper()
                security_type_name = str(row.get("SecurityTypeName") or "").strip()
                if code.startswith("BK") and (classify == "BK" or security_type_name == "板块"):
                    return code, name or code
        except Exception:
            continue
    return None


def _eastmoney_concept_board_members(symbol: str, trade_date: str | None = None) -> pd.DataFrame:
    board_code, board_name = _eastmoney_concept_board_code(symbol)
    params = {
        "pn": "1",
        "pz": "500",
        "po": "1",
        "np": "1",
        "ut": _EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": f"b:{board_code} f:!50",
        "fields": _EASTMONEY_FIELDS,
    }
    rows = _eastmoney_fetch_clist(params, host="29")
    if not rows:
        return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

    frame = pd.DataFrame(rows)
    result = pd.DataFrame(index=frame.index)
    result["symbol"] = frame.get("f12", pd.Series("", index=frame.index)).astype(str).str.replace(r"\D", "", regex=True)
    result["stock_name"] = frame.get("f14", pd.Series("", index=frame.index)).astype(str)
    result["board_code"] = board_code
    result["board_name"] = board_name
    result["change_pct"] = to_numeric_series(frame.get("f3", pd.Series(pd.NA, index=frame.index)))
    result["amount"] = to_numeric_series(frame.get("f6", pd.Series(pd.NA, index=frame.index)))
    result["volume"] = to_numeric_series(frame.get("f5", pd.Series(pd.NA, index=frame.index)))
    result["trade_date"] = trade_date or ""
    result["provider"] = "akshare:eastmoney_direct"
    result["updated_at"] = now_text()
    return ensure_columns(result[result["symbol"].astype(bool)], CONCEPT_BOARD_MEMBERS_COLUMNS)


def _periodic_report_categories(report_types: list[str] | None) -> list[str]:
    if not report_types:
        return ["年报", "半年报", "一季报", "三季报"]
    aliases = {
        "annual": ["年报"],
        "year": ["年报"],
        "yearly": ["年报"],
        "annual_report": ["年报"],
        "年报": ["年报"],
        "semiannual": ["半年报"],
        "halfyear": ["半年报"],
        "half_year": ["半年报"],
        "semiannual_report": ["半年报"],
        "半年报": ["半年报"],
        "quarter": ["一季报", "三季报"],
        "quarterly": ["一季报", "三季报"],
        "quarterly_report": ["一季报", "三季报"],
        "季报": ["一季报", "三季报"],
        "一季报": ["一季报"],
        "三季报": ["三季报"],
    }
    categories: list[str] = []
    for item in report_types:
        raw = str(item).strip()
        for category in aliases.get(raw.lower(), aliases.get(raw, [])):
            if category not in categories:
                categories.append(category)
    return categories or ["年报", "半年报", "一季报", "三季报"]


def _report_type_from_category(category: str) -> str:
    if category == "年报":
        return "annual_report"
    if category == "半年报":
        return "semiannual_report"
    if category in {"一季报", "三季报"}:
        return "quarterly_report"
    return "periodic_report"


def _row_text(row: Any, candidates: list[str]) -> str:
    for col in candidates:
        try:
            value = row.get(col, "")
        except Exception:
            value = ""
        if value is not None and str(value).strip() and str(value).strip().lower() != "nan":
            return str(value).strip()
    return ""


class AkshareProvider(BaseMarketDataProvider):
    """AKShare 数据提供者，覆盖行情、板块、资金流向、研报、公告等。"""

    name = "akshare"

    def is_available(self) -> bool:
        """检查 akshare 是否已安装。"""
        return _ak is not None

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": [
                "realtime_quotes",
                "daily_bars",
                "concept_boards",
                "concept_board_members",
                "stock_fund_flow",
                "board_fund_flow",
                "limit_up_pool",
                "stock_belong_boards",
                "research_reports",
                "announcements",
                "periodic_reports",
                "investor_interactions",
                "hot_themes",
            ],
        }

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 ak.stock_zh_a_spot_em() 获取实时行情。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        if not self.is_available():
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        try:
            df = _ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            # 筛选指定代码
            symbol_set = set(normalize_symbols(symbols)) if symbols else set()
            code_col = pick_column(df, ["代码", "code", "symbol"], "")
            df["__norm_code"] = code_col.astype(str).str.replace(r"\D", "", regex=True)
            if symbol_set:
                df = df[df["__norm_code"].isin(symbol_set)]

            result = pd.DataFrame(index=df.index)
            result["symbol"] = df["__norm_code"]
            result["stock_name"] = pick_column(df, ["名称", "name", "stock_name"], "").astype(str)
            result["price"] = to_numeric_series(pick_column(df, ["最新价", "price", "close"]))
            result["open"] = to_numeric_series(pick_column(df, ["今开", "open"]))
            result["high"] = to_numeric_series(pick_column(df, ["最高", "high"]))
            result["low"] = to_numeric_series(pick_column(df, ["最低", "low"]))
            result["close"] = to_numeric_series(pick_column(df, ["最新价", "close", "price"]))
            result["pre_close"] = to_numeric_series(pick_column(df, ["昨收", "pre_close"]))
            result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
            result["pe"] = to_numeric_series(pick_column(df, ["市盈率-动态", "pe"]))
            result["market_cap"] = to_numeric_series(pick_column(df, ["总市值", "market_cap", "total_mv"]))
            result["float_market_cap"] = to_numeric_series(pick_column(df, ["流通市值", "float_market_cap", "circ_mv"]))
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, MARKET_SNAPSHOT_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_realtime_quotes 失败: %s", exc)
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 ak.stock_zh_a_hist() 获取日K线。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(DAILY_BARS_COLUMNS)

        all_rows: list[pd.DataFrame] = []
        for sym in symbols:
            code = _clean_code(sym)
            try:
                df = _ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
                if df is None or df.empty:
                    continue

                result = pd.DataFrame(index=df.index)
                result["trade_date"] = pick_column(df, ["日期", "date", "trade_date"]).astype(str)
                result["symbol"] = code
                result["stock_name"] = pick_column(df, ["股票名称", "name", "stock_name"], "").astype(str)
                result["open"] = to_numeric_series(pick_column(df, ["开盘", "open"]))
                result["high"] = to_numeric_series(pick_column(df, ["最高", "high"]))
                result["low"] = to_numeric_series(pick_column(df, ["最低", "low"]))
                result["close"] = to_numeric_series(pick_column(df, ["收盘", "close"]))
                result["pre_close"] = to_numeric_series(pick_column(df, ["昨收", "pre_close"]))
                result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
                result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
                result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
                result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
                result["volume_ratio"] = to_numeric_series(pick_column(df, ["量比", "volume_ratio"]))
                result["total_market_cap"] = pd.NA
                result["float_market_cap"] = pd.NA
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, DAILY_BARS_COLUMNS))
            except Exception as exc:
                logger.warning("AKShare get_daily_bars(%s) 失败: %s", code, exc)
                continue

        if not all_rows:
            return empty_frame(DAILY_BARS_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)

    def get_market_overview(self, trade_date: str | None = None) -> pd.DataFrame:
        """聚合全市场实时行情，返回涨跌家数 + 总成交额 + 热门板块。

        利用 ak.stock_zh_a_spot_em() 一次拉取全市场行情后聚合统计，
        同时调用 ak.stock_board_concept_name_em() 取涨幅最高的概念板块。

        Args:
            trade_date: 交易日期（仅用于标注，实际返回实时数据）

        Returns:
            DataFrame，列名遵循 MARKET_OVERVIEW_COLUMNS，单行
        """
        if not self.is_available():
            return empty_frame(MARKET_OVERVIEW_COLUMNS)

        try:
            df = _ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return empty_frame(MARKET_OVERVIEW_COLUMNS)

            pct_series = to_numeric_series(
                pick_column(df, ["涨跌幅", "pct_chg", "change_pct"])
            ).dropna()
            # 过滤掉非正常交易的股票（最新价为 0 或 NaN 的停牌/退市股）
            price_series = to_numeric_series(
                pick_column(df, ["最新价", "price", "close"])
            )
            valid_mask = price_series > 0
            pct_valid = pct_series[valid_mask]

            up_count = int((pct_valid > 0).sum())
            down_count = int((pct_valid < 0).sum())
            flat_count = int((pct_valid == 0).sum())

            amount_series = to_numeric_series(
                pick_column(df, ["成交额", "amount", "turnover"])
            )[valid_mask].fillna(0)
            volume_series = to_numeric_series(
                pick_column(df, ["成交量", "volume", "vol"])
            )[valid_mask].fillna(0)
            total_amount = float(amount_series.sum())
            total_volume = float(volume_series.sum())

            # 热门板块：取概念板块涨幅最高
            hot_sector = ""
            hot_sector_code = ""
            hot_sector_pct = 0.0
            try:
                boards_df = _ak.stock_board_concept_name_em()
                if boards_df is not None and not boards_df.empty:
                    board_pct = to_numeric_series(
                        pick_column(boards_df, ["涨跌幅", "change_pct", "pct_chg"])
                    )
                    board_names = pick_column(
                        boards_df, ["板块名称", "name", "board_name"], ""
                    ).astype(str)
                    board_codes = pick_column(
                        boards_df, ["板块代码", "code", "board_code"], ""
                    ).astype(str)
                    if not board_pct.empty:
                        top_idx = board_pct.idxmax()
                        hot_sector = str(board_names.iloc[top_idx]) if top_idx in board_names.index else ""
                        hot_sector_code = str(board_codes.iloc[top_idx]) if top_idx in board_codes.index else ""
                        hot_sector_pct = float(board_pct.iloc[top_idx])
            except Exception as exc:
                logger.warning("AKShare get_market_overview 热门板块获取失败: %s", exc)

            row = pd.DataFrame([{
                "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
                "total_amount": total_amount,
                "total_volume": total_volume,
                "hot_sector": hot_sector,
                "hot_sector_code": hot_sector_code,
                "hot_sector_pct": hot_sector_pct,
                "provider": self.name,
                "updated_at": now_text(),
            }])
            return ensure_columns(row, MARKET_OVERVIEW_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_market_overview 失败: %s", exc)
            return empty_frame(MARKET_OVERVIEW_COLUMNS)

    def get_all_a_spot(self) -> pd.DataFrame:
        """获取全 A 股（不含北交所）实时行情快照。

        通过 ak.stock_zh_a_spot_em() 一次性拉取全市场，包含涨速、
        5/15/60 分钟涨跌、量比、换手率等盘中选股所需字段。

        Returns:
            DataFrame，列名遵循 ALL_A_SPOT_COLUMNS。
        """
        if not self.is_available():
            return empty_frame(ALL_A_SPOT_COLUMNS)

        try:
            df = _ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return empty_frame(ALL_A_SPOT_COLUMNS)

            result = pd.DataFrame(index=df.index)
            code_col = pick_column(df, ["代码", "code", "symbol"], "").astype(str)
            result["symbol"] = code_col.str.replace(r"\D", "", regex=True)
            result["stock_name"] = pick_column(df, ["名称", "name", "stock_name"], "").astype(str)
            result["price"] = to_numeric_series(pick_column(df, ["最新价", "price", "close"]))
            result["open"] = to_numeric_series(pick_column(df, ["今开", "open"]))
            result["high"] = to_numeric_series(pick_column(df, ["最高", "high"]))
            result["low"] = to_numeric_series(pick_column(df, ["最低", "low"]))
            result["pre_close"] = to_numeric_series(pick_column(df, ["昨收", "昨收盘", "pre_close", "昨日收盘"]))
            result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
            result["change_amount"] = to_numeric_series(pick_column(df, ["涨跌额", "change_amount"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
            result["volume_ratio"] = to_numeric_series(pick_column(df, ["量比", "volume_ratio"]))
            result["speed"] = to_numeric_series(pick_column(df, ["涨速", "speed"]))
            result["change_5min"] = to_numeric_series(pick_column(df, ["5分钟涨跌", "change_5min", "5min_change"]))
            result["change_15min"] = to_numeric_series(pick_column(df, ["15分钟涨跌", "change_15min", "15min_change"]))
            result["change_60min"] = to_numeric_series(pick_column(df, ["60分钟涨跌", "change_60min", "60min_change"]))
            result["market_cap"] = to_numeric_series(pick_column(df, ["总市值", "market_cap", "total_mv"]))
            result["float_market_cap"] = to_numeric_series(pick_column(df, ["流通市值", "float_market_cap", "circ_mv"]))
            result["provider"] = self.name
            result["updated_at"] = now_text()

            result = result[result["symbol"].astype(bool)].copy()
            return ensure_columns(result, ALL_A_SPOT_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_all_a_spot 失败: %s", exc)
            return empty_frame(ALL_A_SPOT_COLUMNS)

    # ----------------------------------------------------------
    # 板块接口
    # ----------------------------------------------------------

    def get_concept_boards(self, trade_date: str | None = None) -> pd.DataFrame:
        """通过 ak.stock_board_concept_name_em() 获取概念板块列表。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARDS_COLUMNS
        """
        if not self.is_available():
            return empty_frame(CONCEPT_BOARDS_COLUMNS)

        try:
            df = _ak.stock_board_concept_name_em()
            if df is None or df.empty:
                return empty_frame(CONCEPT_BOARDS_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["board_code"] = pick_column(df, ["板块代码", "code", "board_code"], "").astype(str)
            result["board_name"] = pick_column(df, ["板块名称", "name", "board_name"], "").astype(str)
            result["change_pct"] = to_numeric_series(pick_column(df, ["涨跌幅", "change_pct", "pct_chg"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["member_count"] = to_numeric_series(pick_column(df, ["成分股数量", "member_count"]))
            result["leading_stock"] = pick_column(df, ["领涨股票", "leading_stock"], "").astype(str)
            result["leading_change_pct"] = to_numeric_series(pick_column(df, ["领涨股票涨跌幅", "leading_change_pct"]))
            result["trade_date"] = trade_date or ""
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, CONCEPT_BOARDS_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_concept_boards 失败: %s", exc)
            return empty_frame(CONCEPT_BOARDS_COLUMNS)

    def get_concept_board_members(
        self,
        board_code_or_name: str,
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """通过 ak.stock_board_concept_cons_em() 获取概念板块成分股。

        Args:
            board_code_or_name: 板块代码或名称
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARD_MEMBERS_COLUMNS
        """
        if not self.is_available():
            return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

        try:
            direct = _eastmoney_concept_board_members(board_code_or_name, trade_date=trade_date)
            if not direct.empty:
                return direct
        except Exception as direct_exc:
            logger.warning("EastMoney direct concept board members failed: %s", direct_exc)

        try:
            df = _ak.stock_board_concept_cons_em(symbol=board_code_or_name)
            if df is None or df.empty:
                return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = pick_column(df, ["代码", "code", "symbol"], "").astype(str).str.replace(r"\D", "", regex=True)
            result["stock_name"] = pick_column(df, ["名称", "name", "stock_name"], "").astype(str)
            result["board_code"] = ""
            result["board_name"] = board_code_or_name
            result["change_pct"] = to_numeric_series(pick_column(df, ["涨跌幅", "change_pct", "pct_chg"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["trade_date"] = trade_date or ""
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, CONCEPT_BOARD_MEMBERS_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_concept_board_members 失败: %s", exc)
            return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

    # ----------------------------------------------------------
    # 资金流向
    # ----------------------------------------------------------

    def get_stock_fund_flow(
        self,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 ak.stock_individual_fund_flow() 获取个股资金流向。

        Args:
            trade_date: 交易日期
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_FUND_FLOW_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(STOCK_FUND_FLOW_COLUMNS)

        all_rows: list[pd.DataFrame] = []
        for sym in symbols:
            code = _clean_code(sym)
            try:
                market = "sh" if code.startswith("6") else "sz"
                df = _ak.stock_individual_fund_flow(stock=code, market=market)
                if df is None or df.empty:
                    continue

                # 只取最近一条
                df = df.tail(1)
                result = pd.DataFrame(index=df.index)
                result["symbol"] = code
                result["stock_name"] = ""
                result["trade_date"] = trade_date
                result["main_net_inflow"] = to_numeric_series(pick_column(df, ["主力净流入-净额", "main_net_inflow"]))
                result["main_net_inflow_pct"] = to_numeric_series(pick_column(df, ["主力净流入-净占比", "main_net_inflow_pct"]))
                result["super_net_inflow"] = to_numeric_series(pick_column(df, ["超大单净流入-净额", "super_net_inflow"]))
                result["big_net_inflow"] = to_numeric_series(pick_column(df, ["大单净流入-净额", "big_net_inflow"]))
                result["mid_net_inflow"] = to_numeric_series(pick_column(df, ["中单净流入-净额", "mid_net_inflow"]))
                result["small_net_inflow"] = to_numeric_series(pick_column(df, ["小单净流入-净额", "small_net_inflow"]))
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, STOCK_FUND_FLOW_COLUMNS))
            except Exception as exc:
                logger.warning("AKShare get_stock_fund_flow(%s) 失败: %s", code, exc)
                continue

        if not all_rows:
            return empty_frame(STOCK_FUND_FLOW_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)

    def get_board_fund_flow(self, trade_date: str) -> pd.DataFrame:
        """获取板块资金流；东方财富失败时回退同花顺独立口径。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 BOARD_FUND_FLOW_COLUMNS
        """
        if not self.is_available():
            return empty_frame(BOARD_FUND_FLOW_COLUMNS)

        try:
            df = self._fund_flow_call(
                lambda: _ak.stock_sector_fund_flow_rank(
                    indicator="今日", sector_type="行业资金流"
                )
            )
            result = self._normalize_board_fund_flow(df, trade_date, source="eastmoney")
            if not result.empty:
                return result
        except Exception as exc:
            logger.warning("AKShare EastMoney board fund flow failed: %s", type(exc).__name__)
        try:
            df = self._fund_flow_call(_load_ths_industry_fund_flow)
            return self._normalize_board_fund_flow(df, trade_date, source="ths")
        except Exception as exc:
            logger.warning("AKShare THS board fund flow failed: %s", type(exc).__name__)
            return empty_frame(BOARD_FUND_FLOW_COLUMNS)

    @staticmethod
    def _fund_flow_call(loader: Any) -> pd.DataFrame:
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                outcome.put((True, loader()))
            except Exception as exc:
                outcome.put((False, exc))

        threading.Thread(target=run, daemon=True).start()
        try:
            succeeded, value = outcome.get(timeout=_FUND_FLOW_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise TimeoutError("fund flow provider timeout") from exc
        if not succeeded:
            raise value
        return value

    @staticmethod
    def _normalize_board_fund_flow(
        df: pd.DataFrame | None, trade_date: str, *, source: str
    ) -> pd.DataFrame:
        if df is None or df.empty:
            return empty_frame(BOARD_FUND_FLOW_COLUMNS)
        result = pd.DataFrame(index=df.index)
        result["board_name"] = pick_column(df, ["名称", "行业", "name", "board_name"], "").astype(str)
        result["board_code"] = pick_column(df, ["代码", "行业指数", "code", "board_code"], "").astype(str)
        change = pick_column(
            df, ["今日涨跌幅", "行业-涨跌幅", "涨跌幅", "change_pct", "pct_chg"]
        )
        if getattr(change, "dtype", None) == object:
            change = change.astype(str).str.rstrip("%")
        result["change_pct"] = to_numeric_series(change)
        amount = pick_column(
            df, ["今日主力净流入-净额", "主力净流入-净额", "净额", "净额(亿)", "main_net_inflow"]
        )
        result["main_net_inflow"] = to_numeric_series(amount)
        if source == "ths":
            result["main_net_inflow"] = result["main_net_inflow"] * 100_000_000
        result["main_net_inflow_pct"] = to_numeric_series(pick_column(
            df, ["今日主力净流入-净占比", "主力净流入-净占比", "main_net_inflow_pct"]
        ))
        for target, aliases in (
            ("super_net_inflow", ["今日超大单净流入-净额", "超大单净流入-净额"]),
            ("big_net_inflow", ["今日大单净流入-净额", "大单净流入-净额"]),
            ("mid_net_inflow", ["今日中单净流入-净额", "中单净流入-净额"]),
            ("small_net_inflow", ["今日小单净流入-净额", "小单净流入-净额"]),
        ):
            result[target] = to_numeric_series(pick_column(df, aliases))
        result["trade_date"] = trade_date
        result["provider"] = f"akshare-{source}"
        result["methodology"] = (
            "东方财富行业资金流" if source == "eastmoney" else "同花顺行业资金流（净额字段，亿元）"
        )
        result["updated_at"] = now_text()
        return ensure_columns(result, BOARD_FUND_FLOW_COLUMNS)

    # ----------------------------------------------------------
    # 涨停池
    # ----------------------------------------------------------

    def get_limit_up_pool(self, trade_date: str) -> pd.DataFrame:
        """通过 ak.stock_zt_pool_em() 获取涨停股池。

        Args:
            trade_date: 交易日期 (YYYY-MM-DD)

        Returns:
            DataFrame，列名遵循 LIMIT_UP_POOL_COLUMNS
        """
        if not self.is_available():
            return empty_frame(LIMIT_UP_POOL_COLUMNS)

        try:
            date_str = trade_date.replace("-", "")
            df = _ak.stock_zt_pool_em(date=date_str)
            if df is None or df.empty:
                return empty_frame(LIMIT_UP_POOL_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = pick_column(df, ["代码", "code", "symbol"], "").astype(str).str.replace(r"\D", "", regex=True)
            result["stock_name"] = pick_column(df, ["名称", "name", "stock_name"], "").astype(str)
            result["close"] = to_numeric_series(pick_column(df, ["收盘价", "close", "price"]))
            result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
            result["limit_up_amount"] = to_numeric_series(pick_column(df, ["涨停封单量", "limit_up_amount"]))
            result["limit_up_time"] = pick_column(df, ["涨停时间", "limit_up_time"], "").astype(str)
            result["first_limit_up_time"] = pick_column(df, ["首次涨停时间", "first_limit_up_time"], "").astype(str)
            result["open_times"] = to_numeric_series(pick_column(df, ["开板次数", "open_times"]))
            result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["trade_date"] = trade_date
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, LIMIT_UP_POOL_COLUMNS)
        except Exception as exc:
            logger.warning("AKShare get_limit_up_pool 失败: %s", exc)
            return empty_frame(LIMIT_UP_POOL_COLUMNS)

    # ----------------------------------------------------------
    # 板块归属
    # ----------------------------------------------------------

    def get_stock_belong_boards(
        self,
        symbols: list[str],
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """通过 ak.stock_board_industry_cons_em() 获取股票所属行业板块。

        Args:
            symbols: 股票代码列表
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 STOCK_BELONG_BOARDS_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(STOCK_BELONG_BOARDS_COLUMNS)

        all_rows: list[pd.DataFrame] = []
        for sym in symbols:
            code = _clean_code(sym)
            try:
                # 查询股票所属行业板块
                df = _ak.stock_board_industry_cons_em(symbol=code)
                if df is None or df.empty:
                    continue

                result = pd.DataFrame(index=df.index)
                result["symbol"] = code
                result["stock_name"] = ""
                result["board_code"] = pick_column(df, ["板块代码", "code", "board_code"], "").astype(str)
                result["board_name"] = pick_column(df, ["板块名称", "name", "board_name"], "").astype(str)
                result["board_type"] = "industry"
                result["trade_date"] = trade_date or ""
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, STOCK_BELONG_BOARDS_COLUMNS))
            except Exception as exc:
                logger.warning("AKShare get_stock_belong_boards(%s) 失败: %s", code, exc)
                continue

        if not all_rows:
            return empty_frame(STOCK_BELONG_BOARDS_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)

    # ----------------------------------------------------------
    # 研报 & 公告
    # ----------------------------------------------------------

    def get_research_reports(self, code: str, count: int = 5) -> list[dict[str, Any]]:
        """通过 ak.stock_research_report_em() 获取券商研报。

        Args:
            code: 股票代码
            count: 返回条数

        Returns:
            研报字典列表 [{title, date, rating, institution, industry, pdf_link}]
        """
        if not self.is_available():
            return []

        try:
            code_clean = _clean_code(code)
            df = _ak.stock_research_report_em(symbol=code_clean)
            if df is None or df.empty:
                return []

            df = df.head(count)
            result: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                result.append({
                    "title": str(row.get("报告名称", "")),
                    "date": str(row.get("日期", "")),
                    "rating": str(row.get("东财评级", "")),
                    "institution": str(row.get("机构", "")),
                    "industry": str(row.get("行业", "")),
                    "pdf_link": str(row.get("报告PDF链接", "")),
                    "code": str(row.get("股票代码", code_clean)),
                    "name": str(row.get("股票简称", "")),
                })
            return result
        except Exception as exc:
            logger.warning("AKShare get_research_reports 失败: %s", exc)
            return []

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """通过 ak.stock_zh_a_disclosure_report_cninfo() 获取公告（巨潮资讯）。

        三级降级：CNINFO(全文搜索) → AKShare → 空

        Args:
            code: 股票代码
            count: 返回条数

        Returns:
            公告字典列表 [{title, date, link}]
        """
        if not self.is_available():
            return []

        try:
            code_clean = _clean_code(code)
            df = _ak.stock_zh_a_disclosure_report_cninfo(symbol=code_clean)
            if df is None or df.empty:
                return []

            df = df.head(count)
            result: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                result.append({
                    "title": str(row.get("公告标题", "")),
                    "date": str(row.get("公告时间", "")),
                    "link": str(row.get("公告链接", "")),
                    "code": str(row.get("代码", code_clean)),
                    "name": str(row.get("简称", "")),
                })
            return result
        except Exception as exc:
            logger.warning("AKShare get_announcements 失败: %s", exc)
            return []

    # ----------------------------------------------------------
    # 热点题材
    # ----------------------------------------------------------

    def get_periodic_reports(
        self,
        code: str,
        report_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch annual, semiannual and quarterly reports from CNINFO via AKShare."""
        if not self.is_available():
            return []

        code_clean = _clean_code(code)
        categories = _periodic_report_categories(report_types)
        result: list[dict[str, Any]] = []
        for category in categories:
            try:
                df = _ak.stock_zh_a_disclosure_report_cninfo(
                    symbol=code_clean,
                    market="沪深京",
                    category=category,
                    start_date="20000101",
                    end_date=datetime.now().strftime("%Y%m%d"),
                )
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    title = _row_text(row, ["公告标题", "标题", "announcementTitle", "title"])
                    if not title:
                        continue
                    result.append(
                        {
                            "title": title,
                            "date": _row_text(row, ["公告时间", "公告日期", "date", "pubdate"]),
                            "link": _row_text(row, ["公告链接", "链接", "pdf_url", "url"]),
                            "pdf_url": _row_text(row, ["公告链接", "链接", "pdf_url", "url"]),
                            "code": _row_text(row, ["代码", "证券代码", "code"]) or code_clean,
                            "name": _row_text(row, ["简称", "证券简称", "name"]),
                            "report_type": _report_type_from_category(category),
                            "source": self.name,
                        }
                    )
                    if len(result) >= count:
                        return result
            except Exception as exc:
                logger.warning("AKShare get_periodic_reports(%s, %s) failed: %s", code_clean, category, exc)
                continue
        return result[:count]

    def get_investor_interactions(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """Fetch investor Q&A from CNINFO IRM or SSE e-interaction via AKShare."""
        if not self.is_available():
            return []

        code_clean = _clean_code(code)
        try:
            if code_clean.startswith("6"):
                return self._get_sse_interactions(code_clean, count)
            return self._get_cninfo_irm_interactions(code_clean, count)
        except Exception as exc:
            logger.warning("AKShare get_investor_interactions(%s) failed: %s", code_clean, exc)
            return []

    def _get_sse_interactions(self, code: str, count: int) -> list[dict[str, Any]]:
        df = _ak.stock_sns_sseinfo(symbol=code)
        if df is None or df.empty:
            return []
        result: list[dict[str, Any]] = []
        for _, row in df.head(count).iterrows():
            question = _row_text(row, ["问题", "提问内容", "question", "content"])
            answer = _row_text(row, ["回答", "回复内容", "answer", "reply"])
            if not question and not answer:
                continue
            result.append(
                {
                    "code": code,
                    "market": "sse",
                    "question": question,
                    "answer": answer,
                    "date": _row_text(row, ["日期", "时间", "date", "time"]),
                    "source": "sseinfo",
                    "url": f"https://sns.sseinfo.com/company.do?uid={code}",
                }
            )
        return result

    def _get_cninfo_irm_interactions(self, code: str, count: int) -> list[dict[str, Any]]:
        questions = _ak.stock_irm_cninfo(symbol=code)
        if questions is None or questions.empty:
            return []
        result: list[dict[str, Any]] = []
        for _, row in questions.head(count).iterrows():
            question_id = _row_text(row, ["问题编号", "提问编号", "questionId", "question_id", "id"])
            question = _row_text(row, ["问题", "提问内容", "question", "content"])
            answer = _row_text(row, ["回答", "回复内容", "answer", "reply"])
            if question_id and not answer:
                answer = self._get_cninfo_irm_answer(question_id)
            if not question and not answer:
                continue
            result.append(
                {
                    "code": code,
                    "market": "cninfo_irm",
                    "question_id": question_id,
                    "question": question,
                    "answer": answer,
                    "date": _row_text(row, ["日期", "时间", "提问时间", "date", "time"]),
                    "source": "cninfo_irm",
                    "url": (
                        f"https://irm.cninfo.com.cn/ircs/question/questionDetail?questionId={question_id}"
                        if question_id
                        else "https://irm.cninfo.com.cn/"
                    ),
                }
            )
        return result

    def _get_cninfo_irm_answer(self, question_id: str) -> str:
        try:
            answers = _ak.stock_irm_ans_cninfo(symbol=question_id)
            if answers is None or answers.empty:
                return ""
            row = answers.iloc[0]
            return _row_text(row, ["回答", "回复内容", "answer", "reply", "内容"])
        except Exception:
            return ""

    def get_hot_themes(self) -> dict[str, Any]:
        """从同花顺页面抓取行业+概念板块排行。

        Returns:
            {source, industry: [...], concept: [...]}
        """
        try:
            import requests as _req
            from bs4 import BeautifulSoup
        except ImportError:
            return {"source": self.name, "industry": [], "concept": [], "error": "缺少 requests/beautifulsoup4"}

        def _scrap_ths_page(url_name: str, label: str) -> list[dict[str, str]]:
            """从同花顺页面抓取板块排行。"""
            try:
                url = f"https://q.10jqka.com.cn/{url_name}/"
                resp = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
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
                logger.warning("同花顺板块页面抓取失败(%s): %s", label, exc)
                return []

        industry = _scrap_ths_page("thshy", "行业板块")
        concept = _scrap_ths_page("thybk", "概念板块")
        return {"source": self.name, "industry": industry[:10], "concept": concept[:10]}
