"""召回候选确认器。

将外部概念表召回的 ThemeMemberCandidate 与 MembershipEvidence 事实证据匹配，
确认后修改候选的 membership_status 为 grounded；未确认的候选保持 recall_only，
只进入探索视图。

不使用 0.3/0.5 硬编码置信度进行生产门禁判断，confidence 字段仅作为 shadow 分数。
"""
from __future__ import annotations

from .contracts import (
    GROUNDING_GROUNDED,
    MEMBERSHIP_STATUS_GROUNDED,
    MEMBERSHIP_STATUS_RECALL_ONLY,
    MembershipEvidence,
    STANCE_SUPPORT,
    ThemeMemberCandidate,
    utc_now,
)
from .repository import MembershipEvidenceStore, _is_expired


class MembershipConfirmator:
    """召回候选确认器。

    通过 MembershipEvidenceStore 查询事实证据，确认召回候选是否成立。
    确认规则（不使用硬编码置信度，使用 grounding_status + stance + 有效期）：
    - 候选必须在 MembershipEvidenceStore 中有匹配的 grounded + support 证据
    - 证据的 valid_until 未过期或为空（UNKNOWN）
    - 确认后候选的 membership_status 设为 grounded
    - 未确认的候选保持 membership_status = recall_only，只进入探索视图
    - confidence 字段仅作为 shadow 分数，不用于门禁判断
    """

    def __init__(self, store: MembershipEvidenceStore) -> None:
        """初始化确认器。

        Args:
            store: 事实证据存储实例。
        """
        self._store = store

    def confirm_candidate(
        self, candidate: ThemeMemberCandidate, evidence: MembershipEvidence
    ) -> bool:
        """用事实证据确认召回候选。

        确认条件：
        1. 候选与证据的 symbol、theme_id 匹配；
        2. 证据 grounding_status 为 grounded；
        3. 证据 stance 为 support；
        4. 证据 valid_until 未过期或为空（UNKNOWN）。

        确认成功时将候选的 membership_status 设为 grounded。
        confidence 字段不做生产门禁判断。

        Args:
            candidate: 待确认的召回候选。
            evidence: 事实证据。

        Returns:
            True 表示确认成功；False 表示未确认。
        """
        if not isinstance(candidate, ThemeMemberCandidate) or not isinstance(evidence, MembershipEvidence):
            return False
        if not self._is_evidence_matching(candidate, evidence):
            return False
        if evidence.grounding_status != GROUNDING_GROUNDED:
            return False
        if evidence.stance != STANCE_SUPPORT:
            return False
        if _is_expired(evidence.valid_until, utc_now()):
            return False
        # 确认成功，设置 membership_status
        candidate.membership_status = MEMBERSHIP_STATUS_GROUNDED
        return True

    def filter_confirmed(
        self, candidates: list[ThemeMemberCandidate]
    ) -> list[ThemeMemberCandidate]:
        """过滤出已确认的候选。

        遍历候选列表，通过 MembershipEvidenceStore 查询匹配的 grounded + support 证据。
        确认的候选 membership_status 设为 grounded 并加入返回列表；
        未确认的候选保持 membership_status = recall_only，不进入返回列表
        （即只进入探索视图）。

        不修改候选对象的 confidence（shadow 分数），只修改 membership_status。

        Args:
            candidates: 召回候选列表。

        Returns:
            已确认的候选列表（原对象引用，membership_status 已更新）。
        """
        confirmed: list[ThemeMemberCandidate] = []
        if not isinstance(candidates, (list, tuple)):
            return []
        for candidate in candidates:
            if not isinstance(candidate, ThemeMemberCandidate):
                continue
            if self._store.is_member_confirmed(candidate.symbol, candidate.theme_id):
                candidate.membership_status = MEMBERSHIP_STATUS_GROUNDED
                confirmed.append(candidate)
        return confirmed

    @staticmethod
    def _is_evidence_matching(
        candidate: ThemeMemberCandidate, evidence: MembershipEvidence
    ) -> bool:
        """判断证据是否与候选匹配（symbol 和 theme_id 一致）。

        Args:
            candidate: 召回候选。
            evidence: 事实证据。

        Returns:
            True 表示匹配。
        """
        return (
            candidate.symbol == evidence.symbol
            and candidate.theme_id == evidence.theme_id
        )
