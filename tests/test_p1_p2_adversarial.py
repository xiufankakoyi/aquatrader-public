"""P1.6/P2.5/P2.6/P2.7 对抗测试。

覆盖用户 P1/P2 验收要求的 5 类对抗场景：
- 同内容跨日快照（P1.1）
- 空 grounded 证据（P1.3）
- counter grounded 不确认成员（P1.3）
- 多源成员血缘保留（P1.2）
- 真实生产调用链：P2.5 TestRunRecord 查询 / P2.6 方向语义 / P2.7 shadow 隔离
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.quant.sector.evaluation.contracts import (
    LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
    LEGACY_SECTOR_ENGINE_POLICY_VERSION,
    PolicyVersion,
)
from core.quant.sector.evaluation.policy_store import (
    DIRECTION_HIGHER_IS_BETTER,
    DIRECTION_LOWER_IS_BETTER,
    DIRECTION_RANGE,
    PolicyVersionStore,
    ProductionPolicyAccessor,
)
from core.quant.theme.source_router import (
    ConceptBoard,
    SourceSnapshot,
    ThemeSnapshotStore,
)
from core.research.theme_membership.confirmator import MembershipConfirmator
from core.research.theme_membership.contracts import (
    GROUNDING_CONFLICTED,
    GROUNDING_GROUNDED,
    GROUNDING_UNVERIFIED,
    MEMBERSHIP_STATUS_GROUNDED,
    MEMBERSHIP_STATUS_RECALL_ONLY,
    MEMBERSHIP_STATUS_CONFLICTED,
    MembershipEvidence,
    STANCE_COUNTER,
    STANCE_NEUTRAL,
    STANCE_SUPPORT,
    ThemeMemberCandidate,
)
from core.research.theme_membership.repository import MembershipEvidenceStore


# ==================== P1.1: 同内容跨日快照 ====================

class TestCrossDaySameContentSnapshot:
    """P1.1: 相同内容在不同 source_as_of 必须保留新的可见性记录。"""

    def test_same_content_different_dates_both_saved(self, tmp_path: Path) -> None:
        """相同 content_hash 在不同 source_as_of 应都保存（不按 content_hash 去重）。"""
        store = ThemeSnapshotStore(db_path=tmp_path / "snap.db")
        # 同一 content_hash，不同 source_as_of
        snap_day1 = SourceSnapshot(
            snapshot_id="snap_day1",
            trade_date="20260710",
            fetched_at="2026-07-10T00:00:00Z",
            providers_used=["wencai"],
            boards=[ConceptBoard(board_name="HBM", provider="wencai", members=["000001"])],
            source_as_of="20260710",
            content_hash="hash_same_content",
        )
        snap_day2 = SourceSnapshot(
            snapshot_id="snap_day2",
            trade_date="20260711",
            fetched_at="2026-07-11T00:00:00Z",
            providers_used=["wencai"],
            boards=[ConceptBoard(board_name="HBM", provider="wencai", members=["000001"])],
            source_as_of="20260711",
            content_hash="hash_same_content",  # 相同内容
        )

        # 两天都应保存成功（不同 source_as_of 必须保留新可见性记录）
        assert store.save_snapshot(snap_day1, policy_version="v1") is True
        assert store.save_snapshot(snap_day2, policy_version="v1") is True

        # 历史查询：20260711 应返回 day2，20260710 应返回 day1
        result_day2 = store.get_historical_snapshot("20260711")
        assert result_day2 is not None
        assert result_day2.snapshot_id == "snap_day2"
        result_day1 = store.get_historical_snapshot("20260710")
        assert result_day1 is not None
        assert result_day1.snapshot_id == "snap_day1"

    def test_same_content_same_date_idempotent(self, tmp_path: Path) -> None:
        """相同 (provider, source_as_of, content_hash) 重复保存应幂等跳过。"""
        store = ThemeSnapshotStore(db_path=tmp_path / "snap.db")
        snap = SourceSnapshot(
            snapshot_id="snap_v1",
            trade_date="20260710",
            fetched_at="2026-07-10T00:00:00Z",
            providers_used=["wencai"],
            boards=[ConceptBoard(board_name="HBM", provider="wencai", members=["000001"])],
            source_as_of="20260710",
            content_hash="hash_xyz",
        )
        assert store.save_snapshot(snap, policy_version="v1") is True
        # 相同复合幂等键应跳过
        assert store.save_snapshot(snap, policy_version="v1") is False


# ==================== P1.3: 空 grounded 证据 ====================

class TestEmptyGroundedEvidenceRejected:
    """P1.3: grounded+support 证据缺关键字段时必须拒绝保存。"""

    def _make_minimal_evidence(self, **overrides: Any) -> MembershipEvidence:
        """构造完整 grounded support 证据，可通过 overrides 覆盖字段。"""
        defaults = dict(
            symbol="600000.SH",
            theme_id="theme_optical",
            stance=STANCE_SUPPORT,
            grounding_status=GROUNDING_GROUNDED,
            source_type="announcement",
            provider="cninfo",
            source_url="https://example.com/doc/123",
            document_id="doc_123",
            document_version_id="docv_123_v1",
            quote="公司是国内领先的光学元件厂商",
            locator={"field": "主营业务", "offset": 120},
            retrieved_at="2025-01-20T00:00:00+00:00",
            valid_from="2025-01-01T00:00:00+00:00",
            valid_until="",
        )
        defaults.update(overrides)
        return MembershipEvidence(**defaults)

    def test_full_grounded_support_evidence_accepted(self, tmp_path: Path) -> None:
        """完整 grounded+support 证据应保存成功。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        evidence = self._make_minimal_evidence()
        assert store.save_evidence(evidence) is True

    @pytest.mark.parametrize(
        "missing_field,override_value",
        [
            ("quote", ""),
            ("document_id", ""),  # document_id 和 document_version_id 都空时拒绝
            ("document_version_id", ""),  # 同上，两者都空时拒绝
            ("locator", {}),
            ("source_url", ""),  # source_url 和 provider 都空时拒绝
            ("retrieved_at", ""),
        ],
    )
    def test_grounded_support_missing_field_rejected(
        self, tmp_path: Path, missing_field: str, override_value: Any,
    ) -> None:
        """grounded+support 证据缺关键字段时必须拒绝保存。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        evidence = self._make_minimal_evidence(**{missing_field: override_value})
        # 校验规则：document_id 和 document_version_id 都空才拒绝，
        # 所以缺任一时需同时把另一个也设空
        if missing_field == "document_id":
            evidence.document_version_id = ""
        if missing_field == "document_version_id":
            evidence.document_id = ""
        # 校验规则：source_url 和 provider 都空才拒绝
        if missing_field == "source_url":
            evidence.provider = ""
        assert store.save_evidence(evidence) is False

    def test_empty_symbol_rejected(self, tmp_path: Path) -> None:
        """symbol 为空时拒绝保存（即使其它字段完整）。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        evidence = self._make_minimal_evidence(symbol="", theme_id="")
        assert store.save_evidence(evidence) is False


# ==================== P1.3: counter grounded 不确认成员 ====================

class TestCounterGroundedDoesNotConfirm:
    """P1.3: counter/neutral/conflicted/expired 证据不得确认成员。"""

    def _make_evidence(self, stance: str, grounding: str) -> MembershipEvidence:
        """构造完整证据，可指定 stance 和 grounding_status。"""
        return MembershipEvidence(
            symbol="600000.SH",
            theme_id="theme_optical",
            stance=stance,
            grounding_status=grounding,
            source_type="announcement",
            provider="cninfo",
            source_url="https://example.com/doc/123",
            document_id="doc_123",
            document_version_id="docv_123_v1",
            quote="公司明确否认进入该领域" if stance == STANCE_COUNTER else "公司业务正常",
            locator={"paragraph": 3},
            retrieved_at="2025-01-20T00:00:00+00:00",
            valid_from="2025-01-01T00:00:00+00:00",
            valid_until="",
            policy_version="policy_v1",
        )

    def test_counter_grounded_does_not_confirm(self, tmp_path: Path) -> None:
        """counter+grounded 证据不应让候选升级为 grounded。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        # 保存 counter+grounded 证据
        counter_evidence = self._make_evidence(STANCE_COUNTER, GROUNDING_GROUNDED)
        assert store.save_evidence(counter_evidence) is True

        # 候选不应被确认（counter stance 不升级为 grounded）
        confirmator = MembershipConfirmator(store=store)
        candidate = ThemeMemberCandidate(
            symbol="600000.SH",
            theme_id="theme_optical",
            source_provider="wencai",
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert candidate.membership_status != MEMBERSHIP_STATUS_GROUNDED
        assert candidate not in confirmed
        # 不在 store 的 confirmed 列表中
        store_confirmed = store.get_confirmed_members("theme_optical")
        assert "600000.SH" not in store_confirmed

    def test_neutral_grounded_does_not_confirm(self, tmp_path: Path) -> None:
        """neutral+grounded 证据不应让候选升级为 grounded。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        neutral_evidence = self._make_evidence(STANCE_NEUTRAL, GROUNDING_GROUNDED)
        assert store.save_evidence(neutral_evidence) is True

        confirmator = MembershipConfirmator(store=store)
        candidate = ThemeMemberCandidate(
            symbol="600000.SH",
            theme_id="theme_optical",
            source_provider="wencai",
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert candidate.membership_status != MEMBERSHIP_STATUS_GROUNDED
        assert candidate not in confirmed

    def test_conflicted_grounding_does_not_confirm(self, tmp_path: Path) -> None:
        """grounding_status=conflicted 的证据不应确认成员。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        conflicted_evidence = self._make_evidence(STANCE_SUPPORT, GROUNDING_CONFLICTED)
        # conflicted grounding 也可能因 grounding_status 被拒绝保存
        # 但即使保存成功，也不应确认成员
        store.save_evidence(conflicted_evidence)

        confirmator = MembershipConfirmator(store=store)
        candidate = ThemeMemberCandidate(
            symbol="600000.SH",
            theme_id="theme_optical",
            source_provider="wencai",
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert candidate.membership_status != MEMBERSHIP_STATUS_GROUNDED
        assert candidate not in confirmed

    def test_support_grounded_confirms(self, tmp_path: Path) -> None:
        """support+grounded 证据应让候选升级为 grounded（正向基线）。"""
        store = MembershipEvidenceStore(db_path=tmp_path / "ev.db")
        support_evidence = self._make_evidence(STANCE_SUPPORT, GROUNDING_GROUNDED)
        assert store.save_evidence(support_evidence) is True

        confirmator = MembershipConfirmator(store=store)
        candidate = ThemeMemberCandidate(
            symbol="600000.SH",
            theme_id="theme_optical",
            source_provider="wencai",
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert candidate.membership_status == MEMBERSHIP_STATUS_GROUNDED
        assert candidate in confirmed
        store_confirmed = store.get_confirmed_members("theme_optical")
        assert "600000.SH" in store_confirmed


# ==================== P1.2: 多源成员血缘保留 ====================

class TestMultiSourceMemberLineage:
    """P1.2: 多源合并保留 provider→board→member 独立观察和各自 source_as_of。"""

    def test_merge_members_preserves_per_provider_independent_observations(self) -> None:
        """_merge_members 应为每条 membership_evidence 保留独立的 provider 观察记录。

        不应合并成逗号分隔的 provider 字符串或丢失任一 provider 的观察。
        每条 evidence 记录单一 provider、board_name、source_as_of 三元组。
        """
        from core.quant.theme.theme_assembler import ThemeAssembler

        # 构造两个不同 provider 的 board，含同一 symbol
        boards = [
            ConceptBoard(
                board_name="HBM",
                board_id="BK_HBM",
                provider="wencai",
                members=["000001.SZ", "000002.SZ"],
                fetched_at="2026-07-10T00:00:00Z",
            ),
            ConceptBoard(
                board_name="HBM_concept",
                board_id="BK_HBM_EF",
                provider="efinance",
                members=["000001.SZ", "000003.SZ"],
                fetched_at="2026-07-11T00:00:00Z",
            ),
        ]

        assembler = ThemeAssembler()
        members = assembler._merge_members(
            boards=boards,
            news_symbols={},
            market_resonance_symbols=[],
            snapshot_source_as_of="20260711",
        )

        # 收集所有 membership_evidence
        all_evidence: list[dict[str, Any]] = []
        for member in members:
            all_evidence.extend(member.membership_evidence)

        # 应有多条 evidence（非合并成单条）
        assert len(all_evidence) >= 2, (
            f"应保留多条独立观察，实际 {len(all_evidence)} 条"
        )

        # 每条 evidence 的 provider 应是单一 provider（非逗号字符串）
        providers_seen = {ev.get("provider", "") for ev in all_evidence}
        for ev in all_evidence:
            provider_str = ev.get("provider", "")
            assert "," not in provider_str, (
                f"provider 不应是逗号分隔字符串: {provider_str}"
            )
        assert {"wencai", "efinance"}.issubset(providers_seen), (
            f"应同时看到 wencai 和 efinance 两个 provider: {providers_seen}"
        )

        # 同一 symbol 在不同 provider 下应有独立的 evidence（血缘保留）
        # 按 (symbol, provider) 分组，000001.SZ 应同时来自 wencai 和 efinance
        symbol_providers: dict[str, set[str]] = {}
        symbol_evidence_count: dict[str, int] = {}
        for ev in all_evidence:
            # membership_evidence 不直接含 symbol，需要通过外层 member 关联
            pass
        # 通过 member 遍历重建 symbol → providers 映射
        for member in members:
            for ev in member.membership_evidence:
                provider = ev.get("provider", "")
                symbol_providers.setdefault(member.symbol, set()).add(provider)
                symbol_evidence_count[member.symbol] = (
                    symbol_evidence_count.get(member.symbol, 0) + 1
                )
        # 000001.SZ 同时在 wencai 和 efinance 出现，应有 2 条独立 evidence
        assert "wencai" in symbol_providers.get("000001.SZ", set())
        assert "efinance" in symbol_providers.get("000001.SZ", set())
        assert symbol_evidence_count.get("000001.SZ", 0) >= 2, (
            "000001.SZ 应有至少 2 条独立 evidence（每 provider 一条）"
        )


# ==================== P2.5: Policy activate 查询真实 TestRunRecord ====================

class TestPolicyActivateWithTestRunRecord:
    """P2.5: PolicyVersionStore.activate 必须查询真实 TestRunRecord。"""

    def _make_verifier(
        self,
        *,
        passed: bool = True,
        data_as_of: str = "20260710",
        policy_version: str = LEGACY_SECTOR_ENGINE_POLICY_VERSION,
        code_version: str = "v1.0.0",
        metrics: dict[str, Any] | None = None,
    ):
        """构造 TestRunVerifier mock。"""
        def verifier(test_run_id, expected_code_version, expected_policy_version):
            if not test_run_id:
                return None
            return {
                "passed": passed,
                "code_version": code_version,
                "data_as_of": data_as_of,
                "policy_version": policy_version,
                "metrics": metrics or {},
            }
        return verifier

    def test_activate_rejects_when_test_run_not_passed(self, tmp_path: Path) -> None:
        """TestRunRecord.passed=False 时激活被拒绝。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier(passed=False),
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        assert result is None

    def test_activate_rejects_when_data_as_of_mismatch(self, tmp_path: Path) -> None:
        """policy.data_as_of 与 TestRunRecord.data_as_of 不匹配时拒绝。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier(data_as_of="20260710"),
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        # 修改 policy 的 data_as_of 与 TestRunRecord 不同
        policy.data_as_of = "20260715"
        store.save(policy)
        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        assert result is None

    def test_activate_rejects_when_policy_version_mismatch(self, tmp_path: Path) -> None:
        """TestRunRecord.policy_version 与目标 policy_version 不匹配时拒绝。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier(
                policy_version="policy_other_v1",
            ),
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        assert result is None

    def test_activate_rejects_when_test_run_id_not_found(self, tmp_path: Path) -> None:
        """TestRunRecord 不存在时（verifier 返回 None）拒绝。"""
        def always_none_verifier(test_run_id, code_version, policy_version):
            return None
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=always_none_verifier,
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_not_exist",
            code_version="v1.0.0",
        )
        assert result is None


# ==================== P2.6: 方向语义 + 必填指标 ====================

class TestPolicyActivateDirectionSemantics:
    """P2.6: activation_criteria 方向语义 + 必填指标拒绝。"""

    def _make_verifier(self, metrics: dict[str, Any]):
        """构造返回指定 metrics 的 verifier。"""
        def verifier(test_run_id, code_version, policy_version):
            return {
                "passed": True,
                "code_version": code_version,
                "data_as_of": "20260710",
                "policy_version": policy_version,
                "metrics": metrics,
            }
        return verifier

    def test_lower_is_better_allows_lower_threshold(self, tmp_path: Path) -> None:
        """lower_is_better 方向：新版本降低阈值（更严格）应被允许，不应被拒。

        以 volatility（正数，越小越好）为例：
        - v1 criteria: volatility <= 0.2
        - v2 criteria: volatility <= 0.15（更严格）
        - 实际 metric: volatility = 0.15（同时满足 v1 和 v2）
        """
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier({"volatility": 0.15}),
        )
        # 先激活 v1，volatility = 0.2（lower_is_better）
        v1 = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        v1.activation_criteria = {"volatility": 0.2}
        v1.activation_criteria_directions = {"volatility": DIRECTION_LOWER_IS_BETTER}
        store.save(v1)
        store.activate(
            v1.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )

        # 创建 v2，volatility = 0.15（更严格，lower_is_better 方向下"未降低标准"）
        v2 = PolicyVersion(
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version="policy_v2_lower_vol",
            activation_criteria={"volatility": 0.15},
            activation_criteria_directions={"volatility": DIRECTION_LOWER_IS_BETTER},
            status="shadow",
        )
        store.save(v2)
        result = store.activate(
            v2.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_002",
            code_version="v1.0.0",
        )
        # lower_is_better 方向下 0.15 <= 0.2，新版本更严格，应允许激活
        assert result is not None
        assert result.status == "active"

    def test_higher_is_better_rejects_lowered_threshold(self, tmp_path: Path) -> None:
        """higher_is_better 方向（默认）：新版本降低阈值应被拒绝。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier({"min_sharpe": 0.6}),
        )
        v1 = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        v1.activation_criteria = {"min_sharpe": 0.5}
        # 默认 higher_is_better，不指定 direction
        store.save(v1)
        store.activate(
            v1.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )

        # v2 把 min_sharpe 从 0.5 降到 0.3（higher_is_better 下降低标准）
        v2 = PolicyVersion(
            feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
            policy_version="policy_v2_lower_sharpe",
            activation_criteria={"min_sharpe": 0.3},
            status="shadow",
        )
        store.save(v2)
        result = store.activate(
            v2.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_002",
            code_version="v1.0.0",
        )
        assert result is None  # 降低标准被拒

    def test_missing_required_metrics_rejects_activation(self, tmp_path: Path) -> None:
        """P2.6: 必填指标缺失时拒绝激活。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier({"min_sharpe": 0.6}),  # 缺 min_ic
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        policy.required_metrics = ["min_sharpe", "min_ic"]
        policy.activation_criteria = {"min_sharpe": 0.5, "min_ic": 0.03}
        store.save(policy)

        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        # min_ic 缺失 → 拒绝
        assert result is None

    def test_range_direction_satisfied_within_range(self, tmp_path: Path) -> None:
        """range 方向：metric 在 [min, max] 区间内应通过。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier({"sharpe": 1.2}),
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        policy.activation_criteria = {
            "sharpe": {"min": 0.5, "max": 2.0},
        }
        policy.activation_criteria_directions = {"sharpe": DIRECTION_RANGE}
        policy.required_metrics = ["sharpe"]
        store.save(policy)

        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        # sharpe=1.2 在 [0.5, 2.0] 区间内 → 通过
        assert result is not None

    def test_range_direction_rejected_outside_range(self, tmp_path: Path) -> None:
        """range 方向：metric 超出 [min, max] 区间应拒绝。"""
        store = PolicyVersionStore(
            db_path=tmp_path / "p.db",
            test_run_verifier=self._make_verifier({"sharpe": 3.0}),
        )
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        policy.activation_criteria = {
            "sharpe": {"min": 0.5, "max": 2.0},
        }
        policy.activation_criteria_directions = {"sharpe": DIRECTION_RANGE}
        policy.required_metrics = ["sharpe"]
        store.save(policy)

        result = store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )
        # sharpe=3.0 超出 [0.5, 2.0] → 拒绝
        assert result is None


# ==================== P2.7: Shadow 生产调用点真正隔离 ====================

class TestShadowIsolationAtProductionCallPoint:
    """P2.7: shadow 在生产调用点真正隔离，不只增加 status 字段。"""

    def test_shadow_policy_invisible_to_production(self, tmp_path: Path) -> None:
        """shadow 状态的策略参数对生产完全不可见。"""
        store = PolicyVersionStore(db_path=tmp_path / "p.db")
        accessor = ProductionPolicyAccessor(store)

        # 旧规则默认 shadow
        params = accessor.get_active_parameters(LEGACY_SECTOR_ENGINE_FEATURE_VERSION)
        # shadow 策略参数对生产不可见
        assert params is None, "shadow 策略参数不应暴露给生产路径"

        # is_shadow 应为 True
        assert accessor.is_shadow(LEGACY_SECTOR_ENGINE_FEATURE_VERSION) is True

        # get_shadow_policy_for_evaluation 应能获取（仅评价用）
        shadow_policy = accessor.get_shadow_policy_for_evaluation(
            LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
        )
        assert shadow_policy is not None
        assert shadow_policy.status == "shadow"

    def test_active_policy_visible_to_production(self, tmp_path: Path) -> None:
        """active 状态的策略参数对生产可见。"""
        store = PolicyVersionStore(db_path=tmp_path / "p.db")
        accessor = ProductionPolicyAccessor(store)

        # 激活一个策略
        policy = store.get_by_policy_version(LEGACY_SECTOR_ENGINE_POLICY_VERSION)
        store.activate(
            policy.policy_version_id,
            approved_by="reviewer",
            test_run_id="tr_001",
            code_version="v1.0.0",
        )

        # 生产路径应能看到参数
        params = accessor.get_active_parameters(LEGACY_SECTOR_ENGINE_FEATURE_VERSION)
        assert params is not None
        assert "rally_threshold" in params
        # is_shadow 应为 False
        assert accessor.is_shadow(LEGACY_SECTOR_ENGINE_FEATURE_VERSION) is False

    def test_no_policy_returns_none_for_production(self, tmp_path: Path) -> None:
        """无任何策略时生产路径返回 None（降级）。"""
        store = PolicyVersionStore(db_path=tmp_path / "p.db")
        accessor = ProductionPolicyAccessor(store)
        params = accessor.get_active_parameters("nonexistent_feature")
        assert params is None
