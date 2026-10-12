"""来源治理策略：采集前检查 robots、许可、限流和可用状态。"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import SourceTrace, _safe_text


@dataclass
class SourceDecision:
    allowed: bool
    reason: str
    reliability_prior: float


class SourceGovernance:
    BLOCKED = {"blocked", "forbidden", "denied"}

    def evaluate(self, source: SourceTrace) -> SourceDecision:
        if not isinstance(source, SourceTrace):
            return SourceDecision(False, "来源记录无效", 0.0)
        availability = _safe_text(source.availability_status).lower()
        robots = _safe_text(source.robots_policy).lower()
        license_policy = _safe_text(source.license_policy).lower()
        if availability in {"failed", "disabled", "unavailable", "offline", "error"}:
            return SourceDecision(False, "来源不可用", 0.0)
        if robots in self.BLOCKED:
            return SourceDecision(False, "robots 策略禁止采集", 0.0)
        if license_policy in self.BLOCKED:
            return SourceDecision(False, "许可策略禁止采集", 0.0)
        priors = {"official": 0.85, "announcement": 0.90, "financial_report": 0.85, "news": 0.60, "forum": 0.30, "unknown": 0.40}
        source_type = _safe_text(source.source_type, "unknown").lower()
        return SourceDecision(True, "允许采集，仍需文档级验证", priors.get(source_type, 0.40))

