"""阶段5 shadow 联合特征与模拟评价测试。

覆盖 Task 5.1-5.4：
- 5.1: 8 个 shadow 特征计算器基本输出与降级
- 5.2: 标签 PIT 安全（label_store 已实现，此处验证 shadow 不读未来数据）
- 5.3: CoreIdentityObservation 序列化与龙虎榜排除
- 5.4: shadow 特征不修改生产状态（READY/WATCH/REJECTED 不变）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.quant.contracts import QuantCandidate, SectorAssessment, SectorPhase
from core.quant.theme.contracts import ThemeMember, ThemePacket
from core.quant.theme.daily_screener import (
    STATUS_READY,
    STATUS_WATCH,
    STATUS_REJECTED,
    ThemeDailyScreener,
    ThemeScreeningResult,
)
from core.quant.theme.shadow_features import (
    CoreIdentityFeature,
    CoreIdentityObservation,
    EarningsRevisionFeature,
    EpsImpactFeature,
    ExpectationGapFeature,
    FEATURE_VERSION,
    PhaseTechnicalInteractionFeature,
    POLICY_VERSION,
    ResearchEvidenceQualityFeature,
    SHADOW_FEATURE_NAMES,
    ShadowFeatureContext,
    ShadowFeatureResult,
    ShadowFeatureSet,
    TechnicalStateFeature,
    ThemePhaseFeature,
)


# ======================================================================
# 测试夹具
# ======================================================================

def _make_member(
    symbol: str = "000001",
    market_role: str = "core",
    evidence: list[dict[str, Any]] | None = None,
) -> ThemeMember:
    """构造测试用 ThemeMember。"""
    return ThemeMember(
        symbol=symbol,
        company_name="测试公司",
        market_role=market_role,
        membership_evidence=evidence or [],
        first_seen_at="20260101",
        last_confirmed_at="20260710",
    )


def _make_packet(
    phase: str = "acceleration",
    members: list[ThemeMember] | None = None,
) -> ThemePacket:
    """构造测试用 ThemePacket。"""
    if members is None:
        members = [_make_member()]
    return ThemePacket(
        theme_id="theme_test_001",
        theme_name="测试主题",
        phase=phase,
        phase_confidence=0.8,
        core_members=members,
        members=members,
        as_of="20260710",
    )


def _make_context(
    *,
    revisions: list[dict[str, Any]] | None = None,
    gap: dict[str, Any] | None = None,
    impact: dict[str, Any] | None = None,
    tech_label: str = "",
) -> ShadowFeatureContext:
    """构造测试用 Provider 上下文。"""
    return ShadowFeatureContext(
        expectation_revision_provider=lambda _s, _a: revisions or [],
        expectation_gap_provider=lambda _s, _a: gap,
        earnings_impact_provider=lambda _s, _a: impact,
        technical_state_provider=lambda _s, _a: tech_label,
    )


# ======================================================================
# Task 5.1: 各特征计算器基本输出（0-1 范围）
# ======================================================================

class TestEarningsRevisionFeature:
    """盈利预测修正特征测试。"""

    def test_upward_revision_high_score(self):
        """上修 → 高分。"""
        revisions = [
            {"value_mid": 1.0, "as_of": "20260701"},
            {"value_mid": 1.2, "as_of": "20260710"},
        ]
        ctx = _make_context(revisions=revisions)
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert 0.0 <= result.value <= 1.0
        assert result.value > 0.5
        assert not result.degraded

    def test_downward_revision_low_score(self):
        """下修 → 低分。"""
        revisions = [
            {"value_mid": 1.2, "as_of": "20260701"},
            {"value_mid": 1.0, "as_of": "20260710"},
        ]
        ctx = _make_context(revisions=revisions)
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert 0.0 <= result.value <= 1.0
        assert result.value < 0.5
        assert not result.degraded

    def test_missing_provider_degraded(self):
        """Provider 缺失 → 降级 0.0。"""
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_empty_history_degraded(self):
        """修正历史为空 → 降级 0.0。"""
        ctx = _make_context(revisions=[])
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_single_revision_degraded(self):
        """仅一个版本无法判断方向 → 降级。"""
        ctx = _make_context(revisions=[{"value_mid": 1.0, "as_of": "20260710"}])
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded


class TestExpectationGapFeature:
    """预期差距特征测试。"""

    def test_positive_gap_high_score(self):
        """正向差距（研究 > 预期）→ 高分。"""
        gap = {"relative_gap_mid": 0.2, "status": "ok", "available_at": "20260710"}
        ctx = _make_context(gap=gap)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert 0.0 <= result.value <= 1.0
        assert result.value > 0.5
        assert not result.degraded

    def test_negative_gap_low_score(self):
        """负向差距 → 低分。"""
        gap = {"relative_gap_mid": -0.3, "status": "ok", "available_at": "20260710"}
        ctx = _make_context(gap=gap)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert 0.0 <= result.value <= 1.0
        assert result.value < 0.5
        assert not result.degraded

    def test_missing_gap_degraded(self):
        """无预期差数据 → 降级 0.0。"""
        ctx = _make_context(gap=None)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_insufficient_status_degraded(self):
        """status=insufficient_data → 降级。"""
        gap = {"relative_gap_mid": 0.1, "status": "insufficient_data", "available_at": "20260710"}
        ctx = _make_context(gap=gap)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded


class TestEpsImpactFeature:
    """EPS 影响度特征测试。"""

    def test_contribution_mapping(self):
        """contribution_mid 直接映射为分数。"""
        impact = {"contribution_mid": 0.6, "status": "ok", "available_at": "20260710"}
        ctx = _make_context(impact=impact)
        result = EpsImpactFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == pytest.approx(0.6)
        assert not result.degraded

    def test_eps_impact_fallback(self):
        """无 contribution 时用 eps_impact 归一化。"""
        impact = {"eps_impact_mid": 0.005, "status": "ok", "available_at": "20260710"}
        ctx = _make_context(impact=impact)
        result = EpsImpactFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert 0.0 <= result.value <= 1.0
        assert result.value == pytest.approx(0.5)
        assert not result.degraded

    def test_missing_impact_degraded(self):
        """无盈利影响数据 → 降级。"""
        ctx = _make_context(impact=None)
        result = EpsImpactFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_insufficient_status_degraded(self):
        """status=insufficient_data → 降级。"""
        impact = {"contribution_mid": 0.5, "status": "insufficient_data", "available_at": "20260710"}
        ctx = _make_context(impact=impact)
        result = EpsImpactFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded


class TestThemePhaseFeature:
    """主题阶段特征测试。"""

    @pytest.mark.parametrize("phase,expected", [
        ("acceleration", 0.90),
        ("startup", 0.65),
        ("climax", 0.70),
        ("recovery", 0.35),
        ("ice", 0.15),
        ("retreat", 0.20),
    ])
    def test_phase_mapping(self, phase: str, expected: float):
        """各阶段映射到预期分数。"""
        result = ThemePhaseFeature().compute(
            symbol="000001", packet=_make_packet(phase=phase), as_of="20260710",
        )
        assert result.value == pytest.approx(expected)
        assert not result.degraded

    def test_unknown_phase_degraded(self):
        """unknown 阶段 → 降级 0.0。"""
        result = ThemePhaseFeature().compute(
            symbol="000001", packet=_make_packet(phase="unknown"), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded

    def test_empty_phase_degraded(self):
        """空阶段 → 降级。"""
        result = ThemePhaseFeature().compute(
            symbol="000001", packet=_make_packet(phase=""), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded


class TestTechnicalStateFeature:
    """技术状态特征测试。"""

    @pytest.mark.parametrize("label,expected", [
        ("REACCELERATION", 0.90),
        ("MA5_HOLD", 0.80),
        ("MA10_RETEST", 0.70),
        ("TREND_PULLBACK", 0.60),
        ("MA20_RETEST", 0.50),
        ("TREND_BROKEN", 0.10),
    ])
    def test_label_mapping(self, label: str, expected: float):
        """技术状态标签映射到预期分数。"""
        ctx = _make_context(tech_label=label)
        result = TechnicalStateFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == pytest.approx(expected)
        assert not result.degraded

    def test_missing_provider_degraded(self):
        """Provider 缺失 → 降级。"""
        result = TechnicalStateFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_empty_label_degraded(self):
        """空标签 → 降级。"""
        ctx = _make_context(tech_label="")
        result = TechnicalStateFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded


class TestPhaseTechnicalInteractionFeature:
    """阶段×技术交互特征测试。"""

    def test_product_of_phase_and_tech(self):
        """交互值 = theme_phase × technical_state。"""
        ctx = _make_context(tech_label="MA10_RETEST")
        result = PhaseTechnicalInteractionFeature().compute(
            symbol="000001",
            packet=_make_packet(phase="acceleration"),
            as_of="20260710",
            context=ctx,
        )
        # acceleration=0.90, MA10_RETEST=0.70 → 0.63
        assert result.value == pytest.approx(0.90 * 0.70)
        assert not result.degraded
        assert 0.0 <= result.value <= 1.0

    def test_degraded_when_phase_unknown(self):
        """阶段 unknown 时交互降级。"""
        ctx = _make_context(tech_label="MA10_RETEST")
        result = PhaseTechnicalInteractionFeature().compute(
            symbol="000001",
            packet=_make_packet(phase="unknown"),
            as_of="20260710",
            context=ctx,
        )
        assert result.value == 0.0
        assert result.degraded

    def test_degraded_when_tech_missing(self):
        """技术状态缺失时交互降级。"""
        result = PhaseTechnicalInteractionFeature().compute(
            symbol="000001",
            packet=_make_packet(phase="acceleration"),
            as_of="20260710",
            context=None,
        )
        assert result.value == 0.0
        assert result.degraded


class TestCoreIdentityFeature:
    """核心身份特征测试。"""

    @pytest.mark.parametrize("role,expected", [
        ("core", 1.0),
        ("leader", 0.8),
        ("secondary", 0.5),
        ("diffusion", 0.3),
    ])
    def test_role_mapping(self, role: str, expected: float):
        """market_role 映射到预期分数。"""
        member = _make_member(market_role=role)
        result = CoreIdentityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        assert result.value == pytest.approx(expected)
        assert not result.degraded

    def test_member_not_found_degraded(self):
        """成员未找到 → 降级。"""
        result = CoreIdentityFeature().compute(
            symbol="999999", packet=_make_packet(), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded

    def test_empty_role_degraded(self):
        """空 market_role → 降级。"""
        member = _make_member(market_role="")
        result = CoreIdentityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded


class TestResearchEvidenceQualityFeature:
    """研究证据质量特征测试。"""

    def test_all_grounded(self):
        """全部 grounded → 满分。"""
        evidence = [
            {"grounding_status": "grounded", "evidence_id": "e1"},
            {"grounding_status": "grounded", "evidence_id": "e2"},
        ]
        member = _make_member(evidence=evidence)
        result = ResearchEvidenceQualityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        assert result.value == pytest.approx(1.0)
        assert not result.degraded

    def test_mixed_grounding(self):
        """混合 grounding → 加权平均。"""
        evidence = [
            {"grounding_status": "grounded", "evidence_id": "e1"},
            {"grounding_status": "unverified", "evidence_id": "e2"},
            {"grounding_status": "conflicted", "evidence_id": "e3"},
        ]
        member = _make_member(evidence=evidence)
        result = ResearchEvidenceQualityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        # (1.0 + 0.5 + 0.0) / 3 = 0.5
        assert result.value == pytest.approx(0.5)
        assert not result.degraded

    def test_no_evidence_degraded(self):
        """无证据 → 降级。"""
        member = _make_member(evidence=[])
        result = ResearchEvidenceQualityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded

    def test_member_not_found_degraded(self):
        """成员未找到 → 降级。"""
        result = ResearchEvidenceQualityFeature().compute(
            symbol="999999", packet=_make_packet(), as_of="20260710",
        )
        assert result.value == 0.0
        assert result.degraded

    def test_unknown_grounding_treated_as_unverified(self):
        """未知 grounding_status 按未验证处理。"""
        evidence = [{"grounding_status": "unknown_status", "evidence_id": "e1"}]
        member = _make_member(evidence=evidence)
        result = ResearchEvidenceQualityFeature().compute(
            symbol="000001", packet=_make_packet(members=[member]), as_of="20260710",
        )
        assert result.value == pytest.approx(0.5)
        assert not result.degraded


# ======================================================================
# Task 5.1: ShadowFeatureSet 聚合输出
# ======================================================================

class TestShadowFeatureSet:
    """shadow 特征集聚合测试。"""

    def test_compute_all_eight_features(self):
        """compute_all 输出 8 个特征。"""
        evidence = [
            {"grounding_status": "grounded", "evidence_id": "e1"},
        ]
        member = _make_member(evidence=evidence)
        ctx = _make_context(
            revisions=[
                {"value_mid": 1.0, "as_of": "20260701"},
                {"value_mid": 1.1, "as_of": "20260710"},
            ],
            gap={"relative_gap_mid": 0.1, "status": "ok", "available_at": "20260710"},
            impact={"contribution_mid": 0.5, "status": "ok", "available_at": "20260710"},
            tech_label="MA10_RETEST",
        )
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001",
            packet=_make_packet(phase="acceleration", members=[member]),
            as_of="20260710",
            context=ctx,
        )
        assert len(feature_set.features) == 8
        for name in SHADOW_FEATURE_NAMES:
            assert name in feature_set.features
            assert 0.0 <= feature_set.features[name] <= 1.0

    def test_feature_and_policy_version(self):
        """feature_version 和 policy_version 标识正确。"""
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        assert feature_set.feature_version == FEATURE_VERSION
        assert feature_set.policy_version == POLICY_VERSION
        assert feature_set.feature_version == "shadow_features_v1"
        assert feature_set.policy_version == "policy_shadow_v1"

    def test_degraded_when_providers_missing(self):
        """所有 Provider 缺失时标记降级。"""
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        # earnings_revision / expectation_gap / eps_impact / technical_state 会降级
        # theme_phase / core_identity / research_evidence_quality 可能不降级
        assert feature_set.degraded is True
        assert len(feature_set.degraded_features) > 0

    def test_to_dict_contains_persistent_fields_only(self):
        """to_dict 只包含可持久化字段。"""
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        data = feature_set.to_dict()
        assert "features" in data
        assert "as_of" in data
        assert "feature_version" in data
        assert "policy_version" in data
        assert "symbol" in data

    def test_round_trip_serialization(self):
        """to_dict / from_dict 往返一致。"""
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        data = feature_set.to_dict()
        restored = ShadowFeatureSet.from_dict(data)
        assert restored.symbol == feature_set.symbol
        assert restored.as_of == feature_set.as_of
        assert restored.features == feature_set.features
        assert restored.feature_version == feature_set.feature_version
        assert restored.policy_version == feature_set.policy_version


# ======================================================================
# Task 5.4: shadow 特征不修改生产状态
# ======================================================================

class TestShadowDoesNotModifyProductionState:
    """shadow 特征不修改 READY/WATCH/REJECTED 状态。"""

    def test_shadow_does_not_change_screener_status(self):
        """shadow 特征计算不改变 daily_screener 的状态输出。"""
        # 构造候选与 packet
        candidate = QuantCandidate(
            candidate_id="c1",
            symbol="000001",
            theme_id="theme_test_001",
            research_score=0.8,
        )
        packet = _make_packet(phase="acceleration")
        # 构造最小日线数据
        import pandas as pd
        daily_data = pd.DataFrame({
            "close": [10.0, 10.5, 11.0, 10.8, 11.2, 11.5],
            "amount": [6e7, 7e7, 8e7, 6e7, 7e7, 9e7],
        })

        # 不带 shadow 的 screener 输出
        screener = ThemeDailyScreener()
        results_without = screener.screen(
            [candidate], [packet],
            daily_data_map={"000001": daily_data},
            as_of="20260710",
        )
        status_without = results_without[0].status

        # 计算 shadow 特征
        ctx = _make_context(
            revisions=[
                {"value_mid": 1.0, "as_of": "20260701"},
                {"value_mid": 1.1, "as_of": "20260710"},
            ],
            gap={"relative_gap_mid": 0.1, "status": "ok", "available_at": "20260710"},
            impact={"contribution_mid": 0.5, "status": "ok", "available_at": "20260710"},
            tech_label="MA10_RETEST",
        )
        _ = ShadowFeatureSet.compute_all(
            symbol="000001", packet=packet, as_of="20260710", context=ctx,
        )

        # 再次运行 screener，状态不应改变
        results_with = screener.screen(
            [candidate], [packet],
            daily_data_map={"000001": daily_data},
            as_of="20260710",
        )
        status_with = results_with[0].status

        assert status_without == status_with
        # 状态应为 READY/WATCH/REJECTED 之一
        assert status_without in (STATUS_READY, STATUS_WATCH, STATUS_REJECTED)

    def test_shadow_does_not_output_trading_actions(self):
        """ShadowFeatureSet.to_dict 不包含交易动作字段。"""
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=None,
        )
        data = feature_set.to_dict()
        # 禁止出现交易动作字段
        forbidden_keys = {"buy", "sell", "position", "stop_loss", "take_profit", "action"}
        assert not (forbidden_keys & set(data.keys()))

    def test_shadow_features_in_zero_one_range(self):
        """所有 shadow 特征值在 [0, 1] 范围内。"""
        evidence = [
            {"grounding_status": "grounded", "evidence_id": "e1"},
            {"grounding_status": "conflicted", "evidence_id": "e2"},
        ]
        member = _make_member(evidence=evidence)
        ctx = _make_context(
            revisions=[
                {"value_mid": 1.0, "as_of": "20260701"},
                {"value_mid": 1.5, "as_of": "20260710"},
            ],
            gap={"relative_gap_mid": 0.8, "status": "ok", "available_at": "20260710"},
            impact={"contribution_mid": 0.9, "status": "ok", "available_at": "20260710"},
            tech_label="REACCELERATION",
        )
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001",
            packet=_make_packet(phase="acceleration", members=[member]),
            as_of="20260710",
            context=ctx,
        )
        for name, value in feature_set.features.items():
            assert 0.0 <= value <= 1.0, f"特征 {name} 越界: {value}"


# ======================================================================
# Task 5.3: CoreIdentityObservation 序列化
# ======================================================================

class TestCoreIdentityObservation:
    """核心股身份版本化测试。"""

    def test_from_member_extracts_fields(self):
        """from_member 提取 market_role 和证据 ID。"""
        evidence = [
            {
                "evidence_id": "evd_001",
                "grounding_status": "grounded",
                "provider": "cninfo",
                "source_type": "announcement",
            },
            {
                "evidence_id": "evd_002",
                "grounding_status": "unverified",
                "provider": "sse_irm",
                "source_type": "qa",
            },
        ]
        member = _make_member(market_role="core", evidence=evidence)
        obs = CoreIdentityObservation.from_member(
            member, theme_id="theme_test_001", as_of="20260710",
        )
        assert obs.symbol == "000001"
        assert obs.theme_id == "theme_test_001"
        assert obs.market_role == "core"
        assert obs.identity_as_of == "20260710"
        assert "evd_001" in obs.evidence_ids
        assert "evd_002" in obs.evidence_ids
        assert "cninfo" in obs.source_providers
        assert "sse_irm" in obs.source_providers
        assert obs.feature_version == FEATURE_VERSION
        assert obs.policy_version == POLICY_VERSION

    def test_dragon_tiger_excluded_from_evidence(self):
        """龙虎榜不作为事实 Evidence，不进入 evidence_ids。"""
        evidence = [
            {
                "evidence_id": "evd_real_001",
                "grounding_status": "grounded",
                "provider": "cninfo",
                "source_type": "announcement",
            },
            {
                "evidence_id": "dragon_tiger_001",
                "grounding_status": "grounded",
                "provider": "eastmoney",
                "source_type": "dragon_tiger",
            },
            {
                "evidence_id": "kline_001",
                "grounding_status": "grounded",
                "provider": "tushare",
                "source_type": "kline",
            },
        ]
        member = _make_member(evidence=evidence)
        obs = CoreIdentityObservation.from_member(
            member, theme_id="theme_test_001", as_of="20260710",
        )
        assert "evd_real_001" in obs.evidence_ids
        # 龙虎榜和 K 线数据不作为事实 Evidence
        assert "dragon_tiger_001" not in obs.evidence_ids
        assert "kline_001" not in obs.evidence_ids

    def test_round_trip_serialization(self):
        """to_dict / from_dict 往返一致。"""
        obs = CoreIdentityObservation(
            identity_as_of="20260710",
            symbol="000001",
            theme_id="theme_test_001",
            market_role="leader",
            evidence_ids=["evd_001", "evd_002"],
            source_providers=["cninfo", "sse_irm"],
            valid_from="20260101",
            valid_until="20261231",
        )
        data = obs.to_dict()
        restored = CoreIdentityObservation.from_dict(data)
        assert restored.symbol == obs.symbol
        assert restored.theme_id == obs.theme_id
        assert restored.market_role == obs.market_role
        assert restored.evidence_ids == obs.evidence_ids
        assert restored.source_providers == obs.source_providers
        assert restored.valid_from == obs.valid_from
        assert restored.valid_until == obs.valid_until
        assert restored.feature_version == FEATURE_VERSION
        assert restored.policy_version == POLICY_VERSION

    def test_from_dict_ignores_unknown_fields(self):
        """from_dict 忽略未知字段。"""
        data = {
            "identity_as_of": "20260710",
            "symbol": "000001",
            "theme_id": "theme_test_001",
            "market_role": "core",
            "unknown_field": "should_be_ignored",
        }
        obs = CoreIdentityObservation.from_dict(data)
        assert obs.symbol == "000001"
        assert obs.market_role == "core"

    def test_is_expired(self):
        """身份过期判定。"""
        obs = CoreIdentityObservation(
            valid_from="20260101",
            valid_until="20260630",
        )
        assert obs.is_expired("20260710") is True
        assert obs.is_expired("20260630") is False
        assert obs.is_expired("20260601") is False

    def test_is_expired_unknown_valid_until(self):
        """valid_until 为空（UNKNOWN）时不过期。"""
        obs = CoreIdentityObservation(valid_until="")
        assert obs.is_expired("20261231") is False


# ======================================================================
# Task 5.2: PIT 安全（不读取未来数据）
# ======================================================================

class TestPitSafety:
    """PIT 安全测试：shadow 特征不读取未来数据。"""

    def test_earnings_revision_filters_future_data(self):
        """盈利预测修正过滤 PIT 之后的未来数据。"""
        # 两个版本，一个在 as_of 之前，一个在 as_of 之后（未来）
        revisions = [
            {"value_mid": 1.0, "as_of": "20260701"},  # 可见
            {"value_mid": 2.0, "as_of": "20260801"},  # 未来（as_of=20260710 后）
        ]
        ctx = _make_context(revisions=revisions)
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        # 只有一个可见版本 → 降级
        assert result.degraded
        assert result.value == 0.0

    def test_expectation_gap_rejects_future_data(self):
        """预期差距数据 available_at 晚于 as_of 时降级。"""
        gap = {
            "relative_gap_mid": 0.2,
            "status": "ok",
            "available_at": "20260801",  # 未来数据
        }
        ctx = _make_context(gap=gap)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.degraded
        assert result.value == 0.0

    def test_eps_impact_rejects_future_data(self):
        """盈利影响数据 available_at 晚于 as_of 时降级。"""
        impact = {
            "contribution_mid": 0.5,
            "status": "ok",
            "available_at": "20260801",  # 未来数据
        }
        ctx = _make_context(impact=impact)
        result = EpsImpactFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.degraded
        assert result.value == 0.0

    def test_feature_value_clamped_to_zero_one(self):
        """特征值始终裁剪到 [0, 1]。"""
        # 极大的正向差距应被裁剪到 1.0
        gap = {"relative_gap_mid": 10.0, "status": "ok", "available_at": "20260710"}
        ctx = _make_context(gap=gap)
        result = ExpectationGapFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.value == 1.0
        assert not result.degraded

    def test_provider_exception_degrades_gracefully(self):
        """Provider 异常时优雅降级，不抛异常。"""
        def raising_provider(_s: str, _a: str) -> list:
            raise RuntimeError("provider down")
        ctx = ShadowFeatureContext(expectation_revision_provider=raising_provider)
        result = EarningsRevisionFeature().compute(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        assert result.degraded
        assert result.value == 0.0

    def test_compute_all_exception_does_not_crash(self):
        """compute_all 中单个特征异常不影响其他特征。"""
        def raising_provider(_s: str, _a: str) -> list:
            raise RuntimeError("boom")
        ctx = ShadowFeatureContext(expectation_revision_provider=raising_provider)
        feature_set = ShadowFeatureSet.compute_all(
            symbol="000001", packet=_make_packet(), as_of="20260710", context=ctx,
        )
        # earnings_revision 降级但其他特征仍计算
        assert "earnings_revision" in feature_set.features
        assert feature_set.features["earnings_revision"] == 0.0
        # theme_phase 不依赖 Provider，应正常计算
        assert feature_set.features["theme_phase"] > 0.0
