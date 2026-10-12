#!/usr/bin/env python
"""AquaTrade ResearchMonitor 持续运行守护进程。

将原本一次性的 premarket / monitor / close CLI 改造为常驻服务：

1. 时段自动调度（北京时间）
   - 07:30-09:25 盘前：触发一次 run_premarket + push
   - 09:30-11:30 / 13:00-15:00 盘中：每 tick 触发 run_monitor 刷新行情快照
   - 15:05-15:30 收盘：触发一次 run_close 归档
   - 其他时段：空闲
2. 日线新鲜度检查
   - 盘前触发前若 latest_date < today 且为交易日，先调用 UnifiedDataUpdater 更新
   - 其他时段每 30 分钟检查一次
3. queued 研究任务并发消费
   - 每 tick 从 ResearchRepository.list_tasks(status='queued') 拉取
   - 用 ThreadPoolExecutor 多线程调用 ResearchOrchestrator.execute
   - CPU 八核，默认 4 worker，每轮最多 8 任务

启动:
    python scripts/run_research_monitor_daemon.py
    python scripts/run_research_monitor_daemon.py --tick-interval 300 --research-workers 4

环境变量:
    RESEARCH_MONITOR_OUTPUT_DIR: 输出目录（同 run_research_monitor.py）
    AQUATRADE_DAEMON_STATE_FILE: 状态文件路径，默认 data/state/research_monitor_daemon.json
    AQUATRADE_DAEMON_ONCE: 设为 1 时只跑一个 tick 后退出（调试用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any

# 清除代理环境变量——国内数据源（Tushare/AKShare/问财/东方财富）不需要代理，
# 代理工具未运行时会导致所有 HTTP 请求失败
_DIRECT_REQUEST_HOSTS = (
    "api.waditu.com",
    "www.pushplus.plus",
    "open.feishu.cn",
)


def _merge_no_proxy_hosts(existing: str = "") -> str:
    """Return a stable proxy-bypass list for direct production endpoints."""
    hosts = [item.strip() for item in str(existing or "").split(",") if item.strip()]
    for host in _DIRECT_REQUEST_HOSTS:
        if host not in hosts:
            hosts.append(host)
    return ",".join(hosts)


for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_proxy_key, None)
for _no_proxy_key in ("NO_PROXY", "no_proxy"):
    os.environ[_no_proxy_key] = _merge_no_proxy_hosts(
        os.environ.get(_no_proxy_key, "")
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime.research_monitor import ResearchMonitor, MARKET_TZ
from core.runtime.research_daemon_health import (
    DaemonAlreadyRunning,
    SingleInstanceLease,
)
from scripts.run_p3_forward_cohort import (
    DEFAULT_ROOT as P3_FORWARD_ROOT,
    append as append_p3_forward,
)

logger = logging.getLogger("research_monitor_daemon")

# 时段边界（北京时间 24h 制，开区间左闭右闭）
PREMARKET_WINDOW = (7, 30, 9, 25)        # 07:30 - 09:25
MONITOR_AM_WINDOW = (9, 30, 11, 30)      # 09:30 - 11:30
MONITOR_PM_WINDOW = (13, 0, 15, 0)       # 13:00 - 15:00
TAIL_RESEARCH_WINDOW = (14, 45, 15, 0)   # 14:45 - 15:00
CLOSE_WINDOW = (15, 5, 15, 30)           # 15:05 - 15:30

# 日线更新检查间隔（秒）
DAILY_UPDATE_CHECK_INTERVAL = 30 * 60
LEARNING_POLICY_VERSION = "research_monitor_production_learning_v1"

# premarket 源预检阈值（环境变量可覆盖）
# - PREMARKET_SNAPSHOT_MAX_AGE_DAYS: snapshot.as_of 距 trade_date 的最大容忍天数
# - PREMARKET_SNAPSHOT_MIN_MEMBERS: snapshot 最少有效成员数
PREMARKET_SNAPSHOT_MAX_AGE_DAYS = int(
    os.environ.get("PREMARKET_SNAPSHOT_MAX_AGE_DAYS", "3")
)
PREMARKET_SNAPSHOT_MIN_MEMBERS = int(
    os.environ.get("PREMARKET_SNAPSHOT_MIN_MEMBERS", "50")
)


def _learning_mode() -> str:
    """Return the governed runtime mode; production never bypasses gates."""
    value = os.environ.get("RESEARCH_LEARNING_MODE", "production").strip().lower()
    return "production" if value == "production" else "shadow"


def _current_code_version() -> str:
    configured = os.environ.get("AQUATRADE_CODE_VERSION", "").strip()
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:
        return ""


def _promote_learning_calibration(
    learning_loop: Any,
    calibration_id: str,
    *,
    code_version: str = "",
) -> dict[str, Any]:
    """Activate an eligible calibration through the existing policy gate."""
    if _learning_mode() != "production":
        return {"status": "shadow", "reason": "production_mode_disabled"}
    if not calibration_id:
        return {"status": "pending", "reason": "calibration_missing"}

    repository = learning_loop.repository
    run = repository.get_calibration(calibration_id)
    if run is None:
        return {"status": "blocked", "reason": "calibration_not_found"}
    if run.status == "active":
        return {
            "status": "active",
            "calibration_id": run.calibration_id,
            "policy_version": run.policy_version,
            "test_run_id": "",
        }

    effective_version = str(code_version or _current_code_version()).strip()
    test_record = next(
        (
            item
            for item in repository.list_test_runs(limit=100)
            if item.passed
            and item.code_version == effective_version
            and "tests/test_feedback_loop_integration.py" in item.test_suite
        ),
        None,
    )
    if test_record is None:
        return {
            "status": "blocked",
            "reason": "passing_test_attestation_missing",
            "calibration_id": calibration_id,
            "code_version": effective_version,
        }

    from core.research.control_plane.calibration import CalibrationController

    try:
        activated = CalibrationController(repository).activate(
            calibration_id,
            os.environ.get(
                "RESEARCH_LEARNING_APPROVER",
                "user-approved-production-learning",
            ),
            test_run_id=test_record.test_run_id,
            code_version=effective_version,
        )
    except (KeyError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "activation_gate_rejected",
            "detail": str(exc),
            "calibration_id": calibration_id,
            "test_run_id": test_record.test_run_id,
        }
    return {
        "status": "active",
        "reason": "governed_activation_succeeded",
        "calibration_id": activated.calibration_id,
        "policy_version": activated.policy_version,
        "test_run_id": test_record.test_run_id,
    }


def _in_window(now: datetime, window: tuple[int, int, int, int]) -> bool:
    """判断 now 是否落在 (start_h, start_m, end_h, end_m) 时段内。"""
    start_h, start_m, end_h, end_m = window
    now_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    return start_minutes <= now_minutes <= end_minutes


def _is_weekend(date: datetime) -> bool:
    """判断是否周末（周六周日）。"""
    return date.weekday() >= 5


def _is_trading_day(query_date: datetime) -> bool:
    """通过 Tushare 交易日历判断是否为交易日。

    Args:
        query_date: 待判断日期。

    Returns:
        True 表示是交易日。
    """
    if _is_weekend(query_date):
        return False
    try:
        import tushare as ts
        from config.config import Config

        pro = ts.pro_api(Config.TUSHARE_TOKEN)
        date_str = query_date.strftime("%Y%m%d")
        cal_df = pro.trade_cal(exchange="SSE", start_date=date_str, end_date=date_str)
        if cal_df is None or cal_df.empty:
            return False
        return int(cal_df.iloc[0]["is_open"]) == 1
    except Exception as exc:
        logger.warning("查询交易日历失败 %s: %s，按周末规则降级", query_date.date(), exc)
        return not _is_weekend(query_date)


def _trading_day_age(source_date: datetime, trade_date: datetime) -> int:
    """Count open sessions after ``source_date`` through ``trade_date``."""
    if source_date.date() > trade_date.date():
        return -1
    age = 0
    cursor = source_date + timedelta(days=1)
    while cursor.date() <= trade_date.date():
        if _is_trading_day(cursor):
            age += 1
        cursor += timedelta(days=1)
    return age


class DailyDataFreshnessChecker:
    """LanceDB daily_ohlcv 日线数据新鲜度检查器。

    防止推送过期数据：盘前触发前必查，过期则触发增量更新。
    """

    def __init__(self) -> None:
        """初始化日线新鲜度检查器，懒加载 UnifiedDataQuery 与 Updater。"""
        self._query = None
        self._updater = None
        self._last_check_at: datetime | None = None

    def _ensure_handles(self) -> None:
        """懒加载 UnifiedDataQuery 与 UnifiedDataUpdater 实例。"""
        if self._query is None:
            from data_svc.unified_data_query import get_unified_data_query

            self._query = get_unified_data_query()
        if self._updater is None:
            from data_svc.storage.unified_updater import UnifiedDataUpdater

            self._updater = UnifiedDataUpdater()

    def latest_date(self) -> str | None:
        """返回 LanceDB daily_ohlcv 最新交易日（YYYY-MM-DD），失败返回 None。"""
        self._ensure_handles()
        try:
            stats = self._query.get_db_stats()
            latest = stats.get("latest_date")
            if not latest:
                return None
            text = str(latest)[:10]
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except Exception as exc:
            logger.warning("读取 LanceDB latest_date 失败: %s", exc)
            return None

    def _run_update(self) -> None:
        """执行更新，并把 ``UpdateResult.success=False`` 转为明确失败。"""
        update_result = self._updater.run_full_update(skip_dragon=True)
        if update_result is not None and getattr(update_result, "success", True) is False:
            message = str(getattr(update_result, "message", "") or "daily update failed")
            raise RuntimeError(message)

    @staticmethod
    def _expected_latest_date(now: datetime) -> str:
        """返回当前时点应已落库的最近完整交易日。"""
        today = now.date()
        if now.hour >= 16 and _is_trading_day(
            datetime.combine(today, datetime.min.time())
        ):
            return today.isoformat()
        candidate = today - timedelta(days=1)
        for _ in range(10):
            if _is_trading_day(datetime.combine(candidate, datetime.min.time())):
                return candidate.isoformat()
            candidate -= timedelta(days=1)
        return candidate.isoformat()

    def ensure_fresh(self, *, force: bool = False) -> dict[str, Any]:
        """检查并按需触发日线增量更新。

        Args:
            force: True 时无论上次检查多久都强制重新检查。

        Returns:
            包含 latest_date / updated / error 的字典。
        """
        now = datetime.now(MARKET_TZ)
        if not force and self._last_check_at is not None:
            elapsed = (now - self._last_check_at).total_seconds()
            if elapsed < DAILY_UPDATE_CHECK_INTERVAL:
                return {"skipped": True, "reason": "checked_recently"}

        self._last_check_at = now
        self._ensure_handles()

        latest_str = self.latest_date()
        today = now.date()
        result: dict[str, Any] = {
            "checked_at": now.isoformat(timespec="seconds"),
            "latest_date": latest_str,
            "today": today.isoformat(),
            "expected_date": self._expected_latest_date(now),
        }

        # LanceDB 为空 → 全量更新
        if latest_str is None:
            logger.warning("LanceDB daily_ohlcv 为空，触发全量更新")
            try:
                self._run_update()
                result["updated"] = True
                result["mode"] = "full"
            except Exception as exc:
                logger.error("全量更新失败: %s", exc)
                result["updated"] = False
                result["error"] = str(exc)
            return result

        latest_date = datetime.strptime(latest_str, "%Y-%m-%d").date()
        expected_date = datetime.strptime(result["expected_date"], "%Y-%m-%d").date()
        if latest_date >= expected_date:
            result["updated"] = False
            result["reason"] = "already_latest"
            return result

        logger.info("日线数据落后 (%s -> %s)，触发增量更新", latest_date, expected_date)
        try:
            self._run_update()
            result["updated"] = True
            result["mode"] = "incremental"
        except Exception as exc:
            logger.error("增量更新失败: %s", exc)
            result["updated"] = False
            result["error"] = str(exc)
        return result


class QueuedTaskConsumer:
    """queued 研究任务并发消费器。

    只消费 dynamic_theme_review，并通过 ResearchOrchestrator.execute 执行。
    Perilla discovery 搜索任务由 KnowledgeLoopEngine 负责执行和落盘；此处
    直接调用 ProviderRegistry 会丢失 DocumentVersion/EvidenceRecord 链路。

    每 tick 从 ResearchRepository 拉取 queued 任务，用 ThreadPoolExecutor 并发执行。
    """

    # capability → ProviderRegistry 方法名映射
    def __init__(
        self,
        monitor: ResearchMonitor,
        *,
        max_workers: int = 4,
        batch_size: int = 8,
    ) -> None:
        """初始化任务消费器。

        Args:
            monitor: ResearchMonitor 实例，用于构建 research runtime。
            max_workers: 线程池最大并发数。
            batch_size: 每 tick 最多处理的任务数。
        """
        self._monitor = monitor
        self._max_workers = max(1, int(max_workers))
        self._batch_size = max(1, int(batch_size))
        self._executor: ThreadPoolExecutor | None = None
        self._provider_registry: Any | None = None
        self._registry_init_failed = False

    def _get_executor(self) -> ThreadPoolExecutor:
        """懒加载线程池（首次调用时创建）。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="research-worker",
            )
        return self._executor

    def _get_provider_registry(self) -> Any | None:
        """懒加载 ProviderRegistry，失败后不再重试。"""
        if self._registry_init_failed:
            return None
        if self._provider_registry is None:
            try:
                from infrastructure.data_providers.provider_registry import ProviderRegistry
                self._provider_registry = ProviderRegistry()
            except Exception as exc:
                logger.warning("ProviderRegistry 不可用: %s", exc)
                self._registry_init_failed = True
                return None
        return self._provider_registry

    def _execute_search_task(self, task: Any) -> tuple[str, bool, str]:
        """执行 AquaTradeResearchEngine 搜索任务。

        Args:
            task: ResearchTask 实例，params 含 aquatrade_capability 字段。

        Returns:
            tuple (task_id, success, error_msg)。
        """
        registry = self._get_provider_registry()
        if registry is None:
            return task.task_id, False, "ProviderRegistry unavailable"

        params = task.params or {}
        capability = params.get("aquatrade_capability", "semantic_search")
        query = params.get("query", "")
        symbols = params.get("symbols") or []

        def _records(value: Any) -> list[dict[str, Any]]:
            if value is None:
                return []
            if hasattr(value, "to_dict") and hasattr(value, "columns"):
                try:
                    return list(value.to_dict(orient="records"))
                except Exception:
                    return []
            if isinstance(value, dict):
                for key in ("results", "data", "items", "announcements", "reports"):
                    rows = value.get(key)
                    if isinstance(rows, list):
                        return [row for row in rows if isinstance(row, dict)]
                return [value]
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            return []

        try:
            retrieved: list[dict[str, Any]] = []
            if capability == "semantic_search":
                retrieved.extend(_records(registry.semantic_search(query=query, count=10)))
            elif capability == "announcements":
                for sym in symbols[:3]:
                    retrieved.extend(_records(registry.get_announcements(code=sym, count=3)))
            elif capability == "news":
                # ProviderRegistry 无 get_news 方法（原代码也静默跳过）
                if not symbols:
                    return task.task_id, True, ""
                for sym in symbols[:3]:
                    try:
                        retrieved.extend(_records(registry.get_announcements(code=sym, count=3)))
                    except Exception:
                        pass
            elif capability == "reports":
                for sym in symbols[:3]:
                    retrieved.extend(_records(registry.get_research_reports(code=sym, count=3)))
            else:
                return task.task_id, False, f"unknown capability: {capability}"
            task.params = dict(params)
            task.params["retrieval_result_count"] = len(retrieved)
            task.params["retrieval_results"] = retrieved[:20]
            task.params["retrieved_at"] = datetime.now(MARKET_TZ).isoformat(
                timespec="seconds"
            )
            return task.task_id, True, ""
        except Exception as exc:
            return task.task_id, False, str(exc)

    def drain_once(self, *, as_of: str) -> dict[str, Any]:
        """拉取一批 queued 任务并发执行（支持 dynamic_theme_review + 搜索任务）。

        Args:
            as_of: 当前 ISO 时间戳，传给 orchestrator.execute。

        Returns:
            包含 fetched / succeeded / failed / skipped / errors 的统计字典。
        """
        repository = self._monitor.repository
        try:
            tasks = repository.list_tasks(status="queued", limit=500)
            tasks.extend(repository.list_tasks(status="retrying", limit=500))
        except Exception as exc:
            logger.error("拉取 queued 研究任务失败: %s", exc)
            return {
                "fetched": 0,
                "succeeded": 0,
                "partial": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [str(exc)],
            }

        batch_candidates = [
            task for task in tasks
            if (
                (task.params or {}).get("workflow") == "dynamic_theme_review"
                or (task.params or {}).get("execution_backend")
                == "AquaTradeResearchEngine"
            )
        ]
        if not batch_candidates:
            return {
                "fetched": 0,
                "succeeded": 0,
                "partial": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [],
            }

        batch = sorted(
            batch_candidates,
            key=lambda task: float(getattr(task, "priority", 0.0) or 0.0),
            reverse=True,
        )[: self._batch_size]
        batch_orchestrator = [
            task for task in batch
            if (task.params or {}).get("workflow") == "dynamic_theme_review"
        ]
        batch_search = [
            task for task in batch
            if (task.params or {}).get("execution_backend")
            == "AquaTradeResearchEngine"
        ]

        if not batch:
            return {
                "fetched": len(batch_candidates),
                "succeeded": 0,
                "partial": 0,
                "failed": 0,
                "skipped": len(batch_candidates),
                "errors": [],
            }

        # 构建 runtime（仅当有 orchestrator 任务时）
        orchestrator = None
        if batch_orchestrator:
            try:
                runtime = self._monitor._build_research_runtime(as_of=as_of)
                orchestrator = getattr(runtime, "orchestrator", None)
            except Exception as exc:
                logger.warning("构建 research runtime 失败: %s", exc)

        executor = self._get_executor()
        from core.research.control_plane.contracts import TaskStatus, utc_now

        def _run_orchestrator_task(task: Any) -> tuple[str, str, str]:
            """执行 dynamic_theme_review 任务。"""
            if orchestrator is None:
                return task.task_id, "failed", "orchestrator unavailable"
            try:
                if task.status == TaskStatus.RETRYING.value:
                    resumer = getattr(orchestrator, "task_resumer", None)
                    resume_task = getattr(resumer, "resume_task", None)
                    if not callable(resume_task):
                        return (
                            task.task_id,
                            "failed",
                            f"task={task.task_id} status=retrying "
                            "reason=resumer_unavailable",
                        )
                    task = resume_task(task)
                task.params = dict(task.params or {})
                task.params.setdefault("as_of", as_of)
                summary = orchestrator.execute(task)
                succeeded = summary.status in {
                    "success", TaskStatus.SUCCEEDED.value,
                }
                outcome = (
                    "succeeded"
                    if succeeded
                    else (
                        "partial"
                        if summary.status == TaskStatus.PARTIAL.value
                        else "failed"
                    )
                )
                err = (
                    summary.errors[0].get("message", "")
                    if summary.errors
                    else ""
                )
                if outcome == "failed" and not err:
                    reason = (
                        getattr(summary, "stopped_reason_code", "")
                        or getattr(summary, "stopped_reason", "")
                        or "no_error_detail"
                    )
                    err = (
                        f"task={task.task_id} status={summary.status} "
                        f"reason={reason}"
                    )
                return task.task_id, outcome, err
            except Exception as exc:
                return task.task_id, "failed", str(exc)

        def _run_search_task(task: Any) -> tuple[str, str, str]:
            """执行 AquaTradeResearchEngine 搜索任务。"""
            try:
                task_id, ok, err = self._execute_search_task(task)
                # 更新任务状态
                task.status = TaskStatus.SUCCEEDED.value if ok else TaskStatus.FAILED.value
                task.finished_at = utc_now()
                if err:
                    task.failure = {"reason": err}
                repository.save_task(task)
                return task_id, "succeeded" if ok else "failed", err
            except Exception as exc:
                try:
                    task.status = TaskStatus.FAILED.value
                    task.finished_at = utc_now()
                    task.failure = {"reason": str(exc)[:200]}
                    repository.save_task(task)
                except Exception:
                    pass
                return task.task_id, "failed", str(exc)

        succeeded = 0
        partial = 0
        failed = 0
        errors: list[str] = []

        # 提交所有任务
        futures = []
        for task in batch_orchestrator:
            futures.append(executor.submit(_run_orchestrator_task, task))
        for task in batch_search:
            futures.append(executor.submit(_run_search_task, task))

        for future in as_completed(futures):
            try:
                _, outcome, err = future.result()
                if outcome == "succeeded":
                    succeeded += 1
                elif outcome == "partial":
                    partial += 1
                else:
                    failed += 1
                    if err:
                        errors.append(err)
            except Exception as exc:
                failed += 1
                errors.append(str(exc))

        logger.info(
            "queued 任务消费完成 fetched=%d batch=%d (orch=%d search=%d) succeeded=%d partial=%d failed=%d",
            len(batch_candidates), len(batch), len(batch_orchestrator), len(batch_search),
            succeeded, partial, failed,
        )
        return {
            "fetched": len(batch_candidates),
            "batch": len(batch),
            "succeeded": succeeded,
            "partial": partial,
            "failed": failed,
            "skipped": len(batch_candidates) - len(batch),
            "errors": errors[:10],
        }

    def shutdown(self) -> None:
        """关闭线程池，等待已提交任务完成。"""
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None


class DaemonState:
    """daemon 跨 tick 状态持久化。

    记录当日各时段 mode 是否已触发，避免重启重复推送。
    """

    def __init__(self, path: Path) -> None:
        """初始化状态存储。

        Args:
            path: 状态文件 JSON 路径。
        """
        self._path = path
        self._lock = threading.Lock()
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """从磁盘加载状态。文件不存在或损坏时返回空字典。"""
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("加载 daemon 状态失败: %s", exc)
        return {}

    def _save(self) -> None:
        """持久化状态到磁盘。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            temporary_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, self._path)
        except Exception as exc:
            logger.warning("保存 daemon 状态失败: %s", exc)

    def mark_runtime(self, **values: Any) -> None:
        """Persist a heartbeat and runtime metadata without losing daily state."""
        with self._lock:
            runtime = self._state.setdefault("runtime", {})
            now = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
            previous_pid = runtime.get("pid")
            incoming_pid = values.get("pid")
            process_restarted = bool(
                values.get("status") == "running"
                and previous_pid
                and incoming_pid
                and previous_pid != incoming_pid
            )
            if process_restarted:
                runtime["started_at"] = now
                runtime["restart_count"] = (
                    int(runtime.get("restart_count") or 0) + 1
                )
                runtime.pop("stopped_at", None)
            else:
                runtime.setdefault("started_at", now)
            runtime.update(values)
            runtime["heartbeat_at"] = now
            self._save()

    def mark_done(self, trade_date: str, mode: str) -> None:
        """标记某交易日某 mode 已完成。

        Args:
            trade_date: 交易日（YYYY-MM-DD）。
            mode: premarket / monitor / close / push。
        """
        with self._lock:
            daily = self._state.setdefault("daily", {})
            daily.setdefault(trade_date, {})
            daily[trade_date][f"{mode}_done"] = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
            # 盘中 monitor 允许多轮触发，但记录最后一轮时间戳
            daily[trade_date][f"{mode}_last_at"] = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
            self._save()

    def mark_attempt(
        self,
        trade_date: str,
        mode: str,
        *,
        status: str,
        reason: str = "",
    ) -> None:
        """Persist a bounded, queryable phase attempt for health checks."""
        with self._lock:
            daily = self._state.setdefault("daily", {})
            daily.setdefault(trade_date, {})
            daily[trade_date][f"{mode}_last_attempt"] = {
                "status": str(status),
                "reason": str(reason)[:500],
                "at": datetime.now(MARKET_TZ).isoformat(
                    timespec="seconds"
                ),
            }
            self._save()

    def clear_done(self, trade_date: str, mode: str) -> None:
        """Remove a stale completion marker while preserving audit history."""
        with self._lock:
            daily = self._state.setdefault("daily", {})
            daily.setdefault(trade_date, {})
            completed_at = daily[trade_date].pop(f"{mode}_done", None)
            if completed_at:
                daily[trade_date][f"{mode}_invalidated"] = {
                    "completed_at": completed_at,
                    "invalidated_at": datetime.now(MARKET_TZ).isoformat(
                        timespec="seconds"
                    ),
                    "reason": "completion_invariant_not_satisfied",
                }
            self._save()

    def mark_forward_hook(self, trade_date: str, result: dict[str, Any]) -> None:
        """记录 close 后 P3 forward 钩子，不与 close 成功状态混淆。"""
        with self._lock:
            daily = self._state.setdefault("daily", {})
            daily.setdefault(trade_date, {})
            daily[trade_date]["p3_forward_hook"] = {
                **result,
                "recorded_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
            }
            self._save()

    def mark_learning_status(
        self,
        section: str,
        result: dict[str, Any],
    ) -> None:
        """Persist shadow-learning status independently from daily modes."""
        with self._lock:
            learning = self._state.setdefault("learning_status", {})
            learning.update(
                {
                    "mode": _learning_mode(),
                    "activation_allowed": _learning_mode() == "production",
                    "policy_version": LEARNING_POLICY_VERSION,
                }
            )
            learning[section] = {
                **result,
                "recorded_at": datetime.now(MARKET_TZ).isoformat(
                    timespec="seconds"
                ),
            }
            self._save()

    def is_done(self, trade_date: str, mode: str) -> bool:
        """查询某交易日某 mode 是否已完成（用于 once 类 mode）。"""
        with self._lock:
            return bool(self._state.get("daily", {}).get(trade_date, {}).get(f"{mode}_done"))

    def last_monitor_at(self, trade_date: str) -> datetime | None:
        """返回当日最后一轮 monitor 触发时间，None 表示尚未触发。"""
        with self._lock:
            value = self._state.get("daily", {}).get(trade_date, {}).get("monitor_last_at")
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except Exception:
                return None

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态快照（线程安全副本）。"""
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))


class ResearchMonitorDaemon:
    """持续运行的 ResearchMonitor 守护进程。"""

    def __init__(
        self,
        *,
        output_dir: str | Path,
        tick_interval: int = 300,
        research_workers: int = 4,
        research_batch_size: int = 8,
        max_research_tasks: int = 6,
        state_file: Path | None = None,
    ) -> None:
        """初始化 daemon。

        Args:
            output_dir: ResearchMonitor 输出目录。
            tick_interval: 主循环 tick 间隔（秒），默认 300（5 分钟）。
            research_workers: queued 研究任务并发 worker 数。
            research_batch_size: 每 tick 最多消费的研究任务数。
            max_research_tasks: premarket 内部 _execute_research_tasks 上限。
            state_file: 状态文件路径，None 时从环境变量或默认路径读取。
        """
        self._tick_interval = max(30, int(tick_interval))
        self._max_research_tasks = max(0, int(max_research_tasks))
        self._stop_event = threading.Event()
        self._monitor = ResearchMonitor(
            output_dir=output_dir,
            persist_quant_signals=True,
        )
        self._freshness_checker = DailyDataFreshnessChecker()
        self._consumer = QueuedTaskConsumer(
            self._monitor,
            max_workers=research_workers,
            batch_size=research_batch_size,
        )

        if state_file is None:
            state_file = Path(
                os.getenv(
                    "AQUATRADE_DAEMON_STATE_FILE",
                    "data/state/research_monitor_daemon.json",
                )
            )
        self._state = DaemonState(Path(state_file))
        pid_file = Path(
            os.getenv(
                "AQUATRADE_DAEMON_PID_FILE",
                str(Path(state_file).with_suffix(".pid")),
            )
        )
        self._lease = SingleInstanceLease(pid_file)
        self._heartbeat_interval = min(30, max(5, self._tick_interval // 3))
        self._heartbeat_thread: threading.Thread | None = None
        self._learning_bridge: Any | None = None
        self._learning_bar_reader: Any | None = None
        self._theme_source_router: Any | None = None

    # ------------------------------------------------------------------
    # 信号与生命周期
    # ------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """注册 SIGINT/SIGTERM 优雅退出。"""
        def _handler(signum: int, frame: Any) -> None:
            logger.info("收到信号 %d，准备退出", signum)
            self._stop_event.set()

        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            # Windows 上 SIGTERM 行为有限，忽略
            pass

    def stop(self) -> None:
        """外部触发停止。"""
        self._stop_event.set()

    def shutdown(self) -> None:
        """清理资源。"""
        self._consumer.shutdown()

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self._heartbeat_interval):
            self._state.mark_runtime(
                pid=os.getpid(),
                status="running",
                tick_interval_seconds=self._tick_interval,
            )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run_forever(self) -> int:
        """daemon 主循环。每 tick 调度时段任务 + 消费 queued 任务。"""
        try:
            self._lease.acquire()
        except DaemonAlreadyRunning as exc:
            logger.error("%s", exc)
            return 3

        logger.info(
            "ResearchMonitor daemon 启动 tick_interval=%ds workers=%d",
            self._tick_interval, self._consumer._max_workers,
        )
        once = os.getenv("AQUATRADE_DAEMON_ONCE", "").lower() in {"1", "true", "yes"}
        self._state.mark_runtime(
            pid=os.getpid(),
            status="running",
            tick_interval_seconds=self._tick_interval,
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="research-daemon-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        try:
            while not self._stop_event.is_set():
                tick_started_at = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
                self._state.mark_runtime(
                    pid=os.getpid(),
                    status="running",
                    tick_interval_seconds=self._tick_interval,
                    last_tick_started_at=tick_started_at,
                )
                try:
                    self._tick()
                    self._state.mark_runtime(
                        last_tick_status="ok",
                        consecutive_failures=0,
                        last_tick_finished_at=datetime.now(MARKET_TZ).isoformat(
                            timespec="seconds"
                        ),
                        last_error=None,
                    )
                except Exception as exc:
                    try:
                        consecutive_failures = int(
                            self._state.snapshot()
                            .get("runtime", {})
                            .get("consecutive_failures", 0)
                        ) + 1
                    except (TypeError, ValueError):
                        consecutive_failures = 1
                    self._state.mark_runtime(
                        last_tick_status="error",
                        consecutive_failures=consecutive_failures,
                        last_tick_finished_at=datetime.now(MARKET_TZ).isoformat(
                            timespec="seconds"
                        ),
                        last_error=str(exc)[:1000],
                    )
                    logger.exception("tick 异常: %s", exc)

                if once:
                    logger.info("AQUATRADE_DAEMON_ONCE=1，单 tick 模式退出")
                    break

                # 等待下一个 tick，支持提前唤醒
                self._stop_event.wait(self._tick_interval)
            return 0
        finally:
            self._stop_event.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=5)
            self.shutdown()
            self._state.mark_runtime(
                pid=os.getpid(),
                status="stopped",
                stopped_at=datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
            )
            self._lease.release()
            logger.info("ResearchMonitor daemon 退出")

    def _tick(self) -> None:
        """单个调度 tick。"""
        now = datetime.now(MARKET_TZ)
        trade_date = now.date().isoformat()
        as_of = now.isoformat(timespec="seconds")
        logger.info("tick @ %s", as_of)

        # 午夜后补偿上一交易日错过的尾盘与收盘任务。
        if now.hour < 7 or _in_window(now, PREMARKET_WINDOW):
            previous_day = now.date() - timedelta(days=1)
            for _ in range(10):
                previous_dt = datetime.combine(
                    previous_day,
                    datetime.min.time(),
                )
                if not _is_trading_day(previous_dt):
                    previous_day -= timedelta(days=1)
                    continue
                previous_trade_date = previous_day.isoformat()
                previous_as_of = f"{previous_trade_date}T15:30:00+08:00"
                if not self._state.is_done(previous_trade_date, "tail_research"):
                    logger.warning(
                        "missed previous-day tail window; running catch-up date=%s",
                        previous_trade_date,
                    )
                    self._run_mode(
                        "tail_research",
                        previous_trade_date,
                        previous_as_of,
                        external_push=False,
                    )
                if (
                    not self._state.is_done(previous_trade_date, "close")
                    or not self._state.is_done(
                        previous_trade_date,
                        "close_postprocess",
                    )
                ):
                    logger.warning(
                        "missed previous-day close window; running catch-up date=%s",
                        previous_trade_date,
                    )
                    self._maybe_run_close(
                        previous_trade_date,
                        previous_as_of,
                        external_push=False,
                        settlement_as_of=as_of,
                    )
                break

        # 非交易日跳过盘前/盘中/收盘（仍消费 queued 任务以消化积压）
        is_trading_day = _is_trading_day(datetime.combine(now.date(), datetime.min.time()))

        daily_check: dict[str, Any] = {}
        if _in_window(now, PREMARKET_WINDOW):
            # 盘前：先确保日线新鲜（force=True），再 premarket
            if is_trading_day:
                daily_check = self._freshness_checker.ensure_fresh(force=True)
                if not daily_check.get("error"):
                    self._settle_learning(
                        as_of=as_of,
                        trigger="premarket_freshness",
                    )
                self._maybe_run_premarket(trade_date, as_of, daily_check)
        elif _in_window(now, MONITOR_AM_WINDOW) or _in_window(now, MONITOR_PM_WINDOW):
            # 盘中：每 tick 触发 monitor（短线监控需要持续刷新行情）
            if is_trading_day:
                # monitor 不去重，每 tick 都跑（但记录最近触发时间）
                self._run_mode("monitor", trade_date, as_of, external_push=True)
                if _in_window(now, TAIL_RESEARCH_WINDOW):
                    self._maybe_run_tail_research(
                        trade_date,
                        as_of,
                        external_push=True,
                    )
        elif _in_window(now, CLOSE_WINDOW):
            # 收盘：触发一次 close
            if is_trading_day:
                self._maybe_run_close(trade_date, as_of)
        elif is_trading_day and (now.hour, now.minute) > (15, 30):
            logger.warning("missed close window; running same-day catch-up")
            self._maybe_run_tail_research(
                trade_date,
                as_of,
                external_push=False,
            )
            self._maybe_run_close(
                trade_date,
                as_of,
                external_push=False,
            )
            if now.hour >= 16:
                daily_check = self._freshness_checker.ensure_fresh(force=False)
                if not daily_check.get("skipped"):
                    self._state.mark_runtime(last_daily_check=daily_check)
                if daily_check.get("error"):
                    logger.error(
                        "收盘后日线更新失败，保持未新鲜状态: %s",
                        daily_check["error"],
                    )
                elif (
                    daily_check.get("updated") is True
                    or daily_check.get("reason") == "already_latest"
                ):
                    self._run_close_postprocessing(trade_date, as_of)
        else:
            # 深夜/非交易时段：低频检查日线新鲜度
            daily_check = self._freshness_checker.ensure_fresh(force=False)

        # 每 tick 都消费 queued 任务（即使在非交易时段也消化积压）
        consume_result = self._consumer.drain_once(as_of=as_of)
        if consume_result.get("fetched", 0) > 0:
            logger.info("queued 消费: %s", consume_result)

    # ------------------------------------------------------------------
    # 各 mode 调度
    # ------------------------------------------------------------------

    def _preflight_premarket_sources(self, trade_date: str) -> dict[str, Any]:
        """检查 premarket 实际使用的主题源，不依赖 discovery universe。"""
        try:
            from core.quant.theme.source_router import ThemeSourceRouter

            if self._theme_source_router is None:
                self._theme_source_router = ThemeSourceRouter(
                    enable_slow_scrapers=True,
                )
            snapshot = self._theme_source_router.fetch_concept_snapshot(trade_date)
        except Exception as exc:
            return {
                "ok": False,
                "reason": f"theme_source_unavailable: {type(exc).__name__}: {exc}",
                "snapshot_id": "",
                "source_as_of": "",
                "as_of": "",
                "member_count": 0,
                "board_count": 0,
            }

        boards = list(getattr(snapshot, "boards", []) or [])
        snapshot_id = str(getattr(snapshot, "snapshot_id", "") or "")
        source_as_of = str(
            getattr(snapshot, "source_as_of", "")
            or getattr(snapshot, "trade_date", "")
            or ""
        )[:10]
        unique_members = {
            str(symbol)
            for board in boards
            for symbol in (
                board.get("members", [])
                if isinstance(board, dict)
                else getattr(board, "members", [])
            ) or []
            if str(symbol)
        }
        base = {
            "snapshot_id": snapshot_id,
            "source_as_of": source_as_of,
            "as_of": source_as_of,
            "member_count": len(unique_members),
            "board_count": len(boards),
            "degraded_reasons": list(
                getattr(snapshot, "degraded_reasons", []) or []
            ),
        }

        if not boards or not snapshot_id:
            return {"ok": False, "reason": "no_theme_snapshot", **base}
        if not source_as_of:
            return {"ok": False, "reason": "snapshot_missing_as_of", **base}

        def _parse_date(value: str) -> datetime:
            compact = value.replace("-", "")[:8]
            return datetime.strptime(compact, "%Y%m%d")

        try:
            trade_dt = _parse_date(trade_date)
            source_dt = _parse_date(source_as_of)
        except ValueError:
            return {
                "ok": False,
                "reason": (
                    f"invalid_date_format: trade_date={trade_date} "
                    f"source_as_of={source_as_of}"
                ),
                **base,
            }

        age_days = _trading_day_age(source_dt, trade_dt)
        if age_days < 0:
            return {
                "ok": False,
                "reason": f"snapshot_from_future: source_as_of={source_as_of}",
                **base,
            }
        if age_days > PREMARKET_SNAPSHOT_MAX_AGE_DAYS:
            return {
                "ok": False,
                "reason": (
                    f"snapshot_stale: source_as_of={source_as_of} "
                    f"age_days={age_days} > max={PREMARKET_SNAPSHOT_MAX_AGE_DAYS}"
                ),
                **base,
            }
        if len(unique_members) < PREMARKET_SNAPSHOT_MIN_MEMBERS:
            return {
                "ok": False,
                "reason": (
                    f"insufficient_members: member_count={len(unique_members)} "
                    f"< min={PREMARKET_SNAPSHOT_MIN_MEMBERS}"
                ),
                **base,
            }
        return {"ok": True, "reason": "ok", **base}

    def _maybe_run_premarket(
        self,
        trade_date: str,
        as_of: str,
        daily_check: dict[str, Any],
    ) -> None:
        """盘前 mode 调度：当日只触发一次 premarket + push。

        源预检失败时返回但不 mark_done，下个 tick 在窗口内重试，
        避免产出必然为零的正式漏斗。重试在调度层完成，不给每个 provider 造重试框架。
        """
        if self._state.is_done(trade_date, "premarket"):
            if not self._state.is_done(trade_date, "push"):
                self._maybe_push_premarket(trade_date, as_of)
            logger.info("当日 premarket 已完成，跳过")
            return

        # 数据过期保护：日线未更新成功则不推 premarket，避免用过期数据生成报告
        if daily_check.get("updated") is False and daily_check.get("error"):
            self._state.mark_attempt(
                trade_date,
                "premarket",
                status="blocked",
                reason=str(daily_check.get("error") or ""),
            )
            logger.error(
                "日线数据更新失败 (%s)，跳过 premarket 触发，避免推送过期数据",
                daily_check.get("error"),
            )
            return

        # 源预检：主题 snapshot 必须新鲜且成员数达标，否则延迟重跑
        preflight = self._preflight_premarket_sources(trade_date)
        if not preflight.get("ok"):
            self._state.mark_attempt(
                trade_date,
                "premarket",
                status="blocked",
                reason=str(preflight.get("reason") or ""),
            )
            logger.warning(
                "premarket 源预检未通过 (%s)，保留未完成状态以便窗口内重试",
                preflight.get("reason"),
            )
            return

        result = self._monitor.run(
            mode="premarket",
            as_of=as_of,
            external_push=False,
            fail_on_empty=False,
            fail_on_degraded=False,
            max_research_tasks=self._max_research_tasks,
        )
        logger.info(
            "premarket 完成 status=%s themes=%d ready_watch=%d errors=%d",
            result.status,
            len(result.payload.get("themes", [])),
            result.payload.get("ready_watch_count", 0),
            len(result.errors),
        )
        if result.status == "failed":
            self._state.mark_attempt(
                trade_date,
                "premarket",
                status="failed",
                reason="; ".join(str(item) for item in result.errors),
            )
            logger.error("premarket 执行失败，保留未完成状态以便窗口内重试")
            return
        if result.status in {"success", "partial"}:
            self._capture_learning_observations(
                list(result.payload.get("observations") or []),
                as_of=str(getattr(result, "as_of", "") or as_of),
            )
        self._state.mark_done(trade_date, "premarket")

        # 推送（premarket 成功或 partial 都允许推送，failed 跳过）
        if result.status in {"success", "partial", "empty"}:
            self._maybe_push_premarket(trade_date, as_of)

    def _maybe_push_premarket(self, trade_date: str, as_of: str) -> None:
        """盘前 dry-run 成功后触发外部推送（当日只推一次）。"""
        if self._state.is_done(trade_date, "push"):
            return
        try:
            result = self._monitor.run(
                mode="push",
                as_of=as_of,
                external_push=True,
            )
        except Exception as exc:
            self._state.mark_attempt(
                trade_date,
                "push",
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
            logger.error(
                "push 执行异常，保留未完成状态以便窗口内重试: %s",
                exc,
            )
            return
        push = result.payload.get("push", {})
        logger.info(
            "push 完成 status=%s sent=%s",
            result.status,
            push.get("sent", False),
        )
        completed = (
            result.status != "failed"
            and (
                bool(push.get("sent"))
                or push.get("reason") == "dedup_skipped"
            )
        )
        if not completed:
            self._state.mark_attempt(
                trade_date,
                "push",
                status="failed",
                reason=str(push.get("reason") or result.status),
            )
            logger.error(
                "push 未完成，保留未完成状态以便窗口内重试: %s",
                push.get("reason") or result.status,
            )
            return
        self._state.mark_done(trade_date, "push")

    def _maybe_run_tail_research(
        self,
        trade_date: str,
        as_of: str,
        *,
        external_push: bool,
    ) -> None:
        """尾盘报告完成与外部送达分别去重和重试。"""
        report_done = self._state.is_done(trade_date, "tail_research")
        delivery_done = self._state.is_done(
            trade_date,
            "tail_research_delivery",
        )
        if report_done and (delivery_done or not external_push):
            return
        self._run_mode(
            "tail_research",
            trade_date,
            as_of,
            external_push=external_push,
        )

    def _maybe_run_close(
        self,
        trade_date: str,
        as_of: str,
        *,
        external_push: bool = True,
        settlement_as_of: str | None = None,
    ) -> None:
        """收盘报告、通知送达和数据学习后处理各自可恢复。"""
        report_done = self._state.is_done(trade_date, "close")
        postprocess_done = self._state.is_done(
            trade_date,
            "close_postprocess",
        )
        delivery_done = self._state.is_done(trade_date, "close_delivery")
        if report_done and not postprocess_done:
            self._run_close_postprocessing(
                trade_date,
                settlement_as_of or as_of,
            )
        if report_done and (delivery_done or not external_push):
            return
        self._run_mode(
            "close",
            trade_date,
            as_of,
            external_push=external_push,
            settlement_as_of=settlement_as_of,
        )

    def _run_mode(
        self,
        mode: str,
        trade_date: str,
        as_of: str,
        *,
        external_push: bool,
        settlement_as_of: str | None = None,
    ) -> None:
        """触发 ResearchMonitor.run(mode=...) 并记录状态。"""
        result = self._monitor.run(
            mode=mode,
            as_of=as_of,
            external_push=external_push,
            fail_on_empty=False,
            fail_on_degraded=False,
            max_research_tasks=self._max_research_tasks,
        )
        logger.info(
            "%s 完成 status=%s errors=%d",
            mode, result.status, len(result.errors),
        )
        if result.status == "failed":
            self._state.mark_attempt(
                trade_date,
                mode,
                status="failed",
                reason="; ".join(str(item) for item in result.errors),
            )
            logger.error("%s 执行失败，保留未完成状态以便窗口内重试", mode)
            return
        self._state.mark_done(trade_date, mode)
        if (
            mode == "close"
            and not self._state.is_done(trade_date, "close_postprocess")
        ):
            self._run_close_postprocessing(
                trade_date,
                settlement_as_of or as_of,
            )
        if external_push and mode in {"tail_research", "close"}:
            push = result.payload.get("push", {})
            push_completed = bool(push.get("sent")) or push.get("reason") == "dedup_skipped"
            if not push_completed:
                self._state.mark_attempt(
                    trade_date,
                    f"{mode}_delivery",
                    status="failed",
                    reason=str(push.get("reason") or "unknown"),
                )
                logger.error(
                    "%s 报告已完成但推送未完成，送达状态将独立重试: %s",
                    mode,
                    push.get("reason") or "unknown",
                )
                return
            self._state.mark_done(trade_date, f"{mode}_delivery")

    def _run_close_postprocessing(
        self,
        trade_date: str,
        as_of: str,
    ) -> None:
        """幂等完成 P3、universe snapshot 与到期预测结算。"""
        learning_status = (
            self._state.snapshot().get("learning_status", {}).get(
                "settlement", {}
            )
        )
        if (
            self._state.is_done(trade_date, "learning_settlement")
            and int(learning_status.get("pending") or 0) > 0
        ):
            logger.warning(
                "invalidating stale learning settlement: trade_date=%s pending=%s",
                trade_date,
                learning_status.get("pending"),
            )
            self._state.clear_done(trade_date, "learning_settlement")
            self._state.clear_done(trade_date, "close_postprocess")

        if not self._state.is_done(trade_date, "p3_forward"):
            forward_result = self._run_p3_forward_hook(trade_date) or {}
            if forward_result.get("hook_ok"):
                self._state.mark_done(trade_date, "p3_forward")

        if not self._state.is_done(trade_date, "daily_research_build"):
            build_result = self._run_daily_research_build(trade_date) or {}
            if build_result.get("status") in {"ok", "skipped"}:
                self._state.mark_done(
                    trade_date,
                    "daily_research_build",
                )

        if not self._state.is_done(trade_date, "learning_settlement"):
            settlement = self._settle_learning(
                as_of=as_of,
                trigger="close",
            )
            if (
                settlement.get("status") == "ok"
                and int(settlement.get("pending") or 0) == 0
            ):
                self._state.mark_done(
                    trade_date,
                    "learning_settlement",
                )

        phases = (
            "p3_forward",
            "daily_research_build",
            "learning_settlement",
        )
        if all(self._state.is_done(trade_date, phase) for phase in phases):
            self._state.mark_done(trade_date, "close_postprocess")

    def _run_daily_research_build(self, trade_date: str) -> dict[str, Any]:
        """收盘后构建 universe snapshot。

        earnings 与 variant perception 必须等待真实盈利证据和 P1 经营模型，
        不从主题成员关系派生代理数据。
        """
        try:
            from core.research.discovery.daily_build import (
                build_and_save_universe_snapshot,
            )
            snap_result = build_and_save_universe_snapshot(trade_date)
            logger.info(
                "daily_research_build[universe]: trade_date=%s status=%s members=%d reason=%s",
                trade_date,
                snap_result.get("status"),
                snap_result.get("member_count", 0),
                snap_result.get("reason"),
            )
            return dict(snap_result)
        except Exception as exc:  # noqa: BLE001 — 防御性：universe 失败不阻断后续
            logger.error("daily_research_build[universe] 异常: %s", exc)
            return {
                "status": "failed",
                "member_count": 0,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    def _run_p3_forward_hook(
        self,
        trigger_trade_date: str,
    ) -> dict[str, Any]:
        """幂等追加 forward 证据，并记录实际 observation 交易日。"""
        try:
            forward_result = append_p3_forward(P3_FORWARD_ROOT)
        except Exception as exc:
            forward_result = {
                "status": "hook_failed",
                "hook_ok": False,
                "observation_trade_date": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        self._state.mark_forward_hook(trigger_trade_date, forward_result)
        if not forward_result.get("hook_ok"):
            logger.error(
                "P3 forward append 失败；close 仍独立完成，认证证据保持未完成: %s",
                forward_result.get("error") or forward_result.get("status"),
            )
        return forward_result

    def _ensure_learning_components(self) -> None:
        if self._learning_bridge is None:
            from core.research.control_plane.learning_bridge import (
                DailyLearningBridge,
            )
            from core.research.control_plane.learning_loop import (
                ProductionLearningLoop,
            )
            from core.research.control_plane.repository import ResearchRepository

            self._learning_bridge = DailyLearningBridge(
                ProductionLearningLoop(ResearchRepository())
            )
        if self._learning_bar_reader is None:
            from core.research.control_plane.learning_bar_reader import (
                ForwardObservationBarReader,
            )

            self._learning_bar_reader = ForwardObservationBarReader(
                P3_FORWARD_ROOT
            )

    @staticmethod
    def _learning_model_version(
        observations: list[dict[str, Any]],
    ) -> str:
        versions = sorted(
            {
                str(row.get("strategy_version") or "").strip()
                for row in observations
                if str(row.get("status") or "").upper() in {"READY", "WATCH"}
                and row.get("research_validated")
                and (
                    row.get("evidence_refs")
                    or row.get("claim_ids")
                    or row.get("source_snapshot_id")
                )
                and str(row.get("strategy_version") or "").strip()
            }
        )
        if not versions:
            raise ValueError("no evidenced READY/WATCH strategy_version")
        digest = hashlib.sha256(
            json.dumps(versions, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return f"observations-{digest[:16]}"

    @staticmethod
    def _next_learning_due_at(as_of: str) -> str:
        generated_at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            raise ValueError("learning as_of must include timezone")
        for offset in range(1, 16):
            candidate = generated_at.date() + timedelta(days=offset)
            if _is_trading_day(datetime.combine(candidate, clock_time.min)):
                return datetime.combine(
                    candidate,
                    clock_time(16, 0),
                    tzinfo=generated_at.tzinfo,
                ).isoformat()
        raise RuntimeError("no trading day found in next 15 calendar days")

    def _capture_learning_observations(
        self,
        observations: list[dict[str, Any]],
        *,
        as_of: str,
    ) -> dict[str, Any]:
        try:
            self._ensure_learning_components()
            model_version = self._learning_model_version(observations)
            due_at = self._next_learning_due_at(as_of)
            capture = self._learning_bridge.capture_observations(
                observations,
                as_of=as_of,
                due_at=due_at,
                model_version=model_version,
                policy_version=LEARNING_POLICY_VERSION,
            )
            result = {
                "status": "ok",
                "as_of": as_of,
                "due_at": due_at,
                "model_version": model_version,
                **asdict(capture),
            }
        except Exception as exc:
            result = {
                "status": "error",
                "as_of": as_of,
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.error("shadow learning capture 降级: %s", result["error"])
        self._state.mark_learning_status("capture", result)
        return result

    def _settle_learning(
        self,
        *,
        as_of: str,
        trigger: str,
    ) -> dict[str, Any]:
        try:
            self._ensure_learning_components()
            settlement = self._learning_bridge.settle_after_close(
                as_of=as_of,
                bar_reader=self._learning_bar_reader,
            )
            result = {
                "status": "ok",
                "trigger": trigger,
                "as_of": as_of,
                **asdict(settlement),
            }
            result["operational"] = self._learning_bridge.status(
                as_of,
                settlement=settlement,
            )
            calibration_id = (
                settlement.shadow_calibration_id
                or result["operational"].get("latest_shadow_run_id", "")
            )
            result["activation"] = _promote_learning_calibration(
                self._learning_bridge.loop,
                calibration_id,
            )
        except Exception as exc:
            result = {
                "status": "error",
                "trigger": trigger,
                "as_of": as_of,
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.error("shadow learning settlement 降级: %s", result["error"])
        self._state.mark_learning_status("settlement", result)
        return result


def _setup_logging(verbose: bool) -> None:
    """配置日志格式与级别。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="AquaTrade ResearchMonitor 持续运行 daemon")
    parser.add_argument(
        "--output-dir",
        default=os.getenv("RESEARCH_MONITOR_OUTPUT_DIR", "data/reports/research_monitor"),
        help="监控输出目录（默认从 RESEARCH_MONITOR_OUTPUT_DIR 环境变量读取）",
    )
    parser.add_argument(
        "--tick-interval",
        type=int,
        default=300,
        help="主循环 tick 间隔秒数（默认 300，最小 30）",
    )
    parser.add_argument(
        "--research-workers",
        type=int,
        default=8,
        help="queued 研究任务并发 worker 数（默认 8，八核 CPU 推荐 8）",
    )
    parser.add_argument(
        "--research-batch-size",
        type=int,
        default=30,
        help="每 tick 最多消费的研究任务数（默认 30）",
    )
    parser.add_argument(
        "--max-research-tasks",
        type=int,
        default=6,
        help="premarket 内部 _execute_research_tasks 上限（默认 6）",
    )
    parser.add_argument(
        "--state-file",
        default="",
        help="daemon 状态文件路径（默认 data/state/research_monitor_daemon.json）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="启用 DEBUG 级别日志",
    )
    return parser


def _bootstrap_p3_model_registry() -> None:
    """启动前确保 P3 初始模型族已登记到 p3_model_registry.json。

    按 P3 计划 P3.1 第 2 条「先登记模型/数据/实验版本，再开始实现或运行」。
    幂等：已存在的 (model_id, version) 跳过；失败时仅记录日志，不阻断启动。
    """
    try:
        from core.research.model_governance.bootstrap import (
            ensure_initial_models_registered,
        )
        from core.research.model_governance.registry import ModelRegistry

        ensure_initial_models_registered(
            ModelRegistry(),
            policy_version=LEARNING_POLICY_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 - 防御性：注册失败不阻断 daemon 启动
        logger.warning("P3 初始模型注册失败（不阻断启动）: %s: %s", type(exc).__name__, exc)


def main() -> int:
    """daemon 入口。"""
    args = build_parser().parse_args()
    _setup_logging(args.verbose)

    # P3.1：先登记模型再启动 daemon，使工作台 model_registry_summary 非空
    _bootstrap_p3_model_registry()

    state_file = Path(args.state_file) if args.state_file else None
    daemon = ResearchMonitorDaemon(
        output_dir=args.output_dir,
        tick_interval=args.tick_interval,
        research_workers=args.research_workers,
        research_batch_size=args.research_batch_size,
        max_research_tasks=args.max_research_tasks,
        state_file=state_file,
    )
    daemon.install_signal_handlers()
    return daemon.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
