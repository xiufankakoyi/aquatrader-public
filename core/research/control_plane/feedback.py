"""三域反馈隔离与任务回流。

防循环机制：
- 去重：dedup_key 24h 内重复丢弃，检查与写入在同一 SQLite 事务中完成；
- 限流：每域每小时最多 RATE_LIMIT_PER_HOUR 条反馈任务；
- 深度控制：parent_task_id + trace_id + feedback_depth ≥ MAX_FEEDBACK_DEPTH 不再生成；
- 过期：任务 expires_at 过期后 TaskRunner 跳过并审计。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any

from .contracts import FeedbackDomain, FeedbackEvent, ResearchTask, TaskStatus, _safe_int, _safe_text, utc_now
from .repository import ResearchRepository


class FeedbackController:
    """三域反馈控制器，事务化去重/限流/深度控制。"""

    DEDUP_TTL_HOURS = 24
    RATE_LIMIT_PER_HOUR = 20
    MAX_FEEDBACK_DEPTH = 3
    TASK_TTL_HOURS = 48

    def __init__(self, repository: ResearchRepository) -> None:
        """初始化。

        Args:
            repository: ResearchRepository 实例。
        """
        self.repository = repository

    def record(
        self,
        event: FeedbackEvent,
        *,
        parent_task_id: str = "",
        trace_id: str = "",
        generate_task: bool = True,
    ) -> FeedbackEvent:
        """记录反馈事件，事务化去重/限流/深度控制。

        Args:
            event: 反馈事件。
            parent_task_id: 父任务 ID，用于深度控制。
            trace_id: 追踪 ID，串联反馈链。
            generate_task: 是否生成研究任务。

        Returns:
            记录后的 FeedbackEvent。
        """
        if not isinstance(event, FeedbackEvent):
            raise ValueError("反馈事件类型无效")
        event.domain = _safe_text(event.domain)
        if event.domain not in {item.value for item in FeedbackDomain}:
            raise ValueError("反馈域必须是 knowledge/retrieval/alpha")
        event.metrics = dict(event.metrics) if isinstance(event.metrics, Mapping) else {}
        event.parent_task_id = _safe_text(parent_task_id or event.parent_task_id)
        event.trace_id = _safe_text(trace_id or event.trace_id)
        event.dedup_key = self._dedup_key(event)
        now = datetime.now(timezone.utc)
        event.expires_at = (now + timedelta(hours=self.TASK_TTL_HOURS)).isoformat()

        with self.repository.transaction() as conn:
            # 1. 去重检查（事务内）
            cutoff = (now - timedelta(hours=self.DEDUP_TTL_HOURS)).isoformat()
            existing = conn.execute(
                "SELECT 1 FROM feedback_events "
                "WHERE json_extract(payload_json, '$.dedup_key')=? "
                "AND json_extract(payload_json, '$.created_at') > ?",
                (event.dedup_key, cutoff),
            ).fetchone()
            if existing:
                event.metrics["deduped"] = True
                return event

            # 2. 限流检查（事务内）
            recent = conn.execute(
                "SELECT COUNT(*) FROM feedback_events "
                "WHERE json_extract(payload_json, '$.domain')=? "
                "AND json_extract(payload_json, '$.created_at') > ?",
                (event.domain, (now - timedelta(hours=1)).isoformat()),
            ).fetchone()[0]
            if recent >= self.RATE_LIMIT_PER_HOUR:
                event.metrics["rate_limited"] = True
                self._save_feedback_in_txn(conn, event)
                return event

            # 3. 深度控制
            depth = self._get_depth_in_txn(conn, event.parent_task_id, event.trace_id)
            event.feedback_depth = depth
            if depth >= self.MAX_FEEDBACK_DEPTH:
                event.metrics["depth_capped"] = True
                self._save_feedback_in_txn(conn, event)
                return event

            # 4. 生成任务
            if generate_task:
                task = self._generate_task(event, depth + 1)
                self._save_task_in_txn(conn, task)
                event.generated_task_id = task.task_id
            event.feedback_depth = depth + 1
            self._save_feedback_in_txn(conn, event)

        # 反馈只触发同域 shadow 校准，绝不跨域；启用仍需人工审批。
        try:
            from .domain_calibrators import fit_domain
            fit_domain(self.repository, event.domain)
        except (ImportError, KeyError, TypeError, ValueError, sqlite3.Error):
            pass
        return event

    @staticmethod
    def _dedup_key(event: FeedbackEvent) -> str:
        """生成去重键：domain + target_id + event_type + metrics 核心字段。"""
        core = json.dumps({
            "domain": event.domain,
            "target_id": event.target_id,
            "event_type": event.event_type,
            "event_kind": event.event_kind,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(core.encode("utf-8")).hexdigest()[:16]

    def _get_depth_in_txn(self, conn: Any, parent_task_id: str, trace_id: str) -> int:
        """查询当前反馈链深度。"""
        if not trace_id:
            return 0
        row = conn.execute(
            "SELECT MAX(json_extract(payload_json, '$.feedback_depth')) "
            "FROM feedback_events "
            "WHERE json_extract(payload_json, '$.trace_id')=?",
            (trace_id,),
        ).fetchone()
        if row and row[0] is not None:
            return max(0, _safe_int(row[0]))
        return 0

    def _generate_task(self, event: FeedbackEvent, depth: int) -> ResearchTask:
        """根据反馈域生成研究任务。"""
        if event.domain == FeedbackDomain.KNOWLEDGE.value:
            task_type = "knowledge_revalidation"
        elif event.domain == FeedbackDomain.RETRIEVAL.value:
            task_type = "retrieval_policy_review"
        else:
            task_type = "alpha_model_review"
        now = datetime.now(timezone.utc)
        return ResearchTask(
            task_type=task_type,
            trigger_source=f"feedback:{event.domain}",
            target_claim_id=event.target_id if event.domain == FeedbackDomain.KNOWLEDGE.value else "",
            parent_task_id=event.parent_task_id,
            trace_id=event.trace_id,
            expires_at=(now + timedelta(hours=self.TASK_TTL_HOURS)).isoformat(),
            params={
                "feedback_id": event.feedback_id,
                "event_type": event.event_type,
                "metrics": event.metrics,
                "feedback_depth": depth,
                "dedup_key": event.dedup_key,
            },
        )

    @staticmethod
    def _save_feedback_in_txn(conn: Any, event: FeedbackEvent) -> None:
        """在事务内保存反馈事件。"""
        payload = json.dumps(event.to_dict(), ensure_ascii=False)
        conn.execute(
            "INSERT INTO feedback_events(feedback_id, domain, created_at, payload_json) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(feedback_id) DO UPDATE SET domain=excluded.domain, "
            "created_at=excluded.created_at, payload_json=excluded.payload_json",
            (event.feedback_id, event.domain, event.created_at, payload),
        )

    @staticmethod
    def _save_task_in_txn(conn: Any, task: ResearchTask) -> None:
        """在事务内保存研究任务。"""
        payload = json.dumps(task.to_dict(), ensure_ascii=False)
        conn.execute(
            "INSERT INTO research_tasks(task_id, status, created_at, payload_json) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
            "created_at=excluded.created_at, payload_json=excluded.payload_json",
            (task.task_id, task.status, task.created_at, payload),
        )

    def list_expired_tasks(self, limit: int = 100) -> list[ResearchTask]:
        """列出已过期但未处理的研究任务。"""
        limit = max(1, _safe_int(limit, 100))
        now_iso = utc_now()
        all_tasks = self.repository.list_tasks(limit=limit * 2)
        return [
            t for t in all_tasks
            if t.expires_at
            and t.expires_at < now_iso
            and t.status in (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value)
        ][:limit]

    def expire_stale_tasks(self) -> int:
        """将过期任务标记为 expired 并审计。

        Returns:
            标记为 expired 的任务数量。
        """
        expired = self.list_expired_tasks(limit=1000)
        for task in expired:
            task.status = "expired"
            task.params["expiry_reason"] = "passed expires_at"
            task.finished_at = utc_now()
            self.repository.save_task(task)
        return len(expired)
