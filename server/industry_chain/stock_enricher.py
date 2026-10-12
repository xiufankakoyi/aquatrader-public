"""Merge verified mappings, manual overrides and auto candidates for a node."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from server.data_sync.normalizer import normalize_symbols
from server.industry_chain.loader import MappingLoader, project_root

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = project_root() / "data" / "industry"
DEFAULT_KNOWLEDGE_DIR = project_root() / "knowledge"


class StockEnricher:
    """Build frontend stock rows without presenting auto candidates as verified."""

    def __init__(self, output_dir: Path | None = None, knowledge_dir: Path | None = None) -> None:
        self.output_dir = output_dir or DEFAULT_OUTPUT_DIR
        self.knowledge_dir = knowledge_dir or DEFAULT_KNOWLEDGE_DIR

    def get_node_stocks(
        self,
        chain_id: str,
        node_id: str,
        include_candidates: bool = True,
        verified_only: bool = False,
        sort_by: str = "system_relevance_score",
    ) -> list[dict[str, Any]]:
        symbol_map: dict[str, dict[str, Any]] = {}
        self._merge_verified_mappings(symbol_map, chain_id, node_id)
        self._merge_manual_overrides(symbol_map, chain_id, node_id)
        if include_candidates:
            self._merge_auto_candidates(symbol_map, chain_id, node_id)
        self._merge_evidence(symbol_map, chain_id, node_id)
        self._merge_market(symbol_map)
        self._merge_limit_up(symbol_map)
        self._merge_fund_flow(symbol_map)

        rows = list(symbol_map.values())
        if verified_only:
            rows = [row for row in rows if row.get("is_verified")]
        if not include_candidates:
            rows = [row for row in rows if not row.get("is_candidate")]
        rows.sort(key=lambda item: _sort_value(item, sort_by), reverse=True)
        return rows

    def _merge_verified_mappings(self, symbol_map: dict[str, dict[str, Any]], chain_id: str, node_id: str) -> None:
        for filename in ("stock_concept_mapping.csv", "concept_stock_mapping.csv"):
            mapping_path = self.knowledge_dir / filename
            if not mapping_path.exists():
                continue
            try:
                mappings = MappingLoader(mapping_path=mapping_path).load_mappings(chain_id=chain_id, node_id=node_id)
                for mapping in mappings:
                    if not mapping.symbol:
                        continue
                    item = _default_row(chain_id, node_id, mapping.symbol, mapping.stock_name)
                    item.update(
                        {
                            "source": "local_mapping",
                            "data_source": filename,
                            "is_verified": True,
                            "is_candidate": False,
                            "flag_label": "已验证",
                            "chain_position": mapping.chain_position,
                            "relevance_score": mapping.relevance_score,
                            "purity_score": mapping.purity_score,
                            "evidence_score": mapping.evidence_score,
                            "market_confirm_score": mapping.market_confirm_score,
                            "final_score": mapping.final_score,
                            "system_relevance_score": mapping.final_score,
                            "evidence_type": mapping.evidence_type,
                            "evidence_text": mapping.evidence_text,
                            "evidence_source": mapping.evidence_source,
                            "updated_at": mapping.updated_at,
                        }
                    )
                    symbol_map[mapping.symbol] = item
            except Exception as exc:
                logger.warning("Load local mapping %s failed: %s", mapping_path, exc)

    def _merge_manual_overrides(self, symbol_map: dict[str, dict[str, Any]], chain_id: str, node_id: str) -> None:
        manual_path = self.knowledge_dir / "data_sources" / "manual_concept_members.csv"
        if not manual_path.exists():
            return
        try:
            df = pd.read_csv(manual_path, dtype=str)
            if "symbol" in df.columns:
                df["symbol"] = normalize_symbols(df["symbol"])
            filtered = df[(df.get("chain_id") == chain_id) & (df.get("node_id") == node_id)]
            for _, row in filtered.iterrows():
                symbol = str(row.get("symbol", ""))
                if not symbol:
                    continue
                item = symbol_map.get(symbol, _default_row(chain_id, node_id, symbol, str(row.get("stock_name", ""))))
                item.update(
                    {
                        "source": "manual_override",
                        "data_source": "manual_concept_members.csv",
                        "source_concept_name": str(row.get("source_concept_name", "")),
                        "matched_board_name": str(row.get("source_concept_name", "")),
                        "is_verified": True,
                        "is_candidate": False,
                        "is_manual_override": True,
                        "flag_label": "人工 override",
                        "final_score": max(_to_float(item.get("final_score")), 0.8),
                        "system_relevance_score": max(_to_float(item.get("system_relevance_score")), 0.8),
                        "evidence_text": str(row.get("notes", item.get("evidence_text", ""))),
                        "updated_at": str(row.get("updated_at", item.get("updated_at", ""))),
                    }
                )
                symbol_map[symbol] = item
        except Exception as exc:
            logger.warning("Load manual overrides failed: %s", exc)

    def _merge_auto_candidates(self, symbol_map: dict[str, dict[str, Any]], chain_id: str, node_id: str) -> None:
        candidates = self._load_parquet("industry_node_candidates.parquet")
        if candidates.empty:
            return
        filtered = candidates[(candidates.get("chain_id") == chain_id) & (candidates.get("node_id") == node_id)]
        for _, row in filtered.iterrows():
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue
            score = _to_float(row.get("auto_relevance_score"))
            item = symbol_map.get(symbol, _default_row(chain_id, node_id, symbol, str(row.get("stock_name", ""))))
            if not item.get("is_verified"):
                item.update(
                    {
                        "source": str(row.get("candidate_source", "auto_concept_board")),
                        "data_source": str(row.get("provider", "")),
                        "is_verified": False,
                        "is_candidate": True,
                        "flag_label": "自动候选",
                        "final_score": score,
                        "system_relevance_score": score,
                    }
                )
            item.update(
                {
                    "source_concept_name": str(row.get("matched_board_name", "")),
                    "matched_board_name": str(row.get("matched_board_name", "")),
                    "matched_keyword": str(row.get("matched_keyword", "")),
                    "candidate_source": str(row.get("candidate_source", "")),
                    "source_confidence": _to_float(row.get("source_confidence")),
                    "auto_relevance_score": score,
                    "provider": str(row.get("provider", "")),
                    "updated_at": str(row.get("updated_at", item.get("updated_at", ""))),
                }
            )
            symbol_map[symbol] = item

    def _merge_evidence(self, symbol_map: dict[str, dict[str, Any]], chain_id: str, node_id: str) -> None:
        evidence = self._load_evidence()
        if evidence.empty:
            return
        filtered = evidence[(evidence.get("chain_id") == chain_id) & (evidence.get("node_id") == node_id)]
        for _, row in filtered.iterrows():
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue
            item = symbol_map.get(symbol, _default_row(chain_id, node_id, symbol, str(row.get("stock_name", ""))))
            confidence = _to_float(row.get("confidence"))
            item.update(
                {
                    "is_verified": True,
                    "is_candidate": False,
                    "flag_label": "已验证",
                    "evidence_type": str(row.get("evidence_type", "")),
                    "evidence_text": str(row.get("evidence_text", "")),
                    "evidence_source": str(row.get("evidence_source", "")),
                    "evidence_score": max(_to_float(item.get("evidence_score")), confidence),
                    "final_score": max(_to_float(item.get("final_score")), confidence),
                    "system_relevance_score": max(_to_float(item.get("system_relevance_score")), confidence),
                    "updated_at": str(row.get("updated_at", item.get("updated_at", ""))),
                }
            )
            symbol_map[symbol] = item

    def _merge_market(self, symbol_map: dict[str, dict[str, Any]]) -> None:
        market = self._load_parquet("market_snapshot.parquet")
        if market.empty:
            market = self._load_parquet("stock_market_snapshot.parquet")
        if market.empty:
            return
        market_map = {str(row.get("symbol", "")): row for _, row in market.iterrows()}
        for symbol, item in symbol_map.items():
            row = market_map.get(symbol)
            if row is None:
                continue
            item["pct_chg"] = _nullable_float(row.get("pct_chg"))
            item["amount"] = _nullable_float(row.get("amount"))
            item["turnover_rate"] = _nullable_float(row.get("turnover_rate"))
            item["volume_ratio"] = _nullable_float(row.get("volume_ratio"))
            if row.get("provider"):
                item["market_provider"] = str(row.get("provider", ""))

    def _merge_limit_up(self, symbol_map: dict[str, dict[str, Any]]) -> None:
        limit_pool = self._load_parquet("limit_up_pool.parquet")
        if limit_pool.empty:
            return
        limit_map = {str(row.get("symbol", "")): row for _, row in limit_pool.iterrows()}
        for symbol, item in symbol_map.items():
            row = limit_map.get(symbol)
            if row is None:
                item["is_limit_up"] = False
                continue
            item["is_limit_up"] = True
            item["limit_status"] = "涨停"
            item["consecutive_limit_count"] = int(_to_float(row.get("consecutive_limit_count")))
            item["limit_up_reason"] = str(row.get("limit_up_reason", ""))

    def _merge_fund_flow(self, symbol_map: dict[str, dict[str, Any]]) -> None:
        flow = self._load_parquet("stock_fund_flow.parquet")
        if flow.empty:
            return
        flow_map = {str(row.get("symbol", "")): row for _, row in flow.iterrows()}
        for symbol, item in symbol_map.items():
            row = flow_map.get(symbol)
            if row is None:
                continue
            item["main_net_inflow"] = _nullable_float(row.get("main_net_inflow"))
            item["fund_flow_provider"] = str(row.get("provider", ""))

    def _load_parquet(self, filename: str) -> pd.DataFrame:
        path = self.output_dir / filename
        try:
            if path.exists():
                return pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Load %s failed: %s", path, exc)
        return pd.DataFrame()

    def _load_evidence(self) -> pd.DataFrame:
        parquet = self._load_parquet("company_evidence.parquet")
        if not parquet.empty:
            return parquet
        csv_path = self.knowledge_dir / "data_sources" / "company_evidence.csv"
        try:
            if csv_path.exists():
                df = pd.read_csv(csv_path, dtype=str)
                if "symbol" in df.columns:
                    df["symbol"] = normalize_symbols(df["symbol"])
                return df
        except Exception as exc:
            logger.warning("Load company evidence failed: %s", exc)
        return pd.DataFrame()


def _default_row(chain_id: str, node_id: str, symbol: str, stock_name: str) -> dict[str, Any]:
    return {
        "chain_id": chain_id,
        "node_id": node_id,
        "symbol": symbol,
        "stock_name": stock_name,
        "chain_position": "",
        "source": "",
        "data_source": "",
        "source_concept_name": "",
        "matched_board_name": "",
        "matched_keyword": "",
        "candidate_source": "",
        "provider": "",
        "is_verified": False,
        "is_candidate": False,
        "is_manual_override": False,
        "flag_label": "自动候选",
        "relevance_score": 0.0,
        "purity_score": 0.0,
        "evidence_score": 0.0,
        "market_confirm_score": 0.0,
        "final_score": 0.0,
        "system_relevance_score": 0.0,
        "source_confidence": 0.0,
        "auto_relevance_score": 0.0,
        "evidence_type": "",
        "evidence_text": "",
        "evidence_source": "",
        "updated_at": "",
        "pct_chg": None,
        "return_5d": None,
        "amount": None,
        "amount_change_ratio": None,
        "turnover_rate": None,
        "volume_ratio": None,
        "limit_status": None,
        "is_limit_up": False,
        "consecutive_limit_count": 0,
        "limit_up_reason": "",
        "main_net_inflow": None,
        "market_provider": "",
        "fund_flow_provider": "",
        "pattern_signal": None,
    }


def _sort_value(item: dict[str, Any], sort_by: str) -> float:
    if sort_by in {"final_score", "system_relevance_score"}:
        return _to_float(item.get("system_relevance_score", item.get("final_score")))
    if sort_by == "hot_score":
        return _to_float(item.get("pct_chg")) + min(max(_to_float(item.get("main_net_inflow")) / 1e8, -5), 5)
    if sort_by == "pct_chg":
        return _to_float(item.get("pct_chg"), -9999)
    if sort_by == "amount":
        return _to_float(item.get("amount"))
    if sort_by == "main_net_inflow":
        return _to_float(item.get("main_net_inflow"))
    return _to_float(item.get("system_relevance_score", item.get("final_score")))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _nullable_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
