"""信息价值（Value of Information, VOI）评估。

VOI 用于衡量对单个研究任务投入单位预算后，期望减少的关键不确定性，
结合证据质量、历史命中率和检索成本进行归一化，确保「高热度主题」
不会仅凭 ``importance`` 自动获得最高预算。

公式（在 ``ValueOfInformationEstimator.estimate`` 内实施）::

    expected_uncertainty_reduction = uncertainty * evidence_quality
    voi_score = (
        expected_uncertainty_reduction
        * max(eps, hit_rate)
        * max(eps, importance)
        * block_factor
        * urgency_factor
        * failure_factor
    ) / max(min_cost_floor, retrieval_cost)

- ``block_factor``：阻断模型任务加成，默认 1.5。
- ``urgency_factor``：截止时间越近越大，已过期取上限。
- ``failure_factor``：``failure_decay_base ** consecutive_failures``，
  连续失败按指数衰减；超过阈值永久停止（``skipped=True``）。
- 冷却未到的任务直接 ``skipped``，VOI=0。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


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


def _parse_iso_datetime(value: str) -> datetime | None:
    """解析 ISO 字符串为 aware datetime；空值或解析失败返回 None。

    Args:
        value: ISO 时间字符串

    Returns:
        timezone-aware datetime 或 None
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class VOIResult:
    """单任务 VOI 计算结果。

    Attributes:
        task_id: 任务 ID
        expected_uncertainty_reduction: 期望不确定性下降（0-1）
        expected_evidence_quality: 期望证据质量（0-1）
        voi_score: 综合 VOI 评分（>=0，无量纲）
        recommended_allocation: 建议分配比例占预算池的份额（0-1）
        components: 评分各分量明细，便于解释和审计
        skipped: 是否被跳过（冷却中或永久停止）
        skip_reason: 跳过原因
    """

    task_id: str
    expected_uncertainty_reduction: float = 0.0
    expected_evidence_quality: float = 0.0
    voi_score: float = 0.0
    recommended_allocation: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str = ""

    def __post_init__(self) -> None:
        self.expected_uncertainty_reduction = max(0.0, min(1.0, _safe_float(self.expected_uncertainty_reduction)))
        self.expected_evidence_quality = max(0.0, min(1.0, _safe_float(self.expected_evidence_quality)))
        self.voi_score = max(0.0, _safe_float(self.voi_score))
        self.recommended_allocation = max(0.0, min(1.0, _safe_float(self.recommended_allocation)))
        self.components = dict(self.components) if isinstance(self.components, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为可审计字典。"""
        return {
            "task_id": self.task_id,
            "expected_uncertainty_reduction": self.expected_uncertainty_reduction,
            "expected_evidence_quality": self.expected_evidence_quality,
            "voi_score": self.voi_score,
            "recommended_allocation": self.recommended_allocation,
            "components": dict(self.components),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


class ValueOfInformationEstimator:
    """基于不确定性和证据质量的 VOI 估计器。

    评分综合考虑：不确定性基线、阻断模型加成、证据质量、历史命中率、
    重要性、时间紧迫度、检索成本和失败衰减。冷却未到或失败超阈值时
    返回 ``skipped=True`` 的零分结果，确保这些任务在预算分配中被跳过。

    Args:
        block_bonus: 阻断模型任务的加成系数（>=1.0）
        deadline_urgency_cap: 时间紧迫度上限（>=1.0）
        deadline_urgency_window_days: 紧迫度线性提升的窗口天数
        failure_decay_base: 每次连续失败的衰减系数 (0, 1]
        min_cost_floor: 检索成本下限，避免除零
        skip_failure_threshold: 连续失败达到阈值后永久停止
        min_hit_rate_floor: 命中率下限，避免无信息任务获得高 VOI
        min_importance_floor: 重要性下限，避免无价值任务获得高 VOI
        now_provider: 自定义时间提供器，便于测试
    """

    DEFAULT_BLOCK_BONUS: float = 1.5
    DEFAULT_DEADLINE_URGENCY_CAP: float = 2.0
    DEFAULT_DEADLINE_URGENCY_WINDOW_DAYS: int = 7
    DEFAULT_FAILURE_DECAY_BASE: float = 0.6
    DEFAULT_MIN_COST_FLOOR: float = 0.01
    DEFAULT_SKIP_FAILURE_THRESHOLD: int = 5
    DEFAULT_MIN_HIT_RATE_FLOOR: float = 0.01
    DEFAULT_MIN_IMPORTANCE_FLOOR: float = 0.01

    def __init__(
        self,
        *,
        block_bonus: float = DEFAULT_BLOCK_BONUS,
        deadline_urgency_cap: float = DEFAULT_DEADLINE_URGENCY_CAP,
        deadline_urgency_window_days: int = DEFAULT_DEADLINE_URGENCY_WINDOW_DAYS,
        failure_decay_base: float = DEFAULT_FAILURE_DECAY_BASE,
        min_cost_floor: float = DEFAULT_MIN_COST_FLOOR,
        skip_failure_threshold: int = DEFAULT_SKIP_FAILURE_THRESHOLD,
        min_hit_rate_floor: float = DEFAULT_MIN_HIT_RATE_FLOOR,
        min_importance_floor: float = DEFAULT_MIN_IMPORTANCE_FLOOR,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化 VOI 估计器并校验参数边界。"""
        failure_decay_base = _safe_float(failure_decay_base, math.nan)
        block_bonus = _safe_float(block_bonus, math.nan)
        deadline_urgency_cap = _safe_float(deadline_urgency_cap, math.nan)
        deadline_urgency_window_days = _safe_int(deadline_urgency_window_days, -1)
        failure_decay_base = _safe_float(failure_decay_base, math.nan)
        min_cost_floor = _safe_float(min_cost_floor, math.nan)
        skip_failure_threshold = _safe_int(skip_failure_threshold, -1)
        min_hit_rate_floor = _safe_float(min_hit_rate_floor, math.nan)
        min_importance_floor = _safe_float(min_importance_floor, math.nan)
        if not 0.0 < failure_decay_base <= 1.0:
            raise ValueError("failure_decay_base 必须在 (0, 1] 区间")
        if block_bonus < 1.0:
            raise ValueError("block_bonus 必须 >= 1.0")
        if deadline_urgency_cap < 1.0:
            raise ValueError("deadline_urgency_cap 必须 >= 1.0")
        if deadline_urgency_window_days < 1:
            raise ValueError("deadline_urgency_window_days 必须 >= 1")
        if skip_failure_threshold < 1:
            raise ValueError("skip_failure_threshold 必须 >= 1")
        if min_cost_floor <= 0:
            raise ValueError("min_cost_floor 必须 > 0")
        if min_hit_rate_floor < 0:
            raise ValueError("min_hit_rate_floor 不能为负")
        if min_importance_floor < 0:
            raise ValueError("min_importance_floor 不能为负")

        self.block_bonus = block_bonus
        self.deadline_urgency_cap = deadline_urgency_cap
        self.deadline_urgency_window_days = deadline_urgency_window_days
        self.failure_decay_base = failure_decay_base
        self.min_cost_floor = min_cost_floor
        self.skip_failure_threshold = skip_failure_threshold
        self.min_hit_rate_floor = min_hit_rate_floor
        self.min_importance_floor = min_importance_floor
        self._now_provider = now_provider

    def _now(self) -> datetime:
        """返回当前 UTC 时间；测试可注入 now_provider。"""
        if callable(self._now_provider):
            value = self._now_provider()
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value
        return datetime.now(timezone.utc)

    def _deadline_urgency(self, deadline: str, now: datetime) -> float:
        """计算 deadline 紧迫度因子。

        - 无 deadline 返回 1.0（中性）。
        - 已过期取 ``deadline_urgency_cap``。
        - 在窗口内线性插值 [1.0, cap]。
        - 超过窗口返回 1.0。

        Args:
            deadline: 截止时间 ISO 字符串
            now: 当前参考时间

        Returns:
            紧迫度因子
        """
        deadline_dt = _parse_iso_datetime(deadline)
        if deadline_dt is None:
            return 1.0
        if deadline_dt <= now:
            return self.deadline_urgency_cap
        seconds_remaining = (deadline_dt - now).total_seconds()
        window_seconds = self.deadline_urgency_window_days * 86400
        if seconds_remaining >= window_seconds:
            return 1.0
        # 越紧迫越大，区间 [1.0, deadline_urgency_cap]
        ratio = 1.0 - (seconds_remaining / window_seconds)
        return 1.0 + (self.deadline_urgency_cap - 1.0) * ratio

    def estimate(self, task: Any) -> VOIResult:
        """估计单个任务的 VOI。

        Args:
            task: ``ResearchTask`` 或具备相同属性的鸭子类型对象

        Returns:
            ``VOIResult``，包含评分和分量明细
        """
        task_id = str(getattr(task, "task_id", ""))
        cooldown_until = str(getattr(task, "cooldown_until", "") or "")
        consecutive_failures = max(0, _safe_int(getattr(task, "consecutive_failures", 0), 0))

        # 1) 永久停止：失败次数超过阈值
        if consecutive_failures >= self.skip_failure_threshold:
            return VOIResult(
                task_id=task_id,
                skipped=True,
                skip_reason=(
                    f"consecutive_failures={consecutive_failures} "
                    f">= threshold={self.skip_failure_threshold}"
                ),
            )

        # 2) 冷却中：cooldown_until 未过
        now = self._now()
        cooldown_dt = _parse_iso_datetime(cooldown_until)
        if cooldown_dt is not None and cooldown_dt > now:
            return VOIResult(
                task_id=task_id,
                skipped=True,
                skip_reason=f"cooldown_until {cooldown_until} not reached",
            )

        # 3) 夹紧输入参数到合法区间
        uncertainty = max(0.0, min(1.0, _safe_float(getattr(task, "uncertainty_score", 0.0))))
        evidence_quality = max(
            0.0, min(1.0, _safe_float(getattr(task, "evidence_quality_estimate", 0.0)))
        )
        retrieval_cost = max(
            self.min_cost_floor, _safe_float(getattr(task, "retrieval_cost", 0.0))
        )
        hit_rate = max(0.0, min(1.0, _safe_float(getattr(task, "historical_hit_rate", 0.0))))
        importance = max(0.0, min(1.0, _safe_float(getattr(task, "importance", 0.0))))
        blocks_model = bool(getattr(task, "blocks_model", False))
        deadline = str(getattr(task, "verification_deadline", "") or "")

        # 4) 期望不确定性下降：高质量证据最多能把不确定性下降到 0
        expected_uncertainty_reduction = uncertainty * evidence_quality

        # 5) 阻断模型加成
        block_factor = self.block_bonus if blocks_model else 1.0

        # 6) 时间紧迫度
        urgency_factor = self._deadline_urgency(deadline, now)

        # 7) 失败衰减：每次连续失败按 failure_decay_base 衰减
        failure_factor = self.failure_decay_base ** consecutive_failures

        # 8) VOI 评分组合
        # 分子：期望下降 × 命中率（夹紧下限）× 重要性（夹紧下限）
        #       × 阻断加成 × 紧迫度 × 失败衰减
        # 分母：检索成本（已夹紧到 min_cost_floor）
        numerator = (
            expected_uncertainty_reduction
            * max(self.min_hit_rate_floor, hit_rate)
            * max(self.min_importance_floor, importance)
            * block_factor
            * urgency_factor
            * failure_factor
        )
        voi_score = numerator / retrieval_cost

        components = {
            "uncertainty": uncertainty,
            "evidence_quality": evidence_quality,
            "hit_rate": hit_rate,
            "importance": importance,
            "retrieval_cost": retrieval_cost,
            "block_factor": block_factor,
            "urgency_factor": urgency_factor,
            "failure_factor": failure_factor,
            "expected_uncertainty_reduction": expected_uncertainty_reduction,
        }

        return VOIResult(
            task_id=task_id,
            expected_uncertainty_reduction=expected_uncertainty_reduction,
            expected_evidence_quality=evidence_quality,
            voi_score=voi_score,
            components=components,
        )

    def estimate_many(self, tasks: list[Any]) -> list[VOIResult]:
        """对多个任务估计 VOI。

        Args:
            tasks: 任务列表

        Returns:
            与输入顺序一致的 ``VOIResult`` 列表
        """
        return [self.estimate(task) for task in tasks] if isinstance(tasks, (list, tuple)) else []
