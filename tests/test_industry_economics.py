"""P1.5 产业经济与竞争格局专项测试。

覆盖 P1 计划 §5 Task P1.5 要求的 4 个验收点：
1. member_of_theme 不进入市场份额、产品和竞争结论
2. 赛道阶段必须有时间和证据
3. 新闻热度不能单独证明需求兑现
4. 公司映射证据为空显示"暂无本地证据"

覆盖 10 个核心模块：
- MarketSizing（TAM/SAM/SOM）
- DemandDriver（需求驱动和渗透率）
- SupplyProfile（供给、产能和扩产周期）
- PriceCostCurve（价格与成本曲线）
- InventoryCycle（库存和交付周期）
- CompetitiveLandscape（行业集中度、主要参与者和份额来源）
- TechnologyRoute（技术路线、替代关系和认证壁垒）
- BargainingPower（上下游议价能力）
- CapexCycle（资本开支和自由现金流周期）
- LifecycleStage（赛道生命周期和失效条件）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.industry import (
    BargainingPower,
    CapexCycle,
    CompanyMapping,
    CompetitiveLandscape,
    CompetitionEngine,
    DemandDriver,
    EconomicsEngine,
    IndustryGap,
    IndustryProfile,
    InventoryCycle,
    LifecycleStage,
    MarketSizing,
    NO_LOCAL_EVIDENCE,
    Participant,
    PriceCostCurve,
    SupplyProfile,
    TechnologyRoute,
)
from core.research.industry.competition_engine import CompetitionRequest
from core.research.industry.economics_engine import EconomicsRequest


# ---------------------------------------------------------------------------
# 辅助函数：构造完整模块数据
# ---------------------------------------------------------------------------


def _make_complete_market_sizing() -> MarketSizing:
    """构造完整的 TAM/SAM/SOM 数据。"""
    return MarketSizing(
        tam=10_000_000_000.0,
        sam=3_000_000_000.0,
        som=300_000_000.0,
        unit="元",
        currency="CNY",
        period="2026-06-30",
        method="top_down",
        source_refs=["ev_ms_1", "ev_ms_2"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_demand_driver(heat_only: bool = False) -> DemandDriver:
    """构造完整的需求驱动数据。"""
    return DemandDriver(
        name="EV 销量",
        direction="positive",
        magnitude=25.0,
        magnitude_unit="%",
        penetration_rate=0.35,
        period="2026-06-30",
        source_refs=["ev_dd_1"],
        available_at="2026-07-01",
        heat_only=heat_only,
        grounding_status="ok",
    )


def _make_complete_supply_profile() -> SupplyProfile:
    """构造完整的供给画像。"""
    return SupplyProfile(
        industry_capacity=5_000_000.0,
        capacity_unit="吨",
        utilization_rate=0.82,
        expansion_lead_time_months=24,
        expansion_phase="expansion",
        period="2026-06-30",
        source_refs=["ev_sp_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_price_cost_curve() -> PriceCostCurve:
    """构造完整的价格成本曲线。"""
    return PriceCostCurve(
        price_points=[("2026-Q1", 100.0), ("2026-Q2", 105.0)],
        cost_points=[("2026-Q1", 60.0), ("2026-Q2", 62.0)],
        unit="元/吨",
        currency="CNY",
        trend="up",
        period="2026-Q2",
        source_refs=["ev_pc_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_inventory_cycle() -> InventoryCycle:
    """构造完整的库存周期数据。"""
    return InventoryCycle(
        inventory_days=45.0,
        delivery_lead_time_days=30,
        inventory_trend="depleting",
        period="2026-06-30",
        source_refs=["ev_ic_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_capex_cycle() -> CapexCycle:
    """构造完整的资本开支周期数据。"""
    return CapexCycle(
        industry_capex=50_000_000_000.0,
        capex_unit="元",
        capex_currency="CNY",
        fcf_cycle_phase="investment",
        capex_to_revenue_ratio=0.18,
        cycle_period_years=5,
        period="2026-06-30",
        source_refs=["ev_cc_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_lifecycle_stage() -> LifecycleStage:
    """构造完整的赛道生命周期阶段数据。"""
    return LifecycleStage(
        stage="growth",
        stage_evidence_id="ev_ls_1",
        stage_dated_at="2026-06-30",
        failure_conditions=["技术路线被替代", "下游需求锐减"],
        period="2026-06-30",
        source_refs=["ev_ls_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_participant(
    name: str = "龙头公司 A",
    share: float = 0.35,
    share_ref: str = "ev_cl_share_a",
) -> Participant:
    """构造完整的参与者数据。"""
    return Participant(
        name=name,
        symbol="600000.SH",
        market_share=share,
        share_basis="revenue",
        share_source_ref=share_ref,
        share_period="2026-06-30",
        role="leader",
    )


def _make_complete_competitive_landscape(
    member_of_theme_excluded: bool = True,
) -> CompetitiveLandscape:
    """构造完整的竞争格局数据。"""
    return CompetitiveLandscape(
        concentration_metric="cr3",
        concentration_value=0.72,
        hhi=1850.0,
        participants=[
            _make_complete_participant("龙头公司 A", 0.35, "ev_cl_a"),
            _make_complete_participant("公司 B", 0.22, "ev_cl_b"),
            _make_complete_participant("公司 C", 0.15, "ev_cl_c"),
        ],
        share_source_refs=["ev_cl_a", "ev_cl_b", "ev_cl_c"],
        period="2026-06-30",
        member_of_theme_excluded=member_of_theme_excluded,
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_technology_route() -> TechnologyRoute:
    """构造完整的技术路线数据。"""
    return TechnologyRoute(
        name="TOPCon 电池",
        status="mainstream",
        adoption_rate=0.55,
        alternatives=["HJT", "BC 电池"],
        certification_barriers=["CQC 认证", "客户验证周期 18 个月"],
        period="2026-06-30",
        source_refs=["ev_tr_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_bargaining_power() -> BargainingPower:
    """构造完整的议价能力数据。"""
    return BargainingPower(
        direction="upstream",
        level="strong",
        factors=["供应商集中度高", "原材料稀缺"],
        period="2026-06-30",
        source_refs=["ev_bp_1"],
        available_at="2026-07-01",
        grounding_status="ok",
    )


def _make_complete_company_mapping(
    has_evidence: bool = True,
) -> CompanyMapping:
    """构造完整的公司映射数据。"""
    return CompanyMapping(
        symbol="600000.SH",
        industry_role="leader",
        exposure_to_industry=0.85,
        evidence_ids=["ev_cm_1", "ev_cm_2"] if has_evidence else [],
        has_local_evidence=has_evidence,
        period="2026-06-30",
    )


def _make_complete_competition_request() -> CompetitionRequest:
    """构造完整 CompetitionRequest（覆盖 10 个模块）。"""
    return CompetitionRequest(
        industry_name="光伏电池",
        industry_code="801031",
        code_system="sw",
        period="2026-06-30",
        market_sizing=_make_complete_market_sizing(),
        demand_drivers=[_make_complete_demand_driver()],
        supply_profile=_make_complete_supply_profile(),
        price_cost_curve=_make_complete_price_cost_curve(),
        inventory_cycle=_make_complete_inventory_cycle(),
        capex_cycle=_make_complete_capex_cycle(),
        lifecycle_stage=_make_complete_lifecycle_stage(),
        competitive_landscape=_make_complete_competitive_landscape(),
        technology_routes=[_make_complete_technology_route()],
        bargaining_powers=[_make_complete_bargaining_power()],
        company_mappings=[_make_complete_company_mapping()],
        source_trace_id="src_test_001",
    )


# ---------------------------------------------------------------------------
# 验收点 1：member_of_theme 不进入市场份额、产品和竞争结论
# ---------------------------------------------------------------------------


class TestAcceptancePoint1MemberOfTheme:
    """验收点 1：member_of_theme 不进入市场份额、产品和竞争结论。"""

    def test_competitive_landscape_with_member_of_theme_excluded_passes(self) -> None:
        """member_of_theme_excluded=True 时不产生 gap。"""
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        member_theme_gaps = [
            g for g in profile.gaps if "member_of_theme" in g.field_name
        ]
        assert member_theme_gaps == []

    def test_competitive_landscape_without_member_of_theme_excluded_triggers_blocker(
        self,
    ) -> None:
        """member_of_theme_excluded=False 时生成 blocker gap。"""
        request = _make_complete_competition_request()
        request.competitive_landscape.member_of_theme_excluded = False
        profile = CompetitionEngine().compute_industry_profile(request)
        blocker_gaps = [
            g
            for g in profile.gaps
            if "member_of_theme" in g.field_name and g.severity == "blocker"
        ]
        assert len(blocker_gaps) == 1
        assert profile.competitive_landscape.grounding_status == "missing"

    def test_participant_market_share_must_have_share_source_ref(self) -> None:
        """参与者份额必须有来源证据，不得从 member_of_theme 推导。"""
        request = _make_complete_competition_request()
        request.competitive_landscape.participants[0].share_source_ref = ""
        profile = CompetitionEngine().compute_industry_profile(request)
        share_ref_gaps = [
            g for g in profile.gaps if "share_source_ref" in g.field_name
        ]
        assert len(share_ref_gaps) == 1
        assert share_ref_gaps[0].severity == "blocker"

    def test_causal_context_industry_ontology_summary_excludes_member_of_theme(
        self, tmp_path: Path
    ) -> None:
        """causal_context.industry_ontology_summary() 严格过滤 member_of_theme。"""
        from core.research.value_chain.causal_context import ResearchCausalContext

        db_path = tmp_path / "research_control.db"
        self._write_research_control_db(db_path)
        summary = ResearchCausalContext(db_path).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        assert summary["member_of_theme_excluded"] is True
        assert summary["competitors"] == []
        assert summary["market_share_evidence"] == []
        # exploration_links_excluded 应大于 0（数据库写入了 member_of_theme 关系）
        assert summary["exploration_links_excluded"] >= 1

    @staticmethod
    def _write_research_control_db(path: Path) -> None:
        """写入测试用 research_control 数据库。"""
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE claims (claim_id TEXT PRIMARY KEY, research_status TEXT, updated_at TEXT, payload_json TEXT);
            CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT, created_at TEXT, payload_json TEXT);
        """)
        claims = [
            (
                "theme",
                "SUPPORTED",
                {
                    "claim_id": "theme",
                    "subject_id": "000001.SZ",
                    "subject_name": "测试公司",
                    "relation": "member_of_theme",
                    "object_name": "测试赛道",
                    "research_status": "SUPPORTED",
                },
            ),
            (
                "causal",
                "SUPPORTED",
                {
                    "claim_id": "causal",
                    "subject_id": "000001.SZ",
                    "subject_name": "测试公司",
                    "relation": "SUPPLIES_TO",
                    "object_name": "客户甲",
                    "direction": "forward",
                    "research_status": "SUPPORTED",
                    "evidence_confidence": 0.8,
                },
            ),
        ]
        for claim_id, status, payload in claims:
            connection.execute(
                "INSERT INTO claims VALUES (?, ?, '', ?)",
                (claim_id, status, json.dumps(payload, ensure_ascii=False)),
            )
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, '', ?)",
            (
                "ev1",
                "causal",
                json.dumps(
                    {
                        "evidence_id": "ev1",
                        "claim_id": "causal",
                        "stance": "support",
                        "quote": "可定位原文",
                        "source_trace_id": "src1",
                        "published_at": "2026-06-20",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()
        connection.close()


# ---------------------------------------------------------------------------
# 验收点 2：赛道阶段必须有时间和证据
# ---------------------------------------------------------------------------


class TestAcceptancePoint2LifecycleStageEvidence:
    """验收点 2：赛道阶段必须有时间和证据。"""

    def test_lifecycle_stage_with_evidence_and_time_passes(self) -> None:
        """阶段证据 ID + 时间齐全时不产生 gap。"""
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        stage_gaps = [g for g in profile.gaps if "lifecycle_stage" in g.field_name]
        assert stage_gaps == []
        assert profile.lifecycle_stage.grounding_status == "ok"

    def test_lifecycle_stage_missing_evidence_triggers_blocker(self) -> None:
        """缺阶段证据 ID 时触发 blocker gap。"""
        request = _make_complete_competition_request()
        request.lifecycle_stage.stage_evidence_id = ""
        profile = CompetitionEngine().compute_industry_profile(request)
        evidence_gaps = [
            g
            for g in profile.gaps
            if g.field_name == "lifecycle_stage.stage_evidence_id"
        ]
        assert len(evidence_gaps) == 1
        assert evidence_gaps[0].severity == "blocker"
        assert evidence_gaps[0].blocker_kind == "stage_no_evidence"
        assert profile.lifecycle_stage.grounding_status == "missing"

    def test_lifecycle_stage_missing_time_triggers_blocker(self) -> None:
        """缺阶段时间时触发 blocker gap。"""
        request = _make_complete_competition_request()
        request.lifecycle_stage.stage_dated_at = ""
        profile = CompetitionEngine().compute_industry_profile(request)
        time_gaps = [
            g
            for g in profile.gaps
            if g.field_name == "lifecycle_stage.stage_dated_at"
        ]
        assert len(time_gaps) == 1
        assert time_gaps[0].severity == "blocker"
        assert time_gaps[0].blocker_kind == "stage_no_time"
        assert profile.lifecycle_stage.grounding_status == "missing"

    def test_lifecycle_stage_missing_both_triggers_two_blocker_gaps(self) -> None:
        """同时缺证据和时间，生成两个 blocker gap。"""
        request = _make_complete_competition_request()
        request.lifecycle_stage.stage_evidence_id = ""
        request.lifecycle_stage.stage_dated_at = ""
        profile = CompetitionEngine().compute_industry_profile(request)
        stage_gaps = [
            g for g in profile.gaps if "lifecycle_stage" in g.field_name
        ]
        assert len(stage_gaps) == 2


# ---------------------------------------------------------------------------
# 验收点 3：新闻热度不能单独证明需求兑现
# ---------------------------------------------------------------------------


class TestAcceptancePoint3HeatOnly:
    """验收点 3：新闻热度不能单独证明需求兑现。"""

    def test_demand_driver_with_evidence_passes(self) -> None:
        """有证据的需求驱动不产生 heat_only gap。"""
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        heat_gaps = [g for g in profile.gaps if "heat_only" in g.field_name]
        assert heat_gaps == []
        assert profile.demand_drivers[0].grounding_status == "ok"

    def test_demand_driver_heat_only_triggers_warning_gap(self) -> None:
        """heat_only=True 生成 warning gap（不阻断）。"""
        request = _make_complete_competition_request()
        request.demand_drivers = [_make_complete_demand_driver(heat_only=True)]
        profile = CompetitionEngine().compute_industry_profile(request)
        heat_gaps = [g for g in profile.gaps if "heat_only" in g.field_name]
        assert len(heat_gaps) == 1
        assert heat_gaps[0].severity == "warning"
        assert heat_gaps[0].blocker_kind == "heat_only"
        assert "新闻热度" in heat_gaps[0].reason

    def test_heat_only_driver_grounding_status_degraded(self) -> None:
        """heat_only=True 的 driver grounding_status='degraded'。"""
        request = _make_complete_competition_request()
        request.demand_drivers = [_make_complete_demand_driver(heat_only=True)]
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.demand_drivers[0].grounding_status == "degraded"

    def test_heat_only_driver_still_in_output(self) -> None:
        """heat_only=True 的 driver 仍保留在输出中（不删除）。"""
        request = _make_complete_competition_request()
        request.demand_drivers = [_make_complete_demand_driver(heat_only=True)]
        profile = CompetitionEngine().compute_industry_profile(request)
        assert len(profile.demand_drivers) == 1
        assert profile.demand_drivers[0].heat_only is True


# ---------------------------------------------------------------------------
# 验收点 4：公司映射证据为空显示"暂无本地证据"
# ---------------------------------------------------------------------------


class TestAcceptancePoint4NoLocalEvidence:
    """验收点 4：公司映射证据为空显示"暂无本地证据"。"""

    def test_company_mapping_with_evidence_displays_ids(self) -> None:
        """有证据时显示证据 ID 列表。"""
        mapping = _make_complete_company_mapping(has_evidence=True)
        display = mapping.local_evidence_display()
        assert "ev_cm_1" in display
        assert "ev_cm_2" in display
        assert display != NO_LOCAL_EVIDENCE

    def test_company_mapping_without_evidence_displays_no_local_evidence(self) -> None:
        """无证据时返回 NO_LOCAL_EVIDENCE 常量。"""
        mapping = _make_complete_company_mapping(has_evidence=False)
        display = mapping.local_evidence_display()
        assert display == NO_LOCAL_EVIDENCE
        assert display == "暂无本地证据"

    def test_company_mapping_empty_evidence_ids_uses_constant(self) -> None:
        """evidence_ids 为空但 has_local_evidence=True 时也返回常量。"""
        mapping = CompanyMapping(
            symbol="600000.SH",
            industry_role="leader",
            evidence_ids=[],
            has_local_evidence=True,
        )
        assert mapping.local_evidence_display() == NO_LOCAL_EVIDENCE

    def test_company_mapping_to_dict_includes_local_evidence_display(self) -> None:
        """to_dict 包含 local_evidence_display 字段。"""
        mapping = _make_complete_company_mapping(has_evidence=False)
        d = mapping.to_dict()
        assert d["local_evidence_display"] == NO_LOCAL_EVIDENCE
        assert d["has_local_evidence"] is False

    def test_company_mapping_without_evidence_triggers_warning_gap(self) -> None:
        """无证据的公司映射触发 warning gap，note 含常量字符串。"""
        request = _make_complete_competition_request()
        request.company_mappings = [
            _make_complete_company_mapping(has_evidence=False)
        ]
        profile = CompetitionEngine().compute_industry_profile(request)
        mapping_gaps = [
            g for g in profile.gaps if "company_mappings" in g.field_name
        ]
        assert len(mapping_gaps) == 1
        assert mapping_gaps[0].severity == "warning"
        assert NO_LOCAL_EVIDENCE in mapping_gaps[0].note


# ---------------------------------------------------------------------------
# 10 个核心模块测试
# ---------------------------------------------------------------------------


class TestMarketSizing:
    """模块 1：TAM/SAM/SOM 口径。"""

    def test_market_sizing_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            market_sizing=_make_complete_market_sizing(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.market_sizing.grounding_status == "ok"
        ms_gaps = [g for g in profile.gaps if "market_sizing" in g.field_name]
        assert ms_gaps == []

    def test_market_sizing_missing_source_refs_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            market_sizing=_make_complete_market_sizing(),
        )
        request.market_sizing.source_refs = []
        profile = EconomicsEngine().compute_industry_economics(request)
        blocker_gaps = [
            g for g in profile.gaps if "market_sizing.source_refs" in g.field_name
        ]
        assert len(blocker_gaps) == 1
        assert blocker_gaps[0].severity == "blocker"

    def test_market_sizing_missing_period_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            market_sizing=_make_complete_market_sizing(),
        )
        request.market_sizing.period = ""
        profile = EconomicsEngine().compute_industry_economics(request)
        period_gaps = [
            g for g in profile.gaps if "market_sizing.period" in g.field_name
        ]
        assert len(period_gaps) == 1
        assert period_gaps[0].severity == "blocker"

    def test_market_sizing_illegal_method_raises(self) -> None:
        sizing = _make_complete_market_sizing()
        sizing.method = "invalid_method"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            market_sizing=sizing,
        )
        with pytest.raises(ValueError, match="非法 market_sizing method"):
            EconomicsEngine().compute_industry_economics(request)

    def test_market_sizing_to_dict_roundtrip(self) -> None:
        sizing = _make_complete_market_sizing()
        d = sizing.to_dict()
        assert d["tam"] == 10_000_000_000.0
        assert d["method"] == "top_down"
        assert d["source_refs"] == ["ev_ms_1", "ev_ms_2"]
        assert d["unit"] == "元"


class TestDemandDriverModule:
    """模块 2：需求驱动和渗透率。"""

    def test_demand_driver_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            demand_drivers=[_make_complete_demand_driver()],
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.demand_drivers[0].grounding_status == "ok"

    def test_demand_driver_missing_source_refs_triggers_blocker(self) -> None:
        driver = _make_complete_demand_driver()
        driver.source_refs = []
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            demand_drivers=[driver],
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.demand_drivers[0].grounding_status == "missing"
        blocker_gaps = [
            g for g in profile.gaps if "demand_drivers" in g.field_name
        ]
        assert len(blocker_gaps) == 1
        assert blocker_gaps[0].severity == "blocker"

    def test_demand_driver_illegal_direction_raises(self) -> None:
        driver = _make_complete_demand_driver()
        driver.direction = "invalid_direction"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            demand_drivers=[driver],
        )
        with pytest.raises(ValueError, match="非法 demand direction"):
            EconomicsEngine().compute_industry_economics(request)

    def test_demand_driver_to_dict_roundtrip(self) -> None:
        driver = _make_complete_demand_driver()
        d = driver.to_dict()
        assert d["name"] == "EV 销量"
        assert d["penetration_rate"] == 0.35
        assert d["heat_only"] is False


class TestSupplyProfile:
    """模块 3：供给、产能和扩产周期。"""

    def test_supply_profile_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            supply_profile=_make_complete_supply_profile(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.supply_profile.grounding_status == "ok"

    def test_supply_profile_missing_source_refs_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            supply_profile=_make_complete_supply_profile(),
        )
        request.supply_profile.source_refs = []
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.supply_profile.grounding_status == "missing"

    def test_supply_profile_illegal_phase_raises(self) -> None:
        supply = _make_complete_supply_profile()
        supply.expansion_phase = "invalid_phase"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            supply_profile=supply,
        )
        with pytest.raises(ValueError, match="非法 supply phase"):
            EconomicsEngine().compute_industry_economics(request)


class TestPriceCostCurve:
    """模块 4：价格与成本曲线。"""

    def test_price_cost_curve_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            price_cost_curve=_make_complete_price_cost_curve(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.price_cost_curve.grounding_status == "ok"

    def test_price_cost_curve_missing_source_refs_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            price_cost_curve=_make_complete_price_cost_curve(),
        )
        request.price_cost_curve.source_refs = []
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.price_cost_curve.grounding_status == "missing"

    def test_price_cost_curve_illegal_trend_raises(self) -> None:
        curve = _make_complete_price_cost_curve()
        curve.trend = "invalid_trend"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            price_cost_curve=curve,
        )
        with pytest.raises(ValueError, match="非法 price trend"):
            EconomicsEngine().compute_industry_economics(request)


class TestInventoryCycle:
    """模块 5：库存和交付周期。"""

    def test_inventory_cycle_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            inventory_cycle=_make_complete_inventory_cycle(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.inventory_cycle.grounding_status == "ok"

    def test_inventory_cycle_missing_source_refs_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            inventory_cycle=_make_complete_inventory_cycle(),
        )
        request.inventory_cycle.source_refs = []
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.inventory_cycle.grounding_status == "missing"

    def test_inventory_cycle_to_dict_roundtrip(self) -> None:
        cycle = _make_complete_inventory_cycle()
        d = cycle.to_dict()
        assert d["inventory_days"] == 45.0
        assert d["delivery_lead_time_days"] == 30
        assert d["inventory_trend"] == "depleting"


class TestCompetitiveLandscapeModule:
    """模块 6：行业集中度、主要参与者和份额来源。"""

    def test_competitive_landscape_complete_passes(self) -> None:
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.competitive_landscape.grounding_status == "ok"
        assert len(profile.competitive_landscape.participants) == 3

    def test_competitive_landscape_illegal_concentration_metric_raises(self) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.concentration_metric = "invalid_metric"
        with pytest.raises(ValueError, match="非法 concentration metric"):
            CompetitionEngine().compute_industry_profile(request)

    def test_competitive_landscape_illegal_share_basis_raises(self) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.participants[0].share_basis = "invalid_basis"
        with pytest.raises(ValueError, match="非法 share basis"):
            CompetitionEngine().compute_industry_profile(request)

    def test_competitive_landscape_market_share_out_of_range_triggers_warning(
        self,
    ) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.participants[0].market_share = 1.5
        profile = CompetitionEngine().compute_industry_profile(request)
        share_gaps = [
            g
            for g in profile.gaps
            if "market_share" in g.field_name and "participants" in g.field_name
        ]
        assert len(share_gaps) == 1
        assert share_gaps[0].severity == "warning"

    def test_competitive_landscape_to_dict_roundtrip(self) -> None:
        landscape = _make_complete_competitive_landscape()
        d = landscape.to_dict()
        assert d["concentration_metric"] == "cr3"
        assert d["concentration_value"] == 0.72
        assert d["member_of_theme_excluded"] is True
        assert len(d["participants"]) == 3


class TestTechnologyRoute:
    """模块 7：技术路线、替代关系和认证壁垒。"""

    def test_technology_route_complete_passes(self) -> None:
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.technology_routes[0].grounding_status == "ok"

    def test_technology_route_missing_source_refs_triggers_blocker(self) -> None:
        request = _make_complete_competition_request()
        request.technology_routes[0].source_refs = []
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.technology_routes[0].grounding_status == "missing"

    def test_technology_route_illegal_status_raises(self) -> None:
        request = _make_complete_competition_request()
        request.technology_routes[0].status = "invalid_status"
        with pytest.raises(ValueError, match="非法 tech status"):
            CompetitionEngine().compute_industry_profile(request)

    def test_technology_route_to_dict_roundtrip(self) -> None:
        route = _make_complete_technology_route()
        d = route.to_dict()
        assert d["name"] == "TOPCon 电池"
        assert d["alternatives"] == ["HJT", "BC 电池"]
        assert d["certification_barriers"] == ["CQC 认证", "客户验证周期 18 个月"]


class TestBargainingPower:
    """模块 8：上下游议价能力。"""

    def test_bargaining_power_complete_passes(self) -> None:
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.bargaining_powers[0].grounding_status == "ok"

    def test_bargaining_power_missing_source_refs_triggers_blocker(self) -> None:
        request = _make_complete_competition_request()
        request.bargaining_powers[0].source_refs = []
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.bargaining_powers[0].grounding_status == "missing"

    def test_bargaining_power_illegal_direction_raises(self) -> None:
        request = _make_complete_competition_request()
        request.bargaining_powers[0].direction = "invalid_direction"
        with pytest.raises(ValueError, match="非法 bargaining direction"):
            CompetitionEngine().compute_industry_profile(request)

    def test_bargaining_power_illegal_level_raises(self) -> None:
        request = _make_complete_competition_request()
        request.bargaining_powers[0].level = "invalid_level"
        with pytest.raises(ValueError, match="非法 bargaining level"):
            CompetitionEngine().compute_industry_profile(request)


class TestCapexCycle:
    """模块 9：资本开支和自由现金流周期。"""

    def test_capex_cycle_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            capex_cycle=_make_complete_capex_cycle(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.capex_cycle.grounding_status == "ok"

    def test_capex_cycle_missing_source_refs_triggers_blocker(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            capex_cycle=_make_complete_capex_cycle(),
        )
        request.capex_cycle.source_refs = []
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.capex_cycle.grounding_status == "missing"

    def test_capex_cycle_illegal_phase_raises(self) -> None:
        capex = _make_complete_capex_cycle()
        capex.fcf_cycle_phase = "invalid_phase"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            capex_cycle=capex,
        )
        with pytest.raises(ValueError, match="非法 capex phase"):
            EconomicsEngine().compute_industry_economics(request)


class TestLifecycleStageModule:
    """模块 10：赛道生命周期和失效条件。"""

    def test_lifecycle_stage_complete_passes(self) -> None:
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            lifecycle_stage=_make_complete_lifecycle_stage(),
        )
        profile = EconomicsEngine().compute_industry_economics(request)
        assert profile.lifecycle_stage.grounding_status == "ok"

    def test_lifecycle_stage_illegal_stage_raises(self) -> None:
        stage = _make_complete_lifecycle_stage()
        stage.stage = "invalid_stage"
        request = EconomicsRequest(
            industry_name="测试行业",
            period="2026-06-30",
            lifecycle_stage=stage,
        )
        with pytest.raises(ValueError, match="非法 lifecycle stage"):
            EconomicsEngine().compute_industry_economics(request)

    def test_lifecycle_stage_to_dict_roundtrip(self) -> None:
        stage = _make_complete_lifecycle_stage()
        d = stage.to_dict()
        assert d["stage"] == "growth"
        assert d["stage_evidence_id"] == "ev_ls_1"
        assert d["stage_dated_at"] == "2026-06-30"
        assert "技术路线被替代" in d["failure_conditions"]


class TestGroundingBoundaries:
    """所有经济模块都必须保留可见时点这一 PIT 边界。"""

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("market_sizing", _make_complete_market_sizing()),
            ("demand_drivers", _make_complete_demand_driver()),
            ("supply_profile", _make_complete_supply_profile()),
            ("price_cost_curve", _make_complete_price_cost_curve()),
            ("inventory_cycle", _make_complete_inventory_cycle()),
            ("capex_cycle", _make_complete_capex_cycle()),
            ("lifecycle_stage", _make_complete_lifecycle_stage()),
        ],
    )
    def test_missing_available_at_creates_blocker_gap(self, field_name: str, value: Any) -> None:
        value.available_at = ""
        request = EconomicsRequest()
        if field_name == "demand_drivers":
            request.demand_drivers = [value]
            prefix = "demand_drivers[0]"
        else:
            setattr(request, field_name, value)
            prefix = field_name

        profile = EconomicsEngine().compute_industry_economics(request)

        assert any(
            gap.field_name == f"{prefix}.available_at" and gap.severity == "blocker"
            for gap in profile.gaps
        )


class TestCompetitionBoundaries:
    """竞争侧输入边界与 grounding 状态回归。"""

    def test_route_and_bargaining_power_require_available_at(self) -> None:
        request = _make_complete_competition_request()
        request.technology_routes[0].available_at = ""
        request.bargaining_powers[0].available_at = ""

        profile = CompetitionEngine().compute_industry_profile(request)

        fields = {gap.field_name for gap in profile.gaps}
        assert "technology_routes[0].available_at" in fields
        assert "bargaining_powers[0].available_at" in fields
        assert profile.technology_routes[0].grounding_status == "missing"
        assert profile.bargaining_powers[0].grounding_status == "missing"

    def test_landscape_requires_available_at(self) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.available_at = ""

        profile = CompetitionEngine().compute_industry_profile(request)

        assert "competitive_landscape.available_at" in {
            gap.field_name for gap in profile.gaps
        }
        assert profile.competitive_landscape.grounding_status == "missing"

    def test_invalid_numeric_share_does_not_crash(self) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.participants[0].market_share = float("nan")

        profile = CompetitionEngine().compute_industry_profile(request)

        assert any(
            gap.field_name.endswith("market_share") and gap.severity == "warning"
            for gap in profile.gaps
        )

    def test_missing_participant_share_evidence_keeps_landscape_degraded(self) -> None:
        request = _make_complete_competition_request()
        request.competitive_landscape.participants[0].share_source_ref = ""

        profile = CompetitionEngine().compute_industry_profile(request)

        assert profile.competitive_landscape.grounding_status == "missing"

    def test_non_list_mapping_evidence_is_treated_as_missing(self) -> None:
        request = _make_complete_competition_request()
        request.company_mappings[0].evidence_ids = "bad"  # type: ignore[assignment]

        profile = CompetitionEngine().compute_industry_profile(request)

        assert any(
            gap.field_name == "company_mappings[0].evidence_ids"
            for gap in profile.gaps
        )


# ---------------------------------------------------------------------------
# 顶层集成测试
# ---------------------------------------------------------------------------


class TestIndustryProfileIntegration:
    """顶层 IndustryProfile 集成测试。"""

    def test_complete_profile_has_no_blocker_gaps(self) -> None:
        """完整输入下不产生 blocker gap。"""
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        blocker_gaps = [g for g in profile.gaps if g.severity == "blocker"]
        assert blocker_gaps == []
        assert profile.industry_name == "光伏电池"
        assert profile.industry_code == "801031"
        assert profile.code_system == "sw"
        assert profile.period == "2026-06-30"

    def test_profile_to_dict_serializes_all_modules(self) -> None:
        """to_dict 序列化所有 10 个模块。"""
        request = _make_complete_competition_request()
        profile = CompetitionEngine().compute_industry_profile(request)
        d = profile.to_dict()
        assert d["market_sizing"] is not None
        assert d["supply_profile"] is not None
        assert d["price_cost_curve"] is not None
        assert d["inventory_cycle"] is not None
        assert d["competitive_landscape"] is not None
        assert d["capex_cycle"] is not None
        assert d["lifecycle_stage"] is not None
        assert len(d["demand_drivers"]) == 1
        assert len(d["technology_routes"]) == 1
        assert len(d["bargaining_powers"]) == 1
        assert len(d["company_mappings"]) == 1
        assert d["schema_version"] == "industry_v1"

    def test_profile_with_empty_inputs_returns_empty_profile(self) -> None:
        """空输入返回空 profile（不崩溃）。"""
        request = CompetitionRequest(
            industry_name="空行业",
            industry_code="",
            period="2026-06-30",
        )
        profile = CompetitionEngine().compute_industry_profile(request)
        assert profile.market_sizing is None
        assert profile.demand_drivers == []
        assert profile.supply_profile is None
        assert profile.competitive_landscape is None
        assert profile.lifecycle_stage is None
        assert profile.gaps == []
        assert profile.industry_name == "空行业"

    def test_profile_with_all_missing_inputs_aggregates_all_gaps(self) -> None:
        """所有模块都缺数据时聚合所有 gap。"""
        request = CompetitionRequest(
            industry_name="缺数据行业",
            period="2026-06-30",
            market_sizing=MarketSizing(period="2026-06-30"),  # 缺 source_refs
            demand_drivers=[DemandDriver(name="dd1")],  # 缺 source_refs/period
            supply_profile=SupplyProfile(period="2026-06-30"),  # 缺 source_refs
            lifecycle_stage=LifecycleStage(stage="growth"),  # 缺 evidence + time
            competitive_landscape=CompetitiveLandscape(
                member_of_theme_excluded=False
            ),  # 触发 member_of_theme leak
        )
        profile = CompetitionEngine().compute_industry_profile(request)
        assert len(profile.gaps) >= 6
        gap_fields = {g.field_name for g in profile.gaps}
        assert "market_sizing.source_refs" in gap_fields
        assert "demand_drivers[0].source_refs" in gap_fields
        assert "supply_profile.source_refs" in gap_fields
        assert "lifecycle_stage.stage_evidence_id" in gap_fields
        assert "lifecycle_stage.stage_dated_at" in gap_fields
        assert "competitive_landscape.member_of_theme_excluded" in gap_fields


# ---------------------------------------------------------------------------
# 校验函数测试
# ---------------------------------------------------------------------------


class TestValidators:
    """校验函数测试。"""

    def test_validate_market_sizing_method_legal(self) -> None:
        from core.research.industry.contracts import validate_market_sizing_method

        for method in ("top_down", "bottom_up", "supply_demand", ""):
            validate_market_sizing_method(method)  # 不抛

    def test_validate_market_sizing_method_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_market_sizing_method

        with pytest.raises(ValueError):
            validate_market_sizing_method("invalid")

    def test_validate_demand_direction_legal(self) -> None:
        from core.research.industry.contracts import validate_demand_direction

        for direction in ("positive", "negative", "neutral", ""):
            validate_demand_direction(direction)

    def test_validate_demand_direction_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_demand_direction

        with pytest.raises(ValueError):
            validate_demand_direction("invalid")

    def test_validate_supply_phase_legal(self) -> None:
        from core.research.industry.contracts import validate_supply_phase

        for phase in ("expansion", "stabilization", "contraction", ""):
            validate_supply_phase(phase)

    def test_validate_supply_phase_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_supply_phase

        with pytest.raises(ValueError):
            validate_supply_phase("invalid")

    def test_validate_price_trend_legal(self) -> None:
        from core.research.industry.contracts import validate_price_trend

        for trend in ("up", "down", "flat", "volatile", ""):
            validate_price_trend(trend)

    def test_validate_price_trend_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_price_trend

        with pytest.raises(ValueError):
            validate_price_trend("invalid")

    def test_validate_lifecycle_stage_legal(self) -> None:
        from core.research.industry.contracts import validate_lifecycle_stage

        for stage in ("embryonic", "growth", "mature", "decline", ""):
            validate_lifecycle_stage(stage)

    def test_validate_lifecycle_stage_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_lifecycle_stage

        with pytest.raises(ValueError):
            validate_lifecycle_stage("invalid")

    def test_validate_company_role_legal(self) -> None:
        from core.research.industry.contracts import validate_company_role

        for role in ("leader", "challenger", "follower", "niche", ""):
            validate_company_role(role)

    def test_validate_company_role_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_company_role

        with pytest.raises(ValueError):
            validate_company_role("invalid")

    def test_validate_concentration_metric_legal(self) -> None:
        from core.research.industry.contracts import validate_concentration_metric

        for metric in ("cr3", "cr5", "cr10", "hhi", ""):
            validate_concentration_metric(metric)

    def test_validate_concentration_metric_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_concentration_metric

        with pytest.raises(ValueError):
            validate_concentration_metric("invalid")

    def test_validate_share_basis_legal(self) -> None:
        from core.research.industry.contracts import validate_share_basis

        for basis in ("revenue", "volume", "capacity", "none", ""):
            validate_share_basis(basis)

    def test_validate_share_basis_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_share_basis

        with pytest.raises(ValueError):
            validate_share_basis("invalid")

    def test_validate_tech_status_legal(self) -> None:
        from core.research.industry.contracts import validate_tech_status

        for status in ("emerging", "mainstream", "declining", "experimental", ""):
            validate_tech_status(status)

    def test_validate_tech_status_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_tech_status

        with pytest.raises(ValueError):
            validate_tech_status("invalid")

    def test_validate_bargaining_direction_legal(self) -> None:
        from core.research.industry.contracts import validate_bargaining_direction

        for direction in ("upstream", "downstream", ""):
            validate_bargaining_direction(direction)

    def test_validate_bargaining_direction_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_bargaining_direction

        with pytest.raises(ValueError):
            validate_bargaining_direction("invalid")

    def test_validate_bargaining_level_legal(self) -> None:
        from core.research.industry.contracts import validate_bargaining_level

        for level in ("strong", "moderate", "weak", ""):
            validate_bargaining_level(level)

    def test_validate_bargaining_level_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_bargaining_level

        with pytest.raises(ValueError):
            validate_bargaining_level("invalid")

    def test_validate_capex_phase_legal(self) -> None:
        from core.research.industry.contracts import validate_capex_phase

        for phase in ("investment", "harvest", "decline", ""):
            validate_capex_phase(phase)

    def test_validate_capex_phase_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_capex_phase

        with pytest.raises(ValueError):
            validate_capex_phase("invalid")

    def test_validate_industry_code_system_legal(self) -> None:
        from core.research.industry.contracts import validate_industry_code_system

        for system in ("sw", "zx", "gics", ""):
            validate_industry_code_system(system)

    def test_validate_industry_code_system_illegal_raises(self) -> None:
        from core.research.industry.contracts import validate_industry_code_system

        with pytest.raises(ValueError):
            validate_industry_code_system("invalid")


# ---------------------------------------------------------------------------
# Causal context industry_ontology_summary 测试
# ---------------------------------------------------------------------------


class TestCausalContextIndustryOntologySummary:
    """causal_context.industry_ontology_summary() 方法测试。"""

    @pytest.fixture
    def research_db(self, tmp_path: Path) -> Path:
        """构造测试用 research_control 数据库。"""
        db_path = tmp_path / "research_control.db"
        connection = sqlite3.connect(db_path)
        connection.executescript("""
            CREATE TABLE claims (claim_id TEXT PRIMARY KEY, research_status TEXT, updated_at TEXT, payload_json TEXT);
            CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT, created_at TEXT, payload_json TEXT);
        """)
        claims = [
            (
                "theme",
                "SUPPORTED",
                {
                    "claim_id": "theme",
                    "subject_id": "000001.SZ",
                    "subject_name": "测试公司",
                    "relation": "member_of_theme",
                    "object_name": "测试赛道",
                    "research_status": "SUPPORTED",
                },
            ),
            (
                "causal",
                "SUPPORTED",
                {
                    "claim_id": "causal",
                    "subject_id": "000001.SZ",
                    "subject_name": "测试公司",
                    "relation": "SUPPLIES_TO",
                    "object_name": "客户甲",
                    "direction": "forward",
                    "research_status": "SUPPORTED",
                    "evidence_confidence": 0.8,
                },
            ),
        ]
        for claim_id, status, payload in claims:
            connection.execute(
                "INSERT INTO claims VALUES (?, ?, '', ?)",
                (claim_id, status, json.dumps(payload, ensure_ascii=False)),
            )
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, '', ?)",
            (
                "ev1",
                "causal",
                json.dumps(
                    {
                        "evidence_id": "ev1",
                        "claim_id": "causal",
                        "stance": "support",
                        "quote": "可定位原文",
                        "source_trace_id": "src1",
                        "published_at": "2026-06-20",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()
        connection.close()
        return db_path

    def test_summary_returns_dict_with_required_fields(
        self, research_db: Path
    ) -> None:
        from core.research.value_chain.causal_context import ResearchCausalContext

        summary = ResearchCausalContext(research_db).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        required_keys = {
            "products", "upstream", "downstream", "demand_drivers",
            "competitors", "market_share_evidence",
            "member_of_theme_excluded", "exploration_links_excluded",
            "observed_facts_count", "source", "gaps",
        }
        assert required_keys.issubset(summary.keys())

    def test_summary_competitors_always_empty(self, research_db: Path) -> None:
        from core.research.value_chain.causal_context import ResearchCausalContext

        summary = ResearchCausalContext(research_db).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        # 验收点 1：竞争结论只能从 CompetitiveLandscape 来
        assert summary["competitors"] == []

    def test_summary_market_share_evidence_always_empty(
        self, research_db: Path
    ) -> None:
        from core.research.value_chain.causal_context import ResearchCausalContext

        summary = ResearchCausalContext(research_db).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        # 验收点 1：份额证据不从 ontology 来
        assert summary["market_share_evidence"] == []

    def test_summary_excludes_member_of_theme(self, research_db: Path) -> None:
        from core.research.value_chain.causal_context import ResearchCausalContext

        summary = ResearchCausalContext(research_db).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        assert summary["member_of_theme_excluded"] is True
        # 数据库写入了 1 个 member_of_theme 关系
        assert summary["exploration_links_excluded"] >= 1

    def test_summary_downstream_includes_verified_edges(self, research_db: Path) -> None:
        from core.research.value_chain.causal_context import ResearchCausalContext

        summary = ResearchCausalContext(research_db).industry_ontology_summary(
            symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
        )
        # SUPPLIES_TO 应该出现在 downstream
        assert len(summary["downstream"]) >= 1
        assert summary["downstream"][0]["relation"] == "SUPPLIES_TO"


# ---------------------------------------------------------------------------
# IndustryGap 测试
# ---------------------------------------------------------------------------


class TestIndustryGap:
    """IndustryGap 数据类测试。"""

    def test_gap_to_dict_roundtrip(self) -> None:
        gap = IndustryGap(
            field_name="test_field",
            reason="test reason",
            blocker_kind="missing",
            severity="blocker",
            note="test note",
        )
        d = gap.to_dict()
        assert d["field_name"] == "test_field"
        assert d["reason"] == "test reason"
        assert d["blocker_kind"] == "missing"
        assert d["severity"] == "blocker"
        assert d["note"] == "test note"

    def test_gap_default_severity_is_blocker(self) -> None:
        gap = IndustryGap()
        assert gap.severity == "blocker"
        assert gap.blocker_kind == "missing"


class TestContractBoundaries:
    """契约序列化边界：坏输入降级，不阻断整个研究结果。"""

    def test_string_source_refs_do_not_expand_into_characters(self) -> None:
        payload = MarketSizing(source_refs="not-a-list").to_dict()

        assert payload["source_refs"] == []

    def test_malformed_price_points_are_skipped(self) -> None:
        payload = PriceCostCurve(
            price_points=["bad", ("2026-Q1", 100.0), ["incomplete"]],
        ).to_dict()

        assert payload["price_points"] == [["2026-Q1", 100.0]]

    def test_malformed_nested_items_are_skipped(self) -> None:
        payload = IndustryProfile(
            demand_drivers=[None, DemandDriver(name="valid")],  # type: ignore[list-item]
            company_mappings=[None],  # type: ignore[list-item]
            market_sizing={},  # type: ignore[arg-type]
        ).to_dict()

        assert payload["demand_drivers"] == [DemandDriver(name="valid").to_dict()]
        assert payload["company_mappings"] == []
        assert payload["market_sizing"] is None

    @pytest.mark.parametrize("validator", [
        "validate_market_sizing_method",
        "validate_demand_direction",
        "validate_supply_phase",
        "validate_price_trend",
        "validate_inventory_trend",
        "validate_share_basis",
        "validate_concentration_metric",
        "validate_tech_status",
        "validate_bargaining_direction",
        "validate_bargaining_level",
        "validate_capex_phase",
        "validate_lifecycle_stage",
        "validate_company_role",
        "validate_industry_code_system",
    ])
    def test_validators_reject_non_string_values_with_value_error(self, validator: str) -> None:
        from core.research.industry import contracts

        with pytest.raises(ValueError):
            getattr(contracts, validator)(123)


# ---------------------------------------------------------------------------
# 常量测试
# ---------------------------------------------------------------------------


class TestConstants:
    """常量定义测试。"""

    def test_no_local_evidence_constant_value(self) -> None:
        assert NO_LOCAL_EVIDENCE == "暂无本地证据"

    def test_lifecycle_stages_contains_all_phases(self) -> None:
        from core.research.industry.contracts import LIFECYCLE_STAGES

        assert "embryonic" in LIFECYCLE_STAGES
        assert "growth" in LIFECYCLE_STAGES
        assert "mature" in LIFECYCLE_STAGES
        assert "decline" in LIFECYCLE_STAGES

    def test_company_roles_contains_all_roles(self) -> None:
        from core.research.industry.contracts import COMPANY_ROLES

        assert "leader" in COMPANY_ROLES
        assert "challenger" in COMPANY_ROLES
        assert "follower" in COMPANY_ROLES
        assert "niche" in COMPANY_ROLES

    def test_gap_kinds_contains_required_types(self) -> None:
        from core.research.industry.contracts import (
            GAP_KIND_HEAT_ONLY,
            GAP_KIND_MEMBER_OF_THEME_LEAK,
            GAP_KIND_MISSING,
            GAP_KIND_STAGE_NO_EVIDENCE,
            GAP_KIND_STAGE_NO_TIME,
        )

        assert GAP_KIND_MISSING == "missing"
        assert GAP_KIND_HEAT_ONLY == "heat_only"
        assert GAP_KIND_STAGE_NO_EVIDENCE == "stage_no_evidence"
        assert GAP_KIND_STAGE_NO_TIME == "stage_no_time"
        assert GAP_KIND_MEMBER_OF_THEME_LEAK == "member_of_theme_leak"
