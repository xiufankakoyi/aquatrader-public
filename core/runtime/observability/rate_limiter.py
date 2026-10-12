"""API 速率限制器。

按 P3.8 安全要求：
- 按 IP 和端点限制请求速率
- 内存滑动窗口实现，无需 Redis
- 线程安全
- 超限时返回 429 状态码提示

设计原则：
- KISS：滑动窗口计数器，避免复杂令牌桶
- 防御性：所有参数校验，避免负数或零窗口
- 不记录请求体或密钥；仅记录 IP 与端点计数
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RateLimitRule:
    """速率限制规则。

    Attributes:
        max_requests: 窗口内最大请求数。
        window_seconds: 窗口大小（秒）。
        key: 规则键名；用于端点级配置。
    """

    max_requests: int
    window_seconds: float
    key: str = "default"

    def __post_init__(self) -> None:
        """校验规则参数。"""
        if self.max_requests <= 0:
            raise ValueError(f"max_requests 必须为正数（当前 {self.max_requests}）")
        if self.window_seconds <= 0:
            raise ValueError(
                f"window_seconds 必须为正数（当前 {self.window_seconds}）"
            )


@dataclass
class _WindowCounter:
    """滑动窗口计数器。

    Attributes:
        timestamps: 请求时间戳列表（按时间升序）。
    """

    timestamps: list[float] = field(default_factory=list)

    def cleanup(self, now: float, window_seconds: float) -> None:
        """清理过期时间戳。"""
        cutoff = now - window_seconds
        self.timestamps = [ts for ts in self.timestamps if ts >= cutoff]

    def count(self) -> int:
        """返回当前窗口内请求数。"""
        return len(self.timestamps)


class RateLimiter:
    """按 IP 和端点的速率限制器。

    用法::

        limiter = RateLimiter()
        limiter.add_rule("/api/research", max_requests=60, window_seconds=60)
        allowed, retry_after = limiter.check("127.0.0.1", "/api/research")
        if not allowed:
            return jsonify({"error": "rate limited"}), 429
    """

    # 默认规则：每分钟 60 次
    DEFAULT_RULE = RateLimitRule(max_requests=60, window_seconds=60.0, key="default")

    def __init__(self) -> None:
        """初始化速率限制器。"""
        self._lock = threading.RLock()
        self._rules: dict[str, RateLimitRule] = {}
        # 计数器：(ip, endpoint_key) -> _WindowCounter
        self._counters: dict[tuple[str, str], _WindowCounter] = {}

    def add_rule(
        self,
        endpoint: str,
        *,
        max_requests: int,
        window_seconds: float,
    ) -> None:
        """为端点添加速率限制规则。

        Args:
            endpoint: 端点路径（如 ``/api/research``）。
            max_requests: 窗口内最大请求数。
            window_seconds: 窗口大小（秒）。
        """
        rule = RateLimitRule(
            max_requests=max_requests,
            window_seconds=window_seconds,
            key=endpoint,
        )
        with self._lock:
            self._rules[endpoint] = rule

    def get_rule(self, endpoint: str) -> RateLimitRule | None:
        """获取端点规则；不存在返回 None。"""
        with self._lock:
            return self._rules.get(endpoint)

    def check(
        self,
        ip: str,
        endpoint: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, float]:
        """检查请求是否允许通过。

        Args:
            ip: 客户端 IP。
            endpoint: 端点路径。
            now: 当前时间戳；``None`` 时使用 time.monotonic()。

        Returns:
            (allowed, retry_after_seconds) 元组。
            allowed 为 True 时 retry_after 为 0；
            allowed 为 False 时 retry_after 为建议的等待秒数。
        """
        current = now if now is not None else time.monotonic()
        rule = self.get_rule(endpoint) or self.DEFAULT_RULE
        key = (ip, rule.key)
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                counter = _WindowCounter()
                self._counters[key] = counter
            counter.cleanup(current, rule.window_seconds)
            if counter.count() >= rule.max_requests:
                # 计算最早请求的过期时间
                oldest = counter.timestamps[0] if counter.timestamps else current
                retry_after = max(0.1, oldest + rule.window_seconds - current)
                return (False, round(retry_after, 3))
            counter.timestamps.append(current)
            return (True, 0.0)

    def reset(self, ip: str | None = None, endpoint: str | None = None) -> None:
        """重置计数器。

        Args:
            ip: 指定 IP；``None`` 时重置所有 IP。
            endpoint: 指定端点；``None`` 时重置所有端点。
        """
        with self._lock:
            if ip is None and endpoint is None:
                self._counters.clear()
                return
            keys_to_remove = []
            for key in self._counters:
                key_ip, key_endpoint = key
                if ip is not None and key_ip != ip:
                    continue
                if endpoint is not None and key_endpoint != endpoint:
                    continue
                keys_to_remove.append(key)
            for key in keys_to_remove:
                self._counters.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        """导出当前计数器快照。"""
        with self._lock:
            counters: dict[str, Any] = {}
            for (ip, endpoint), counter in self._counters.items():
                key = f"{ip}|{endpoint}"
                counters[key] = {
                    "count": counter.count(),
                    "max_requests": (
                        self._rules.get(endpoint, self.DEFAULT_RULE).max_requests
                        if endpoint in self._rules
                        else self.DEFAULT_RULE.max_requests
                    ),
                }
            return {
                "rule_count": len(self._rules),
                "rules": {
                    endpoint: {
                        "max_requests": rule.max_requests,
                        "window_seconds": rule.window_seconds,
                    }
                    for endpoint, rule in self._rules.items()
                },
                "counters": counters,
            }


# 模块级全局单例
_global_limiter: RateLimiter | None = None
_global_lock = threading.Lock()


def get_global_limiter() -> RateLimiter:
    """获取全局速率限制器单例。"""
    global _global_limiter
    if _global_limiter is None:
        with _global_lock:
            if _global_limiter is None:
                _global_limiter = RateLimiter()
    return _global_limiter


__all__ = [
    "RateLimitRule",
    "RateLimiter",
    "get_global_limiter",
]
