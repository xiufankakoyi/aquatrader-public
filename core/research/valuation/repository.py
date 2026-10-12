"""SQLite 持久化：估值案例与报告。

SCHEMA_VERSION=1，幂等迁移。
- valuation_cases 表（case_id PK, company_symbol 索引, as_of PIT 过滤）
- valuation_reports 表（report_id PK, company_symbol 索引, as_of PIT 过滤）

提供 save_case / get_case / list_cases / save_report / get_report / list_reports 方法。
重启回读通过 to_dict/from_dict 往返测试覆盖。
"""
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

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ValuationRepository:
    """估值案例与报告的 SQLite 持久化仓储。"""

    SCHEMA_VERSION = 1
    TABLES = {
        "valuation_cases": ("case_id", "company_symbol", "as_of", "method"),
        "valuation_reports": ("report_id", "company_symbol", "as_of"),
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化仓储并执行幂等迁移。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``VALUATION_DB_PATH`` 读取，默认
                ``data/research_control/valuation.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "VALUATION_DB_PATH",
                str(PROJECT_ROOT / "data" / "research_control" / "valuation.db"),
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """获取 SQLite 连接上下文管理器。"""
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
        """幂等迁移：创建所有表与索引，并记录 schema 版本。"""
        with self._lock, self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS valuation_schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for table, (pk, index_col, as_of_col, *extra) in self.TABLES.items():
                # 通用列：PK / company_symbol / as_of / created_at / payload_json
                extra_cols_sql = ""
                if extra:
                    extra_cols_sql = ", " + ", ".join(
                        f"{c} TEXT NOT NULL DEFAULT ''" for c in extra
                    )
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, "
                    f"{index_col} TEXT NOT NULL DEFAULT '', "
                    f"{as_of_col} TEXT NOT NULL DEFAULT '', "
                    f"created_at TEXT NOT NULL DEFAULT '', "
                    f"payload_json TEXT NOT NULL"
                    f"{extra_cols_sql})"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{index_col} "
                    f"ON {table}({index_col})"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{as_of_col} "
                    f"ON {table}({as_of_col})"
                )
            conn.execute(
                "INSERT INTO valuation_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def _payload(item: Any) -> dict[str, Any]:
        """统一转为 dict payload。"""
        if hasattr(item, "to_dict"):
            payload = item.to_dict()
            if isinstance(payload, Mapping):
                return dict(payload)
            raise ValueError("payload must be a mapping")
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, Mapping):
            return dict(item)
        raise TypeError("item must be a mapping or serializable object")

    @staticmethod
    def _limit(limit: Any, default: int = 100) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 1000))

    @staticmethod
    def _decode_payload(raw: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    # ---- 通用 save/get/list ----

    def _save(self, table: str, item: Any) -> None:
        """通用保存方法。"""
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        pk, index_col, as_of_col, *extra_cols = self.TABLES[table]
        payload = self._payload(item)
        key = str(payload.get(pk) or "")
        if not key:
            raise ValueError(f"missing primary key: {pk}")
        index_value = str(payload.get(index_col) or "")
        as_of_value = str(payload.get(as_of_col) or "")
        created_at = str(payload.get("created_at") or "")
        extra_values = [str(payload.get(c) or "") for c in extra_cols]
        with self._lock, self.connect() as conn:
            cols = f"{pk},{index_col},{as_of_col},created_at,payload_json"
            placeholders = "?,?,?,?,?"
            if extra_cols:
                cols += "," + ",".join(extra_cols)
                placeholders += "," + ",".join(["?"] * len(extra_cols))
            updates = (
                f"{index_col}=excluded.{index_col}, "
                f"{as_of_col}=excluded.{as_of_col}, "
                f"created_at=excluded.created_at, "
                f"payload_json=excluded.payload_json"
            )
            if extra_cols:
                updates += ", " + ", ".join(f"{c}=excluded.{c}" for c in extra_cols)
            sql = (
                f"INSERT INTO {table}({cols}) VALUES({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}"
            )
            conn.execute(
                sql,
                (key, index_value, as_of_value, created_at,
                 json.dumps(payload, ensure_ascii=False, allow_nan=False), *extra_values),
            )

    def _get(self, table: str, key: str) -> dict[str, Any] | None:
        """通用单条查询。"""
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        pk = self.TABLES[table][0]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,),
            ).fetchone()
        return self._decode_payload(row[0]) if row else None

    def _list(
        self,
        table: str,
        *,
        index_value: str = "",
        as_of: str = "",
        method: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """通用列表查询。

        Args:
            table: 表名
            index_value: company_symbol 过滤
            as_of: PIT 过滤，仅返回 as_of <= 该时点的记录
            method: 方法过滤（仅 valuation_cases 表有效）
            limit: 上限，1-1000
        """
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        pk, index_col, as_of_col, *extra_cols = self.TABLES[table]
        clauses: list[str] = []
        params: list[Any] = []
        if index_value:
            clauses.append(f"{index_col}=?")
            params.append(index_value)
        if as_of:
            clauses.append(f"{as_of_col}<>'' AND {as_of_col}<=?")
            params.append(as_of)
        if method and "method" in extra_cols:
            clauses.append("method=?")
            params.append(method)
        where = " AND ".join(clauses) if clauses else ""
        query = f"SELECT payload_json FROM {table}"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(
                query, (*params, self._limit(limit)),
            ).fetchall()
        return [
            payload
            for row in rows
            if (payload := self._decode_payload(row[0])) is not None
        ]

    # ---- 估值案例 ----

    def save_case(self, case: Any) -> None:
        """保存估值案例。

        Args:
            case: ValuationCase 对象或 dict
        """
        self._save("valuation_cases", case)

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        """按 case_id 获取估值案例。"""
        return self._get("valuation_cases", case_id)

    def list_cases(
        self,
        *,
        company_symbol: str = "",
        method: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询估值案例列表，支持 symbol/method/as_of/limit 过滤。

        Args:
            company_symbol: 公司代码过滤
            method: 方法过滤
            as_of: PIT 过滤，仅返回 as_of <= 该时点的记录
            limit: 上限
        """
        return self._list(
            "valuation_cases",
            index_value=company_symbol,
            as_of=as_of,
            method=method,
            limit=limit,
        )

    # ---- 估值报告 ----

    def save_report(self, report: Any) -> None:
        """保存估值报告。

        Args:
            report: ValuationReport 对象或 dict
        """
        self._save("valuation_reports", report)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """按 report_id 获取估值报告。"""
        return self._get("valuation_reports", report_id)

    def list_reports(
        self,
        *,
        company_symbol: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询估值报告列表，支持 symbol/as_of/limit 过滤。"""
        return self._list(
            "valuation_reports",
            index_value=company_symbol,
            as_of=as_of,
            limit=limit,
        )

    def stats(self) -> dict[str, Any]:
        """返回仓储统计信息。"""
        with self.connect() as conn:
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self.TABLES
            }
        return {"schema_version": self.SCHEMA_VERSION, "counts": counts}
