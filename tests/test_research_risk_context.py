"""P2.8 专项测试：风险与组合上下文。

覆盖：
- 6 个风险维度完整性
- 综合风险等级聚合（任一 extreme → overall extreme）
- 集中度 HHI 计算
- 情景压力测试（3 个标准情景 + 自定义情景）
- 组合快照只读适配器
- AGENTS.md 合规（无交易动作建议 / 字段缺失降级）

边界：只输出风险暴露和压力结果，不输出仓位或交易动作。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.research.risk import (
    DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
    DIMENSION_FACTOR_CORRELATION,
    DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH,
    DIMENSION_LIQUIDITY_SUSPENSION_CAPACITY,
    DIMENSION_MACRO_FX_RATE_COMMODITY,
    DIMENSION_SCENARIO_STRESS,
    DRIVER_DIRECTION_DOWNSIDE,
    DRIVER_DIRECTION_NEUTRAL,
    DRIVER_DIRECTION_UPSIDE,
    DRIVER_MAGNITUDE_LARGE,
    DRIVER_MAGNITUDE_MEDIUM,
    DRIVER_MAGNITUDE_SMALL,
    ERR_INVALID_LEVEL,
    ERR_MISSING_DIMENSION,
    ERR_TRADE_ACTION_KEYWORD,
    FORBIDDEN_TRADE_ACTION_KEYWORDS,
    Holding,
    NOTICE,
    PortfolioSnapshot,
    RISK_DIMENSIONS,
    RISK_DIMENSION_LABELS,
    RISK_LEVEL_EXTREME,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RISK_LEVEL_SCORES,
    ResearchRiskContext,
    ResearchRiskEngine,
    RiskDimension,
    RiskDriver,
    SCENARIO_BASE_CASE,
    SCENARIO_BEAR_CASE,
    SCENARIO_BULL_CASE,
    SCENARIO_CUSTOM,
    SCENARIO_TYPES,
    ScenarioStressEngine,
    ScenarioStressTest,
    STANDARD_SCENARIO_TEMPLATES,
    aggregate_overall_risk_level,
    build_research_risk_engine,
    build_scenario_stress_engine,
    compute_concentration_hhi,
    compute_industry_exposure,
)
from core.quant.portfolio.portfolio_snapshot_adapter import (
    PortfolioSnapshotAdapter,
    build_portfolio_snapshot_adapter,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


AS_OF = "2026-07-01T00:00:00+00:00"
SYMBOL = "TEST001"


def make_driver(
    *,
    driver_id: str = "drv_1",
    name: str = "风险驱动 1",
    direction: str = DRIVER_DIRECTION_DOWNSIDE,
    magnitude: str = DRIVER_MAGNITUDE_MEDIUM,
) -> RiskDriver:
    """构造有效的 RiskDriver。"""
    return RiskDriver(
        driver_id=driver_id,
        name=name,
        description=f"{name} 的描述",
        direction=direction,
        magnitude=magnitude,
        evidence_refs=[f"evidence_{driver_id}"],
        source_refs=[f"source_{driver_id}"],
    )


def make_dimension(
    *,
    dimension: str,
    level: str = RISK_LEVEL_MEDIUM,
    drivers: list[RiskDriver] | None = None,
    note: str = "",
) -> RiskDimension:
    """构造有效的 RiskDimension。"""
    return RiskDimension(
        dimension=dimension,
        level=level,
        score=RISK_LEVEL_SCORES[level],
        drivers=drivers or [make_driver(driver_id=f"drv_{dimension}")],
        evidence_refs=[f"evidence_{dimension}"],
        mitigants=[f"缓释因素_{dimension}"],
        source_refs=[f"source_{dimension}"],
        as_of=AS_OF,
        note=note,
    )


def make_six_dimensions(*, extreme_dim: str | None = None) -> list[RiskDimension]:
    """构造 6 个完整的风险维度。

    Args:
        extreme_dim: 若指定，该维度等级为 extreme
    """
    result: list[RiskDimension] = []
    for dim in RISK_DIMENSIONS:
        level = RISK_LEVEL_EXTREME if dim == extreme_dim else RISK_LEVEL_MEDIUM
        result.append(make_dimension(dimension=dim, level=level))
    return result


def make_portfolio_snapshot(
    *,
    holdings: list[Holding] | None = None,
    as_of: str = AS_OF,
) -> PortfolioSnapshot:
    """构造有效的 PortfolioSnapshot。"""
    if holdings is None:
        holdings = [
            Holding(stock_code="TEST001", stock_name="测试公司", weight=0.4, industry="制造业", market_value=4000.0),
            Holding(stock_code="TEST002", stock_name="测试公司 2", weight=0.3, industry="制造业", market_value=3000.0),
            Holding(stock_code="TEST003", stock_name="测试公司 3", weight=0.2, industry="金融业", market_value=2000.0),
            Holding(stock_code="TEST004", stock_name="测试公司 4", weight=0.1, industry="科技业", market_value=1000.0),
        ]
    weights = [h.weight for h in holdings]
    holdings_weights = {h.stock_code: h.weight for h in holdings}
    holdings_industries = {h.stock_code: h.industry for h in holdings}
    return PortfolioSnapshot(
        as_of=as_of,
        holdings=holdings,
        total_market_value=sum(h.market_value for h in holdings),
        industry_exposure=compute_industry_exposure(holdings_weights, holdings_industries),
        concentration_hhi=compute_concentration_hhi(weights),
        top_holdings=sorted(holdings, key=lambda h: h.weight, reverse=True)[:10],
    )


# ---------------------------------------------------------------------------
# 测试 1：契约常量
# ---------------------------------------------------------------------------


class TestContracts:
    """测试契约常量完整性。"""

    def test_six_risk_dimensions(self):
        """6 个风险维度全部定义。"""
        assert len(RISK_DIMENSIONS) == 6
        for dim in RISK_DIMENSIONS:
            assert dim in RISK_DIMENSION_LABELS
            # 中文标签
            label = RISK_DIMENSION_LABELS[dim]
            assert any("\u4e00" <= ch <= "\u9fff" for ch in label)

    def test_four_risk_levels(self):
        """4 个风险等级。"""
        assert len(RISK_LEVEL_SCORES) == 4
        assert RISK_LEVEL_SCORES[RISK_LEVEL_LOW] == 0.25
        assert RISK_LEVEL_SCORES[RISK_LEVEL_MEDIUM] == 0.5
        assert RISK_LEVEL_SCORES[RISK_LEVEL_HIGH] == 0.75
        assert RISK_LEVEL_SCORES[RISK_LEVEL_EXTREME] == 1.0

    def test_three_driver_directions(self):
        """3 个驱动方向。"""
        from core.research.risk import DRIVER_DIRECTIONS
        assert len(DRIVER_DIRECTIONS) == 3
        assert DRIVER_DIRECTION_UPSIDE in DRIVER_DIRECTIONS
        assert DRIVER_DIRECTION_DOWNSIDE in DRIVER_DIRECTIONS
        assert DRIVER_DIRECTION_NEUTRAL in DRIVER_DIRECTIONS

    def test_three_driver_magnitudes(self):
        """3 个驱动幅度。"""
        from core.research.risk import DRIVER_MAGNITUDES
        assert len(DRIVER_MAGNITUDES) == 3
        assert DRIVER_MAGNITUDE_SMALL in DRIVER_MAGNITUDES
        assert DRIVER_MAGNITUDE_MEDIUM in DRIVER_MAGNITUDES
        assert DRIVER_MAGNITUDE_LARGE in DRIVER_MAGNITUDES

    def test_four_scenario_types(self):
        """4 个情景类型。"""
        assert len(SCENARIO_TYPES) == 4
        assert SCENARIO_BEAR_CASE in SCENARIO_TYPES
        assert SCENARIO_BASE_CASE in SCENARIO_TYPES
        assert SCENARIO_BULL_CASE in SCENARIO_TYPES
        assert SCENARIO_CUSTOM in SCENARIO_TYPES

    def test_notice_disclaims_trade_advice(self):
        """NOTICE 明确否认交易建议。"""
        assert "风险暴露" in NOTICE
        assert "压力结果" in NOTICE
        assert "交易动作" in NOTICE

    def test_forbidden_keywords_complete(self):
        """9 个中文交易动作词。"""
        assert len(FORBIDDEN_TRADE_ACTION_KEYWORDS) == 9
        for kw in ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓", "调仓"):
            assert kw in FORBIDDEN_TRADE_ACTION_KEYWORDS

    def test_risk_dimension_default_score_from_level(self):
        """RiskDimension 默认 score 由 level 推导。"""
        d = RiskDimension(dimension=DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE, level=RISK_LEVEL_HIGH)
        assert d.score == RISK_LEVEL_SCORES[RISK_LEVEL_HIGH]

    def test_risk_driver_contains_trade_action(self):
        """RiskDriver 检测交易动作词。"""
        d = RiskDriver(driver_id="drv_1", name="建议买入该标的")
        assert d.contains_trade_action() is True

    def test_risk_dimension_contains_trade_action_in_note(self):
        """RiskDimension 检测 note 中的交易动作词。"""
        d = RiskDimension(
            dimension=DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
            level=RISK_LEVEL_HIGH,
            note="建议止损",
        )
        assert d.contains_trade_action() is True

    def test_research_risk_context_derives_id(self):
        """ResearchRiskContext 自动推导 context_id。"""
        ctx = ResearchRiskContext(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            model_version="v1",
        )
        assert ctx.context_id.startswith("risk_ctx_")
        # 相同输入 → 相同 ID
        ctx2 = ResearchRiskContext(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            model_version="v1",
        )
        assert ctx.context_id == ctx2.context_id

    def test_scenario_stress_derives_id(self):
        """ScenarioStressTest 自动推导 test_id。"""
        test = ScenarioStressTest(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            scenario_name="test",
            model_version="v1",
        )
        assert test.test_id.startswith("stress_")

    def test_portfolio_snapshot_derives_id(self):
        """PortfolioSnapshot 自动推导 snapshot_id。"""
        snap = PortfolioSnapshot(as_of=AS_OF, holdings=[
            Holding(stock_code="A", weight=0.5, industry="X"),
            Holding(stock_code="B", weight=0.5, industry="Y"),
        ])
        assert snap.snapshot_id.startswith("snap_")


# ---------------------------------------------------------------------------
# 测试 2：HHI 计算与行业暴露
# ---------------------------------------------------------------------------


class TestConcentrationAndExposure:
    """测试集中度 HHI 与行业暴露计算。"""

    def test_hhi_single_holding(self):
        """单一持仓 HHI = 1。"""
        assert compute_concentration_hhi([1.0]) == 1.0

    def test_hhi_equal_weights(self):
        """N 个等权持仓 HHI = 1/N。"""
        n = 4
        weights = [1.0 / n] * n
        assert abs(compute_concentration_hhi(weights) - 1.0 / n) < 1e-9

    def test_hhi_empty(self):
        """空持仓 HHI = 0。"""
        assert compute_concentration_hhi([]) == 0.0

    def test_hhi_unequal_weights(self):
        """不等权持仓 HHI = Σ(w_i²)。"""
        weights = [0.5, 0.3, 0.2]
        expected = 0.25 + 0.09 + 0.04
        assert abs(compute_concentration_hhi(weights) - expected) < 1e-9

    def test_industry_exposure_aggregates(self):
        """行业暴露按行业聚合。"""
        holdings_weights = {"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1}
        holdings_industries = {"A": "制造业", "B": "制造业", "C": "金融业", "D": "科技业"}
        exposure = compute_industry_exposure(holdings_weights, holdings_industries)
        assert exposure["制造业"] == 0.7
        assert exposure["金融业"] == 0.2
        assert exposure["科技业"] == 0.1

    def test_industry_exposure_empty(self):
        """空持仓行业暴露 = 空字典。"""
        assert compute_industry_exposure({}, {}) == {}


# ---------------------------------------------------------------------------
# 测试 3：综合风险等级聚合
# ---------------------------------------------------------------------------


class TestAggregateOverallRisk:
    """测试综合风险等级聚合。"""

    def test_empty_dimensions_returns_low(self):
        """无维度 → low。"""
        level, score = aggregate_overall_risk_level([])
        assert level == RISK_LEVEL_LOW
        assert score == 0.0

    def test_all_low_dimensions_returns_low(self):
        """所有维度 low → overall low。"""
        dims = [make_dimension(dimension=d, level=RISK_LEVEL_LOW) for d in RISK_DIMENSIONS]
        level, score = aggregate_overall_risk_level(dims)
        assert level == RISK_LEVEL_LOW
        assert score == RISK_LEVEL_SCORES[RISK_LEVEL_LOW]

    def test_one_extreme_propagates(self):
        """任一维度 extreme → overall extreme。"""
        dims = make_six_dimensions(extreme_dim=DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE)
        level, score = aggregate_overall_risk_level(dims)
        assert level == RISK_LEVEL_EXTREME

    def test_max_medium_returns_medium(self):
        """最大维度 medium → overall medium。"""
        dims = [make_dimension(dimension=d, level=RISK_LEVEL_MEDIUM) for d in RISK_DIMENSIONS]
        level, score = aggregate_overall_risk_level(dims)
        assert level == RISK_LEVEL_MEDIUM

    def test_max_high_returns_high(self):
        """最大维度 high → overall high。"""
        dims = [
            make_dimension(dimension=d, level=RISK_LEVEL_HIGH if d == DIMENSION_FACTOR_CORRELATION else RISK_LEVEL_LOW)
            for d in RISK_DIMENSIONS
        ]
        level, score = aggregate_overall_risk_level(dims)
        assert level == RISK_LEVEL_HIGH


# ---------------------------------------------------------------------------
# 测试 4：研究风险引擎
# ---------------------------------------------------------------------------


class TestResearchRiskEngine:
    """测试 ResearchRiskEngine。"""

    def test_build_context_with_six_dimensions(self):
        """6 个维度齐全 → 构建成功。"""
        engine = build_research_risk_engine()
        dimensions = make_six_dimensions()
        ctx = engine.build_context(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            dimensions=dimensions,
            factor_exposures={"market": 1.1, "value": -0.3},
            correlation_notes=["与某板块相关性较高"],
            portfolio_snapshot=make_portfolio_snapshot(),
        )
        assert ctx.company_symbol == SYMBOL
        assert ctx.as_of == AS_OF
        assert len(ctx.dimensions) == 6
        assert ctx.overall_risk_level == RISK_LEVEL_MEDIUM
        assert ctx.concentration_hhi > 0
        assert ctx.factor_exposures["market"] == 1.1

    def test_build_context_missing_dimension_raises(self):
        """缺维度 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        # 只提供 5 个维度
        dimensions = [make_dimension(dimension=d) for d in RISK_DIMENSIONS[:5]]
        with pytest.raises(ValueError, match="缺少风险维度"):
            engine.build_context(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                dimensions=dimensions,
            )

    def test_build_context_invalid_level_raises(self):
        """非法 level → 抛 ValueError。"""
        engine = build_research_risk_engine()
        dimensions = make_six_dimensions()
        dimensions[0].level = "invalid_level"
        with pytest.raises(ValueError, match="非法风险等级"):
            engine.build_context(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                dimensions=dimensions,
            )

    def test_build_context_trade_action_in_note_raises(self):
        """维度 note 含交易动作词 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        dimensions = make_six_dimensions()
        dimensions[0].note = "建议买入该标的"
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.build_context(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                dimensions=dimensions,
            )

    def test_build_context_trade_action_in_driver_raises(self):
        """驱动 name 含交易动作词 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        dimensions = make_six_dimensions()
        dimensions[0].drivers[0].name = "建议加仓"
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.build_context(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                dimensions=dimensions,
            )

    def test_build_context_extreme_dimension_propagates(self):
        """任一维度 extreme → overall extreme。"""
        engine = build_research_risk_engine()
        dimensions = make_six_dimensions(extreme_dim=DIMENSION_INDUSTRY_SUPPLY_DEMAND_TECH)
        ctx = engine.build_context(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            dimensions=dimensions,
        )
        assert ctx.overall_risk_level == RISK_LEVEL_EXTREME

    def test_build_dimension_factory(self):
        """build_dimension 工厂方法。"""
        engine = build_research_risk_engine()
        d = engine.build_dimension(
            dimension=DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE,
            level=RISK_LEVEL_HIGH,
            drivers=[engine.build_driver(driver_id="drv1", name="test")],
        )
        assert d.dimension == DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE
        assert d.level == RISK_LEVEL_HIGH
        assert d.score == RISK_LEVEL_SCORES[RISK_LEVEL_HIGH]

    def test_build_dimension_invalid_dimension_raises(self):
        """非法维度 key → 抛 ValueError。"""
        engine = build_research_risk_engine()
        with pytest.raises(ValueError, match="非法风险维度"):
            engine.build_dimension(dimension="invalid_dim", level=RISK_LEVEL_LOW)

    def test_build_driver_invalid_direction_raises(self):
        """非法方向 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        with pytest.raises(ValueError, match="非法驱动方向"):
            engine.build_driver(
                driver_id="drv1", name="test", direction="invalid"
            )

    def test_build_driver_invalid_magnitude_raises(self):
        """非法幅度 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        with pytest.raises(ValueError, match="非法驱动幅度"):
            engine.build_driver(
                driver_id="drv1", name="test", magnitude="invalid"
            )

    def test_build_driver_trade_action_raises(self):
        """驱动名称含交易动作词 → 抛 ValueError。"""
        engine = build_research_risk_engine()
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.build_driver(driver_id="drv1", name="建议清仓")

    def test_context_to_dict_contains_notice(self):
        """序列化含 NOTICE。"""
        engine = build_research_risk_engine()
        ctx = engine.build_context(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            dimensions=make_six_dimensions(),
        )
        d = ctx.to_dict()
        assert "notice" in d
        assert "交易动作" in d["notice"]


# ---------------------------------------------------------------------------
# 测试 5：情景压力测试引擎
# ---------------------------------------------------------------------------


class TestScenarioStressEngine:
    """测试 ScenarioStressEngine。"""

    def test_bear_case_negative_revenue_impact(self):
        """悲观情景收入影响为负。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        assert test.revenue_impact_pct < 0
        assert test.profit_impact_pct < 0
        assert test.cashflow_impact_pct < 0
        assert test.valuation_impact_pct < 0

    def test_bull_case_positive_revenue_impact(self):
        """乐观情景收入影响为正。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BULL_CASE,
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        assert test.revenue_impact_pct > 0
        assert test.profit_impact_pct > 0
        assert test.cashflow_impact_pct > 0
        assert test.valuation_impact_pct > 0

    def test_base_case_zero_impact(self):
        """基准情景所有影响为 0。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BASE_CASE,
        )
        assert test.revenue_impact_pct == 0.0
        assert test.profit_impact_pct == 0.0
        assert test.cashflow_impact_pct == 0.0
        assert test.valuation_impact_pct == 0.0

    def test_custom_scenario_with_assumptions(self):
        """自定义情景可指定 assumptions。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_CUSTOM,
            scenario_name="中美贸易摩擦升级",
            scenario_description="假设关税上升导致出口下降",
            assumptions={
                "revenue_shock_pct": -15.0,
                "margin_shock_pct": -3.0,
                "fcf_conversion_drop_pct": -5.0,
                "multiple_compress_pct": -25.0,
            },
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        assert test.revenue_impact_pct == -15.0
        # 利润 = -15 × 2 + (-3 / 0.10 × 100) = -30 + -30 = -60
        assert test.profit_impact_pct == pytest.approx(-60.0, abs=0.1)
        # 现金流 = -60 + -5 = -65
        assert test.cashflow_impact_pct == pytest.approx(-65.0, abs=0.1)
        assert test.valuation_impact_pct == -25.0

    def test_invalid_scenario_type_raises(self):
        """非法情景类型 → 抛 ValueError。"""
        engine = build_scenario_stress_engine()
        with pytest.raises(ValueError, match="非法情景类型"):
            engine.compute(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                scenario_type="invalid",
            )

    def test_non_baseline_missing_baseline_raises(self):
        """非基准情景缺 baseline → 抛 ValueError。"""
        engine = build_scenario_stress_engine()
        with pytest.raises(ValueError, match="非基准情景必须提供 baseline.revenue"):
            engine.compute(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                scenario_type=SCENARIO_BEAR_CASE,
            )

    def test_trade_action_in_scenario_name_raises(self):
        """情景名称含交易动作词 → 抛 ValueError。"""
        engine = build_scenario_stress_engine()
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.compute(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                scenario_type=SCENARIO_CUSTOM,
                scenario_name="建议买入情景",
                baseline={"revenue": 1000.0, "margin": 0.10},
            )

    def test_template_defaults_applied(self):
        """标准情景模板默认值生效。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        template = STANDARD_SCENARIO_TEMPLATES[SCENARIO_BEAR_CASE]
        assert test.scenario_name == template["scenario_name"]
        assert test.assumptions["revenue_shock_pct"] == template["assumptions"]["revenue_shock_pct"]

    def test_custom_overrides_template(self):
        """自定义 assumptions 覆盖模板默认值。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            assumptions={"revenue_shock_pct": -20.0},  # 覆盖模板 -10
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        assert test.assumptions["revenue_shock_pct"] == -20.0
        # 其他模板参数保留
        assert test.assumptions["multiple_compress_pct"] == -20.0

    def test_to_dict_contains_notice(self):
        """序列化含 NOTICE。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BASE_CASE,
        )
        d = test.to_dict()
        assert "notice" in d
        assert "交易动作" in d["notice"]

    def test_to_dict_contains_scenario_type_label(self):
        """序列化含中文情景类型标签。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        d = test.to_dict()
        assert d["scenario_type_label"] == "悲观情景"


# ---------------------------------------------------------------------------
# 测试 6：组合快照只读适配器
# ---------------------------------------------------------------------------


class TestPortfolioSnapshotAdapter:
    """测试 PortfolioSnapshotAdapter。"""

    def test_build_snapshot_from_holdings(self):
        """从显式 holdings 构建快照。"""
        adapter = build_portfolio_snapshot_adapter()
        holdings = [
            Holding(stock_code="A", stock_name="公司 A", weight=0.5, industry="制造业", market_value=5000.0),
            Holding(stock_code="B", stock_name="公司 B", weight=0.3, industry="金融业", market_value=3000.0),
            Holding(stock_code="C", stock_name="公司 C", weight=0.2, industry="科技业", market_value=2000.0),
        ]
        snap = adapter.build_snapshot_from_holdings(holdings=holdings, as_of=AS_OF)
        assert snap.as_of == AS_OF
        assert len(snap.holdings) == 3
        assert snap.total_market_value == 10000.0
        # HHI = 0.25 + 0.09 + 0.04 = 0.38
        assert abs(snap.concentration_hhi - 0.38) < 1e-6
        assert snap.industry_exposure["制造业"] == 0.5
        assert snap.industry_exposure["金融业"] == 0.3
        assert snap.industry_exposure["科技业"] == 0.2

    def test_build_snapshot_top_holdings_sorted(self):
        """top_holdings 按权重降序。"""
        adapter = build_portfolio_snapshot_adapter()
        holdings = [
            Holding(stock_code="A", weight=0.1, industry="X"),
            Holding(stock_code="B", weight=0.5, industry="X"),
            Holding(stock_code="C", weight=0.3, industry="X"),
            Holding(stock_code="D", weight=0.1, industry="X"),
        ]
        snap = adapter.build_snapshot_from_holdings(holdings=holdings, as_of=AS_OF)
        assert snap.top_holdings[0].stock_code == "B"
        assert snap.top_holdings[1].stock_code == "C"
        assert snap.top_holdings[2].stock_code in ("A", "D")

    def test_build_snapshot_empty_holdings(self):
        """空 holdings → 空快照。"""
        adapter = build_portfolio_snapshot_adapter()
        snap = adapter.build_snapshot_from_holdings(holdings=[], as_of=AS_OF)
        assert len(snap.holdings) == 0
        assert snap.total_market_value == 0.0
        assert snap.concentration_hhi == 0.0

    def test_build_snapshot_without_position_manager_raises(self):
        """未注入 PositionManager 调用 build_snapshot → RuntimeError。"""
        adapter = build_portfolio_snapshot_adapter()
        with pytest.raises(RuntimeError, match="PositionManager 未注入"):
            adapter.build_snapshot(as_of=AS_OF)

    def test_build_snapshot_with_mock_position_manager(self):
        """注入 mock PositionManager 构建快照。"""
        from unittest.mock import MagicMock
        mock_pm = MagicMock()

        # 模拟 Position dataclass
        class MockPosition:
            def __init__(self, code, name, industry):
                self.stock_code = code
                self.stock_name = name
                self.weight = 25.0  # 百分比
                self.industry = industry
                self.market_value = 2500.0

        mock_pm.get_all_positions.return_value = [
            MockPosition("A", "公司 A", "制造业"),
            MockPosition("B", "公司 B", "金融业"),
            MockPosition("C", "公司 C", "科技业"),
            MockPosition("D", "公司 D", "制造业"),
        ]
        mock_pm.calculate_analysis.return_value = {
            "summary": {"total_market_value": 10000.0},
        }

        adapter = build_portfolio_snapshot_adapter(position_manager=mock_pm)
        snap = adapter.build_snapshot(as_of=AS_OF)
        assert len(snap.holdings) == 4
        # weight 25% → 0.25
        assert snap.holdings[0].weight == 0.25
        # 行业聚合
        assert snap.industry_exposure["制造业"] == 0.5
        assert snap.industry_exposure["金融业"] == 0.25
        assert snap.industry_exposure["科技业"] == 0.25

    def test_build_snapshot_graceful_degradation_on_calculate_failure(self):
        """calculate_analysis 抛错时优雅降级（均等权重）。"""
        from unittest.mock import MagicMock
        mock_pm = MagicMock()

        class MockPosition:
            def __init__(self, code, name, industry):
                self.stock_code = code
                self.stock_name = name
                self.industry = industry

        mock_pm.get_all_positions.return_value = [
            MockPosition("A", "公司 A", "制造业"),
            MockPosition("B", "公司 B", "金融业"),
        ]
        mock_pm.calculate_analysis.side_effect = RuntimeError("price data unavailable")

        adapter = build_portfolio_snapshot_adapter(position_manager=mock_pm)
        snap = adapter.build_snapshot(as_of=AS_OF)
        # 降级为均等权重
        assert len(snap.holdings) == 2
        assert snap.holdings[0].weight == 0.5
        assert snap.holdings[1].weight == 0.5

    def test_build_snapshot_empty_positions(self):
        """PositionManager 返回空持仓 → 空快照。"""
        from unittest.mock import MagicMock
        mock_pm = MagicMock()
        mock_pm.get_all_positions.return_value = []
        adapter = build_portfolio_snapshot_adapter(position_manager=mock_pm)
        snap = adapter.build_snapshot(as_of=AS_OF)
        assert len(snap.holdings) == 0
        assert snap.total_market_value == 0.0


# ---------------------------------------------------------------------------
# 端到端集成测试
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端：构建完整风险上下文 + 情景压力测试。"""

    def test_full_pipeline_risk_context_with_stress(self):
        """完整流程：构建风险上下文 + 3 个标准情景压力测试。"""
        risk_engine = build_research_risk_engine()
        stress_engine = build_scenario_stress_engine()

        # 构建风险上下文
        dimensions = make_six_dimensions()
        snap = make_portfolio_snapshot()
        ctx = risk_engine.build_context(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            dimensions=dimensions,
            factor_exposures={"market": 1.2, "size": -0.3, "value": 0.4},
            correlation_notes=["与某板块相关性 0.7"],
            portfolio_snapshot=snap,
        )

        # 3 个标准情景压力测试
        for scenario in (SCENARIO_BEAR_CASE, SCENARIO_BASE_CASE, SCENARIO_BULL_CASE):
            test = stress_engine.compute(
                company_symbol=SYMBOL,
                as_of=AS_OF,
                scenario_type=scenario,
                baseline={"revenue": 1000.0, "margin": 0.10},
            )
            assert test.company_symbol == SYMBOL
            # 基准情景 → 0；悲观 → 负；乐观 → 正
            if scenario == SCENARIO_BASE_CASE:
                assert test.revenue_impact_pct == 0.0
            elif scenario == SCENARIO_BEAR_CASE:
                assert test.revenue_impact_pct < 0
            elif scenario == SCENARIO_BULL_CASE:
                assert test.revenue_impact_pct > 0

        # 校验上下文聚合
        assert ctx.overall_risk_level == RISK_LEVEL_MEDIUM
        assert ctx.concentration_hhi > 0
        assert "market" in ctx.factor_exposures

    def test_portfolio_snapshot_feeds_into_risk_context(self):
        """组合快照的 HHI / 行业暴露进入风险上下文。"""
        risk_engine = build_research_risk_engine()
        snap = make_portfolio_snapshot()
        ctx = risk_engine.build_context(
            company_symbol=SYMBOL,
            as_of=AS_OF,
            dimensions=make_six_dimensions(),
            portfolio_snapshot=snap,
        )
        assert ctx.concentration_hhi == snap.concentration_hhi
        assert ctx.concentration_hhi > 0


# ---------------------------------------------------------------------------
# AGENTS.md 合规
# ---------------------------------------------------------------------------


class TestAgentsMdCompliance:
    """AGENTS.md 合规检查。"""

    def test_no_trade_advice_in_source(self):
        """源码中无交易动作建议。"""
        contracts_path = Path(__file__).parent.parent / "core" / "research" / "risk" / "contracts.py"
        risk_path = Path(__file__).parent.parent / "core" / "research" / "risk" / "research_risk.py"
        stress_path = Path(__file__).parent.parent / "core" / "research" / "risk" / "scenario_stress.py"
        adapter_path = Path(__file__).parent.parent / "core" / "quant" / "portfolio" / "portfolio_snapshot_adapter.py"

        for path in (contracts_path, risk_path, stress_path, adapter_path):
            text = path.read_text(encoding="utf-8")
            for kw in ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓", "调仓"):
                # FORBIDDEN_TRADE_ACTION_KEYWORDS 元组中的字符串本身不算违规
                if kw in text and f'"{kw}"' not in text and f"'{kw}'" not in text:
                    pytest.fail(f"{path.name} 含交易动作词：{kw}")

    def test_notice_disclaims_trade_advice(self):
        """NOTICE 明确否认交易动作建议。"""
        assert "不输出" in NOTICE or "不表示" in NOTICE
        assert "交易动作" in NOTICE

    def test_risk_engine_rejects_trade_action_in_all_levels(self):
        """引擎在 driver / dimension / context 三层均拒绝交易动作词。"""
        engine = build_research_risk_engine()

        # driver 层
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.build_driver(driver_id="x", name="建议加仓")

        # dimension 层
        d = make_dimension(dimension=DIMENSION_CORPORATE_FINANCIAL_GOVERNANCE)
        d.note = "建议减仓"
        with pytest.raises(ValueError, match="含交易动作关键词"):
            engine.build_context(
                company_symbol=SYMBOL, as_of=AS_OF,
                dimensions=[d] + make_six_dimensions()[1:],
            )

    def test_stress_engine_rejects_trade_action(self):
        """压力测试引擎拒绝情景名称/描述中的交易动作词。"""
        engine = build_scenario_stress_engine()
        for kw in FORBIDDEN_TRADE_ACTION_KEYWORDS:
            with pytest.raises(ValueError, match="含交易动作关键词"):
                engine.compute(
                    company_symbol=SYMBOL, as_of=AS_OF,
                    scenario_type=SCENARIO_CUSTOM,
                    scenario_name=f"建议{kw}",
                    baseline={"revenue": 1000.0, "margin": 0.10},
                )

    def test_graceful_degradation_on_missing_baseline(self):
        """非基准情景缺 baseline → 抛 ValueError（不崩溃）。"""
        engine = build_scenario_stress_engine()
        with pytest.raises(ValueError):
            engine.compute(
                company_symbol=SYMBOL, as_of=AS_OF,
                scenario_type=SCENARIO_BEAR_CASE,
            )

    def test_no_position_advice_output(self):
        """输出不含仓位建议（边界要求）。"""
        engine = build_research_risk_engine()
        ctx = engine.build_context(
            company_symbol=SYMBOL, as_of=AS_OF,
            dimensions=make_six_dimensions(),
        )
        d = ctx.to_dict()
        # 不应含仓位建议字段
        assert "position_advice" not in d
        assert "trade_action" not in d
        assert "weight_recommendation" not in d

    def test_stress_output_only_quantitative(self):
        """压力测试输出仅为定量结果（impact_pct）。"""
        engine = build_scenario_stress_engine()
        test = engine.compute(
            company_symbol=SYMBOL, as_of=AS_OF,
            scenario_type=SCENARIO_BEAR_CASE,
            baseline={"revenue": 1000.0, "margin": 0.10},
        )
        d = test.to_dict()
        # 应含 4 个 impact_pct 字段
        assert "revenue_impact_pct" in d
        assert "profit_impact_pct" in d
        assert "cashflow_impact_pct" in d
        assert "valuation_impact_pct" in d
        # 不应含交易动作字段
        assert "trade_action" not in d
        assert "position_advice" not in d
