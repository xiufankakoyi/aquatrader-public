"""Session-aware monitor scheduling with bounded backoff.

Schedules monitoring cadence based on the current MarketSession, with
bounded exponential backoff on consecutive failures. State is persisted to
a small JSON file so that a restarted scheduler can resume the same-day
cadence without duplicating runs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.runtime.market_session import MarketSession, MarketSessionStateMachine, MARKET_TZ

logger = logging.getLogger(__name__)

# 默认的时段 -> 监控间隔（秒）映射。
DEFAULT_SESSION_INTERVALS: dict[MarketSession, int] = {
    MarketSession.MORNING: 300,       # 5 分钟
    MarketSession.AFTERNOON: 300,     # 5 分钟
    MarketSession.CALL_AUCTION: 120,  # 2 分钟，集合竞价期间更频繁
    MarketSession.LUNCH_BREAK: 1800,  # 30 分钟
    MarketSession.PRE_MARKET: 600,    # 10 分钟
    MarketSession.POST_MARKET: 600,   # 10 分钟
    MarketSession.CLOSED: 3600,        # 1 小时
}

DEFAULT_MAX_BACKOFF_SECONDS: int = 1800
DEFAULT_BASE_BACKOFF_SECONDS: int = 60


@dataclass
class MonitorSchedule:
    """Mapping from MarketSession to monitoring interval (seconds)."""

    session_intervals: dict[MarketSession, int] = field(
        default_factory=lambda: dict(DEFAULT_SESSION_INTERVALS)
    )

    def interval_for(self, session: MarketSession) -> int:
        """Return the configured interval for the given session.

        Falls back to DEFAULT_SESSION_INTERVALS when the session is missing
        from the override map, and ultimately to 300 seconds if the default
        map is also missing the entry.
        """
        if session in self.session_intervals:
            return int(self.session_intervals[session])
        return int(DEFAULT_SESSION_INTERVALS.get(session, 300))


class MonitorScheduler:
    """Decide when the intraday monitor should run next.

    Args:
        state_machine: ``MarketSessionStateMachine`` used to classify the
            current wall-clock time. ``None`` 时创建默认实例（按周一到周五
            判断交易日）。
        schedule: ``MonitorSchedule`` 自定义时段间隔。``None`` 时使用默认值。
        state_path: 持久化状态文件路径。``None`` 时不持久化，仅内存运行。
        max_backoff_seconds: 失败退避的最大上限。
        base_backoff_seconds: 失败退避的初始基数，按 2^failure_count 增长。
    """

    def __init__(
        self,
        state_machine: MarketSessionStateMachine | None = None,
        schedule: MonitorSchedule | None = None,
        state_path: str | Path | None = None,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
    ) -> None:
        self.state_machine = state_machine or MarketSessionStateMachine()
        self.schedule = schedule or MonitorSchedule()
        self.state_path = Path(state_path) if state_path else None
        self.max_backoff_seconds = int(max_backoff_seconds)
        self.base_backoff_seconds = int(base_backoff_seconds)
        # 运行期状态
        self._failure_count: int = 0
        self._last_run: datetime | None = None
        self._last_trade_date: str = ""
        # 启动时尝试从持久化恢复
        self._restore_state()

    def next_interval(self, now: datetime) -> int:
        """Return the seconds to wait before the next monitoring run.

        If the scheduler is currently in a backoff state due to consecutive
        failures, the interval is the bounded exponential backoff value, but
        never smaller than the current session cadence (failures should never
        make us run *more* frequently than the normal session interval).
        Otherwise it is the cadence configured for the current session.
        """
        session = self.state_machine.current_session(now)
        session_interval = self.schedule.interval_for(session)
        if self._failure_count > 0:
            backoff = self._compute_backoff(self._failure_count)
            return max(backoff, session_interval)
        return session_interval

    def should_run(self, now: datetime, last_run: datetime | None = None) -> bool:
        """Decide whether the monitor should run now.

        Args:
            now: 当前时间。
            last_run: 上一次执行时间。``None`` 时使用内部记录的 last_run
                （来自 ``record_success`` / ``record_failure`` / 持久化）。
        """
        reference_run = last_run if last_run is not None else self._last_run
        # 跨日则强制重置：新交易日必须执行一次
        market_now = self._to_market_datetime(now)
        today_text = market_now.date().isoformat()
        if self._last_trade_date and self._last_trade_date != today_text:
            return True
        if reference_run is None:
            return True
        elapsed = (market_now - self._to_market_datetime(reference_run)).total_seconds()
        if elapsed < 0:
            # 时钟回拨或测试边界：立即执行
            return True
        return elapsed >= self.next_interval(now)

    def record_success(self, now: datetime | None = None) -> None:
        """Record a successful run, clearing any backoff state."""
        self._failure_count = 0
        self._last_run = now or datetime.now(MARKET_TZ)
        self._last_trade_date = self._to_market_datetime(self._last_run).date().isoformat()
        self._persist_state()

    def record_failure(self, now: datetime | None = None) -> None:
        """Record a failed run, increasing the bounded backoff."""
        self._failure_count += 1
        self._last_run = now or datetime.now(MARKET_TZ)
        self._last_trade_date = self._to_market_datetime(self._last_run).date().isoformat()
        self._persist_state()

    def reset_backoff(self) -> None:
        """Explicitly reset the failure counter without recording a run."""
        self._failure_count = 0
        self._persist_state()

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def last_run(self) -> datetime | None:
        return self._last_run

    def _compute_backoff(self, failure_count: int) -> int:
        """Bounded exponential backoff: base * 2^(n-1), capped at max."""
        if failure_count <= 0:
            return 0
        raw = self.base_backoff_seconds * (2 ** (failure_count - 1))
        return min(int(raw), self.max_backoff_seconds)

    def _persist_state(self) -> None:
        """Persist last_run / failure_count for restart recovery."""
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_run": self._last_run.isoformat(timespec="seconds")
                    if self._last_run else "",
                "failure_count": self._failure_count,
                "last_trade_date": self._last_trade_date,
                "schema_version": "monitor_scheduler_v1",
            }
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("monitor scheduler state persist failed: %s", exc)

    def _restore_state(self) -> None:
        """Restore last_run / failure_count from the persistence file."""
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("monitor scheduler state restore failed: %s", exc)
            return
        last_run_text = str(payload.get("last_run") or "")
        if last_run_text:
            try:
                self._last_run = datetime.fromisoformat(last_run_text)
            except ValueError:
                self._last_run = None
        self._failure_count = int(payload.get("failure_count") or 0)
        self._last_trade_date = str(payload.get("last_trade_date") or "")

    @staticmethod
    def _to_market_datetime(value: datetime) -> datetime:
        """归一化 datetime 到 Asia/Shanghai。"""
        if value.tzinfo is None:
            return value.replace(tzinfo=MARKET_TZ)
        return value.astimezone(MARKET_TZ)
