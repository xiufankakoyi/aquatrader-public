"""Perilla Discovery Engine - 市场范围标签器.

对公司候选进行市场分类标注，识别核心市场（A 股/美股）与
非核心市场，并为非核心市场生成风险提示。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Optional, Tuple

from .models import MARKETS, CompanyCandidate

logger = logging.getLogger(__name__)

# 核心市场集合
_CORE_MARKETS = {"A_SHARE", "US"}

# 市场 -> 风险提示映射
_MARKET_WARNINGS = {
    "HK": "港股市场，非A股核心标的",
    "JP": "日股市场，非A股核心标的",
    "TW": "台股市场，非A股核心标的",
    "KR": "韩股市场，非A股核心标的",
    "EU": "欧股市场，非A股核心标的",
    "OTHER": "非核心市场标的，需人工确认",
    "UNKNOWN": "非核心市场标的，需人工确认",
}


def _field(value: object, name: str, default: object = "") -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default


class MarketScopeTagger:
    """市场范围标签器.

    对公司候选的市场归属进行分类标注，识别核心市场与非核心市场，
    并为非核心市场生成相应的风险提示信息。
    """

    def tag_market(
        self, candidate: CompanyCandidate,
    ) -> Tuple[str, Optional[str]]:
        """对公司候选进行市场标注.

        Args:
            candidate: 公司候选对象。

        Returns:
            (market, market_warning) 元组：
            - market: 标准化市场标识。
            - market_warning: 市场风险提示，核心市场为 None。
        """
        market = _field(candidate, "market")
        market = market.strip().upper() if isinstance(market, str) else ""
        company_name = _field(candidate, "company_name")

        # 标准化市场标识
        if market not in MARKETS:
            logger.warning(
                "Unknown market '%s' for %s, defaulting to UNKNOWN",
                market, company_name,
            )
            market = "UNKNOWN"

        # 核心市场无警告
        if self.is_core_market(market):
            return market, None

        # 非核心市场生成警告
        warning = _MARKET_WARNINGS.get(market, "非核心市场标的，需人工确认")
        logger.debug(
            "Non-core market for %s: %s -> %s",
            company_name, market, warning,
        )
        return market, warning

    @staticmethod
    def is_core_market(market: str) -> bool:
        """判断是否为核心市场.

        核心市场包括 A 股和美股。

        Args:
            market: 市场标识。

        Returns:
            True 表示核心市场，False 表示非核心市场。
        """
        return isinstance(market, str) and market.strip().upper() in _CORE_MARKETS
