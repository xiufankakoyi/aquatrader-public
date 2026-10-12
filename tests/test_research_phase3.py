"""Phase 3: Research 产能与第一方检索专线测试。

测试覆盖：
- Task 3.1: 本地检索短路器（LocalEvidenceRetriever）
- Task 3.2: 第一方专线编排器（FirstPartyRetriever）+ 来源状态机
- Task 3.3: 全局预算管理器（GlobalBudgetManager）+ 任务最低保障
- Task 3.4: ResearchTask attempt/run + checkpoint + TaskResumer
- Task 3.5: 搜索结果必须经过 Evidence Evaluator（网页摘要不能成证据）

约束：
- 不删除、跳过、xfail 或放宽现有测试
- 不修改 Claim 事实置信度
- 龙虎榜/K线/成交数据不进入事实 Evidence
- 跨日续跑不重复创建同一 Claim
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.research.control_plane.budget_manager import (
    BudgetAllocationResult,
    GlobalBudgetManager,
    GlobalBudgetSnapshot,
)
from core.research.control_plane.contracts import (
    Claim,
    ClaimStatus,
    EvidenceRecord,
    EvidenceStance,
    ResearchTask,
    TaskBudget,
    TaskStatus,
    utc_now,
)
from core.research.control_plane.first_party_retriever import (
    CompanyIRSiteProvider,
    FirstPartyRetrievalResult,
    FirstPartyRetriever,
    InteractivePlatformProvider,
    SourceStatus,
    SseInteractiveProvider,
    SzseInteractiveProvider,
)
from core.research.control_plane.local_retrieval import (
    LocalEvidenceRetriever,
    LocalRetrievalResult,
)
from core.research.control_plane.orchestrator import (
    BudgetMeter,
    ExecutionSummary,
    ResearchOrchestrator,
    STOP_REASON_CODES,
)
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.task_resumer import (
    ResearchTaskAttempt,
    TaskResumer,
)


# ======================================================================
# 测试辅助
# ======================================================================

def _make_temp_repo() -> ResearchRepository:
    """创建临时 ResearchRepository。"""
    tmp_dir = tempfile.mkdtemp(prefix="phase3_test_")
    return ResearchRepository(db_path=Path(tmp_dir) / "test.db")


def _make_claim(
    claim_id: str = "claim_test",
    research_status: str = ClaimStatus.DISCOVERED.value,
    counter_search_completed: bool = False,
    conflict_status: str = "none",
) -> Claim:
    """创建测试 Claim。"""
    return Claim(
        claim_id=claim_id,
        subject_id="600000",
        subject_name="测试公司",
        relation="related_to",
        object_id="theme_test",
        object_name="测试主题",
        research_status=research_status,
        counter_search_completed=counter_search_completed,
        conflict_status=conflict_status,
    )


def _make_evidence(
    evidence_id: str = "ev_test",
    claim_id: str = "claim_test",
    stance: str = EvidenceStance.SUPPORT.value,
    lineage_id: str = "lineage_1",
    is_first_party: bool = False,
    quote: str = "测试引用",
    grounding_status: str = "exact",
) -> EvidenceRecord:
    """创建测试 Evidence。"""
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id=claim_id,
        stance=stance,
        lineage_id=lineage_id,
        is_first_party=is_first_party,
        quote=quote,
        grounding_status=grounding_status,
        source_trace_id=f"src_{evidence_id}",
        evidence_kind="document",
    )


# ======================================================================
# Task 3.1: 本地检索短路器测试
# ======================================================================

class TestLocalEvidenceRetriever:
    """本地检索短路器测试。"""

    def test_local_empty_returns_no_hit(self) -> None:
        """本地无证据时返回 local_empty。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        retriever = LocalEvidenceRetriever(repo)
        result = retriever.query(claim)
        assert not result.hit
        assert result.reason_code == "local_empty"

    def test_local_partial_returns_no_hit(self) -> None:
        """本地证据不足时返回 local_partial。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        # 只有一条非第一方证据，独立来源不足
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1", is_first_party=False
        ))
        retriever = LocalEvidenceRetriever(repo)
        result = retriever.query(claim)
        assert not result.hit
        assert result.reason_code == "local_partial"
        assert len(result.evidence) == 1

    def test_local_sufficient_returns_hit(self) -> None:
        """本地有合格证据时返回 local_sufficient。"""
        repo = _make_temp_repo()
        claim = _make_claim(counter_search_completed=True)
        repo.save_claim(claim)
        # 2 条独立来源 + 1 条第一方 + support 证据
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1",
            is_first_party=True, stance=EvidenceStance.SUPPORT.value,
        ))
        repo.save_evidence(_make_evidence(
            evidence_id="ev_2", lineage_id="lineage_2",
            is_first_party=False, stance=EvidenceStance.SUPPORT.value,
        ))
        retriever = LocalEvidenceRetriever(repo)
        result = retriever.query(claim)
        assert result.hit
        assert result.reason_code == "local_sufficient"
        assert result.independent_sources == 2
        assert result.first_party_sources == 1
        assert result.support_count == 2
        assert "first_party" in result.skipped_stages

    def test_local_query_failed_on_exception(self) -> None:
        """repository 异常时降级为 local_query_failed。"""
        repo = MagicMock()
        repo.list_evidence.side_effect = RuntimeError("DB error")
        retriever = LocalEvidenceRetriever(repo)
        claim = _make_claim()
        result = retriever.query(claim)
        assert not result.hit
        assert result.reason_code == "local_query_failed"

    def test_local_does_not_modify_claim_status(self) -> None:
        """本地检索只读，不修改 Claim 状态。"""
        repo = _make_temp_repo()
        claim = _make_claim(research_status=ClaimStatus.DISCOVERED.value)
        repo.save_claim(claim)
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1", is_first_party=True
        ))
        retriever = LocalEvidenceRetriever(repo)
        original_status = claim.research_status
        retriever.query(claim)
        # Claim 状态未被修改
        assert claim.research_status == original_status


# ======================================================================
# Task 3.2: 第一方专线编排器测试
# ======================================================================

class TestFirstPartyRetriever:
    """第一方专线编排器测试。"""

    def test_first_party_no_providers_returns_unavailable(self) -> None:
        """无 provider 配置时返回 first_party_unavailable。"""
        retriever = FirstPartyRetriever()
        result = retriever.search("600000 测试主题", limit=5)
        assert not result.documents
        # 无 provider 时 skipped_providers 为空，但 documents 也为空
        assert result.reason_code in (
            "first_party_no_results", "first_party_unavailable"
        )

    def test_first_party_provider_failure_marks_degraded(self) -> None:
        """provider 调用失败时标记 degraded。"""
        failing_provider = MagicMock()
        failing_provider.search.side_effect = RuntimeError("network error")
        failing_provider.name = "failing_provider"
        retriever = FirstPartyRetriever(
            interactive_providers=[failing_provider]
        )
        result = retriever.search("600000 测试", limit=5)
        assert "failing_provider" in result.skipped_providers
        status = result.source_status.get("failing_provider")
        assert status is not None
        assert status.consecutive_failures == 1

    def test_first_party_consecutive_failures_marks_unavailable(self) -> None:
        """连续失败超过阈值后标记 unavailable。"""
        failing_provider = MagicMock()
        failing_provider.search.side_effect = RuntimeError("network error")
        failing_provider.name = "failing_provider"
        retriever = FirstPartyRetriever(
            interactive_providers=[failing_provider],
        )
        retriever.MAX_CONSECUTIVE_FAILURES = 2
        # 连续失败 2 次
        retriever.search("600000 测试", limit=5)
        retriever.search("600000 测试", limit=5)
        status = retriever.get_status().get("failing_provider")
        assert status is not None
        assert status.status == "unavailable"

    def test_first_party_empty_query_returns_no_terms(self) -> None:
        """空查询返回 first_party_no_terms。"""
        retriever = FirstPartyRetriever()
        result = retriever.search("", limit=5)
        assert result.reason_code == "first_party_no_terms"

    def test_first_party_deduplicates_by_url(self) -> None:
        """相同 URL 的文档去重。"""
        from core.research.perilla.discovery.models import SourceDocument

        doc1 = SourceDocument(
            url="http://example.com/doc1",
            title="文档1",
            content_text="内容1",
        )
        doc2 = SourceDocument(
            url="http://example.com/doc1",  # 相同 URL
            title="文档1重复",
            content_text="内容2",
        )
        doc3 = SourceDocument(
            url="http://example.com/doc2",
            title="文档2",
            content_text="内容3",
        )
        provider = MagicMock()
        provider.search.return_value = [doc1, doc2, doc3]
        provider.name = "test_provider"
        retriever = FirstPartyRetriever(interactive_providers=[provider])
        result = retriever.search("600000 测试", limit=5)
        # doc2 应被去重
        assert len(result.documents) == 2

    def test_interactive_platform_provider_returns_empty(self) -> None:
        """互动平台 provider 占位实现返回空列表。"""
        provider = SseInteractiveProvider()
        result = provider.search("600000 测试", limit=5)
        assert result == []

    def test_company_ir_site_provider_returns_empty(self) -> None:
        """公司官网 provider 占位实现返回空列表。"""
        provider = CompanyIRSiteProvider()
        result = provider.search("600000 测试", limit=5)
        assert result == []


# ======================================================================
# Task 3.3: 全局预算管理器测试
# ======================================================================

class TestGlobalBudgetManager:
    """全局预算管理器测试。"""

    def test_allocate_grants_when_sufficient(self) -> None:
        """全局余量足够时分配请求预算。"""
        manager = GlobalBudgetManager()
        requested = TaskBudget(max_requests=10, max_documents=20)
        result = manager.allocate(requested)
        assert result.granted
        assert result.reason_code == "budget_granted"
        assert result.allocated.max_requests == 10

    def test_allocate_denies_when_below_min(self) -> None:
        """全局余量低于最低保障时拒绝分配。"""
        # 设置一个很小的全局预算
        small_total = TaskBudget(max_requests=3, max_documents=5)
        manager = GlobalBudgetManager(
            daily_total=small_total,
            min_per_task=TaskBudget(max_requests=5, max_documents=10),
        )
        requested = TaskBudget(max_requests=10, max_documents=20)
        result = manager.allocate(requested)
        assert not result.granted
        assert result.reason_code == "budget_exhausted"

    def test_allocate_grants_min_when_insufficient(self) -> None:
        """全局余量不足但够最低保障时分配最低保障。"""
        # 全局余量 8，请求 10，最低 5
        total = TaskBudget(max_requests=8, max_documents=20)
        manager = GlobalBudgetManager(
            daily_total=total,
            min_per_task=TaskBudget(max_requests=5, max_documents=10),
        )
        requested = TaskBudget(max_requests=10, max_documents=20)
        result = manager.allocate(requested)
        assert result.granted
        assert result.reason_code == "budget_insufficient"
        # 分配 min(remaining=8, requested=10) = 8
        assert result.allocated.max_requests == 8

    def test_snapshot_returns_current_state(self) -> None:
        """snapshot 返回当前预算快照。"""
        manager = GlobalBudgetManager()
        manager.allocate(TaskBudget(max_requests=10))
        snapshot = manager.snapshot()
        assert snapshot.active_tasks == 1
        assert snapshot.consumed.max_requests == 10
        assert snapshot.remaining.max_requests == manager.daily_total.max_requests - 10

    def test_release_decrements_active_tasks(self) -> None:
        """release 减少活跃任务数。"""
        manager = GlobalBudgetManager()
        manager.allocate(TaskBudget(max_requests=10))
        assert manager.snapshot().active_tasks == 1
        manager.release(TaskBudget(max_requests=5))
        assert manager.snapshot().active_tasks == 0

    def test_single_task_cannot_consume_all_budget(self) -> None:
        """单任务不能吞噬全部预算（最低保障机制）。"""
        # 全局预算 100，最低保障 5，第一个任务请求 95
        # 所有维度保持一致，避免 TaskBudget 默认值干扰
        total = TaskBudget(
            max_requests=100, max_seconds=100.0,
            max_tokens=100, max_cost=100.0, max_documents=100,
        )
        manager = GlobalBudgetManager(
            daily_total=total,
            min_per_task=TaskBudget(
                max_requests=5, max_seconds=5.0,
                max_tokens=5, max_cost=5.0, max_documents=5,
            ),
        )
        # 第一个任务请求 95，分配 95
        r1 = manager.allocate(TaskBudget(
            max_requests=95, max_seconds=95.0,
            max_tokens=95, max_cost=95.0, max_documents=95,
        ))
        assert r1.granted
        # 剩余 5，第二个任务请求 10，但只能分配 5（最低保障）
        r2 = manager.allocate(TaskBudget(
            max_requests=10, max_seconds=10.0,
            max_tokens=10, max_cost=10.0, max_documents=10,
        ))
        assert r2.granted
        assert r2.reason_code == "budget_insufficient"
        assert r2.allocated.max_requests == 5
        # 第三个任务请求 5，但剩余 0，拒绝
        r3 = manager.allocate(TaskBudget(
            max_requests=5, max_seconds=5.0,
            max_tokens=5, max_cost=5.0, max_documents=5,
        ))
        assert not r3.granted


# ======================================================================
# Task 3.4: ResearchTask attempt + TaskResumer 测试
# ======================================================================

class TestResearchTaskAttempt:
    """ResearchTask attempt 模型测试。"""

    def test_research_task_has_attempt_fields(self) -> None:
        """ResearchTask 包含 attempt 相关字段。"""
        task = ResearchTask()
        assert hasattr(task, "run_id")
        assert hasattr(task, "attempt_number")
        assert hasattr(task, "attempt_id")
        assert hasattr(task, "attempt_started_at")
        assert hasattr(task, "attempt_finished_at")
        assert hasattr(task, "checkpoint")
        assert task.run_id  # 默认非空
        assert task.attempt_number == 0

    def test_attempt_to_checkpoint_returns_dict(self) -> None:
        """ResearchTaskAttempt.to_checkpoint 返回字典。"""
        attempt = ResearchTaskAttempt(
            attempt_id="att_1",
            task_id="rt_1",
            run_id="run_1",
            target_claim_id="claim_1",
            attempt_number=1,
            status="succeeded",
            completed_stages=["local", "first_party"],
            seen_document_ids=["doc_1", "doc_2"],
            saved_evidence_ids=["ev_1"],
            consumed_budget={"requests": 5},
            next_action="none",
        )
        checkpoint = attempt.to_checkpoint()
        assert isinstance(checkpoint, dict)
        assert checkpoint["attempt_id"] == "att_1"
        assert checkpoint["attempt_number"] == 1
        assert "local" in checkpoint["completed_stages"]
        assert "doc_1" in checkpoint["seen_document_ids"]


class TestTaskResumer:
    """TaskResumer 跨日续跑测试。"""

    def test_resume_task_does_not_precreate_attempt(self) -> None:
        """resume_task 只排队恢复，真实 attempt 由 execute 创建。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.RUNNING.value
        task.attempt_number = 1
        repo.save_task(task)

        resumer = TaskResumer(repo)
        resumed = resumer.resume_task(task)
        assert resumed.attempt_number == 2
        assert resumed.status == TaskStatus.QUEUED.value
        assert resumed.attempt_id == ""

        # 恢复本身不得留下幽灵 running attempt。
        attempts = resumer.list_attempts(task.task_id)
        assert attempts == []

    def test_resume_task_restores_checkpoint(self) -> None:
        """resume_task 从最新 attempt 恢复 checkpoint。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.PARTIAL.value
        task.attempt_number = 1
        task.checkpoint = {
            "completed_stages": ["local"],
            "seen_document_ids": ["doc_1"],
            "saved_evidence_ids": ["ev_1"],
            "consumed_budget": {"requests": 3},
        }
        repo.save_task(task)

        # 手动保存一个 attempt 记录
        attempt = ResearchTaskAttempt(
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id=task.target_claim_id,
            attempt_number=1,
            status="partial",
            completed_stages=["local"],
            seen_document_ids=["doc_1", "doc_2"],
            saved_evidence_ids=["ev_1"],
            consumed_budget={"requests": 3},
        )
        resumer = TaskResumer(repo)
        resumer.save_attempt(attempt)

        resumed = resumer.resume_task(task)
        # checkpoint 应从 attempt 恢复
        assert "doc_2" in resumed.checkpoint["seen_document_ids"]
        assert resumed.attempt_number == 2

    def test_resume_task_preserves_finished_partial_attempt(self) -> None:
        """已结束的 partial attempt 可供续跑，但历史状态不得被改写。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.PARTIAL.value
        repo.save_task(task)

        resumer = TaskResumer(repo)
        attempt = ResearchTaskAttempt(
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="partial",
            finished_at=utc_now(),
            seen_document_ids=["doc_finished"],
        )
        resumer.save_attempt(attempt)

        resumed = resumer.resume_task(task)
        assert resumed.status == TaskStatus.QUEUED.value
        stored = resumer.list_attempts(task.task_id)
        assert stored[0].status == "partial"
        assert stored[0].finished_at == attempt.finished_at

    def test_resume_task_closes_all_unfinished_attempts(self) -> None:
        """较新的已结束 attempt 不得遮蔽更早的幽灵 running attempt。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.QUEUED.value
        repo.save_task(task)

        resumer = TaskResumer(repo)
        stale = ResearchTaskAttempt(
            attempt_id="att_stale",
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="running",
        )
        finished = ResearchTaskAttempt(
            attempt_id="att_finished",
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=2,
            status="partial",
            finished_at=utc_now(),
            seen_document_ids=["doc_latest"],
        )
        resumer.save_attempt(finished)
        # 模拟旧 attempt 的迟到回填：物理 rowid 更新，但逻辑编号更旧。
        resumer.save_attempt(stale)

        resumable = resumer.list_resumable_tasks()
        assert [item.task_id for item in resumable] == [task.task_id]
        resumed = resumer.resume_task(resumable[0])

        attempts = {item.attempt_id: item for item in resumer.list_attempts(task.task_id)}
        assert attempts["att_stale"].status == "interrupted"
        assert attempts["att_finished"].status == "partial"
        assert resumed.checkpoint["seen_document_ids"] == ["doc_latest"]

    def test_resume_task_closes_unfinished_attempt_outside_recent_1000(self) -> None:
        """超过最近 1000 条窗口的幽灵 attempt 也必须一次性关闭。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.QUEUED.value
        repo.save_task(task)

        resumer = TaskResumer(repo)
        stale = ResearchTaskAttempt(
            attempt_id="att_old_stale",
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="running",
        )
        payloads = [stale] + [
            ResearchTaskAttempt(
                attempt_id=f"att_finished_{number}",
                task_id=task.task_id,
                target_claim_id=claim.claim_id,
                attempt_number=number,
                status="partial",
                finished_at=utc_now(),
            )
            for number in range(2, 1002)
        ]
        with repo._connect() as conn:
            conn.executemany(
                "INSERT INTO task_attempts "
                "(attempt_id, task_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (item.attempt_id, item.task_id, item.created_at,
                     __import__("json").dumps(item.to_dict(), ensure_ascii=False))
                    for item in payloads
                ],
            )

        resumable = resumer.list_resumable_tasks()
        resumer.resume_task(resumable[0])
        stored = resumer._get_attempt("att_old_stale")
        assert stored is not None
        assert stored.status == "interrupted"

    def test_resume_task_does_not_create_duplicate_claim(self) -> None:
        """is_claim_already_researched 检测重复 Claim。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        resumer = TaskResumer(repo)
        # 保存一个 running attempt
        attempt = ResearchTaskAttempt(
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="running",
        )
        resumer.save_attempt(attempt)
        assert resumer.is_claim_already_researched(claim.claim_id)
        assert not resumer.is_claim_already_researched("claim_other")

    def test_list_resumable_tasks(self) -> None:
        """list_resumable_tasks 列出未完成状态的任务。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        # 创建 3 个任务：running、partial、succeeded
        t1 = ResearchTask(target_claim_id=claim.claim_id)
        t1.status = TaskStatus.RUNNING.value
        repo.save_task(t1)
        t2 = ResearchTask(target_claim_id=claim.claim_id)
        t2.status = TaskStatus.PARTIAL.value
        repo.save_task(t2)
        t3 = ResearchTask(target_claim_id=claim.claim_id)
        t3.status = TaskStatus.SUCCEEDED.value
        repo.save_task(t3)

        resumer = TaskResumer(repo)
        resumable = resumer.list_resumable_tasks()
        task_ids = {t.task_id for t in resumable}
        assert t1.task_id in task_ids
        assert t2.task_id in task_ids
        assert t3.task_id not in task_ids

    def test_list_resumable_tasks_reconciles_queued_task_with_running_attempt(self) -> None:
        """queued task 若仍挂 active attempt，启动恢复必须能发现并关闭它。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.QUEUED.value
        repo.save_task(task)

        resumer = TaskResumer(repo)
        resumer.save_attempt(ResearchTaskAttempt(
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="running",
        ))

        resumable = resumer.list_resumable_tasks()
        assert [item.task_id for item in resumable] == [task.task_id]

        resumer.resume_task(resumable[0])
        attempts = resumer.list_attempts(task.task_id)
        assert attempts[0].status == "interrupted"

    def test_list_resumable_tasks_ignores_finished_partial_attempt(self) -> None:
        """已结束的 partial attempt 是历史结果，不得被启动恢复改写。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.QUEUED.value
        repo.save_task(task)

        resumer = TaskResumer(repo)
        resumer.save_attempt(ResearchTaskAttempt(
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="partial",
            finished_at=utc_now(),
        ))

        assert resumer.list_resumable_tasks() == []

    def test_resumer_skips_corrupt_payloads_and_normalizes_checkpoint(self) -> None:
        """坏持久化行不应阻断恢复，checkpoint 类型错误应安全降级。"""
        repo = _make_temp_repo()
        task = ResearchTask(task_id="task-corrupt-resume")
        task.status = TaskStatus.QUEUED.value
        task.checkpoint = {
            "completed_stages": "local",
            "seen_document_ids": None,
            "consumed_budget": ["bad"],
        }
        repo.save_task(task)
        resumer = TaskResumer(repo)
        valid = ResearchTaskAttempt(
            task_id=task.task_id,
            attempt_number=1,
            status="partial",
            finished_at=utc_now(),
        )
        with repo._connect() as conn:
            conn.execute(
                "INSERT INTO task_attempts "
                "(attempt_id, task_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
                ("att-corrupt", task.task_id, utc_now(), "[]"),
            )
        resumer.save_attempt(valid)

        attempts = resumer.list_attempts(task.task_id)
        assert [item.attempt_id for item in attempts] == [valid.attempt_id]
        started = resumer.start_attempt(task)
        assert started.completed_stages == []
        assert started.seen_document_ids == []
        assert started.consumed_budget == {}

    def test_resumer_skips_corrupt_task_payload(self) -> None:
        """坏 task 行不会让启动恢复流程直接抛 JSONDecodeError。"""
        repo = _make_temp_repo()
        with repo._connect() as conn:
            conn.execute(
                "INSERT INTO research_tasks "
                "(task_id, status, created_at, payload_json) VALUES (?, ?, ?, ?)",
                ("task-corrupt", TaskStatus.RUNNING.value, utc_now(), "[]"),
            )

        assert TaskResumer(repo).list_resumable_tasks() == []

    def test_finalize_attempt_writes_final_state(self) -> None:
        """finalize_attempt 写入 attempt 最终状态。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.attempt_id = "att_test"
        task.attempt_number = 1
        task.attempt_started_at = utc_now()

        resumer = TaskResumer(repo)
        attempt = resumer.finalize_attempt(
            task,
            status="succeeded",
            completed_stages=["local", "first_party"],
            seen_document_ids=["doc_1"],
            saved_evidence_ids=["ev_1"],
            consumed_budget={"requests": 5},
            next_action="none",
        )
        assert attempt.status == "succeeded"
        assert attempt.attempt_id == "att_test"
        # task.checkpoint 应同步更新
        assert "first_party" in task.checkpoint["completed_stages"]

    def test_claim_attempt_allows_only_one_worker_for_stale_task_copies(self) -> None:
        """两个 worker 持有同一 queued 快照时，只有一个能原子领取 attempt。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        repo.save_task(task)

        worker_one_task = repo.get_task(task.task_id)
        worker_two_task = repo.get_task(task.task_id)
        assert worker_one_task is not None
        assert worker_two_task is not None

        first = TaskResumer(repo).claim_attempt(worker_one_task)
        second = TaskResumer(repo).claim_attempt(worker_two_task)

        assert first is not None
        assert second is None
        attempts = TaskResumer(repo).list_attempts(task.task_id)
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1

    def test_claim_attempt_rejects_stale_copy_after_task_finished(self) -> None:
        """首个 worker 结束后，旧 queued 副本不得再次领取终态 task。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        repo.save_task(task)
        first_copy = repo.get_task(task.task_id)
        stale_copy = repo.get_task(task.task_id)
        assert first_copy is not None
        assert stale_copy is not None

        resumer = TaskResumer(repo)
        attempt = resumer.claim_attempt(first_copy)
        assert attempt is not None
        resumer.finalize_attempt(
            first_copy,
            status="succeeded",
            completed_stages=[],
            seen_document_ids=[],
            saved_evidence_ids=[],
            consumed_budget={},
        )
        first_copy.status = TaskStatus.SUCCEEDED.value
        repo.save_task(first_copy)

        assert resumer.claim_attempt(stale_copy) is None
        assert len(resumer.list_attempts(task.task_id)) == 1

    def test_list_resumable_tasks_is_globally_bounded_to_500(self) -> None:
        """不同状态合计再多，启动恢复也只返回最早 500 个 task。"""
        repo = _make_temp_repo()
        tasks = []
        for number in range(510):
            task = ResearchTask(
                task_id=f"rt_bound_{number:04d}",
                target_claim_id=f"claim_bound_{number:04d}",
                status=(
                    TaskStatus.RUNNING.value if number % 2 == 0
                    else TaskStatus.PARTIAL.value
                ),
            )
            tasks.append(task)
        with repo._connect() as conn:
            conn.executemany(
                "INSERT INTO research_tasks "
                "(task_id, status, created_at, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (item.task_id, item.status, item.created_at,
                     __import__("json").dumps(item.to_dict(), ensure_ascii=False))
                    for item in tasks
                ],
            )

        assert len(TaskResumer(repo).list_resumable_tasks()) == 500

    def test_resume_task_does_not_overwrite_concurrent_claim(
        self, monkeypatch
    ) -> None:
        """恢复过程中不能在关闭旧 attempt 后再用 queued 状态覆盖新领取。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        task.status = TaskStatus.QUEUED.value
        repo.save_task(task)
        resumer = TaskResumer(repo)
        resumer.save_attempt(ResearchTaskAttempt(
            task_id=task.task_id,
            target_claim_id=claim.claim_id,
            attempt_number=1,
            status="running",
        ))

        original_save = repo.save_task

        def claim_before_legacy_save(item):
            current = repo.get_task(item.task_id)
            assert current is not None
            TaskResumer(repo).claim_attempt(current)
            original_save(item)

        monkeypatch.setattr(repo, "save_task", claim_before_legacy_save)
        resumer.resume_task(task)

        persisted = repo.get_task(task.task_id)
        assert persisted is not None
        unfinished = [
            item for item in resumer.list_attempts(task.task_id)
            if item.status in {"running", "partial", "retrying"}
            and not item.finished_at
        ]
        assert persisted.status == TaskStatus.QUEUED.value
        assert unfinished == []


# ======================================================================
# Task 3.5: Orchestrator 集成测试
# ======================================================================

class TestOrchestratorPhase3Integration:
    """Orchestrator Phase 3 集成测试。"""

    def test_orchestrator_accepts_new_components(self) -> None:
        """Orchestrator 接受新的 Phase 3 组件（向后兼容）。"""
        repo = _make_temp_repo()
        mock_retriever = MagicMock()
        mock_extractor = MagicMock()
        mock_local = MagicMock()
        mock_first_party = MagicMock()
        mock_budget = MagicMock()
        mock_resumer = MagicMock()

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            local_retriever=mock_local,
            first_party_retriever=mock_first_party,
            global_budget_manager=mock_budget,
            task_resumer=mock_resumer,
        )
        assert orchestrator.local_retriever is mock_local
        assert orchestrator.first_party_retriever is mock_first_party
        assert orchestrator.global_budget_manager is mock_budget
        assert orchestrator.task_resumer is mock_resumer

    def test_orchestrator_local_short_circuit_skips_network(self) -> None:
        """本地命中时跳过网络检索。"""
        repo = _make_temp_repo()
        claim = _make_claim(counter_search_completed=True)
        repo.save_claim(claim)
        # 准备本地合格证据
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1",
            is_first_party=True, stance=EvidenceStance.SUPPORT.value,
        ))
        repo.save_evidence(_make_evidence(
            evidence_id="ev_2", lineage_id="lineage_2",
            is_first_party=False, stance=EvidenceStance.SUPPORT.value,
        ))

        mock_retriever = MagicMock()
        mock_extractor = MagicMock()
        local_retriever = LocalEvidenceRetriever(repo)

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            local_retriever=local_retriever,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        assert summary.local_retrieval_hit
        assert summary.stopped_reason_code == "local_sufficient"
        # 网络检索器不应被调用
        mock_retriever.search.assert_not_called()
        assert task.status == TaskStatus.SUCCEEDED.value

    def test_orchestrator_global_budget_denied_fails_task(self) -> None:
        """全局预算耗尽时任务失败。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)

        mock_retriever = MagicMock()
        mock_extractor = MagicMock()
        # 全局预算管理器：拒绝所有分配
        budget_manager = GlobalBudgetManager(
            daily_total=TaskBudget(max_requests=0, max_documents=0),
            min_per_task=TaskBudget(max_requests=1, max_documents=1),
        )

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            global_budget_manager=budget_manager,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        assert task.status == TaskStatus.FAILED.value
        assert summary.stopped_reason_code == "budget_exhausted"
        # 网络检索器不应被调用
        mock_retriever.search.assert_not_called()

    def test_orchestrator_records_attempt_on_success(self) -> None:
        """成功执行后记录 attempt。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        resumer = TaskResumer(repo)

        mock_retriever = MagicMock()
        mock_retriever.search.return_value = []
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = []

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            task_resumer=resumer,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        assert summary.attempt_id
        assert summary.attempt_number == 1
        # attempt 应已持久化
        attempts = resumer.list_attempts(task.task_id)
        assert len(attempts) == 1
        assert attempts[0].status in ("succeeded", "partial")

    def test_orchestrator_rejects_second_worker_after_atomic_claim(self) -> None:
        """同一 queued task 被领取后，持旧快照的 worker 不得重复执行。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        task = ResearchTask(target_claim_id=claim.claim_id)
        repo.save_task(task)
        first_worker = repo.get_task(task.task_id)
        second_worker = repo.get_task(task.task_id)
        assert first_worker is not None
        assert second_worker is not None

        resumer = TaskResumer(repo)
        assert resumer.claim_attempt(first_worker) is not None
        retriever = MagicMock()
        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=retriever,
            extractor=MagicMock(),
            task_resumer=resumer,
        )

        summary = orchestrator.execute(second_worker)

        assert summary.stopped_reason_code == "task_already_running"
        retriever.search.assert_not_called()
        assert len(resumer.list_attempts(task.task_id)) == 1

    def test_orchestrator_stopped_reason_code_is_structured(self) -> None:
        """stopped_reason_code 是结构化原因码。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)

        mock_retriever = MagicMock()
        mock_retriever.search.return_value = []
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = []

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        # 所有可能的停止原因码
        valid_codes = set(STOP_REASON_CODES.keys()) | {"budget_exhausted", "exception"}
        assert summary.stopped_reason_code in valid_codes or summary.stopped_reason_code == ""

    def test_orchestrator_claim_not_found_returns_code(self) -> None:
        """Claim 不存在时返回 claim_not_found 原因码。"""
        repo = _make_temp_repo()
        mock_retriever = MagicMock()
        mock_extractor = MagicMock()
        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
        )
        task = ResearchTask(target_claim_id="nonexistent_claim")
        summary = orchestrator.execute(task)
        assert summary.stopped_reason_code == "claim_not_found"
        assert task.status == TaskStatus.FAILED.value


# ======================================================================
# Task 3.5: 网页摘要不能成证据硬校验
# ======================================================================

class TestWebSnippetNotEvidence:
    """网页摘要不能直接成为证据的硬校验测试。"""

    def test_evidence_without_quote_is_rejected(self) -> None:
        """无 quote 的 EvidenceRecord 被拒绝保存。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        # 无 quote
        ev = EvidenceRecord(
            evidence_id="ev_no_quote",
            claim_id=claim.claim_id,
            stance=EvidenceStance.SUPPORT.value,
            quote="",  # 空 quote
            grounding_status="unknown",
            source_trace_id="src_test",
        )
        # 通过 repository.save_evidence 保存
        # 注意：save_evidence 不校验 quote，校验在 orchestrator 层
        # 这里测试 orchestrator 的硬校验逻辑
        # 构造一个 extractor 返回无 quote 的证据
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [ev]
        mock_retriever = MagicMock()
        from core.research.perilla.discovery.models import SourceDocument

        doc = SourceDocument(url="http://example.com", title="网页", content_text="摘要内容")
        mock_retriever.search.return_value = [doc]

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)
        # 无 quote 的证据不应被保存
        assert summary.evidence_saved == 0

    def test_evidence_without_grounding_is_rejected(self) -> None:
        """grounding_status 不在允许集合的 EvidenceRecord 被拒绝。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        # grounding_status="unknown" 不在 {exact, partial, structured}
        ev = EvidenceRecord(
            evidence_id="ev_no_grounding",
            claim_id=claim.claim_id,
            stance=EvidenceStance.SUPPORT.value,
            quote="有引用但无 grounding",
            grounding_status="unknown",  # 不允许
            source_trace_id="src_test",
        )
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [ev]
        mock_retriever = MagicMock()
        from core.research.perilla.discovery.models import SourceDocument

        doc = SourceDocument(url="http://example.com", title="网页", content_text="内容")
        mock_retriever.search.return_value = [doc]

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)
        # 无 grounding 的证据不应被保存
        assert summary.evidence_saved == 0


# ======================================================================
# Task 3.4: 检索顺序测试
# ======================================================================

class TestRetrievalOrder:
    """检索顺序测试：本地 → 第一方 → 二手 → 普通网页。"""

    def test_local_retrieval_runs_before_network(self) -> None:
        """本地检索在网络检索之前执行。"""
        repo = _make_temp_repo()
        claim = _make_claim(counter_search_completed=True)
        repo.save_claim(claim)
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1",
            is_first_party=True, stance=EvidenceStance.SUPPORT.value,
        ))
        repo.save_evidence(_make_evidence(
            evidence_id="ev_2", lineage_id="lineage_2",
            is_first_party=False, stance=EvidenceStance.SUPPORT.value,
        ))

        mock_retriever = MagicMock()
        mock_extractor = MagicMock()
        local_retriever = LocalEvidenceRetriever(repo)

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            local_retriever=local_retriever,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        # 本地命中，"local" 应在 completed_stages 中
        assert "local" in summary.completed_stages
        # 网络检索不应执行
        mock_retriever.search.assert_not_called()

    def test_local_partial_continues_to_network(self) -> None:
        """本地部分命中时继续网络检索。"""
        repo = _make_temp_repo()
        claim = _make_claim()
        repo.save_claim(claim)
        # 只有 1 条证据，不满足本地短路条件
        repo.save_evidence(_make_evidence(
            evidence_id="ev_1", lineage_id="lineage_1", is_first_party=False
        ))

        mock_retriever = MagicMock()
        mock_retriever.search.return_value = []
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = []
        local_retriever = LocalEvidenceRetriever(repo)

        orchestrator = ResearchOrchestrator(
            repository=repo,
            retriever=mock_retriever,
            extractor=mock_extractor,
            local_retriever=local_retriever,
        )
        task = ResearchTask(target_claim_id=claim.claim_id)
        summary = orchestrator.execute(task)

        # 本地未短路，应继续网络检索
        assert not summary.local_retrieval_hit
        # "local" 仍在 completed_stages 中
        assert "local" in summary.completed_stages
        # 网络检索器应被调用
        mock_retriever.search.assert_called()
