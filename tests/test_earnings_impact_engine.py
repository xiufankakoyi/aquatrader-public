"""EarningsImpactEngine 盈利映射层专项测试。

覆盖：
- 三情景结果可从原文证据追溯到每项输入
- 缺份额/产能/ASP/利润率时安全返回缺口，不产生伪精确结果
- 未来财报、未来股本版本在历史 as_of 下不可见
- 单位换算、币种、期间和总股本版本测试
- 旧 valuation_bridge 启发式不能进入正式结果
- 区间传播
- 敏感性矩阵
- SQLite 版本化存储与迁移幂等
- API smoke
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from flask import Flask

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.earnings.contracts import (
    AssumptionSourceType,
    CapacityModel,
    DemandModel,
    DriverType,
    EarningsDriver,
    EarningsImpact,
    EarningsScenario,
    EarningsScenarioKind,
    EarningsStatus,
    ForecastFinancials,
    ForecastHorizon,
    ImpactAssumption,
    InputGap,
    IntegratedForecast,
    Interval,
    MarginModel,
    PricingModel,
    RECONCILIATION_TOLERANCE,
    ReconciliationCheck,
    ReconciliationStatus,
    RevenueExposure,
    RevenueExposureSource,
    SensitivityItem,
    ShareCapitalVersion,
    UnitKind,
)
from core.research.earnings.engine import (
    EarningsImpactEngine,
    EarningsImpactRequest,
    ForecastBuildRequest,
    IntegratedForecastEngine,
)
from core.research.earnings.interval import Interval as I
from core.research.earnings.pit import (
    capital_visible_as_of,
    filter_capitals_as_of,
    filter_inputs_as_of,
    input_visible_as_of,
)
from core.research.earnings.repository import EarningsRepository
from core.research_graph.valuation_bridge import ValuationBridge, ValuationSignal


# ---------- 辅助函数 ----------

def _make_complete_assumption(
    variable_name: str,
    low: float,
    high: float | None = None,
    unit: str = UnitKind.PIECES.value,
    source_claim_id: str = "claim_test",
    as_of: str = "2026-01-01T00:00:00+00:00",
    period: str = "2026Q1",
    published_at: str = "2025-12-01T00:00:00+00:00",
    retrieved_at: str = "2025-12-15T00:00:00+00:00",
) -> ImpactAssumption:
    """构造完整计算假设。"""
    return ImpactAssumption(
        variable_name=variable_name,
        value=I.from_pair(low, high),
        unit=unit,
        currency="CNY",
        period=period,
        as_of=as_of,
        data_version="v1",
        source_claim_id=source_claim_id,
        source_evidence_id="ev_test",
        extraction_method="manual",
        confidence=I.from_pair(0.8, 0.95),
        published_at=published_at,
        retrieved_at=retrieved_at,
        source_kind="company_disclosure",
        source_type=AssumptionSourceType.DISCLOSED.value,
    )


def _make_complete_request(
    as_of: str = "2026-01-01T00:00:00+00:00",
    market_share_low: float = 0.15,
    market_share_high: float = 0.20,
) -> EarningsImpactRequest:
    """构造完整盈利测算请求。"""
    driver = EarningsDriver(
        company_symbol="600000.SH",
        company_name="测试公司",
        product_name="测试产品",
        period="2026Q1",
        driver_type=DriverType.DEMAND_EXPANSION.value,
        claim_ids=["claim_test"],
    )
    exposure = RevenueExposure(
        driver_id=driver.driver_id,
        company_symbol="600000.SH",
        product_name="测试产品",
        exposure_ratio=I.from_pair(0.15, 0.20),
        revenue_base=_make_complete_assumption(
            "revenue_base", 1_000_000_000, unit=UnitKind.YUAN.value
        ),
        source_kind=RevenueExposureSource.COMPANY_DISCLOSURE.value,
        source_claim_id="claim_test",
        source_evidence_id="ev_test",
        as_of=as_of,
        period="2026Q1",
        data_version="v1",
        published_at="2025-12-01T00:00:00+00:00",
        retrieved_at="2025-12-15T00:00:00+00:00",
    )
    demand = DemandModel(
        driver_id=driver.driver_id,
        market_size=_make_complete_assumption(
            "market_size", 10_000_000, unit=UnitKind.PIECES.value
        ),
        per_unit_usage=_make_complete_assumption(
            "per_unit_usage", 2.0, unit=UnitKind.PIECES.value
        ),
        market_growth_delta=I.from_pair(0.20, 0.30),
        market_growth_delta_input=_make_complete_assumption(
            "market_growth_delta", 0.20, 0.30, unit=UnitKind.RATIO.value
        ),
        market_growth_delta_kind="rate",
    )
    capacity = CapacityModel(
        driver_id=driver.driver_id,
        nameplate_capacity=_make_complete_assumption(
            "nameplate_capacity", 5_000_000, unit=UnitKind.PIECES.value
        ),
        effective_capacity=_make_complete_assumption(
            "effective_capacity", 4_000_000, unit=UnitKind.PIECES.value
        ),
        utilization_rate=_make_complete_assumption(
            "utilization_rate", 0.85, 0.90, unit=UnitKind.RATIO.value
        ),
        yield_rate=_make_complete_assumption(
            "yield_rate", 0.95, 0.98, unit=UnitKind.RATIO.value
        ),
    )
    pricing = PricingModel(
        driver_id=driver.driver_id,
        asp=_make_complete_assumption("asp", 100.0, 120.0, unit=UnitKind.YUAN.value),
        unit_cost=_make_complete_assumption("unit_cost", 60.0, 70.0, unit=UnitKind.YUAN.value),
        period="2026Q1",
    )
    margin = MarginModel(
        driver_id=driver.driver_id,
        gross_margin=_make_complete_assumption(
            "gross_margin", 0.35, 0.40, unit=UnitKind.RATIO.value
        ),
        expense_ratio=_make_complete_assumption(
            "expense_ratio", 0.10, 0.12, unit=UnitKind.RATIO.value
        ),
        tax_rate=_make_complete_assumption(
            "tax_rate", 0.15, 0.20, unit=UnitKind.RATIO.value
        ),
    )
    capital = ShareCapitalVersion(
        company_symbol="600000.SH",
        period="2026Q1",
        shares_outstanding=_make_complete_assumption(
            "shares_outstanding", 1_000_000_000, unit=UnitKind.PIECES.value
        ),
        as_of=as_of,
        source_claim_id="claim_test",
        source_evidence_id="ev_test",
        published_at="2025-12-01T00:00:00+00:00",
        retrieved_at="2025-12-15T00:00:00+00:00",
    )
    market_share = _make_complete_assumption(
        "market_share", market_share_low, market_share_high, unit=UnitKind.RATIO.value
    )
    return EarningsImpactRequest(
        driver=driver,
        exposure=exposure,
        demand=demand,
        capacity=capacity,
        pricing=pricing,
        margin=margin,
        capital_versions=[capital],
        market_share=market_share,
        as_of=as_of,
        historical=True,
    )


# ---------- 区间算术测试 ----------

class TestInterval:
    def test_add(self):
        assert (I(1, 2) + I(3, 4)).low == 4
        assert (I(1, 2) + I(3, 4)).high == 6

    def test_sub(self):
        assert (I(1, 2) - I(3, 4)).low == -3
        assert (I(1, 2) - I(3, 4)).high == -1

    def test_mul_positive(self):
        result = I(1, 2) * I(3, 4)
        assert result.low == 3
        assert result.high == 8

    def test_mul_negative(self):
        result = I(1, 2) * I(-4, -3)
        assert result.low == -8
        assert result.high == -3

    def test_minimum(self):
        result = I.minimum(I(1, 5), I(2, 3))
        assert result.low == 1
        assert result.high == 3

    def test_div(self):
        result = I(10, 20) / I(2, 4)
        assert result.low == 2.5
        assert result.high == 10.0

    def test_div_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError):
            I(1, 2) / I(-1, 1)

    def test_clamp(self):
        result = I(1, 10).clamp(floor=2, ceil=8)
        assert result.low == 2
        assert result.high == 8

    def test_point(self):
        result = I.point(5)
        assert result.is_point
        assert result.mid == 5

    def test_auto_swap(self):
        result = I(5, 1)
        assert result.low == 1
        assert result.high == 5

    def test_serialize_roundtrip(self):
        original = I(1.5, 2.5)
        d = original.to_dict()
        restored = I.from_dict(d)
        assert restored is not None
        assert restored.low == 1.5
        assert restored.high == 2.5

    def test_non_finite_endpoints_are_rejected(self):
        with pytest.raises(ValueError):
            I(float("inf"), 1.0)
        with pytest.raises(ValueError):
            I(1.0, float("-inf"))

    def test_malformed_payload_is_ignored(self):
        assert I.from_dict([]) is None
        assert I.from_dict({"low": "not-a-number", "high": 1}) is None
        assert I.from_dict({"mid": 1, "width": "not-a-number"}) is None


class TestSerializableBoundary:
    def test_non_mapping_payload_uses_dataclass_defaults(self):
        restored = EarningsDriver.from_dict(["not", "a", "mapping"])
        assert restored.company_symbol == ""
        assert restored.evidence_ids == []


# ---------- PIT 测试 ----------

class TestPointInTime:
    def test_future_input_blocked_in_historical_mode(self):
        """未来输入在历史 as_of 下不可见。"""
        future_input = _make_complete_assumption(
            "future_input",
            100,
            published_at="2026-06-01T00:00:00+00:00",
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        assert (
            input_visible_as_of(future_input, "2026-01-01T00:00:00+00:00", historical=True)
            is False
        )

    def test_past_input_visible_in_historical_mode(self):
        """过去输入在历史 as_of 下可见。"""
        past_input = _make_complete_assumption(
            "past_input",
            100,
            published_at="2025-06-01T00:00:00+00:00",
            retrieved_at="2025-06-15T00:00:00+00:00",
        )
        assert (
            input_visible_as_of(past_input, "2026-01-01T00:00:00+00:00", historical=True) is True
        )

    def test_time_unknown_input_blocked_in_historical_mode(self):
        """时间未知数据不得进入历史测算。"""
        unknown_input = ImpactAssumption(variable_name="unknown", value=I.point(100))
        assert (
            input_visible_as_of(unknown_input, "2026-01-01T00:00:00+00:00", historical=True)
            is False
        )

    def test_future_capital_blocked(self):
        """未来股本版本在历史 as_of 下不可见。"""
        future_capital = ShareCapitalVersion(
            company_symbol="600000.SH",
            shares_outstanding=_make_complete_assumption(
                "shares",
                1_000_000_000,
                published_at="2026-06-01T00:00:00+00:00",
                retrieved_at="2026-06-15T00:00:00+00:00",
            ),
            as_of="2026-06-01T00:00:00+00:00",
            published_at="2026-06-01T00:00:00+00:00",
        )
        assert (
            capital_visible_as_of(future_capital, "2026-01-01T00:00:00+00:00", historical=True)
            is False
        )

    def test_filter_inputs_as_of(self):
        """批量过滤输入，返回可见数和被拦截数。"""
        inputs = [
            _make_complete_assumption(
                "past",
                100,
                published_at="2025-06-01T00:00:00+00:00",
                retrieved_at="2025-06-15T00:00:00+00:00",
            ),
            _make_complete_assumption(
                "future",
                200,
                published_at="2026-06-01T00:00:00+00:00",
                retrieved_at="2026-06-15T00:00:00+00:00",
            ),
        ]
        visible, blocked = filter_inputs_as_of(
            inputs, "2026-01-01T00:00:00+00:00", historical=True
        )
        assert len(visible) == 1
        assert blocked == 1
        assert visible[0].variable_name == "past"

    def test_filter_capitals_as_of(self):
        """批量过滤股本版本。"""
        capitals = [
            ShareCapitalVersion(
                company_symbol="600000.SH",
                shares_outstanding=_make_complete_assumption(
                    "old_shares",
                    1_000_000_000,
                    published_at="2025-01-01T00:00:00+00:00",
                    retrieved_at="2025-01-15T00:00:00+00:00",
                ),
                as_of="2025-01-01T00:00:00+00:00",
                published_at="2025-01-01T00:00:00+00:00",
            ),
            ShareCapitalVersion(
                company_symbol="600000.SH",
                shares_outstanding=_make_complete_assumption(
                    "new_shares",
                    1_200_000_000,
                    published_at="2026-06-01T00:00:00+00:00",
                    retrieved_at="2026-06-15T00:00:00+00:00",
                ),
                as_of="2026-06-01T00:00:00+00:00",
                published_at="2026-06-01T00:00:00+00:00",
            ),
        ]
        visible, blocked = filter_capitals_as_of(
            capitals, "2026-01-01T00:00:00+00:00", historical=True
        )
        assert len(visible) == 1
        assert blocked == 1
        assert visible[0].shares_outstanding.variable_name == "old_shares"


# ---------- 缺输入测试 ----------

class TestInsufficientData:
    def test_missing_demand_returns_insufficient_data(self):
        """缺需求模型返回 insufficient_data。"""
        driver = EarningsDriver(company_symbol="600000.SH", company_name="测试公司")
        request = EarningsImpactRequest(driver=driver, as_of="2026-01-01T00:00:00+00:00")
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert len(result.data_gaps) > 0
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "demand_model" in gap_fields

    def test_missing_capacity_returns_gap(self):
        """缺产能模型返回缺口。"""
        request = _make_complete_request()
        request.capacity = None
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "capacity_model" in gap_fields

    def test_missing_asp_returns_gap(self):
        """缺 ASP 返回缺口。"""
        request = _make_complete_request()
        request.pricing = None
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "pricing_model" in gap_fields

    def test_missing_margin_returns_gap(self):
        """缺利润率模型返回缺口。"""
        request = _make_complete_request()
        request.margin = None
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "margin_model" in gap_fields

    def test_missing_capital_returns_gap(self):
        """缺股本返回缺口。"""
        request = _make_complete_request()
        request.capital_versions = []
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g.field_name for g in result.data_gaps}
        assert any("shares_outstanding" in f for f in gap_fields)

    def test_missing_source_returns_gap(self):
        """输入缺来源 Claim/Evidence 返回缺口。"""
        request = _make_complete_request()
        request.demand.market_size.source_claim_id = ""
        request.demand.market_size.source_evidence_id = ""
        result = EarningsImpactEngine().compute(request)
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "market_size" in gap_fields

    def test_no_fake_precision_on_missing(self):
        """缺输入时不产生伪精确结果。"""
        request = _make_complete_request()
        request.demand = None
        result = EarningsImpactEngine().compute(request)
        assert len(result.scenarios) == 0
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value


# ---------- 三情景计算测试 ----------

class TestScenarioComputation:
    def test_three_scenarios_generated(self):
        """完整输入时生成三情景。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        assert result.status in {
            EarningsStatus.OK.value,
            EarningsStatus.DEGRADED.value,
            EarningsStatus.PARTIAL.value,
        }
        assert len(result.scenarios) == 3
        kinds = {s.kind for s in result.scenarios}
        assert kinds == {
            EarningsScenarioKind.BEARISH.value,
            EarningsScenarioKind.BASELINE.value,
            EarningsScenarioKind.BULLISH.value,
        }

    def test_bearish_eps_lower_than_bullish(self):
        """悲观情景 EPS 低于乐观情景。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        bear = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BEARISH.value
        )
        bull = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BULLISH.value
        )
        assert bear.eps_impact.mid <= bull.eps_impact.mid

    def test_interval_propagation(self):
        """区间传播：三情景共同体现区间宽度，单情景为端点/中点。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        bear = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BEARISH.value
        )
        base = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BASELINE.value
        )
        bull = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BULLISH.value
        )
        # 三情景覆盖区间，整体宽度大于 0
        assert bear.eps_impact.mid <= base.eps_impact.mid <= bull.eps_impact.mid
        assert bull.incremental_revenue.mid >= bear.incremental_revenue.mid
        assert base.eps_impact.width >= 0

    def test_formula_trace_exists(self):
        """公式追踪存在。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        baseline = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BASELINE.value
        )
        trace = baseline.formula_trace
        assert "new_demand" in trace
        assert "deliverable_volume" in trace
        assert "new_revenue" in trace
        assert "new_gross_profit" in trace
        assert "new_net_profit" in trace
        assert "eps_contribution" in trace
        assert "new_demand_formula" in trace
        assert "deliverable_formula" in trace

    def test_sensitivity_matrix_exists(self):
        """敏感性矩阵存在。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        assert len(result.sensitivity) > 0
        input_names = {item.input_name for item in result.sensitivity}
        assert "market_size" in input_names
        assert "asp" in input_names
        assert "gross_margin" in input_names

    def test_inputs_traceable_to_source(self):
        """三情景结果可从原文证据追溯到每项输入。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        snapshot = result.base_inputs_snapshot
        assert "demand" in snapshot
        assert snapshot["demand"]["market_size"]["source_claim_id"] == "claim_test"
        assert snapshot["demand"]["market_size"]["source_evidence_id"] == "ev_test"
        assert "exposure" in snapshot
        assert snapshot["exposure"]["source_claim_id"] == "claim_test"

    def test_deliverable_volume_is_min_of_demand_and_capacity(self):
        """可兑现销量 = min(需求×份额, 产能×利用率×良率)。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        baseline = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BASELINE.value
        )
        trace = baseline.formula_trace
        assert trace["new_demand"]["low"] > 0
        assert trace["capacity_volume"]["low"] > 0
        deliverable = trace["deliverable_volume"]
        assert deliverable["high"] <= max(
            trace["new_demand"]["high"], trace["capacity_volume"]["high"]
        )


# ---------- valuation_bridge 启发式硬阻断测试 ----------

class TestValuationBridgeHeuristicBlocked:
    def test_valuation_signal_is_exploration_hypothesis(self):
        """ValuationSignal 始终标记为探索假设。"""
        signal = ValuationSignal(neuron_id="n1", company_symbol="A公司", revenue_exposure=0.5)
        assert signal.is_exploration_hypothesis is True
        d = signal.to_dict()
        assert d["is_exploration_hypothesis"] is True

    def test_bridge_report_is_exploration_hypothesis(self):
        """ValuationBridgeReport 标记为探索假设。"""
        bridge = ValuationBridge()
        neuron = type(
            "N",
            (),
            {
                "neuron_id": "n1",
                "canonical_name": "A公司",
                "node_type": "COMPANY",
                "synapses": [],
            },
        )()
        report = bridge.analyze(neuron, [])
        assert report.is_exploration_hypothesis is True
        d = report.to_dict()
        assert d["is_exploration_hypothesis"] is True
        assert "warning" in d

    def test_exploration_hypothesis_blocked_from_formal_result(self):
        """探索假设硬阻断进入正式 EarningsImpact。"""
        request = _make_complete_request()
        request.exposure.is_exploration_hypothesis = True
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value

    def test_ineligible_source_kind_blocked(self):
        """来源类型不接受时硬阻断。"""
        request = _make_complete_request()
        request.exposure.source_kind = "graph_heuristic"
        request.exposure.source_claim_id = ""
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value


# ---------- SQLite 版本化存储测试 ----------

class TestRepository:
    @pytest.fixture
    def repo(self, tmp_path):
        return EarningsRepository(tmp_path / "earnings.db")

    def test_migration_idempotent(self, repo):
        """迁移幂等。"""
        repo.migrate()
        repo.migrate()
        with sqlite3.connect(repo.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {
            "earnings_drivers",
            "earnings_assumptions",
            "earnings_exposures",
            "earnings_capital_versions",
            "earnings_models",
            "earnings_scenarios",
            "earnings_impacts",
            "earnings_gaps",
            "earnings_state_audit",
        } <= tables

    def test_save_and_get_driver(self, repo):
        driver = EarningsDriver(company_symbol="600000.SH", company_name="测试公司")
        repo.save_driver(driver)
        got = repo.get_driver(driver.driver_id)
        assert got is not None
        assert got.company_symbol == "600000.SH"

    def test_save_and_get_assumption(self, repo):
        item = _make_complete_assumption("test_assumption", 100, 200)
        repo.save_assumption(item)
        got = repo.get_assumption(item.assumption_id)
        assert got is not None
        assert got.variable_name == "test_assumption"
        assert got.value.low == 100
        assert got.value.high == 200

    def test_save_and_get_assumption_compat_alias(self, repo):
        """save_input/get_input 兼容别名仍可工作。"""
        item = _make_complete_assumption("compat_input", 10, 20)
        repo.save_input(item)
        got = repo.get_input(item.assumption_id)
        assert got is not None
        assert got.variable_name == "compat_input"

    def test_save_and_get_impact(self, repo):
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        repo.save_impact(result)
        got = repo.get_impact(result.impact_id)
        assert got is not None
        assert got.status == result.status
        assert len(got.scenarios) == 3

    def test_save_and_get_result_compat_alias(self, repo):
        """save_result/get_result 兼容别名仍可工作。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        repo.save_result(result)
        got = repo.get_result(result.impact_id)
        assert got is not None
        assert got.impact_id == result.impact_id

    def test_list_impacts_by_status(self, repo):
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        repo.save_impact(result)
        items = repo.list_impacts(status=result.status)
        assert len(items) >= 1

    def test_audit_recorded(self, repo):
        """状态审计记录。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        repo.save_impact(result)
        audit = repo.list_audit(entity_type="impact")
        assert len(audit) >= 1
        assert audit[0]["entity_id"] == result.impact_id

    def test_stats(self, repo):
        stats = repo.stats()
        # P1.4 将 schema_version 从 3 升级到 4（新增 earnings_forecasts 表）
        assert stats["schema_version"] == 4
        assert "counts" in stats

    def test_serialize_roundtrip(self, repo):
        """序列化往返测试。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        repo.save_impact(result)
        got = repo.get_impact(result.impact_id)
        assert got is not None
        assert isinstance(got.scenarios, list)
        assert len(got.scenarios) == 3
        assert isinstance(got.scenarios[0], EarningsScenario)
        assert isinstance(got.scenarios[0].incremental_revenue, Interval)
        assert isinstance(got.sensitivity, list)
        if got.sensitivity:
            assert isinstance(got.sensitivity[0], SensitivityItem)
        assert isinstance(got.data_gaps, list)

    def test_save_impact_keeps_same_kind_scenarios_distinct(self, repo):
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        result.scenarios = [
            EarningsScenario(kind="baseline"),
            EarningsScenario(kind="baseline"),
        ]
        repo.save_impact(result)
        scenario_ids = tuple(scenario.scenario_id for scenario in result.scenarios)
        with sqlite3.connect(repo.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM earnings_scenarios WHERE scenario_id IN (?, ?)",
                scenario_ids,
            ).fetchone()[0]
        assert count == 2

    def test_malformed_persisted_payload_is_skipped(self, repo):
        driver = EarningsDriver(company_symbol="600000.SH")
        repo.save_driver(driver)
        with sqlite3.connect(repo.db_path) as conn:
            conn.execute(
                "UPDATE earnings_drivers SET payload_json=? WHERE driver_id=?",
                ("not-json", driver.driver_id),
            )
        assert repo.get_driver(driver.driver_id) is None
        assert repo.list_drivers() == []

    def test_invalid_list_limit_falls_back_to_default(self, repo):
        assert repo.list_drivers(limit="invalid") == []


# ---------- 单位换算测试 ----------

class TestUnitConversion:
    def test_tons_to_kg(self):
        from core.research.earnings.contracts import convert_unit

        result, ok = convert_unit(I.point(2.0), UnitKind.TONS.value, UnitKind.KG.value)
        assert ok
        assert result.low == 2000.0

    def test_wan_yuan_to_yuan(self):
        from core.research.earnings.contracts import convert_unit

        result, ok = convert_unit(I.point(100.0), UnitKind.WAN_YUAN.value, UnitKind.YUAN.value)
        assert ok
        assert result.low == 1_000_000.0

    def test_percent_to_ratio(self):
        from core.research.earnings.contracts import convert_unit

        result, ok = convert_unit(I.point(35.0), UnitKind.PERCENT.value, UnitKind.RATIO.value)
        assert ok
        assert abs(result.low - 0.35) < 1e-6

    def test_incompatible_units_not_converted(self):
        from core.research.earnings.contracts import convert_unit

        result, ok = convert_unit(I.point(100.0), UnitKind.PIECES.value, UnitKind.YUAN.value)
        assert ok is False
        assert result.low == 100.0


# ---------- 币种与期间测试 ----------

class TestCurrencyAndPeriod:
    def test_currency_preserved(self):
        """币种在结果中保留。"""
        request = _make_complete_request()
        request.exposure.revenue_base.currency = "USD"
        request.pricing.asp.currency = "USD"
        request.pricing.unit_cost.currency = "USD"
        result = EarningsImpactEngine().compute(request)
        assert result.currency == "USD"

    def test_period_preserved(self):
        """期间在结果中保留。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        assert result.period == "2026Q1"


# ---------- API smoke 测试 ----------

class TestApiSmoke:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EARNINGS_DB_PATH", str(tmp_path / "earnings.db"))
        import server.routes.earnings_routes as mod

        mod._repository = None
        mod._engine = None
        app = Flask(__name__)
        app.register_blueprint(earnings_bp)
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def test_health(self, client):
        resp = client.get("/api/research/earnings/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "ok"
        assert data["data"]["module"] == "earnings_impact"
        # P1.4 将 schema_version 从 3 升级到 4（新增 earnings_forecasts 表）
        assert data["data"]["schema_version"] == 4

    def test_compute_insufficient_data(self, client):
        """缺输入时 compute 返回 insufficient_data，不崩溃。"""
        resp = client.post(
            "/api/research/earnings/compute",
            json={
                "driver": {"company_symbol": "600000.SH", "company_name": "测试公司"},
                "as_of": "2026-01-01T00:00:00+00:00",
                "save": False,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == EarningsStatus.INSUFFICIENT_DATA.value
        assert len(data["data"]["data_gaps"]) > 0

    def test_compute_full(self, client):
        """完整输入时 compute 返回三情景。"""
        request = _make_complete_request()
        payload = {
            "driver": request.driver.to_dict(),
            "exposure": request.exposure.to_dict(),
            "demand": request.demand.to_dict(),
            "capacity": request.capacity.to_dict(),
            "pricing": request.pricing.to_dict(),
            "margin": request.margin.to_dict(),
            "capital_versions": [c.to_dict() for c in request.capital_versions],
            "market_share": request.market_share.to_dict(),
            "as_of": request.as_of,
            "historical": True,
            "save": False,
        }
        resp = client.post("/api/research/earnings/compute", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] in {
            EarningsStatus.OK.value,
            EarningsStatus.DEGRADED.value,
            EarningsStatus.PARTIAL.value,
        }
        assert len(data["data"]["scenarios"]) == 3

    def test_results_list(self, client):
        """结果列表不崩溃。"""
        resp = client.get("/api/research/earnings/results")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_impacts_list(self, client):
        """impacts 新端点不崩溃。"""
        resp = client.get("/api/research/earnings/impacts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_gaps_list(self, client):
        """缺口列表不崩溃。"""
        resp = client.get("/api/research/earnings/gaps")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_inputs_list(self, client):
        """输入列表（兼容别名）不崩溃。"""
        resp = client.get("/api/research/earnings/inputs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_assumptions_list(self, client):
        """assumptions 新端点不崩溃。"""
        resp = client.get("/api/research/earnings/assumptions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_models_list(self, client):
        """模型列表不崩溃。"""
        resp = client.get("/api/research/earnings/models")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_scenarios_list(self, client):
        """情景列表不崩溃。"""
        resp = client.get("/api/research/earnings/scenarios")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_missing_driver_returns_400(self, client):
        """缺 driver 字段返回 400。"""
        resp = client.post("/api/research/earnings/compute", json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "MISSING_DRIVER"

    def test_pit_future_capital_blocked_in_compute(self, client):
        """PIT: 未来股本在历史 as_of 下不可见，compute 返回 insufficient_data。"""
        request = _make_complete_request(as_of="2026-01-01T00:00:00+00:00")
        for cap in request.capital_versions:
            cap.published_at = "2026-06-01T00:00:00+00:00"
            cap.retrieved_at = "2026-06-15T00:00:00+00:00"
            cap.as_of = "2026-06-01T00:00:00+00:00"
        payload = {
            "driver": request.driver.to_dict(),
            "exposure": request.exposure.to_dict(),
            "demand": request.demand.to_dict(),
            "capacity": request.capacity.to_dict(),
            "pricing": request.pricing.to_dict(),
            "margin": request.margin.to_dict(),
            "capital_versions": [c.to_dict() for c in request.capital_versions],
            "market_share": request.market_share.to_dict(),
            "as_of": "2026-01-01T00:00:00+00:00",
            "historical": True,
            "save": False,
        }
        resp = client.post("/api/research/earnings/compute", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["status"] == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g["field_name"] for g in data["data"]["data_gaps"]}
        assert any("shares_outstanding" in f for f in gap_fields)


# ---------- 第二阶段严格契约回归 ----------

class TestStrictStage2Contract:
    """防止以默认值或量纲猜测生成伪精确盈利结果。"""

    def test_historical_missing_as_of_returns_insufficient_data(self):
        """historical=true 且缺 as_of 必须返回 insufficient_data + 审计 gap。"""
        driver = EarningsDriver(company_symbol="600000.SH", company_name="测试公司")
        request = EarningsImpactRequest(
            driver=driver,
            as_of="",
            historical=True,
        )
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g.field_name for g in result.data_gaps}
        assert "as_of" in gap_fields
        as_of_gap = next(g for g in result.data_gaps if g.field_name == "as_of")
        assert as_of_gap.severity == "blocker"
        assert "禁止回退当前时间" in as_of_gap.reason

    def test_historical_missing_as_of_does_not_fallback_to_now(self):
        """historical=true 且缺 as_of 时结果 as_of 必须为空，不能是当前时间。"""
        driver = EarningsDriver(company_symbol="600000.SH", company_name="测试公司")
        request = EarningsImpactRequest(
            driver=driver,
            as_of="",
            historical=True,
        )
        result = EarningsImpactEngine().compute(request)
        assert result.as_of == ""

    def test_non_historical_missing_as_of_allows_current_time(self):
        """非历史模式下缺 as_of 可以使用当前时间（允许）。"""
        driver = EarningsDriver(company_symbol="600000.SH", company_name="测试公司")
        request = EarningsImpactRequest(
            driver=driver,
            as_of="",
            historical=False,
        )
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert result.as_of != ""
        as_of_gaps = [g for g in result.data_gaps if g.field_name == "as_of"]
        assert len(as_of_gaps) == 0

    def test_future_noncapital_input_cannot_reach_compute(self):
        request = _make_complete_request(as_of="2026-01-01T00:00:00+00:00")
        request.pricing.asp.published_at = "2026-06-01T00:00:00+00:00"
        request.pricing.asp.retrieved_at = "2026-06-02T00:00:00+00:00"
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any("asp" in gap.field_name for gap in result.data_gaps)

    @pytest.mark.parametrize("field_name", ["yield_rate", "expense_ratio", "tax_rate"])
    def test_missing_financial_input_never_uses_hidden_default(self, field_name):
        request = _make_complete_request()
        owner = request.capacity if field_name == "yield_rate" else request.margin
        setattr(owner, field_name, ImpactAssumption(variable_name=field_name))
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any(field_name in gap.field_name for gap in result.data_gaps)

    def test_revenue_exposure_is_not_a_market_share_substitute(self):
        request = _make_complete_request()
        request.market_share = None
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any("market_share" in gap.field_name for gap in result.data_gaps)

    def test_growth_delta_semantics_must_be_explicit(self):
        request = _make_complete_request()
        assert hasattr(request.demand, "market_growth_delta_kind")
        request.demand.market_growth_delta_kind = "unknown"
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any("market_growth_delta_kind" in gap.field_name for gap in result.data_gaps)

    def test_currency_mismatch_requires_conversion_evidence(self):
        request = _make_complete_request()
        request.pricing.asp.currency = "USD"
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any("currency" in gap.field_name for gap in result.data_gaps)

    def test_incompatible_quantity_units_are_rejected(self):
        request = _make_complete_request()
        request.capacity.effective_capacity.unit = UnitKind.TONS.value
        result = EarningsImpactEngine().compute(request)
        assert result.status == EarningsStatus.INSUFFICIENT_DATA.value
        assert any("unit" in gap.field_name for gap in result.data_gaps)


# ---------- 模型容器持久化测试 ----------

class TestModelPersistence:
    """验证 Demand/Capacity/Pricing/Margin 模型容器真正版本化持久化。"""

    @pytest.fixture
    def repo(self, tmp_path):
        return EarningsRepository(tmp_path / "earnings.db")

    def test_save_and_get_demand_model(self, repo):
        """保存并查询需求模型容器。"""
        request = _make_complete_request()
        repo.save_demand_model(request.demand)
        got = repo.get_model(request.demand.model_id)
        assert got is not None
        assert got.model_id == request.demand.model_id
        assert got.driver_id == request.demand.driver_id
        assert got.market_size.variable_name == "market_size"
        assert got.market_growth_delta_kind == "rate"

    def test_save_and_get_capacity_model(self, repo):
        """保存并查询产能模型容器。"""
        request = _make_complete_request()
        repo.save_capacity_model(request.capacity)
        got = repo.get_model(request.capacity.model_id)
        assert got is not None
        assert got.model_id == request.capacity.model_id
        assert got.effective_capacity.variable_name == "effective_capacity"
        assert got.yield_rate.variable_name == "yield_rate"

    def test_save_and_get_pricing_model(self, repo):
        """保存并查询定价模型容器。"""
        request = _make_complete_request()
        repo.save_pricing_model(request.pricing)
        got = repo.get_model(request.pricing.model_id)
        assert got is not None
        assert got.model_id == request.pricing.model_id
        assert got.asp.variable_name == "asp"
        assert got.unit_cost.variable_name == "unit_cost"

    def test_save_and_get_margin_model(self, repo):
        """保存并查询利润率模型容器。"""
        request = _make_complete_request()
        repo.save_margin_model(request.margin)
        got = repo.get_model(request.margin.model_id)
        assert got is not None
        assert got.model_id == request.margin.model_id
        assert got.gross_margin.variable_name == "gross_margin"
        assert got.tax_rate.variable_name == "tax_rate"

    def test_list_models_by_driver(self, repo):
        """按 driver_id 查询所有模型容器。"""
        request = _make_complete_request()
        repo.save_demand_model(request.demand)
        repo.save_capacity_model(request.capacity)
        repo.save_pricing_model(request.pricing)
        repo.save_margin_model(request.margin)
        models = repo.list_models(driver_id=request.driver.driver_id)
        assert len(models) == 4
        type_names = {type(m).__name__ for m in models}
        assert type_names == {"DemandModel", "CapacityModel", "PricingModel", "MarginModel"}

    def test_list_models_by_type(self, repo):
        """按 model_type 过滤查询。"""
        request = _make_complete_request()
        repo.save_demand_model(request.demand)
        repo.save_capacity_model(request.capacity)
        demand_models = repo.list_models(driver_id=request.driver.driver_id, model_type="demand")
        assert len(demand_models) == 1
        assert isinstance(demand_models[0], DemandModel)
        capacity_models = repo.list_models(
            driver_id=request.driver.driver_id, model_type="capacity"
        )
        assert len(capacity_models) == 1
        assert isinstance(capacity_models[0], CapacityModel)

    def test_list_demand_models_shortcut(self, repo):
        """list_demand_models 快捷方法。"""
        request = _make_complete_request()
        repo.save_demand_model(request.demand)
        items = repo.list_demand_models(driver_id=request.driver.driver_id)
        assert len(items) == 1
        assert isinstance(items[0], DemandModel)

    def test_get_model_not_found(self, repo):
        """查询不存在的模型返回 None。"""
        assert repo.get_model("nonexistent") is None

    def test_model_persistence_idempotent(self, repo):
        """重复保存同一模型不创建多条记录。"""
        request = _make_complete_request()
        repo.save_demand_model(request.demand)
        repo.save_demand_model(request.demand)
        models = repo.list_models(driver_id=request.driver.driver_id, model_type="demand")
        assert len(models) == 1

    def test_model_v2_migration_adds_model_type_column(self, repo):
        """v2 迁移添加 model_type 列。"""
        with sqlite3.connect(repo.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_models)").fetchall()}
        assert "model_type" in cols
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='earnings_models'"
            ).fetchall()
        }
        assert "idx_earnings_models_model_type" in indexes


# ---------- 情景区间端点与敏感性重算测试 ----------

class TestScenarioIntervalAndSensitivity:
    """验证情景来自证据区间端点，敏感性真实重算。"""

    def test_bearish_uses_low_bullish_uses_high(self):
        """悲观情景使用区间下界，乐观情景使用区间上界。"""
        request = _make_complete_request()
        request.demand.market_growth_delta_input.value = I.from_pair(0.20, 0.30)
        request.pricing.asp.value = I.from_pair(100.0, 120.0)
        result = EarningsImpactEngine().compute(request)
        bear = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BEARISH.value
        )
        bull = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BULLISH.value
        )
        base = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BASELINE.value
        )
        assert bear.eps_impact.mid < bull.eps_impact.mid
        assert bear.eps_impact.mid <= base.eps_impact.mid <= bull.eps_impact.mid

    def test_point_interval_produces_identical_scenarios(self):
        """点区间（low=high）时三情景相同，不伪造差异。"""
        request = _make_complete_request()
        request.demand.market_growth_delta_input.value = I.point(0.25)
        request.demand.market_size.value = I.point(10_000_000)
        request.demand.per_unit_usage.value = I.point(2.0)
        request.market_share.value = I.point(0.18)
        request.capacity.effective_capacity.value = I.point(4_000_000)
        request.capacity.utilization_rate.value = I.point(0.88)
        request.capacity.yield_rate.value = I.point(0.96)
        request.pricing.asp.value = I.point(110.0)
        request.margin.gross_margin.value = I.point(0.38)
        request.margin.expense_ratio.value = I.point(0.11)
        request.margin.tax_rate.value = I.point(0.18)
        result = EarningsImpactEngine().compute(request)
        bear = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BEARISH.value
        )
        base = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BASELINE.value
        )
        bull = next(
            s for s in result.scenarios if s.kind == EarningsScenarioKind.BULLISH.value
        )
        assert bear.eps_impact.mid == base.eps_impact.mid == bull.eps_impact.mid

    def test_sensitivity_truly_recomputes(self):
        """敏感性矩阵是真实重算的结果，不是复用基准区间。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        assert len(result.sensitivity) > 0
        asp_sens = next(s for s in result.sensitivity if s.input_name == "asp")
        assert asp_sens.eps_impact_low != asp_sens.eps_impact_high
        assert asp_sens.elasticity != 0
        assert asp_sens.base_value == request.pricing.asp.value.mid

    def test_sensitivity_sorted_by_elasticity(self):
        """敏感性矩阵按弹性绝对值降序排列。"""
        request = _make_complete_request()
        result = EarningsImpactEngine().compute(request)
        elasticities = [abs(s.elasticity) for s in result.sensitivity]
        assert elasticities == sorted(elasticities, reverse=True)

    def test_sensitivity_skips_zero_base_value(self):
        """基准值为零的输入不进入敏感性矩阵。"""
        request = _make_complete_request()
        request.margin.expense_ratio.value = I.point(0.0)
        result = EarningsImpactEngine().compute(request)
        names = {s.input_name for s in result.sensitivity}
        assert "expense_ratio" not in names


# ---------- 模型查询 API smoke 测试 ----------

class TestModelQueryApiSmoke:
    """模型查询 API 端点 smoke 测试。"""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EARNINGS_DB_PATH", str(tmp_path / "earnings.db"))
        import server.routes.earnings_routes as mod

        mod._repository = None
        mod._engine = None
        app = Flask(__name__)
        app.register_blueprint(earnings_bp)
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def test_models_by_driver_missing_param(self, client):
        """缺 driver_id 参数返回 400。"""
        resp = client.get("/api/research/earnings/models/by_driver")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["error"]["code"] == "MISSING_DRIVER_ID"

    def test_models_by_driver_empty(self, client):
        """无模型时返回空列表。"""
        resp = client.get("/api/research/earnings/models/by_driver?driver_id=drv_test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"] == []

    def test_create_and_query_models_by_driver(self, client):
        """POST /models 创建带模型容器的驱动，GET /models/by_driver 查询回来。"""
        request = _make_complete_request()
        payload = {
            "driver": request.driver.to_dict(),
            "demand": request.demand.to_dict(),
            "capacity": request.capacity.to_dict(),
            "pricing": request.pricing.to_dict(),
            "margin": request.margin.to_dict(),
        }
        resp = client.post("/api/research/earnings/models", json=payload)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        models_saved = data["data"]["models_saved"]
        assert set(models_saved) == {"demand", "capacity", "pricing", "margin"}

        driver_id = data["data"]["driver"]["driver_id"]
        resp = client.get(f"/api/research/earnings/models/by_driver?driver_id={driver_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]) == 4

    def test_query_single_model(self, client):
        """GET /models/<model_id> 查询单个模型容器。"""
        request = _make_complete_request()
        payload = {
            "driver": request.driver.to_dict(),
            "demand": request.demand.to_dict(),
        }
        resp = client.post("/api/research/earnings/models", json=payload)
        driver_id = resp.get_json()["data"]["driver"]["driver_id"]
        resp = client.get(f"/api/research/earnings/models/by_driver?driver_id={driver_id}")
        model_id = resp.get_json()["data"][0]["model_id"]
        resp = client.get(f"/api/research/earnings/models/{model_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["model_id"] == model_id

    def test_query_model_not_found(self, client):
        """查询不存在的模型返回 404。"""
        resp = client.get("/api/research/earnings/models/nonexistent")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["error"]["code"] == "MODEL_NOT_FOUND"

    def test_compute_persists_models(self, client):
        """compute 端点 save=true 时持久化模型容器。"""
        request = _make_complete_request()
        payload = {
            "driver": request.driver.to_dict(),
            "exposure": request.exposure.to_dict(),
            "demand": request.demand.to_dict(),
            "capacity": request.capacity.to_dict(),
            "pricing": request.pricing.to_dict(),
            "margin": request.margin.to_dict(),
            "capital_versions": [c.to_dict() for c in request.capital_versions],
            "market_share": request.market_share.to_dict(),
            "as_of": request.as_of,
            "historical": True,
            "save": True,
        }
        resp = client.post("/api/research/earnings/compute", json=payload)
        assert resp.status_code == 200
        driver_id = resp.get_json()["data"]["driver_id"]
        resp = client.get(f"/api/research/earnings/models/by_driver?driver_id={driver_id}")
        assert resp.status_code == 200
        models = resp.get_json()["data"]
        assert len(models) == 4

    def test_historical_missing_as_of_via_api(self, client):
        """API: historical=true 且缺 as_of 返回 insufficient_data + as_of gap。"""
        resp = client.post(
            "/api/research/earnings/compute",
            json={
                "driver": {"company_symbol": "600000.SH", "company_name": "测试公司"},
                "historical": True,
                "as_of": "",
                "save": False,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["data"]["status"] == EarningsStatus.INSUFFICIENT_DATA.value
        gap_fields = {g["field_name"] for g in data["data"]["data_gaps"]}
        assert "as_of" in gap_fields
        assert data["data"]["as_of"] == ""

    def test_recompute_endpoint(self, client):
        """POST /recompute 基于变更假设重算。"""
        request = _make_complete_request()
        base_payload = {
            "driver": request.driver.to_dict(),
            "exposure": request.exposure.to_dict(),
            "demand": request.demand.to_dict(),
            "capacity": request.capacity.to_dict(),
            "pricing": request.pricing.to_dict(),
            "margin": request.margin.to_dict(),
            "capital_versions": [c.to_dict() for c in request.capital_versions],
            "market_share": request.market_share.to_dict(),
            "as_of": request.as_of,
            "historical": True,
        }
        changed = [
            {
                **request.pricing.asp.to_dict(),
                "value": I.from_pair(200.0, 220.0).to_dict(),
            }
        ]
        resp = client.post(
            "/api/research/earnings/recompute",
            json={"base_request": base_payload, "changed_assumptions": changed, "save": False},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["data"]["scenarios"]) == 3


# 导入 earnings_bp 用于测试
from server.routes.earnings_routes import earnings_bp


class TestEarningsFrontendEvidenceBoundary:
    """The manual UI must never manufacture evidence or quantitative inputs."""

    @staticmethod
    def _source() -> str:
        return (
            _PROJECT_ROOT / "myapp/src/components/earnings/EarningsMappingTab.vue"
        ).read_text(encoding="utf-8")

    def test_no_synthetic_provenance_or_hidden_numeric_defaults(self):
        source = self._source()
        forbidden = (
            "claim_web_input",
            "ev_web_input",
            "web-v1",
            "2025-12-01",
            "2025-12-15",
            "1_000_000_000",
            "* 1.25",
            "0.96",
            "* 0.6",
        )
        assert not [token for token in forbidden if token in source]

    def test_partial_exposure_does_not_emit_zero_placeholders(self):
        source = self._source()
        assert "exposureRatio || { low: 0, high: 0 }" not in source
        assert "revenueBaseInput || {" not in source
        assert "currency: 'CNY'" not in source
        assert "exposure_source_kind: 'company_disclosure'" not in source


# ============================================================================
# P1.4 三表联动盈利预测引擎测试
# 覆盖 8 项硬测试：
#   1. 三表勾稽（资产=负债+权益、现金勾稽、留存收益勾稽）
#   2. 现金不凭空产生（closing_cash = opening_cash + OCF + ICF + FCF）
#   3. 产能约束销量（segment_impacts 中的产能约束传递到 forecast）
#   4. 股本变化影响 EPS（不同 shares_outstanding 产生不同 eps）
#   5. 情景单调性（bearish EPS ≤ baseline EPS ≤ bullish EPS）
#   6. 重复收入阻断（同一产品多 impact 出现时产生 warning gap）
#   7. PIT 防泄漏（future 股本/输入在历史 as_of 下不可见）
#   8. 相同输入幂等、不同版本并存（save_forecast 版本冲突抛 ValueError）
# ============================================================================


# ---------- P1.4 辅助函数 ----------

def _make_balance_assumption(
    variable_name: str,
    low: float,
    high: float | None = None,
    unit: str = UnitKind.RATIO.value,
    as_of: str = "2026-01-01T00:00:00+00:00",
    period: str = "2026Q1",
    published_at: str = "2025-12-01T00:00:00+00:00",
    retrieved_at: str = "2025-12-15T00:00:00+00:00",
) -> ImpactAssumption:
    """构造完整的资产负债表/现金流量表假设。"""
    return ImpactAssumption(
        variable_name=variable_name,
        value=I.from_pair(low, high),
        unit=unit,
        currency="CNY",
        period=period,
        as_of=as_of,
        data_version="v1",
        source_claim_id="claim_test",
        source_evidence_id="ev_test",
        extraction_method="manual",
        confidence=I.from_pair(0.8, 0.95),
        published_at=published_at,
        retrieved_at=retrieved_at,
        source_kind="company_disclosure",
        source_type=AssumptionSourceType.DISCLOSED.value,
    )


def _make_segment_impact(
    symbol: str = "600000.SH",
    period: str = "2026Q1",
    product: str = "测试产品",
    driver_id: str = "drv_test_1",
    revenue: float = 1_000_000_000.0,
    gross_profit: float = 300_000_000.0,
    net_profit: float = 200_000_000.0,
    as_of: str = "2026-01-01T00:00:00+00:00",
) -> EarningsImpact:
    """构造已计算完成的 EarningsImpact（用作 forecast 的 segment 输入）。

    forecast 直接使用 segment_impacts 中 baseline 情景的 incremental_revenue/gross_profit/net_profit，
    不会重新计算产能约束（产能约束已在上游 EarningsImpactEngine 中应用）。
    """
    base = EarningsScenario(kind=EarningsScenarioKind.BASELINE.value)
    base.incremental_revenue = I.point(revenue)
    base.incremental_gross_profit = I.point(gross_profit)
    base.incremental_net_profit = I.point(net_profit)
    return EarningsImpact(
        driver_id=driver_id,
        symbol=symbol,
        company_name="测试公司",
        product=product,
        period=period,
        as_of=as_of,
        status=EarningsStatus.OK.value,
        scenarios=[base],
    )


def _make_complete_forecast_request(
    scenario: str = EarningsScenarioKind.BASELINE.value,
    shares_outstanding: float = 1_000_000_000.0,
    segment_revenue: float = 1_000_000_000.0,
    segment_gross_profit: float = 300_000_000.0,
    segment_net_profit: float = 200_000_000.0,
    as_of: str = "2026-01-01T00:00:00+00:00",
    period: str = "2026Q1",
    forecast_version: int = 1,
    historical: bool = True,
    capex_ratio_low: float = 0.08,
    capex_ratio_high: float = 0.12,
    dividend_payout_ratio_low: float = 0.20,
    dividend_payout_ratio_high: float = 0.40,
    tax_rate_low: float = 0.15,
    tax_rate_high: float = 0.25,
    financial_expense_low: float = 10_000_000.0,
    financial_expense_high: float = 30_000_000.0,
    shares_published_at: str = "2025-12-01T00:00:00+00:00",
    shares_retrieved_at: str = "2025-12-15T00:00:00+00:00",
    balance_inputs_override: dict[str, ImpactAssumption] | None = None,
) -> ForecastBuildRequest:
    """构造完整的 ForecastBuildRequest。"""
    impact = _make_segment_impact(
        revenue=segment_revenue,
        gross_profit=segment_gross_profit,
        net_profit=segment_net_profit,
        as_of=as_of,
        period=period,
    )
    balance_inputs: dict[str, ImpactAssumption] = {
        "dso_days": _make_balance_assumption("dso_days", 60, 90, unit="days", as_of=as_of, period=period),
        "dio_days": _make_balance_assumption("dio_days", 60, 90, unit="days", as_of=as_of, period=period),
        "dpo_days": _make_balance_assumption("dpo_days", 30, 45, unit="days", as_of=as_of, period=period),
        "capex_ratio": _make_balance_assumption(
            "capex_ratio", capex_ratio_low, capex_ratio_high, unit=UnitKind.RATIO.value, as_of=as_of, period=period
        ),
        "depreciation_ratio": _make_balance_assumption(
            "depreciation_ratio", 0.08, 0.12, unit=UnitKind.RATIO.value, as_of=as_of, period=period
        ),
        "tax_rate": _make_balance_assumption(
            "tax_rate", tax_rate_low, tax_rate_high, unit=UnitKind.RATIO.value, as_of=as_of, period=period
        ),
        "dividend_payout_ratio": _make_balance_assumption(
            "dividend_payout_ratio",
            dividend_payout_ratio_low,
            dividend_payout_ratio_high,
            unit=UnitKind.RATIO.value,
            as_of=as_of,
            period=period,
        ),
        "minority_ratio": _make_balance_assumption(
            "minority_ratio", 0.0, 0.05, unit=UnitKind.RATIO.value, as_of=as_of, period=period
        ),
        "financial_expense": _make_balance_assumption(
            "financial_expense",
            financial_expense_low,
            financial_expense_high,
            unit=UnitKind.YUAN.value,
            as_of=as_of,
            period=period,
        ),
        "share_change": _make_balance_assumption(
            "share_change", 0.0, 0.0, unit=UnitKind.YUAN.value, as_of=as_of, period=period
        ),
        "debt_change": _make_balance_assumption(
            "debt_change", 0.0, 0.0, unit=UnitKind.YUAN.value, as_of=as_of, period=period
        ),
    }
    if balance_inputs_override:
        balance_inputs.update(balance_inputs_override)

    share_capital = ShareCapitalVersion(
        company_symbol="600000.SH",
        period=period,
        shares_outstanding=_make_balance_assumption(
            "shares_outstanding",
            shares_outstanding,
            unit=UnitKind.PIECES.value,
            as_of=as_of,
            period=period,
            published_at=shares_published_at,
            retrieved_at=shares_retrieved_at,
        ),
        as_of=as_of,
        source_claim_id="claim_test",
        source_evidence_id="ev_test",
        published_at=shares_published_at,
        retrieved_at=shares_retrieved_at,
    )
    opening_balance = {
        "cash": 500_000_000.0,
        "accounts_receivable": 100_000_000.0,
        "inventory": 200_000_000.0,
        "fixed_assets": 1_000_000_000.0,
        "short_term_debt": 300_000_000.0,
        "long_term_debt": 500_000_000.0,
        # 期初平衡：负债 800M + 权益 1B = 资产 1.8B
        "total_liabilities": 800_000_000.0,
        "total_equity": 1_000_000_000.0,
        "equity_parent": 1_000_000_000.0,
        "total_assets": 1_800_000_000.0,
    }
    return ForecastBuildRequest(
        symbol="600000.SH",
        as_of=as_of,
        period=period,
        horizon=ForecastHorizon.ANNUAL.value,
        scenario=scenario,
        segment_impacts=[impact],
        balance_inputs=balance_inputs,
        opening_balance=opening_balance,
        share_capital=share_capital,
        capital_versions=[share_capital],
        historical=historical,
        forecast_version=forecast_version,
    )


# ---------- 1. 三表勾稽测试 ----------


class TestForecastReconciliation:
    """硬测试 1：三表勾稽。"""

    def test_balance_sheet_identity(self):
        """资产 = 负债 + 权益。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        assert forecast.financials.total_assets > 0
        expected = forecast.financials.total_liabilities + forecast.financials.total_equity
        assert abs(forecast.financials.total_assets - expected) <= RECONCILIATION_TOLERANCE, (
            f"资产 ≠ 负债+权益：assets={forecast.financials.total_assets}, "
            f"liab+equity={expected}"
        )

    def test_cash_flow_reconciliation(self):
        """closing_cash = opening_cash + OCF + ICF + FCF。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        f = forecast.financials
        expected = f.opening_cash + f.operating_cash_flow + f.investing_cash_flow + f.financing_cash_flow
        assert abs(f.closing_cash - expected) <= RECONCILIATION_TOLERANCE, (
            f"现金流不勾稽：closing={f.closing_cash}, expected={expected}"
        )

    def test_retained_earnings_linkage(self):
        """total_equity = opening_equity + net_profit - dividend（含少数股东损益）。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        f = forecast.financials
        opening_equity = request.opening_balance.get("total_equity", 0.0)
        # dividend 在 trace.balance_sheet 中记录
        dividend = forecast.formula_trace.get("balance_sheet", {}).get("dividend", 0.0)
        # total_equity 包含少数股东权益，故用 net_profit（含少数股东损益）
        expected = opening_equity + f.net_profit - dividend
        assert abs(f.total_equity - expected) <= RECONCILIATION_TOLERANCE, (
            f"留存收益不勾稽：total_equity={f.total_equity}, expected={expected}"
        )

    def test_all_reconciliation_checks_pass(self):
        """完整请求下所有勾稽检查项 status=ok。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        assert len(forecast.reconciliation_checks) == 3
        for check in forecast.reconciliation_checks:
            assert check.status == ReconciliationStatus.OK.value, (
                f"勾稽检查 {check.name} 未通过：{check.note}, "
                f"diff={check.difference}"
            )

    def test_reconciliation_check_names(self):
        """勾稽检查项名称正确。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        names = {c.name for c in forecast.reconciliation_checks}
        assert "balance_sheet_identity" in names
        assert "cash_flow_reconciliation" in names
        assert "retained_earnings_linkage" in names


# ---------- 2. 现金不凭空产生测试 ----------


class TestForecastCashNoMoneyFromNothing:
    """硬测试 2：现金不凭空产生。"""

    def test_closing_cash_equals_opening_plus_flows(self):
        """期末现金 = 期初现金 + OCF + ICF + FCF。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        f = forecast.financials
        flows = f.operating_cash_flow + f.investing_cash_flow + f.financing_cash_flow
        assert abs(f.closing_cash - f.opening_cash - flows) <= RECONCILIATION_TOLERANCE

    def test_zero_capex_and_dividend_preserves_cash(self):
        """capex_ratio=0 且 dividend_payout_ratio=0 时，ICF=0 且 FCF 不含分红。"""
        request = _make_complete_forecast_request(
            capex_ratio_low=0.0, capex_ratio_high=0.0,
            dividend_payout_ratio_low=0.0, dividend_payout_ratio_high=0.0,
        )
        forecast = IntegratedForecastEngine.build_forecast(request)
        f = forecast.financials
        assert abs(f.capex) <= RECONCILIATION_TOLERANCE
        # ICF = -capex = 0
        assert abs(f.investing_cash_flow) <= RECONCILIATION_TOLERANCE
        # 现金变化 = OCF + 0 + FCF
        flows = f.operating_cash_flow + f.financing_cash_flow
        assert abs(f.closing_cash - f.opening_cash - flows) <= RECONCILIATION_TOLERANCE

    def test_cash_flow_does_not_invent_external_sources(self):
        """现金流不凭空产生：closing = opening + 三流之和（绝对约束）。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        f = forecast.financials
        # 严格断言：closing_cash 必须严格等于 opening + 三流之和
        assert f.closing_cash == pytest.approx(
            f.opening_cash + f.operating_cash_flow + f.investing_cash_flow + f.financing_cash_flow,
            abs=RECONCILIATION_TOLERANCE,
        )


# ---------- 3. 产能约束销量测试 ----------


class TestForecastCapacityConstraint:
    """硬测试 3：产能约束销量。

    forecast 的 revenue 来自 segment_impacts 的 baseline 情景，
    segment_impacts 是上游 EarningsImpactEngine 计算结果（已反映产能约束）。
    forecast 不应补回被产能约束截断的销量。
    """

    def test_segment_revenue_aggregated(self):
        """多 segment 收入汇总到公司级 revenue。"""
        impact1 = _make_segment_impact(
            product="产品A", driver_id="drv_a", revenue=500_000_000, gross_profit=150_000_000, net_profit=100_000_000
        )
        impact2 = _make_segment_impact(
            product="产品B", driver_id="drv_b", revenue=300_000_000, gross_profit=90_000_000, net_profit=60_000_000
        )
        request = _make_complete_forecast_request()
        request.segment_impacts = [impact1, impact2]
        forecast = IntegratedForecastEngine.build_forecast(request)
        assert forecast.financials.revenue == pytest.approx(800_000_000, abs=1.0)
        assert forecast.financials.gross_profit == pytest.approx(240_000_000, abs=1.0)

    def test_capacity_constrained_revenue_not_restored(self):
        """若 segment 的 incremental_revenue 已被产能约束到 800M，forecast revenue 应等于 800M。"""
        # 构造一个 segment impact，revenue 已被约束为 800M（不是 1B）
        constrained_impact = _make_segment_impact(
            revenue=800_000_000, gross_profit=240_000_000, net_profit=160_000_000
        )
        request = _make_complete_forecast_request()
        request.segment_impacts = [constrained_impact]
        forecast = IntegratedForecastEngine.build_forecast(request)
        # forecast revenue 应等于约束后的 800M，不能"补回"到 1B
        assert forecast.financials.revenue == pytest.approx(800_000_000, abs=1.0)

    def test_segment_summary_recorded_in_trace(self):
        """segment 汇总明细记录在 formula_trace 中。"""
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        segment_summary = forecast.formula_trace.get("segment_summary", [])
        assert len(segment_summary) == 1
        assert segment_summary[0]["revenue"] == pytest.approx(1_000_000_000, abs=1.0)


# ---------- 4. 股本变化影响 EPS 测试 ----------


class TestForecastShareCapitalEPS:
    """硬测试 4：股本变化影响 EPS。"""

    def test_shares_outstanding_affects_eps(self):
        """不同 shares_outstanding 产生不同 eps。"""
        request_small = _make_complete_forecast_request(shares_outstanding=1_000_000_000)
        request_large = _make_complete_forecast_request(shares_outstanding=2_000_000_000)
        forecast_small = IntegratedForecastEngine.build_forecast(request_small)
        forecast_large = IntegratedForecastEngine.build_forecast(request_large)
        eps_small = forecast_small.financials.eps
        eps_large = forecast_large.financials.eps
        assert eps_small > 0
        assert eps_large > 0
        # 股本翻倍，EPS 减半
        assert eps_large == pytest.approx(eps_small / 2.0, rel=1e-6)

    def test_zero_shares_zero_eps(self):
        """shares_outstanding=0 时 eps=0（不产生除零异常）。"""
        request = _make_complete_forecast_request(shares_outstanding=0.0)
        # shares=0 会导致 _select_latest_capital 仍然返回 capital（因为 shares_outstanding.is_complete() 检查 unit/period/as_of 等）
        # 但 shares_outstanding_value = max(0.0, 0.0) = 0.0
        forecast = IntegratedForecastEngine.build_forecast(request)
        # 如果 capital 被判定为不完整，会产生 blocker gap，但 forecast 仍可能返回
        # 这里只需验证 eps=0 不产生异常
        assert forecast.financials.eps == 0.0

    def test_eps_uses_pit_visible_capital(self):
        """EPS 使用 PIT 可见的最新股本版本。"""
        # 构造两个股本版本，一个旧（1B 股）一个新（1.5B 股）
        # as_of=2026-01-01 时只能看到旧版本（新版本 published_at=2026-06-01）
        old_capital = ShareCapitalVersion(
            company_symbol="600000.SH",
            period="2026Q1",
            shares_outstanding=_make_balance_assumption(
                "shares_outstanding", 1_000_000_000, unit=UnitKind.PIECES.value,
                published_at="2025-12-01T00:00:00+00:00",
                retrieved_at="2025-12-15T00:00:00+00:00",
            ),
            as_of="2026-01-01T00:00:00+00:00",
            source_claim_id="claim_old",
            source_evidence_id="ev_old",
            published_at="2025-12-01T00:00:00+00:00",
            retrieved_at="2025-12-15T00:00:00+00:00",
        )
        new_capital = ShareCapitalVersion(
            company_symbol="600000.SH",
            period="2026Q1",
            shares_outstanding=_make_balance_assumption(
                "shares_outstanding", 1_500_000_000, unit=UnitKind.PIECES.value,
                published_at="2026-06-01T00:00:00+00:00",
                retrieved_at="2026-06-15T00:00:00+00:00",
            ),
            as_of="2026-06-01T00:00:00+00:00",
            source_claim_id="claim_new",
            source_evidence_id="ev_new",
            published_at="2026-06-01T00:00:00+00:00",
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        request = _make_complete_forecast_request(shares_outstanding=1_000_000_000)
        request.share_capital = old_capital
        request.capital_versions = [old_capital, new_capital]
        # historical=True，as_of=2026-01-01，新版本不可见
        forecast = IntegratedForecastEngine.build_forecast(request)
        # 应使用旧版本 1B 股，而非新版本 1.5B 股
        assert forecast.financials.shares_outstanding == pytest.approx(1_000_000_000, rel=1e-6)


# ---------- 5. 情景单调性测试 ----------


class TestForecastScenarioMonotonicity:
    """硬测试 5：情景单调性只在适用变量上成立。

    bearish: financial_expense/tax_rate/capex_ratio/depreciation_ratio/dividend_payout_ratio 取上界
    bullish: 这些反向变量取下界
    baseline: 取中点

    EPS 单调性：bearish ≤ baseline ≤ bullish
    （revenue 来自 segment_impacts 固定值，不受 scenario 影响；
    但 financial_expense/tax_rate/minority_ratio 会影响 net_profit_parent -> EPS）
    """

    def test_eps_monotonicity_bearish_baseline_bullish(self):
        """bearish EPS ≤ baseline EPS ≤ bullish EPS。"""
        bearish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BEARISH.value)
        baseline_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BASELINE.value)
        bullish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BULLISH.value)
        bearish = IntegratedForecastEngine.build_forecast(bearish_req)
        baseline = IntegratedForecastEngine.build_forecast(baseline_req)
        bullish = IntegratedForecastEngine.build_forecast(bullish_req)
        eps_bear = bearish.financials.eps
        eps_base = baseline.financials.eps
        eps_bull = bullish.financials.eps
        assert eps_bear <= eps_base + 1e-6, f"bearish {eps_bear} > baseline {eps_base}"
        assert eps_base <= eps_bull + 1e-6, f"baseline {eps_base} > bullish {eps_bull}"

    def test_net_profit_parent_monotonicity(self):
        """bearish net_profit_parent ≤ baseline ≤ bullish。"""
        bearish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BEARISH.value)
        baseline_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BASELINE.value)
        bullish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BULLISH.value)
        bearish = IntegratedForecastEngine.build_forecast(bearish_req)
        baseline = IntegratedForecastEngine.build_forecast(baseline_req)
        bullish = IntegratedForecastEngine.build_forecast(bullish_req)
        npp_bear = bearish.financials.net_profit_parent
        npp_base = baseline.financials.net_profit_parent
        npp_bull = bullish.financials.net_profit_parent
        assert npp_bear <= npp_base + 1e-6
        assert npp_base <= npp_bull + 1e-6

    def test_revenue_invariant_across_scenarios(self):
        """revenue 来自 segment_impacts（固定），不随 scenario 变化。"""
        bearish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BEARISH.value)
        bullish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BULLISH.value)
        bearish = IntegratedForecastEngine.build_forecast(bearish_req)
        bullish = IntegratedForecastEngine.build_forecast(bullish_req)
        # revenue 不受 scenario 影响
        assert bearish.financials.revenue == pytest.approx(bullish.financials.revenue, abs=1e-6)

    def test_financial_expense_scenario_direction(self):
        """financial_expense 是反向变量：bearish 取上界，bullish 取下界。"""
        bearish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BEARISH.value)
        bullish_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BULLISH.value)
        baseline_req = _make_complete_forecast_request(scenario=EarningsScenarioKind.BASELINE.value)
        bearish = IntegratedForecastEngine.build_forecast(bearish_req)
        bullish = IntegratedForecastEngine.build_forecast(bullish_req)
        baseline = IntegratedForecastEngine.build_forecast(baseline_req)
        # financial_expense: bearish >= baseline >= bullish
        assert bearish.financials.operating_profit <= baseline.financials.operating_profit + 1e-6
        assert baseline.financials.operating_profit <= bullish.financials.operating_profit + 1e-6


# ---------- 6. 重复收入阻断测试 ----------


class TestForecastDuplicateRevenue:
    """硬测试 6：重复收入阻断。"""

    def test_duplicate_product_same_driver_produces_warning(self):
        """同一 (symbol, period, product, driver_id) 在多 impact 中出现产生 warning gap。"""
        impact1 = _make_segment_impact(
            product="产品A", driver_id="drv_dup", revenue=500_000_000
        )
        impact2 = _make_segment_impact(
            product="产品A", driver_id="drv_dup", revenue=300_000_000
        )
        request = _make_complete_forecast_request()
        request.segment_impacts = [impact1, impact2]
        forecast = IntegratedForecastEngine.build_forecast(request)
        warning_gaps = [g for g in forecast.data_gaps if g.severity == "warning" and "duplicate" in g.field_name]
        assert len(warning_gaps) > 0, "应检测到重复收入并产生 warning gap"

    def test_different_products_no_duplicate_warning(self):
        """不同产品（不同 product_name）不产生重复收入 warning。"""
        impact1 = _make_segment_impact(
            product="产品A", driver_id="drv_a", revenue=500_000_000
        )
        impact2 = _make_segment_impact(
            product="产品B", driver_id="drv_b", revenue=300_000_000
        )
        request = _make_complete_forecast_request()
        request.segment_impacts = [impact1, impact2]
        forecast = IntegratedForecastEngine.build_forecast(request)
        warning_gaps = [g for g in forecast.data_gaps if g.severity == "warning" and "duplicate" in g.field_name]
        assert len(warning_gaps) == 0, "不同产品不应产生重复收入 warning"

    def test_same_product_different_driver_no_warning(self):
        """同一产品但不同 driver_id 不产生重复 warning（不同驱动可独立测算）。"""
        impact1 = _make_segment_impact(
            product="产品A", driver_id="drv_a", revenue=500_000_000
        )
        impact2 = _make_segment_impact(
            product="产品A", driver_id="drv_b", revenue=300_000_000
        )
        request = _make_complete_forecast_request()
        request.segment_impacts = [impact1, impact2]
        forecast = IntegratedForecastEngine.build_forecast(request)
        warning_gaps = [g for g in forecast.data_gaps if g.severity == "warning" and "duplicate" in g.field_name]
        assert len(warning_gaps) == 0


# ---------- 7. PIT 防泄漏测试 ----------


class TestForecastPITLeakage:
    """硬测试 7：PIT 防泄漏。"""

    def test_missing_as_of_blocked_in_historical_mode(self):
        """historical=true 缺 as_of 阻断，返回 insufficient_data。"""
        request = _make_complete_forecast_request(as_of="")
        forecast = IntegratedForecastEngine.build_forecast(request)
        assert forecast.data_version == "historical_missing_as_of"
        blocker_gaps = [g for g in forecast.data_gaps if g.severity == "blocker"]
        assert len(blocker_gaps) > 0
        assert any(g.field_name == "as_of" for g in blocker_gaps)

    def test_future_capital_blocked_in_historical_mode(self):
        """未来股本版本在历史 as_of 下不可见，产生 blocker。"""
        future_capital = ShareCapitalVersion(
            company_symbol="600000.SH",
            period="2026Q1",
            shares_outstanding=_make_balance_assumption(
                "shares_outstanding", 1_500_000_000, unit=UnitKind.PIECES.value,
                published_at="2026-06-01T00:00:00+00:00",
                retrieved_at="2026-06-15T00:00:00+00:00",
            ),
            as_of="2026-06-01T00:00:00+00:00",
            source_claim_id="claim_future",
            source_evidence_id="ev_future",
            published_at="2026-06-01T00:00:00+00:00",
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        request = _make_complete_forecast_request(as_of="2026-01-01T00:00:00+00:00")
        # 只提供未来股本版本
        request.share_capital = future_capital
        request.capital_versions = [future_capital]
        forecast = IntegratedForecastEngine.build_forecast(request)
        # 未来股本被 PIT 拦截，产生 blocker
        blocker_gaps = [g for g in forecast.data_gaps if g.severity == "blocker"]
        assert any("shares_outstanding" in g.field_name for g in blocker_gaps)

    def test_future_balance_input_blocked(self):
        """未来的 balance_input（如 capex_ratio）在历史 as_of 下不可见。"""
        future_capex = _make_balance_assumption(
            "capex_ratio", 0.10, 0.15,
            published_at="2026-06-01T00:00:00+00:00",
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        request = _make_complete_forecast_request(as_of="2026-01-01T00:00:00+00:00")
        request.balance_inputs["capex_ratio"] = future_capex
        forecast = IntegratedForecastEngine.build_forecast(request)
        # 未来 capex_ratio 被 PIT 拦截，导致必备输入缺失
        blocker_gaps = [g for g in forecast.data_gaps if g.severity == "blocker"]
        assert any(g.field_name == "capex_ratio" for g in blocker_gaps)

    def test_historical_false_allows_future_input(self):
        """historical=false 时未来输入可见（实时模式由调用方负责）。"""
        future_capex = _make_balance_assumption(
            "capex_ratio", 0.10, 0.15,
            published_at="2026-06-01T00:00:00+00:00",
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        request = _make_complete_forecast_request(
            as_of="2026-01-01T00:00:00+00:00", historical=False
        )
        request.balance_inputs["capex_ratio"] = future_capex
        forecast = IntegratedForecastEngine.build_forecast(request)
        # historical=false 不应因 PIT 拦截产生 capex_ratio blocker
        capex_blockers = [
            g for g in forecast.data_gaps
            if g.severity == "blocker" and g.field_name == "capex_ratio"
            and "不可见" in g.reason
        ]
        assert len(capex_blockers) == 0


# ---------- 8. 相同输入幂等、不同版本并存测试 ----------


class TestForecastRepository:
    """硬测试 8：相同输入幂等、不同版本并存。"""

    def test_save_and_get_forecast(self, tmp_path):
        """保存并按 forecast_id 查询预测。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        repo.save_forecast(forecast)
        retrieved = repo.get_forecast(forecast.forecast_id)
        assert retrieved is not None
        assert retrieved.forecast_id == forecast.forecast_id
        assert retrieved.financials.revenue == pytest.approx(forecast.financials.revenue, abs=1.0)

    def test_forecast_id_not_overwritable(self, tmp_path):
        """相同 forecast_id 重复保存抛 ValueError。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        request = _make_complete_forecast_request()
        forecast = IntegratedForecastEngine.build_forecast(request)
        repo.save_forecast(forecast)
        # 相同 forecast_id 再次保存应抛错
        with pytest.raises(ValueError, match="forecast_id"):
            repo.save_forecast(forecast)

    def test_same_version_key_not_overwritable(self, tmp_path):
        """相同 (symbol, period, scenario, model_version, forecast_version) 抛 ValueError。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        request = _make_complete_forecast_request(forecast_version=1)
        forecast1 = IntegratedForecastEngine.build_forecast(request)
        repo.save_forecast(forecast1)
        # 同版本键但不同 forecast_id 的预测不应能覆盖
        request2 = _make_complete_forecast_request(forecast_version=1)
        forecast2 = IntegratedForecastEngine.build_forecast(request2)
        with pytest.raises(ValueError, match="已存在"):
            repo.save_forecast(forecast2)

    def test_different_versions_coexist(self, tmp_path):
        """不同 forecast_version 可并存。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        request_v1 = _make_complete_forecast_request(forecast_version=1)
        forecast_v1 = IntegratedForecastEngine.build_forecast(request_v1)
        repo.save_forecast(forecast_v1)
        request_v2 = _make_complete_forecast_request(forecast_version=2)
        forecast_v2 = IntegratedForecastEngine.build_forecast(request_v2)
        repo.save_forecast(forecast_v2)  # 不同版本应能保存
        versions = repo.list_forecast_versions("600000.SH", "2026Q1")
        assert sorted(versions) == [1, 2]

    def test_list_forecasts_filter_by_symbol(self, tmp_path):
        """按 symbol 过滤查询预测列表。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        request = _make_complete_forecast_request(forecast_version=1)
        forecast = IntegratedForecastEngine.build_forecast(request)
        repo.save_forecast(forecast)
        items = repo.list_forecasts(symbol="600000.SH")
        assert len(items) == 1
        assert items[0].forecast_id == forecast.forecast_id
        # 不存在的公司
        empty = repo.list_forecasts(symbol="999999.SH")
        assert len(empty) == 0

    def test_get_latest_forecast_by_version(self, tmp_path):
        """get_latest_forecast 返回最新版本。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        for version in [1, 2, 3]:
            req = _make_complete_forecast_request(forecast_version=version)
            fcst = IntegratedForecastEngine.build_forecast(req)
            repo.save_forecast(fcst)
        latest = repo.get_latest_forecast("600000.SH", "2026Q1")
        assert latest is not None
        assert latest.forecast_version == 3

    def test_get_latest_forecast_pit_filter(self, tmp_path):
        """get_latest_forecast 按 forecast.as_of PIT 过滤。

        forecast.as_of 表示测算时点；查询时只返回 as_of <= 查询时点的预测。
        v1.as_of=2025-12-15，v2.as_of=2026-06-01；
        查询 as_of=2026-01-01 时只能看到 v1。
        """
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        # v1 测算时点在 2025-12
        req_v1 = _make_complete_forecast_request(
            forecast_version=1,
            as_of="2025-12-15T00:00:00+00:00",
        )
        forecast_v1 = IntegratedForecastEngine.build_forecast(req_v1)
        repo.save_forecast(forecast_v1)
        # v2 测算时点在 2026-06（晚于查询时点 2026-01-01）
        req_v2 = _make_complete_forecast_request(
            forecast_version=2,
            as_of="2026-06-01T00:00:00+00:00",
            shares_published_at="2026-06-01T00:00:00+00:00",
            shares_retrieved_at="2026-06-15T00:00:00+00:00",
        )
        forecast_v2 = IntegratedForecastEngine.build_forecast(req_v2)
        repo.save_forecast(forecast_v2)
        # 查询 as_of=2026-01-01，只能看到 v1（v2 测算时点晚于查询时点）
        latest_early = repo.get_latest_forecast("600000.SH", "2026Q1", as_of="2026-01-01T00:00:00+00:00")
        assert latest_early is not None
        assert latest_early.forecast_version == 1
        # 查询 as_of=2026-07-01，能看到 v2
        latest_late = repo.get_latest_forecast("600000.SH", "2026Q1", as_of="2026-07-01T00:00:00+00:00")
        assert latest_late is not None
        assert latest_late.forecast_version == 2

    def test_list_forecast_versions_sorted_asc(self, tmp_path):
        """list_forecast_versions 返回升序版本号列表。"""
        repo = EarningsRepository(db_path=str(tmp_path / "test_forecast.db"))
        # 逆序保存以验证排序
        for version in [3, 1, 2]:
            req = _make_complete_forecast_request(forecast_version=version)
            fcst = IntegratedForecastEngine.build_forecast(req)
            repo.save_forecast(fcst)
        versions = repo.list_forecast_versions("600000.SH", "2026Q1")
        assert versions == [1, 2, 3]

    def test_idempotent_input_snapshot(self):
        """相同输入产生相同的 input_snapshot（幂等追溯）。"""
        req1 = _make_complete_forecast_request()
        req2 = _make_complete_forecast_request()
        forecast1 = IntegratedForecastEngine.build_forecast(req1)
        forecast2 = IntegratedForecastEngine.build_forecast(req2)
        # input_snapshot 应一致（除 forecast_id/created_at 外）
        snap1 = forecast1.input_snapshot
        snap2 = forecast2.input_snapshot
        assert snap1["symbol"] == snap2["symbol"]
        assert snap1["period"] == snap2["period"]
        assert snap1["scenario"] == snap2["scenario"]
        assert snap1["forecast_version"] == snap2["forecast_version"]


# ---------- P1.4 API 端点测试 ----------


class TestForecastApi:
    """/api/research/earnings/forecasts/* 端点 smoke 测试。"""

    @staticmethod
    def _make_app(tmp_path) -> Flask:
        """构造测试 app，使用临时数据库。"""
        import server.routes.earnings_routes as mod
        # 重置全局 repository/engine 并注入临时 DB
        mod._repository = EarningsRepository(db_path=str(tmp_path / "test_api.db"))
        mod._engine = EarningsImpactEngine()
        app = Flask(__name__)
        app.register_blueprint(mod.earnings_bp)
        return app

    def test_forecast_build_endpoint(self, tmp_path):
        """POST /forecasts/build 构建预测。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            request_obj = _make_complete_forecast_request()
            payload = {
                "symbol": request_obj.symbol,
                "as_of": request_obj.as_of,
                "period": request_obj.period,
                "scenario": request_obj.scenario,
                "horizon": request_obj.horizon,
                "historical": request_obj.historical,
                "forecast_version": request_obj.forecast_version,
                "segment_impacts": [imp.to_dict() for imp in request_obj.segment_impacts],
                "balance_inputs": {k: v.to_dict() for k, v in request_obj.balance_inputs.items()},
                "opening_balance": request_obj.opening_balance,
                "share_capital": request_obj.share_capital.to_dict(),
                "capital_versions": [c.to_dict() for c in request_obj.capital_versions],
                "save": True,
            }
            resp = client.post("/api/research/earnings/forecasts/build", json=payload)
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["data"]["symbol"] == "600000.SH"
            assert data["data"]["financials"]["revenue"] > 0
            assert len(data["data"]["reconciliation_checks"]) == 3

    def test_forecast_build_missing_as_of(self, tmp_path):
        """historical=true 缺 as_of 返回 400。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/research/earnings/forecasts/build", json={
                "symbol": "600000.SH",
                "period": "2026Q1",
                "historical": True,
            })
            assert resp.status_code == 400
            data = resp.get_json()
            assert data["ok"] is False
            assert data["error"]["code"] == "MISSING_AS_OF"

    def test_forecast_build_missing_symbol(self, tmp_path):
        """缺 symbol 返回 400。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            resp = client.post("/api/research/earnings/forecasts/build", json={
                "as_of": "2026-01-01T00:00:00+00:00",
                "period": "2026Q1",
            })
            assert resp.status_code == 400
            assert resp.get_json()["error"]["code"] == "MISSING_SYMBOL"

    def test_forecast_list_and_detail(self, tmp_path):
        """GET /forecasts 列表查询 + GET /forecasts/<id> 详情。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            # 先构建一个预测
            request_obj = _make_complete_forecast_request()
            payload = {
                "symbol": request_obj.symbol,
                "as_of": request_obj.as_of,
                "period": request_obj.period,
                "scenario": request_obj.scenario,
                "segment_impacts": [imp.to_dict() for imp in request_obj.segment_impacts],
                "balance_inputs": {k: v.to_dict() for k, v in request_obj.balance_inputs.items()},
                "opening_balance": request_obj.opening_balance,
                "share_capital": request_obj.share_capital.to_dict(),
                "capital_versions": [c.to_dict() for c in request_obj.capital_versions],
                "save": True,
            }
            build_resp = client.post("/api/research/earnings/forecasts/build", json=payload)
            assert build_resp.status_code == 200
            forecast_id = build_resp.get_json()["data"]["forecast_id"]
            # 列表查询
            list_resp = client.get("/api/research/earnings/forecasts?symbol=600000.SH")
            assert list_resp.status_code == 200
            list_data = list_resp.get_json()["data"]
            assert len(list_data) >= 1
            # 详情查询
            detail_resp = client.get(f"/api/research/earnings/forecasts/{forecast_id}")
            assert detail_resp.status_code == 200
            assert detail_resp.get_json()["data"]["forecast_id"] == forecast_id
            # 不存在的 forecast_id
            not_found = client.get("/api/research/earnings/forecasts/nonexistent_id")
            assert not_found.status_code == 404

    def test_forecast_latest_endpoint(self, tmp_path):
        """GET /forecasts/latest 返回最新版本。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            for version in [1, 2]:
                request_obj = _make_complete_forecast_request(forecast_version=version)
                payload = {
                    "symbol": request_obj.symbol,
                    "as_of": request_obj.as_of,
                    "period": request_obj.period,
                    "scenario": request_obj.scenario,
                    "forecast_version": version,
                    "segment_impacts": [imp.to_dict() for imp in request_obj.segment_impacts],
                    "balance_inputs": {k: v.to_dict() for k, v in request_obj.balance_inputs.items()},
                    "opening_balance": request_obj.opening_balance,
                    "share_capital": request_obj.share_capital.to_dict(),
                    "capital_versions": [c.to_dict() for c in request_obj.capital_versions],
                    "save": True,
                }
                client.post("/api/research/earnings/forecasts/build", json=payload)
            # 查询最新
            resp = client.get("/api/research/earnings/forecasts/latest?symbol=600000.SH&period=2026Q1")
            assert resp.status_code == 200
            data = resp.get_json()["data"]
            assert data["forecast_version"] == 2

    def test_forecast_versions_endpoint(self, tmp_path):
        """GET /forecasts/versions 返回版本号列表。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            for version in [1, 2, 3]:
                request_obj = _make_complete_forecast_request(forecast_version=version)
                payload = {
                    "symbol": request_obj.symbol,
                    "as_of": request_obj.as_of,
                    "period": request_obj.period,
                    "scenario": request_obj.scenario,
                    "forecast_version": version,
                    "segment_impacts": [imp.to_dict() for imp in request_obj.segment_impacts],
                    "balance_inputs": {k: v.to_dict() for k, v in request_obj.balance_inputs.items()},
                    "opening_balance": request_obj.opening_balance,
                    "share_capital": request_obj.share_capital.to_dict(),
                    "capital_versions": [c.to_dict() for c in request_obj.capital_versions],
                    "save": True,
                }
                client.post("/api/research/earnings/forecasts/build", json=payload)
            resp = client.get("/api/research/earnings/forecasts/versions?symbol=600000.SH&period=2026Q1")
            assert resp.status_code == 200
            data = resp.get_json()["data"]
            assert sorted(data["versions"]) == [1, 2, 3]

    def test_forecast_build_version_conflict(self, tmp_path):
        """同版本重复保存返回 409。"""
        app = self._make_app(tmp_path)
        with app.test_client() as client:
            request_obj = _make_complete_forecast_request(forecast_version=1)
            payload = {
                "symbol": request_obj.symbol,
                "as_of": request_obj.as_of,
                "period": request_obj.period,
                "scenario": request_obj.scenario,
                "forecast_version": 1,
                "segment_impacts": [imp.to_dict() for imp in request_obj.segment_impacts],
                "balance_inputs": {k: v.to_dict() for k, v in request_obj.balance_inputs.items()},
                "opening_balance": request_obj.opening_balance,
                "share_capital": request_obj.share_capital.to_dict(),
                "capital_versions": [c.to_dict() for c in request_obj.capital_versions],
                "save": True,
            }
            resp1 = client.post("/api/research/earnings/forecasts/build", json=payload)
            assert resp1.status_code == 200
            # 同版本再次构建应返回 409
            resp2 = client.post("/api/research/earnings/forecasts/build", json=payload)
            assert resp2.status_code == 409
            assert resp2.get_json()["error"]["code"] == "FORECAST_VERSION_EXISTS"
