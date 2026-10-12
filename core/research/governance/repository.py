"""P1.6 治理台账 SQLite 持久化。

参考 P1.4 EarningsRepository 模式：
- 版本化存储，按 (symbol, period, version) 唯一索引防止覆盖
- PIT 过滤：available_at <= as_of
- 幂等迁移
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    CapitalAction,
    CapitalAllocationImpact,
    EquityIncentive,
    GovernanceGap,
    GovernanceLedger,
    GovernanceRisk,
    ManagementCommitment,
    ManagementPerson,
    PersonnelAssessment,
    RelatedPartyTransaction,
    RiskEvent,
    ShareholderChange,
    new_id,
    utc_now,
)


class GovernanceRepository:
    """治理台账 SQLite 存储，带版本化和 PIT 过滤。

    SCHEMA_VERSION=1：初始版本，单表 governance_ledgers 存储完整台账 JSON。
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``GOVERNANCE_DB_PATH`` 读取，默认
                ``data/research_control/governance.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "GOVERNANCE_DB_PATH",
                "data/research_control/governance.db",
            )
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        self.db_path = path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """事务化连接。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """创建/升级 schema；可重复执行（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS governance_ledgers ("
                "ledger_id TEXT PRIMARY KEY, symbol TEXT NOT NULL DEFAULT '', "
                "period TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', "
                "as_of TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', "
                "payload_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_governance_ledgers_symbol "
                "ON governance_ledgers(symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_governance_ledgers_period "
                "ON governance_ledgers(period)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_governance_ledgers_as_of "
                "ON governance_ledgers(as_of)"
            )
            # 复合唯一索引：防止 (symbol, period, version) 被覆盖
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_ledgers_version_unique "
                "ON governance_ledgers(symbol, period, version)"
            )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    # ----- 写入 -----

    def save_ledger(
        self, ledger: GovernanceLedger, version: str = "v1"
    ) -> str:
        """保存治理台账。

        幂等：若 (symbol, period, version) 已存在则抛 ValueError，防止覆盖。

        Args:
            ledger: 治理台账
            version: 版本号（如 v1、v2）

        Returns:
            ledger_id

        Raises:
            ValueError: 若 (symbol, period, version) 已存在
        """
        if not isinstance(ledger, GovernanceLedger):
            raise TypeError("ledger must be GovernanceLedger")
        version = str(version or "").strip()
        if not version:
            raise ValueError("version 不能为空")
        if not ledger.symbol:
            raise ValueError("ledger.symbol 不能为空")
        if not ledger.period:
            raise ValueError("ledger.period 不能为空")
        ledger_id = new_id("ledger")
        if not ledger.available_at:
            raise ValueError("ledger.available_at 不能为空，且不得用报告期代替")
        as_of = ledger.available_at
        if not ledger.created_at:
            ledger.created_at = utc_now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT ledger_id FROM governance_ledgers "
                "WHERE symbol=? AND period=? AND version=?",
                (ledger.symbol, ledger.period, version),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"治理台账已存在: symbol={ledger.symbol}, "
                    f"period={ledger.period}, version={version}；"
                    "禁止覆盖，请使用新版本号"
                )
            conn.execute(
                "INSERT INTO governance_ledgers "
                "(ledger_id, symbol, period, version, as_of, created_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ledger_id,
                    ledger.symbol,
                    ledger.period,
                    version,
                    as_of,
                    ledger.created_at,
                    json.dumps(ledger.to_dict(), ensure_ascii=False, allow_nan=False),
                ),
            )
        return ledger_id

    # ----- 读取 -----

    def get_ledger(self, ledger_id: str) -> GovernanceLedger | None:
        """按 ledger_id 获取台账。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM governance_ledgers WHERE ledger_id=?",
                (ledger_id,),
            ).fetchone()
        if row is None:
            return None
        return self._safe_deserialize(row["payload_json"])

    def list_ledgers(
        self,
        *,
        symbol: str = "",
        period: str = "",
        version: str = "",
    ) -> list[GovernanceLedger]:
        """列出台账（按过滤条件）。

        Args:
            symbol: 公司代码过滤（空字符串 = 不过滤）
            period: 期间过滤
            version: 版本过滤

        Returns:
            台账列表，按 created_at 升序
        """
        clauses: list[str] = []
        params: list[str] = []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if period:
            clauses.append("period=?")
            params.append(period)
        if version:
            clauses.append("version=?")
            params.append(version)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM governance_ledgers {where} "
                "ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [
            ledger for row in rows
            if (ledger := self._safe_deserialize(row["payload_json"])) is not None
        ]

    def get_latest_ledger(
        self, *, symbol: str, as_of: str = ""
    ) -> GovernanceLedger | None:
        """获取最新可见台账（PIT 过滤）。

        Args:
            symbol: 公司代码
            as_of: PIT 时点；空表示取所有版本中最新

        Returns:
            最新台账或 None
        """
        clauses = ["symbol=?"]
        params: list[str] = [symbol]
        if as_of:
            clauses.append("as_of<=?")
            params.append(as_of)
        where = f"WHERE {' AND '.join(clauses)}"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM governance_ledgers {where} "
                "ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        return self._safe_deserialize(row["payload_json"])

    def list_ledger_versions(
        self, *, symbol: str, period: str
    ) -> list[GovernanceLedger]:
        """列出某 symbol + period 的所有版本（按 created_at 升序）。

        Args:
            symbol: 公司代码
            period: 期间

        Returns:
            台账版本列表
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM governance_ledgers "
                "WHERE symbol=? AND period=? "
                "ORDER BY created_at ASC",
                (symbol, period),
            ).fetchall()
        return [
            ledger for row in rows
            if (ledger := self._safe_deserialize(row["payload_json"])) is not None
        ]

    def stats(self) -> dict[str, Any]:
        """返回存储统计。"""
        with self._lock, self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM governance_ledgers"
            ).fetchone()[0]
            schema_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        return {
            "schema_version": int(schema_row["value"]) if schema_row else 0,
            "total_ledgers": total,
            "db_path": str(self.db_path),
        }

    # ----- 内部工具 -----

    @staticmethod
    def _deserialize(payload_json: str) -> GovernanceLedger:
        """从 JSON 反序列化为 GovernanceLedger。"""
        data = json.loads(payload_json)
        if not isinstance(data, Mapping):
            raise ValueError("governance payload must be an object")

        def build(model: type[Any], raw: Any) -> Any | None:
            if not isinstance(raw, Mapping):
                return None
            names = {item.name for item in fields(model)}
            values = {key: value for key, value in raw.items() if key in names}
            try:
                return model(**values)
            except (TypeError, ValueError):
                return None

        def many(model: type[Any], key: str) -> list[Any]:
            raw_items = data.get(key, [])
            if not isinstance(raw_items, list):
                return []
            return [item for raw in raw_items if (item := build(model, raw)) is not None]

        shareholder_changes: list[ShareholderChange] = []
        raw_changes = data.get("shareholder_changes", [])
        if isinstance(raw_changes, list):
            for raw in raw_changes:
                change = build(ShareholderChange, raw)
                if change is None:
                    continue
                change.person = build(
                    ManagementPerson,
                    raw.get("person") if isinstance(raw, Mapping) else None,
                )
                shareholder_changes.append(change)
        return GovernanceLedger(
            symbol=data.get("symbol", ""),
            company_name=data.get("company_name", ""),
            period=data.get("period", ""),
            available_at=data.get("available_at", ""),
            shareholder_changes=shareholder_changes,
            management_persons=many(ManagementPerson, "management_persons"),
            equity_incentives=many(EquityIncentive, "equity_incentives"),
            related_party_transactions=many(RelatedPartyTransaction, "related_party_transactions"),
            risk_events=many(RiskEvent, "risk_events"),
            capital_actions=many(CapitalAction, "capital_actions"),
            management_commitments=many(ManagementCommitment, "management_commitments"),
            personnel_assessments=many(PersonnelAssessment, "personnel_assessments"),
            governance_risks=many(GovernanceRisk, "governance_risks"),
            capital_allocation_impacts=many(CapitalAllocationImpact, "capital_allocation_impacts"),
            gaps=many(GovernanceGap, "gaps"),
            source_trace_id=data.get("source_trace_id", ""),
            schema_version=data.get("schema_version", "governance_v1"),
            created_at=data.get("created_at", ""),
        )

    @classmethod
    def _safe_deserialize(cls, payload_json: Any) -> GovernanceLedger | None:
        try:
            return cls._deserialize(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
