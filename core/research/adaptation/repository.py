"""SQLite transaction source for four-domain feedback, calibration and reports."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator


class AdaptationRepository:
    SCHEMA_VERSION = 1
    TABLES = {
        "domain_feedback": ("feedback_id", "domain"),
        "attribution_review_tasks": ("task_id", "source_domain"),
        "adaptation_calibrations": ("calibration_id", "domain"),
        "four_domain_reports": ("report_id", "as_of"),
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``ADAPTATION_DB_PATH`` 读取，默认
                ``data/research_control/adaptation.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "ADAPTATION_DB_PATH",
                "data/research_control/adaptation.db",
            )
        self.db_path = Path(db_path); self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock(); self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30); conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL"); yield conn; conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS adaptation_schema_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            for table, (pk, index_col) in self.TABLES.items():
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({pk} TEXT PRIMARY KEY,{index_col} TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT '',payload_json TEXT NOT NULL)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{index_col} ON {table}({index_col})")
            conn.execute("INSERT INTO adaptation_schema_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.SCHEMA_VERSION),))

    @staticmethod
    def payload(item: Any) -> dict[str, Any]:
        if hasattr(item, "to_dict"):
            data = item.to_dict()
        elif is_dataclass(item):
            data = asdict(item)
        elif isinstance(item, Mapping):
            data = dict(item)
        else:
            raise TypeError("adaptation payload must be a mapping or serializable object")
        if not isinstance(data, dict):
            raise TypeError("adaptation payload must be a mapping")
        return data

    def save(self, table: str, item: Any) -> None:
        if table not in self.TABLES: raise ValueError(f"unsupported table: {table}")
        pk, index_col = self.TABLES[table]; data = self.payload(item); key = str(data.get(pk) or "")
        if not key: raise ValueError(f"missing primary key: {pk}")
        with self._lock, self.connect() as conn:
            conn.execute(
                f"INSERT INTO {table}({pk},{index_col},created_at,payload_json) VALUES(?,?,?,?) ON CONFLICT({pk}) DO UPDATE SET {index_col}=excluded.{index_col},created_at=excluded.created_at,payload_json=excluded.payload_json",
                (
                    key,
                    str(data.get(index_col) or ""),
                    str(data.get("created_at") or ""),
                    json.dumps(data, ensure_ascii=False, allow_nan=False),
                ),
            )

    def save_feedback_and_review(self, feedback: Any, review: Any | None) -> None:
        """Atomically persist feedback and its cross-domain review request."""
        fb = self.payload(feedback); rv = self.payload(review) if review else None
        if not str(fb.get("feedback_id") or ""):
            raise ValueError("missing feedback_id")
        if rv is not None and not str(rv.get("task_id") or ""):
            raise ValueError("missing task_id")
        with self._lock, self.connect() as conn:
            conn.execute("INSERT INTO domain_feedback(feedback_id,domain,created_at,payload_json) VALUES(?,?,?,?) ON CONFLICT(feedback_id) DO UPDATE SET payload_json=excluded.payload_json", (fb["feedback_id"], fb.get("domain", ""), fb.get("created_at", ""), json.dumps(fb, ensure_ascii=False, allow_nan=False)))
            if rv:
                conn.execute("INSERT INTO attribution_review_tasks(task_id,source_domain,created_at,payload_json) VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json", (rv["task_id"], rv.get("source_domain", ""), rv.get("created_at", ""), json.dumps(rv, ensure_ascii=False, allow_nan=False)))

    def list(self, table: str, index_value: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if table not in self.TABLES: raise ValueError(f"unsupported table: {table}")
        _, index_col = self.TABLES[table]; query = f"SELECT payload_json FROM {table}"; params: list[Any] = []
        if index_value: query += f" WHERE {index_col}=?"; params.append(index_value)
        query += " ORDER BY rowid DESC LIMIT ?"
        try: normalized_limit = int(limit)
        except (TypeError, ValueError, OverflowError): normalized_limit = 100
        params.append(max(1, min(normalized_limit, 1000)))
        with self.connect() as conn: rows = conn.execute(query, params).fetchall()
        return [data for row in rows if (data := self._decode(row[0])) is not None]

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        if table not in self.TABLES: raise ValueError(f"unsupported table: {table}")
        if not isinstance(key, str) or not key:
            return None
        pk, _ = self.TABLES[table]
        with self.connect() as conn: row = conn.execute(f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)).fetchone()
        return self._decode(row[0]) if row else None

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        try: data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError): return None
        return data if isinstance(data, dict) else None

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn: counts = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in self.TABLES}
        return {"schema_version": self.SCHEMA_VERSION, "counts": counts}
