"""Production-integrity tests for the dynamic-theme runtime."""
from __future__ import annotations

from unittest.mock import patch
import time

import pandas as pd

from core.quant.theme.contracts import (
    ThemeCatalyst,
    ThemeMember,
    ThemePacket,
    generate_theme_id,
)
from core.quant.theme.event_clusterer import EventClusterer
from core.quant.theme.source_router import ConceptBoard, SourceSnapshot, ThemeSourceRouter
from core.research.control_plane.contracts import (
    Claim,
    EvidenceRecord,
    ResearchTask,
    TaskStatus,
)
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.theme_review import ThemeReviewProcessor
from infrastructure.data_providers.market.provider_health import ProviderHealth


def _quality_snapshot(board_count: int, members_per_board: int) -> SourceSnapshot:
    return SourceSnapshot(
        boards=[
            ConceptBoard(
                board_name=f"主题{i}",
                members=[f"{i:03d}{j:03d}" for j in range(members_per_board)],
            )
            for i in range(board_count)
        ]
    )


def test_last_good_quality_rejects_partial_and_coverage_collapse() -> None:
    partial = _quality_snapshot(1, 2)
    healthy = _quality_snapshot(10, 10)
    collapsed = _quality_snapshot(5, 4)

    assert not ThemeSourceRouter._is_last_good_quality(partial)
    assert ThemeSourceRouter._is_last_good_quality(healthy)
    assert not ThemeSourceRouter._is_last_good_quality(collapsed, healthy)


def test_market_sampling_keeps_all_hypothesis_buckets() -> None:
    rows = []
    for idx, change in enumerate((-8.0, -3.0, -0.1, 0.5, 1.5, 4.0, 9.0)):
        rows.append({
            "board_name": f"板块{idx}",
            "board_code": str(idx),
            "change_pct": change,
            "amount": 1000 - idx,
        })
    with patch(
        "infrastructure.data_providers.market.efinance_provider.EfinanceProvider.get_concept_boards",
        return_value=pd.DataFrame(rows),
    ):
        boards = ThemeSourceRouter(
            akshare_provider=object(), persist_last_good=False,
        )._fetch_efinance_concepts(max_boards=5)

    assert {board.discovery_bucket for board in boards} == {
        "moderate", "neutral", "positive", "negative", "liquid",
    }


def test_live_ths_names_are_enriched_with_wencai_members(monkeypatch) -> None:
    router = ThemeSourceRouter(
        akshare_provider=object(),
        persist_last_good=False,
        enable_slow_scrapers=True,
    )
    live = ConceptBoard(board_name="半导体", provider="ths", members=[])
    enriched = ConceptBoard(
        board_name="半导体",
        provider="wencai",
        members=["600000.SH"],
    )
    monkeypatch.setattr(
        router,
        "_fetch_all_sources_parallel",
        lambda _max_boards: ([live], ["ths"], []),
    )
    monkeypatch.setattr(
        router,
        "_resolve_market_board_members",
        lambda boards, **_kwargs: [enriched] if boards == [live] else [],
    )

    snapshot = router.fetch_concept_snapshot("2026-08-17")

    assert "wencai" in snapshot.providers_used
    assert any(board.members == ["600000.SH"] for board in snapshot.boards)


def test_failed_rankings_refresh_stale_membership_with_wencai(monkeypatch) -> None:
    router = ThemeSourceRouter(
        akshare_provider=object(),
        persist_last_good=False,
        enable_slow_scrapers=True,
    )
    router._last_good_snapshot = SourceSnapshot(
        snapshot_id="stale",
        trade_date="2026-08-20",
        source_as_of="2026-08-20",
        providers_used=["local_last_good"],
        boards=[
            ConceptBoard(board_name=f"主题{i}", members=[f"old-{i}"])
            for i in range(10)
        ],
    )
    refreshed = [
        ConceptBoard(
            board_name=f"主题{i}",
            provider="wencai",
            members=[f"600{index:03d}.SH" for index in range(i * 10, i * 10 + 10)],
        )
        for i in range(6)
    ]
    monkeypatch.setattr(
        router,
        "_fetch_all_sources_parallel",
        lambda _max_boards: ([], [], ["eastmoney_failed"]),
    )
    monkeypatch.setattr(
        router,
        "_resolve_market_board_members",
        lambda boards, **_kwargs: refreshed if boards == router._last_good_snapshot.boards else [],
    )

    snapshot = router.fetch_concept_snapshot("2026-09-03")

    assert snapshot.source_as_of == "2026-09-03"
    assert snapshot.providers_used == ["wencai"]
    assert len({symbol for board in snapshot.boards for symbol in board.members}) == 60
    assert "using_last_good_snapshot" not in snapshot.degraded_reasons


def test_eastmoney_failure_domain_is_probed_once_then_circuited(monkeypatch) -> None:
    """efinance/AKShare share push2 and must not amplify one upstream ban."""
    router = ThemeSourceRouter(
        akshare_provider=None,
        persist_last_good=False,
        enable_slow_scrapers=True,
    )
    router._circuit_breakers["ths"]["circuit_open_until"] = time.time() + 60
    attempts = {"efinance": 0, "akshare": 0}

    def fail_efinance(_max_boards):
        attempts["efinance"] += 1
        raise RuntimeError("push2 disconnected")

    def fetch_same_upstream_again(_max_boards):
        attempts["akshare"] += 1
        return [ConceptBoard(board_name="不应重复请求", provider="eastmoney")]

    monkeypatch.setattr(router, "_fetch_efinance_concepts", fail_efinance)
    monkeypatch.setattr(router, "_fetch_eastmoney_concepts", fetch_same_upstream_again)

    first = router._fetch_all_sources_parallel(30)
    second = router._fetch_all_sources_parallel(30)

    assert first[0] == []
    assert second[0] == []
    assert attempts == {"efinance": 1, "akshare": 0}
    assert any(reason.startswith("eastmoney_failed:") for reason in first[2])


def test_eastmoney_timeout_opens_circuit_before_next_cycle(monkeypatch) -> None:
    router = ThemeSourceRouter(
        persist_last_good=False,
        enable_slow_scrapers=True,
    )
    router.DEFAULT_TIMEOUT = 0.01
    router._circuit_breakers["ths"]["circuit_open_until"] = time.time() + 60
    attempts = 0

    def slow_fetch(_max_boards):
        nonlocal attempts
        attempts += 1
        time.sleep(0.05)
        return []

    monkeypatch.setattr(router, "_fetch_efinance_concepts", slow_fetch)

    first = router._fetch_all_sources_parallel(30)
    second = router._fetch_all_sources_parallel(30)

    assert attempts == 1
    assert "eastmoney_failed: timeout after 0.01s" in first[2]
    assert "eastmoney_circuit_open" in second[2]


def test_theme_identity_does_not_change_with_membership() -> None:
    first = generate_theme_id("商业航天", "商业航天", ["000001"])
    second = generate_theme_id("商业航天", "商业航天", ["000002", "000003"])
    assert first == second


def test_eastmoney_fields_and_new_acronym_theme_are_normalized() -> None:
    clusterer = EventClusterer()
    events = clusterer.cluster_events([
        {
            "title": "厂商发布 AI PC 新平台",
            "summary": "面向端侧智能市场推出新产品",
            "showTime": "2026-07-13 07:30:00",
            "url": "https://finance.eastmoney.com/a/123.html",
        }
    ], min_co_occurrence=1)

    assert len(events) == 1
    assert "AI PC" in events[0].theme_hints
    assert events[0].event_date == "2026-07-13 07:30:00"
    assert events[0].sources == ["finance.eastmoney.com"]


def test_provider_health_status_does_not_deadlock(tmp_path) -> None:
    health = ProviderHealth(tmp_path / "health.json")
    health.report_success("tencent")
    status = health.get_status("tencent")
    assert status["available"] is True
    assert status["status"] == "active"


def test_tdx_false_connect_rotates_all_hosts(monkeypatch) -> None:
    import infrastructure.data_providers.market.tdx_provider as module

    instances = []

    class FakeAPI:
        def __init__(self):
            self.disconnected = False
            instances.append(self)

        def connect(self, *_args, **_kwargs):
            return False

        def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(module, "_TdxAPI", FakeAPI)
    monkeypatch.setattr(module, "TDX_HOSTS", [("a", "1.1.1.1", 1), ("b", "2.2.2.2", 2)])
    provider = module.TdxProvider()
    monkeypatch.setattr(provider, "_throttle", lambda: None)

    assert provider._connect() is None
    assert len(instances) == 2
    assert all(item.disconnected for item in instances)
    assert provider.health()["available"] is False
    assert provider.health()["status"] == "failed"


def _theme_packet(*, negative: bool = False) -> ThemePacket:
    member = ThemeMember(
        symbol="000001",
        membership_sources=["wencai_semantic"],
        membership_confidence=0.6,
        market_role="secondary",
        membership_evidence=[{
            "provider": "wencai",
            "theme_name": "AI PC",
            "raw_response_hash": "hash1",
            "fetched_at": "2026-07-13T07:30:00+08:00",
        }],
        market_features={
            "leadership_score": 0.8,
            "market_role_candidate": "leader_candidate",
            "technical_state": "MA10_RETEST",
        },
    )
    return ThemePacket(
        theme_id="theme_aipc",
        theme_run_id="run_aipc",
        theme_name="AI PC",
        as_of="2026-07-13T08:00:00+08:00",
        members=[member],
        core_members=[member],
        catalysts=[ThemeCatalyst(
            title="公司澄清无相关业务" if negative else "厂商发布 AI PC 平台",
            summary="不存在相关业务" if negative else "新平台发布",
            source="example.com",
        )],
    )


def test_strict_theme_review_seeds_claim_evidence_and_task(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    processor = ThemeReviewProcessor(
        repository=repository,
        company_name_by_symbol={"000001": "测试公司"},
    )

    result = processor.review(_theme_packet(), as_of="2026-07-13T08:00:00+08:00")

    assert result.enhanced_packet.core_members == []
    assert result.review_summary["research_gate_status"] == "pending"
    assert repository.stats()["counts"]["claims"] == 1
    assert repository.stats()["counts"]["evidence"] == 1
    tasks = repository.list_tasks(limit=10)
    assert len(tasks) == 1
    assert tasks[0].task_type == "evidence_completion"
    assert tasks[0].params["workflow"] == "dynamic_theme_review"
    assert tasks[0].params["retrieval_version"] == processor.RETRIEVAL_VERSION


def test_ensure_member_claim_does_not_infer_business_role_from_market_features(
    tmp_path,
) -> None:
    """市场特征可保留溯源，但不能自动生成产业核心证据。"""
    repository = ResearchRepository(tmp_path / "research.db")
    processor = ThemeReviewProcessor(
        repository=repository,
        company_name_by_symbol={"000001": "测试公司"},
    )

    processor._ensure_member_claim(
        _theme_packet(),
        _theme_packet().members[0],
        matching=[],
        as_of="2026-07-13T08:00:00+08:00",
    )

    claims = repository.list_claims(limit=10)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.attributes.get("market_features", {}).get("leadership_score") == 0.8
    assert "market_role" not in claim.attributes
    assert "industry_core_score" not in claim.attributes


def test_retrieval_adapter_upgrade_requeues_exhausted_task(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    processor = ThemeReviewProcessor(
        repository=repository,
        company_name_by_symbol={"000001": "测试公司"},
    )
    processor.review(_theme_packet(), as_of="2026-07-13T08:00:00+08:00")
    task = repository.list_tasks(limit=10)[0]
    task.status = "partial"
    task.retry_count = task.max_retries
    task.params["retrieval_version"] = "legacy_adapter"
    repository.save_task(task)

    processor.review(_theme_packet(), as_of="2026-07-13T08:00:00+08:00")

    refreshed = repository.get_task(task.task_id)
    assert refreshed.status == "queued"
    assert refreshed.retry_count == 0
    assert refreshed.params["retrieval_version"] == processor.RETRIEVAL_VERSION
    assert refreshed.failure["code"] == "RETRIEVAL_ADAPTER_UPGRADED"


def test_theme_review_reuses_active_claim_task_across_day_keys(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    processor = ThemeReviewProcessor(repository=repository)
    packet = _theme_packet()
    claim = Claim(
        claim_id="claim_active_task",
        subject_id="000001",
        subject_name="娴嬭瘯鍏徃",
        relation="member_of_theme",
        object_id="theme_aipc",
        object_name="AI PC",
        high_impact=True,
        attributes={"theme_id": "theme_aipc"},
    )
    repository.save_claim(claim)
    active = ResearchTask(
        task_id="active-claim-task",
        target_claim_id=claim.claim_id,
        status=TaskStatus.RUNNING.value,
        params={"workflow": "dynamic_theme_review"},
    )
    repository.save_task(active)

    task_id = processor._ensure_research_task(
        packet,
        packet.members[0],
        claim,
        as_of="2026-07-14T08:00:00+08:00",
    )

    assert task_id == active.task_id
    assert len(repository.list_tasks(limit=10)) == 1


def test_strict_theme_review_accepts_only_active_sufficient_claim(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="claim_active",
        subject_id="000001",
        subject_name="测试公司",
        relation="member_of_theme",
        object_id="theme_aipc",
        object_name="AI PC",
        research_status="DISCOVERED",
        mapping_confidence=1.0,
        counter_search_completed=True,
        counter_evidence_evaluated=True,
        attributes={"theme_id": "theme_aipc", "market_role": "core"},
    )
    repository.save_claim(claim)
    for index in (1, 2):
        repository.save_evidence(EvidenceRecord(
            evidence_id=f"ev{index}",
            claim_id=claim.claim_id,
            source_trace_id=f"src{index}",
            lineage_id=f"lineage{index}",
            stance="support",
            quote=f"测试公司属于 AI PC 产业证据{index}",
            source_quality=0.8,
            match_score=1.0,
            is_first_party=index == 1,
            retrieved_at="2026-07-12T08:00:00+08:00",
            grounding_status="structured",
            metadata={"slots": ["identity", "relationship"]},
        ))
    claim.research_status = "ACTIVE"
    repository.save_claim(claim)

    result = ThemeReviewProcessor(repository=repository).review(
        _theme_packet(), as_of="2026-07-13T08:00:00+08:00"
    )

    assert [member.symbol for member in result.enhanced_packet.core_members] == ["000001"]
    assert result.enhanced_packet.core_members[0].research_claim_ids == ["claim_active"]
    assert result.review_summary["research_gate_status"] == "validated"


def test_strict_counter_status_is_unknown_without_executed_search(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    result = ThemeReviewProcessor(repository=repository).review(
        _theme_packet(), as_of="2026-07-13T08:00:00+08:00"
    )
    assert result.review_summary["negative_status"] == "unknown"


def test_theme_review_degrades_malformed_packet_fields_without_crashing() -> None:
    packet = ThemePacket(
        theme_id="theme_malformed",
        theme_name="AI PC",
        aliases=None,
        catalysts=None,
        members=None,
        core_members=None,
        source_status=None,
        degradation_reasons=None,
        negative_risk_score="not-a-number",
    )

    result = ThemeReviewProcessor().review(packet, max_new_themes="bad")

    assert result.enhanced_packet.members == []
    assert result.enhanced_packet.catalysts == []
    assert result.enhanced_packet.negative_risk_score == 0.0


def test_theme_review_task_returns_structured_error_for_malformed_packet() -> None:
    task = ResearchTask(
        task_id="task-malformed-theme",
        params={"theme_packet": {"members": "bad", "catalysts": "bad"}},
    )

    result = ThemeReviewProcessor().process_task(task)

    assert result["status"] == "error"
    assert result["error_code"] == "INVALID_THEME_PACKET"


def test_explicit_negative_blocks_theme_even_without_external_search(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    result = ThemeReviewProcessor(repository=repository).review(
        _theme_packet(negative=True), as_of="2026-07-13T08:00:00+08:00"
    )
    assert result.review_summary["negative_status"] == "blocking"
    assert result.review_summary["research_gate_status"] == "rejected"
    assert result.enhanced_packet.core_members == []


def test_member_counter_does_not_block_every_company_in_theme(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    claim = Claim(
        claim_id="member_denial",
        subject_id="000001",
        subject_name="测试公司",
        relation="member_of_theme",
        object_id="theme_aipc",
        object_name="AI PC",
        counter_search_executed=True,
        mapping_confidence=1.0,
        attributes={"theme_id": "theme_aipc"},
    )
    repository.save_claim(claim)
    repository.save_evidence(EvidenceRecord(
        evidence_id="member_counter",
        claim_id=claim.claim_id,
        stance="counter",
        source_trace_id="member_counter_source",
        quote="测试公司（000001）明确表示公司不涉及 AI PC 业务。",
        source_quality=0.9,
        match_score=1.0,
        retrieved_at="2026-07-12T08:00:00+08:00",
    ))

    result = ThemeReviewProcessor(repository=repository).review(
        _theme_packet(), as_of="2026-07-13T08:00:00+08:00"
    )

    assert result.review_summary["negative_status"] == "clear"
    assert result.review_summary["research_gate_status"] != "rejected"
    items = result.review_summary["counter_evidence"]["items"]
    assert items[0]["severity"] == "member_only"
