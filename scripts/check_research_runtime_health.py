#!/usr/bin/env python
"""Persist and optionally notify on Research runtime closure health."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime.research_daemon_health import (
    inspect_daemon_health,
    inspect_research_cycle_health,
)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_health_snapshot(
    *,
    state_path: Path,
    pid_path: Path,
    research_db_path: Path,
    cohort_observations_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    daemon = inspect_daemon_health(
        state_path,
        pid_path=pid_path,
        now=now,
    )
    cycle = inspect_research_cycle_health(
        state_path=state_path,
        research_db_path=research_db_path,
        cohort_observations_path=cohort_observations_path,
        now=now,
    )
    issues = list(cycle["issues"])
    if not daemon.get("daemon_running"):
        issues.insert(0, f"daemon_{daemon.get('health_reason')}")
    return {
        "as_of": now.isoformat(),
        "status": "healthy" if not issues else "degraded",
        "issues": issues,
        "daemon": daemon,
        "cycle": cycle,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def notify_health_transition(
    snapshot: dict[str, Any],
    *,
    alert_state_path: Path,
    now: datetime | None = None,
) -> bool:
    """Notify only when the issue fingerprint changes."""
    now = now or datetime.now(timezone.utc)
    issues = sorted(str(item) for item in snapshot.get("issues") or [])
    fingerprint = hashlib.sha256(
        json.dumps(issues, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    previous = _read_json(alert_state_path)

    if not issues:
        _atomic_write(
            alert_state_path,
            {
                "fingerprint": fingerprint,
                "status": "healthy",
                "attempted_at": now.isoformat(),
            },
        )
        return False
    if previous.get("fingerprint") == fingerprint:
        return False

    from core.notifications.models import NotificationPayload
    from core.notifications.notification_service import (
        get_notification_service_core,
    )

    summary = "；".join(issues)
    sent = get_notification_service_core().notify(
        NotificationPayload(
            event_type="system_error",
            title="AquaTrade 研究闭环异常",
            summary=summary,
            priority="high",
            data={
                "delivery_required_any": ["openclaw", "pushplus"],
                "health_issues": issues,
            },
        )
    )
    _atomic_write(
        alert_state_path,
        {
            "fingerprint": fingerprint,
            "status": "sent" if sent else "delivery_failed",
            "attempted_at": now.isoformat(),
        },
    )
    return bool(sent)


def select_notification_snapshot(
    snapshot: dict[str, Any],
    *,
    daemon_only: bool,
) -> dict[str, Any]:
    """Restrict watchdog notifications to the scope it owns."""
    if not daemon_only:
        return snapshot
    daemon = snapshot.get("daemon") or {}
    daemon_running = bool(daemon.get("daemon_running"))
    issues = []
    if not daemon_running:
        issues = [
            f"daemon_{daemon.get('health_reason') or 'not_running'}"
        ]
    return {
        **snapshot,
        "status": "healthy" if not issues else "degraded",
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-path",
        default="data/state/research_monitor_daemon.json",
    )
    parser.add_argument(
        "--pid-path",
        default="data/state/research_monitor_daemon.pid",
    )
    parser.add_argument(
        "--research-db-path",
        default="data/research_control/research_control.db",
    )
    parser.add_argument(
        "--cohort-observations-path",
        default="data/research_control/p3_forward_v6/observations.json",
    )
    parser.add_argument(
        "--output-path",
        default="data/state/research_runtime_health.json",
    )
    parser.add_argument(
        "--alert-state-path",
        default="data/state/research_runtime_health_alert.json",
    )
    parser.add_argument("--daemon-only", action="store_true")
    parser.add_argument("--notify", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    snapshot = build_health_snapshot(
        state_path=Path(args.state_path),
        pid_path=Path(args.pid_path),
        research_db_path=Path(args.research_db_path),
        cohort_observations_path=Path(args.cohort_observations_path),
    )
    _atomic_write(Path(args.output_path), snapshot)
    if args.notify:
        notify_health_transition(
            select_notification_snapshot(
                snapshot,
                daemon_only=args.daemon_only,
            ),
            alert_state_path=Path(args.alert_state_path),
        )
    print(json.dumps(snapshot, ensure_ascii=False))
    if not snapshot["daemon"].get("daemon_running"):
        return 2
    if not args.daemon_only and snapshot["issues"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
