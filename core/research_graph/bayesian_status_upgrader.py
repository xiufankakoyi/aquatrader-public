"""贝叶斯状态升级器：用概率推断替代固定阈值。

设计思路：
- 把"是否应该升级到下一状态"建模为二元假设 H
- 用 Beta(α, β) 分布作为共轭先验，α 表示 support 等效计数，β 表示 counter 等效计数
- 每条 evidence slot 按质量和匹配分数更新 α 或 β
- 后验均值 E[p] = α / (α + β) 表示升级置信度
- 有效样本量 α + β 表示证据充分程度
- 升级条件 = 置信度 > 动态阈值 AND 样本量 > 最小样本量 AND 反证比例可控

相比固定阈值的优势：
- 自动权衡证据数量与质量：1条高质量公告 ≈ 多条低质量论坛帖
- 反证自动抑制：counter 证据增加 β，降低后验均值
- 不确定性感知：证据少但质量高时仍可升级，证据多但互相矛盾时保持谨慎
- 无需人工微调 1/3/5/8 这类硬编码数值
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.research_graph.candidate_schema import (
    SOURCE_QUALITY_BOOST,
    CandidateStatus,
    next_status,
)


@dataclass
class BayesianUpgradeResult:
    """贝叶斯升级决策结果。"""

    new_status: str
    posterior_mean: float
    posterior_variance: float
    effective_support: float
    effective_counter: float
    confidence: float  # 后验均值
    credible_lower: float  # 95% 置信下限
    sample_size: float
    decision_reason: str


class BayesianStatusUpgrader:
    """基于 Beta-Bernoulli 共轭先验的状态升级器。

    Args:
        status_priors: 每个当前状态对应的 Beta 先验 (alpha, beta)。
            alpha 越大表示对"应升级"的先验信念越强，beta 越大越保守。
        confidence_thresholds: 每个当前状态升级所需的最低后验置信度。
        min_sample_sizes: 每个当前状态升级所需的最小有效样本量。
        counter_penalty: counter 证据对 beta 的惩罚倍数（默认 1.5）。
        uncertainty_bonus: 是否对证据充分的状态降低置信度阈值（默认 False，保持保守）。
        source_discount_kappa: 来源可信度折扣的平滑参数（默认 0.5）。
            折扣 = quality / (quality + kappa)；kappa 越小，高质量来源相对优势越大。
    """

    VERSION = "bayesian-v1"

    DEFAULT_STATUS_PRIORS: dict[str, tuple[float, float]] = {
        # 越往后的状态越保守（先验 beta 越大）
        CandidateStatus.DISCOVERED.value: (1.0, 1.0),   # uniform，最宽松
        CandidateStatus.CANDIDATE.value: (1.5, 1.5),
        CandidateStatus.SUPPORTED.value: (2.0, 2.0),
        CandidateStatus.ACTIVE.value: (3.0, 3.0),       # TRUSTED 需更谨慎
    }

    DEFAULT_CONFIDENCE_THRESHOLDS: dict[str, float] = {
        # 用 posterior_mean/credible_lower 加权做决策
        # 调整原则：
        # - 少量高质量证据（3条 announcement/offical）应能到 ACTIVE
        # - 大量低质量证据（forum/blog）应卡在 CANDIDATE/SUPPORTED，到不了 ACTIVE
        # - TRUSTED 保持最高门槛
        CandidateStatus.DISCOVERED.value: 0.55,
        CandidateStatus.CANDIDATE.value: 0.65,
        CandidateStatus.SUPPORTED.value: 0.65,
        CandidateStatus.ACTIVE.value: 0.85,
    }

    DEFAULT_MIN_SAMPLE_SIZES: dict[str, float] = {
        CandidateStatus.DISCOVERED.value: 1.0,
        CandidateStatus.CANDIDATE.value: 2.0,
        CandidateStatus.SUPPORTED.value: 2.5,
        CandidateStatus.ACTIVE.value: 4.0,
    }

    def __init__(
        self,
        status_priors: dict[str, tuple[float, float]] | None = None,
        confidence_thresholds: dict[str, float] | None = None,
        min_sample_sizes: dict[str, float] | None = None,
        counter_penalty: float = 1.5,
        uncertainty_bonus: bool = False,
        source_discount_kappa: float = 0.5,
    ) -> None:
        """初始化贝叶斯升级器。"""
        self.status_priors = status_priors or dict(self.DEFAULT_STATUS_PRIORS)
        self.confidence_thresholds = confidence_thresholds or dict(
            self.DEFAULT_CONFIDENCE_THRESHOLDS
        )
        self.min_sample_sizes = min_sample_sizes or dict(self.DEFAULT_MIN_SAMPLE_SIZES)
        self.counter_penalty = counter_penalty
        self.uncertainty_bonus = uncertainty_bonus
        self.source_discount_kappa = source_discount_kappa

    @classmethod
    def apply_policy(cls, policy: Any | None = None) -> "BayesianStatusUpgrader":
        """从 PolicyVersion 构造升级器，policy 为 None 时使用默认参数。

        Args:
            policy: PolicyVersion 或 None。parameters 中可包含:
                - claim_activation_threshold: SUPPORTED 升级阈值
                - source_discount_kappa: 来源折扣参数

        Returns:
            BayesianStatusUpgrader 实例。
        """
        thresholds = dict(cls.DEFAULT_CONFIDENCE_THRESHOLDS)
        kappa = 0.5
        if policy is not None:
            params = getattr(policy, "parameters", {}) or {}
            learned_threshold = params.get("claim_activation_threshold")
            if isinstance(learned_threshold, (int, float)):
                thresholds[CandidateStatus.SUPPORTED.value] = max(
                    0.55, min(0.90, float(learned_threshold))
                )
            learned_kappa = params.get("source_discount_kappa")
            if isinstance(learned_kappa, (int, float)):
                kappa = max(0.3, min(1.0, float(learned_kappa)))
        return cls(
            confidence_thresholds=thresholds,
            source_discount_kappa=kappa,
        )

    def decide(
        self,
        current_status: str,
        evidence_slots: list[dict[str, Any]],
        stance: str,
        source_quality: float,
    ) -> BayesianUpgradeResult:
        """根据证据做贝叶斯升级决策。

        Args:
            current_status: 当前状态。
            evidence_slots: 累计证据槽列表。
            stance: 本次证据立场（"support" 或 "counter"）。
            source_quality: 本次证据来源质量分。

        Returns:
            BayesianUpgradeResult，包含 new_status 和决策依据。
        """
        support_slots = self._independent_slots(
            [s for s in evidence_slots if s.get("stance") == "support"]
        )
        counter_slots = self._independent_slots(
            [s for s in evidence_slots if s.get("stance") == "counter"]
        )

        # 先验
        prior_alpha, prior_beta = self.status_priors.get(
            current_status, (2.0, 2.0)
        )

        # 观测更新：用 quality * match_score 作为等效计数
        effective_support = prior_alpha + sum(
            self._slot_weight(s) for s in support_slots
        )
        effective_counter = prior_beta + sum(
            self._slot_weight(s) * self.counter_penalty for s in counter_slots
        )

        total = effective_support + effective_counter
        if total <= 0:
            return BayesianUpgradeResult(
                new_status=current_status,
                posterior_mean=0.5,
                posterior_variance=0.0,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=0.5,
                credible_lower=0.0,
                sample_size=0.0,
                decision_reason="无有效证据",
            )

        posterior_mean = effective_support / total
        # Beta 分布方差 = αβ / ((α+β)^2 (α+β+1))
        posterior_variance = (
            effective_support * effective_counter
        ) / (total * total * (total + 1.0))

        # 95% 置信下限（正态近似，对 Beta 足够用）
        std = math.sqrt(posterior_variance)
        credible_lower = max(0.0, posterior_mean - 1.645 * std)

        # 升级目标
        target = next_status(current_status)
        # Candidate-level Bayesian scoring is advisory.  It may discover and
        # support a hypothesis, but only ClaimPolicy plus an audited
        # StateTransition may mint ACTIVE/TRUSTED.
        if target in {
            CandidateStatus.ACTIVE.value,
            CandidateStatus.TRUSTED.value,
        }:
            target = CandidateStatus.SUPPORTED.value

        # 降级/保持检查（保留原有安全规则）
        n_support = len(support_slots)
        n_counter = len(counter_slots)

        # 强反证 → REJECTED
        if n_counter >= 3 and n_support == 0:
            return BayesianUpgradeResult(
                new_status=CandidateStatus.REJECTED.value,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="强反证：counter≥3 且 support=0",
            )

        # 反证占优 → CONFLICTED
        if n_counter > n_support and n_counter >= 2:
            return BayesianUpgradeResult(
                new_status=CandidateStatus.CONFLICTED.value,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="反证占优：counter>support 且 counter≥2",
            )

        # 反证和中性材料只更新审计记录，不触发升级。
        if stance != "support":
            return BayesianUpgradeResult(
                new_status=current_status,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="本次不是支持证据，不触发升级",
            )

        # 终态保护
        if current_status in (
            CandidateStatus.TRUSTED.value,
            CandidateStatus.REJECTED.value,
        ):
            return BayesianUpgradeResult(
                new_status=current_status,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="已达终态",
            )

        # CONFLICTED 恢复规则
        if current_status == CandidateStatus.CONFLICTED.value:
            new_status = (
                CandidateStatus.CANDIDATE.value
                if n_support >= n_counter + 2
                else current_status
            )
            return BayesianUpgradeResult(
                new_status=new_status,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="CONFLICTED 恢复判断",
            )

        # DORMANT 恢复
        if current_status == CandidateStatus.DORMANT.value:
            return BayesianUpgradeResult(
                new_status=CandidateStatus.DISCOVERED.value,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="DORMANT 恢复为 DISCOVERED",
            )

        # 目标为终态则保持
        if target == current_status:
            return BayesianUpgradeResult(
                new_status=current_status,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason="无下一状态",
            )

        # 贝叶斯升级判定
        threshold = self.confidence_thresholds.get(current_status, 0.75)
        min_sample = self.min_sample_sizes.get(current_status, 2.0)

        # 不确定性奖励：证据充分时降低阈值（可选）
        effective_threshold = threshold
        if self.uncertainty_bonus and total >= min_sample * 2:
            effective_threshold = max(0.55, threshold - 0.05)

        # 决策分数： posterior_mean 与 credible_lower 的加权
        # - posterior_mean 反映证据平均质量（让高质量证据快速升级）
        # - credible_lower 反映不确定性（证据少时保守）
        # 加权系数 0.7/0.3 兼顾灵敏度与稳健性
        decision_score = 0.7 * posterior_mean + 0.3 * credible_lower

        # 终态门控：升级到 ACTIVE/TRUSTED 必须满足额外条件
        # 禁止贝叶斯升级器绕过统一控制面直接升到终态
        gate_reason = ""
        if target in (CandidateStatus.ACTIVE.value, CandidateStatus.TRUSTED.value):
            # 1. 反证检索必须已完成
            has_counter_search = any(
                s.get("stance") == "counter" for s in evidence_slots
            )
            if not has_counter_search:
                gate_reason = "反证检索未完成，禁止升级到 ACTIVE/TRUSTED"
            # 2. 独立来源必须 ≥ 2
            elif n_support < 2:
                gate_reason = f"独立来源不足({n_support}<2)，禁止升级到 ACTIVE/TRUSTED"
            # 3. 无未解决冲突
            elif n_counter > 0 and n_counter >= n_support:
                gate_reason = "存在未解决反证冲突，禁止升级到 ACTIVE/TRUSTED"
            # 4. TRUSTED 必须有人工复核标记
            if target == CandidateStatus.TRUSTED.value and not gate_reason:
                has_review = any(
                    s.get("review_passed") is True for s in evidence_slots
                )
                if not has_review:
                    gate_reason = "TRUSTED 必须经过人工复核，贝叶斯升级器无权直接授予"

        if gate_reason:
            # 门控失败：保持当前状态，不升级到终态
            return BayesianUpgradeResult(
                new_status=current_status,
                posterior_mean=posterior_mean,
                posterior_variance=posterior_variance,
                effective_support=effective_support,
                effective_counter=effective_counter,
                confidence=posterior_mean,
                credible_lower=credible_lower,
                sample_size=total,
                decision_reason=gate_reason,
            )

        if decision_score >= effective_threshold and total >= min_sample:
            new_status = target
            reason = (
                f"贝叶斯升级: decision_score={decision_score:.3f} >= "
                f"threshold={effective_threshold:.3f}, sample={total:.1f}"
            )
        else:
            new_status = current_status
            reason = (
                f"保持当前: decision_score={decision_score:.3f} < "
                f"threshold={effective_threshold:.3f} 或 sample={total:.1f} < {min_sample}"
            )

        return BayesianUpgradeResult(
            new_status=new_status,
            posterior_mean=posterior_mean,
            posterior_variance=posterior_variance,
            effective_support=effective_support,
            effective_counter=effective_counter,
            confidence=posterior_mean,
            credible_lower=credible_lower,
            sample_size=total,
            decision_reason=reason,
        )

    def _slot_weight(self, slot: dict[str, Any]) -> float:
        """计算单条证据对 Beta 参数的等效贡献。

        权重 = source_discount(quality) * match_score * time_decay * correlation_discount

        source_discount = quality / (quality + source_discount_kappa) 是 quality 的
        **唯一入口**，避免大量低质量证据堆砌主导后验：
        - quality=1.5 (announcement), kappa=0.5 → 折扣≈0.75，单条贡献大
        - quality=0.4 (forum), kappa=0.5 → 折扣≈0.44，单条贡献小，数量难弥补

        注意：不再额外乘 quality，否则来源质量会被非线性计算两次。
        """
        if slot.get("evidence_kind") in {
            "generated", "pseudo_evidence", "retrieval_hypothesis", "query_prototype"
        }:
            return 0.0
        if slot.get("stance") in {"stale", "retracted", "neutral"}:
            return 0.0
        quality = slot.get("quality", SOURCE_QUALITY_BOOST.get("unknown", 0.5))
        match_score = slot.get("match_score", 0.5)
        time_decay = 1.0
        timestamp = slot.get("published_at") or slot.get("event_date") or slot.get("retrieved_at")
        if timestamp:
            try:
                parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 86400)
                half_life = float(slot.get("half_life_days", 365.0) or 365.0)
                time_decay = math.exp(-math.log(2) * age_days / max(half_life, 1.0))
            except (TypeError, ValueError):
                time_decay = 0.75
        correlation_discount = max(0.1, min(1.0, float(slot.get("correlation_discount", 1.0) or 1.0)))
        # 来源可信度折扣：kappa 越小，高质量来源相对低质量的优势越大。
        # source_discount 是 quality 的唯一入口，不再额外乘 quality。
        source_discount = quality / (quality + self.source_discount_kappa)
        return max(0.0, source_discount * match_score * time_decay * correlation_discount)

    def _independent_slots(self, slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按来源血缘折叠转载/重复材料，每个独立来源只保留最强证据。"""
        strongest: dict[str, dict[str, Any]] = {}
        for slot in slots:
            lineage = str(
                slot.get("source_lineage_id")
                or slot.get("canonical_url")
                or slot.get("correlation_group")
                or slot.get("source_url")
                or slot.get("evidence_id")
                or id(slot)
            )
            current = strongest.get(lineage)
            if current is None or self._slot_weight(slot) > self._slot_weight(current):
                strongest[lineage] = slot
        return list(strongest.values())

    def explain(self, current_status: str, evidence_slots: list[dict[str, Any]]) -> str:
        """生成人类可读的升级决策解释。"""
        result = self.decide(current_status, evidence_slots, "support", 0.5)
        return (
            f"状态={current_status} → {result.new_status}\n"
            f"  后验置信度={result.confidence:.3f}\n"
            f"  95%置信下限={result.credible_lower:.3f}\n"
            f"  有效support={result.effective_support:.2f}\n"
            f"  有效counter={result.effective_counter:.2f}\n"
            f"  样本量={result.sample_size:.1f}\n"
            f"  原因={result.decision_reason}"
        )
