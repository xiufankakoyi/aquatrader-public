"""本地证据检索短路器。

Phase 3：在进入网络检索前先查本地 DocumentVersion/Evidence/MembershipEvidence，
命中合格证据时短路返回，避免重复网络抓取。

设计原则：
- KISS：只做查询和合格性判断，不做证据生成或状态迁移
- DRY：复用 ResearchRepository 现有查询能力，不重写存储
- 防御性：所有异常降级为"未命中"，不阻断主流程
- 不修改 Claim/Evidence 状态，只读
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
from dataclasses import dataclass, field
from typing import Any

from .contracts import Claim, EvidenceRecord, EvidenceStance, _safe_int, _safe_text

logger = logging.getLogger(__name__)


@dataclass
class LocalRetrievalResult:
    """本地检索结果。

    Attributes:
        hit: 是否命中合格本地证据。
        reason_code: 命中/未命中原因码：
            local_sufficient / local_partial / local_empty / local_query_failed
        evidence: 命中的本地证据列表（hit=False 时为空）。
        independent_sources: 独立来源数（按 lineage_id 折叠）。
        first_party_sources: 第一方来源数。
        support_count: 支持证据数。
        counter_count: 反证证据数。
        skipped_stages: 因本地命中而跳过的检索阶段列表。
    """

    hit: bool = False
    reason_code: str = "local_empty"
    evidence: list[EvidenceRecord] = field(default_factory=list)
    independent_sources: int = 0
    first_party_sources: int = 0
    support_count: int = 0
    counter_count: int = 0
    skipped_stages: list[str] = field(default_factory=list)


class LocalEvidenceRetriever:
    """本地证据检索短路器。

    在 ResearchOrchestrator.execute() 入口前查询本地是否已有合格证据，
    命中时返回 LocalRetrievalResult(hit=True)，让编排器跳过网络检索阶段。

    合格性判断与 ClaimPolicy 保持一致：
    - 至少 N 条独立来源（lineage_id 折叠）
    - 至少 1 条第一方来源（is_first_party=True）
    - 反证搜索已完成或反证证据已评估
    - 无未解决冲突

    Notes:
        - 只读：不修改 Claim/Evidence 状态
        - 防御性：任何异常降级为未命中
        - 不替代 Evidence Evaluator：命中后仍由编排器决定是否停止
    """

    # 本地短路所需的最小独立来源数
    MIN_INDEPENDENT_SOURCES: int = 2
    # 本地短路所需的最小第一方来源数
    MIN_FIRST_PARTY_SOURCES: int = 1

    def __init__(
        self,
        repository: Any,
        *,
        membership_evidence_store: Any | None = None,
        min_independent_sources: int | None = None,
        min_first_party_sources: int | None = None,
    ) -> None:
        """初始化本地检索短路器。

        Args:
            repository: ResearchRepository 实例，用于查询本地 Evidence。
            membership_evidence_store: 可选的 MembershipEvidenceStore，用于
                查询公司—主题关系是否已有本地事实证据。
            min_independent_sources: 覆盖默认最小独立来源数。
            min_first_party_sources: 覆盖默认最小第一方来源数。
        """
        self.repository = repository
        self.membership_evidence_store = membership_evidence_store
        self.min_independent_sources = max(
            1, _safe_int(min_independent_sources, self.MIN_INDEPENDENT_SOURCES)
        )
        self.min_first_party_sources = max(
            0, _safe_int(min_first_party_sources, self.MIN_FIRST_PARTY_SOURCES)
        )

    def query(self, claim: Claim) -> LocalRetrievalResult:
        """查询本地是否已有合格证据。

        Args:
            claim: 待检索的 Claim。

        Returns:
            LocalRetrievalResult：hit=True 表示本地命中可短路。
        """
        if not isinstance(claim, Claim) or not claim.claim_id:
            return LocalRetrievalResult(reason_code="local_empty")

        try:
            evidence = self.repository.list_evidence(claim.claim_id, limit=500)
        except Exception as exc:
            logger.warning(
                "本地证据查询失败 claim=%s: %s", claim.claim_id, exc
            )
            return LocalRetrievalResult(reason_code="local_query_failed")

        if not isinstance(evidence, (list, tuple, set)):
            evidence = []
        evidence = [item for item in evidence if isinstance(item, EvidenceRecord)]
        if not evidence:
            return LocalRetrievalResult(reason_code="local_empty")

        # 统计独立来源（按 lineage_id 折叠）、第一方来源、support/counter
        independent_lineages: set[str] = set()
        first_party_lineages: set[str] = set()
        support_count = 0
        counter_count = 0
        for item in evidence:
            if not item.can_affect_confidence:
                continue
            lineage_id = _safe_text(item.lineage_id)
            if lineage_id:
                independent_lineages.add(lineage_id)
                if item.is_first_party is True:
                    first_party_lineages.add(lineage_id)
            if item.stance == EvidenceStance.SUPPORT.value:
                support_count += 1
            elif item.stance == EvidenceStance.COUNTER.value:
                counter_count += 1

        independent_sources = len(independent_lineages)
        first_party_count = len(first_party_lineages)

        # 合格性判断
        sufficient = (
            independent_sources >= self.min_independent_sources
            and first_party_count >= self.min_first_party_sources
            and support_count > 0
            and claim.counter_search_completed is True
            and _safe_text(claim.conflict_status).lower() != "unresolved"
        )

        if sufficient:
            return LocalRetrievalResult(
                hit=True,
                reason_code="local_sufficient",
                evidence=evidence,
                independent_sources=independent_sources,
                first_party_sources=first_party_count,
                support_count=support_count,
                counter_count=counter_count,
                skipped_stages=["first_party", "second_hand", "web"],
            )

        # 部分命中：有证据但不足够，仍可帮助减少网络抓取
        if evidence:
            return LocalRetrievalResult(
                hit=False,
                reason_code="local_partial",
                evidence=evidence,
                independent_sources=independent_sources,
                first_party_sources=first_party_count,
                support_count=support_count,
                counter_count=counter_count,
                skipped_stages=[],
            )

        return LocalRetrievalResult(reason_code="local_empty")

    def query_membership(
        self, symbol: str, theme_id: str
    ) -> list[Any]:
        """查询本地是否已有公司—主题关系事实证据。

        用于主题成员确认场景：外部概念表/问财召回的候选在本地已有
        MembershipEvidence 时，不需要重新检索。

        Args:
            symbol: 证券代码。
            theme_id: 主题 ID。

        Returns:
            本地 MembershipEvidence 列表（可能为空）。
        """
        if self.membership_evidence_store is None:
            return []
        symbol = _safe_text(symbol)
        theme_id = _safe_text(theme_id)
        if not symbol or not theme_id:
            return []
        try:
            result = self.membership_evidence_store.list_by_symbol_theme(
                symbol=symbol, theme_id=theme_id
            )
            return list(result) if isinstance(result, (list, tuple, set)) else []
        except Exception as exc:
            logger.warning(
                "本地 MembershipEvidence 查询失败 symbol=%s theme=%s: %s",
                symbol, theme_id, exc,
            )
            return []
