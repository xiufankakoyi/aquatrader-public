"""研究预算分配器。

按 VOI 加权把预算池分配给各研究任务，并对冷却、停止、版本化和指纹
生成提供可审计的决策记录。本模块不修改现有
``core/research/control_plane/budget_manager.py``，仅提供新的
``resource_allocation`` 视角的预算决策能力。

核心保证：
- 高热度（仅 ``importance`` 高）不会自动获得最高预算，必须经过 VOI 评分。
- 重复失败任务按指数衰减；达到 ``cooldown_failures_trigger`` 进入冷却；
  达到 VOI 估计器的 ``skip_failure_threshold`` 永久停止。
- 阻断模型任务通过 ``block_bonus`` 获得 VOI 加成。
- 每次决策生成递增 ``version`` 与确定性 ``fingerprint``，可追溯和回放。
- ``statistics`` 暴露累计成本、命中率和不确定性下降。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .value_of_information import VOIResult, ValueOfInformationEstimator


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _utc_now_iso() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ResearchTask:
    """资源分配视角的研究任务。

    与 ``control_plane.contracts.ResearchTask`` 不同，本契约聚焦于
    资源分配决策所需的输入字段，便于在不破坏既有任务模型的前提下扩展。

    Attributes:
        task_id: 任务 ID
        symbol: 标的代码
        uncertainty_score: 当前不确定性 0-1
        blocks_model: 是否阻断模型
        evidence_quality_estimate: 期望证据质量 0-1
        retrieval_cost: 检索成本估计（>0）
        historical_hit_rate: 历史命中率 0-1
        importance: 观点重要性 0-1
        verification_deadline: 验证截止时间 ISO 字符串
        cooldown_until: 冷却截止时间 ISO 字符串
        consecutive_failures: 连续失败次数
    """

    task_id: str
    symbol: str = ""
    uncertainty_score: float = 0.0
    blocks_model: bool = False
    evidence_quality_estimate: float = 0.0
    retrieval_cost: float = 1.0
    historical_hit_rate: float = 0.5
    importance: float = 0.5
    verification_deadline: str = ""
    cooldown_until: str = ""
    consecutive_failures: int = 0

    def __post_init__(self) -> None:
        self.uncertainty_score = max(0.0, min(1.0, _safe_float(self.uncertainty_score)))
        self.evidence_quality_estimate = max(0.0, min(1.0, _safe_float(self.evidence_quality_estimate)))
        self.retrieval_cost = max(0.0, _safe_float(self.retrieval_cost))
        self.historical_hit_rate = max(0.0, min(1.0, _safe_float(self.historical_hit_rate)))
        self.importance = max(0.0, min(1.0, _safe_float(self.importance)))
        self.consecutive_failures = max(0, _safe_int(self.consecutive_failures))

    def to_dict(self) -> dict[str, Any]:
        """序列化为可审计字典。"""
        return {
            "task_id": self.task_id,
            "symbol": self.symbol,
            "uncertainty_score": self.uncertainty_score,
            "blocks_model": self.blocks_model,
            "evidence_quality_estimate": self.evidence_quality_estimate,
            "retrieval_cost": self.retrieval_cost,
            "historical_hit_rate": self.historical_hit_rate,
            "importance": self.importance,
            "verification_deadline": self.verification_deadline,
            "cooldown_until": self.cooldown_until,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class AllocationDecision:
    """单个任务的预算分配决策。

    Attributes:
        task_id: 任务 ID
        voi: VOI 评估结果
        allocated_amount: 实际分配预算
        decision_reasons: 决策理由列表
        skipped: 是否被跳过
        skip_reason: 跳过原因
    """

    task_id: str
    voi: VOIResult
    allocated_amount: float = 0.0
    decision_reasons: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可审计字典。"""
        return {
            "task_id": self.task_id,
            "voi": self.voi.to_dict(),
            "allocated_amount": self.allocated_amount,
            "decision_reasons": list(self.decision_reasons),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


@dataclass
class BudgetAllocation:
    """预算分配结果，可序列化为带指纹的版本化决策。

    Attributes:
        total_budget: 总预算池
        allocations: task_id -> 分配金额
        decision_reasons: 每个任务的决策理由
        version: 版本号（每次 ``allocate`` 调用递增）
        fingerprint: 决策指纹（SHA256 前缀），相同决策稳定可复现
        created_at: 决策时间 ISO 字符串
        decisions: 完整的 ``AllocationDecision`` 列表
    """

    total_budget: float = 0.0
    allocations: dict[str, float] = field(default_factory=dict)
    decision_reasons: dict[str, list[str]] = field(default_factory=dict)
    version: int = 1
    fingerprint: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    decisions: list[AllocationDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可审计字典。"""
        return {
            "total_budget": self.total_budget,
            "allocations": dict(self.allocations),
            "decision_reasons": {k: list(v) for k, v in self.decision_reasons.items()},
            "version": self.version,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "decisions": [d.to_dict() for d in self.decisions],
        }


class ResearchBudgetAllocator:
    """按 VOI 加权分配研究预算。

    设计原则：
    - 高热度（仅 ``importance`` 高）不自动获得最高预算，必须经过 VOI 评分。
    - 重复失败任务降权；达到 ``cooldown_failures_trigger`` 进入冷却；
      达到 VOI 估计器 ``skip_failure_threshold`` 永久停止。
    - 冷却期任务不分配。
    - 阻断模型任务通过 ``block_bonus`` 获得 VOI 加成。
    - 每次决策生成递增 ``version`` 与确定性 ``fingerprint``，可追溯和回放。
    - ``statistics`` 暴露累计成本、命中率和不确定性下降。

    Args:
        total_budget: 可分配的总预算池（>=0）
        voi_estimator: 自定义 VOI 估计器；None 则使用默认
        cooldown_failures_trigger: 触发冷却的连续失败次数
        cooldown_hours: 冷却时长（小时）
        min_voi_threshold: VOI 低于此值的任务不分配
        now_provider: 自定义时间提供器，便于测试
    """

    DEFAULT_COOLDOWN_FAILURES_TRIGGER: int = 2
    DEFAULT_COOLDOWN_HOURS: float = 24.0

    def __init__(
        self,
        total_budget: float,
        *,
        voi_estimator: ValueOfInformationEstimator | None = None,
        cooldown_failures_trigger: int = DEFAULT_COOLDOWN_FAILURES_TRIGGER,
        cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
        min_voi_threshold: float = 0.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化预算分配器并校验参数边界。"""
        total_budget = _safe_float(total_budget, math.nan)
        cooldown_failures_trigger = _safe_int(cooldown_failures_trigger, -1)
        cooldown_hours = _safe_float(cooldown_hours, math.nan)
        min_voi_threshold = _safe_float(min_voi_threshold, math.nan)
        if not math.isfinite(total_budget) or total_budget < 0:
            raise ValueError("total_budget 不能为负")
        if cooldown_failures_trigger < 1:
            raise ValueError("cooldown_failures_trigger 必须 >= 1")
        if cooldown_hours < 0:
            raise ValueError("cooldown_hours 不能为负")
        if min_voi_threshold < 0:
            raise ValueError("min_voi_threshold 不能为负")

        self.total_budget = total_budget
        self.voi_estimator = voi_estimator or ValueOfInformationEstimator()
        self.cooldown_failures_trigger = cooldown_failures_trigger
        self.cooldown_hours = cooldown_hours
        self.min_voi_threshold = min_voi_threshold
        self._now_provider = now_provider
        self._version = 0
        # 决策历史与结果历史，仅用于统计；生产可由调用方持久化。
        self._decision_history: list[dict[str, Any]] = []
        self._outcome_history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 时间与指纹
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        """返回当前 UTC 时间；测试可注入 now_provider。"""
        if callable(self._now_provider):
            value = self._now_provider()
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value
        return datetime.now(timezone.utc)

    def _compute_fingerprint(self, allocation: BudgetAllocation) -> str:
        """计算决策指纹。

        指纹对 ``version``、``total_budget``、``allocations`` 与
        ``decision_reasons`` 做稳定 JSON 序列化后取 SHA256 前 24 位，
        确保相同决策可复现、不同决策可区分。

        Args:
            allocation: 已组装好的分配结果

        Returns:
            ``ba_`` 前缀的指纹字符串
        """
        payload = {
            "version": allocation.version,
            "total_budget": round(allocation.total_budget, 6),
            "allocations": {
                k: round(v, 6) for k, v in sorted(allocation.allocations.items())
            },
            "reasons": {
                k: list(v) for k, v in sorted(allocation.decision_reasons.items())
            },
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return "ba_" + hashlib.sha256(encoded).hexdigest()[:24]

    # ------------------------------------------------------------------
    # 预算分配
    # ------------------------------------------------------------------
    def allocate(self, tasks: list[ResearchTask]) -> BudgetAllocation:
        """对一批任务分配预算。

        流程：
        1. 计算每个任务的 VOI。
        2. 跳过冷却中或永久停止的任务（VOI=0）。
        3. 过滤 VOI 低于 ``min_voi_threshold`` 的任务。
        4. 按 VOI 加权把 ``total_budget`` 分配给剩余任务。
        5. 生成版本号和指纹，记录到历史。

        Args:
            tasks: 待分配任务列表

        Returns:
            ``BudgetAllocation`` 包含分配结果、理由、版本和指纹
        """
        self._version += 1
        allocation = BudgetAllocation(
            total_budget=self.total_budget,
            version=self._version,
            created_at=self._now().isoformat(timespec="seconds"),
        )

        if not isinstance(tasks, (list, tuple)) or not tasks:
            allocation.fingerprint = self._compute_fingerprint(allocation)
            self._record_decision(allocation)
            return allocation

        # 1) 计算 VOI
        safe_tasks = [task for task in tasks if isinstance(task, ResearchTask)]
        voi_results: list[tuple[ResearchTask, VOIResult]] = [
            (task, self.voi_estimator.estimate(task)) for task in safe_tasks
        ]

        # 2) 过滤被跳过的（冷却或永久停止）
        eligible: list[tuple[ResearchTask, VOIResult]] = []
        for task, voi in voi_results:
            if voi.skipped:
                decision = AllocationDecision(
                    task_id=task.task_id,
                    voi=voi,
                    allocated_amount=0.0,
                    decision_reasons=[f"skipped: {voi.skip_reason}"],
                    skipped=True,
                    skip_reason=voi.skip_reason,
                )
                allocation.decisions.append(decision)
                allocation.allocations[task.task_id] = 0.0
                allocation.decision_reasons[task.task_id] = decision.decision_reasons
            else:
                eligible.append((task, voi))

        # 3) 过滤 VOI 低于阈值的
        filtered: list[tuple[ResearchTask, VOIResult]] = []
        for task, voi in eligible:
            if voi.voi_score < self.min_voi_threshold:
                decision = AllocationDecision(
                    task_id=task.task_id,
                    voi=voi,
                    allocated_amount=0.0,
                    decision_reasons=[
                        f"voi_score={voi.voi_score:.6f} "
                        f"< threshold={self.min_voi_threshold:.6f}",
                    ],
                )
                allocation.decisions.append(decision)
                allocation.allocations[task.task_id] = 0.0
                allocation.decision_reasons[task.task_id] = decision.decision_reasons
            else:
                filtered.append((task, voi))

        # 4) 按 VOI 加权分配
        total_voi = sum(v.voi_score for _, v in filtered)
        if total_voi <= 0 or not filtered:
            for task, voi in filtered:
                decision = AllocationDecision(
                    task_id=task.task_id,
                    voi=voi,
                    allocated_amount=0.0,
                    decision_reasons=["total_voi <= 0; no budget to allocate"],
                )
                allocation.decisions.append(decision)
                allocation.allocations[task.task_id] = 0.0
                allocation.decision_reasons[task.task_id] = decision.decision_reasons
            allocation.fingerprint = self._compute_fingerprint(allocation)
            self._record_decision(allocation)
            return allocation

        for task, voi in filtered:
            share = voi.voi_score / total_voi
            amount = self.total_budget * share
            decision = AllocationDecision(
                task_id=task.task_id,
                voi=voi,
                allocated_amount=amount,
                decision_reasons=[
                    f"voi_score={voi.voi_score:.6f}",
                    f"share={share:.6f} (voi/total_voi="
                    f"{voi.voi_score:.6f}/{total_voi:.6f})",
                    f"allocated={amount:.6f}",
                ],
            )
            allocation.decisions.append(decision)
            allocation.allocations[task.task_id] = amount
            allocation.decision_reasons[task.task_id] = decision.decision_reasons

        allocation.fingerprint = self._compute_fingerprint(allocation)
        self._record_decision(allocation)
        return allocation

    def _record_decision(self, allocation: BudgetAllocation) -> None:
        """记录决策摘要到内部历史，供 ``statistics`` 聚合。"""
        self._decision_history.append(
            {
                "version": allocation.version,
                "fingerprint": allocation.fingerprint,
                "created_at": allocation.created_at,
                "allocations": dict(allocation.allocations),
                "total_budget": allocation.total_budget,
            }
        )

    # ------------------------------------------------------------------
    # 冷却与停止判定
    # ------------------------------------------------------------------
    def should_apply_cooldown(self, task: ResearchTask) -> tuple[bool, str]:
        """判断任务是否需要进入冷却。

        Args:
            task: 当前任务

        Returns:
            (是否进入冷却, 原因)
        """
        if task.consecutive_failures >= self.cooldown_failures_trigger:
            return True, (
                f"consecutive_failures={task.consecutive_failures} "
                f">= trigger={self.cooldown_failures_trigger}"
            )
        return False, ""

    def compute_cooldown_until(
        self, task: ResearchTask, now: datetime | None = None
    ) -> str:
        """计算冷却截止时间 ISO 字符串。

        Args:
            task: 任务（仅用于上下文；冷却时长来自分配器配置）
            now: 参考时间，None 则使用当前

        Returns:
            ISO 字符串
        """
        base = now or self._now()
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        cooldown_end = base + timedelta(hours=self.cooldown_hours)
        return cooldown_end.isoformat(timespec="seconds")

    def is_permanently_stopped(self, task: ResearchTask) -> bool:
        """判断任务是否已达到永久停止阈值。

        Args:
            task: 当前任务

        Returns:
            True 表示该任务的连续失败次数已达 VOI 估计器的停止阈值
        """
        return task.consecutive_failures >= self.voi_estimator.skip_failure_threshold

    # ------------------------------------------------------------------
    # 结果反馈与统计
    # ------------------------------------------------------------------
    def record_outcome(
        self,
        task: ResearchTask,
        *,
        succeeded: bool,
        cost_incurred: float = 0.0,
        uncertainty_after: float | None = None,
    ) -> dict[str, Any]:
        """根据执行结果更新任务状态并记录到结果历史。

        成功则重置 ``consecutive_failures`` 并清空 ``cooldown_until``；
        失败则递增失败计数，并在达到 ``cooldown_failures_trigger`` 时设置
        ``cooldown_until``；达到 VOI 估计器的 ``skip_failure_threshold``
        时标记 ``stopped``，下次 ``allocate`` 会被永久跳过。

        Args:
            task: 待更新任务（原地修改 ``consecutive_failures`` 等字段）
            succeeded: 是否成功
            cost_incurred: 实际消耗成本
            uncertainty_after: 执行后不确定性（可选）

        Returns:
            变更摘要字典，便于审计和断言
        """
        changes: dict[str, Any] = {
            "task_id": task.task_id,
            "succeeded": succeeded,
            "timestamp": self._now().isoformat(timespec="seconds"),
        }

        if succeeded:
            if task.consecutive_failures > 0:
                changes["previous_failures"] = task.consecutive_failures
                task.consecutive_failures = 0
                changes["reset_failures"] = True
            if uncertainty_after is not None:
                prev = task.uncertainty_score
                task.uncertainty_score = max(0.0, min(1.0, _safe_float(uncertainty_after)))
                changes["uncertainty_before"] = prev
                changes["uncertainty_after"] = task.uncertainty_score
                changes["uncertainty_reduction"] = prev - task.uncertainty_score
            changes["cooldown_until"] = ""
            task.cooldown_until = ""
        else:
            task.consecutive_failures += 1
            changes["consecutive_failures"] = task.consecutive_failures
            if task.consecutive_failures >= self.cooldown_failures_trigger:
                cooldown = self.compute_cooldown_until(task)
                task.cooldown_until = cooldown
                changes["cooldown_until"] = cooldown
                changes["cooldown_reason"] = (
                    f"failures={task.consecutive_failures} "
                    f">= trigger={self.cooldown_failures_trigger}"
                )
            if self.is_permanently_stopped(task):
                changes["stopped"] = True
                changes["stop_reason"] = (
                    f"failures={task.consecutive_failures} "
                    f">= skip_threshold="
                    f"{self.voi_estimator.skip_failure_threshold}"
                )

        cost_incurred = max(0.0, _safe_float(cost_incurred))
        if cost_incurred:
            changes["cost_incurred"] = cost_incurred

        self._outcome_history.append(
            {
                "task_id": task.task_id,
                "succeeded": succeeded,
                "cost_incurred": cost_incurred,
                "uncertainty_before": changes.get("uncertainty_before"),
                "uncertainty_after": changes.get("uncertainty_after"),
                "uncertainty_reduction": changes.get("uncertainty_reduction", 0.0),
                "consecutive_failures": task.consecutive_failures,
                "timestamp": changes["timestamp"],
            }
        )
        return changes

    def statistics(self) -> dict[str, Any]:
        """聚合统计累计成本、命中率和不确定性下降。

        Returns:
            包含以下字段的统计字典：
            - ``total_cost``：累计消耗成本
            - ``total_attempts``：累计执行次数
            - ``successful_attempts``：成功次数
            - ``hit_rate``：命中率 = 成功 / 总次数
            - ``total_uncertainty_reduction``：累计不确定性下降
            - ``average_uncertainty_reduction``：平均每次不确定性下降
            - ``decisions_made``：决策次数
            - ``tasks_stopped``：被永久停止的任务数（按当前已知状态）
        """
        outcomes = self._outcome_history
        if not outcomes:
            return {
                "total_cost": 0.0,
                "total_attempts": 0,
                "successful_attempts": 0,
                "hit_rate": 0.0,
                "total_uncertainty_reduction": 0.0,
                "average_uncertainty_reduction": 0.0,
                "decisions_made": len(self._decision_history),
                "tasks_stopped": 0,
            }
        total_cost = float(sum(o["cost_incurred"] for o in outcomes))
        total_attempts = len(outcomes)
        successful = sum(1 for o in outcomes if o["succeeded"])
        hit_rate = successful / total_attempts if total_attempts > 0 else 0.0
        reductions = [float(o.get("uncertainty_reduction") or 0.0) for o in outcomes]
        total_reduction = float(sum(reductions))
        avg_reduction = total_reduction / total_attempts if total_attempts > 0 else 0.0
        # tasks_stopped：统计最近一次决策中 skipped 且原因包含 threshold 的任务
        tasks_stopped = 0
        if self._decision_history:
            latest = self._decision_history[-1]
            tasks_stopped = sum(
                1
                for reason_list in latest.get("allocations", {}).values()
                if isinstance(reason_list, str) and "threshold" in reason_list
            )
        return {
            "total_cost": total_cost,
            "total_attempts": total_attempts,
            "successful_attempts": successful,
            "hit_rate": hit_rate,
            "total_uncertainty_reduction": total_reduction,
            "average_uncertainty_reduction": avg_reduction,
            "decisions_made": len(self._decision_history),
            "tasks_stopped": tasks_stopped,
        }
