"""P2.2 基本面发现漏斗测试。

按 P2 计划 §4 Task P2.2 验收：
- 结果可解释到字段和公式
- 去掉技术字段后基本面排序不发生定义性变化
- 缺数据公司不会被默认中性值抬高
- 相同输入和 policy 结果可复现

测试组织：
- TestPolicies：Policy 数据类与仓储
- TestFunnelSingleDimension：单维度评分
- TestFunnelClassification：公司分类（include/exclude/pending_data/shadow_*）
- TestFunnelPareto：Pareto frontier
- TestFunnelSnapshot：整个截面评估
- TestFunnelShadowVsActive：shadow vs active policy
- TestExplainabilityAcceptance：验收点 1（可解释到字段和公式）
- TestTechnicalFieldRemovalAcceptance：验收点 2（去掉技术字段后排序不变）
- TestMissingDataNotElevatedAcceptance：验收点 3（缺数据不被中性值抬高）
- TestDeterminismAcceptance：验收点 4（相同输入相同结果）
- TestFunnelGraceful：字段缺失降级（AGENTS.md）
- TestNoTradeAdvice：AGENTS.md 合规性
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from core.research.discovery.contracts import (
    DATA_AVAILABILITY_AVAILABLE,
    DATA_AVAILABILITY_MISSING,
    DATA_AVAILABILITY_NOT_COMPUTABLE,
    FIELD_GROUP_CONSENSUS_REVISIONS,
    FIELD_GROUP_EVIDENCE_QUALITY,
    FIELD_GROUP_FINANCIAL_QUALITY,
    FIELD_GROUP_GROWTH,
    FIELD_GROUP_INDUSTRY_TIMING,
    FIELD_GROUP_NAMES,
    FIELD_GROUP_VALUATION,
    FieldGroup,
    SnapshotField,
    UniverseMember,
    UniverseSnapshot,
)
from core.research.discovery.funnel import (
    CompanyFunnelResult,
    DimensionScore,
    FundamentalFunnel,
    FunnelOutput,
    STATUS_EXCLUDE,
    STATUS_INCLUDE,
    STATUS_PENDING_DATA,
    STATUS_SHADOW_INCLUDE,
    _min_max_normalize,
)
from core.research.discovery.policies import (
    DIMENSION_EVIDENCE_QUALITY,
    DIMENSION_EXPECTATION,
    DIMENSION_GROWTH,
    DIMENSION_INDUSTRY_TIMING,
    DIMENSION_NAMES,
    DIMENSION_QUALITY,
    DIMENSION_VALUATION_BURDEN,
    GATING_STATUSES,
    POLICY_STATUS_ACTIVE,
    POLICY_STATUS_DEPRECATED,
    POLICY_STATUS_DRAFT,
    POLICY_STATUS_SHADOW,
    POLICY_STATUSES,
    DimensionPolicy,
    FunnelPolicy,
    PolicyRepository,
    build_default_shadow_policy,
    build_empty_active_policy,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_field(
    name: str,
    value: float | None,
    status: str = DATA_AVAILABILITY_AVAILABLE,
    available_at: str = "2024-06-30",
    source_refs: list[str] | None = None,
    unit: str = "ratio",
) -> SnapshotField:
    """构造 SnapshotField。"""
    return SnapshotField(
        name=name,
        value=value,
        unit=unit,
        available_at=available_at,
        source_refs=source_refs or ["src_test_001"],
        status=status,
        formula=f"{name} = value",
    )


def _make_field_group(
    name: str,
    fields: list[SnapshotField] | None = None,
) -> FieldGroup:
    """构造 FieldGroup。"""
    return FieldGroup(name=name, fields=fields or [])


def _make_member(
    symbol: str,
    quality_fields: list[SnapshotField] | None = None,
    growth_fields: list[SnapshotField] | None = None,
    expectation_fields: list[SnapshotField] | None = None,
    valuation_fields: list[SnapshotField] | None = None,
    industry_fields: list[SnapshotField] | None = None,
    evidence_fields: list[SnapshotField] | None = None,
    as_of: str = "2024-12-31",
    company_name: str = "",
) -> UniverseMember:
    """构造测试用 UniverseMember。

    默认填满 9 个字段组（未指定的为空 FieldGroup）。
    """
    field_groups: dict[str, FieldGroup] = {n: FieldGroup(name=n) for n in FIELD_GROUP_NAMES}
    if quality_fields is not None:
        field_groups[FIELD_GROUP_FINANCIAL_QUALITY] = _make_field_group(
            FIELD_GROUP_FINANCIAL_QUALITY, quality_fields
        )
    if growth_fields is not None:
        field_groups[FIELD_GROUP_GROWTH] = _make_field_group(FIELD_GROUP_GROWTH, growth_fields)
    if expectation_fields is not None:
        field_groups[FIELD_GROUP_CONSENSUS_REVISIONS] = _make_field_group(
            FIELD_GROUP_CONSENSUS_REVISIONS, expectation_fields
        )
    if valuation_fields is not None:
        field_groups[FIELD_GROUP_VALUATION] = _make_field_group(
            FIELD_GROUP_VALUATION, valuation_fields
        )
    if industry_fields is not None:
        field_groups[FIELD_GROUP_INDUSTRY_TIMING] = _make_field_group(
            FIELD_GROUP_INDUSTRY_TIMING, industry_fields
        )
    if evidence_fields is not None:
        field_groups[FIELD_GROUP_EVIDENCE_QUALITY] = _make_field_group(
            FIELD_GROUP_EVIDENCE_QUALITY, evidence_fields
        )
    return UniverseMember(
        member_id=f"member_test_{symbol}",
        snapshot_id="snap_test",
        symbol=symbol,
        company_name=company_name or symbol,
        as_of=as_of,
        field_groups=field_groups,
    )


def _make_full_quality_fields(
    roic: float = 0.15,
    ocf_to_net_income: float = 1.0,
    debt_to_equity: float = 0.5,
    gross_margin: float = 0.3,
    net_margin: float = 0.15,
) -> list[SnapshotField]:
    """构造完整的财务质量字段。"""
    return [
        _make_field("roic", roic),
        _make_field("ocf_to_net_income", ocf_to_net_income),
        _make_field("debt_to_equity", debt_to_equity),
        _make_field("gross_margin", gross_margin),
        _make_field("net_margin", net_margin),
    ]


def _make_full_growth_fields(
    revenue_growth: float = 0.2,
    net_profit_growth: float = 0.25,
    incremental_roic: float = 0.1,
    eps_growth: float = 0.2,
) -> list[SnapshotField]:
    """构造完整的成长字段。"""
    return [
        _make_field("revenue_growth_yoy", revenue_growth),
        _make_field("net_profit_growth_yoy", net_profit_growth),
        _make_field("incremental_roic", incremental_roic),
        _make_field("eps_growth_yoy", eps_growth),
    ]


def _make_full_valuation_fields(
    pe_to_growth: float = 1.0,
    ev_ebitda_to_growth: float = 1.0,
    pb_to_roe: float = 0.1,
) -> list[SnapshotField]:
    """构造完整的估值字段。"""
    return [
        _make_field("pe_to_growth", pe_to_growth),
        _make_field("ev_ebitda_to_growth", ev_ebitda_to_growth),
        _make_field("pb_to_roe", pb_to_roe),
    ]


def _make_active_policy_with_weights() -> FunnelPolicy:
    """构造带权重的 active policy（用于门禁测试）。"""
    policy = build_default_shadow_policy()
    policy.policy_id = "test_active_policy"
    policy.version = "v1.0.0-active"
    policy.status = POLICY_STATUS_ACTIVE
    policy.description = "测试用 active policy，含完整权重。"
    policy.config_hash = policy.compute_config_hash()
    return policy


# ---------------------------------------------------------------------------
# TestPolicies：Policy 数据类与仓储
# ---------------------------------------------------------------------------


class TestPolicies:
    """Policy 数据类与仓储测试。"""

    def test_dimension_policy_validate_ok(self) -> None:
        """DimensionPolicy 合法配置通过校验。"""
        dp = DimensionPolicy(
            dimension=DIMENSION_QUALITY,
            weight_map={"roic": 0.3, "roe": 0.7},
            threshold_min=0.3,
            threshold_max=1.0,
            missing_strategy="exclude",
            neutral_fill=0.5,
        )
        assert dp.validate() == []

    def test_dimension_policy_validate_invalid_dimension(self) -> None:
        """非法维度名校验失败。"""
        dp = DimensionPolicy(dimension="invalid_dim")
        errors = dp.validate()
        assert any("非法维度名" in e for e in errors)

    def test_dimension_policy_validate_invalid_missing_strategy(self) -> None:
        """非法 missing_strategy 校验失败。"""
        dp = DimensionPolicy(
            dimension=DIMENSION_QUALITY,
            missing_strategy="bogus",
        )
        errors = dp.validate()
        assert any("missing_strategy" in e for e in errors)

    def test_dimension_policy_validate_negative_weight(self) -> None:
        """负权重校验失败。"""
        dp = DimensionPolicy(
            dimension=DIMENSION_QUALITY,
            weight_map={"roic": -0.1},
        )
        errors = dp.validate()
        assert any("权重为负" in e for e in errors)

    def test_dimension_policy_validate_threshold_inversion(self) -> None:
        """threshold_min > threshold_max 校验失败。"""
        dp = DimensionPolicy(
            dimension=DIMENSION_QUALITY,
            threshold_min=0.8,
            threshold_max=0.3,
        )
        errors = dp.validate()
        assert any("threshold_min" in e for e in errors)

    def test_funnel_policy_validate_ok(self) -> None:
        """完整 FunnelPolicy 合法配置通过校验。"""
        policy = build_default_shadow_policy()
        assert policy.validate() == []

    def test_funnel_policy_validate_missing_id(self) -> None:
        """缺 policy_id 校验失败。"""
        policy = build_default_shadow_policy()
        policy.policy_id = ""
        errors = policy.validate()
        assert any("policy_id" in e for e in errors)

    def test_funnel_policy_validate_invalid_status(self) -> None:
        """非法 status 校验失败。"""
        policy = build_default_shadow_policy()
        policy.status = "bogus"
        errors = policy.validate()
        assert any("status" in e for e in errors)

    def test_funnel_policy_config_hash_deterministic(self) -> None:
        """相同 Policy 配置 hash 一致。"""
        p1 = build_default_shadow_policy()
        p2 = build_default_shadow_policy()
        # created_at 不同不影响 config_hash
        p2.created_at = p1.created_at
        assert p1.compute_config_hash() == p2.compute_config_hash()

    def test_funnel_policy_is_gating(self) -> None:
        """仅 active 状态可作为门禁。"""
        shadow = build_default_shadow_policy()
        assert shadow.is_gating() is False
        assert shadow.is_shadow() is True
        active = _make_active_policy_with_weights()
        assert active.is_gating() is True
        assert active.is_shadow() is False

    def test_funnel_policy_serialization_round_trip(self) -> None:
        """Policy 序列化往返一致。"""
        policy = build_default_shadow_policy()
        d = policy.to_dict()
        restored = FunnelPolicy.from_dict(d)
        assert restored.policy_id == policy.policy_id
        assert restored.version == policy.version
        assert restored.status == policy.status
        assert set(restored.dimensions.keys()) == set(policy.dimensions.keys())
        assert restored.config_hash == policy.config_hash

    def test_policy_repository_save_and_get(self) -> None:
        """仓储 save/get。"""
        repo = PolicyRepository()
        policy = build_default_shadow_policy()
        repo.save(policy)
        got = repo.get(policy.policy_id, policy.version)
        assert got is not None
        assert got.policy_id == policy.policy_id
        # 不带 version 取最新
        latest = repo.get(policy.policy_id)
        assert latest is not None
        assert latest.version == policy.version

    def test_policy_repository_no_overwrite(self) -> None:
        """同 (policy_id, version) 不可覆盖。"""
        repo = PolicyRepository()
        policy = build_default_shadow_policy()
        repo.save(policy)
        with pytest.raises(ValueError, match="Policy 已存在"):
            repo.save(policy)

    def test_policy_repository_list_by_status(self) -> None:
        """按状态过滤列出。"""
        repo = PolicyRepository()
        shadow = build_default_shadow_policy()
        active = _make_active_policy_with_weights()
        repo.save(shadow)
        repo.save(active)
        assert len(repo.list_shadow()) == 1
        assert len(repo.list_active()) == 1
        assert len(repo.list_policies()) == 2

    def test_build_empty_active_policy(self) -> None:
        """空 active policy 构造合法。"""
        policy = build_empty_active_policy()
        assert policy.status == POLICY_STATUS_ACTIVE
        assert policy.is_gating() is True
        # 空权重合法（不产生实际评分）
        for dim in DIMENSION_NAMES:
            dp = policy.get_dimension(dim)
            assert dp is not None
            assert dp.weight_map == {}


# ---------------------------------------------------------------------------
# TestFunnelSingleDimension：单维度评分
# ---------------------------------------------------------------------------


def test_funnel_degrades_non_finite_field_values() -> None:
    policy = build_default_shadow_policy()
    funnel = FundamentalFunnel(policy)
    member = _make_member(
        "600519.SH",
        quality_fields=[_make_field("roic", float("nan"))],
    )

    result = funnel.evaluate(member)

    assert result.dimension_scores[DIMENSION_QUALITY].is_missing
    assert _min_max_normalize(float("inf"), 0.0, 1.0) == 0.5


class TestFunnelSingleDimension:
    """单维度评分测试。"""

    def test_evaluate_single_dimension_quality(self) -> None:
        """评估财务质量维度。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(roic=0.2),
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        assert ds.dimension == DIMENSION_QUALITY
        assert ds.score is not None
        assert 0.0 <= ds.score <= 1.0
        assert ds.used_field_count == 5
        assert ds.coverage_ratio == 1.0
        assert ds.is_missing is False

    def test_evaluate_dimension_missing_fields(self) -> None:
        """字段缺失时维度标记为缺数据。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # quality 字段组为空
        member = _make_member("600519.SH")
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        assert ds.is_missing is True
        assert ds.missing_reason == "no_field_available"
        assert ds.score is None

    def test_evaluate_dimension_below_min_coverage(self) -> None:
        """覆盖率 < 0.5 标记为缺数据。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 仅 1/5 字段（覆盖率 0.2 < 0.5）
        member = _make_member(
            "600519.SH",
            quality_fields=[_make_field("roic", 0.15)],
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        assert ds.is_missing is True
        assert ds.missing_reason == "below_min_coverage"

    def test_evaluate_dimension_not_computable_status(self) -> None:
        """字段 status=not_computable 不参与评分。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=[
                _make_field("roic", 0.15, status=DATA_AVAILABILITY_NOT_COMPUTABLE),
                _make_field("ocf_to_net_income", 1.0),
                _make_field("debt_to_equity", 0.5),
                _make_field("gross_margin", 0.3),
                _make_field("net_margin", 0.15),
            ],
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        # 4/5 = 0.8 ≥ 0.5（roic 不可用，其余 4 个可用）
        assert ds.is_missing is False
        assert ds.used_field_count == 4
        assert ds.coverage_ratio == 0.8

    def test_evaluate_field_scores_traceable(self) -> None:
        """字段评分可追溯到字段名、值、归一化分数、来源。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(),
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        assert len(ds.field_scores) == 5
        for fs in ds.field_scores:
            assert "name" in fs
            assert "value" in fs
            assert "normalized_score" in fs
            assert "weight" in fs
            assert "available_at" in fs

    def test_evaluate_min_max_normalize_higher_is_better(self) -> None:
        """归一化：越大越好。"""
        # roic 在 [0, 0.25]，0.125 → 0.5
        score = _min_max_normalize(0.125, 0.0, 0.25, higher_is_better=True)
        assert abs(score - 0.5) < 1e-6

    def test_evaluate_min_max_normalize_lower_is_better(self) -> None:
        """归一化：越小越好（如 PE）。"""
        # pe_to_growth 在 [0, 3]，1.5 → 0.5（反向）
        score = _min_max_normalize(1.5, 0.0, 3.0, higher_is_better=False)
        assert abs(score - 0.5) < 1e-6

    def test_evaluate_min_max_normalize_clamp(self) -> None:
        """归一化：超出范围 clamp 到 [0, 1]。"""
        assert _min_max_normalize(-1.0, 0.0, 1.0) == 0.0
        assert _min_max_normalize(2.0, 0.0, 1.0) == 1.0

    def test_evaluate_missing_strategy_exclude(self) -> None:
        """exclude 策略：仅按已用字段归一化（默认）。"""
        policy = build_default_shadow_policy()
        # 强制 exclude 策略
        for dim in policy.dimensions.values():
            dim.missing_strategy = "exclude"
        policy.config_hash = policy.compute_config_hash()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=[
                _make_field("roic", 0.2),     # 0.8
                _make_field("ocf_to_net_income", 1.5),  # 1.0
                # debt_to_equity, gross_margin 缺
                _make_field("net_margin", 0.3),  # 1.0
            ],
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        # 3/5 = 0.6 ≥ 0.5
        assert ds.is_missing is False
        # exclude 策略：weight_total 仅含已用字段权重 0.3 + 0.2 + 0.15
        # weighted = 0.8*0.3 + 1.0*0.2 + 1.0*0.15
        # debt_to_equity (0.2) 与 gross_margin (0.15) 不计入
        assert ds.used_field_count == 3

    def test_evaluate_missing_strategy_penalize(self) -> None:
        """penalize 策略：缺字段按 0 计入。"""
        policy = build_default_shadow_policy()
        for dim in policy.dimensions.values():
            dim.missing_strategy = "penalize"
        policy.config_hash = policy.compute_config_hash()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=[
                _make_field("roic", 0.25),     # 1.0
                _make_field("roe", 0.25),      # 1.0
                _make_field("ocf_to_net_income", 1.5),  # 1.0
                _make_field("net_margin", 0.3),  # 1.0
                # debt_to_equity, gross_margin 缺 → 0
            ],
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        # penalize：缺字段权重加入分母
        # weight_total = 0.3+0.2+0.2+0.15+0.15 (all)
        # weighted = 1.0*0.3 + 1.0*0.2 + 1.0*0.2 + 1.0*0.15 + 0*0.15
        # score = weighted / weight_total
        assert ds.is_missing is False
        assert ds.score is not None
        # penalize 分数应低于 exclude
        assert ds.score < 0.9  # 4 个 1.0 + 1 个 0 → < 1.0


# ---------------------------------------------------------------------------
# TestFunnelClassification：公司分类
# ---------------------------------------------------------------------------


class TestFunnelClassification:
    """公司分类测试。"""

    def test_classify_shadow_policy_returns_shadow_status(self) -> None:
        """shadow policy 仅产生 shadow_* 状态。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 所有字段都有 → 6 维度都有
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        # shadow policy 不产生 include
        assert result.status == STATUS_SHADOW_INCLUDE
        assert "shadow_policy_does_not_produce_gating_decision" in result.inclusion_reasons

    def test_classify_pending_data_low_coverage(self) -> None:
        """维度覆盖率低于阈值 → pending_data。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 仅 1 个维度有数据 → coverage 1/6 ≈ 0.17 < 0.8
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(),
        )
        result = funnel.evaluate(member)
        assert result.status == STATUS_PENDING_DATA
        assert any("dimension_coverage" in r for r in result.pending_data_reasons)

    def test_classify_active_policy_exclude_on_threshold_min(self) -> None:
        """active policy：维度低于 threshold_min → exclude。"""
        policy = _make_active_policy_with_weights()
        # 把 quality threshold_min 设为 0.95（不可能达到）
        policy.dimensions[DIMENSION_QUALITY].threshold_min = 0.95
        policy.config_hash = policy.compute_config_hash()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        # active policy + threshold 不通过 → exclude
        assert result.status == STATUS_EXCLUDE
        assert any("threshold_min" in r for r in result.exclusion_reasons)

    def test_classify_active_policy_include_on_pass(self) -> None:
        """active policy：所有维度通过门禁 → include。"""
        policy = _make_active_policy_with_weights()
        # 默认 threshold_min=0.3，正常数据可通过
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "600519.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        assert result.status == STATUS_INCLUDE
        assert any("dimension_coverage" in r for r in result.inclusion_reasons)


# ---------------------------------------------------------------------------
# TestFunnelPareto：Pareto frontier
# ---------------------------------------------------------------------------


class TestFunnelPareto:
    """Pareto frontier 测试。"""

    def test_pareto_single_dominating(self) -> None:
        """一个公司支配另一个 → 后者不在 frontier。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # A 在所有维度都比 B 强
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(roic=0.25),  # 1.0
            growth_fields=_make_full_growth_fields(revenue_growth=0.5),  # 1.0
            expectation_fields=[
                _make_field("consensus_revision_direction", 1.0),
                _make_field("consensus_dispersion", 0.0),
                _make_field("independent_forecast_gap", 0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=0.0, ev_ebitda_to_growth=0.0, pb_to_roe=0.0
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 1.0),
                _make_field("supply_demand_balance", 1.0),
                _make_field("policy_tailwind", 1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 5),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 1.0),
                _make_field("completeness_score", 1.0),
            ],
        )
        member_b = _make_member(
            "B.SH",
            quality_fields=_make_full_quality_fields(roic=0.05),  # 0.2
            growth_fields=_make_full_growth_fields(revenue_growth=-0.2),  # 0.0
            expectation_fields=[
                _make_field("consensus_revision_direction", -1.0),
                _make_field("consensus_dispersion", 0.3),
                _make_field("independent_forecast_gap", -0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=3.0, ev_ebitda_to_growth=3.0, pb_to_roe=0.3
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.0),
                _make_field("supply_demand_balance", -1.0),
                _make_field("policy_tailwind", -1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 0),
                _make_field("pit_compliance", 0.0),
                _make_field("independence_score", 0.0),
                _make_field("completeness_score", 0.0),
            ],
        )
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        assert "A.SH" in output.pareto_frontier
        # B 被 A 支配 → 不在 frontier
        assert "B.SH" not in output.pareto_frontier
        # B 的结果记录被 A 支配
        result_b = output.get_result("B.SH")
        assert result_b is not None
        assert "A.SH" in result_b.pareto_dominated_by

    def test_pareto_non_comparable_both_in_frontier(self) -> None:
        """不可比（缺不同维度）→ 双方都在 frontier。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # A 只有 quality，B 只有 growth
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
        )
        member_b = _make_member(
            "B.SH",
            growth_fields=_make_full_growth_fields(),
        )
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        # 双方维度不可比 → 都在 frontier
        assert "A.SH" in output.pareto_frontier
        assert "B.SH" in output.pareto_frontier

    def test_pareto_no_data_excluded_from_frontier(self) -> None:
        """完全缺数据的公司不参与 Pareto 比较。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        # B 全空
        member_b = _make_member("B.SH")
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        # A 在 frontier
        assert "A.SH" in output.pareto_frontier
        # B 无任何可比维度 → 不参与
        assert "B.SH" not in output.pareto_frontier

    def test_pareto_dominates_method_logic(self) -> None:
        """_dominates 方法逻辑测试。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 构造两个 result 对象
        r_a = CompanyFunnelResult(symbol="A")
        r_a.dimension_scores = {
            DIMENSION_QUALITY: DimensionScore(dimension=DIMENSION_QUALITY, score=0.9),
            DIMENSION_GROWTH: DimensionScore(dimension=DIMENSION_GROWTH, score=0.5),
        }
        r_b = CompanyFunnelResult(symbol="B")
        r_b.dimension_scores = {
            DIMENSION_QUALITY: DimensionScore(dimension=DIMENSION_QUALITY, score=0.5),
            DIMENSION_GROWTH: DimensionScore(dimension=DIMENSION_GROWTH, score=0.5),
        }
        # A 支配 B（quality 严格大，growth 相等）
        assert funnel._dominates(r_a, r_b, (DIMENSION_QUALITY, DIMENSION_GROWTH)) is True
        # B 不支配 A
        assert funnel._dominates(r_b, r_a, (DIMENSION_QUALITY, DIMENSION_GROWTH)) is False

    def test_pareto_dominates_with_missing_data(self) -> None:
        """_dominates 缺数据维度不可比，需有严格大于的可比维度才能支配。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        r_a = CompanyFunnelResult(symbol="A")
        r_a.dimension_scores = {
            DIMENSION_QUALITY: DimensionScore(dimension=DIMENSION_QUALITY, score=0.5),
            DIMENSION_GROWTH: DimensionScore(
                dimension=DIMENSION_GROWTH, score=None, is_missing=True
            ),
        }
        r_b = CompanyFunnelResult(symbol="B")
        r_b.dimension_scores = {
            DIMENSION_QUALITY: DimensionScore(dimension=DIMENSION_QUALITY, score=0.5),
            DIMENSION_GROWTH: DimensionScore(dimension=DIMENSION_GROWTH, score=1.0),
        }
        # quality 维度 A=B（相等，不严格大于）；growth 不可比
        # A 不能支配 B（无严格大于的可比维度）
        assert funnel._dominates(r_a, r_b, (DIMENSION_QUALITY, DIMENSION_GROWTH)) is False
        # 反向：B 也不能支配 A（quality 相等，growth 不可比）
        assert funnel._dominates(r_b, r_a, (DIMENSION_QUALITY, DIMENSION_GROWTH)) is False


# ---------------------------------------------------------------------------
# TestFunnelSnapshot：整个截面评估
# ---------------------------------------------------------------------------


class TestFunnelSnapshot:
    """整个截面评估测试。"""

    def test_evaluate_snapshot_returns_funnel_output(self) -> None:
        """评估整个截面返回 FunnelOutput。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        member_b = _make_member("B.SH", growth_fields=_make_full_growth_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        assert isinstance(output, FunnelOutput)
        assert output.snapshot_id == "snap_test"
        assert output.as_of == "2024-12-31"
        assert output.policy_id == policy.policy_id
        assert output.policy_version == policy.version
        assert output.policy_status == POLICY_STATUS_SHADOW
        assert len(output.results) == 2
        # 两个公司覆盖率都不足 → research_pending
        assert len(output.research_pending_symbols) == 2

    def test_evaluate_snapshot_grouping(self) -> None:
        """评估结果按分组归类。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 公司 A：6 维度全有 → shadow_include
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        # 公司 B：仅 1 维度 → pending_data
        member_b = _make_member("B.SH", quality_fields=_make_full_quality_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        assert "A.SH" in output.shadow_symbols
        assert "B.SH" in output.research_pending_symbols
        assert "high_conviction" in output.grouping
        assert "shadow" in output.grouping
        assert "research_pending" in output.grouping
        assert "excluded" in output.grouping

    def test_evaluate_snapshot_funnel_id_deterministic(self) -> None:
        """相同 snapshot + 相同 policy → 相同 funnel_id。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a],
        )
        output1 = funnel.evaluate_snapshot(snapshot)
        output2 = funnel.evaluate_snapshot(snapshot)
        assert output1.funnel_id == output2.funnel_id

    def test_evaluate_snapshot_serialization_round_trip(self) -> None:
        """FunnelOutput 序列化往返一致。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member],
        )
        output = funnel.evaluate_snapshot(snapshot)
        d = output.to_dict()
        assert isinstance(d, dict)
        assert d["funnel_id"] == output.funnel_id
        assert len(d["results"]) == 1
        # JSON 可序列化
        s = json.dumps(d, ensure_ascii=False)
        assert isinstance(s, str)


# ---------------------------------------------------------------------------
# TestFunnelShadowVsActive：shadow vs active policy
# ---------------------------------------------------------------------------


class TestFunnelShadowVsActive:
    """shadow vs active policy 行为差异。"""

    def test_shadow_does_not_produce_include(self) -> None:
        """shadow policy 不产生 include 状态。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        assert result.status != STATUS_INCLUDE
        assert result.status == STATUS_SHADOW_INCLUDE

    def test_active_can_produce_include(self) -> None:
        """active policy 可产生 include 状态。"""
        policy = _make_active_policy_with_weights()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        assert result.status == STATUS_INCLUDE

    def test_active_can_produce_exclude(self) -> None:
        """active policy 可产生 exclude 状态。"""
        policy = _make_active_policy_with_weights()
        # 把 quality threshold_min 设为不可能值（同时调高 threshold_max 避免校验失败）
        policy.dimensions[DIMENSION_QUALITY].threshold_min = 1.5
        policy.dimensions[DIMENSION_QUALITY].threshold_max = 2.0
        policy.config_hash = policy.compute_config_hash()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        assert result.status == STATUS_EXCLUDE

    def test_invalid_policy_raises(self) -> None:
        """非法 Policy 构造 FundamentalFunnel 抛错。"""
        policy = build_default_shadow_policy()
        policy.policy_id = ""  # 使校验失败
        with pytest.raises(ValueError, match="Policy 校验失败"):
            FundamentalFunnel(policy)


# ---------------------------------------------------------------------------
# TestExplainabilityAcceptance：验收点 1（可解释到字段和公式）
# ---------------------------------------------------------------------------


class TestExplainabilityAcceptance:
    """验收点 1：结果可解释到字段和公式。"""

    def test_dimension_score_contains_formula(self) -> None:
        """维度评分含公式字段。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        assert ds.formula  # 非空
        assert "weighted_sum" in ds.formula

    def test_field_scores_contain_name_value_score(self) -> None:
        """字段评分含字段名、原始值、归一化分数。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(roic=0.2),
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        roic_score = next(fs for fs in ds.field_scores if fs["name"] == "roic")
        assert roic_score["value"] == 0.2
        assert roic_score["normalized_score"] is not None
        assert "weight" in roic_score
        assert "available_at" in roic_score
        assert "source_refs" in roic_score

    def test_missing_field_recorded_with_reason(self) -> None:
        """缺字段记录原因。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=[_make_field("roic", 0.2)],  # 仅 1/5
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        # 覆盖率 < 0.5 → 缺数据
        assert ds.is_missing is True
        assert ds.missing_reason == "below_min_coverage"
        # 字段评分列表中有 4 个 missing 状态
        missing_count = sum(1 for fs in ds.field_scores if fs.get("status") == "missing")
        assert missing_count == 4

    def test_result_traceable_to_policy(self) -> None:
        """结果可追溯到 policy_id/version/status/config_hash。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH")
        result = funnel.evaluate(member)
        assert result.policy_id == policy.policy_id
        assert result.policy_version == policy.version
        assert result.policy_status == policy.status
        assert result.config_hash == policy.config_hash

    def test_inclusion_exclusion_reasons_explainable(self) -> None:
        """入选/排除/待补原因可解释。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 缺数据 → pending_data
        member = _make_member("A.SH")
        result = funnel.evaluate(member)
        assert result.status == STATUS_PENDING_DATA
        assert len(result.pending_data_reasons) > 0
        # 原因包含具体维度
        assert any("dimension_coverage" in r for r in result.pending_data_reasons)


# ---------------------------------------------------------------------------
# TestTechnicalFieldRemovalAcceptance：验收点 2（去掉技术字段后排序不变）
# ---------------------------------------------------------------------------


class TestTechnicalFieldRemovalAcceptance:
    """验收点 2：去掉技术字段后基本面排序不发生定义性变化。

    释义：6 个独立排序维度都是基本面维度（quality/growth/expectation/
    valuation_burden/industry_timing/evidence_quality），不含纯技术字段
    （如成交量、换手率、动量信号）。移除任意一个维度后，剩余维度的
    Pareto frontier 应保持相对稳定（不发生定义性反转）。
    """

    def test_pareto_frontier_stable_after_removing_one_dimension(self) -> None:
        """移除一个维度后 Pareto frontier 保持稳定。"""
        policy = build_default_shadow_policy()
        # 公司 A 在所有维度都强
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(roic=0.25),
            growth_fields=_make_full_growth_fields(revenue_growth=0.5),
            expectation_fields=[
                _make_field("consensus_revision_direction", 1.0),
                _make_field("consensus_dispersion", 0.0),
                _make_field("independent_forecast_gap", 0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=0.0, ev_ebitda_to_growth=0.0, pb_to_roe=0.0
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 1.0),
                _make_field("supply_demand_balance", 1.0),
                _make_field("policy_tailwind", 1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 5),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 1.0),
                _make_field("completeness_score", 1.0),
            ],
        )
        # 公司 B 在所有维度都弱
        member_b = _make_member(
            "B.SH",
            quality_fields=_make_full_quality_fields(roic=0.05),
            growth_fields=_make_full_growth_fields(revenue_growth=-0.2),
            expectation_fields=[
                _make_field("consensus_revision_direction", -1.0),
                _make_field("consensus_dispersion", 0.3),
                _make_field("independent_forecast_gap", -0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=3.0, ev_ebitda_to_growth=3.0, pb_to_roe=0.3
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.0),
                _make_field("supply_demand_balance", -1.0),
                _make_field("policy_tailwind", -1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 0),
                _make_field("pit_compliance", 0.0),
                _make_field("independence_score", 0.0),
                _make_field("completeness_score", 0.0),
            ],
        )
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )

        # 原始 frontier
        funnel_full = FundamentalFunnel(policy)
        output_full = funnel_full.evaluate_snapshot(snapshot)
        assert "A.SH" in output_full.pareto_frontier
        assert "B.SH" not in output_full.pareto_frontier

        # 移除 evidence_quality 维度后
        policy_reduced = copy.deepcopy(policy)
        policy_reduced.pareto_dimensions = tuple(
            d for d in DIMENSION_NAMES if d != DIMENSION_EVIDENCE_QUALITY
        )
        policy_reduced.config_hash = policy_reduced.compute_config_hash()
        funnel_reduced = FundamentalFunnel(policy_reduced)
        output_reduced = funnel_reduced.evaluate_snapshot(snapshot)
        # A 仍在 frontier，B 仍不在
        assert "A.SH" in output_reduced.pareto_frontier
        assert "B.SH" not in output_reduced.pareto_frontier

    def test_no_technical_dimensions_in_default_policy(self) -> None:
        """默认 Policy 6 维度均为基本面维度，无技术字段。"""
        policy = build_default_shadow_policy()
        # 6 维度都是基本面维度
        for dim in DIMENSION_NAMES:
            assert dim in policy.dimensions
        # 不含技术字段维度（如 volume/momentum/turnover）
        forbidden = {"volume", "momentum", "turnover", "technical_signal"}
        for dim in policy.dimensions:
            assert dim not in forbidden


# ---------------------------------------------------------------------------
# TestMissingDataNotElevatedAcceptance：验收点 3（缺数据公司不被中性值抬高）
# ---------------------------------------------------------------------------


class TestMissingDataNotElevatedAcceptance:
    """验收点 3：缺数据公司不会被默认中性值抬高。"""

    def test_missing_dimension_returns_none_score(self) -> None:
        """缺数据维度 score=None。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH")  # 全空
        result = funnel.evaluate(member)
        for dim in DIMENSION_NAMES:
            ds = result.dimension_scores[dim]
            assert ds.is_missing is True
            assert ds.score is None

    def test_missing_company_classified_as_pending_data(self) -> None:
        """缺数据公司分类为 pending_data，不进入 high_conviction。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH")  # 全空
        result = funnel.evaluate(member)
        assert result.status == STATUS_PENDING_DATA
        assert result.status != STATUS_INCLUDE
        assert result.status != STATUS_SHADOW_INCLUDE

    def test_missing_company_not_in_high_conviction_group(self) -> None:
        """缺数据公司不在 high_conviction 分组。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member("A.SH")  # 全空
        member_b = _make_member(
            "B.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        # A 缺数据 → research_pending
        assert "A.SH" in output.research_pending_symbols
        assert "A.SH" not in output.shadow_symbols
        assert "A.SH" not in output.high_conviction_symbols
        # B 数据全 → shadow
        assert "B.SH" in output.shadow_symbols

    def test_missing_data_does_not_produce_neutral_score(self) -> None:
        """缺数据不产生中性分（默认 exclude 策略）。"""
        policy = build_default_shadow_policy()
        # 确认默认 missing_strategy=exclude
        for dim in policy.dimensions.values():
            assert dim.missing_strategy == "exclude"
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=[_make_field("roic", 0.2)],  # 仅 1/5
        )
        result = funnel.evaluate(member)
        ds = result.dimension_scores[DIMENSION_QUALITY]
        # 覆盖率不足 → 缺数据，score=None
        assert ds.is_missing is True
        assert ds.score is None

    def test_company_with_partial_data_lower_coverage_than_full(self) -> None:
        """部分缺数据公司的覆盖率低于全数据公司。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        # 公司 A：全数据
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        # 公司 B：仅 1 维度有数据
        member_b = _make_member(
            "B.SH",
            quality_fields=_make_full_quality_fields(),
        )
        result_a = funnel.evaluate(member_a)
        result_b = funnel.evaluate(member_b)
        # A 覆盖率 = 6/6 = 1.0
        assert result_a.get_available_dimension_count() == 6
        # B 覆盖率 = 1/6 ≈ 0.17
        assert result_b.get_available_dimension_count() == 1
        # A 是 shadow_include，B 是 pending_data
        assert result_a.status == STATUS_SHADOW_INCLUDE
        assert result_b.status == STATUS_PENDING_DATA


# ---------------------------------------------------------------------------
# TestDeterminismAcceptance：验收点 4（相同输入相同结果）
# ---------------------------------------------------------------------------


class TestDeterminismAcceptance:
    """验收点 4：相同输入和 policy 结果可复现。"""

    def test_same_input_same_score(self) -> None:
        """相同 member + 相同 policy → 相同得分。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
        )
        r1 = funnel.evaluate(member)
        r2 = funnel.evaluate(member)
        assert r1.dimension_scores[DIMENSION_QUALITY].score == \
               r2.dimension_scores[DIMENSION_QUALITY].score

    def test_same_input_same_status(self) -> None:
        """相同 member + 相同 policy → 相同状态。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH")
        r1 = funnel.evaluate(member)
        r2 = funnel.evaluate(member)
        assert r1.status == r2.status

    def test_same_input_same_pareto_frontier(self) -> None:
        """相同 snapshot + 相同 policy → 相同 Pareto frontier。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        member_b = _make_member("B.SH", growth_fields=_make_full_growth_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        o1 = funnel.evaluate_snapshot(snapshot)
        o2 = funnel.evaluate_snapshot(snapshot)
        assert o1.pareto_frontier == o2.pareto_frontier
        assert o1.funnel_id == o2.funnel_id

    def test_same_input_same_funnel_id(self) -> None:
        """相同 snapshot + 相同 policy → 相同 funnel_id。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member],
        )
        o1 = funnel.evaluate_snapshot(snapshot)
        o2 = funnel.evaluate_snapshot(snapshot)
        assert o1.funnel_id == o2.funnel_id

    def test_different_policy_different_config_hash(self) -> None:
        """不同 policy → 不同 config_hash。"""
        p1 = build_default_shadow_policy()
        p2 = _make_active_policy_with_weights()
        assert p1.config_hash != p2.config_hash

    def test_different_policy_different_funnel_id(self) -> None:
        """不同 policy → 不同 funnel_id。"""
        p1 = build_default_shadow_policy()
        p2 = _make_active_policy_with_weights()
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member],
        )
        o1 = FundamentalFunnel(p1).evaluate_snapshot(snapshot)
        o2 = FundamentalFunnel(p2).evaluate_snapshot(snapshot)
        assert o1.funnel_id != o2.funnel_id


# ---------------------------------------------------------------------------
# TestFunnelGraceful：字段缺失降级（AGENTS.md）
# ---------------------------------------------------------------------------


class TestFunnelGraceful:
    """字段缺失降级测试（AGENTS.md）。"""

    def test_evaluate_snapshot_with_empty_members(self) -> None:
        """空截面不崩溃。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[],
        )
        output = funnel.evaluate_snapshot(snapshot)
        assert output.results == []
        assert output.pareto_frontier == []

    def test_evaluate_snapshot_with_all_missing_member(self) -> None:
        """全空 member 不崩溃，标记 pending_data。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member("A.SH")  # 全空
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member],
        )
        output = funnel.evaluate_snapshot(snapshot)
        assert len(output.results) == 1
        assert output.results[0].status == STATUS_PENDING_DATA

    def test_evaluate_member_with_no_field_groups(self) -> None:
        """无字段组的 member 不崩溃。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = UniverseMember(symbol="A.SH", as_of="2024-12-31")
        # field_groups 为空 dict
        result = funnel.evaluate(member)
        # 所有维度都缺数据 → pending_data
        assert result.status == STATUS_PENDING_DATA
        for dim in DIMENSION_NAMES:
            ds = result.dimension_scores[dim]
            assert ds.is_missing is True


# ---------------------------------------------------------------------------
# TestNoTradeAdvice：AGENTS.md 合规性
# ---------------------------------------------------------------------------


class TestNoTradeAdvice:
    """AGENTS.md 合规性测试。

    硬门禁：不输出买入/卖出/仓位/止盈/止损等投资建议。
    """

    # 源码禁止关键词（中文交易动作词）
    SOURCE_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓",
    )

    # payload 禁止关键词（含"投资建议"）
    PAYLOAD_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
        "买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓",
        "投资建议",
    )

    @classmethod
    def _check_source_no_advice(cls) -> list[str]:
        """检查源码不含交易动作关键词。"""
        violations: list[str] = []
        src_dir = Path(__file__).parent.parent / "core" / "research" / "discovery"
        files = [
            src_dir / "policies.py",
            src_dir / "funnel.py",
            src_dir / "__init__.py",
        ]
        for fp in files:
            if not fp.exists():
                continue
            content = fp.read_text(encoding="utf-8")
            for kw in cls.SOURCE_FORBIDDEN_KEYWORDS:
                if kw in content:
                    violations.append(f"{fp.name}: 含禁用关键词 '{kw}'")
        return violations

    @classmethod
    def _check_payload_no_advice(cls) -> list[str]:
        """检查 payload 不含投资建议关键词。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        violations: list[str] = []
        for kw in cls.PAYLOAD_FORBIDDEN_KEYWORDS:
            if kw in payload:
                violations.append(f"payload 含禁用关键词 '{kw}'")
        return violations

    def test_source_no_trade_advice_keywords(self) -> None:
        """源码不含交易动作关键词。"""
        violations = self._check_source_no_advice()
        assert violations == [], "源码含禁用关键词: " + "; ".join(violations)

    def test_payload_no_trade_advice_keywords(self) -> None:
        """payload 不含投资建议关键词。"""
        violations = self._check_payload_no_advice()
        assert violations == [], "payload 含禁用关键词: " + "; ".join(violations)

    def test_shadow_policy_does_not_produce_gating(self) -> None:
        """shadow policy 不产生门禁决策（不暗示买卖）。"""
        policy = build_default_shadow_policy()
        # shadow policy 不能作为门禁
        assert policy.is_gating() is False
        # 所有结果都是 shadow_* 状态，不产生 include/exclude
        funnel = FundamentalFunnel(policy)
        member = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        result = funnel.evaluate(member)
        assert result.status in (STATUS_SHADOW_INCLUDE,)
        assert result.status not in (STATUS_INCLUDE, STATUS_EXCLUDE)

    def test_no_buy_sell_signal_in_output(self) -> None:
        """输出不含买卖信号。"""
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        member_a = _make_member("A.SH", quality_fields=_make_full_quality_fields())
        member_b = _make_member("B.SH", growth_fields=_make_full_growth_fields())
        snapshot = UniverseSnapshot(
            snapshot_id="snap_test",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )
        output = funnel.evaluate_snapshot(snapshot)
        payload = json.dumps(output.to_dict(), ensure_ascii=False)
        # 不含买卖信号关键词
        for kw in ("buy", "sell", "long", "short_position", "buy_signal", "sell_signal"):
            # 不区分大小写检查
            assert kw.lower() not in payload.lower(), f"payload 含 '{kw}'"


# ---------------------------------------------------------------------------
# TestEndToEndIntegration：端到端集成
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端集成测试。"""

    def test_full_workflow_snapshot_to_funnel(self) -> None:
        """完整工作流：snapshot → funnel → output。"""
        # 1. 构造截面（3 家公司）
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(roic=0.25),
            growth_fields=_make_full_growth_fields(revenue_growth=0.5),
            expectation_fields=[
                _make_field("consensus_revision_direction", 1.0),
                _make_field("consensus_dispersion", 0.0),
                _make_field("independent_forecast_gap", 0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=0.5, ev_ebitda_to_growth=0.5, pb_to_roe=0.05
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 1.0),
                _make_field("supply_demand_balance", 1.0),
                _make_field("policy_tailwind", 1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 5),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 1.0),
                _make_field("completeness_score", 1.0),
            ],
        )
        member_b = _make_member(
            "B.SH",
            quality_fields=_make_full_quality_fields(roic=0.05),
            growth_fields=_make_full_growth_fields(revenue_growth=-0.2),
            expectation_fields=[
                _make_field("consensus_revision_direction", -1.0),
                _make_field("consensus_dispersion", 0.3),
                _make_field("independent_forecast_gap", -0.3),
            ],
            valuation_fields=_make_full_valuation_fields(
                pe_to_growth=3.0, ev_ebitda_to_growth=3.0, pb_to_roe=0.3
            ),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.0),
                _make_field("supply_demand_balance", -1.0),
                _make_field("policy_tailwind", -1.0),
            ],
            evidence_fields=[
                _make_field("source_count", 0),
                _make_field("pit_compliance", 0.0),
                _make_field("independence_score", 0.0),
                _make_field("completeness_score", 0.0),
            ],
        )
        member_c = _make_member("C.SH")  # 全空
        snapshot = UniverseSnapshot(
            snapshot_id="snap_e2e",
            as_of="2024-12-31",
            members=[member_a, member_b, member_c],
        )

        # 2. 用 shadow policy 评估
        policy = build_default_shadow_policy()
        funnel = FundamentalFunnel(policy)
        output = funnel.evaluate_snapshot(snapshot)

        # 3. 验证结果
        assert len(output.results) == 3
        # A 数据完整 → shadow_include
        assert "A.SH" in output.shadow_symbols
        # B 数据完整 → shadow_include（shadow policy 不产生 exclude）
        assert "B.SH" in output.shadow_symbols
        # C 全空 → pending_data
        assert "C.SH" in output.research_pending_symbols
        # A 在 Pareto frontier 上，B 不在（被 A 支配）
        assert "A.SH" in output.pareto_frontier
        assert "B.SH" not in output.pareto_frontier
        # C 无可比维度 → 不在 frontier
        assert "C.SH" not in output.pareto_frontier

    def test_active_policy_workflow(self) -> None:
        """active policy 工作流：产生 include/exclude 决策。"""
        # 公司 A 全数据
        member_a = _make_member(
            "A.SH",
            quality_fields=_make_full_quality_fields(),
            growth_fields=_make_full_growth_fields(),
            expectation_fields=[
                _make_field("consensus_revision_direction", 0.5),
                _make_field("consensus_dispersion", 0.1),
                _make_field("independent_forecast_gap", 0.1),
            ],
            valuation_fields=_make_full_valuation_fields(),
            industry_fields=[
                _make_field("lifecycle_stage_score", 0.8),
                _make_field("supply_demand_balance", 0.5),
                _make_field("policy_tailwind", 0.5),
            ],
            evidence_fields=[
                _make_field("source_count", 3),
                _make_field("pit_compliance", 1.0),
                _make_field("independence_score", 0.8),
                _make_field("completeness_score", 0.8),
            ],
        )
        # 公司 B 全空
        member_b = _make_member("B.SH")
        snapshot = UniverseSnapshot(
            snapshot_id="snap_active",
            as_of="2024-12-31",
            members=[member_a, member_b],
        )

        policy = _make_active_policy_with_weights()
        funnel = FundamentalFunnel(policy)
        output = funnel.evaluate_snapshot(snapshot)

        # A 数据完整 → include（active policy 产生门禁决策）
        assert "A.SH" in output.high_conviction_symbols
        # B 缺数据 → pending_data
        assert "B.SH" in output.research_pending_symbols
        # A 在 frontier
        assert "A.SH" in output.pareto_frontier

    def test_policy_repository_persistence(self) -> None:
        """Policy 仓储持久化（内存版）。"""
        repo = PolicyRepository()
        policy = build_default_shadow_policy()
        repo.save(policy)
        # 多次查询一致
        for _ in range(3):
            got = repo.get(policy.policy_id, policy.version)
            assert got is not None
            assert got.config_hash == policy.config_hash
