"""认知缺口调度器：基于紫苏叶评分的优先队列，替换 FIFO TopicPool。

核心区别（对比旧 TopicPool）：
    旧：deque.popleft() — FIFO，所有主题同等优先级
    新：heapq — 优先队列，research_value 高的先出队

调度策略：
    1. 从 UnifiedResearchMemory.identify_gaps() 获取缺口
    2. 通过 PerillaGapScorer 评分
    3. 只取 Top-K 进入待研究队列
    4. 失败的任务进入冷却，不立即重试
    5. 队列空时自动 refill
    6. 种子主题优先级低于真实缺口
"""
from __future__ import annotations

import hashlib
import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.perilla_gap_scorer import PerillaGapScorer, ScoredGap
from core.research_graph.unified_memory import UnifiedResearchMemory

logger = logging.getLogger(__name__)


@dataclass(order=True)
class ResearchTask:
    """研究任务（可排序：按 priority 降序，created_at 升序）。

    heapq 是最小堆，所以 priority 取负值实现最大堆。
    """

    sort_priority: float  # = -research_value，用于堆排序
    created_at: float
    task_id: str = field(compare=False)
    node_id: str = field(default="", compare=False)
    node_name: str = field(default="", compare=False)
    gap_type: str = field(default="seed", compare=False)
    research_value: float = field(default=0.0, compare=False)
    source: str = field(default="seed", compare=False)  # seed / gap / derived
    status: str = field(default="pending", compare=False)
    attempts: int = field(default=0, compare=False)
    cooldown_until: float = field(default=0.0, compare=False)
    dimensions: dict[str, float] = field(default_factory=dict, compare=False)
    reason: str = field(default="", compare=False)


class CognitiveGapScheduler:
    """认知缺口调度器：优先队列 + 冷却 + 去重。

    Args:
        memory: 统一研究记忆层。
        scorer: 紫苏叶评分器。
        max_queue_size: 待研究队列最大长度。
        max_attempts: 单个任务最大尝试次数。
        cooldown_seconds: 失败任务冷却时间（秒）。
        top_k_per_refill: 每次 refill 取 Top-K 缺口入队。
    """

    def __init__(
        self,
        memory: UnifiedResearchMemory,
        scorer: PerillaGapScorer,
        max_queue_size: int = 100,
        max_attempts: int = 3,
        cooldown_seconds: float = 300.0,
        top_k_per_refill: int = 20,
    ) -> None:
        self._memory = memory
        self._scorer = scorer
        self._max_queue_size = max_queue_size
        self._max_attempts = max_attempts
        self._cooldown_seconds = cooldown_seconds
        self._top_k = top_k_per_refill

        # 优先队列（最小堆，用负 priority 模拟最大堆）
        self._heap: list[ResearchTask] = []
        # 去重：node_name → 是否在队列中或冷却中
        self._enqueued: set[str] = set()
        # 已完成（避免重复入队）
        self._completed: set[str] = set()
        # 冷却中的任务
        self._cooling: dict[str, ResearchTask] = {}
        # 运行中的任务（next_task 弹出后存于此，mark_done 时查找）
        self._running: dict[str, ResearchTask] = {}

        # 统计
        self._total_dispatched = 0
        self._total_succeeded = 0
        self._total_failed = 0
        self._total_refills = 0

    # ------------------------------------------------------------------
    # 种子主题（冷启动）
    # ------------------------------------------------------------------

    def add_seed_topic(self, topic: str, priority: float = 0.3) -> str:
        """添加种子主题（冷启动用，优先级低于真实缺口）。

        Args:
            topic: 种子主题名称。
            priority: 优先级 [0, 1]，默认 0.3（低于缺口）。

        Returns:
            任务 ID。
        """
        if topic in self._enqueued or topic in self._completed:
            return ""
        return self._enqueue_task(
            node_name=topic,
            gap_type="seed",
            research_value=priority,
            source="seed",
            reason=f"种子主题: {topic}",
        )

    # ------------------------------------------------------------------
    # 队列操作
    # ------------------------------------------------------------------

    def next_task(self) -> ResearchTask | None:
        """获取下一个最高优先级任务。

        跳过冷却中的任务。队列空时自动 refill。

        Returns:
            ResearchTask 或 None（无可用任务）。
        """
        now = time.time()

        # 先检查冷却到期的任务，重新入队
        self._requeue_cooled_tasks(now)

        # 队列空时 refill
        if not self._heap:
            self.refill()
            if not self._heap:
                return None

        # 弹出最高优先级任务
        task = heapq.heappop(self._heap)
        self._enqueued.discard(task.node_name)
        task.status = "running"
        task.attempts += 1
        self._running[task.task_id] = task
        self._total_dispatched += 1
        logger.info(
            "派发任务: %s (value=%.4f, source=%s, attempts=%d)",
            task.node_name, task.research_value, task.source, task.attempts,
        )
        return task

    def mark_done(
        self,
        task_id: str,
        success: bool,
        reason: str = "",
        source_status: str = "ok",
    ) -> None:
        """标记任务完成状态。

        Args:
            task_id: 任务 ID。
            success: 是否成功。
            reason: 完成原因。
            source_status: 源状态 "ok"/"degraded"/"unavailable"。
                - "ok" + success=False: 源正常但无新知识，标记完成不重试
                  （重试结果相同，浪费预算）。
                - "unavailable"/"degraded" + success=False: 源不可用或降级，
                  冷却后重试（源恢复后可能产出新知识）。
        """
        task = self._running.pop(task_id, None)

        if success:
            self._total_succeeded += 1
            self._completed.add(task.node_name)
            logger.info("任务成功: %s (%s)", task.node_name, reason)
            return

        # 失败处理
        self._total_failed += 1

        # 修复 2.1：source_status="ok" + 失败 —— 源正常但无新知识，
        # 重试结果相同，直接标记完成避免浪费预算
        if source_status == "ok":
            self._completed.add(task.node_name)
            logger.warning(
                "任务失败但源正常，标记完成不重试: %s (reason=%s)",
                task.node_name, reason,
            )
            return

        # source_status="unavailable" 或 "degraded"：源不可用/降级，冷却后重试
        if task.attempts < self._max_attempts:
            # 重试：放入冷却
            task.status = "cooling"
            task.cooldown_until = time.time() + self._cooldown_seconds
            self._cooling[task_id] = task
            logger.warning(
                "任务失败，进入冷却: %s (attempts=%d, source=%s, reason=%s)",
                task.node_name, task.attempts, source_status, reason,
            )
        else:
            # 超过最大尝试次数，永久失败
            self._completed.add(task.node_name)
            logger.warning(
                "任务永久失败: %s (attempts=%d, source=%s, reason=%s)",
                task.node_name, task.attempts, source_status, reason,
            )

    # ------------------------------------------------------------------
    # Refill：从缺口扫描补充队列
    # ------------------------------------------------------------------

    def refill(self) -> int:
        """从统一记忆扫描缺口，评分后补充队列。

        Returns:
            新入队任务数。
        """
        self._total_refills += 1

        # 1. 识别缺口
        gaps = self._memory.identify_gaps(max_gaps=200)

        # 2. 紫苏叶评分
        scored_gaps = self._scorer.score_gaps(gaps, max_precise=self._top_k)

        # 3. 入队（去重）
        added = 0
        for sg in scored_gaps:
            if len(self._heap) >= self._max_queue_size:
                break

            node_name = sg.gap.get("node_name", "")
            if not node_name:
                # 尝试从边获取节点名
                edge_id = sg.gap.get("edge_id", "")
                if edge_id:
                    store = self._memory.graph_store
                    edge = store.edges.get(edge_id)
                    if edge and edge.subjects:
                        node = store.nodes.get(edge.subjects[0])
                        if node:
                            node_name = node.name

            if not node_name or node_name in self._enqueued or node_name in self._completed:
                continue

            self._enqueue_task(
                node_name=node_name,
                node_id=sg.gap.get("node_id", ""),
                gap_type=sg.gap.get("type", "unknown"),
                research_value=sg.research_value,
                source="gap",
                dimensions=sg.dimensions,
                reason=sg.reason,
            )
            added += 1

        logger.info(
            "Refill: 扫描 %d 缺口, 评分 %d, 新入队 %d (队列总长 %d)",
            len(gaps), len(scored_gaps), added, len(self._heap),
        )
        return added

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """待处理任务数（不含冷却中的）。"""
        return len(self._heap)

    def cooling_count(self) -> int:
        """冷却中任务数。"""
        return len(self._cooling)

    def snapshot(self) -> dict[str, Any]:
        """调度器状态快照。"""
        # 堆中 Top-5 任务预览
        top_preview = []
        for task in sorted(self._heap, key=lambda t: t.sort_priority)[:5]:
            top_preview.append({
                "node_name": task.node_name,
                "research_value": task.research_value,
                "source": task.source,
                "gap_type": task.gap_type,
            })

        return {
            "pending": len(self._heap),
            "cooling": len(self._cooling),
            "completed": len(self._completed),
            "total_dispatched": self._total_dispatched,
            "total_succeeded": self._total_succeeded,
            "total_failed": self._total_failed,
            "total_refills": self._total_refills,
            "top_preview": top_preview,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _enqueue_task(
        self,
        node_name: str,
        node_id: str = "",
        gap_type: str = "unknown",
        research_value: float = 0.0,
        source: str = "gap",
        dimensions: dict[str, float] | None = None,
        reason: str = "",
    ) -> str:
        """入队任务。

        Args:
            node_name: 节点名称。
            node_id: 节点 ID。
            gap_type: 缺口类型。
            research_value: 研究价值。
            source: 来源（seed/gap/derived）。
            dimensions: 评分维度。
            reason: 评分理由。

        Returns:
            任务 ID。
        """
        task_id = hashlib.sha1(
            f"{node_name}|{gap_type}|{time.time()}".encode("utf-8")
        ).hexdigest()[:12]

        task = ResearchTask(
            sort_priority=-research_value,  # 负值实现最大堆
            created_at=time.time(),
            task_id=task_id,
            node_id=node_id,
            node_name=node_name,
            gap_type=gap_type,
            research_value=research_value,
            source=source,
            dimensions=dimensions or {},
            reason=reason,
        )

        heapq.heappush(self._heap, task)
        self._enqueued.add(node_name)
        return task_id

    def _requeue_cooled_tasks(self, now: float) -> None:
        """将冷却到期的任务重新入队。

        Args:
            now: 当前时间戳。
        """
        expired = [
            task_id for task_id, task in self._cooling.items()
            if task.cooldown_until <= now
        ]
        for task_id in expired:
            task = self._cooling.pop(task_id)
            task.status = "pending"
            task.cooldown_until = 0.0
            heapq.heappush(self._heap, task)
            self._enqueued.add(task.node_name)
            logger.info("冷却到期，重新入队: %s", task.node_name)
