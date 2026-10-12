from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_research_runtime_health.py"
    )
    spec = importlib.util.spec_from_file_location(
        "research_runtime_health_cli_test_target",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_health_snapshot_combines_daemon_and_cycle_issues(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "inspect_daemon_health",
        lambda *args, **kwargs: {
            "daemon_running": False,
            "health_reason": "stale_heartbeat",
        },
    )
    monkeypatch.setattr(
        module,
        "inspect_research_cycle_health",
        lambda **kwargs: {
            "status": "degraded",
            "issues": ["due_learning_unsettled"],
        },
    )

    snapshot = module.build_health_snapshot(
        state_path=tmp_path / "state.json",
        pid_path=tmp_path / "state.pid",
        research_db_path=tmp_path / "research.db",
        cohort_observations_path=tmp_path / "observations.json",
        now=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc),
    )

    assert snapshot["status"] == "degraded"
    assert snapshot["issues"] == [
        "daemon_stale_heartbeat",
        "due_learning_unsettled",
    ]


def test_health_alert_is_deduplicated_during_cooldown(
    tmp_path: Path,
) -> None:
    module = _load_module()
    now = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    issues = ["due_learning_unsettled"]
    fingerprint = __import__("hashlib").sha256(
        json.dumps(issues, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    alert_state = tmp_path / "alert.json"
    alert_state.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "attempted_at": (now - timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    sent = module.notify_health_transition(
        {"issues": issues},
        alert_state_path=alert_state,
        now=now,
    )

    assert sent is False
    persisted = json.loads(alert_state.read_text(encoding="utf-8"))
    assert persisted["attempted_at"] == (
        now - timedelta(minutes=5)
    ).isoformat()


def test_same_health_issue_is_not_resent_after_cooldown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    now = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    issues = ["premarket_incomplete"]
    fingerprint = __import__("hashlib").sha256(
        json.dumps(issues, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    alert_state = tmp_path / "alert.json"
    attempted_at = now - timedelta(hours=2)
    alert_state.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "status": "delivery_failed",
                "attempted_at": attempted_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    service_module = __import__(
        "core.notifications.notification_service",
        fromlist=["get_notification_service_core"],
    )

    class _UnexpectedService:
        def notify(self, payload):
            raise AssertionError("unchanged issue must not be resent")

    monkeypatch.setattr(
        service_module,
        "get_notification_service_core",
        lambda: _UnexpectedService(),
    )

    sent = module.notify_health_transition(
        {"issues": issues},
        alert_state_path=alert_state,
        now=now,
    )

    assert sent is False
    persisted = json.loads(alert_state.read_text(encoding="utf-8"))
    assert persisted["attempted_at"] == attempted_at.isoformat()


def test_daemon_only_notification_excludes_cycle_issues() -> None:
    module = _load_module()
    snapshot = {
        "issues": ["premarket_incomplete"],
        "daemon": {
            "daemon_running": True,
            "health_reason": "healthy",
        },
    }

    selected = module.select_notification_snapshot(
        snapshot,
        daemon_only=True,
    )

    assert selected["issues"] == []
    assert selected["status"] == "healthy"


def test_healthy_transition_clears_previous_alert(
    tmp_path: Path,
) -> None:
    module = _load_module()
    now = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    alert_state = tmp_path / "alert.json"
    alert_state.write_text(
        json.dumps({"status": "delivery_failed"}),
        encoding="utf-8",
    )

    sent = module.notify_health_transition(
        {"issues": []},
        alert_state_path=alert_state,
        now=now,
    )

    assert sent is False
    persisted = json.loads(alert_state.read_text(encoding="utf-8"))
    assert persisted["status"] == "healthy"


def test_windows_task_installer_prevents_battery_shutdown_and_retires_split_jobs() -> None:
    root = Path(__file__).resolve().parents[1]
    installer = (
        root / "scripts" / "admin" / "install_research_monitor_tasks.ps1"
    ).read_text(encoding="utf-8")

    assert "-AllowStartIfOnBatteries" in installer
    assert "-DontStopIfGoingOnBatteries" in installer
    assert "AquaTrade_ResearchMonitor_Daemon" in installer
    assert "AquaTrade_ResearchMonitor_Watchdog" in installer
    assert "AquaTrade_HBM_DailyPaperTrading" in installer
    assert "AquaTrade_HBM_DailyCloseNAV" in installer
    assert "Disable-ScheduledTask" in installer


def test_watchdog_uses_health_exit_code_and_restarts_only_unhealthy_daemon() -> None:
    watchdog = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "start"
        / "ensure_research_daemon.ps1"
    ).read_text(encoding="utf-8")

    assert "check_research_runtime_health.py" in watchdog
    assert "--daemon-only" in watchdog
    assert "--notify" in watchdog
    assert "if ($HealthExit -eq 2)" in watchdog
    assert "Stop-ScheduledTask" in watchdog
    assert "research_monitor_daemon.json" in watchdog
    assert "Get-CimInstance Win32_Process" in watchdog
    assert "run_research_monitor_daemon.py" in watchdog
    assert "Stop-Process" in watchdog
    assert "Start-ScheduledTask" in watchdog


def test_daemon_launcher_redirects_native_logs_without_error_wrapping() -> None:
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "start"
        / "run_research_daemon.ps1"
    ).read_text(encoding="utf-8")

    assert "Start-Process" in launcher
    assert "-RedirectStandardOutput" in launcher
    assert "-RedirectStandardError" in launcher
    assert "-WindowStyle Hidden" in launcher
    assert "1>>" not in launcher
    assert "2>>" not in launcher
