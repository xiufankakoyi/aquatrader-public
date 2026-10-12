"""通知数据模型

定义 NotificationPayload 数据类，用于表示通知消息的完整信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# 合法的事件类型
VALID_EVENT_TYPES: set[str] = {
    "task_started",
    "task_succeeded",
    "task_failed",
    "daily_brief_ready",
    "new_high_priority_candidate",
    "evidence_upgraded",
    "watchlist_changed",
    "system_error",
}

# 合法的优先级
VALID_PRIORITIES: set[str] = {"low", "medium", "high"}

# summary 最大长度
SUMMARY_MAX_LENGTH: int = 200


@dataclass
class NotificationPayload:
    """通知消息数据模型

    Attributes:
        event_type: 事件类型，必须在 VALID_EVENT_TYPES 中
        title: 通知标题
        summary: 通知摘要，不超过 SUMMARY_MAX_LENGTH 字符
        priority: 优先级，"low" | "medium" | "high"
        links: 关联链接列表，每项包含 text 和 url
        data: 附加数据字典
        timestamp: ISO 格式时间戳，自动设置为当前 UTC 时间
    """

    event_type: str
    title: str
    summary: str
    priority: str
    links: list[dict[str, str]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        """校验字段合法性。"""
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"非法 event_type: '{self.event_type}'，"
                f"合法值: {sorted(VALID_EVENT_TYPES)}"
            )
        if self.priority not in VALID_PRIORITIES:
            raise ValueError(
                f"非法 priority: '{self.priority}'，"
                f"合法值: {sorted(VALID_PRIORITIES)}"
            )
        if len(self.summary) > SUMMARY_MAX_LENGTH:
            self.summary = self.summary[:SUMMARY_MAX_LENGTH - 3] + "..."

    def to_dict(self) -> dict[str, Any]:
        """将 NotificationPayload 转换为可序列化字典。

        Returns:
            包含所有字段的字典
        """
        return {
            "event_type": self.event_type,
            "title": self.title,
            "summary": self.summary,
            "priority": self.priority,
            "links": self.links,
            "data": self.data,
            "timestamp": self.timestamp,
        }
