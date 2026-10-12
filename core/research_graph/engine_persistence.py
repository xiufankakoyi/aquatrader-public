"""自主研究引擎持久化恢复：状态序列化/反序列化。

持久化策略：
    1. GraphStore 状态：序列化为 JSON（节点/边/证据/mutations）
    2. 调度器状态：序列化 pending/cooling/completed 集合
    3. 引擎统计：迭代次数、成功/失败计数
    4. 原子写入：先写临时文件再 rename，避免中断损坏
    5. 修复 3：environment 内存池（evidence_pool/stance/neuron_map）
       序列化为 environment_state.json，恢复后由 memory._ensure_environment
       在 cold_start 后注入，避免重启后 EvidenceLedgerReaderAdapter 查询为空。

恢复策略：
    1. 从 checkpoint.json 恢复 GraphStore
    2. 从 scheduler_state.json 恢复调度器队列
    3. 从 engine_state.json 恢复引擎统计
    4. 从 environment_state.json 恢复 environment 内存池（暂存到 memory.pending）
    5. LanceDB（neuron_state/chunks）自带持久化，无需额外处理
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from core.research_graph.autonomous_research_engine import (
    AutonomousResearchEngine,
    EngineBudget,
)
from core.research_graph.cognitive_gap_scheduler import (
    CognitiveGapScheduler,
    ResearchTask,
)
from core.research_graph.unified_memory import UnifiedResearchMemory

logger = logging.getLogger(__name__)


class EnginePersistence:
    """引擎持久化管理器。

    Args:
        checkpoint_dir: checkpoint 目录路径。
    """

    GRAPH_FILE = "graph_store.json"
    SCHEDULER_FILE = "scheduler_state.json"
    ENGINE_FILE = "engine_state.json"
    MEMORY_FILE = "memory_meta.json"
    # 修复 3：environment 内存池状态文件
    ENVIRONMENT_FILE = "environment_state.json"

    def __init__(self, checkpoint_dir: str | Path) -> None:
        self._dir = Path(checkpoint_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save(
        self,
        engine: AutonomousResearchEngine,
        memory: UnifiedResearchMemory,
        scheduler: CognitiveGapScheduler,
    ) -> None:
        """原子保存引擎完整状态。

        Args:
            engine: 自主研究引擎实例。
            memory: 统一记忆层。
            scheduler: 认知缺口调度器。
        """
        timestamp = time.time()
        logger.info("保存 checkpoint 到 %s", self._dir)

        # 1. GraphStore 状态
        self._save_graph_store(memory)

        # 2. 调度器状态
        self._save_scheduler(scheduler)

        # 3. 引擎状态
        self._save_engine(engine, timestamp)

        # 4. 记忆层元数据
        self._save_memory_meta(memory, timestamp)

        # 5. 修复 3：environment 内存池（若已初始化）
        self._save_environment_state(memory, timestamp)

        logger.info("Checkpoint 保存完成")

    def _save_environment_state(
        self, memory: UnifiedResearchMemory, timestamp: float,
    ) -> None:
        """保存 environment 内存池状态（修复 3）。

        environment 惰性初始化，未创建时跳过（无内存池可保存）。
        已创建时调用 environment.export_evidence_routing_state 导出
        _evidence_pool / _evidence_stance / _neuron_evidence_map。

        Args:
            memory: 统一记忆层。
            timestamp: 保存时间戳。
        """
        # 直接访问私有属性，避免触发惰性初始化（无数据时不应创建 environment）
        env = memory._environment
        if env is None:
            return
        try:
            payload = env.export_evidence_routing_state()
            payload["saved_at"] = timestamp
            self._atomic_write_json(self._dir / self.ENVIRONMENT_FILE, payload)
        except Exception as exc:
            logger.warning("保存 environment 状态失败: %s", exc)

    def _save_graph_store(self, memory: UnifiedResearchMemory) -> None:
        """保存 GraphStore 到 JSON。"""
        store = memory.graph_store
        payload = store.to_dict()
        self._atomic_write_json(self._dir / self.GRAPH_FILE, payload)

    def _save_scheduler(self, scheduler: CognitiveGapScheduler) -> None:
        """保存调度器状态。"""
        # 序列化 pending 堆
        pending = []
        for task in scheduler._heap:
            pending.append(self._task_to_dict(task))

        # 序列化 cooling
        cooling = []
        for task in scheduler._cooling.values():
            cooling.append(self._task_to_dict(task))

        # 序列化 running
        running = []
        for task in scheduler._running.values():
            running.append(self._task_to_dict(task))

        payload = {
            "pending": pending,
            "cooling": cooling,
            "running": running,
            "completed": list(scheduler._completed),
            "stats": {
                "total_dispatched": scheduler._total_dispatched,
                "total_succeeded": scheduler._total_succeeded,
                "total_failed": scheduler._total_failed,
                "total_refills": scheduler._total_refills,
            },
            "saved_at": time.time(),
        }
        self._atomic_write_json(self._dir / self.SCHEDULER_FILE, payload)

    def _save_engine(
        self, engine: AutonomousResearchEngine, timestamp: float
    ) -> None:
        """保存引擎状态。"""
        payload = {
            "iteration": engine._iteration,
            "start_time": engine._start_time,
            "last_tick": engine._last_tick,
            "stop_requested": engine._stop_requested,
            "stats": {
                "total_researched": engine._total_researched,
                "total_succeeded": engine._total_succeeded,
                "total_failed": engine._total_failed,
                "total_nodes_added": engine._total_nodes_added,
                "total_edges_added": engine._total_edges_added,
                "total_evidence_added": engine._total_evidence_added,
                "total_docs_recalled": engine._total_docs_recalled,
                "total_ticks": engine._total_ticks,
            },
            "budget": asdict(engine._budget),
            "saved_at": timestamp,
        }
        self._atomic_write_json(self._dir / self.ENGINE_FILE, payload)

    def _save_memory_meta(
        self, memory: UnifiedResearchMemory, timestamp: float
    ) -> None:
        """保存记忆层元数据（节点/边/证据计数）。"""
        snapshot = memory.snapshot()
        payload = {
            "snapshot": snapshot,
            "data_root": str(memory._data_root),
            "saved_at": timestamp,
        }
        self._atomic_write_json(self._dir / self.MEMORY_FILE, payload)

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def load_graph_store(self, memory: UnifiedResearchMemory) -> bool:
        """从 checkpoint 恢复 GraphStore。

        Args:
            memory: 统一记忆层。

        Returns:
            是否成功恢复。
        """
        path = self._dir / self.GRAPH_FILE
        if not path.exists():
            return False

        try:
            from core.research_graph.store import GraphStore
            # 初始化持久化适配器后再替换 GraphStore，避免恢复后新写入只停留在内存。
            memory._ensure_graph_store()
            store = GraphStore.load_json(path)
            # 替换 memory 内部的 graph_store
            memory._graph_store = store
            memory._evidence_ledger = None
            memory._environment = None
            memory._node_count = len(store.nodes)
            memory._edge_count = len(store.edges)
            memory._evidence_count = len(store.evidence)
            memory._topic_nodes = {}
            for node_id, node in store.nodes.items():
                topic = str((node.attributes or {}).get("topic", ""))
                if topic:
                    memory._topic_nodes.setdefault(topic, set()).add(node_id)
            # checkpoint 是完整快照；同步回 memory 的 LanceDB，保证恢复后新增
            # 数据与旧快照共存，而不是只把后续新增项写入空表。
            for node in store.nodes.values():
                memory._persist_node(node)
            for evidence in store.evidence.values():
                memory._persist_evidence(evidence)
            for edge in store.edges.values():
                memory._persist_edge(edge)
            memory._node_cache.invalidate_all()
            memory._adjacency_cache.invalidate_all()
            logger.info(
                "GraphStore 恢复: %d 节点, %d 边, %d 证据",
                len(store.nodes), len(store.edges), len(store.evidence),
            )
            return True
        except Exception as exc:
            logger.error("GraphStore 恢复失败: %s", exc)
            return False

    def load_scheduler(self, scheduler: CognitiveGapScheduler) -> bool:
        """从 checkpoint 恢复调度器状态。

        Args:
            scheduler: 认知缺口调度器。

        Returns:
            是否成功恢复。
        """
        path = self._dir / self.SCHEDULER_FILE
        if not path.exists():
            return False

        try:
            payload = self._read_json(path)

            # 恢复 pending 堆
            import heapq
            scheduler._heap = []
            scheduler._enqueued = set()
            for task_dict in payload.get("pending", []):
                task = self._dict_to_task(task_dict)
                if task:
                    heapq.heappush(scheduler._heap, task)
                    scheduler._enqueued.add(task.node_name)

            # 恢复 cooling
            scheduler._cooling = {}
            for task_dict in payload.get("cooling", []):
                task = self._dict_to_task(task_dict)
                if task:
                    scheduler._cooling[task.task_id] = task

            # 恢复 running（重新入队，因为无法确定是否已完成）
            for task_dict in payload.get("running", []):
                task = self._dict_to_task(task_dict)
                if task and task.node_name not in scheduler._enqueued:
                    heapq.heappush(scheduler._heap, task)
                    scheduler._enqueued.add(task.node_name)

            # 恢复 completed
            scheduler._completed = set(payload.get("completed", []))

            # 恢复统计
            stats = payload.get("stats", {})
            scheduler._total_dispatched = stats.get("total_dispatched", 0)
            scheduler._total_succeeded = stats.get("total_succeeded", 0)
            scheduler._total_failed = stats.get("total_failed", 0)
            scheduler._total_refills = stats.get("total_refills", 0)

            logger.info(
                "调度器恢复: pending=%d, cooling=%d, completed=%d",
                len(scheduler._heap),
                len(scheduler._cooling),
                len(scheduler._completed),
            )
            return True
        except Exception as exc:
            logger.error("调度器恢复失败: %s", exc)
            return False

    def load_engine(self, engine: AutonomousResearchEngine) -> bool:
        """从 checkpoint 恢复引擎统计。

        Args:
            engine: 自主研究引擎。

        Returns:
            是否成功恢复。
        """
        path = self._dir / self.ENGINE_FILE
        if not path.exists():
            return False

        try:
            payload = self._read_json(path)
            engine._iteration = payload.get("iteration", 0)
            engine._start_time = payload.get("start_time", 0.0)
            engine._last_tick = payload.get("last_tick", 0.0)
            engine._stop_requested = False  # 恢复时清除停止标志

            stats = payload.get("stats", {})
            engine._total_researched = stats.get("total_researched", 0)
            engine._total_succeeded = stats.get("total_succeeded", 0)
            engine._total_failed = stats.get("total_failed", 0)
            engine._total_nodes_added = stats.get("total_nodes_added", 0)
            engine._total_edges_added = stats.get("total_edges_added", 0)
            engine._total_evidence_added = stats.get("total_evidence_added", 0)
            engine._total_docs_recalled = stats.get("total_docs_recalled", 0)
            engine._total_ticks = stats.get("total_ticks", 0)

            logger.info(
                "引擎恢复: iteration=%d, researched=%d",
                engine._iteration, engine._total_researched,
            )
            return True
        except Exception as exc:
            logger.error("引擎恢复失败: %s", exc)
            return False

    def has_checkpoint(self) -> bool:
        """检查是否存在有效 checkpoint。"""
        return (self._dir / self.GRAPH_FILE).exists()

    def load_environment_state(self) -> dict[str, Any] | None:
        """从 checkpoint 恢复 environment 内存池状态（修复 3）。

        返回的 dict 应由调用方调用 memory.set_pending_environment_state(state)
        暂存，_ensure_environment 在 cold_start 后注入。这是为了避免在
        environment 惰性初始化前直接注入。

        Returns:
            environment 状态 dict；不存在或读取失败时返回 None。
        """
        path = self._dir / self.ENVIRONMENT_FILE
        if not path.exists():
            return None
        try:
            payload = self._read_json(path)
            logger.info(
                "Environment 状态恢复: evidence_ids=%d, neuron_map=%d",
                len(payload.get("evidence_ids", [])),
                len(payload.get("neuron_evidence_map", {})),
            )
            return payload
        except Exception as exc:
            logger.error("Environment 状态恢复失败: %s", exc)
            return None

    def checkpoint_info(self) -> dict[str, Any]:
        """返回 checkpoint 元信息。"""
        info: dict[str, Any] = {"dir": str(self._dir), "files": {}}
        for fname in [
            self.GRAPH_FILE, self.SCHEDULER_FILE, self.ENGINE_FILE,
            self.MEMORY_FILE, self.ENVIRONMENT_FILE,
        ]:
            path = self._dir / fname
            if path.exists():
                data = self._read_json(path)
                info["files"][fname] = {
                    "size_bytes": path.stat().st_size,
                    "saved_at": data.get("saved_at", 0),
                }
        return info

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _task_to_dict(self, task: ResearchTask) -> dict[str, Any]:
        """序列化 ResearchTask。"""
        return {
            "task_id": task.task_id,
            "node_id": task.node_id,
            "node_name": task.node_name,
            "gap_type": task.gap_type,
            "research_value": task.research_value,
            "source": task.source,
            "status": task.status,
            "attempts": task.attempts,
            "cooldown_until": task.cooldown_until,
            "dimensions": task.dimensions,
            "reason": task.reason,
            "created_at": task.created_at,
            "sort_priority": task.sort_priority,
        }

    def _dict_to_task(self, data: dict[str, Any]) -> ResearchTask | None:
        """反序列化 ResearchTask。"""
        try:
            return ResearchTask(
                sort_priority=float(data.get("sort_priority", -0.5)),
                created_at=float(data.get("created_at", time.time())),
                task_id=str(data.get("task_id", "")),
                node_id=str(data.get("node_id", "")),
                node_name=str(data.get("node_name", "")),
                gap_type=str(data.get("gap_type", "unknown")),
                research_value=float(data.get("research_value", 0.5)),
                source=str(data.get("source", "restored")),
                status=str(data.get("status", "pending")),
                attempts=int(data.get("attempts", 0)),
                cooldown_until=float(data.get("cooldown_until", 0.0)),
                dimensions=dict(data.get("dimensions", {})),
                reason=str(data.get("reason", "")),
            )
        except Exception as exc:
            logger.warning("任务反序列化失败: %s", exc)
            return None

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        """原子写入 JSON（先写临时文件再 rename）。"""
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        # Windows 上 rename 会覆盖目标
        os.replace(str(tmp_path), str(path))

    def _read_json(self, path: Path) -> dict[str, Any]:
        """读取 JSON 文件。"""
        return json.loads(path.read_text(encoding="utf-8"))
