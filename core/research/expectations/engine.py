"""PIT-safe expectation gap calculation, implied expectation reverse-engineering,
and full thesis lifecycle state machine.

扩展版本：
- ExpectationGapEngine: 增加 relative_gap / gap_confidence / catalyst_window / valuation_level
- ImpliedExpectationEngine: 从估值数据反推市场隐含预期，标记为 implied
- ThesisLifecycle: 完整状态机，支持 8 种状态和自动迁移
- P1.7 ConsensusSummaryEngine: 聚合一致预期（中位数/分位数/来源数/分歧度/修正方向），
  支持分机构、分发布日期、分预测期间版本序列；来源不足时不可计算
"""
from __future__ import annotations

import statistics
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from typing import Iterable

from core.research.earnings.interval import Interval

from .contracts import (
    AquatradeForecast,
    Catalyst,
    ConsensusEstimate,
    ConsensusSummary,
    ExpectationGap,
    ExpectationSource,
    ExpectationValue,
    GapStatus,
    ImpliedExpectation,
    ImpliedExpectationInput,
    InvestmentThesis,
    MarketExpectation,
    ReviewTask,
    ThesisAudit,
    ThesisStatus,
    utc_now,
)


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def _time(value: str) -> datetime | None:
    """解析 ISO 时间字符串，失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def visible_as_of(value: ExpectationValue, as_of: str, historical: bool = True) -> bool:
    """PIT 可见性检查：published_at ≤ cutoff 且 retrieved_at ≤ cutoff。"""
    if not historical:
        return True
    if not isinstance(value, ExpectationValue):
        return False
    cutoff = _time(as_of)
    published, retrieved = _time(value.published_at), _time(value.retrieved_at)
    if cutoff is None or published is None or retrieved is None:
        return False
    return published <= cutoff and retrieved <= cutoff


def latest_visible_consensus(
    revisions: Iterable[ConsensusEstimate], *, metric: str, period: str, as_of: str,
) -> tuple[ConsensusEstimate, ExpectationValue] | None:
    """从修订序列中选取 as_of 时点最新可见的一致预期。"""
    candidates: list[tuple[datetime, ConsensusEstimate, ExpectationValue]] = []
    for revision in revisions:
        if not isinstance(revision, ConsensusEstimate):
            continue
        values = revision.values if isinstance(revision.values, (list, tuple)) else []
        for value in values:
            if not isinstance(value, ExpectationValue):
                continue
            if value.metric == metric and value.period == period and visible_as_of(value, as_of, True):
                candidates.append((_time(value.retrieved_at) or datetime.min, revision, value))
    if not candidates:
        return None
    _, revision, value = max(candidates, key=lambda row: row[0])
    return revision, value


# ---------------------------------------------------------------------------
# 隐含预期反推引擎
# ---------------------------------------------------------------------------

class ImpliedExpectationEngine:
    """从当前估值反推市场隐含预期。

    反推结果必须标记为 implied，不得标记为 consensus。
    如果输入数据不完整，返回 None 而非伪造。
    """

    def reverse_engineer(
        self, inp: ImpliedExpectationInput, *, forecast_period: str = "",
    ) -> MarketExpectation | None:
        """从估值数据反推隐含预期。

        Args:
            inp: 市场数据输入（价格、股本、PE 等）
            forecast_period: 预测期间

        Returns:
            MarketExpectation 或 None（输入不完整时）
        """
        if not isinstance(inp, ImpliedExpectationInput):
            return None
        numeric_fields = (
            "current_price", "shares_outstanding", "trailing_EPS",
            "pe_ratio", "pb_ratio", "growth_rate_assumed", "margin_assumed",
        )
        parsed: dict[str, float] = {}
        try:
            for name in numeric_fields:
                value = _finite_float(getattr(inp, name))
                if value is None:
                    return None
                parsed[name] = value
        except AttributeError:
            return None

        current_price = parsed["current_price"]
        shares_outstanding = parsed["shares_outstanding"]
        pe_ratio = parsed["pe_ratio"]
        growth_rate_assumed = parsed["growth_rate_assumed"]
        margin_assumed = parsed["margin_assumed"]

        missing: list[str] = []
        if current_price <= 0:
            missing.append("current_price")
        if shares_outstanding <= 0:
            missing.append("shares_outstanding")
        if pe_ratio <= 0:
            missing.append("pe_ratio")
        if not inp.symbol:
            missing.append("symbol")
        if not inp.as_of:
            missing.append("as_of")
        if missing:
            return None

        # 隐含 EPS = 当前股价 / PE
        implied_eps = current_price / pe_ratio

        # 隐含净利润 = 隐含 EPS × 总股本
        implied_net_profit = implied_eps * shares_outstanding

        # 隐含收入：如果有 margin_assumed，则反推；否则设为 [0, 0]
        if margin_assumed > 0:
            implied_revenue = implied_net_profit / margin_assumed
        else:
            implied_revenue = 0.0

        # 隐含估值倍数 = PE
        implied_pe = pe_ratio

        # 区间：使用 growth_rate_assumed 生成不确定性区间
        growth = growth_rate_assumed
        if growth > 0:
            eps_low = implied_eps * (1 - growth * 0.3)
            eps_high = implied_eps * (1 + growth * 0.3)
            profit_low = implied_net_profit * (1 - growth * 0.3)
            profit_high = implied_net_profit * (1 + growth * 0.3)
        else:
            eps_low = eps_high = implied_eps
            profit_low = profit_high = implied_net_profit

        return MarketExpectation(
            symbol=inp.symbol,
            expected_revenue=Interval(implied_revenue * 0.9, implied_revenue * 1.1) if implied_revenue > 0 else Interval(0, 0),
            expected_net_profit=Interval(profit_low, profit_high),
            expected_EPS=Interval(eps_low, eps_high),
            valuation_multiple=Interval(implied_pe * 0.9, implied_pe * 1.1),
            expectation_source=ExpectationSource.IMPLIED.value,
            source_count=1,
            available_at=inp.as_of,
            forecast_period=forecast_period,
            confidence=Interval(0.3, 0.5),  # 隐含预期置信度较低
            evidence_ids=[],
            version="implied_v1",
            currency="CNY",
            published_at=inp.as_of,
            retrieved_at=inp.as_of,
        )


# ---------------------------------------------------------------------------
# 预期差引擎
# ---------------------------------------------------------------------------

class ExpectationGapEngine:
    """计算研究盈利预测与市场预期之间的预期差。

    区间减法: [a,b] - [c,d] = [a-d, b-c]
    relative_gap = gap / abs(expectation_value)
    """

    def compute(
        self, *, company_symbol: str, metric: str, period: str,
        research_value: Interval, earnings_result_id: str,
        earnings_data_version: str, expectation_id: str,
        expectation_kind: str, expectation_value: ExpectationValue | None,
        market_data_version: str, as_of: str, historical: bool = True,
        expectation_source: str = "",
        catalyst_window: str = "",
        valuation_level: str = "",
        crowding_available: bool = False,
        revision_speed: str = "",
        data_expires_at: str = "",
    ) -> ExpectationGap:
        """计算预期差。

        Args:
            expectation_source: 预期来源（用于优先级排序）
            catalyst_window: 催化剂时间窗口
            valuation_level: 估值水平描述
            crowding_available: 拥挤度数据是否可用
            revision_speed: 预期修正速度
            data_expires_at: 数据有效期限

        Returns:
            ExpectationGap 对象
        """
        source = expectation_source or expectation_kind
        if not isinstance(research_value, Interval):
            research_value = Interval.point(0.0)
        source_priority = ExpectationSource.priority(source)

        missing: list[str] = []
        if historical and not as_of:
            missing.append("as_of")
        if not earnings_result_id or not earnings_data_version:
            missing.append("earnings_version")
        if not expectation_id or not market_data_version:
            missing.append("expectation_version")
        if expectation_value is None:
            missing.append("expectation_value")
        elif not isinstance(expectation_value, ExpectationValue):
            missing.append("expectation_value_incomplete")
        elif not isinstance(expectation_value.value, Interval) or not isinstance(expectation_value.confidence, Interval):
            missing.append("expectation_value_incomplete")
        elif not expectation_value.is_complete():
            missing.append("expectation_value_incomplete")
        elif not visible_as_of(expectation_value, as_of, historical):
            missing.append("expectation_not_visible_as_of")

        if missing:
            # 区分数据缺失（无值）和数据不足（值不完整或不可见）
            status = (GapStatus.EXPECTATION_DATA_MISSING.value
                      if ("expectation_value" in missing or "expectation_version" in missing
                          or "expectation_id" in missing)
                      else GapStatus.INSUFFICIENT_DATA.value)
            return ExpectationGap(
                company_symbol=company_symbol, metric=metric, period=period,
                research_value=research_value, expectation_kind=expectation_kind,
                expectation_source=source, source_priority=source_priority,
                earnings_result_id=earnings_result_id,
                earnings_data_version=earnings_data_version,
                expectation_id=expectation_id, market_data_version=market_data_version,
                as_of=as_of, status=status, gaps=missing,
                catalyst_window=catalyst_window, valuation_level=valuation_level,
                crowding_available=crowding_available, revision_speed=revision_speed,
                data_expires_at=data_expires_at,
            )

        expected = expectation_value.value
        # 区间减法: [a,b] - [c,d] = [a-d, b-c]
        gap = Interval(research_value.low - expected.high, research_value.high - expected.low)

        # relative_gap = gap / abs(expectation_value)
        abs_low = abs(expected.low) if abs(expected.low) > 1e-10 else 1e-10
        abs_high = abs(expected.high) if abs(expected.high) > 1e-10 else 1e-10
        relative_gap = Interval(
            gap.low / max(abs_low, abs_high) if max(abs_low, abs_high) > 1e-10 else 0.0,
            gap.high / max(abs_low, abs_high) if max(abs_low, abs_high) > 1e-10 else 0.0,
        )

        # 预期差置信度：取研究值和预期值置信度的较小值
        research_conf = expectation_value.confidence
        gap_confidence = Interval(
            min(research_conf.low, 0.5),
            min(research_conf.high, 0.8),
        )

        return ExpectationGap(
            company_symbol=company_symbol, metric=metric, period=period,
            research_value=research_value, expectation_value=expected, gap=gap,
            relative_gap=relative_gap,
            expectation_kind=expectation_kind, expectation_source=source,
            source_priority=source_priority,
            earnings_result_id=earnings_result_id,
            earnings_data_version=earnings_data_version,
            expectation_id=expectation_id, market_data_version=market_data_version,
            as_of=as_of, gap_confidence=gap_confidence,
            catalyst_window=catalyst_window, valuation_level=valuation_level,
            crowding_available=crowding_available, revision_speed=revision_speed,
            data_expires_at=data_expires_at,
        )

    def compute_from_market_expectation(
        self, *, research_value: Interval, earnings_result_id: str,
        earnings_data_version: str, market_expectation: MarketExpectation,
        metric: str, market_data_version: str, as_of: str, historical: bool = True,
        catalyst_window: str = "", valuation_level: str = "",
        crowding_available: bool = False, revision_speed: str = "",
        data_expires_at: str = "",
    ) -> ExpectationGap:
        """从 MarketExpectation 对象计算预期差。

        根据 metric 自动选择对应的预期值字段。
        """
        field_map = {
            "revenue": "expected_revenue",
            "net_profit": "expected_net_profit",
            "EPS": "expected_EPS",
        }
        field_name = field_map.get(metric, "expected_net_profit")
        expected_interval: Interval = getattr(market_expectation, field_name)

        # 构造 ExpectationValue 以复用 compute 方法
        ev = ExpectationValue(
            metric=metric, value=expected_interval,
            unit="yuan" if metric != "EPS" else "yuan_per_share",
            currency=market_expectation.currency,
            period=market_expectation.forecast_period,
            as_of=market_expectation.available_at,
            data_version=market_expectation.version,
            source_claim_id="market_expectation",
            source_evidence_id=",".join(market_expectation.evidence_ids[:3]),
            extraction_method="market_expectation",
            confidence=market_expectation.confidence,
            published_at=market_expectation.published_at,
            retrieved_at=market_expectation.retrieved_at,
        )

        return self.compute(
            company_symbol=market_expectation.symbol, metric=metric,
            period=market_expectation.forecast_period,
            research_value=research_value, earnings_result_id=earnings_result_id,
            earnings_data_version=earnings_data_version,
            expectation_id=market_expectation.expectation_id,
            expectation_kind=market_expectation.expectation_source,
            expectation_value=ev, market_data_version=market_data_version,
            as_of=as_of, historical=historical,
            expectation_source=market_expectation.expectation_source,
            catalyst_window=catalyst_window, valuation_level=valuation_level,
            crowding_available=crowding_available, revision_speed=revision_speed,
            data_expires_at=data_expires_at,
        )


# ---------------------------------------------------------------------------
# 投资观点状态机
# ---------------------------------------------------------------------------

class ThesisLifecycle:
    """投资观点完整状态机。

    状态迁移规则：
    - DRAFT → EVIDENCE_PENDING: 必要 Claim 全部存在
    - EVIDENCE_PENDING → ACTIVE: 必要 Claim 全部验证通过 + 有盈利影响
    - ACTIVE → HIGH_CONVICTION: 预期差显著 + 催化剂在窗口内 + 证据充足
    - 任意活跃状态 → CONFLICTED: 反证进入或必要 Claim 失效
    - CONFLICTED → INVALIDATED: 反证确认或无法解决冲突
    - 任意状态 → EXPIRED: 超过有效期或催化剂窗口结束
    - EXPIRED → ARCHIVED: 归档

    不修改 Claim 置信度或交易状态。
    """

    INVALID_CLAIM_STATES = ThesisStatus.invalid_set()

    # 合法状态迁移
    TRANSITIONS: dict[str, set[str]] = {
        ThesisStatus.DRAFT.value: {
            ThesisStatus.EVIDENCE_PENDING.value,
            ThesisStatus.CONFLICTED.value,
            ThesisStatus.ARCHIVED.value,
        },
        ThesisStatus.EVIDENCE_PENDING.value: {
            ThesisStatus.ACTIVE.value,
            ThesisStatus.CONFLICTED.value,
            ThesisStatus.INVALIDATED.value,
            ThesisStatus.EXPIRED.value,
        },
        ThesisStatus.ACTIVE.value: {
            ThesisStatus.HIGH_CONVICTION.value,
            ThesisStatus.CONFLICTED.value,
            ThesisStatus.INVALIDATED.value,
            ThesisStatus.EXPIRED.value,
        },
        ThesisStatus.HIGH_CONVICTION.value: {
            ThesisStatus.ACTIVE.value,  # 降级
            ThesisStatus.CONFLICTED.value,
            ThesisStatus.INVALIDATED.value,
            ThesisStatus.EXPIRED.value,
        },
        ThesisStatus.CONFLICTED.value: {
            ThesisStatus.ACTIVE.value,  # 冲突解决
            ThesisStatus.INVALIDATED.value,
            ThesisStatus.EXPIRED.value,
        },
        ThesisStatus.INVALIDATED.value: {
            ThesisStatus.ARCHIVED.value,
        },
        ThesisStatus.EXPIRED.value: {
            ThesisStatus.ARCHIVED.value,
        },
        ThesisStatus.ARCHIVED.value: set(),
    }

    def _can_transition(self, from_status: str, to_status: str) -> bool:
        """检查状态迁移是否合法。"""
        return to_status in self.TRANSITIONS.get(from_status, set())

    def _transition(
        self, thesis: InvestmentThesis, to_status: str, reason: str, *,
        operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """执行状态迁移，返回更新后的 thesis 和可选的复核任务。"""
        old = thesis.status
        if old == to_status:
            return thesis, None
        if not self._can_transition(old, to_status):
            return thesis, None

        thesis.status = to_status
        thesis.updated_at = utc_now()
        thesis.version += 1
        audit = ThesisAudit(
            thesis_id=thesis.thesis_id, from_status=old, to_status=to_status,
            reason=reason, operator=operator,
        )
        thesis.audits.append(audit)
        review = ReviewTask(thesis_id=thesis.thesis_id, reason=reason) if to_status == ThesisStatus.CONFLICTED.value else None
        return thesis, review

    def evaluate_claims(
        self, thesis: InvestmentThesis, claim_states: dict[str, str], *, operator: str = "system",
    ) -> tuple[InvestmentThesis, list[ReviewTask]]:
        """必要 Claim 失效时降级 thesis，写审计 + 生成复核任务。

        返回 (thesis, list[ReviewTask])。
        """
        states = claim_states if isinstance(claim_states, Mapping) else {}
        required_claims = thesis.required_claim_ids if isinstance(thesis.required_claim_ids, (list, tuple)) else []
        affected = [
            cid for cid in required_claims
            if isinstance(cid, str)
            and isinstance(states.get(cid, ""), str)
            and states.get(cid, "").upper() in self.INVALID_CLAIM_STATES
        ]
        if not affected:
            return thesis, []

        reason = "necessary_claim_invalid:" + ",".join(sorted(affected))

        # 根据当前状态决定降级目标
        current = thesis.status
        if current in ThesisStatus.active_set() or current in (
            ThesisStatus.EVIDENCE_PENDING.value, ThesisStatus.DRAFT.value,
        ):
            updated, review = self._transition(
                thesis, ThesisStatus.CONFLICTED.value, reason, operator=operator,
            )
        else:
            updated, review = self._transition(
                thesis, ThesisStatus.INVALIDATED.value, reason, operator=operator,
            )

        reviews = [review] if review else []
        return updated, reviews

    def add_contradictory_evidence(
        self, thesis: InvestmentThesis, evidence_id: str, *, reason: str = "",
        operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """新反证进入后降级。"""
        if evidence_id and evidence_id not in thesis.contradictory_evidence_ids:
            thesis.contradictory_evidence_ids.append(evidence_id)
        r = reason or f"contradictory_evidence_added:{evidence_id}"
        if thesis.status in ThesisStatus.active_set():
            return self._transition(thesis, ThesisStatus.CONFLICTED.value, r, operator=operator)
        elif thesis.status == ThesisStatus.DRAFT.value:
            return self._transition(thesis, ThesisStatus.CONFLICTED.value, r, operator=operator)
        return thesis, None

    def check_catalyst_expiry(
        self, thesis: InvestmentThesis, as_of: str = "", *, operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """催化剂窗口结束后重新评估。"""
        catalysts = thesis.catalysts if isinstance(thesis.catalysts, (list, tuple)) else []
        expired = [c for c in catalysts if isinstance(c, Catalyst) and c.is_expired(as_of)]
        if not expired:
            return thesis, None
        reason = "catalyst_window_expired:" + ",".join(
            str(c.description or "")[:30] for c in expired
        )
        if thesis.status in ThesisStatus.active_set():
            return self._transition(thesis, ThesisStatus.EXPIRED.value, reason, operator=operator)
        return thesis, None

    def check_expiry(
        self, thesis: InvestmentThesis, as_of: str = "", *, operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """检查观点是否超过有效期。"""
        if not thesis.valid_until:
            return thesis, None
        cutoff = _time(as_of) if as_of else datetime.now(timezone.utc)
        end = _time(thesis.valid_until)
        if cutoff is None or end is None or cutoff <= end:
            return thesis, None
        reason = f"thesis_expired:{thesis.valid_until}"
        if thesis.status != ThesisStatus.EXPIRED.value and thesis.status != ThesisStatus.ARCHIVED.value:
            return self._transition(thesis, ThesisStatus.EXPIRED.value, reason, operator=operator)
        return thesis, None

    def promote_to_active(
        self, thesis: InvestmentThesis, *, has_earnings_impact: bool = True,
        operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """从 EVIDENCE_PENDING 升级到 ACTIVE。"""
        if thesis.status != ThesisStatus.EVIDENCE_PENDING.value:
            return thesis, None
        if not has_earnings_impact:
            return thesis, None
        return self._transition(
            thesis, ThesisStatus.ACTIVE.value,
            "evidence_verified_and_earnings_impact_available", operator=operator,
        )

    def promote_to_high_conviction(
        self, thesis: InvestmentThesis, *, gap_significant: bool = True,
        catalyst_in_window: bool = True, evidence_sufficient: bool = True,
        operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """从 ACTIVE 升级到 HIGH_CONVICTION。"""
        if thesis.status != ThesisStatus.ACTIVE.value:
            return thesis, None
        if not (gap_significant and catalyst_in_window and evidence_sufficient):
            return thesis, None
        return self._transition(
            thesis, ThesisStatus.HIGH_CONVICTION.value,
            "gap_significant_and_catalyst_in_window", operator=operator,
        )

    def archive(
        self, thesis: InvestmentThesis, *, reason: str = "archived", operator: str = "system",
    ) -> tuple[InvestmentThesis, ReviewTask | None]:
        """归档观点。"""
        if thesis.status not in (ThesisStatus.EXPIRED.value, ThesisStatus.INVALIDATED.value):
            return thesis, None
        return self._transition(thesis, ThesisStatus.ARCHIVED.value, reason, operator=operator)


# ---------------------------------------------------------------------------
# 一致预期聚合统计引擎（P1.7）
# ---------------------------------------------------------------------------

class ConsensusSummaryEngine:
    """一致预期聚合统计引擎（P1.7）。

    从多家机构的 ConsensusEstimate 列表计算：
    - 中位数、均值、25/75 分位数、极值
    - 来源数（去重后的机构数）
    - 分歧度（标准差 + 极差）
    - 修正方向（与上一期相比 up/down/flat/unknown）

    支持分机构、分发布日期、分预测期间的版本序列过滤。
    来源数低于阈值（默认 3）时 status="insufficient_sources"，预期差不可计算。
    """

    DEFAULT_MIN_SOURCES = 3

    def compute_summary(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        estimates: list[ConsensusEstimate],
        previous_summary: ConsensusSummary | None = None,
        min_sources_threshold: int | None = None,
        historical: bool = True,
    ) -> ConsensusSummary:
        """从 ConsensusEstimate 列表计算一致预期聚合统计。

        Args:
            company_symbol: 公司代码
            metric: 指标名（如 net_profit / revenue / EPS）
            period: 预测期间
            as_of: PIT 时点，仅考虑 published_at ≤ as_of 的预期
            estimates: 多家机构的 ConsensusEstimate 列表
            previous_summary: 上一期 ConsensusSummary（用于计算修正方向）
            min_sources_threshold: 最小来源数阈值，None 时使用默认值 3
            historical: True 时启用 PIT 过滤，False 时不过滤

        Returns:
            ConsensusSummary 实例。来源不足时 status="insufficient_sources"
        """
        try:
            threshold = int(
                min_sources_threshold
                if min_sources_threshold is not None else self.DEFAULT_MIN_SOURCES
            )
        except (TypeError, ValueError):
            threshold = self.DEFAULT_MIN_SOURCES
        threshold = max(1, threshold)
        base = ConsensusSummary(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
            min_sources_threshold=threshold,
            previous_summary_id=(previous_summary.summary_id if previous_summary else ""),
        )

        # 1) 展平为 (value, provider, published_at, estimate_id, retrieved_at) 列表
        flat: list[tuple[float, str, str, str, str]] = []
        contributing_ids: list[str] = []
        contributing_providers: list[str] = []
        raw_estimates = estimates if isinstance(estimates, (list, tuple)) else []
        for est in raw_estimates:
            if not isinstance(est, ConsensusEstimate):
                continue
            values = est.values if isinstance(est.values, (list, tuple)) else []
            for v in values:
                if not isinstance(v, ExpectationValue):
                    continue
                if v.metric != metric or v.period != period:
                    continue
                if historical and not visible_as_of(v, as_of, True):
                    continue
                try:
                    mid_value = float(v.value.mid)
                except (AttributeError, TypeError, ValueError):
                    continue
                if not isfinite(mid_value):
                    continue
                provider_name = est.institution_id or est.provider or "unknown"
                flat.append((mid_value, provider_name, v.published_at, est.consensus_id, v.retrieved_at))
                if est.consensus_id not in contributing_ids:
                    contributing_ids.append(est.consensus_id)
                if provider_name not in contributing_providers:
                    contributing_providers.append(provider_name)

        # 2) 来源不足时返回不可计算
        unique_sources = set(contributing_providers)
        if not flat:
            base.status = "no_data"
            base.notes.append("no_visible_estimates_as_of")
            return base
        if len(unique_sources) < threshold:
            base.status = "insufficient_sources"
            base.source_count = len(unique_sources)
            base.contributing_estimate_ids = contributing_ids
            base.contributing_providers = contributing_providers
            base.notes.append(
                f"source_count={len(unique_sources)} < threshold={threshold}"
            )
            return base

        # 3) 计算统计量
        values = [item[0] for item in flat]
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        median = statistics.median(sorted_vals)
        mean = statistics.fmean(sorted_vals) if n > 0 else 0.0
        # 分位数：线性插值
        p25 = self._percentile(sorted_vals, 25)
        p75 = self._percentile(sorted_vals, 75)
        min_v = sorted_vals[0]
        max_v = sorted_vals[-1]
        # 分歧度
        if n >= 2:
            dispersion_std = statistics.pstdev(sorted_vals)
            dispersion_range = max_v - min_v
        else:
            dispersion_std = 0.0
            dispersion_range = 0.0

        # 4) 修正方向：与上一期 median 比较
        revision_direction = "unknown"
        revision_magnitude = 0.0
        if previous_summary is not None and previous_summary.status == "ok":
            delta = median - previous_summary.median
            revision_magnitude = delta
            if abs(delta) < 1e-9:
                revision_direction = "flat"
            elif delta > 0:
                revision_direction = "up"
            else:
                revision_direction = "down"

        return ConsensusSummary(
            summary_id=base.summary_id,
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
            median=median,
            mean=mean,
            p25=p25,
            p75=p75,
            min_value=min_v,
            max_value=max_v,
            source_count=len(unique_sources),
            dispersion_std=dispersion_std,
            dispersion_range=dispersion_range,
            revision_direction=revision_direction,
            revision_magnitude=revision_magnitude,
            previous_summary_id=base.previous_summary_id,
            contributing_estimate_ids=contributing_ids,
            contributing_providers=contributing_providers,
            status="ok",
            min_sources_threshold=threshold,
            notes=[],
        )

    @staticmethod
    def _percentile(sorted_vals: list[float], pct: float) -> float:
        """线性插值分位数。

        Args:
            sorted_vals: 已排序的非空数值列表
            pct: 百分位（0-100）

        Returns:
            分位数值
        """
        if not sorted_vals:
            return 0.0
        if len(sorted_vals) == 1:
            return sorted_vals[0]
        # NumPy linear interpolation equivalent
        rank = (pct / 100.0) * (len(sorted_vals) - 1)
        lower_idx = int(rank)
        upper_idx = min(lower_idx + 1, len(sorted_vals) - 1)
        frac = rank - lower_idx
        return sorted_vals[lower_idx] + frac * (sorted_vals[upper_idx] - sorted_vals[lower_idx])

    def filter_visible_estimates(
        self,
        estimates: list[ConsensusEstimate],
        *,
        metric: str,
        period: str,
        as_of: str,
        historical: bool = True,
    ) -> list[ConsensusEstimate]:
        """PIT 过滤：仅保留含可见预期值的 ConsensusEstimate。

        Args:
            estimates: 原始 ConsensusEstimate 列表
            metric: 指标名
            period: 预测期间
            as_of: PIT 时点
            historical: True 时启用 PIT 过滤

        Returns:
            过滤后的 ConsensusEstimate 列表（保留原对象引用）
        """
        visible: list[ConsensusEstimate] = []
        raw_estimates = estimates if isinstance(estimates, (list, tuple)) else []
        for est in raw_estimates:
            if not isinstance(est, ConsensusEstimate):
                continue
            has_match = False
            values = est.values if isinstance(est.values, (list, tuple)) else []
            for v in values:
                if not isinstance(v, ExpectationValue):
                    continue
                if v.metric != metric or v.period != period:
                    continue
                if historical and not visible_as_of(v, as_of, True):
                    continue
                has_match = True
                break
            if has_match:
                visible.append(est)
        return visible


# ---------------------------------------------------------------------------
# AquaTrade 预测构建器（P1.7 第四类独立预期）
# ---------------------------------------------------------------------------

class AquatradeForecastBuilder:
    """构建 AquaTrade 独立研究预测（第四类预期，与公司指引/一致预期/隐含预期并列）。

    不得冒充公司指引或一致预期；必须保留 model_version、as_of、assumptions_snapshot
    和 formula_trace 以满足审计要求。
    """

    def build(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        value_low: float,
        value_high: float,
        unit: str,
        currency: str,
        model_version: str,
        integrated_forecast_id: str = "",
        scenario: str = "base",
        assumptions_snapshot: dict | None = None,
        formula_trace: list[str] | None = None,
        source_evidence_id: str = "",
        revision_of: str = "",
        revision_reason: str = "",
    ) -> AquatradeForecast:
        """构建 AquaTradeForecast 实例。

        Args:
            company_symbol: 公司代码
            metric: 指标名（revenue / net_profit / EPS 等）
            period: 预测期间
            as_of: 测算时点
            value_low / value_high: 预测值区间
            unit: 单位
            currency: 币种
            model_version: 模型版本（必填，禁止硬编码默认值）
            integrated_forecast_id: 关联 P1.4 IntegratedForecast.forecast_id（可选）
            scenario: 研究情景（base/stress/optimistic）
            assumptions_snapshot: 输入假设快照
            formula_trace: 公式轨迹
            source_evidence_id: 来源证据 ID
            revision_of: 前序预测 ID（修订链）
            revision_reason: 修订原因

        Returns:
            AquatradeForecast 实例

        Raises:
            ValueError: 缺少必填字段时
        """
        missing: list[str] = []
        if not company_symbol:
            missing.append("company_symbol")
        if not metric:
            missing.append("metric")
        if not period:
            missing.append("period")
        if not as_of:
            missing.append("as_of")
        if not model_version:
            missing.append("model_version")
        if missing:
            raise ValueError(f"AquatradeForecast 缺少必填字段: {missing}")

        low = _finite_float(value_low)
        high = _finite_float(value_high)
        if low is None or high is None:
            raise ValueError("value_low/value_high must be finite numbers")
        ev = ExpectationValue(
            metric=metric,
            value=Interval(low, high),
            unit=unit,
            currency=currency,
            period=period,
            as_of=as_of,
            data_version=model_version,
            source_claim_id=f"aquatrade_research_{model_version}",
            source_evidence_id=source_evidence_id,
            extraction_method="aquatrade_research_pipeline",
            confidence=Interval(0.5, 0.7),
            published_at=as_of,
            retrieved_at=as_of,
        )
        return AquatradeForecast(
            company_symbol=company_symbol,
            values=[ev],
            integrated_forecast_id=integrated_forecast_id,
            model_version=model_version,
            scenario=scenario,
            as_of=as_of,
            forecast_period=period,
            assumptions_snapshot=(
                dict(assumptions_snapshot) if isinstance(assumptions_snapshot, Mapping) else {}
            ),
            formula_trace=(
                [item for item in formula_trace if isinstance(item, str)]
                if isinstance(formula_trace, (list, tuple))
                else []
            ),
            revision_of=revision_of,
            revision_reason=revision_reason,
        )
