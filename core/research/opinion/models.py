"""研究观点数据模型。

提供单一对象"ResearchOpinion",把证据、置信度、风险、反证、修正触发条件绑定在一起。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from core.research_graph.models import utc_now


@dataclass
class RiskFactor:
    """单一风险因子。"""

    name: str
    severity: str = "medium"  # high / medium / low
    is_falsifiable: bool = True
    falsification_evidence: str = ""  # 什么证据可证伪该风险

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "name": self.name,
            "severity": self.severity,
            "is_falsifiable": self.is_falsifiable,
            "falsification_evidence": self.falsification_evidence,
        }


@dataclass
class UncertaintyBreakdown:
    """不确定性拆解。

    五个方面(0-1),数值越大表示该来源的不确定性越高。
    """

    data_gap: float = 0.0  # 数据缺口
    logic_leap: float = 0.0  # 逻辑跳跃(证据到论点的推导距离)
    contradiction: float = 0.0  # 反证存在
    staleness: float = 0.0  # 证据时效性衰减
    source_concentration: float = 0.0  # 来源集中(单一来源权重过高)

    def __post_init__(self) -> None:
        for name in (
            "data_gap", "logic_leap", "contradiction", "staleness", "source_concentration"
        ):
            try:
                value = float(getattr(self, name))
            except (TypeError, ValueError, OverflowError):
                value = 0.0
            setattr(self, name, min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0)

    @property
    def total(self) -> float:
        """加权总不确定性,范围 [0,1]。"""
        return min(
            1.0,
            (
                self.data_gap * 0.30
                + self.logic_leap * 0.20
                + self.contradiction * 0.25
                + self.staleness * 0.15
                + self.source_concentration * 0.10
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "data_gap": round(self.data_gap, 4),
            "logic_leap": round(self.logic_leap, 4),
            "contradiction": round(self.contradiction, 4),
            "staleness": round(self.staleness, 4),
            "source_concentration": round(self.source_concentration, 4),
            "total": round(self.total, 4),
        }


@dataclass
class ResearchOpinion:
    """单一研究观点。

    把 thesis(观点陈述)、证据、置信度、风险、反证、修正触发条件绑定在一起,
    供研究员和 CIO agent 直接消费。
    """

    opinion_id: str
    thesis: str  # 核心观点陈述,例如"HBM 电镀液国产替代正在加速"
    theme: str = ""  # 所属主题
    root_neuron_id: str = ""  # 可选:关联神经元
    root_claim_id: str = ""  # 可选:关联 claim

    confidence_score: float = 0.0  # 0-1 数值置信度
    confidence_level: str = "low"  # high / medium / low

    evidence_ids: list[str] = field(default_factory=list)
    counter_evidence_ids: list[str] = field(default_factory=list)
    evidence_quality_avg: float = 0.0

    risk_factors: list[RiskFactor] = field(default_factory=list)
    uncertainty: UncertaintyBreakdown = field(default_factory=UncertaintyBreakdown)

    revision_triggers: list[str] = field(default_factory=list)
    reasoning_chain: list[str] = field(default_factory=list)

    scenario_stress_results: dict[str, Any] = field(default_factory=dict)

    generated_at: str = field(default_factory=utc_now)
    expires_at: str = ""

    def __post_init__(self) -> None:
        """设置默认过期时间(30 天后)并校验置信度档位。"""
        self.opinion_id = str(self.opinion_id or "")
        self.thesis = str(self.thesis or "")
        self.theme = str(self.theme or "")
        self.root_neuron_id = str(self.root_neuron_id or "")
        self.root_claim_id = str(self.root_claim_id or "")
        try:
            score = float(self.confidence_score)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        self.confidence_score = min(1.0, max(0.0, score)) if math.isfinite(score) else 0.0
        try:
            quality = float(self.evidence_quality_avg)
        except (TypeError, ValueError, OverflowError):
            quality = 0.0
        self.evidence_quality_avg = min(1.0, max(0.0, quality)) if math.isfinite(quality) else 0.0
        self.evidence_ids = _text_list(self.evidence_ids)
        self.counter_evidence_ids = _text_list(self.counter_evidence_ids)
        self.revision_triggers = _text_list(self.revision_triggers)
        self.reasoning_chain = _text_list(self.reasoning_chain)
        self.risk_factors = [
            item for item in self.risk_factors if isinstance(item, RiskFactor)
        ] if isinstance(self.risk_factors, (list, tuple)) else []
        if not isinstance(self.uncertainty, UncertaintyBreakdown):
            self.uncertainty = UncertaintyBreakdown()
        self.scenario_stress_results = (
            dict(self.scenario_stress_results)
            if isinstance(self.scenario_stress_results, Mapping) else {}
        )
        self.generated_at = str(self.generated_at or "")
        self.expires_at = str(self.expires_at or "")
        if not self.expires_at:
            try:
                dt = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                dt = datetime.now(timezone.utc)
            self.expires_at = (dt + timedelta(days=30)).isoformat()

        if self.confidence_level not in {"high", "medium", "low"}:
            self.confidence_level = self._score_to_level(self.confidence_score)

    @staticmethod
    def _score_to_level(score: float) -> str:
        """数值置信度转档位。"""
        if score >= 0.75:
            return "high"
        if score >= 0.45:
            return "medium"
        return "low"

    def is_expired(self, reference_time: str = "") -> bool:
        """检查观点是否过期。"""
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            ref = (
                datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
                if reference_time
                else datetime.now(timezone.utc)
            )
            return ref >= exp
        except (ValueError, AttributeError):
            return False

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "opinion_id": self.opinion_id,
            "thesis": self.thesis,
            "theme": self.theme,
            "root_neuron_id": self.root_neuron_id,
            "root_claim_id": self.root_claim_id,
            "confidence_score": round(self.confidence_score, 4),
            "confidence_level": self.confidence_level,
            "evidence_ids": list(self.evidence_ids),
            "counter_evidence_ids": list(self.counter_evidence_ids),
            "evidence_quality_avg": round(self.evidence_quality_avg, 4),
            "risk_factors": [r.to_dict() for r in self.risk_factors],
            "uncertainty": self.uncertainty.to_dict(),
            "revision_triggers": list(self.revision_triggers),
            "reasoning_chain": list(self.reasoning_chain),
            "scenario_stress_results": dict(self.scenario_stress_results),
            "generated_at": self.generated_at,
            "expires_at": self.expires_at,
        }


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if item is not None and str(item)]
