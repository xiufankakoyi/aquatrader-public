"""ResearchTask attempt 模型与跨日续跑恢复器。

Phase 3：保持 ResearchTask 稳定身份，每次执行记录独立 attempt；
checkpoint 保存已完成阶段/已见文档/证据/预算消耗/失败类型/下一动作；
次日恢复 attempt 不重复创建同一 Claim。

设计原则：
- KISS：attempt 表只存审计快照，运行时状态仍在 ResearchTask.checkpoint
- DRY：复用 ResearchRepository 的 payload_json 持久化模式
- 防御性：恢复时校验 task_id/run_id/claim_id 一致性
- 不通过简单删除 task_id 日期字段实现续跑
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    ResearchTask,
    Serializable,
    TaskStatus,
    _as_mapping,
    _safe_bool,
    _safe_int,
    _safe_text,
    _text_list,
    new_id,
    utc_now,
)
from .repository import ResearchRepository

logger = logging.getLogger(__name__)


def _unique_texts(value: Any) -> list[str]:
    return list(dict.fromkeys(_text_list(value)))


def _decode_mapping(raw: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


@dataclass
class ResearchTaskAttempt(Serializable):
    """单次执行 attempt 记录。

    独立于 ResearchTask 的审计快照，记录每次执行的完整状态。
    跨日续跑时通过 run_id 关联同一 Claim 的多次 attempt。

    Attributes:
        attempt_id: attempt 唯一 ID。
        task_id: 关联的 ResearchTask ID。
        run_id: 跨日执行链 ID（同一 Claim 多次 attempt 共享）。
        target_claim_id: 关联的 Claim ID。
        attempt_number: attempt 编号（从 1 开始递增）。
        status: attempt 状态（running/succeeded/partial/failed/interrupted）。
        started_at: 开始时间。
        finished_at: 结束时间。
        completed_stages: 已完成的检索阶段列表（local/first_party/second_hand/web）。
        seen_document_ids: 已处理的文档 ID 集合。
        saved_evidence_ids: 已保存的证据 ID 集合。
        consumed_budget: 已消耗的预算快照（dict 形式）。
        failure_type: 失败类型（budget_exhausted/first_party_unavailable/exception/...）。
        failure_detail: 失败详情。
        next_action: 下一步动作（resume_first_party/continue_second_hand/...）。
        created_at: 记录创建时间。
    """

    attempt_id: str = field(default_factory=lambda: new_id("att"))
    task_id: str = ""
    run_id: str = ""
    target_claim_id: str = ""
    attempt_number: int = 0
    status: str = "running"
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    completed_stages: list[str] = field(default_factory=list)
    seen_document_ids: list[str] = field(default_factory=list)
    saved_evidence_ids: list[str] = field(default_factory=list)
    consumed_budget: dict[str, Any] = field(default_factory=dict)
    failure_type: str = ""
    failure_detail: str = ""
    next_action: str = ""
    created_at: str = field(default_factory=utc_now)
    checkpoint_version: int = 1
    current_stage: str = ""
    completed_query_keys: list[str] = field(default_factory=list)
    seen_document_fingerprints: list[str] = field(default_factory=list)
    budget_by_date: dict[str, Any] = field(default_factory=dict)
    source_status: dict[str, Any] = field(default_factory=dict)
    retrieval_stats: dict[str, Any] = field(default_factory=dict)
    degradation_codes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.attempt_id = _safe_text(self.attempt_id) or new_id("att")
        self.task_id = _safe_text(self.task_id)
        self.run_id = _safe_text(self.run_id)
        self.target_claim_id = _safe_text(self.target_claim_id)
        self.attempt_number = _safe_int(self.attempt_number, 0, minimum=0)
        self.status = _safe_text(self.status, "running")
        self.started_at = _safe_text(self.started_at)
        self.finished_at = _safe_text(self.finished_at)
        self.completed_stages = _unique_texts(self.completed_stages)
        self.seen_document_ids = _unique_texts(self.seen_document_ids)
        self.saved_evidence_ids = _unique_texts(self.saved_evidence_ids)
        self.consumed_budget = _as_mapping(self.consumed_budget)
        self.failure_type = _safe_text(self.failure_type)
        self.failure_detail = _safe_text(self.failure_detail)
        self.next_action = _safe_text(self.next_action)
        self.created_at = _safe_text(self.created_at) or utc_now()
        self.checkpoint_version = _safe_int(self.checkpoint_version, 1, minimum=1)
        self.current_stage = _safe_text(self.current_stage)
        self.completed_query_keys = _unique_texts(self.completed_query_keys)
        self.seen_document_fingerprints = _unique_texts(
            self.seen_document_fingerprints
        )
        self.budget_by_date = _as_mapping(self.budget_by_date)
        self.source_status = _as_mapping(self.source_status)
        self.retrieval_stats = _as_mapping(self.retrieval_stats)
        self.degradation_codes = _unique_texts(self.degradation_codes)

    def to_checkpoint(self) -> dict[str, Any]:
        """转换为 ResearchTask.checkpoint 字典格式。

        Returns:
            可直接赋值给 ResearchTask.checkpoint 的字典。
        """
        return {
            "checkpoint_version": self.checkpoint_version,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "current_stage": self.current_stage,
            "completed_stages": list(self.completed_stages),
            "completed_query_keys": list(self.completed_query_keys),
            "seen_document_ids": list(self.seen_document_ids),
            "seen_document_fingerprints": list(self.seen_document_fingerprints),
            "saved_evidence_ids": list(self.saved_evidence_ids),
            "consumed_budget": dict(self.consumed_budget),
            "budget_by_date": dict(self.budget_by_date),
            "source_status": dict(self.source_status),
            "retrieval_stats": dict(self.retrieval_stats),
            "degradation_codes": list(self.degradation_codes),
            "failure_type": self.failure_type,
            "failure_detail": self.failure_detail,
            "next_action": self.next_action,
        }


class TaskResumer:
    """跨日续跑恢复器。

    启动时扫描状态为 running/partial/retrying 但 finished_at 为空的任务，
    从最新 attempt 的 checkpoint 恢复跨日续跑所需的状态。

    恢复规则：
    - 不重复创建同一 Claim（通过 target_claim_id 去重）
    - 从最新 attempt 的 checkpoint 恢复已完成阶段/已见文档/已消耗预算
    - 标记 task 为 queued 等待下次执行
    - 递增 attempt_number，但由 execute() 创建唯一的新 attempt
    """

    # 可恢复的状态集合（未完成的任务）
    RESUMABLE_STATUSES: frozenset[str] = frozenset({
        TaskStatus.RUNNING.value,
        TaskStatus.PARTIAL.value,
        TaskStatus.RETRYING.value,
    })

    def __init__(self, repository: ResearchRepository) -> None:
        """初始化恢复器。

        Args:
            repository: ResearchRepository 实例。
        """
        self.repository = repository

    @staticmethod
    def _is_active_attempt(attempt: ResearchTaskAttempt | None) -> bool:
        return bool(
            attempt is not None
            and attempt.status in TaskResumer.RESUMABLE_STATUSES
            and not attempt.finished_at
        )

    @staticmethod
    def _checkpoint(task: ResearchTask) -> dict[str, Any]:
        return _as_mapping(getattr(task, "checkpoint", {}))

    @staticmethod
    def _load_task(raw: Any) -> ResearchTask | None:
        payload = _decode_mapping(raw)
        if not payload or not _safe_text(payload.get("task_id")):
            return None
        try:
            return ResearchTask.from_dict(payload)
        except (TypeError, ValueError, OverflowError):
            logger.warning("skip malformed research task payload")
            return None

    @staticmethod
    def _load_attempt(raw: Any) -> ResearchTaskAttempt | None:
        payload = _decode_mapping(raw)
        if payload is None:
            return None
        try:
            return ResearchTaskAttempt.from_dict(payload)
        except (TypeError, ValueError, OverflowError):
            logger.warning("skip malformed research attempt payload")
            return None

    def _attempt_rows(
        self, task_id: str, *, conn: Any | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        if conn is None:
            with self.repository._connect() as connection:
                rows = connection.execute(
                    "SELECT rowid, payload_json FROM task_attempts "
                    "WHERE task_id=? ORDER BY rowid DESC",
                    (task_id,),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT rowid, payload_json FROM task_attempts "
                "WHERE task_id=? ORDER BY rowid DESC",
                (task_id,),
            ).fetchall()
        decoded: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            payload = _decode_mapping(row[1])
            if payload is not None:
                decoded.append((int(row[0]), payload))
        return decoded

    @staticmethod
    def _latest_attempt(
        rows: list[tuple[int, dict[str, Any]]],
    ) -> ResearchTaskAttempt | None:
        candidates: list[tuple[int, int, ResearchTaskAttempt]] = []
        for rowid, payload in rows:
            attempt = TaskResumer._load_attempt(payload)
            if attempt is not None:
                candidates.append((attempt.attempt_number, rowid, attempt))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def list_resumable_tasks(self) -> list[ResearchTask]:
        """列出所有可恢复的未完成任务。

        Returns:
            状态为 running/partial/retrying 的 ResearchTask 列表。
        """
        with self.repository._connect() as conn:
            rows = conn.execute(
                "SELECT t.task_id, t.status, t.payload_json FROM research_tasks t "
                "WHERE t.status IN ('running', 'partial', 'retrying') "
                "OR (t.status=? AND EXISTS ("
                "SELECT 1 FROM task_attempts a WHERE a.task_id=t.task_id "
                "AND json_extract(CASE WHEN json_valid(a.payload_json) "
                "THEN a.payload_json ELSE '{}' END, '$.status') "
                "IN ('running', 'partial', 'retrying') "
                "AND COALESCE(json_extract(CASE WHEN json_valid(a.payload_json) "
                "THEN a.payload_json ELSE '{}' END, '$.finished_at'), '')=''"
                ")) ORDER BY t.created_at, t.task_id LIMIT 500",
                (TaskStatus.QUEUED.value,),
            ).fetchall()

        tasks: list[ResearchTask] = []
        for task_id, row_status, raw in rows:
            task = self._load_task(raw)
            if task is None:
                continue
            if row_status not in self.RESUMABLE_STATUSES and row_status != TaskStatus.QUEUED.value:
                continue
            task.status = row_status
            tasks.append(task)
        return tasks

    def find_latest_attempt(self, task_id: str) -> ResearchTaskAttempt | None:
        """查询某 task 的最新 attempt 记录。

        Args:
            task_id: ResearchTask ID。

        Returns:
            最新的 ResearchTaskAttempt；无记录时返回 None。
        """
        attempts = self.list_attempts(task_id, limit=1)
        return attempts[0] if attempts else None

    def list_attempts(
        self, task_id: str, limit: int = 100
    ) -> list[ResearchTaskAttempt]:
        """列出某 task 的所有 attempt 记录（按创建时间降序）。

        Args:
            task_id: ResearchTask ID。
            limit: 最大返回数量。

        Returns:
            ResearchTaskAttempt 列表。
        """
        safe_limit = max(1, min(_safe_int(limit, 100), 1000))
        rows = self._attempt_rows(task_id)
        attempts = [
            attempt
            for _, payload in rows
            if (attempt := self._load_attempt(payload)) is not None
        ]
        attempts.sort(key=lambda item: item.attempt_number, reverse=True)
        return attempts[:safe_limit]

    def save_attempt(self, attempt: ResearchTaskAttempt) -> None:
        """保存 attempt 记录。

        Args:
            attempt: ResearchTaskAttempt 实例。
        """
        if not isinstance(attempt, ResearchTaskAttempt):
            raise TypeError("attempt must be a ResearchTaskAttempt")
        normalized = ResearchTaskAttempt.from_dict(attempt.to_dict())
        attempt.__dict__.update(normalized.__dict__)
        self.repository._upsert(
            "task_attempts",
            "attempt_id",
            attempt.attempt_id,
            {"task_id": attempt.task_id, "created_at": attempt.created_at},
            attempt.to_dict(),
        )

    def resume_task(self, task: ResearchTask) -> ResearchTask:
        """恢复未完成的 task，准备跨日续跑。

        恢复流程：
        1. 查找最新 attempt 记录
        2. 从 attempt.checkpoint 恢复 task.checkpoint
        3. 递增 attempt_number
        4. 关闭旧 active attempt，不预建新 attempt
        5. 标记 task.status=queued 等待 execute() 执行

        Args:
            task: 待恢复的 ResearchTask。

        Returns:
            更新后的 ResearchTask（已写回数据库）。
        """
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        with self.repository._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json FROM research_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            persisted = self._load_task(row[0]) if row is not None else None
            if persisted is None:
                persisted = ResearchTask.from_dict(task.to_dict())
                persisted.task_id = task.task_id
            attempt_rows = self._attempt_rows(task.task_id, conn=conn)
            latest_attempt = self._latest_attempt(attempt_rows)
            if latest_attempt is not None:
                persisted.checkpoint = latest_attempt.to_checkpoint()
                persisted.attempt_number = max(
                    _safe_int(persisted.attempt_number, 0, minimum=0),
                    latest_attempt.attempt_number,
                ) + 1
            else:
                persisted.attempt_number = (
                    _safe_int(persisted.attempt_number, 0, minimum=0) + 1
                )

            now = utc_now()
            for rowid, payload in attempt_rows:
                attempt = self._load_attempt(payload)
                if not self._is_active_attempt(attempt):
                    continue
                payload.update(
                    {
                        "status": "interrupted",
                        "finished_at": now,
                        "failure_type": "stale_recovered",
                        "failure_detail": "checkpoint restored; queued for resume",
                        "next_action": "resume",
                    }
                )
                conn.execute(
                    "UPDATE task_attempts SET payload_json=? WHERE rowid=?",
                    (json.dumps(payload, ensure_ascii=False, allow_nan=False), rowid),
                )
            persisted.attempt_id = ""
            persisted.params = _as_mapping(persisted.params)
            persisted.params.pop("_legacy_resume_placeholder", None)
            persisted.params["_resume_pending"] = True
            persisted.attempt_started_at = ""
            persisted.attempt_finished_at = ""
            persisted.status = TaskStatus.QUEUED.value
            persisted.progress = {
                "percent": 0,
                "message": (
                    f"跨日续跑，从 attempt "
                    f"{max(0, persisted.attempt_number - 1)} 恢复"
                ),
            }
            persisted.updated_at = now
            payload = json.dumps(
                persisted.to_dict(), ensure_ascii=False, allow_nan=False
            )
            if row is None:
                conn.execute(
                    "INSERT INTO research_tasks "
                    "(task_id, status, created_at, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        persisted.task_id,
                        persisted.status,
                        persisted.created_at,
                        payload,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE research_tasks SET status=?, payload_json=? WHERE task_id=?",
                    (persisted.status, payload, persisted.task_id),
                )
        task.__dict__.update(persisted.__dict__)
        return task

    def start_attempt(self, task: ResearchTask) -> ResearchTaskAttempt:
        """Create the one real running attempt for ``execute()``."""
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        if task.attempt_id and task.params.get("_legacy_resume_placeholder"):
            attempt = self._get_attempt(task.attempt_id)
            if attempt is not None:
                attempt.status = "running"
                attempt.started_at = task.attempt_started_at or utc_now()
                attempt.finished_at = ""
                attempt.next_action = ""
                self.save_attempt(attempt)
                return attempt
        checkpoint = self._checkpoint(task)
        attempt = ResearchTaskAttempt(
            attempt_id=task.attempt_id or new_id("att"),
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id=task.target_claim_id,
            attempt_number=task.attempt_number,
            status="running",
            started_at=task.attempt_started_at or utc_now(),
            completed_stages=_text_list(checkpoint.get("completed_stages")),
            seen_document_ids=_text_list(checkpoint.get("seen_document_ids")),
            saved_evidence_ids=_text_list(checkpoint.get("saved_evidence_ids")),
            consumed_budget=_as_mapping(checkpoint.get("consumed_budget")),
            current_stage=_safe_text(checkpoint.get("current_stage")),
            completed_query_keys=_text_list(checkpoint.get("completed_query_keys")),
            seen_document_fingerprints=_text_list(
                checkpoint.get("seen_document_fingerprints")
            ),
            budget_by_date=_as_mapping(checkpoint.get("budget_by_date")),
            source_status=_as_mapping(checkpoint.get("source_status")),
            retrieval_stats=_as_mapping(checkpoint.get("retrieval_stats")),
            degradation_codes=_text_list(checkpoint.get("degradation_codes")),
        )
        task.attempt_id = attempt.attempt_id
        self.save_attempt(attempt)
        return attempt

    def claim_attempt(self, task: ResearchTask) -> ResearchTaskAttempt | None:
        """Atomically claim a queued task and create its one running attempt."""
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        with self.repository._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_json FROM research_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            if row is None:
                persisted = ResearchTask.from_dict(task.to_dict())
                conn.execute(
                    "INSERT INTO research_tasks "
                    "(task_id, status, created_at, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        persisted.task_id,
                        persisted.status,
                        persisted.created_at,
                        json.dumps(
                            persisted.to_dict(), ensure_ascii=False, allow_nan=False
                        ),
                    ),
                )
            else:
                persisted = self._load_task(row[0])
                if persisted is None:
                    persisted = ResearchTask.from_dict(task.to_dict())
                    persisted.task_id = task.task_id
            if persisted.status != TaskStatus.QUEUED.value:
                return None
            attempt_rows = self._attempt_rows(task.task_id, conn=conn)
            if any(
                self._is_active_attempt(self._load_attempt(payload))
                for _, payload in attempt_rows
            ):
                return None
            max_attempt = max(
                (
                    _safe_int(payload.get("attempt_number"), 0, minimum=0)
                    for _, payload in attempt_rows
                ),
                default=0,
            )
            persisted.params = _as_mapping(persisted.params)
            resume_pending = _safe_bool(
                persisted.params.pop("_resume_pending", False)
            )
            persisted.params.pop("_legacy_resume_placeholder", None)
            persisted_attempt_number = _safe_int(
                persisted.attempt_number, 0, minimum=0
            )
            if resume_pending and persisted_attempt_number > max_attempt:
                attempt_number = persisted_attempt_number
            else:
                attempt_number = max(max_attempt, persisted_attempt_number) + 1

            persisted.attempt_number = attempt_number
            persisted.attempt_id = new_id("att")
            persisted.attempt_started_at = utc_now()
            persisted.attempt_finished_at = ""
            persisted.status = TaskStatus.RUNNING.value
            persisted.started_at = persisted.started_at or utc_now()
            persisted.progress = {"percent": 1, "message": "开始统一检索"}
            checkpoint = self._checkpoint(persisted)
            attempt = ResearchTaskAttempt(
                attempt_id=persisted.attempt_id,
                task_id=persisted.task_id,
                run_id=persisted.run_id,
                target_claim_id=persisted.target_claim_id,
                attempt_number=attempt_number,
                status="running",
                started_at=persisted.attempt_started_at,
                completed_stages=_text_list(checkpoint.get("completed_stages")),
                seen_document_ids=_text_list(checkpoint.get("seen_document_ids")),
                saved_evidence_ids=_text_list(checkpoint.get("saved_evidence_ids")),
                consumed_budget=_as_mapping(checkpoint.get("consumed_budget")),
                current_stage=_safe_text(checkpoint.get("current_stage")),
                completed_query_keys=_text_list(checkpoint.get("completed_query_keys")),
                seen_document_fingerprints=_text_list(
                    checkpoint.get("seen_document_fingerprints")
                ),
                budget_by_date=_as_mapping(checkpoint.get("budget_by_date")),
                source_status=_as_mapping(checkpoint.get("source_status")),
                retrieval_stats=_as_mapping(checkpoint.get("retrieval_stats")),
                degradation_codes=_text_list(checkpoint.get("degradation_codes")),
            )
            conn.execute(
                "INSERT INTO task_attempts "
                "(attempt_id, task_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.created_at,
                    json.dumps(
                        attempt.to_dict(), ensure_ascii=False, allow_nan=False
                    ),
                ),
            )
            conn.execute(
                "UPDATE research_tasks SET status=?, payload_json=? WHERE task_id=?",
                (
                    persisted.status,
                    json.dumps(
                        persisted.to_dict(), ensure_ascii=False, allow_nan=False
                    ),
                    persisted.task_id,
                ),
            )
        task.__dict__.update(persisted.__dict__)
        return attempt

    def _get_attempt(self, attempt_id: str) -> ResearchTaskAttempt | None:
        attempt_id = _safe_text(attempt_id)
        if not attempt_id:
            return None
        with self.repository._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM task_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if not row:
            return None
        return self._load_attempt(row[0])

    def update_checkpoint(
        self,
        task: ResearchTask,
        *,
        current_stage: str = "",
        completed_stages: list[str] | None = None,
        completed_query_keys: list[str] | None = None,
        seen_document_ids: list[str] | None = None,
        seen_document_fingerprints: list[str] | None = None,
        saved_evidence_ids: list[str] | None = None,
        consumed_budget: dict[str, Any] | None = None,
        budget_by_date: dict[str, Any] | None = None,
        source_status: dict[str, Any] | None = None,
        retrieval_stats: dict[str, Any] | None = None,
        degradation_codes: list[str] | None = None,
        failure_type: str = "",
        next_action: str = "",
    ) -> ResearchTaskAttempt | None:
        """Persist an in-flight checkpoint by upserting the same attempt."""
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        attempt = self._get_attempt(task.attempt_id) if task.attempt_id else None
        if attempt is None:
            attempt = self.start_attempt(task)
        attempt.status = "running"
        attempt.current_stage = _safe_text(current_stage) or attempt.current_stage
        if completed_stages is not None:
            attempt.completed_stages = _unique_texts(completed_stages)
        if completed_query_keys is not None:
            attempt.completed_query_keys = _unique_texts(completed_query_keys)
        if seen_document_ids is not None:
            attempt.seen_document_ids = _unique_texts(seen_document_ids)
        if seen_document_fingerprints is not None:
            attempt.seen_document_fingerprints = _unique_texts(
                seen_document_fingerprints
            )
        if saved_evidence_ids is not None:
            attempt.saved_evidence_ids = _unique_texts(saved_evidence_ids)
        if consumed_budget is not None:
            attempt.consumed_budget = _as_mapping(consumed_budget)
        if budget_by_date is not None:
            attempt.budget_by_date = _as_mapping(budget_by_date)
        if source_status is not None:
            attempt.source_status = _as_mapping(source_status)
        if retrieval_stats is not None:
            attempt.retrieval_stats = _as_mapping(retrieval_stats)
        if degradation_codes is not None:
            attempt.degradation_codes = _unique_texts(degradation_codes)
        attempt.failure_type = _safe_text(failure_type)
        attempt.next_action = _safe_text(next_action)
        self.save_attempt(attempt)
        task.checkpoint = attempt.to_checkpoint()
        self.repository.save_task(task)
        return attempt

    def is_claim_already_researched(self, claim_id: str) -> bool:
        """检查某 Claim 是否已有进行中的 task（避免重复创建）。

        Args:
            claim_id: Claim ID。

        Returns:
            True 表示已有 running/partial/retrying/queued 状态的 task 关联此 Claim。
        """
        if not claim_id:
            return False
        # Check both task state and attempt state.  A queued task is already
        # owned by the claim even though it has not started network work.
        with self.repository._connect() as conn:
            task_row = conn.execute(
                "SELECT 1 FROM research_tasks "
                "WHERE status IN ('queued', 'running', 'retrying', 'partial') "
                "AND json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.target_claim_id')=? "
                "LIMIT 1",
                (claim_id,),
            ).fetchone()
            attempt_row = conn.execute(
                "SELECT 1 FROM task_attempts "
                "WHERE json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.target_claim_id')=? "
                "AND json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.status') "
                "IN ('running', 'partial', 'retrying') "
                "AND COALESCE(json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.finished_at'), '')='' "
                "LIMIT 1",
                (claim_id,),
            ).fetchone()
        return task_row is not None or attempt_row is not None

    def active_task_id_for_claim(self, claim_id: str) -> str:
        """Return an existing active task/attempt owner for a Claim, if any."""
        if not claim_id:
            return ""
        with self.repository._connect() as conn:
            task_row = conn.execute(
                "SELECT payload_json FROM research_tasks "
                "WHERE status IN ('queued', 'running', 'retrying', 'partial') "
                "AND json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.target_claim_id')=? "
                "ORDER BY rowid DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
            if task_row:
                payload = _decode_mapping(task_row[0])
                if payload:
                    return _safe_text(payload.get("task_id"))
            attempt_row = conn.execute(
                "SELECT payload_json FROM task_attempts "
                "WHERE json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.target_claim_id')=? "
                "AND json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.status') "
                "IN ('running', 'partial', 'retrying') "
                "AND COALESCE(json_extract(CASE WHEN json_valid(payload_json) "
                "THEN payload_json ELSE '{}' END, '$.finished_at'), '')='' "
                "ORDER BY rowid DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
        if attempt_row:
            attempt = self._load_attempt(attempt_row[0])
            if attempt is not None and self._is_active_attempt(attempt):
                return attempt.task_id
        return ""

    def finalize_attempt(
        self,
        task: ResearchTask,
        *,
        status: str,
        completed_stages: list[str],
        seen_document_ids: list[str],
        saved_evidence_ids: list[str],
        consumed_budget: dict[str, Any],
        failure_type: str = "",
        failure_detail: str = "",
        next_action: str = "",
    ) -> ResearchTaskAttempt:
        """完成 attempt 记录，写入最终状态。

        Args:
            task: 关联的 ResearchTask。
            status: attempt 最终状态（succeeded/partial/failed/interrupted）。
            completed_stages: 已完成的检索阶段列表。
            seen_document_ids: 已处理的文档 ID 集合。
            saved_evidence_ids: 已保存的证据 ID 集合。
            consumed_budget: 已消耗的预算快照。
            failure_type: 失败类型（成功时为空）。
            failure_detail: 失败详情（成功时为空）。
            next_action: 下一步动作建议。

        Returns:
            更新后的 ResearchTaskAttempt。
        """
        if not isinstance(task, ResearchTask):
            raise TypeError("task must be a ResearchTask")
        checkpoint = self._checkpoint(task)
        attempt_id = _safe_text(task.attempt_id)
        if not attempt_id:
            # 兼容：task 未设置 attempt_id 时新建
            attempt_id = new_id("att")
            task.attempt_id = attempt_id

        attempt = ResearchTaskAttempt(
            attempt_id=attempt_id,
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id=task.target_claim_id,
            attempt_number=task.attempt_number,
            status=_safe_text(status, "failed"),
            started_at=task.attempt_started_at or utc_now(),
            finished_at=utc_now(),
            completed_stages=_unique_texts(completed_stages),
            seen_document_ids=_unique_texts(seen_document_ids),
            saved_evidence_ids=_unique_texts(saved_evidence_ids),
            consumed_budget=_as_mapping(consumed_budget),
            failure_type=_safe_text(failure_type),
            failure_detail=_safe_text(failure_detail),
            next_action=_safe_text(next_action),
            checkpoint_version=_safe_int(
                checkpoint.get("checkpoint_version"), 1, minimum=1
            ),
            current_stage=_safe_text(checkpoint.get("current_stage")),
            completed_query_keys=_text_list(checkpoint.get("completed_query_keys")),
            seen_document_fingerprints=_text_list(
                checkpoint.get("seen_document_fingerprints")
            ),
            budget_by_date=_as_mapping(checkpoint.get("budget_by_date")),
            source_status=_as_mapping(checkpoint.get("source_status")),
            retrieval_stats=_as_mapping(checkpoint.get("retrieval_stats")),
            degradation_codes=_text_list(checkpoint.get("degradation_codes")),
        )
        self.save_attempt(attempt)

        # 同步更新 task.checkpoint
        task.checkpoint = attempt.to_checkpoint()
        task.attempt_finished_at = attempt.finished_at
        # Persist the final checkpoint together with the attempt.  Otherwise
        # reloading the task can expose the previous in-flight checkpoint.
        self.repository.save_task(task)
        return attempt
