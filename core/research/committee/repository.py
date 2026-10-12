"""P2.7 投委会工作流 SQLite 仓储。

按 P2 计划 §242-279 Task P2.7：
- 持久化 CommitteeWorkflow + reviews + audit_trail
- UNIQUE(workflow_id) 不可覆盖
- PIT 过滤：list_by_company / list_by_status / list_by_thesis
- 线程安全：WAL + RLock
- 幂等迁移
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .contracts import CommitteeWorkflow


SCHEMA_VERSION: int = 1
MAX_QUERY_LIMIT: int = 1000


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _workflow_payload(workflow: CommitteeWorkflow) -> dict[str, Any]:
    payload = workflow.to_dict()
    for field_name in (
        "workflow_id", "thesis_id", "company_symbol", "created_at", "as_of",
        "status", "lead_analyst_user_id",
    ):
        payload[field_name] = _text(payload.get(field_name))
    return payload

TABLES: dict[str, tuple[str, ...]] = {
    "committee_workflows": ("workflow_id", "thesis_id", "company_symbol"),
    "committee_reviews": ("review_id", "workflow_id"),
    "committee_audit_trail": ("audit_id", "workflow_id"),
    "committee_schema_meta": ("key",),
}


class CommitteeRepository:
    """投委会工作流 SQLite 仓储。"""

    def __init__(self, db_path: str | Path) -> None:
        """初始化仓储。

        Args:
            db_path: SQLite 数据库路径
        """
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ------------------------------------------------------------------
    # 连接与迁移
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """打开连接。"""
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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS committee_workflows("
                "workflow_id TEXT PRIMARY KEY NOT NULL,"
                "thesis_id TEXT NOT NULL DEFAULT '',"
                "company_symbol TEXT NOT NULL DEFAULT '',"
                "created_at TEXT NOT NULL,"
                "payload_json TEXT NOT NULL,"
                "as_of TEXT NOT NULL DEFAULT '',"
                "status TEXT NOT NULL DEFAULT '',"
                "lead_analyst_user_id TEXT NOT NULL DEFAULT '',"
                "invalidated_by_assumption_change INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_committee_workflows_thesis "
                "ON committee_workflows(thesis_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_committee_workflows_company "
                "ON committee_workflows(company_symbol, as_of, status)"
            )
            # 业务列幂等迁移
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(committee_workflows)").fetchall()
            }
            for col in (
                "thesis_id",
                "company_symbol",
                "as_of",
                "status",
                "lead_analyst_user_id",
                "invalidated_by_assumption_change",
            ):
                if col not in cols:
                    default = "0" if col == "invalidated_by_assumption_change" else "''"
                    conn.execute(
                        f"ALTER TABLE committee_workflows ADD COLUMN {col} "
                        f"{'INTEGER' if col == 'invalidated_by_assumption_change' else 'TEXT'} "
                        f"NOT NULL DEFAULT {default}"
                    )

            conn.execute(
                "CREATE TABLE IF NOT EXISTS committee_schema_meta("
                "key TEXT PRIMARY KEY NOT NULL,"
                "value TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT INTO committee_schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self, workflow: CommitteeWorkflow) -> None:
        """保存工作流。

        - workflow_id 已存在时抛 ValueError
        - 同 thesis_id 可有多条记录（不同 workflow_id）

        Args:
            workflow: CommitteeWorkflow 对象

        Raises:
            ValueError: workflow_id 已存在
        """
        if not isinstance(workflow, CommitteeWorkflow):
            raise ValueError("invalid workflow")
        payload = _workflow_payload(workflow)
        workflow_id = payload["workflow_id"]
        if not workflow_id:
            raise ValueError("missing primary key: workflow_id")

        thesis_id = payload["thesis_id"]
        company_symbol = payload["company_symbol"]
        created_at = payload["created_at"]
        as_of = payload["as_of"]
        status = payload["status"]
        lead_analyst = payload["lead_analyst_user_id"]
        invalidated = 1 if workflow.invalidated_by_assumption_change else 0

        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM committee_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"workflow_id already exists: {workflow_id}")

            conn.execute(
                "INSERT INTO committee_workflows("
                "workflow_id, thesis_id, company_symbol, created_at, payload_json, "
                "as_of, status, lead_analyst_user_id, invalidated_by_assumption_change"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    thesis_id,
                    company_symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    as_of,
                    status,
                    lead_analyst,
                    invalidated,
                ),
            )

    def update(self, workflow: CommitteeWorkflow) -> bool:
        """更新工作流（全字段覆盖）。

        Args:
            workflow: CommitteeWorkflow 对象

        Returns:
            True 表示更新成功；False 表示工作流不存在
        """
        if not isinstance(workflow, CommitteeWorkflow):
            return False
        payload = _workflow_payload(workflow)
        workflow_id = payload["workflow_id"]
        if not workflow_id:
            return False
        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM committee_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if existing is None:
                return False
            conn.execute(
                "UPDATE committee_workflows SET "
                "thesis_id=?, company_symbol=?, payload_json=?, as_of=?, status=?, "
                "lead_analyst_user_id=?, invalidated_by_assumption_change=? "
                "WHERE workflow_id=?",
                (
                    payload["thesis_id"],
                    payload["company_symbol"],
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    payload["as_of"],
                    payload["status"],
                    payload["lead_analyst_user_id"],
                    1 if workflow.invalidated_by_assumption_change else 0,
                    workflow_id,
                ),
            )
            return True

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        """按 workflow_id 查询。"""
        workflow_id = _text(workflow_id)
        if not workflow_id:
            return None
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM committee_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return None
            return self._decode_payload(row["payload_json"])

    def list_by_thesis(
        self,
        *,
        thesis_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按 thesis_id 列表查询。"""
        thesis_id = _text(thesis_id)
        if not thesis_id:
            return []
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM committee_workflows WHERE thesis_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (thesis_id, self._safe_limit(limit)),
            ).fetchall()
            return self._decode_rows(rows)

    def list_by_company(
        self,
        *,
        company_symbol: str,
        as_of: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按公司代码列表查询（支持 PIT + status 过滤）。"""
        company_symbol = _text(company_symbol)
        if not company_symbol:
            return []
        clauses = ["company_symbol=?"]
        params: list[Any] = [company_symbol]
        as_of = _text(as_of)
        status = _text(status)
        if as_of:
            clauses.append("(as_of='' OR as_of<=?)")
            params.append(as_of)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = (
            "SELECT payload_json FROM committee_workflows WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(self._safe_limit(limit))
        with self._lock, self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return self._decode_rows(rows)

    def list_by_status(
        self,
        *,
        status: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按状态列表查询。"""
        status = _text(status)
        if not status:
            return []
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM committee_workflows WHERE status=? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, self._safe_limit(limit)),
            ).fetchall()
            return self._decode_rows(rows)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """返回仓储统计信息。"""
        with self._lock, self.connect() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM committee_workflows"
            ).fetchone()
            total = int(total_row["n"]) if total_row else 0
            invalidated_row = conn.execute(
                "SELECT COUNT(*) AS n FROM committee_workflows "
                "WHERE invalidated_by_assumption_change=1"
            ).fetchone()
            invalidated = int(invalidated_row["n"]) if invalidated_row else 0
            sv_row = conn.execute(
                "SELECT value FROM committee_schema_meta WHERE key='schema_version'"
            ).fetchone()
            try:
                schema_version = int(sv_row["value"]) if sv_row else 0
            except (TypeError, ValueError, OverflowError):
                schema_version = 0
            return {
                "schema_version": schema_version,
                "total": total,
                "invalidated_by_assumption_change": invalidated,
            }

    @staticmethod
    def _safe_limit(value: Any) -> int:
        """把查询条数限制在有限范围内。"""
        try:
            return max(1, min(int(value), MAX_QUERY_LIMIT))
        except (TypeError, ValueError, OverflowError):
            return 100

    @staticmethod
    def _decode_payload(payload_json: Any) -> dict[str, Any] | None:
        """读取 JSON 载荷；坏行降级为 None，不阻塞其它记录。"""
        if not isinstance(payload_json, str):
            return None
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    @classmethod
    def _decode_rows(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = cls._decode_payload(row["payload_json"])
            if payload is not None:
                result.append(payload)
        return result
