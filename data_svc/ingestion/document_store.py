"""本地 SQLite 事件文档存储。"""

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional

import pandas as pd


class DocumentStore:
    """基于 SQLite 的简单事件存储，用于投研管线写入和查询事件。"""

    def __init__(self, db_path: str = "data/research_events.db"):
        """
        初始化文档存储。

        Args:
            db_path: SQLite 数据库路径。
        """
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接。"""
        return sqlite3.connect(self.db_path)

    @contextmanager
    def _cursor(self):
        """提供自动提交的游标上下文。"""
        conn = self._connect()
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()

    def _init_table(self) -> None:
        """初始化事件表。"""
        with self._cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    payload TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    hash_id TEXT UNIQUE
                )
                """
            )

    def insert_event(self, event_type: str, payload: dict, hash_id: str) -> bool:
        """
        插入一条事件，重复 hash_id 时返回 False。

        Args:
            event_type: 事件类型。
            payload: 事件载荷。
            hash_id: 事件唯一哈希。

        Returns:
            是否插入成功。
        """
        try:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO events (event_type, payload, hash_id) VALUES (?, ?, ?)",
                    (event_type, json.dumps(payload, ensure_ascii=False, default=str), hash_id),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def query_events(
        self, event_type: Optional[str] = None, limit: int = 100
    ) -> pd.DataFrame:
        """
        查询事件。

        Args:
            event_type: 事件类型过滤，None 表示全部。
            limit: 最大返回条数。

        Returns:
            事件数据框。
        """
        conn = self._connect()
        try:
            if event_type:
                df = pd.read_sql_query(
                    """
                    SELECT * FROM events
                    WHERE event_type = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    conn,
                    params=(event_type, limit),
                )
            else:
                df = pd.read_sql_query(
                    """
                    SELECT * FROM events
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    conn,
                    params=(limit,),
                )
            return df
        finally:
            conn.close()

    def save_document_from_raw_document(self, doc: Any) -> bool:
        """
        保存原始文档对象到事件存储。

        Args:
            doc: 原始文档对象或字典。

        Returns:
            是否保存成功。
        """
        try:
            if hasattr(doc, "to_dict"):
                payload = doc.to_dict()
            elif hasattr(doc, "__dict__"):
                payload = doc.__dict__
            else:
                payload = dict(doc) if isinstance(doc, dict) else {"content": str(doc)}

            doc_id = str(payload.get("id", payload.get("doc_id", hash(str(payload)))))
            return self.insert_event("raw_document", payload, doc_id)
        except Exception:
            return False
