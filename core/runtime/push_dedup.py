"""Persistent push deduplication store.

Avoids re-pushing the same state change to downstream channels within a
trade date, and survives process restarts by persisting to SQLite. Failed
pushes are recorded separately so they can be retried later.

Dedup key: ``(date, entity_id, state_change, policy_version)`` -- the same
quadruple is only pushed at most once per (date, policy_version) bucket.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PushDedupStore:
    """SQLite-backed deduplication store for outbound pushes.

    Args:
        db_path: SQLite 文件路径。``None`` 时从环境变量
            ``PUSH_DEDUP_DB_PATH`` 读取，默认 ``data/state/push_dedup.db``。
        table_name: 主表名，默认 ``pushed_records``。
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path: str | Path | None = None,
        table_name: str = "pushed_records",
    ) -> None:
        if db_path is None:
            db_path = os.getenv(
                "PUSH_DEDUP_DB_PATH",
                "data/state/push_dedup.db",
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.table_name = table_name
        self.failures_table = f"{table_name}_failures"
        self._lock = threading.RLock()
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create tables if missing. Idempotent and safe to call repeatedly."""
        with self._lock, self._connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
                "dedup_key TEXT PRIMARY KEY,"
                "trade_date TEXT NOT NULL,"
                "entity_id TEXT NOT NULL,"
                "state_change TEXT NOT NULL,"
                "policy_version TEXT NOT NULL,"
                "pushed_at TEXT NOT NULL,"
                "payload_json TEXT"
                ")"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table_name}_date "
                f"ON {self.table_name}(trade_date)"
            )
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.failures_table} ("
                "dedup_key TEXT PRIMARY KEY,"
                "trade_date TEXT NOT NULL,"
                "entity_id TEXT NOT NULL,"
                "state_change TEXT NOT NULL,"
                "policy_version TEXT NOT NULL,"
                "error TEXT NOT NULL,"
                "failed_at TEXT NOT NULL,"
                "attempt_count INTEGER NOT NULL DEFAULT 1,"
                "payload_json TEXT"
                ")"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.failures_table}_date "
                f"ON {self.failures_table}(trade_date)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) "
                "VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def build_key(
        *,
        date: str,
        entity_id: str,
        state_change: str,
        policy_version: str,
    ) -> str:
        """Build the canonical dedup key from the quadruple.

        Args:
            date: 交易日期 ``YYYY-MM-DD``。
            entity_id: 实体 ID（如 claim_id / theme_id / symbol）。
            state_change: 状态变更类型（如 ``READY_TO_WATCH``）。
            policy_version: 推送策略版本（如 ``push_policy_v1``）。

        Returns:
            形如 ``push_dedup:<sha1>`` 的稳定键。
        """
        raw = "|".join([
            str(date or ""),
            str(entity_id or ""),
            str(state_change or ""),
            str(policy_version or ""),
        ])
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return f"push_dedup:{digest}"

    def should_push(
        self,
        *,
        date: str,
        entity_id: str,
        state_change: str,
        policy_version: str,
    ) -> bool:
        """Decide whether a push should be sent for this quadruple.

        Returns ``False`` if the same key was already successfully pushed
        (i.e. recorded via ``record_push``). Returns ``True`` otherwise,
        including when there is a previously failed attempt waiting retry.
        """
        if not self._is_meaningful_state_change(state_change):
            logger.debug(
                "skip non-meaningful state_change=%s entity=%s",
                state_change, entity_id,
            )
            return False
        key = self.build_key(
            date=date,
            entity_id=entity_id,
            state_change=state_change,
            policy_version=policy_version,
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {self.table_name} WHERE dedup_key=?",
                (key,),
            ).fetchone()
        return row is None

    def record_push(
        self,
        *,
        date: str,
        entity_id: str,
        state_change: str,
        policy_version: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Record a successful push. Removes any prior failure entry.

        Returns the dedup key recorded.
        """
        key = self.build_key(
            date=date,
            entity_id=entity_id,
            state_change=state_change,
            policy_version=policy_version,
        )
        payload_text = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {self.table_name} "
                "(dedup_key, trade_date, entity_id, state_change, "
                "policy_version, pushed_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, str(date), str(entity_id), str(state_change),
                 str(policy_version), _utc_now_iso(), payload_text),
            )
            # Successful push clears any pending failure entry for the same key
            conn.execute(
                f"DELETE FROM {self.failures_table} WHERE dedup_key=?",
                (key,),
            )
        return key

    def record_failure(
        self,
        *,
        date: str,
        entity_id: str,
        state_change: str,
        policy_version: str,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Record a failed push attempt so it can be retried.

        The same key's attempt count is incremented when called repeatedly.
        Returns the dedup key recorded.
        """
        key = self.build_key(
            date=date,
            entity_id=entity_id,
            state_change=state_change,
            policy_version=policy_version,
        )
        payload_text = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                f"SELECT attempt_count FROM {self.failures_table} "
                "WHERE dedup_key=?",
                (key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    f"INSERT INTO {self.failures_table} "
                    "(dedup_key, trade_date, entity_id, state_change, "
                    "policy_version, error, failed_at, attempt_count, "
                    "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (key, str(date), str(entity_id), str(state_change),
                     str(policy_version), str(error), _utc_now_iso(),
                     payload_text),
                )
            else:
                conn.execute(
                    f"UPDATE {self.failures_table} "
                    "SET error=?, failed_at=?, attempt_count=attempt_count+1, "
                    "payload_json=? WHERE dedup_key=?",
                    (str(error), _utc_now_iso(), payload_text, key),
                )
        return key

    def list_failures(self, limit: int = 100) -> list[dict[str, Any]]:
        """List failed push records that are candidates for retry."""
        limit_int = max(1, int(limit))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT dedup_key, trade_date, entity_id, state_change, "
                f"policy_version, error, failed_at, attempt_count, payload_json "
                f"FROM {self.failures_table} "
                f"ORDER BY failed_at ASC LIMIT ?",
                (limit_int,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            payload_text = row["payload_json"] or ""
            try:
                payload = json.loads(payload_text) if payload_text else None
            except json.JSONDecodeError:
                payload = None
            output.append({
                "dedup_key": row["dedup_key"],
                "date": row["trade_date"],
                "entity_id": row["entity_id"],
                "state_change": row["state_change"],
                "policy_version": row["policy_version"],
                "error": row["error"],
                "failed_at": row["failed_at"],
                "attempt_count": int(row["attempt_count"]),
                "payload": payload,
            })
        return output

    def clear_failures(self, dedup_key: str) -> None:
        """Remove a failure entry after a successful retry."""
        with self._lock, self._connect() as conn:
            conn.execute(
                f"DELETE FROM {self.failures_table} WHERE dedup_key=?",
                (dedup_key,),
            )

    def purge_for_date(self, date: str) -> int:
        """Purge all dedup records for the given trade date.

        Useful for re-running the same-day pipeline after a manual reset.
        Returns the number of rows deleted.
        """
        with self._lock, self._connect() as conn:
            cur1 = conn.execute(
                f"DELETE FROM {self.table_name} WHERE trade_date=?",
                (str(date),),
            )
            cur2 = conn.execute(
                f"DELETE FROM {self.failures_table} WHERE trade_date=?",
                (str(date),),
            )
            return int(cur1.rowcount or 0) + int(cur2.rowcount or 0)

    @staticmethod
    def _is_meaningful_state_change(state_change: str) -> bool:
        """Filter out non-meaningful state changes.

        Empty / ``UNCHANGED`` / ``NO_CHANGE`` are considered non-meaningful
        and will never be pushed.
        """
        text = str(state_change or "").strip().upper()
        if not text:
            return False
        return text not in {"UNCHANGED", "NO_CHANGE", "NONE", "NULL"}
