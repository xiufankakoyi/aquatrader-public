"""P2.5 一致预期修正引擎。

按 P2 计划 §4 Task P2.5：
- 1/3/6 个月收入和利润预测修正
- 预测来源数和分歧度
- 公司指引与市场共识差
- 预期数据缺失和来源集中风险

输出对象：
- RevisionWindow：1m / 3m / 6m 时间窗口
- RevisionPoint：单条修正事件（含 published_at / source / value / delta_vs_prev）
- RevisionSequence：按发布日期重建的修正序列
- ConsensusDiversity：来源数与分歧度统计
- GuidanceConsensusGap：公司指引 vs 市场共识差
- SourceConcentrationRisk：来源集中风险
- RevisionEngine：主引擎

硬门禁（P2.5 验收 + AGENTS.md）：
- 修正序列必须按 published_at 重建（验收点 1）
- 单一机构来源不能冒充市场共识（验收点 3）
- 无可靠共识时输出不可计算（验收点 4）
- 不输出交易动作建议
- 字段缺失降级处理
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Iterable

from core.research.earnings.interval import Interval

from .contracts import (
    CompanyGuidance,
    ConsensusEstimate,
    ConsensusSummary,
    ExpectationSource,
    ExpectationValue,
    GapStatus,
    utc_now,
)
from .engine import visible_as_of


# ---------------------------------------------------------------------------
# 时间窗口（P2.5 §194）
# ---------------------------------------------------------------------------

REVISION_WINDOW_1M: str = "1m"
REVISION_WINDOW_3M: str = "3m"
REVISION_WINDOW_6M: str = "6m"

REVISION_WINDOWS: tuple[str, ...] = (REVISION_WINDOW_1M, REVISION_WINDOW_3M, REVISION_WINDOW_6M)

# 窗口对应天数（用于 PIT 过滤）
_WINDOW_DAYS: dict[str, int] = {
    REVISION_WINDOW_1M: 30,
    REVISION_WINDOW_3M: 90,
    REVISION_WINDOW_6M: 180,
}


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

STATUS_OK: str = "ok"
STATUS_INSUFFICIENT_SOURCES: str = "insufficient_sources"
STATUS_NOT_COMPUTABLE: str = "not_computable"
STATUS_NO_DATA: str = "no_data"
STATUS_DEGRADED: str = "degraded"

# 默认最小来源数（验收点 3：单一机构不能冒充市场共识）
DEFAULT_MIN_SOURCES: int = 3

# 来源集中度阈值（HHI 越高越集中；0.5 表示单一来源占比 50% 以上）
DEFAULT_CONCENTRATION_HHI_THRESHOLD: float = 0.5


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _source_id(value: Any) -> str:
    """Return a hashable, display-safe source identifier."""
    if isinstance(value, str):
        return value.strip() or "unknown"
    if value is None or isinstance(value, (list, tuple, set, dict)):
        return "unknown"
    return str(value).strip() or "unknown"


def _valid_estimates(value: Any) -> list[ConsensusEstimate]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, ConsensusEstimate)]


def _valid_values(estimate: Any) -> list[ExpectationValue]:
    values = estimate.values if isinstance(estimate.values, (list, tuple)) else []
    return [
        item for item in values
        if isinstance(item, ExpectationValue) and isinstance(item.value, Interval)
    ]


def _parse_iso(value: str) -> datetime | None:
    """解析 ISO 时间字符串，失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# 修正序列
# ---------------------------------------------------------------------------


@dataclass
class RevisionPoint:
    """单条修正事件。

    每条记录代表一家机构在某发布日期对某预测期间的预期修正。

    Attributes:
        published_at: 发布日期（ISO 8601）
        institution_id: 机构 ID
        provider: 数据提供商
        value: 预期值区间
        retrieved_at: 检索时间
        delta_vs_previous: 与同机构前一条预期的差值（None 表示首条）
        revision_direction: 修正方向（up/down/flat/unknown）
        estimate_id: 关联 ConsensusEstimate.consensus_id
        source_claim_id: 来源 claim ID
        source_evidence_id: 来源 evidence ID
    """

    published_at: str = ""
    institution_id: str = ""
    provider: str = ""
    value: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    retrieved_at: str = ""
    delta_vs_previous: float | None = None
    revision_direction: str = "unknown"
    estimate_id: str = ""
    source_claim_id: str = ""
    source_evidence_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "published_at": self.published_at,
            "institution_id": self.institution_id,
            "provider": self.provider,
            "value": self.value.to_dict(),
            "retrieved_at": self.retrieved_at,
            "delta_vs_previous": self.delta_vs_previous,
            "revision_direction": self.revision_direction,
            "estimate_id": self.estimate_id,
            "source_claim_id": self.source_claim_id,
            "source_evidence_id": self.source_evidence_id,
        }


@dataclass
class RevisionSequence:
    """按发布日期重建的修正序列。

    验收点 1：修正序列按 published_at 升序排列，每条记录可追溯到机构与发布日期。

    Attributes:
        sequence_id: 序列唯一 ID（基于 company_symbol/metric/period/as_of/window 派生）
        company_symbol: 公司代码
        metric: 指标名（revenue / net_profit / EPS）
        period: 预测期间
        as_of: PIT 时点
        window: 时间窗口（1m / 3m / 6m）
        revision_points: 修正点列表（按 published_at 升序）
        source_count: 来源机构数（去重）
        revision_count: 修正次数（去重后）
        net_revision: 净修正幅度（最新 - 最早的中位数）
        revision_direction: 整体修正方向（up/down/flat/unknown）
        status: 状态（ok / insufficient_sources / not_computable / no_data）
        notes: 备注列表
    """

    sequence_id: str = ""
    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    as_of: str = ""
    window: str = ""
    revision_points: list[RevisionPoint] = field(default_factory=list)
    source_count: int = 0
    revision_count: int = 0
    net_revision: float = 0.0
    revision_direction: str = "unknown"
    status: str = STATUS_OK
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """派生 sequence_id（如果未提供）。"""
        if not self.sequence_id:
            self.sequence_id = (
                f"revseq_{self.company_symbol}_{self.metric}_"
                f"{self.period}_{self.as_of}_{self.window}"
            )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "sequence_id": self.sequence_id,
            "company_symbol": self.company_symbol,
            "metric": self.metric,
            "period": self.period,
            "as_of": self.as_of,
            "window": self.window,
            "revision_points": [p.to_dict() for p in self.revision_points],
            "source_count": self.source_count,
            "revision_count": self.revision_count,
            "net_revision": self.net_revision,
            "revision_direction": self.revision_direction,
            "status": self.status,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 来源数与分歧度
# ---------------------------------------------------------------------------


@dataclass
class ConsensusDiversity:
    """一致预期来源数与分歧度统计。

    Attributes:
        company_symbol: 公司代码
        metric: 指标名
        period: 预测期间
        as_of: PIT 时点
        source_count: 来源机构数（去重）
        institution_ids: 机构 ID 列表
        median: 中位数
        mean: 均值
        p25: 25 分位数
        p75: 75 分位数
        min_value: 最小值
        max_value: 最大值
        dispersion_std: 标准差
        dispersion_range: 极差
        dispersion_cv: 变异系数（std / abs(mean)）
        status: 状态（ok / insufficient_sources / not_computable / no_data）
        notes: 备注
    """

    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    as_of: str = ""
    source_count: int = 0
    institution_ids: list[str] = field(default_factory=list)
    median: float = 0.0
    mean: float = 0.0
    p25: float = 0.0
    p75: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0
    dispersion_std: float = 0.0
    dispersion_range: float = 0.0
    dispersion_cv: float = 0.0
    status: str = STATUS_OK
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "company_symbol": self.company_symbol,
            "metric": self.metric,
            "period": self.period,
            "as_of": self.as_of,
            "source_count": self.source_count,
            "institution_ids": list(self.institution_ids),
            "median": self.median,
            "mean": self.mean,
            "p25": self.p25,
            "p75": self.p75,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "dispersion_std": self.dispersion_std,
            "dispersion_range": self.dispersion_range,
            "dispersion_cv": self.dispersion_cv,
            "status": self.status,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 公司指引 vs 市场共识差
# ---------------------------------------------------------------------------


@dataclass
class GuidanceConsensusGap:
    """公司指引与市场共识差。

    Attributes:
        company_symbol: 公司代码
        metric: 指标名
        period: 预测期间
        as_of: PIT 时点
        guidance_value: 公司指引值区间
        consensus_median: 市场共识中位数
        gap: 差值（guidance - consensus）
        relative_gap: 相对差值（gap / abs(consensus)）
        guidance_source_count: 公司指引来源数（通常为 1）
        consensus_source_count: 共识来源数
        status: 状态（ok / insufficient_sources / not_computable / no_data）
        notes: 备注
    """

    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    as_of: str = ""
    guidance_value: Interval = field(default_factory=lambda: Interval(0.0, 0.0))
    consensus_median: float = 0.0
    gap: float = 0.0
    relative_gap: float = 0.0
    guidance_source_count: int = 0
    consensus_source_count: int = 0
    status: str = STATUS_OK
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "company_symbol": self.company_symbol,
            "metric": self.metric,
            "period": self.period,
            "as_of": self.as_of,
            "guidance_value": self.guidance_value.to_dict(),
            "consensus_median": self.consensus_median,
            "gap": self.gap,
            "relative_gap": self.relative_gap,
            "guidance_source_count": self.guidance_source_count,
            "consensus_source_count": self.consensus_source_count,
            "status": self.status,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 来源集中风险
# ---------------------------------------------------------------------------


@dataclass
class SourceConcentrationRisk:
    """来源集中风险。

    基于 HHI（Herfindahl-Hirschman Index）评估来源集中度。
    HHI = sum(market_share^2)，取值 [0, 1]；越高越集中。

    Attributes:
        company_symbol: 公司代码
        metric: 指标名
        period: 预测期间
        as_of: PIT 时点
        source_count: 来源机构数
        hhi: HHI 指数
        top_source_share: 最大来源占比
        top_source_id: 最大来源 ID
        is_concentrated: 是否构成集中风险
        threshold: 集中风险阈值
        status: 状态
        notes: 备注
    """

    company_symbol: str = ""
    metric: str = ""
    period: str = ""
    as_of: str = ""
    source_count: int = 0
    hhi: float = 0.0
    top_source_share: float = 0.0
    top_source_id: str = ""
    is_concentrated: bool = False
    threshold: float = DEFAULT_CONCENTRATION_HHI_THRESHOLD
    status: str = STATUS_OK
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "company_symbol": self.company_symbol,
            "metric": self.metric,
            "period": self.period,
            "as_of": self.as_of,
            "source_count": self.source_count,
            "hhi": self.hhi,
            "top_source_share": self.top_source_share,
            "top_source_id": self.top_source_id,
            "is_concentrated": self.is_concentrated,
            "threshold": self.threshold,
            "status": self.status,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 修正引擎
# ---------------------------------------------------------------------------


class RevisionEngine:
    """一致预期修正引擎（P2.5）。

    提供四类输出：
    1. 修正序列：1/3/6 个月窗口内的修正事件，按 published_at 重建
    2. 来源数与分歧度：分机构去重后的统计
    3. 公司指引 vs 市场共识差
    4. 来源集中风险：HHI 指数评估

    硬门禁：
    - 修正序列必须按 published_at 升序（验收点 1）
    - 单一机构来源（source_count < min_sources）时 status="insufficient_sources"，
      不可冒充市场共识（验收点 3）
    - 无可靠共识时 status="not_computable" / "no_data"（验收点 4）
    """

    def __init__(
        self,
        *,
        min_sources: int = DEFAULT_MIN_SOURCES,
        concentration_hhi_threshold: float = DEFAULT_CONCENTRATION_HHI_THRESHOLD,
    ) -> None:
        """初始化修正引擎。

        Args:
            min_sources: 最小来源数阈值，默认 3
            concentration_hhi_threshold: 来源集中 HHI 阈值，默认 0.5
        """
        self.min_sources = max(1, _safe_int(min_sources, DEFAULT_MIN_SOURCES))
        self.concentration_hhi_threshold = max(
            0.0, min(1.0, _safe_float(concentration_hhi_threshold, DEFAULT_CONCENTRATION_HHI_THRESHOLD))
        )

    # ------------------------------------------------------------------
    # 修正序列（验收点 1）
    # ------------------------------------------------------------------

    def build_revision_sequence(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        estimates: list[ConsensusEstimate],
        window: str = REVISION_WINDOW_3M,
        historical: bool = True,
    ) -> RevisionSequence:
        """按发布日期重建修正序列。

        流程：
        1. PIT 过滤：仅保留 published_at ≤ as_of 的预期
        2. 窗口过滤：仅保留 published_at ≥ as_of - window_days 的预期
        3. 按 institution_id 去重，同机构仅保留最新版本（最新 published_at）
        4. 按 published_at 升序排列
        5. 计算每条记录与同机构前一条的 delta_vs_previous
        6. 整体修正方向 = 最新中位数 - 最早中位数

        Args:
            company_symbol: 公司代码
            metric: 指标名（revenue / net_profit / EPS）
            period: 预测期间
            as_of: PIT 时点
            estimates: ConsensusEstimate 列表
            window: 时间窗口（1m / 3m / 6m）
            historical: True 时启用 PIT 过滤

        Returns:
            RevisionSequence；来源不足或无数据时 status 反映原因
        """
        if window not in REVISION_WINDOWS:
            window = REVISION_WINDOW_3M
        window_days = _WINDOW_DAYS[window]

        result = RevisionSequence(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
            window=window,
        )

        cutoff_dt = _parse_iso(as_of)
        if cutoff_dt is None:
            result.status = STATUS_NOT_COMPUTABLE
            result.notes.append("invalid_as_of")
            return result

        # 计算窗口下界
        from datetime import timedelta

        window_start_dt = cutoff_dt - timedelta(days=window_days)
        window_start_str = window_start_dt.isoformat()

        # 1) 展平 + PIT + 窗口过滤
        candidates: list[tuple[str, str, str, str, Interval, str, str, str]] = []
        # (published_at, institution_id, provider, estimate_id, value, retrieved_at, claim_id, evidence_id)
        for est in _valid_estimates(estimates):
            for v in _valid_values(est):
                if v.metric != metric or v.period != period:
                    continue
                if historical and not visible_as_of(v, as_of, True):
                    continue
                # 窗口过滤：published_at >= window_start
                pub_dt = _parse_iso(v.published_at)
                if pub_dt is None:
                    continue
                if pub_dt < window_start_dt:
                    continue
                inst = _source_id(est.institution_id or est.provider)
                candidates.append((
                    v.published_at,
                    inst,
                    _source_id(est.provider),
                    est.consensus_id,
                    v.value,
                    v.retrieved_at,
                    v.source_claim_id,
                    v.source_evidence_id,
                ))

        if not candidates:
            result.status = STATUS_NO_DATA
            result.notes.append(f"no_visible_estimates_in_window_{window}")
            return result

        # 2) 按 institution_id 去重（同机构仅保留最新 published_at 版本）
        by_institution: dict[str, tuple[str, str, str, str, Interval, str, str, str]] = {}
        for row in candidates:
            inst = row[1]
            existing = by_institution.get(inst)
            if existing is None or _parse_iso(row[0]) > _parse_iso(existing[0]):
                by_institution[inst] = row

        unique_rows = list(by_institution.values())
        source_count = len(unique_rows)

        # 3) 来源不足（验收点 3）
        if source_count < self.min_sources:
            result.status = STATUS_INSUFFICIENT_SOURCES
            result.source_count = source_count
            result.notes.append(
                f"source_count={source_count} < min_sources={self.min_sources}"
            )

        # 4) 按 published_at 升序排列（验收点 1）
        unique_rows.sort(key=lambda r: r[0])

        # 5) 构建 RevisionPoint 列表，计算 delta_vs_previous（同机构）
        # 按 (institution_id, published_at) 排序计算 delta
        institution_prev_value: dict[str, float] = {}
        revision_points: list[RevisionPoint] = []
        for row in unique_rows:
            pub, inst, provider, est_id, value, ret, claim_id, evid_id = row
            mid = value.mid
            prev = institution_prev_value.get(inst)
            if prev is None:
                delta = None
                direction = "unknown"
            else:
                delta = mid - prev
                if abs(delta) < 1e-9:
                    direction = "flat"
                elif delta > 0:
                    direction = "up"
                else:
                    direction = "down"
            institution_prev_value[inst] = mid
            revision_points.append(RevisionPoint(
                published_at=pub,
                institution_id=inst,
                provider=provider,
                value=value,
                retrieved_at=ret,
                delta_vs_previous=delta,
                revision_direction=direction,
                estimate_id=est_id,
                source_claim_id=claim_id,
                source_evidence_id=evid_id,
            ))

        # 6) 整体修正方向：最新中位数 - 最早中位数
        if len(revision_points) >= 2:
            earliest = revision_points[0].value.mid
            latest = revision_points[-1].value.mid
            net = latest - earliest
            result.net_revision = net
            if abs(net) < 1e-9:
                result.revision_direction = "flat"
            elif net > 0:
                result.revision_direction = "up"
            else:
                result.revision_direction = "down"
        else:
            result.revision_direction = "unknown"

        result.revision_points = revision_points
        result.source_count = source_count
        result.revision_count = len(revision_points)

        if result.status == STATUS_OK and source_count >= self.min_sources:
            result.status = STATUS_OK
        return result

    def build_all_windows(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        estimates: list[ConsensusEstimate],
        historical: bool = True,
    ) -> dict[str, RevisionSequence]:
        """构建 1m / 3m / 6m 三个窗口的修正序列。

        Args:
            company_symbol: 公司代码
            metric: 指标名
            period: 预测期间
            as_of: PIT 时点
            estimates: ConsensusEstimate 列表
            historical: True 时启用 PIT 过滤

        Returns:
            {window: RevisionSequence} 字典
        """
        return {
            w: self.build_revision_sequence(
                company_symbol=company_symbol,
                metric=metric,
                period=period,
                as_of=as_of,
                estimates=estimates,
                window=w,
                historical=historical,
            )
            for w in REVISION_WINDOWS
        }

    # ------------------------------------------------------------------
    # 来源数与分歧度（验收点 3 / 4）
    # ------------------------------------------------------------------

    def compute_consensus_diversity(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        estimates: list[ConsensusEstimate],
        historical: bool = True,
    ) -> ConsensusDiversity:
        """计算一致预期来源数与分歧度。

        验收点 3：单一机构来源不能冒充市场共识。
        验收点 4：无可靠共识时输出不可计算。

        Args:
            company_symbol: 公司代码
            metric: 指标名
            period: 预测期间
            as_of: PIT 时点
            estimates: ConsensusEstimate 列表
            historical: True 时启用 PIT 过滤

        Returns:
            ConsensusDiversity；来源不足时 status="insufficient_sources"
        """
        result = ConsensusDiversity(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
        )

        # 展平 + PIT 过滤 + 按 institution_id 去重保留最新
        by_institution: dict[str, tuple[float, str]] = {}
        # institution_id -> (mid_value, retrieved_at)
        for est in _valid_estimates(estimates):
            for v in _valid_values(est):
                if v.metric != metric or v.period != period:
                    continue
                if historical and not visible_as_of(v, as_of, True):
                    continue
                inst = _source_id(est.institution_id or est.provider)
                mid = v.value.mid
                existing = by_institution.get(inst)
                retrieved_at = _parse_iso(v.retrieved_at)
                existing_retrieved_at = _parse_iso(existing[1]) if existing else None
                if existing is None or (
                    retrieved_at is not None
                    and (existing_retrieved_at is None or retrieved_at > existing_retrieved_at)
                ):
                    by_institution[inst] = (mid, v.retrieved_at)

        if not by_institution:
            result.status = STATUS_NO_DATA
            result.notes.append("no_visible_estimates_as_of")
            return result

        institution_ids = sorted(by_institution.keys())
        values = [by_institution[i][0] for i in institution_ids]
        source_count = len(institution_ids)

        result.institution_ids = institution_ids
        result.source_count = source_count

        if source_count < self.min_sources:
            result.status = STATUS_INSUFFICIENT_SOURCES
            result.notes.append(
                f"source_count={source_count} < min_sources={self.min_sources}"
            )
            # 仍计算统计量但标记为不可靠
        else:
            result.status = STATUS_OK

        # 计算统计量
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        result.median = statistics.median(sorted_vals)
        result.mean = statistics.fmean(sorted_vals) if n > 0 else 0.0
        result.p25 = self._percentile(sorted_vals, 25)
        result.p75 = self._percentile(sorted_vals, 75)
        result.min_value = sorted_vals[0]
        result.max_value = sorted_vals[-1]
        if n >= 2:
            result.dispersion_std = statistics.pstdev(sorted_vals)
            result.dispersion_range = result.max_value - result.min_value
            if abs(result.mean) > 1e-10:
                result.dispersion_cv = result.dispersion_std / abs(result.mean)
        return result

    # ------------------------------------------------------------------
    # 公司指引 vs 市场共识差
    # ------------------------------------------------------------------

    def compute_guidance_consensus_gap(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        guidance: CompanyGuidance | None,
        estimates: list[ConsensusEstimate],
        historical: bool = True,
    ) -> GuidanceConsensusGap:
        """计算公司指引与市场共识差。

        Args:
            company_symbol: 公司代码
            metric: 指标名
            period: 预测期间
            as_of: PIT 时点
            guidance: 公司指引（None 时 status="not_computable"）
            estimates: ConsensusEstimate 列表
            historical: True 时启用 PIT 过滤

        Returns:
            GuidanceConsensusGap；缺数据时 status 反映原因
        """
        result = GuidanceConsensusGap(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
        )

        # 公司指引缺失
        if not isinstance(guidance, CompanyGuidance):
            result.status = STATUS_NOT_COMPUTABLE
            result.notes.append("guidance_missing")
            return result

        # 从公司指引中提取 metric + period 对应值
        guidance_value: Interval | None = None
        guidance_claim_ids: set[str] = set()
        for v in _valid_values(guidance):
            if v.metric != metric or v.period != period:
                continue
            if historical and not visible_as_of(v, as_of, True):
                continue
            guidance_value = v.value
            if v.source_claim_id:
                guidance_claim_ids.add(v.source_claim_id)
            break

        if guidance_value is None:
            result.status = STATUS_NOT_COMPUTABLE
            result.notes.append("guidance_value_not_visible_as_of")
            return result

        result.guidance_value = guidance_value
        result.guidance_source_count = len(guidance_claim_ids) or 1

        # 计算共识中位数
        diversity = self.compute_consensus_diversity(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
            estimates=estimates,
            historical=historical,
        )
        result.consensus_source_count = diversity.source_count

        if diversity.status == STATUS_NO_DATA:
            result.status = STATUS_NOT_COMPUTABLE
            result.notes.append("consensus_no_data")
            return result
        if diversity.status == STATUS_INSUFFICIENT_SOURCES:
            result.status = STATUS_INSUFFICIENT_SOURCES
            result.notes.append("consensus_insufficient_sources")
            # 仍计算 gap 但标记不可靠
        else:
            result.status = STATUS_OK

        consensus_median = diversity.median
        result.consensus_median = consensus_median
        guidance_mid = guidance_value.mid
        result.gap = guidance_mid - consensus_median
        if abs(consensus_median) > 1e-10:
            result.relative_gap = result.gap / abs(consensus_median)
        return result

    # ------------------------------------------------------------------
    # 来源集中风险
    # ------------------------------------------------------------------

    def compute_source_concentration_risk(
        self,
        *,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        estimates: list[ConsensusEstimate],
        historical: bool = True,
    ) -> SourceConcentrationRisk:
        """评估来源集中风险（基于 HHI 指数）。

        HHI = sum(market_share^2)，取值 [1/n, 1]：
        - 1/n：完全分散
        - 1：完全集中（单一来源）

        阈值默认 0.5：当 top1 来源占比 ≥ 50% 或 HHI ≥ 0.5 时视为集中风险。

        Args:
            company_symbol: 公司代码
            metric: 指标名
            period: 预测期间
            as_of: PIT 时点
            estimates: ConsensusEstimate 列表
            historical: True 时启用 PIT 过滤

        Returns:
            SourceConcentrationRisk
        """
        result = SourceConcentrationRisk(
            company_symbol=company_symbol,
            metric=metric,
            period=period,
            as_of=as_of,
            threshold=self.concentration_hhi_threshold,
        )

        # 按 institution_id 聚合（同机构所有发布版本计为 1 个来源）
        institution_counts: dict[str, int] = {}
        for est in _valid_estimates(estimates):
            for v in _valid_values(est):
                if v.metric != metric or v.period != period:
                    continue
                if historical and not visible_as_of(v, as_of, True):
                    continue
                inst = _source_id(est.institution_id or est.provider)
                institution_counts[inst] = institution_counts.get(inst, 0) + 1

        source_count = len(institution_counts)
        result.source_count = source_count

        if source_count == 0:
            result.status = STATUS_NO_DATA
            result.notes.append("no_visible_estimates_as_of")
            return result

        # HHI = sum(share^2)，share = count / total
        total = sum(institution_counts.values())
        hhi = sum((c / total) ** 2 for c in institution_counts.values())
        result.hhi = hhi

        # top source
        top_inst = max(institution_counts.items(), key=lambda kv: kv[1])
        result.top_source_id = top_inst[0]
        result.top_source_share = top_inst[1] / total

        # 集中风险判定
        result.is_concentrated = (
            hhi >= self.concentration_hhi_threshold
            or result.top_source_share >= self.concentration_hhi_threshold
            or source_count < self.min_sources
        )

        if result.is_concentrated:
            if source_count < self.min_sources:
                result.status = STATUS_INSUFFICIENT_SOURCES
                result.notes.append(
                    f"source_count={source_count} < min_sources={self.min_sources}"
                )
            else:
                result.status = STATUS_DEGRADED
                result.notes.append(
                    f"hhi={hhi:.4f} >= threshold={self.concentration_hhi_threshold}"
                )
        else:
            result.status = STATUS_OK
        return result

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

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
        rank = (pct / 100.0) * (len(sorted_vals) - 1)
        lower_idx = int(rank)
        upper_idx = min(lower_idx + 1, len(sorted_vals) - 1)
        frac = rank - lower_idx
        return sorted_vals[lower_idx] + frac * (sorted_vals[upper_idx] - sorted_vals[lower_idx])


def build_revision_engine(
    *,
    min_sources: int = DEFAULT_MIN_SOURCES,
    concentration_hhi_threshold: float = DEFAULT_CONCENTRATION_HHI_THRESHOLD,
) -> RevisionEngine:
    """工厂函数：构建 RevisionEngine。"""
    return RevisionEngine(
        min_sources=min_sources,
        concentration_hhi_threshold=concentration_hhi_threshold,
    )
