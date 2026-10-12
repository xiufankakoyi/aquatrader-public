"""自主研究引擎主循环：认知缺口驱动，替换 long_run_hypergraph.py 的主题驱动模式。

核心区别（对比旧 long_run_hypergraph.py）：
    旧：主题 → subprocess → 套模板扩展 → 搜索 → 生成更多主题 → FIFO
    新：统一记忆 → 识别缺口 → 紫苏叶评分 → Top-K 深挖 → 合并回记忆 → tick

关键特性：
    1. 进程内连续运行（不启动 subprocess，保留工作记忆）
    2. 所有主题共享同一 UnifiedResearchMemory（消除记忆孤岛）
    3. 研究动作可插拔（ResearchAction 协议）
    4. 预算约束（迭代次数、时间、搜索次数）
    5. 优雅停止（收到信号后完成当前任务）
    6. 每次迭代触发环境 tick（神经元网络自主学习）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.research_graph.cognitive_gap_scheduler import (
    CognitiveGapScheduler,
    ResearchTask,
)
from core.research_graph.unified_memory import UnifiedResearchMemory

logger = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    """单次研究动作的结果。

    Attributes:
        success: 是否成功。
        new_nodes: 新增节点名称列表。
        new_edges: 新增边 ID 列表。
        new_evidence: 新增证据 ID 列表。
        documents_recalled: 召回文档数。
        reason: 完成原因。
        source_status: 来源可用性，取值 "ok"/"degraded"/"unavailable"。
            "ok"=来源正常；"degraded"=外部 provider 不可用降级为仅本地；
            "unavailable"=无可用文档或 LLM 不可用。engine 据此累计来源统计。
    """

    success: bool = False
    new_nodes: list[str] = field(default_factory=list)
    new_edges: list[str] = field(default_factory=list)
    new_evidence: list[str] = field(default_factory=list)
    documents_recalled: int = 0
    reason: str = ""
    source_status: str = "ok"


class ResearchAction(Protocol):
    """研究动作协议：可插拔的研究执行器。"""

    def execute(
        self, task: ResearchTask, memory: UnifiedResearchMemory
    ) -> ResearchResult:
        """执行研究动作。

        Args:
            task: 研究任务。
            memory: 统一记忆层（可读写）。

        Returns:
            研究结果。
        """
        ...


@dataclass
class EngineBudget:
    """引擎预算约束。

    Attributes:
        max_iterations: 最大迭代次数（None 表示不限）。
        max_seconds: 最大运行秒数（None 表示不限）。
        tick_interval: 环境 tick 间隔（秒）。
        refill_interval: 重新扫描缺口的间隔（迭代次数）。
        max_iteration_seconds: 单次迭代 wall-clock 预算（秒，None 表示不限）。
            超时时 engine 不强制中断 action（Python 难以安全中断），
            但会把本次迭代标记为失败并记入 _total_iteration_timeouts，
            便于运维定位卡死的 ResearchAction。
    """

    max_iterations: int | None = None
    max_seconds: float | None = None
    tick_interval: float = 5.0
    refill_interval: int = 10
    max_iteration_seconds: float | None = None


class AutonomousResearchEngine:
    """自主研究引擎：认知缺口驱动的主循环。

    Args:
        memory: 统一研究记忆层。
        scheduler: 认知缺口调度器。
        research_action: 研究动作执行器。
        budget: 预算约束。
    """

    def __init__(
        self,
        memory: UnifiedResearchMemory,
        scheduler: CognitiveGapScheduler,
        research_action: ResearchAction,
        budget: EngineBudget | None = None,
    ) -> None:
        self._memory = memory
        self._scheduler = scheduler
        self._action = research_action
        self._budget = budget or EngineBudget()

        self._stop_requested = False
        self._iteration = 0
        self._start_time = 0.0
        self._last_tick = 0.0

        # 统计
        self._total_researched = 0
        self._total_succeeded = 0
        self._total_failed = 0
        self._total_nodes_added = 0
        self._total_edges_added = 0
        self._total_evidence_added = 0
        self._total_docs_recalled = 0
        self._total_ticks = 0
        # 修复 1：来源状态统计（ok/degraded/unavailable 各自计数）
        self._total_source_ok = 0
        self._total_source_degraded = 0
        self._total_source_unavailable = 0
        # 修复 2：engine 层独立校验出的"空转"次数（success=True 但 0 产出）
        self._total_idle = 0
        # 修复 2.2：按 source_status 细分空转原因
        # - unavailable: 源不可用导致空转（应冷却重试，等源恢复）
        # - degraded: 源降级导致空转（应冷却重试）
        # - no_progress: 源正常但无新知识（应标记完成不重试，避免浪费预算）
        self._total_idle_unavailable = 0
        self._total_idle_degraded = 0
        self._total_idle_no_progress = 0
        # 修复 4：单次迭代耗时统计（用于 snapshot 报告 budget 消耗）
        self._total_iteration_timeouts = 0
        self._iteration_elapsed_total = 0.0
        self._iteration_elapsed_max = 0.0

    def run(
        self,
        max_iterations: int | None = None,
        max_seconds: float | None = None,
    ) -> dict[str, Any]:
        """阻塞运行主循环。

        Args:
            max_iterations: 覆盖预算的最大迭代次数。
            max_seconds: 覆盖预算的最大运行秒数。

        Returns:
            运行结束时的快照。
        """
        max_iter = max_iterations or self._budget.max_iterations
        max_sec = max_seconds or self._budget.max_seconds

        self._start_time = time.time()
        self._stop_requested = False
        logger.info(
            "自主研究引擎启动 (max_iter=%s, max_sec=%s)",
            max_iter, max_sec,
        )

        while not self._stop_requested:
            # 预算检查
            if max_iter is not None and self._iteration >= max_iter:
                logger.info("达到最大迭代次数 %d，停止", max_iter)
                break
            if max_sec is not None and (time.time() - self._start_time) >= max_sec:
                logger.info("达到最大运行时间 %.0f 秒，停止", max_sec)
                break

            self._iteration += 1
            self._run_single_iteration()

        logger.info(
            "自主研究引擎停止 (迭代=%d, 成功=%d, 失败=%d, 耗时=%.1f 秒)",
            self._iteration,
            self._total_succeeded,
            self._total_failed,
            time.time() - self._start_time,
        )
        return self.snapshot()

    def stop(self) -> None:
        """请求优雅停止（完成当前迭代后退出）。"""
        self._stop_requested = True
        logger.info("收到停止请求，将在当前迭代完成后退出")

    def snapshot(self) -> dict[str, Any]:
        """引擎运行状态快照。"""
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        max_sec = self._budget.max_seconds
        # 修复 4：budget 消耗报告，便于运维定位卡死/超时
        iteration_avg = (
            self._iteration_elapsed_total / self._total_researched
            if self._total_researched > 0 else 0.0
        )
        budget_report: dict[str, Any] = {
            "max_iterations": self._budget.max_iterations,
            "max_seconds": max_sec,
            "max_iteration_seconds": self._budget.max_iteration_seconds,
            "elapsed_seconds": round(elapsed, 2),
            "remaining_seconds": (
                round(max_sec - elapsed, 2) if max_sec is not None else None
            ),
            "iteration_avg_seconds": round(iteration_avg, 3),
            "iteration_max_seconds": round(self._iteration_elapsed_max, 3),
            "iteration_timeouts": self._total_iteration_timeouts,
        }
        return {
            "iteration": self._iteration,
            "elapsed_seconds": round(elapsed, 2),
            "total_researched": self._total_researched,
            "total_succeeded": self._total_succeeded,
            "total_failed": self._total_failed,
            "total_nodes_added": self._total_nodes_added,
            "total_edges_added": self._total_edges_added,
            "total_evidence_added": self._total_evidence_added,
            "total_docs_recalled": self._total_docs_recalled,
            "total_ticks": self._total_ticks,
            # 修复 1：来源状态分布
            "total_source_ok": self._total_source_ok,
            "total_source_degraded": self._total_source_degraded,
            "total_source_unavailable": self._total_source_unavailable,
            # 修复 2：engine 层独立校验出的空转次数
            "total_idle": self._total_idle,
            # 修复 2.2：按 source_status 细分空转原因
            "total_idle_unavailable": self._total_idle_unavailable,
            "total_idle_degraded": self._total_idle_degraded,
            "total_idle_no_progress": self._total_idle_no_progress,
            "budget": budget_report,
            "scheduler": self._scheduler.snapshot(),
            "memory": self._memory.snapshot(),
        }

    # ------------------------------------------------------------------
    # 内部：单次迭代
    # ------------------------------------------------------------------

    def _run_single_iteration(self) -> None:
        """执行单次研究迭代。"""
        iteration_start = time.time()

        # 1. 获取下一个任务
        task = self._scheduler.next_task()
        if task is None:
            logger.info("迭代 %d: 无可用任务，等待 refill", self._iteration)
            time.sleep(1.0)  # 避免空转
            return

        logger.info(
            "迭代 %d: 研究目标=%s (value=%.4f, source=%s)",
            self._iteration, task.node_name, task.research_value, task.source,
        )

        # 2. 执行研究动作
        try:
            result = self._action.execute(task, self._memory)
        except Exception as exc:
            logger.error("研究动作异常: %s", exc, exc_info=True)
            result = ResearchResult(
                success=False, reason=f"异常: {exc}",
                source_status="unavailable",
            )

        # 修复 2：engine 层独立校验产出。即便 ResearchAction 自检失效
        # （如未来新增 action 未实现 progressed 判定），engine 也不应把
        # "success=True 但 0 产出"当成真成功，否则节点会被误标记为已完成。
        if result.success and not (
            result.new_nodes or result.new_edges or result.new_evidence
        ):
            logger.warning(
                "迭代 %d: engine 层独立校验失败——success=True 但 0 产出，降级为空转",
                self._iteration,
            )
            result.success = False
            result.reason = (
                f"{result.reason} [engine 层独立校验：0 产出]"
                if result.reason else "engine 层独立校验：0 产出"
            )
            self._total_idle += 1
            # 修复 2.2：按 source_status 细分空转原因，便于运维定位
            if result.source_status == "unavailable":
                self._total_idle_unavailable += 1
            elif result.source_status == "degraded":
                self._total_idle_degraded += 1
            else:
                self._total_idle_no_progress += 1

        # 修复 1：累计来源状态统计
        self._accumulate_source_status(result.source_status)

        # 3. 标记任务完成
        # 修复 2.3：传 source_status 给 scheduler，让其按源状态决策重试
        self._scheduler.mark_done(
            task.task_id,
            result.success,
            result.reason,
            source_status=result.source_status,
        )

        # 4. 更新统计
        self._total_researched += 1
        if result.success:
            self._total_succeeded += 1
        else:
            self._total_failed += 1
        self._total_nodes_added += len(result.new_nodes)
        self._total_edges_added += len(result.new_edges)
        self._total_evidence_added += len(result.new_evidence)
        self._total_docs_recalled += result.documents_recalled

        # 修复 4：迭代耗时统计 + 超时检测
        iteration_elapsed = time.time() - iteration_start
        self._iteration_elapsed_total += iteration_elapsed
        if iteration_elapsed > self._iteration_elapsed_max:
            self._iteration_elapsed_max = iteration_elapsed
        if self._budget.max_iteration_seconds is not None:
            if iteration_elapsed > self._budget.max_iteration_seconds:
                self._total_iteration_timeouts += 1
                logger.warning(
                    "迭代 %d 超时: elapsed=%.2fs > budget=%.2fs (reason=%s)",
                    self._iteration, iteration_elapsed,
                    self._budget.max_iteration_seconds, result.reason,
                )

        logger.info(
            "迭代 %d 完成: success=%s, nodes=%d, edges=%d, docs=%d, "
            "source=%s, elapsed=%.2fs",
            self._iteration, result.success,
            len(result.new_nodes), len(result.new_edges),
            result.documents_recalled, result.source_status,
            iteration_elapsed,
        )

        # 5. 间歇性触发环境 tick
        now = time.time()
        if now - self._last_tick >= self._budget.tick_interval:
            self._trigger_tick()
            self._last_tick = now

        # 6. 间歇性 refill
        if self._iteration % self._budget.refill_interval == 0:
            added = self._scheduler.refill()
            logger.info("迭代 %d: refill 补充 %d 任务", self._iteration, added)

    def _accumulate_source_status(self, source_status: str) -> None:
        """累计来源状态统计（修复 1）。

        Args:
            source_status: ResearchResult.source_status 取值。
                非 "degraded"/"unavailable" 一律计入 "ok"。
        """
        if source_status == "degraded":
            self._total_source_degraded += 1
        elif source_status == "unavailable":
            self._total_source_unavailable += 1
        else:
            self._total_source_ok += 1

    def _trigger_tick(self) -> None:
        """触发神经元环境 tick（自主学习）。"""
        try:
            self._memory.tick()
            self._total_ticks += 1
            logger.debug("环境 tick %d 完成", self._total_ticks)
        except Exception as exc:
            logger.warning("环境 tick 失败: %s", exc)
