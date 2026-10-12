"""自动 ResearchCandidate 发布器。

从版本化上游对象（InvestmentThesis、EarningsImpact、ExpectationGap）
自动构建 ResearchCandidate，不接受裸分数。

核心原则：
1. industry_core_score / company_exposure_score 必须从版本化上游对象推导
2. 上游对象必须存在且非空，否则跳过该 symbol
3. 所有分数的计算过程必须可溯源到 Claim、Evidence 和模型版本
4. 不输出买卖、仓位、止盈止损建议
5. P0.6: 只读接入 CompanyResearchService，不反向修改事实/Evidence/readiness；
   没有正式 run 时仍可创建观察候选，但必须标记 research_incomplete / observational_only。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from math import isfinite
from typing import Any

from core.research.candidate.contracts import ResearchCompleteness
from core.research.candidate.engine import CandidateBuilder
from core.research.candidate.repository import CandidateRepository
from core.research.earnings.repository import EarningsRepository
from core.research.expectations.contracts import InvestmentThesis

logger = logging.getLogger(__name__)


# 八维 readiness 阻断状态：出现任一即视为研究不完整
_BLOCKING_READINESS_STATUSES = frozenset({"blocked", "not_computable"})


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if isfinite(parsed) else default


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _claim_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return _text_list(value)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class ResearchCandidatePublisher:
    """自动从版本化上游对象发布 ResearchCandidate。

    只从持久化的 Thesis、EarningsImpact、ExpectationGap 读取数据，
    不接受 API 调用方传入的裸分数（industry_core_score 等）。
    P0.6: 通过可选 research_service 只读接入正式研究档案。
    """

    # Thesis 状态白名单：只有这些状态的 Thesis 才会触发候选发布
    PUBLISHABLE_THESIS_STATUS = frozenset({
        "active", "high_conviction",
    })

    def __init__(
        self,
        expectations_repo: Any,
        earnings_repo: EarningsRepository,
        candidate_repo: CandidateRepository,
        builder: CandidateBuilder | None = None,
        claim_repo: Any = None,
        research_service: Any = None,
    ) -> None:
        """初始化发布器。

        Args:
            expectations_repo: ExpectationRepository，用于读取 Thesis 和 ExpectationGap。
            earnings_repo: EarningsRepository，用于读取 EarningsImpact。
            candidate_repo: CandidateRepository，用于持久化发布的候选。
            builder: 可选的 CandidateBuilder，为 None 时使用默认实例。
            claim_repo: 可选的 ResearchRepository，用于读取 Claim 追溯字段。
                为 None 时新字段保留默认值（降级处理）。
            research_service: 可选的 CompanyResearchService，用于只读读取正式研究 run。
                必须只调用 get_latest 之类只读方法；禁止调用 create_run。
        """
        self.expectations_repo = expectations_repo
        self.earnings_repo = earnings_repo
        self.candidate_repo = candidate_repo
        self.builder = builder or CandidateBuilder()
        self.claim_repo = claim_repo
        self.research_service = research_service

    def publish_for_symbol(self, symbol: str, *, as_of: str = "") -> list[dict[str, Any]]:
        """为指定 symbol 自动构建并发布 ResearchCandidate。

        从版本化上游对象读取数据，不接受裸分数。

        Args:
            symbol: 公司代码。
            as_of: PIT 时点，为空时使用最新数据。

        Returns:
            发布的候选字典列表（可能为空）。
        """
        # 1. 读取该 symbol 的所有 Thesis
        if not isinstance(symbol, str) or not symbol.strip():
            return []
        symbol = symbol.strip()
        safe_as_of = _text(as_of)
        theses_raw = self.expectations_repo.list(
            "investment_theses", index_value=symbol, limit=100,
        )
        if not theses_raw:
            logger.debug("symbol=%s 无 Thesis，跳过", symbol)
            return []

        published: list[dict[str, Any]] = []
        for thesis_dict in theses_raw if isinstance(theses_raw, (list, tuple)) else []:
            if not isinstance(thesis_dict, dict):
                continue
            try:
                thesis = InvestmentThesis.from_dict(thesis_dict)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                logger.debug("跳过损坏 Thesis symbol=%s: %s", symbol, exc)
                continue
            # 只发布活跃状态的 Thesis
            if thesis.status not in self.PUBLISHABLE_THESIS_STATUS:
                continue

            # 2. 读取关联的 EarningsImpact
            earnings_impact = self._load_earnings_impact(symbol)

            # 3. 读取关联的 ExpectationGap
            expectation_gap = self._load_expectation_gap(symbol)

            # 4. 从版本化上游对象推导分数（不接受裸分数）
            # 无结构化证据时返回 None，不推测
            industry_core_score = self._derive_industry_core_score(thesis)
            company_exposure_score = self._derive_company_exposure_score(
                thesis, earnings_impact,
            )
            evidence_confidence = self._derive_evidence_confidence(thesis)

            # 5. 构建候选（None 分数由 builder 标记为 NOT_COMPUTABLE + 暂无本地证据）
            # P0.6: 只读读取正式研究 run；不存在时标记为 observational_only
            research_bundle = self._load_research_run(symbol)
            run_id = _text(research_bundle.get("run_id")) if isinstance(research_bundle, Mapping) else ""
            readiness_summary = _as_dict(research_bundle.get("readiness_summary")) if isinstance(research_bundle, Mapping) else {}
            research_gaps = _text_list(research_bundle.get("gaps")) if isinstance(research_bundle, Mapping) else []
            research_completeness = self._derive_completeness(
                run_id, readiness_summary,
            )

            candidate = self.builder.build(
                thesis=thesis.to_dict(),
                earnings_impact=earnings_impact,
                expectation_gap=expectation_gap,
                sector_id=thesis.sector_id,
                as_of=safe_as_of or thesis.valid_from,
                industry_core_score=industry_core_score,
                company_exposure_score=company_exposure_score,
                evidence_confidence=evidence_confidence,
                catalysts=[c.to_dict() if hasattr(c, "to_dict") else c
                           for c in (thesis.catalysts if isinstance(thesis.catalysts, (list, tuple)) else [])
                           if isinstance(c, Mapping) or hasattr(c, "to_dict")],
                research_run_id=run_id,
                research_readiness_summary=readiness_summary,
                research_gaps=research_gaps,
                research_completeness=research_completeness,
            )

            # 6. 标记来源追踪
            candidate.source_trace = self._build_source_trace(
                thesis, earnings_impact, expectation_gap,
            )
            if run_id:
                candidate.source_trace.append(f"research_run:{run_id}")

            # 7. 从 Claim 复制追溯字段（claim_repo 不可用时降级为默认值）
            self._attach_claim_trace(candidate, thesis)

            # 8. 持久化
            self.candidate_repo.save_candidate(candidate)
            published.append(candidate.to_dict())

        if not published:
            logger.debug("symbol=%s 无活跃 Thesis，跳过", symbol)
        return published

    def publish_batch(self, symbols: list[str], *, as_of: str = "") -> dict[str, Any]:
        """批量发布多个 symbol 的候选。

        Args:
            symbols: 公司代码列表。
            as_of: PIT 时点。

        Returns:
            {published: int, skipped: int, errors: list, candidates: list}
        """
        published_count = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        all_candidates: list[dict[str, Any]] = []
        safe_symbols = symbols if isinstance(symbols, (list, tuple)) else []
        for symbol in safe_symbols:
            try:
                result = self.publish_for_symbol(symbol, as_of=as_of)
                if result:
                    published_count += len(result)
                    all_candidates.extend(result)
                else:
                    skipped += 1
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
                logger.warning("发布 symbol=%s 候选失败: %s", symbol, exc)
        return {
            "published": published_count,
            "skipped": skipped,
            "errors": errors,
            "candidates": all_candidates,
        }

    def _load_earnings_impact(self, symbol: str) -> dict[str, Any]:
        """从 EarningsRepository 读取该 symbol 最新的 EarningsImpact。

        EarningsImpact.status 的合法值为 ok/insufficient_data/degraded/partial，
        不存在 "verified" 状态；这里筛选 status=ok 的已完成结果。

        Args:
            symbol: 公司代码。

        Returns:
            EarningsImpact 字典，无数据时返回空字典。
        """
        try:
            impacts = self.earnings_repo.list_impacts(status="ok", limit=100)
            if not isinstance(impacts, (list, tuple)):
                return {}
            for impact in impacts:
                if isinstance(impact, Mapping):
                    impact_symbol = _text(impact.get("symbol"))
                    payload = dict(impact)
                else:
                    impact_symbol = _text(getattr(impact, "symbol", ""))
                    converter = getattr(impact, "to_dict", None)
                    if not callable(converter):
                        continue
                    try:
                        payload = _as_dict(converter())
                    except (TypeError, ValueError, AttributeError):
                        continue
                if impact_symbol == symbol and payload:
                    return payload
        except Exception as exc:
            logger.debug("读取 EarningsImpact 失败 symbol=%s: %s", symbol, exc)
        return {}

    def _attach_claim_trace(self, candidate: Any, thesis: InvestmentThesis) -> None:
        """从 Thesis 关联的 Claim 复制追溯字段到候选。

        使用 thesis.required_claim_ids 的第一个 ID 作为主 Claim。
        claim_repo 不可用或 Claim 不存在时，新字段保留默认值（降级处理）。

        Args:
            candidate: 已构建的 ResearchCandidate，将被原地修改。
            thesis: 投资观点，用于定位关联的 Claim。
        """
        if self.claim_repo is None:
            return
        required_claim_ids = _claim_ids(getattr(thesis, "required_claim_ids", []))
        if not required_claim_ids:
            return
        primary_claim_id = required_claim_ids[0]
        try:
            claim = self.claim_repo.get_claim(primary_claim_id)
        except Exception as exc:
            logger.debug(
                "读取 Claim 失败 candidate=%s claim_id=%s: %s",
                getattr(candidate, "candidate_id", ""), primary_claim_id, exc,
            )
            return
        if claim is None:
            return
        data = _as_dict(claim)
        get = data.get if data else lambda key, default=None: getattr(claim, key, default)
        candidate.claim_id = _text(get("claim_id"))
        candidate.claim_status = _text(get("research_status"))
        candidate.mapping_confidence = max(0.0, min(1.0, _safe_float(get("mapping_confidence"))))
        candidate.company_id = _text(get("subject_id"))
        candidate.evidence_as_of = _text(get("valid_from")) or _text(get("created_at"))

    def _load_expectation_gap(self, symbol: str) -> dict[str, Any]:
        """从 ExpectationRepository 读取该 symbol 最新的 ExpectationGap。

        Args:
            symbol: 公司代码。

        Returns:
            ExpectationGap 字典，无数据时返回空字典。
        """
        try:
            gaps = self.expectations_repo.list(
                "expectation_gaps", index_value=symbol, limit=10,
            )
            if isinstance(gaps, (list, tuple)):
                for gap in gaps:
                    if isinstance(gap, dict):
                        return gap
        except Exception as exc:
            logger.debug("读取 ExpectationGap 失败 symbol=%s: %s", symbol, exc)
        return {}

    def _derive_industry_core_score(self, thesis: InvestmentThesis) -> float | None:
        """从 Thesis 推导产业核心程度评分。

        只从结构化业务证据推导，不使用假设数量等启发式。
        没有结构化证据时返回 None，表示未知。

        Args:
            thesis: 投资观点。

        Returns:
            产业核心程度评分 (0-1)，或 None 表示暂无本地证据。
        """
        # 只从 Thesis 的结构化 industry_core_score 字段读取
        # 该字段应由产业链分析模块基于结构化业务数据填入
        raw_attributes = getattr(thesis, "attributes", {})
        attributes = raw_attributes if isinstance(raw_attributes, Mapping) else {}
        structured_score = attributes.get("industry_core_score")
        if not isinstance(structured_score, bool):
            parsed = _safe_float(structured_score, float("nan"))
            if isfinite(parsed):
                return round(max(0.0, min(1.0, parsed)), 4)
        # 无结构化证据时返回 None，不推测
        return None

    def _derive_company_exposure_score(
        self, thesis: InvestmentThesis, earnings_impact: dict[str, Any],
    ) -> float | None:
        """从 EarningsImpact 推导公司收入敞口评分。

        只从 EarningsImpact 中的结构化 revenue_exposure 字段读取，
        不混入 Claim 数量等启发式。没有结构化证据时返回 None。

        Args:
            thesis: 投资观点。
            earnings_impact: 盈利影响字典。

        Returns:
            公司收入敞口评分 (0-1)，或 None 表示暂无本地证据。
        """
        # 从 EarningsImpact 读取结构化收入敞口
        revenue_exposure = _as_dict(earnings_impact).get("revenue_exposure", {})
        if isinstance(revenue_exposure, Mapping):
            exposure_pct = _safe_float(revenue_exposure.get("percentage"))
            if exposure_pct > 0:
                return round(max(0.0, min(1.0, exposure_pct / 0.5)), 4)
        # 无结构化收入数据时返回 None，不推测
        return None

    def _derive_evidence_confidence(self, thesis: InvestmentThesis) -> Any:
        """从 Thesis 推导证据置信度区间。

        只从 Thesis 的结构化 confidence_interval 字段读取。
        没有结构化证据时返回 None，表示未知。

        Args:
            thesis: 投资观点。

        Returns:
            Interval 证据置信度区间，或 None 表示暂无本地证据。
        """
        from core.research.earnings.interval import Interval
        # 优先从 Thesis 的结构化字段读取
        raw_attributes = getattr(thesis, "attributes", {})
        attributes = raw_attributes if isinstance(raw_attributes, Mapping) else {}
        structured_confidence = attributes.get("evidence_confidence")
        if isinstance(structured_confidence, Mapping):
            low = _safe_float(structured_confidence.get("low"), float("nan"))
            high = _safe_float(structured_confidence.get("high"), float("nan"))
            if not all(isfinite(value) for value in (low, high)):
                return None
            if low > 0 or high > 0:
                low = max(0.0, min(1.0, low))
                high = max(0.0, min(1.0, high))
                return Interval(round(low, 4), round(high, 4))
        # 无结构化置信度数据时返回 None，不按证据条数推测
        return None

    def _build_source_trace(
        self,
        thesis: InvestmentThesis,
        earnings_impact: dict[str, Any],
        expectation_gap: dict[str, Any],
    ) -> list[str]:
        """构建来源追踪链，记录所有版本化上游对象 ID。

        Args:
            thesis: 投资观点。
            earnings_impact: 盈利影响字典。
            expectation_gap: 预期差字典。

        Returns:
            来源追踪 ID 列表。
        """
        trace: list[str] = []
        thesis_id = _text(getattr(thesis, "thesis_id", ""))
        if thesis_id:
            trace.append(f"thesis:{thesis_id}@{_text(getattr(thesis, 'status', ''))}")
        impact = _as_dict(earnings_impact)
        impact_id = _text(impact.get("impact_id"))
        if impact_id:
            version = _text(impact.get("schema_version"), "v1")
            trace.append(f"earnings_impact:{impact_id}@{version}")
        gap = _as_dict(expectation_gap)
        gap_id = _text(gap.get("gap_id"))
        if gap_id:
            trace.append(f"expectation_gap:{gap_id}")
        return trace

    def _load_research_run(self, symbol: str) -> dict[str, Any]:
        """只读读取公司最新研究 run；不存在或失败时降级。

        禁止调用 create_run 等写入方法。返回结构：
        {run_id, readiness_summary, gaps}
        - readiness_summary: 八维 status 精简快照（不复制事实/证据状态）
        - gaps: 去重缺口列表，来自 degradation_reasons + 各维度 missing_items

        Args:
            symbol: 公司代码。

        Returns:
            读取到的研究 run 摘要；为空表示未绑定正式 run。
        """
        if self.research_service is None:
            return {"run_id": "", "readiness_summary": {}, "gaps": []}
        try:
            latest = self.research_service.get_latest(symbol)
        except Exception as exc:
            logger.debug(
                "读取正式研究 run 失败 symbol=%s: %s", symbol, exc,
            )
            return {"run_id": "", "readiness_summary": {}, "gaps": []}
        if not isinstance(latest, Mapping) or latest.get("degraded") or latest.get("run") is None:
            # 没有正式 run：返回空，由 _derive_completeness 判定为 observational_only
            return {"run_id": "", "readiness_summary": {}, "gaps": []}
        run_record = latest.get("run") if isinstance(latest.get("run"), Mapping) else {}
        run_id = _text(run_record.get("run_id"))
        dossier = latest.get("dossier") if isinstance(latest.get("dossier"), Mapping) else {}
        readiness = dossier.get("research_readiness") if isinstance(dossier.get("research_readiness"), Mapping) else {}
        acceptance = latest.get("acceptance") if isinstance(latest.get("acceptance"), Mapping) else {}
        degradation_reasons = _text_list(acceptance.get("degradation_reasons"))
        return {
            "run_id": run_id,
            "readiness_summary": self._summarize_readiness(readiness),
            "gaps": self._collect_research_gaps(
                readiness, degradation_reasons,
            ),
        }

    @staticmethod
    def _summarize_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
        """把八维 readiness 压缩成 status-only 精简快照。

        不复制 evidence_refs/missing_items 全集，避免反向传播事实状态。
        score 保留以便下游快速判断；score=None 透传为不可计算。
        """
        summary: dict[str, Any] = {}
        for name, raw in readiness.items() if isinstance(readiness, Mapping) else []:
            if not isinstance(raw, Mapping):
                continue
            dimension = _text(name)
            if not dimension:
                continue
            summary[dimension] = {
                "status": _text(raw.get("status"), "not_computable"),
                "score": raw.get("score"),
                "missing_count": len(raw.get("missing_items") or []) if isinstance(raw.get("missing_items"), (list, tuple)) else 0,
            }
        return summary

    @staticmethod
    def _collect_research_gaps(
        readiness: dict[str, Any], degradation_reasons: list[str],
    ) -> list[str]:
        """汇总缺口：阻断维度 + 显式 degradation_reasons。

        去重保留顺序；只保留 dimension:status 形式或 degradation 字符串。
        """
        gaps: list[str] = []
        seen: set[str] = set()
        for reason in _text_list(degradation_reasons):
            if reason not in seen:
                seen.add(reason)
                gaps.append(reason)
        for name, raw in readiness.items() if isinstance(readiness, Mapping) else []:
            if not isinstance(raw, Mapping):
                continue
            dimension = _text(name)
            status = _text(raw.get("status"))
            if status in _BLOCKING_READINESS_STATUSES:
                key = f"{dimension}:{status}"
                if key not in seen:
                    seen.add(key)
                    gaps.append(key)
        return gaps

    @staticmethod
    def _derive_completeness(
        run_id: str, readiness_summary: dict[str, Any],
    ) -> str:
        """根据 run 与 readiness 判定研究完整度。

        规则：
        - 无 run_id → OBSERVATIONAL_ONLY。
        - 任一维度 status ∈ {blocked, not_computable} → RESEARCH_INCOMPLETE。
        - 否则 → RESEARCH_COMPLETE。

        Args:
            run_id: 正式研究 run ID（空串表示未绑定）。
            readiness_summary: 八维 readiness 精简快照。

        Returns:
            ResearchCompleteness 枚举值。
        """
        if not _text(run_id):
            return ResearchCompleteness.OBSERVATIONAL_ONLY.value
        for summary in readiness_summary.values() if isinstance(readiness_summary, Mapping) else []:
            if not isinstance(summary, Mapping):
                continue
            status = _text(summary.get("status"))
            if status in _BLOCKING_READINESS_STATUSES:
                return ResearchCompleteness.RESEARCH_INCOMPLETE.value
        return ResearchCompleteness.RESEARCH_COMPLETE.value
