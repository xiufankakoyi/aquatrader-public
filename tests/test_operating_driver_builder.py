"""P1.3 产品与分部经营驱动模型 测试。

按 P1 计划 Task P1.3 验收点设计：
- [ ] 至少三类不同商业模式样本通过：制造、软件/服务、周期品
- [ ] 缺 blocker 输入时输出 gap，不输出精确预测
- [ ] 所有数值驱动可追溯
"""
from __future__ import annotations

from typing import Any

import pytest

from core.research.operating_model.contracts import (
    BUSINESS_MODEL_CYCLICAL,
    BUSINESS_MODEL_MANUFACTURING,
    BUSINESS_MODEL_SOFTWARE_SERVICE,
    BUSINESS_MODELS,
    DRIVER_KINDS,
    ForwardAssumption,
    OperatingDriver,
    OperatingGap,
    OperatingModel,
    Segment,
    validate_business_model,
    validate_driver_kind,
    validate_observation_kind,
)
from core.research.operating_model.driver_builder import (
    DUPLICATE_REVENUE_BASE_THRESHOLD,
    _is_qualitative_only,
    _normalize_metric_to_kind,
    build_operating_model,
    detect_duplicate_revenue_base,
)
from core.research.operating_model.repository import OperatingModelRepository


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _fact(
    product: str, period: str, metric: str, value: float, unit: str = ""
) -> dict[str, Any]:
    """构造 observed_fact dict。"""
    return {
        "product_name": product,
        "period": period,
        "metric": metric,
        "value": value,
        "unit": unit,
        "source_evidence_id": f"ev_{product}_{metric}_{period}",
        "claim_id": f"claim_{product}_{metric}_{period}",
    }


def _disclosed(
    product: str, period: str, metric: str, value: float | None,
    quote: str = "", unit: str = "",
) -> dict[str, Any]:
    """构造 forward_disclosed dict。"""
    return {
        "product_name": product,
        "object_name": product,
        "period": period,
        "metric": metric,
        "value": value,
        "quote": quote,
        "unit": unit,
        "source_evidence_id": f"ev_fwd_{product}_{metric}_{period}",
        "claim_id": f"claim_fwd_{product}_{metric}_{period}",
    }


@pytest.fixture
def manufacturing_facts() -> list[dict[str, Any]]:
    """制造业样本：宁德时代风格 — 产能/利用率/销量/ASP/毛利率。"""
    return [
        _fact("动力电池", "2023-12-31", "有效产能", 100.0, "GWh"),
        _fact("动力电池", "2023-12-31", "利用率", 0.85, "%"),
        _fact("动力电池", "2023-12-31", "销量", 80.0, "GWh"),
        _fact("动力电池", "2023-12-31", "ASP", 0.9, "元/Wh"),
        _fact("动力电池", "2023-12-31", "毛利率", 0.22, "%"),
        _fact("动力电池", "2023-12-31", "产品收入", 72_000_000_000.0, "元"),
        _fact("储能系统", "2023-12-31", "产品收入", 20_000_000_000.0, "元"),
    ]


@pytest.fixture
def software_facts() -> list[dict[str, Any]]:
    """软件/服务样本：用友网络风格 — ARR/订阅/留存率/付费客户。"""
    return [
        _fact("云服务", "2023-12-31", "产品收入", 5_000_000_000.0, "元"),
        _fact("云服务", "2023-12-31", "毛利率", 0.60, "%"),
        _fact("云服务", "2023-12-31", "ARR", 4_000_000_000.0, "元"),
        _fact("传统软件", "2023-12-31", "产品收入", 2_000_000_000.0, "元"),
    ]


@pytest.fixture
def cyclical_facts() -> list[dict[str, Any]]:
    """周期品样本：万华化学风格 — 产能/产量/销量/单位成本/价格传导。"""
    return [
        _fact("MDI", "2023-12-31", "名义产能", 3_000_000.0, "吨"),
        _fact("MDI", "2023-12-31", "产量", 2_700_000.0, "吨"),
        _fact("MDI", "2023-12-31", "销量", 2_650_000.0, "吨"),
        _fact("MDI", "2023-12-31", "ASP", 14_000.0, "元/吨"),
        _fact("MDI", "2023-12-31", "单位成本", 9_000.0, "元/吨"),
        _fact("MDI", "2023-12-31", "产品收入", 37_100_000_000.0, "元"),
    ]


# ---------------------------------------------------------------------------
# 验收点 1：至少三类不同商业模式样本通过
# ---------------------------------------------------------------------------


class TestBusinessModels:
    """验收点：制造、软件/服务、周期品三类样本通过。"""

    def test_manufacturing_model_builds(self, manufacturing_facts):
        """制造业样本应构建成功，包含产能/利用率/销量等驱动。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        assert model.business_model == BUSINESS_MODEL_MANUFACTURING
        assert model.period == "2023-12-31"
        # 应包含至少 2 个分部（动力电池、储能系统）
        segment_names = {s.segment_name for s in model.segments}
        assert "动力电池" in segment_names
        assert "储能系统" in segment_names
        # 动力电池分部应含有效产能/利用率/销量等驱动
        battery_drivers = next(s for s in model.segments if s.segment_name == "动力电池").drivers
        kinds = {d.kind for d in battery_drivers}
        assert {"effective_capacity", "utilization_rate", "sales_volume", "asp",
                "gross_margin", "product_revenue"}.issubset(kinds)

    def test_software_service_model_builds(self, software_facts):
        """软件/服务样本应构建成功。"""
        model = build_operating_model(
            symbol="600588.SH",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_SOFTWARE_SERVICE,
            observed_facts=software_facts,
            company_revenue=7_000_000_000.0,
        )
        assert model.business_model == BUSINESS_MODEL_SOFTWARE_SERVICE
        # 软件公司也应有产品收入与毛利率
        all_kinds = {d.kind for d in model.all_drivers()}
        assert "product_revenue" in all_kinds
        assert "gross_margin" in all_kinds

    def test_cyclical_model_builds(self, cyclical_facts):
        """周期品样本应构建成功，含产能/产量/销量/ASP/单位成本。"""
        model = build_operating_model(
            symbol="600309.SH",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_CYCLICAL,
            observed_facts=cyclical_facts,
            company_revenue=37_100_000_000.0,
        )
        assert model.business_model == BUSINESS_MODEL_CYCLICAL
        kinds = {d.kind for d in model.all_drivers()}
        assert {"nameplate_capacity", "sales_volume", "asp", "unit_cost",
                "product_revenue"}.issubset(kinds)

    def test_invalid_business_model_rejected(self):
        """非法商业模式应抛 ValueError。"""
        with pytest.raises(ValueError, match="非法商业模式"):
            build_operating_model(
                symbol="x", period="2023-12-31",
                business_model="invalid_model",
            )


# ---------------------------------------------------------------------------
# 验收点 2：缺 blocker 输入时输出 gap，不输出精确预测
# ---------------------------------------------------------------------------


class TestGapOutput:
    """验收点：缺 blocker 输入时输出 gap。"""

    def test_forward_disclosed_without_source_ref_becomes_gap(self):
        """forward_disclosed 缺 source_ref 时输出 gap。"""
        disclosed = _disclosed("EV 电池", "2025-12-31", "销量", 100.0, quote="2025 年销量 100 GWh")
        disclosed["source_evidence_id"] = ""  # 清空 source_ref
        disclosed["claim_id"] = ""

        model = build_operating_model(
            symbol="300750.SZ",
            period="2024-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            forward_disclosed=[disclosed],
        )
        # 缺 source_ref → gap，不入 assumptions
        assert len(model.assumptions) == 0
        assert len(model.gaps) == 1
        assert model.gaps[0].blocker_kind == "forward_evidence_missing"

    def test_qualitative_only_text_becomes_gap(self):
        """定性词转数值时输出 gap（如"显著提升"）。"""
        disclosed = _disclosed(
            "EV 电池", "2025-12-31", "销量", 100.0,
            quote="2025 年销量将显著提升",  # 仅定性词，无数字
            unit="GWh",
        )
        # 但 value 是数值 — 引擎应检测到 quote 是定性词，输出 gap
        model = build_operating_model(
            symbol="300750.SZ",
            period="2024-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            forward_disclosed=[disclosed],
        )
        assert len(model.gaps) == 1
        assert model.gaps[0].blocker_kind == "qualitative_only"
        assert len(model.assumptions) == 0

    def test_forward_assumption_without_source_ref_becomes_gap(self):
        """显式 ForwardAssumption 缺 source_ref 时输出 gap。"""
        assumption = ForwardAssumption(
            name="EV 电池销量", kind="sales_volume", value=120.0,
            unit="GWh", period="2025-12-31",
            source_ref="",  # 缺 source_ref
            assumption_text="基于产能爬坡假设",
        )
        model = build_operating_model(
            symbol="300750.SZ",
            period="2024-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            forward_assumptions=[assumption],
        )
        assert len(model.assumptions) == 0
        assert len(model.gaps) == 1
        assert model.gaps[0].reason == "forward_assumption_missing_source_ref"

    def test_no_assumptions_when_no_forward_input(self, manufacturing_facts):
        """无 forward_disclosed 且无 forward_assumptions 时 assumptions 为空。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        # 历史事实不入 assumptions
        assert len(model.assumptions) == 0
        # 也没有 gap（未触发定性词检测）
        # 不强制 gaps 为 0（可能因其他原因产生 gap），但至少不应自动生成 assumption
        for assumption in model.assumptions:
            assert assumption.source_ref, "assumption 必须有 source_ref"


# ---------------------------------------------------------------------------
# 验收点 3：所有数值驱动可追溯
# ---------------------------------------------------------------------------


class TestTraceability:
    """验收点：所有数值驱动可追溯。"""

    def test_historical_drivers_have_source_ref(self, manufacturing_facts):
        """历史驱动必须带 source_ref，grounding_status=grounding_ok。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
        )
        for driver in model.all_drivers():
            if driver.value is not None:
                assert driver.source_ref, f"驱动 {driver.name} 缺 source_ref"
                assert driver.observation_kind == "historical_disclosed"
                assert driver.grounding_status in ("grounding_ok", "grounding_partial", "missing")

    def test_forward_assumptions_have_source_ref(self):
        """前瞻假设必须带 source_ref。"""
        assumption = ForwardAssumption(
            name="EV 电池销量 2025E", kind="sales_volume", value=120.0,
            unit="GWh", period="2025-12-31",
            source_ref="ev_2025_plan_001",
            assumption_text="基于 2024 年产能爬坡 50% 假设",
            basis="disclosed_forward_plan",
        )
        model = build_operating_model(
            symbol="300750.SZ",
            period="2024-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            forward_assumptions=[assumption],
        )
        assert len(model.assumptions) == 1
        assert model.assumptions[0].source_ref == "ev_2025_plan_001"

    def test_metric_to_kind_mapping_coverage(self):
        """metric 名映射应覆盖常见中英文表达。"""
        cases = [
            ("有效产能", "effective_capacity"),
            ("利用率", "utilization_rate"),
            ("销量", "sales_volume"),
            ("ASP", "asp"),
            ("毛利率", "gross_margin"),
            ("产品收入", "product_revenue"),
            ("市场规模", "market_size"),
            ("渗透率", "penetration"),
            ("股本", "share_count"),
            ("稀释因素", "dilution_factor"),
            ("税率", "tax_rate"),
            ("费用率", "expense_ratio"),
        ]
        for metric, expected_kind in cases:
            assert _normalize_metric_to_kind(metric) == expected_kind, (
                f"metric '{metric}' 应映射到 {expected_kind}"
            )

    def test_unmapped_metric_returns_empty(self):
        """无法映射的 metric 返回空串。"""
        assert _normalize_metric_to_kind("未知指标") == ""
        assert _normalize_metric_to_kind("") == ""


# ---------------------------------------------------------------------------
# 规则：历史观察值不得自动成为未来假设
# ---------------------------------------------------------------------------


class TestHistoricalNotForward:
    """验收点：历史事实不入 assumptions。"""

    def test_historical_facts_only_in_drivers(self, manufacturing_facts):
        """历史事实只入 drivers，observation_kind=historical_disclosed。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        for driver in model.all_drivers():
            assert driver.observation_kind == "historical_disclosed"
        # 不应自动生成 assumption
        assert len(model.assumptions) == 0


# ---------------------------------------------------------------------------
# 规则：多产品共享收入基数检测
# ---------------------------------------------------------------------------


class TestDuplicateRevenueDetection:
    """验收点：多产品共享收入基数检测。"""

    def test_duplicate_revenue_warning_when_products_exceed_total(self):
        """多产品收入之和超过公司总收入时产生告警。"""
        facts = [
            _fact("动力电池", "2023-12-31", "产品收入", 80_000_000_000.0, "元"),
            _fact("储能系统", "2023-12-31", "产品收入", 30_000_000_000.0, "元"),
            # 总和 110B > 公司总收入 100B * (1+5%) = 105B
        ]
        model = build_operating_model(
            symbol="x", period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=facts,
            company_revenue=100_000_000_000.0,
        )
        assert len(model.duplicate_revenue_warnings) >= 1
        assert "重复计算" in model.duplicate_revenue_warnings[0]

    def test_no_warning_when_products_within_total(self, manufacturing_facts):
        """产品收入之和在公司总收入合理范围内时无告警。"""
        # 动力电池 72B + 储能 20B = 92B，公司总收入 92B，无超
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        assert model.duplicate_revenue_warnings == []

    def test_detect_duplicate_revenue_base_directly(self):
        """直接调用 detect_duplicate_revenue_base 测试。"""
        drivers = [
            OperatingDriver(name="A", kind="product_revenue", value=60.0,
                            unit="元", period="2023-12-31", segment_name="A"),
            OperatingDriver(name="B", kind="product_revenue", value=50.0,
                            unit="元", period="2023-12-31", segment_name="B"),
        ]
        # 公司总收入 100，产品总和 110 > 105 (100*1.05)
        warnings = detect_duplicate_revenue_base(drivers, company_revenue=100.0)
        assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# 规则：单位、币种和期间不一致必须阻断
# ---------------------------------------------------------------------------


class TestConsistencyCheck:
    """验收点：单位/币种/期间一致性。"""

    def test_consistency_errors_when_unit_mismatch(self):
        """同 kind 同 period 不同 unit 时产生 consistency_error。"""
        facts = [
            _fact("产品 A", "2023-12-31", "ASP", 100.0, "元/吨"),
            # 同 kind=asp 同 period 但 unit 不同
            _fact("产品 B", "2023-12-31", "ASP", 200.0, "元/kg"),
        ]
        model = build_operating_model(
            symbol="x", period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=facts,
        )
        assert len(model.consistency_errors) >= 1
        assert any("unit 不一致" in err for err in model.consistency_errors)

    def test_no_consistency_errors_when_uniform(self, manufacturing_facts):
        """单位一致时无 consistency_errors。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        # manufacturing_facts 中 ASP 单位是元/Wh，毛利率是 %，utilization_rate 是 %
        # 这些 kind 不同，unit 不同但合法；只检测同 kind 同 period 的 unit 不一致
        # 当前 fixture 不应触发 unit 不一致
        assert all("unit 不一致" not in err for err in model.consistency_errors)


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


class TestContractValidation:
    """验收点：契约校验函数。"""

    def test_validate_driver_kind_accepts_valid(self):
        """合法 driver kind 通过。"""
        for kind in DRIVER_KINDS:
            validate_driver_kind(kind)  # 不抛异常即通过

    def test_validate_driver_kind_rejects_invalid(self):
        """非法 driver kind 抛 ValueError。"""
        with pytest.raises(ValueError, match="非法驱动类型"):
            validate_driver_kind("invalid_kind")

    def test_validate_business_model_accepts_valid(self):
        """合法商业模式通过。"""
        for model in BUSINESS_MODELS:
            validate_business_model(model)

    def test_validate_business_model_rejects_invalid(self):
        """非法商业模式抛 ValueError。"""
        with pytest.raises(ValueError, match="非法商业模式"):
            validate_business_model("invalid_model")

    def test_validate_observation_kind(self):
        """观察种类校验。"""
        validate_observation_kind("historical_disclosed")
        validate_observation_kind("forward_assumption")
        with pytest.raises(ValueError, match="非法 observation_kind"):
            validate_observation_kind("invalid")

    def test_is_qualitative_only_helper(self):
        """定性词检测辅助函数。"""
        assert _is_qualitative_only("将显著提升") is True
        assert _is_qualitative_only("销量将大幅增长") is True
        # 含数字 → 非纯定性
        assert _is_qualitative_only("销量将提升 50%") is False
        # 不含定性词
        assert _is_qualitative_only("基于产能爬坡") is False
        # 空文本
        assert _is_qualitative_only("") is False


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


class TestSerialization:
    """验收点：to_dict 序列化完整。"""

    def test_operating_driver_to_dict(self):
        """OperatingDriver.to_dict 包含所有字段。"""
        d = OperatingDriver(
            name="EV 电池销量", kind="sales_volume", value=100.0,
            unit="GWh", currency="", period="2023-12-31",
            source_ref="ev_001", observation_kind="historical_disclosed",
            grounding_status="grounding_ok", reported_value=100.0,
            segment_name="动力电池", note="",
        )
        result = d.to_dict()
        assert result["name"] == "EV 电池销量"
        assert result["kind"] == "sales_volume"
        assert result["value"] == 100.0
        assert result["unit"] == "GWh"
        assert result["source_ref"] == "ev_001"
        assert result["observation_kind"] == "historical_disclosed"
        assert result["segment_name"] == "动力电池"

    def test_operating_model_to_dict(self, manufacturing_facts):
        """OperatingModel.to_dict 包含所有字段。"""
        model = build_operating_model(
            symbol="300750.SZ",
            period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        d = model.to_dict()
        assert d["symbol"] == "300750.SZ"
        assert d["business_model"] == BUSINESS_MODEL_MANUFACTURING
        assert "segments" in d
        assert "drivers" in d
        assert "assumptions" in d
        assert "gaps" in d
        assert "duplicate_revenue_warnings" in d
        assert "consistency_errors" in d
        assert d["schema_version"] == "operating_model_v1"

    def test_segment_to_dict(self):
        """Segment.to_dict 包含 drivers。"""
        seg = Segment(
            segment_name="动力电池",
            products=["电池 A", "电池 B"],
            drivers=[OperatingDriver(name="销量", kind="sales_volume", value=80.0)],
        )
        d = seg.to_dict()
        assert d["segment_name"] == "动力电池"
        assert d["products"] == ["电池 A", "电池 B"]
        assert len(d["drivers"]) == 1

    def test_forward_assumption_to_dict(self):
        """ForwardAssumption.to_dict 包含所有字段。"""
        a = ForwardAssumption(
            name="EV 销量 2025E", kind="sales_volume", value=120.0,
            unit="GWh", currency="", period="2025-12-31",
            source_ref="ev_001", assumption_text="基于产能爬坡",
            basis="disclosed_forward_plan",
        )
        d = a.to_dict()
        assert d["name"] == "EV 销量 2025E"
        assert d["value"] == 120.0
        assert d["basis"] == "disclosed_forward_plan"

    def test_operating_gap_to_dict(self):
        """OperatingGap.to_dict 包含所有字段。"""
        g = OperatingGap(
            name="EV 销量 2026E", kind="sales_volume", period="2026-12-31",
            reason="no_forward_evidence", blocker_kind="forward_evidence_missing",
        )
        d = g.to_dict()
        assert d["blocker_kind"] == "forward_evidence_missing"
        assert d["reason"] == "no_forward_evidence"


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


class TestOperatingModelRepository:
    """验收点：OperatingModelRepository 行为。"""

    def test_save_and_get_latest(self, manufacturing_facts):
        """保存与获取最新版本。"""
        repo = OperatingModelRepository()
        model = build_operating_model(
            symbol="300750.SZ", period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
            company_revenue=92_000_000_000.0,
        )
        repo.save(model, version=1)
        assert repo.count() == 1
        fetched = repo.get_latest("300750.SZ", "2023-12-31")
        assert fetched is not None
        assert fetched.symbol == "300750.SZ"

    def test_duplicate_save_raises(self, manufacturing_facts):
        """同 (symbol, period, version) 重复保存抛错。"""
        repo = OperatingModelRepository()
        model = build_operating_model(
            symbol="x", period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
        )
        repo.save(model, version=1)
        with pytest.raises(ValueError, match="不可覆盖"):
            repo.save(model, version=1)

    def test_invalid_version_raises(self):
        """version <= 0 抛错。"""
        repo = OperatingModelRepository()
        model = OperatingModel(symbol="x", period="2023-12-31")
        with pytest.raises(ValueError, match="version 必须"):
            repo.save(model, version=0)

    def test_boolean_or_non_model_inputs_are_rejected(self):
        repo = OperatingModelRepository()

        with pytest.raises(ValueError):
            repo.save(OperatingModel(symbol="x", period="2023-12-31"), version=True)
        with pytest.raises(TypeError):
            repo.save(object(), version=1)  # type: ignore[arg-type]

    def test_list_periods_and_versions(self, manufacturing_facts):
        """list_periods / list_versions 正常工作。"""
        repo = OperatingModelRepository()
        m1 = build_operating_model(
            symbol="x", period="2023-12-31",
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
        )
        m2 = build_operating_model(
            symbol="x", period="2023-12-31",  # 同期间不同版本
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
        )
        m3 = build_operating_model(
            symbol="x", period="2024-06-30",  # 不同期间
            business_model=BUSINESS_MODEL_MANUFACTURING,
            observed_facts=manufacturing_facts,
        )
        repo.save(m1, version=1)
        repo.save(m2, version=2)
        repo.save(m3, version=1)

        periods = repo.list_periods("x")
        assert "2023-12-31" in periods
        assert "2024-06-30" in periods

        versions = repo.list_versions("x", "2023-12-31")
        assert versions == [1, 2]

    def test_get_latest_returns_none_when_missing(self):
        """无模型时返回 None。"""
        repo = OperatingModelRepository()
        assert repo.get_latest("nonexistent", "2023-12-31") is None
