"""主题成员召回与事实确认分离模块。

外部概念表（问财/东财/同花顺）召回的成员只作为 ThemeMemberCandidate 候选，
需经 MembershipEvidence 事实证据确认后方可进入正式视图。
已确认 MembershipEvidence 必须来自可定位材料（公告/互动/官网/研报）。
"""
from .confirmator import MembershipConfirmator
from .contracts import (
    CONFIRMED_CONFIDENCE,
    GROUNDING_CONFLICTED,
    GROUNDING_GROUNDED,
    GROUNDING_UNVERIFIED,
    MembershipEvidence,
    RECALL_CONFIDENCE,
    STANCE_COUNTER,
    STANCE_NEUTRAL,
    STANCE_SUPPORT,
    ThemeMemberCandidate,
)
from .repository import MembershipEvidenceStore

__all__ = [
    "MembershipConfirmator",
    "MembershipEvidence",
    "MembershipEvidenceStore",
    "ThemeMemberCandidate",
    "RECALL_CONFIDENCE",
    "CONFIRMED_CONFIDENCE",
    "GROUNDING_GROUNDED",
    "GROUNDING_UNVERIFIED",
    "GROUNDING_CONFLICTED",
    "STANCE_SUPPORT",
    "STANCE_COUNTER",
    "STANCE_NEUTRAL",
]
