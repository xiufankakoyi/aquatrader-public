"""P3.6 研究资源与预算优化模块测试。

覆盖 P3.6 §「验收」四项硬门禁：
- 高热度不自动获得最高预算
- 已重复失败的任务有冷却和停止条件
- 预算决策保存理由和版本
- 成本、命中和不确定性下降可统计

运行：``python -m pytest tests/test_research_resource_allocation.py -q``
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.research.resource_allocation import (
    AllocationDecision,
    BudgetAllocation,
    ResearchBudgetAllocator,
    ResearchTask,
    VOIResult,
    ValueOfInformationEstimator,
)


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "t1",
    *,
    symbol: str = "TEST",
    uncertainty_score: float = 0.6,
    blocks_model: bool = False,
    evidence_quality_estimate: float = 0.5,
    retrieval_cost: float = 1.0,
    historical_hit_rate: float = 0.5,
    importance: float = 0.5,
    verification_deadline: str = "",
    cooldown_until: str = "",
    consecutive_failures: int = 0,
) -> ResearchTask:
    """构造一个 ``ResearchTask``，便于测试快速调整字段。"""
    return ResearchTask(
        task_id=task_id,
        symbol=symbol,
        uncertainty_score=uncertainty_score,
        blocks_model=blocks_model,
        evidence_quality_estimate=evidence_quality_estimate,
        retrieval_cost=retrieval_cost,
        historical_hit_rate=historical_hit_rate,
        importance=importance,
        verification_deadline=verification_deadline,
        cooldown_until=cooldown_until,
        consecutive_failures=consecutive_failures,
    )


def _future_iso(days: int = 3) -> str:
    """返回未来 ``days`` 天后的 ISO 字符串。"""
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def _past_iso(days: int = 1) -> str:
    """返回过去 ``days`` 天前的 ISO 字符串。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------------------------
# VOI 计算
# ---------------------------------------------------------------------------


class TestValueOfInformation:
    """VOI 估计器测试。"""

    def test_voi_score_formula(self) -> None:
        """VOI 评分应等于公式组合：不确定性×质量×命中×重要×加成×紧迫×衰减 / 成本。"""
        task = _make_task(
            uncertainty_score=0.8,
            evidence_quality_estimate=0.5,
            historical_hit_rate=0.6,
            importance=0.7,
            retrieval_cost=2.0,
            blocks_model=True,
        )
        estimator = ValueOfInformationEstimator()
        result = estimator.estimate(task)

        # 期望下降 = 0.8 * 0.5 = 0.4
        assert result.expected_uncertainty_reduction == pytest.approx(0.4)
        # block_factor=1.5, urgency=1.0, failure=1.0
        # voi = 0.4 * 0.6 * 0.7 * 1.5 * 1.0 * 1.0 / 2.0 = 0.126
        assert result.voi_score == pytest.approx(0.126, rel=1e-6)
        assert result.components["block_factor"] == pytest.approx(1.5)

    def test_zero_uncertainty_zero_reduction(self) -> None:
        """不确定性为 0 时，期望下降与 VOI 均为 0。"""
        task = _make_task(uncertainty_score=0.0)
        result = ValueOfInformationEstimator().estimate(task)
        assert result.expected_uncertainty_reduction == 0.0
        assert result.voi_score == 0.0
        assert result.skipped is False

    def test_blocks_model_bonus(self) -> None:
        """阻断模型任务应获得 block_bonus 加成。"""
        base_task = _make_task(blocks_model=False)
        block_task = _make_task(task_id="t2", blocks_model=True)
        estimator = ValueOfInformationEstimator(block_bonus=2.0)
        base_voi = estimator.estimate(base_task).voi_score
        block_voi = estimator.estimate(block_task).voi_score
        # block 因子 2.0 应使 VOI 翻倍
        assert block_voi == pytest.approx(base_voi * 2.0, rel=1e-6)

    def test_consecutive_failures_decayed(self) -> None:
        """连续失败应按 failure_decay_base 指数衰减 VOI。"""
        fresh = _make_task(consecutive_failures=0)
        failed = _make_task(task_id="t2", consecutive_failures=3)
        estimator = ValueOfInformationEstimator(failure_decay_base=0.5)
        fresh_voi = estimator.estimate(fresh).voi_score
        failed_voi = estimator.estimate(failed).voi_score
        # 衰减因子 0.5^3 = 0.125
        assert failed_voi == pytest.approx(fresh_voi * 0.125, rel=1e-6)

    def test_permanent_stop_after_threshold(self) -> None:
        """连续失败达到阈值应被永久跳过。"""
        task = _make_task(consecutive_failures=5)
        estimator = ValueOfInformationEstimator(skip_failure_threshold=5)
        result = estimator.estimate(task)
        assert result.skipped is True
        assert "threshold" in result.skip_reason
        assert result.voi_score == 0.0

    def test_cooldown_skipped(self) -> None:
        """冷却未到的任务应被跳过。"""
        task = _make_task(cooldown_until=_future_iso(days=2))
        result = ValueOfInformationEstimator().estimate(task)
        assert result.skipped is True
        assert "cooldown" in result.skip_reason
        assert result.voi_score == 0.0

    def test_cooldown_expired_not_skipped(self) -> None:
        """冷却已过的任务不应被跳过。"""
        task = _make_task(cooldown_until=_past_iso(days=1))
        result = ValueOfInformationEstimator().estimate(task)
        assert result.skipped is False
        assert result.voi_score > 0.0

    def test_deadline_urgency_factor(self) -> None:
        """接近截止时间的任务应获得更高紧迫度因子。"""
        now = datetime.now(timezone.utc)
        future_far = (now + timedelta(days=30)).isoformat(timespec="seconds")
        future_near = (now + timedelta(hours=12)).isoformat(timespec="seconds")

        far_task = _make_task(verification_deadline=future_far)
        near_task = _make_task(task_id="t2", verification_deadline=future_near)

        estimator = ValueOfInformationEstimator(now_provider=lambda: now)
        far_voi = estimator.estimate(far_task).voi_score
        near_voi = estimator.estimate(near_task).voi_score

        # 紧迫度更高应使 VOI 更大
        assert near_voi > far_voi

    def test_deadline_past_uses_cap(self) -> None:
        """截止时间已过应使用紧迫度上限。"""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=1)).isoformat(timespec="seconds")
        task = _make_task(verification_deadline=past)
        estimator = ValueOfInformationEstimator(
            deadline_urgency_cap=2.5, now_provider=lambda: now
        )
        result = estimator.estimate(task)
        assert result.components["urgency_factor"] == pytest.approx(2.5)

    def test_high_retrieval_cost_lowers_voi(self) -> None:
        """检索成本越高，VOI 越低。"""
        cheap = _make_task(retrieval_cost=0.5)
        expensive = _make_task(task_id="t2", retrieval_cost=10.0)
        estimator = ValueOfInformationEstimator()
        cheap_voi = estimator.estimate(cheap).voi_score
        expensive_voi = estimator.estimate(expensive).voi_score
        assert cheap_voi > expensive_voi

    def test_invalid_params_raise(self) -> None:
        """非法初始化参数应抛出 ValueError。"""
        with pytest.raises(ValueError):
            ValueOfInformationEstimator(failure_decay_base=1.5)
        with pytest.raises(ValueError):
            ValueOfInformationEstimator(block_bonus=0.5)
        with pytest.raises(ValueError):
            ValueOfInformationEstimator(skip_failure_threshold=0)


# ---------------------------------------------------------------------------
# 预算分配
# ---------------------------------------------------------------------------


class TestBudgetAllocation:
    """预算分配器测试。"""

    def test_high_importance_does_not_dominate(self) -> None:
        """高 importance 但低 VOI 的任务不应自动获得最高预算。"""
        # 任务 A：高热度但低不确定性下降、高成本
        hot_task = _make_task(
            task_id="hot",
            importance=1.0,
            uncertainty_score=0.1,
            evidence_quality_estimate=0.1,
            retrieval_cost=10.0,
            historical_hit_rate=0.1,
        )
        # 任务 B：低热度但高 VOI
        valuable_task = _make_task(
            task_id="val",
            importance=0.2,
            uncertainty_score=0.9,
            evidence_quality_estimate=0.9,
            retrieval_cost=0.5,
            historical_hit_rate=0.9,
        )
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        result = allocator.allocate([hot_task, valuable_task])

        assert result.allocations["val"] > result.allocations["hot"]
        # 高热度任务不能拿超过一半预算
        assert result.allocations["hot"] < 50.0

    def test_blocks_model_priority(self) -> None:
        """其他条件相同时，阻断模型任务应分到更多预算。"""
        normal = _make_task(task_id="normal", blocks_model=False)
        blocking = _make_task(task_id="block", blocks_model=True)
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        result = allocator.allocate([normal, blocking])
        assert result.allocations["block"] > result.allocations["normal"]

    def test_allocation_sums_to_total(self) -> None:
        """所有可分配任务的预算之和应等于总预算池。"""
        tasks = [_make_task(task_id=f"t{i}", uncertainty_score=0.5 + i * 0.1) for i in range(3)]
        allocator = ResearchBudgetAllocator(total_budget=300.0)
        result = allocator.allocate(tasks)
        total = sum(result.allocations.values())
        assert total == pytest.approx(300.0, rel=1e-6)

    def test_empty_tasks_returns_empty_allocation(self) -> None:
        """空任务列表应返回空分配，但仍生成版本和指纹。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        result = allocator.allocate([])
        assert result.allocations == {}
        assert result.decisions == []
        assert result.version == 1
        assert result.fingerprint.startswith("ba_")

    def test_total_budget_zero(self) -> None:
        """总预算为 0 时所有分配应为 0。"""
        task = _make_task()
        allocator = ResearchBudgetAllocator(total_budget=0.0)
        result = allocator.allocate([task])
        assert result.allocations["t1"] == 0.0

    def test_cooldown_task_skipped_in_allocation(self) -> None:
        """冷却中的任务在分配时应被跳过。"""
        active = _make_task(task_id="active")
        cooling = _make_task(task_id="cooling", cooldown_until=_future_iso(days=2))
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        result = allocator.allocate([active, cooling])

        assert result.allocations["cooling"] == 0.0
        assert result.allocations["active"] == 100.0
        # 决策中应记录 skipped 原因
        cooling_decision = next(d for d in result.decisions if d.task_id == "cooling")
        assert cooling_decision.skipped is True
        assert "cooldown" in cooling_decision.skip_reason

    def test_permanently_stopped_task_skipped(self) -> None:
        """连续失败超过停止阈值的任务应被永久跳过。"""
        stopped = _make_task(
            task_id="stopped", consecutive_failures=5
        )
        active = _make_task(task_id="active")
        estimator = ValueOfInformationEstimator(skip_failure_threshold=5)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, voi_estimator=estimator
        )
        result = allocator.allocate([stopped, active])

        assert result.allocations["stopped"] == 0.0
        assert result.allocations["active"] == 100.0
        stopped_decision = next(d for d in result.decisions if d.task_id == "stopped")
        assert stopped_decision.skipped is True
        assert "threshold" in stopped_decision.skip_reason

    def test_min_voi_threshold_filters(self) -> None:
        """VOI 低于阈值的任务应被过滤，分配 0。"""
        low_voi = _make_task(
            task_id="low",
            uncertainty_score=0.05,
            evidence_quality_estimate=0.05,
            retrieval_cost=10.0,
        )
        high_voi = _make_task(
            task_id="high",
            uncertainty_score=0.9,
            evidence_quality_estimate=0.9,
            retrieval_cost=0.5,
        )
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, min_voi_threshold=0.05
        )
        result = allocator.allocate([low_voi, high_voi])
        assert result.allocations["low"] == 0.0
        assert result.allocations["high"] == 100.0

    def test_decision_reasons_recorded(self) -> None:
        """每个任务的决策理由应被记录且非空。"""
        tasks = [_make_task(task_id=f"t{i}") for i in range(2)]
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        result = allocator.allocate(tasks)
        for task_id in ("t0", "t1"):
            assert task_id in result.decision_reasons
            assert len(result.decision_reasons[task_id]) > 0
            # 理由中应包含 voi_score 信息
            reasons = " ".join(result.decision_reasons[task_id])
            assert "voi_score" in reasons

    # ------------------------------------------------------------------
    # 版本化与指纹
    # ------------------------------------------------------------------

    def test_version_increments(self) -> None:
        """每次分配调用应使版本号递增。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        v1 = allocator.allocate([_make_task()])
        v2 = allocator.allocate([_make_task()])
        v3 = allocator.allocate([_make_task()])
        assert v1.version == 1
        assert v2.version == 2
        assert v3.version == 3

    def test_fingerprint_deterministic_for_same_decision(self) -> None:
        """相同决策（同版本、同分配、同理由）应生成相同指纹。"""
        # 直接构造两个内容一致的 BudgetAllocation
        a1 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 60.0, "t2": 40.0},
            decision_reasons={"t1": ["r1"], "t2": ["r2"]},
            version=1,
        )
        a2 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 60.0, "t2": 40.0},
            decision_reasons={"t1": ["r1"], "t2": ["r2"]},
            version=1,
        )
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        fp1 = allocator._compute_fingerprint(a1)
        fp2 = allocator._compute_fingerprint(a2)
        assert fp1 == fp2
        assert fp1.startswith("ba_")

    def test_fingerprint_differs_for_different_decisions(self) -> None:
        """不同分配比例应生成不同指纹。"""
        a1 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 60.0, "t2": 40.0},
            decision_reasons={"t1": ["r1"], "t2": ["r2"]},
            version=1,
        )
        a2 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 50.0, "t2": 50.0},
            decision_reasons={"t1": ["r1"], "t2": ["r2"]},
            version=1,
        )
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        assert allocator._compute_fingerprint(a1) != allocator._compute_fingerprint(a2)

    def test_fingerprint_differs_for_different_version(self) -> None:
        """版本号不同但分配相同的决策也应生成不同指纹。"""
        a1 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 100.0},
            decision_reasons={"t1": ["r1"]},
            version=1,
        )
        a2 = BudgetAllocation(
            total_budget=100.0,
            allocations={"t1": 100.0},
            decision_reasons={"t1": ["r1"]},
            version=2,
        )
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        assert allocator._compute_fingerprint(a1) != allocator._compute_fingerprint(a2)


# ---------------------------------------------------------------------------
# 冷却、停止与结果反馈
# ---------------------------------------------------------------------------


class TestCooldownAndOutcome:
    """冷却、停止条件与 ``record_outcome`` 测试。"""

    def test_should_apply_cooldown_at_trigger(self) -> None:
        """失败次数达到 trigger 时应判定需要冷却。"""
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, cooldown_failures_trigger=2
        )
        ok, _ = allocator.should_apply_cooldown(_make_task(consecutive_failures=1))
        assert ok is False
        ok, reason = allocator.should_apply_cooldown(_make_task(consecutive_failures=2))
        assert ok is True
        assert "trigger" in reason

    def test_compute_cooldown_until_uses_configured_hours(self) -> None:
        """冷却截止时间应按 ``cooldown_hours`` 偏移。"""
        now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, cooldown_hours=6.0
        )
        cooldown_iso = allocator.compute_cooldown_until(_make_task(), now=now)
        # 6 小时后应为 18:00:00
        assert "18:00:00" in cooldown_iso

    def test_record_outcome_success_resets_failures(self) -> None:
        """成功执行应重置 consecutive_failures 并清空 cooldown_until。"""
        task = _make_task(consecutive_failures=3, cooldown_until=_future_iso(days=1))
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        changes = allocator.record_outcome(
            task, succeeded=True, uncertainty_after=0.2
        )
        assert task.consecutive_failures == 0
        assert task.cooldown_until == ""
        assert changes.get("reset_failures") is True
        assert changes.get("uncertainty_reduction") == pytest.approx(0.6 - 0.2)

    def test_record_outcome_failure_triggers_cooldown(self) -> None:
        """失败次数达到 trigger 时应设置 cooldown_until。"""
        task = _make_task(consecutive_failures=1)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, cooldown_failures_trigger=2, cooldown_hours=12.0
        )
        changes = allocator.record_outcome(task, succeeded=False, cost_incurred=2.5)
        assert task.consecutive_failures == 2
        assert task.cooldown_until != ""
        assert changes.get("cooldown_until") == task.cooldown_until
        assert changes.get("cost_incurred") == 2.5

    def test_record_outcome_marks_stopped_at_threshold(self) -> None:
        """达到永久停止阈值时应标记 stopped。"""
        estimator = ValueOfInformationEstimator(skip_failure_threshold=3)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, voi_estimator=estimator
        )
        task = _make_task(consecutive_failures=2)
        changes = allocator.record_outcome(task, succeeded=False)
        assert task.consecutive_failures == 3
        assert changes.get("stopped") is True
        assert "skip_threshold" in changes.get("stop_reason", "")

    def test_is_permanently_stopped(self) -> None:
        """``is_permanently_stopped`` 应正确判定阈值。"""
        estimator = ValueOfInformationEstimator(skip_failure_threshold=4)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0, voi_estimator=estimator
        )
        assert allocator.is_permanently_stopped(_make_task(consecutive_failures=3)) is False
        assert allocator.is_permanently_stopped(_make_task(consecutive_failures=4)) is True


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


class TestStatistics:
    """``statistics`` 累计指标测试。"""

    def test_empty_statistics(self) -> None:
        """无决策无结果时统计应为零。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        stats = allocator.statistics()
        assert stats["total_cost"] == 0.0
        assert stats["total_attempts"] == 0
        assert stats["hit_rate"] == 0.0
        assert stats["decisions_made"] == 0

    def test_statistics_aggregates_cost_hit_and_reduction(self) -> None:
        """统计应聚合成本、命中率和不确定性下降。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)

        # 第一次决策：3 个任务
        tasks = [_make_task(task_id=f"t{i}") for i in range(3)]
        allocator.allocate(tasks)

        # 记录 3 次结果：2 成功 1 失败
        allocator.record_outcome(
            tasks[0], succeeded=True, cost_incurred=1.5, uncertainty_after=0.2
        )
        allocator.record_outcome(
            tasks[1], succeeded=True, cost_incurred=2.0, uncertainty_after=0.3
        )
        allocator.record_outcome(tasks[2], succeeded=False, cost_incurred=0.5)

        stats = allocator.statistics()
        assert stats["total_attempts"] == 3
        assert stats["successful_attempts"] == 2
        assert stats["hit_rate"] == pytest.approx(2 / 3)
        assert stats["total_cost"] == pytest.approx(4.0)
        # 0.4 + 0.3 = 0.7
        assert stats["total_uncertainty_reduction"] == pytest.approx(0.7)
        assert stats["decisions_made"] == 1


# ---------------------------------------------------------------------------
# 集成场景
# ---------------------------------------------------------------------------


class TestIntegration:
    """集成场景：覆盖 P3.6 §「验收」全部硬门禁。"""

    def test_acceptance_high_heat_not_highest_budget(self) -> None:
        """验收点 1：高热度不自动获得最高预算。"""
        # 高热度主题：importance=1.0 但其他维度都差
        hot = _make_task(
            task_id="hot",
            importance=1.0,
            uncertainty_score=0.2,
            evidence_quality_estimate=0.2,
            retrieval_cost=5.0,
            historical_hit_rate=0.2,
        )
        # 关键阻断：importance 中等但阻断模型、不确定性高、证据好
        critical = _make_task(
            task_id="critical",
            importance=0.5,
            blocks_model=True,
            uncertainty_score=0.9,
            evidence_quality_estimate=0.8,
            retrieval_cost=1.0,
            historical_hit_rate=0.7,
        )
        allocator = ResearchBudgetAllocator(total_budget=200.0)
        result = allocator.allocate([hot, critical])
        assert result.allocations["critical"] > result.allocations["hot"]

    def test_acceptance_cooldown_and_stop(self) -> None:
        """验收点 2：重复失败任务有冷却和停止条件。"""
        estimator = ValueOfInformationEstimator(skip_failure_threshold=3)
        allocator = ResearchBudgetAllocator(
            total_budget=100.0,
            voi_estimator=estimator,
            cooldown_failures_trigger=2,
            cooldown_hours=12.0,
        )
        task = _make_task(task_id="flaky")

        # 失败两次 -> 进入冷却
        allocator.record_outcome(task, succeeded=False)
        allocator.record_outcome(task, succeeded=False)
        assert task.cooldown_until != ""

        # 冷却中分配应为 0
        result = allocator.allocate([task])
        assert result.allocations["flaky"] == 0.0

        # 第三次失败 -> 永久停止
        changes = allocator.record_outcome(task, succeeded=False)
        assert changes.get("stopped") is True

        # 永久停止后 VOI 也应被跳过
        voi = estimator.estimate(task)
        assert voi.skipped is True

    def test_acceptance_decision_versioned_with_reasons(self) -> None:
        """验收点 3：预算决策保存理由和版本。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        tasks = [_make_task(task_id=f"t{i}") for i in range(2)]
        result = allocator.allocate(tasks)

        # 版本号、指纹、决策理由均应存在
        assert result.version >= 1
        assert result.fingerprint.startswith("ba_")
        assert len(result.fingerprint) == 27  # "ba_" + 24 hex
        for task_id in ("t0", "t1"):
            assert task_id in result.decision_reasons
            assert len(result.decision_reasons[task_id]) >= 1
        # 序列化往返保持稳定
        payload = result.to_dict()
        assert payload["version"] == result.version
        assert payload["fingerprint"] == result.fingerprint

    def test_acceptance_statistics_trackable(self) -> None:
        """验收点 4：成本、命中和不确定性下降可统计。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        # 每个 task 初始 uncertainty_score 默认 0.6
        tasks = [_make_task(task_id=f"t{i}") for i in range(4)]
        allocator.allocate(tasks)

        allocator.record_outcome(
            tasks[0], succeeded=True, cost_incurred=1.0, uncertainty_after=0.1
        )
        allocator.record_outcome(
            tasks[1], succeeded=True, cost_incurred=2.0, uncertainty_after=0.2
        )
        allocator.record_outcome(tasks[2], succeeded=False, cost_incurred=0.5)
        allocator.record_outcome(
            tasks[3], succeeded=True, cost_incurred=1.5, uncertainty_after=0.0
        )

        stats = allocator.statistics()
        assert stats["total_attempts"] == 4
        assert stats["successful_attempts"] == 3
        assert stats["hit_rate"] == pytest.approx(0.75)
        assert stats["total_cost"] == pytest.approx(5.0)
        # 0.5 + 0.4 + 0(失败) + 0.6 = 1.5
        assert stats["total_uncertainty_reduction"] == pytest.approx(1.5)
        assert stats["decisions_made"] == 1

    def test_failed_task_lowered_in_next_allocation(self) -> None:
        """重复失败任务在下次分配中应获得更低预算。"""
        allocator = ResearchBudgetAllocator(total_budget=100.0)
        task_a = _make_task(task_id="a")
        task_b = _make_task(task_id="b")

        # 初始 VOI 相同，平分
        result1 = allocator.allocate([task_a, task_b])
        assert result1.allocations["a"] == pytest.approx(50.0)
        assert result1.allocations["b"] == pytest.approx(50.0)

        # task_a 失败一次，VOI 衰减
        allocator.record_outcome(task_a, succeeded=False)
        result2 = allocator.allocate([task_a, task_b])
        # task_a 应获得更少
        assert result2.allocations["a"] < result2.allocations["b"]

    def test_constructor_validates_inputs(self) -> None:
        """非法构造参数应抛出 ValueError。"""
        with pytest.raises(ValueError):
            ResearchBudgetAllocator(total_budget=-1.0)
        with pytest.raises(ValueError):
            ResearchBudgetAllocator(
                total_budget=100.0, cooldown_failures_trigger=0
            )
        with pytest.raises(ValueError):
            ResearchBudgetAllocator(total_budget=100.0, cooldown_hours=-1.0)
