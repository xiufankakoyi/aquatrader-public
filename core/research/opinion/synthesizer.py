"""研究观点置信度合成器。

把多条 evidence 的质量、反证、时效性、来源多样性合成为单一 ResearchOpinion,
并显式拆解不确定性来源。
"""
from __future__ import annotations

import uuid
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from core.research.evidence.evidence_quality import score_evidence
from core.research.opinion.models import (
    ResearchOpinion,
    RiskFactor,
    UncertaintyBreakdown,
)


class OpinionConfidenceSynthesizer:
    """把证据合成为研究观点并计算整体置信度。

    合成逻辑(全部规则/统计,不引入外部 ML):
      1. 单条 evidence 质量:复用 evidence_quality.score_evidence 六维评分
      2. 支持证据平均质量 -> 基础置信度
      3. 反证惩罚:每条反证按质量扣减,最多扣 0.4
      4. 证据数量饱和:支持证据 >=5 条后边际收益递减
      5. 时效性衰减:旧证据整体打折扣
      6. 来源多样性:单一来源过高降低置信度
      7. 输出 confidence_score = base * penalty 三项 + bonus
    """

    def __init__(
        self,
        evidence_staleness_days: int = 30,
        max_contradiction_penalty: float = 0.4,
        source_concentration_threshold: float = 0.6,
    ) -> None:
        """初始化合成器。

        Args:
            evidence_staleness_days: 超过该天数的证据开始时效性衰减。
            max_contradiction_penalty: 反证最大总惩罚。
            source_concentration_threshold: 单一来源占比超过该阈值视为集中。
        """
        try:
            self._staleness_days = max(0, int(evidence_staleness_days))
        except (TypeError, ValueError, OverflowError):
            self._staleness_days = 30
        self._max_contradiction_penalty = _bounded_number(max_contradiction_penalty, 0.4)
        self._source_concentration_threshold = _bounded_number(
            source_concentration_threshold, 0.6
        )

    def synthesize(
        self,
        thesis: str,
        theme: str,
        evidence_list: list[Any],
        counter_evidence_list: list[Any] | None = None,
        *,
        root_neuron_id: str = "",
        root_claim_id: str = "",
        reference_date: str = "",
    ) -> ResearchOpinion:
        """从证据列表合成研究观点。

        Args:
            thesis: 核心观点陈述。
            theme: 主题。
            evidence_list: 支持证据列表(Evidence 或兼容 dict)。
            counter_evidence_list: 反证列表,可选。
            root_neuron_id: 关联神经元 ID。
            root_claim_id: 关联 claim ID。
            reference_date: 参考日期,默认今天。

        Returns:
            ResearchOpinion 实例。
        """
        evidence_list = list(evidence_list) if isinstance(evidence_list, (list, tuple)) else []
        counter_evidence_list = (
            list(counter_evidence_list)
            if isinstance(counter_evidence_list, (list, tuple)) else []
        )
        ref = reference_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        support_scores = [self._evidence_score(ev, ref) for ev in evidence_list]
        counter_scores = [self._evidence_score(ev, ref) for ev in counter_evidence_list]

        base_confidence = self._base_confidence(support_scores)
        contradiction_penalty = self._contradiction_penalty(counter_scores)
        staleness_penalty = self._staleness_penalty(evidence_list, ref)
        source_penalty = self._source_concentration_penalty(evidence_list)

        # data_gap: 支持证据不足则不确定性高
        data_gap = max(0.0, 1.0 - len(support_scores) / 5.0)
        if not support_scores:
            data_gap = 1.0

        # logic_leap: 证据平均质量越低,逻辑跳跃越大
        avg_quality = sum(support_scores) / max(1, len(support_scores)) if support_scores else 0.0
        logic_leap = max(0.0, 1.0 - avg_quality)

        uncertainty = UncertaintyBreakdown(
            data_gap=data_gap,
            logic_leap=logic_leap,
            contradiction=min(1.0, contradiction_penalty / 0.4) if contradiction_penalty else 0.0,
            staleness=staleness_penalty,
            source_concentration=source_penalty,
        )

        confidence_score = max(
            0.0,
            min(
                1.0,
                base_confidence
                * (1.0 - contradiction_penalty)
                * (1.0 - staleness_penalty)
                * (1.0 - source_penalty)
                + self._evidence_count_bonus(len(support_scores)),
            ),
        )

        risk_factors = self._extract_risk_factors(
            evidence_list, counter_evidence_list, uncertainty
        )
        revision_triggers = self._build_revision_triggers(
            uncertainty, counter_evidence_list
        )
        reasoning_chain = self._build_reasoning_chain(
            thesis,
            len(evidence_list),
            len(counter_evidence_list),
            avg_quality,
            confidence_score,
        )

        return ResearchOpinion(
            opinion_id=f"op_{uuid.uuid4().hex[:12]}",
            thesis=thesis,
            theme=theme,
            root_neuron_id=root_neuron_id,
            root_claim_id=root_claim_id,
            confidence_score=round(confidence_score, 4),
            evidence_ids=[self._evidence_id(ev) for ev in evidence_list],
            counter_evidence_ids=[self._evidence_id(ev) for ev in counter_evidence_list],
            evidence_quality_avg=round(avg_quality, 4),
            risk_factors=risk_factors,
            uncertainty=uncertainty,
            revision_triggers=revision_triggers,
            reasoning_chain=reasoning_chain,
        )

    def update_with_new_evidence(
        self,
        opinion: ResearchOpinion,
        new_evidence_list: list[Any],
        new_counter_evidence_list: list[Any] | None = None,
        reference_date: str = "",
    ) -> ResearchOpinion:
        """用新证据更新观点,返回新观点并保留 opinion_id。

        Args:
            opinion: 现有观点。
            new_evidence_list: 新增支持证据。
            new_counter_evidence_list: 新增反证。
            reference_date: 参考日期。

        Returns:
            更新后的 ResearchOpinion。
        """
        if not isinstance(opinion, ResearchOpinion):
            raise TypeError("opinion must be ResearchOpinion")
        new_evidence = (
            list(new_evidence_list)
            if isinstance(new_evidence_list, (list, tuple)) else []
        )
        new_counter = (
            list(new_counter_evidence_list)
            if isinstance(new_counter_evidence_list, (list, tuple)) else []
        )
        merged_support = list({ev_id: ev_id for ev_id in opinion.evidence_ids}.keys())
        merged_counter = list({ev_id: ev_id for ev_id in opinion.counter_evidence_ids}.keys())

        # 重新构建完整的 Evidence 对象,这里只保存 ID,实际更新需要外部传入完整列表
        # 简化:把现有 opinion 的 ID 与新证据合并,重新 synthesize
        merged_evidence = [{"evidence_id": eid} for eid in merged_support] + new_evidence
        merged_counter = [{"evidence_id": eid} for eid in merged_counter] + list(new_counter)

        updated = self.synthesize(
            thesis=opinion.thesis,
            theme=opinion.theme,
            evidence_list=merged_evidence,
            counter_evidence_list=merged_counter,
            root_neuron_id=opinion.root_neuron_id,
            root_claim_id=opinion.root_claim_id,
            reference_date=reference_date,
        )
        updated.opinion_id = opinion.opinion_id
        return updated

    def compute_confidence_delta(
        self,
        old_opinion: ResearchOpinion,
        new_opinion: ResearchOpinion,
    ) -> dict[str, float]:
        """计算两次观点更新之间的置信度变化。

        Args:
            old_opinion: 旧观点。
            new_opinion: 新观点。

        Returns:
            dict,含 confidence_delta、uncertainty_delta 等。
        """
        return {
            "confidence_delta": round(
                new_opinion.confidence_score - old_opinion.confidence_score, 4
            ),
            "uncertainty_delta": round(
                new_opinion.uncertainty.total - old_opinion.uncertainty.total, 4
            ),
            "evidence_count_delta": len(new_opinion.evidence_ids)
            - len(old_opinion.evidence_ids),
            "counter_evidence_count_delta": len(new_opinion.counter_evidence_ids)
            - len(old_opinion.counter_evidence_ids),
        }

    def _evidence_score(self, evidence: Any, reference_date: str) -> float:
        """计算单条证据的综合质量分,优先使用已有 quality_score。"""
        if hasattr(evidence, "quality_score") and evidence.quality_score:
            return _bounded_number(evidence.quality_score, 0.0)
        if isinstance(evidence, Mapping) and evidence.get("quality_score") is not None:
            return _bounded_number(evidence["quality_score"], 0.0)

        ev_type = self._get_attr(evidence, "evidence_type", "")
        confidence = self._get_attr(evidence, "confidence", "medium")
        grounding = self._get_attr(evidence, "grounding_status", "")
        cross_validation = self._get_attr(evidence, "cross_validation_status", "")
        data_date = self._get_attr(evidence, "data_date", "")
        text = self._get_attr(evidence, "evidence_text", "")

        score = score_evidence(
            evidence_type=ev_type,
            confidence=confidence,
            grounding_status=grounding,
            cross_validation_status=cross_validation,
            data_date=data_date,
            evidence_text=text,
            reference_date=reference_date,
        )
        return _bounded_number(score.total, 0.0)

    def _base_confidence(self, support_scores: list[float]) -> float:
        """基于支持证据质量计算基础置信度,带饱和。"""
        if not support_scores:
            return 0.0
        avg = sum(support_scores) / len(support_scores)
        count_factor = min(1.0, len(support_scores) / 5.0)
        return avg * (0.6 + 0.4 * count_factor)

    def _contradiction_penalty(self, counter_scores: list[float]) -> float:
        """反证惩罚,按反证质量累加,有上限。"""
        if not counter_scores:
            return 0.0
        penalty = sum(score * 0.25 for score in counter_scores)
        return min(penalty, self._max_contradiction_penalty)

    def _staleness_penalty(
        self, evidence_list: list[Any], reference_date: str
    ) -> float:
        """支持证据的时效性衰减惩罚。

        以 evidence_staleness_days 为阈值,越旧的证据惩罚越高。
        """
        if not evidence_list:
            return 0.0
        try:
            ref = datetime.strptime(reference_date[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            ref = datetime.now(timezone.utc)

        penalties: list[float] = []
        for ev in evidence_list:
            data_date = self._get_attr(ev, "data_date", "")
            if not data_date:
                penalties.append(0.3)
                continue
            try:
                dd = datetime.strptime(data_date[:10], "%Y-%m-%d")
                delta_days = (ref - dd).days
            except (ValueError, TypeError):
                penalties.append(0.3)
                continue
            if delta_days <= 7:
                penalties.append(0.0)
            elif delta_days <= 30:
                penalties.append(0.1)
            elif delta_days <= 90:
                penalties.append(0.25)
            else:
                penalties.append(0.45)
        return sum(penalties) / len(penalties)

    def _source_concentration_penalty(self, evidence_list: list[Any]) -> float:
        """来源集中惩罚。"""
        if not evidence_list:
            return 0.0
        sources: dict[str, int] = {}
        for ev in evidence_list:
            src = self._get_attr(ev, "source_name", "") or self._get_attr(ev, "source_type", "unknown")
            src = str(src)
            sources[src] = sources.get(src, 0) + 1
        if not sources:
            return 0.0
        max_share = max(sources.values()) / len(evidence_list)
        if max_share >= self._source_concentration_threshold:
            return min(1.0, (max_share - self._source_concentration_threshold) / 0.4)
        return 0.0

    def _evidence_count_bonus(self, count: int) -> float:
        """证据数量饱和后的小额奖励上限。"""
        return min(0.05, count * 0.01)

    def _extract_risk_factors(
        self,
        evidence_list: list[Any],
        counter_evidence_list: list[Any],
        uncertainty: UncertaintyBreakdown,
    ) -> list[RiskFactor]:
        """从证据和不确定性中提取风险因子。"""
        risks: list[RiskFactor] = []
        if uncertainty.data_gap > 0.5:
            risks.append(
                RiskFactor(
                    name="证据数量不足",
                    severity="high" if uncertainty.data_gap > 0.8 else "medium",
                    is_falsifiable=True,
                    falsification_evidence="新增至少 2 条独立来源的支持证据",
                )
            )
        if uncertainty.contradiction > 0.3:
            risks.append(
                RiskFactor(
                    name="存在反证",
                    severity="high" if uncertainty.contradiction > 0.6 else "medium",
                    is_falsifiable=True,
                    falsification_evidence="反证被后续数据证伪或被解释",
                )
            )
        if uncertainty.staleness > 0.3:
            risks.append(
                RiskFactor(
                    name="证据时效性不足",
                    severity="medium",
                    is_falsifiable=True,
                    falsification_evidence="出现 7 天内的新证据",
                )
            )
        if uncertainty.source_concentration > 0.3:
            risks.append(
                RiskFactor(
                    name="来源过于集中",
                    severity="medium",
                    is_falsifiable=True,
                    falsification_evidence="新增不同来源的交叉验证证据",
                )
            )
        # 从 counter evidence 文本提取风险名称
        for ev in counter_evidence_list[:2]:
            text = self._get_attr(ev, "summary", "") or self._get_attr(ev, "evidence_text", "")
            if text:
                risks.append(
                    RiskFactor(
                        name=text[:40],
                        severity="medium",
                        is_falsifiable=True,
                        falsification_evidence="出现相反事实证据",
                    )
                )
        return risks

    def _build_revision_triggers(
        self,
        uncertainty: UncertaintyBreakdown,
        counter_evidence_list: list[Any],
    ) -> list[str]:
        """生成观点修正触发条件。"""
        triggers: list[str] = []
        if uncertainty.data_gap > 0.5:
            triggers.append("新增 2 条以上独立来源的支持证据")
        if counter_evidence_list:
            triggers.append("出现新的反证或现有反证被强化")
        if uncertainty.staleness > 0.3:
            triggers.append("关键证据超过 30 天未更新")
        triggers.append("主题龙头公司财报或公告与该观点直接冲突")
        return triggers

    def _build_reasoning_chain(
        self,
        thesis: str,
        support_count: int,
        counter_count: int,
        avg_quality: float,
        confidence_score: float,
    ) -> list[str]:
        """生成观点的显式推理链。"""
        return [
            f"观点:{thesis}",
            f"支持证据 {support_count} 条,反证 {counter_count} 条",
            f"证据平均质量 {avg_quality:.2f}",
            f"综合置信度 {confidence_score:.2f}",
        ]

    def _evidence_id(self, evidence: Any) -> str:
        """提取 evidence_id。"""
        return self._get_attr(evidence, "evidence_id", "")

    @staticmethod
    def _get_attr(obj: Any, name: str, default: Any = "") -> Any:
        """兼容 dict 和 dataclass 的属性读取。"""
        if obj is None:
            return default
        if isinstance(obj, Mapping):
            return obj.get(name, default)
        return getattr(obj, name, default)


def _bounded_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(1.0, max(0.0, number)) if math.isfinite(number) else default
