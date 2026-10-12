"""EfinanceProvider - 东方财富行情数据源

通过 efinance 库获取实时行情、K线、板块资金流向等数据。
无需 API Key，公开数据源。

注意: EastMoney 接口可能被限流，实时行情失败时自动降级到直接 HTTP 请求。
"""

from __future__ import annotations

import logging
import time
import random
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import (
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
    normalize_symbols,
    normalize_trade_date,
    now_text,
    pick_column,
    to_numeric_series,
)

logger = logging.getLogger(__name__)

# 尝试导入 efinance
_efinance: Any = None
try:
    import efinance as _efinance
except ImportError:
    pass

# EastMoney 限速保护
_EM_LAST_CALL: float = 0.0
_EM_MIN_INTERVAL: float = 0.3  # 东财接口最少间隔0.3秒


class EfinanceProvider(BaseMarketDataProvider):
    """东方财富行情数据源（通过 efinance 库）。"""

    name = "efinance"

    def is_available(self) -> bool:
        """检查 efinance 是否已安装。"""
        return _efinance is not None

    def health(self) -> dict[str, Any]:
        """返回数据源健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": [
                "realtime_quotes",
                "daily_bars",
                "concept_boards",
                "board_fund_flow",
                "stock_fund_flow",
                "stock_belong_boards",
            ],
        }

    @staticmethod
    def _throttle() -> None:
        """EastMoney 限速：每次调用至少间隔0.3秒，加随机 jitter。"""
        global _EM_LAST_CALL
        elapsed = time.time() - _EM_LAST_CALL
        if elapsed < _EM_MIN_INTERVAL:
            time.sleep(round(_EM_MIN_INTERVAL - elapsed + random.random() * 0.1, 3))
        _EM_LAST_CALL = time.time()

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 ef.stock.get_realtime_quotes() 获取实时行情。

        efinance 返回列: 股票代码, 股票名称, 涨跌幅, 最新价, 最高, 最低,
        今开, 涨跌额, 换手率, 量比, 动态市盈率, 成交量, 成交额,
        昨日收盘, 总市值, 流通市值

        限流时自动降级到直接 HTTP 请求东财 API。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        if not self.is_available():
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        # 方案1: efinance 库调用（带重试）
        for attempt in range(2):
            try:
                self._throttle()
                df = _efinance.stock.get_realtime_quotes()
                if df is not None and not df.empty:
                    return self._parse_realtime_df(df, symbols)
            except Exception as e:
                logger.warning("Efinance get_realtime_quotes 尝试%d失败: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(1)

        # 方案2: 直接 HTTP 请求东财 API（降级）
        logger.info("Efinance 库调用失败，降级到直接 HTTP 请求")
        return self._realtime_quotes_via_http(symbols)

    def _parse_realtime_df(self, df: pd.DataFrame, symbols: list[str] | None) -> pd.DataFrame:
        """解析 efinance 实时行情 DataFrame。

        Args:
            df: efinance 原始 DataFrame
            symbols: 股票代码列表

        Returns:
            标准化后的 DataFrame
        """
        symbol_set = set(normalize_symbols(symbols)) if symbols else set()
        code_col = pick_column(df, ["股票代码", "代码", "code"], "")
        df["__norm_code"] = code_col.astype(str).str.replace(r"\D", "", regex=True)
        if symbol_set:
            df = df[df["__norm_code"].isin(symbol_set)]

        result = pd.DataFrame(index=df.index)
        result["symbol"] = df["__norm_code"]
        result["stock_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
        result["price"] = to_numeric_series(pick_column(df, ["最新价", "price", "close"]))
        result["open"] = to_numeric_series(pick_column(df, ["今开", "open"]))
        result["high"] = to_numeric_series(pick_column(df, ["最高", "high"]))
        result["low"] = to_numeric_series(pick_column(df, ["最低", "low"]))
        result["close"] = to_numeric_series(pick_column(df, ["最新价", "close", "price"]))
        result["pre_close"] = to_numeric_series(pick_column(df, ["昨日收盘", "昨收", "pre_close"]))
        result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
        result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
        result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
        result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
        result["pe"] = to_numeric_series(pick_column(df, ["动态市盈率", "市盈率", "pe"]))
        result["market_cap"] = to_numeric_series(pick_column(df, ["总市值", "market_cap", "total_mv"]))
        result["float_market_cap"] = to_numeric_series(pick_column(df, ["流通市值", "float_market_cap", "circ_mv"]))
        result["provider"] = self.name
        result["updated_at"] = now_text()
        return ensure_columns(result[result["symbol"].astype(bool)], MARKET_SNAPSHOT_COLUMNS)

    def _realtime_quotes_via_http(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """直接 HTTP 请求东财 API 获取实时行情（efinance 库失败时的降级方案）。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        try:
            import requests as _req
        except ImportError:
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

        try:
            # 东财全市场行情 API
            url = "https://82.push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": 1,
                "pz": 5000,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23",
            }
            self._throttle()
            resp = _req.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            items = data.get("data", {}).get("diff", [])
            if not items:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)

            rows = []
            symbol_set = set(normalize_symbols(symbols)) if symbols else set()
            for item in items:
                code = str(item.get("f12", ""))
                if symbol_set and code not in symbol_set:
                    continue
                rows.append({
                    "symbol": code,
                    "stock_name": str(item.get("f14", "")),
                    "price": item.get("f2", 0),
                    "open": item.get("f17", 0),
                    "high": item.get("f15", 0),
                    "low": item.get("f16", 0),
                    "close": item.get("f2", 0),
                    "pre_close": item.get("f18", 0),
                    "pct_chg": item.get("f3", 0),
                    "amount": item.get("f6", 0),
                    "volume": item.get("f5", 0),
                    "turnover_rate": item.get("f8", 0),
                    "pe": item.get("f9", 0),
                    "market_cap": item.get("f20", 0),
                    "float_market_cap": item.get("f21", 0),
                    "provider": self.name,
                    "updated_at": now_text(),
                })

            if not rows:
                return empty_frame(MARKET_SNAPSHOT_COLUMNS)
            return ensure_columns(pd.DataFrame(rows), MARKET_SNAPSHOT_COLUMNS)
        except Exception as e:
            logger.warning("Efinance HTTP 降级请求失败: %s", e)
            return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    def get_market_overview(self, trade_date: str | None = None) -> pd.DataFrame:
        """聚合全市场实时行情，返回涨跌家数 + 总成交额 + 热门板块。

        复用 get_realtime_quotes() 拉取全市场行情后聚合统计，
        再调用 get_concept_boards() 取涨幅最高的概念板块。

        Args:
            trade_date: 交易日期（仅用于标注，实际返回实时数据）

        Returns:
            DataFrame，列名遵循 MARKET_OVERVIEW_COLUMNS，单行
        """
        if not self.is_available():
            return empty_frame(MARKET_OVERVIEW_COLUMNS)

        try:
            df = self.get_realtime_quotes()
            if df is None or df.empty:
                return empty_frame(MARKET_OVERVIEW_COLUMNS)

            # 过滤掉停牌/退市股（price <= 0）
            valid_df = df[df["price"] > 0]
            pct_series = valid_df["pct_chg"].dropna()

            up_count = int((pct_series > 0).sum())
            down_count = int((pct_series < 0).sum())
            flat_count = int((pct_series == 0).sum())

            total_amount = float(valid_df["amount"].fillna(0).sum())
            total_volume = float(valid_df["volume"].fillna(0).sum())

            # 热门板块：复用 get_concept_boards
            hot_sector = ""
            hot_sector_code = ""
            hot_sector_pct = 0.0
            try:
                boards_df = self.get_concept_boards()
                if boards_df is not None and not boards_df.empty:
                    board_pct = boards_df["change_pct"]
                    if not board_pct.empty:
                        top_idx = board_pct.idxmax()
                        hot_sector = str(boards_df.loc[top_idx, "board_name"] or "")
                        hot_sector_code = str(boards_df.loc[top_idx, "board_code"] or "")
                        hot_sector_pct = float(board_pct.loc[top_idx])
            except Exception as exc:
                logger.warning("Efinance get_market_overview 热门板块获取失败: %s", exc)

            row = pd.DataFrame([{
                "trade_date": trade_date or time.strftime("%Y-%m-%d"),
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
            logger.warning("Efinance get_market_overview 失败: %s", exc)
            return empty_frame(MARKET_OVERVIEW_COLUMNS)

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 ef.stock.get_quote_history() 获取日K线。

        efinance 返回列: 股票名称, 股票代码, 日期, 开盘, 收盘, 最高,
        最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率

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
            # 去除可能的市场后缀
            code = sym.replace(".SH", "").replace(".SZ", "")
            try:
                df = _efinance.stock.get_quote_history(
                    code,
                    beg=start_date.replace("-", ""),
                    end=end_date.replace("-", ""),
                    klt=101,  # 日K
                    fqt=1,    # 前复权
                )
                if df is None or df.empty:
                    continue
                # 多股票时返回 dict，单股票返回 DataFrame
                if isinstance(df, dict):
                    df = df.get(code, pd.DataFrame())
                if df.empty:
                    continue

                result = pd.DataFrame(index=df.index)
                result["trade_date"] = pick_column(df, ["日期", "date", "trade_date"]).astype(str)
                result["symbol"] = code
                result["stock_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
                result["open"] = to_numeric_series(pick_column(df, ["开盘", "open"]))
                result["high"] = to_numeric_series(pick_column(df, ["最高", "high"]))
                result["low"] = to_numeric_series(pick_column(df, ["最低", "low"]))
                result["close"] = to_numeric_series(pick_column(df, ["收盘", "close"]))
                result["pre_close"] = pd.NA  # efinance 日K不直接提供昨收
                result["pct_chg"] = to_numeric_series(pick_column(df, ["涨跌幅", "pct_chg", "change_pct"]))
                result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
                result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
                result["turnover_rate"] = to_numeric_series(pick_column(df, ["换手率", "turnover_rate"]))
                result["volume_ratio"] = pd.NA  # efinance 日K不直接提供量比
                result["total_market_cap"] = pd.NA
                result["float_market_cap"] = pd.NA
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, DAILY_BARS_COLUMNS))
            except Exception as e:
                logger.warning("Efinance get_daily_bars(%s) 失败: %s", code, e)
                continue

        if not all_rows:
            return empty_frame(DAILY_BARS_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        count: int = 30,
    ) -> dict[str, Any]:
        """通过 ef.stock.get_quote_history() 获取K线数据。

        Args:
            code: 股票代码
            period: 周期 daily/weekly/monthly/60min/30min/15min/5min
            count: 返回条数

        Returns:
            {source, code, period, bars: [{date,open,high,low,close,volume,amount}]}
        """
        if not self.is_available():
            return {"source": self.name, "code": code, "period": period, "bars": []}

        # efinance klt 参数映射
        klt_map = {
            "daily": 101,
            "weekly": 102,
            "monthly": 103,
            "60min": 60,
            "30min": 30,
            "15min": 15,
            "5min": 5,
            "1min": 1,
        }
        klt = klt_map.get(period, 101)

        try:
            df = _efinance.stock.get_quote_history(code, klt=klt, fqt=1)
            if df is None or df.empty:
                return {"source": self.name, "code": code, "period": period, "bars": []}
            if isinstance(df, dict):
                df = df.get(code, pd.DataFrame())
            if df.empty:
                return {"source": self.name, "code": code, "period": period, "bars": []}

            # 只取最近 count 条
            df = df.tail(count)

            bars = []
            for _, row in df.iterrows():
                bar = {
                    "date": str(pick_column(row.to_frame().T, ["日期", "date"]).iloc[0]),
                    "open": float(to_numeric_series(pick_column(row.to_frame().T, ["开盘", "open"])).iloc[0] or 0),
                    "high": float(to_numeric_series(pick_column(row.to_frame().T, ["最高", "high"])).iloc[0] or 0),
                    "low": float(to_numeric_series(pick_column(row.to_frame().T, ["最低", "low"])).iloc[0] or 0),
                    "close": float(to_numeric_series(pick_column(row.to_frame().T, ["收盘", "close"])).iloc[0] or 0),
                    "volume": float(to_numeric_series(pick_column(row.to_frame().T, ["成交量", "volume"])).iloc[0] or 0),
                    "amount": float(to_numeric_series(pick_column(row.to_frame().T, ["成交额", "amount"])).iloc[0] or 0),
                }
                bars.append(bar)

            return {"source": self.name, "code": code, "period": period, "bars": bars}
        except Exception as e:
            logger.warning("Efinance get_kline(%s) 失败: %s", code, e)
            return {"source": self.name, "code": code, "period": period, "bars": []}

    # ----------------------------------------------------------
    # 板块接口
    # ----------------------------------------------------------

    def get_concept_boards(self, trade_date: str | None = None) -> pd.DataFrame:
        """通过 ef.stock.get_realtime_quotes('概念板块') 获取概念板块列表。

        efinance 概念板块返回列与股票行情一致:
        股票代码, 股票名称, 涨跌幅, 最新价, 成交额, 成交量 等

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARDS_COLUMNS
        """
        if not self.is_available():
            return empty_frame(CONCEPT_BOARDS_COLUMNS)

        try:
            df = _efinance.stock.get_realtime_quotes("概念板块")
            if df is None or df.empty:
                return empty_frame(CONCEPT_BOARDS_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["board_code"] = pick_column(df, ["股票代码", "代码", "code"], "").astype(str)
            result["board_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
            result["change_pct"] = to_numeric_series(pick_column(df, ["涨跌幅", "change_pct", "pct_chg"]))
            result["amount"] = to_numeric_series(pick_column(df, ["成交额", "amount", "turnover"]))
            result["volume"] = to_numeric_series(pick_column(df, ["成交量", "volume", "vol"]))
            result["member_count"] = pd.NA  # efinance 概念板块行情不直接提供成分股数量
            result["leading_stock"] = ""  # efinance 不直接提供领涨股
            result["leading_change_pct"] = pd.NA
            result["trade_date"] = trade_date or ""
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, CONCEPT_BOARDS_COLUMNS)
        except Exception as e:
            logger.warning("Efinance get_concept_boards 失败: %s", e)
            return empty_frame(CONCEPT_BOARDS_COLUMNS)

    def get_concept_board_members(
        self,
        board_code_or_name: str,
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """获取概念板块成分股。

        efinance 没有直接获取板块成分股的 API，此方法返回空 DataFrame。

        Args:
            board_code_or_name: 板块代码或名称
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARD_MEMBERS_COLUMNS
        """
        # efinance 不提供板块成分股 API，降级返回空
        return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

    # ----------------------------------------------------------
    # 股票基础信息
    # ----------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """通过 ef.stock.get_base_info() 获取股票基础信息。

        efinance 返回列: 股票代码, 股票名称, 市盈率(动), 市净率,
        所处行业, 总市值, 流通市值, 板块编号, ROE, 净利率, 净利润, 毛利率

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        if not self.is_available() or not symbols:
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

        try:
            # efinance get_base_info 支持单只或多只
            codes = [s.replace(".SH", "").replace(".SZ", "") for s in symbols]
            if len(codes) == 1:
                df = _efinance.stock.get_base_info(codes[0])
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
            else:
                df = _efinance.stock.get_base_info(codes)
            if df is None or df.empty:
                return empty_frame(STOCK_BASIC_INFO_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["symbol"] = pick_column(df, ["股票代码", "代码", "code"], "").astype(str).str.replace(r"\D", "", regex=True)
            result["stock_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
            result["industry"] = pick_column(df, ["所处行业", "行业", "industry"], "").astype(str)
            result["area"] = ""  # efinance 不提供地区字段
            result["list_date"] = ""  # efinance 不提供上市日期
            result["business_scope"] = ""  # efinance 不提供经营范围
            result["main_business"] = ""  # efinance 不提供主营业务
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result[result["symbol"].astype(bool)], STOCK_BASIC_INFO_COLUMNS)
        except Exception as e:
            logger.warning("Efinance get_stock_basic_info 失败: %s", e)
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)

    # ----------------------------------------------------------
    # 资金流向
    # ----------------------------------------------------------

    def get_stock_fund_flow(
        self,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """通过 ef.stock.get_history_bill() 获取个股资金流向。

        efinance 返回列: 股票名称, 股票代码, 日期, 主力净流入,
        小单净流入, 中单净流入, 大单净流入, 超大单净流入,
        主力净流入占比, 小单流入净占比, 中单流入净占比,
        大单流入净占比, 超大单流入净占比, 收盘价, 涨跌幅

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
            code = sym.replace(".SH", "").replace(".SZ", "")
            try:
                df = _efinance.stock.get_history_bill(code)
                if df is None or df.empty:
                    continue

                # 筛选指定交易日期
                date_col = pick_column(df, ["日期", "date"], "").astype(str)
                trade_date_norm = trade_date.replace("-", "")
                # efinance 日期格式为 YYYY-MM-DD
                mask = date_col.str.startswith(trade_date.replace("-", ""))
                if not mask.any():
                    # 也尝试 YYYYMMDD 格式
                    mask = date_col.str.startswith(trade_date)
                if mask.any():
                    df = df[mask]
                else:
                    # 未找到指定日期，取最近一条
                    df = df.tail(1)

                result = pd.DataFrame(index=df.index)
                result["symbol"] = code
                result["stock_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
                result["trade_date"] = trade_date
                result["main_net_inflow"] = to_numeric_series(pick_column(df, ["主力净流入", "main_net_inflow"]))
                result["main_net_inflow_pct"] = to_numeric_series(pick_column(df, ["主力净流入占比", "main_net_inflow_pct"]))
                result["super_net_inflow"] = to_numeric_series(pick_column(df, ["超大单净流入", "super_net_inflow"]))
                result["big_net_inflow"] = to_numeric_series(pick_column(df, ["大单净流入", "big_net_inflow"]))
                result["mid_net_inflow"] = to_numeric_series(pick_column(df, ["中单净流入", "mid_net_inflow"]))
                result["small_net_inflow"] = to_numeric_series(pick_column(df, ["小单净流入", "small_net_inflow"]))
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, STOCK_FUND_FLOW_COLUMNS))
            except Exception as e:
                logger.warning("Efinance get_stock_fund_flow(%s) 失败: %s", code, e)
                continue

        if not all_rows:
            return empty_frame(STOCK_FUND_FLOW_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)

    def get_board_fund_flow(self, trade_date: str) -> pd.DataFrame:
        """通过 ef.stock.get_realtime_quotes('行业板块') 获取板块资金流向。

        efinance 行业板块行情返回列与股票行情一致，不含资金流向细分数据。
        此方法映射板块行情数据到板块资金流向格式，资金净流入字段为空。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 BOARD_FUND_FLOW_COLUMNS
        """
        if not self.is_available():
            return empty_frame(BOARD_FUND_FLOW_COLUMNS)

        try:
            df = _efinance.stock.get_realtime_quotes("行业板块")
            if df is None or df.empty:
                return empty_frame(BOARD_FUND_FLOW_COLUMNS)

            result = pd.DataFrame(index=df.index)
            result["board_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
            result["board_code"] = pick_column(df, ["股票代码", "代码", "code"], "").astype(str)
            result["change_pct"] = to_numeric_series(pick_column(df, ["涨跌幅", "change_pct", "pct_chg"]))
            # efinance 行业板块行情不提供资金流向细分数据
            result["main_net_inflow"] = pd.NA
            result["super_net_inflow"] = pd.NA
            result["big_net_inflow"] = pd.NA
            result["mid_net_inflow"] = pd.NA
            result["small_net_inflow"] = pd.NA
            result["trade_date"] = trade_date
            result["provider"] = self.name
            result["updated_at"] = now_text()
            return ensure_columns(result, BOARD_FUND_FLOW_COLUMNS)
        except Exception as e:
            logger.warning("Efinance get_board_fund_flow 失败: %s", e)
            return empty_frame(BOARD_FUND_FLOW_COLUMNS)

    # ----------------------------------------------------------
    # 涨停池
    # ----------------------------------------------------------

    def get_limit_up_pool(self, trade_date: str) -> pd.DataFrame:
        """获取涨停股池。

        efinance 不提供涨停池 API，此方法返回空 DataFrame。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 LIMIT_UP_POOL_COLUMNS
        """
        # efinance 不提供涨停池 API，降级返回空
        return empty_frame(LIMIT_UP_POOL_COLUMNS)

    # ----------------------------------------------------------
    # 板块归属
    # ----------------------------------------------------------

    def get_stock_belong_boards(
        self,
        symbols: list[str],
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """通过 ef.stock.get_belong_board() 获取股票所属板块。

        efinance 返回列: 股票名称, 股票代码, 板块代码, 板块名称, 板块涨幅

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
            code = sym.replace(".SH", "").replace(".SZ", "")
            try:
                df = _efinance.stock.get_belong_board(code)
                if df is None or df.empty:
                    continue

                result = pd.DataFrame(index=df.index)
                result["symbol"] = code
                result["stock_name"] = pick_column(df, ["股票名称", "名称", "name"], "").astype(str)
                result["board_code"] = pick_column(df, ["板块代码", "code", "board_code"], "").astype(str)
                result["board_name"] = pick_column(df, ["板块名称", "name", "board_name"], "").astype(str)
                # efinance 不区分行业/概念板块类型，根据板块代码前缀推断
                board_code_col = result["board_code"].astype(str)
                result["board_type"] = board_code_col.apply(_infer_board_type)
                result["trade_date"] = trade_date or ""
                result["provider"] = self.name
                result["updated_at"] = now_text()
                all_rows.append(ensure_columns(result, STOCK_BELONG_BOARDS_COLUMNS))
            except Exception as e:
                logger.warning("Efinance get_stock_belong_boards(%s) 失败: %s", code, e)
                continue

        if not all_rows:
            return empty_frame(STOCK_BELONG_BOARDS_COLUMNS)
        return pd.concat(all_rows, ignore_index=True)


def _infer_board_type(board_code: str) -> str:
    """根据板块代码推断板块类型。

    东方财富板块代码规则:
    - BK04xx/BK05xx/BK06xx 通常是行业板块
    - BK08xx/BK09xx/BK1xxx 通常是概念板块
    - 其他归为 unknown

    Args:
        board_code: 板块代码

    Returns:
        板块类型字符串: industry / concept / unknown
    """
    if not board_code or not board_code.startswith("BK"):
        return "unknown"
    # 提取数字部分
    try:
        num = int(board_code[2:])
    except ValueError:
        return "unknown"
    # 行业板块编号通常较小，概念板块编号较大
    if num < 800:
        return "industry"
    return "concept"
