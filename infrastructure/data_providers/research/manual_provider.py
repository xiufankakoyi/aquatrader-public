"""
ManualProvider

从本地 CSV 读取手工维护的数据，包括：
- knowledge/data_sources/manual_concept_members.csv
- knowledge/data_sources/company_evidence.csv

作为最基础的数据源，不依赖任何外部服务，始终可用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from infrastructure.data_providers.base import BaseMarketDataProvider
from server.industry_chain.loader import project_root

logger = logging.getLogger(__name__)


class ManualProvider(BaseMarketDataProvider):
    """本地手工数据提供者。"""

    name = "manual"

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or (project_root() / "knowledge" / "data_sources")
        self._concept_members_path = self.data_dir / "manual_concept_members.csv"
        self._evidence_path = self.data_dir / "company_evidence.csv"

    def is_available(self) -> bool:
        """只要目录存在即认为可用（文件可以后续补充）。"""
        return self.data_dir.exists()

    def health(self) -> dict[str, Any]:
        """返回健康状态，包含文件是否存在。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "concept_members_exists": self._concept_members_path.exists(),
            "evidence_exists": self._evidence_path.exists(),
        }

    def get_concept_members(self, concept_name: str | None = None) -> pd.DataFrame:
        """读取手工维护的概念成分映射。"""
        if not self._concept_members_path.exists():
            logger.warning("manual_concept_members.csv 不存在，返回空 DataFrame")
            return pd.DataFrame(columns=[
                "source", "source_concept_name", "chain_id", "node_id",
                "symbol", "stock_name", "updated_at", "notes",
            ])

        try:
            df = pd.read_csv(self._concept_members_path, dtype=str)
            if concept_name:
                df = df[df["source_concept_name"].astype(str).str.contains(concept_name, na=False)]
            df["source"] = "manual"
            return df
        except Exception as exc:
            logger.warning(f"读取 manual_concept_members.csv 失败: {exc}")
            return pd.DataFrame(columns=[
                "source", "source_concept_name", "chain_id", "node_id",
                "symbol", "stock_name", "updated_at", "notes",
            ])

    def get_market_snapshot(self, trade_date: str | None = None) -> pd.DataFrame:
        """本地手工数据不提供行情。"""
        return pd.DataFrame(columns=[
            "symbol", "stock_name", "close", "pct_chg", "amount", "trade_date",
        ])

    def get_stock_profile(self, symbols: list[str]) -> pd.DataFrame:
        """本地手工数据不提供股票 profile。"""
        return pd.DataFrame(columns=[
            "symbol", "stock_name", "industry", "list_date",
        ])

    def get_news_titles(self, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
        """本地手工数据不提供新闻。"""
        return pd.DataFrame(columns=[
            "title", "source", "pub_date", "symbol",
        ])

    def get_company_evidence(self) -> pd.DataFrame:
        """
        读取本地公司证据库。

        Returns:
            DataFrame 包含字段:
                - chain_id
                - node_id
                - symbol
                - stock_name
                - evidence_type
                - evidence_text
                - evidence_source
                - confidence
                - updated_at
                - is_verified
        """
        if not self._evidence_path.exists():
            logger.warning("company_evidence.csv 不存在，返回空 DataFrame")
            return pd.DataFrame(columns=[
                "chain_id", "node_id", "symbol", "stock_name",
                "evidence_type", "evidence_text", "evidence_source",
                "confidence", "updated_at", "is_verified",
            ])

        try:
            df = pd.read_csv(self._evidence_path, dtype=str)
            df["is_verified"] = df["is_verified"].astype(str).str.lower().isin(["true", "1", "yes"])
            df["confidence"] = pd.to_numeric(df.get("confidence"), errors="coerce").fillna(0.0)
            return df
        except Exception as exc:
            logger.warning(f"读取 company_evidence.csv 失败: {exc}")
            return pd.DataFrame(columns=[
                "chain_id", "node_id", "symbol", "stock_name",
                "evidence_type", "evidence_text", "evidence_source",
                "confidence", "updated_at", "is_verified",
            ])
