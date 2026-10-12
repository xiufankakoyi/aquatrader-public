"""每日研究构建链路测试。

覆盖：
- Task 3: _preflight_premarket_sources 检查真实主题候选源
- Task 5: _theme_payload 拆分 policy_validated_count 与 core_role_count
- Task 6: build_and_save_universe_snapshot 幂等与降级
- Task 7: 缺少真实盈利证据时保持降级
- Task 8: 缺少真实 P1 模型时保持降级
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 加载 daemon 模块（非包模块）
_DAEMON_PATH = PROJECT_ROOT / "scripts" / "run_research_monitor_daemon.py"
_spec = importlib.util.spec_from_file_location("run_research_monitor_daemon", _DAEMON_PATH)
assert _spec is not None and _spec.loader is not None
daemon_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon_module)

MARKET_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------- #
# Task 3: _preflight_premarket_sources 源预检
# ---------------------------------------------------------------------- #


def test_preflight_returns_not_ok_when_no_snapshot(monkeypatch, tmp_path: Path) -> None:
    """真实主题源没有板块时预检返回 ok=False。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _EmptyRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="",
                source_as_of="",
                boards=[],
                degraded_reasons=["all_sources_failed_no_last_good"],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _EmptyRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert result["reason"] == "no_theme_snapshot"
    assert result["snapshot_id"] == ""


def test_preflight_returns_not_ok_when_snapshot_stale(monkeypatch, tmp_path: Path) -> None:
    """snapshot as_of 距 trade_date 超过阈值时返回 ok=False。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _StaleRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_stale",
                source_as_of="2026-07-01",
                boards=[SimpleNamespace(members=[f"{i:06d}" for i in range(100)])],
                degraded_reasons=["using_last_good_snapshot"],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _StaleRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert "snapshot_stale" in result["reason"]
    assert result["source_as_of"] == "2026-07-01"


def test_preflight_returns_not_ok_when_insufficient_members(
    monkeypatch, tmp_path: Path
) -> None:
    """snapshot member_count 低于阈值时返回 ok=False。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _LowMemberRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_low",
                source_as_of="2026-07-25",
                boards=[SimpleNamespace(members=[f"{i:06d}" for i in range(10)])],
                degraded_reasons=[],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _LowMemberRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert "insufficient_members" in result["reason"]
    assert result["member_count"] == 10


def test_preflight_returns_ok_when_snapshot_fresh_and_sufficient(
    monkeypatch, tmp_path: Path
) -> None:
    """snapshot 新鲜且成员数达标时返回 ok=True。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _GoodRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_good",
                source_as_of="2026-07-25",
                boards=[
                    SimpleNamespace(members=[f"{i:06d}" for i in range(200)])
                ],
                degraded_reasons=[],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _GoodRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is True
    assert result["reason"] == "ok"
    assert result["snapshot_id"] == "snap_good"
    assert result["member_count"] == 200


def test_preflight_returns_not_ok_when_repo_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    """真实主题源不可用时返回 ok=False。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    def _raise(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        _raise,
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert "theme_source_unavailable" in result["reason"]


def test_preflight_rejects_future_theme_snapshot(monkeypatch, tmp_path: Path) -> None:
    """PIT 预检不得接受未来主题快照。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _FutureRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_future",
                source_as_of="2026-07-27",
                boards=[SimpleNamespace(members=[f"{i:06d}" for i in range(100)])],
                degraded_reasons=[],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _FutureRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert "snapshot_from_future" in result["reason"]


def test_preflight_accepts_compact_source_as_of(monkeypatch, tmp_path: Path) -> None:
    """版本化主题仓储的 YYYYMMDD 日期也应正确参与新鲜度校验。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _CompactDateRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_compact",
                source_as_of="20260725",
                boards=[SimpleNamespace(members=[f"{i:06d}" for i in range(100)])],
                degraded_reasons=[],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _CompactDateRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is True


def test_preflight_degrades_when_board_members_are_missing(
    monkeypatch, tmp_path: Path
) -> None:
    """外部主题源字段缺失时应降级，不能让 daemon 崩溃。"""
    daemon = daemon_module.ResearchMonitorDaemon(
        output_dir=tmp_path,
        state_file=tmp_path / "state.json",
    )

    class _MissingMembersRouter:
        def fetch_concept_snapshot(self, trade_date: str) -> Any:
            return SimpleNamespace(
                snapshot_id="snap_missing_members",
                source_as_of="2026-07-25",
                boards=[SimpleNamespace(members=None)],
                degraded_reasons=["provider_missing_members"],
            )

    monkeypatch.setattr(
        "core.quant.theme.source_router.ThemeSourceRouter",
        lambda *a, **kw: _MissingMembersRouter(),
    )

    result = daemon._preflight_premarket_sources("2026-07-26")
    assert result["ok"] is False
    assert "insufficient_members" in result["reason"]


# ---------------------------------------------------------------------- #
# Task 6: build_and_save_universe_snapshot 幂等与降级
# ---------------------------------------------------------------------- #


def test_build_and_save_universe_snapshot_skipped_when_force_false_existing(
    monkeypatch, tmp_path: Path
) -> None:
    """force=False 且同 as_of 已有 snapshot 时返回 skipped。"""
    from core.research.discovery import daily_build

    class _ExistingRepo:
        def get_by_as_of(self, as_of: str, universe_name: str = "") -> Any:
            return SimpleNamespace(
                snapshot_id="snap_existing",
                as_of="2026-07-26",
                members=[1, 2, 3],
                exclusions=[],
            )

    monkeypatch.setattr(
        daily_build, "UniverseSnapshotRepository", lambda *a, **kw: _ExistingRepo()
    )

    result = daily_build.build_and_save_universe_snapshot(
        "2026-07-26", force=False, repo=_ExistingRepo()
    )
    assert result["status"] == "skipped"
    assert result["snapshot_id"] == "snap_existing"
    assert result["member_count"] == 3


def test_build_does_not_treat_older_snapshot_as_same_day(tmp_path: Path) -> None:
    """历史最近快照不能让新的交易日永久跳过。"""
    from core.research.discovery import daily_build
    from core.research.discovery.universe_snapshot import (
        EmptyFieldGroupAssembler,
        UniverseEntry,
    )

    class _Provider:
        def list_universe(self, as_of: str) -> list[Any]:
            return [UniverseEntry(symbol="000001", company_name="测试公司")]

    class _Repo:
        def __init__(self) -> None:
            self.saved = None

        def get_by_as_of(self, as_of: str, universe_name: str = "") -> Any:
            return SimpleNamespace(
                snapshot_id="snap_old",
                as_of="2026-07-25",
                members=[1],
                exclusions=[],
            )

        def save(self, snapshot: Any) -> None:
            self.saved = snapshot

    repo = _Repo()
    result = daily_build.build_and_save_universe_snapshot(
        "2026-07-26",
        force=False,
        provider=_Provider(),
        assembler=EmptyFieldGroupAssembler(),
        repo=repo,
    )

    assert result["status"] == "ok"
    assert repo.saved is not None


def test_build_and_save_universe_snapshot_handles_provider_failure(
    monkeypatch, tmp_path: Path
) -> None:
    """provider 返回空列表时拒绝保存空截面。"""
    from core.research.discovery import daily_build
    from core.research.discovery.universe_snapshot import (
        BuilderConfig,
        EmptyFieldGroupAssembler,
        UniverseSnapshotBuilder,
    )

    class _EmptyProvider:
        def list_universe(self, as_of: str) -> list:
            return []

    class _SaveRepo:
        def __init__(self) -> None:
            self.saved = None
            self._existing = None

        def get_by_as_of(self, as_of: str, universe_name: str = "") -> Any:
            return self._existing

        def save(self, snapshot: Any) -> None:
            self.saved = snapshot

    repo = _SaveRepo()
    result = daily_build.build_and_save_universe_snapshot(
        "2026-07-26",
        force=True,
        provider=_EmptyProvider(),
        assembler=EmptyFieldGroupAssembler(),
        repo=repo,
    )
    assert result["status"] == "failed"
    assert result["reason"] == "empty_universe"
    assert result["member_count"] == 0
    assert repo.saved is None


def test_all_a_share_provider_falls_back_to_local_pit_daily_pool(
    monkeypatch,
) -> None:
    from core.research.discovery import daily_build

    class _BrokenAkshare:
        @staticmethod
        def stock_zh_a_spot_em():
            raise RuntimeError("external source unavailable")

    class _LocalQuery:
        def get_trade_dates(self, start: str, end: str) -> list[str]:
            assert end == "2026-07-28"
            return ["2026-07-24", "2026-07-28"]

        def get_stock_pool(self, date: str):
            import pandas as pd

            assert date == "2026-07-28"
            return pd.DataFrame(
                [
                    {
                        "stock_code": "600519.SH",
                        "trade_date": "2026-07-28",
                    },
                    {
                        "stock_code": "000001.SZ",
                        "trade_date": "2026-07-28",
                    },
                ]
            )

    monkeypatch.setitem(__import__("sys").modules, "akshare", _BrokenAkshare())
    provider = daily_build.AllAShareUniverseProvider(
        local_query=_LocalQuery()
    )

    entries = provider.list_universe("2026-07-28")

    assert [entry.symbol for entry in entries] == [
        "600519.SH",
        "000001.SZ",
    ]
    assert provider.last_provider == "local_daily_ohlcv"
    assert provider.last_source_as_of == "2026-07-28"


def test_local_universe_fallback_never_reads_after_snapshot_date(
    monkeypatch,
) -> None:
    from core.research.discovery import daily_build

    class _BrokenAkshare:
        @staticmethod
        def stock_zh_a_spot_em():
            raise RuntimeError("external source unavailable")

    class _LocalQuery:
        def get_trade_dates(self, start: str, end: str) -> list[str]:
            assert end == "2026-07-24"
            return ["2026-07-23", "2026-07-24"]

        def get_stock_pool(self, date: str):
            import pandas as pd

            return pd.DataFrame(
                [{"stock_code": "600519.SH", "trade_date": date}]
            )

    monkeypatch.setitem(__import__("sys").modules, "akshare", _BrokenAkshare())
    provider = daily_build.AllAShareUniverseProvider(
        local_query=_LocalQuery()
    )

    provider.list_universe("2026-07-24")

    assert provider.last_source_as_of == "2026-07-24"


def test_universe_provider_uses_polars_row_iterator() -> None:
    import polars as pl

    from core.research.discovery.daily_build import AllAShareUniverseProvider

    frame = pl.DataFrame(
        {
            "code": ["600519", "000001", "830001"],
            "name": ["贵州茅台", "平安银行", "北交所样本"],
        }
    )
    entries = AllAShareUniverseProvider()._entries_from_frame(frame)

    assert [entry.symbol for entry in entries] == ["600519.SH", "000001.SZ"]
    assert entries[0].company_name == "贵州茅台"


# ---------------------------------------------------------------------- #
# Task 7: 缺少真实盈利证据时保持降级
# ---------------------------------------------------------------------- #


def test_produce_daily_earnings_drivers_skipped_when_no_research_repo(
    monkeypatch, tmp_path: Path
) -> None:
    """ResearchRepository 不可用时返回 skipped。"""
    from core.research.earnings import daily_producer

    def _raise(*a, **kw):
        raise ImportError("module not found")

    monkeypatch.setattr(
        "core.research.control_plane.repository.ResearchRepository",
        _raise,
    )

    result = daily_producer.produce_daily_earnings_drivers("2026-07-26")
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_real_earnings_driver_evidence"
    assert result["driver_count"] == 0


def test_produce_daily_earnings_drivers_skipped_when_no_active_claims(
    monkeypatch, tmp_path: Path
) -> None:
    """无 ACTIVE Claim 时返回 skipped。"""
    from core.research.earnings import daily_producer

    class _EmptyResearchRepo:
        def list_claims(self, limit: int = 100) -> list:
            return []

    class _EmptyEarningsRepo:
        def get_driver(self, driver_id: str) -> Any:
            return None

        def save_driver(self, item: Any) -> None:
            pass

        def save_assumption(self, item: Any) -> None:
            pass

    result = daily_producer.produce_daily_earnings_drivers(
        "2026-07-26",
        research_repo=_EmptyResearchRepo(),
        earnings_repo=_EmptyEarningsRepo(),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_real_earnings_driver_evidence"


def test_produce_daily_earnings_drivers_does_not_infer_from_theme_claim(
    monkeypatch, tmp_path: Path
) -> None:
    """主题成员关系不能被解释成真实盈利驱动。"""
    from core.research.earnings import daily_producer

    class _FakeClaim:
        def __init__(self) -> None:
            self.claim_id = "claim_theme_abc123def456"
            self.research_status = "ACTIVE"
            self.relation = "member_of_theme"
            self.subject_id = "000001"
            self.subject_name = "测试公司"
            self.created_at = "2026-07-26T08:00:00+08:00"
            self.attributes = {
                "theme_id": "theme_test",
                "theme_name": "测试主题",
                "market_role": "core",
                "market_features": {"leadership_score": 0.85},
            }

    class _FakeResearchRepo:
        def list_claims(self, limit: int = 100) -> list:
            return [_FakeClaim()]

    class _FakeEarningsRepo:
        def __init__(self) -> None:
            self.drivers: dict[str, Any] = {}
            self.assumptions: dict[str, Any] = {}

        def get_driver(self, driver_id: str) -> Any:
            return self.drivers.get(driver_id)

        def save_driver(self, item: Any) -> None:
            self.drivers[item.driver_id] = item

        def save_assumption(self, item: Any) -> None:
            self.assumptions[item.assumption_id] = item

    earnings_repo = _FakeEarningsRepo()
    result = daily_producer.produce_daily_earnings_drivers(
        "2026-07-26",
        research_repo=_FakeResearchRepo(),
        earnings_repo=earnings_repo,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_real_earnings_driver_evidence"
    assert earnings_repo.drivers == {}
    assert earnings_repo.assumptions == {}


# ---------------------------------------------------------------------- #
# Task 8: 缺少真实 P1 模型时保持降级
# ---------------------------------------------------------------------- #


def test_produce_daily_variant_perceptions_skipped_when_no_research_repo(
    monkeypatch, tmp_path: Path
) -> None:
    """ResearchRepository 不可用时返回 skipped。"""
    from core.research.thesis import daily_producer

    def _raise(*a, **kw):
        raise ImportError("module not found")

    monkeypatch.setattr(
        "core.research.control_plane.repository.ResearchRepository",
        _raise,
    )

    result = daily_producer.produce_daily_variant_perceptions("2026-07-26")
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_real_p1_operating_model"
    assert result["thesis_count"] == 0


def test_produce_daily_variant_perceptions_skipped_when_no_active_claims(
    monkeypatch, tmp_path: Path
) -> None:
    """无 ACTIVE Claim 时返回 skipped。"""
    from core.research.thesis import daily_producer

    class _EmptyResearchRepo:
        def list_claims(self, limit: int = 100) -> list:
            return []

    class _EmptyExpectationRepo:
        def list_consensus_summaries(self, *, company_symbol: str = "", limit: int = 100) -> list:
            return []

    class _EmptyThesisRepo:
        def list_latest_by_symbol(self, *, symbol: str, as_of: str = "", limit: int = 100) -> list:
            return []

        def save(self, thesis: Any) -> None:
            pass

    result = daily_producer.produce_daily_variant_perceptions(
        "2026-07-26",
        research_repo=_EmptyResearchRepo(),
        expectation_repo=_EmptyExpectationRepo(),
        thesis_repo=_EmptyThesisRepo(),
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_real_p1_operating_model"


def test_produce_daily_variant_perceptions_skipped_when_no_consensus(
    monkeypatch, tmp_path: Path
) -> None:
    """有 ACTIVE Claim 但无 consensus 时返回 skipped_no_consensus。"""
    from core.research.thesis import daily_producer

    class _FakeClaim:
        def __init__(self) -> None:
            self.claim_id = "claim_theme_abc123def456"
            self.research_status = "ACTIVE"
            self.relation = "member_of_theme"
            self.subject_id = "000001"
            self.subject_name = "测试公司"
            self.created_at = "2026-07-26T08:00:00+08:00"
            self.attributes = {
                "theme_id": "theme_test",
                "theme_name": "测试主题",
                "market_role": "core",
                "market_features": {"leadership_score": 0.85},
            }

    class _FakeResearchRepo:
        def list_claims(self, limit: int = 100) -> list:
            return [_FakeClaim()]

    class _EmptyExpectationRepo:
        def list_consensus_summaries(self, *, company_symbol: str = "", limit: int = 100) -> list:
            return []

    class _EmptyThesisRepo:
        def list_latest_by_symbol(self, *, symbol: str, as_of: str = "", limit: int = 100) -> list:
            return []

        def save(self, thesis: Any) -> None:
            pass

    result = daily_producer.produce_daily_variant_perceptions(
        "2026-07-26",
        research_repo=_FakeResearchRepo(),
        expectation_repo=_EmptyExpectationRepo(),
        thesis_repo=_EmptyThesisRepo(),
    )
    assert result["status"] == "skipped"
    assert result["thesis_count"] == 0
    assert result["skipped_no_consensus"] == 0
    assert result["reason"] == "missing_real_p1_operating_model"


# ---------------------------------------------------------------------- #
# Task 5: _theme_payload 拆分 policy_validated_count 与 core_role_count
# ---------------------------------------------------------------------- #


def test_theme_payload_splits_policy_validated_and_core_role_counts() -> None:
    """_theme_payload 应分别输出 policy_validated_count 与 core_role_count。

    场景：3 个成员 review，2 个 validated（1 个 core、1 个 secondary），
    1 个 pending_evidence。
    期望：policy_validated_count=2, core_role_count=1, validated_core_count=1（别名）
    """
    from core.runtime.research_monitor import ResearchMonitor

    class _FakePacket:
        theme_id = "theme_test"
        theme_run_id = "run_test"
        theme_name = "测试主题"
        theme_type = "news_persistence"
        phase = "acceleration"
        phase_confidence = 0.7
        market_features = {}
        negative_risk_score = 0.2
        members = [1, 2, 3]  # 3 个成员
        core_members = [1]  # 1 个 core
        source_snapshot_id = "snap_test"
        source_status = {}
        degraded = False
        degradation_reasons = []
        strategy_version = "v1"

    item = {
        "enhanced": _FakePacket(),
        "summary": {
            "negative_status": "unknown",
            "research_gate_status": "validated",
            "member_reviews": [
                {"symbol": "000001", "status": "validated", "market_role": "core"},
                {"symbol": "000002", "status": "validated", "market_role": "secondary"},
                {"symbol": "000003", "status": "pending_evidence"},
            ],
            "generated_task_ids": [],
        },
    }

    # _theme_payload 是实例方法但内部不使用 self；用 __new__ 跳过 __init__
    # 避免触发 ResearchMonitor 的重型依赖初始化。
    monitor = ResearchMonitor.__new__(ResearchMonitor)
    payload = monitor._theme_payload(item)
    assert payload["policy_validated_count"] == 2
    assert payload["core_role_count"] == 1
    # 向后兼容别名
    assert payload["validated_core_count"] == 1
    assert payload["member_count"] == 3
