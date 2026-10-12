import pytest
from flask import Flask

from core.research.earnings.interval import Interval
from core.research.expectations.contracts import (
    CompanyGuidance, ConsensusEstimate, ExpectationKind, ExpectationValue,
    ImpliedExpectation, InvestmentThesis, ThesisStatus, ValuationSnapshot,
)
from core.research.expectations.engine import (
    ExpectationGapEngine, ThesisLifecycle, latest_visible_consensus,
)
from core.research.expectations.repository import ExpectationRepository


def value(low=4.0, high=5.0, retrieved="2026-01-02T00:00:00+00:00"):
    return ExpectationValue(
        metric="net_profit", value=Interval(low, high), unit="yuan", currency="CNY",
        period="2027", as_of="2026-01-01T00:00:00+00:00", data_version="cons-v1",
        source_claim_id="claim-cons", source_evidence_id="ev-cons",
        extraction_method="licensed_feed", confidence=Interval(.7, .9),
        published_at="2026-01-01T00:00:00+00:00", retrieved_at=retrieved,
    )


def test_three_expectation_domains_are_distinct_contracts():
    guidance = CompanyGuidance(values=[value()])
    consensus = ConsensusEstimate(values=[value()], provider="provider", sample_size=8)
    implied = ImpliedExpectation(values=[value()], model_name="reverse_dcf", model_version="v1")
    assert guidance.kind == ExpectationKind.COMPANY_GUIDANCE.value
    assert consensus.kind == ExpectationKind.CONSENSUS.value
    assert implied.kind == ExpectationKind.IMPLIED.value
    assert len({guidance.kind, consensus.kind, implied.kind}) == 3


def test_expectation_gap_is_interval_and_version_traceable():
    result = ExpectationGapEngine().compute(
        company_symbol="TEST", metric="net_profit", period="2027",
        research_value=Interval(3.2, 5.1), earnings_result_id="earn-1",
        earnings_data_version="earn-v2", expectation_id="cons-1",
        expectation_kind="consensus", expectation_value=value(4.5, 4.8),
        market_data_version="market-v7", as_of="2026-02-01T00:00:00+00:00",
    )
    assert result.status == "ok"
    assert result.gap.low == pytest.approx(-1.6)
    assert result.gap.high == pytest.approx(.6)
    assert result.earnings_result_id == "earn-1"
    assert result.earnings_data_version == "earn-v2"
    assert result.market_data_version == "market-v7"


def test_future_consensus_revision_is_invisible_historically():
    old = ConsensusEstimate(consensus_id="old", values=[value(4, 5, "2026-01-02T00:00:00+00:00")])
    future = ConsensusEstimate(consensus_id="future", values=[value(6, 7, "2026-03-02T00:00:00+00:00")])
    selected = latest_visible_consensus(
        [old, future], metric="net_profit", period="2027",
        as_of="2026-02-01T00:00:00+00:00",
    )
    assert selected is not None and selected[0].consensus_id == "old"


def test_missing_or_future_expectation_safely_degrades():
    future = value(retrieved="2027-01-01T00:00:00+00:00")
    result = ExpectationGapEngine().compute(
        company_symbol="TEST", metric="net_profit", period="2027",
        research_value=Interval(3, 5), earnings_result_id="earn-1",
        earnings_data_version="earn-v1", expectation_id="cons-1",
        expectation_kind="consensus", expectation_value=future,
        market_data_version="market-v1", as_of="2026-02-01T00:00:00+00:00",
    )
    assert result.status == "insufficient_data"
    assert "expectation_not_visible_as_of" in result.gaps
    assert result.gap == Interval(0, 0)


def test_valuation_and_crowding_do_not_enter_fact_confidence():
    snapshot = ValuationSnapshot(
        current_valuation={"pe": 25.0}, historical_percentile={"pe": .8},
        crowding={"turnover_percentile": .9}, market_data_version="m1",
    )
    assert not hasattr(snapshot, "fact_confidence")
    assert snapshot.current_valuation != snapshot.crowding


def test_required_claim_failure_degrades_thesis_and_creates_review():
    thesis = InvestmentThesis(
        required_claim_ids=["c1", "c2"], core_assumptions=["capacity on time"],
        invalidation_conditions=["capacity delayed"], weakest_assumption="capacity on time",
        next_evidence_required="commissioning announcement", status=ThesisStatus.ACTIVE.value,
    )
    updated, reviews = ThesisLifecycle().evaluate_claims(thesis, {"c1": "ACTIVE", "c2": "CONFLICTED"})
    assert updated.status == ThesisStatus.CONFLICTED.value
    assert len(updated.audits) == 1
    assert reviews and reviews[0].thesis_id == thesis.thesis_id
    assert "c2" in reviews[0].reason


def test_thesis_contract_has_falsification_and_next_evidence_without_trade_advice():
    thesis = InvestmentThesis(
        invalidation_conditions=["customer validation failed"],
        next_evidence_required="customer certification evidence",
    )
    rendered = str(thesis.to_dict())
    assert thesis.invalidation_conditions and thesis.next_evidence_required
    for forbidden in ("buy", "sell", "position", "stop_loss"):
        assert forbidden not in rendered.lower()


def test_sqlite_migration_is_idempotent_and_domains_are_separate(tmp_path):
    repo = ExpectationRepository(tmp_path / "expectations.db")
    repo.migrate()
    guidance = CompanyGuidance(company_symbol="TEST", values=[value()])
    consensus = ConsensusEstimate(company_symbol="TEST", values=[value()], provider="p", sample_size=3)
    implied = ImpliedExpectation(company_symbol="TEST", values=[value()], model_name="reverse_dcf", model_version="v1")
    repo.save("company_guidance", guidance)
    repo.save("consensus_estimates", consensus)
    repo.save("implied_expectations", implied)
    assert repo.get("company_guidance", guidance.guidance_id)["kind"] == "company_guidance"
    assert repo.get("consensus_estimates", consensus.consensus_id)["kind"] == "consensus"
    assert repo.get("implied_expectations", implied.implied_id)["kind"] == "implied"
    assert repo.stats()["counts"]["consensus_estimates"] == 1


def test_thesis_degrade_audit_and_review_are_atomic_persisted(tmp_path):
    repo = ExpectationRepository(tmp_path / "expectations.db")
    thesis = InvestmentThesis(company_symbol="TEST", required_claim_ids=["c1"], status="active")
    thesis, reviews = ThesisLifecycle().evaluate_claims(thesis, {"c1": "REJECTED"})
    repo.save_thesis_bundle(thesis, reviews)
    saved = repo.get("investment_theses", thesis.thesis_id)
    assert saved["status"] == "conflicted"
    assert len(repo.list("thesis_audits", index_value=thesis.thesis_id)) == 1
    assert len(repo.list("thesis_review_tasks", index_value=thesis.thesis_id)) == 1


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import server.routes.expectations_routes as routes
    repo = ExpectationRepository(tmp_path / "api.db")
    monkeypatch.setattr(routes, "_repository", repo)
    app = Flask(__name__)
    app.register_blueprint(routes.expectations_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_expectations_api_keeps_domains_separate_and_computes_gap(api_client):
    raw_value = value().to_dict()
    for endpoint, extra in (
        ("guidance", {}),
        ("consensus", {"provider": "p", "sample_size": 4, "dispersion": {"low": .1, "high": .2}}),
        ("implied", {"model_name": "reverse_dcf", "model_version": "v1", "market_data_version": "m1"}),
    ):
        response = api_client.post(f"/api/research/expectations/{endpoint}", json={
            "company_symbol": "TEST", "values": [raw_value], **extra,
        })
        assert response.status_code == 201
    response = api_client.post("/api/research/expectations/gaps", json={
        "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
        "research_value": {"low": 3.2, "high": 5.1}, "earnings_result_id": "e1",
        "earnings_data_version": "ev1", "expectation_id": "c1",
        "expectation_kind": "consensus", "expectation_value": raw_value,
        "market_data_version": "m1", "as_of": "2026-02-01T00:00:00+00:00",
    })
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "ok"


def test_thesis_api_exposes_falsification_and_generates_review(api_client):
    created = api_client.post("/api/research/expectations/theses", json={
        "company_symbol": "TEST", "title": "可证伪研究观点", "status": "active",
        "required_claim_ids": ["c1"], "core_assumptions": ["产能按期"],
        "invalidation_conditions": ["投产延期"], "weakest_assumption": "产能按期",
        "next_evidence_required": "投产公告", "valid_until": "2027-01-01",
        "review_at": "2026-09-01",
    })
    assert created.status_code == 201
    thesis_id = created.get_json()["data"]["thesis_id"]
    evaluated = api_client.post(
        f"/api/research/expectations/theses/{thesis_id}/evaluate-claims",
        json={"claim_states": {"c1": "CONFLICTED"}},
    )
    data = evaluated.get_json()["data"]
    assert data["thesis"]["status"] == "conflicted"
    assert data["reviews"] and data["thesis"]["invalidation_conditions"]


# ---------------------------------------------------------------------------
# 扩展测试：ExpectationsGap / InvestmentThesis / 状态机
# ---------------------------------------------------------------------------

from core.research.expectations.contracts import (
    Catalyst,
    ExpectationGap,
    ExpectationSource,
    GapStatus,
    ImpliedExpectationInput,
    MarketExpectation,
    ThesisStatus,
)
from core.research.expectations.engine import ImpliedExpectationEngine


def make_market_expectation(
    *, source: str = ExpectationSource.CONSENSUS.value,
    profit_low: float = 4.0, profit_high: float = 5.0,
    eps_low: float = 1.0, eps_high: float = 1.2,
    conf_low: float = 0.6, conf_high: float = 0.8,
    available_at: str = "2026-01-01T00:00:00+00:00",
    symbol: str = "TEST",
) -> MarketExpectation:
    """构造统一市场预期对象。"""
    return MarketExpectation(
        symbol=symbol,
        expected_revenue=Interval(10e8, 12e8),
        expected_net_profit=Interval(profit_low, profit_high),
        expected_EPS=Interval(eps_low, eps_high),
        valuation_multiple=Interval(25.0, 30.0),
        expectation_source=source,
        source_count=1,
        available_at=available_at,
        forecast_period="2027",
        confidence=Interval(conf_low, conf_high),
        evidence_ids=["ev-me-1"],
        version="v1",
        published_at=available_at,
        retrieved_at=available_at,
    )


class TestExpectationSourcePriority:
    """预期来源优先级（P1.7 扩展为 7 级，新增 AQUATRADE_FORECAST）。"""

    def test_priority_order(self):
        """公司指引 > 一致预期 > 券商 > 隐含 > AquaTrade 预测 > 用户输入 > 系统推断。"""
        assert ExpectationSource.priority(ExpectationSource.COMPANY_GUIDANCE.value) == 1
        assert ExpectationSource.priority(ExpectationSource.CONSENSUS.value) == 2
        assert ExpectationSource.priority(ExpectationSource.ANALYST_FORECAST.value) == 3
        assert ExpectationSource.priority(ExpectationSource.IMPLIED.value) == 4
        assert ExpectationSource.priority(ExpectationSource.AQUATRADE_FORECAST.value) == 5
        assert ExpectationSource.priority(ExpectationSource.USER_INPUT.value) == 6
        assert ExpectationSource.priority(ExpectationSource.SYSTEM_INFERRED.value) == 7

    def test_four_independent_domains(self):
        """P1.7 要求的四类独立预期来源互不覆盖。"""
        four = ExpectationSource.four_independent_domains()
        assert set(four) == {
            "company_guidance", "consensus", "implied", "aquatrade_forecast",
        }
        assert len(set(four)) == 4

    def test_different_sources_not_merged(self):
        """不同来源不混并，每个实例对应单一来源。"""
        guidance = make_market_expectation(source=ExpectationSource.COMPANY_GUIDANCE.value)
        consensus = make_market_expectation(source=ExpectationSource.CONSENSUS.value)
        assert guidance.expectation_source != consensus.expectation_source
        assert guidance.source_priority < consensus.source_priority

    def test_implied_is_marked_not_consensus(self):
        """反推结果标记为 implied，不得标记为 consensus。"""
        me = make_market_expectation(source=ExpectationSource.IMPLIED.value)
        assert me.is_implied is True
        assert me.expectation_source != ExpectationSource.CONSENSUS.value


class TestConsensusMissing:
    """一致预期缺失场景。"""

    def test_expectation_data_missing_status(self):
        """缺失预期值时返回 EXPECTATION_DATA_MISSING。"""
        result = ExpectationGapEngine().compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(3, 5), earnings_result_id="earn-1",
            earnings_data_version="earn-v1", expectation_id="",
            expectation_kind="consensus", expectation_value=None,
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        assert result.status == GapStatus.EXPECTATION_DATA_MISSING.value
        assert result.gap == Interval(0, 0)

    def test_insufficient_data_when_value_incomplete(self):
        """预期值不完整时返回 INSUFFICIENT_DATA。"""
        incomplete = ExpectationValue(
            metric="net_profit", value=Interval(4, 5), unit="yuan", currency="CNY",
            period="2027", as_of="2026-01-01T00:00:00+00:00", data_version="v1",
            source_claim_id="", source_evidence_id="", extraction_method="",
            confidence=Interval(.7, .9), published_at="", retrieved_at="",
        )
        result = ExpectationGapEngine().compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(3, 5), earnings_result_id="earn-1",
            earnings_data_version="earn-v1", expectation_id="cons-1",
            expectation_kind="consensus", expectation_value=incomplete,
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        assert result.status == GapStatus.INSUFFICIENT_DATA.value


class TestMultiSourceConflict:
    """多来源预期冲突。"""

    def test_different_sources_produce_different_gaps(self):
        """不同来源的预期值产生不同预期差。"""
        engine = ExpectationGapEngine()
        high_val = value(4.0, 4.5)  # 较低预期
        low_val = value(6.0, 7.0)   # 较高预期
        r1 = engine.compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(5, 6), earnings_result_id="e1",
            earnings_data_version="ev1", expectation_id="c1",
            expectation_kind="consensus", expectation_value=high_val,
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        r2 = engine.compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(5, 6), earnings_result_id="e1",
            earnings_data_version="ev1", expectation_id="c2",
            expectation_kind="consensus", expectation_value=low_val,
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        assert r1.gap != r2.gap
        assert r1.gap.low > r2.gap.low  # 较低预期 → 更大的正差

    def test_gap_from_market_expectation(self):
        """从 MarketExpectation 对象计算预期差。"""
        me = make_market_expectation(profit_low=4.0, profit_high=5.0)
        result = ExpectationGapEngine().compute_from_market_expectation(
            research_value=Interval(6, 7), earnings_result_id="e1",
            earnings_data_version="ev1", market_expectation=me,
            metric="net_profit", market_data_version="m1",
            as_of="2026-02-01T00:00:00+00:00",
        )
        assert result.status == GapStatus.OK.value
        # gap = [6-5, 7-4] = [1, 3]
        assert result.gap.low == pytest.approx(1.0)
        assert result.gap.high == pytest.approx(3.0)


class TestNegativeProfit:
    """负利润公司。"""

    def test_negative_expectation_gap(self):
        """负利润预期下预期差不崩溃。"""
        negative_val = ExpectationValue(
            metric="net_profit", value=Interval(-5, -3), unit="yuan", currency="CNY",
            period="2027", as_of="2026-01-01T00:00:00+00:00", data_version="v1",
            source_claim_id="c1", source_evidence_id="e1", extraction_method="feed",
            confidence=Interval(.6, .8), published_at="2026-01-01T00:00:00+00:00",
            retrieved_at="2026-01-02T00:00:00+00:00",
        )
        result = ExpectationGapEngine().compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(-2, -1), earnings_result_id="e1",
            earnings_data_version="ev1", expectation_id="c1",
            expectation_kind="consensus", expectation_value=negative_val,
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        assert result.status == GapStatus.OK.value
        # gap = [-2 - (-3), -1 - (-5)] = [1, 4]
        assert result.gap.low == pytest.approx(1.0)
        assert result.gap.high == pytest.approx(4.0)


class TestImpliedReverseEngineer:
    """隐含预期反推。"""

    def test_reverse_engineer_from_pe(self):
        """从 PE 反推隐含 EPS。"""
        inp = ImpliedExpectationInput(
            symbol="300308.SZ", current_price=100.0, shares_outstanding=1e8,
            trailing_EPS=1.5, pe_ratio=50.0, pb_ratio=8.0,
            growth_rate_assumed=0.2, margin_assumed=0.25,
            market_data_version="m1", as_of="2026-07-01T00:00:00+00:00",
        )
        me = ImpliedExpectationEngine().reverse_engineer(inp, forecast_period="2027")
        assert me is not None
        assert me.expectation_source == ExpectationSource.IMPLIED.value
        assert me.is_implied is True
        # 隐含 EPS = 100 / 50 = 2.0
        assert me.expected_EPS.mid == pytest.approx(2.0, rel=0.2)
        # 置信度较低
        assert me.confidence.high <= 0.5

    def test_reverse_engineer_returns_none_on_missing_input(self):
        """输入不完整时返回 None。"""
        inp = ImpliedExpectationInput(symbol="", current_price=0)
        assert ImpliedExpectationEngine().reverse_engineer(inp) is None

    def test_implied_not_labeled_as_consensus(self):
        """反推结果不得标记为 consensus。"""
        inp = ImpliedExpectationInput(
            symbol="TEST", current_price=50.0, shares_outstanding=1e8,
            pe_ratio=30.0, as_of="2026-07-01T00:00:00+00:00",
        )
        me = ImpliedExpectationEngine().reverse_engineer(inp)
        assert me.expectation_source == "implied"
        assert me.expectation_source != "consensus"


class TestThesisStateMachine:
    """投资观点完整状态机。"""

    def _active_thesis(self) -> InvestmentThesis:
        return InvestmentThesis(
            company_symbol="TEST", required_claim_ids=["c1", "c2"],
            status=ThesisStatus.ACTIVE.value,
            valid_until="2027-12-31T00:00:00+00:00",
        )

    def test_draft_to_evidence_pending(self):
        """DRAFT → EVIDENCE_PENDING 合法迁移。"""
        thesis = InvestmentThesis(status=ThesisStatus.DRAFT.value)
        lc = ThesisLifecycle()
        assert lc._can_transition(ThesisStatus.DRAFT.value, ThesisStatus.EVIDENCE_PENDING.value)

    def test_invalid_transition_blocked(self):
        """非法迁移被阻止。"""
        thesis = InvestmentThesis(status=ThesisStatus.ARCHIVED.value)
        lc = ThesisLifecycle()
        updated, _ = lc._transition(
            thesis, ThesisStatus.ACTIVE.value, "test", operator="test",
        )
        assert updated.status == ThesisStatus.ARCHIVED.value  # 未变

    def test_active_to_high_conviction(self):
        """ACTIVE → HIGH_CONVICTION。"""
        thesis = self._active_thesis()
        lc = ThesisLifecycle()
        updated, _ = lc.promote_to_high_conviction(
            thesis, gap_significant=True, catalyst_in_window=True,
            evidence_sufficient=True, operator="test",
        )
        assert updated.status == ThesisStatus.HIGH_CONVICTION.value
        assert updated.version == 2

    def test_high_conviction_promotion_requires_all_conditions(self):
        """HIGH_CONVICTION 升级需全部条件满足。"""
        thesis = self._active_thesis()
        lc = ThesisLifecycle()
        updated, _ = lc.promote_to_high_conviction(
            thesis, gap_significant=False, catalyst_in_window=True,
            evidence_sufficient=True, operator="test",
        )
        assert updated.status == ThesisStatus.ACTIVE.value  # 未升级

    def test_contradictory_evidence_degrades_active(self):
        """反证加入后 ACTIVE → CONFLICTED。"""
        thesis = self._active_thesis()
        lc = ThesisLifecycle()
        updated, review = lc.add_contradictory_evidence(
            thesis, "ev-contra-1", reason="反证测试", operator="test",
        )
        assert updated.status == ThesisStatus.CONFLICTED.value
        assert "ev-contra-1" in updated.contradictory_evidence_ids
        assert review is not None

    def test_catalyst_expiry_expires_thesis(self):
        """催化剂窗口过期后 → EXPIRED。"""
        expired_catalyst = Catalyst(
            description="Q1财报", window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-03-01T00:00:00+00:00",
        )
        thesis = InvestmentThesis(
            company_symbol="TEST", status=ThesisStatus.ACTIVE.value,
            catalysts=[expired_catalyst],
        )
        lc = ThesisLifecycle()
        updated, _ = lc.check_catalyst_expiry(
            thesis, as_of="2026-07-01T00:00:00+00:00", operator="test",
        )
        assert updated.status == ThesisStatus.EXPIRED.value

    def test_thesis_expiry_after_valid_until(self):
        """超过 valid_until 后 → EXPIRED。"""
        thesis = InvestmentThesis(
            company_symbol="TEST", status=ThesisStatus.ACTIVE.value,
            valid_until="2026-06-01T00:00:00+00:00",
        )
        lc = ThesisLifecycle()
        updated, _ = lc.check_expiry(
            thesis, as_of="2026-07-01T00:00:00+00:00", operator="test",
        )
        assert updated.status == ThesisStatus.EXPIRED.value

    def test_archive_from_expired(self):
        """EXPIRED → ARCHIVED。"""
        thesis = InvestmentThesis(
            company_symbol="TEST", status=ThesisStatus.EXPIRED.value,
        )
        lc = ThesisLifecycle()
        updated, _ = lc.archive(thesis, operator="test")
        assert updated.status == ThesisStatus.ARCHIVED.value

    def test_audit_recorded_on_transition(self):
        """每次状态迁移保存审计记录。"""
        thesis = self._active_thesis()
        lc = ThesisLifecycle()
        updated, _ = lc.add_contradictory_evidence(
            thesis, "ev-1", reason="test", operator="test",
        )
        assert len(updated.audits) == 1
        audit = updated.audits[0]
        assert audit.from_status == ThesisStatus.ACTIVE.value
        assert audit.to_status == ThesisStatus.CONFLICTED.value
        assert audit.operator == "test"

    def test_version_increments_on_transition(self):
        """每次状态迁移 version +1。"""
        thesis = self._active_thesis()
        lc = ThesisLifecycle()
        updated, _ = lc.promote_to_high_conviction(
            thesis, gap_significant=True, catalyst_in_window=True,
            evidence_sufficient=True, operator="test",
        )
        assert updated.version == 2


class TestGapSignChange:
    """预期差由正转负。"""

    def test_positive_to_negative_gap(self):
        """研究值从高于预期变为低于预期，预期差由正转负。"""
        engine = ExpectationGapEngine()
        # 正预期差：研究值 [6,7] - 预期 [4,5] = [1,3]
        positive = engine.compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(6, 7), earnings_result_id="e1",
            earnings_data_version="ev1", expectation_id="c1",
            expectation_kind="consensus", expectation_value=value(4, 5),
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        # 负预期差：研究值 [3,4] - 预期 [4,5] = [-2,0]
        negative = engine.compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(3, 4), earnings_result_id="e1",
            earnings_data_version="ev1", expectation_id="c1",
            expectation_kind="consensus", expectation_value=value(4, 5),
            market_data_version="m1", as_of="2026-02-01T00:00:00+00:00",
        )
        assert positive.gap.low > 0
        assert negative.gap.high <= 0


class TestVersionRollback:
    """版本回滚测试。"""

    def test_thesis_version_increments_through_lifecycle(self, tmp_path):
        """观点版本随状态迁移递增。"""
        repo = ExpectationRepository(tmp_path / "vr.db")
        thesis = InvestmentThesis(
            company_symbol="TEST", required_claim_ids=["c1"],
            status=ThesisStatus.DRAFT.value,
        )
        repo.save_thesis_bundle(thesis, [])

        lc = ThesisLifecycle()
        # DRAFT → EVIDENCE_PENDING → ACTIVE → HIGH_CONVICTION
        thesis, _ = lc._transition(
            thesis, ThesisStatus.EVIDENCE_PENDING.value, "to_pending", operator="test",
        )
        thesis, _ = lc.promote_to_active(thesis, has_earnings_impact=True, operator="test")
        thesis, _ = lc.promote_to_high_conviction(
            thesis, gap_significant=True, catalyst_in_window=True,
            evidence_sufficient=True, operator="test",
        )
        repo.save_thesis_bundle(thesis, [])

        loaded = repo.get("investment_theses", thesis.thesis_id)
        assert loaded["version"] == 4  # 初始1 + 3次迁移
        assert loaded["status"] == ThesisStatus.HIGH_CONVICTION.value
        assert len(loaded["audits"]) == 3

    def test_persisted_audit_chain_is_reconstructable(self, tmp_path):
        """持久化的审计链可重建状态迁移历史。"""
        repo = ExpectationRepository(tmp_path / "audit.db")
        thesis = InvestmentThesis(
            company_symbol="TEST", required_claim_ids=["c1"],
            status=ThesisStatus.ACTIVE.value,
        )
        lc = ThesisLifecycle()
        thesis, _ = lc.add_contradictory_evidence(thesis, "ev-1", operator="test")
        thesis, _ = lc._transition(thesis, ThesisStatus.INVALIDATED.value, "confirmed", operator="test")
        repo.save_thesis_bundle(thesis, [])

        loaded = repo.get("investment_theses", thesis.thesis_id)
        audits = loaded["audits"]
        # 审计链应能重建: ACTIVE → CONFLICTED → INVALIDATED
        assert len(audits) == 2
        assert audits[0]["from_status"] == "active"
        assert audits[0]["to_status"] == "conflicted"
        assert audits[1]["from_status"] == "conflicted"
        assert audits[1]["to_status"] == "invalidated"


class TestExpectationAPIExtended:
    """扩展 API 测试。"""

    def test_market_expectation_endpoint(self, api_client):
        """市场预期端点：不同来源不混并。"""
        for source in ("company_guidance", "consensus", "implied"):
            resp = api_client.post("/api/research/expectations/market_expectations", json={
                "symbol": "TEST",
                "expected_revenue": {"low": 10e8, "high": 12e8},
                "expected_net_profit": {"low": 4, "high": 5},
                "expected_EPS": {"low": 1.0, "high": 1.2},
                "valuation_multiple": {"low": 25, "high": 30},
                "expectation_source": source,
                "source_count": 1,
                "available_at": "2026-01-01T00:00:00+00:00",
                "forecast_period": "2027",
                "confidence": {"low": 0.5, "high": 0.7},
                "evidence_ids": ["ev-1"],
                "version": "v1",
                "published_at": "2026-01-01T00:00:00+00:00",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
            })
            assert resp.status_code == 201
        resp = api_client.get("/api/research/expectations/market_expectations?symbol=TEST")
        assert resp.status_code == 200
        items = resp.get_json()["data"]
        assert len(items) == 3
        sources = {item["expectation_source"] for item in items}
        assert sources == {"company_guidance", "consensus", "implied"}

    def test_implied_reverse_endpoint(self, api_client):
        """隐含预期反推端点。"""
        resp = api_client.post("/api/research/expectations/implied_reverse", json={
            "symbol": "TEST", "current_price": 100.0, "shares_outstanding": 1e8,
            "pe_ratio": 50.0, "growth_rate_assumed": 0.2,
            "as_of": "2026-07-01T00:00:00+00:00", "save": False,
        })
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["expectation_source"] == "implied"
        assert data["expectation_source"] != "consensus"

    def test_add_contradiction_endpoint(self, api_client):
        """反证加入端点。"""
        created = api_client.post("/api/research/expectations/theses", json={
            "company_symbol": "TEST", "status": "active",
            "required_claim_ids": ["c1"],
        })
        thesis_id = created.get_json()["data"]["thesis_id"]
        resp = api_client.post(
            f"/api/research/expectations/theses/{thesis_id}/add-contradiction",
            json={"evidence_id": "ev-contra", "reason": "测试反证"},
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["thesis"]["status"] == "conflicted"
        assert "ev-contra" in data["thesis"]["contradictory_evidence_ids"]

    def test_check_expiry_endpoint(self, api_client):
        """有效期检查端点。"""
        created = api_client.post("/api/research/expectations/theses", json={
            "company_symbol": "TEST", "status": "active",
            "valid_until": "2026-01-01T00:00:00+00:00",
        })
        thesis_id = created.get_json()["data"]["thesis_id"]
        resp = api_client.post(
            f"/api/research/expectations/theses/{thesis_id}/check-expiry",
            json={"as_of": "2026-07-01T00:00:00+00:00"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["thesis"]["status"] == "expired"

    def test_promote_endpoint(self, api_client):
        """观点升级端点。"""
        created = api_client.post("/api/research/expectations/theses", json={
            "company_symbol": "TEST", "status": "evidence_pending",
            "required_claim_ids": ["c1"],
        })
        thesis_id = created.get_json()["data"]["thesis_id"]
        resp = api_client.post(
            f"/api/research/expectations/theses/{thesis_id}/promote",
            json={"action": "to_active", "has_earnings_impact": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["thesis"]["status"] == "active"
