from __future__ import annotations

import json
from datetime import datetime

from core.research.control_plane.contracts import CalibrationRun
from core.research.control_plane.learning_loop import ProductionLearningLoop
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.test_run_record import TestRunStore
from core.runtime.research_monitor import ResearchMonitor
from core.quant.theme.source_router import ConceptBoard, SourceSnapshot
from scripts import run_research_monitor_daemon as daemon


def test_proxy_bypass_covers_market_and_notification_hosts():
    value = daemon._merge_no_proxy_hosts("localhost,127.0.0.1")

    hosts = {item.strip() for item in value.split(",") if item.strip()}
    assert {
        "localhost",
        "127.0.0.1",
        "api.waditu.com",
        "www.pushplus.plus",
        "open.feishu.cn",
    } <= hosts


def test_snapshot_age_counts_trading_days_not_weekend(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_is_trading_day",
        lambda value: value.weekday() < 5,
    )

    age = daemon._trading_day_age(
        datetime(2026, 8, 14),
        datetime(2026, 8, 17),
    )

    assert age == 1


def test_premarket_preflight_reuses_theme_router(monkeypatch):
    instances = []

    class FakeRouter:
        def __init__(self, **_kwargs):
            instances.append(self)

        def fetch_concept_snapshot(self, trade_date):
            return SourceSnapshot(
                snapshot_id="snap_live",
                trade_date=trade_date,
                source_as_of=trade_date,
                boards=[
                    ConceptBoard(
                        board_name=f"主题{index}",
                        members=[f"{index:02d}{member:04d}" for member in range(10)],
                    )
                    for index in range(10)
                ],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        FakeRouter,
    )
    runtime = object.__new__(daemon.ResearchMonitorDaemon)
    runtime._theme_source_router = None

    assert runtime._preflight_premarket_sources("20260903")["ok"] is True
    assert runtime._preflight_premarket_sources("20260903")["ok"] is True
    assert len(instances) == 1


def test_feishu_counts_as_required_external_delivery(tmp_path, monkeypatch):
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    report_path = output_dir / "report.md"
    report_path.write_text("report", encoding="utf-8")
    (output_dir / "latest_success.json").write_text(
        json.dumps({"trade_date": "2026-08-17", "run_id": "run-1"}),
        encoding="utf-8",
    )

    captured = {}

    class FakeNotificationService:
        def notify(self, payload):
            captured["required"] = payload.data["delivery_required_any"]
            return "feishu" in captured["required"]

    monkeypatch.setattr(
        "core.notifications.notification_service.get_notification_service_core",
        lambda: FakeNotificationService(),
    )

    class FakeDedupStore:
        def should_push(self, **kwargs):
            return True

        def record_push(self, **kwargs):
            return None

        def record_failure(self, **kwargs):
            return None

    monitor = ResearchMonitor(
        output_dir=output_dir,
        push_dedup_store=FakeDedupStore(),
    )
    monkeypatch.setattr(monitor, "_push_market_freshness_error", lambda payload: {})

    result = monitor._maybe_push(
        external_push=True,
        trade_date="2026-08-17",
        title="test",
        summary="test",
        report_path=report_path,
        payload={"mode": "close", "run_id": "run-1", "status": "success"},
    )

    assert result["sent"] is True
    assert captured["required"] == ["openclaw", "pushplus", "feishu"]


def test_learning_status_is_production_but_keeps_activation_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LEARNING_MODE", "production")
    state = daemon.DaemonState(tmp_path / "daemon.json")

    state.mark_learning_status("settlement", {"status": "ok", "settled": 0})

    learning = state.snapshot()["learning_status"]
    assert learning["mode"] == "production"
    assert learning["activation_allowed"] is True
    assert learning["settlement"]["settled"] == 0


def test_production_learning_refuses_unattested_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LEARNING_MODE", "production")
    repository = ResearchRepository(tmp_path / "research.db")
    run = CalibrationRun(
        domain="alpha",
        status="shadow",
        sample_size=100,
        metrics={"walk_forward_score": 0.70, "false_promotion_rate": 0.05},
        parameters={"alpha_weight_multiplier": 1.04},
        bounds={"alpha_weight_multiplier": {"min": 0.8, "max": 1.2}},
    )
    repository.save_calibration(run)

    result = daemon._promote_learning_calibration(
        ProductionLearningLoop(repository),
        run.calibration_id,
        code_version="abc123",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "passing_test_attestation_missing"
    assert repository.get_active_policy("alpha") is None


def test_production_learning_activates_governed_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LEARNING_MODE", "production")
    repository = ResearchRepository(tmp_path / "research.db")
    run = CalibrationRun(
        domain="alpha",
        status="shadow",
        sample_size=100,
        metrics={"walk_forward_score": 0.70, "false_promotion_rate": 0.05},
        parameters={"alpha_weight_multiplier": 1.04},
        bounds={"alpha_weight_multiplier": {"min": 0.8, "max": 1.2}},
    )
    repository.save_calibration(run)
    test_record = TestRunStore(repository).record_run(
        code_version="abc123",
        test_suite="tests/test_feedback_loop_integration.py",
        passed=True,
        metrics={"passed": 1, "failed": 0},
    )

    result = daemon._promote_learning_calibration(
        ProductionLearningLoop(repository),
        run.calibration_id,
        code_version="abc123",
    )

    assert result["status"] == "active"
    assert result["test_run_id"] == test_record.test_run_id
    policy = repository.get_active_policy("alpha")
    assert policy is not None
    assert policy.parameters["alpha_weight_multiplier"] == 1.04
