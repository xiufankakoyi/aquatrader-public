"""Stage 3 completion regressions for first-party routing and evidence gates."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from core.research.control_plane.contracts import Claim, ResearchTask
from core.research.control_plane.first_party_retriever import (
    CompanyIRSiteProvider,
    FirstPartyRetrievalResult,
    SseInteractiveProvider,
    SzseInteractiveProvider,
)
from core.research.control_plane.local_retrieval import LocalRetrievalResult
from core.research.control_plane.orchestrator import ResearchOrchestrator
from core.research.control_plane.perilla_adapter import SearchDiscoveryRetriever
from core.research.control_plane.production_retrieval import CninfoPdfRetriever
from core.research.control_plane.repository import ResearchRepository
from core.research.perilla.discovery.models import SourceDocument


def _repo(tmp_path: Path) -> ResearchRepository:
    return ResearchRepository(tmp_path / "research.db")


def _claim() -> Claim:
    return Claim(
        claim_id="claim_stage3_completion",
        subject_id="600000",
        subject_name="测试公司",
        object_name="测试主题",
    )


class _InteractionProvider:
    name = "akshare"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def is_available(self) -> bool:
        return True

    def get_investor_interactions(self, code: str, count: int = 10) -> list[dict]:
        return self.rows[:count]


def test_sse_adapter_uses_verified_akshare_interactions() -> None:
    provider = _InteractionProvider([
        {
            "code": "600000",
            "market": "sse",
            "question_id": "q1",
            "question": "公司是否开展测试主题业务？",
            "answer": "请以公告披露为准。",
            "date": "2026-07-10",
            "url": "https://sns.sseinfo.com/company.do?uid=600000",
            "source": "sseinfo",
        }
    ])

    documents = SseInteractiveProvider(market_provider=provider).search(
        "600000 测试主题", limit=5
    )

    assert len(documents) == 1
    assert documents[0].source_type == "investor_interaction"
    assert "公司是否开展测试主题业务" in documents[0].content_text
    assert documents[0].metadata["market"] == "sse"


def test_szse_adapter_uses_verified_cninfo_irm_interactions() -> None:
    provider = _InteractionProvider([
        {
            "code": "000001",
            "market": "cninfo_irm",
            "question_id": "q2",
            "question": "公司是否开展测试主题业务？",
            "answer": "暂无相关披露。",
            "date": "2026-07-10",
            "url": "https://irm.cninfo.com.cn/ircs/question/questionDetail?questionId=q2",
            "source": "cninfo_irm",
        }
    ])

    documents = SzseInteractiveProvider(market_provider=provider).search(
        "000001 测试主题", limit=5
    )

    assert len(documents) == 1
    assert documents[0].metadata["market"] == "cninfo_irm"
    assert documents[0].metadata["source_backend"] == "cninfo_irm"


def test_company_ir_provider_only_crawls_explicit_urls() -> None:
    crawler = MagicMock()
    crawler.crawl_url.return_value = SourceDocument(
        document_id="ir_doc",
        source_type="official_site",
        url="https://example.com/ir",
        title="投资者关系",
        content_text="公司持续开展测试主题相关研发。",
    )
    provider = CompanyIRSiteProvider(crawler=crawler)

    assert provider.search("600000 测试公司", limit=5) == []
    crawler.crawl_url.assert_not_called()

    documents = provider.search(
        "600000 测试公司",
        limit=5,
        context={"company_ir_urls": ["https://example.com/ir"]},
    )

    assert len(documents) == 1
    crawler.crawl_url.assert_called_once_with("https://example.com/ir")


def test_first_party_boundaries_skip_bad_limits_and_documents() -> None:
    provider = SseInteractiveProvider(market_provider=None, request_timeout="bad", rate_limit_per_minute="bad")
    assert provider.search(None, limit="bad") == []
    assert FirstPartyRetrievalResult().documents == []


def test_cninfo_pdf_retriever_accepts_first_party_tier(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    claim = _claim()
    repository.save_claim(claim)

    class Provider:
        def search_announcements(self, query: str, count: int) -> list[dict]:
            return [{
                "title": "测试主题公告",
                "date": "2026-07-10",
                "code": "600000",
                "pdf_url": "https://static.cninfo.com.cn/test.pdf",
                "targeted_search": True,
            }]

        def get_announcements(self, code: str, count: int) -> list[dict]:
            return []

        def get_periodic_reports(self, code: str, count: int) -> list[dict]:
            return []

    retriever = CninfoPdfRetriever(repository, provider=Provider())
    retriever._document_from_pdf = lambda row, current_claim, terms: SourceDocument(  # type: ignore[method-assign]
        document_id="cninfo_doc",
        source_type="announcement",
        url=row["pdf_url"],
        title=row["title"],
        content_text="公告主体：测试公司（600000）。公司开展测试主题相关业务。",
    )

    documents = retriever.search("600000 测试主题", tier="first_party", limit=2)

    assert len(documents) == 1


def test_search_discovery_marks_results_as_snippet_only() -> None:
    discovery = MagicMock()
    discovery.search.return_value = [SourceDocument(
        document_id="search_doc",
        source_type="search_result",
        url="https://example.com/article",
        title="搜索结果",
        content_text="搜索摘要",
    )]

    documents = SearchDiscoveryRetriever(discovery).search(
        "测试主题", tier="web", limit=5
    )

    assert documents[0].metadata["snippet_only"] is True


def test_orchestrator_calls_first_party_before_legacy_retriever(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    claim = _claim()
    repository.save_claim(claim)
    events: list[str] = []

    local = MagicMock()
    local.query.return_value = LocalRetrievalResult(reason_code="local_empty")
    first_party = MagicMock()
    first_party.search.side_effect = lambda *args, **kwargs: (
        events.append("first_party") or FirstPartyRetrievalResult()
    )
    retriever = MagicMock()
    retriever.search.side_effect = lambda *args, **kwargs: (
        events.append("legacy") or []
    )
    extractor = MagicMock()
    extractor.extract.return_value = []

    summary = ResearchOrchestrator(
        repository,
        retriever,
        extractor,
        local_retriever=local,
        first_party_retriever=first_party,
    ).execute(ResearchTask(target_claim_id=claim.claim_id))

    assert "first_party" in events
    assert events.index("first_party") < events.index("legacy")
    assert "first_party" in summary.completed_stages


def test_orchestrator_rejects_snippets_and_market_documents(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    claim = _claim()
    repository.save_claim(claim)
    retriever = MagicMock()
    retriever.search.return_value = [
        SourceDocument(
            document_id="snippet",
            source_type="search_result",
            url="https://example.com/snippet",
            content_text="公司开展测试主题。",
            metadata={"snippet_only": True},
        ),
        SourceDocument(
            document_id="kline",
            source_type="kline",
            url="https://market.example/kline",
            content_text="价格和成交量",
            metadata={"data_domain": "market"},
        ),
    ]
    extractor = MagicMock()
    extractor.extract.return_value = []

    summary = ResearchOrchestrator(
        repository, retriever, extractor
    ).execute(ResearchTask(target_claim_id=claim.claim_id))

    assert extractor.extract.call_count == 0
    assert summary.market_documents_ignored >= 1
    assert repository.list_evidence(claim.claim_id) == []
