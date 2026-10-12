"""SQLite 持久化 for ResearchCandidate。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class CandidateRepository:
    """ResearchCandidate SQLite 存储。"""

    SCHEMA_VERSION = 1
    TABLES = {
        "research_candidates": ("candidate_id", "symbol"),
        "candidate_reports": ("candidate_id", "symbol"),
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``CANDIDATES_DB_PATH`` 读取，默认
                ``data/research_control/candidates.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "CANDIDATES_DB_PATH",
                "data/research_control/candidates.db",
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """幂等迁移。"""
        with self._lock, self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS candidate_schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for table, (pk, index_col) in self.TABLES.items():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, {index_col} TEXT NOT NULL DEFAULT '', "
                    "created_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{index_col} ON {table}({index_col})"
                )
            conn.execute(
                "INSERT INTO candidate_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    def save_candidate(self, candidate: Any) -> None:
        """保存候选对象。"""
        converter = getattr(candidate, "to_dict", None)
        if callable(converter):
            payload = converter()
        elif isinstance(candidate, Mapping):
            payload = dict(candidate)
        else:
            payload = None
        if not isinstance(payload, dict):
            raise TypeError("candidate payload must be a mapping")
        key = _text(payload.get("candidate_id"))
        if not key:
            raise ValueError("missing candidate_id")
        index_value = _text(payload.get("symbol"))
        created_at = _text(payload.get("created_at"))
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO research_candidates(candidate_id,symbol,created_at,payload_json) "
                "VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
                "symbol=excluded.symbol,created_at=excluded.created_at,payload_json=excluded.payload_json",
                (
                    key,
                    index_value,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        candidate_id = _text(candidate_id)
        if not candidate_id:
            return None
        """查询单个候选。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM research_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return self._decode_payload(row[0]) if row else None

    def list_candidates(
        self, *, symbol: str = "", status: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询候选列表。"""
        query = "SELECT payload_json FROM research_candidates"
        conditions: list[str] = []
        params: list[Any] = []
        symbol = _text(symbol)
        status = _text(status)
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if status:
            conditions.append(
                "json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END,'$.status')=?"
            )
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY rowid DESC LIMIT ?"
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            normalized_limit = 100
        params.append(max(1, min(normalized_limit, 1000)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    def save_report(self, report: Any) -> None:
        """保存研究报告。"""
        converter = getattr(report, "to_dict", None)
        if callable(converter):
            payload = converter()
        elif isinstance(report, Mapping):
            payload = dict(report)
        else:
            payload = None
        if not isinstance(payload, dict):
            raise TypeError("report payload must be a mapping")
        key = _text(payload.get("candidate_id"))
        if not key:
            raise ValueError("missing candidate_id")
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO candidate_reports(candidate_id,symbol,created_at,payload_json) "
                "VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,created_at=excluded.created_at",
                (key, _text(payload.get("symbol")),
                 _text(payload.get("generated_at")),
                 json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )

    def get_report(self, candidate_id: str) -> dict[str, Any] | None:
        candidate_id = _text(candidate_id)
        if not candidate_id:
            return None
        """查询研究报告。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM candidate_reports WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return self._decode_payload(row[0]) if row else None

    @staticmethod
    def _decode_payload(raw: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def stats(self) -> dict[str, Any]:
        """返回存储统计。"""
        with self.connect() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self.TABLES
            }
        return {"schema_version": self.SCHEMA_VERSION, "counts": counts}
