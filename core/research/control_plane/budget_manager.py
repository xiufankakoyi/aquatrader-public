"""Persistent global research budget reservations.

The control plane uses a small SQLite ledger instead of a process-local
counter.  Reservations hold capacity while a worker is running; only the
actual request/document usage is moved to ``consumed`` and unused capacity is
returned by ``release``.  The legacy ``allocate`` API is retained for callers
that do not yet provide task/attempt/action identities.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .contracts import TaskBudget, _safe_float, _safe_int, _safe_text

logger = logging.getLogger(__name__)


def _zero_budget() -> TaskBudget:
    return TaskBudget(
        max_requests=0,
        max_seconds=0.0,
        max_tokens=0,
        max_cost=0.0,
        max_documents=0,
    )


def _copy_budget(value: Any) -> TaskBudget:
    if not isinstance(value, TaskBudget):
        return _budget_from_payload(value)
    return TaskBudget(**asdict(value))


def _budget_payload(value: TaskBudget) -> dict[str, Any]:
    return asdict(value)


def _budget_from_payload(value: Any) -> TaskBudget:
    if isinstance(value, TaskBudget):
        return _copy_budget(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return TaskBudget.from_dict(value if isinstance(value, Mapping) else {})


def _add(a: TaskBudget, b: TaskBudget) -> TaskBudget:
    return TaskBudget(
        max_requests=max(0, a.max_requests + b.max_requests),
        max_seconds=max(0.0, a.max_seconds + b.max_seconds),
        max_tokens=max(0, a.max_tokens + b.max_tokens),
        max_cost=max(0.0, a.max_cost + b.max_cost),
        max_documents=max(0, a.max_documents + b.max_documents),
    )


def _sub(a: TaskBudget, b: TaskBudget) -> TaskBudget:
    return TaskBudget(
        max_requests=max(0, a.max_requests - b.max_requests),
        max_seconds=max(0.0, a.max_seconds - b.max_seconds),
        max_tokens=max(0, a.max_tokens - b.max_tokens),
        max_cost=max(0.0, a.max_cost - b.max_cost),
        max_documents=max(0, a.max_documents - b.max_documents),
    )


def _min_budget(a: TaskBudget, b: TaskBudget) -> TaskBudget:
    return TaskBudget(
        max_requests=min(a.max_requests, b.max_requests),
        max_seconds=min(a.max_seconds, b.max_seconds),
        max_tokens=min(a.max_tokens, b.max_tokens),
        max_cost=min(a.max_cost, b.max_cost),
        max_documents=min(a.max_documents, b.max_documents),
    )


def _satisfies(available: TaskBudget, required: TaskBudget) -> bool:
    if not isinstance(available, TaskBudget) or not isinstance(required, TaskBudget):
        return False
    return (
        available.max_requests >= required.max_requests
        and available.max_seconds >= required.max_seconds
        and available.max_tokens >= required.max_tokens
        and available.max_cost >= required.max_cost
        and available.max_documents >= required.max_documents
    )


def _positive(value: TaskBudget) -> bool:
    return any(
        (
            value.max_requests,
            value.max_seconds,
            value.max_tokens,
            value.max_cost,
            value.max_documents,
        )
    )


def _valid_budget_day(value: Any) -> str:
    text = _safe_text(value)[:10]
    try:
        date.fromisoformat(text)
    except (TypeError, ValueError):
        return ""
    return text


@dataclass
class GlobalBudgetSnapshot:
    date: str = ""
    total: TaskBudget = field(default_factory=TaskBudget)
    consumed: TaskBudget = field(default_factory=TaskBudget)
    remaining: TaskBudget = field(default_factory=TaskBudget)
    active_tasks: int = 0
    reserved_per_task: TaskBudget = field(default_factory=TaskBudget)
    denied_count: int = 0
    reserved: TaskBudget = field(default_factory=_zero_budget)
    released: TaskBudget = field(default_factory=_zero_budget)
    reservation_count: int = 0

    @property
    def budget_date(self) -> str:
        return self.date

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "budget_date": self.date,
            "total": _budget_payload(self.total),
            "consumed": _budget_payload(self.consumed),
            "remaining": _budget_payload(self.remaining),
            "reserved": _budget_payload(self.reserved),
            "released": _budget_payload(self.released),
            "active_tasks": self.active_tasks,
            "reserved_per_task": _budget_payload(self.reserved_per_task),
            "denied_count": self.denied_count,
            "reservation_count": self.reservation_count,
        }


@dataclass
class BudgetAllocationResult:
    granted: bool = False
    allocated: TaskBudget = field(default_factory=TaskBudget)
    reason_code: str = "budget_granted"
    reason_detail: str = ""
    reservation_id: str = ""
    budget_date: str = ""
    already_reserved: bool = False
    consumed: TaskBudget = field(default_factory=_zero_budget)
    released: TaskBudget = field(default_factory=_zero_budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "allocated": _budget_payload(self.allocated),
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "reservation_id": self.reservation_id,
            "budget_date": self.budget_date,
            "already_reserved": self.already_reserved,
            "consumed": _budget_payload(self.consumed),
            "released": _budget_payload(self.released),
        }


@dataclass
class BudgetReservation:
    reservation_id: str
    budget_date: str
    task_id: str
    attempt_id: str
    action_id: str
    requested: TaskBudget
    allocated: TaskBudget
    consumed: TaskBudget = field(default_factory=_zero_budget)
    released: TaskBudget = field(default_factory=_zero_budget)
    status: str = "reserved"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("requested", "allocated", "consumed", "released"):
            payload[key] = _budget_payload(getattr(self, key))
        return payload


class GlobalBudgetManager:
    """Daily budget manager with an optional SQLite-backed ledger.

    ``repository`` or ``db_path`` enables the persistent mode used by the
    monitor.  Omitting both preserves the old process-local behavior for
    callers/tests that only use ``allocate/release/snapshot``.
    """

    DEFAULT_DAILY_TOTAL = TaskBudget(
        max_requests=2000,
        max_seconds=7200.0,
        max_tokens=2_000_000,
        max_cost=2000.0,
        max_documents=3000,
    )
    DEFAULT_MIN_PER_TASK = TaskBudget(
        max_requests=5,
        max_seconds=30.0,
        max_tokens=5000,
        max_cost=5.0,
        max_documents=10,
    )
    BUDGET_TIMEZONE = "Asia/Shanghai"

    def __init__(
        self,
        *,
        daily_total: TaskBudget | None = None,
        min_per_task: TaskBudget | None = None,
        repository: Any | None = None,
        db_path: str | Path | None = None,
        budget_date: str | None = None,
        date_provider: Any | None = None,
    ) -> None:
        self.daily_total = _copy_budget(daily_total or self.DEFAULT_DAILY_TOTAL)
        self.min_per_task = _copy_budget(min_per_task or self.DEFAULT_MIN_PER_TASK)
        self.repository = repository
        repository_path = getattr(repository, "db_path", None)
        selected_path = db_path or repository_path
        self.db_path = Path(selected_path) if selected_path else None
        self._persistent = self.db_path is not None
        self._fixed_date = _safe_text(budget_date)[:10]
        self._date_provider = date_provider
        self._lock = threading.RLock()

        # Compatibility/in-memory ledger.
        self._current_date = ""
        self._consumed = _zero_budget()
        self._reserved = _zero_budget()
        self._released = _zero_budget()
        self._active_tasks = 0
        self._denied_count = 0
        self._memory_reservations: dict[str, BudgetReservation] = {}
        self._legacy_reservation_ids: list[str] = []
        if self._persistent:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()
        else:
            self._reset_memory_if_new_day()

    # ------------------------------------------------------------------
    # Date and SQLite primitives
    # ------------------------------------------------------------------
    def _budget_day(self) -> str:
        fixed = _valid_budget_day(self._fixed_date)
        if fixed:
            return fixed
        if callable(self._date_provider):
            value = self._date_provider()
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%d")
            if isinstance(value, date):
                return value.isoformat()
            provided = _valid_budget_day(value)
            if provided:
                return provided
        return datetime.now(ZoneInfo(self.BUDGET_TIMEZONE)).strftime("%Y-%m-%d")

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self.db_path is None:
            raise RuntimeError("persistent budget database is not configured")
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._lock, self._connect(write=True) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS research_budget_days (
                    budget_date TEXT PRIMARY KEY,
                    total_json TEXT NOT NULL,
                    reserved_json TEXT NOT NULL,
                    consumed_json TEXT NOT NULL,
                    released_json TEXT NOT NULL,
                    active_tasks INTEGER NOT NULL DEFAULT 0,
                    denied_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS research_budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    budget_date TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    action_id TEXT NOT NULL DEFAULT '',
                    requested_json TEXT NOT NULL,
                    allocated_json TEXT NOT NULL,
                    consumed_json TEXT NOT NULL,
                    released_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (budget_date, task_id, attempt_id, action_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_budget_reservations_date "
                "ON research_budget_reservations(budget_date, status, updated_at)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _ensure_day(self, conn: sqlite3.Connection, budget_day: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM research_budget_days WHERE budget_date=?", (budget_day,)
        ).fetchone()
        if row:
            return row
        now = self._now()
        zero = json.dumps(_budget_payload(_zero_budget()))
        conn.execute(
            """INSERT INTO research_budget_days
               (budget_date,total_json,reserved_json,consumed_json,released_json,
                active_tasks,denied_count,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                budget_day,
                json.dumps(_budget_payload(self.daily_total)),
                zero,
                zero,
                zero,
                0,
                0,
                now,
            ),
        )
        return conn.execute(
            "SELECT * FROM research_budget_days WHERE budget_date=?", (budget_day,)
        ).fetchone()

    @staticmethod
    def _row_budget(row: sqlite3.Row, name: str) -> TaskBudget:
        return _budget_from_payload(row[f"{name}_json"])

    def _snapshot_from_row(self, row: sqlite3.Row) -> GlobalBudgetSnapshot:
        total = self._row_budget(row, "total")
        reserved = self._row_budget(row, "reserved")
        consumed = self._row_budget(row, "consumed")
        released = self._row_budget(row, "released")
        remaining = _sub(_sub(total, consumed), reserved)
        with self._connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM research_budget_reservations "
                "WHERE budget_date=? AND status='reserved'",
                (row["budget_date"],),
            ).fetchone()
        return GlobalBudgetSnapshot(
            date=str(row["budget_date"]),
            total=total,
            consumed=consumed,
            remaining=remaining,
            active_tasks=int(row["active_tasks"] or 0),
            reserved_per_task=_copy_budget(self.min_per_task),
            denied_count=int(row["denied_count"] or 0),
            reserved=reserved,
            released=released,
            reservation_count=int(count_row["n"] if count_row else 0),
        )

    # ------------------------------------------------------------------
    # Public reservation API
    # ------------------------------------------------------------------
    def reserve(
        self,
        requested: TaskBudget,
        *,
        task_id: str = "",
        attempt_id: str = "",
        action_id: str = "",
        budget_date: str | None = None,
    ) -> BudgetAllocationResult:
        """Idempotently reserve daily capacity for one task action."""
        if not isinstance(requested, TaskBudget):
            return BudgetAllocationResult(
                granted=False,
                allocated=_zero_budget(),
                reason_code="invalid_budget",
                reason_detail="requested budget must be a TaskBudget",
            )
        requested = _copy_budget(requested)
        day = _valid_budget_day(budget_date) or self._budget_day()
        action = _safe_text(action_id)
        if not action:
            action = "action_" + uuid.uuid4().hex
        reservation_id = "br_" + hashlib.sha256(
            f"{day}|{task_id}|{attempt_id}|{action}".encode("utf-8")
        ).hexdigest()[:24]
        if self._persistent:
            return self._reserve_sqlite(
                requested,
                day=day,
                task_id=_safe_text(task_id),
                attempt_id=_safe_text(attempt_id),
                action_id=action,
                reservation_id=reservation_id,
            )
        return self._reserve_memory(
            requested,
            day=day,
            task_id=_safe_text(task_id),
            attempt_id=_safe_text(attempt_id),
            action_id=action,
            reservation_id=reservation_id,
        )

    def _allocation_for_remaining(
        self, remaining: TaskBudget, requested: TaskBudget
    ) -> tuple[TaskBudget, str, str] | None:
        if _satisfies(remaining, requested):
            return requested, "budget_granted", "requested budget reserved"
        if _satisfies(remaining, self.min_per_task):
            allocated = _min_budget(remaining, requested)
            return allocated, "budget_insufficient", "daily budget insufficient; minimum guarantee reserved"
        return None

    def _reserve_sqlite(
        self,
        requested: TaskBudget,
        *,
        day: str,
        task_id: str,
        attempt_id: str,
        action_id: str,
        reservation_id: str,
    ) -> BudgetAllocationResult:
        with self._lock, self._connect(write=True) as conn:
            row = self._ensure_day(conn, day)
            existing = conn.execute(
                "SELECT * FROM research_budget_reservations WHERE budget_date=? "
                "AND task_id=? AND attempt_id=? AND action_id=?",
                (day, task_id, attempt_id, action_id),
            ).fetchone()
            if existing:
                return self._result_from_reservation(existing, already=True)
            total = self._row_budget(row, "total")
            reserved = self._row_budget(row, "reserved")
            consumed = self._row_budget(row, "consumed")
            remaining = _sub(_sub(total, consumed), reserved)
            allocation = self._allocation_for_remaining(remaining, requested)
            now = self._now()
            if allocation is None:
                conn.execute(
                    "UPDATE research_budget_days SET denied_count=denied_count+1, updated_at=? "
                    "WHERE budget_date=?",
                    (now, day),
                )
                return BudgetAllocationResult(
                    granted=False,
                    allocated=_zero_budget(),
                    reason_code="budget_exhausted",
                    reason_detail=f"remaining budget below minimum guarantee: {remaining}",
                    budget_date=day,
                )
            allocated, reason_code, detail = allocation
            conn.execute(
                """INSERT INTO research_budget_reservations
                   (reservation_id,budget_date,task_id,attempt_id,action_id,
                    requested_json,allocated_json,consumed_json,released_json,
                    status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reservation_id,
                    day,
                    task_id,
                    attempt_id,
                    action_id,
                    json.dumps(_budget_payload(requested)),
                    json.dumps(_budget_payload(allocated)),
                    json.dumps(_budget_payload(_zero_budget())),
                    json.dumps(_budget_payload(_zero_budget())),
                    "reserved",
                    now,
                    now,
                ),
            )
            new_reserved = _add(reserved, allocated)
            conn.execute(
                "UPDATE research_budget_days SET reserved_json=?, active_tasks=active_tasks+1, updated_at=? "
                "WHERE budget_date=?",
                (json.dumps(_budget_payload(new_reserved)), now, day),
            )
            return BudgetAllocationResult(
                granted=True,
                allocated=allocated,
                reason_code=reason_code,
                reason_detail=detail,
                reservation_id=reservation_id,
                budget_date=day,
            )

    def _reserve_memory(
        self,
        requested: TaskBudget,
        *,
        day: str,
        task_id: str,
        attempt_id: str,
        action_id: str,
        reservation_id: str,
    ) -> BudgetAllocationResult:
        with self._lock:
            self._reset_memory_if_new_day(day)
            for item in self._memory_reservations.values():
                if (
                    item.budget_date == day
                    and item.task_id == task_id
                    and item.attempt_id == attempt_id
                    and item.action_id == action_id
                ):
                    return BudgetAllocationResult(
                        granted=True,
                        allocated=_copy_budget(item.allocated),
                        reason_code="budget_granted",
                        reason_detail="idempotent reservation replay",
                        reservation_id=item.reservation_id,
                        budget_date=day,
                        already_reserved=True,
                        consumed=_copy_budget(item.consumed),
                        released=_copy_budget(item.released),
                    )
            remaining = _sub(_sub(self.daily_total, self._consumed), self._reserved)
            allocation = self._allocation_for_remaining(remaining, requested)
            if allocation is None:
                self._denied_count += 1
                return BudgetAllocationResult(
                    granted=False,
                    allocated=_zero_budget(),
                    reason_code="budget_exhausted",
                    reason_detail=f"remaining budget below minimum guarantee: {remaining}",
                    budget_date=day,
                )
            allocated, reason_code, detail = allocation
            now = self._now()
            item = BudgetReservation(
                reservation_id=reservation_id,
                budget_date=day,
                task_id=task_id,
                attempt_id=attempt_id,
                action_id=action_id,
                requested=requested,
                allocated=allocated,
                created_at=now,
                updated_at=now,
            )
            self._memory_reservations[reservation_id] = item
            self._reserved = _add(self._reserved, allocated)
            self._active_tasks += 1
            return BudgetAllocationResult(
                granted=True,
                allocated=allocated,
                reason_code=reason_code,
                reason_detail=detail,
                reservation_id=reservation_id,
                budget_date=day,
            )

    def consume(
        self,
        reservation_id: str,
        consumed: TaskBudget | None = None,
    ) -> BudgetAllocationResult:
        """Move actual usage from a reservation to daily consumed usage."""
        usage = _copy_budget(consumed or _zero_budget())
        if self._persistent:
            return self._consume_sqlite(_safe_text(reservation_id), usage)
        with self._lock:
            item = self._memory_reservations.get(_safe_text(reservation_id))
            if item is None:
                return BudgetAllocationResult(False, reason_code="reservation_not_found")
            available = _sub(_sub(item.allocated, item.consumed), item.released)
            delta = _min_budget(available, usage)
            item.consumed = _add(item.consumed, delta)
            item.updated_at = self._now()
            self._reserved = _sub(self._reserved, delta)
            self._consumed = _add(self._consumed, delta)
            if not _positive(delta) and item.status != "reserved":
                return self._result_from_memory(item)
            item.status = "consumed" if _positive(delta) else item.status
            return self._result_from_memory(item)

    def _consume_sqlite(self, reservation_id: str, usage: TaskBudget) -> BudgetAllocationResult:
        with self._lock, self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM research_budget_reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if not row:
                return BudgetAllocationResult(False, reason_code="reservation_not_found")
            allocated = _budget_from_payload(row["allocated_json"])
            current = _budget_from_payload(row["consumed_json"])
            released = _budget_from_payload(row["released_json"])
            delta = _min_budget(_sub(_sub(allocated, current), released), usage)
            if not _positive(delta):
                return self._result_from_reservation(row, already=True)
            day = str(row["budget_date"])
            day_row = self._ensure_day(conn, day)
            reserved_total = self._row_budget(day_row, "reserved")
            consumed_total = self._row_budget(day_row, "consumed")
            new_current = _add(current, delta)
            conn.execute(
                "UPDATE research_budget_reservations SET consumed_json=?, status='consumed', updated_at=? "
                "WHERE reservation_id=?",
                (json.dumps(_budget_payload(new_current)), self._now(), reservation_id),
            )
            conn.execute(
                "UPDATE research_budget_days SET reserved_json=?, consumed_json=?, updated_at=? WHERE budget_date=?",
                (
                    json.dumps(_budget_payload(_sub(reserved_total, delta))),
                    json.dumps(_budget_payload(_add(consumed_total, delta))),
                    self._now(),
                    day,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM research_budget_reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            return self._result_from_reservation(updated)

    def allocate(self, requested: TaskBudget) -> BudgetAllocationResult:
        """Legacy allocation: reserve and immediately mark requested usage."""
        action = "legacy_" + uuid.uuid4().hex
        result = self.reserve(requested, action_id=action)
        if result.granted and result.reservation_id:
            self.consume(result.reservation_id, result.allocated)
            with self._lock:
                self._legacy_reservation_ids.append(result.reservation_id)
            result.consumed = _copy_budget(result.allocated)
        return result

    def release(
        self,
        consumed: TaskBudget | str | None = None,
        *,
        reservation_id: str = "",
        task_id: str = "",
        attempt_id: str = "",
        action_id: str = "",
        unused: TaskBudget | None = None,
    ) -> None:
        """Release unused reservation capacity; repeated calls are harmless.

        The positional ``TaskBudget`` argument is accepted for the legacy
        API.  In persistent mode, callers should pass ``reservation_id`` (or
        the full idempotency key) so the exact reservation is audited.
        """
        if isinstance(consumed, str) and not reservation_id:
            reservation_id = consumed
        if not reservation_id and task_id and attempt_id and action_id:
            day = self._budget_day()
            reservation_id = "br_" + hashlib.sha256(
                f"{day}|{task_id}|{attempt_id}|{action_id}".encode("utf-8")
            ).hexdigest()[:24]
        if not reservation_id:
            with self._lock:
                if self._legacy_reservation_ids:
                    reservation_id = self._legacy_reservation_ids.pop()
                elif not self._persistent:
                    self._active_tasks = max(0, self._active_tasks - 1)
                    return
        if not reservation_id:
            return
        if unused is not None:
            unused = _copy_budget(unused)
        reservation_id = _safe_text(reservation_id)
        if not reservation_id:
            return
        if self._persistent:
            self._release_sqlite(reservation_id, unused)
        else:
            self._release_memory(reservation_id, unused)

    def _release_memory(self, reservation_id: str, unused: TaskBudget | None) -> None:
        with self._lock:
            item = self._memory_reservations.get(reservation_id)
            if item is None:
                return
            if item.status == "released":
                return
            available = _sub(_sub(item.allocated, item.consumed), item.released)
            returned = _min_budget(available, unused or available)
            self._reserved = _sub(self._reserved, returned)
            self._released = _add(self._released, returned)
            item.released = _add(item.released, returned)
            item.status = "released"
            item.updated_at = self._now()
            self._active_tasks = max(0, self._active_tasks - 1)

    def _release_sqlite(self, reservation_id: str, unused: TaskBudget | None) -> None:
        with self._lock, self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM research_budget_reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if not row:
                return
            if str(row["status"]) == "released":
                return
            allocated = _budget_from_payload(row["allocated_json"])
            current = _budget_from_payload(row["consumed_json"])
            released = _budget_from_payload(row["released_json"])
            available = _sub(_sub(allocated, current), released)
            returned = _min_budget(available, unused or available)
            day = str(row["budget_date"])
            day_row = self._ensure_day(conn, day)
            reserved_total = self._row_budget(day_row, "reserved")
            released_total = self._row_budget(day_row, "released")
            new_released = _add(released, returned)
            status = "released" if not _positive(_sub(available, returned)) else "reserved"
            conn.execute(
                "UPDATE research_budget_reservations SET released_json=?, status=?, updated_at=? WHERE reservation_id=?",
                (json.dumps(_budget_payload(new_released)), status, self._now(), reservation_id),
            )
            active_delta = 1 if status == "released" else 0
            conn.execute(
                "UPDATE research_budget_days SET reserved_json=?, released_json=?, active_tasks=MAX(0, active_tasks-?), updated_at=? WHERE budget_date=?",
                (
                    json.dumps(_budget_payload(_sub(reserved_total, returned))),
                    json.dumps(_budget_payload(_add(released_total, returned))),
                    active_delta,
                    self._now(),
                    day,
                ),
            )

    # ------------------------------------------------------------------
    # Snapshots and audit
    # ------------------------------------------------------------------
    def snapshot(self, *, budget_date: str | None = None) -> GlobalBudgetSnapshot:
        day = _valid_budget_day(budget_date) or self._budget_day()
        if self._persistent:
            with self._lock, self._connect(write=True) as conn:
                row = self._ensure_day(conn, day)
            return self._snapshot_from_row(row)
        with self._lock:
            self._reset_memory_if_new_day(day)
            remaining = _sub(_sub(self.daily_total, self._consumed), self._reserved)
            return GlobalBudgetSnapshot(
                date=self._current_date,
                total=_copy_budget(self.daily_total),
                consumed=_copy_budget(self._consumed),
                remaining=remaining,
                active_tasks=self._active_tasks,
                reserved_per_task=_copy_budget(self.min_per_task),
                denied_count=self._denied_count,
                reserved=_copy_budget(self._reserved),
                released=_copy_budget(self._released),
                reservation_count=sum(
                    1 for item in self._memory_reservations.values()
                    if item.status == "reserved"
                ),
            )

    def list_reservations(
        self,
        *,
        budget_date: str | None = None,
        task_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        day = _valid_budget_day(budget_date) or self._budget_day()
        task_id = _safe_text(task_id)
        safe_limit = max(1, min(_safe_int(limit, 100), 1000))
        if self._persistent:
            with self._connect() as conn:
                query = "SELECT * FROM research_budget_reservations WHERE budget_date=?"
                params: list[Any] = [day]
                if task_id:
                    query += " AND task_id=?"
                    params.append(task_id)
                query += " ORDER BY rowid DESC LIMIT ?"
                params.append(safe_limit)
                rows = conn.execute(query, tuple(params)).fetchall()
            return [self._reservation_row_to_dict(row) for row in rows]
        return [
            item.to_dict()
            for item in list(self._memory_reservations.values())[::-1]
            if item.budget_date == day and (not task_id or item.task_id == task_id)
        ][:safe_limit]

    @staticmethod
    def _reservation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "budget_date": row["budget_date"],
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "action_id": row["action_id"],
            "requested": _budget_payload(_budget_from_payload(row["requested_json"])),
            "allocated": _budget_payload(_budget_from_payload(row["allocated_json"])),
            "consumed": _budget_payload(_budget_from_payload(row["consumed_json"])),
            "released": _budget_payload(_budget_from_payload(row["released_json"])),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _result_from_reservation(
        cls, row: sqlite3.Row, *, already: bool = False
    ) -> BudgetAllocationResult:
        allocated = _budget_from_payload(row["allocated_json"])
        return BudgetAllocationResult(
            granted=True,
            allocated=allocated,
            reason_code="budget_granted",
            reason_detail="idempotent reservation replay" if already else "",
            reservation_id=str(row["reservation_id"]),
            budget_date=str(row["budget_date"]),
            already_reserved=already,
            consumed=_budget_from_payload(row["consumed_json"]),
            released=_budget_from_payload(row["released_json"]),
        )

    @staticmethod
    def _result_from_memory(item: BudgetReservation) -> BudgetAllocationResult:
        return BudgetAllocationResult(
            granted=True,
            allocated=_copy_budget(item.allocated),
            reason_code="budget_granted",
            reservation_id=item.reservation_id,
            budget_date=item.budget_date,
            consumed=_copy_budget(item.consumed),
            released=_copy_budget(item.released),
        )

    def _reset_memory_if_new_day(self, day: str | None = None) -> None:
        current = str(day or self._budget_day())[:10]
        if self._current_date != current:
            self._current_date = current
            self._consumed = _zero_budget()
            self._reserved = _zero_budget()
            self._released = _zero_budget()
            self._active_tasks = 0
            self._denied_count = 0
            self._memory_reservations.clear()
            self._legacy_reservation_ids.clear()

    # ------------------------------------------------------------------
    # P2.4：闲时预算上限辅助方法
    # 按 P2.4 §174「闲时预算不超过总预算 20%」，提供显式 API 供
    # TopKResearchScheduler 在闲时模式调用，避免在调度器内重复实现。
    # ------------------------------------------------------------------

    DEFAULT_IDLE_BUDGET_RATIO: float = 0.2

    def idle_budget_cap(
        self,
        ratio: float | None = None,
        *,
        budget_date: str | None = None,
    ) -> TaskBudget:
        """计算闲时预算上限（默认 20% 总预算）。

        Args:
            ratio: 自定义比例（None 则使用 DEFAULT_IDLE_BUDGET_RATIO）；
                自动夹紧到 [0, 1]
            budget_date: 指定预算日（None 则当日）

        Returns:
            闲时预算上限 TaskBudget
        """
        cap_ratio = max(0.0, min(1.0, _safe_float(ratio, self.DEFAULT_IDLE_BUDGET_RATIO)))
        snapshot = self.snapshot(budget_date=budget_date)
        total = snapshot.total
        return TaskBudget(
            max_requests=int(total.max_requests * cap_ratio),
            max_seconds=total.max_seconds * cap_ratio,
            max_tokens=int(total.max_tokens * cap_ratio),
            max_cost=total.max_cost * cap_ratio,
            max_documents=int(total.max_documents * cap_ratio),
        )

    def idle_budget_remaining(
        self,
        ratio: float | None = None,
        *,
        budget_date: str | None = None,
    ) -> TaskBudget:
        """计算闲时预算剩余量（上限减去已消耗）。

        Args:
            ratio: 自定义比例（None 则使用 DEFAULT_IDLE_BUDGET_RATIO）
            budget_date: 指定预算日

        Returns:
            剩余闲时预算 TaskBudget（不会为负）
        """
        cap = self.idle_budget_cap(ratio, budget_date=budget_date)
        snapshot = self.snapshot(budget_date=budget_date)
        consumed = snapshot.consumed
        return TaskBudget(
            max_requests=max(0, cap.max_requests - consumed.max_requests),
            max_seconds=max(0.0, cap.max_seconds - consumed.max_seconds),
            max_tokens=max(0, cap.max_tokens - consumed.max_tokens),
            max_cost=max(0.0, cap.max_cost - consumed.max_cost),
            max_documents=max(0, cap.max_documents - consumed.max_documents),
        )

    def is_idle_budget_within_cap(
        self,
        requested: TaskBudget,
        ratio: float | None = None,
        *,
        budget_date: str | None = None,
    ) -> bool:
        """检查请求是否在闲时预算上限内。

        Args:
            requested: 请求预算
            ratio: 自定义比例（None 则使用 DEFAULT_IDLE_BUDGET_RATIO）
            budget_date: 指定预算日

        Returns:
            True 表示请求预算在闲时上限内可满足
        """
        if not isinstance(requested, TaskBudget):
            return False
        remaining = self.idle_budget_remaining(ratio, budget_date=budget_date)
        return _satisfies(remaining, requested)
