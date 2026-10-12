"""研究操作系统指标收集器。

按 P3.8 生产要求，覆盖以下指标：
- 数据源可用率、陈旧度、覆盖率、限流状态
- 队列深度、预算消耗、缓存命中率、重试次数、死信数
- 模型版本和降级状态

设计原则：
- 线程安全（使用 threading.Lock）
- 内存指标，不依赖外部存储；可周期性导出快照
- 不记录密钥；指标值仅数值或字符串标签
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProviderMetric:
    """单个数据源（provider）的指标快照。

    Attributes:
        name: provider 名称。
        available: 是否可用。
        availability_rate: 可用率（0.0-1.0）。
        staleness_seconds: 数据陈旧度（秒）。
        coverage_ratio: 覆盖率（0.0-1.0）。
        rate_limited: 是否被限流。
        last_success_at: 最后一次成功时间。
        last_error: 最后一次失败原因。
        request_count: 总请求数。
        failure_count: 失败请求数。
    """

    name: str
    available: bool = True
    availability_rate: float = 1.0
    staleness_seconds: float = 0.0
    coverage_ratio: float = 1.0
    rate_limited: bool = False
    last_success_at: str = ""
    last_error: str = ""
    request_count: int = 0
    failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "name": self.name,
            "available": self.available,
            "availability_rate": round(self.availability_rate, 4),
            "staleness_seconds": round(self.staleness_seconds, 3),
            "coverage_ratio": round(self.coverage_ratio, 4),
            "rate_limited": self.rate_limited,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "request_count": self.request_count,
            "failure_count": self.failure_count,
        }


@dataclass
class QueueMetric:
    """队列与任务指标快照。

    Attributes:
        queue_depth: 当前队列深度。
        in_flight: 执行中的任务数。
        budget_remaining: 剩余预算（请求数）。
        budget_total: 总预算（请求数）。
        cache_hits: 缓存命中次数。
        cache_misses: 缓存未命中次数。
        retry_count: 重试总次数。
        dead_letter_count: 死信数。
        succeeded_count: 成功任务数。
        failed_count: 失败任务数。
    """

    queue_depth: int = 0
    in_flight: int = 0
    budget_remaining: float = 0.0
    budget_total: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    retry_count: int = 0
    dead_letter_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率（0.0-1.0）。"""
        total = self.cache_hits + self.cache_misses
        if total <= 0:
            return 0.0
        return self.cache_hits / total

    @property
    def budget_consumed_ratio(self) -> float:
        """预算消耗比例（0.0-1.0）。"""
        if self.budget_total <= 0:
            return 0.0
        consumed = self.budget_total - self.budget_remaining
        return max(0.0, min(1.0, consumed / self.budget_total))

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "queue_depth": self.queue_depth,
            "in_flight": self.in_flight,
            "budget_remaining": round(self.budget_remaining, 3),
            "budget_total": round(self.budget_total, 3),
            "budget_consumed_ratio": round(self.budget_consumed_ratio, 4),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "retry_count": self.retry_count,
            "dead_letter_count": self.dead_letter_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
        }


@dataclass
class ModelMetric:
    """模型版本与降级状态指标。

    Attributes:
        model_id: 模型 ID。
        version: 当前版本。
        status: 模型状态（active/suspended/degraded 等）。
        degraded: 是否处于降级状态。
        rollback_version: 回滚版本。
        last_activated_at: 最后激活时间。
    """

    model_id: str
    version: str = ""
    status: str = "unknown"
    degraded: bool = False
    rollback_version: str = ""
    last_activated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "model_id": self.model_id,
            "version": self.version,
            "status": self.status,
            "degraded": self.degraded,
            "rollback_version": self.rollback_version,
            "last_activated_at": self.last_activated_at,
        }


class MetricsCollector:
    """研究操作系统指标收集器。

    线程安全；提供 provider/queue/model 三类指标的记录与快照。
    所有指标仅保存在内存中，可周期性导出。
    """

    def __init__(self) -> None:
        """初始化指标收集器。"""
        self._lock = threading.RLock()
        self._providers: dict[str, ProviderMetric] = {}
        self._queue = QueueMetric()
        self._models: dict[str, ModelMetric] = {}
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------
    # provider 指标
    # ------------------------------------------------------------------
    def register_provider(self, name: str) -> ProviderMetric:
        """注册数据源；已存在时返回现有记录。

        Args:
            name: provider 名称。

        Returns:
            ProviderMetric 实例。
        """
        with self._lock:
            if name not in self._providers:
                self._providers[name] = ProviderMetric(name=name)
            return self._providers[name]

    def record_provider_request(
        self,
        name: str,
        *,
        success: bool,
        error: str = "",
        staleness_seconds: float = 0.0,
        coverage_ratio: float | None = None,
        rate_limited: bool = False,
    ) -> None:
        """记录一次 provider 请求结果。

        Args:
            name: provider 名称。
            success: 是否成功。
            error: 失败原因；成功时为空。
            staleness_seconds: 数据陈旧度（秒）。
            coverage_ratio: 覆盖率（0.0-1.0）。
            rate_limited: 是否被限流。
        """
        with self._lock:
            metric = self.register_provider(name)
            metric.request_count += 1
            if not success:
                metric.failure_count += 1
                metric.last_error = error
            else:
                metric.last_success_at = _utc_now_iso()
                metric.last_error = ""
            metric.available = success and not rate_limited
            metric.rate_limited = rate_limited
            metric.staleness_seconds = max(metric.staleness_seconds, staleness_seconds)
            if coverage_ratio is not None:
                metric.coverage_ratio = coverage_ratio
            # 可用率 = 成功请求数 / 总请求数
            if metric.request_count > 0:
                metric.availability_rate = (
                    (metric.request_count - metric.failure_count)
                    / metric.request_count
                )

    def set_provider_available(self, name: str, available: bool) -> None:
        """直接设置 provider 可用性（用于健康探测）。"""
        with self._lock:
            metric = self.register_provider(name)
            metric.available = available
            if available:
                metric.last_success_at = _utc_now_iso()

    def get_provider(self, name: str) -> ProviderMetric | None:
        """获取 provider 指标；不存在返回 None。"""
        with self._lock:
            metric = self._providers.get(name)
            return ProviderMetric(**metric.__dict__) if metric else None

    # ------------------------------------------------------------------
    # 队列指标
    # ------------------------------------------------------------------
    def set_queue_depth(self, depth: int, in_flight: int = 0) -> None:
        """设置队列深度与执行中任务数。"""
        with self._lock:
            self._queue.queue_depth = max(0, int(depth))
            self._queue.in_flight = max(0, int(in_flight))

    def set_budget(self, remaining: float, total: float) -> None:
        """设置预算剩余与总量。"""
        with self._lock:
            self._queue.budget_remaining = max(0.0, float(remaining))
            self._queue.budget_total = max(0.0, float(total))

    def record_cache_hit(self) -> None:
        """记录一次缓存命中。"""
        with self._lock:
            self._queue.cache_hits += 1

    def record_cache_miss(self) -> None:
        """记录一次缓存未命中。"""
        with self._lock:
            self._queue.cache_misses += 1

    def record_retry(self) -> None:
        """记录一次重试。"""
        with self._lock:
            self._queue.retry_count += 1

    def record_dead_letter(self) -> None:
        """记录一个死信。"""
        with self._lock:
            self._queue.dead_letter_count += 1

    def record_task_outcome(self, *, succeeded: bool) -> None:
        """记录任务最终结果。"""
        with self._lock:
            if succeeded:
                self._queue.succeeded_count += 1
            else:
                self._queue.failed_count += 1

    # ------------------------------------------------------------------
    # 模型指标
    # ------------------------------------------------------------------
    def register_model(
        self,
        model_id: str,
        version: str,
        status: str = "active",
        rollback_version: str = "",
    ) -> ModelMetric:
        """注册或更新模型版本指标。"""
        with self._lock:
            if model_id not in self._models:
                self._models[model_id] = ModelMetric(
                    model_id=model_id,
                    version=version,
                    status=status,
                    rollback_version=rollback_version,
                )
            else:
                metric = self._models[model_id]
                metric.version = version
                metric.status = status
                metric.rollback_version = rollback_version
            if status == "active":
                self._models[model_id].last_activated_at = _utc_now_iso()
            return self._models[model_id]

    def set_model_degraded(self, model_id: str, degraded: bool) -> None:
        """设置模型降级状态。"""
        with self._lock:
            if model_id in self._models:
                self._models[model_id].degraded = degraded

    def get_model(self, model_id: str) -> ModelMetric | None:
        """获取模型指标；不存在返回 None。"""
        with self._lock:
            metric = self._models.get(model_id)
            return ModelMetric(**metric.__dict__) if metric else None

    # ------------------------------------------------------------------
    # 快照导出
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """导出当前所有指标的快照。"""
        with self._lock:
            return {
                "generated_at": _utc_now_iso(),
                "uptime_seconds": round(time.monotonic() - self._started_at, 3),
                "providers": {
                    name: metric.to_dict()
                    for name, metric in self._providers.items()
                },
                "queue": self._queue.to_dict(),
                "models": {
                    model_id: metric.to_dict()
                    for model_id, metric in self._models.items()
                },
                "summary": {
                    "provider_count": len(self._providers),
                    "model_count": len(self._models),
                    "overall_available": all(
                        m.available for m in self._providers.values()
                    ) if self._providers else True,
                    "any_degraded_model": any(
                        m.degraded for m in self._models.values()
                    ),
                },
            }

    def reset(self) -> None:
        """清空所有指标；主要用于测试。"""
        with self._lock:
            self._providers.clear()
            self._queue = QueueMetric()
            self._models.clear()
            self._started_at = time.monotonic()


# 模块级单例；供全局共享
_global_collector: MetricsCollector | None = None
_global_lock = threading.Lock()


def get_global_collector() -> MetricsCollector:
    """获取全局指标收集器单例。"""
    global _global_collector
    if _global_collector is None:
        with _global_lock:
            if _global_collector is None:
                _global_collector = MetricsCollector()
    return _global_collector


__all__ = [
    "MetricsCollector",
    "ModelMetric",
    "ProviderMetric",
    "QueueMetric",
    "get_global_collector",
]
