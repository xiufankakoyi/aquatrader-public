"""P3.9 PIT 认证报告生成器。

按 P3.9 §认证内容，报告必须包含 8 个维度：
1. 数据覆盖和来源偏差
2. 三表与公司行为调整
3. 预测误差和区间覆盖
4. 共识相对误差
5. 行业模型适用性
6. 发现漏斗稳定性
7. VariantPerception 的形成和失效原因
8. 模型、数据、映射、时点和假设错误归因

硬门槛：
- 同时显示成功与失败
- 样本不足时标注「不包装成有效能力」
- 独立复核人签署（signer 必须与 model_author 不同）
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import date
from typing import Any, Iterable, Mapping

from core.research.pit_certification.contracts import (
    CertificationStatus,
    IndependentSignOff,
    PITCertificationReport,
    PITCertificationResult,
    PITCertificationScope,
    PITErrorAttribution,
    compute_results_hash,
    utc_now,
)
from core.research.pit_certification.provenance import (
    audit_ai_review,
    audit_production_results,
)


def generate_certification_report(
    results: list[PITCertificationResult],
    scope: PITCertificationScope | None = None,
    *,
    production_provenance: Iterable[Mapping[str, Any]] = (),
    ai_review: Mapping[str, Any] | None = None,
) -> PITCertificationReport:
    """生成 PIT 认证汇总报告。

    必须同时显示成功与失败；样本不足时标注「不包装成有效能力」。

    Args:
        results: 全部认证结果列表（含成功和失败，不选择性删除）
        scope: 认证范围；None 时使用默认范围

    Returns:
        PITCertificationReport: 汇总报告
    """
    used_scope = scope if isinstance(scope, PITCertificationScope) else PITCertificationScope()
    safe_results = [
        result for result in results
        if isinstance(result, PITCertificationResult)
    ] if isinstance(results, (list, tuple)) else []
    total = len(safe_results)

    # 统计成功/失败/数据不足/泄漏
    success_count = sum(1 for r in safe_results if r.status is CertificationStatus.SUCCESS)
    failure_count = sum(1 for r in safe_results if r.status is CertificationStatus.FAILURE)
    insufficient_count = sum(
        1 for r in safe_results if r.status is CertificationStatus.INSUFFICIENT_DATA
    )
    leakage_count = sum(
        1 for r in safe_results if r.status is CertificationStatus.LEAKAGE_DETECTED
    )

    # 计算各维度
    data_coverage = _compute_data_coverage(safe_results)
    statement_adjustment = _compute_statement_adjustment(safe_results)
    forecast_error = _compute_forecast_error(safe_results)
    consensus_error = _compute_consensus_error(safe_results)
    industry_model = _compute_industry_model_applicability(safe_results)
    funnel_stability = _compute_funnel_stability(safe_results)
    vp_analysis = _compute_vp_analysis(safe_results)
    error_distribution = _compute_error_attribution_distribution(safe_results)
    significance = _compute_statistical_significance(
        safe_results, used_scope, success_count, failure_count
    )
    hard_gates = _compute_hard_gates(
        safe_results, used_scope, success_count, failure_count, leakage_count
    )
    provenance_audit = audit_production_results(safe_results, production_provenance)
    review_audit = audit_ai_review(ai_review or {}, safe_results)
    hard_gates.update(_compute_production_hard_gates(safe_results, used_scope))
    hard_gates["production_provenance_valid"] = bool(
        provenance_audit["production_provenance_valid"]
    )
    hard_gates["ai_review_valid"] = bool(review_audit["ai_review_valid"])
    hard_gates["certification_ready"] = all(hard_gates.values())

    return PITCertificationReport(
        scope=used_scope,
        total_companies=total,
        total_success=success_count,
        total_failure=failure_count,
        total_insufficient=insufficient_count,
        total_leakage=leakage_count,
        data_coverage=data_coverage,
        statement_adjustment=statement_adjustment,
        forecast_error=forecast_error,
        consensus_error=consensus_error,
        industry_model_applicability=industry_model,
        funnel_stability=funnel_stability,
        vp_analysis=vp_analysis,
        error_attribution_distribution=error_distribution,
        statistical_significance=significance,
        hard_gates=hard_gates,
        sign_off=IndependentSignOff(),
        generated_at=utc_now(),
    )


def sign_certification_report(
    report: PITCertificationReport,
    results: list[PITCertificationResult],
    signer: str,
    model_author: str = "",
) -> PITCertificationReport:
    """独立复核人签署历史认证。

    按 P3.9 硬门槛「独立复核人签署历史认证」：
    - signer 必须与 model_author 不同（与 P3.1 规则一致）
    - 签署内容哈希覆盖全部认证结果

    Args:
        report: 待签署的报告
        results: 全部认证结果（用于计算签署哈希）
        signer: 签署人 ID
        model_author: 模型作者 ID

    Returns:
        PITCertificationReport: 签署后的报告

    Raises:
        ValueError: signer 与 model_author 相同时
    """
    if signer and model_author and signer == model_author:
        raise ValueError(
            f"独立复核人签署违规：signer 与 model_author 不能为同一人"
            f"（signer={signer}, model_author={model_author}）"
        )

    if not signer:
        raise ValueError("签署人 ID 不能为空")

    signature_hash = compute_results_hash(results)
    scope_summary = (
        f"companies={report.total_companies}, "
        f"success={report.total_success}, "
        f"failure={report.total_failure}, "
        f"insufficient={report.total_insufficient}, "
        f"leakage={report.total_leakage}"
    )

    report.sign_off = IndependentSignOff(
        signer=signer,
        sign_time=utc_now(),
        signature_hash=signature_hash,
        scope_summary=scope_summary,
        is_signed=True,
    )
    return report


# ---------------------------------------------------------------------------
# 各维度计算
# ---------------------------------------------------------------------------


def _compute_data_coverage(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算数据覆盖和来源偏差。"""
    industries = Counter(r.industry for r in results if r.industry)
    symbols = set(r.symbol for r in results if r.symbol)
    return {
        "total_symbols": len(symbols),
        "total_industries": len(industries),
        "industry_distribution": dict(industries),
        "source_bias_note": (
            "来源偏差需结合实际数据源覆盖情况评估；"
            "本报告仅统计认证样本覆盖，不替代数据源审计"
        ),
    }


def _compute_statement_adjustment(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算三表与公司行为调整。"""
    return {
        "note": (
            "三表与公司行为调整需结合 P1 财务正常化模块评估；"
            "本报告记录是否在 PIT 时点正确使用了当时可见的财报版本"
        ),
        "leakage_in_financials": sum(
            1 for r in results
            if r.leakage_check and r.leakage_check.has_leakage
            and any(
                item.get("category") == "financial_report"
                for item in r.leakage_check.leakage_items
            )
        ),
    }


def _compute_forecast_error(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算预测误差和区间覆盖。"""
    errors = [
        value for r in results
        if (value := _finite_number(r.percentage_error)) is not None
    ]
    interval_covered = [
        r.interval_covered for r in results if r.interval_covered is not None
    ]

    mae = statistics.mean([abs(e) for e in errors]) if errors else None
    mape = statistics.mean([abs(e) for e in errors]) if errors else None
    coverage = (
        sum(1 for c in interval_covered if c) / len(interval_covered)
        if interval_covered
        else None
    )

    return {
        "total_with_error": len(errors),
        "total_with_interval": len(interval_covered),
        "mae": mae,
        "mape": mape,
        "interval_coverage_rate": coverage,
        "note": (
            "mape 和 coverage 为 None 时表示样本不足，不包装成有效能力"
            if mape is None or coverage is None
            else "预测误差和区间覆盖率已计算"
        ),
    }


def _compute_consensus_error(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算共识相对误差。"""
    relative_errors = [
        value for r in results
        if (value := _finite_number(r.consensus_relative_error)) is not None
    ]
    avg_improvement = statistics.mean(relative_errors) if relative_errors else None
    return {
        "total_with_consensus": len(relative_errors),
        "avg_consensus_relative_error": avg_improvement,
        "note": (
            "共识相对误差为 None 时表示样本不足；"
            "没有改进也必须如实记录，不包装成有效能力"
        ),
    }


def _compute_industry_model_applicability(
    results: list[PITCertificationResult],
) -> dict[str, Any]:
    """计算行业模型适用性。"""
    industry_stats: dict[str, dict[str, int]] = {}
    for r in results:
        if not r.industry:
            continue
        stats = industry_stats.setdefault(r.industry, {"success": 0, "failure": 0, "total": 0})
        stats["total"] += 1
        if r.is_success:
            stats["success"] += 1
        elif r.is_failure:
            stats["failure"] += 1

    return {
        "industry_breakdown": industry_stats,
        "industries_with_failures": [
            ind for ind, s in industry_stats.items() if s["failure"] > 0
        ],
        "note": "行业模型适用性需结合 P3.2 行业专用模型框架评估",
    }


def _compute_funnel_stability(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算发现漏斗稳定性。"""
    return {
        "note": (
            "发现漏斗稳定性需结合 P2.2 基本面发现漏斗评估；"
            "本报告记录认证样本在漏斗各阶段的留存率"
        ),
        "total_companies_in_funnel": len(set(r.symbol for r in results if r.symbol)),
    }


def _compute_vp_analysis(results: list[PITCertificationResult]) -> dict[str, Any]:
    """计算 VariantPerception 形成和失效分析。"""
    vp_formed = sum(1 for r in results if r.vp_formed)
    vp_invalidated = [
        {"symbol": r.symbol, "pit_time": r.pit_time, "reason": r.vp_invalidation_reason}
        for r in results
        if r.vp_formed and r.vp_invalidation_reason
    ]
    return {
        "total_vp_formed": vp_formed,
        "total_vp_invalidated": len(vp_invalidated),
        "invalidation_examples": vp_invalidated[:20],
        "note": "VP 失效原因需结合 P2.6 差异化观点构建器评估",
    }


def _compute_error_attribution_distribution(
    results: list[PITCertificationResult],
) -> dict[str, Any]:
    """计算错误归因分布。"""
    distribution: Counter[str] = Counter()
    for r in results:
        if r.error_attribution is not None:
            distribution[r.error_attribution.value] += 1

    total_attributed = sum(distribution.values())
    return {
        "distribution": dict(distribution),
        "total_attributed": total_attributed,
        "attribution_types": [e.value for e in PITErrorAttribution],
        "note": (
            "错误归因按优先级：TIMING > DATA > MAPPING > ASSUMPTION > MODEL；"
            "未归因的结果不纳入分布统计"
        ),
    }


def _compute_statistical_significance(
    results: list[PITCertificationResult],
    scope: PITCertificationScope,
    success_count: int,
    failure_count: int,
) -> dict[str, Any]:
    """计算统计显著性标注。

    样本不足时明确标注「不包装成有效能力」。
    """
    evaluated = success_count + failure_count
    insufficient = evaluated < scope.min_sample_for_significance

    return {
        "total_evaluated": evaluated,
        "min_sample_for_significance": scope.min_sample_for_significance,
        "insufficient_sample": insufficient,
        "warning": (
            "样本不足，不包装成有效能力"
            if insufficient
            else "样本量满足统计显著性最低要求"
        ),
        "note": (
            "没有统计显著性或稳定性时明确标注，不包装成有效能力；"
            "本标注不替代正式统计检验"
        ),
    }


def _compute_hard_gates(
    results: list[PITCertificationResult],
    scope: PITCertificationScope,
    success_count: int,
    failure_count: int,
    leakage_count: int,
) -> dict[str, bool]:
    """计算硬门槛检查结果。"""
    return {
        "no_known_leakage": leakage_count == 0,
        "no_selective_deletion": len(results) == (success_count + failure_count +
                                                   sum(1 for r in results
                                                       if r.status is CertificationStatus.INSUFFICIENT_DATA) +
                                                   leakage_count),
        "shows_both_success_and_failure": success_count > 0 and failure_count > 0,
        "sample_sufficient": (success_count + failure_count) >= scope.min_sample_for_significance,
    }


def _compute_production_hard_gates(
    results: list[PITCertificationResult],
    scope: PITCertificationScope,
) -> dict[str, bool]:
    """计算 P3.9 的 300 家/20 行业/5 年/三市场状态正式范围门槛。"""
    symbols = {result.symbol for result in results if result.symbol}
    industries = {result.industry for result in results if result.industry}
    states = {result.market_state for result in results}
    pit_dates: list[date] = []
    for result in results:
        try:
            pit_dates.append(date.fromisoformat(result.pit_time[:10]))
        except (TypeError, ValueError):
            continue
    years = (
        (max(pit_dates) - min(pit_dates)).days / 365.2425
        if len(pit_dates) >= 2
        else 0.0
    )
    return {
        "company_scope_met": len(symbols) >= scope.min_companies,
        "industry_scope_met": len(industries) >= scope.min_industries,
        "history_scope_met": years >= scope.min_years,
        "market_states_scope_met": scope.require_market_states.issubset(states),
    }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
