"""QUANT 独立 SQLite 存储。

新建 quant.db 独立库，不与 Research 的 research_control.db 共享。
存储 QuantSignal 记录，支持按日期/状态/板块查询。

P0-5 修复要点：
1. Schema 版本化迁移：通过 SCHEMA_VERSION 增量升级，不破坏旧库
2. 审慎更新：save_signal 改为 INSERT 新记录 + UPDATE 已有记录，
   保留 created_at 审计字段，避免 INSERT OR REPLACE 覆盖创建时间
3. JSON 类型一致：reject_reasons/screening_result/decision_trace
   写入时序列化为 JSON 字符串，读取时反序列化回原类型
4. 追溯字段：新增 thesis_id/claim_id/company_id/mapping_confidence/
   evidence_as_of/strategy_version 列，支持审计回溯
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from core.quant.contracts import (
    QuantSignal,
    RejectReason,
    ScreeningResult,
    SectorPhase,
    SignalStatus,
)

logger = logging.getLogger(__name__)

# Schema 版本（每次结构变更必须递增）
SCHEMA_VERSION = 2

# 默认数据库路径
DEFAULT_DB_PATH = "data/quant/quant.db"


class QuantRepository:
    """QUANT SQLite 存储仓库。

    管理 QuantSignal 的持久化，独立于 Research 仓库。
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        """初始化 QUANT 仓库。

        Args:
            db_path: SQLite 数据库路径。
        """
        self._db_path = str(db_path)
        self._ensure_schema()
        self._migrate_schema()

    def _ensure_schema(self) -> None:
        """创建数据库 schema（幂等）。"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quant_signals (
                    signal_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    sector_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sector_phase TEXT NOT NULL,
                    research_score REAL DEFAULT 0,
                    reject_reasons TEXT DEFAULT '[]',
                    screening_result TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT DEFAULT '',
                    degraded INTEGER DEFAULT 0,
                    decision_trace TEXT DEFAULT '[]',
                    updated_at TEXT DEFAULT '',
                    -- P0-4 追溯字段
                    thesis_id TEXT DEFAULT '',
                    claim_id TEXT DEFAULT '',
                    company_id TEXT DEFAULT '',
                    mapping_confidence REAL DEFAULT 0,
                    evidence_as_of TEXT DEFAULT '',
                    strategy_version TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quant_signals_date ON quant_signals(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quant_signals_status ON quant_signals(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quant_signals_sector ON quant_signals(sector_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quant_signals_symbol ON quant_signals(symbol)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quant_signals_candidate ON quant_signals(candidate_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quant_schema_version (
                    version INTEGER PRIMARY KEY
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO quant_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    def _migrate_schema(self) -> None:
        """增量迁移：根据当前版本号补充缺失的列。

        v1 → v2：新增追溯字段（thesis_id/claim_id/company_id/
        mapping_confidence/evidence_as_of/strategy_version）。
        """
        with sqlite3.connect(self._db_path) as conn:
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(quant_signals)").fetchall()
            }
            # v2 追溯字段
            v2_columns = [
                ("thesis_id", "TEXT DEFAULT ''"),
                ("claim_id", "TEXT DEFAULT ''"),
                ("company_id", "TEXT DEFAULT ''"),
                ("mapping_confidence", "REAL DEFAULT 0"),
                ("evidence_as_of", "TEXT DEFAULT ''"),
                ("strategy_version", "TEXT DEFAULT ''"),
            ]
            for col_name, col_type in v2_columns:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE quant_signals ADD COLUMN {col_name} {col_type}"
                    )
                    logger.info("迁移：新增列 %s", col_name)

            # 更新 schema 版本号
            current = conn.execute(
                "SELECT MAX(version) FROM quant_schema_version"
            ).fetchone()[0]
            if current is None or current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO quant_schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )

    def save_signal(self, signal: QuantSignal) -> bool:
        """保存 QuantSignal。

        P0-5：改用审慎更新策略。
        - 新信号（signal_id 不存在）：INSERT，记录 created_at
        - 已有信号：UPDATE 业务字段，保留 created_at 不变，更新 updated_at

        Args:
            signal: QuantSignal 实例。

        Returns:
            True 表示保存成功。
        """
        try:
            reject_reasons_json = json.dumps(
                [r.value for r in signal.reject_reasons], ensure_ascii=False
            )
            screening_json = (
                json.dumps(signal.to_dict().get("screening_result"), ensure_ascii=False)
                if signal.screening_result
                else None
            )
            trace_json = json.dumps(signal.decision_trace, ensure_ascii=False)
            now = signal.created_at

            with sqlite3.connect(self._db_path) as conn:
                # 检查是否已存在
                existing = conn.execute(
                    "SELECT created_at FROM quant_signals WHERE signal_id = ?",
                    (signal.signal_id,),
                ).fetchone()

                if existing is None:
                    # 新记录：INSERT，created_at 由本次写入
                    conn.execute(
                        """
                        INSERT INTO quant_signals
                        (signal_id, candidate_id, symbol, sector_id, as_of, status,
                         sector_phase, research_score, reject_reasons, screening_result,
                         created_at, expires_at, degraded, decision_trace, updated_at,
                         thesis_id, claim_id, company_id, mapping_confidence,
                         evidence_as_of, strategy_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal.signal_id,
                            signal.candidate_id,
                            signal.symbol,
                            signal.sector_id,
                            signal.as_of,
                            signal.status.value,
                            signal.sector_phase.value,
                            round(signal.research_score, 4),
                            reject_reasons_json,
                            screening_json,
                            now,
                            signal.expires_at,
                            1 if signal.degraded else 0,
                            trace_json,
                            now,
                            signal.thesis_id,
                            signal.claim_id,
                            signal.company_id,
                            round(signal.mapping_confidence, 4),
                            signal.evidence_as_of,
                            signal.strategy_version,
                        ),
                    )
                else:
                    # 已有记录：UPDATE 业务字段，保留 created_at
                    conn.execute(
                        """
                        UPDATE quant_signals SET
                            candidate_id = ?,
                            symbol = ?,
                            sector_id = ?,
                            as_of = ?,
                            status = ?,
                            sector_phase = ?,
                            research_score = ?,
                            reject_reasons = ?,
                            screening_result = ?,
                            expires_at = ?,
                            degraded = ?,
                            decision_trace = ?,
                            updated_at = ?,
                            thesis_id = ?,
                            claim_id = ?,
                            company_id = ?,
                            mapping_confidence = ?,
                            evidence_as_of = ?,
                            strategy_version = ?
                        WHERE signal_id = ?
                        """,
                        (
                            signal.candidate_id,
                            signal.symbol,
                            signal.sector_id,
                            signal.as_of,
                            signal.status.value,
                            signal.sector_phase.value,
                            round(signal.research_score, 4),
                            reject_reasons_json,
                            screening_json,
                            signal.expires_at,
                            1 if signal.degraded else 0,
                            trace_json,
                            now,
                            signal.thesis_id,
                            signal.claim_id,
                            signal.company_id,
                            round(signal.mapping_confidence, 4),
                            signal.evidence_as_of,
                            signal.strategy_version,
                            signal.signal_id,
                        ),
                    )
            return True
        except Exception as exc:
            logger.error("保存 QuantSignal 失败: %s", exc)
            return False

    def save_signals_batch(self, signals: list[QuantSignal]) -> int:
        """批量保存 QuantSignal。

        Args:
            signals: QuantSignal 列表。

        Returns:
            成功保存的数量。
        """
        saved = 0
        for signal in signals:
            if self.save_signal(signal):
                saved += 1
        return saved

    def list_signals(
        self,
        *,
        as_of: str = "",
        status: str = "",
        sector_id: str = "",
        symbol: str = "",
        candidate_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询 QuantSignal 列表。

        Args:
            as_of: 按日期过滤（精确匹配）。
            status: 按状态过滤。
            sector_id: 按板块过滤。
            symbol: 按股票代码过滤。
            candidate_id: 按候选 ID 过滤（P0-4 新增）。
            limit: 返回上限。

        Returns:
            信号字典列表（JSON 字段已反序列化）。
        """
        query = "SELECT * FROM quant_signals WHERE 1=1"
        params: list[Any] = []

        if as_of:
            query += " AND as_of = ?"
            params.append(as_of)
        if status:
            query += " AND status = ?"
            params.append(status)
        if sector_id:
            query += " AND sector_id = ?"
            params.append(sector_id)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if candidate_id:
            query += " AND candidate_id = ?"
            params.append(candidate_id)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        """获取单个 QuantSignal。

        Args:
            signal_id: 信号 ID。

        Returns:
            信号字典或 None（JSON 字段已反序列化）。
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM quant_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_ready_signals(self, as_of: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """获取 READY 状态信号。

        Args:
            as_of: 按日期过滤。
            limit: 返回上限。

        Returns:
            READY 信号字典列表。
        """
        return self.list_signals(as_of=as_of, status=SignalStatus.READY.value, limit=limit)

    def list_watch_signals(self, as_of: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """获取 WATCH 状态信号。

        Args:
            as_of: 按日期过滤。
            limit: 返回上限。

        Returns:
            WATCH 信号字典列表。
        """
        return self.list_signals(as_of=as_of, status=SignalStatus.WATCH.value, limit=limit)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """把数据库行转换为字典，反序列化 JSON 字段。

        P0-5：确保 reject_reasons/screening_result/decision_trace
        读取后保持原生类型（list/dict），而非字符串。

        Args:
            row: sqlite3.Row 行。

        Returns:
            反序列化后的字典。
        """
        data = dict(row)
        # 反序列化 JSON 字段
        for field in ("reject_reasons", "decision_trace"):
            raw = data.get(field)
            if isinstance(raw, str):
                try:
                    data[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[field] = []
            elif raw is None:
                data[field] = []

        # screening_result 可空
        sr_raw = data.get("screening_result")
        if isinstance(sr_raw, str):
            try:
                data["screening_result"] = json.loads(sr_raw)
            except (json.JSONDecodeError, TypeError):
                data["screening_result"] = None
        elif sr_raw is None:
            data["screening_result"] = None

        # 布尔字段：SQLite 存为 0/1，转回 bool
        if "degraded" in data:
            data["degraded"] = bool(data["degraded"])

        return data
