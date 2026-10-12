"""Thread-safe throttling primitives for outbound search requests."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass
class SearchThrottleStats:
    """Runtime counters exposed for diagnostics and smoke tests."""

    started: int = 0
    completed: int = 0
    failed: int = 0
    active: int = 0
    peak_active: int = 0
    wait_seconds: float = 0.0


class SearchThrottlePool:
    """Bound concurrency and request start rate for one upstream search source."""

    def __init__(
        self,
        max_concurrency: int = 4,
        min_interval: float = 0.0,
        failure_cooldown: float = 2.0,
        burst_capacity: int = 1,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_interval = max(0.0, float(min_interval))
        self.failure_cooldown = max(0.0, float(failure_cooldown))
        self.burst_capacity = max(1, int(burst_capacity))
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._schedule_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._tokens = float(self.burst_capacity)
        self._last_refill = time.monotonic()
        self._stats = SearchThrottleStats()

    def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run one callable inside the concurrency and start-rate limits."""
        with self._semaphore:
            waited = self._wait_for_start_slot()
            with self._stats_lock:
                self._stats.started += 1
                self._stats.active += 1
                self._stats.peak_active = max(
                    self._stats.peak_active,
                    self._stats.active,
                )
                self._stats.wait_seconds += waited
            try:
                result = func(*args, **kwargs)
            except Exception:
                with self._stats_lock:
                    self._stats.failed += 1
                if self.failure_cooldown:
                    self.penalize(self.failure_cooldown)
                raise
            else:
                with self._stats_lock:
                    self._stats.completed += 1
                return result
            finally:
                with self._stats_lock:
                    self._stats.active -= 1

    def penalize(self, seconds: float) -> None:
        """Prevent new requests from starting during an upstream cooldown."""
        seconds = max(0.0, float(seconds))
        if not seconds:
            return
        with self._schedule_lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + seconds,
            )
            self._tokens = 0.0

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable copy of current counters and configuration."""
        with self._stats_lock:
            payload = asdict(self._stats)
        payload.update(
            {
                "max_concurrency": self.max_concurrency,
                "min_interval": self.min_interval,
                "burst_capacity": self.burst_capacity,
            }
        )
        return payload

    def _wait_for_start_slot(self) -> float:
        started_wait = time.monotonic()
        with self._schedule_lock:
            while True:
                now = time.monotonic()
                if now < self._cooldown_until:
                    time.sleep(self._cooldown_until - now)
                    continue
                if self.min_interval <= 0:
                    break

                elapsed = max(0.0, now - self._last_refill)
                self._tokens = min(
                    float(self.burst_capacity),
                    self._tokens + elapsed / self.min_interval,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    break
                time.sleep((1.0 - self._tokens) * self.min_interval)
        return max(0.0, time.monotonic() - started_wait)
