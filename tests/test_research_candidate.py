"""ResearchCandidate 专项测试。

覆盖场景：
1. 三类分数严格分开
2. 市场预期缺失时 alpha_score 不可计算
3. 产业核心不等于 Alpha
4. 催化剂过期影响评分
5. 盈利影响大但证据置信度低
6. 高增长估值透支
7. 负利润公司
8. 报告生成与溯源
9. 持久化幂等
10. PIT 边界
11. 版本回滚
12. 状态降级
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from flask import Flask

from core.research.candidate.contracts import (
    CandidateReport,
    CandidateStatus,
    ResearchCandidate,
    ScoreBreakdown,
    ScoreComputability,
)
from core.research.candidate.engine import CandidateBuilder, CandidateReportBuilder
from core.research.candidate.repository import CandidateRepository
from core.research.earnings.interval import Interval


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def make_thesis_dict(
    *, status: str = "active", required_claims: int = 3,
    weakest: str = "产能按期投产", valid_until: str = "2027-12-31",
) -> dict:
    """构造 InvestmentThesis 字典。"""
    return {
        "thesis_id": "thesis_test_001",
        "company_symbol": "300308.SZ",
        "sector_id": "optical_module",
        "status": status,
        "required_claim_ids": [f"claim_{i}" for i in range(required_claims)],
        "supporting_evidence_ids": [f"ev_sup_{i}" for i in range(5)],
        "contradictory_evidence_ids": [],
        "weakest_assumption": weakest,
        "invalidation_conditions": ["产能延期超过6个月", "客户认证失败"],
        "valid_until": valid_until,
        "next_review_at": "2026-09-01",
    }


def make_earnings_impact_dict(
    *, status: str = "ok", eps_low: float = 0.5, eps_high: float = 0.8,
    revenue_low: float = 5e8, revenue_high: float = 8e8,
    profit_low: float = 2e8, profit_high: float = 3.5e8,
) -> dict:
    """构造 EarningsImpact 字典。"""
    return {
        "impact_id": "earn_test_001",
        "symbol": "300308.SZ",
        "product": "800G光模块",
        "period": "2027",
        "status": status,
        "eps_impact": {"low": eps_low, "high": eps_high},
        "incremental_revenue": {"low": revenue_low, "high": revenue_high},
        "incremental_net_profit": {"low": profit_low, "high": profit_high},
        "falsification_conditions": ["客户未通过认证", "良率低于预期"],
    }


def make_gap_dict(
    *, status: str = "ok", gap_low: float = 0.3, gap_high: float = 0.6,
    rel_low: float = 0.08, rel_high: float = 0.15,
    source: str = "consensus", conf_low: float = 0.6, conf_high: float = 0.8,
) -> dict:
    """构造 ExpectationGap 字典。"""
    return {
        "gap_id": "gap_test_001",
        "company_symbol": "300308.SZ",
        "metric": "net_profit",
        "period": "2027",
        "gap": {"low": gap_low, "high": gap_high},
        "relative_gap": {"low": rel_low, "high": rel_high},
        "status": status,
        "expectation_source": source,
        "gap_confidence": {"low": conf_low, "high": conf_high},
        "gaps": [],
    }


def make_valuation_snapshot_dict(
    *, pe_percentile: float = 0.5, pe: float = 30.0,
) -> dict:
    """构造 ValuationSnapshot 字典。"""
    return {
        "snapshot_id": "val_test_001",
        "company_symbol": "300308.SZ",
        "current_valuation": {"pe": pe},
        "historical_percentile": {"pe": pe_percentile},
        "crowding": {"turnover_percentile": 0.4},
        "market_data_version": "market_v1",
    }


def make_catalysts(
    *, expired: bool = False, count: int = 2,
) -> list[dict]:
    """构造催化剂列表。"""
    if expired:
        return [
            {
                "description": "Q2财报发布",
                "window_start": "2026-01-01T00:00:00+00:00",
                "window_end": "2026-03-01T00:00:00+00:00",
                "claim_id": "claim_cat_1",
                "evidence_id": "ev_cat_1",
                "probability": 0.7,
            }
        ]
    future = (datetime(2027, 6, 1, tzinfo=timezone.utc)).isoformat()
    return [
        {
            "description": f"催化剂_{i}",
            "window_start": "2026-06-01T00:00:00+00:00",
            "window_end": future,
            "claim_id": f"claim_cat_{i}",
            "evidence_id": f"ev_cat_{i}",
            "probability": 0.6 + i * 0.1,
        }
        for i in range(count)
    ]


def build_candidate(
    builder: CandidateBuilder | None = None,
    *,
    thesis: dict | None = None,
    earnings_impact: dict | None = None,
    expectation_gap: dict | None = None,
    valuation_snapshot: dict | None = None,
    industry_core: float = 0.7,
    company_exposure: float = 0.6,
    evidence_confidence: Interval | None = None,
    catalysts: list[dict] | None = None,
    as_of: str = "2026-07-01T00:00:00+00:00",
) -> ResearchCandidate:
    """构建候选对象的便捷封装。"""
    b = builder or CandidateBuilder()
    return b.build(
        thesis=thesis if thesis is not None else make_thesis_dict(),
        earnings_impact=earnings_impact if earnings_impact is not None else make_earnings_impact_dict(),
        expectation_gap=expectation_gap if expectation_gap is not None else make_gap_dict(),
        valuation_snapshot=valuation_snapshot if valuation_snapshot is not None else make_valuation_snapshot_dict(),
        sector_id="optical_module",
        as_of=as_of,
        industry_core_score=industry_core,
        company_exposure_score=company_exposure,
        evidence_confidence=evidence_confidence if evidence_confidence is not None else Interval(0.6, 0.8),
        catalysts=catalysts if catalysts is not None else make_catalysts(),
    )


# ---------------------------------------------------------------------------
# 测试：三类分数严格分开
# ---------------------------------------------------------------------------

class TestThreeScoresSeparated:
    """三类分数必须严格分开，禁止用一个总分掩盖差异。"""

    def test_three_scores_are_independent_fields(self):
        """三个分数是独立字段，不是总分。"""
        candidate = build_candidate()
        assert candidate.research_score is not candidate.alpha_score
        assert candidate.alpha_score is not candidate.core_score
        assert candidate.research_score is not candidate.core_score

    def test_research_score_does_not_depend_on_gap(self):
        """research_score 不依赖预期差数据。"""
        # 有预期差
        c1 = build_candidate(expectation_gap=make_gap_dict(status="ok"))
        # 无预期差
        c2 = build_candidate(expectation_gap=make_gap_dict(status="expectation_data_missing"))
        # research_score 应该相近（都基于证据和 Claim）
        r1_mid = c1.research_score.score.mid
        r2_mid = c2.research_score.score.mid
        assert abs(r1_mid - r2_mid) < 0.15, "research_score 不应受预期差影响"

    def test_core_score_does_not_equal_alpha(self):
        """产业核心不自动等同于存在 Alpha。"""
        # 产业核心但无预期差
        candidate = build_candidate(
            industry_core=0.9,
            company_exposure=0.85,
            expectation_gap=make_gap_dict(status="expectation_data_missing"),
        )
        assert candidate.core_score.score.mid > 0.5, "core_score 应该高"
        assert candidate.alpha_score.computability == ScoreComputability.NOT_COMPUTABLE.value
        assert candidate.alpha_score.score.mid < 0.1, "alpha_score 应该接近0"

    def test_no_single_total_score_hides_differences(self):
        """不存在一个总分会掩盖三者差异。"""
        candidate = build_candidate()
        d = candidate.to_dict()
        # 必须有三个独立分数键
        assert "research_score" in d
        assert "alpha_score" in d
        assert "core_score" in d
        # 不应该有 "total_score" 或 "overall_score"
        assert "total_score" not in d
        assert "overall_score" not in d


# ---------------------------------------------------------------------------
# 测试：市场预期缺失
# ---------------------------------------------------------------------------

class TestExpectationDataMissing:
    """市场预期数据缺失时的降级处理。"""

    def test_alpha_score_not_computable_when_gap_missing(self):
        """市场预期缺失时 alpha_score 必须标记不可计算。"""
        candidate = build_candidate(
            expectation_gap=make_gap_dict(status="expectation_data_missing"),
        )
        assert candidate.alpha_score.computability == ScoreComputability.NOT_COMPUTABLE.value
        assert candidate.alpha_score.score.mid == 0.0

    def test_research_score_still_computable_when_gap_missing(self):
        """市场预期缺失时 research_score 仍可输出。"""
        candidate = build_candidate(
            expectation_gap=make_gap_dict(status="expectation_data_missing"),
        )
        assert candidate.research_score.computability != ScoreComputability.NOT_COMPUTABLE.value
        assert candidate.research_score.score.mid > 0.0

    def test_core_score_still_computable_when_gap_missing(self):
        """市场预期缺失时 core_score 仍可输出。"""
        candidate = build_candidate(
            expectation_gap=make_gap_dict(status="expectation_data_missing"),
        )
        assert candidate.core_score.computability == ScoreComputability.COMPUTABLE.value
        assert candidate.core_score.score.mid > 0.0

    def test_candidate_status_degraded_when_alpha_not_computable(self):
        """alpha 不可计算时候选状态降级。"""
        candidate = build_candidate(
            expectation_gap=make_gap_dict(status="expectation_data_missing"),
        )
        assert candidate.status == CandidateStatus.DEGRADED.value


# ---------------------------------------------------------------------------
# 测试：催化剂过期
# ---------------------------------------------------------------------------

class TestCatalystExpiry:
    """催化剂过期影响评分。"""

    def test_expired_catalysts_reduce_score(self):
        """过期催化剂降低评分。"""
        active = build_candidate(catalysts=make_catalysts(expired=False, count=2))
        expired = build_candidate(catalysts=make_catalysts(expired=True, count=1))
        assert active.catalyst_score.score.mid > expired.catalyst_score.score.mid

    def test_all_expired_catalysts_degrade_computability(self):
        """全部催化剂过期时评分降级。"""
        candidate = build_candidate(catalysts=make_catalysts(expired=True, count=1))
        assert candidate.catalyst_score.computability == ScoreComputability.DEGRADED.value

    def test_no_catalysts_degrades_score(self):
        """无催化剂时评分降级。"""
        candidate = build_candidate(catalysts=[])
        assert candidate.catalyst_score.computability == ScoreComputability.DEGRADED.value
        assert candidate.catalyst_score.score.mid == 0.0


# ---------------------------------------------------------------------------
# 测试：盈利影响与证据置信度
# ---------------------------------------------------------------------------

class TestEarningsAndEvidence:
    """盈利影响和证据置信度场景。"""

    def test_large_earnings_but_low_evidence_confidence(self):
        """盈利影响大但证据置信度低 → research_score 低。"""
        high_ev = build_candidate(
            earnings_impact=make_earnings_impact_dict(
                eps_low=1.0, eps_high=2.0, status="ok",
            ),
            evidence_confidence=Interval(0.2, 0.4),
        )
        low_ev = build_candidate(
            earnings_impact=make_earnings_impact_dict(
                eps_low=1.0, eps_high=2.0, status="ok",
            ),
            evidence_confidence=Interval(0.7, 0.9),
        )
        assert high_ev.research_score.score.mid < low_ev.research_score.score.mid
        # 但 core_score 应该相近（都基于产业位置）
        assert abs(high_ev.core_score.score.mid - low_ev.core_score.score.mid) < 0.1

    def test_industry_core_but_small_earnings(self):
        """产业核心但盈利影响很小 → core_score 高，但 earnings 数据不完整。"""
        candidate = build_candidate(
            industry_core=0.9,
            company_exposure=0.85,
            earnings_impact=make_earnings_impact_dict(
                eps_low=0.01, eps_high=0.02, status="partial",
            ),
        )
        assert candidate.core_score.score.mid > 0.5
        # partial 状态降低 research_score 的 earnings_completeness 分量
        assert candidate.research_score.components.get("earnings_completeness", 0) < 0.30

    def test_negative_profit_company(self):
        """负利润公司 → 预期差计算不崩溃。"""
        candidate = build_candidate(
            earnings_impact=make_earnings_impact_dict(
                profit_low=-3e8, profit_high=-1e8, status="ok",
            ),
            expectation_gap=make_gap_dict(
                gap_low=-0.5, gap_high=-0.2,
                rel_low=-0.12, rel_high=-0.05,
            ),
        )
        # 候选对象应该能正常构建
        assert candidate.symbol == "300308.SZ"
        assert candidate.earnings_impact["incremental_net_profit"]["low"] < 0
        # 负预期差降低 alpha_score（相对差绝对值 0.085 → gap_score=0.20）
        # 但不至于归零，因为催化剂和估值分量仍在
        assert candidate.alpha_score.score.mid < 0.6


# ---------------------------------------------------------------------------
# 测试：高增长估值透支
# ---------------------------------------------------------------------------

class TestValuationExhausted:
    """高增长但估值已透支。"""

    def test_high_valuation_reduces_alpha_score(self):
        """高估值分位降低 alpha_score 的估值分量。"""
        low_val = build_candidate(
            valuation_snapshot=make_valuation_snapshot_dict(pe_percentile=0.2),
        )
        high_val = build_candidate(
            valuation_snapshot=make_valuation_snapshot_dict(pe_percentile=0.9),
        )
        assert low_val.alpha_score.components.get("valuation_level", 0) > \
               high_val.alpha_score.components.get("valuation_level", 0)

    def test_high_valuation_increases_risk(self):
        """高估值增加估值风险。"""
        low_val = build_candidate(
            valuation_snapshot=make_valuation_snapshot_dict(pe_percentile=0.2),
        )
        high_val = build_candidate(
            valuation_snapshot=make_valuation_snapshot_dict(pe_percentile=0.9),
        )
        assert high_val.valuation_risk.mid > low_val.valuation_risk.mid


# ---------------------------------------------------------------------------
# 测试：报告生成与溯源
# ---------------------------------------------------------------------------

class TestReportAndTraceability:
    """报告生成与溯源。"""

    def test_report_has_all_required_sections(self):
        """报告包含所有必需章节。"""
        candidate = build_candidate()
        report = CandidateReportBuilder().build(candidate)
        required = {
            "entry_reason", "industry_position", "key_facts",
            "earnings_impact", "market_expectation", "expectation_gap",
            "catalysts", "weakest_assumption", "contradictions",
            "invalidation", "next_validation", "scores",
        }
        assert required.issubset(set(report.report_sections.keys()))

    def test_report_claims_traceable(self):
        """报告中的 Claim 可溯源。"""
        candidate = build_candidate()
        report = CandidateReportBuilder().build(candidate)
        assert len(report.key_claims) > 0
        for claim in report.key_claims:
            assert "claim_id" in claim
            assert claim["claim_id"].startswith("claim_")

    def test_report_formula_references(self):
        """报告包含公式引用。"""
        candidate = build_candidate()
        report = CandidateReportBuilder().build(candidate)
        assert len(report.formula_references) >= 3
        formulas = [f["formula"] for f in report.formula_references]
        assert any("research_score" in f for f in formulas)
        assert any("alpha_score" in f for f in formulas)
        assert any("core_score" in f for f in formulas)

    def test_report_no_trade_advice(self):
        """报告不包含交易建议。"""
        candidate = build_candidate()
        report = CandidateReportBuilder().build(candidate)
        # 只检查 section 值文本，不检查 JSON 键名（避免 industry_position 误匹配）
        values_text = " ".join(str(v) for v in report.report_sections.values()).lower()
        for forbidden in ("buy", "sell", "stop_loss", "止损", "买入", "卖出", "仓位"):
            assert forbidden not in values_text, f"报告不应包含交易建议: {forbidden}"

    def test_source_trace_links_to_upstream(self):
        """来源追踪连接到上游对象。"""
        candidate = build_candidate()
        assert any("thesis:" in t for t in candidate.source_trace)
        assert any("claim:" in t for t in candidate.source_trace)
        assert any("earnings:" in t for t in candidate.source_trace)
        assert any("gap:" in t for t in candidate.source_trace)

    def test_decision_trace_records_scores(self):
        """决策追踪记录三类分数。"""
        candidate = build_candidate()
        trace_text = " ".join(candidate.decision_trace)
        assert "research_score=" in trace_text
        assert "alpha_score=" in trace_text
        assert "core_score=" in trace_text
        assert "gap_status=" in trace_text


# ---------------------------------------------------------------------------
# 测试：持久化与幂等
# ---------------------------------------------------------------------------

class TestPersistenceAndIdempotency:
    """持久化与幂等。"""

    def test_save_and_load_roundtrip(self, tmp_path):
        """保存后加载的数据应一致。"""
        repo = CandidateRepository(tmp_path / "candidates.db")
        candidate = build_candidate()
        repo.save_candidate(candidate)
        loaded = repo.get_candidate(candidate.candidate_id)
        assert loaded is not None
        assert loaded["symbol"] == candidate.symbol
        assert loaded["thesis_id"] == candidate.thesis_id

    def test_save_is_idempotent(self, tmp_path):
        """重复保存不创建重复记录。"""
        repo = CandidateRepository(tmp_path / "candidates.db")
        candidate = build_candidate()
        repo.save_candidate(candidate)
        repo.save_candidate(candidate)
        items = repo.list_candidates(symbol="300308.SZ")
        assert len(items) == 1

    def test_report_save_and_load(self, tmp_path):
        """报告保存后可加载。"""
        repo = CandidateRepository(tmp_path / "candidates.db")
        candidate = build_candidate()
        repo.save_candidate(candidate)
        report = CandidateReportBuilder().build(candidate)
        repo.save_report(report)
        loaded = repo.get_report(candidate.candidate_id)
        assert loaded is not None
        assert "scores" in loaded["report_sections"]

    def test_migration_is_idempotent(self, tmp_path):
        """迁移幂等。"""
        repo = CandidateRepository(tmp_path / "candidates.db")
        repo.migrate()
        repo.migrate()
        stats = repo.stats()
        assert stats["schema_version"] == 1

    def test_malformed_rows_are_skipped_and_limit_degrades(self, tmp_path):
        repo = CandidateRepository(tmp_path / "malformed.db")
        candidate = build_candidate()
        repo.save_candidate(candidate)
        with repo.connect() as connection:
            connection.execute(
                "UPDATE research_candidates SET payload_json=? WHERE candidate_id=?",
                ("not-json", candidate.candidate_id),
            )
        assert repo.get_candidate(candidate.candidate_id) is None
        assert repo.list_candidates(limit="bad") == []


# ---------------------------------------------------------------------------
# 测试：PIT 边界
# ---------------------------------------------------------------------------

class TestPointInTime:
    """PIT 边界测试。"""

    def test_as_of_is_recorded(self):
        """as_of 时点被记录。"""
        candidate = build_candidate(as_of="2026-06-15T00:00:00+00:00")
        assert candidate.as_of == "2026-06-15T00:00:00+00:00"

    def test_available_at_is_set(self):
        """available_at 被设置。"""
        candidate = build_candidate()
        assert candidate.available_at != ""

    def test_expires_at_from_thesis(self):
        """expires_at 从 thesis 的 valid_until 继承。"""
        candidate = build_candidate(
            thesis=make_thesis_dict(valid_until="2027-12-31"),
        )
        assert candidate.expires_at == "2027-12-31"


# ---------------------------------------------------------------------------
# 测试：版本与序列化
# ---------------------------------------------------------------------------

class TestVersionAndSerialization:
    """版本与序列化测试。"""

    def test_model_version_is_set(self):
        """model_version 被设置。"""
        candidate = build_candidate()
        assert candidate.model_version == "candidate_v1"

    def test_roundtrip_serialization(self):
        """序列化/反序列化往返一致。"""
        candidate = build_candidate()
        d = candidate.to_dict()
        restored = ResearchCandidate.from_dict(d)
        assert restored.symbol == candidate.symbol
        assert restored.thesis_id == candidate.thesis_id
        assert restored.research_score.score.low == pytest.approx(candidate.research_score.score.low)
        assert restored.alpha_score.computability == candidate.alpha_score.computability

    def test_score_breakdown_roundtrip(self):
        """ScoreBreakdown 序列化往返一致。"""
        sb = ScoreBreakdown(
            score=Interval(0.5, 0.7),
            components={"a": 0.3, "b": 0.4},
            computability=ScoreComputability.COMPUTABLE.value,
            reason="test",
        )
        d = sb.to_dict()
        restored = ScoreBreakdown.from_dict(d)
        assert restored.score.low == 0.5
        assert restored.score.high == 0.7
        assert restored.components["a"] == 0.3
        assert restored.computability == ScoreComputability.COMPUTABLE.value

    def test_malformed_contracts_degrade_to_defaults(self):
        assert ScoreBreakdown.from_dict(["bad"]).score == Interval(0.0, 0.0)
        candidate = ResearchCandidate.from_dict({
            "evidence_confidence": {"low": "bad", "high": 1},
            "invalidation_conditions": "bad",
            "research_gaps": [None, "gap"],
        })
        assert candidate.evidence_confidence == Interval(0.0, 0.0)
        assert candidate.invalidation_conditions == []
        assert candidate.research_gaps == ["gap"]
        assert CandidateReport.from_dict(["bad"]).report_sections == {}

    def test_builder_skips_malformed_collections(self):
        candidate = CandidateBuilder().build(
            thesis=["bad"],
            earnings_impact=["bad"],
            expectation_gap=["bad"],
            valuation_snapshot=["bad"],
            catalysts=[None, {"window_end": None}],
            research_gaps=[None, "missing"],
        )
        assert candidate.symbol == ""
        assert candidate.research_gaps == ["missing"]
        assert candidate.status == CandidateStatus.DEGRADED.value


# ---------------------------------------------------------------------------
# 测试：API 端点
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """Flask 测试客户端。"""
    import server.routes.candidate_routes as routes
    repo = CandidateRepository(tmp_path / "api.db")
    monkeypatch.setattr(routes, "_repository", repo)
    monkeypatch.setattr(routes, "_builder", CandidateBuilder())
    monkeypatch.setattr(routes, "_report_builder", CandidateReportBuilder())
    app = Flask(__name__)
    app.register_blueprint(routes.candidate_bp)
    app.config["TESTING"] = True
    return app.test_client()


class TestCandidateAPI:
    """Candidate API 端点测试。"""

    def test_health_endpoint(self, api_client):
        """健康检查。"""
        resp = api_client.get("/api/research/candidates/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["data"]["status"] == "ok"

    def test_build_and_retrieve(self, api_client):
        """构建候选后可查询。"""
        resp = api_client.post("/api/research/candidates/build", json={
            "thesis": make_thesis_dict(),
            "earnings_impact": make_earnings_impact_dict(),
            "expectation_gap": make_gap_dict(),
            "valuation_snapshot": make_valuation_snapshot_dict(),
            "sector_id": "optical_module",
            "as_of": "2026-07-01T00:00:00+00:00",
            "industry_core_score": 0.7,
            "company_exposure_score": 0.6,
            "catalysts": make_catalysts(),
        })
        assert resp.status_code == 201
        candidate_id = resp.get_json()["data"]["candidate_id"]

        # 查询单个
        resp = api_client.get(f"/api/research/candidates/{candidate_id}")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["candidate_id"] == candidate_id

    def test_build_without_save(self, api_client):
        """save=false 时不持久化。"""
        resp = api_client.post("/api/research/candidates/build", json={
            "thesis": make_thesis_dict(),
            "earnings_impact": make_earnings_impact_dict(),
            "expectation_gap": make_gap_dict(),
            "valuation_snapshot": make_valuation_snapshot_dict(),
            "save": False,
        })
        assert resp.status_code == 201
        candidate_id = resp.get_json()["data"]["candidate_id"]
        resp = api_client.get(f"/api/research/candidates/{candidate_id}")
        assert resp.status_code == 404

    def test_report_generation(self, api_client):
        """报告自动生成。"""
        resp = api_client.post("/api/research/candidates/build", json={
            "thesis": make_thesis_dict(),
            "earnings_impact": make_earnings_impact_dict(),
            "expectation_gap": make_gap_dict(),
            "valuation_snapshot": make_valuation_snapshot_dict(),
            "catalysts": make_catalysts(),
        })
        candidate_id = resp.get_json()["data"]["candidate_id"]

        resp = api_client.get(f"/api/research/candidates/{candidate_id}/report")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "scores" in data["report_sections"]
        assert len(data["formula_references"]) >= 3

    def test_list_candidates(self, api_client):
        """列表查询。"""
        api_client.post("/api/research/candidates/build", json={
            "thesis": make_thesis_dict(),
            "earnings_impact": make_earnings_impact_dict(),
            "expectation_gap": make_gap_dict(),
            "valuation_snapshot": make_valuation_snapshot_dict(),
        })
        resp = api_client.get("/api/research/candidates?symbol=300308.SZ")
        assert resp.status_code == 200
        assert resp.get_json()["total"] >= 1

    def test_not_found_returns_404(self, api_client):
        """不存在的候选返回 404。"""
        resp = api_client.get("/api/research/candidates/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 端到端 QUANT 候选发布测试
# ---------------------------------------------------------------------------

class TestEndToEndPublish:
    """验证 ResearchCandidatePublisher 从版本化上游对象到 ResearchCandidate 的
    端到端发布链路。

    覆盖：
    1. active Thesis + ok EarningsImpact + ExpectationGap → 成功发布
    2. source_trace 包含上游对象 ID
    3. PIT 字段 (as_of) 正确
    4. 无结构化业务证据时评分为 NOT_COMPUTABLE
    5. 无 active Thesis 时不发布
    6. EarningsImpact status 不是 ok 时不读取
    """

    def test_publish_with_active_thesis_and_ok_impact(self, tmp_path):
        """active Thesis + ok EarningsImpact + ExpectationGap → 成功发布候选。"""
        from core.research.candidate.publisher import ResearchCandidatePublisher
        from core.research.earnings.contracts import EarningsImpact, EarningsStatus
        from core.research.earnings.interval import Interval
        from core.research.expectations.contracts import (
            InvestmentThesis,
            ThesisStatus,
        )
        from core.research.expectations.repository import ExpectationRepository
        from core.research.earnings.repository import EarningsRepository

        # 准备临时仓库
        expectations_repo = ExpectationRepository(
            db_path=tmp_path / "expectations.db",
        )
        earnings_repo = EarningsRepository(
            db_path=tmp_path / "earnings.db",
        )
        candidate_repo = CandidateRepository(
            db_path=tmp_path / "candidates.db",
        )

        # 构造 active Thesis
        thesis = InvestmentThesis(
            thesis_id="thesis_e2e_001",
            company_symbol="300308.SZ",
            sector_id="optical_module",
            title="中际旭创800G光模块放量",
            thesis_summary="800G光模块需求爆发，公司作为龙头受益",
            status=ThesisStatus.ACTIVE.value,
            required_claim_ids=["claim_1", "claim_2"],
            supporting_evidence_ids=["ev_1", "ev_2", "ev_3"],
            valid_from="2026-01-01",
            valid_until="2027-12-31",
        )
        expectations_repo.save("investment_theses", thesis)

        # 构造 ok EarningsImpact
        impact = EarningsImpact(
            impact_id="imp_e2e_001",
            driver_id="drv_e2e_001",
            symbol="300308.SZ",
            company_name="中际旭创",
            product="800G光模块",
            period="2027",
            as_of="2026-07-01",
            status=EarningsStatus.OK.value,
            eps_impact=Interval(0.5, 0.8),
            incremental_revenue=Interval(5e8, 8e8),
            incremental_net_profit=Interval(2e8, 3.5e8),
        )
        earnings_repo.save_impact(impact)

        # 构造 ExpectationGap
        gap_dict = {
            "gap_id": "gap_e2e_001",
            "company_symbol": "300308.SZ",
            "metric": "net_profit",
            "period": "2027",
            "gap": {"low": 0.3, "high": 0.6},
            "relative_gap": {"low": 0.08, "high": 0.15},
            "status": "ok",
            "expectation_source": "consensus",
            "gap_confidence": {"low": 0.6, "high": 0.8},
            "created_at": "2026-07-01T00:00:00+00:00",
        }
        expectations_repo.save("expectation_gaps", type("Gap", (), {
            "to_dict": lambda self: gap_dict,
        })())

        # 执行发布
        publisher = ResearchCandidatePublisher(
            expectations_repo=expectations_repo,
            earnings_repo=earnings_repo,
            candidate_repo=candidate_repo,
        )
        published = publisher.publish_for_symbol("300308.SZ", as_of="2026-07-01")

        # 验证：成功发布 1 条候选
        assert len(published) == 1, f"应发布 1 条候选，实际 {len(published)}"
        candidate = published[0]

        # 验证 symbol 和 thesis_id
        assert candidate["symbol"] == "300308.SZ"
        assert candidate["thesis_id"] == "thesis_e2e_001"
        assert candidate["thesis_status"] == "active"

        # 验证 PIT 字段
        assert candidate["as_of"] == "2026-07-01"

        # 验证 source_trace 包含上游对象 ID
        source_trace_str = " ".join(candidate["source_trace"])
        assert "thesis_e2e_001" in source_trace_str
        assert "imp_e2e_001" in source_trace_str

        # 验证 earnings_impact 字段非空
        assert candidate["earnings_impact"].get("impact_id") == "imp_e2e_001"

        # 验证 expectation_gap 字段非空
        assert candidate["expectation_gap"].get("gap_id") == "gap_e2e_001"

        # 验证无结构化业务证据时评分为 NOT_COMPUTABLE
        assert candidate["industry_core_score"]["computability"] == "not_computable"
        assert candidate["company_exposure_score"]["computability"] == "not_computable"

        # 验证已持久化
        loaded = candidate_repo.get_candidate(candidate["candidate_id"])
        assert loaded is not None
        assert loaded["symbol"] == "300308.SZ"

    def test_no_active_thesis_no_publish(self, tmp_path):
        """无 active Thesis 时不发布。"""
        from core.research.candidate.publisher import ResearchCandidatePublisher
        from core.research.expectations.contracts import (
            InvestmentThesis,
            ThesisStatus,
        )
        from core.research.expectations.repository import ExpectationRepository
        from core.research.earnings.repository import EarningsRepository

        expectations_repo = ExpectationRepository(db_path=tmp_path / "exp.db")
        earnings_repo = EarningsRepository(db_path=tmp_path / "earn.db")
        candidate_repo = CandidateRepository(db_path=tmp_path / "cand.db")

        # 只保存 draft Thesis（非 active）
        thesis = InvestmentThesis(
            thesis_id="thesis_draft_001",
            company_symbol="300308.SZ",
            status=ThesisStatus.DRAFT.value,
        )
        expectations_repo.save("investment_theses", thesis)

        publisher = ResearchCandidatePublisher(
            expectations_repo=expectations_repo,
            earnings_repo=earnings_repo,
            candidate_repo=candidate_repo,
        )
        published = publisher.publish_for_symbol("300308.SZ")
        assert len(published) == 0, "draft Thesis 不应发布"

    def test_impact_not_ok_not_loaded(self, tmp_path):
        """EarningsImpact status 不是 ok 时不读取。"""
        from core.research.candidate.publisher import ResearchCandidatePublisher
        from core.research.earnings.contracts import EarningsImpact, EarningsStatus
        from core.research.expectations.contracts import (
            InvestmentThesis,
            ThesisStatus,
        )
        from core.research.expectations.repository import ExpectationRepository
        from core.research.earnings.repository import EarningsRepository

        expectations_repo = ExpectationRepository(db_path=tmp_path / "exp.db")
        earnings_repo = EarningsRepository(db_path=tmp_path / "earn.db")
        candidate_repo = CandidateRepository(db_path=tmp_path / "cand.db")

        thesis = InvestmentThesis(
            thesis_id="thesis_partial_001",
            company_symbol="300308.SZ",
            status=ThesisStatus.ACTIVE.value,
        )
        expectations_repo.save("investment_theses", thesis)

        # 保存 insufficient_data 状态的 EarningsImpact
        impact = EarningsImpact(
            impact_id="imp_partial_001",
            symbol="300308.SZ",
            status=EarningsStatus.INSUFFICIENT_DATA.value,
        )
        earnings_repo.save_impact(impact)

        publisher = ResearchCandidatePublisher(
            expectations_repo=expectations_repo,
            earnings_repo=earnings_repo,
            candidate_repo=candidate_repo,
        )
        published = publisher.publish_for_symbol("300308.SZ")

        # 候选应发布（有 active Thesis），但 earnings_impact 字段为空
        assert len(published) == 1
        assert published[0]["earnings_impact"] == {}


# ---------------------------------------------------------------------------
# P0.6：候选只读接入正式研究档案
# ---------------------------------------------------------------------------

class TestResearchRunLinkage:
    """P0.6：候选必须能定位到具体 run 和证据缺口。"""

    def test_candidate_with_complete_research_run_links_run_id(self):
        """绑定正式 run 且无阻断维度时 research_completeness=complete。"""
        builder = CandidateBuilder()
        readiness = {
            "fact_completeness": {"status": "ok", "score": 1.0},
            "source_traceability": {"status": "ok", "score": 1.0},
            "source_independence": {"status": "ok", "score": 1.0},
            "pit_integrity": {"status": "ok", "score": 1.0},
            "forecast_input_completeness": {"status": "ok", "score": 1.0},
            "model_computability": {"status": "ok", "score": 1.0},
            "valuation_computability": {"status": "ok", "score": 1.0},
            "research_uncertainty": {"status": "ok", "score": 0.0},
        }
        candidate = builder.build(
            thesis=make_thesis_dict(),
            earnings_impact=make_earnings_impact_dict(),
            expectation_gap=make_gap_dict(),
            valuation_snapshot=make_valuation_snapshot_dict(),
            industry_core_score=0.7,
            company_exposure_score=0.6,
            evidence_confidence=Interval(0.6, 0.8),
            catalysts=make_catalysts(),
            research_run_id="value_20260718T142050Z_abc123",
            research_readiness_summary={
                name: {"status": "ok", "score": 1.0} for name in readiness
            },
            research_gaps=[],
            research_completeness="research_complete",
        )
        assert candidate.research_run_id == "value_20260718T142050Z_abc123"
        assert candidate.research_completeness == "research_complete"
        # 完整研究允许 ACTIVE
        assert candidate.status == CandidateStatus.ACTIVE.value
        # decision_trace 必须记录完整度
        assert any("research_completeness=research_complete" in t for t in candidate.decision_trace)

    def test_candidate_with_blocked_readiness_is_incomplete(self):
        """绑定 run 但存在阻断维度时 research_completeness=incomplete。"""
        builder = CandidateBuilder()
        candidate = builder.build(
            thesis=make_thesis_dict(),
            earnings_impact=make_earnings_impact_dict(),
            expectation_gap=make_gap_dict(),
            valuation_snapshot=make_valuation_snapshot_dict(),
            industry_core_score=0.7,
            company_exposure_score=0.6,
            evidence_confidence=Interval(0.6, 0.8),
            catalysts=make_catalysts(),
            research_run_id="value_20260718T142050Z_def456",
            research_readiness_summary={
                "forecast_input_completeness": {"status": "blocked", "score": None},
            },
            research_gaps=["forecast_input_completeness:blocked"],
            research_completeness="research_incomplete",
        )
        assert candidate.research_completeness == "research_incomplete"
        # 不完整研究强制 DEGRADED，禁止被技术状态/主题热度提升
        assert candidate.status == CandidateStatus.DEGRADED.value

    def test_candidate_without_run_is_observational_only(self):
        """无 run 时强制为 observational_only，禁止伪造 complete。"""
        builder = CandidateBuilder()
        candidate = builder.build(
            thesis=make_thesis_dict(),
            earnings_impact=make_earnings_impact_dict(),
            expectation_gap=make_gap_dict(),
            valuation_snapshot=make_valuation_snapshot_dict(),
            industry_core_score=0.7,
            company_exposure_score=0.6,
            evidence_confidence=Interval(0.6, 0.8),
            catalysts=make_catalysts(),
            research_run_id="",  # 无绑定
            research_readiness_summary={},
            research_gaps=[],
            # 调用方尝试伪造 complete
            research_completeness="research_complete",
        )
        # builder 必须纠正为 observational_only
        assert candidate.research_completeness == "observational_only"
        assert candidate.status == CandidateStatus.DEGRADED.value
        assert candidate.research_run_id == ""

    def test_publisher_reads_research_service_without_writing(self, tmp_path):
        """publisher 通过 research_service 只读读取 latest run，禁止 create_run。"""
        from core.research.candidate.publisher import ResearchCandidatePublisher
        from core.research.expectations.contracts import (
            InvestmentThesis,
            ThesisStatus,
        )
        from core.research.expectations.repository import ExpectationRepository
        from core.research.earnings.repository import EarningsRepository

        expectations_repo = ExpectationRepository(db_path=tmp_path / "exp.db")
        earnings_repo = EarningsRepository(db_path=tmp_path / "earn.db")
        candidate_repo = CandidateRepository(db_path=tmp_path / "cand.db")

        thesis = InvestmentThesis(
            thesis_id="thesis_p06_001",
            company_symbol="300308.SZ",
            status=ThesisStatus.ACTIVE.value,
        )
        expectations_repo.save("investment_theses", thesis)

        # 注入 fake research_service，跟踪调用方法
        class FakeResearchService:
            def __init__(self):
                self.calls = []

            def get_latest(self, symbol):
                self.calls.append(("get_latest", symbol))
                # 模拟无正式 run
                return {
                    "run": None,
                    "dossier": None,
                    "degraded": True,
                    "degradation_reason": "no_runs_for_symbol",
                }

        fake = FakeResearchService()
        publisher = ResearchCandidatePublisher(
            expectations_repo=expectations_repo,
            earnings_repo=earnings_repo,
            candidate_repo=candidate_repo,
            research_service=fake,
        )
        published = publisher.publish_for_symbol("300308.SZ")

        # 仍发布候选（观察候选），但标记为 observational_only
        assert len(published) == 1
        candidate = published[0]
        assert candidate["research_completeness"] == "observational_only"
        assert candidate["research_run_id"] == ""
        assert candidate["status"] == CandidateStatus.DEGRADED.value

        # 只调用只读 get_latest，禁止调用 create_run
        methods_called = [c[0] for c in fake.calls]
        assert "get_latest" in methods_called
        assert "create_run" not in methods_called

    def test_publisher_attaches_run_id_when_latest_available(self, tmp_path):
        """publisher 读到 latest run 时把 run_id 写入候选。"""
        from core.research.candidate.publisher import ResearchCandidatePublisher
        from core.research.expectations.contracts import (
            InvestmentThesis,
            ThesisStatus,
        )
        from core.research.expectations.repository import ExpectationRepository
        from core.research.earnings.repository import EarningsRepository

        expectations_repo = ExpectationRepository(db_path=tmp_path / "exp.db")
        earnings_repo = EarningsRepository(db_path=tmp_path / "earn.db")
        candidate_repo = CandidateRepository(db_path=tmp_path / "cand.db")

        thesis = InvestmentThesis(
            thesis_id="thesis_p06_002",
            company_symbol="300308.SZ",
            status=ThesisStatus.ACTIVE.value,
        )
        expectations_repo.save("investment_theses", thesis)

        class FakeResearchService:
            def get_latest(self, symbol):
                return {
                    "run": {"run_id": "value_20260718T142050Z_xyz789",
                            "symbol": symbol, "status": "partial"},
                    "dossier": {
                        "research_readiness": {
                            "fact_completeness": {"status": "partial", "score": 1.0,
                                                  "missing_items": ["x"]},
                            "forecast_input_completeness": {
                                "status": "blocked", "score": None,
                                "missing_items": ["capacity"],
                            },
                        },
                    },
                    "acceptance": {
                        "degradation_reasons": [
                            "company_forward_driver_evidence_missing",
                        ],
                    },
                    "degraded": False,
                }

        publisher = ResearchCandidatePublisher(
            expectations_repo=expectations_repo,
            earnings_repo=earnings_repo,
            candidate_repo=candidate_repo,
            research_service=FakeResearchService(),
        )
        published = publisher.publish_for_symbol("300308.SZ")

        assert len(published) == 1
        candidate = published[0]
        # run_id 已绑定
        assert candidate["research_run_id"] == "value_20260718T142050Z_xyz789"
        # 存在阻断维度 → research_incomplete
        assert candidate["research_completeness"] == "research_incomplete"
        # 缺口列表包含 degradation_reasons + 阻断维度
        gaps = candidate["research_gaps"]
        assert "company_forward_driver_evidence_missing" in gaps
        assert "forecast_input_completeness:blocked" in gaps
        # source_trace 包含 research_run:...
        assert any("research_run:value_20260718T142050Z_xyz789" in t
                   for t in candidate["source_trace"])
        # 强制 DEGRADED
        assert candidate["status"] == CandidateStatus.DEGRADED.value

    def test_old_candidate_v1_dict_still_loadable(self):
        """旧候选（candidate_v1，无新字段）反序列化时不崩溃。"""
        from core.research.candidate.contracts import ResearchCompleteness

        old_dict = {
            "candidate_id": "cand_old_v1",
            "symbol": "300308.SZ",
            "sector_id": "optical_module",
            "as_of": "2026-01-01",
            "model_version": "candidate_v1",
            "status": "active",
            # 无 research_run_id / research_readiness_summary / research_gaps
            # / research_completeness / candidate_model_version
        }
        candidate = ResearchCandidate.from_dict(old_dict)
        # 旧候选字段使用默认值
        assert candidate.candidate_id == "cand_old_v1"
        assert candidate.model_version == "candidate_v1"
        # 默认为 observational_only（未绑定 run）
        assert candidate.research_completeness == ResearchCompleteness.OBSERVATIONAL_ONLY.value
        assert candidate.research_run_id == ""
        assert candidate.research_gaps == []
        # 保留原 status（不重新评估）
        assert candidate.status == "active"

    def test_candidate_api_accepts_research_run_id(self, api_client):
        """API /build 端点支持 research_run_id 等新字段透传。"""
        resp = api_client.post("/api/research/candidates/build", json={
            "thesis": make_thesis_dict(),
            "earnings_impact": make_earnings_impact_dict(),
            "expectation_gap": make_gap_dict(),
            "valuation_snapshot": make_valuation_snapshot_dict(),
            "industry_core_score": 0.7,
            "company_exposure_score": 0.6,
            "research_run_id": "value_20260718T142050Z_api001",
            "research_readiness_summary": {
                "fact_completeness": {"status": "ok", "score": 1.0},
            },
            "research_gaps": [],
            "research_completeness": "research_complete",
        })
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["research_run_id"] == "value_20260718T142050Z_api001"
        assert data["research_completeness"] == "research_complete"
        assert data["candidate_model_version"] == "candidate_v2"

    def test_completeness_blocks_active_status_even_with_high_scores(self):
        """高 research_score 也不能让 research_incomplete 变成 ACTIVE。"""
        builder = CandidateBuilder()
        candidate = builder.build(
            thesis=make_thesis_dict(required_claims=5),
            earnings_impact=make_earnings_impact_dict(status="ok"),
            expectation_gap=make_gap_dict(status="ok"),
            valuation_snapshot=make_valuation_snapshot_dict(pe_percentile=0.2),
            industry_core_score=0.9,
            company_exposure_score=0.85,
            evidence_confidence=Interval(0.9, 1.0),
            catalysts=make_catalysts(count=3),
            research_run_id="value_20260718T142050Z_high001",
            research_readiness_summary={
                "forecast_input_completeness": {"status": "blocked"},
            },
            research_gaps=["forecast_input_completeness:blocked"],
            research_completeness="research_incomplete",
        )
        # 即使 research_score 高，仍因不完整强制 DEGRADED
        assert candidate.research_score.score.mid > 0.5
        assert candidate.status == CandidateStatus.DEGRADED.value
