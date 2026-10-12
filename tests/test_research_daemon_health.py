from __future__ import annotations

import json
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _load_daemon_module():
    project_root = Path(__file__).resolve().parents[1]
    path = project_root / "scripts" / "run_research_monitor_daemon.py"
    spec = importlib.util.spec_from_file_location("daemon_health_test_target", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_state(*, pid: int, heartbeat_at: datetime) -> dict:
    return {
        "runtime": {
            "pid": pid,
            "heartbeat_at": heartbeat_at.isoformat(),
            "tick_interval_seconds": 300,
            "status": "running",
        }
    }


def test_health_requires_a_fresh_heartbeat_and_matching_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state_path.write_text(
        json.dumps(_runtime_state(pid=321, heartbeat_at=now - timedelta(seconds=10))),
        encoding="utf-8",
    )
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: pid == 321)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["daemon_running"] is True
    assert result["health_reason"] == "healthy"
    assert result["daemon_pid"] == 321


def test_health_rejects_a_stale_heartbeat_even_when_pid_is_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state_path.write_text(
        json.dumps(_runtime_state(pid=321, heartbeat_at=now - timedelta(minutes=20))),
        encoding="utf-8",
    )
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: True)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["daemon_running"] is False
    assert result["health_reason"] == "stale_heartbeat"


def test_health_rejects_pid_reuse_by_an_unrelated_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state_path.write_text(
        json.dumps(_runtime_state(pid=321, heartbeat_at=now)),
        encoding="utf-8",
    )
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: False)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["daemon_running"] is False
    assert result["health_reason"] == "process_not_running"


def test_health_distinguishes_live_process_from_failed_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state = _runtime_state(pid=321, heartbeat_at=now)
    state["runtime"]["last_tick_status"] = "error"
    state["runtime"]["consecutive_failures"] = 2
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: True)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["process_alive"] is True
    assert result["scheduler_healthy"] is False
    assert result["health_status"] == "degraded"
    assert result["health_reason"] == "tick_error"


def test_health_detects_tick_timeout_with_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state = _runtime_state(pid=321, heartbeat_at=now)
    state["runtime"]["last_tick_started_at"] = (
        now - timedelta(minutes=20)
    ).isoformat()
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: True)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["process_alive"] is True
    assert result["scheduler_healthy"] is False
    assert result["health_reason"] == "tick_timeout"


def test_corrupt_tick_interval_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    state = _runtime_state(pid=321, heartbeat_at=now)
    state["runtime"]["tick_interval_seconds"] = "not-a-number"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: True)

    result = health.inspect_daemon_health(state_path, pid_path=pid_path, now=now)

    assert result["process_alive"] is True
    assert result["scheduler_healthy"] is False
    assert result["health_reason"] == "invalid_tick_interval"


def test_single_instance_lease_reclaims_a_stale_pid_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: False)

    lease = health.SingleInstanceLease(pid_path)
    lease.acquire()

    assert json.loads(pid_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lease.release()
    assert not pid_path.exists()


def test_single_instance_lease_refuses_a_live_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("321", encoding="utf-8")
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: pid == 321)

    with pytest.raises(health.DaemonAlreadyRunning):
        health.SingleInstanceLease(pid_path).acquire()


def test_single_instance_lease_uses_os_lock_against_competitors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    pid_path = tmp_path / "daemon.pid"
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: False)
    first = health.SingleInstanceLease(pid_path)
    second = health.SingleInstanceLease(pid_path)
    first.acquire()

    with pytest.raises(health.DaemonAlreadyRunning):
        second.acquire()

    first.release()
    second.acquire()
    second.release()


def test_lease_release_never_deletes_another_owner_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.runtime import research_daemon_health as health

    pid_path = tmp_path / "daemon.pid"
    monkeypatch.setattr(health, "_process_matches_daemon", lambda pid: False)
    lease = health.SingleInstanceLease(pid_path)
    lease.acquire()
    pid_path.write_text(
        json.dumps({"pid": 987, "owner_token": "replacement-owner"}),
        encoding="utf-8",
    )

    lease.release()

    assert pid_path.exists()
    assert json.loads(pid_path.read_text(encoding="utf-8"))["pid"] == 987


def test_task_launcher_keeps_python_attached_to_task_scheduler() -> None:
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "start"
        / "run_research_daemon.ps1"
    ).read_text(encoding="utf-8")

    assert "Start-Process" not in launcher
    assert "& $Python @Arguments" in launcher


def test_daemon_state_persists_runtime_heartbeat(tmp_path: Path) -> None:
    daemon_module = _load_daemon_module()
    state_path = tmp_path / "daemon.json"
    state = daemon_module.DaemonState(state_path)

    state.mark_runtime(
        pid=321,
        status="running",
        tick_interval_seconds=300,
        last_tick_status="ok",
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))["runtime"]
    assert persisted["pid"] == 321
    assert persisted["status"] == "running"
    assert persisted["tick_interval_seconds"] == 300
    assert persisted["last_tick_status"] == "ok"
    assert datetime.fromisoformat(persisted["heartbeat_at"]).tzinfo is not None


def test_daemon_enables_production_signal_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    daemon_module = _load_daemon_module()
    received: dict = {}

    class _Monitor:
        def __init__(self, **kwargs) -> None:
            received.update(kwargs)

    monkeypatch.setattr(daemon_module, "ResearchMonitor", _Monitor)
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "daemon.json",
    )

    assert received["persist_quant_signals"] is True
    daemon.shutdown()


def test_daemon_lifecycle_owns_pid_and_records_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    daemon_module = _load_daemon_module()
    state_path = tmp_path / "daemon.json"
    pid_path = tmp_path / "daemon.pid"
    monkeypatch.setenv("AQUATRADE_DAEMON_ONCE", "1")
    monkeypatch.setenv("AQUATRADE_DAEMON_PID_FILE", str(pid_path))

    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=30,
        state_file=state_path,
    )
    observed: dict = {}

    def _tick() -> None:
        observed["pid"] = json.loads(
            pid_path.read_text(encoding="utf-8")
        )["pid"]
        observed["runtime"] = json.loads(
            state_path.read_text(encoding="utf-8")
        )["runtime"]

    daemon._tick = _tick

    assert daemon.run_forever() == 0
    assert observed["pid"] == os.getpid()
    assert observed["runtime"]["status"] == "running"
    assert observed["runtime"]["pid"] == os.getpid()
    assert not pid_path.exists()
    final_runtime = json.loads(state_path.read_text(encoding="utf-8"))["runtime"]
    assert final_runtime["status"] == "stopped"


def test_daemon_maintains_consecutive_tick_failure_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    daemon_module = _load_daemon_module()
    state_path = tmp_path / "daemon.json"
    monkeypatch.setenv("AQUATRADE_DAEMON_ONCE", "1")
    monkeypatch.setenv(
        "AQUATRADE_DAEMON_PID_FILE",
        str(tmp_path / "daemon.pid"),
    )
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=30,
        state_file=state_path,
    )

    def _tick() -> None:
        raise RuntimeError("tick failed")

    daemon._tick = _tick

    assert daemon.run_forever() == 0
    runtime = json.loads(state_path.read_text(encoding="utf-8"))["runtime"]
    assert runtime["last_tick_status"] == "error"
    assert runtime["consecutive_failures"] == 1


def test_status_endpoint_uses_combined_process_and_heartbeat_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from flask import Flask
    from core.runtime import research_daemon_health as health
    from server.routes.system_routes import system_bp

    monkeypatch.setenv("AQUATRADE_DAEMON_STATE_FILE", str(tmp_path / "daemon.json"))
    monkeypatch.setattr(
        health,
        "inspect_daemon_health",
        lambda *args, **kwargs: {
            "daemon_running": True,
            "daemon_pid": 321,
            "health_reason": "healthy",
            "heartbeat_at": "2026-07-23T10:00:00+08:00",
        },
    )
    app = Flask(__name__)
    app.register_blueprint(system_bp)

    response = app.test_client().get("/api/research-daemon/status")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["daemon_running"] is True
    assert data["health_reason"] == "healthy"


def test_cycle_health_exposes_due_learning_and_stalled_forward_progress(
    tmp_path: Path,
) -> None:
    import sqlite3
    from core.runtime import research_daemon_health as health

    state_path = tmp_path / "daemon.json"
    state_path.write_text(
        json.dumps(
            {
                "daily": {
                    "2026-07-28": {
                        "premarket_done": "2026-07-28T07:45:00+08:00",
                        "close_done": "2026-07-28T15:05:00+08:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "research.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE learning_predictions ("
            "prediction_id TEXT PRIMARY KEY, as_of TEXT, entity_id TEXT, "
            "payload_json TEXT, recorded_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE learning_observations ("
            "observation_id TEXT PRIMARY KEY, prediction_id TEXT, "
            "observed_at TEXT, payload_json TEXT, recorded_at TEXT)"
        )
        connection.execute(
            "INSERT INTO learning_predictions VALUES (?,?,?,?,?)",
            (
                "p1",
                "2026-07-26T23:45:00+00:00",
                "600519.SH",
                json.dumps(
                    {
                        "as_of": "2026-07-27T07:45:00+08:00",
                        "due_at": "2026-07-28T16:00:00+08:00",
                    }
                ),
                "2026-07-26T23:45:01+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO learning_predictions VALUES (?,?,?,?,?)",
            (
                "p1-rerun",
                "2026-07-27T08:00:00+08:00",
                "600519.SH",
                json.dumps({"due_at": "2026-07-28T16:00:00+08:00"}),
                "2026-07-27T08:00:01+08:00",
            ),
        )
    cohort_path = tmp_path / "observations.json"
    cohort_path.write_text(
        json.dumps([{"trade_date": "2026-07-24"}]),
        encoding="utf-8",
    )

    result = health.inspect_research_cycle_health(
        state_path=state_path,
        research_db_path=db_path,
        cohort_observations_path=cohort_path,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
    )

    assert result["learning"]["due_unsettled"] == 2
    assert result["learning"]["duplicate_cohorts"] == 1
    assert result["learning"]["outcomes_total"] == 0
    assert result["forward_progress"]["collected_trade_days"] == 1
    assert result["forward_progress"]["required_trade_days"] == 20
    assert "due_learning_unsettled" in result["issues"]
    assert "forward_progress_stalled" in result["issues"]
    assert "close_postprocess_incomplete" in result["issues"]


def test_cycle_health_counts_daily_predictions_and_return_feedback(
    tmp_path: Path,
) -> None:
    import sqlite3
    from core.runtime import research_daemon_health as health

    state_path = tmp_path / "daemon.json"
    state_path.write_text(
        json.dumps(
            {
                "daily": {
                    "2026-07-28": {
                        "close_done": "2026-07-28T15:05:00+08:00",
                        "p3_forward_done": "2026-07-28T15:06:00+08:00",
                        "daily_research_build_done": "2026-07-28T15:07:00+08:00",
                        "learning_settlement_done": "2026-07-28T15:08:00+08:00",
                        "close_postprocess_done": "2026-07-28T15:08:01+08:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "research.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE learning_predictions ("
            "prediction_id TEXT PRIMARY KEY, as_of TEXT, entity_id TEXT, "
            "payload_json TEXT, recorded_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE learning_observations ("
            "observation_id TEXT PRIMARY KEY, prediction_id TEXT, "
            "observed_at TEXT, payload_json TEXT, recorded_at TEXT)"
        )
        connection.execute(
            "INSERT INTO learning_predictions VALUES (?,?,?,?,?)",
            (
                "p1",
                "2026-07-26T23:45:00+00:00",
                "600519.SH",
                json.dumps(
                    {
                        "as_of": "2026-07-27T07:45:00+08:00",
                        "due_at": "2026-07-28T16:00:00+08:00",
                    }
                ),
                "2026-07-26T23:45:01+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO learning_observations VALUES (?,?,?,?,?)",
            (
                "o1",
                "p1",
                "2026-07-28T16:05:00+08:00",
                json.dumps(
                    {
                        "actual_value": 0.0123,
                        "was_correct": True,
                        "attribution": "MODEL",
                        "outcome_context": {
                            "trade_date": "2026-07-28",
                            "stock_return": 0.0143,
                            "benchmark_return": 0.002,
                            "excess_return": 0.0123,
                        },
                    }
                ),
                "2026-07-28T16:05:01+08:00",
            ),
        )
    cohort_path = tmp_path / "observations.json"
    cohort_path.write_text(
        json.dumps(
            [
                {"trade_date": "2026-07-24"},
                {"trade_date": "2026-07-28"},
            ]
        ),
        encoding="utf-8",
    )

    result = health.inspect_research_cycle_health(
        state_path=state_path,
        research_db_path=db_path,
        cohort_observations_path=cohort_path,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
    )

    assert result["learning"]["predictions_by_day"] == {"2026-07-27": 1}
    assert result["learning"]["outcomes_by_day"] == {"2026-07-28": 1}
    assert result["learning"]["due_unsettled"] == 0
    assert result["learning"]["latest_return_feedback"] == {
        "actual_value": 0.0123,
        "attribution": "MODEL",
        "observed_at": "2026-07-28T16:05:00+08:00",
        "prediction_id": "p1",
        "was_correct": True,
        "trade_date": "2026-07-28",
        "stock_return": 0.0143,
        "benchmark_return": 0.002,
        "excess_return": 0.0123,
    }
    assert "due_learning_unsettled" not in result["issues"]
    assert "forward_progress_stalled" not in result["issues"]


def test_cycle_health_reports_missed_daily_phases_after_deadline(
    tmp_path: Path,
) -> None:
    import sqlite3
    from core.runtime import research_daemon_health as health

    state_path = tmp_path / "daemon.json"
    state_path.write_text(
        json.dumps(
            {
                "daily": {
                    "2026-07-28": {
                        "premarket_last_attempt": {
                            "status": "failed",
                            "reason": "daily stock pool empty",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "research.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE learning_predictions ("
            "prediction_id TEXT, as_of TEXT, entity_id TEXT, "
            "payload_json TEXT, recorded_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE learning_observations ("
            "observation_id TEXT, prediction_id TEXT, observed_at TEXT, "
            "payload_json TEXT, recorded_at TEXT)"
        )
    cohort_path = tmp_path / "observations.json"
    cohort_path.write_text("[]", encoding="utf-8")

    result = health.inspect_research_cycle_health(
        state_path=state_path,
        research_db_path=db_path,
        cohort_observations_path=cohort_path,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
    )

    assert "premarket_incomplete" in result["issues"]
    assert "tail_research_incomplete" in result["issues"]
    assert "close_incomplete" in result["issues"]
    assert result["cycle"]["premarket_last_attempt"]["reason"] == (
        "daily stock pool empty"
    )
