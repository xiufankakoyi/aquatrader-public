"""Production retrieval regression tests without external network access."""
from __future__ import annotations

from core.research.control_plane.contracts import Claim, EvidenceRecord
from core.research.control_plane.perilla_adapter import GroundedEvidenceExtractor
from core.research.control_plane.maintenance import invalidate_ambiguous_entity_evidence
from core.research.control_plane.policies import ClaimPolicy
from core.research.control_plane.production_retrieval import (
    CninfoPdfRetriever,
    ProductionResearchRetriever,
)
from core.research.control_plane.repository import ResearchRepository
from core.research.perilla.discovery.models import SourceDocument
from infrastructure.data_providers.research.cninfo_provider import CninfoProvider


def test_cninfo_fulltext_search_normalizes_and_filters_security_code(monkeypatch) -> None:
    provider = CninfoProvider()
    monkeypatch.setattr(
        provider,
        "_search_announcements",
        lambda code, page_size: [
            {
                "secCode": "600584",
                "secName": "长电科技",
                "announcementTitle": "<em>先进封装</em>项目公告",
                "announcementTime": 1783728000000,
                "adjunctUrl": "finalpage/test.PDF",
            },
            {"secCode": "000001", "announcementTitle": "无关公告"},
        ],
    )

    rows = provider.search_announcements("600584 先进封装", count=5)

    assert len(rows) == 1
    assert rows[0]["code"] == "600584"
    assert rows[0]["title"] == "先进封装项目公告"
    assert rows[0]["targeted_search"] is True


def test_official_retriever_targets_query_claim_and_bounds_pdf_attempts(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    repository.save_claim(Claim(
        claim_id="old_theme",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="旧主题",
    ))
    repository.save_claim(Claim(
        claim_id="advanced_packaging",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    ))

    class Provider:
        def search_announcements(self, query, count):
            return [{
                "title": "先进封装公告",
                "date": "2026-07-10",
                "code": "600584",
                "pdf_url": f"https://static.cninfo.com.cn/{query[-4:]}.pdf",
                "targeted_search": True,
            }]

        def get_announcements(self, code, count):
            return []

        def get_periodic_reports(self, code, count):
            return []

    retriever = CninfoPdfRetriever(
        repository,
        provider=Provider(),
        max_pdf_attempts=1,
        cache_dir=tmp_path / "cache",
    )
    seen = []

    def fake_document(row, claim, terms):
        seen.append((row, claim.claim_id, terms))
        return SourceDocument(
            document_id="official",
            source_type="announcement",
            url=row["pdf_url"],
            title=row["title"],
            content_text="公告主体：长电科技（600584）。公司从事先进封装技术研发。",
        )

    retriever._document_from_pdf = fake_document  # type: ignore[method-assign]
    rows = retriever.search("600584 先进封装 业务", tier="high", limit=4)

    assert len(rows) == 1
    assert len(seen) == 1
    assert seen[0][1] == "advanced_packaging"


def test_official_retriever_accepts_discovery_terms_without_persisted_claim(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    calls = {"search": 0, "announcements": 0}

    class Provider:
        def search_announcements(self, query, count):
            calls["search"] += 1
            return []

        def get_announcements(self, code, count):
            calls["announcements"] += 1
            return []

        def get_periodic_reports(self, code, count):
            return [{"title": "测试公司年报", "date": "2026-04-01", "code": code, "pdf_url": "https://example.com/report.pdf"}]

    retriever = CninfoPdfRetriever(repository, provider=Provider(), max_pdf_attempts=1)
    seen = []
    retriever._document_from_pdf = lambda row, claim, terms: (  # type: ignore[method-assign]
        seen.append((claim.subject_name, terms))
        or SourceDocument(document_id="report", source_type="announcement", url=row["pdf_url"], title=row["title"], content_text="body")
    )

    documents = retriever.search(
        "000001 测试公司 产品 客户", tier="first_party", limit=2,
        context={"symbol": "000001.SZ", "company_name": "测试公司", "discovery_terms": ["产品", "客户"], "periodic_only": True},
    )

    assert len(documents) == 1
    assert seen == [("测试公司", ["产品", "客户"])]
    assert calls == {"search": 0, "announcements": 0}


def test_cninfo_retriever_ranks_full_periodic_report_before_notice() -> None:
    rows = CninfoPdfRetriever._dedupe_and_rank([
        {"title": "2026年第一季度报告披露的提示性公告", "date": "2026-04-17", "pdf_url": "hint", "report_type": "quarterly_report"},
        {"title": "2026年一季度报告", "date": "2026-04-17", "pdf_url": "full", "report_type": "quarterly_report"},
        {"title": "关于变更2026年第一季度报告预约披露时间的公告", "date": "2026-04-15", "pdf_url": "schedule", "report_type": "quarterly_report"},
    ])

    assert rows[0]["pdf_url"] == "full"


def test_cninfo_excerpt_cache_isolated_by_research_terms() -> None:
    assert CninfoPdfRetriever._document_cache_key(
        "https://example.com/report.pdf", ["产能"]
    ) != CninfoPdfRetriever._document_cache_key(
        "https://example.com/report.pdf", ["年度工作计划"]
    )


def test_cninfo_pdf_loader_rejects_untrusted_hosts(tmp_path) -> None:
    retriever = CninfoPdfRetriever(
        ResearchRepository(tmp_path / "research.db"),
        provider=object(),
        cache_dir=tmp_path / "cache",
    )
    try:
        retriever._load_pdf("https://example.com/report.pdf")
    except ValueError as exc:
        assert "allowed HTTPS host" in str(exc)
    else:
        raise AssertionError("untrusted PDF host must be rejected")


def test_official_retriever_skips_malformed_provider_rows(tmp_path) -> None:
    calls: list[str] = []

    class Provider:
        def search_announcements(self, query, count):
            calls.append("search")
            return ({"title": "测试公告", "pdf_url": "https://example.com/a.pdf"}, "bad")

        def get_announcements(self, code, count):
            return {"not": "a list"}

        def get_periodic_reports(self, code, count):
            return [{"title": "空链接", "pdf_url": ""}]

    retriever = CninfoPdfRetriever(
        object(), provider=Provider(), max_pdf_attempts=1, cache_dir=tmp_path / "cache"
    )
    retriever._document_from_pdf = lambda row, claim, terms: SourceDocument(  # type: ignore[method-assign]
        document_id="doc",
        source_type="announcement",
        url=row["pdf_url"],
        title=row["title"],
        content_text="body",
    )

    documents = retriever.search(
        "000001 测试主题",
        tier="FIRST_PARTY",
        limit=2,
        context={"discovery_terms": ["测试主题", 123], "periodic_only": "false"},
    )

    assert [document.document_id for document in documents] == ["doc"]
    assert calls == ["search"]


def test_legacy_retriever_falls_back_when_official_or_web_fails() -> None:
    class Official:
        def search(self, *args, **kwargs):
            raise RuntimeError("official unavailable")

    class Web:
        def search(self, *args, **kwargs):
            raise RuntimeError("web unavailable")

    assert ProductionResearchRetriever(Official(), Web()).search(
        "query", tier="web", limit=2
    ) == []


def test_cninfo_symbol_scoped_document_can_ground_relationship(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim1",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    document = SourceDocument(
        document_id="cninfo_doc",
        source_type="announcement",
        url="https://static.cninfo.com.cn/finalpage/report.PDF",
        title="长电科技年度报告",
        published_at="2026-04-01",
        fetched_at="2026-07-13T00:00:00+00:00",
        content_text="公告主体：长电科技（600584）。公司持续研发先进封装技术并提供相关服务。",
        metadata={"symbol": "600584", "source_backend": "cninfo_pdf"},
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    )

    assert len(evidence) == 1
    assert evidence[0].is_first_party is True
    assert evidence[0].source_quality == 1.0
    assert evidence[0].metadata["evidence_tier"] == "first_party_authoritative"
    assert evidence[0].stance == "support"
    assert evidence[0].locator["char_end"] > evidence[0].locator["char_start"]
    assert repository.save_evidence(evidence[0]) is True
    assert repository.list_evidence(claim.claim_id)[0].evidence_id == evidence[0].evidence_id


def test_board_secretary_reply_is_authoritative_first_party_evidence(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim_ir",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    document = SourceDocument(
        document_id="ir_doc",
        source_type="investor_interaction",
        url="https://sns.sseinfo.com/question/1",
        title="投资者互动问答",
        published_at="2026-07-15",
        fetched_at="2026-07-15T08:00:00+00:00",
        content_text=(
            "投资者问题：公司是否开展先进封装业务？\n"
            "公司回复：长电科技持续开展先进封装技术研发和生产。"
        ),
        metadata={"symbol": "600584", "source_backend": "sse_interactive"},
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    )

    assert len(evidence) == 1
    assert evidence[0].is_first_party is True
    assert evidence[0].source_quality == 1.0
    assert evidence[0].stance == "support"
    assert repository.save_evidence(evidence[0]) is True


def test_official_industry_mention_is_not_company_membership(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim_ev",
        subject_id="000021.SZ",
        subject_name="深科技",
        object_name="新能源汽车",
    )
    document = SourceDocument(
        document_id="cninfo_ev",
        source_type="announcement",
        url="https://static.cninfo.com.cn/finalpage/report.PDF",
        title="深科技年度报告",
        content_text=(
            "公告主体：深科技（000021）。公司所处的行业情况：在生成式 AI、"
            "高性能计算及新能源汽车驱动下，全球半导体市场呈现复苏趋势，"
            "行业产品性能要求持续提升。"
        ),
        metadata={"symbol": "000021", "source_backend": "cninfo_pdf"},
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    )

    assert evidence == []


def test_official_quote_selects_direct_company_excerpt(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim_packaging",
        subject_id="000021.SZ",
        subject_name="深科技",
        object_name="先进封装",
    )
    document = SourceDocument(
        document_id="cninfo_packaging",
        source_type="announcement",
        url="https://static.cninfo.com.cn/finalpage/report.PDF",
        title="深科技年度报告",
        content_text=(
            "公告主体：深科技（000021）。行业情况：全球先进封装市场规模持续增长。"
            "\n\n公告主体：深科技（000021）。公司在封装测试领域积极布局，"
            "成立先进封装研究院并开展工艺技术联合研发。"
        ),
        metadata={"symbol": "000021", "source_backend": "cninfo_pdf"},
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    )

    assert len(evidence) == 1
    assert "成立先进封装研究院" in evidence[0].quote
    assert "市场规模持续增长" not in evidence[0].quote


def test_neutral_directory_page_is_no_support_not_expired() -> None:
    claim = Claim(claim_id="claim1", research_status="DORMANT")
    neutral = EvidenceRecord(
        claim_id=claim.claim_id,
        stance="neutral",
        retrieved_at="2026-07-12T08:00:00+08:00",
    )

    result = ClaimPolicy().evaluate(
        claim, [neutral], as_of="2026-07-13T08:00:00+08:00"
    )

    assert result.target_status == "DISCOVERED"
    assert result.reason_codes == ["no_support"]


def test_publisher_disclaimers_are_not_relationship_counter_evidence(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim1",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    document = SourceDocument(
        document_id="web_doc",
        source_type="news",
        url="https://example.com/article",
        title="长电科技（600584）先进封装业务",
        content_text=(
            "公司持续研发先进封装技术。数据来源免责声明：相关信息并未经过"
            "本网站证实。本公司公告不存在虚假记载、误导性陈述或重大遗漏。"
        ),
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="counter"
    )

    assert len(evidence) == 1
    assert evidence[0].stance == "neutral"


def test_explicit_theme_denial_is_counter_evidence(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim1",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    document = SourceDocument(
        document_id="denial",
        source_type="announcement",
        url="https://static.cninfo.com.cn/denial.PDF",
        title="长电科技（600584）澄清公告",
        content_text="长电科技（600584）澄清：公司不涉及先进封装业务。",
    )

    evidence = GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="counter"
    )

    assert len(evidence) == 1
    assert evidence[0].stance == "counter"


def test_maintenance_invalidates_legacy_disclaimer_counter(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim1",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    repository.save_claim(claim)
    repository.save_evidence(EvidenceRecord(
        evidence_id="legacy_counter",
        claim_id=claim.claim_id,
        stance="counter",
        source_trace_id="legacy_source",
        quote=(
            "长电科技（600584）先进封装技术。相关信息并未经过本网站证实，"
            "没有虚假记载、误导性陈述。"
        ),
        extraction_method="grounded_rule",
        retrieved_at="2026-07-12T08:00:00+08:00",
    ))

    stats = invalidate_ambiguous_entity_evidence(
        repository, as_of="2026-07-13T08:00:00+08:00"
    )

    stored = repository.list_evidence(claim.claim_id)[0]
    assert stats["invalidated_evidence"] == 1
    assert stored.metadata["invalidation_reason"] == (
        "counter_not_grounded_to_researched_relationship"
    )
    assert stored.can_affect_confidence is False


def test_maintenance_restores_symbol_scoped_official_quote_without_code(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim1",
        subject_id="600584.SH",
        subject_name="长电科技",
        object_name="先进封装",
    )
    repository.save_claim(claim)
    repository.save_evidence(EvidenceRecord(
        evidence_id="official_direct",
        claim_id=claim.claim_id,
        stance="support",
        source_trace_id="cninfo_source",
        quote="报告期，公司围绕先进封装持续推进业务结构优化与核心能力建设。",
        extraction_method="grounded_rule",
        retrieved_at="2026-07-12T08:00:00+08:00",
        is_first_party=True,
        metadata={
            "symbol": "600584",
            "source_backend": "cninfo_pdf",
            "invalidated": True,
            "invalidation_reason": "entity_disambiguation_v2_missing_security_code",
            "invalidated_at": "2026-07-12T09:00:00+08:00",
        },
    ))

    stats = invalidate_ambiguous_entity_evidence(
        repository, as_of="2026-07-13T08:00:00+08:00"
    )

    stored = repository.list_evidence(claim.claim_id)[0]
    assert stats["revalidated_evidence"] == 1
    assert stored.metadata.get("invalidated") is None
    assert stored.can_affect_confidence is True
