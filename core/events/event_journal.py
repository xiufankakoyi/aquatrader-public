"""Local event journal — append-only JSONL storage for event envelopes."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

from .models import EventEnvelope

logger = logging.getLogger(__name__)


class LocalEventJournal:
    """Append-only JSONL event journal.

    Events are written to ``data/events/journal/YYYY-MM-DD/events.jsonl``.
    Each line is a self-contained JSON object representing one EventEnvelope.

    Attributes:
        base_dir: 日志根目录，默认为 data/events/journal
    """

    def __init__(self, base_dir: Optional[str] = None) -> None:
        """初始化事件日志。

        Args:
            base_dir: 日志根目录，默认为项目根目录下 data/events/journal
        """
        if base_dir is None:
            # 相对于项目根目录
            project_root = Path(__file__).resolve().parents[2]
            base_dir = str(project_root / "data" / "events" / "journal")
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _journal_path(self, date: str) -> Path:
        """获取指定日期的日志文件路径。

        Args:
            date: 日期字符串 YYYY-MM-DD

        Returns:
            日志文件路径
        """
        day_dir = self.base_dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir / "events.jsonl"

    def write(self, envelope: EventEnvelope) -> None:
        """将事件信封追加写入日志。

        写入失败时仅记录警告，不抛出异常，避免影响主流程。

        Args:
            envelope: 事件信封
        """
        try:
            # 优先使用 payload 中的 trade_date，否则使用 created_at 的日期部分
            date_str = ""
            payload = envelope.payload or {}
            if isinstance(payload, dict):
                date_str = payload.get("trade_date", "") or payload.get("data_date", "")
            if not date_str and envelope.created_at:
                date_str = envelope.created_at[:10]
            if not date_str:
                date_str = datetime.now().strftime("%Y-%m-%d")

            journal_path = self._journal_path(date_str)
            line = json.dumps(asdict(envelope), ensure_ascii=False, default=str)
            with open(journal_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.warning("Failed to write event journal for %s", envelope.event_id, exc_info=True)

    def read_events(
        self,
        date: str,
        topic: Optional[str] = None,
    ) -> List[EventEnvelope]:
        """读取指定日期的事件。

        Args:
            date: 日期字符串 YYYY-MM-DD
            topic: 可选，按 topic 过滤

        Returns:
            事件信封列表
        """
        journal_path = self._journal_path(date)
        if not journal_path.exists():
            return []

        results: List[EventEnvelope] = []
        try:
            with open(journal_path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        envelope = EventEnvelope(**data)
                        if topic is None or envelope.topic == topic:
                            results.append(envelope)
                    except (json.JSONDecodeError, TypeError):
                        logger.debug(
                            "Skipping malformed line %d in %s", line_no, journal_path
                        )
        except Exception:
            logger.warning("Failed to read event journal for %s", date, exc_info=True)

        return results

    def iter_events(
        self,
        start_date: str,
        end_date: str,
        topic: Optional[str] = None,
    ) -> Iterator[EventEnvelope]:
        """按日期范围迭代事件。

        Args:
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            topic: 可选，按 topic 过滤

        Yields:
            事件信封
        """
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            logger.error("Invalid date format, expected YYYY-MM-DD")
            return

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            for envelope in self.read_events(date_str, topic=topic):
                yield envelope
            current = datetime(
                current.year, current.month, current.day
            )
            # 前进一天
            from datetime import timedelta
            current = current + timedelta(days=1)

    def list_dates(self) -> List[str]:
        """列出日志中所有有数据的日期。

        Returns:
            日期字符串列表，按升序排列
        """
        if not self.base_dir.exists():
            return []

        dates: List[str] = []
        for entry in sorted(self.base_dir.iterdir()):
            if entry.is_dir() and len(entry.name) == 10 and "-" in entry.name:
                journal_file = entry / "events.jsonl"
                if journal_file.exists():
                    dates.append(entry.name)
        return dates

    def read_events_by_trace_id(self, trace_id: str) -> List[EventEnvelope]:
        """Read events matching a specific trace_id across all dates.

        Args:
            trace_id: The trace_id to search for.

        Returns:
            List of matching EventEnvelope instances.
        """
        results = []
        for date_str in self.list_dates():
            for event in self.read_events(date_str):
                if event.trace_id == trace_id:
                    results.append(event)
        return results

    def read_events_by_provider(self, provider: str) -> List[EventEnvelope]:
        """Read events from a specific provider across all dates.

        Args:
            provider: The provider name to filter by.

        Returns:
            List of matching EventEnvelope instances.
        """
        results = []
        for date_str in self.list_dates():
            for event in self.read_events(date_str):
                payload = event.payload or {}
                if payload.get("provider") == provider or event.source == provider:
                    results.append(event)
        return results

    def read_events_by_symbol(self, symbol: str) -> List[EventEnvelope]:
        """Read events for a specific stock symbol across all dates.

        Args:
            symbol: The stock code to filter by.

        Returns:
            List of matching EventEnvelope instances.
        """
        results = []
        for date_str in self.list_dates():
            for event in self.read_events(date_str):
                payload = event.payload or {}
                if payload.get("symbol") == symbol:
                    results.append(event)
        return results

    def read_events_by_topic(self, topic: str, start_date: str = None, end_date: str = None) -> List[EventEnvelope]:
        """Read events for a specific topic, optionally within a date range.

        Args:
            topic: The topic to filter by.
            start_date: Optional start date (YYYY-MM-DD).
            end_date: Optional end date (YYYY-MM-DD).

        Returns:
            List of matching EventEnvelope instances.
        """
        results = []
        dates = self.list_dates()
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]
        for date_str in dates:
            for event in self.read_events(date_str, topic=topic):
                results.append(event)
        return results
