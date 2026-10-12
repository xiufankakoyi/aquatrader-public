"""Redis Stream event bus — optional Redis transport with local journal fallback."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .event_bus import EventBus
from .event_journal import LocalEventJournal
from .models import EventEnvelope

logger = logging.getLogger(__name__)


class RedisStreamBus:
    """Redis Stream based event bus with automatic fallback to LocalEventJournal.

    Attempts to connect to Redis on initialization. If Redis is unavailable,
    sets ``_available = False`` and transparently falls back to the local
    EventBus + journal. All Redis operations are non-blocking — failures are
    caught and logged, never propagating to the caller.

    Attributes:
        available: Redis 是否可用
        stream_prefix: Redis Stream 键名前缀
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream_prefix: str = "aquatrade:events",
        journal: Optional[LocalEventJournal] = None,
    ) -> None:
        """初始化 Redis Stream 事件总线。

        如果 Redis 连接失败，自动降级为本地 EventBus。

        Args:
            redis_url: Redis 连接 URL，默认从环境变量 REDIS_URL 读取
            stream_prefix: Redis Stream 键名前缀
            journal: 本地事件日志实例
        """
        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.stream_prefix = stream_prefix
        self._available = False
        self._redis_client: Any = None
        self._last_error = ""
        self._local_bus = EventBus(journal=journal)

        # 尝试连接 Redis
        try:
            import redis

            self._redis_client = redis.from_url(self._redis_url, decode_responses=True)
            self._redis_client.ping()
            self._available = True
            logger.info("RedisStreamBus connected to Redis at %s", self._redis_url)
        except ImportError:
            self._last_error = "redis_package_missing"
            logger.info("redis package not installed, falling back to local event bus")
        except Exception as e:
            self._last_error = f"redis_unavailable:{type(e).__name__}"
            logger.warning("Redis unavailable (%s), falling back to local event bus", e)

    @property
    def available(self) -> bool:
        """Redis 是否可用。"""
        return self._available

    @property
    def last_error(self) -> str:
        """最近一次 Redis transport 失败原因；空字符串表示尚未失败。"""
        return self._last_error

    def health(self) -> Dict[str, Any]:
        """返回 Redis 与本地 journal 的可观测状态。"""
        return {
            "redis_available": self._available,
            "local_fallback": True,
            "last_error": self._last_error,
            "stream_prefix": self.stream_prefix,
        }

    def _stream_key(self, topic: str) -> str:
        """根据 topic 生成 Redis Stream 键名。

        Args:
            topic: 事件主题

        Returns:
            Redis Stream 键名
        """
        return f"{self.stream_prefix}:{topic}"

    def publish(self, envelope: EventEnvelope) -> None:
        """发布事件。

        优先尝试写入 Redis Stream，同时写入本地日志。
        如果 Redis 不可用，仅使用本地 EventBus。

        Args:
            envelope: 事件信封
        """
        # 始终写入本地日志
        try:
            self._local_bus.journal.write(envelope)
        except Exception:
            logger.warning("Failed to write local journal for event %s", envelope.event_id, exc_info=True)

        # 尝试写入 Redis Stream
        if self._available:
            try:
                stream_key = self._stream_key(envelope.topic)
                data = asdict(envelope)
                # Redis Stream 要求值必须是字符串，将复杂类型序列化
                flat_data: Dict[str, str] = {}
                for key, value in data.items():
                    if isinstance(value, (dict, list)):
                        flat_data[key] = json.dumps(value, ensure_ascii=False, default=str)
                    elif value is None:
                        flat_data[key] = ""
                    else:
                        flat_data[key] = str(value)

                self._redis_client.xadd(stream_key, flat_data)
                logger.debug("Published event %s to Redis stream '%s'", envelope.event_id, stream_key)
            except Exception:
                logger.warning(
                    "Redis publish failed for event %s, event is still in local journal",
                    envelope.event_id,
                    exc_info=True,
                )
                # 标记 Redis 不可用，后续不再尝试
                self._available = False
                self._last_error = "redis_publish_failed"

        # 分发给本地订阅者
        self._dispatch_local(envelope)

    def subscribe(self, topic: str, handler: Any) -> None:
        """注册本地事件处理器。

        Args:
            topic: 事件主题
            handler: 事件处理回调函数
        """
        self._local_bus.subscribe(topic, handler)

    def unsubscribe(self, topic: str, handler: Any) -> None:
        """取消本地事件处理器订阅。

        Args:
            topic: 事件主题
            handler: 要移除的回调函数
        """
        self._local_bus.unsubscribe(topic, handler)

    def _dispatch_local(self, envelope: EventEnvelope) -> None:
        """将事件分发给本地订阅者。

        Args:
            envelope: 事件信封
        """
        topic = envelope.topic
        handlers = self._local_bus._subscribers.get(topic, [])
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

    def replay(self, topic: str, start_date: str, end_date: str) -> int:
        """从本地日志回放事件。

        Args:
            topic: 事件主题
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD

        Returns:
            回放的事件数量
        """
        return self._local_bus.replay(topic, start_date, end_date)

    def read_stream(
        self,
        topic: str,
        count: int = 100,
        block_ms: int = 0,
    ) -> List[Dict[str, Any]]:
        """从 Redis Stream 读取事件（消费者模式）。

        仅在 Redis 可用时有效。

        Args:
            topic: 事件主题
            count: 读取数量上限
            block_ms: 阻塞等待毫秒数，0 表示不阻塞

        Returns:
            事件数据列表，每项包含 stream_id 和 data
        """
        if not self._available:
            return []

        try:
            stream_key = self._stream_key(topic)
            # 使用 XREAD 从最新位置读取
            messages = self._redis_client.xread(
                {stream_key: "0-0"}, count=count, block=block_ms
            )
            results: List[Dict[str, Any]] = []
            if messages:
                for _, entries in messages:
                    for stream_id, data in entries:
                        results.append({"stream_id": stream_id, "data": data})
            return results
        except Exception:
            logger.warning("Redis XREAD failed for stream '%s'", topic, exc_info=True)
            self._available = False
            self._last_error = "redis_read_failed"
            return []

    def close(self) -> None:
        """关闭 Redis 连接。"""
        if self._redis_client is not None:
            try:
                self._redis_client.close()
            except Exception as exc:
                self._last_error = f"redis_close_failed:{type(exc).__name__}"
                logger.debug("Redis close failed", exc_info=True)
