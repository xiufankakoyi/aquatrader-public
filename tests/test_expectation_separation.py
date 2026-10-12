"""P1.7 专项测试：公司指引、第三方一致预期、市场隐含预期与 AquaTrade 预测分离。

覆盖 4 个验收点：
1. 四类预期不会互相覆盖（独立表 + 独立 dataclass + 独立 kind 字段）
2. 任意 as_of 可重建当时可见共识（PIT 重建 + 机构去重 + 修订链）
3. 无共识时预期差不可计算（ConsensusSummary.status != "ok" → 预期差标记 EXPECTATION_DATA_MISSING）
4. 每条共识保留原 provider 和发布日期（institution_id + published_at 必填）

附加覆盖：
- ConsensusSummaryEngine 中位数/分位数/来源数/分歧度/修正方向
- AquatradeForecastBuilder 必填字段校验
- 第四类预期（AquaTrade）不冒充公司指引或一致预期
- /api/research/expectations/aquatrade_forecasts 与 /consensus_summary 端点 smoke
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from flask import Flask

from core.research.earnings.interval import Interval
from core.research.expectations.contracts import (
    AquatradeForecast,
    CompanyGuidance,
    ConsensusEstimate,
    ConsensusSummary,
    ExpectationGap,
    ExpectationKind,
    ExpectationSource,
    ExpectationValue,
    GapStatus,
    ImpliedExpectation,
)
from core.research.expectations.engine import (
    AquatradeForecastBuilder,
    ConsensusSummaryEngine,
    ExpectationGapEngine,
    ImpliedExpectationEngine,
    visible_as_of,
)
from core.research.expectations.repository import ExpectationRepository


# ---------------------------------------------------------------------------
# 测试夹具：构造四类独立预期
# ---------------------------------------------------------------------------

PUBLISHED_AT_OLD = "2026-01-10T00:00:00+00:00"
PUBLISHED_AT_MID = "2026-03-15T00:00:00+00:00"
PUBLISHED_AT_NEW = "2026-06-20T00:00:00+00:00"
AS_OF_BEFORE_NEW = "2026-05-01T00:00:00+00:00"
AS_OF_AFTER_NEW = "2026-07-01T00:00:00+00:00"


def make_value(
    *, metric: str = "net_profit",
    low: float = 4.0, high: float = 5.0,
    period: str = "2027",
    published_at: str = PUBLISHED_AT_OLD,
    retrieved_at: str | None = None,
    unit: str = "yuan",
    currency: str = "CNY",
    confidence_low: float = 0.7,
    confidence_high: float = 0.9,
    source_claim_id: str = "claim-x",
    source_evidence_id: str = "ev-x",
) -> ExpectationValue:
    """构造完整可用的 ExpectationValue。"""
    return ExpectationValue(
        metric=metric,
        value=Interval(low, high),
        unit=unit, currency=currency, period=period,
        as_of=published_at, data_version="v1",
        source_claim_id=source_claim_id,
        source_evidence_id=source_evidence_id,
        extraction_method="licensed_feed",
        confidence=Interval(confidence_low, confidence_high),
        published_at=published_at,
        retrieved_at=retrieved_at or published_at,
    )


def make_consensus(
    *, institution: str, analyst: str = "",
    low: float = 4.0, high: float = 5.0,
    published_at: str = PUBLISHED_AT_OLD,
    period: str = "2027",
    company: str = "TEST",
    revision_of: str = "",
    consensus_id: str = "",
    metric: str = "net_profit",
) -> ConsensusEstimate:
    """构造单家机构的一次预期（保留 institution_id + published_at）。"""
    return ConsensusEstimate(
        consensus_id=consensus_id or f"cons_{institution}_{published_at[:10]}",
        company_symbol=company,
        values=[make_value(
            metric=metric, low=low, high=high,
            period=period, published_at=published_at,
            source_claim_id=f"claim-{institution}",
            source_evidence_id=f"ev-{institution}",
        )],
        provider=institution,
        institution_id=institution,
        analyst_id=analyst,
        sample_size=1,
        revision_of=revision_of,
        created_at=published_at,
    )


# ---------------------------------------------------------------------------
# 验收点 1：四类预期不会互相覆盖
# ---------------------------------------------------------------------------

class TestAcceptancePoint1FourIndependentDomains:
    """四类预期互不覆盖：独立 dataclass + 独立 kind 字段 + 独立表 + 独立 API 端点。"""

    def test_four_kinds_are_distinct(self):
        """四类预期 kind 字段互不相同。"""
        kinds = {
            ExpectationKind.COMPANY_GUIDANCE.value,
            ExpectationKind.CONSENSUS.value,
            ExpectationKind.IMPLIED.value,
            ExpectationKind.AQUATRADE_FORECAST.value,
        }
        assert kinds == {"company_guidance", "consensus", "implied", "aquatrade_forecast"}
        assert len(kinds) == 4

    def test_four_dataclasses_do_not_overwrite_each_other_in_repository(self, tmp_path):
        """同公司的四类预期可独立持久化与查询，互不覆盖。"""
        repo = ExpectationRepository(tmp_path / "sep.db")

        guidance = CompanyGuidance(
            company_symbol="TEST",
            values=[make_value(low=4.0, high=4.5, source_claim_id="cg")],
        )
        consensus = make_consensus(institution="inst1", low=4.5, high=5.0)
        implied = ImpliedExpectation(
            company_symbol="TEST",
            values=[make_value(low=5.0, high=5.5, source_claim_id="im")],
            model_name="reverse_dcf", model_version="v1",
        )
        aqua = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.2, value_high=4.8,
            unit="yuan", currency="CNY", model_version="aquatr-v1",
            scenario="base", source_evidence_id="ev-aqf",
        )

        repo.save("company_guidance", guidance)
        repo.save("consensus_estimates", consensus)
        repo.save("implied_expectations", implied)
        repo.save_aquatrade_forecast(aqua)

        # 四张独立表
        stats = repo.stats()["counts"]
        assert stats["company_guidance"] == 1
        assert stats["consensus_estimates"] == 1
        assert stats["implied_expectations"] == 1
        assert stats["aquatrade_forecasts"] == 1
        assert stats["consensus_summaries"] == 0  # 未计算 summary

        # 每张表 kind 字段保持独立
        assert repo.get("company_guidance", guidance.guidance_id)["kind"] == "company_guidance"
        assert repo.get("consensus_estimates", consensus.consensus_id)["kind"] == "consensus"
        assert repo.get("implied_expectations", implied.implied_id)["kind"] == "implied"
        aqf_payload = repo.get("aquatrade_forecasts", aqua.forecast_id)
        assert aqf_payload["kind"] == "aquatrade_forecast"
        assert aqf_payload["expectation_source"] == ExpectationSource.AQUATRADE_FORECAST.value

    def test_aquatrade_forecast_does_not_impersonate_consensus(self):
        """AquaTrade 预测不得冒充公司指引或一致预期。"""
        aqf = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.2, value_high=4.8,
            unit="yuan", currency="CNY", model_version="v1",
        )
        assert aqf.kind == ExpectationKind.AQUATRADE_FORECAST.value
        assert aqf.kind != ExpectationKind.CONSENSUS.value
        assert aqf.kind != ExpectationKind.COMPANY_GUIDANCE.value
        assert aqf.kind != ExpectationKind.IMPLIED.value
        assert aqf.expectation_source == ExpectationSource.AQUATRADE_FORECAST.value
        assert ExpectationSource.priority(aqf.expectation_source) == 5

    def test_revision_chain_preserves_aquatrade_versions(self, tmp_path):
        """AquaTrade 预测修订链不被覆盖。"""
        repo = ExpectationRepository(tmp_path / "rev.db")
        a1 = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=4.5,
            unit="yuan", currency="CNY", model_version="v1",
        )
        a2 = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_NEW, value_low=4.5, value_high=5.0,
            unit="yuan", currency="CNY", model_version="v2",
            revision_of=a1.forecast_id, revision_reason="capex_increase",
        )
        repo.save_aquatrade_forecast(a1)
        repo.save_aquatrade_forecast(a2)
        # 两个 forecast_id 独立存在
        assert repo.get("aquatrade_forecasts", a1.forecast_id) is not None
        assert repo.get("aquatrade_forecasts", a2.forecast_id) is not None
        latest = repo.get_latest_aquatrade_forecast(
            company_symbol="TEST", period="2027", as_of=AS_OF_AFTER_NEW,
        )
        assert latest is not None
        assert latest["forecast_id"] == a2.forecast_id
        assert latest["revision_of"] == a1.forecast_id


# ---------------------------------------------------------------------------
# 验收点 2：任意 as_of 可重建当时可见共识
# ---------------------------------------------------------------------------

class TestAcceptancePoint2PITReconstruction:
    """PIT 重建：给定任意 as_of，能重建当时可见的共识。"""

    def test_reconstruct_visible_consensus_at_as_of(self, tmp_path):
        """as_of=2026-05-01 时只能看到 old/mid 两家机构的预期，看不到 6 月新预期。"""
        repo = ExpectationRepository(tmp_path / "pit.db")
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", low=4.0, high=4.5, published_at=PUBLISHED_AT_OLD,
        ))
        repo.save("consensus_estimates", make_consensus(
            institution="inst2", low=4.5, high=5.0, published_at=PUBLISHED_AT_MID,
        ))
        repo.save("consensus_estimates", make_consensus(
            institution="inst3", low=5.5, high=6.0, published_at=PUBLISHED_AT_NEW,
        ))

        visible = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_BEFORE_NEW,
        )
        providers = {p["institution_id"] for p in visible}
        assert providers == {"inst1", "inst2"}
        assert "inst3" not in providers

        visible_after = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_AFTER_NEW,
        )
        providers_after = {p["institution_id"] for p in visible_after}
        assert providers_after == {"inst1", "inst2", "inst3"}

    def test_same_institution_keeps_latest_visible_revision(self, tmp_path):
        """同一机构在 as_of 前有多次修订时，保留最新可见版本。"""
        repo = ExpectationRepository(tmp_path / "rev2.db")
        # inst1 第一次发布
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", low=4.0, high=4.5,
            published_at=PUBLISHED_AT_OLD, consensus_id="c_v1",
        ))
        # inst1 修订（在 as_of 前）
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", low=4.4, high=4.8,
            published_at=PUBLISHED_AT_MID, consensus_id="c_v2",
            revision_of="c_v1",
        ))
        # inst1 再次修订（as_of 之后，不可见）
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", low=5.5, high=6.0,
            published_at=PUBLISHED_AT_NEW, consensus_id="c_v3",
            revision_of="c_v2",
        ))

        visible = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_BEFORE_NEW,
        )
        assert len(visible) == 1
        assert visible[0]["consensus_id"] == "c_v2"
        assert visible[0]["revision_of"] == "c_v1"
        # 不应看到 v3
        assert all(p["consensus_id"] != "c_v3" for p in visible)

    def test_pit_reconstruction_idempotent(self, tmp_path):
        """重复调用 PIT 重建返回相同结果。"""
        repo = ExpectationRepository(tmp_path / "idem.db")
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", low=4.0, high=4.5,
        ))
        repo.save("consensus_estimates", make_consensus(
            institution="inst2", low=4.5, high=5.0,
        ))
        first = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_AFTER_NEW,
        )
        second = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_AFTER_NEW,
        )
        assert {p["consensus_id"] for p in first} == {p["consensus_id"] for p in second}

    def test_empty_when_no_visible_estimates(self, tmp_path):
        """as_of 早于所有发布时间时返回空列表。"""
        repo = ExpectationRepository(tmp_path / "empty.db")
        repo.save("consensus_estimates", make_consensus(
            institution="inst1", published_at=PUBLISHED_AT_NEW,
        ))
        visible = repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", "2025-01-01T00:00:00+00:00",
        )
        assert visible == []

    def test_missing_required_args_returns_empty(self, tmp_path):
        """缺少必填参数时返回空列表，不抛异常。"""
        repo = ExpectationRepository(tmp_path / "args.db")
        repo.save("consensus_estimates", make_consensus(institution="inst1"))
        assert repo.list_consensus_visible_as_of("", "net_profit", "2027", AS_OF_AFTER_NEW) == []
        assert repo.list_consensus_visible_as_of("TEST", "", "2027", AS_OF_AFTER_NEW) == []
        assert repo.list_consensus_visible_as_of("TEST", "net_profit", "", AS_OF_AFTER_NEW) == []
        assert repo.list_consensus_visible_as_of("TEST", "net_profit", "2027", "") == []


# ---------------------------------------------------------------------------
# 验收点 3：无共识时预期差不可计算
# ---------------------------------------------------------------------------

class TestAcceptancePoint3NoConsensusNoGap:
    """无共识时预期差不可计算：ConsensusSummary.status != "ok" → 预期差 EXPECTATION_DATA_MISSING。"""

    def test_insufficient_sources_blocks_gap_computation(self):
        """来源不足时 ConsensusSummary.status="insufficient_sources"，预期差不可计算。"""
        # 仅 1 家机构（少于阈值 3）
        estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.5),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        assert summary.status == "insufficient_sources"
        assert summary.source_count == 1
        assert summary.median == 0.0
        assert "source_count=1 < threshold=3" in summary.notes

        # 预期差应返回 EXPECTATION_DATA_MISSING
        gap = ExpectationGapEngine().compute(
            company_symbol="TEST", metric="net_profit", period="2027",
            research_value=Interval(4.5, 5.0),
            earnings_result_id="e1", earnings_data_version="ev1",
            expectation_id="", expectation_kind="consensus",
            expectation_value=None,
            market_data_version="m1", as_of=AS_OF_AFTER_NEW,
        )
        assert gap.status == GapStatus.EXPECTATION_DATA_MISSING.value

    def test_no_visible_estimates_returns_no_data(self):
        """无可见预期时返回 status="no_data"。"""
        estimates = [
            make_consensus(institution="inst1", published_at=PUBLISHED_AT_NEW),
        ]
        # as_of 早于所有发布时间
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of="2025-01-01T00:00:00+00:00", estimates=estimates,
        )
        assert summary.status == "no_data"
        assert "no_visible_estimates_as_of" in summary.notes

    def test_sufficient_sources_produces_ok_summary(self):
        """来源数 ≥ 阈值时返回 status="ok"。"""
        estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.5),
            make_consensus(institution="inst2", low=4.5, high=5.0),
            make_consensus(institution="inst3", low=4.8, high=5.2),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        assert summary.status == "ok"
        assert summary.source_count == 3
        # median of [4.25, 4.75, 5.0] = 4.75
        assert summary.median == pytest.approx(4.75)

    def test_custom_threshold(self):
        """可自定义最小来源数阈值。"""
        estimates = [
            make_consensus(institution="inst1"),
            make_consensus(institution="inst2"),
        ]
        # 默认阈值 3 → 不足
        summary_default = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        assert summary_default.status == "insufficient_sources"

        # 阈值 2 → 充足
        summary_custom = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
            min_sources_threshold=2,
        )
        assert summary_custom.status == "ok"
        assert summary_custom.source_count == 2


# ---------------------------------------------------------------------------
# 验收点 4：每条共识保留原 provider 和发布日期
# ---------------------------------------------------------------------------

class TestAcceptancePoint4ProviderAndPublishedAtPreserved:
    """每条共识保留原 provider（institution_id）和发布日期（published_at）。"""

    def test_institution_and_published_at_persisted(self, tmp_path):
        """ConsensusEstimate 持久化后 institution_id 和 published_at 可读回。"""
        repo = ExpectationRepository(tmp_path / "prov.db")
        est = make_consensus(
            institution="cicc",
            analyst="analyst_a",
            low=4.0, high=4.5,
            published_at=PUBLISHED_AT_MID,
        )
        repo.save("consensus_estimates", est)

        loaded = repo.get("consensus_estimates", est.consensus_id)
        assert loaded["institution_id"] == "cicc"
        assert loaded["analyst_id"] == "analyst_a"
        # published_at 在 ExpectationValue 内部
        assert loaded["values"][0]["published_at"] == PUBLISHED_AT_MID
        assert loaded["values"][0]["retrieved_at"] == PUBLISHED_AT_MID

    def test_contributing_providers_recorded_in_summary(self):
        """ConsensusSummary.contributing_providers 保留所有机构标识。"""
        estimates = [
            make_consensus(institution="cicc", low=4.0, high=4.5),
            make_consensus(institution="htsc", low=4.5, high=5.0),
            make_consensus(institution="guotai", low=4.8, high=5.2),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        assert summary.status == "ok"
        assert set(summary.contributing_providers) == {"cicc", "htsc", "guotai"}
        assert len(summary.contributing_estimate_ids) == 3

    def test_contributing_estimate_ids_unique(self):
        """同一家机构的多条修订，贡献 estimate_id 去重前已展开。"""
        est_v1 = make_consensus(
            institution="cicc", low=4.0, high=4.5,
            published_at=PUBLISHED_AT_OLD, consensus_id="cicc_v1",
        )
        est_v2 = make_consensus(
            institution="cicc", low=4.2, high=4.8,
            published_at=PUBLISHED_AT_MID, consensus_id="cicc_v2",
            revision_of="cicc_v1",
        )
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=[est_v1, est_v2],
            min_sources_threshold=1,
        )
        # 两条 estimate 都参与，但 unique providers 只有一个
        assert summary.source_count == 1
        assert set(summary.contributing_estimate_ids) == {"cicc_v1", "cicc_v2"}
        assert summary.contributing_providers == ["cicc"]


# ---------------------------------------------------------------------------
# 附加：ConsensusSummaryEngine 统计功能
# ---------------------------------------------------------------------------

class TestConsensusSummaryStatistics:
    """ConsensusSummaryEngine 统计：中位数/分位数/分歧度/修正方向。"""

    def test_median_and_percentiles(self):
        """中位数、25/75 分位数计算正确（NumPy linear 默认插值）。"""
        estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.0),
            make_consensus(institution="inst2", low=5.0, high=5.0),
            make_consensus(institution="inst3", low=6.0, high=6.0),
            make_consensus(institution="inst4", low=7.0, high=7.0),
            make_consensus(institution="inst5", low=8.0, high=8.0),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        # values = [4, 5, 6, 7, 8]
        assert summary.median == pytest.approx(6.0)
        assert summary.mean == pytest.approx(6.0)
        assert summary.min_value == pytest.approx(4.0)
        assert summary.max_value == pytest.approx(8.0)
        # NumPy linear: rank_25 = 0.25 * 4 = 1.0 → idx=1, frac=0 → sorted[1] = 5
        assert summary.p25 == pytest.approx(5.0)
        # rank_75 = 0.75 * 4 = 3.0 → idx=3, frac=0 → sorted[3] = 7
        assert summary.p75 == pytest.approx(7.0)

    def test_dispersion_metrics(self):
        """分歧度（标准差 + 极差）计算正确。"""
        estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.0),
            make_consensus(institution="inst2", low=6.0, high=6.0),
            make_consensus(institution="inst3", low=8.0, high=8.0),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        # 极差 = 8 - 4 = 4
        assert summary.dispersion_range == pytest.approx(4.0)
        # 总体标准差: var = ((4-6)^2 + (6-6)^2 + (8-6)^2)/3 = 8/3 ≈ 2.667, std ≈ 1.633
        assert summary.dispersion_std == pytest.approx(1.633, rel=1e-2)

    def test_revision_direction_up(self):
        """修订方向：median 上升 → "up"。"""
        prev_estimates = [
            make_consensus(institution="inst1", low=3.0, high=3.5),
            make_consensus(institution="inst2", low=3.5, high=4.0),
            make_consensus(institution="inst3", low=4.0, high=4.5),
        ]
        prev_summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, estimates=prev_estimates,
        )
        # 新一期预期上调
        new_estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.5, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst2", low=4.5, high=5.0, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst3", low=5.0, high=5.5, published_at=PUBLISHED_AT_MID),
        ]
        new_summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_BEFORE_NEW, estimates=new_estimates,
            previous_summary=prev_summary,
        )
        assert new_summary.revision_direction == "up"
        assert new_summary.revision_magnitude > 0

    def test_revision_direction_down(self):
        """修订方向：median 下降 → "down"。"""
        prev_summary = ConsensusSummary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, median=6.0, status="ok",
        )
        new_estimates = [
            make_consensus(institution="inst1", low=3.0, high=3.5, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst2", low=3.5, high=4.0, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst3", low=4.0, high=4.5, published_at=PUBLISHED_AT_MID),
        ]
        new_summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_BEFORE_NEW, estimates=new_estimates,
            previous_summary=prev_summary,
        )
        assert new_summary.revision_direction == "down"
        assert new_summary.revision_magnitude < 0

    def test_revision_direction_flat(self):
        """修订方向：median 不变 → "flat"。"""
        prev_summary = ConsensusSummary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, median=4.0, status="ok",
        )
        new_estimates = [
            make_consensus(institution="inst1", low=3.0, high=5.0, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst2", low=3.5, high=4.5, published_at=PUBLISHED_AT_MID),
            make_consensus(institution="inst3", low=4.0, high=4.0, published_at=PUBLISHED_AT_MID),
        ]
        # 中位数 = sorted([4.0, 4.0, 4.0])[1] = 4.0
        new_summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_BEFORE_NEW, estimates=new_estimates,
            previous_summary=prev_summary,
        )
        assert new_summary.revision_direction == "flat"
        assert new_summary.revision_magnitude == pytest.approx(0.0)

    def test_revision_direction_unknown_without_previous(self):
        """无前序 summary 时 revision_direction="unknown"。"""
        estimates = [
            make_consensus(institution="inst1", low=4.0, high=4.5),
            make_consensus(institution="inst2", low=4.5, high=5.0),
            make_consensus(institution="inst3", low=5.0, high=5.5),
        ]
        summary = ConsensusSummaryEngine().compute_summary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, estimates=estimates,
        )
        assert summary.revision_direction == "unknown"
        assert summary.revision_magnitude == 0.0
        assert summary.previous_summary_id == ""

    def test_filter_visible_estimates(self):
        """filter_visible_estimates 仅返回含可见值的 ConsensusEstimate。"""
        estimates = [
            make_consensus(institution="inst1", published_at=PUBLISHED_AT_OLD),
            make_consensus(institution="inst2", published_at=PUBLISHED_AT_NEW),
        ]
        engine = ConsensusSummaryEngine()
        visible = engine.filter_visible_estimates(
            estimates, metric="net_profit", period="2027",
            as_of=AS_OF_BEFORE_NEW,
        )
        assert len(visible) == 1
        assert visible[0].institution_id == "inst1"


# ---------------------------------------------------------------------------
# 附加：AquatradeForecastBuilder 必填校验
# ---------------------------------------------------------------------------

class TestAquatradeForecastBuilderValidation:
    """AquatradeForecastBuilder 必填字段校验，禁止硬编码默认值。"""

    def test_missing_company_symbol_raises(self):
        with pytest.raises(ValueError, match="company_symbol"):
            AquatradeForecastBuilder().build(
                company_symbol="", metric="net_profit", period="2027",
                as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
                unit="yuan", currency="CNY", model_version="v1",
            )

    def test_missing_model_version_raises(self):
        with pytest.raises(ValueError, match="model_version"):
            AquatradeForecastBuilder().build(
                company_symbol="TEST", metric="net_profit", period="2027",
                as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
                unit="yuan", currency="CNY", model_version="",
            )

    def test_missing_as_of_raises(self):
        with pytest.raises(ValueError, match="as_of"):
            AquatradeForecastBuilder().build(
                company_symbol="TEST", metric="net_profit", period="2027",
                as_of="", value_low=4.0, value_high=5.0,
                unit="yuan", currency="CNY", model_version="v1",
            )

    def test_assumptions_snapshot_and_formula_trace_preserved(self):
        """构建时 assumptions_snapshot 和 formula_trace 保留，支持审计。"""
        aqf = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
            unit="yuan", currency="CNY", model_version="v1",
            assumptions_snapshot={"share_count": 1e8, "gross_margin": 0.25},
            formula_trace=["net_profit = revenue * gross_margin - opex"],
        )
        assert aqf.assumptions_snapshot == {"share_count": 1e8, "gross_margin": 0.25}
        assert aqf.formula_trace == ["net_profit = revenue * gross_margin - opex"]
        assert aqf.values[0].extraction_method == "aquatrade_research_pipeline"

    def test_value_interval_preserved(self):
        """AquaTrade 预测的区间值被保留。"""
        aqf = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="revenue", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=1e9, value_high=1.2e9,
            unit="yuan", currency="CNY", model_version="v1",
        )
        assert aqf.values[0].value.low == pytest.approx(1e9)
        assert aqf.values[0].value.high == pytest.approx(1.2e9)
        assert aqf.values[0].metric == "revenue"


# ---------------------------------------------------------------------------
# 附加：visible_as_of 行为
# ---------------------------------------------------------------------------

class TestVisibleAsOfBehavior:
    """visible_as_of 工具函数：published_at 和 retrieved_at 都 ≤ as_of 才可见。"""

    def test_visible_when_both_times_le_cutoff(self):
        v = make_value(published_at="2026-01-01T00:00:00+00:00",
                       retrieved_at="2026-01-02T00:00:00+00:00")
        assert visible_as_of(v, "2026-02-01T00:00:00+00:00") is True

    def test_invisible_when_published_after_cutoff(self):
        v = make_value(published_at="2026-07-01T00:00:00+00:00",
                       retrieved_at="2026-01-02T00:00:00+00:00")
        assert visible_as_of(v, "2026-02-01T00:00:00+00:00") is False

    def test_invisible_when_retrieved_after_cutoff(self):
        v = make_value(published_at="2026-01-01T00:00:00+00:00",
                       retrieved_at="2026-07-01T00:00:00+00:00")
        assert visible_as_of(v, "2026-02-01T00:00:00+00:00") is False

    def test_invisible_when_missing_timestamps(self):
        v = make_value(published_at="", retrieved_at="")
        assert visible_as_of(v, "2026-02-01T00:00:00+00:00") is False

    def test_non_historical_mode_bypasses_filter(self):
        v = make_value(published_at="2099-01-01T00:00:00+00:00",
                       retrieved_at="2099-01-02T00:00:00+00:00")
        assert visible_as_of(v, "2026-02-01T00:00:00+00:00", historical=False) is True


# ---------------------------------------------------------------------------
# 附加：API smoke
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import server.routes.expectations_routes as routes
    repo = ExpectationRepository(tmp_path / "api.db")
    monkeypatch.setattr(routes, "_repository", repo)
    app = Flask(__name__)
    app.register_blueprint(routes.expectations_bp)
    app.config["TESTING"] = True
    return app.test_client()


class TestAquatradeForecastAPI:
    """AquaTrade 预测 API 端点 smoke。"""

    def test_post_aquatrade_forecast(self, api_client):
        resp = api_client.post("/api/research/expectations/aquatrade_forecasts", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": PUBLISHED_AT_OLD, "value_low": 4.0, "value_high": 5.0,
            "unit": "yuan", "currency": "CNY", "model_version": "v1",
            "scenario": "base", "source_evidence_id": "ev-1",
            "assumptions_snapshot": {"k": "v"}, "formula_trace": ["f1"],
        })
        assert resp.status_code == 201
        data = resp.get_json()["data"]
        assert data["kind"] == "aquatrade_forecast"
        assert data["expectation_source"] == "aquatrade_forecast"
        assert data["model_version"] == "v1"
        assert data["assumptions_snapshot"] == {"k": "v"}
        assert data["formula_trace"] == ["f1"]

    def test_post_missing_required_returns_400(self, api_client):
        resp = api_client.post("/api/research/expectations/aquatrade_forecasts", json={
            "company_symbol": "", "metric": "net_profit", "period": "2027",
            "as_of": PUBLISHED_AT_OLD, "value_low": 4.0, "value_high": 5.0,
            "unit": "yuan", "currency": "CNY", "model_version": "v1",
        })
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "INSUFFICIENT_INPUT"

    def test_list_with_as_of_filter(self, api_client):
        api_client.post("/api/research/expectations/aquatrade_forecasts", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": PUBLISHED_AT_OLD, "value_low": 4.0, "value_high": 5.0,
            "unit": "yuan", "currency": "CNY", "model_version": "v1",
        })
        api_client.post("/api/research/expectations/aquatrade_forecasts", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": PUBLISHED_AT_NEW, "value_low": 5.0, "value_high": 6.0,
            "unit": "yuan", "currency": "CNY", "model_version": "v2",
        })
        # as_of=2026-05-01 时只能看到 v1
        resp = api_client.get(
            "/api/research/expectations/aquatrade_forecasts"
            "?company_symbol=TEST&period=2027&as_of=2026-05-01T00:00:00+00:00"
        )
        assert resp.status_code == 200
        items = resp.get_json()["data"]
        assert len(items) == 1
        assert items[0]["model_version"] == "v1"

    def test_latest_endpoint(self, api_client):
        api_client.post("/api/research/expectations/aquatrade_forecasts", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": PUBLISHED_AT_OLD, "value_low": 4.0, "value_high": 5.0,
            "unit": "yuan", "currency": "CNY", "model_version": "v1",
        })
        resp = api_client.get(
            "/api/research/expectations/aquatrade_forecasts/latest"
            "?company_symbol=TEST&period=2027&as_of=2026-07-01T00:00:00+00:00"
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["model_version"] == "v1"

    def test_latest_not_found_returns_404(self, api_client):
        resp = api_client.get(
            "/api/research/expectations/aquatrade_forecasts/latest"
            "?company_symbol=MISSING&period=2027"
        )
        assert resp.status_code == 404


class TestConsensusSummaryAPI:
    """一致预期聚合统计 API 端点 smoke。"""

    def test_post_summary_insufficient_sources(self, api_client):
        """仅 1 家机构时返回 insufficient_sources。"""
        api_client.post("/api/research/expectations/consensus", json={
            "company_symbol": "TEST",
            "values": [make_value(low=4.0, high=4.5, source_claim_id="c1").to_dict()],
            "institution_id": "inst1", "provider": "inst1", "sample_size": 1,
        })
        resp = api_client.post("/api/research/expectations/consensus_summary", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": AS_OF_AFTER_NEW, "save": False,
        })
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "insufficient_sources"
        assert data["source_count"] == 1

    def test_post_summary_ok(self, api_client):
        """3 家机构时返回 ok，median 计算正确。"""
        for i, (low, high, inst) in enumerate([
            (4.0, 4.5, "inst1"), (4.5, 5.0, "inst2"), (5.0, 5.5, "inst3"),
        ]):
            api_client.post("/api/research/expectations/consensus", json={
                "company_symbol": "TEST",
                "values": [make_value(low=low, high=high, source_claim_id=f"c{i}").to_dict()],
                "institution_id": inst, "provider": inst, "sample_size": 1,
            })
        resp = api_client.post("/api/research/expectations/consensus_summary", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": AS_OF_AFTER_NEW, "save": True,
        })
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["status"] == "ok"
        assert data["source_count"] == 3
        # values = [4.25, 4.75, 5.25] -> median = 4.75
        assert data["median"] == pytest.approx(4.75)
        assert set(data["contributing_providers"]) == {"inst1", "inst2", "inst3"}
        # GET 查询已缓存
        get_resp = api_client.get(
            "/api/research/expectations/consensus_summary"
            "?company_symbol=TEST&metric=net_profit&period=2027"
        )
        assert get_resp.status_code == 200
        items = get_resp.get_json()["data"]
        assert any(item["status"] == "ok" for item in items)

    def test_post_missing_required_returns_400(self, api_client):
        resp = api_client.post("/api/research/expectations/consensus_summary", json={
            "company_symbol": "", "metric": "net_profit", "period": "2027",
            "as_of": AS_OF_AFTER_NEW,
        })
        assert resp.status_code == 400
        assert resp.get_json()["error"]["code"] == "INSUFFICIENT_INPUT"

    def test_pit_filter_in_summary_endpoint(self, api_client):
        """summary 端点 PIT 过滤：as_of 早于某些发布时间时不计入。"""
        api_client.post("/api/research/expectations/consensus", json={
            "company_symbol": "TEST",
            "values": [make_value(low=4.0, high=4.5, source_claim_id="c1",
                                  published_at=PUBLISHED_AT_OLD).to_dict()],
            "institution_id": "inst_old", "provider": "inst_old", "sample_size": 1,
        })
        api_client.post("/api/research/expectations/consensus", json={
            "company_symbol": "TEST",
            "values": [make_value(low=5.0, high=5.5, source_claim_id="c2",
                                  published_at=PUBLISHED_AT_NEW).to_dict()],
            "institution_id": "inst_new", "provider": "inst_new", "sample_size": 1,
        })
        # as_of=2026-05-01 仅可见 inst_old
        resp = api_client.post("/api/research/expectations/consensus_summary", json={
            "company_symbol": "TEST", "metric": "net_profit", "period": "2027",
            "as_of": AS_OF_BEFORE_NEW, "save": False,
        })
        data = resp.get_json()["data"]
        assert data["status"] == "insufficient_sources"
        assert data["source_count"] == 1
        assert "inst_old" in data["contributing_providers"]
        assert "inst_new" not in data["contributing_providers"]


# ---------------------------------------------------------------------------
# 附加：序列化与重启回读
# ---------------------------------------------------------------------------

class TestSerializationRoundtrip:
    """dataclass to_dict / from_dict 往返一致性。"""

    def test_aquatrade_forecast_roundtrip(self):
        aqf = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
            unit="yuan", currency="CNY", model_version="v1",
            assumptions_snapshot={"k": 1}, formula_trace=["f"],
        )
        d = aqf.to_dict()
        restored = AquatradeForecast.from_dict(d)
        assert restored.forecast_id == aqf.forecast_id
        assert restored.company_symbol == aqf.company_symbol
        assert restored.model_version == aqf.model_version
        assert restored.assumptions_snapshot == {"k": 1}
        assert restored.formula_trace == ["f"]
        assert len(restored.values) == 1
        assert restored.values[0].value.low == pytest.approx(4.0)

    def test_consensus_summary_roundtrip(self):
        s = ConsensusSummary(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=AS_OF_AFTER_NEW, median=4.5, mean=4.6, p25=4.2, p75=4.9,
            min_value=4.0, max_value=5.0, source_count=3,
            dispersion_std=0.3, dispersion_range=1.0,
            revision_direction="up", revision_magnitude=0.3,
            contributing_estimate_ids=["e1", "e2", "e3"],
            contributing_providers=["inst1", "inst2", "inst3"],
            status="ok", min_sources_threshold=3,
        )
        d = s.to_dict()
        restored = ConsensusSummary.from_dict(d)
        assert restored.median == pytest.approx(4.5)
        assert restored.contributing_providers == ["inst1", "inst2", "inst3"]
        assert restored.status == "ok"


def test_expectation_contracts_skip_malformed_nested_payloads():
    from core.research.expectations.contracts import (
        CompanyGuidance,
        ExpectationValue,
        InvestmentThesis,
    )

    assert ExpectationValue.from_dict(["malformed"]).metric == ""
    assert CompanyGuidance.from_dict({"values": ["malformed", None]}).values == []
    thesis = InvestmentThesis.from_dict({"catalysts": ["malformed"], "audits": [None]})
    assert thesis.catalysts == []
    assert thesis.audits == []


def test_expectation_contracts_keep_interval_fields_well_typed():
    from core.research.expectations.contracts import ExpectationValue, MarketExpectation
    from core.research.earnings.interval import Interval

    value = ExpectationValue.from_dict({"value": {"low": "bad"}, "confidence": None})
    market = MarketExpectation.from_dict({"expected_revenue": {"low": "bad"}})

    assert value.value == Interval(0.0, 0.0)
    assert value.confidence == Interval(0.0, 0.0)
    assert market.expected_revenue == Interval(0.0, 0.0)


def test_expectation_engines_skip_malformed_boundary_inputs():
    engine = ConsensusSummaryEngine()
    assert visible_as_of(None, AS_OF_AFTER_NEW) is False
    summary = engine.compute_summary(
        company_symbol="TEST",
        metric="net_profit",
        period="2027",
        as_of=AS_OF_AFTER_NEW,
        estimates=None,
    )
    assert summary.status == "no_data"
    assert engine.filter_visible_estimates(None, metric="net_profit", period="2027", as_of=AS_OF_AFTER_NEW) == []
    assert ImpliedExpectationEngine().reverse_engineer(None) is None


def test_implied_engine_uses_parsed_numeric_inputs():
    from core.research.expectations.contracts import ImpliedExpectationInput

    inp = ImpliedExpectationInput(
        symbol="TEST",
        current_price="50",
        shares_outstanding="100000000",
        pe_ratio="30",
        as_of=AS_OF_AFTER_NEW,
    )

    result = ImpliedExpectationEngine().reverse_engineer(inp)

    assert result is not None
    assert result.expected_EPS.mid == pytest.approx(50 / 30)


def test_thesis_expiry_ignores_invalid_as_of():
    from core.research.expectations.contracts import InvestmentThesis, ThesisStatus
    from core.research.expectations.engine import ThesisLifecycle

    thesis = InvestmentThesis(
        status=ThesisStatus.ACTIVE.value,
        valid_until=AS_OF_AFTER_NEW,
    )

    updated, review = ThesisLifecycle().check_expiry(thesis, as_of="not-a-date")

    assert updated.status == ThesisStatus.ACTIVE.value
    assert review is None


def test_consensus_engine_skips_malformed_estimate_rows():
    from core.research.expectations.engine import ConsensusSummaryEngine

    estimate = ConsensusEstimate(
        company_symbol="TEST",
        provider="inst-1",
        institution_id="inst-1",
        values=[None, ExpectationValue(
            metric="net_profit", value=Interval(10.0, 10.0), unit="yuan",
            period="2027", as_of=AS_OF_AFTER_NEW, data_version="v1",
            source_claim_id="claim", source_evidence_id="evidence",
            extraction_method="test", published_at=AS_OF_AFTER_NEW,
            retrieved_at=AS_OF_AFTER_NEW,
        )],
    )

    summary = ConsensusSummaryEngine().compute_summary(
        company_symbol="TEST", metric="net_profit", period="2027",
        as_of=AS_OF_AFTER_NEW, estimates=[None, estimate],
        min_sources_threshold=1,
    )

    assert summary.status == "ok"
    assert summary.median == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 附加：repository 迁移幂等性
# ---------------------------------------------------------------------------

class TestRepositoryMigrationIdempotency:
    """v3 迁移幂等：多次启动不破坏数据。"""

    def test_migrate_multiple_times_preserves_data(self, tmp_path):
        repo = ExpectationRepository(tmp_path / "mig.db")
        repo.migrate()
        # 插入数据
        repo.save_aquatrade_forecast(
            AquatradeForecastBuilder().build(
                company_symbol="TEST", metric="net_profit", period="2027",
                as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
                unit="yuan", currency="CNY", model_version="v1",
            )
        )
        # 再次 migrate
        repo.migrate()
        # 数据仍在
        stats = repo.stats()
        # P2.5 升级 SCHEMA_VERSION 从 3 到 4
        assert stats["schema_version"] == 4
        assert stats["counts"]["aquatrade_forecasts"] == 1
        assert stats["counts"]["consensus_summaries"] == 0
        assert stats["counts"]["consensus_estimates"] == 0

    def test_repository_skips_malformed_json_rows(self, tmp_path):
        repo = ExpectationRepository(tmp_path / "bad.db")
        with repo.connect() as connection:
            connection.execute(
                "INSERT INTO company_guidance(guidance_id, company_symbol, created_at, payload_json) "
                "VALUES ('bad', 'TEST', '', '[]')"
            )
            connection.execute(
                "INSERT INTO consensus_estimates(consensus_id, company_symbol, created_at, payload_json) "
                "VALUES ('bad-estimate', 'TEST', '', ?) ",
                (json.dumps({"values": [None]}),),
            )

        assert repo.get("company_guidance", "bad") is None
        assert repo.list("company_guidance", index_value="TEST", limit="invalid") == []
        assert repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", "2026-07-18"
        ) == []

    def test_repository_rejects_unknown_tables_and_skips_bad_pit_fields(self, tmp_path):
        repo = ExpectationRepository(tmp_path / "bad-fields.db")
        with pytest.raises(ValueError):
            repo.get("unknown", "id")
        with repo.connect() as connection:
            connection.execute(
                "INSERT INTO consensus_estimates(consensus_id, company_symbol, created_at, payload_json) "
                "VALUES ('bad-fields', 'TEST', '', ?)",
                (json.dumps({"institution_id": {"bad": True}, "values": [
                    {"metric": "net_profit", "period": "2027", "published_at": 123, "retrieved_at": None}
                ]}),),
            )
        assert repo.list_consensus_visible_as_of(
            "TEST", "net_profit", "2027", AS_OF_AFTER_NEW
        ) == []

    def test_v3_business_columns_populated(self, tmp_path):
        """业务列通过专用保存方法写入，可被 SQL 索引查询命中。"""
        repo = ExpectationRepository(tmp_path / "biz.db")
        aqf = AquatradeForecastBuilder().build(
            company_symbol="TEST", metric="net_profit", period="2027",
            as_of=PUBLISHED_AT_OLD, value_low=4.0, value_high=5.0,
            unit="yuan", currency="CNY", model_version="v1", scenario="base",
            integrated_forecast_id="if-1",
        )
        repo.save_aquatrade_forecast(aqf)
        # 直接通过 SQL 验证业务列
        import sqlite3
        with sqlite3.connect(str(repo.db_path)) as conn:
            row = conn.execute(
                "SELECT period, scenario, model_version, as_of, integrated_forecast_id "
                "FROM aquatrade_forecasts WHERE forecast_id=?",
                (aqf.forecast_id,),
            ).fetchone()
        assert row == ("2027", "base", "v1", PUBLISHED_AT_OLD, "if-1")
