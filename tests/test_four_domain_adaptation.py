import pytest
from flask import Flask

from core.research.adaptation.contracts import CalibrationVersion, DomainFeedback, LearningDomain
from core.research.adaptation.engine import (
    CalibrationGate, DomainFeedbackFirewall, diagnose_portfolio, grouped_returns,
    neutralize_signals, pearson_ic, rank_ic, walk_forward_windows,
)
from core.research.adaptation.repository import AdaptationRepository
from core.research.adaptation.reporting import ExecutionAttributionAdapter, FourDomainReportBuilder
from core.paper_trading.backtest_execution_adapter import BacktestExecutionPolicyAdapter
from core.quant.backtest.models import BacktestConfig


@pytest.mark.parametrize("domain,kind", [
    ("knowledge", "claim_verified"), ("research", "forecast_error"),
    ("alpha", "out_of_sample_ic"), ("execution", "slippage"),
])
def test_each_feedback_type_is_accepted_only_in_its_domain(domain, kind):
    task = DomainFeedbackFirewall().route(DomainFeedback(domain=domain, attribution_type=kind, entity_id="x"))
    assert task and task.source_domain == domain


def test_cross_domain_feedback_cannot_directly_mutate_another_domain():
    firewall = DomainFeedbackFirewall()
    with pytest.raises(ValueError):
        firewall.route(DomainFeedback(domain="knowledge", attribution_type="slippage", entity_id="claim-1"))
    task = firewall.route(DomainFeedback(domain="execution", attribution_type="slippage", entity_id="trade-1"))
    assert task.status == "pending" and not hasattr(task, "claim_confidence")


def test_walk_forward_is_strictly_time_ordered():
    windows = walk_forward_windows([f"2026-01-{d:02d}" for d in range(1, 11)], 4, 2)
    assert windows
    assert all(window["train_end"] < window["test_start"] for window in windows)


def test_cross_sectional_ic_rank_ic_and_group_returns():
    signal = [1, 2, 3, 4, 5]
    outcome = [-.2, -.1, 0, .1, .2]
    assert pearson_ic(signal, outcome) == pytest.approx(1.0)
    assert rank_ic(signal, outcome) == pytest.approx(1.0)
    grouped = grouped_returns(signal, outcome, groups=5)
    assert grouped["Q5"] > grouped["Q1"]
    assert rank_ic([1, "bad", 3], [1, 2, 3]) == pytest.approx(1.0)


def test_adaptation_metrics_and_repository_skip_malformed_inputs(tmp_path):
    assert pearson_ic(None, None) is None
    assert grouped_returns([1, "bad"], [0.1, "bad"]) == {"Q5": 0.1}
    assert diagnose_portfolio([None, {"weight": "bad"}], as_of="2026-01-01").violations == []
    assert neutralize_signals([None, {"signal": "bad"}]) == [0.0]

    repo = AdaptationRepository(tmp_path / "malformed.db")
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO domain_feedback(feedback_id, domain, created_at, payload_json) VALUES (?, ?, ?, ?)",
            ("bad", "execution", "", "not-json"),
        )
    assert repo.get("domain_feedback", "bad") is None
    assert repo.list("domain_feedback", limit="bad") == []


def test_portfolio_diagnostics_cover_industry_style_beta_liquidity_capacity():
    result = diagnose_portfolio([
        {"symbol": "A", "weight": .25, "industry": "半导体", "beta": 1.2, "size": -.3,
         "style_exposure": {"growth": .8}, "participation_rate": .25, "capacity_utilization": .1},
        {"symbol": "B", "weight": .2, "industry": "半导体", "beta": .8, "size": .2,
         "style_exposure": {"growth": .4}, "participation_rate": .1, "capacity_utilization": .25},
    ], as_of="2026-01-01")
    assert result.industry_exposure["半导体"] == pytest.approx(.45)
    assert result.style_exposure["growth"] == pytest.approx(.28)
    assert {"liquidity", "capacity", "industry:半导体"}.issubset(result.violations)


def test_industry_and_style_neutralization_removes_systematic_exposure():
    rows = [
        {"industry": "A", "signal": 3, "style_exposure": {"growth": 1}},
        {"industry": "A", "signal": 1, "style_exposure": {"growth": -1}},
        {"industry": "B", "signal": 5, "style_exposure": {"growth": 1}},
        {"industry": "B", "signal": 3, "style_exposure": {"growth": -1}},
    ]
    residual = neutralize_signals(rows, style_keys=["growth"])
    assert sum(residual[:2]) == pytest.approx(0)
    assert sum(residual[2:]) == pytest.approx(0)
    assert sum(r * row["style_exposure"]["growth"] for r, row in zip(residual, rows)) == pytest.approx(0)


def test_calibration_requires_minimum_sample_bounds_shadow_and_approval():
    gate = CalibrationGate()
    bad = CalibrationVersion(
        domain=LearningDomain.ALPHA.value, parameters={"weight": 2.0}, bounds={"weight": (0, 1)},
        sample_size=5, minimum_sample_size=30, status="enabled", approved_by="",
    )
    reasons = gate.validate(bad)
    assert "minimum_sample_not_met" in reasons
    assert "out_of_bounds:weight" in reasons
    assert "manual_approval_required" in reasons
    good = CalibrationVersion(
        domain="alpha", parameters={"weight": .4}, bounds={"weight": (0, 1)},
        sample_size=50, status="shadow",
    )
    assert gate.validate(good) == []


def test_adaptation_contracts_normalize_malformed_payloads():
    feedback = DomainFeedback(feedback_id=None, domain=1, metrics=["bad"])
    assert feedback.domain == ""
    assert feedback.metrics == {}
    version = CalibrationVersion(
        parameters=["bad"], bounds={"x": "bad"}, sample_size="bad"
    )
    assert version.parameters == {}
    assert version.bounds == {}
    assert version.sample_size == 0


def test_feedback_and_cross_domain_review_are_atomically_persisted(tmp_path):
    repo = AdaptationRepository(tmp_path / "adaptation.db")
    feedback = DomainFeedback(domain="execution", attribution_type="slippage", entity_id="trade-1")
    review = DomainFeedbackFirewall().route(feedback)
    repo.save_feedback_and_review(feedback, review)
    assert repo.list("domain_feedback", "execution")[0]["entity_id"] == "trade-1"
    assert repo.list("attribution_review_tasks", "execution")[0]["attribution_type"] == "slippage"
    repo.migrate()
    assert repo.stats()["counts"]["domain_feedback"] == 1


def test_execution_adapter_only_attributes_existing_execution_output():
    class ExistingExecutionModel:
        pass
    adapter = ExecutionAttributionAdapter(ExistingExecutionModel())
    result = adapter.summarize(
        {"buys": [{"code": "A"}], "sells": [], "skipped": ["B"]},
        {"commission": 12.5, "slippage": 3.0, "limit_rejection": 1.0},
    )
    assert result["commission"] == 12.5 and result["skipped_count"] == 1
    assert not hasattr(adapter, "simulate_fill")


def test_paper_execution_reuses_backtest_config_for_slippage_volume_and_market_constraints():
    class Bar:
        open = 10.0; volume = 10_000; is_suspended = False; is_limit_up = False; is_limit_down = False
    config = BacktestConfig(slippage_rate=.002, volume_cap_ratio=.1, commission_rate=.0005, sell_tax=.002)
    adapter = BacktestExecutionPolicyAdapter(config)
    buy = adapter.decide(Bar(), side="buy")
    sell = adapter.decide(Bar(), side="sell")
    assert buy.price == pytest.approx(10.02) and sell.price == pytest.approx(9.98)
    assert buy.max_quantity == 1000
    Bar.is_suspended = True
    assert adapter.decide(Bar(), side="buy").rejection_reason == "suspended"


def test_report_contains_pit_exposure_ic_cost_and_all_feedback_domains():
    diagnostics = diagnose_portfolio([], as_of="2026-01-01")
    feedback = [
        DomainFeedback(domain="knowledge", attribution_type="claim_verified"),
        DomainFeedback(domain="research", attribution_type="forecast_error"),
        DomainFeedback(domain="alpha", attribution_type="out_of_sample_ic"),
        DomainFeedback(domain="execution", attribution_type="slippage"),
    ]
    report = FourDomainReportBuilder().build(
        as_of="2026-01-01", signals=[1, 2, 3], outcomes=[-.1, 0, .1],
        diagnostics=diagnostics, feedback=feedback, pit_blocked_count=4,
        cost_attribution={"commission": 5.0, "slippage": 2.0},
        walk_forward_windows=walk_forward_windows([f"2026-01-{i:02d}" for i in range(1, 7)], 3, 1),
    )
    payload = report.to_dict()
    assert payload["pit_blocked_count"] == 4
    assert payload["ic"] == pytest.approx(1.0)
    assert payload["cost_attribution"]["commission"] == 5.0
    assert set(payload["feedback_stats"]) == {"knowledge", "research", "alpha", "execution"}


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import server.routes.adaptation_routes as routes
    monkeypatch.setattr(routes, "_repository", AdaptationRepository(tmp_path / "api.db"))
    app = Flask(__name__); app.config["TESTING"] = True; app.register_blueprint(routes.adaptation_bp)
    return app.test_client()


def test_feedback_api_rejects_cross_domain_mutation_and_persists_review(api_client):
    rejected = api_client.post("/api/research/adaptation/feedback", json={
        "domain": "knowledge", "attribution_type": "slippage", "entity_id": "claim-1",
    })
    assert rejected.status_code == 400
    accepted = api_client.post("/api/research/adaptation/feedback", json={
        "domain": "execution", "attribution_type": "slippage", "entity_id": "trade-1",
    })
    assert accepted.status_code == 201
    reviews = api_client.get("/api/research/adaptation/reviews?source_domain=execution").get_json()["data"]
    assert len(reviews) == 1 and reviews[0]["entity_id"] == "trade-1"


def test_calibration_api_enforces_shadow_sample_bounds_and_approval(api_client):
    direct_enable = api_client.post("/api/research/adaptation/calibrations", json={
        "domain": "alpha", "parameters": {"weight": .4}, "bounds": {"weight": [0, 1]},
        "sample_size": 50, "status": "enabled", "approved_by": "reviewer",
    })
    assert direct_enable.status_code == 400
    assert direct_enable.get_json()["error"]["code"] == "SHADOW_REQUIRED"
    rejected = api_client.post("/api/research/adaptation/calibrations", json={
        "domain": "alpha", "parameters": {"weight": 1.5}, "bounds": {"weight": [0, 1]},
        "sample_size": 5, "minimum_sample_size": 30, "status": "enabled",
    })
    assert rejected.status_code == 400
    accepted = api_client.post("/api/research/adaptation/calibrations", json={
        "domain": "alpha", "parameters": {"weight": .4}, "bounds": {"weight": [0, 1]},
        "sample_size": 50, "minimum_sample_size": 30, "status": "shadow",
    })
    assert accepted.status_code == 201
    calibration_id = accepted.get_json()["data"]["calibration_id"]
    no_approval = api_client.post(f"/api/research/adaptation/calibrations/{calibration_id}/enable", json={})
    assert no_approval.status_code == 400
    enabled = api_client.post(f"/api/research/adaptation/calibrations/{calibration_id}/enable", json={"approved_by": "reviewer"})
    assert enabled.status_code == 200 and enabled.get_json()["data"]["status"] == "enabled"
    rollback_without_approval = api_client.post(f"/api/research/adaptation/calibrations/{calibration_id}/rollback", json={})
    assert rollback_without_approval.status_code == 400
    rolled = api_client.post(f"/api/research/adaptation/calibrations/{calibration_id}/rollback", json={"approved_by": "reviewer"})
    assert rolled.status_code == 201
    assert rolled.get_json()["data"]["rollback_of"] == calibration_id


def test_report_api_smoke_has_required_acceptance_fields(api_client):
    missing_as_of = api_client.post("/api/research/adaptation/reports", json={"signals": [1], "outcomes": [1]})
    assert missing_as_of.status_code == 400
    response = api_client.post("/api/research/adaptation/reports", json={
        "as_of": "2026-02-01", "signals": [1, 2, 3], "outcomes": [-.1, 0, .1],
        "dates": [f"2026-01-{i:02d}" for i in range(1, 7)], "train_size": 3, "test_size": 1,
        "pit_blocked_count": 2, "portfolio_rows": [{"symbol": "A", "weight": .2, "industry": "半导体"}],
        "cost_attribution": {"commission": 2, "slippage": 1},
        "feedback": [{"domain": "alpha", "attribution_type": "out_of_sample_ic"}],
    })
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["pit_blocked_count"] == 2 and data["ic"] is not None
    assert "industry_exposure" in data["diagnostics"] and "commission" in data["cost_attribution"]
