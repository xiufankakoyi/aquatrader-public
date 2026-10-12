"""P2.6 专项测试：差异化观点对象。

覆盖 3 个验收点：
1. 任一观点可定位市场输入、内部模型和差异变量
2. 缺市场观点或内部模型时不能形成正式 VariantPerception
3. 观点版本不可覆盖

附加覆盖：
- 强制规则 1：market_view 不可由系统臆测
- 强制规则 2：internal_view 必须来自 P1 可审计模型
- 强制规则 3：差异必须落到具体经营驱动
- 强制规则 4：催化只描述验证事件，不输出交易动作
- AGENTS.md 合规（无交易动作建议 / 字段缺失降级 / 不破坏现有功能）
- 仓储持久化 + PIT 过滤 + 状态迁移 + 版本链
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.research.thesis import (
    DriverDifference,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    GENERIC_DIFFERENCE_KEYWORDS,
    InvalidationCondition,
    InternalView,
    MARKET_VIEW_SOURCE_CONSENSUS,
    MARKET_VIEW_SOURCE_GUIDANCE,
    MARKET_VIEW_SOURCE_IMPLIED,
    MARKET_VIEW_SOURCES,
    MarketView,
    REJECT_CATALYST_TRADE_ACTION,
    REJECT_GENERIC_DIFFERENCE,
    REJECT_INVALIDATION_GENERIC,
    REJECT_MISSING_DRIVER_DIFFERENCES,
    REJECT_MISSING_INTERNAL_VIEW,
    REJECT_MISSING_MARKET_VIEW,
    REVIEW_FREQUENCY_QUARTERLY,
    THESIS_STATUS_ACTIVE,
    THESIS_STATUS_DRAFT,
    THESIS_STATUS_INVALIDATED,
    THESIS_STATUS_SUPERSEDED,
    ThesisCatalyst,
    ThesisRepository,
    ValidationSchedule,
    VALUATION_DIRECTION_HIGHER,
    VALUATION_MAGNITUDE_LARGE,
    VALUATION_MAGNITUDE_MEDIUM,
    ValuationImplication,
    VariantPerception,
    VALIDATION_TYPE_EARNINGS_RELEASE,
    VALIDATION_TYPE_POLICY_ANNOUNCEMENT,
    build_variant_perception_builder,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


AS_OF = "2026-07-01T00:00:00+00:00"
SYMBOL = "TEST001"


def make_market_view(
    *,
    source_type: str = MARKET_VIEW_SOURCE_CONSENSUS,
    source_ref: str = "cons_sum_TEST001_2026-07-01",
    as_of: str = AS_OF,
    summary: str = "市场一致预期：net_profit 2027e 中位数 4.5 亿元",
    metric_values: dict[str, Any] | None = None,
) -> MarketView:
    """构造有效的 MarketView。"""
    return MarketView(
        source_type=source_type,
        source_ref=source_ref,
        as_of=as_of,
        summary=summary,
        metric_values=metric_values
        or {"median": 4.5, "source_count": 5, "metric": "net_profit", "period": "2027"},
        retrieved_at="2026-07-02T00:00:00+00:00",
    )


def make_internal_view(
    *,
    model_name: str = "operating_driver_builder",
    model_version: str = "v1.3",
    source_ref: str = "opmodel_TEST001_2026-07-01",
    as_of: str = AS_OF,
    summary: str = "内部模型：基于产能扩张 + 单价提升，net_profit 2027e 5.0 亿元",
    metric_values: dict[str, Any] | None = None,
) -> InternalView:
    """构造有效的 InternalView。"""
    return InternalView(
        model_name=model_name,
        model_version=model_version,
        source_ref=source_ref,
        as_of=as_of,
        summary=summary,
        metric_values=metric_values or {"net_profit_2027e": 5.0},
        computed_at="2026-07-03T00:00:00+00:00",
    )


def make_driver_difference(
    *,
    driver_name: str = "EV电池销量",
    market_value: float | None = 10.0,
    internal_value: float | None = 11.0,
    unit: str = "GWh",
    period: str = "2027",
    reasoning: str = "内部模型基于公司新增产能 5GWh 投产，市场未充分计入",
    evidence_refs: list[str] | None = None,
) -> DriverDifference:
    """构造有效的 DriverDifference。"""
    return DriverDifference(
        driver_name=driver_name,
        market_value=market_value,
        internal_value=internal_value,
        unit=unit,
        period=period,
        reasoning=reasoning,
        evidence_refs=evidence_refs or ["ev-001", "ev-002"],
    )


def make_catalyst(
    *,
    event_description: str = "2027Q1 财报发布，验证 EV 电池销量爬坡",
    expected_date: str = "2027-04-30",
    validation_type: str = VALIDATION_TYPE_EARNINGS_RELEASE,
    evidence_refs: list[str] | None = None,
) -> ThesisCatalyst:
    """构造有效的 ThesisCatalyst。"""
    return ThesisCatalyst(
        event_description=event_description,
        expected_date=expected_date,
        validation_type=validation_type,
        evidence_refs=evidence_refs or ["ev-001"],
    )


def make_invalidation_condition(
    *,
    condition_description: str = "2027Q1 财报披露 EV 电池销量低于 9 GWh",
    threshold: float = 9.0,
    monitor_metric: str = "EV电池销量",
    monitor_source: str = "季报披露",
) -> InvalidationCondition:
    """构造有效的 InvalidationCondition。"""
    return InvalidationCondition(
        condition_description=condition_description,
        threshold=threshold,
        monitor_metric=monitor_metric,
        monitor_source=monitor_source,
    )


def make_valid_thesis() -> VariantPerception:
    """构造一个完整有效的 VariantPerception（status=draft）。"""
    builder = build_variant_perception_builder()
    return builder.build(
        symbol=SYMBOL,
        as_of=AS_OF,
        market_view=make_market_view(),
        internal_view=make_internal_view(),
        difference="内部模型预计 EV 电池销量 11GWh，市场共识 10GWh，差异 1GWh",
        driver_differences=[make_driver_difference()],
        valuation_implication=ValuationImplication(
            direction=VALUATION_DIRECTION_HIGHER,
            magnitude_band=VALUATION_MAGNITUDE_MEDIUM,
            reasoning="销量差异 10% 对应净利差异约 11%，估值应高于市场隐含",
            evidence_refs=["ev-001", "ev-002"],
        ),
        supporting_fact_ids=["fact-001", "fact-002"],
        counter_evidence_ids=["cev-001"],
        catalysts=[make_catalyst()],
        validation_schedule=ValidationSchedule(
            next_review_date="2027-05-15",
            frequency=REVIEW_FREQUENCY_QUARTERLY,
            triggers=["2027Q1 财报发布", "EV 电池月度产销数据"],
        ),
        invalidation_conditions=[make_invalidation_condition()],
        uncertainty=0.3,
        owner="analyst-A",
        reviewer="reviewer-B",
        model_versions={"operating_driver_builder": "v1.3", "valuation_engine": "v2.1"},
    )


@pytest.fixture
def repo(tmp_path: Path) -> ThesisRepository:
    """临时仓储。"""
    return ThesisRepository(tmp_path / "thesis.db")


# ---------------------------------------------------------------------------
# 验收点 1：任一观点可定位市场输入、内部模型和差异变量
# ---------------------------------------------------------------------------


class TestAcceptancePoint1Locatable:
    """验收点 1：任一观点可定位市场输入、内部模型和差异变量。"""

    def test_market_view_has_source_type_and_ref(self):
        """market_view 必须有 source_type + source_ref + as_of，可定位到共识/隐含/指引源。"""
        mv = make_market_view()
        assert mv.is_valid()
        assert mv.source_type in MARKET_VIEW_SOURCES
        assert mv.source_ref  # 非空，可追溯到具体源记录
        assert mv.as_of  # PIT 时点

    def test_internal_view_has_model_name_version_and_ref(self):
        """internal_view 必须有 model_name + model_version + source_ref，可定位到 P1 模型。"""
        iv = make_internal_view()
        assert iv.is_valid()
        assert iv.model_name  # P1 模型名
        assert iv.model_version  # 模型版本
        assert iv.source_ref  # 可追溯到具体 P1 模型实例
        assert iv.as_of

    def test_driver_differences_have_driver_name_market_internal_delta(self):
        """driver_differences 每条必须有 driver_name + market_value/internal_value + delta。"""
        diff = make_driver_difference()
        assert diff.is_valid()
        assert diff.driver_name  # 具体驱动名
        assert diff.market_value is not None or diff.internal_value is not None
        # delta 由 builder 计算，此处直接构造时为 None；用 builder 验证
        builder = build_variant_perception_builder()
        built = builder.build_driver_difference(
            driver_name="EV电池销量",
            market_value=10.0,
            internal_value=11.0,
            unit="GWh",
            period="2027",
            reasoning="测试",
        )
        assert built.delta == pytest.approx(1.0)
        assert built.relative_delta == pytest.approx(0.1)

    def test_thesis_can_locate_all_three_inputs(self):
        """整个 thesis 应能定位市场输入、内部模型和差异变量（核心验收点 1）。"""
        thesis = make_valid_thesis()
        # 市场输入可定位
        assert thesis.market_view.is_valid()
        assert thesis.market_view.source_ref  # 可追溯到 consensus_summary_id
        # 内部模型可定位
        assert thesis.internal_view.is_valid()
        assert thesis.internal_view.source_ref  # 可追溯到 operating_model_id
        assert thesis.internal_view.model_name  # P1 模型名
        # 差异变量可定位
        assert len(thesis.driver_differences) >= 1
        for diff in thesis.driver_differences:
            assert diff.is_valid()
            assert diff.driver_name  # 具体驱动
            assert diff.reasoning  # 差异推理

    def test_market_view_with_invalid_source_type_not_locatable(self):
        """source_type 不在 MARKET_VIEW_SOURCES 中时不可定位。"""
        mv = MarketView(
            source_type="system_inferred",  # 不允许
            source_ref="some-id",
            as_of=AS_OF,
        )
        assert not mv.is_valid()

    def test_internal_view_with_missing_model_version_not_locatable(self):
        """model_version 缺失时不可定位。"""
        iv = InternalView(
            model_name="operating_driver_builder",
            model_version="",  # 缺失
            source_ref="some-id",
            as_of=AS_OF,
        )
        assert not iv.is_valid()

    def test_thesis_id_deterministic(self):
        """相同业务键 + version 产生相同 thesis_id（确定性）。"""
        thesis1 = make_valid_thesis()
        thesis2 = make_valid_thesis()
        # 相同输入（symbol/as_of/market_source_ref/internal_source_ref/version）
        assert thesis1.thesis_id == thesis2.thesis_id
        assert thesis1.thesis_id.startswith(f"vp_{SYMBOL}_{AS_OF}_")

    def test_thesis_id_changes_with_version(self):
        """不同 version 产生不同 thesis_id。"""
        thesis_v1 = make_valid_thesis()
        thesis_v2 = make_valid_thesis()
        thesis_v2.version = 2
        thesis_v2.thesis_id = ""  # 清空触发重新派生
        thesis_v2.__post_init__()
        assert thesis_v1.thesis_id != thesis_v2.thesis_id


# ---------------------------------------------------------------------------
# 验收点 2：缺市场观点或内部模型时不能形成正式 VariantPerception
# ---------------------------------------------------------------------------


class TestAcceptancePoint2NoMissingInputs:
    """验收点 2：缺市场观点或内部模型时不能形成正式 VariantPerception。"""

    def test_missing_market_view_rejects_active(self):
        """缺 market_view 时 promote_to_active 失败 + 填充 rejection_reason。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=MarketView(),  # 空 market_view
            internal_view=make_internal_view(),
            difference="差异描述",
            driver_differences=[make_driver_difference()],
        )
        assert thesis.status == THESIS_STATUS_DRAFT
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.status == THESIS_STATUS_DRAFT  # 保持 draft
        assert thesis.rejection_reason == REJECT_MISSING_MARKET_VIEW
        assert "market_view" in thesis.rejection_details

    def test_missing_internal_view_rejects_active(self):
        """缺 internal_view 时 promote_to_active 失败。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=InternalView(),  # 空 internal_view
            difference="差异描述",
            driver_differences=[make_driver_difference()],
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_MISSING_INTERNAL_VIEW
        assert "internal_view" in thesis.rejection_details

    def test_missing_driver_differences_rejects_active(self):
        """缺 driver_differences 时 promote_to_active 失败（差异必须落到具体驱动）。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异描述",
            driver_differences=[],  # 空
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_MISSING_DRIVER_DIFFERENCES

    def test_generic_difference_rejects_active(self):
        """差异总结仅含「我们更乐观」类定性描述时拒绝。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="我们更乐观",  # 仅含定性描述
            driver_differences=[make_driver_difference()],
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_GENERIC_DIFFERENCE

    def test_driver_difference_with_empty_driver_name_rejects(self):
        """driver_difference.driver_name 为空时拒绝。"""
        builder = build_variant_perception_builder()
        bad_diff = DriverDifference(
            driver_name="",  # 空
            market_value=10.0,
            internal_value=11.0,
            reasoning="测试",
        )
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异",
            driver_differences=[bad_diff],
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_GENERIC_DIFFERENCE

    def test_catalyst_with_trade_action_rejects_active(self):
        """催化剂含交易动作词时拒绝（强制规则 4 + AGENTS.md）。"""
        builder = build_variant_perception_builder()
        bad_catalyst = make_catalyst(
            event_description="2027Q1 财报发布后建议买入",  # 含「买入」
        )
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异",
            driver_differences=[make_driver_difference()],
            catalysts=[bad_catalyst],
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_CATALYST_TRADE_ACTION

    def test_invalidation_too_generic_rejects_active(self):
        """证伪条件含「业绩不及预期」类空泛描述时拒绝。"""
        builder = build_variant_perception_builder()
        bad_cond = make_invalidation_condition(
            condition_description="业绩不及预期",  # 空泛
        )
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异",
            driver_differences=[make_driver_difference()],
            invalidation_conditions=[bad_cond],
        )
        success = builder.promote_to_active(thesis)
        assert not success
        assert thesis.rejection_reason == REJECT_INVALIDATION_GENERIC

    def test_valid_thesis_promotes_to_active(self):
        """完整有效 thesis 可提升为 active。"""
        builder = build_variant_perception_builder()
        thesis = make_valid_thesis()
        success = builder.promote_to_active(thesis)
        assert success
        assert thesis.status == THESIS_STATUS_ACTIVE
        assert thesis.rejection_reason == ""
        assert thesis.rejection_details == ""

    def test_draft_status_allowed_with_missing_views(self):
        """draft 状态允许缺 market_view/internal_view（仅不能提升为 active）。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=MarketView(),  # 空
            internal_view=InternalView(),  # 空
            difference="",
            driver_differences=[],
        )
        # 构建完成时为 draft，不抛异常
        assert thesis.status == THESIS_STATUS_DRAFT
        assert thesis.thesis_id  # 仍有 thesis_id


# ---------------------------------------------------------------------------
# 验收点 3：观点版本不可覆盖
# ---------------------------------------------------------------------------


class TestAcceptancePoint3VersionNotOverwritable:
    """验收点 3：观点版本不可覆盖。"""

    def test_save_existing_thesis_id_raises(self, repo: ThesisRepository):
        """同 thesis_id 重复 save 抛 ValueError。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        with pytest.raises(ValueError, match="already exists"):
            repo.save(thesis)

    def test_revision_of_links_versions(self, repo: ThesisRepository):
        """新版本 revision_of 指向旧版本 thesis_id，构成版本链。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        repo.save(thesis_v1)

        # 创建新版本
        thesis_v2 = builder.create_revision(
            old_thesis=thesis_v1,
            new_difference="更新后的差异描述",
            new_driver_differences=[
                make_driver_difference(internal_value=11.5),
            ],
        )
        assert thesis_v2.version == 2
        assert thesis_v2.revision_of == thesis_v1.thesis_id
        assert thesis_v2.thesis_id != thesis_v1.thesis_id

        repo.save(thesis_v2)
        # 两个版本共存
        assert repo.get(thesis_v1.thesis_id) is not None
        assert repo.get(thesis_v2.thesis_id) is not None

    def test_supersede_marks_old_as_superseded(self, repo: ThesisRepository):
        """supersede 仅修改旧版本 status=superseded，不删除数据。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        repo.save(thesis_v1)

        thesis_v2 = builder.create_revision(
            old_thesis=thesis_v1,
            new_difference="v2 差异",
        )
        repo.save(thesis_v2)

        # 标记旧版本为 superseded
        success = repo.supersede(
            old_thesis_id=thesis_v1.thesis_id,
            new_thesis_id=thesis_v2.thesis_id,
        )
        assert success

        # 旧版本数据仍在，仅 status 变为 superseded
        v1_payload = repo.get(thesis_v1.thesis_id)
        assert v1_payload is not None
        assert v1_payload["status"] == THESIS_STATUS_SUPERSEDED
        assert v1_payload["thesis_id"] == thesis_v1.thesis_id  # thesis_id 不变
        # 其他字段保持不变
        assert v1_payload["symbol"] == thesis_v1.symbol
        assert v1_payload["as_of"] == thesis_v1.as_of

        # 新版本不受影响
        v2_payload = repo.get(thesis_v2.thesis_id)
        assert v2_payload["status"] == THESIS_STATUS_DRAFT  # 新版本保持 draft

    def test_supersede_nonexistent_returns_false(self, repo: ThesisRepository):
        """supersede 不存在的 thesis_id 返回 False。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        # 旧版本不存在
        assert not repo.supersede(old_thesis_id="nonexistent", new_thesis_id=thesis.thesis_id)
        # 新版本不存在
        assert not repo.supersede(old_thesis_id=thesis.thesis_id, new_thesis_id="nonexistent")

    def test_list_latest_excludes_superseded(self, repo: ThesisRepository):
        """list_latest_by_symbol 排除 superseded 状态。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        repo.save(thesis_v1)
        thesis_v2 = builder.create_revision(old_thesis=thesis_v1, new_difference="v2")
        repo.save(thesis_v2)
        repo.supersede(old_thesis_id=thesis_v1.thesis_id, new_thesis_id=thesis_v2.thesis_id)

        latest = repo.list_latest_by_symbol(symbol=SYMBOL)
        assert len(latest) == 1
        assert latest[0]["thesis_id"] == thesis_v2.thesis_id
        # list_by_symbol 不过滤 superseded
        all_versions = repo.list_by_symbol(symbol=SYMBOL)
        assert len(all_versions) == 2

    def test_old_version_data_unchanged_after_new_version_saved(self, repo: ThesisRepository):
        """新版本保存后旧版本数据完全不变。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        v1_difference_original = thesis_v1.difference
        repo.save(thesis_v1)

        thesis_v2 = builder.create_revision(
            old_thesis=thesis_v1,
            new_difference="完全不同的差异描述",
        )
        repo.save(thesis_v2)

        # 重新读取 v1，确认数据未变
        v1_payload = repo.get(thesis_v1.thesis_id)
        assert v1_payload["difference"] == v1_difference_original
        assert v1_payload["difference"] != "完全不同的差异描述"

    def test_list_versions_traverses_chain(self, repo: ThesisRepository):
        """list_versions 沿 revision_of 链回溯所有版本。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        repo.save(thesis_v1)
        thesis_v2 = builder.create_revision(old_thesis=thesis_v1, new_difference="v2")
        repo.save(thesis_v2)
        thesis_v3 = builder.create_revision(old_thesis=thesis_v2, new_difference="v3")
        repo.save(thesis_v3)

        # 从 v2 查询，应返回 v1, v2, v3 三个版本
        versions = repo.list_versions(thesis_v2.thesis_id)
        assert len(versions) == 3
        version_numbers = sorted(v["version"] for v in versions)
        assert version_numbers == [1, 2, 3]


# ---------------------------------------------------------------------------
# 强制规则覆盖
# ---------------------------------------------------------------------------


class TestMandatoryRules:
    """4 条强制规则覆盖。"""

    def test_rule1_market_view_must_be_from_consensus_implied_or_guidance(self):
        """规则 1：market_view.source_type 必须是 consensus/implied/company_guidance。"""
        # 三种合法 source_type
        for st in MARKET_VIEW_SOURCES:
            mv = MarketView(source_type=st, source_ref="ref", as_of=AS_OF)
            assert mv.is_valid(), f"source_type={st} 应合法"
        # 非法 source_type
        for st in ("system_inferred", "user_input", "analyst_forecast", ""):
            mv = MarketView(source_type=st, source_ref="ref", as_of=AS_OF)
            assert not mv.is_valid(), f"source_type={st} 应非法"

    def test_rule2_internal_view_must_be_from_p1_model(self):
        """规则 2：internal_view 必须有 model_name + model_version + source_ref。"""
        iv = make_internal_view()
        assert iv.is_valid()
        assert iv.model_name  # P1 模型名
        assert iv.model_version  # 模型版本

    def test_rule3_driver_difference_must_be_specific(self):
        """规则 3：差异必须落到具体经营驱动，不能仅含「我们更乐观」。"""
        # 具体驱动有效
        diff = make_driver_difference(driver_name="EV电池销量")
        assert diff.is_valid()
        # 仅含定性关键词无效
        for kw in GENERIC_DIFFERENCE_KEYWORDS:
            builder = build_variant_perception_builder()
            thesis = builder.build(
                symbol=SYMBOL,
                as_of=AS_OF,
                market_view=make_market_view(),
                internal_view=make_internal_view(),
                difference=kw,
                driver_differences=[make_driver_difference()],
            )
            success = builder.promote_to_active(thesis)
            assert not success
            assert thesis.rejection_reason == REJECT_GENERIC_DIFFERENCE

    def test_rule4_catalyst_only_describes_validation_events(self):
        """规则 4：催化仅描述验证事件，不含交易动作词。"""
        # 合法催化剂
        cat_valid = make_catalyst(event_description="2027Q1 财报发布")
        assert cat_valid.is_valid()
        assert not cat_valid.contains_trade_action()
        # 含交易动作词的催化剂
        for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS:
            cat_bad = ThesisCatalyst(
                event_description=f"事件后建议{kw}",
                expected_date="2027-04-30",
                validation_type=VALIDATION_TYPE_EARNINGS_RELEASE,
            )
            assert cat_bad.contains_trade_action()
            assert not cat_bad.is_valid()


# ---------------------------------------------------------------------------
# 仓储持久化与查询
# ---------------------------------------------------------------------------


class TestRepositoryPersistence:
    """仓储持久化与查询。"""

    def test_save_and_get(self, repo: ThesisRepository):
        """save + get 往返。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        payload = repo.get(thesis.thesis_id)
        assert payload is not None
        assert payload["thesis_id"] == thesis.thesis_id
        assert payload["symbol"] == SYMBOL
        assert payload["as_of"] == AS_OF
        assert payload["market_view"]["source_type"] == MARKET_VIEW_SOURCE_CONSENSUS
        assert payload["internal_view"]["model_name"] == "operating_driver_builder"

    def test_get_nonexistent_returns_none(self, repo: ThesisRepository):
        """get 不存在的 thesis_id 返回 None。"""
        assert repo.get("nonexistent") is None

    def test_list_by_symbol_pit_filter(self, repo: ThesisRepository):
        """list_by_symbol 支持 PIT 过滤。"""
        builder = build_variant_perception_builder()
        # 旧 thesis（as_of=2026-07-01）
        thesis_old = make_valid_thesis()
        repo.save(thesis_old)
        # 新 thesis（as_of=2026-09-01）
        thesis_new = builder.build(
            symbol=SYMBOL,
            as_of="2026-09-01T00:00:00+00:00",
            market_view=make_market_view(source_ref="ref-new"),
            internal_view=make_internal_view(source_ref="ref-new-internal"),
            difference="新差异",
            driver_differences=[make_driver_difference()],
        )
        repo.save(thesis_new)

        # PIT 过滤：as_of=2026-08-01 只能看到 thesis_old
        rows = repo.list_by_symbol(symbol=SYMBOL, as_of="2026-08-01T00:00:00+00:00")
        assert len(rows) == 1
        assert rows[0]["thesis_id"] == thesis_old.thesis_id

        # 不带 PIT 过滤：看到所有
        rows_all = repo.list_by_symbol(symbol=SYMBOL)
        assert len(rows_all) == 2

    def test_list_by_symbol_status_filter(self, repo: ThesisRepository):
        """list_by_symbol 支持 status 过滤。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        repo.update_status(thesis.thesis_id, THESIS_STATUS_INVALIDATED)

        rows_invalidated = repo.list_by_symbol(symbol=SYMBOL, status=THESIS_STATUS_INVALIDATED)
        assert len(rows_invalidated) == 1
        assert rows_invalidated[0]["status"] == THESIS_STATUS_INVALIDATED

        rows_draft = repo.list_by_symbol(symbol=SYMBOL, status=THESIS_STATUS_DRAFT)
        assert len(rows_draft) == 0

    def test_list_by_empty_symbol_returns_empty(self, repo: ThesisRepository):
        """symbol 为空时返回空列表。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        assert repo.list_by_symbol(symbol="") == []
        assert repo.list_latest_by_symbol(symbol="") == []

    def test_malformed_payload_is_skipped(self, repo: ThesisRepository):
        thesis = make_valid_thesis()
        repo.save(thesis)
        with repo.connect() as conn:
            conn.execute(
                "UPDATE variant_perceptions SET payload_json=? WHERE thesis_id=?",
                ("not-json", thesis.thesis_id),
            )

        assert repo.get(thesis.thesis_id) is None
        assert repo.list_by_symbol(symbol=SYMBOL) == []
        assert not repo.update_status(thesis.thesis_id, THESIS_STATUS_INVALIDATED)

    def test_invalid_limit_falls_back_to_safe_page_size(self, repo: ThesisRepository):
        thesis = make_valid_thesis()
        repo.save(thesis)
        assert repo.list_by_symbol(symbol=SYMBOL, limit="invalid") == [thesis.to_dict()]

    def test_update_status(self, repo: ThesisRepository):
        """update_status 修改状态。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        assert repo.update_status(thesis.thesis_id, THESIS_STATUS_INVALIDATED)
        payload = repo.get(thesis.thesis_id)
        assert payload["status"] == THESIS_STATUS_INVALIDATED

    def test_update_status_invalid_returns_false(self, repo: ThesisRepository):
        """update_status 非法状态返回 False。"""
        thesis = make_valid_thesis()
        repo.save(thesis)
        assert not repo.update_status(thesis.thesis_id, "invalid_status")

    def test_update_status_nonexistent_returns_false(self, repo: ThesisRepository):
        """update_status 不存在 thesis_id 返回 False。"""
        assert not repo.update_status("nonexistent", THESIS_STATUS_INVALIDATED)

    def test_schema_version(self, repo: ThesisRepository):
        """schema_version 校验。"""
        stats = repo.stats()
        assert stats["schema_version"] == 1
        assert "counts" in stats
        assert stats["counts"]["total"] == 0

    def test_stats_counts_by_status(self, repo: ThesisRepository):
        """stats 按 status 计数。"""
        builder = build_variant_perception_builder()
        t1 = make_valid_thesis()
        repo.save(t1)
        t2 = builder.build(
            symbol=SYMBOL,
            as_of="2026-08-01T00:00:00+00:00",
            market_view=make_market_view(source_ref="ref-2"),
            internal_view=make_internal_view(source_ref="ref-2-internal"),
            difference="差异2",
            driver_differences=[make_driver_difference()],
        )
        repo.save(t2)
        repo.update_status(t1.thesis_id, THESIS_STATUS_INVALIDATED)

        stats = repo.stats()
        assert stats["counts"]["total"] == 2
        assert stats["counts"][THESIS_STATUS_INVALIDATED] == 1
        assert stats["counts"][THESIS_STATUS_DRAFT] == 1


# ---------------------------------------------------------------------------
# 集成：build → promote → save → list → supersede → list_latest
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端集成测试。"""

    def test_build_promote_save_supersede_list(self, repo: ThesisRepository):
        """完整流程：build → promote → save → 创建新版本 → supersede → list_latest。"""
        builder = build_variant_perception_builder()

        # 1. 构建并提升 v1
        thesis_v1 = make_valid_thesis()
        assert builder.promote_to_active(thesis_v1)
        assert thesis_v1.status == THESIS_STATUS_ACTIVE
        repo.save(thesis_v1)

        # 2. 创建新版本 v2
        thesis_v2 = builder.create_revision(
            old_thesis=thesis_v1,
            new_difference="v2 差异",
            new_driver_differences=[
                make_driver_difference(internal_value=11.5),
            ],
        )
        assert thesis_v2.version == 2
        assert thesis_v2.revision_of == thesis_v1.thesis_id
        # v2 提升为 active
        assert builder.promote_to_active(thesis_v2)
        repo.save(thesis_v2)

        # 3. supersede v1
        assert repo.supersede(
            old_thesis_id=thesis_v1.thesis_id,
            new_thesis_id=thesis_v2.thesis_id,
        )

        # 4. list_latest 只返回 v2（排除 superseded）
        latest = repo.list_latest_by_symbol(symbol=SYMBOL)
        assert len(latest) == 1
        assert latest[0]["thesis_id"] == thesis_v2.thesis_id
        assert latest[0]["status"] == THESIS_STATUS_ACTIVE

        # 5. list_versions 返回 v1 + v2
        all_versions = repo.list_versions(thesis_v1.thesis_id)
        assert len(all_versions) == 2
        version_numbers = sorted(v["version"] for v in all_versions)
        assert version_numbers == [1, 2]

    def test_to_dict_roundtrip(self):
        """to_dict 序列化往返保留所有字段。"""
        thesis = make_valid_thesis()
        payload = thesis.to_dict()
        # 关键字段都存在
        required_fields = [
            "thesis_id", "symbol", "as_of",
            "market_view", "internal_view", "difference",
            "driver_differences", "valuation_implication",
            "supporting_fact_ids", "counter_evidence_ids",
            "catalysts", "validation_schedule",
            "invalidation_conditions", "uncertainty",
            "owner", "reviewer", "model_versions", "status",
            "version", "revision_of", "created_at",
            "rejection_reason", "rejection_details",
        ]
        for field_name in required_fields:
            assert field_name in payload, f"missing field: {field_name}"
        # 嵌套对象也序列化
        assert payload["market_view"]["source_type"] == MARKET_VIEW_SOURCE_CONSENSUS
        assert payload["internal_view"]["model_name"] == "operating_driver_builder"
        assert len(payload["driver_differences"]) == 1
        assert payload["driver_differences"][0]["driver_name"] == "EV电池销量"
        assert payload["valuation_implication"]["direction"] == VALUATION_DIRECTION_HIGHER


# ---------------------------------------------------------------------------
# AGENTS.md 合规
# ---------------------------------------------------------------------------


class TestAgentsMdCompliance:
    """AGENTS.md 合规检查。"""

    def test_no_trade_advice_in_source(self):
        """源码不出现交易动作关键词。"""
        import core.research.thesis.contracts as contracts_mod

        # 扫描 contracts.py / variant_perception.py / repository.py 三个源码文件
        forbidden = ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓")
        source_files = [
            Path(contracts_mod.__file__).parent / "contracts.py",
            Path(contracts_mod.__file__).parent / "variant_perception.py",
            Path(contracts_mod.__file__).parent / "repository.py",
        ]
        for source_file in source_files:
            source_text = source_file.read_text(encoding="utf-8")
            # FORBIDDEN_TRADE_ACTION_KEYWORDS 定义本身允许出现（作为禁词列表）
            lines = source_text.splitlines()
            in_forbidden_def = False
            cleaned_lines = []
            for line in lines:
                if "FORBIDDEN_TRADE_ACTION_KEYWORDS" in line and "(" in line:
                    in_forbidden_def = True
                    continue
                if in_forbidden_def:
                    if ")" in line:
                        in_forbidden_def = False
                    continue
                cleaned_lines.append(line)
            cleaned_text = "\n".join(cleaned_lines)
            for kw in forbidden:
                assert kw not in cleaned_text, (
                    f"源码 {source_file.name} 不应直接出现「{kw}」"
                    f"（仅可在 FORBIDDEN_TRADE_ACTION_KEYWORDS 中）"
                )

    def test_no_trade_advice_in_payload(self):
        """payload（to_dict 输出）不出现交易动作关键词。"""
        thesis = make_valid_thesis()
        # 添加含禁词的催化剂（应被 promote 拒绝）
        bad_catalyst = ThesisCatalyst(
            event_description="建议买入",
            expected_date="2027-04-30",
        )
        thesis.catalysts = [bad_catalyst]
        builder = build_variant_perception_builder()
        success = builder.promote_to_active(thesis)
        assert not success
        # payload 中 catalysts 字段虽然保留（用于审计），但 thesis 状态保持 draft
        payload = thesis.to_dict()
        assert payload["status"] == THESIS_STATUS_DRAFT
        assert payload["rejection_reason"] == REJECT_CATALYST_TRADE_ACTION

    def test_field_missing_graceful(self):
        """字段缺失降级处理，不抛异常。"""
        # 空 market_view 不抛异常
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol="",
            as_of="",
            market_view=MarketView(),
            internal_view=InternalView(),
            difference="",
            driver_differences=[],
        )
        assert thesis.status == THESIS_STATUS_DRAFT
        # 不抛异常的 to_dict
        payload = thesis.to_dict()
        assert payload["symbol"] == ""
        assert payload["market_view"]["source_type"] == ""

    def test_malformed_nested_fields_degrade_without_crashing(self):
        thesis = VariantPerception(
            symbol="TEST001",
            market_view=MarketView(metric_values=None),  # type: ignore[arg-type]
            internal_view=InternalView(metric_values=None),  # type: ignore[arg-type]
            driver_differences=None,  # type: ignore[arg-type]
            valuation_implication=None,  # type: ignore[arg-type]
            catalysts=None,  # type: ignore[arg-type]
            invalidation_conditions=None,  # type: ignore[arg-type]
            uncertainty="not-a-number",  # type: ignore[arg-type]
        )

        payload = thesis.to_dict()

        assert payload["market_view"]["metric_values"] == {}
        assert payload["internal_view"]["metric_values"] == {}
        assert payload["catalysts"] == []
        assert payload["uncertainty"] == 0.0
        assert thesis.can_promote_to_active()[0] is False

    def test_invalid_validation_frequency_rejects_promotion(self):
        thesis = make_valid_thesis()
        thesis.validation_schedule.frequency = "weekly"

        assert thesis.validation_schedule.is_valid() is False

    def test_uncertainty_clamped(self):
        """uncertainty 被限制在 [0, 1]。"""
        builder = build_variant_perception_builder()
        thesis = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异",
            driver_differences=[make_driver_difference()],
            uncertainty=1.5,  # 超出上限
        )
        assert thesis.uncertainty == 1.0
        thesis_neg = builder.build(
            symbol=SYMBOL,
            as_of=AS_OF,
            market_view=make_market_view(),
            internal_view=make_internal_view(),
            difference="差异",
            driver_differences=[make_driver_difference()],
            uncertainty=-0.5,
        )
        assert thesis_neg.uncertainty == 0.0


# ---------------------------------------------------------------------------
# Builder 适配器：从 P1.7 / P2.5 源构造 MarketView
# ---------------------------------------------------------------------------


class TestBuilderAdapters:
    """Builder 适配器测试：从 P1.7 / P2.5 源对象构造 MarketView。"""

    def test_build_market_view_from_consensus_dict(self):
        """从 ConsensusSummary 字典构造 market_view。"""
        builder = build_variant_perception_builder()
        consensus_summary = {
            "consensus_id": "cons_TEST001_2026-07-01",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "metric": "net_profit",
            "period": "2027",
            "median": 4.5,
            "source_count": 5,
            "status": "ok",
            "created_at": "2026-07-02T00:00:00+00:00",
        }
        mv = builder.build_market_view_from_consensus(consensus_summary=consensus_summary)
        assert mv.source_type == MARKET_VIEW_SOURCE_CONSENSUS
        assert mv.source_ref == "cons_TEST001_2026-07-01"
        assert mv.as_of == AS_OF
        assert mv.is_valid()
        assert mv.metric_values["median"] == 4.5
        assert mv.metric_values["source_count"] == 5

    def test_build_market_view_from_implied_dict(self):
        """从 ImpliedExpectationSummary 字典构造 market_view。"""
        builder = build_variant_perception_builder()
        implied_summary = {
            "summary_id": "impl_TEST001_2026-07-01",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "metrics": [
                {"metric_kind": "implied_growth", "value": 0.08},
                {"metric_kind": "implied_margin", "value": 0.15},
            ],
            "status": "ok",
            "created_at": "2026-07-02T00:00:00+00:00",
        }
        mv = builder.build_market_view_from_implied(implied_summary=implied_summary)
        assert mv.source_type == MARKET_VIEW_SOURCE_IMPLIED
        assert mv.source_ref == "impl_TEST001_2026-07-01"
        assert mv.is_valid()
        assert mv.metric_values["implied_growth"] == 0.08
        assert mv.metric_values["implied_margin"] == 0.15

    def test_build_market_view_from_guidance_dict(self):
        """从 CompanyGuidance 字典构造 market_view。"""
        builder = build_variant_perception_builder()
        guidance = {
            "guidance_id": "guid_TEST001_2026-07-01",
            "company_symbol": SYMBOL,
            "as_of": AS_OF,
            "values": [
                {"metric": "net_profit", "low": 4.3, "high": 4.7, "period": "2027"},
            ],
            "retrieved_at": "2026-07-02T00:00:00+00:00",
        }
        mv = builder.build_market_view_from_guidance(guidance=guidance)
        assert mv.source_type == MARKET_VIEW_SOURCE_GUIDANCE
        assert mv.source_ref == "guid_TEST001_2026-07-01"
        assert mv.is_valid()
        assert mv.metric_values["net_profit"]["low"] == 4.3
        assert mv.metric_values["net_profit"]["high"] == 4.7

    def test_build_market_view_from_none_returns_empty(self):
        """传入 None 返回空 MarketView（字段缺失降级）。"""
        builder = build_variant_perception_builder()
        mv = builder.build_market_view_from_consensus(consensus_summary=None)
        assert not mv.is_valid()
        mv = builder.build_market_view_from_implied(implied_summary=None)
        assert not mv.is_valid()
        mv = builder.build_market_view_from_guidance(guidance=None)
        assert not mv.is_valid()

    def test_build_internal_view_from_p1(self):
        """从 P1 模型构造 internal_view。"""
        builder = build_variant_perception_builder()
        iv = builder.build_internal_view_from_p1(
            model_name="valuation_engine",
            model_version="v2.1",
            source_ref="valcase_TEST001_2026-07-01",
            as_of=AS_OF,
            summary="内部 DCF 估值",
            metric_values={"fair_value": 25.5},
        )
        assert iv.is_valid()
        assert iv.model_name == "valuation_engine"
        assert iv.model_version == "v2.1"
        assert iv.source_ref == "valcase_TEST001_2026-07-01"
        assert iv.metric_values["fair_value"] == 25.5

    def test_build_driver_difference_auto_compute_delta(self):
        """build_driver_difference 自动计算 delta / relative_delta。"""
        builder = build_variant_perception_builder()
        diff = builder.build_driver_difference(
            driver_name="EV电池销量",
            market_value=10.0,
            internal_value=11.0,
            unit="GWh",
            period="2027",
            reasoning="测试",
        )
        assert diff.delta == pytest.approx(1.0)
        assert diff.relative_delta == pytest.approx(0.1)

    def test_build_driver_difference_market_missing(self):
        """market_value=None 时 delta 仅表示 internal_value（市场未覆盖）。"""
        builder = build_variant_perception_builder()
        diff = builder.build_driver_difference(
            driver_name="新驱动",
            market_value=None,
            internal_value=5.0,
            unit="GWh",
            period="2027",
            reasoning="市场未覆盖",
        )
        assert diff.delta == 5.0  # 仅 internal_value
        assert diff.relative_delta is None  # 无法计算相对差

    def test_create_revision_preserves_evidence(self):
        """create_revision 沿用旧版本 supporting_fact_ids / counter_evidence_ids。"""
        builder = build_variant_perception_builder()
        thesis_v1 = make_valid_thesis()
        thesis_v2 = builder.create_revision(
            old_thesis=thesis_v1,
            new_difference="v2 差异",
        )
        assert thesis_v2.supporting_fact_ids == thesis_v1.supporting_fact_ids
        assert thesis_v2.counter_evidence_ids == thesis_v1.counter_evidence_ids
        assert thesis_v2.owner == thesis_v1.owner
        assert thesis_v2.reviewer == thesis_v1.reviewer
