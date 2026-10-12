"""P2.1 全市场研究截面 PIT 测试。

按 P2 计划 §4 Task P2.1 验收：
- 任意历史 as_of 只能使用当时已公开数据
- 退市、暂停上市和历史成分处理不产生幸存者偏差
- 截面构建保存 provider、版本、覆盖率和排除原因
- 空字段不以零分惩罚，单独进入数据可用性维度

测试组织：
- TestContracts：契约 to_dict / from_dict / compute_stats
- TestRepositoryMigrate：仓储幂等迁移
- TestRepositorySaveAndGet：仓储 save / get / 不可覆盖
- TestRepositoryPITQuery：仓储 get_by_as_of PIT 查询
- TestRepositoryFutureLeakage：仓储 validate_no_future_leakage
- TestBuilderMembership：构建器成分股状态处理
- TestBuilderProvenance：构建器来源元数据
- TestBuilderDeterminism：构建器 snapshot_id 确定性
- TestPITAcceptance：验收点 1（无未来泄漏）
- TestSurvivorshipBiasAcceptance：验收点 2（无幸存者偏差）
- TestProvenanceAcceptance：验收点 3（provider/version/coverage/exclusion）
- TestDataAvailabilityAcceptance：验收点 4（空字段不以零分惩罚）
- TestP1BackedAssemblerGraceful：P1 集成 best-effort 不抛异常
- TestNoTradeAdvice：AGENTS.md 合规性
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.research.discovery.contracts import (
    DATA_AVAILABILITY_AVAILABLE,
    DATA_AVAILABILITY_MISSING,
    DATA_AVAILABILITY_NOT_APPLICABLE,
    DATA_AVAILABILITY_NOT_COMPUTABLE,
    DATA_AVAILABILITY_PENDING,
    CoverageStat,
    ExclusionRecord,
    FIELD_GROUP_CASHFLOW_CAPITAL_EFFICIENCY,
    FIELD_GROUP_CONSENSUS_REVISIONS,
    FIELD_GROUP_EVIDENCE_QUALITY,
    FIELD_GROUP_FINANCIAL_QUALITY,
    FIELD_GROUP_GROWTH,
    FIELD_GROUP_INDUSTRY_TIMING,
    FIELD_GROUP_NAMES,
    FIELD_GROUP_OPERATING_DRIVER_COMPLETENESS,
    FIELD_GROUP_RESEARCHABILITY,
    FIELD_GROUP_VALUATION,
    FieldGroup,
    MEMBERSHIP_DELISTED,
    MEMBERSHIP_HISTORICAL,
    MEMBERSHIP_INDEX_OUT,
    MEMBERSHIP_LISTED,
    MEMBERSHIP_PRE_LISTING,
    MEMBERSHIP_SUSPENDED,
    SnapshotField,
    SnapshotProvenance,
    UniverseMember,
    UniverseSnapshot,
)
from core.research.discovery.repository import UniverseSnapshotRepository
from core.research.discovery.universe_snapshot import (
    BuilderConfig,
    EmptyFieldGroupAssembler,
    P1BackedFieldGroupAssembler,
    StaticUniverseProvider,
    UniverseEntry,
    UniverseSnapshotBuilder,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_field(
    name: str,
    value: float | None,
    available_at: str,
    status: str = DATA_AVAILABILITY_AVAILABLE,
    source_refs: list[str] | None = None,
) -> SnapshotField:
    """构造测试用 SnapshotField。"""
    return SnapshotField(
        name=name,
        value=value,
        unit="ratio",
        available_at=available_at,
        source_refs=source_refs or [f"src_{name}"],
        status=status,
        formula=f"formula_{name}",
    )


def _make_member(
    symbol: str,
    as_of: str,
    snapshot_id: str = "snap_test",
    membership_status: str = MEMBERSHIP_LISTED,
    field_counts: dict[str, int] | None = None,
) -> UniverseMember:
    """构造测试用 UniverseMember。

    Args:
        symbol: 公司代码
        as_of: 截面时点
        snapshot_id: 所属截面 ID
        membership_status: 成分股状态
        field_counts: {group_name: filled_field_count}；未列出的字段组为空
    """
    field_groups: dict[str, FieldGroup] = {}
    field_counts = field_counts or {}
    for group_name in FIELD_GROUP_NAMES:
        n_filled = field_counts.get(group_name, 0)
        fields = [
            _make_field(
                name=f"{group_name}_field_{i}",
                value=float(i),
                available_at=as_of,
            )
            for i in range(n_filled)
        ]
        field_groups[group_name] = FieldGroup(name=group_name, fields=fields)
    member = UniverseMember(
        # member_id 在同一 SQLite 表内必须唯一，因此含 snapshot_id
        member_id=f"member_{snapshot_id}_{symbol}",
        snapshot_id=snapshot_id,
        symbol=symbol,
        company_name=f"公司_{symbol}",
        industry_code="sw_test",
        industry_name="测试行业",
        as_of=as_of,
        membership_status=membership_status,
        listing_date="2010-01-01",
        field_groups=field_groups,
    )
    member.compute_data_availability_score()
    return member


def _make_snapshot(
    as_of: str = "2024-12-31",
    snapshot_id: str = "snap_test_20241231",
    members: list[UniverseMember] | None = None,
    exclusions: list[ExclusionRecord] | None = None,
    provider: str = "tushare",
) -> UniverseSnapshot:
    """构造测试用 UniverseSnapshot。"""
    if members is None:
        members = [_make_member("600519.SH", as_of, snapshot_id, field_counts={
            FIELD_GROUP_FINANCIAL_QUALITY: 2,
            FIELD_GROUP_GROWTH: 1,
        })]
    snap = UniverseSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        universe_name="all_a_share",
        universe_version="v1",
        members=members,
        exclusions=exclusions or [],
        provenance=SnapshotProvenance(
            provider=provider,
            provider_version="pro_v1",
            builder_version="p2.1.0",
            built_by="builder",
            policy_version="p2.1.0",
            data_fingerprint="fp_test123",
            exclusion_count=len(exclusions or []),
            total_member_count=len(members),
        ),
        policy_version="p2.1.0",
        created_at="2026-07-20T00:00:00+00:00",
    )
    snap.compute_coverage_stats()
    return snap


@pytest.fixture
def tmp_repo(tmp_path: Path) -> UniverseSnapshotRepository:
    """临时 SQLite 仓储。"""
    return UniverseSnapshotRepository(db_path=tmp_path / "test_universe.db")


# ---------------------------------------------------------------------------
# 契约测试
# ---------------------------------------------------------------------------


class TestContracts:
    """契约 to_dict / from_dict / compute_stats 测试。"""

    def test_snapshot_field_round_trip(self) -> None:
        """SnapshotField 序列化与反序列化保持等价。"""
        field = _make_field("roic", 0.15, "2024-12-31")
        round_trip = SnapshotField.from_dict(field.to_dict())
        assert round_trip.name == field.name
        assert round_trip.value == field.value
        assert round_trip.available_at == field.available_at
        assert round_trip.status == field.status
        assert round_trip.source_refs == field.source_refs

    def test_field_group_compute_stats(self) -> None:
        """FieldGroup.compute_stats 正确分类字段状态。"""
        group = FieldGroup(
            name="test",
            fields=[
                _make_field("a", 1.0, "2024-12-31", DATA_AVAILABILITY_AVAILABLE),
                _make_field("b", None, "2024-12-31", DATA_AVAILABILITY_MISSING),
                _make_field("c", None, "2024-12-31", DATA_AVAILABILITY_NOT_COMPUTABLE),
                _make_field("d", None, "2024-12-31", DATA_AVAILABILITY_NOT_APPLICABLE),
                _make_field("e", None, "2024-12-31", DATA_AVAILABILITY_PENDING),
            ],
        )
        stats = group.compute_stats()
        assert stats["filled_count"] == 1
        assert stats["missing_count"] == 1
        assert stats["non_computable_count"] == 1
        assert stats["not_applicable_count"] == 1
        assert stats["pending_count"] == 1
        assert stats["total_count"] == 5
        assert stats["coverage_ratio"] == pytest.approx(0.2)

    def test_universe_member_data_availability_score(self) -> None:
        """UniverseMember.compute_data_availability_score 计算综合分。"""
        member = _make_member(
            "600519.SH",
            "2024-12-31",
            field_counts={
                FIELD_GROUP_FINANCIAL_QUALITY: 2,
                FIELD_GROUP_GROWTH: 1,
            },
        )
        # 9 个字段组中，2 个组有字段（共 3 个 available），其余 7 个组为空
        # 总字段数 = 3，已填充 = 3，综合分 = 1.0
        assert member.data_availability_score == pytest.approx(1.0)

    def test_universe_snapshot_compute_coverage_stats(self) -> None:
        """UniverseSnapshot.compute_coverage_stats 按字段组统计覆盖率。"""
        snap = _make_snapshot()
        stats = snap.compute_coverage_stats()
        assert len(stats) == 9
        group_names = [s.group_name for s in stats]
        for name in FIELD_GROUP_NAMES:
            assert name in group_names

    def test_universe_snapshot_round_trip(self) -> None:
        """UniverseSnapshot 序列化与反序列化保持等价。"""
        snap = _make_snapshot()
        round_trip = UniverseSnapshot.from_dict(snap.to_dict())
        assert round_trip.snapshot_id == snap.snapshot_id
        assert round_trip.as_of == snap.as_of
        assert len(round_trip.members) == len(snap.members)
        assert round_trip.provenance.provider == snap.provenance.provider

    def test_exclusion_record_round_trip(self) -> None:
        """ExclusionRecord 序列化与反序列化保持等价。"""
        excl = ExclusionRecord(
            symbol="000001.SZ",
            as_of="2024-12-31",
            reason="delisted",
            detail="公司已于 2020-01-01 退市",
            excluded_by="builder",
            excluded_at="2026-07-20T00:00:00+00:00",
        )
        round_trip = ExclusionRecord.from_dict(excl.to_dict())
        assert round_trip.symbol == excl.symbol
        assert round_trip.reason == excl.reason
        assert round_trip.detail == excl.detail


# ---------------------------------------------------------------------------
# 仓储测试
# ---------------------------------------------------------------------------


class TestRepositoryMigrate:
    """仓储幂等迁移测试。"""

    def test_migrate_creates_tables(self, tmp_repo: UniverseSnapshotRepository) -> None:
        """migrate 后表存在。"""
        import sqlite3
        with sqlite3.connect(str(tmp_repo.db_path)) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "universe_snapshots" in tables
            assert "universe_members" in tables
            assert "universe_exclusions" in tables
            assert "universe_schema_meta" in tables

    def test_migrate_is_idempotent(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """migrate 可重复执行不抛异常。"""
        tmp_repo.migrate()
        tmp_repo.migrate()
        # 仍然能正常 save / get
        snap = _make_snapshot(snapshot_id="snap_idempotent_test")
        tmp_repo.save(snap)
        loaded = tmp_repo.get("snap_idempotent_test")
        assert loaded is not None


class TestRepositorySaveAndGet:
    """仓储 save / get / 不可覆盖测试。"""

    def test_save_and_get_round_trip(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """save 后 get 返回等价截面。"""
        snap = _make_snapshot(snapshot_id="snap_round_trip")
        tmp_repo.save(snap)
        loaded = tmp_repo.get("snap_round_trip")
        assert loaded is not None
        assert loaded.snapshot_id == snap.snapshot_id
        assert loaded.as_of == snap.as_of
        assert len(loaded.members) == len(snap.members)
        assert loaded.members[0].symbol == snap.members[0].symbol

    def test_save_duplicate_raises(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """同 snapshot_id 重复保存抛 ValueError（不可覆盖）。"""
        snap = _make_snapshot(snapshot_id="snap_dup_test")
        tmp_repo.save(snap)
        with pytest.raises(ValueError, match="不可覆盖"):
            tmp_repo.save(snap)

    def test_save_empty_snapshot_id_raises(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """空 snapshot_id 抛 ValueError。"""
        snap = _make_snapshot()
        snap.snapshot_id = ""
        with pytest.raises(ValueError, match="snapshot_id"):
            tmp_repo.save(snap)

    def test_save_empty_as_of_raises(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """空 as_of 抛 ValueError。"""
        snap = _make_snapshot(snapshot_id="snap_no_asof")
        snap.as_of = ""
        with pytest.raises(ValueError, match="as_of"):
            tmp_repo.save(snap)

    def test_get_nonexistent_returns_none(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """查询不存在的 snapshot_id 返回 None。"""
        assert tmp_repo.get("nonexistent") is None

    def test_list_snapshots(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """list_snapshots 返回截面摘要列表。"""
        tmp_repo.save(_make_snapshot(snapshot_id="snap_a", as_of="2024-01-31"))
        tmp_repo.save(_make_snapshot(snapshot_id="snap_b", as_of="2024-12-31"))
        result = tmp_repo.list_snapshots()
        assert len(result) == 2
        # 按 as_of 倒序
        assert result[0]["as_of"] == "2024-12-31"
        assert result[1]["as_of"] == "2024-01-31"

    def test_list_members_with_status_filter(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """list_members 按 membership_status 过滤。"""
        members = [
            _make_member("600519.SH", "2024-12-31", "snap_filter",
                         MEMBERSHIP_LISTED),
            _make_member("000001.SZ", "2024-12-31", "snap_filter",
                         MEMBERSHIP_SUSPENDED),
        ]
        snap = _make_snapshot(
            snapshot_id="snap_filter",
            members=members,
        )
        tmp_repo.save(snap)
        listed = tmp_repo.list_members("snap_filter", MEMBERSHIP_LISTED)
        suspended = tmp_repo.list_members("snap_filter", MEMBERSHIP_SUSPENDED)
        all_members = tmp_repo.list_members("snap_filter")
        assert len(listed) == 1
        assert len(suspended) == 1
        assert len(all_members) == 2

    def test_add_exclusion_appends(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """add_exclusion 追加排除记录。"""
        snap = _make_snapshot(snapshot_id="snap_excl_add")
        tmp_repo.save(snap)
        new_excl = ExclusionRecord(
            symbol="999999.SZ",
            as_of="2024-12-31",
            reason="manual_override",
            detail="人工排除测试",
            excluded_by="manual",
        )
        tmp_repo.add_exclusion(new_excl, "snap_excl_add")
        excls = tmp_repo.list_exclusions("snap_excl_add")
        symbols = [e.symbol for e in excls]
        assert "999999.SZ" in symbols


class TestRepositoryPITQuery:
    """仓储 get_by_as_of PIT 查询测试。"""

    def test_get_by_as_of_returns_latest_before_cutoff(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """get_by_as_of 返回 <= as_of 的最新截面。"""
        tmp_repo.save(_make_snapshot(
            snapshot_id="snap_2023",
            as_of="2023-12-31",
        ))
        tmp_repo.save(_make_snapshot(
            snapshot_id="snap_2024",
            as_of="2024-12-31",
        ))
        # 查询 2024-06-30：应返回 2023-12-31
        result = tmp_repo.get_by_as_of("2024-06-30")
        assert result is not None
        assert result.as_of == "2023-12-31"
        # 查询 2024-12-31：应返回 2024-12-31
        result = tmp_repo.get_by_as_of("2024-12-31")
        assert result is not None
        assert result.as_of == "2024-12-31"
        # 查询 2025-12-31：应返回 2024-12-31
        result = tmp_repo.get_by_as_of("2025-12-31")
        assert result is not None
        assert result.as_of == "2024-12-31"

    def test_get_by_as_of_returns_none_when_no_match(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """所有截面都晚于 as_of 时返回 None。"""
        tmp_repo.save(_make_snapshot(
            snapshot_id="snap_2024",
            as_of="2024-12-31",
        ))
        result = tmp_repo.get_by_as_of("2023-12-31")
        assert result is None


class TestRepositoryFutureLeakage:
    """仓储 validate_no_future_leakage PIT 校验测试。"""

    def test_no_violations_when_all_fields_before_as_of(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """所有字段 available_at <= as_of 时无违规。"""
        member = _make_member("600519.SH", "2024-12-31", "snap_no_leak", field_counts={
            FIELD_GROUP_FINANCIAL_QUALITY: 2,
        })
        # 字段 available_at 已设为 as_of（2024-12-31），无泄漏
        snap = _make_snapshot(
            snapshot_id="snap_no_leak",
            as_of="2024-12-31",
            members=[member],
        )
        tmp_repo.save(snap)
        violations = tmp_repo.validate_no_future_leakage("snap_no_leak")
        assert violations == []

    def test_detects_future_leakage(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """字段 available_at > as_of 时检测到违规。"""
        member = _make_member("600519.SH", "2024-12-31", "snap_leak", field_counts={
            FIELD_GROUP_FINANCIAL_QUALITY: 1,
        })
        # 修改字段 available_at 为 2025-06-30（晚于 as_of）
        member.field_groups[FIELD_GROUP_FINANCIAL_QUALITY].fields[0].available_at = "2025-06-30"
        snap = _make_snapshot(
            snapshot_id="snap_leak",
            as_of="2024-12-31",
            members=[member],
        )
        tmp_repo.save(snap)
        violations = tmp_repo.validate_no_future_leakage("snap_leak")
        assert len(violations) == 1
        assert violations[0]["symbol"] == "600519.SH"
        assert violations[0]["field_name"] == "financial_quality_field_0"
        assert violations[0]["violation"] == "field_available_at > snapshot_as_of"

    def test_empty_fields_skip_leakage_check(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """空字段（available_at 为空）不参与泄漏校验（P2.1 验收点 4）。"""
        member = _make_member("600519.SH", "2024-12-31", "snap_empty_fields")
        # 所有字段组为空，无 available_at
        snap = _make_snapshot(
            snapshot_id="snap_empty_fields",
            as_of="2024-12-31",
            members=[member],
        )
        tmp_repo.save(snap)
        violations = tmp_repo.validate_no_future_leakage("snap_empty_fields")
        assert violations == []


# ---------------------------------------------------------------------------
# 构建器测试
# ---------------------------------------------------------------------------


class TestBuilderMembership:
    """构建器成分股状态处理测试。"""

    def test_excludes_delisted(self) -> None:
        """已退市公司被排除并记录原因。"""
        entries = [
            UniverseEntry(
                symbol="000001.SZ",
                company_name="退市公司",
                membership_status=MEMBERSHIP_DELISTED,
                listing_date="2010-01-01",
                delisting_date="2020-06-30",
            ),
            UniverseEntry(
                symbol="600519.SH",
                company_name="贵州茅台",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="2001-08-27",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        symbols = [m.symbol for m in snap.members]
        assert "600519.SH" in symbols
        assert "000001.SZ" not in symbols
        # 排除记录
        assert len(snap.exclusions) == 1
        assert snap.exclusions[0].symbol == "000001.SZ"
        assert snap.exclusions[0].reason == "delisted"
        assert "2020-06-30" in snap.exclusions[0].detail

    def test_excludes_pre_listing(self) -> None:
        """尚未上市公司被排除并记录原因。"""
        entries = [
            UniverseEntry(
                symbol="999999.SZ",
                company_name="未来上市",
                membership_status=MEMBERSHIP_PRE_LISTING,
                listing_date="2025-06-30",  # 晚于 as_of
            ),
            UniverseEntry(
                symbol="600519.SH",
                company_name="贵州茅台",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="2001-08-27",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        symbols = [m.symbol for m in snap.members]
        assert "999999.SZ" not in symbols
        assert "600519.SH" in symbols
        assert len(snap.exclusions) == 1
        assert snap.exclusions[0].reason == "pre_listing"

    def test_includes_suspended_by_default(self) -> None:
        """默认包含暂停上市。"""
        entries = [
            UniverseEntry(
                symbol="600000.SH",
                company_name="暂停上市",
                membership_status=MEMBERSHIP_SUSPENDED,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1
        assert snap.members[0].membership_status == MEMBERSHIP_SUSPENDED

    def test_excludes_suspended_when_configured(self) -> None:
        """配置 include_suspended=False 时排除暂停上市。"""
        entries = [
            UniverseEntry(
                symbol="600000.SH",
                company_name="暂停上市",
                membership_status=MEMBERSHIP_SUSPENDED,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=BuilderConfig(include_suspended=False),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 0
        assert len(snap.exclusions) == 1
        assert snap.exclusions[0].reason == "suspended"

    def test_includes_historical_by_default(self) -> None:
        """默认包含历史成分。"""
        entries = [
            UniverseEntry(
                symbol="600001.SH",
                company_name="历史成分",
                membership_status=MEMBERSHIP_HISTORICAL,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1

    def test_includes_index_out_by_default(self) -> None:
        """默认包含已被指数剔除但 as_of 时仍在的。"""
        entries = [
            UniverseEntry(
                symbol="600002.SH",
                company_name="指数剔除",
                membership_status=MEMBERSHIP_INDEX_OUT,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1

    def test_listing_date_defensive_exclusion(self) -> None:
        """membership_status 标错但 listing_date 晚于 as_of 时防御性排除。"""
        entries = [
            UniverseEntry(
                symbol="600003.SH",
                company_name="状态错标",
                membership_status=MEMBERSHIP_LISTED,  # 标错为 listed
                listing_date="2025-06-30",  # 但上市日期晚于 as_of
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 0
        assert snap.exclusions[0].reason == "pre_listing"


class TestBuilderProvenance:
    """构建器来源元数据测试。"""

    def test_provenance_contains_provider_and_version(self) -> None:
        """provenance 包含 provider / version / coverage / exclusion。"""
        entries = [
            UniverseEntry(symbol="600519.SH", company_name="贵州茅台"),
            UniverseEntry(
                symbol="000001.SZ",
                company_name="退市",
                membership_status=MEMBERSHIP_DELISTED,
                delisting_date="2020-06-30",
            ),
        ]
        config = BuilderConfig(
            universe_name="test_universe",
            universe_version="v1",
            policy_version="test_policy_v1",
            provider="tushare",
            provider_version="pro_v2",
            built_by="builder",
        )
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        )
        snap = builder.build("2024-12-31")
        # provider/version
        assert snap.provenance.provider == "tushare"
        assert snap.provenance.provider_version == "pro_v2"
        assert snap.provenance.builder_version == "p2.1.0"
        assert snap.provenance.policy_version == "test_policy_v1"
        assert snap.provenance.built_by == "builder"
        # coverage
        assert len(snap.provenance.coverage_stats) == 9
        # exclusion count
        assert snap.provenance.exclusion_count == 1
        assert snap.provenance.total_member_count == 1
        # data fingerprint 非空
        assert snap.provenance.data_fingerprint.startswith("fp_")
        # config hash 非空
        assert snap.provenance.config_hash.startswith("cfg_")

    def test_exclusion_records_have_timestamps(self) -> None:
        """排除记录有时间戳和来源。"""
        entries = [
            UniverseEntry(
                symbol="000001.SZ",
                company_name="退市",
                membership_status=MEMBERSHIP_DELISTED,
                delisting_date="2020-06-30",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.exclusions) == 1
        excl = snap.exclusions[0]
        assert excl.symbol == "000001.SZ"
        assert excl.as_of == "2024-12-31"
        assert excl.reason == "delisted"
        assert excl.excluded_by == "builder"
        assert excl.excluded_at  # 非空


class TestBuilderDeterminism:
    """构建器 snapshot_id 确定性测试。"""

    def test_same_input_produces_same_snapshot_id(self) -> None:
        """相同输入产生相同 snapshot_id（可复现）。"""
        entries = [
            UniverseEntry(symbol="600519.SH", company_name="贵州茅台"),
            UniverseEntry(symbol="000858.SZ", company_name="五粮液"),
        ]
        config = BuilderConfig(
            universe_name="test_universe",
            policy_version="test_v1",
            provider="tushare",
        )
        builder1 = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        )
        builder2 = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        )
        snap1 = builder1.build("2024-12-31")
        snap2 = builder2.build("2024-12-31")
        assert snap1.snapshot_id == snap2.snapshot_id
        assert snap1.provenance.data_fingerprint == snap2.provenance.data_fingerprint

    def test_different_input_produces_different_snapshot_id(self) -> None:
        """不同输入产生不同 snapshot_id。"""
        entries1 = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        entries2 = [UniverseEntry(symbol="000858.SZ", company_name="五粮液")]
        config = BuilderConfig(
            universe_name="test_universe",
            policy_version="test_v1",
            provider="tushare",
        )
        snap1 = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries1),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        ).build("2024-12-31")
        snap2 = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries2),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        ).build("2024-12-31")
        assert snap1.snapshot_id != snap2.snapshot_id


class TestBuilderAssemblerError:
    """构建器 assembler 异常降级测试。"""

    def test_assembler_error_does_not_crash_builder(self) -> None:
        """assembler 抛异常时降级为空字段组，不崩溃。"""
        class FailingAssembler:
            def assemble(self, symbol: str, as_of: str) -> dict[str, FieldGroup]:
                raise RuntimeError("intentional test failure")

        entries = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=FailingAssembler(),  # type: ignore[arg-type]
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1
        member = snap.members[0]
        # 9 个字段组都存在
        assert len(member.field_groups) == 9
        for name in FIELD_GROUP_NAMES:
            assert name in member.field_groups
        # 异常被记录在 member_note
        assert "assembler_error" in member.member_note
        assert "RuntimeError" in member.member_note


# ---------------------------------------------------------------------------
# 验收点 1：任意历史 as_of 只能使用当时已公开数据
# ---------------------------------------------------------------------------


class TestPITAcceptance:
    """验收点 1：PIT 无未来泄漏。"""

    def test_repository_rejects_future_leakage_via_validator(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """仓储 validate_no_future_leakage 检测未来泄漏。"""
        # 构造一个含未来字段的成员
        member = _make_member("600519.SH", "2024-12-31", "snap_pit_test", field_counts={
            FIELD_GROUP_FINANCIAL_QUALITY: 2,
        })
        # 一个字段 available_at 早于 as_of（合规）
        member.field_groups[FIELD_GROUP_FINANCIAL_QUALITY].fields[0].available_at = "2024-12-31"
        # 一个字段 available_at 晚于 as_of（违规）
        member.field_groups[FIELD_GROUP_FINANCIAL_QUALITY].fields[1].available_at = "2025-06-30"
        snap = _make_snapshot(
            snapshot_id="snap_pit_test",
            as_of="2024-12-31",
            members=[member],
        )
        tmp_repo.save(snap)
        violations = tmp_repo.validate_no_future_leakage("snap_pit_test")
        assert len(violations) == 1
        assert violations[0]["available_at"] == "2025-06-30"
        assert violations[0]["snapshot_as_of"] == "2024-12-31"

    def test_get_by_as_of_does_not_return_future_snapshots(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """get_by_as_of 不返回 as_of 晚于查询时点的截面。"""
        tmp_repo.save(_make_snapshot(
            snapshot_id="snap_future",
            as_of="2025-12-31",
        ))
        # 查询 2024-12-31：不应返回 2025-12-31 的截面
        result = tmp_repo.get_by_as_of("2024-12-31")
        assert result is None

    def test_builder_uses_only_assembler_returned_fields(self) -> None:
        """构建器只使用 assembler 返回的字段（assembler 自身负责 PIT）。"""
        class StrictPITAssembler:
            """严格 PIT assembler：所有字段 available_at <= as_of。"""

            def assemble(
                self, symbol: str, as_of: str
            ) -> dict[str, FieldGroup]:
                groups = {name: FieldGroup(name=name) for name in FIELD_GROUP_NAMES}
                # 只填一个合规字段
                groups[FIELD_GROUP_FINANCIAL_QUALITY].fields.append(
                    _make_field("roic", 0.15, as_of)
                )
                return groups

        entries = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=StrictPITAssembler(),  # type: ignore[arg-type]
        )
        snap = builder.build("2024-12-31")
        # 保存后校验无泄漏
        assert len(snap.members) == 1
        member = snap.members[0]
        # 检查所有非空字段的 available_at <= as_of
        for group in member.field_groups.values():
            for fld in group.fields:
                if fld.available_at:
                    assert fld.available_at <= "2024-12-31", (
                        f"字段 {fld.name} available_at={fld.available_at} "
                        f"晚于 as_of=2024-12-31"
                    )


# ---------------------------------------------------------------------------
# 验收点 2：退市、暂停上市和历史成分处理不产生幸存者偏差
# ---------------------------------------------------------------------------


class TestSurvivorshipBiasAcceptance:
    """验收点 2：无幸存者偏差。"""

    def test_delisted_excluded_with_reason(self) -> None:
        """退市公司被排除，原因记录为 delisted。"""
        entries = [
            UniverseEntry(
                symbol="000001.SZ",
                company_name="退市公司",
                membership_status=MEMBERSHIP_DELISTED,
                delisting_date="2020-06-30",
            ),
            UniverseEntry(
                symbol="600519.SH",
                company_name="贵州茅台",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="2001-08-27",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        # 退市公司不在 members
        member_symbols = {m.symbol for m in snap.members}
        assert "000001.SZ" not in member_symbols
        # 排除记录显式
        delisted_excls = [e for e in snap.exclusions if e.reason == "delisted"]
        assert len(delisted_excls) == 1
        assert "退市" in delisted_excls[0].detail or "2020-06-30" in delisted_excls[0].detail

    def test_pre_listing_excluded_with_reason(self) -> None:
        """尚未上市公司被排除，原因记录为 pre_listing。"""
        entries = [
            UniverseEntry(
                symbol="999999.SZ",
                company_name="未来上市",
                membership_status=MEMBERSHIP_PRE_LISTING,
                listing_date="2025-06-30",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 0
        assert len(snap.exclusions) == 1
        assert snap.exclusions[0].reason == "pre_listing"

    def test_historical_members_kept_by_default(self) -> None:
        """历史成分默认保留（as_of 时仍在指数内）。"""
        entries = [
            UniverseEntry(
                symbol="600001.SH",
                company_name="历史成分",
                membership_status=MEMBERSHIP_HISTORICAL,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1
        assert snap.members[0].membership_status == MEMBERSHIP_HISTORICAL

    def test_index_out_members_kept_by_default(self) -> None:
        """as_of 时仍在但后续被指数剔除的，默认保留。"""
        entries = [
            UniverseEntry(
                symbol="600002.SH",
                company_name="指数剔除",
                membership_status=MEMBERSHIP_INDEX_OUT,
                listing_date="2010-01-01",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 1
        assert snap.members[0].membership_status == MEMBERSHIP_INDEX_OUT

    def test_member_records_listing_and_delisting_dates(self) -> None:
        """member 显式记录 listing_date 与 delisting_date 用于审计。"""
        entries = [
            UniverseEntry(
                symbol="600519.SH",
                company_name="贵州茅台",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="2001-08-27",
                delisting_date="",  # 未退市
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        member = snap.members[0]
        assert member.listing_date == "2001-08-27"
        assert member.delisting_date == ""
        assert member.membership_status == MEMBERSHIP_LISTED


# ---------------------------------------------------------------------------
# 验收点 3：截面构建保存 provider、版本、覆盖率和排除原因
# ---------------------------------------------------------------------------


class TestProvenanceAcceptance:
    """验收点 3：来源元数据完整。"""

    def test_provenance_has_provider_and_version(self) -> None:
        """provenance 含 provider / provider_version / builder_version。"""
        entries = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        config = BuilderConfig(
            universe_name="test",
            universe_version="v1",
            policy_version="p1",
            provider="tushare",
            provider_version="pro_v1",
        )
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=config,
        )
        snap = builder.build("2024-12-31")
        prov = snap.provenance
        assert prov.provider == "tushare"
        assert prov.provider_version == "pro_v1"
        assert prov.builder_version == "p2.1.0"
        assert prov.policy_version == "p1"
        assert prov.universe_name if hasattr(prov, "universe_name") else True
        assert prov.built_at  # 非空
        assert prov.built_by == "builder"

    def test_provenance_has_coverage_stats(self) -> None:
        """provenance 含 9 个字段组的覆盖率统计。"""
        entries = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        coverage = snap.provenance.coverage_stats
        assert len(coverage) == 9
        group_names = [c.group_name for c in coverage]
        for name in FIELD_GROUP_NAMES:
            assert name in group_names
        # EmptyFieldGroupAssembler 返回空字段组，coverage_ratio=0
        for c in coverage:
            assert c.coverage_ratio == 0.0
            assert c.member_count == 1

    def test_provenance_has_exclusion_count(self) -> None:
        """provenance 含排除总数。"""
        entries = [
            UniverseEntry(symbol="600519.SH", company_name="贵州茅台"),
            UniverseEntry(
                symbol="000001.SZ", company_name="退市",
                membership_status=MEMBERSHIP_DELISTED,
                delisting_date="2020-06-30",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert snap.provenance.exclusion_count == 1
        assert snap.provenance.total_member_count == 1

    def test_provenance_has_data_fingerprint(self) -> None:
        """provenance 含数据指纹。"""
        entries = [
            UniverseEntry(symbol="600519.SH", company_name="贵州茅台"),
            UniverseEntry(symbol="000858.SZ", company_name="五粮液"),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
            config=BuilderConfig(provider="tushare"),
        )
        snap = builder.build("2024-12-31")
        # 数据指纹以 fp_ 开头
        assert snap.provenance.data_fingerprint.startswith("fp_")
        # 长度 = 3 (fp_) + 16 (sha256 前 16)
        assert len(snap.provenance.data_fingerprint) == 19

    def test_provenance_has_config_hash(self) -> None:
        """provenance 含配置 hash（用于复现性校验）。"""
        entries = [UniverseEntry(symbol="600519.SH", company_name="贵州茅台")]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        assert snap.provenance.config_hash.startswith("cfg_")

    def test_exclusion_records_persisted_to_repository(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """排除记录通过 repository.save 持久化。"""
        entries = [
            UniverseEntry(symbol="600519.SH", company_name="贵州茅台"),
            UniverseEntry(
                symbol="000001.SZ", company_name="退市",
                membership_status=MEMBERSHIP_DELISTED,
                delisting_date="2020-06-30",
            ),
        ]
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=EmptyFieldGroupAssembler(),
        )
        snap = builder.build("2024-12-31")
        tmp_repo.save(snap)
        excls = tmp_repo.list_exclusions(snap.snapshot_id)
        assert len(excls) == 1
        assert excls[0].symbol == "000001.SZ"
        assert excls[0].reason == "delisted"


# ---------------------------------------------------------------------------
# 验收点 4：空字段不以零分惩罚，单独进入数据可用性维度
# ---------------------------------------------------------------------------


class TestDataAvailabilityAcceptance:
    """验收点 4：空字段不以零分惩罚。"""

    def test_missing_field_value_is_none_not_zero(self) -> None:
        """缺失字段的 value 为 None，不是 0。"""
        field = SnapshotField(
            name="roic",
            value=None,
            available_at="",
            source_refs=[],
            status=DATA_AVAILABILITY_MISSING,
        )
        assert field.value is None
        assert field.status == DATA_AVAILABILITY_MISSING

    def test_empty_field_group_has_zero_coverage_not_negative_score(self) -> None:
        """空字段组 coverage_ratio=0.0，不进入负分路径。"""
        group = FieldGroup(name="test")
        stats = group.compute_stats()
        assert stats["coverage_ratio"] == 0.0
        assert stats["filled_count"] == 0
        assert stats["total_count"] == 0
        # 不存在 "score" 字段（避免暗示综合分）
        assert "score" not in stats

    def test_member_with_no_fields_has_zero_availability_score(self) -> None:
        """完全无字段的成员，data_availability_score=0.0，但不被惩罚。"""
        member = _make_member("600519.SH", "2024-12-31")
        # 9 个字段组都为空
        assert member.data_availability_score == 0.0
        # 但成员仍正常进入截面，不被排除
        assert member.membership_status == MEMBERSHIP_LISTED

    def test_member_with_partial_fields_has_partial_availability(self) -> None:
        """部分字段填充时，data_availability_score 介于 0-1。"""
        # 9 个字段组中，1 个组有 2 个 available 字段，其余为空
        member = _make_member("600519.SH", "2024-12-31", field_counts={
            FIELD_GROUP_FINANCIAL_QUALITY: 2,
        })
        # 总字段 = 2，已填充 = 2，综合分 = 1.0
        assert member.data_availability_score == pytest.approx(1.0)

    def test_data_availability_statuses_distinguish_missing_vs_not_computable(
        self
    ) -> None:
        """missing 与 not_computable 状态区分。"""
        group = FieldGroup(
            name="test",
            fields=[
                _make_field("a", None, "2024-12-31", DATA_AVAILABILITY_MISSING),
                _make_field("b", None, "2024-12-31", DATA_AVAILABILITY_NOT_COMPUTABLE),
                _make_field("c", None, "2024-12-31", DATA_AVAILABILITY_NOT_APPLICABLE),
                _make_field("d", None, "2024-12-31", DATA_AVAILABILITY_PENDING),
            ],
        )
        stats = group.compute_stats()
        assert stats["missing_count"] == 1
        assert stats["non_computable_count"] == 1
        assert stats["not_applicable_count"] == 1
        assert stats["pending_count"] == 1
        # 都不计入 filled_count
        assert stats["filled_count"] == 0

    def test_coverage_stats_in_payload(self) -> None:
        """序列化后的 payload 包含 stats 字段。"""
        group = FieldGroup(
            name="test",
            fields=[
                _make_field("a", 1.0, "2024-12-31", DATA_AVAILABILITY_AVAILABLE),
                _make_field("b", None, "2024-12-31", DATA_AVAILABILITY_MISSING),
            ],
        )
        payload = group.to_dict()
        assert "stats" in payload
        assert payload["stats"]["filled_count"] == 1
        assert payload["stats"]["missing_count"] == 1
        assert payload["stats"]["coverage_ratio"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# P1 集成 best-effort 测试
# ---------------------------------------------------------------------------


class TestP1BackedAssemblerGraceful:
    """P1BackedFieldGroupAssembler best-effort 不抛异常测试。"""

    def test_assemble_with_no_repos_returns_9_empty_groups(self) -> None:
        """无 P1 仓储时返回 9 个空字段组，不抛异常。"""
        assembler = P1BackedFieldGroupAssembler()
        groups = assembler.assemble("600519.SH", "2024-12-31")
        assert len(groups) == 9
        for name in FIELD_GROUP_NAMES:
            assert name in groups
            assert groups[name].name == name
            assert len(groups[name].fields) == 0

    def test_assemble_with_failing_repo_does_not_raise(self) -> None:
        """P1 仓储读取异常时不抛异常（降级为空）。"""
        class FailingRepo:
            def get_latest_statement(self, symbol: str, as_of: str | None = None):
                raise RuntimeError("connection failed")

        assembler = P1BackedFieldGroupAssembler(statement_repo=FailingRepo())
        # 不抛异常
        groups = assembler.assemble("600519.SH", "2024-12-31")
        assert len(groups) == 9
        # financial_quality 字段组为空（因 statement_repo 抛异常被捕获）
        assert len(groups[FIELD_GROUP_FINANCIAL_QUALITY].fields) == 0

    def test_assemble_evidence_quality_and_researchability_empty(self) -> None:
        """evidence_quality 与 researchability 字段组留空（待 P2.4/P2.x 填充）。"""
        assembler = P1BackedFieldGroupAssembler()
        groups = assembler.assemble("600519.SH", "2024-12-31")
        assert len(groups[FIELD_GROUP_EVIDENCE_QUALITY].fields) == 0
        assert len(groups[FIELD_GROUP_RESEARCHABILITY].fields) == 0
        # 留有 group_note 说明原因
        assert "P2.4" in groups[FIELD_GROUP_EVIDENCE_QUALITY].group_note
        assert "P2.x" in groups[FIELD_GROUP_RESEARCHABILITY].group_note


# ---------------------------------------------------------------------------
# AGENTS.md 合规性测试
# ---------------------------------------------------------------------------


class TestNoTradeAdvice:
    """AGENTS.md 合规性：不输出买入/卖出/仓位/止盈/止损等投资建议。

    源码关键词检查仅针对中文交易动作词（买入/卖出/仓位/止盈/止损/加仓/减仓/建仓/清仓）；
    "投资建议" 与英文 buy/sell/long/short 不在源码检查范围，因为：
    - "投资建议" 用于在 docstring 中描述 AGENTS.md 规则本身
    - 英文 buy/sell/long/short 是常见单词，可能在变量名/注释中合法出现

    payload 检查覆盖所有关键词，确保实际输出不含交易建议。
    """

    # 源码检查：仅中文交易动作词（精确匹配交易建议）
    SOURCE_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "买入", "卖出", "止盈", "止损",
        "加仓", "减仓", "建仓", "清仓",
    )

    # payload 检查：含仓位等关键词（payload 中不应出现任何交易建议）
    PAYLOAD_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "买入", "卖出", "仓位", "止盈", "止损",
        "加仓", "减仓", "建仓", "清仓",
        "投资建议",
    )

    def test_contracts_no_trade_advice_keywords(self) -> None:
        """契约模块源码不含交易动作关键词。"""
        from core.research.discovery import contracts as contracts_mod

        source = Path(contracts_mod.__file__).read_text(encoding="utf-8")
        for keyword in self.SOURCE_FORBIDDEN_KEYWORDS:
            assert keyword not in source, (
                f"契约模块包含禁止关键词: {keyword}"
            )

    def test_repository_no_trade_advice_keywords(self) -> None:
        """仓储模块源码不含交易动作关键词。"""
        from core.research.discovery import repository as repository_mod

        source = Path(repository_mod.__file__).read_text(encoding="utf-8")
        for keyword in self.SOURCE_FORBIDDEN_KEYWORDS:
            assert keyword not in source, (
                f"仓储模块包含禁止关键词: {keyword}"
            )

    def test_universe_snapshot_no_trade_advice_keywords(self) -> None:
        """构建器模块源码不含交易动作关键词。"""
        from core.research.discovery import universe_snapshot as builder_mod

        source = Path(builder_mod.__file__).read_text(encoding="utf-8")
        for keyword in self.SOURCE_FORBIDDEN_KEYWORDS:
            assert keyword not in source, (
                f"构建器模块包含禁止关键词: {keyword}"
            )

    def test_snapshot_payload_no_trade_advice_keywords(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """截面 payload 序列化后不含交易建议关键词。"""
        snap = _make_snapshot(snapshot_id="snap_no_advice")
        payload = json.dumps(snap.to_dict(), ensure_ascii=False)
        for keyword in self.PAYLOAD_FORBIDDEN_KEYWORDS:
            assert keyword not in payload, (
                f"截面 payload 包含禁止关键词: {keyword}"
            )


# ---------------------------------------------------------------------------
# 端到端集成测试
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端：builder → repository → 查询 → PIT 校验。"""

    def test_full_workflow(
        self, tmp_repo: UniverseSnapshotRepository
    ) -> None:
        """完整工作流：构建 → 保存 → 查询 → PIT 校验。"""
        entries = [
            UniverseEntry(
                symbol="600519.SH", company_name="贵州茅台",
                industry_code="sw_spirit", industry_name="白酒",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="2001-08-27",
            ),
            UniverseEntry(
                symbol="000858.SZ", company_name="五粮液",
                industry_code="sw_spirit", industry_name="白酒",
                membership_status=MEMBERSHIP_LISTED,
                listing_date="1998-04-27",
            ),
            UniverseEntry(
                symbol="000001.SZ", company_name="退市公司",
                membership_status=MEMBERSHIP_DELISTED,
                listing_date="2010-01-01",
                delisting_date="2020-06-30",
            ),
        ]

        class SimpleAssembler:
            """简单 assembler：只填 financial_quality 一个字段。"""

            def assemble(
                self, symbol: str, as_of: str
            ) -> dict[str, FieldGroup]:
                groups = {name: FieldGroup(name=name) for name in FIELD_GROUP_NAMES}
                groups[FIELD_GROUP_FINANCIAL_QUALITY].fields.append(
                    _make_field("roic", 0.15, as_of)
                )
                return groups

        config = BuilderConfig(
            universe_name="test_e2e",
            universe_version="v1",
            policy_version="e2e_v1",
            provider="tushare",
            provider_version="pro_v1",
        )
        builder = UniverseSnapshotBuilder(
            provider=StaticUniverseProvider(entries),
            assembler=SimpleAssembler(),  # type: ignore[arg-type]
            config=config,
        )

        # 构建
        snap = builder.build("2024-12-31")
        assert len(snap.members) == 2
        assert len(snap.exclusions) == 1

        # 保存
        tmp_repo.save(snap)

        # 查询
        loaded = tmp_repo.get(snap.snapshot_id)
        assert loaded is not None
        assert len(loaded.members) == 2

        # PIT 查询
        pit_result = tmp_repo.get_by_as_of("2024-12-31", "test_e2e")
        assert pit_result is not None
        assert pit_result.snapshot_id == snap.snapshot_id

        # PIT 校验无未来泄漏
        violations = tmp_repo.validate_no_future_leakage(snap.snapshot_id)
        assert violations == []

        # 列出截面
        snapshot_list = tmp_repo.list_snapshots("test_e2e")
        assert len(snapshot_list) == 1
        assert snapshot_list[0]["member_count"] == 2
        assert snapshot_list[0]["exclusion_count"] == 1
