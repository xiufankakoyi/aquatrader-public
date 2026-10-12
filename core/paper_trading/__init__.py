"""Paper trading module for daily pre-market research-driven simulation."""

from core.paper_trading.portfolio import Portfolio, TradeRecord
from core.paper_trading.selector import ResearchViewSelector
from core.paper_trading.data_source import AkSharePriceSource

__all__ = ["Portfolio", "TradeRecord", "ResearchViewSelector", "AkSharePriceSource"]
