"""P2.6 差异化观点 SQLite 仓储。

按 P2 计划 §208-227 Task P2.6：
- 验收点 3：观点版本不可覆盖
  - UNIQUE(thesis_id) 不可覆盖
  - revision_of 形成版本链
  - supersede(old_id, new_id) 标记旧版本为 superseded
- PIT 过滤：list_by_symbol(symbol, as_of, status, limit)
- 线程安全：WAL 模式 + RLock
- 幂等迁移：CREATE TABLE IF NOT EXISTS + 业务列 ALTER TABLE
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .contracts import (
    THESIS_STATUSES,
    THESIS_STATUS_ACTIVE,
    THESIS_STATUS_DRAFT,
    THESIS_STATUS_INVALIDATED,
    THESIS_STATUS_SUPERSEDED,
    THESIS_STATUS_EXPIRED,
    VariantPerception,
)


SCHEMA_VERSION: int = 1


def _safe_limit(value: Any, *, default: int = 100, maximum: int = 1000) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(limit, maximum))


def _decode_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

TABLES: dict[str, tuple[str, ...]] = {
    "variant_perceptions": ("thesis_id", "symbol"),
    "thesis_schema_meta": ("key",),
}


class ThesisRepository:
    """差异化观点 SQLite 仓储。

    验收点 3：观点版本不可覆盖。
    - save() 在 thesis_id 已存在时抛 ValueError
    - supersede(old_id, new_id) 仅标记旧版本 status=superseded，不删除数据
    - 旧版本数据完全不变，可通过 get(old_id) 查询
    """

    def __init__(self, db_path: str | Path) -> None:
        """初始化仓储。

        Args:
            db_path: SQLite 数据库路径
        """
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        # 确保父目录存在
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ------------------------------------------------------------------
    # 连接与迁移
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """打开一个连接（启用 WAL + 外键）。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _migrate(self) -> None:
        """幂等迁移。"""
        with self._lock, self.connect() as conn:
            # variant_perceptions 表
            conn.execute(
                "CREATE TABLE IF NOT EXISTS variant_perceptions("
                "thesis_id TEXT PRIMARY KEY NOT NULL,"
                "symbol TEXT NOT NULL,"
                "created_at TEXT NOT NULL,"
                "payload_json TEXT NOT NULL,"
                "as_of TEXT NOT NULL DEFAULT '',"
                "status TEXT NOT NULL DEFAULT '',"
                "version INTEGER NOT NULL DEFAULT 1,"
                "revision_of TEXT NOT NULL DEFAULT '',"
                "market_source_ref TEXT NOT NULL DEFAULT '',"
                "internal_source_ref TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variant_perceptions_symbol "
                "ON variant_perceptions(symbol, as_of, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variant_perceptions_revision "
                "ON variant_perceptions(revision_of)"
            )
            # 业务列幂等迁移
            cols = {row[1] for row in conn.execute("PRAGMA table_info(variant_perceptions)").fetchall()}
            for col in ("as_of", "status", "version", "revision_of", "market_source_ref", "internal_source_ref"):
                if col not in cols:
                    conn.execute(
                        f"ALTER TABLE variant_perceptions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                    )

            # schema_meta 表
            conn.execute(
                "CREATE TABLE IF NOT EXISTS thesis_schema_meta("
                "key TEXT PRIMARY KEY NOT NULL,"
                "value TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT INTO thesis_schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # 写入：save / supersede / update_status
    # ------------------------------------------------------------------

    def save(self, thesis: VariantPerception) -> None:
        """保存差异化观点。

        验收点 3：观点版本不可覆盖。
        - thesis_id 已存在时抛 ValueError（不覆盖）
        - 同 symbol 可有多条记录（不同 thesis_id）

        Args:
            thesis: VariantPerception 对象

        Raises:
            ValueError: thesis_id 已存在
        """
        if not isinstance(thesis, VariantPerception):
            raise TypeError("thesis must be a VariantPerception")
        thesis_id = thesis.thesis_id
        if not thesis_id:
            raise ValueError("missing primary key: thesis_id")

        payload = thesis.to_dict()
        symbol = thesis.symbol
        created_at = thesis.created_at
        as_of = thesis.as_of
        status = thesis.status
        version = thesis.version
        revision_of = thesis.revision_of
        market_source_ref = thesis.market_view.source_ref
        internal_source_ref = thesis.internal_view.source_ref

        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM variant_perceptions WHERE thesis_id=?",
                (thesis_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"thesis_id already exists: {thesis_id}")

            conn.execute(
                "INSERT INTO variant_perceptions("
                "thesis_id, symbol, created_at, payload_json, as_of, status, "
                "version, revision_of, market_source_ref, internal_source_ref"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    thesis_id,
                    symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    as_of,
                    status,
                    version,
                    revision_of,
                    market_source_ref,
                    internal_source_ref,
                ),
            )

    def supersede(self, *, old_thesis_id: str, new_thesis_id: str) -> bool:
        """标记旧版本为 superseded。

        验收点 3：版本不可覆盖。supersede 仅修改旧版本 status 字段，不删除数据。
        旧版本 thesis_id 保持不变，可通过 get(old_thesis_id) 查询。

        Args:
            old_thesis_id: 旧版本 thesis_id
            new_thesis_id: 新版本 thesis_id（必须已存在）

        Returns:
            True 表示标记成功；False 表示旧版本不存在或新版本不存在
        """
        with self._lock, self.connect() as conn:
            old_row = conn.execute(
                "SELECT payload_json FROM variant_perceptions WHERE thesis_id=?",
                (old_thesis_id,),
            ).fetchone()
            if old_row is None:
                return False
            new_row = conn.execute(
                "SELECT 1 FROM variant_perceptions WHERE thesis_id=?",
                (new_thesis_id,),
            ).fetchone()
            if new_row is None:
                return False

            # 更新旧版本 payload 中 status 字段 + 数据库 status 字段
            payload = _decode_payload(old_row["payload_json"])
            if payload is None:
                return False
            payload["status"] = THESIS_STATUS_SUPERSEDED
            conn.execute(
                "UPDATE variant_perceptions SET status=?, payload_json=? WHERE thesis_id=?",
                (
                    THESIS_STATUS_SUPERSEDED,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    old_thesis_id,
                ),
            )
            return True

    def update_status(self, thesis_id: str, new_status: str) -> bool:
        """更新观点状态。

        Args:
            thesis_id: 观点 ID
            new_status: 新状态（必须在 THESIS_STATUSES 中）

        Returns:
            True 表示更新成功；False 表示观点不存在或状态非法
        """
        if new_status not in THESIS_STATUSES:
            return False
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM variant_perceptions WHERE thesis_id=?",
                (thesis_id,),
            ).fetchone()
            if row is None:
                return False
            payload = _decode_payload(row["payload_json"])
            if payload is None:
                return False
            payload["status"] = new_status
            conn.execute(
                "UPDATE variant_perceptions SET status=?, payload_json=? WHERE thesis_id=?",
                (new_status, json.dumps(payload, ensure_ascii=False, allow_nan=False), thesis_id),
            )
            return True

    # ------------------------------------------------------------------
    # 读取：get / list
    # ------------------------------------------------------------------

    def get(self, thesis_id: str) -> dict[str, Any] | None:
        """按 thesis_id 查询。"""
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM variant_perceptions WHERE thesis_id=?",
                (thesis_id,),
            ).fetchone()
            if row is None:
                return None
            return _decode_payload(row["payload_json"])

    def list_by_symbol(
        self,
        *,
        symbol: str,
        as_of: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按公司代码列表查询（支持 PIT + status 过滤）。

        Args:
            symbol: 公司代码
            as_of: PIT 时点（None 表示不过滤；非空时仅返回 as_of <= 此值的记录）
            status: 状态过滤（None 表示不过滤）
            limit: 返回上限

        Returns:
            payload 字典列表
        """
        if not symbol:
            return []
        clauses = ["symbol=?"]
        params: list[Any] = [symbol]
        if as_of:
            clauses.append("(as_of='' OR as_of<=?)")
            params.append(as_of)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = (
            "SELECT payload_json FROM variant_perceptions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(_safe_limit(limit))
        with self._lock, self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                payload
                for row in rows
                if (payload := _decode_payload(row["payload_json"])) is not None
            ]

    def list_latest_by_symbol(
        self,
        *,
        symbol: str,
        as_of: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按公司代码查询最新版本（排除 superseded）。

        Args:
            symbol: 公司代码
            as_of: PIT 时点
            limit: 返回上限

        Returns:
            payload 字典列表（不含 superseded 状态）
        """
        if not symbol:
            return []
        clauses = ["symbol=?", "status<>?"]
        params: list[Any] = [symbol, THESIS_STATUS_SUPERSEDED]
        if as_of:
            clauses.append("(as_of='' OR as_of<=?)")
            params.append(as_of)
        sql = (
            "SELECT payload_json FROM variant_perceptions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY version DESC, created_at DESC LIMIT ?"
        )
        params.append(_safe_limit(limit))
        with self._lock, self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                payload
                for row in rows
                if (payload := _decode_payload(row["payload_json"])) is not None
            ]

    def list_versions(self, thesis_id: str) -> list[dict[str, Any]]:
        """沿 revision_of 链回溯所有版本。

        从给定 thesis_id 开始，沿 revision_of 链向前回溯（旧版本），
        然后向后查找所有以给定 thesis_id 为 revision_of 的记录（新版本）。

        Args:
            thesis_id: 起始观点 ID

        Returns:
            所有相关版本（按 version 升序）
        """
        with self._lock, self.connect() as conn:
            # 向前回溯（旧版本）
            chain: list[dict[str, Any]] = []
            current_id = thesis_id
            seen: set[str] = set()
            while current_id and current_id not in seen:
                seen.add(current_id)
                row = conn.execute(
                    "SELECT payload_json FROM variant_perceptions WHERE thesis_id=?",
                    (current_id,),
                ).fetchone()
                if row is None:
                    break
                payload = _decode_payload(row["payload_json"])
                if payload is None:
                    break
                chain.append(payload)
                current_id = payload.get("revision_of", "")

            # 向后查找（新版本）
            new_versions: list[dict[str, Any]] = []
            queue = [thesis_id]
            while queue:
                next_queue: list[str] = []
                for tid in queue:
                    rows = conn.execute(
                        "SELECT payload_json FROM variant_perceptions WHERE revision_of=?",
                        (tid,),
                    ).fetchall()
                    for r in rows:
                        payload = _decode_payload(r["payload_json"])
                        payload_id = payload.get("thesis_id", "") if payload else ""
                        if not payload_id or payload_id in seen:
                            continue
                        seen.add(payload_id)
                        new_versions.append(payload)
                        next_queue.append(payload_id)
                queue = next_queue

            all_versions = chain + new_versions
            all_versions.sort(key=lambda p: p.get("version", 1))
            return all_versions

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """返回仓储统计信息。"""
        with self._lock, self.connect() as conn:
            counts: dict[str, int] = {}
            for status in THESIS_STATUSES:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM variant_perceptions WHERE status=?",
                    (status,),
                ).fetchone()
                counts[status] = int(row["n"]) if row else 0
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM variant_perceptions"
            ).fetchone()
            counts["total"] = int(total_row["n"]) if total_row else 0

            sv_row = conn.execute(
                "SELECT value FROM thesis_schema_meta WHERE key='schema_version'"
            ).fetchone()
            schema_version = int(sv_row["value"]) if sv_row else 0

            return {
                "schema_version": schema_version,
                "counts": counts,
            }
