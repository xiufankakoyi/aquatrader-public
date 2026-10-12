"""Event bus — in-process pub/sub with journal persistence."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Callable, Dict, List, Optional

from .event_journal import LocalEventJournal
from .models import EventEnvelope, EventTopic

logger = logging.getLogger(__name__)


class EventBus:
    """In-process event bus with journal persistence.

    Supports publish/subscribe pattern with topic-based routing.
    Every published event is automatically persisted to LocalEventJournal.
    Handler errors are caught and logged to prevent cascade failures.

    Attributes:
        journal: 本地事件日志实例
    """

    def __init__(self, journal: Optional[LocalEventJournal] = None) -> None:
        """初始化事件总线。

        Args:
            journal: 事件日志实例，默认创建 LocalEventJournal
        """
        self.journal = journal or LocalEventJournal()
        self._subscribers: Dict[str, List[Callable[[EventEnvelope], None]]] = {}

    def subscribe(self, topic: str, handler: Callable[[EventEnvelope], None]) -> None:
        """注册事件处理器。

        同一个 handler 可以订阅多个 topic，同一个 topic 可以有多个 handler。

        Args:
            topic: 事件主题，对应 EventTopic 的值
            handler: 事件处理回调函数，接收 EventEnvelope 参数
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)
            logger.debug("Subscribed handler %s to topic '%s'", handler.__name__, topic)

    def unsubscribe(self, topic: str, handler: Callable[[EventEnvelope], None]) -> None:
        """取消事件处理器订阅。

        Args:
            topic: 事件主题
            handler: 要移除的回调函数
        """
        handlers = self._subscribers.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug("Unsubscribed handler %s from topic '%s'", handler.__name__, topic)

    def publish(self, envelope: EventEnvelope) -> None:
        """发布事件到所有订阅者。

        事件会先写入日志，然后分发给所有订阅了该 topic 的 handler。
        单个 handler 的异常不会影响其他 handler 的执行。

        Args:
            envelope: 事件信封
        """
        # 1. 持久化到日志
        try:
            self.journal.write(envelope)
        except Exception:
            logger.warning("Failed to journal event %s", envelope.event_id, exc_info=True)

        # 2. 分发给订阅者
        topic = envelope.topic
        handlers = self._subscribers.get(topic, [])
        if not handlers:
            logger.debug("No subscribers for topic '%s'", topic)
            return

        for handler in handlers:
            try:
                handler(envelope)
            except Exception:
                logger.error(
                    "Handler %s failed for event %s on topic '%s'",
                    handler.__name__,
                    envelope.event_id,
                    topic,
                    exc_info=True,
                )

    def replay(
        self,
        topic: str,
        start_date: str,
        end_date: str,
    ) -> int:
        """从日志回放事件到当前所有订阅者。

        读取指定日期范围和 topic 的事件，重新发布到事件总线。

        Args:
            topic: 事件主题
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD

        Returns:
            回放的事件数量
        """
        count = 0
        for envelope in self.journal.iter_events(start_date, end_date, topic=topic):
            # 回放时不重复写日志，直接分发给订阅者
            handlers = self._subscribers.get(topic, [])
            for handler in handlers:
                try:
                    handler(envelope)
                except Exception:
                    logger.error(
                        "Handler %s failed during replay for event %s",
                        handler.__name__,
                        envelope.event_id,
                        exc_info=True,
                    )
            count += 1

        logger.info("Replayed %d events for topic '%s' from %s to %s", count, topic, start_date, end_date)
        return count

    def subscriber_count(self, topic: str) -> int:
        """获取指定 topic 的订阅者数量。

        Args:
            topic: 事件主题

        Returns:
            订阅者数量
        """
        return len(self._subscribers.get(topic, []))

    def topics(self) -> List[str]:
        """获取当前有订阅者的所有 topic。

        Returns:
            topic 列表
        """
        return list(self._subscribers.keys())
