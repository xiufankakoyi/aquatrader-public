"""Raw evidence store.

Stores and retrieves raw evidence records before grading and binding.
Uses JSON file persistence with auto-save on write and auto-load on init.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.research.perilla.evidence.evidence_schema import RawEvidenceRecord

logger = logging.getLogger(__name__)

_DEFAULT_STORAGE_DIR = Path("data/research_runs/perilla_raw_evidence")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class RawEvidenceStore:
    """Persistent store for raw evidence records using JSONL files."""

    def __init__(
        self,
        storage_path: str | Path | None = None,
        auto_persist: bool = True,
    ) -> None:
        """Initialize the evidence store.

        Args:
            storage_path: 存储目录路径，None 使用默认路径。
            auto_persist: save 时是否自动持久化到磁盘。
        """
        self._storage_dir = Path(storage_path) if storage_path else _DEFAULT_STORAGE_DIR
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: dict[str, RawEvidenceRecord] = {}
        self._auto_persist = auto_persist
        self._persist_file = self._storage_dir / "raw_evidence.jsonl"
        try:
            self._load_from_disk()
        except Exception as exc:
            logger.debug("RawEvidenceStore 加载已有记录失败: %s", exc)

    def save(self, record: RawEvidenceRecord) -> None:
        """Save a raw evidence record.

        Args:
            record: Record to store.
        """
        if not isinstance(record, RawEvidenceRecord) or not _text(record.evidence_id):
            return
        with self._lock:
            self._records[_text(record.evidence_id)] = record
        if self._auto_persist:
            self._persist_to_disk()

    def save_many(self, records: list[RawEvidenceRecord]) -> None:
        """批量保存多条记录，减少 IO 次数。

        Args:
            records: 记录列表。
        """
        records = records if isinstance(records, (list, tuple)) else []
        with self._lock:
            for record in records:
                if isinstance(record, RawEvidenceRecord) and _text(record.evidence_id):
                    self._records[_text(record.evidence_id)] = record
        if self._auto_persist:
            self._persist_to_disk()

    def get(self, evidence_id: str) -> RawEvidenceRecord | None:
        """Retrieve a raw evidence record by ID.

        Args:
            evidence_id: Evidence record identifier.

        Returns:
            Stored record or None.
        """
        with self._lock:
            return self._records.get(_text(evidence_id))

    def get_all(self) -> list[RawEvidenceRecord]:
        """返回所有记录。"""
        with self._lock:
            return list(self._records.values())

    def count(self) -> int:
        """返回记录总数。"""
        with self._lock:
            return len(self._records)

    def delete(self, evidence_id: str) -> bool:
        """删除一条记录。

        Args:
            evidence_id: 证据 ID。

        Returns:
            是否删除成功。
        """
        evidence_id = _text(evidence_id)
        with self._lock:
            if evidence_id not in self._records:
                return False
            del self._records[evidence_id]
            if self._auto_persist:
                self._persist_to_disk()
            return True

    def query_by_source_type(self, source_type: str) -> list[RawEvidenceRecord]:
        """按来源类型查询记录。

        Args:
            source_type: 来源类型字符串。

        Returns:
            匹配的记录列表。
        """
        source_type = _text(source_type)
        with self._lock:
            return [r for r in self._records.values() if _text(r.source_type) == source_type]

    def _persist_to_disk(self) -> None:
        """将所有记录写入 JSONL 文件。"""
        tmp_path: Path | None = None
        try:
            fd, raw_tmp_path = tempfile.mkstemp(
                prefix=f".{self._persist_file.name}.",
                dir=self._storage_dir,
            )
            tmp_path = Path(raw_tmp_path)
            with self._lock:
                records = list(self._records.values())
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(asdict(record), ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._persist_file)
        except Exception as exc:
            logger.warning("RawEvidenceStore 持久化失败: %s", exc)

            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _load_from_disk(self) -> None:
        """从 JSONL 文件加载记录。"""
        if not self._persist_file.exists():
            return
        loaded = 0
        with self._persist_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                    if not isinstance(data, Mapping):
                        continue
                    record = RawEvidenceRecord(
                        evidence_id=_text(data.get("evidence_id")),
                        source_document_id=_text(data.get("source_document_id")),
                        source_type=_text(data.get("source_type")),
                        evidence_text=_text(data.get("evidence_text")),
                        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), Mapping) else {},
                    )
                    if record.evidence_id:
                        self._records[record.evidence_id] = record
                        loaded += 1
                except Exception:
                    continue
        if loaded:
            logger.debug("RawEvidenceStore 从磁盘加载 %d 条记录", loaded)
