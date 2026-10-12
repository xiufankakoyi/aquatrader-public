from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


class ExpectationContext:
    """Expose expectations only when their evidence exists and was visible as-of."""

    def __init__(
        self,
        expectation_db: str | Path = "data/research_control/expectations.db",
        research_db: str | Path = "data/research_control/research_control.db",
    ) -> None:
        self.expectation_db = Path(expectation_db)
        self.research_db = Path(research_db)

    @staticmethod
    def _visible(value: Any, as_of: str) -> bool:
        raw = str(value or "")[:10]
        if not raw:
            return False
        try:
            return datetime.fromisoformat(raw).date() <= datetime.fromisoformat(as_of[:10]).date()
        except (TypeError, ValueError):
            return False

    def _visible_evidence_ids(self, as_of: str) -> set[str]:
        if not self.research_db.exists():
            return set()
        connection = sqlite3.connect(self.research_db)
        try:
            rows = connection.execute("SELECT evidence_id, payload_json FROM evidence").fetchall()
        except sqlite3.OperationalError:
            return set()
        finally:
            connection.close()
        visible: set[str] = set()
        for evidence_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            visible_at = payload.get("published_at") or payload.get("retrieved_at")
            if self._visible(visible_at, as_of):
                visible.add(str(evidence_id))
        return visible

    def for_company(self, symbol: str, as_of: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "verified_expectations": [], "rejected_expectations": [],
            "gaps": [], "source": str(self.expectation_db),
        }
        if not self.expectation_db.exists():
            result["gaps"].append("expectation_store_missing")
            return result
        evidence_ids = self._visible_evidence_ids(as_of)
        connection = sqlite3.connect(self.expectation_db)
        try:
            rows = connection.execute(
                "SELECT payload_json FROM market_expectations WHERE symbol=? ORDER BY created_at",
                (symbol,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            connection.close()
        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            available_at = payload.get("available_at") or payload.get("published_at")
            raw_references = payload.get("evidence_ids", [])
            if isinstance(raw_references, str):
                raw_references = [raw_references]
            if not isinstance(raw_references, (list, tuple, set)):
                raw_references = []
            references = {str(item) for item in raw_references if item}
            rejection_reasons: list[str] = []
            if not self._visible(available_at, as_of):
                rejection_reasons.append("not_visible_as_of")
            if not references or not references.intersection(evidence_ids):
                rejection_reasons.append("evidence_not_found")
            item = {**payload, "validation": {"rejection_reasons": rejection_reasons}}
            target = "rejected_expectations" if rejection_reasons else "verified_expectations"
            result[target].append(item)
        if not result["verified_expectations"]:
            result["gaps"].append("verified_market_expectation_missing")
        return result
