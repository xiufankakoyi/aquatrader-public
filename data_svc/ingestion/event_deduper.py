"""事件去重。"""

import hashlib
from typing import Iterable


class EventDeduper:
    """基于事件内容哈希的去重器。"""

    @staticmethod
    def event_hash(event: dict) -> str:
        """计算事件哈希。"""
        try:
            content = str(sorted(event.items()))
        except TypeError:
            content = str(event)
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def deduplicate(self, events: Iterable[dict], existing_hashes: set | None = None) -> list[dict]:
        """
        从事件列表中过滤掉已存在哈希的事件。

        Args:
            events: 新事件列表。
            existing_hashes: 已存在的事件哈希集合。

        Returns:
            仅包含新事件的列表。
        """
        existing_hashes = existing_hashes or set()
        new_events: list[dict] = []
        for ev in events:
            h = self.event_hash(ev)
            if h not in existing_hashes:
                new_events.append(ev)
                existing_hashes.add(h)
        return new_events

    def dedupe(self, events: list[dict]) -> tuple[list[dict], dict[str, int]]:
        """
        对事件去重并返回统计信息。

        Args:
            events: 事件列表。

        Returns:
            (唯一事件列表, 统计字典)
        """
        unique = self.deduplicate(events)
        return unique, {
            "unique_count": len(unique),
            "duplicate_count": len(events) - len(unique),
            "possible_duplicate_count": 0,
        }
