"""CandidateGraph 状态枚举与升级规则。

状态机:
  升级路径:DISCOVERED → CANDIDATE → SUPPORTED → ACTIVE → TRUSTED
  降级路径:反证→CONFLICTED,强反证→REJECTED,长期无证据→DORMANT
  剪除:generic_node/duplicate_node/low_value_edge→PRUNED(不参与召回)

查询视图:
  research_view:仅 ACTIVE/TRUSTED(正式研究输出)
  exploration_view:除 PRUNED 外所有状态(探索/召回)
"""
from __future__ import annotations

from enum import Enum


class CandidateStatus(str, Enum):
    """候选节点/边的生命周期状态。"""

    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    ACTIVE = "ACTIVE"
    TRUSTED = "TRUSTED"
    CONFLICTED = "CONFLICTED"
    REJECTED = "REJECTED"
    DORMANT = "DORMANT"
    PRUNED = "PRUNED"


UPGRADE_PATHS: dict[CandidateStatus, CandidateStatus] = {
    CandidateStatus.DISCOVERED: CandidateStatus.CANDIDATE,
    CandidateStatus.CANDIDATE: CandidateStatus.SUPPORTED,
    CandidateStatus.SUPPORTED: CandidateStatus.ACTIVE,
    CandidateStatus.ACTIVE: CandidateStatus.TRUSTED,
}

RESEARCH_VISIBLE_STATUSES: frozenset[str] = frozenset({
    CandidateStatus.ACTIVE.value,
    CandidateStatus.TRUSTED.value,
})

EXPLORATION_VISIBLE_STATUSES: frozenset[str] = frozenset({
    CandidateStatus.DISCOVERED.value,
    CandidateStatus.CANDIDATE.value,
    CandidateStatus.SUPPORTED.value,
    CandidateStatus.ACTIVE.value,
    CandidateStatus.TRUSTED.value,
    CandidateStatus.CONFLICTED.value,
    CandidateStatus.DORMANT.value,
})

PRUNED_STATUSES: frozenset[str] = frozenset({
    CandidateStatus.PRUNED.value,
    CandidateStatus.REJECTED.value,
})


def next_status(current: str) -> str:
    """返回下一个升级状态,已是终态则返回原状态。"""
    try:
        cs = CandidateStatus(current)
    except ValueError:
        return current
    return UPGRADE_PATHS.get(cs, cs).value


def is_research_visible(status: str) -> bool:
    """是否在 research_view 中可见。"""
    return status in RESEARCH_VISIBLE_STATUSES


def is_exploration_visible(status: str) -> bool:
    """是否在 exploration_view 中可见(PRUNED/REJECTED 不可见)。"""
    return status in EXPLORATION_VISIBLE_STATUSES


EVIDENCE_THRESHOLDS: dict[str, int] = {
    # 升级到下一状态所需的 support 证据数
    # 调整说明：原阈值 1/3/5/8 在真实证据稀疏分布下难以触达 ACTIVE
    # 实测 68 条证据分散到 N 节点，平均每节点 <2 条 → research_view 永远为 0
    # 现降为 1/2/3/5，使核心节点在合理证据量下能进入 research_view
    CandidateStatus.DISCOVERED.value: 1,   # →CANDIDATE
    CandidateStatus.CANDIDATE.value: 2,    # →SUPPORTED
    CandidateStatus.SUPPORTED.value: 3,    # →ACTIVE
    CandidateStatus.ACTIVE.value: 5,       # →TRUSTED
}

SOURCE_QUALITY_BOOST: dict[str, float] = {
    "announcement": 1.5,
    "official": 1.4,
    "financial_report": 1.5,
    "company_website": 1.3,
    "technical_doc": 1.3,
    "local_cache": 1.3,
    "research_report": 1.2,
    "industry_report": 1.1,
    "news": 1.0,
    "local_vector_memory": 0.9,
    "investor_interaction": 0.9,
    "search_result": 1.0,
    "crawler": 0.8,
    "api": 0.7,
    "blog": 0.5,
    "forum": 0.4,
    "unknown": 0.5,
}
