"""ThemePacket SQLite 持久化存储。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import ThemePacket


class ThemeRepository:
    """ThemePacket SQLite 存储，支持版本链查询。

    使用 WAL 模式提升并发读性能，所有写操作通过 RLock 串行化以保证线程安全。
    payload_json 字段保存完整的 ThemePacket 序列化结果，便于 schema 演进。
    """

    def __init__(self, db_path: str | Path = "data/theme_control/theme.db") -> None:
        """初始化仓库并执行 schema 迁移。

        Args:
            db_path: SQLite 数据库文件路径，父目录会自动创建。
        """
        self.db_path = Path(db_path)
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
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """创建 schema（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS theme_packets ("
                "theme_run_id TEXT PRIMARY KEY, "
                "theme_id TEXT NOT NULL, "
                "theme_name TEXT NOT NULL, "
                "theme_type TEXT NOT NULL, "
                "as_of TEXT NOT NULL, "
                "detected_at TEXT NOT NULL, "
                "source_snapshot_id TEXT NOT NULL, "
                "strategy_version TEXT NOT NULL, "
                "payload_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_theme_packets_theme_id "
                "ON theme_packets(theme_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_theme_packets_as_of "
                "ON theme_packets(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_theme_packets_type "
                "ON theme_packets(theme_type)"
            )

    def save_packet(self, packet: ThemePacket) -> None:
        """保存 ThemePacket（按 theme_run_id 主键 upsert）。

        Args:
            packet: 待保存的主题包。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO theme_packets "
                "(theme_run_id, theme_id, theme_name, theme_type, as_of, detected_at, "
                "source_snapshot_id, strategy_version, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    packet.theme_run_id,
                    packet.theme_id,
                    packet.theme_name,
                    packet.theme_type,
                    packet.as_of,
                    packet.detected_at,
                    packet.source_snapshot_id,
                    packet.strategy_version,
                    json.dumps(packet.to_dict(), ensure_ascii=False),
                ),
            )

    def get_packet(self, theme_run_id: str) -> ThemePacket | None:
        """按 theme_run_id 查询。

        Args:
            theme_run_id: 主题运行 ID。

        Returns:
            ThemePacket 实例，未找到返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM theme_packets WHERE theme_run_id=?",
                (theme_run_id,),
            ).fetchone()
        return ThemePacket.from_dict(json.loads(row[0])) if row else None

    def list_versions(self, theme_id: str) -> list[ThemePacket]:
        """按 theme_id 查版本链（按 as_of 降序）。

        Args:
            theme_id: 主题 ID。

        Returns:
            该主题所有历史版本的列表，最新在前。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM theme_packets "
                "WHERE theme_id=? ORDER BY as_of DESC",
                (theme_id,),
            ).fetchall()
        return [ThemePacket.from_dict(json.loads(row[0])) for row in rows]

    def list_recent_packets(self, limit: int = 100, theme_type: str = "") -> list[ThemePacket]:
        """查询最近的 ThemePacket。

        Args:
            limit: 返回条数上限（强制不超过 1000）。
            theme_type: 可选，按主题类型过滤。

        Returns:
            最近的 ThemePacket 列表，按 as_of 降序。
        """
        query = "SELECT payload_json FROM theme_packets"
        params: tuple = ()
        if theme_type:
            query += " WHERE theme_type=?"
            params = (theme_type,)
        query += " ORDER BY as_of DESC LIMIT ?"
        params = (*params, min(limit, 1000))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ThemePacket.from_dict(json.loads(row[0])) for row in rows]
