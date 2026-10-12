"""
EvidenceService

本地证据库服务。
- 读取 company_evidence.csv
- 读取 manual_concept_members.csv
- 提供证据查询和汇总
- 所有查询降级处理，不崩溃
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from server.industry_chain.loader import project_root

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = project_root() / "knowledge" / "data_sources"
DEFAULT_OUTPUT_DIR = project_root() / "data" / "industry"


class EvidenceService:
    """证据服务。"""

    def __init__(self, data_dir: Path | None = None, output_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.output_dir = output_dir or DEFAULT_OUTPUT_DIR
        self._evidence_df: pd.DataFrame | None = None
        self._manual_df: pd.DataFrame | None = None

    def _load_evidence(self) -> pd.DataFrame:
        """加载证据数据，带缓存。"""
        if self._evidence_df is not None:
            return self._evidence_df

        # 优先读取同步后的 parquet
        parquet_path = self.output_dir / "company_evidence.parquet"
        csv_path = self.data_dir / "company_evidence.csv"

        try:
            if parquet_path.exists():
                self._evidence_df = pd.read_parquet(parquet_path)
            elif csv_path.exists():
                self._evidence_df = pd.read_csv(csv_path, dtype=str)
            else:
                self._evidence_df = pd.DataFrame()
        except Exception as exc:
            logger.warning(f"加载证据数据失败: {exc}")
            self._evidence_df = pd.DataFrame()

        return self._evidence_df

    def _load_manual(self) -> pd.DataFrame:
        """加载手工概念成员数据，带缓存。"""
        if self._manual_df is not None:
            return self._manual_df

        parquet_path = self.output_dir / "concept_members.parquet"
        csv_path = self.data_dir / "manual_concept_members.csv"

        try:
            if parquet_path.exists():
                self._manual_df = pd.read_parquet(parquet_path)
            elif csv_path.exists():
                self._manual_df = pd.read_csv(csv_path, dtype=str)
            else:
                self._manual_df = pd.DataFrame()
        except Exception as exc:
            logger.warning(f"加载手工概念成员失败: {exc}")
            self._manual_df = pd.DataFrame()

        return self._manual_df

    def get_node_evidence(self, chain_id: str, node_id: str) -> list[dict[str, Any]]:
        """获取指定节点的证据列表。"""
        df = self._load_evidence()
        if df.empty:
            return []

        try:
            filtered = df[
                (df.get("chain_id") == chain_id) &
                (df.get("node_id") == node_id)
            ]
            return filtered.to_dict("records")
        except Exception as exc:
            logger.warning(f"查询节点证据失败: {exc}")
            return []

    def get_stock_evidence(self, chain_id: str, node_id: str, symbol: str) -> list[dict[str, Any]]:
        """获取指定个股的详细证据。"""
        df = self._load_evidence()
        if df.empty:
            return []

        try:
            filtered = df[
                (df.get("chain_id") == chain_id) &
                (df.get("node_id") == node_id) &
                (df.get("symbol") == symbol)
            ]
            return filtered.to_dict("records")
        except Exception as exc:
            logger.warning(f"查询个股证据失败: {exc}")
            return []

    def get_node_evidence_summary(self, chain_id: str, node_id: str) -> dict[str, Any]:
        """获取节点证据汇总。"""
        evidence_list = self.get_node_evidence(chain_id, node_id)
        if not evidence_list:
            return {
                "evidence_count": 0,
                "verified_count": 0,
                "evidence_types": [],
                "latest_update": None,
            }

        verified_count = sum(1 for e in evidence_list if e.get("is_verified"))
        evidence_types = list({str(e.get("evidence_type", "")) for e in evidence_list if e.get("evidence_type")})
        updated_ats = [str(e.get("updated_at", "")) for e in evidence_list if e.get("updated_at")]
        latest_update = max(updated_ats) if updated_ats else None

        return {
            "evidence_count": len(evidence_list),
            "verified_count": verified_count,
            "evidence_types": evidence_types,
            "latest_update": latest_update,
        }

    def clear_cache(self) -> None:
        """清除缓存。"""
        self._evidence_df = None
        self._manual_df = None
