"""MembershipEvidence SQLite 持久化存储。

存储公司—主题关系的事实证据，支持按公司/主题查询、已确认成员查询、
手动过期证据等操作。所有字段以独立列存储，locator 以 JSON 文本保存。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .contracts import (
    GROUNDING_CONFLICTED,
    GROUNDING_GROUNDED,
    GROUNDING_UNVERIFIED,
    MembershipEvidence,
    STANCE_COUNTER,
    STANCE_NEUTRAL,
    STANCE_SUPPORT,
    utc_now,
)


def _is_expired(valid_until: str, now: str) -> bool:
    """判断证据是否已过期。

    Args:
        valid_until: 有效截止时间（ISO 字符串，空串表示 UNKNOWN）。
        now: 当前时间（ISO 字符串）。

    Returns:
        True 表示已过期；valid_until 为空（UNKNOWN）时返回 False。
    """
    valid_until = str(valid_until or "")
    now = str(now or "")
    if not valid_until:
        return False
    try:
        valid_dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if valid_dt.tzinfo is None:
            valid_dt = valid_dt.replace(tzinfo=timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        return valid_dt < now_dt
    except (ValueError, TypeError):
        # 时间解析失败时按字符串比较，避免异常导致误判
        return bool(now) and valid_until < now


class MembershipEvidenceStore:
    """MembershipEvidence SQLite 存储。

    使用 WAL 模式提升并发读性能，所有写操作通过 RLock 串行化保证线程安全。
    表 membership_evidence 为所有字段建立独立列，locator 以 JSON 文本保存。
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path = "data/theme_control/membership_evidence.db") -> None:
        """初始化仓库并执行 schema 迁移。

        Args:
            db_path: SQLite 数据库文件路径，父目录会自动创建。
        """
        path = Path(db_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        self.db_path = path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接上下文。

        启用 WAL 日志模式，异常时回滚，正常退出时提交。
        """
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
        """创建 schema（幂等）。

        表 membership_evidence 包含所有 MembershipEvidence 字段，
        其中 locator 以 JSON 文本保存。建立 (symbol, theme_id) 联合索引
        和 valid_until 索引。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS membership_evidence ("
                "evidence_id TEXT PRIMARY KEY, "
                "symbol TEXT NOT NULL DEFAULT '', "
                "company_id TEXT NOT NULL DEFAULT '', "
                "theme_id TEXT NOT NULL DEFAULT '', "
                "stance TEXT NOT NULL DEFAULT '', "
                "source_type TEXT NOT NULL DEFAULT '', "
                "provider TEXT NOT NULL DEFAULT '', "
                "source_url TEXT NOT NULL DEFAULT '', "
                "document_id TEXT NOT NULL DEFAULT '', "
                "document_version_id TEXT NOT NULL DEFAULT '', "
                "quote TEXT NOT NULL DEFAULT '', "
                "locator_json TEXT NOT NULL DEFAULT '{}', "
                "published_at TEXT NOT NULL DEFAULT '', "
                "retrieved_at TEXT NOT NULL DEFAULT '', "
                "valid_from TEXT NOT NULL DEFAULT '', "
                "valid_until TEXT NOT NULL DEFAULT '', "
                "lineage_id TEXT NOT NULL DEFAULT '', "
                "grounding_status TEXT NOT NULL DEFAULT '', "
                "policy_version TEXT NOT NULL DEFAULT '', "
                "confidence REAL NOT NULL DEFAULT 0.0)"
            )
            # (symbol, theme_id) 联合索引
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_membership_symbol_theme_id "
                "ON membership_evidence(symbol, theme_id)"
            )
            # valid_until 索引，用于过期查询
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_membership_valid_until "
                "ON membership_evidence(valid_until)"
            )
            # grounding_status 索引，用于已确认成员查询
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_membership_grounding "
                "ON membership_evidence(grounding_status)"
            )

    def _row_to_evidence(self, row: sqlite3.Row) -> MembershipEvidence:
        """将数据库行转换为 MembershipEvidence 实例。"""
        data = dict(row)
        # locator_json 还原为 dict
        locator_json = data.pop("locator_json", "{}")
        try:
            locator = json.loads(locator_json)
            data["locator"] = dict(locator) if isinstance(locator, Mapping) else {}
        except (json.JSONDecodeError, TypeError):
            data["locator"] = {}
        return MembershipEvidence.from_dict(data)

    def save_evidence(self, evidence: MembershipEvidence) -> bool:
        """保存事实证据，保存前强校验 grounded support 必填字段。

        校验规则：
        - 所有证据必须有 symbol、theme_id
        - grounded + support 证据必须包含：quote、DocumentVersion
          (document_id 或 document_version_id)、locator、
          source_url 或 provider、retrieved_at
        - 空 symbol/theme_id/quote 的 grounded support 证据拒绝保存
        - counter/neutral/conflicted/unverified 证据可保存但不作为确认依据

        Args:
            evidence: 事实证据实例。

        Returns:
            True 表示保存成功；False 表示校验失败被拒绝。
        """
        # 强校验：所有证据必须有 symbol、theme_id
        if not isinstance(evidence, MembershipEvidence):
            return False
        if not evidence.symbol or not evidence.theme_id:
            return False
        # grounded + support 证据必须有完整可定位材料
        if (
            evidence.grounding_status == GROUNDING_GROUNDED
            and evidence.stance == STANCE_SUPPORT
        ):
            if not evidence.quote:
                return False
            if not evidence.document_id and not evidence.document_version_id:
                return False
            if not evidence.locator:
                return False
            if not evidence.source_url and not evidence.provider:
                return False
            if not evidence.retrieved_at:
                return False
        try:
            locator_json = json.dumps(
                evidence.locator,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return False
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO membership_evidence ("
                "evidence_id, symbol, company_id, theme_id, stance, "
                "source_type, provider, source_url, document_id, "
                "document_version_id, quote, locator_json, published_at, "
                "retrieved_at, valid_from, valid_until, lineage_id, "
                "grounding_status, policy_version, confidence"
                ") VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                ") ON CONFLICT(evidence_id) DO UPDATE SET "
                "symbol=excluded.symbol, company_id=excluded.company_id, "
                "theme_id=excluded.theme_id, stance=excluded.stance, "
                "source_type=excluded.source_type, provider=excluded.provider, "
                "source_url=excluded.source_url, document_id=excluded.document_id, "
                "document_version_id=excluded.document_version_id, "
                "quote=excluded.quote, locator_json=excluded.locator_json, "
                "published_at=excluded.published_at, "
                "retrieved_at=excluded.retrieved_at, "
                "valid_from=excluded.valid_from, valid_until=excluded.valid_until, "
                "lineage_id=excluded.lineage_id, "
                "grounding_status=excluded.grounding_status, "
                "policy_version=excluded.policy_version, "
                "confidence=excluded.confidence",
                (
                    evidence.evidence_id,
                    evidence.symbol,
                    evidence.company_id,
                    evidence.theme_id,
                    evidence.stance,
                    evidence.source_type,
                    evidence.provider,
                    evidence.source_url,
                    evidence.document_id,
                    evidence.document_version_id,
                    evidence.quote,
                    locator_json,
                    evidence.published_at,
                    evidence.retrieved_at,
                    evidence.valid_from,
                    evidence.valid_until,
                    evidence.lineage_id,
                    evidence.grounding_status,
                    evidence.policy_version,
                    evidence.confidence,
                ),
            )
        return True

    def get_evidence(self, symbol: str, theme_id: str) -> list[MembershipEvidence]:
        """查询某公司某主题的全部证据。

        Args:
            symbol: 公司代码。
            theme_id: 主题 ID。

        Returns:
            证据列表，按 retrieved_at 降序排列。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM membership_evidence "
                "WHERE symbol=? AND theme_id=? "
                "ORDER BY retrieved_at DESC",
                (symbol, theme_id),
            ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def get_confirmed_members(self, theme_id: str) -> list[str]:
        """查询某主题的所有已确认成员。

        已确认成员定义：存在 grounding_status=grounded、stance=support
        且 valid_until 未过期或为空（UNKNOWN）的证据。
        counter/neutral/conflicted/expired 证据不得确认成员。

        Args:
            theme_id: 主题 ID。

        Returns:
            已确认成员的 symbol 列表（去重）。
        """
        now = utc_now()
        with self._lock, self._connect() as conn:
            # 一次查询所有 grounded + support 证据，避免 N+1
            rows = conn.execute(
                "SELECT symbol, valid_until FROM membership_evidence "
                "WHERE theme_id=? AND grounding_status=? AND stance=?",
                (theme_id, GROUNDING_GROUNDED, STANCE_SUPPORT),
            ).fetchall()
        # 在 Python 侧过滤过期证据，避免 SQL 中 ISO 时间解析的兼容问题
        members: list[str] = []
        seen: set[str] = set()
        for row in rows:
            symbol = row["symbol"]
            if symbol in seen:
                continue
            if not _is_expired(row["valid_until"], now):
                members.append(symbol)
                seen.add(symbol)
        return members

    def is_member_confirmed(self, symbol: str, theme_id: str) -> bool:
        """判断某公司是否是某主题的已确认成员。

        需存在 grounding_status=grounded、stance=support 且 valid_until
        未过期或为空的证据。counter/neutral/conflicted 证据不确认。

        Args:
            symbol: 公司代码。
            theme_id: 主题 ID。

        Returns:
            True 表示已确认。
        """
        now = utc_now()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT valid_until FROM membership_evidence "
                "WHERE symbol=? AND theme_id=? AND grounding_status=? AND stance=?",
                (symbol, theme_id, GROUNDING_GROUNDED, STANCE_SUPPORT),
            ).fetchall()
        for row in rows:
            if not _is_expired(row["valid_until"], now):
                return True
        return False

    def expire_evidence(self, evidence_id: str, expired_at: str) -> bool:
        """手动过期证据。

        将指定证据的 valid_until 设置为 expired_at，使其不再被确认为有效。

        Args:
            evidence_id: 证据 ID。
            expired_at: 过期时间（ISO 字符串）。

        Returns:
            True 表示存在该证据且已更新；False 表示证据不存在。
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE membership_evidence SET valid_until=? "
                "WHERE evidence_id=?",
                (expired_at, evidence_id),
            )
            return cursor.rowcount > 0
