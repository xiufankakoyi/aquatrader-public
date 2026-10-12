"""Smoke tests for scripts/run_research_monitor_daemon.py.

验证 daemon 的核心调度逻辑：
- 时段判定 _in_window
- DaemonState 持久化
- DailyDataFreshnessChecker 日线新鲜度检查（mock 外部依赖）
- QueuedTaskConsumer 并发消费（mock repository + runtime）
- ResearchMonitorDaemon 单 tick 调度（mock ResearchMonitor.run）

不连接真实 Tushare / LanceDB / ResearchRepository。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 通过 importlib 加载 scripts/run_research_monitor_daemon.py（非包模块）
_DAEMON_PATH = PROJECT_ROOT / "scripts" / "run_research_monitor_daemon.py"
_spec = importlib.util.spec_from_file_location("run_research_monitor_daemon", _DAEMON_PATH)
assert _spec is not None and _spec.loader is not None
daemon_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon_module)

MARKET_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------- #
# 时段判定
# ---------------------------------------------------------------------- #

def test_in_window_basic() -> None:
    """_in_window 应正确判定时间是否落在窗口内。"""
    assert daemon_module._in_window(
        datetime(2026, 7, 15, 8, 0, tzinfo=MARKET_TZ),
        daemon_module.PREMARKET_WINDOW,
    )
    assert not daemon_module._in_window(
        datetime(2026, 7, 15, 10, 0, tzinfo=MARKET_TZ),
        daemon_module.PREMARKET_WINDOW,
    )
    assert daemon_module._in_window(
        datetime(2026, 7, 15, 10, 0, tzinfo=MARKET_TZ),
        daemon_module.MONITOR_AM_WINDOW,
    )
    assert daemon_module._in_window(
        datetime(2026, 7, 15, 14, 30, tzinfo=MARKET_TZ),
        daemon_module.MONITOR_PM_WINDOW,
    )
    assert not daemon_module._in_window(
        datetime(2026, 7, 15, 12, 0, tzinfo=MARKET_TZ),  # 午休
        daemon_module.MONITOR_AM_WINDOW,
    )


def test_is_weekend() -> None:
    """_is_weekend 应正确识别周末。"""
    assert daemon_module._is_weekend(datetime(2026, 7, 18))  # 周六
    assert daemon_module._is_weekend(datetime(2026, 7, 19))  # 周日
    assert not daemon_module._is_weekend(datetime(2026, 7, 15))  # 周三


# ---------------------------------------------------------------------- #
# DaemonState 持久化
# ---------------------------------------------------------------------- #

def test_daemon_state_mark_and_query(tmp_path: Path) -> None:
    """DaemonState 应正确标记和查询当日 mode 完成状态。"""
    state = daemon_module.DaemonState(tmp_path / "state.json")
    assert not state.is_done("2026-07-15", "premarket")

    state.mark_done("2026-07-15", "premarket")
    assert state.is_done("2026-07-15", "premarket")
    assert not state.is_done("2026-07-15", "close")
    assert not state.is_done("2026-07-16", "premarket")

    # 重启后状态应持久化
    state_reloaded = daemon_module.DaemonState(tmp_path / "state.json")
    assert state_reloaded.is_done("2026-07-15", "premarket")


def test_daemon_state_last_monitor_at(tmp_path: Path) -> None:
    """DaemonState 应记录 monitor 最近触发时间。"""
    state = daemon_module.DaemonState(tmp_path / "state.json")
    assert state.last_monitor_at("2026-07-15") is None

    state.mark_done("2026-07-15", "monitor")
    last = state.last_monitor_at("2026-07-15")
    assert last is not None
    # 时间戳应为 mark_done 调用时刻，年份必为当前年
    assert last.year == datetime.now(MARKET_TZ).year


# ---------------------------------------------------------------------- #
# DailyDataFreshnessChecker
# ---------------------------------------------------------------------- #

class _StubQuery:
    """UnifiedDataQuery 替身，返回固定 latest_date。"""

    def __init__(self, latest_date: str | None) -> None:
        self._latest = latest_date

    def get_db_stats(self) -> dict[str, Any]:
        return {"latest_date": self._latest} if self._latest else {}


class _StubUpdater:
    """UnifiedDataUpdater 替身，记录调用次数。"""

    def __init__(self, *, result: Any = None) -> None:
        self.full_update_calls = 0
        self.result = result

    def run_full_update(self, *, skip_dragon: bool = True) -> Any:
        self.full_update_calls += 1
        return self.result


def test_freshness_checker_skips_recent_check(monkeypatch) -> None:
    """非 force 模式下，30 分钟内的二次检查应被跳过。"""
    today = datetime.now(MARKET_TZ).date().isoformat()
    checker = daemon_module.DailyDataFreshnessChecker()
    checker._ensure_handles = lambda: None  # type: ignore[method-assign]
    checker._query = _StubQuery(today)  # type: ignore[attr-defined]  # 已是最新
    checker._updater = _StubUpdater()  # type: ignore[attr-defined]

    # 第一次检查（force=True，数据已是最新）
    result1 = checker.ensure_fresh(force=True)
    assert result1.get("latest_date") == today
    assert result1.get("updated") is False
    assert result1.get("reason") == "already_latest"

    # 第二次检查（非 force，应被跳过）
    result2 = checker.ensure_fresh(force=False)
    assert result2.get("skipped") is True


def test_freshness_checker_triggers_update_when_stale(monkeypatch) -> None:
    """数据落后且为交易日时应触发增量更新。"""
    checker = daemon_module.DailyDataFreshnessChecker()
    # 强制把今天设为 2026-07-16（周四），latest_date=2026-07-14（落后两天）
    monkeypatch.setattr(
        daemon_module, "_is_trading_day", lambda date: True
    )
    monkeypatch.setattr(
        daemon_module.DailyDataFreshnessChecker, "latest_date",
        lambda self: "2026-07-14",
    )

    stub_updater = _StubUpdater()
    checker._query = _StubQuery("2026-07-14")  # type: ignore[attr-defined]
    checker._updater = stub_updater  # type: ignore[attr-defined]
    checker._ensure_handles = lambda: None  # type: ignore[method-assign]
    # 强制 now 为 2026-07-16
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 9, 0, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )

    result = checker.ensure_fresh(force=True)
    assert result.get("updated") is True
    assert result.get("mode") == "incremental"
    assert stub_updater.full_update_calls == 1


def test_freshness_checker_accepts_previous_close_before_market(monkeypatch) -> None:
    """盘前最新日线应是上一交易日，不应误抓尚未收盘的今日数据。"""
    checker = daemon_module.DailyDataFreshnessChecker()
    checker._query = _StubQuery("2026-07-15")  # type: ignore[attr-defined]
    checker._updater = _StubUpdater()  # type: ignore[attr-defined]
    checker._ensure_handles = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 8, 0, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )

    result = checker.ensure_fresh(force=True)

    assert result["reason"] == "already_latest"
    assert result["expected_date"] == "2026-07-15"
    assert checker._updater.full_update_calls == 0


def test_freshness_checker_reports_updater_failure(monkeypatch) -> None:
    """Updater 返回 success=False 时不能伪报 updated=True。"""
    checker = daemon_module.DailyDataFreshnessChecker()
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)
    monkeypatch.setattr(
        daemon_module.DailyDataFreshnessChecker, "latest_date",
        lambda self: "2026-07-14",
    )
    checker._query = _StubQuery("2026-07-14")  # type: ignore[attr-defined]
    checker._updater = _StubUpdater(
        result=SimpleNamespace(success=False, message="daily provider failed")
    )  # type: ignore[attr-defined]
    checker._ensure_handles = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 9, 0, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )

    result = checker.ensure_fresh(force=True)

    assert result["updated"] is False
    assert result["error"] == "daily provider failed"


# ---------------------------------------------------------------------- #
# QueuedTaskConsumer
# ---------------------------------------------------------------------- #

class _FakeOrchestrator:
    """ResearchOrchestrator 替身，记录 execute 调用。"""

    def __init__(
        self,
        *,
        fail_task_ids: set[str] | None = None,
        success_status: str = "success",
    ) -> None:
        self._fail_task_ids = fail_task_ids or set()
        self._success_status = success_status
        self.executed: list[str] = []
        self._lock = __import__("threading").Lock()

    def execute(self, task: Any) -> Any:
        with self._lock:
            self.executed.append(task.task_id)
        if task.task_id in self._fail_task_ids:
            return SimpleNamespace(status="failed", errors=[{"message": "mock failure"}])
        return SimpleNamespace(status=self._success_status, errors=[])


class _FakeRepo:
    """ResearchRepository 替身。"""

    def __init__(self, tasks: list[Any]) -> None:
        self._tasks = tasks
        self.requested_statuses: list[str] = []

    def list_tasks(self, status: str = "", limit: int = 100) -> list[Any]:
        self.requested_statuses.append(status)
        return [task for task in self._tasks if not status or task.status == status]

    def save_task(self, task: Any) -> None:
        return None


def _make_task(task_id: str, *, workflow: str = "dynamic_theme_review", status: str = "queued") -> Any:
    """构造测试用 ResearchTask 替身。"""
    return SimpleNamespace(
        task_id=task_id,
        status=status,
        params={"workflow": workflow},
        priority=0.5,
        target_claim_id=f"claim_{task_id}",
    )


def test_queued_consumer_drains_batch() -> None:
    """queued 任务消费器应并发执行 batch_size 个任务。"""
    tasks = [_make_task(f"t{i}") for i in range(20)]
    fake_repo = _FakeRepo(tasks)
    fake_orchestrator = _FakeOrchestrator()

    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=fake_orchestrator),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=4, batch_size=8)
    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")

    assert result["fetched"] == 20
    assert result["batch"] == 8
    assert result["succeeded"] == 8
    assert result["failed"] == 0
    assert result["skipped"] == 12
    assert len(fake_orchestrator.executed) == 8
    assert "queued" in fake_repo.requested_statuses
    consumer.shutdown()


def test_queued_consumer_records_failures() -> None:
    """消费器应正确分类成功与失败。"""
    tasks = [_make_task(f"t{i}") for i in range(5)]
    fake_repo = _FakeRepo(tasks)
    fake_orchestrator = _FakeOrchestrator(fail_task_ids={"t1", "t3"})

    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=fake_orchestrator),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=2, batch_size=5)
    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")

    assert result["succeeded"] == 3
    assert result["failed"] == 2
    assert len(result["errors"]) >= 1
    consumer.shutdown()


def test_queued_consumer_resumes_retrying_task_before_execution() -> None:
    task = _make_task("retry-1", status="retrying")
    fake_repo = _FakeRepo([task])
    resumed: list[str] = []

    class _Resumer:
        def resume_task(self, item: Any) -> Any:
            resumed.append(item.task_id)
            item.status = "queued"
            return item

    fake_orchestrator = _FakeOrchestrator()
    fake_orchestrator.task_resumer = _Resumer()
    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(
            orchestrator=fake_orchestrator
        ),
    )
    consumer = daemon_module.QueuedTaskConsumer(
        monitor,
        max_workers=1,
        batch_size=1,
    )

    result = consumer.drain_once(
        as_of="2026-07-29T00:10:00+08:00"
    )

    assert resumed == ["retry-1"]
    assert fake_orchestrator.executed == ["retry-1"]
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    consumer.shutdown()


def test_queued_consumer_failure_without_message_is_still_observable() -> None:
    task = _make_task("silent-failure")
    fake_repo = _FakeRepo([task])

    class _SilentFailureOrchestrator:
        task_resumer = None

        def execute(self, item: Any) -> Any:
            return SimpleNamespace(
                status="retrying",
                errors=[],
                stopped_reason_code="task_already_running",
            )

    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(
            orchestrator=_SilentFailureOrchestrator()
        ),
    )
    consumer = daemon_module.QueuedTaskConsumer(
        monitor,
        max_workers=1,
        batch_size=1,
    )

    result = consumer.drain_once(
        as_of="2026-07-29T00:10:00+08:00"
    )

    assert result["failed"] == 1
    assert result["errors"] == [
        "task=silent-failure status=retrying reason=task_already_running"
    ]
    consumer.shutdown()


def test_queued_consumer_accepts_persisted_succeeded_status() -> None:
    fake_repo = _FakeRepo([_make_task("t1")])
    fake_orchestrator = _FakeOrchestrator(success_status="succeeded")
    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=fake_orchestrator),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=1, batch_size=1)

    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")

    assert result["succeeded"] == 1
    assert result["failed"] == 0
    consumer.shutdown()


def test_queued_consumer_counts_partial_as_completed_not_failed() -> None:
    fake_repo = _FakeRepo([_make_task("partial-1")])
    fake_orchestrator = _FakeOrchestrator(success_status="partial")
    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(
            orchestrator=fake_orchestrator
        ),
    )
    consumer = daemon_module.QueuedTaskConsumer(
        monitor,
        max_workers=1,
        batch_size=1,
    )

    result = consumer.drain_once(
        as_of="2026-07-29T00:12:00+08:00"
    )

    assert result["succeeded"] == 0
    assert result["partial"] == 1
    assert result["failed"] == 0
    assert result["errors"] == []
    consumer.shutdown()


def test_queued_consumer_skips_non_dynamic_theme_workflow() -> None:
    """workflow != dynamic_theme_review 的任务应被过滤掉。"""
    tasks = [
        _make_task("t1", workflow="other_workflow"),
        _make_task("t2", workflow="dynamic_theme_review"),
        _make_task("t3", status="success"),  # 非 queued 状态
    ]
    fake_repo = _FakeRepo(tasks)
    fake_orchestrator = _FakeOrchestrator()

    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=fake_orchestrator),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=2, batch_size=8)
    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")

    assert result["fetched"] == 1
    assert result["succeeded"] == 1
    consumer.shutdown()


def test_queued_consumer_executes_aquatrade_search_tasks() -> None:
    task = _make_task("search-1", workflow="evidence_completion")
    task.params.update({
        "execution_backend": "AquaTradeResearchEngine",
        "aquatrade_capability": "announcements",
        "symbols": ["600664"],
        "query": "哈药股份 中报预告",
    })
    fake_repo = _FakeRepo([task])
    monitor = SimpleNamespace(
        repository=fake_repo,
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=None),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=2, batch_size=8)
    registry = SimpleNamespace(
        get_announcements=lambda **kwargs: {"results": [{"title": "中报预告"}]}
    )
    consumer._provider_registry = registry

    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")

    assert result["fetched"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert task.status == "succeeded"
    assert task.params["retrieval_result_count"] == 1
    assert task.params["retrieval_results"][0]["title"] == "中报预告"
    consumer.shutdown()


def test_queued_consumer_empty_queue() -> None:
    """空队列时应返回零计数且不报错。"""
    monitor = SimpleNamespace(
        repository=_FakeRepo([]),
        _build_research_runtime=lambda *, as_of: SimpleNamespace(orchestrator=_FakeOrchestrator()),
    )
    consumer = daemon_module.QueuedTaskConsumer(monitor, max_workers=2, batch_size=8)
    result = consumer.drain_once(as_of="2026-07-15T10:00:00+08:00")
    assert result["fetched"] == 0
    assert result["succeeded"] == 0
    consumer.shutdown()


# ---------------------------------------------------------------------- #
# ResearchMonitorDaemon 单 tick 调度
# ---------------------------------------------------------------------- #

class _StubMonitor:
    """ResearchMonitor 替身，记录所有 run 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (mode, as_of)
        self.repository = _FakeRepo([])

    def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
        self.calls.append((mode, as_of))
        return SimpleNamespace(
            mode=mode,
            status="success",
            run_id=f"{mode}_test",
            as_of=as_of,
            errors=[],
            payload={"themes": [], "ready_watch_count": 0, "push": {"sent": True}},
            exit_code=0,
        )

    def _build_research_runtime(self, *, as_of: str) -> Any:
        return SimpleNamespace(orchestrator=_FakeOrchestrator())


def test_daemon_premarket_window_triggers_premarket_and_push(monkeypatch, tmp_path: Path) -> None:
    """盘前时段应触发 premarket + push。"""
    # 模拟盘前 08:00
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 8, 0, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]
    daemon._preflight_premarket_sources = lambda trade_date: {  # type: ignore[method-assign]
        "ok": True,
        "reason": "ok",
    }
    daemon._freshness_checker.ensure_fresh = lambda *, force=True: {"updated": False, "reason": "already_latest"}  # type: ignore[method-assign]

    daemon._tick()

    modes = [call[0] for call in stub_monitor.calls]
    assert "premarket" in modes
    assert "push" in modes
    assert daemon._state.is_done("2026-07-16", "premarket")
    assert daemon._state.is_done("2026-07-16", "push")


def test_daemon_monitor_window_triggers_monitor(monkeypatch, tmp_path: Path) -> None:
    """盘中时段应触发 monitor。"""
    # 模拟盘中 10:30
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 10, 30, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]
    daemon._freshness_checker.ensure_fresh = lambda *, force=False: {"skipped": True}  # type: ignore[method-assign]

    daemon._tick()

    modes = [call[0] for call in stub_monitor.calls]
    assert "monitor" in modes
    assert "premarket" not in modes  # 盘中不触发 premarket


def test_daemon_close_window_triggers_close_once(monkeypatch, tmp_path: Path) -> None:
    """收盘时段应触发 close 且当日只触发一次。"""
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 15, 10, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]
    daemon._run_close_postprocessing = (  # type: ignore[method-assign]
        lambda trade_date, as_of: daemon._state.mark_done(
            trade_date,
            "close_postprocess",
        )
    )
    daemon._freshness_checker.ensure_fresh = lambda *, force=False: {"skipped": True}  # type: ignore[method-assign]

    daemon._tick()
    daemon._tick()  # 第二次 tick 不应重复触发 close

    close_calls = [c for c in stub_monitor.calls if c[0] == "close"]
    assert len(close_calls) == 1


def test_daemon_after_close_catches_up_tail_and_close(monkeypatch, tmp_path: Path) -> None:
    """交易日收盘后启动也必须补跑尾盘研究和收盘复盘。"""
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(2026, 7, 16, 22, 0, tzinfo=MARKET_TZ)
                ),
                "strptime": datetime.strptime,
                "combine": datetime.combine,
                "min": datetime.min,
            },
        ),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]

    daemon._tick()

    modes = [call[0] for call in stub_monitor.calls]
    assert modes[-2:] == ["tail_research", "close"]
    assert daemon._state.is_done("2026-07-16", "tail_research")
    assert daemon._state.is_done("2026-07-16", "close")


def test_daemon_after_midnight_catches_up_previous_trade_date(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(2026, 7, 17, 0, 30, tzinfo=MARKET_TZ)
                ),
                "strptime": datetime.strptime,
                "combine": datetime.combine,
                "min": datetime.min,
            },
        ),
    )
    monkeypatch.setattr(
        daemon_module,
        "_is_trading_day",
        lambda value: value.date().isoformat() == "2026-07-16",
    )
    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]

    daemon._tick()

    previous_calls = [call for call in stub_monitor.calls if call[1][:10] == "2026-07-16"]
    assert [call[0] for call in previous_calls] == ["tail_research", "close"]


def test_daemon_1450_triggers_tail_research_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 14, 50, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)
    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]

    daemon._tick()
    daemon._tick()

    tail_calls = [call for call in stub_monitor.calls if call[0] == "tail_research"]
    assert len(tail_calls) == 1
    assert daemon._state.is_done("2026-07-16", "tail_research")


def test_tail_research_delivery_failure_keeps_report_done(
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )

    class _FailedPushMonitor(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            result.payload["push"] = {"sent": False, "reason": "all_channels_failed"}
            return result

    daemon._monitor = _FailedPushMonitor()  # type: ignore[attr-defined]

    daemon._run_mode(
        "tail_research",
        "2026-07-16",
        "2026-07-16T14:45:00+08:00",
        external_push=True,
    )

    assert daemon._state.is_done("2026-07-16", "tail_research")
    assert not daemon._state.is_done(
        "2026-07-16",
        "tail_research_delivery",
    )
    daemon._consumer.shutdown()


def test_close_success_runs_forward_hook_without_masking_hook_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = _StubMonitor()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        daemon_module,
        "append_p3_forward",
        lambda root: {"status": "hook_failed", "hook_ok": False},
    )

    daemon._run_mode(
        "close",
        "2026-07-16",
        "2026-07-16T15:10:00+08:00",
        external_push=True,
    )

    snapshot = daemon._state.snapshot()
    assert daemon._state.is_done("2026-07-16", "close")
    assert snapshot["daily"]["2026-07-16"]["p3_forward_hook"]["hook_ok"] is False
    daemon._consumer.shutdown()


def test_evening_fresh_data_retries_forward_hook_and_records_actual_trade_date(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type("DT", (), {
            "now": staticmethod(
                lambda tz=None: datetime(2026, 7, 16, 16, 5, tzinfo=MARKET_TZ)
            ),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda value: True)
    calls: list[Path] = []
    monkeypatch.setattr(
        daemon_module,
        "append_p3_forward",
        lambda root: (
            calls.append(root)
            or {
                "status": "appended",
                "hook_ok": True,
                "observation_trade_date": "2026-07-16",
            }
        ),
    )
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = _StubMonitor()  # type: ignore[attr-defined]
    daemon._state.mark_done("2026-07-16", "tail_research")
    daemon._state.mark_done("2026-07-16", "close")
    daemon._freshness_checker.ensure_fresh = lambda *, force=False: {  # type: ignore[method-assign]
        "updated": False,
        "reason": "already_latest",
        "latest_date": "2026-07-16",
    }
    daemon._consumer.drain_once = lambda *, as_of: {"fetched": 0}  # type: ignore[method-assign]

    daemon._tick()

    hook = daemon._state.snapshot()["daily"]["2026-07-16"]["p3_forward_hook"]
    assert len(calls) == 1
    assert hook["hook_ok"] is True
    assert hook["observation_trade_date"] == "2026-07-16"
    daemon._consumer.shutdown()


def test_daemon_non_trading_day_skips_modes(monkeypatch, tmp_path: Path) -> None:
    """非交易日即使在盘前时段也不应触发 premarket（但仍消费 queued 任务）。"""
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 18, 8, 0, tzinfo=MARKET_TZ)),  # 周六
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: False)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]
    daemon._freshness_checker.ensure_fresh = lambda *, force=True: {"updated": False, "reason": "not_trading_day"}  # type: ignore[method-assign]

    daemon._tick()

    # 非交易日不应触发 premarket / monitor / close
    modes = [call[0] for call in stub_monitor.calls]
    assert "premarket" not in modes
    assert "monitor" not in modes
    assert "close" not in modes


def test_daemon_premarket_skipped_when_data_update_failed(monkeypatch, tmp_path: Path) -> None:
    """盘前日线更新失败时应跳过 premarket，避免推送过期数据。"""
    monkeypatch.setattr(
        daemon_module, "datetime",
        type("DT", (), {
            "now": staticmethod(lambda tz=None: datetime(2026, 7, 16, 8, 0, tzinfo=MARKET_TZ)),
            "strptime": datetime.strptime,
            "combine": datetime.combine,
            "min": datetime.min,
        }),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)

    stub_monitor = _StubMonitor()
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = stub_monitor  # type: ignore[attr-defined]
    # 模拟日线更新失败
    daemon._freshness_checker.ensure_fresh = lambda *, force=True: {  # type: ignore[method-assign]
        "updated": False,
        "error": "tushare api timeout",
    }

    daemon._tick()

    # 日线更新失败，premarket 应被跳过
    modes = [call[0] for call in stub_monitor.calls]
    assert "premarket" not in modes
    assert not daemon._state.is_done("2026-07-16", "premarket")


def test_failed_premarket_is_not_marked_done_and_can_retry(monkeypatch, tmp_path: Path) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )

    class _FailedThenSuccessfulMonitor(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            if mode == "premarket" and len(
                [call for call in self.calls if call[0] == "premarket"]
            ) == 1:
                result.status = "failed"
                result.errors = [{"message": "temporary provider error"}]
            return result

    monitor = _FailedThenSuccessfulMonitor()
    daemon._monitor = monitor  # type: ignore[attr-defined]
    daemon._preflight_premarket_sources = lambda trade_date: {  # type: ignore[method-assign]
        "ok": True,
        "reason": "ok",
    }
    daily_check = {"updated": False, "reason": "already_latest"}

    daemon._maybe_run_premarket(
        "2026-07-16",
        "2026-07-16T08:00:00+08:00",
        daily_check,
    )
    assert not daemon._state.is_done("2026-07-16", "premarket")

    daemon._maybe_run_premarket(
        "2026-07-16",
        "2026-07-16T08:01:00+08:00",
        daily_check,
    )
    assert daemon._state.is_done("2026-07-16", "premarket")
    assert len([call for call in monitor.calls if call[0] == "premarket"]) == 2
    daemon.shutdown()


def test_failed_non_push_mode_is_not_marked_done(tmp_path: Path) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )

    class _FailedMonitor(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            result.status = "failed"
            result.errors = [{"message": "temporary research error"}]
            return result

    daemon._monitor = _FailedMonitor()  # type: ignore[attr-defined]

    daemon._run_mode(
        "tail_research",
        "2026-07-16",
        "2026-07-16T15:30:00+08:00",
        external_push=False,
    )

    assert not daemon._state.is_done("2026-07-16", "tail_research")
    daemon.shutdown()


def test_daemon_evening_checks_and_records_daily_freshness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(
                        2026, 7, 16, 23, 0, tzinfo=MARKET_TZ
                    )
                ),
                "strptime": datetime.strptime,
                "combine": datetime.combine,
                "min": datetime.min,
            },
        ),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._monitor = _StubMonitor()  # type: ignore[attr-defined]
    daemon._state.mark_done("2026-07-16", "tail_research")
    daemon._state.mark_done("2026-07-16", "close")
    calls: list[bool] = []
    freshness_result = {
        "updated": False,
        "latest_date": "2026-07-15",
        "expected_date": "2026-07-16",
        "error": "daily provider timeout",
    }

    def _ensure_fresh(*, force: bool = False) -> dict[str, Any]:
        calls.append(force)
        return freshness_result

    daemon._freshness_checker.ensure_fresh = _ensure_fresh  # type: ignore[method-assign]

    daemon._tick()

    assert calls == [False]
    assert daemon._state.snapshot()["runtime"]["last_daily_check"] == freshness_result
    daemon.shutdown()


def test_learning_capture_is_evidenced_idempotent_and_uses_next_trade_day(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.research.control_plane.learning_bridge import DailyLearningBridge
    from core.research.control_plane.learning_loop import ProductionLearningLoop
    from core.research.control_plane.repository import ResearchRepository

    monkeypatch.setattr(
        daemon_module,
        "_is_trading_day",
        lambda value: value.date().isoformat() == "2026-07-17",
    )
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )
    loop = ProductionLearningLoop(ResearchRepository(tmp_path / "learning.db"))
    daemon._learning_bridge = DailyLearningBridge(loop)
    observations = [
        {
            "symbol": "600519.SH",
            "status": "WATCH",
            "research_validated": True,
            "claim_ids": ["claim-1"],
            "source_snapshot_id": "snapshot-1",
            "final_score": 0.72,
            "mapping_confidence": 0.9,
            "degradation_reasons": [],
            "strategy_version": "strategy-observation-v7",
        },
        {
            "symbol": "000001.SZ",
            "status": "UNKNOWN",
            "research_validated": True,
            "claim_ids": ["claim-2"],
            "strategy_version": "strategy-observation-v7",
        },
    ]

    first = daemon._capture_learning_observations(
        observations,
        as_of="2026-07-16T08:00:00+08:00",
    )
    second = daemon._capture_learning_observations(
        observations,
        as_of="2026-07-16T08:00:00+08:00",
    )

    assert first["appended"] == 1
    assert first["skipped"] == 1
    assert second["appended"] == 0
    assert second["duplicates"] == 1
    prediction = loop.get_prediction(first["prediction_ids"][0])
    assert prediction is not None
    assert prediction.due_at == "2026-07-17T16:00:00+08:00"
    assert prediction.model_version.startswith("observations-")
    assert prediction.policy_version == daemon_module.LEARNING_POLICY_VERSION
    learning = daemon._state.snapshot()["learning_status"]
    assert learning["mode"] == "production"
    assert learning["activation_allowed"] is True
    daemon.shutdown()


def test_learning_settlement_error_degrades_without_raising(tmp_path: Path) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _BrokenBridge:
        def settle_after_close(self, **kwargs: Any) -> Any:
            raise RuntimeError("learning database unavailable")

    daemon._learning_bridge = _BrokenBridge()
    daemon._learning_bar_reader = object()

    result = daemon._settle_learning(
        as_of="2026-07-17T16:10:00+08:00",
        trigger="forward_hook",
    )

    assert result["status"] == "error"
    learning = daemon._state.snapshot()["learning_status"]["settlement"]
    assert learning["status"] == "error"
    assert "RuntimeError" in learning["error"]
    daemon.shutdown()


def test_forward_hook_does_not_own_shadow_settlement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )
    monkeypatch.setattr(
        daemon_module,
        "append_p3_forward",
        lambda root: {
            "status": "appended",
            "hook_ok": True,
            "observation_trade_date": "2026-07-17",
        },
    )
    calls: list[dict[str, str]] = []
    daemon._settle_learning = lambda **kwargs: calls.append(kwargs) or {}  # type: ignore[method-assign]

    daemon._run_p3_forward_hook("2026-07-17")

    assert calls == []
    daemon.shutdown()


def test_close_postprocessing_is_not_blocked_by_delivery_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """收盘学习闭环必须独立于微信等通知通道。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )

    class _FailedDeliveryMonitor(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            result.payload["push"] = {
                "sent": False,
                "reason": "all_channels_failed",
            }
            return result

    daemon._monitor = _FailedDeliveryMonitor()  # type: ignore[attr-defined]
    postprocess_calls: list[str] = []
    monkeypatch.setattr(
        daemon,
        "_run_p3_forward_hook",
        lambda trade_date: postprocess_calls.append(f"p3:{trade_date}"),
    )
    monkeypatch.setattr(
        daemon,
        "_run_daily_research_build",
        lambda trade_date: postprocess_calls.append(f"build:{trade_date}"),
    )
    monkeypatch.setattr(
        daemon,
        "_settle_learning",
        lambda **kwargs: postprocess_calls.append(f"settle:{kwargs['trigger']}") or {},
    )

    daemon._run_mode(
        "close",
        "2026-07-16",
        "2026-07-16T15:10:00+08:00",
        external_push=True,
    )

    assert postprocess_calls == [
        "p3:2026-07-16",
        "build:2026-07-16",
        "settle:close",
    ]
    assert daemon._state.is_done("2026-07-16", "close")
    assert not daemon._state.is_done("2026-07-16", "close_delivery")
    daemon.shutdown()


def test_close_delivery_retries_without_repeating_postprocessing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )

    class _RecoveringDeliveryMonitor(_StubMonitor):
        attempts = 0

        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            self.attempts += 1
            result.payload["push"] = {
                "sent": self.attempts >= 2,
                "reason": "" if self.attempts >= 2 else "all_channels_failed",
            }
            return result

    monitor = _RecoveringDeliveryMonitor()
    daemon._monitor = monitor  # type: ignore[attr-defined]
    postprocess_calls: list[str] = []
    def _postprocess(trade_date: str, as_of: str) -> None:
        postprocess_calls.append(trade_date)
        daemon._state.mark_done(trade_date, "close_postprocess")

    monkeypatch.setattr(daemon, "_run_close_postprocessing", _postprocess)

    daemon._maybe_run_close("2026-07-16", "2026-07-16T15:10:00+08:00")
    daemon._maybe_run_close("2026-07-16", "2026-07-16T15:15:00+08:00")

    assert monitor.attempts == 2
    assert postprocess_calls == ["2026-07-16"]
    assert daemon._state.is_done("2026-07-16", "close_delivery")
    daemon.shutdown()


def test_historical_close_catchup_uses_current_time_for_learning_settlement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=60,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._state.mark_done("2026-08-14", "close")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        daemon,
        "_run_close_postprocessing",
        lambda trade_date, as_of: calls.append((trade_date, as_of)),
    )

    daemon._maybe_run_close(
        "2026-08-14",
        "2026-08-14T15:30:00+08:00",
        external_push=False,
        settlement_as_of="2026-08-17T03:00:00+08:00",
    )

    assert calls == [("2026-08-14", "2026-08-17T03:00:00+08:00")]
    daemon.shutdown()


def test_tail_window_cannot_be_missed_by_a_five_minute_tick(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(
                        2026, 7, 16, 14, 55, tzinfo=MARKET_TZ
                    )
                ),
                "strptime": datetime.strptime,
                "combine": datetime.combine,
                "min": datetime.min,
            },
        ),
    )
    monkeypatch.setattr(daemon_module, "_is_trading_day", lambda date: True)
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=300,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    monitor = _StubMonitor()
    daemon._monitor = monitor  # type: ignore[attr-defined]
    daemon._consumer.drain_once = lambda **kwargs: {  # type: ignore[method-assign]
        "fetched": 0,
    }

    daemon._tick()

    assert "tail_research" in [mode for mode, _ in monitor.calls]
    daemon.shutdown()


def test_morning_restart_catches_up_previous_trade_day(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(
                    lambda tz=None: datetime(
                        2026, 7, 17, 8, 0, tzinfo=MARKET_TZ
                    )
                ),
                "strptime": datetime.strptime,
                "combine": datetime.combine,
                "min": datetime.min,
            },
        ),
    )
    monkeypatch.setattr(
        daemon_module,
        "_is_trading_day",
        lambda value: value.date().isoformat() in {"2026-07-16", "2026-07-17"},
    )
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=300,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    monitor = _StubMonitor()
    daemon._monitor = monitor  # type: ignore[attr-defined]
    daemon._consumer.drain_once = lambda **kwargs: {  # type: ignore[method-assign]
        "fetched": 0,
    }
    daemon._preflight_premarket_sources = lambda trade_date: {  # type: ignore[method-assign]
        "ok": True,
        "reason": "ok",
    }
    daemon._freshness_checker.ensure_fresh = lambda **kwargs: {  # type: ignore[method-assign]
        "updated": False,
        "reason": "already_latest",
    }

    daemon._tick()

    previous_modes = [
        mode for mode, as_of in monitor.calls if as_of[:10] == "2026-07-16"
    ]
    assert previous_modes == ["tail_research", "close"]
    daemon.shutdown()


def test_pending_due_learning_is_retried_after_data_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=300,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    daemon._state.mark_done("2026-07-28", "p3_forward")
    daemon._state.mark_done("2026-07-28", "daily_research_build")
    settlements = iter(
        [
            {"status": "ok", "pending": 2, "settled": 0},
            {"status": "ok", "pending": 0, "settled": 2},
        ]
    )
    monkeypatch.setattr(
        daemon,
        "_settle_learning",
        lambda **kwargs: next(settlements),
    )

    daemon._run_close_postprocessing(
        "2026-07-28",
        "2026-07-28T16:00:00+08:00",
    )
    assert not daemon._state.is_done(
        "2026-07-28",
        "learning_settlement",
    )

    daemon._run_close_postprocessing(
        "2026-07-28",
        "2026-07-28T16:05:00+08:00",
    )
    assert daemon._state.is_done(
        "2026-07-28",
        "learning_settlement",
    )
    assert daemon._state.is_done("2026-07-28", "close_postprocess")
    daemon.shutdown()


def test_stale_learning_completion_is_invalidated_and_retried(
    monkeypatch,
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        tick_interval=300,
        research_workers=2,
        research_batch_size=4,
        state_file=tmp_path / "state.json",
    )
    for phase in (
        "p3_forward",
        "daily_research_build",
        "learning_settlement",
        "close_postprocess",
    ):
        daemon._state.mark_done("2026-07-28", phase)
    daemon._state.mark_learning_status(
        "settlement",
        {"status": "ok", "pending": 2, "settled": 0},
    )
    monkeypatch.setattr(
        daemon,
        "_settle_learning",
        lambda **kwargs: {"status": "ok", "pending": 0, "settled": 2},
    )

    daemon._run_close_postprocessing(
        "2026-07-28",
        "2026-07-28T16:05:00+08:00",
    )

    daily = daemon._state.snapshot()["daily"]["2026-07-28"]
    assert daily["learning_settlement_invalidated"]["reason"] == (
        "completion_invariant_not_satisfied"
    )
    assert daemon._state.is_done("2026-07-28", "learning_settlement")
    assert daemon._state.is_done("2026-07-28", "close_postprocess")
    daemon.shutdown()


def test_runtime_restart_replaces_started_at_and_counts_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = daemon_module.DaemonState(tmp_path / "state.json")
    timestamps = iter(
        [
            datetime(2026, 7, 28, 7, 0, tzinfo=MARKET_TZ),
            datetime(2026, 7, 28, 8, 0, tzinfo=MARKET_TZ),
        ]
    )
    monkeypatch.setattr(
        daemon_module,
        "datetime",
        type(
            "DT",
            (),
            {"now": staticmethod(lambda tz=None: next(timestamps))},
        ),
    )

    state.mark_runtime(pid=100, status="running")
    first_started_at = state.snapshot()["runtime"]["started_at"]
    state.mark_runtime(pid=200, status="running")
    runtime = state.snapshot()["runtime"]

    assert runtime["started_at"] != first_started_at
    assert runtime["started_at"] == "2026-07-28T08:00:00+08:00"
    assert runtime["restart_count"] == 1


def test_failed_premarket_push_is_not_marked_done_and_retries(
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _PushFailsOnce(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            result = super().run(mode=mode, as_of=as_of, **kwargs)
            if mode == "push" and len(
                [call for call in self.calls if call[0] == "push"]
            ) == 1:
                result.status = "failed"
                result.payload["push"] = {
                    "sent": False,
                    "reason": "provider_timeout",
                }
            return result

    monitor = _PushFailsOnce()
    daemon._monitor = monitor  # type: ignore[attr-defined]

    daemon._maybe_push_premarket(
        "2026-07-16",
        "2026-07-16T08:00:00+08:00",
    )
    assert not daemon._state.is_done("2026-07-16", "push")

    daemon._maybe_push_premarket(
        "2026-07-16",
        "2026-07-16T08:01:00+08:00",
    )
    assert daemon._state.is_done("2026-07-16", "push")
    daemon.shutdown()


def test_premarket_push_exception_degrades_without_marking_done(
    tmp_path: Path,
) -> None:
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _PushRaises(_StubMonitor):
        def run(self, *, mode: str, as_of: str, **kwargs: Any) -> Any:
            if mode == "push":
                raise RuntimeError("push provider unavailable")
            return super().run(mode=mode, as_of=as_of, **kwargs)

    daemon._monitor = _PushRaises()  # type: ignore[attr-defined]

    daemon._maybe_push_premarket(
        "2026-07-16",
        "2026-07-16T08:00:00+08:00",
    )

    assert not daemon._state.is_done("2026-07-16", "push")
    daemon.shutdown()
