"""Event router — route events to handlers by event type, avoiding circular deps."""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Set

from .models import EventEnvelope, EnvelopeType

logger = logging.getLogger(__name__)


class EventRouter:
    """Route events to registered handlers based on event_type.

    Uses callable handlers instead of direct imports to avoid circular
    dependencies. Each route is registered via ``add_route(event_type, handler)``.

    Predefined routing conventions:
        - raw_event        -> normalizer
        - normalized_event -> small_batch_processor / evidence_center
        - extraction_done  -> evidence_center
        - evidence_created -> graph
        - graph_updated    -> validation_loop
        - candidate_created -> risk / strategy / display / VRD

    Attributes:
        routes: 已注册的路由映射
    """

    def __init__(self) -> None:
        """初始化事件路由器。"""
        self._routes: Dict[str, List[Callable[[EventEnvelope], None]]] = {}
        self._dedup_seen: Set[str] = set()
        self._dedup_enabled = True

    def add_route(
        self,
        event_type: str,
        handler: Callable[[EventEnvelope], None],
    ) -> None:
        """注册事件路由。

        同一个 event_type 可以注册多个 handler，按注册顺序依次调用。
        同一个 handler 不会被重复注册到同一个 event_type。

        Args:
            event_type: 事件类型，对应 EnvelopeType 的值
            handler: 事件处理回调函数
        """
        if event_type not in self._routes:
            self._routes[event_type] = []
        if handler not in self._routes[event_type]:
            self._routes[event_type].append(handler)
            logger.debug("Routed handler %s to event_type '%s'", handler.__name__, event_type)

    def remove_route(
        self,
        event_type: str,
        handler: Callable[[EventEnvelope], None],
    ) -> None:
        """移除事件路由。

        Args:
            event_type: 事件类型
            handler: 要移除的回调函数
        """
        handlers = self._routes.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug("Removed handler %s from event_type '%s'", handler.__name__, event_type)

    def route(self, envelope: EventEnvelope) -> None:
        """路由事件到对应的处理器。

        根据 envelope.event_type 查找已注册的 handler 并依次调用。
        单个 handler 的异常不会影响其他 handler 的执行。
        支持基于 trace_id + event_id 的去重，防止同一事件被重复路由。

        Args:
            envelope: 事件信封
        """
        # 去重检查
        if self._dedup_enabled:
            dedup_key = f"{envelope.trace_id}:{envelope.event_id}"
            if dedup_key in self._dedup_seen:
                logger.debug("Dedup: skipping already-routed event %s", envelope.event_id)
                return
            self._dedup_seen.add(dedup_key)

        event_type = envelope.event_type
        handlers = self._routes.get(event_type, [])

        if not handlers:
            logger.debug("No handlers registered for event_type '%s'", event_type)
            return

        logger.debug(
            "Routing event %s (type=%s) to %d handler(s)",
            envelope.event_id,
            event_type,
            len(handlers),
        )

        for handler in handlers:
            try:
                handler(envelope)
            except Exception:
                logger.error(
                    "Route handler %s failed for event %s (type=%s)",
                    handler.__name__,
                    envelope.event_id,
                    event_type,
                    exc_info=True,
                )

    def has_route(self, event_type: str) -> bool:
        """检查指定事件类型是否有注册的路由。

        Args:
            event_type: 事件类型

        Returns:
            是否有路由
        """
        return bool(self._routes.get(event_type))

    def handler_count(self, event_type: str) -> int:
        """获取指定事件类型的处理器数量。

        Args:
            event_type: 事件类型

        Returns:
            处理器数量
        """
        return len(self._routes.get(event_type, []))

    def registered_types(self) -> List[str]:
        """获取所有已注册路由的事件类型。

        Returns:
            事件类型列表
        """
        return list(self._routes.keys())

    def setup_default_routes(
        self,
        normalizer: Callable[[EventEnvelope], None],
        evidence_center: Callable[[EventEnvelope], None],
        small_batch_processor: Callable[[EventEnvelope], None],
        graph_handler: Callable[[EventEnvelope], None],
        validation_loop: Callable[[EventEnvelope], None],
        risk_handler: Callable[[EventEnvelope], None],
        strategy_handler: Callable[[EventEnvelope], None],
        display_handler: Callable[[EventEnvelope], None],
        vrd_handler: Callable[[EventEnvelope], None],
    ) -> None:
        """设置默认路由规则。

        按照预定义的事件流转路径注册路由：
        - raw_event -> normalizer
        - normalized_event -> small_batch_processor, evidence_center
        - extraction_done -> evidence_center
        - evidence_created -> graph_handler
        - graph_updated -> validation_loop
        - candidate_created -> risk_handler, strategy_handler, display_handler, vrd_handler

        Args:
            normalizer: 原始事件标准化处理器
            evidence_center: 证据中心处理器
            small_batch_processor: 小批量处理器
            graph_handler: 图谱处理器
            validation_loop: 验证循环处理器
            risk_handler: 风险处理器
            strategy_handler: 策略处理器
            display_handler: 展示处理器
            vrd_handler: VRD 处理器
        """
        self.add_route(EnvelopeType.RAW_EVENT, normalizer)
        self.add_route(EnvelopeType.NORMALIZED_EVENT, small_batch_processor)
        self.add_route(EnvelopeType.NORMALIZED_EVENT, evidence_center)
        self.add_route(EnvelopeType.EXTRACTION_DONE, evidence_center)
        self.add_route(EnvelopeType.EVIDENCE_CREATED, graph_handler)
        self.add_route(EnvelopeType.GRAPH_UPDATED, validation_loop)
        self.add_route(EnvelopeType.CANDIDATE_CREATED, risk_handler)
        self.add_route(EnvelopeType.CANDIDATE_CREATED, strategy_handler)
        self.add_route(EnvelopeType.CANDIDATE_CREATED, display_handler)
        self.add_route(EnvelopeType.CANDIDATE_CREATED, vrd_handler)
        logger.info("Default routes configured with %d event types", len(self._routes))

    def clear_dedup_cache(self) -> None:
        """清除去重缓存。

        在长时间运行的进程中，去重缓存可能持续增长，
        定期清理可以释放内存。
        """
        self._dedup_seen.clear()
        logger.debug("Dedup cache cleared")
