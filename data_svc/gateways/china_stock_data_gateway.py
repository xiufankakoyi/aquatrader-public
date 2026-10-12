"""中国 A 股数据网关，为投研管线提供统一数据入口。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from data_svc.ingestion.document_store import DocumentStore
from data_svc.unified_data_query import UnifiedDataQuery
from infrastructure.data_providers.provider_registry import ProviderRegistry
from infrastructure.data_providers.research.news_aggregator import NewsAggregator


class ChinaStockDataGateway:
    """整合行情、新闻、公告与事件存储的统一网关。"""

    def __init__(
        self,
        market_data: Optional[UnifiedDataQuery] = None,
        news: Optional[NewsAggregator] = None,
        event_store: Optional[DocumentStore] = None,
    ):
        """
        初始化网关。

        Args:
            market_data: 行情查询实例。
            news: 新闻聚合实例。
            event_store: 事件存储实例。
        """
        self.market_data = market_data or UnifiedDataQuery()
        self.news = news or NewsAggregator(registry=ProviderRegistry())
        self.event_store = event_store or DocumentStore()

    def get_market_snapshot(self, symbols: List[str]) -> Dict[str, Any]:
        """
        批量获取最近行情快照。

        Args:
            symbols: 股票代码列表。

        Returns:
            以 symbol 为键的最新行情字典。
        """
        result: Dict[str, Any] = {}
        for sym in symbols:
            try:
                df = self.market_data.get_stock_history(sym, None, None, limit=20)
                if df is not None and not df.empty:
                    result[sym] = df.iloc[-1].to_dict()
            except Exception:
                continue
        return result

    def get_daily_bars(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        批量获取日线数据。

        Args:
            symbols: 股票代码列表。
            start_date: 起始日期（YYYY-MM-DD）。
            end_date: 结束日期（YYYY-MM-DD）。

        Returns:
            合并后的日线数据框。
        """
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = start_date or end
        frames: List[pd.DataFrame] = []
        for sym in symbols:
            try:
                df = self.market_data.get_stock_history(sym, start, end)
                if df is not None and not df.empty:
                    if "symbol" not in df.columns:
                        df["symbol"] = sym
                    frames.append(df)
            except Exception:
                continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def get_news_feed(
        self,
        symbols: Optional[List[str]] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        获取新闻 feed，可按代码与时间窗口过滤。

        Args:
            symbols: 需要过滤的股票代码，None 表示全部。
            from_date: 起始日期。
            to_date: 结束日期。
            limit: 最大条数。

        Returns:
            新闻列表。
        """
        try:
            items = self.news.fetch_sina_news(pages=1) + self.news.fetch_ths_headlines()
        except Exception:
            items = []

        if symbols:
            keywords = {s.lower() for s in symbols}
            items = [
                it for it in items
                if any(kw in str(it.get("title", "")).lower() or
                       kw in str(it.get("content", "")).lower()
                       for kw in keywords)
            ]
        return items[:limit]

    def get_recent_events(
        self,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        """获取最近存储的投研事件。"""
        return self.event_store.query_events(event_type, limit)

    def get_realtime_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """获取实时行情快照。"""
        try:
            registry = ProviderRegistry()
            return registry.get_realtime_quotes(symbols)
        except Exception as e:
            return {"error": str(e)}
