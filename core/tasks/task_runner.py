"""任务执行器

使用 ThreadPoolExecutor 管理异步任务的提交、执行和取消。
支持：
- 任务提交与后台执行
- 任务状态查询
- 排队中任务取消（运行中任务不支持取消）
- 重型任务互斥（同一时刻仅允许一个重型任务运行）
- 任务风暴防护（防抖：短时间内重复提交同类型任务会被拒绝）
- 执行日志记录到 data/tasks/logs/{task_id}.log
"""

from __future__ import annotations

import io
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from config.logger import get_logger
from core.tasks.models import Task
from core.tasks.task_store import TaskStore
from core.tasks.task_registry import TaskRegistry

logger = get_logger(__name__)

# 防抖窗口：同一 task_type 在此时间窗口内不允许重复提交（秒）
_DEBOUNCE_SECONDS = 5


class TaskRunner:
    """任务执行器

    通过 ThreadPoolExecutor 管理异步任务生命周期。
    单例模式，全局共享一个线程池。
    """

    _instance: Optional[TaskRunner] = None
    _init_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> TaskRunner:
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, max_workers: int = 2) -> None:
        """初始化任务执行器

        Args:
            max_workers: 线程池最大工作线程数
        """
        # 避免重复初始化
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="task")
        self._store = TaskStore()
        self._futures: dict[str, Future] = {}
        self._running_tasks: set[str] = set()
        self._running_types: dict[str, str] = {}  # task_id -> task_type
        self._lock = threading.Lock()
        # 防抖记录: task_type -> 最近一次提交时间戳
        self._last_submit_time: dict[str, float] = {}

        # 注册任务执行函数
        self._executors: dict[str, Callable[..., dict[str, Any]]] = {
            "run_perilla_topic_fast": self._execute_perilla_topic_fast,
            "run_perilla_batch_fast": self._execute_perilla_batch_fast,
            "run_daily_brief": self._execute_daily_brief,
            "refresh_watchlist": self._execute_refresh_watchlist,
            "generate_single_stock_evidence_pack": self._execute_single_stock_evidence,
        }

    def _execute_perilla_topic_fast(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行紫苏叶主题快速扫描

        Args:
            params: 任务参数，需包含 topic 字段

        Returns:
            包含 status 和 report_paths 的字典
        """
        from core.research.perilla.discovery.autonomous_perilla_engine import AutonomousPerillaEngine

        topic = params.get("topic", "")
        engine = AutonomousPerillaEngine(
            mode="topic",
            topic=topic,
            no_llm=params.get("no_llm", True),
            output_dir=params.get("output_dir", "data/reports/perilla_discovery"),
        )
        report = engine.run()
        report_paths = []
        if hasattr(report, "report_path") and report.report_path:
            report_paths.append(report.report_path)
        return {"status": "success", "report_paths": report_paths}

    def _execute_perilla_batch_fast(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行紫苏叶批量快速扫描

        Args:
            params: 任务参数

        Returns:
            包含 status 和 report_paths 的字典
        """
        from core.research.perilla.discovery.autonomous_perilla_engine import AutonomousPerillaEngine

        engine = AutonomousPerillaEngine(
            mode="auto",
            no_llm=params.get("no_llm", True),
            output_dir=params.get("output_dir", "data/reports/perilla_discovery"),
        )
        report = engine.run()
        report_paths = []
        if hasattr(report, "report_path") and report.report_path:
            report_paths.append(report.report_path)
        return {"status": "success", "report_paths": report_paths}

    def _execute_daily_brief(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行日报生成

        流程:
        1. 运行 PerillaKnowledgeLoopEngine 生成今日新词雷达 + 证据边图谱
        2. 运行 ResearchEngine.run_daily() 生成研究工作台日报
        3. 合并两者为 data/reports/daily_brief.md
        4. 触发 notify_daily_brief_ready() 推送通知

        Args:
            params: 任务参数，可选 date、symbols、skip_perilla、skip_research

        Returns:
            包含 status、result、report_path 的字典
        """
        from datetime import datetime
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent.parent
        reports_dir = project_root / "data" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        perilla_dir = reports_dir / "perilla_discovery"
        perilla_dir.mkdir(parents=True, exist_ok=True)

        trade_date = params.get("date") or datetime.now().strftime("%Y-%m-%d")
        skip_perilla = bool(params.get("skip_perilla", False))
        skip_research = bool(params.get("skip_research", False))

        perilla_summary: str = ""
        perilla_selected: dict[str, Any] = {}
        perilla_stats: dict[str, Any] = {}
        if not skip_perilla:
            try:
                from core.research.perilla.discovery.knowledge_loop_engine import (
                    PerillaKnowledgeLoopEngine,
                )

                engine = PerillaKnowledgeLoopEngine(
                    project_root=project_root,
                    output_dir=perilla_dir,
                    storage_path=str(perilla_dir / "sag.json"),
                    max_execute_tasks=int(params.get("max_execute_tasks", 3)),
                    use_llm_explanations=bool(params.get("use_llm", True)),
                    max_llm_terms=int(params.get("max_llm_terms", 5)),
                )
                loop_result = engine.run(execute_search=bool(params.get("execute_search", True)))
                perilla_summary = loop_result.summary_text or ""
                perilla_selected = loop_result.selected_new_term or {}
                perilla_stats = {
                    "raw_documents": len(loop_result.raw_documents),
                    "raw_terms": len(loop_result.raw_terms),
                    "candidate_concepts": len(loop_result.candidate_concepts),
                    "evidence_edges": len(loop_result.evidence_edges),
                    "search_tasks": len(loop_result.search_tasks),
                    "task_runs": len(loop_result.task_runs),
                    "sag_ingested": loop_result.sag_stats.get("ingested", 0),
                    "sag_skipped": loop_result.sag_stats.get("skipped", 0),
                    "promotion_candidates": len(loop_result.promotion_candidates),
                }
                logger.info(
                    "Perilla loop done: selected=%s, stats=%s",
                    perilla_selected.get("normalized_name", ""),
                    perilla_stats,
                )
            except Exception as exc:
                logger.error("Perilla loop failed: %s", exc, exc_info=True)
                perilla_summary = f"## 今日新词雷达\n\nPerilla 引擎运行失败: {exc}\n"

        research_result: dict[str, Any] = {}
        if not skip_research:
            try:
                from core.research.engine.research_engine import ResearchEngine

                engine = ResearchEngine()
                research_result = engine.run_daily(
                    trade_date=trade_date,
                    symbols=params.get("symbols"),
                )
            except Exception as exc:
                logger.error("ResearchEngine daily failed: %s", exc, exc_info=True)
                research_result = {"success": False, "errors": [str(exc)]}

        # 合并日报
        brief_path = reports_dir / "daily_brief.md"
        sections: list[str] = [
            f"# AquaTrader 每日简报 {trade_date}",
            "",
            f"生成时间: {datetime.now().isoformat()}",
            "",
        ]
        if perilla_summary:
            sections.append(perilla_summary.strip())
            sections.append("")
        if perilla_selected:
            sections.append("## 今日冷门新词推送")
            sections.append(
                f"- 新词: {perilla_selected.get('normalized_name', '')}"
                f" | 主线: {perilla_selected.get('root_theme', '')}"
                f" | 类型: {perilla_selected.get('term_type', '')}"
                f" | 证据: {perilla_selected.get('evidence_level', '')}"
            )
            co_terms = perilla_selected.get("co_terms", []) or []
            if co_terms:
                sections.append(f"- 共现: {', '.join(co_terms[:8])}")
            companies = perilla_selected.get("related_companies", []) or []
            if companies:
                sections.append(f"- 相关公司: {', '.join(companies[:5])}")
            sections.append("")
        if perilla_stats:
            sections.append("## 知识闭环统计")
            for key, value in perilla_stats.items():
                sections.append(f"- {key}: {value}")
            sections.append("")
        if research_result:
            sections.append("## ResearchEngine 日报")
            sections.append(f"- success: {research_result.get('success', False)}")
            sections.append(f"- trade_date: {research_result.get('trade_date', trade_date)}")
            sections.append(f"- elapsed: {research_result.get('elapsed_seconds', 0)}s")
            sections.append(f"- evidence_count: {research_result.get('evidence_count', 0)}")
            sections.append(f"- entity_count: {research_result.get('entity_count', 0)}")
            sections.append(f"- relation_count: {research_result.get('relation_count', 0)}")
            errors = research_result.get("errors", []) or []
            if errors:
                sections.append(f"- errors: {len(errors)}")
                for err in errors[:3]:
                    sections.append(f"  - {err}")
            sections.append("")
        sections.append("---")
        sections.append("说明: 本简报基于 AquaTrader 内部 raw_documents 和 ResearchEngine 数据源，不构成投资建议。")

        brief_path.write_text("\n".join(sections), encoding="utf-8")
        logger.info("Daily brief written: %s", brief_path)

        # 触发日报就绪通知（断点D修复）
        try:
            from server.services.notification_service import notify_daily_brief_ready

            notify_daily_brief_ready()
        except Exception as exc:
            logger.warning("notify_daily_brief_ready failed: %s", exc)

        return {
            "status": "success",
            "result": {
                "trade_date": trade_date,
                "perilla_selected": perilla_selected.get("normalized_name", ""),
                "perilla_stats": perilla_stats,
                "research_success": research_result.get("success", False),
            },
            "report_path": str(brief_path),
        }

    def _execute_refresh_watchlist(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行观察池刷新

        Args:
            params: 任务参数

        Returns:
            包含 status 和 result 的字典
        """
        from core.quant.portfolio.watchlist_manager import WatchlistManager

        manager = WatchlistManager()
        result = manager.check_all_signals()
        return {"status": "success", "result": result}

    def _execute_single_stock_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行单股证据包生成

        Args:
            params: 任务参数，需包含 symbol 字段

        Returns:
            包含 status、report_paths 和 result 的字典

        Raises:
            ValueError: symbol 参数为空时
        """
        from core.research.engine.research_engine import ResearchEngine

        symbol = params.get("symbol", "")
        if not symbol:
            raise ValueError("symbol 参数不能为空")

        engine = ResearchEngine()
        result = engine.run_single_stock(
            symbol=symbol,
            days=int(params.get("depth", 365)),
        )
        report_paths = []
        if isinstance(result, dict) and result.get("report_path"):
            report_paths.append(result["report_path"])
        return {"status": "success", "report_paths": report_paths, "result": result}

    def submit(self, task_type: str, params: dict[str, Any], source: str, *, skip_debounce: bool = False) -> str:
        """提交一个异步任务

        Args:
            task_type: 任务类型
            params: 任务参数
            source: 任务来源
            skip_debounce: 跳过防抖检查（用于确认流程等内部调用）

        Returns:
            task_id: 任务唯一标识

        Raises:
            ValueError: 任务类型未知、参数校验失败或防抖拒绝
            RuntimeError: 重型任务并发冲突
        """
        # 1. 校验任务类型
        if task_type not in self._executors:
            raise ValueError(f"未知的任务类型: {task_type}")

        # 2. 参数校验
        valid, err_msg = TaskRegistry.validate_params(task_type, params)
        if not valid:
            raise ValueError(err_msg)

        # 3. 防抖检查（确认流程可跳过）
        if not skip_debounce:
            now = time.time()
            last_submit = self._last_submit_time.get(task_type, 0)
            if now - last_submit < _DEBOUNCE_SECONDS:
                raise ValueError(
                    f"任务 {task_type} 提交过于频繁，请 {_DEBOUNCE_SECONDS - int(now - last_submit)} 秒后重试"
                )
            self._last_submit_time[task_type] = now

        # 4. 重型任务互斥检查
        if TaskRegistry.is_heavy_task(task_type) and self.is_heavy_task_running():
            raise RuntimeError("当前有重型任务正在执行，请稍后再试")

        # 5. 创建 Task 对象
        task_id = uuid.uuid4().hex[:12]
        now_iso = datetime.now(timezone.utc).isoformat()
        task = Task(
            task_id=task_id,
            task_type=task_type,
            status="queued",
            params=params,
            source=source,
            created_at=now_iso,
        )
        self._store.save(task)

        # 6. 提交到线程池
        future = self._executor.submit(self._run_task, task)
        with self._lock:
            self._futures[task_id] = future

        logger.info("任务已提交: task_id=%s, type=%s, source=%s", task_id, task_type, source)
        return task_id

    def _run_task(self, task: Task) -> None:
        """在线程池中执行任务

        Args:
            task: 待执行的 Task 实例
        """
        task_id = task.task_id
        task_type = task.task_type
        log_path = str(self._store._logs_dir / f"{task_id}.log")

        # 更新状态为 running
        now_iso = datetime.now(timezone.utc).isoformat()
        self._store.update(
            task_id,
            status="running",
            started_at=now_iso,
            log_path=log_path,
            progress={"percent": 0, "message": "任务开始执行"},
        )
        with self._lock:
            self._running_tasks.add(task_id)
            self._running_types[task_id] = task_type

        start_ms = time.time()

        # 捕获 stdout/stderr 到日志文件
        log_buffer = io.StringIO()
        try:
            with redirect_stdout(log_buffer), redirect_stderr(log_buffer):
                executor_fn = self._executors.get(task_type)
                if executor_fn is None:
                    raise ValueError(f"无执行函数: {task_type}")

                result = executor_fn(task.params)

            # 提取报告路径
            report_paths = result.get("report_paths", []) if isinstance(result, dict) else []

            elapsed_ms = int((time.time() - start_ms) * 1000)
            now_iso = datetime.now(timezone.utc).isoformat()

            self._store.update(
                task_id,
                status="success",
                finished_at=now_iso,
                elapsed_ms=elapsed_ms,
                report_paths=report_paths,
                progress={"percent": 100, "message": "任务完成"},
            )
            logger.info("任务完成: task_id=%s, elapsed=%dms", task_id, elapsed_ms)

        except Exception as exc:
            elapsed_ms = int((time.time() - start_ms) * 1000)
            now_iso = datetime.now(timezone.utc).isoformat()

            self._store.update(
                task_id,
                status="failed",
                finished_at=now_iso,
                elapsed_ms=elapsed_ms,
                error={"code": "TASK_EXECUTION_ERROR", "message": str(exc)},
                progress={"percent": 0, "message": f"任务失败: {exc}"},
            )
            logger.error("任务失败: task_id=%s, error=%s", task_id, exc)

        finally:
            # 写入日志文件
            try:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(log_buffer.getvalue())
            except OSError as exc:
                logger.warning("写入任务日志 %s 失败: %s", log_path, exc)

            with self._lock:
                self._running_tasks.discard(task_id)
                self._running_types.pop(task_id, None)
                self._futures.pop(task_id, None)

            # 触发通知
            self._notify_completion(task_id)

    def _notify_completion(self, task_id: str) -> None:
        """任务完成后的通知回调

        根据任务状态调用对应的通知服务方法。

        Args:
            task_id: 完成的任务 ID
        """
        try:
            task = self._store.get(task_id)
            if task is None:
                return

            from server.services.notification_service import (
                notify_task_succeeded,
                notify_task_failed,
            )

            if task.status == "success":
                notify_task_succeeded(task_id, task.task_type, task.elapsed_ms or 0)
            elif task.status == "failed":
                error_msg = task.error.get("message", "未知错误") if task.error else "未知错误"
                notify_task_failed(task_id, task.task_type, error_msg)
        except Exception as exc:
            logger.warning("任务完成通知发送失败: task_id=%s, error=%s", task_id, exc)

    def get_status(self, task_id: str) -> Optional[dict[str, Any]]:
        """获取任务状态

        Args:
            task_id: 任务唯一标识

        Returns:
            任务状态字典，不存在时返回 None
        """
        task = self._store.get(task_id)
        if task is None:
            return None
        return task.to_dict()

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """取消任务

        仅支持取消状态为 "queued" 的任务。
        运行中的任务不支持取消（返回 unsupported 提示）。

        Args:
            task_id: 任务唯一标识

        Returns:
            (是否成功, 消息)
        """
        task = self._store.get(task_id)
        if task is None:
            return False, f"任务不存在: {task_id}"

        if task.status == "queued":
            # 尝试从线程池取消
            with self._lock:
                future = self._futures.get(task_id)
                if future and future.cancel():
                    self._futures.pop(task_id, None)

            now_iso = datetime.now(timezone.utc).isoformat()
            self._store.update(
                task_id,
                status="cancelled",
                finished_at=now_iso,
                progress={"percent": 0, "message": "任务已取消"},
            )
            return True, "任务已取消"

        if task.status == "running":
            return False, "运行中的任务不支持取消"

        return False, f"任务当前状态为 {task.status}，无法取消"

    def list_running_tasks(self) -> list[str]:
        """获取当前正在运行的任务 ID 列表

        Returns:
            运行中任务的 task_id 列表
        """
        with self._lock:
            return list(self._running_tasks)

    def is_heavy_task_running(self) -> bool:
        """检查是否有重型任务正在运行

        Returns:
            True 表示当前有重型任务在运行
        """
        with self._lock:
            for tid in self._running_tasks:
                ttype = self._running_types.get(tid, "")
                if TaskRegistry.is_heavy_task(ttype):
                    return True
        return False


def get_task_runner() -> TaskRunner:
    """获取 TaskRunner 单例

    Returns:
        TaskRunner 实例
    """
    return TaskRunner()
