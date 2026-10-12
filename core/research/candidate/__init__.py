"""ResearchCandidate 最终输出层模块。

将产业链事实、盈利影响、市场预期差和投资观点聚合成统一候选对象。
三类分数严格分开：research_score / alpha_score / core_score。
不输出买卖、仓位、止盈止损建议。
"""
from .contracts import (
    CandidateReport,
    CandidateStatus,
    ResearchCandidate,
    ScoreBreakdown,
    ScoreComputability,
)
from .engine import CandidateBuilder, CandidateReportBuilder
from .repository import CandidateRepository

__all__ = [
    "CandidateBuilder",
    "CandidateReport",
    "CandidateReportBuilder",
    "CandidateRepository",
    "CandidateStatus",
    "ResearchCandidate",
    "ScoreBreakdown",
    "ScoreComputability",
]
