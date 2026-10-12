"""Phase 1 smoke test：版本化主题数据与证据库新后端能力。

覆盖：
1. ThemeSnapshotStore 基本 CRUD 与 PIT 历史查询
2. MembershipEvidenceStore 基本 CRUD 与确认状态
3. ConceptBoard 截断结构化字段序列化
4. ThemeMemberCandidate 召回候选默认置信度
"""
from __future__ import annotations

from pathlib import Path

from core.quant.theme.source_router import (
    ConceptBoard,
    SourceSnapshot,
    ThemeSnapshotStore,
)
from core.research.theme_membership.contracts import (
    GROUNDING_GROUNDED,
    MembershipEvidence,
    RECALL_CONFIDENCE,
    ThemeMemberCandidate,
)
from core.research.theme_membership.repository import MembershipEvidenceStore


def test_theme_snapshot_store_crud_smoke(tmp_path: Path) -> None:
    """ThemeSnapshotStore 基本保存与 PIT 历史查询。"""
    store = ThemeSnapshotStore(db_path=tmp_path / "snapshots.db")

    snapshot = SourceSnapshot(
        snapshot_id="snap_smoke_001",
        trade_date="20260710",
        fetched_at="2026-07-10T00:00:00Z",
        providers_used=["eastmoney"],
        boards=[
            ConceptBoard(
                board_name="HBM",
                provider="eastmoney",
                members=["000001", "000002"],
                returned_count=2,
            ),
        ],
        source_as_of="20260710",
        content_hash="hash_smoke_001",
    )

    # 保存成功，重复保存幂等
    assert store.save_snapshot(snapshot, policy_version="smoke_test") is True
    assert store.save_snapshot(snapshot, policy_version="smoke_test") is False

    # PIT 历史查询：source_as_of <= requested 返回最新版本
    result = store.get_historical_snapshot("20260711")
    assert result is not None
    assert result.snapshot_id == "snap_smoke_001"

    # 未来快照在历史 as_of 下不可见
    future = store.get_historical_snapshot("20260709")
    assert future is None


def test_theme_snapshot_store_prefers_recent_broad_membership_snapshot(tmp_path: Path) -> None:
    """成员映射不能被更新但严重截断的小快照覆盖。"""
    store = ThemeSnapshotStore(db_path=tmp_path / "snapshots.db")
    broad = SourceSnapshot(
        snapshot_id="snap_broad",
        trade_date="20260714",
        fetched_at="2026-07-14T15:00:00+08:00",
        providers_used=["easy_tdx"],
        boards=[
            ConceptBoard(
                board_name="创新药",
                provider="easy_tdx",
                members=["603127", *[f"60{i:04d}" for i in range(100)]],
            )
        ],
        source_as_of="20260714",
        content_hash="hash_broad",
    )
    narrow = SourceSnapshot(
        snapshot_id="snap_narrow",
        trade_date="20260716",
        fetched_at="2026-07-16T12:00:00+08:00",
        providers_used=["efinance"],
        boards=[ConceptBoard(board_name="小板块", provider="efinance", members=["000001"])],
        source_as_of="20260716",
        content_hash="hash_narrow",
    )
    assert store.save_snapshot(broad) is True
    assert store.save_snapshot(narrow) is True

    result = store.get_historical_membership_snapshot("20260716")

    assert result is not None
    assert result.snapshot_id == "snap_broad"
    assert "603127" in result.boards[0].members


def test_membership_evidence_store_crud_smoke(tmp_path: Path) -> None:
    """MembershipEvidenceStore 基本保存与确认查询。"""
    store = MembershipEvidenceStore(db_path=tmp_path / "membership.db")

    evidence = MembershipEvidence(
        symbol="000001.SZ",
        theme_id="HBM",
        stance="support",
        source_type="announcement",
        provider="cninfo",
        source_url="https://example.com/ann/001",
        document_id="doc_001",
        quote="公司是 HBM 产业链供应商",
        locator={"paragraph": 3},
        published_at="2026-07-10",
        retrieved_at="2026-07-10T08:00:00Z",
        valid_from="2026-07-10",
        valid_until="",
        lineage_id="line_001",
        grounding_status=GROUNDING_GROUNDED,
        policy_version="smoke_test",
        confidence=0.6,
    )

    # 保存并读回（get_evidence 按 symbol+theme_id 查询，返回列表）
    assert store.save_evidence(evidence) is True
    retrieved_list = store.get_evidence("000001.SZ", "HBM")
    assert len(retrieved_list) >= 1
    retrieved = retrieved_list[0]
    assert retrieved.symbol == "000001.SZ"
    assert retrieved.grounding_status == GROUNDING_GROUNDED
    assert retrieved.locator == {"paragraph": 3}

    # 确认成员查询：返回 symbol 列表
    confirmed = store.get_confirmed_members("HBM")
    assert "000001.SZ" in confirmed

    assert store.is_member_confirmed("000001.SZ", "HBM") is True
    assert store.is_member_confirmed("000099.SZ", "HBM") is False


def test_concept_board_truncation_fields_serialize() -> None:
    """ConceptBoard 截断结构化字段可正确序列化。"""
    board = ConceptBoard(
        board_name="测试概念",
        provider="eastmoney",
        members=["000001", "000002", "000003"],
        requested_count=30,
        returned_count=3,
        reported_total=100,
        coverage_ratio=0.03,
        pagination_supported=False,
        next_page_available=False,
        members_truncated=True,
    )

    data = board.to_dict()

    assert data["requested_count"] == 30
    assert data["returned_count"] == 3
    assert data["reported_total"] == 100
    assert data["coverage_ratio"] == 0.03
    assert data["pagination_supported"] is False
    assert data["next_page_available"] is False
    assert data["members_truncated"] is True


def test_theme_member_candidate_default_confidence() -> None:
    """外部概念表召回的 ThemeMemberCandidate 默认 membership_status 为 recall_only。

    confidence 字段已降级为 shadow 分数（0.0），不再用于生产门禁。
    """
    candidate = ThemeMemberCandidate(
        symbol="000001.SZ",
        theme_id="HBM",
        source_provider="wencai",
    )

    assert candidate.membership_confidence == RECALL_CONFIDENCE
    assert candidate.membership_status == "recall_only"
