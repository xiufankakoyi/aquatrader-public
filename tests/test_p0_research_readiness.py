"""P0.2 ResearchReadiness 八维契约与边界测试。

按 docs/research/institutional/p0-contracts.md §4 与 plan P0.2 验证：
- 市场一致预期存在但公司前瞻驱动为空时，forecast_input_completeness 必须 blocked
- 估值缺失时 valuation_computability.score 必须为 None（不得为 0.5）
- 技术状态很强但事实为空时，technical 不参与 fact_completeness
- 所有来源为空时安全降级，research_uncertainty.score 必须为 1.0
- 未来可见数据被 PIT 过滤后 pit_integrity 不被打满
- 中际旭创样本（仅有 market_implied_scenarios）不再误导为前瞻驱动完整
"""
from __future__ import annotations

from typing import Any

import pytest

from core.research.value_chain.contracts import (
    RESEARCH_READINESS_DIMENSIONS,
    RESEARCH_READINESS_VALID_STATUSES,
    ReadinessDimension,
    ResearchReadiness,
    validate_assumption_source_type,
    validate_assumption_transition,
)
from core.research.value_chain.readiness import build_research_readiness


def _empty_inputs() -> dict[str, Any]:
    """返回所有字段均为空的 readiness 输入。"""
    return {
        "industry_context": {"chain_status": "missing", "verified_edges": [], "ontology_edges": []},
        "financial_quality": {"status": "not_computable", "metrics": {}},
        "profit_bridge": {"status": "not_computable", "market_implied_scenarios": []},
        "expectation_context": {"verified_expectations": [], "gaps": []},
        "valuation_context": {"status": "not_computable", "history": {}},
        "valuation_state": {"status": "not_computable"},
        "source_snapshots": {},
        "as_of": "2026-07-18",
    }


def _market_implied_only_inputs() -> dict[str, Any]:
    """返回只有 market_implied_scenarios、缺少前瞻驱动证据的输入。

    对应中际旭创现有样本场景：市场隐含约束已生成，但公司未披露前瞻数值。
    """
    inputs = _empty_inputs()
    inputs["profit_bridge"] = {
        "status": "market_implied",
        "market_implied_scenarios": [{
            "period": "2026Q4",
            "capacity_change_required": 0.4,
            "model_kind": "market_implied_constraint_scenario",
            "is_company_guidance": False,
            "updates_fact_confidence": False,
        }],
        "forward_driver_assumptions": [],
        "forward_disclosed_indicators": [{"direction": "increase", "eligible_as_forward_assumption": False}],
        "forward_guidance": [],
    }
    return inputs


# ---------------------------------------------------------------------------
# 维度字段不变量
# ---------------------------------------------------------------------------


def test_research_readiness_has_eight_dimensions() -> None:
    """build_research_readiness 必须返回 P0 计划 3.3 的八维 readiness。"""
    readiness = build_research_readiness(**_empty_inputs())
    payload = readiness.to_dict()
    assert set(payload.keys()) == set(RESEARCH_READINESS_DIMENSIONS)
    for name, dimension in payload.items():
        assert set(dimension.keys()) == {"status", "score", "missing_items", "evidence_refs"}, (
            f"dimension {name} has unexpected keys: {set(dimension.keys())}"
        )
        assert dimension["status"] in RESEARCH_READINESS_VALID_STATUSES
        assert isinstance(dimension["missing_items"], list)
        assert isinstance(dimension["evidence_refs"], list)


def test_research_readiness_score_never_neutral_when_missing() -> None:
    """所有来源为空时不可返回中性分；research_uncertainty.score 必须 1.0。"""
    readiness = build_research_readiness(**_empty_inputs())
    payload = readiness.to_dict()
    # 估值、前瞻、模型必须 None
    assert payload["valuation_computability"]["score"] is None
    assert payload["forecast_input_completeness"]["score"] is None
    assert payload["model_computability"]["score"] is None
    # research_uncertainty 必须 1.0（最高不确定性）
    assert payload["research_uncertainty"]["score"] == 1.0
    assert payload["research_uncertainty"]["status"] == "not_computable"
    # 不得出现 0.5 伪中性
    for name, dimension in payload.items():
        assert dimension["score"] != 0.5, f"{name}.score 不得为 0.5 伪中性"


# ---------------------------------------------------------------------------
# P0.2 边界规则
# ---------------------------------------------------------------------------


def test_market_implied_scenarios_do_not_boost_forecast_input_completeness() -> None:
    """市场隐含场景只能提升 model_computability.market_expectation_computability，
    不能提升 forecast_input_completeness。"""
    inputs = _market_implied_only_inputs()
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    # forecast_input_completeness 必须仍 blocked
    assert payload["forecast_input_completeness"]["status"] == "blocked"
    assert payload["forecast_input_completeness"]["score"] is None
    assert "market_implied_scenarios_not_counted_as_forecast_input" in (
        payload["forecast_input_completeness"]["missing_items"]
    )
    # model_computability 因 forecast_input blocked 必须 blocked
    assert payload["model_computability"]["status"] == "blocked"
    assert payload["model_computability"]["score"] is None


def test_valuation_missing_returns_null_score_not_neutral() -> None:
    """估值缺失时 valuation_computability.score 必须为 None，禁止 0.5 中性。"""
    inputs = _empty_inputs()
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    assert payload["valuation_computability"]["status"] == "blocked"
    assert payload["valuation_computability"]["score"] is None
    assert "valuation_history_missing" in payload["valuation_computability"]["missing_items"]


def test_technical_state_does_not_boost_fact_completeness() -> None:
    """技术状态很强但事实为空时，fact_completeness 仍必须 blocked。

    readiness 构建函数不接受 technical_context 参数，确保技术状态隔离。
    """
    inputs = _empty_inputs()
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    # fact_completeness 不得有 technical_* 字段
    assert "technical" not in " ".join(payload["fact_completeness"]["missing_items"]).lower()
    assert payload["fact_completeness"]["status"] == "blocked"
    assert payload["fact_completeness"]["score"] is None


def test_all_sources_missing_safely_degrades() -> None:
    """所有来源为空时 readiness 必须安全降级，不抛异常。"""
    inputs = _empty_inputs()
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    # 至少 4 个维度 blocked 或 not_computable
    blocked = [d for d in payload.values() if d["status"] in {"blocked", "not_computable"}]
    assert len(blocked) >= 4
    # 没有维度抛出异常字符串
    for name, dimension in payload.items():
        for item in dimension["missing_items"]:
            assert "exception" not in str(item).lower()
            assert "error" not in str(item).lower()


def test_readiness_skips_malformed_rows_and_keeps_zero_percentile() -> None:
    inputs = _empty_inputs()
    inputs["industry_context"] = {
        "verified_edges": [{"evidence": [None]}],
        "observed_facts": [None],
        "forward_plans": [None],
    }
    inputs["profit_bridge"] = {
        "forward_driver_assumptions": [None],
        "forward_disclosed_indicators": [None],
        "observed_historical_drivers": [None],
        "market_implied_scenarios": [],
        "forward_guidance": [],
    }
    inputs["source_snapshots"] = {
        "daily_bars": {
            "status": "ok",
            "records": [None],
            "excluded_future_records": "invalid",
            "excluded_unknown_time_records": None,
        },
    }
    inputs["valuation_context"] = {
        "status": "ok",
        "history": {"pe_ttm": {"percentile": 0}},
    }
    inputs["valuation_state"] = {"status": "ok"}

    payload = build_research_readiness(**inputs).to_dict()

    assert "valuation_percentile_missing:pe_ttm" not in payload["valuation_computability"]["missing_items"]


def test_pit_integrity_reflects_excluded_future_records() -> None:
    """PIT 过滤后的未来记录必须体现在 pit_integrity 维度。"""
    inputs = _empty_inputs()
    inputs["source_snapshots"] = {
        "announcements": {
            "status": "ok",
            "records": [{"title": "visible", "date": "2026-06-29"}],
            "excluded_future_records": 1,
            "excluded_unknown_time_records": 0,
        },
    }
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    assert payload["pit_integrity"]["status"] == "partial"
    assert "future_records_excluded:1" in payload["pit_integrity"]["missing_items"]


def test_pit_integrity_ok_when_no_exclusion() -> None:
    """无未来记录排除时 pit_integrity 必须 ok。"""
    inputs = _empty_inputs()
    inputs["source_snapshots"] = {
        "daily_bars": {
            "status": "ok",
            "records": [{"trade_date": "2026-07-01"}, {"trade_date": "2026-07-02"}],
            "excluded_future_records": 0,
            "excluded_unknown_time_records": 0,
        },
    }
    readiness = build_research_readiness(**inputs)
    payload = readiness.to_dict()
    assert payload["pit_integrity"]["status"] == "ok"


def test_research_readiness_attached_to_dossier() -> None:
    """CompanyDossier.research_readiness 必须被填充且为 ResearchReadiness 实例。"""
    from core.research.value_chain.contracts import CompanyDossier, ResearchReadiness

    dossier = CompanyDossier(symbol="000001.SZ")
    assert isinstance(dossier.research_readiness, ResearchReadiness)
    payload = dossier.to_dict()
    assert "research_readiness" in payload
    assert set(payload["research_readiness"].keys()) == set(RESEARCH_READINESS_DIMENSIONS)


def test_company_dossier_serialization_is_detached() -> None:
    from core.research.value_chain.contracts import CompanyDossier

    dossier = CompanyDossier(
        symbol="000001.SZ",
        company_profile={"industry": "test"},
        financial_history=[{"period": "2026-01-01"}],
    )

    payload = dossier.to_dict()
    payload["company_profile"]["industry"] = "changed"
    payload["financial_history"][0]["period"] = "changed"

    assert dossier.company_profile["industry"] == "test"
    assert dossier.financial_history[0]["period"] == "2026-01-01"


def test_research_assessment_includes_readiness_and_deprecation_notice() -> None:
    """build_research_assessment 必须包含 research_readiness 与 deprecation_notice。"""
    from core.research.value_chain.research_assessment import build_research_assessment

    result = build_research_assessment(
        industry_context={"chain_status": "verified", "verified_edges": [], "ontology_edges": []},
        financial_quality={"status": "ok", "metrics": {}},
        profit_bridge={
            "market_implied_scenarios": [{
                "period": "2027Q4", "capacity_change_required": 0.4,
                "model_kind": "market_implied_constraint_scenario",
            }],
            "forward_disclosed_indicators": [{"direction": "increase"}],
        },
        structured_consensus={"status": "verified", "periods": {}},
        valuation_context={"status": "ok", "history": {}},
        technical_context={"status": "ok", "technical_state": "TREND_BROKEN"},
        degradation_reasons=["company_forward_driver_evidence_missing"],
    )
    assert "research_readiness" in result
    assert "deprecation_notice" in result
    assert "deprecated" in result["deprecation_notice"]
    # 旧字段保留兼容
    assert "evidence_completeness" in result
    assert "dimension_ready" in result


def test_research_assessment_skips_malformed_nested_records() -> None:
    from core.research.value_chain.research_assessment import build_research_assessment

    result = build_research_assessment(
        industry_context={"verified_edges": [None], "ontology_edges": [None]},
        financial_quality={"status": "not_computable", "metrics": {"bad": None}},
        profit_bridge={
            "market_implied_scenarios": [None],
            "forward_disclosed_indicators": [None],
            "forward_driver_assumptions": [None],
            "forward_guidance": [],
        },
        structured_consensus={"status": "verified", "periods": {"2027Q4": None}},
        valuation_context={"status": "ok", "history": {"pe_ttm": None}},
        technical_context={"status": "ok"},
        degradation_reasons=["bad_input"],
    )

    assert result["execution_constraints"] == []
    assert result["consensus_dispersion"] == {}
    assert result["next_research_focus"][0]["gap"] == "bad_input"


def test_service_build_dossier_attaches_readiness(tmp_path) -> None:
    """CompanyValueResearchService.build_dossier 必须返回带 research_readiness 的 dossier。"""
    import pandas as pd

    class FakeRegistry:
        def __init__(self) -> None:
            self.logs: list[dict] = []

        def get_stock_basic_info(self, symbols=None):
            return pd.DataFrame([{
                "symbol": symbols[0], "stock_name": "测试公司", "industry": "测试行业",
                "main_business": "测试产品", "provider": "fake_profile",
            }])

        def get_daily_bars(self, start_date, end_date, symbols=None):
            rows = [
                {"trade_date": f"2026-06-{day:02d}", "symbol": symbols[0],
                 "close": 10.0 + day * 0.1, "volume": 1000, "amount": 10000, "provider": "fake_bars"}
                for day in range(1, 30)
            ]
            return pd.DataFrame(rows)

        def get_earnings_forecasts(self, start_date, end_date, symbols=None):
            return pd.DataFrame()

        def get_announcements(self, code, count=10):
            return [{"title": "公告", "date": "2026-06-29", "source": "fake"}]

        def smart_quote(self, code):
            return {"code": code, "price": 12.9, "pe": 0, "source": "fake_quote"}

    from core.research.value_chain.service import CompanyValueResearchService

    report = CompanyValueResearchService(
        registry=FakeRegistry(),
        causal_context=_StubCausalContext(),
        expectation_context=_StubExpectationContext(),
    ).build_dossier("000001.SZ", as_of="2026-06-30")

    assert isinstance(report.research_readiness, ResearchReadiness)
    payload = report.research_readiness.to_dict()
    assert set(payload.keys()) == set(RESEARCH_READINESS_DIMENSIONS)
    # 估值缺失（FakeRegistry.smart_quote pe=0）必须 not_computable
    assert payload["valuation_computability"]["status"] == "blocked"


class _StubCausalContext:
    """测试用 CausalContext stub，避免依赖真实 DB。"""

    def for_company(self, *, symbol: str, company_name: str, as_of: str) -> dict[str, Any]:
        return {
            "verified_edges": [],
            "exploration_links": [],
            "observed_facts": [],
            "forward_plans": [],
            "ontology_edges": [],
            "ontology_mappings": [],
            "rejected_edges": [],
            "gaps": ["verified_causal_chain_missing"],
            "slot_coverage": {"product": False, "upstream_constraint": False,
                              "downstream": False, "demand_driver": False},
            "chain_status": "missing",
            "source": "stub",
        }


class _StubExpectationContext:
    """测试用 ExpectationContext stub。"""

    def for_company(self, symbol: str, as_of: str) -> dict[str, Any]:
        return {
            "verified_expectations": [],
            "rejected_expectations": [],
            "gaps": ["verified_market_expectation_missing"],
            "source": "stub",
        }


# ---------------------------------------------------------------------------
# 假设状态迁移与 source_type 校验
# ---------------------------------------------------------------------------


def test_validate_assumption_transition_accepts_legal_moves() -> None:
    """合法迁移必须通过校验。"""
    for from_status, to_status in (
        ("draft", "active"),
        ("active", "falsified"),
        ("active", "superseded"),
        ("falsified", "superseded"),
    ):
        validate_assumption_transition(from_status, to_status)


def test_validate_assumption_transition_rejects_backwards() -> None:
    """回环迁移必须被拒绝。"""
    for from_status, to_status in (
        ("active", "draft"),
        ("falsified", "active"),
        ("superseded", "active"),
    ):
        with pytest.raises(ValueError, match="非法假设状态迁移"):
            validate_assumption_transition(from_status, to_status)


def test_validate_assumption_source_type_rejects_unknown() -> None:
    """未知 source_type 必须被拒绝。"""
    validate_assumption_source_type("company_disclosed")
    validate_assumption_source_type("third_party_consensus")
    validate_assumption_source_type("market_implied")
    validate_assumption_source_type("analyst_assumption")
    with pytest.raises(ValueError, match="非法假设 source_type"):
        validate_assumption_source_type("unknown_blend")


def test_readiness_dimension_defaults_are_safe() -> None:
    """ReadinessDimension 默认值不得包含中性分。"""
    dim = ReadinessDimension()
    assert dim.status == "not_computable"
    assert dim.score is None
    assert dim.missing_items == []
    assert dim.evidence_refs == []


def test_research_readiness_defaults_are_safe() -> None:
    """ResearchReadiness 默认所有维度都不可计算且无中性分。"""
    readiness = ResearchReadiness()
    payload = readiness.to_dict()
    for name, dimension in payload.items():
        assert dimension["score"] is None, f"{name}.score 必须 None，不允许 0.5 中性"
        assert dimension["status"] == "not_computable"
