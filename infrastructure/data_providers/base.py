"""
数据提供者基类与共享工具函数。

所有数据源提供者继承 BaseMarketDataProvider，统一列名与降级逻辑。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 列名常量 — 与 provider_registry.py 导入保持一致
# ============================================================

DAILY_BARS_COLUMNS: list[str] = [
    "trade_date",
    "symbol",
    "stock_name",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "amount",
    "volume",
    "turnover_rate",
    "volume_ratio",
    "total_market_cap",
    "float_market_cap",
    "provider",
    "updated_at",
]

MARKET_SNAPSHOT_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "price",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "amount",
    "volume",
    "turnover_rate",
    "pe",
    "market_cap",
    "float_market_cap",
    "bid1", "ask1", "bid_vol1", "ask_vol1",
    "bid2", "ask2", "bid_vol2", "ask_vol2",
    "bid3", "ask3", "bid_vol3", "ask_vol3",
    "bid4", "ask4", "bid_vol4", "ask_vol4",
    "bid5", "ask5", "bid_vol5", "ask_vol5",
    "provider",
    "market_timestamp",
    "updated_at",
]

ALL_A_SPOT_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "price",
    "open",
    "high",
    "low",
    "pre_close",
    "pct_chg",
    "change_amount",
    "amount",
    "volume",
    "turnover_rate",
    "volume_ratio",
    "speed",
    "change_5min",
    "change_15min",
    "change_60min",
    "market_cap",
    "float_market_cap",
    "provider",
    "updated_at",
]

CONCEPT_BOARDS_COLUMNS: list[str] = [
    "board_code",
    "board_name",
    "change_pct",
    "amount",
    "volume",
    "member_count",
    "leading_stock",
    "leading_change_pct",
    "trade_date",
    "provider",
    "updated_at",
]

CONCEPT_BOARD_MEMBERS_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "board_code",
    "board_name",
    "change_pct",
    "amount",
    "volume",
    "trade_date",
    "provider",
    "updated_at",
]

STOCK_BASIC_INFO_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "industry",
    "area",
    "list_date",
    "business_scope",
    "main_business",
    "provider",
    "updated_at",
]

STOCK_FUND_FLOW_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "trade_date",
    "main_net_inflow",
    "main_net_inflow_pct",
    "super_net_inflow",
    "big_net_inflow",
    "mid_net_inflow",
    "small_net_inflow",
    "provider",
    "updated_at",
]

BOARD_FUND_FLOW_COLUMNS: list[str] = [
    "board_name",
    "board_code",
    "change_pct",
    "main_net_inflow",
    "main_net_inflow_pct",
    "super_net_inflow",
    "big_net_inflow",
    "mid_net_inflow",
    "small_net_inflow",
    "trade_date",
    "provider",
    "methodology",
    "updated_at",
]

EARNINGS_FORECAST_COLUMNS: list[str] = [
    "symbol", "announcement_date", "period_end", "forecast_type",
    "change_min_pct", "change_max_pct", "net_profit_min_wan",
    "net_profit_max_wan", "summary", "change_reason", "provider", "updated_at",
]

FINANCE_SNAPSHOT_COLUMNS: list[str] = [
    "symbol", "period", "published_at", "revenue", "net_profit",
    "operating_cash_flow", "gross_margin", "net_margin", "roe", "roic",
    "debt_ratio", "total_assets", "total_liabilities", "total_equity",
    "minority_interest", "total_debt", "cash_and_equivalents",
    "cash_change", "cash_and_equivalents_begin", "cash_and_equivalents_end",
    "provider", "updated_at",
]

CONSENSUS_ESTIMATE_COLUMNS: list[str] = [
    "symbol", "company_name", "report_date", "report_title", "report_type",
    "org_name", "author_name", "forecast_period", "revenue_forecast_yuan",
    "operating_profit_forecast_yuan", "total_profit_forecast_yuan",
    "net_profit_forecast_yuan", "eps_forecast", "pe_forecast", "roe_forecast",
    "provider", "updated_at",
]

VALUATION_HISTORY_COLUMNS: list[str] = [
    "symbol", "trade_date", "close", "turnover_rate", "pe", "pe_ttm",
    "pb", "ps", "ps_ttm", "dividend_yield", "dividend_yield_ttm",
    "total_market_cap", "float_market_cap", "provider", "updated_at",
]

LIMIT_UP_POOL_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "close",
    "pct_chg",
    "limit_up_amount",
    "limit_up_time",
    "first_limit_up_time",
    "open_times",
    "turnover_rate",
    "amount",
    "volume",
    "trade_date",
    "provider",
    "updated_at",
]

STOCK_BELONG_BOARDS_COLUMNS: list[str] = [
    "symbol",
    "stock_name",
    "board_code",
    "board_name",
    "board_type",
    "trade_date",
    "provider",
    "updated_at",
]

MARKET_OVERVIEW_COLUMNS: list[str] = [
    "trade_date",
    "up_count",
    "down_count",
    "flat_count",
    "total_amount",
    "total_volume",
    "hot_sector",
    "hot_sector_code",
    "hot_sector_pct",
    "provider",
    "updated_at",
]


# ============================================================
# 工具函数
# ============================================================


def empty_frame(columns: list[str]) -> pd.DataFrame:
    """创建指定列名的空 DataFrame。"""
    return pd.DataFrame(columns=columns)


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """确保 DataFrame 包含指定列，缺失列用 NaN 填充。"""
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns]


def normalize_symbol(raw: str) -> dict[str, Any]:
    """将单个 A 股代码规范为 ``000001.SZ`` 格式。"""
    if not raw:
        return {"symbol": "", "exchange": "", "raw_symbol": ""}
    raw_str = str(raw).strip().upper()
    match = re.search(r"(\d{6})", raw_str)
    if not match:
        return {"symbol": raw_str, "exchange": "", "raw_symbol": raw}
    code = match.group(1)
    if ".SH" in raw_str or raw_str.startswith("SH") or code.startswith(("6", "5", "9")):
        exchange = "SH"
    elif ".BJ" in raw_str or raw_str.startswith("BJ") or code.startswith(("4", "8")):
        exchange = "BJ"
    else:
        exchange = "SZ"
    return {"symbol": f"{code}.{exchange}", "exchange": exchange, "raw_symbol": raw}


def normalize_symbols(values: Any) -> pd.Series:
    """批量规范代码，并保持 pandas Series 的索引契约。"""
    series = values if isinstance(values, pd.Series) else pd.Series(values, dtype=object)
    return series.astype(str).apply(lambda value: normalize_symbol(value)["symbol"])


def stock_code(symbol: str) -> str:
    """返回 BaoStock 等数据源使用的六位代码部分。"""
    normalized = normalize_symbol(symbol)["symbol"]
    return normalized.split(".")[0] if normalized else str(symbol).split(".")[0]


def normalize_trade_date(date: str | None) -> str:
    """将日期标准化为 YYYY-MM-DD 格式。

    Args:
        date: 日期字符串，支持 YYYYMMDD / YYYY-MM-DD / None

    Returns:
        YYYY-MM-DD 格式字符串，输入为空时返回当天日期
    """
    if not date:
        return datetime.now().strftime("%Y-%m-%d")
    date = str(date).strip().replace("-", "").replace("/", "")
    if len(date) == 8:
        return f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return str(date)


def now_text() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pick_column(
    df: pd.DataFrame,
    candidates: list[str],
    default: Any = pd.NA,
) -> pd.Series:
    """从 DataFrame 中按候选列名顺序选取第一个存在的列。

    Args:
        df: 目标 DataFrame
        candidates: 候选列名列表
        default: 所有候选列均不存在时的默认值

    Returns:
        匹配到的 Series，或填充默认值的 Series
    """
    for col in candidates:
        if col in df.columns:
            return df[col]
    return pd.Series(default, index=df.index, dtype=object)


def to_numeric_series(s: pd.Series) -> pd.Series:
    """将 Series 转换为数值类型，无法转换的设为 NaN。"""
    return pd.to_numeric(s, errors="coerce")


# ============================================================
# 基类
# ============================================================


class BaseMarketDataProvider(ABC):
    """所有行情数据提供者的抽象基类。

    子类必须实现 name 类属性和 is_available 方法。
    其余方法提供默认空实现，子类按需覆写。
    """

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """检查数据源是否可用（库已安装 + 凭证已配置）。"""

    def health(self) -> dict[str, Any]:
        """返回数据源健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": [],
        }

    # ----------------------------------------------------------
    # 行情接口
    # ----------------------------------------------------------

    def get_realtime_quotes(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取实时行情快照。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 MARKET_SNAPSHOT_COLUMNS
        """
        return empty_frame(MARKET_SNAPSHOT_COLUMNS)

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """获取日K线数据。

        Args:
            start_date: 起始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            symbols: 股票代码列表，None 表示全市场

        Returns:
            DataFrame，列名遵循 DAILY_BARS_COLUMNS
        """
        return empty_frame(DAILY_BARS_COLUMNS)

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        count: int = 30,
    ) -> dict[str, Any]:
        """获取K线数据（通用接口）。

        Args:
            code: 股票代码
            period: 周期 daily/weekly/monthly/60min/30min/15min/5min
            count: 返回条数

        Returns:
            {source, code, period, bars: [{date,open,high,low,close,volume,amount}]}
        """
        return {"source": self.name, "code": code, "period": period, "bars": []}

    def get_market_overview(self, trade_date: str | None = None) -> pd.DataFrame:
        """获取市场整体概况（涨跌家数 + 总成交额 + 热门板块）。

        用于替换市场情绪卡片中的占位值。子类通过聚合全市场实时行情实现。

        Args:
            trade_date: 交易日期（YYYY-MM-DD），None 表示当日实时

        Returns:
            DataFrame，列名遵循 MARKET_OVERVIEW_COLUMNS，单行
        """
        return empty_frame(MARKET_OVERVIEW_COLUMNS)

    # ----------------------------------------------------------
    # 板块接口
    # ----------------------------------------------------------

    def get_concept_boards(self, trade_date: str | None = None) -> pd.DataFrame:
        """获取概念板块列表。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARDS_COLUMNS
        """
        return empty_frame(CONCEPT_BOARDS_COLUMNS)

    def get_concept_board_members(
        self,
        board_code_or_name: str,
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """获取概念板块成分股。

        Args:
            board_code_or_name: 板块代码或名称
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 CONCEPT_BOARD_MEMBERS_COLUMNS
        """
        return empty_frame(CONCEPT_BOARD_MEMBERS_COLUMNS)

    # ----------------------------------------------------------
    # 股票基础信息
    # ----------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取股票基础信息。

        Args:
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_BASIC_INFO_COLUMNS
        """
        return empty_frame(STOCK_BASIC_INFO_COLUMNS)

    # ----------------------------------------------------------
    # 资金流向
    # ----------------------------------------------------------

    def get_stock_fund_flow(
        self,
        trade_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """获取个股资金流向。

        Args:
            trade_date: 交易日期
            symbols: 股票代码列表

        Returns:
            DataFrame，列名遵循 STOCK_FUND_FLOW_COLUMNS
        """
        return empty_frame(STOCK_FUND_FLOW_COLUMNS)

    def get_board_fund_flow(self, trade_date: str) -> pd.DataFrame:
        """获取板块资金流向。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 BOARD_FUND_FLOW_COLUMNS
        """
        return empty_frame(BOARD_FUND_FLOW_COLUMNS)

    def get_earnings_forecasts(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> pd.DataFrame:
        """获取公司业绩预告方向与区间。"""
        return empty_frame(EARNINGS_FORECAST_COLUMNS)

    def get_finance_snapshot(
        self,
        symbols: list[str] | None = None,
        as_of: str | None = None,
        periods: int = 12,
    ) -> pd.DataFrame:
        """Return point-in-time financial statement metrics when supported."""
        return empty_frame(FINANCE_SNAPSHOT_COLUMNS)

    def get_consensus_estimates(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        return empty_frame(CONSENSUS_ESTIMATE_COLUMNS)

    def get_valuation_history(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> pd.DataFrame:
        return empty_frame(VALUATION_HISTORY_COLUMNS)

    # ----------------------------------------------------------
    # 涨停池
    # ----------------------------------------------------------

    def get_limit_up_pool(self, trade_date: str) -> pd.DataFrame:
        """获取涨停股池。

        Args:
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 LIMIT_UP_POOL_COLUMNS
        """
        return empty_frame(LIMIT_UP_POOL_COLUMNS)

    # ----------------------------------------------------------
    # 板块归属
    # ----------------------------------------------------------

    def get_stock_belong_boards(
        self,
        symbols: list[str],
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """获取股票所属板块。

        Args:
            symbols: 股票代码列表
            trade_date: 交易日期

        Returns:
            DataFrame，列名遵循 STOCK_BELONG_BOARDS_COLUMNS
        """
        return empty_frame(STOCK_BELONG_BOARDS_COLUMNS)

    # ----------------------------------------------------------
    # 公告 & 研报
    # ----------------------------------------------------------

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """获取个股公告列表。

        Args:
            code: 股票代码
            count: 返回条数

        Returns:
            公告字典列表
        """
        return []

    def get_research_reports(self, code: str, count: int = 5) -> list[dict[str, Any]]:
        """获取个股研报列表。

        Args:
            code: 股票代码
            count: 返回条数

        Returns:
            研报字典列表
        """
        return []

    # ----------------------------------------------------------
    # 热点 & 语义搜索
    # ----------------------------------------------------------

    def get_periodic_reports(
        self,
        code: str,
        report_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch annual, semiannual and quarterly reports for one stock."""
        return []

    def get_investor_interactions(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """Fetch investor relations Q&A / board secretary interactions."""
        return []

    def get_hot_themes(self) -> dict[str, Any]:
        """获取热点题材排行。"""
        return {"source": self.name, "industry": [], "concept": []}

    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        """语义搜索选股。

        Args:
            query: 自然语言查询
            count: 返回条数

        Returns:
            搜索结果字典
        """
        return {"source": self.name, "query": query, "results": [], "error": "不支持语义搜索"}
