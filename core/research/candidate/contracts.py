"""ResearchCandidate 最终输出契约。

AquaResearch 的最终统一输出，将产业链事实、盈利影响、市场预期差和投资观点
聚合成一个结构化候选对象，交由后续量化模块使用。

三类分数严格分开：
- research_score: 事实与研究可信度
- alpha_score: 盈利预期差、催化剂和兑现概率
- core_score: 产业核心程度和受益纯度

禁止用一个总分掩盖三者差异。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from collections.abc import Mapping
from uuid import uuid4

from core.research.earnings.interval import Interval


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


class CandidateStatus(str, Enum):
    """候选状态。"""
    DRAFT = "draft"
    ACTIVE = "active"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class ScoreComputability(str, Enum):
    """分数可计算性。"""
    COMPUTABLE = "computable"
    DEGRADED = "degraded"
    NOT_COMPUTABLE = "not_computable"


class ResearchCompleteness(str, Enum):
    """研究完整度标记（P0.6）。

    - RESEARCH_COMPLETE: 已绑定正式研究 run，且 readiness 无阻断维度。
    - RESEARCH_INCOMPLETE: 已绑定正式研究 run，但存在阻断维度或缺口。
    - OBSERVATIONAL_ONLY: 未绑定正式研究 run，仅基于主题/盈利影响构建。
    不能因技术状态或主题热度提升为 RESEARCH_COMPLETE。
    """
    RESEARCH_COMPLETE = "research_complete"
    RESEARCH_INCOMPLETE = "research_incomplete"
    OBSERVATIONAL_ONLY = "observational_only"


@dataclass
class ScoreBreakdown:
    """分数明细，支持溯源。"""
    score: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    components: dict[str, float] = field(default_factory=dict)
    computability: str = ScoreComputability.COMPUTABLE.value
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": (self.score if isinstance(self.score, Interval) else Interval(0.0, 0.0)).to_dict(),
            "components": dict(self.components) if isinstance(self.components, Mapping) else {},
            "computability": self.computability,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScoreBreakdown":
        data = dict(raw) if isinstance(raw, Mapping) else {}
        data["score"] = Interval.from_dict(data.get("score", {})) or Interval(0.0, 0.0)
        data["components"] = dict(data["components"]) if isinstance(data.get("components"), Mapping) else {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _score_dict(value: Any) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, ScoreBreakdown) else ScoreBreakdown().to_dict()


@dataclass
class ResearchCandidate:
    """AquaResearch 最终统一输出。

    只负责向后续量化模块提供研究结果，不负责：
    - K线判断
    - 分时买点
    - 仓位
    - 实盘下单
    - 止损价格
    - 交易指令
    """
    candidate_id: str = field(default_factory=lambda: new_id("cand"))
    symbol: str = ""
    sector_id: str = ""
    as_of: str = ""
    available_at: str = ""
    expires_at: str = ""
    thesis_id: str = ""
    thesis_status: str = ""
    # Claim 追溯字段：用于定位候选背后的 Claim 与映射质量
    claim_id: str = ""
    claim_status: str = ""
    mapping_confidence: float = 0.0
    company_id: str = ""
    evidence_as_of: str = ""
    evidence_confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    industry_core_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    company_exposure_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    earnings_impact: dict[str, Any] = field(default_factory=dict)
    expectation_gap: dict[str, Any] = field(default_factory=dict)
    expectation_gap_confidence: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    catalyst_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    catalyst_window: str = ""
    valuation_risk: Interval = field(default_factory=lambda: Interval(0.0, 1.0))
    uncertainty_score: Interval = field(default_factory=lambda: Interval(0.0, 1.0))
    research_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    alpha_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    core_score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    weakest_assumption: str = ""
    invalidation_conditions: list[str] = field(default_factory=list)
    next_review_at: str = ""
    source_trace: list[str] = field(default_factory=list)
    decision_trace: list[str] = field(default_factory=list)
    model_version: str = "candidate_v1"
    status: str = CandidateStatus.DRAFT.value
    # 主题追溯字段（动态主题控制面）
    # 由 DynamicThemeEngine 写入，adapter 透传到 QuantCandidate
    theme_id: str = ""            # 关联主题 ID
    theme_run_id: str = ""        # 关联主题运行 ID（版本链）
    theme_type: str = ""          # 主题类型（news_persistence/ice_reversal/...）
    theme_phase: str = ""         # 主题阶段（STARTUP/ACCELERATION/...）
    # P0.6：正式研究档案只读接入字段
    # research_run_id 关联到 CompanyResearchService 的具体 run_id；
    # research_readiness_summary 是八维 readiness 的精简快照（不复制事实状态）；
    # research_gaps 汇总阻断维度与 degradation_reasons，便于下游判断证据缺口；
    # research_completeness 标记是否完整、不完整或仅观察。
    # 旧候选（candidate_v1）这些字段为空，ResearchCompleteness 默认 OBSERVATIONAL_ONLY。
    research_run_id: str = ""
    research_readiness_summary: dict[str, Any] = field(default_factory=dict)
    research_gaps: list[str] = field(default_factory=list)
    research_completeness: str = ResearchCompleteness.OBSERVATIONAL_ONLY.value
    candidate_model_version: str = "candidate_v2"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "sector_id": self.sector_id,
            "as_of": self.as_of,
            "available_at": self.available_at,
            "expires_at": self.expires_at,
            "thesis_id": self.thesis_id,
            "thesis_status": self.thesis_status,
            "claim_id": self.claim_id,
            "claim_status": self.claim_status,
            "mapping_confidence": self.mapping_confidence,
            "company_id": self.company_id,
            "evidence_as_of": self.evidence_as_of,
            "evidence_confidence": (self.evidence_confidence if isinstance(self.evidence_confidence, Interval) else Interval(0.0, 0.0)).to_dict(),
            "industry_core_score": _score_dict(self.industry_core_score),
            "company_exposure_score": _score_dict(self.company_exposure_score),
            "earnings_impact": self.earnings_impact,
            "expectation_gap": self.expectation_gap,
            "expectation_gap_confidence": (self.expectation_gap_confidence if isinstance(self.expectation_gap_confidence, Interval) else Interval(0.0, 0.0)).to_dict(),
            "catalyst_score": _score_dict(self.catalyst_score),
            "catalyst_window": self.catalyst_window,
            "valuation_risk": (self.valuation_risk if isinstance(self.valuation_risk, Interval) else Interval(0.0, 0.0)).to_dict(),
            "uncertainty_score": (self.uncertainty_score if isinstance(self.uncertainty_score, Interval) else Interval(0.0, 0.0)).to_dict(),
            "research_score": _score_dict(self.research_score),
            "alpha_score": _score_dict(self.alpha_score),
            "core_score": _score_dict(self.core_score),
            "weakest_assumption": self.weakest_assumption,
            "invalidation_conditions": [item for item in self.invalidation_conditions if isinstance(item, str)] if isinstance(self.invalidation_conditions, (list, tuple)) else [],
            "next_review_at": self.next_review_at,
            "source_trace": [item for item in self.source_trace if isinstance(item, str)] if isinstance(self.source_trace, (list, tuple)) else [],
            "decision_trace": [item for item in self.decision_trace if isinstance(item, str)] if isinstance(self.decision_trace, (list, tuple)) else [],
            "model_version": self.model_version,
            "status": self.status,
            # 主题追溯字段（动态主题控制面）
            "theme_id": self.theme_id,
            "theme_run_id": self.theme_run_id,
            "theme_type": self.theme_type,
            "theme_phase": self.theme_phase,
            # P0.6：正式研究档案只读接入字段
            "research_run_id": self.research_run_id,
            "research_readiness_summary": dict(self.research_readiness_summary) if isinstance(self.research_readiness_summary, Mapping) else {},
            "research_gaps": [item for item in self.research_gaps if isinstance(item, str)] if isinstance(self.research_gaps, (list, tuple)) else [],
            "research_completeness": self.research_completeness,
            "candidate_model_version": self.candidate_model_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ResearchCandidate":
        """从字典反序列化。"""
        data = dict(raw) if isinstance(raw, Mapping) else {}
        for key in ("evidence_confidence", "expectation_gap_confidence",
                    "valuation_risk", "uncertainty_score"):
            data[key] = Interval.from_dict(data.get(key, {})) or Interval(0.0, 0.0)
        for key in ("industry_core_score", "company_exposure_score",
                    "catalyst_score", "research_score", "alpha_score", "core_score"):
            data[key] = ScoreBreakdown.from_dict(data.get(key, {}))
        for key in ("invalidation_conditions", "source_trace", "decision_trace", "research_gaps"):
            data[key] = [item for item in data.get(key, []) if isinstance(item, str)] if isinstance(data.get(key), (list, tuple)) else []
        data["research_readiness_summary"] = dict(data["research_readiness_summary"]) if isinstance(data.get("research_readiness_summary"), Mapping) else {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CandidateReport:
    """人类可读研究报告。

    所有关键结论可定位到 Claim、Evidence 或计算公式。
    """
    candidate_id: str = ""
    symbol: str = ""
    report_sections: dict[str, str] = field(default_factory=dict)
    key_claims: list[dict[str, str]] = field(default_factory=list)
    evidence_references: list[dict[str, str]] = field(default_factory=list)
    formula_references: list[dict[str, str]] = field(default_factory=list)
    generated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CandidateReport":
        data = dict(raw) if isinstance(raw, Mapping) else {}
        for key in ("report_sections",):
            data[key] = dict(data[key]) if isinstance(data.get(key), Mapping) else {}
        for key in ("key_claims", "evidence_references", "formula_references"):
            data[key] = [dict(item) for item in data.get(key, []) if isinstance(item, Mapping)] if isinstance(data.get(key), (list, tuple)) else []
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
