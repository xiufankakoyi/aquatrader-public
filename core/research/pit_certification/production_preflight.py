"""P3-Engineering 正式运行前置检查。"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.research.pit_certification.contracts import (
    PITCertificationResult,
    PITCertificationScope,
)
from core.research.pit_certification.report_generator import (
    generate_certification_report,
)


def evaluate_preflight(
    results: list[PITCertificationResult],
    provenance: Iterable[Mapping[str, Any]],
    ai_review: Mapping[str, Any],
    *,
    active_model_count: int,
    active_policy_count: int,
    scope: PITCertificationScope | None = None,
) -> dict[str, Any]:
    """汇总正式认证门槛；任何缺口都保持 blocked，不自动降级范围。"""
    try:
        active_model_count = max(0, int(active_model_count))
    except (TypeError, ValueError, OverflowError):
        active_model_count = 0
    try:
        active_policy_count = max(0, int(active_policy_count))
    except (TypeError, ValueError, OverflowError):
        active_policy_count = 0
    report = generate_certification_report(
        results,
        scope,
        production_provenance=provenance,
        ai_review=ai_review,
    )
    blockers = [
        gate for gate, passed in report.hard_gates.items()
        if not passed and gate != "certification_ready"
    ]
    if active_model_count < 1:
        blockers.append("no_active_production_model")
    if active_policy_count < 1:
        blockers.append("no_active_research_policy")
    return {
        "status": "ready" if not blockers else "blocked",
        "active_model_count": active_model_count,
        "active_policy_count": active_policy_count,
        "blockers": blockers,
        "hard_gates": report.hard_gates,
        "report": report.to_dict(),
    }
