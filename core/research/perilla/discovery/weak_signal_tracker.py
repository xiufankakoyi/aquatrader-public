"""Track weak concept/company signals without upgrading supplier evidence."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List

from .evidence_search_task_generator import SAFE_EVIDENCE_LEVELS


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        return []
    try:
        values = list(value)
    except Exception:
        return []
    return [item for item in values if isinstance(item, Mapping)]


class WeakSignalTracker:
    """Build weak signal records from discovered concepts and local candidates."""

    def build_signals(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        company_candidates: Iterable[Dict[str, Any]] | None = None,
        inbox_items: Iterable[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        seen: set[str] = set()

        concepts = _rows(discovered_concepts)
        for concept in concepts:
            signals.extend(self._signals_for_concept(concept, seen))

        concept_by_name = {
            _text(item.get("normalized_name") or item.get("concept_name")): item
            for item in concepts
            if _text(item.get("normalized_name") or item.get("concept_name"))
        }
        for candidate in _rows(company_candidates):
            signal = self._signal_for_candidate(candidate, concept_by_name)
            if signal and signal["signal_id"] not in seen:
                seen.add(signal["signal_id"])
                signals.append(signal)

        for inbox_item in _rows(inbox_items):
            signal = self._signal_for_inbox_item(inbox_item)
            if signal and signal["signal_id"] not in seen:
                seen.add(signal["signal_id"])
                signals.append(signal)

        signals.sort(
            key=lambda item: (-_to_float(item.get("priority_score")), item.get("signal_id", ""))
        )
        return signals

    def identify_market_anomalies(
        self,
        market_records: Iterable[Dict[str, Any]],
        known_news_symbols: Iterable[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """Identify abnormal quote records from already available market data."""
        known_symbols = {_text(symbol) for symbol in known_news_symbols or [] if _text(symbol)}
        anomalies: list[dict[str, Any]] = []
        for record in _rows(market_records):
            symbol = _text(record.get("symbol") or record.get("code"))
            company = _text(record.get("company_name") or record.get("name"))
            change_pct = _to_float(
                record.get("change_pct", record.get("pct_chg", record.get("涨跌幅")))
            )
            amount = _to_float(record.get("amount", record.get("成交额")))
            turnover = _to_float(record.get("turnover_rate", record.get("换手率")))
            absolute_change_pct = abs(change_pct)
            if absolute_change_pct < 7.0 and amount < 500000000 and turnover < 8.0:
                continue
            if symbol in known_symbols:
                continue
            score = min(max(absolute_change_pct / 10.0, amount / 1000000000, turnover / 15.0), 1.0)
            anomalies.append(
                {
                    "anomaly_id": f"ma_{_short_hash(symbol + company)}",
                    "symbol": symbol,
                    "company_name": company,
                    "concept": _text(record.get("concept") or record.get("industry")),
                    "root_theme": _text(record.get("root_theme")),
                    "change_pct": change_pct,
                    "amount": amount,
                    "turnover_rate": turnover,
                    "anomaly_score": round(score, 4),
                    "status": "needs_reverse_search",
                    "detected_at": datetime.now().isoformat(),
                }
            )
        anomalies.sort(key=lambda item: (-item["anomaly_score"], item["symbol"]))
        return anomalies

    def _signals_for_concept(
        self,
        concept: Dict[str, Any],
        seen: set[str],
    ) -> List[Dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(concept, Mapping):
            return rows
        concept_name = _text(concept.get("normalized_name") or concept.get("concept_name"))
        root_theme = _text(concept.get("root_theme"))
        if not concept_name:
            return rows
        companies = concept.get("related_companies", [])
        if isinstance(companies, str):
            companies = [companies]
        for company in companies if isinstance(companies, (list, tuple)) else []:
            company_name = _text(company)
            if not company_name:
                continue
            signal = self._build_signal(
                concept_name=concept_name,
                root_theme=root_theme,
                company_name=company_name,
                signal_type="\u6587\u672c\u5171\u73b0",
                evidence_level=self._safe_level(
                    _text(concept.get("evidence_level"), "HYPOTHESIS")
                ),
                signal_summary=(
                    f"{company_name} \u4e0e {concept_name} "
                    f"\u5728 inbox/\u6982\u5ff5\u7ebf\u7d22\u4e2d\u5171\u73b0\uff0c"
                    "\u9700\u8981\u72ec\u7acb\u8bc1\u636e\u9a8c\u8bc1\u3002"
                ),
                source_items=concept.get("source_items", []) or [],
                priority_score=_to_float(concept.get("concept_score")),
            )
            if signal["signal_id"] in seen:
                continue
            seen.add(signal["signal_id"])
            rows.append(signal)
        return rows

    def _signal_for_candidate(
        self,
        candidate: Dict[str, Any],
        concept_by_name: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(candidate, Mapping):
            return {}
        company = self._format_company(candidate)
        if not company:
            return {}
        text = " ".join(
            _text(value)
            for value in [
                candidate.get("claim_summary"),
                candidate.get("mapping_sources"),
                candidate.get("concept_id"),
            ]
            if value
        )
        concept_name = ""
        for name in concept_by_name:
            if name and name in text:
                concept_name = name
                break
        if not concept_name:
            concept_name = _text(candidate.get("concept_id"), "\u672a\u5339\u914d\u6982\u5ff5")
        return self._build_signal(
            concept_name=concept_name,
            root_theme=_text(concept_by_name.get(concept_name, {}).get("root_theme")),
            company_name=company,
            signal_type="\u5019\u9009\u516c\u53f8\u58f0\u660e",
            evidence_level=self._safe_level(
                _text(candidate.get("evidence_level"), "HYPOTHESIS")
            ),
            signal_summary=_text(candidate.get("claim_summary")),
            source_items=[
                {
                    "candidate_id": candidate.get("candidate_id", ""),
                    "evidence_ids": candidate.get("evidence_ids", []),
                    "mapping_sources": candidate.get("mapping_sources", []),
                }
            ],
            priority_score=0.45,
        )

    def _signal_for_inbox_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(item, Mapping):
            return {}
        company = self._format_company(item)
        concept = _text(item.get("concept") or item.get("main_dish"))
        if not company or not concept:
            return {}
        priority = min(_to_float(item.get("research_priority_score")) / 100.0, 1.0)
        return self._build_signal(
            concept_name=concept,
            root_theme="",
            company_name=company,
            signal_type="\u6536\u4ef6\u7bb1\u7ebf\u7d22",
            evidence_level=self._safe_level(
                _text(item.get("evidence_level"), "HYPOTHESIS")
            ),
            signal_summary=_text(item.get("bottleneck_logic") or item.get("notes")),
            source_items=[
                {
                    "item_id": item.get("item_id", ""),
                    "chain_node": item.get("chain_node", ""),
                    "inbox_status": item.get("inbox_status", ""),
                    "evidence_links": item.get("evidence_links", []),
                }
            ],
            priority_score=max(priority, 0.25),
        )

    @staticmethod
    def _build_signal(
        concept_name: str,
        root_theme: str,
        company_name: str,
        signal_type: str,
        evidence_level: str,
        signal_summary: str,
        source_items: List[Dict[str, Any]],
        priority_score: float,
    ) -> Dict[str, Any]:
        key = f"{_text(company_name)}|{_text(concept_name)}|{_text(signal_type)}"
        return {
            "signal_id": f"ws_{_short_hash(key)}",
            "concept_name": _text(concept_name),
            "root_theme": _text(root_theme),
            "company_name": _text(company_name),
            "signal_type": _text(signal_type),
            "signal_summary": _text(signal_summary),
            "source_items": _rows(source_items)[:10],
            "priority_score": round(min(max(_to_float(priority_score), 0.0), 1.0), 4),
            "status": "pending_verification",
            "last_checked_at": datetime.now().isoformat(),
            "evidence_level": evidence_level,
        }

    @staticmethod
    def _safe_level(level: str) -> str:
        return level if level in SAFE_EVIDENCE_LEVELS else "HYPOTHESIS"

    @staticmethod
    def _format_company(item: Dict[str, Any]) -> str:
        if not isinstance(item, Mapping):
            return ""
        company = _text(item.get("company_name"))
        symbol = _text(item.get("symbol"))
        if not company:
            return ""
        if symbol and symbol not in company and symbol not in {"\u672a\u4e0a\u5e02", "None", "null"}:
            return f"{company}({symbol})"
        return company


def _short_hash(text: str) -> str:
    return hashlib.sha1(_text(text).encode("utf-8")).hexdigest()[:12]


def _to_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0
