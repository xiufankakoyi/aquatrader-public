"""每日 PIT 板块特征 SQLite 持久化存储。

持久化每日板块的 phase/分数等特征，as_of 严格 PIT（不得写入未来日期）。
未来表现标签不写入此表，只在 EvaluationLabelStore 中生成。

PIT 安全规则：
- as_of 不得晚于今天（写入未来日期会被拒绝）
- 同一 (as_of, sector_id, feature_version) 唯一，重复写入幂等
- 历史查询返回 as_of <= requested 的最新版本
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .contracts import SectorFeatureRecord

logger = logging.getLogger(__name__)


def _today_utc() -> str:
    """返回今天的 UTC 日期（YYYYMMDD）。"""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _normalize_as_of(value: str) -> str:
    """将日期字符串归一化为 YYYYMMDD 格式。"""
    return str(value or "").replace("-", "").replace(":", "").replace("T", "")[:8]


class SectorFeatureStore:
    """每日 PIT 板块特征 SQLite 存储。

    持久化每日板块特征，as_of 严格 PIT。所有写操作通过 RLock 串行化。
    """

    SCHEMA_VERSION = 1
    DEFAULT_DB_PATH = "data/theme_control/sector_features.db"

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化存储并执行 schema 迁移。

        Args:
            db_path: 数据库路径，默认为 data/theme_control/sector_features.db。
        """
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接上下文，启用 WAL 模式。"""
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
        """创建表与索引（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sector_features ("
                "record_id TEXT PRIMARY KEY, "
                "as_of TEXT NOT NULL, "
                "sector_id TEXT NOT NULL, "
                "sector_name TEXT NOT NULL DEFAULT '', "
                "phase TEXT NOT NULL DEFAULT 'unknown', "
                "phase_confidence REAL NOT NULL DEFAULT 0.0, "
                "momentum_score REAL NOT NULL DEFAULT 0.0, "
                "breadth_score REAL NOT NULL DEFAULT 0.0, "
                "turnover_score REAL NOT NULL DEFAULT 0.0, "
                "dispersion_score REAL NOT NULL DEFAULT 0.0, "
                "feature_version TEXT NOT NULL DEFAULT '', "
                "policy_version TEXT NOT NULL DEFAULT '', "
                "degraded INTEGER NOT NULL DEFAULT 0, "
                "degraded_fields_json TEXT NOT NULL DEFAULT '[]', "
                "extra_json TEXT NOT NULL DEFAULT '{}', "
                "created_at TEXT NOT NULL DEFAULT '')"
            )
            # (as_of, sector_id, feature_version) 唯一索引，保证幂等
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_feature_unique "
                "ON sector_features(as_of, sector_id, feature_version)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sector_feature_as_of "
                "ON sector_features(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sector_feature_sector_id "
                "ON sector_features(sector_id)"
            )

    def save(self, record: SectorFeatureRecord) -> bool:
        """保存每日板块特征记录。

        PIT 安全规则：
        - as_of 不得晚于今天（写入未来日期会被拒绝）
        - 同一 (as_of, sector_id, feature_version) 唯一，重复写入幂等跳过

        Args:
            record: 板块特征记录。

        Returns:
            True 表示新写入；False 表示因 PIT 违规或重复而跳过。
        """
        normalized_as_of = _normalize_as_of(record.as_of)
        if not normalized_as_of:
            logger.warning("保存板块特征失败：as_of 为空")
            return False
        # PIT 检查：不得写入未来日期
        today = _today_utc()
        if normalized_as_of > today:
            logger.warning(
                "保存板块特征被拒绝：as_of=%s 晚于今天=%s（PIT 违规）",
                normalized_as_of, today,
            )
            return False

        degraded_fields_json = json.dumps(record.degraded_fields, ensure_ascii=False)
        extra_json = json.dumps(record.extra, ensure_ascii=False)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with self._lock, self._connect() as conn:
                # 幂等：同一 (as_of, sector_id, feature_version) 已存在则跳过
                existing = conn.execute(
                    "SELECT 1 FROM sector_features "
                    "WHERE as_of=? AND sector_id=? AND feature_version=?",
                    (normalized_as_of, record.sector_id, record.feature_version),
                ).fetchone()
                if existing:
                    return False
                conn.execute(
                    "INSERT INTO sector_features ("
                    "record_id, as_of, sector_id, sector_name, phase, "
                    "phase_confidence, momentum_score, breadth_score, "
                    "turnover_score, dispersion_score, feature_version, "
                    "policy_version, degraded, degraded_fields_json, "
                    "extra_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.record_id,
                        normalized_as_of,
                        record.sector_id,
                        record.sector_name,
                        record.phase,
                        record.phase_confidence,
                        record.momentum_score,
                        record.breadth_score,
                        record.turnover_score,
                        record.dispersion_score,
                        record.feature_version,
                        record.policy_version,
                        1 if record.degraded else 0,
                        degraded_fields_json,
                        extra_json,
                        created_at,
                    ),
                )
            return True
        except Exception as exc:
            logger.warning("保存板块特征失败: %s", exc)
            return False

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SectorFeatureRecord:
        """将数据库行转换为 SectorFeatureRecord 实例。"""
        try:
            degraded_fields = json.loads(row["degraded_fields_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            degraded_fields = []
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        return SectorFeatureRecord(
            record_id=row["record_id"],
            as_of=row["as_of"],
            sector_id=row["sector_id"],
            sector_name=row["sector_name"],
            phase=row["phase"],
            phase_confidence=row["phase_confidence"],
            momentum_score=row["momentum_score"],
            breadth_score=row["breadth_score"],
            turnover_score=row["turnover_score"],
            dispersion_score=row["dispersion_score"],
            feature_version=row["feature_version"],
            policy_version=row["policy_version"],
            degraded=bool(row["degraded"]),
            degraded_fields=list(degraded_fields),
            extra=extra,
        )

    def get_features_at(
        self,
        as_of: str,
        sector_id: str = "",
        feature_version: str = "",
    ) -> list[SectorFeatureRecord]:
        """查询某 PIT 时点的板块特征。

        PIT 查询规则：返回 as_of <= requested 的最新版本，不从未来倒推。

        Args:
            as_of: 请求的 PIT 时点（YYYYMMDD 或 ISO 日期）。
            sector_id: 可选的板块 ID 过滤。
            feature_version: 可选的特征版本过滤。

        Returns:
            板块特征记录列表。
        """
        normalized = _normalize_as_of(as_of)
        if not normalized:
            return []
        with self._lock, self._connect() as conn:
            if sector_id and feature_version:
                rows = conn.execute(
                    "SELECT * FROM sector_features "
                    "WHERE as_of <= ? AND sector_id=? AND feature_version=? "
                    "ORDER BY as_of DESC",
                    (normalized, sector_id, feature_version),
                ).fetchall()
            elif sector_id:
                rows = conn.execute(
                    "SELECT * FROM sector_features "
                    "WHERE as_of <= ? AND sector_id=? "
                    "ORDER BY as_of DESC, feature_version",
                    (normalized, sector_id),
                ).fetchall()
            elif feature_version:
                rows = conn.execute(
                    "SELECT * FROM sector_features "
                    "WHERE as_of <= ? AND feature_version=? "
                    "ORDER BY as_of DESC, sector_id",
                    (normalized, feature_version),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sector_features "
                    "WHERE as_of <= ? ORDER BY as_of DESC, sector_id",
                    (normalized,),
                ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_dates(self, feature_version: str = "") -> list[str]:
        """列出所有已持久化的日期（按升序）。

        Args:
            feature_version: 可选的特征版本过滤。

        Returns:
            日期列表（YYYYMMDD）。
        """
        with self._lock, self._connect() as conn:
            if feature_version:
                rows = conn.execute(
                    "SELECT DISTINCT as_of FROM sector_features "
                    "WHERE feature_version=? ORDER BY as_of ASC",
                    (feature_version,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT as_of FROM sector_features ORDER BY as_of ASC"
                ).fetchall()
        return [row["as_of"] for row in rows]
