"""ResearchCandidate 构建引擎。

从 InvestmentThesis、EarningsImpact、ExpectationGap 等上游输入
构建最终的 ResearchCandidate，计算三类独立分数并生成人类可读报告。

分数计算原则：
- research_score: 事实与研究可信度，基于证据置信度、Claim 状态、数据质量
- alpha_score: 盈利预期差、催化剂和兑现概率，市场预期缺失时必须降级
- core_score: 产业核心程度和受益纯度，不得等同于 Alpha
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
import math
from typing import Any

from core.research.earnings.interval import Interval as I

from .contracts import (
    CandidateReport,
    CandidateStatus,
    ResearchCandidate,
    ResearchCompleteness,
    ScoreBreakdown,
    ScoreComputability,
    utc_now,
)


def _time(value: str) -> datetime | None:
    """解析 ISO 时间字符串。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return normalized if math.isfinite(normalized) else default


def _optional_float(value: Any) -> float | None:
    """返回有限浮点数；非法输入按缺失处理。"""
    if isinstance(value, bool):
        return None
    normalized = _finite_float(value, float("nan"))
    return normalized if math.isfinite(normalized) else None


def _unit_float(value: Any) -> float | None:
    """规范化 [0, 1] 分数，非法或非有限输入按缺失处理。"""
    normalized = _optional_float(value)
    return max(0.0, min(1.0, normalized)) if normalized is not None else None


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _score_mid(value: Any) -> float:
    if not isinstance(value, ScoreBreakdown) or not isinstance(value.score, I):
        return 0.0
    return value.score.mid


def _computability(value: Any) -> str:
    return value.computability if isinstance(value, ScoreBreakdown) else "unknown"


def _score_reason(value: Any) -> str:
    return _text(value.reason) if isinstance(value, ScoreBreakdown) else ""


def _format_interval(value: Any, *, scale: float = 1.0, decimals: int = 4) -> str:
    data = _as_dict(value)
    low = _optional_float(data.get("low"))
    high = _optional_float(data.get("high"))
    if low is None or high is None:
        return ""
    return f"[{low / scale:.{decimals}f}, {high / scale:.{decimals}f}]"


class CandidateBuilder:
    """从上游研究对象构建 ResearchCandidate。

    不输出买卖、仓位、止盈止损建议。
    """

    def build(
        self, *,
        thesis: dict[str, Any] | None = None,
        earnings_impact: dict[str, Any] | None = None,
        expectation_gap: dict[str, Any] | None = None,
        valuation_snapshot: dict[str, Any] | None = None,
        sector_id: str = "",
        as_of: str = "",
        industry_core_score: float | None = None,
        company_exposure_score: float | None = None,
        evidence_confidence: I | None = None,
        catalysts: list[dict[str, Any]] | None = None,
        research_run_id: str = "",
        research_readiness_summary: dict[str, Any] | None = None,
        research_gaps: list[str] | None = None,
        research_completeness: str = ResearchCompleteness.OBSERVATIONAL_ONLY.value,
    ) -> ResearchCandidate:
        """构建 ResearchCandidate。

        Args:
            thesis: InvestmentThesis 字典（含 thesis_id, status, required_claim_ids 等）
            earnings_impact: EarningsImpact 字典（含 eps_impact, status 等）
            expectation_gap: ExpectationGap 字典（含 gap, relative_gap, status 等）
            valuation_snapshot: ValuationSnapshot 字典
            sector_id: 产业板块 ID
            as_of: PIT 时点
            industry_core_score: 产业核心程度评分 (0-1)，None 表示暂无本地证据
            company_exposure_score: 公司收入敞口评分 (0-1)，None 表示暂无本地证据
            evidence_confidence: 证据置信度区间，None 表示暂无本地证据
            catalysts: 催化剂列表
            research_run_id: P0.6 关联的正式研究 run ID，空串表示未绑定。
            research_readiness_summary: P0.6 八维 readiness 精简快照，不复制事实状态。
            research_gaps: P0.6 关键缺口列表（degradation_reasons + readiness.missing_items 汇总）。
            research_completeness: P0.6 研究完整度标记；非 complete 时强制降级 status。

        Returns:
            ResearchCandidate 对象
        """
        thesis = _as_dict(thesis)
        earnings_impact = _as_dict(earnings_impact)
        expectation_gap = _as_dict(expectation_gap)
        valuation_snapshot = _as_dict(valuation_snapshot)
        catalysts = _mapping_list(catalysts)
        research_gaps = _text_list(research_gaps)
        ev_conf = evidence_confidence if isinstance(evidence_confidence, I) else I(0.0, 0.0)

        symbol = _text(thesis.get("company_symbol")) or _text(earnings_impact.get("symbol"))
        sector_id = _text(sector_id) or _text(thesis.get("sector_id"))
        as_of = _text(as_of)
        research_run_id = _text(research_run_id)
        industry_core_score = _unit_float(industry_core_score)
        company_exposure_score = _unit_float(company_exposure_score)
        now = utc_now()

        # 构建产业核心程度 ScoreBreakdown（None 时标记不可计算）
        if industry_core_score is not None:
            industry_core_breakdown = ScoreBreakdown(
                score=I(industry_core_score * 0.8, industry_core_score),
                components={"industry_core": industry_core_score},
                computability=ScoreComputability.COMPUTABLE.value,
            )
        else:
            industry_core_breakdown = ScoreBreakdown(
                score=I(0.0, 0.0),
                components={},
                computability=ScoreComputability.NOT_COMPUTABLE.value,
                reason="暂无本地证据: industry_core_score",
            )

        # 构建公司收入敞口 ScoreBreakdown（None 时标记不可计算）
        if company_exposure_score is not None:
            company_exposure_breakdown = ScoreBreakdown(
                score=I(company_exposure_score * 0.8, company_exposure_score),
                components={"company_exposure": company_exposure_score},
                computability=ScoreComputability.COMPUTABLE.value,
            )
        else:
            company_exposure_breakdown = ScoreBreakdown(
                score=I(0.0, 0.0),
                components={},
                computability=ScoreComputability.NOT_COMPUTABLE.value,
                reason="暂无本地证据: company_exposure_score",
            )

        # 计算三类分数
        research_score = self._compute_research_score(
            thesis, earnings_impact, ev_conf,
        )
        alpha_score = self._compute_alpha_score(
            expectation_gap, earnings_impact, catalysts, valuation_snapshot, as_of,
        )
        core_score = self._compute_core_score(
            industry_core_score, company_exposure_score, earnings_impact,
        )

        # 催化剂评分
        catalyst_score = self._compute_catalyst_score(catalysts, as_of)

        # 估值风险
        valuation_risk = self._compute_valuation_risk(valuation_snapshot)

        # 不确定性
        uncertainty_score = self._compute_uncertainty(
            research_score, alpha_score, earnings_impact,
        )

        # 催化剂窗口
        catalyst_window = self._extract_catalyst_window(catalysts)

        # 预期差置信度
        gap_confidence = I.from_dict(
            expectation_gap.get("gap_confidence", {"low": 0.0, "high": 0.0})
        ) if expectation_gap else I(0.0, 0.0)
        gap_confidence = gap_confidence or I(0.0, 0.0)

        # 最弱假设
        weakest = _text(thesis.get("weakest_assumption"))

        # 失效条件
        invalidation = _text_list(thesis.get("invalidation_conditions"))

        # 下次复核时间
        next_review = _text(thesis.get("next_review_at")) or _text(thesis.get("review_at"))

        # 来源追踪
        source_trace = self._build_source_trace(thesis, earnings_impact, expectation_gap)

        # 决策追踪
        decision_trace = self._build_decision_trace(
            research_score, alpha_score, core_score, expectation_gap,
        )
        # 记录暂无本地证据的字段
        if industry_core_score is None:
            decision_trace.append("no_evidence:industry_core_score=暂无本地证据")
        if company_exposure_score is None:
            decision_trace.append("no_evidence:company_exposure_score=暂无本地证据")
        if evidence_confidence is None:
            decision_trace.append("no_evidence:evidence_confidence=暂无本地证据")

        # 状态：任一关键分数不可计算时降级
        status = CandidateStatus.ACTIVE.value
        if (research_score.computability == ScoreComputability.NOT_COMPUTABLE.value
                or alpha_score.computability == ScoreComputability.NOT_COMPUTABLE.value
                or industry_core_breakdown.computability == ScoreComputability.NOT_COMPUTABLE.value
                or company_exposure_breakdown.computability == ScoreComputability.NOT_COMPUTABLE.value):
            status = CandidateStatus.DEGRADED.value

        # P0.6: 研究完整度强制约束
        # RESEARCH_COMPLETE 是唯一允许 ACTIVE 的状态；
        # RESEARCH_INCOMPLETE / OBSERVATIONAL_ONLY 一律降级为 DEGRADED，
        # 避免技术状态或主题热度把不完整研究包装成完整结论。
        normalized_completeness = self._normalize_completeness(
            research_completeness, research_run_id,
        )
        if normalized_completeness != ResearchCompleteness.RESEARCH_COMPLETE.value:
            status = CandidateStatus.DEGRADED.value
        # 追加研究缺口到 decision_trace，便于下游审计
        if research_gaps:
            decision_trace.append(
                f"research_gaps={len(research_gaps)}:{';'.join(research_gaps[:3])}"
            )
        decision_trace.append(f"research_completeness={normalized_completeness}")

        return ResearchCandidate(
            symbol=symbol,
            sector_id=sector_id or thesis.get("sector_id", ""),
            as_of=as_of,
            available_at=now,
            expires_at=_text(thesis.get("valid_until")),
            thesis_id=_text(thesis.get("thesis_id")),
            thesis_status=_text(thesis.get("status")),
            evidence_confidence=ev_conf,
            industry_core_score=industry_core_breakdown,
            company_exposure_score=company_exposure_breakdown,
            earnings_impact=earnings_impact,
            expectation_gap=expectation_gap,
            expectation_gap_confidence=gap_confidence,
            catalyst_score=catalyst_score,
            catalyst_window=catalyst_window,
            valuation_risk=valuation_risk,
            uncertainty_score=uncertainty_score,
            research_score=research_score,
            alpha_score=alpha_score,
            core_score=core_score,
            weakest_assumption=weakest,
            invalidation_conditions=list(invalidation),
            next_review_at=next_review,
            source_trace=source_trace,
            decision_trace=decision_trace,
            status=status,
            research_run_id=research_run_id,
            research_readiness_summary=_as_dict(research_readiness_summary),
            research_gaps=research_gaps,
            research_completeness=normalized_completeness,
        )

    @staticmethod
    def _normalize_completeness(
        value: str, research_run_id: str,
    ) -> str:
        """规范化研究完整度标记。

        无 run 时一律 OBSERVATIONAL_ONLY，禁止凭完整度字符串提升。
        """
        valid = {item.value for item in ResearchCompleteness}
        normalized_value = _text(value)
        normalized = normalized_value if normalized_value in valid else ResearchCompleteness.OBSERVATIONAL_ONLY.value
        if not research_run_id:
            return ResearchCompleteness.OBSERVATIONAL_ONLY.value
        return normalized

    def _compute_research_score(
        self, thesis: dict[str, Any], earnings_impact: dict[str, Any],
        ev_conf: I,
    ) -> ScoreBreakdown:
        """计算研究可信度分数。

        基于：证据置信度、Claim 数量与状态、盈利影响数据完整性。
        """
        components: dict[str, float] = {}
        reasons: list[str] = []

        # 证据置信度（权重 0.40）
        conf_mid = (ev_conf.low + ev_conf.high) / 2
        components["evidence_confidence"] = conf_mid * 0.40

        # Claim 覆盖（权重 0.30）
        required_claims = _text_list(thesis.get("required_claim_ids"))
        claim_count = len(required_claims)
        if claim_count >= 3:
            components["claim_coverage"] = 0.30
        elif claim_count >= 1:
            components["claim_coverage"] = 0.30 * (claim_count / 3)
        else:
            components["claim_coverage"] = 0.0
            reasons.append("no_required_claims")

        # 盈利影响数据完整性（权重 0.30）
        impact_status = earnings_impact.get("status", "")
        if impact_status == "ok":
            components["earnings_completeness"] = 0.30
        elif impact_status == "partial":
            components["earnings_completeness"] = 0.15
            reasons.append("earnings_partial")
        elif impact_status == "degraded":
            components["earnings_completeness"] = 0.10
            reasons.append("earnings_degraded")
        else:
            components["earnings_completeness"] = 0.0
            reasons.append("earnings_missing")

        total = sum(components.values())
        computability = ScoreComputability.COMPUTABLE.value
        if not earnings_impact:
            computability = ScoreComputability.DEGRADED.value
            reasons.append("no_earnings_impact")

        return ScoreBreakdown(
            score=I(max(0, total - 0.05), min(1.0, total + 0.05)),
            components=components,
            computability=computability,
            reason="; ".join(reasons),
        )

    def _compute_alpha_score(
        self, expectation_gap: dict[str, Any], earnings_impact: dict[str, Any],
        catalysts: list[dict[str, Any]], valuation_snapshot: dict[str, Any],
        as_of: str,
    ) -> ScoreBreakdown:
        """计算 Alpha 分数。

        基于：预期差大小、催化剂时间、估值水平。
        如果市场预期数据缺失，必须降级或标记不可计算。
        """
        components: dict[str, float] = {}
        reasons: list[str] = []

        gap_status = expectation_gap.get("status", "")

        # 如果预期数据缺失，alpha_score 不可计算
        if not expectation_gap or gap_status in ("expectation_data_missing", "insufficient_data"):
            components["gap_score"] = 0.0
            components["catalyst_timing"] = 0.0
            components["valuation_level"] = 0.0
            return ScoreBreakdown(
                score=I(0.0, 0.0),
                components=components,
                computability=ScoreComputability.NOT_COMPUTABLE.value,
                reason=f"expectation_data_missing:{gap_status}",
            )

        # 预期差大小（权重 0.50）
        relative_gap = _as_dict(expectation_gap.get("relative_gap"))
        rel_mid = abs((_finite_float(relative_gap.get("low")) + _finite_float(relative_gap.get("high"))) / 2)

        if rel_mid > 0.20:
            components["gap_score"] = 0.50
        elif rel_mid > 0.10:
            components["gap_score"] = 0.35
        elif rel_mid > 0.05:
            components["gap_score"] = 0.20
        else:
            components["gap_score"] = 0.05
            reasons.append("gap_too_small")

        # 催化剂时间（权重 0.25）
        active_catalysts = 0
        for cat in catalysts:
            window_end = cat.get("window_end", "")
            if isinstance(window_end, str) and window_end.strip():
                end = _time(window_end)
                cutoff = _time(as_of) if as_of else datetime.now(timezone.utc)
                if end and cutoff and end >= cutoff:
                    active_catalysts += 1
            elif window_end:
                continue
            else:
                active_catalysts += 0.5  # 无时间限制的催化剂半权重

        if active_catalysts >= 2:
            components["catalyst_timing"] = 0.25
        elif active_catalysts >= 1:
            components["catalyst_timing"] = 0.15
        else:
            components["catalyst_timing"] = 0.0
            reasons.append("no_active_catalyst")

        # 估值水平（权重 0.25）
        percentile = valuation_snapshot.get("historical_percentile", {})
        pe_percentile = _unit_float(percentile.get("pe", 0.5)) if isinstance(percentile, Mapping) else 0.5
        pe_percentile = 0.5 if pe_percentile is None else pe_percentile

        if pe_percentile < 0.3:
            components["valuation_level"] = 0.25
        elif pe_percentile < 0.5:
            components["valuation_level"] = 0.20
        elif pe_percentile < 0.7:
            components["valuation_level"] = 0.10
        else:
            components["valuation_level"] = 0.05
            reasons.append("valuation_already_high")

        total = sum(components.values())
        computability = ScoreComputability.COMPUTABLE.value
        if gap_status == "degraded":
            computability = ScoreComputability.DEGRADED.value
            reasons.append("gap_degraded")

        return ScoreBreakdown(
            score=I(max(0, total - 0.05), min(1.0, total + 0.05)),
            components=components,
            computability=computability,
            reason="; ".join(reasons),
        )

    def _compute_core_score(
        self, industry_core: float | None, company_exposure: float | None,
        earnings_impact: dict[str, Any],
    ) -> ScoreBreakdown:
        """计算产业核心分数。

        基于：产业核心程度、公司收入敞口、受益纯度。
        不得把"产业核心"自动等同于"存在 Alpha"。
        当 industry_core 或 company_exposure 为 None 时，标记为不可计算。
        """
        components: dict[str, float] = {}
        reasons: list[str] = []

        # 产业核心程度（权重 0.40）
        if industry_core is not None and math.isfinite(_finite_float(industry_core, float("nan"))):
            components["industry_core"] = max(0.0, min(_finite_float(industry_core), 1.0)) * 0.40
        else:
            reasons.append("暂无本地证据:industry_core")

        # 公司收入敞口（权重 0.35）
        if company_exposure is not None and math.isfinite(_finite_float(company_exposure, float("nan"))):
            components["company_exposure"] = max(0.0, min(_finite_float(company_exposure), 1.0)) * 0.35
        else:
            reasons.append("暂无本地证据:company_exposure")

        # 受益纯度（权重 0.25）：盈利影响数据是否支持
        impact_status = earnings_impact.get("status", "")
        if impact_status == "ok":
            components["benefit_purity"] = 0.25
        elif impact_status == "partial":
            components["benefit_purity"] = 0.15
        else:
            components["benefit_purity"] = 0.05

        total = sum(components.values())
        computability = ScoreComputability.COMPUTABLE.value
        if reasons:
            computability = ScoreComputability.NOT_COMPUTABLE.value
        return ScoreBreakdown(
            score=I(max(0, total - 0.05), min(1.0, total + 0.05)),
            components=components,
            computability=computability,
            reason="; ".join(reasons),
        )

    def _compute_catalyst_score(
        self, catalysts: list[dict[str, Any]], as_of: str,
    ) -> ScoreBreakdown:
        """计算催化剂评分。"""
        if not catalysts:
            return ScoreBreakdown(
                score=I(0.0, 0.0),
                components={"catalyst_count": 0.0},
                computability=ScoreComputability.DEGRADED.value,
                reason="no_catalysts",
            )

        active = 0
        expired = 0
        for cat in catalysts:
            window_end = cat.get("window_end", "")
            if isinstance(window_end, str) and window_end.strip():
                end = _time(window_end)
                cutoff = _time(as_of) if as_of else datetime.now(timezone.utc)
                if end and cutoff and end >= cutoff:
                    active += 1
                else:
                    expired += 1
            elif window_end:
                expired += 1
            else:
                active += 1

        score = min(1.0, active * 0.3 + (len(catalysts) - expired) * 0.1)
        return ScoreBreakdown(
            score=I(max(0, score - 0.05), min(1.0, score + 0.05)),
            components={"active": active, "expired": expired, "total": len(catalysts)},
            computability=ScoreComputability.COMPUTABLE.value if active > 0 else ScoreComputability.DEGRADED.value,
            reason=f"active={active},expired={expired}" if expired > 0 else "",
        )

    def _compute_valuation_risk(self, snapshot: dict[str, Any]) -> I:
        """计算估值风险区间。"""
        snapshot = _as_dict(snapshot)
        percentile = snapshot.get("historical_percentile", {})
        pe_pct = _finite_float(percentile.get("pe", 0.5), 0.5) if isinstance(percentile, dict) else 0.5
        pe_pct = max(0.0, min(1.0, pe_pct))
        # 估值分位越高，风险越大
        return I(max(0, pe_pct - 0.1), min(1.0, pe_pct + 0.1))

    def _compute_uncertainty(
        self, research: ScoreBreakdown, alpha: ScoreBreakdown,
        earnings_impact: dict[str, Any],
    ) -> I:
        """计算不确定性分数。"""
        # 不确定性 = 1 - 研究置信度
        research_mid = (research.score.low + research.score.high) / 2
        alpha_mid = (alpha.score.low + alpha.score.high) / 2

        # 如果 alpha 不可计算，不确定性增加
        if alpha.computability == ScoreComputability.NOT_COMPUTABLE.value:
            return I(0.6, 0.9)
        elif alpha.computability == ScoreComputability.DEGRADED.value:
            return I(0.4, 0.7)

        uncertainty = 1.0 - min(research_mid, alpha_mid)
        return I(max(0, uncertainty - 0.1), min(1.0, uncertainty + 0.1))

    def _extract_catalyst_window(self, catalysts: list[dict[str, Any]]) -> str:
        """提取催化剂窗口描述。"""
        if not catalysts:
            return ""
        starts = [(_time(c["window_start"]), c["window_start"].strip()) for c in catalysts
                  if isinstance(c.get("window_start"), str) and _time(c["window_start"])]
        ends = [(_time(c["window_end"]), c["window_end"].strip()) for c in catalysts
                if isinstance(c.get("window_end"), str) and _time(c["window_end"])]
        if starts and ends:
            return f"{min(starts, key=lambda item: item[0])[1]} ~ {max(ends, key=lambda item: item[0])[1]}"
        if ends:
            return f"until {max(ends, key=lambda item: item[0])[1]}"
        return "open"

    def _build_source_trace(
        self, thesis: dict[str, Any], earnings_impact: dict[str, Any],
        expectation_gap: dict[str, Any],
    ) -> list[str]:
        """构建来源追踪。"""
        trace: list[str] = []
        thesis_id = _text(thesis.get("thesis_id"))
        if thesis_id:
            trace.append(f"thesis:{thesis_id}")
        trace.extend(f"claim:{cid}" for cid in _text_list(thesis.get("required_claim_ids")))
        impact_id = _text(earnings_impact.get("impact_id"))
        if impact_id:
            trace.append(f"earnings:{impact_id}")
        gap_id = _text(expectation_gap.get("gap_id"))
        if gap_id:
            trace.append(f"gap:{gap_id}")
        return trace

    def _build_decision_trace(
        self, research: ScoreBreakdown, alpha: ScoreBreakdown,
        core: ScoreBreakdown, expectation_gap: dict[str, Any],
    ) -> list[str]:
        """构建决策追踪。"""
        trace: list[str] = []
        trace.append(f"research_score={research.score.mid:.3f}({research.computability})")
        trace.append(f"alpha_score={alpha.score.mid:.3f}({alpha.computability})")
        trace.append(f"core_score={core.score.mid:.3f}({core.computability})")
        gap_status = _text(expectation_gap.get("status"), "missing")
        trace.append(f"gap_status={gap_status}")
        if research.reason:
            trace.append(f"research_reason={research.reason}")
        if alpha.reason:
            trace.append(f"alpha_reason={alpha.reason}")
        return trace


class CandidateReportBuilder:
    """生成人类可读研究报告。

    所有关键结论可定位到 Claim、Evidence 或计算公式。
    """

    def build(self, candidate: ResearchCandidate) -> CandidateReport:
        """生成研究报告。"""
        sections: dict[str, str] = {}
        key_claims: list[dict[str, str]] = []
        evidence_refs: list[dict[str, str]] = []
        formula_refs: list[dict[str, str]] = []

        # 为什么进入候选
        sections["entry_reason"] = self._entry_reason(candidate)

        # 产业链位置
        sections["industry_position"] = self._industry_position(candidate)

        # 关键事实
        sections["key_facts"] = self._key_facts(candidate)

        # 盈利影响区间
        sections["earnings_impact"] = self._earnings_impact_section(candidate)

        # 市场预期
        sections["market_expectation"] = self._market_expectation_section(candidate)

        # 预期差
        sections["expectation_gap"] = self._expectation_gap_section(candidate)

        # 催化剂
        sections["catalysts"] = self._catalyst_section(candidate)

        # 最弱假设
        sections["weakest_assumption"] = _text(candidate.weakest_assumption) or "暂无识别"

        # 反证
        sections["contradictions"] = self._contradiction_section(candidate)

        # 失效条件
        sections["invalidation"] = "\n".join(
            f"- {item}" for item in _text_list(candidate.invalidation_conditions)
        ) or "暂无"

        # 下一步验证
        sections["next_validation"] = self._next_validation(candidate)

        # 三类分数
        sections["scores"] = self._score_section(candidate)

        # 来源追踪
        for trace in _text_list(candidate.source_trace):
            if trace.startswith("claim:"):
                key_claims.append({"claim_id": trace.split(":", 1)[1], "source": "thesis"})
            elif trace.startswith("earnings:"):
                evidence_refs.append({
                    "evidence_id": trace.split(":", 1)[1],
                    "source": "earnings_impact",
                })
            elif trace.startswith("gap:"):
                evidence_refs.append({
                    "evidence_id": trace.split(":", 1)[1],
                    "source": "expectation_gap",
                })

        # 公式引用
        formula_refs.append({
            "formula": "research_score = evidence_confidence*0.40 + claim_coverage*0.30 + earnings_completeness*0.30",
            "section": "scores",
        })
        formula_refs.append({
            "formula": "alpha_score = gap_score*0.50 + catalyst_timing*0.25 + valuation_level*0.25",
            "section": "scores",
        })
        formula_refs.append({
            "formula": "core_score = industry_core*0.40 + company_exposure*0.35 + benefit_purity*0.25",
            "section": "scores",
        })

        return CandidateReport(
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            report_sections=sections,
            key_claims=key_claims,
            evidence_references=evidence_refs,
            formula_references=formula_refs,
        )

    def _entry_reason(self, c: ResearchCandidate) -> str:
        """为什么进入候选。"""
        parts: list[str] = []
        thesis_status = _text(c.thesis_status)
        if thesis_status:
            parts.append(f"观点状态: {thesis_status}")
        if _score_mid(c.core_score) > 0.5:
            parts.append("产业核心程度较高")
        if (_computability(c.alpha_score) == ScoreComputability.COMPUTABLE.value
                and _score_mid(c.alpha_score) > 0.3):
            parts.append("存在预期差")
        if _score_mid(c.catalyst_score) > 0.3:
            parts.append("有催化剂在窗口内")
        return "; ".join(parts) if parts else "暂无明确入选理由"

    def _industry_position(self, c: ResearchCandidate) -> str:
        """产业链位置。"""
        sector_id = _text(c.sector_id)
        if sector_id:
            return f"板块: {sector_id}"
        return "板块未指定"

    def _key_facts(self, c: ResearchCandidate) -> str:
        """关键事实。"""
        impact = _as_dict(c.earnings_impact)
        if not impact:
            return "暂无盈利影响数据"
        parts = [
            f"产品: {_text(impact.get('product'), '未知')}",
            f"期间: {_text(impact.get('period'), '未知')}",
            f"状态: {_text(impact.get('status'), '未知')}",
        ]
        return "; ".join(parts)

    def _earnings_impact_section(self, c: ResearchCandidate) -> str:
        """盈利影响区间。"""
        impact = _as_dict(c.earnings_impact)
        if not impact:
            return "暂无盈利影响数据"
        eps = _format_interval(impact.get("eps_impact"))
        revenue = _format_interval(impact.get("incremental_revenue"), scale=1e8, decimals=2)
        profit = _format_interval(impact.get("incremental_net_profit"), scale=1e8, decimals=2)
        parts = []
        if eps:
            parts.append(f"EPS 影响: {eps}")
        if revenue:
            parts.append(f"新增收入: {revenue} 亿元")
        if profit:
            parts.append(f"新增净利润: {profit} 亿元")
        return "; ".join(parts) if parts else "暂无区间数据"

    def _market_expectation_section(self, c: ResearchCandidate) -> str:
        """市场预期。"""
        gap = _as_dict(c.expectation_gap)
        if not gap:
            return "暂无市场预期数据"
        source = _text(gap.get("expectation_source"), "未知")
        status = _text(gap.get("status"), "未知")
        return f"来源: {source}; 状态: {status}"

    def _expectation_gap_section(self, c: ResearchCandidate) -> str:
        """预期差。"""
        gap = _as_dict(c.expectation_gap)
        if not gap:
            return "暂无预期差数据"
        g = _format_interval(gap.get("gap"))
        rg = _format_interval(gap.get("relative_gap"))
        status = _text(gap.get("status"), "未知")
        if status != "ok":
            return f"状态: {status}; 缺失原因: {gap.get('gaps', [])}"
        if not g or not rg:
            return "状态: ok; 缺失原因: 区间数据缺失"
        confidence = c.expectation_gap_confidence
        confidence_text = (
            f"[{confidence.low:.2f}, {confidence.high:.2f}]"
            if isinstance(confidence, I) else "暂无"
        )
        parts = [f"绝对差: {g}", f"相对差: {rg}", f"置信度: {confidence_text}"]
        return "; ".join(parts)

    def _catalyst_section(self, c: ResearchCandidate) -> str:
        """催化剂。"""
        if _computability(c.catalyst_score) == ScoreComputability.DEGRADED.value:
            return "暂无有效催化剂"
        return f"评分: {_score_mid(c.catalyst_score):.3f}; 窗口: {_text(c.catalyst_window)}"

    def _contradiction_section(self, c: ResearchCandidate) -> str:
        """反证。"""
        impact = _as_dict(c.earnings_impact)
        if not impact:
            return "暂无"
        contradictions = _text_list(impact.get("falsification_conditions"))
        return "\n".join(f"- {condition}" for condition in contradictions) or "暂无"

    def _next_validation(self, c: ResearchCandidate) -> str:
        """下一步应验证什么。"""
        weakest = _text(c.weakest_assumption)
        if weakest:
            return f"优先验证最弱假设: {weakest}"
        return "暂无明确验证方向"

    def _score_section(self, c: ResearchCandidate) -> str:
        """三类分数。"""
        lines = [
            f"research_score: {_score_mid(c.research_score):.3f} ({_computability(c.research_score)})",
            f"alpha_score: {_score_mid(c.alpha_score):.3f} ({_computability(c.alpha_score)})",
            f"core_score: {_score_mid(c.core_score):.3f} ({_computability(c.core_score)})",
        ]
        research_reason = _score_reason(c.research_score)
        alpha_reason = _score_reason(c.alpha_score)
        if research_reason:
            lines.append(f"  research_reason: {research_reason}")
        if alpha_reason:
            lines.append(f"  alpha_reason: {alpha_reason}")
        return "\n".join(lines)
