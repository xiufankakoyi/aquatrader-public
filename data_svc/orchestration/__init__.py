"""Data update orchestration for LanceDB-backed refreshes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Dict, Optional

from config.logger import get_logger

logger = get_logger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    DEGRADED = "degraded"
    SKIPPED = "skipped"
    FAILED = "failed"


class PhaseStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


@dataclass
class TaskResult:
    task_name: str
    status: TaskStatus
    message: str = ""
    rows_written: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class PhaseResult:
    phase: str
    status: PhaseStatus = PhaseStatus.PENDING
    task_results: Dict[str, TaskResult] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def all_success(self) -> bool:
        return all(
            r.status in (TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.DEGRADED)
            for r in self.task_results.values()
        )

    @property
    def all_critical_success(self) -> bool:
        return self.task_results.get("stock_daily", TaskResult("stock_daily", TaskStatus.FAILED)).status == TaskStatus.SUCCESS


@dataclass
class OrchestratorResult:
    target_date: str
    phase1: PhaseResult = field(default_factory=lambda: PhaseResult(phase="ingestion"))
    phase2: PhaseResult = field(default_factory=lambda: PhaseResult(phase="factor"))
    total_duration_ms: float = 0.0

    @property
    def success(self) -> bool:
        return self.phase1.all_critical_success and self.phase2.all_success


class DataOrchestrator:
    """Coordinates the full update pipeline into LanceDB."""

    def __init__(self, progress_callback: Optional[Callable[[str, float, str], None]] = None):
        self.progress_callback = progress_callback
        self._task_results: Dict[str, TaskResult] = {}

    def run(
        self,
        target_date: Optional[str] = None,
        skip_crawler: bool = False,
        skip_factors: bool = False,
    ) -> OrchestratorResult:
        started = time.time()
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        result = OrchestratorResult(target_date=target_date)
        self._report("STARTING", 0, f"starting update for {target_date}")

        result.phase1 = self._run_phase1(target_date, skip_crawler=skip_crawler)
        if result.phase1.all_critical_success and not skip_factors:
            result.phase2 = self._run_phase2(target_date)
        elif skip_factors:
            result.phase2 = PhaseResult(
                phase="factor",
                status=PhaseStatus.SUCCESS,
                task_results={"factors": TaskResult("factors", TaskStatus.SKIPPED, message="factor precompute skipped")},
            )
        else:
            result.phase2 = PhaseResult(
                phase="factor",
                status=PhaseStatus.FAILED,
                task_results={"factors": TaskResult("factors", TaskStatus.FAILED, message="skipped because stock_daily failed")},
            )

        result.total_duration_ms = (time.time() - started) * 1000
        self._report("COMPLETED" if result.success else "FAILED", 100, self._summarize(result))
        return result

    def _run_phase1(self, target_date: str, skip_crawler: bool) -> PhaseResult:
        started = time.time()
        phase = PhaseResult(phase="ingestion", status=PhaseStatus.RUNNING)
        tasks = [
            ("stock_daily", self._ingest_stock_daily),
            ("index_daily", self._ingest_index_daily),
            ("stock_info", self._ingest_stock_info),
        ]
        if not skip_crawler:
            tasks.append(("dragon_eye", self._ingest_dragon_eye))
        else:
            phase.task_results["dragon_eye"] = TaskResult("dragon_eye", TaskStatus.SKIPPED, message="crawler skipped")

        for index, (name, handler) in enumerate(tasks, 1):
            self._report("PHASE1", 10 + index * 10, f"running {name}")
            phase.task_results[name] = self._timed(name, handler, target_date)
            self._task_results[name] = phase.task_results[name]
            if name == "stock_daily" and phase.task_results[name].status == TaskStatus.FAILED:
                break

        phase.status = PhaseStatus.SUCCESS if phase.all_success else PhaseStatus.PARTIAL_SUCCESS
        if not phase.all_critical_success:
            phase.status = PhaseStatus.FAILED
        phase.duration_ms = (time.time() - started) * 1000
        return phase

    def _run_phase2(self, target_date: str) -> PhaseResult:
        started = time.time()
        phase = PhaseResult(phase="factor", status=PhaseStatus.RUNNING)
        self._report("PHASE2", 75, "running factor precompute")
        phase.task_results["factors"] = self._timed("factors", self._precompute_factors, target_date)
        self._task_results["factors"] = phase.task_results["factors"]
        phase.task_results["alpha_beta_factors"] = TaskResult(
            "alpha_beta_factors",
            TaskStatus.SKIPPED,
            message="included in full factor precompute",
        )
        phase.status = PhaseStatus.SUCCESS if phase.all_success else PhaseStatus.PARTIAL_SUCCESS
        phase.duration_ms = (time.time() - started) * 1000
        return phase

    def _timed(self, name: str, handler: Callable[[str], TaskResult], target_date: str) -> TaskResult:
        started = time.time()
        try:
            result = handler(target_date)
        except Exception as exc:
            logger.exception("[Orchestrator] task %s failed", name)
            result = TaskResult(name, TaskStatus.FAILED, error=str(exc), message=str(exc))
        result.duration_ms = (time.time() - started) * 1000
        return result

    def _ingest_stock_daily(self, target_date: str) -> TaskResult:
        from data_svc.storage.unified_updater import UnifiedDataUpdater

        ymd = target_date.replace("-", "")
        result = UnifiedDataUpdater().update_stock_daily(start_date=ymd, end_date=ymd)
        expected_dates = result.get("expected_dates", 0)
        updated_dates = result.get("dates_updated", 0)
        failed_dates = result.get("failed_dates", [])
        status = (
            TaskStatus.SUCCESS
            if expected_dates == updated_dates and not failed_dates
            else TaskStatus.FAILED
        )
        return TaskResult(
            "stock_daily",
            status,
            message=f"updated {result.get('dates_updated', 0)} days, {result.get('records_added', 0)} rows",
            rows_written=result.get("records_added", 0),
            metadata={
                "failed_dates": failed_dates,
                "invalid_rows": result.get("invalid_rows", 0),
                "missing_auxiliary": result.get("missing_auxiliary", {}),
            },
        )

    def _ingest_index_daily(self, target_date: str) -> TaskResult:
        from data_svc.storage.unified_updater import UnifiedDataUpdater

        ymd = target_date.replace("-", "")
        result = UnifiedDataUpdater().update_benchmark_daily(start_date=ymd, end_date=ymd)
        return TaskResult(
            "index_daily",
            TaskStatus.SUCCESS,
            message=f"updated {result.get('dates_updated', 0)} days, {result.get('records_added', 0)} rows",
            rows_written=result.get("records_added", 0),
        )

    def _ingest_stock_info(self, target_date: str) -> TaskResult:
        from data_svc.storage.unified_updater import UnifiedDataUpdater

        result = UnifiedDataUpdater().update_stock_basic()
        rows = result.get("records_updated", 0)
        return TaskResult(
            "stock_info",
            TaskStatus.SUCCESS,
            message=f"updated stock_info snapshot: {rows} rows",
            rows_written=rows,
        )

    def _ingest_dragon_eye(self, target_date: str) -> TaskResult:
        from data_svc.storage.unified_updater import UnifiedDataUpdater

        result = UnifiedDataUpdater().update_dragon_eye(target_date=target_date)
        rows = result.get("records_updated", 0)
        status = TaskStatus.SUCCESS if rows > 0 else TaskStatus.DEGRADED
        return TaskResult(
            "dragon_eye",
            status,
            message=f"ingested {rows} DragonEye rows" if rows > 0 else "no DragonEye rows ingested",
            rows_written=rows,
        )

    def _precompute_factors(self, target_date: str) -> TaskResult:
        from data_svc.storage.factor_precompute_service import FactorPrecomputeService

        result = FactorPrecomputeService().precompute_all_factors(start_date=target_date, end_date=target_date)
        if result.success:
            return TaskResult(
                "factors",
                TaskStatus.SUCCESS,
                message=result.message,
                rows_written=result.records_computed,
            )
        return TaskResult("factors", TaskStatus.FAILED, message=result.message, error=result.error)

    def _report(self, stage: str, progress: float, message: str):
        logger.info("[Orchestrator] %s %.1f%% %s", stage, progress, message)
        if self.progress_callback:
            self.progress_callback(stage, progress, message)

    def _summarize(self, result: OrchestratorResult) -> str:
        lines = [
            f"AquaTrade update {'succeeded' if result.success else 'failed'} for {result.target_date}",
            f"Total: {result.total_duration_ms / 1000:.1f}s",
            f"Phase1: {result.phase1.status.value} ({result.phase1.duration_ms / 1000:.1f}s)",
        ]
        for task in result.phase1.task_results.values():
            lines.append(f"  - {task.task_name}: {task.status.value}, rows={task.rows_written}, {task.message}")
        lines.append(f"Phase2: {result.phase2.status.value} ({result.phase2.duration_ms / 1000:.1f}s)")
        for task in result.phase2.task_results.values():
            lines.append(f"  - {task.task_name}: {task.status.value}, rows={task.rows_written}, {task.message}")
        return "\n".join(lines)


__all__ = [
    "DataOrchestrator",
    "OrchestratorResult",
    "PhaseResult",
    "PhaseStatus",
    "TaskResult",
    "TaskStatus",
]

