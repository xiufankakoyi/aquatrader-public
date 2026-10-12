"""Process lease and heartbeat inspection for the research monitor daemon."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class DaemonAlreadyRunning(RuntimeError):
    """Raised when another research monitor daemon owns the process lease."""


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            return int(json.loads(raw)["pid"])
        return int(raw)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None


def _read_owner(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("owner_token") or "")
    except (AttributeError, OSError, TypeError, ValueError):
        return ""


def _process_matches_daemon(pid: int) -> bool:
    """Return whether *pid* is a live research monitor daemon process."""
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        command = " ".join(process.cmdline()).lower().replace("\\", "/")
        return "run_research_monitor_daemon.py" in command
    except Exception:
        return False


class SingleInstanceLease:
    """OS-locked lease with owner-checked PID metadata cleanup."""

    def __init__(self, pid_path: str | Path) -> None:
        self.path = Path(pid_path)
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self._owned = False
        self._owner_token = uuid4().hex
        self._lock_handle: Any | None = None

    def _acquire_os_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise DaemonAlreadyRunning(
                "research monitor daemon lease is already locked"
            ) from exc
        self._lock_handle = handle

    def _release_os_lock(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_os_lock()
        existing_pid = _read_pid(self.path)
        if existing_pid and _process_matches_daemon(existing_pid):
            self._release_os_lock()
            raise DaemonAlreadyRunning(
                f"research monitor daemon already running: pid={existing_pid}"
            )
        try:
            temporary = self.path.with_suffix(
                f"{self.path.suffix}.{self._owner_token}.tmp"
            )
            temporary.write_text(
                json.dumps(
                    {"pid": os.getpid(), "owner_token": self._owner_token},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except Exception:
            self._release_os_lock()
            raise
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        try:
            if _read_owner(self.path) == self._owner_token:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._owned = False
            self._release_os_lock()

    def __enter__(self) -> "SingleInstanceLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def inspect_daemon_health(
    state_path: str | Path,
    *,
    pid_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine PID identity and heartbeat freshness into one truthful status."""
    state_path = Path(state_path)
    pid_path = Path(pid_path) if pid_path else state_path.with_suffix(".pid")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        state = {}
    runtime = state.get("runtime") if isinstance(state, dict) else {}
    if not isinstance(runtime, dict):
        runtime = {}

    runtime_pid = runtime.get("pid")
    try:
        runtime_pid = int(runtime_pid)
    except (TypeError, ValueError):
        runtime_pid = None
    file_pid = _read_pid(pid_path)
    heartbeat = _parse_timestamp(runtime.get("heartbeat_at"))
    invalid_tick_interval = False
    try:
        tick_interval = max(
            1,
            int(runtime.get("tick_interval_seconds") or 300),
        )
    except (TypeError, ValueError):
        tick_interval = 300
        invalid_tick_interval = True
    stale_after = max(90, tick_interval * 2 + 60)

    result: dict[str, Any] = {
        "daemon_running": False,
        "process_alive": False,
        "scheduler_healthy": False,
        "health_status": "unhealthy",
        "daemon_pid": runtime_pid or file_pid,
        "health_reason": "missing_pid_file",
        "heartbeat_at": runtime.get("heartbeat_at"),
        "heartbeat_stale_after_seconds": stale_after,
    }
    if file_pid is None:
        return result
    if runtime_pid is None:
        result["health_reason"] = "missing_runtime_pid"
        return result
    if runtime_pid != file_pid:
        result["health_reason"] = "pid_mismatch"
        return result
    if not _process_matches_daemon(file_pid):
        result["health_reason"] = "process_not_running"
        return result
    result["process_alive"] = True
    result["health_status"] = "degraded"
    if heartbeat is None:
        result["health_reason"] = "missing_heartbeat"
        return result

    heartbeat_age = max(0.0, (now - heartbeat.astimezone(now.tzinfo)).total_seconds())
    result["heartbeat_age_seconds"] = round(heartbeat_age, 3)
    if heartbeat_age > stale_after:
        result["health_reason"] = "stale_heartbeat"
        return result
    if invalid_tick_interval:
        result["health_reason"] = "invalid_tick_interval"
        return result

    tick_started = _parse_timestamp(runtime.get("last_tick_started_at"))
    tick_finished = _parse_timestamp(runtime.get("last_tick_finished_at"))
    if (
        tick_started is not None
        and (tick_finished is None or tick_finished < tick_started)
        and (now - tick_started.astimezone(now.tzinfo)).total_seconds()
        > stale_after
    ):
        result["health_reason"] = "tick_timeout"
        return result

    try:
        consecutive_failures = max(
            0,
            int(runtime.get("consecutive_failures") or 0),
        )
    except (TypeError, ValueError):
        consecutive_failures = 1
    result["consecutive_failures"] = consecutive_failures
    if runtime.get("last_tick_status") == "error" or consecutive_failures:
        result["health_reason"] = "tick_error"
        return result

    result["daemon_running"] = True
    result["scheduler_healthy"] = True
    result["health_status"] = "healthy"
    result["health_reason"] = "healthy"
    return result


def _read_json_value(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def inspect_research_cycle_health(
    *,
    state_path: str | Path,
    research_db_path: str | Path,
    cohort_observations_path: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect persisted daily learning and forward-observation progress.

    This deliberately reads the append-only ledgers instead of treating
    generic retrieval feedback or generated reports as proof of learning.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    market_now = now.astimezone(timezone(timedelta(hours=8)))
    state = _read_json_value(Path(state_path), {})
    daily = state.get("daily", {}) if isinstance(state, dict) else {}
    if not isinstance(daily, dict):
        daily = {}
    current_date = market_now.date().isoformat()
    current_cycle = daily.get(current_date, {})
    if not isinstance(current_cycle, dict):
        current_cycle = {}

    predictions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    database_error = ""
    try:
        with sqlite3.connect(Path(research_db_path)) as connection:
            connection.row_factory = sqlite3.Row
            predictions = [
                dict(row)
                for row in connection.execute(
                    "SELECT prediction_id,as_of,entity_id,payload_json,recorded_at "
                    "FROM learning_predictions ORDER BY recorded_at"
                )
            ]
            observations = [
                dict(row)
                for row in connection.execute(
                    "SELECT prediction_id,observed_at,payload_json,recorded_at "
                    "FROM learning_observations ORDER BY recorded_at"
                )
            ]
    except (OSError, sqlite3.Error) as exc:
        database_error = f"{type(exc).__name__}: {exc}"

    settled_prediction_ids = {
        str(row.get("prediction_id") or "") for row in observations
    }
    due_unsettled = 0
    cohort_counts: Counter[tuple[str, ...]] = Counter()
    for row in predictions:
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError):
            payload = {}
        due_at_raw = str(payload.get("due_at") or "")
        cohort_counts[
            (
                str(row.get("entity_id") or payload.get("entity_id") or ""),
                str(payload.get("research_question") or ""),
                due_at_raw[:10],
                str(payload.get("model_version") or ""),
                str(payload.get("policy_version") or ""),
            )
        ] += 1
        if str(row.get("prediction_id") or "") in settled_prediction_ids:
            continue
        due_at = _parse_timestamp(payload.get("due_at"))
        if due_at is not None and due_at <= now.astimezone(due_at.tzinfo):
            due_unsettled += 1
    duplicate_cohorts = sum(
        count - 1 for count in cohort_counts.values() if count > 1
    )

    prediction_days: list[str] = []
    for row in predictions:
        try:
            prediction_payload = json.loads(
                str(row.get("payload_json") or "{}")
            )
        except (TypeError, ValueError):
            prediction_payload = {}
        semantic_as_of = (
            prediction_payload.get("as_of")
            if isinstance(prediction_payload, dict)
            else None
        ) or row.get("as_of")
        parsed_as_of = _parse_timestamp(semantic_as_of)
        if parsed_as_of is not None:
            prediction_days.append(
                parsed_as_of.astimezone(
                    timezone(timedelta(hours=8))
                ).date().isoformat()
            )
    predictions_by_day = Counter(prediction_days)
    outcome_days: list[str] = []
    for row in observations:
        try:
            outcome_payload = json.loads(
                str(row.get("payload_json") or "{}")
            )
        except (TypeError, ValueError):
            outcome_payload = {}
        outcome_context = outcome_payload.get("outcome_context") or {}
        trade_date = (
            str(outcome_context.get("trade_date") or "")[:10]
            if isinstance(outcome_context, dict)
            else ""
        )
        if not trade_date:
            observed_at = _parse_timestamp(row.get("observed_at"))
            if observed_at is not None:
                trade_date = observed_at.astimezone(
                    timezone(timedelta(hours=8))
                ).date().isoformat()
        if trade_date:
            outcome_days.append(trade_date)
    outcomes_by_day = Counter(outcome_days)
    latest_return_feedback: dict[str, Any] = {}
    if observations:
        latest = observations[-1]
        try:
            payload = json.loads(str(latest.get("payload_json") or "{}"))
        except (TypeError, ValueError):
            payload = {}
        outcome_context = payload.get("outcome_context") or {}
        if not isinstance(outcome_context, dict):
            outcome_context = {}
        latest_return_feedback = {
            "actual_value": payload.get("actual_value"),
            "attribution": payload.get("attribution"),
            "observed_at": latest.get("observed_at"),
            "prediction_id": latest.get("prediction_id"),
            "was_correct": payload.get("was_correct"),
            "trade_date": outcome_context.get("trade_date"),
            "stock_return": outcome_context.get("stock_return"),
            "benchmark_return": outcome_context.get(
                "benchmark_return"
            ),
            "excess_return": outcome_context.get("excess_return"),
        }

    cohort = _read_json_value(Path(cohort_observations_path), [])
    if not isinstance(cohort, list):
        cohort = []
    trade_dates = sorted(
        {
            str(row.get("trade_date") or "")[:10]
            for row in cohort
            if isinstance(row, dict) and str(row.get("trade_date") or "")[:10]
        }
    )
    latest_forward_date = trade_dates[-1] if trade_dates else ""

    issues: list[str] = []
    if database_error:
        issues.append("learning_ledger_unavailable")
    if due_unsettled:
        issues.append("due_learning_unsettled")
    if current_cycle.get("close_done") and not current_cycle.get(
        "close_postprocess_done"
    ):
        issues.append("close_postprocess_incomplete")
    if (
        current_cycle.get("close_done")
        and latest_forward_date < current_date
    ):
        issues.append("forward_progress_stalled")
    if current_cycle:
        market_minutes = market_now.hour * 60 + market_now.minute
        if market_minutes > 9 * 60 + 25 and not current_cycle.get(
            "premarket_done"
        ):
            issues.append("premarket_incomplete")
        if (
            market_minutes > 9 * 60 + 25
            and current_cycle.get("premarket_done")
            and not current_cycle.get("push_done")
        ):
            issues.append("premarket_delivery_incomplete")
        if market_minutes > 15 * 60 and not current_cycle.get(
            "tail_research_done"
        ):
            issues.append("tail_research_incomplete")
        if market_minutes > 15 * 60 + 30 and not current_cycle.get(
            "close_done"
        ):
            issues.append("close_incomplete")

    return {
        "as_of": now.isoformat(),
        "trade_date": current_date,
        "status": "healthy" if not issues else "degraded",
        "issues": issues,
        "cycle": {
            "premarket_done": bool(current_cycle.get("premarket_done")),
            "premarket_delivery_done": bool(
                current_cycle.get("push_done")
            ),
            "tail_research_done": bool(
                current_cycle.get("tail_research_done")
            ),
            "tail_research_delivery_done": bool(
                current_cycle.get("tail_research_delivery_done")
            ),
            "close_done": bool(current_cycle.get("close_done")),
            "close_delivery_done": bool(
                current_cycle.get("close_delivery_done")
            ),
            "close_postprocess_done": bool(
                current_cycle.get("close_postprocess_done")
            ),
            "premarket_last_attempt": current_cycle.get(
                "premarket_last_attempt",
                {},
            ),
            "push_last_attempt": current_cycle.get(
                "push_last_attempt",
                {},
            ),
            "tail_research_last_attempt": current_cycle.get(
                "tail_research_last_attempt",
                {},
            ),
            "close_last_attempt": current_cycle.get(
                "close_last_attempt",
                {},
            ),
        },
        "learning": {
            "predictions_total": len(predictions),
            "outcomes_total": len(observations),
            "due_unsettled": due_unsettled,
            "duplicate_cohorts": duplicate_cohorts,
            "predictions_by_day": dict(sorted(predictions_by_day.items())),
            "outcomes_by_day": dict(sorted(outcomes_by_day.items())),
            "latest_return_feedback": latest_return_feedback,
            "database_error": database_error,
        },
        "forward_progress": {
            "collected_trade_days": len(trade_dates),
            "required_trade_days": 20,
            "remaining_trade_days": max(0, 20 - len(trade_dates)),
            "latest_trade_date": latest_forward_date,
            "trade_dates": trade_dates,
        },
    }
