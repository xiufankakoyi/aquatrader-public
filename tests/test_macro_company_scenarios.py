"""P3.4 跨域因果与宏观情景系统测试。

覆盖：
- 传导路径契约校验
- 方向与时滞
- 相关性 ≠ 因果
- 压力情景构建
- 行业适用性过滤

运行：
    venv\\Scripts\\python.exe -m pytest tests/test_macro_company_scenarios.py -q
"""
from __future__ import annotations

import pytest

from core.research.scenario import (
    DIRECTION_NEGATIVE,
    DIRECTION_POSITIVE,
    DIRECTION_NEUTRAL,
    CompanyStressEngine,
    CompanyStressScenario,
    CorrelationCausationError,
    DriverImpact,
    ForecastSnapshot,
    MacroShock,
    MacroTransmissionEngine,
    MacroTransmissionRegistry,
    MacroVariable,
    RISK_LEVEL_HIGH,
    RISK_LEVEL_LOW,
    RISK_LEVEL_MEDIUM,
    RiskDelta,
    ScenarioContractError,
    TARGET_DRIVER_DISCOUNT_RATE,
    TARGET_DRIVER_FCF_CONVERSION,
    TARGET_DRIVER_GROSS_MARGIN,
    TARGET_DRIVER_REVENUE,
    TARGET_DRIVER_VALUATION_MULTIPLE,
    TransmissionPath,
    TransmissionRegistryError,
    assert_causal_path,
    build_default_registry,
    build_default_stress_engine,
)
from core.research.scenario.contracts import (
    MACRO_CATEGORY_COMMODITY_ENERGY,
    MACRO_CATEGORY_DEMAND_CAPEX,
    MACRO_CATEGORY_INDUSTRY_PRICE_INVENTORY_UTIL,
    MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT,
    MACRO_CATEGORY_TRADE_POLICY_SUPPLY,
    TARGET_DRIVER_CAPEX,
    TARGET_DRIVER_OPEX_RATIO,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_macro_variable(
    name: str = "policy_rate",
    category: str = MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT,
    value: float = 5.0,
    unit: str = "%",
    as_of: str = "2024-12-31",
    source_ref: str = "src:pboc:policy_rate",
) -> MacroVariable:
    """构造测试用宏观变量。"""
    return MacroVariable(
        name=name,
        category=category,
        value=value,
        unit=unit,
        as_of=as_of,
        source_ref=source_ref,
    )


def _make_path(
    source_variable: str = "policy_rate",
    target_driver: str = TARGET_DRIVER_DISCOUNT_RATE,
    direction: str = DIRECTION_POSITIVE,
    lag_months: int = 0,
    applicable_industries: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = ("ev:test:001",),
    confidence: float = 0.80,
    sensitivity: float = 1.0,
    description: str = "测试路径",
) -> TransmissionPath:
    """构造测试用传导路径。"""
    return TransmissionPath(
        source_variable=source_variable,
        target_driver=target_driver,
        direction=direction,
        lag_months=lag_months,
        applicable_industries=applicable_industries,
        evidence_refs=evidence_refs,
        confidence=confidence,
        sensitivity=sensitivity,
        description=description,
    )


def _make_baseline_forecast() -> ForecastSnapshot:
    """构造测试用基线财务快照。"""
    return ForecastSnapshot(
        revenue=100000.0,
        gross_margin=0.70,
        opex_ratio=0.20,
        net_profit=50000.0,
        fcf=40000.0,
        discount_rate=0.10,
        valuation_multiple=30.0,
        enterprise_value=1500000.0,
    )


def _build_registry_with_evidence() -> MacroTransmissionRegistry:
    """构造所有默认路径都附加证据的注册表（用于压力测试）。"""
    registry = build_default_registry()
    for path in registry.all_paths():
        if path.correlation_only:
            registry.attach_evidence(
                path.path_id,
                evidence_refs=(f"ev:template:{path.source_variable}:"
                               f"{path.target_driver}",),
            )
    return registry


def _build_stress_engine_with_evidence(
    horizon_months: int = 12,
    allow_correlation_only: bool = False,
) -> CompanyStressEngine:
    """构造所有路径已附加证据的压力测试引擎。"""
    registry = _build_registry_with_evidence()
    transmission = MacroTransmissionEngine(
        registry=registry,
        horizon_months=horizon_months,
        allow_correlation_only=allow_correlation_only,
    )
    return CompanyStressEngine(transmission)


# ---------------------------------------------------------------------------
# A. MacroVariable 契约
# ---------------------------------------------------------------------------


class TestMacroVariableContract:
    """宏观变量契约测试。"""

    def test_creation(self) -> None:
        """构造合法 MacroVariable。"""
        var = _make_macro_variable()
        assert var.name == "policy_rate"
        assert var.value == 5.0
        assert var.unit == "%"
        assert var.as_of == "2024-12-31"
        assert var.source_ref.startswith("src:")

    def test_invalid_category_raises(self) -> None:
        """非法 category 必须抛错。"""
        with pytest.raises(ScenarioContractError, match="category 非法"):
            _make_macro_variable(category="unknown_category")

    def test_missing_source_ref_raises(self) -> None:
        """source_ref 为空必须抛错（不得凭空编造来源）。"""
        with pytest.raises(ScenarioContractError, match="source_ref 必填"):
            MacroVariable(
                name="policy_rate",
                category=MACRO_CATEGORY_RATE_FX_INFLATION_CREDIT,
                value=5.0,
                unit="%",
                as_of="2024-12-31",
                source_ref="",
            )


# ---------------------------------------------------------------------------
# B. TransmissionPath 契约与校验
# ---------------------------------------------------------------------------


class TestTransmissionPathContract:
    """传导路径契约测试。"""

    def test_creation(self) -> None:
        """构造合法 TransmissionPath 并派生 path_id。"""
        path = _make_path()
        assert path.path_id.startswith("path_")
        assert path.sign() == 1.0
        assert path.correlation_only is False  # 有证据且置信度高

    def test_invalid_direction_raises(self) -> None:
        """非法 direction 必须抛错。"""
        with pytest.raises(ScenarioContractError, match="direction 非法"):
            _make_path(direction="up")

    def test_invalid_confidence_raises(self) -> None:
        """confidence 越界 [0,1] 必须抛错。"""
        with pytest.raises(ScenarioContractError, match="confidence 越界"):
            _make_path(confidence=1.5)

    def test_negative_lag_raises(self) -> None:
        """lag_months 为负必须抛错。"""
        with pytest.raises(ScenarioContractError, match="lag_months 不能为负"):
            _make_path(lag_months=-1)

    def test_invalid_target_driver_raises(self) -> None:
        """非法 target_driver 必须抛错。"""
        with pytest.raises(ScenarioContractError, match="target_driver 非法"):
            _make_path(target_driver="unknown_driver")

    def test_direction_sign_mapping(self) -> None:
        """方向到符号映射正确。"""
        assert _make_path(direction=DIRECTION_POSITIVE).sign() == 1.0
        assert _make_path(direction=DIRECTION_NEGATIVE).sign() == -1.0
        assert _make_path(direction=DIRECTION_NEUTRAL).sign() == 0.0

    def test_lag_months_zero_and_positive(self) -> None:
        """lag_months 可为 0 或正整数。"""
        assert _make_path(lag_months=0).lag_months == 0
        assert _make_path(lag_months=24).lag_months == 24


# ---------------------------------------------------------------------------
# C. 相关性 ≠ 因果
# ---------------------------------------------------------------------------


class TestCorrelationCausation:
    """相关性不能自动声明因果。"""

    def test_no_evidence_marks_correlation_only(self) -> None:
        """evidence_refs 为空时 correlation_only=True。"""
        path = _make_path(evidence_refs=())
        assert path.correlation_only is True

    def test_low_confidence_marks_correlation_only(self) -> None:
        """confidence 低于阈值时 correlation_only=True。"""
        path = _make_path(confidence=0.20)
        assert path.correlation_only is True

    def test_assert_causal_path_rejects_correlation_only(self) -> None:
        """assert_causal_path 拒绝相关性观察路径。"""
        path = _make_path(evidence_refs=())
        with pytest.raises(CorrelationCausationError, match="相关性不能自动声明因果"):
            assert_causal_path(path)

    def test_assert_causal_path_accepts_valid(self) -> None:
        """assert_causal_path 通过合法因果路径。"""
        path = _make_path(evidence_refs=("ev:001",), confidence=0.75)
        assert_causal_path(path)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# D. 注册表
# ---------------------------------------------------------------------------


class TestTransmissionRegistry:
    """传导路径注册表测试。"""

    def test_register_and_lookup(self) -> None:
        """注册路径后可按 source / target 查询。"""
        registry = MacroTransmissionRegistry()
        path = _make_path()
        registry.register(path)
        assert len(registry) == 1
        assert registry.get(path.path_id) is path
        sources = registry.paths_for_source("policy_rate")
        assert len(sources) == 1
        targets = registry.paths_for_target(TARGET_DRIVER_DISCOUNT_RATE)
        assert len(targets) == 1

    def test_industry_filter(self) -> None:
        """行业过滤：空 applicable_industries 适用全部，指定后仅适用匹配行业。"""
        registry = MacroTransmissionRegistry()
        all_path = _make_path(
            source_variable="pmi",
            target_driver=TARGET_DRIVER_REVENUE,
            applicable_industries=(),
        )
        specific_path = _make_path(
            source_variable="oil_price",
            target_driver=TARGET_DRIVER_GROSS_MARGIN,
            applicable_industries=("航空", "化工"),
        )
        registry.register(all_path)
        registry.register(specific_path)
        # 全行业路径适用任何行业
        assert all_path.applies_to_industry("食品饮料") is True
        assert all_path.applies_to_industry("化工") is True
        # 指定行业路径只适用匹配行业
        assert specific_path.applies_to_industry("化工") is True
        assert specific_path.applies_to_industry("食品饮料") is False
        # 行业查询
        food_paths = registry.paths_for_industry("食品饮料")
        assert all_path in food_paths
        assert specific_path not in food_paths

    def test_duplicate_register_raises(self) -> None:
        """重复注册相同 path_id 必须抛错。"""
        registry = MacroTransmissionRegistry()
        path = _make_path()
        registry.register(path)
        with pytest.raises(TransmissionRegistryError, match="已存在"):
            registry.register(path)

    def test_attach_evidence_promotes_to_causal(self) -> None:
        """attach_evidence 将 correlation_only 路径升级为因果路径。"""
        registry = MacroTransmissionRegistry()
        path = _make_path(evidence_refs=())  # correlation_only=True
        registry.register(path)
        assert path.correlation_only is True
        upgraded = registry.attach_evidence(
            path.path_id, evidence_refs=("ev:upgrade:001",)
        )
        assert upgraded.correlation_only is False
        assert upgraded.evidence_refs == ("ev:upgrade:001",)
        # 原路径已被替换
        assert registry.get(path.path_id) is None or (
            registry.get(path.path_id) is upgraded
        )

    def test_default_registry_covers_all_categories(self) -> None:
        """默认注册表覆盖 5 类宏观变量。"""
        registry = build_default_registry()
        all_paths = registry.all_paths()
        assert len(all_paths) >= 20  # 至少 20 条模板路径
        # 验证每类宏观变量都有对应路径
        source_variables = {p.source_variable for p in all_paths}
        # 利率 / 汇率 / 通胀 / 信用
        assert "policy_rate" in source_variables
        assert "cny_usd" in source_variables
        assert "cpi" in source_variables
        assert "credit_spread" in source_variables
        # 商品 / 能源
        assert "oil_price" in source_variables
        assert "steel_price" in source_variables
        assert "lithium_price" in source_variables
        # 需求 / 资本开支
        assert "pmi" in source_variables
        assert "consumer_confidence" in source_variables
        assert "fixed_asset_investment" in source_variables
        assert "capex_index" in source_variables
        # 贸易 / 关税 / 政策 / 供应链
        assert "tariff_rate" in source_variables
        assert "supply_chain_index" in source_variables
        assert "export_control" in source_variables
        # 行业价格 / 库存 / 利用率
        assert "industry_inventory_days" in source_variables
        assert "industry_capacity_util" in source_variables
        assert "industry_price_index" in source_variables


# ---------------------------------------------------------------------------
# E. 公司压力测试
# ---------------------------------------------------------------------------


class TestCompanyStressScenario:
    """公司压力测试引擎测试。"""

    def test_bear_scenario_rate_hike(self) -> None:
        """利率上行 50bp 情景：消费公司估值压缩、现金流略降。"""
        engine = _build_stress_engine_with_evidence(horizon_months=12)
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=0.50,  # +50bp
            shock_unit="%",
            rationale="测试利率上行 50bp",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="利率上行 50bp 情景",
            scenario_description="测试利率上行对消费行业估值的影响",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # driver_impacts 应包含 discount_rate / valuation_multiple / fcf_conversion
        drivers = {di.driver for di in scenario.driver_impacts}
        assert TARGET_DRIVER_DISCOUNT_RATE in drivers
        assert TARGET_DRIVER_VALUATION_MULTIPLE in drivers
        assert TARGET_DRIVER_FCF_CONVERSION in drivers
        # revenue 未受政策利率直接冲击
        assert scenario.revenue_impact_pct == pytest.approx(0.0, abs=1e-6)
        # 估值倍数应下降（direction=-, shock=+）
        assert scenario.valuation_impact_pct < 0
        # 折现率应上升
        assert scenario.forecast_after.discount_rate > \
            scenario.forecast_before.discount_rate
        # 估值倍数应下降
        assert scenario.forecast_after.valuation_multiple < \
            scenario.forecast_before.valuation_multiple
        # 风险分数应上升
        assert scenario.risk_delta.score_delta > 0

    def test_industry_filter_oil_price_on_consumer(self) -> None:
        """油价冲击对食品饮料行业无传导路径（行业不匹配）。"""
        engine = _build_stress_engine_with_evidence()
        oil_var = _make_macro_variable(
            name="oil_price",
            category=MACRO_CATEGORY_COMMODITY_ENERGY,
            value=100.0,
            unit="USD/bbl",
            source_ref="src:energy:oil",
        )
        shock = MacroShock(
            variable=oil_var,
            shock_value=0.20,  # +20%
            shock_unit="fraction",
            rationale="油价上涨 20%",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="油价上行情景",
            scenario_description="测试油价对消费行业的影响",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # 食品饮料行业不在 oil_price 的 applicable_industries 中
        # → 无 driver_impacts（或仅含全行业路径，但 oil_price 路径都指定行业）
        oil_drivers = [
            di for di in scenario.driver_impacts
            if di.source_variable == "oil_price"
        ]
        assert oil_drivers == []
        # 油价冲击应被记录在 skipped_no_path 中
        assert "oil_price" in scenario.skipped_no_path

    def test_industry_filter_oil_price_on_airline(self) -> None:
        """油价冲击对航空行业有传导路径（行业匹配）。"""
        engine = _build_stress_engine_with_evidence()
        oil_var = _make_macro_variable(
            name="oil_price",
            category=MACRO_CATEGORY_COMMODITY_ENERGY,
            value=100.0,
            unit="USD/bbl",
            source_ref="src:energy:oil",
        )
        shock = MacroShock(
            variable=oil_var,
            shock_value=0.20,
            shock_unit="fraction",
            rationale="油价上涨 20%",
        )
        scenario = engine.stress(
            symbol="airline-001",
            industry="航空",
            as_of="2024-12-31",
            scenario_name="油价上行情景",
            scenario_description="测试油价对航空行业毛利率的影响",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # 航空行业在 oil_price → gross_margin 路径的适用范围内
        drivers = {di.driver for di in scenario.driver_impacts}
        assert TARGET_DRIVER_GROSS_MARGIN in drivers
        # 油价上升 → 毛利率下降（direction=-）
        gm_impact = next(
            di for di in scenario.driver_impacts
            if di.driver == TARGET_DRIVER_GROSS_MARGIN
        )
        assert gm_impact.impact_pct < 0

    def test_no_trade_action_keyword_in_scenario_name(self) -> None:
        """情景名称含交易动作词必须抛错。"""
        engine = _build_stress_engine_with_evidence()
        var = _make_macro_variable()
        shock = MacroShock(
            variable=var, shock_value=0.50, shock_unit="%", rationale=""
        )
        with pytest.raises(ScenarioContractError, match="交易动作关键词"):
            engine.stress(
                symbol="600519.SH",
                industry="食品饮料",
                as_of="2024-12-31",
                scenario_name="买入信号情景",
                scenario_description="",
                macro_shocks=[shock],
                baseline_forecast=_make_baseline_forecast(),
            )

    def test_aggregation_multiple_shocks_same_driver(self) -> None:
        """多个冲击指向同一 driver 时应聚合（求和）。"""
        engine = _build_stress_engine_with_evidence(horizon_months=12)
        # policy_rate + credit_spread 都影响 discount_rate
        rate_var = _make_macro_variable(name="policy_rate", value=5.0)
        credit_var = _make_macro_variable(
            name="credit_spread",
            value=2.0,
            unit="%",
            source_ref="src:bond:credit_spread",
        )
        shocks = [
            MacroShock(
                variable=rate_var,
                shock_value=0.50,
                shock_unit="%",
                rationale="利率 +50bp",
            ),
            MacroShock(
                variable=credit_var,
                shock_value=0.30,
                shock_unit="%",
                rationale="信用利差 +30bp",
            ),
        ]
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="利率与信用双重收紧情景",
            scenario_description="测试多冲击聚合",
            macro_shocks=shocks,
            baseline_forecast=_make_baseline_forecast(),
        )
        # discount_rate 应同时受 policy_rate 和 credit_spread 影响
        dr_impact = next(
            (di for di in scenario.driver_impacts
             if di.driver == TARGET_DRIVER_DISCOUNT_RATE),
            None,
        )
        assert dr_impact is not None
        # 聚合后的 source_variable 应包含两个源
        assert "policy_rate" in dr_impact.source_variable
        assert "credit_spread" in dr_impact.source_variable
        # 折现率应上升（两个正向冲击叠加）
        assert dr_impact.impact_pct > 0
        assert scenario.forecast_after.discount_rate > \
            scenario.forecast_before.discount_rate

    def test_risk_delta_escalation_on_large_adverse(self) -> None:
        """大幅不利冲击触发风险等级升级。"""
        engine = _build_stress_engine_with_evidence(horizon_months=12)
        # 极端利率上行 500bp
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=5.00,  # +500bp
            shock_unit="%",
            rationale="极端利率上行测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="极端利率上行情景",
            scenario_description="测试风险等级升级",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
            baseline_risk_level=RISK_LEVEL_LOW,
        )
        # 估值倍数压缩幅度应触发 escalated_drivers
        assert len(scenario.risk_delta.escalated_drivers) > 0
        # 风险分数应明显上升
        assert scenario.risk_delta.score_delta > 0.05
        # 风险等级应升级
        assert scenario.risk_delta.risk_level_after != RISK_LEVEL_LOW or \
            scenario.risk_delta.risk_score_after > \
            scenario.risk_delta.risk_score_before

    def test_lag_factor_zero_when_lag_exceeds_horizon(self) -> None:
        """lag_months >= horizon_months 时当期影响为 0。"""
        # horizon=3，policy_rate → fcf_conversion 路径 lag=3
        # lag_factor = (3-3)/3 = 0 → 不产生当期影响
        engine = _build_stress_engine_with_evidence(horizon_months=3)
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=0.50,
            shock_unit="%",
            rationale="短评估期测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="短评估期情景",
            scenario_description="测试时滞超出评估期",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # fcf_conversion 路径 lag=3，horizon=3 → lag_factor=0
        # 该路径应被记录在 correlation_only_paths（因 factor<=0 跳过）
        fcf_in_drivers = any(
            di.driver == TARGET_DRIVER_FCF_CONVERSION
            for di in scenario.driver_impacts
        )
        assert fcf_in_drivers is False

    def test_empty_shocks_returns_zero_impact(self) -> None:
        """空冲击列表返回零影响情景。"""
        engine = _build_stress_engine_with_evidence()
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="空冲击情景",
            scenario_description="无冲击基线对照",
            macro_shocks=[],
            baseline_forecast=_make_baseline_forecast(),
        )
        assert scenario.driver_impacts == ()
        assert scenario.revenue_impact_pct == 0.0
        assert scenario.profit_impact_pct == 0.0
        assert scenario.cashflow_impact_pct == 0.0
        assert scenario.valuation_impact_pct == 0.0
        assert scenario.risk_delta.score_delta == 0.0
        # forecast_before == forecast_after
        assert scenario.forecast_after.revenue == \
            scenario.forecast_before.revenue

    def test_skipped_no_path_for_unknown_variable(self) -> None:
        """未匹配到传导路径的宏观变量记录在 skipped_no_path。"""
        engine = _build_stress_engine_with_evidence()
        # 自定义一个没有传导路径的变量
        unknown_var = _make_macro_variable(
            name="nonexistent_macro_var",
            value=1.0,
            unit="index",
            source_ref="src:test:unknown",
        )
        shock = MacroShock(
            variable=unknown_var,
            shock_value=0.10,
            shock_unit="fraction",
            rationale="无路径变量测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="无路径变量情景",
            scenario_description="测试未匹配路径降级",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        assert "nonexistent_macro_var" in scenario.skipped_no_path
        assert scenario.driver_impacts == ()

    def test_correlation_only_paths_skipped_by_default(self) -> None:
        """默认 allow_correlation_only=False 时相关性观察路径被跳过。"""
        # 使用默认注册表（路径无证据，correlation_only=True）
        engine = build_default_stress_engine(
            horizon_months=12, allow_correlation_only=False
        )
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=0.50,
            shock_unit="%",
            rationale="相关性观察测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="相关性观察情景",
            scenario_description="测试相关性观察路径被跳过",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # 所有 policy_rate 路径都是 correlation_only=True → 被跳过
        assert scenario.driver_impacts == ()
        assert len(scenario.correlation_only_paths) > 0

    def test_correlation_only_allowed_when_flag_true(self) -> None:
        """allow_correlation_only=True 时相关性观察路径可参与传导。"""
        engine = build_default_stress_engine(
            horizon_months=12, allow_correlation_only=True
        )
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=0.50,
            shock_unit="%",
            rationale="允许相关性观察测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="允许相关性观察情景",
            scenario_description="测试显式允许相关性路径",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        # 允许相关性观察后应有 driver_impacts
        assert len(scenario.driver_impacts) > 0

    def test_direction_propagation_positive_and_negative(self) -> None:
        """正向冲击 + 负方向路径 → driver 下降；正向冲击 + 正方向路径 → driver 上升。"""
        # policy_rate → discount_rate (direction=+)
        # policy_rate → valuation_multiple (direction=-)
        # shock = +50bp (正向)
        engine = _build_stress_engine_with_evidence(horizon_months=12)
        var = _make_macro_variable(name="policy_rate", value=5.0)
        shock = MacroShock(
            variable=var,
            shock_value=0.50,
            shock_unit="%",
            rationale="方向传导测试",
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="方向传导测试情景",
            scenario_description="测试正负方向传导",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        dr_impact = next(
            di for di in scenario.driver_impacts
            if di.driver == TARGET_DRIVER_DISCOUNT_RATE
        )
        vm_impact = next(
            di for di in scenario.driver_impacts
            if di.driver == TARGET_DRIVER_VALUATION_MULTIPLE
        )
        # policy_rate 上升 → discount_rate 上升（+）
        assert dr_impact.impact_pct > 0
        # policy_rate 上升 → valuation_multiple 下降（-）
        assert vm_impact.impact_pct < 0

    def test_scenario_id_deterministic(self) -> None:
        """相同输入生成相同 scenario_id。"""
        engine = _build_stress_engine_with_evidence()
        var = _make_macro_variable()
        shock = MacroShock(
            variable=var, shock_value=0.50, shock_unit="%", rationale=""
        )
        common_kwargs = dict(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="确定性测试",
            scenario_description="",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        s1 = engine.stress(**common_kwargs)
        s2 = engine.stress(**common_kwargs)
        assert s1.scenario_id == s2.scenario_id

    def test_no_trade_action_in_output(self) -> None:
        """输出序列化结果不含交易动作关键词。"""
        engine = _build_stress_engine_with_evidence()
        var = _make_macro_variable()
        shock = MacroShock(
            variable=var, shock_value=0.50, shock_unit="%", rationale=""
        )
        scenario = engine.stress(
            symbol="600519.SH",
            industry="食品饮料",
            as_of="2024-12-31",
            scenario_name="合规测试",
            scenario_description="",
            macro_shocks=[shock],
            baseline_forecast=_make_baseline_forecast(),
        )
        serialized = scenario.to_dict()
        # 序列化结果中不应出现交易动作关键词
        import json
        text = json.dumps(serialized, ensure_ascii=False)
        forbidden = ("买入", "卖出", "止盈", "止损", "加仓", "减仓",
                     "建仓", "清仓", "调仓")
        for kw in forbidden:
            assert kw not in text, f"序列化结果含禁止关键词：{kw}"

    def test_missing_baseline_revenue_raises(self) -> None:
        """baseline_forecast.revenue <= 0 必须抛错。"""
        engine = _build_stress_engine_with_evidence()
        var = _make_macro_variable()
        shock = MacroShock(
            variable=var, shock_value=0.50, shock_unit="%", rationale=""
        )
        bad_baseline = ForecastSnapshot(
            revenue=0.0,
            gross_margin=0.70,
            opex_ratio=0.20,
            net_profit=0.0,
            fcf=0.0,
            discount_rate=0.10,
            valuation_multiple=30.0,
            enterprise_value=0.0,
        )
        with pytest.raises(ScenarioContractError, match="revenue 必须为正"):
            engine.stress(
                symbol="600519.SH",
                industry="食品饮料",
                as_of="2024-12-31",
                scenario_name="缺失基线测试",
                scenario_description="",
                macro_shocks=[shock],
                baseline_forecast=bad_baseline,
            )


# ---------------------------------------------------------------------------
# F. 传导引擎 lag_factor 单元测试
# ---------------------------------------------------------------------------


class TestTransmissionEngineLagFactor:
    """传导引擎 lag_factor 计算测试。"""

    def test_lag_factor_zero_horizon(self) -> None:
        """horizon=0 时 lag_factor=0。"""
        registry = _build_registry_with_evidence()
        engine = MacroTransmissionEngine(
            registry=registry, horizon_months=0
        )
        assert engine.lag_factor(0) == 0.0
        assert engine.lag_factor(12) == 0.0

    def test_lag_factor_full_when_lag_zero(self) -> None:
        """lag=0 时 lag_factor=1.0（当期完全兑现）。"""
        registry = _build_registry_with_evidence()
        engine = MacroTransmissionEngine(
            registry=registry, horizon_months=12
        )
        assert engine.lag_factor(0) == 1.0

    def test_lag_factor_partial(self) -> None:
        """lag=6, horizon=12 → lag_factor=0.5。"""
        registry = _build_registry_with_evidence()
        engine = MacroTransmissionEngine(
            registry=registry, horizon_months=12
        )
        assert engine.lag_factor(6) == pytest.approx(0.5)

    def test_lag_factor_zero_when_lag_exceeds_horizon(self) -> None:
        """lag >= horizon 时 lag_factor=0。"""
        registry = _build_registry_with_evidence()
        engine = MacroTransmissionEngine(
            registry=registry, horizon_months=12
        )
        assert engine.lag_factor(12) == 0.0
        assert engine.lag_factor(24) == 0.0
