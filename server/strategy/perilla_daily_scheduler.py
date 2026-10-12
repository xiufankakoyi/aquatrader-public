"""紫苏叶闲时自主研究调度器。

每天 9:00 和 15:00 自动运行 AutonomousResearchEngine，
驱动神经元网络进行认知缺口识别与本地推理。

特性:
    1. APScheduler 可用时优先使用 APScheduler cron 任务
    2. APScheduler 不可用时降级为 threading.Timer 定时唤醒
    3. 提供 register_scheduler(scheduler) 供外部调度器集成
    4. 提供 run_idle_research() 供手动触发单次研究
    5. 使用 UnifiedResearchMemory(data_root="data/research_memory") 共享世界模型

集成入口:
    server/asgi_entry.py 在 ASGI lifespan startup 阶段调用
    get_perilla_daily_scheduler().start() 启动调度器。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 每日触发时刻（24 小时制）
_DAILY_TRIGGER_HOURS: tuple[int, ...] = (9, 15)

# 项目根目录，用于定位 data/research_memory
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def run_idle_research(
    max_iterations: int = 3,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """手动触发一次闲时自主研究。

    创建 AutonomousResearchEngine 并执行少量迭代，
    使用统一记忆层（data_root 默认 data/research_memory）。

    Args:
        max_iterations: 最大迭代次数，默认 3（闲时少量推理）。
        data_root: 统一记忆层数据根目录，None 时使用项目默认路径。

    Returns:
        引擎运行快照 dict，含 iteration/total_succeeded/memory 等字段。
        异常时返回 {"success": False, "reason": "..."}。
    """
    from core.research_graph.autonomous_research_engine import (
        AutonomousResearchEngine,
        EngineBudget,
    )
    from core.research_graph.cognitive_gap_scheduler import CognitiveGapScheduler
    from core.research_graph.llm_research_action import LLMResearchAction
    from core.research_graph.perilla_gap_scorer import PerillaGapScorer
    from core.research_graph.unified_memory import UnifiedResearchMemory

    if data_root is None:
        data_root = _PROJECT_ROOT / "data" / "research_memory"

    logger.info("[PerillaIdle] 启动闲时研究 (max_iter=%d, data_root=%s)", max_iterations, data_root)

    try:
        memory = UnifiedResearchMemory(data_root=str(data_root), cache_max_size=50000)
        scorer = PerillaGapScorer(memory, light_screen_threshold=0.15)
        scheduler = CognitiveGapScheduler(
            memory, scorer,
            max_queue_size=100,
            max_attempts=3,
            cooldown_seconds=300.0,
            top_k_per_refill=20,
        )
        action = LLMResearchAction(
            recall_limit=5,
            llm_timeout=45,
            llm_tier="flash",
            max_nodes_per_task=10,
            max_edges_per_task=15,
        )
        budget = EngineBudget(
            max_iterations=max_iterations,
            max_seconds=None,
            tick_interval=5.0,
            refill_interval=10,
        )
        engine = AutonomousResearchEngine(
            memory, scheduler, action, budget=budget,
        )

        # 补充种子任务（若记忆层已有节点，refill 会识别缺口）
        scheduler.refill()

        snapshot = engine.run(max_iterations=max_iterations)
        logger.info(
            "[PerillaIdle] 闲时研究完成: iteration=%d, succeeded=%d, failed=%d",
            snapshot.get("iteration", 0),
            snapshot.get("total_succeeded", 0),
            snapshot.get("total_failed", 0),
        )
        return snapshot
    except Exception as exc:
        logger.error("[PerillaIdle] 闲时研究异常: %s", exc, exc_info=True)
        return {"success": False, "reason": str(exc)}


class PerillaDailyScheduler:
    """紫苏叶每日闲时研究调度器。

    优先使用 APScheduler（若已安装），否则降级为 threading.Timer。
    每天 9:00 和 15:00 触发 run_idle_research()。

    单例模式：通过 get_perilla_daily_scheduler() 获取。
    """

    def __init__(self) -> None:
        self._started = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._apscheduler_registered: bool = False

    def start(self) -> None:
        """启动调度器（幂等，重复调用安全）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            logger.info(
                "[PerillaScheduler] 启动，触发时刻=%s",
                ",".join(f"{h:02d}:00" for h in _DAILY_TRIGGER_HOURS),
            )
        self._schedule_next()

    def stop(self) -> None:
        """停止调度器。"""
        with self._lock:
            self._started = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        logger.info("[PerillaScheduler] 已停止")

    def run_now(self) -> dict[str, Any]:
        """手动触发一次闲时研究（同步阻塞）。

        Returns:
            run_idle_research() 的返回值。
        """
        return run_idle_research()

    def _schedule_next(self) -> None:
        """调度下一次触发（threading.Timer 降级模式）。"""
        with self._lock:
            if not self._started:
                return
            # 若已通过 APScheduler 注册，不再使用 Timer
            if self._apscheduler_registered:
                return
            # 取消已有的 Timer
            if self._timer is not None:
                self._timer.cancel()

            next_run = self._next_run_time()
            delay = max(1.0, (next_run - datetime.now()).total_seconds())
            logger.info(
                "[PerillaScheduler] 下次触发: %s (%.0f 秒后)",
                next_run.strftime("%Y-%m-%d %H:%M:%S"),
                delay,
            )
            self._timer = threading.Timer(delay, self._timer_callback)
            self._timer.daemon = True
            self._timer.start()

    def _timer_callback(self) -> None:
        """Timer 触发回调：执行研究并调度下一次。"""
        try:
            run_idle_research()
        except Exception as exc:
            logger.error("[PerillaScheduler] Timer 触发研究失败: %s", exc, exc_info=True)
        finally:
            self._schedule_next()

    @staticmethod
    def _next_run_time() -> datetime:
        """计算下一个触发时刻（今天 9:00/15:00 或明天 9:00）。

        Returns:
            下次触发的 datetime。
        """
        now = datetime.now()
        for hour in _DAILY_TRIGGER_HOURS:
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target > now:
                return target
        # 今天已过所有触发点，明天 9:00
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=_DAILY_TRIGGER_HOURS[0], minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------

_scheduler_instance: PerillaDailyScheduler | None = None


def get_perilla_daily_scheduler() -> PerillaDailyScheduler:
    """获取 PerillaDailyScheduler 单例。

    ASGI 启动时调用此函数并执行 .start()。

    Returns:
        PerillaDailyScheduler 单例实例。
    """
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = PerillaDailyScheduler()
    return _scheduler_instance


def register_scheduler(scheduler: Any) -> bool:
    """向外部 APScheduler 实例注册闲时研究任务。

    若 APScheduler 可用且 scheduler 是兼容实例，则添加 cron 任务
    （每天 9:00 和 15:00 触发 run_idle_research），并停止内部 Timer 降级模式。

    Args:
        scheduler: APScheduler Scheduler 实例（BackgroundScheduler/AsyncIOScheduler 等）。

    Returns:
        True 表示已成功注册到外部调度器；False 表示降级到内部 Timer 模式。
    """
    instance = get_perilla_daily_scheduler()

    try:
        # 尝试 APScheduler 风格的 add_job
        scheduler.add_job(
            run_idle_research,
            trigger="cron",
            hour="9,15",
            minute=0,
            id="perilla_idle_research",
            replace_existing=True,
        )
        with instance._lock:
            instance._apscheduler_registered = True
            # 若内部 Timer 已启动，取消它
            if instance._timer is not None:
                instance._timer.cancel()
                instance._timer = None
        logger.info("[PerillaScheduler] 已注册到外部 APScheduler (cron 9:00/15:00)")
        return True
    except Exception as exc:
        logger.warning(
            "[PerillaScheduler] 注册外部调度器失败，降级到 Timer: %s", exc,
        )
        # 降级：启动内部 Timer
        instance.start()
        return False
