"""Research→QUANT 只读适配器。

从 ResearchCandidate 生成 QuantCandidate，不复制投研模型，
不携带 Claim/Evidence/Thesis 等投研内部对象。

职责边界：
- 只读：不修改 ResearchCandidate 的任何字段
- 隔离：不导入 core.research_graph 或 core.research.control_plane
- 降级：字段缺失时标记 degraded 而非崩溃
- PIT：保留 as_of/available_at/expires_at，由下游 PIT 检查使用
- 唯一性：candidate_id 缺失时自动生成 uuid，避免多候选互相覆盖
- 规范性：company_id 缺失时不回退为证券代码，避免伪造规范 ID
"""
from __future__ import annotations

import logging
import uuid as uuid_module
from typing import Any

from core.quant.contracts import QuantCandidate

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转浮点，失败返回默认值。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_interval_bounds(interval_obj: Any, default_low: float, default_high: float) -> tuple[float, float]:
    """从 Interval 对象提取上下界，失败返回默认值。

    Args:
        interval_obj: Interval 对象（需有 low/high 属性）或 dict。
        default_low: 默认下界。
        default_high: 默认上界。

    Returns:
        (low, high) 元组。
    """
    if interval_obj is None:
        return default_low, default_high
    # 对象形式
    low = _safe_float(getattr(interval_obj, "low", None), default_low)
    high = _safe_float(getattr(interval_obj, "high", None), default_high)
    if low == default_low and high == default_high:
        # 尝试 dict 形式
        if isinstance(interval_obj, dict):
            low = _safe_float(interval_obj.get("low"), default_low)
            high = _safe_float(interval_obj.get("high"), default_high)
    return low, high


def _score_breakdown_to_float(score_obj: Any) -> float:
    """从 ScoreBreakdown 提取分数标量。

    ScoreBreakdown.score 是 Interval，取中位数作为标量分数。
    缺失时返回 0.0。

    Args:
        score_obj: ScoreBreakdown 对象或 dict。

    Returns:
        分数标量 [0, 1]。
    """
    if score_obj is None:
        return 0.0
    # 获取 score 字段
    score_field = getattr(score_obj, "score", None)
    if score_field is None and isinstance(score_obj, dict):
        score_field = score_obj.get("score")
    if score_field is None:
        return 0.0
    low, high = _safe_interval_bounds(score_field, 0.0, 0.0)
    return (low + high) / 2.0 if (low or high) else 0.0


def adapt_research_candidate(candidate: Any) -> QuantCandidate:
    """将 ResearchCandidate 适配为 QuantCandidate。

    只读操作：不修改输入对象。隔离操作：不导入投研内部模块。
    降级操作：字段缺失时标记 degraded 并记录 degraded_fields。

    Args:
        candidate: ResearchCandidate 对象或 dict（兼容 to_dict 输出）。

    Returns:
        QuantCandidate 实例。
    """
    degraded_fields: list[str] = []

    # 兼容对象和 dict 两种输入
    def _get(name: str) -> Any:
        if hasattr(candidate, name):
            return getattr(candidate, name)
        if isinstance(candidate, dict):
            return candidate.get(name)
        return None

    candidate_id = str(_get("candidate_id") or "")
    symbol = str(_get("symbol") or "")
    sector_id = str(_get("sector_id") or "")
    as_of = str(_get("as_of") or "")
    available_at = str(_get("available_at") or "")
    expires_at = str(_get("expires_at") or "")
    catalyst_window = str(_get("catalyst_window") or "")

    # candidate_id 缺失时自动生成 uuid，避免多候选互相覆盖
    if not candidate_id:
        candidate_id = f"cand-auto-{uuid_module.uuid4().hex[:12]}"
        degraded_fields.append("candidate_id")

    # 分数提取（ScoreBreakdown → 标量）
    research_score = _score_breakdown_to_float(_get("research_score"))
    alpha_score = _score_breakdown_to_float(_get("alpha_score"))
    core_score = _score_breakdown_to_float(_get("core_score"))

    # Interval 提取
    evidence_conf = _get("evidence_confidence")
    ev_low, ev_high = _safe_interval_bounds(evidence_conf, 0.0, 0.0)

    valuation_risk = _get("valuation_risk")
    vr_low, vr_high = _safe_interval_bounds(valuation_risk, 0.0, 1.0)

    # source_trace
    source_trace_raw = _get("source_trace")
    if isinstance(source_trace_raw, list):
        source_trace = [str(s) for s in source_trace_raw]
    else:
        source_trace = []
        if source_trace_raw is not None:
            degraded_fields.append("source_trace")

    # 追溯字段提取（P0-4 契约补齐）
    thesis_id = str(_get("thesis_id") or "")
    thesis_status = str(_get("thesis_status") or "")
    claim_id = str(_get("claim_id") or "")
    claim_status = str(_get("claim_status") or "")
    # company_id 缺失时不回退为 symbol，避免把证券代码伪装成规范公司 ID
    company_id = str(_get("company_id") or "")
    if not company_id:
        degraded_fields.append("company_id")
    mapping_confidence = _safe_float(_get("mapping_confidence"), 0.0)
    evidence_as_of = str(_get("evidence_as_of") or _get("as_of") or "")

    # 主题追溯字段（动态主题控制面）
    theme_id = str(_get("theme_id") or "")
    theme_run_id = str(_get("theme_run_id") or "")
    theme_type = str(_get("theme_type") or "")
    theme_phase = str(_get("theme_phase") or "")

    # 关键字段缺失检查
    if not symbol:
        degraded_fields.append("symbol")
    if not as_of:
        degraded_fields.append("as_of")
    if not sector_id:
        degraded_fields.append("sector_id")
    if not available_at:
        degraded_fields.append("available_at")
    if not thesis_id:
        degraded_fields.append("thesis_id")
    # 追溯字段缺失检查（P0-5 契约收紧）
    if not claim_id:
        degraded_fields.append("claim_id")
    if not claim_status:
        degraded_fields.append("claim_status")
    if mapping_confidence < 0.0001:  # 0 或接近 0 视为缺失
        degraded_fields.append("mapping_confidence")

    # 分数缺失检查（0.0 可能是真实值也可能是缺失）
    if research_score == 0.0 and alpha_score == 0.0 and core_score == 0.0:
        degraded_fields.append("scores")

    if degraded_fields:
        logger.debug(
            "QuantCandidate 降级: candidate_id=%s, degraded_fields=%s",
            candidate_id, degraded_fields,
        )

    return QuantCandidate(
        candidate_id=candidate_id,
        symbol=symbol,
        sector_id=sector_id,
        as_of=as_of,
        available_at=available_at,
        expires_at=expires_at,
        research_score=research_score,
        alpha_score=alpha_score,
        core_score=core_score,
        evidence_confidence_low=ev_low,
        evidence_confidence_high=ev_high,
        valuation_risk_low=vr_low,
        valuation_risk_high=vr_high,
        catalyst_window=catalyst_window,
        source_trace=source_trace,
        degraded=bool(degraded_fields),
        degraded_fields=degraded_fields,
        # 追溯字段
        thesis_id=thesis_id,
        thesis_status=thesis_status,
        claim_id=claim_id,
        claim_status=claim_status,
        company_id=company_id,
        mapping_confidence=mapping_confidence,
        evidence_as_of=evidence_as_of,
        # 主题追溯字段（动态主题控制面）
        theme_id=theme_id,
        theme_run_id=theme_run_id,
        theme_type=theme_type,
        theme_phase=theme_phase,
    )


def adapt_research_candidates(candidates: list[Any]) -> list[QuantCandidate]:
    """批量适配 ResearchCandidate 列表。

    Args:
        candidates: ResearchCandidate 对象列表或 dict 列表。

    Returns:
        QuantCandidate 列表。
    """
    return [adapt_research_candidate(c) for c in candidates if c is not None]
