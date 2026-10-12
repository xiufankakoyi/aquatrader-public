from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _write_research_control_db(path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE claims (claim_id TEXT PRIMARY KEY, research_status TEXT, updated_at TEXT, payload_json TEXT);
        CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT, created_at TEXT, payload_json TEXT);
    """)
    claims = [
        ("theme", "SUPPORTED", {"claim_id": "theme", "subject_id": "000001.SZ", "subject_name": "测试公司", "relation": "member_of_theme", "object_name": "测试赛道", "research_status": "SUPPORTED"}),
        ("causal", "SUPPORTED", {"claim_id": "causal", "subject_id": "000001.SZ", "subject_name": "测试公司", "relation": "SUPPLIES_TO", "object_name": "客户甲", "direction": "forward", "research_status": "SUPPORTED", "evidence_confidence": 0.8}),
        ("unverified", "DISCOVERED", {"claim_id": "unverified", "subject_id": "000001.SZ", "subject_name": "测试公司", "relation": "PRODUCES", "object_name": "产品甲", "research_status": "DISCOVERED"}),
    ]
    for claim_id, status, payload in claims:
        connection.execute("INSERT INTO claims VALUES (?, ?, '', ?)", (claim_id, status, json.dumps(payload, ensure_ascii=False)))
    connection.execute(
        "INSERT INTO evidence VALUES (?, ?, '', ?)",
        ("ev1", "causal", json.dumps({"evidence_id": "ev1", "claim_id": "causal", "stance": "support", "quote": "可定位原文", "source_trace_id": "src1", "published_at": "2026-06-20"}, ensure_ascii=False)),
    )
    connection.commit()
    connection.close()


class FakeRegistry:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def get_stock_basic_info(self, symbols=None):
        self.logs.append({"method": "get_stock_basic_info", "provider_used": "fake_profile", "success": True, "row_count": 1})
        return pd.DataFrame([{
            "symbol": symbols[0], "stock_name": "测试公司", "industry": "测试行业",
            "main_business": "测试产品", "provider": "fake_profile",
        }])

    def get_daily_bars(self, start_date, end_date, symbols=None):
        rows = []
        for idx in range(30):
            rows.append({
                "trade_date": f"2026-06-{idx + 1:02d}" if idx < 30 else "2026-06-30",
                "symbol": symbols[0], "close": 10.0 + idx * 0.1,
                "volume": 1000 + idx, "amount": 10000 + idx,
                "provider": "fake_bars",
            })
        self.logs.append({"method": "get_daily_bars", "provider_used": "fake_bars", "success": True, "row_count": len(rows)})
        return pd.DataFrame(rows)

    def get_earnings_forecasts(self, start_date, end_date, symbols=None):
        self.logs.append({"method": "get_earnings_forecasts", "provider_used": "fake_forecast", "success": False, "row_count": 0, "error_message": "empty result"})
        return pd.DataFrame()

    def get_announcements(self, code, count=10):
        return [{"title": "测试公告", "date": "2026-06-30", "source": "fake_announcement"}]

    def smart_quote(self, code):
        return {"code": code, "price": 12.9, "pe": 0, "source": "fake_quote"}


def test_missing_valuation_is_not_treated_as_neutral() -> None:
    from core.research.value_chain.service import CompanyValueResearchService

    report = CompanyValueResearchService(registry=FakeRegistry()).build_dossier(
        "000001.SZ", as_of="2026-06-30"
    )

    assert report.valuation.status == "not_computable"
    assert report.valuation.metrics == {}
    assert "valuation_missing" in report.degradation_reasons


def test_market_data_does_not_masquerade_as_financial_fundamentals() -> None:
    from core.research.value_chain.service import CompanyValueResearchService

    report = CompanyValueResearchService(registry=FakeRegistry()).build_dossier(
        "000001.SZ", as_of="2026-06-30"
    )

    assert report.financial_quality.status == "not_computable"
    assert report.financial_quality.metrics == {}
    assert "financial_history_missing" in report.degradation_reasons
    assert report.technical_context["technical_state"] != "UNKNOWN"


def test_run_writes_a_new_auditable_directory(tmp_path: Path) -> None:
    from core.research.value_chain.service import CompanyValueResearchService

    result = CompanyValueResearchService(registry=FakeRegistry()).run(
        "000001.SZ", as_of="2026-06-30", output_root=tmp_path
    )

    run_dir = Path(result["run_dir"])
    assert run_dir.parent == tmp_path
    assert (run_dir / "dossier.json").exists()
    assert (run_dir / "acceptance.json").exists()
    dossier = json.loads((run_dir / "dossier.json").read_text(encoding="utf-8"))
    assert dossier["symbol"] == "000001.SZ"
    assert dossier["source_snapshots"]["profile"]["provider"] == "fake_profile"
    assert dossier["source_snapshots"]["earnings_forecast"]["status"] == "empty"


def test_default_run_indexes_the_same_control_repository_used_by_api(
    tmp_path: Path, monkeypatch,
) -> None:
    from core.research.value_chain.repository import ResearchRunRepository
    from core.research.value_chain.service import CompanyValueResearchService

    control_db = tmp_path / "research_control" / "research_value_runs.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RESEARCH_VALUE_RUNS_DB_PATH", str(control_db))
    result = CompanyValueResearchService(registry=FakeRegistry()).run(
        "000001.SZ", as_of="2026-06-30",
    )

    repo = ResearchRunRepository(
        db_path=control_db,
        runs_root=tmp_path / "data" / "research_value_runs",
    )
    assert repo.get_run(result["run_id"]) is not None
    assert not (tmp_path / "data" / "research_value_runs" / "research_value_runs.db").exists()


def test_dossier_exposes_formal_research_facts(tmp_path: Path) -> None:
    from core.research.value_chain.causal_context import ResearchCausalContext
    from core.research.value_chain.service import CompanyValueResearchService

    research_db = tmp_path / "research_control.db"
    _write_research_control_db(research_db)
    dossier = CompanyValueResearchService(
        registry=FakeRegistry(),
        causal_context=ResearchCausalContext(research_db),
    ).build_dossier("000001.SZ", as_of="2026-06-30").to_dict()

    assert dossier["schema_version"] == "company_dossier_v8"
    assert dossier["research_facts"]
    fact = dossier["research_facts"][0]
    assert fact["fact_id"] == "causal"
    assert fact["source_trace_id"] == "src1"
    assert fact["evidence_ids"] == ["ev1"]
    assert "research_assumptions" in dossier


def test_financial_quality_only_computes_supported_metrics() -> None:
    from core.research.value_chain.fundamentals import compute_financial_quality

    result = compute_financial_quality([
        {"period": "2024", "revenue": 100.0, "net_profit": 10.0, "operating_cash_flow": 8.0},
        {"period": "2025", "revenue": 120.0, "net_profit": 15.0, "operating_cash_flow": 18.0},
    ])

    assert result.status == "partial"
    assert result.metrics["revenue_growth"].value == 0.2
    assert result.metrics["net_profit_growth"].value == 0.5
    assert result.metrics["cash_conversion"].value == 1.2
    assert "roe" in result.missing_metrics


def test_financial_quality_skips_malformed_and_nonfinite_values() -> None:
    from core.research.value_chain.fundamentals import compute_financial_quality

    result = compute_financial_quality([
        None,
        {"period": "2024", "revenue": "nan", "net_profit": "inf"},
        {"period": "2025", "revenue": 100.0, "net_profit": 10.0, "operating_cash_flow": 5.0},
    ])

    assert result.status == "partial"
    assert "revenue_growth" not in result.metrics
    assert result.metrics["cash_conversion"].value == 0.5


def test_historical_run_filters_future_and_unknown_visibility_records() -> None:
    from core.research.value_chain.source_gateway import ResearchSourceGateway

    registry = FakeRegistry()
    registry.get_announcements = lambda code, count=10: [
        {"title": "visible", "date": "2026-06-29", "source": "fake_announcement"},
        {"title": "future", "date": "2026-07-01", "source": "fake_announcement"},
        {"title": "unknown", "source": "fake_announcement"},
    ]

    snapshots = ResearchSourceGateway(registry=registry).collect(
        "000001.SZ", as_of="2026-06-30"
    )

    assert [item["title"] for item in snapshots["announcements"].records] == ["visible"]
    assert snapshots["announcements"].excluded_future_records == 1
    assert snapshots["announcements"].excluded_unknown_time_records == 1


def test_source_gateway_visibility_filter_does_not_use_pandas_datetime(monkeypatch) -> None:
    import pandas as pd

    from core.research.value_chain.source_gateway import ResearchSourceGateway

    def fail_to_datetime(*args, **kwargs):
        raise AssertionError("visibility filtering should use the standard library")

    monkeypatch.setattr(pd, "to_datetime", fail_to_datetime)
    snapshot = ResearchSourceGateway(registry=FakeRegistry())._capture(
        "announcements",
        "2026-06-30",
        lambda: [{"date": "2026-06-29"}, {"date": "2026-07-01"}],
        visible_time_fields=("date",),
    )

    assert snapshot.status == "ok"
    assert snapshot.records == [{"date": "2026-06-29"}]
    assert snapshot.excluded_future_records == 1


def test_source_gateway_ignores_malformed_snapshot_cache(tmp_path: Path) -> None:
    from core.research.value_chain.contracts import SourceSnapshot
    from core.research.value_chain.source_gateway import ResearchSourceGateway

    gateway = ResearchSourceGateway(registry=FakeRegistry(), cache_dir=tmp_path)
    cache_path = gateway._cache_path("000001.SZ", "consensus_estimates")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("[]", encoding="utf-8")
    snapshot = SourceSnapshot(
        capability="consensus_estimates", status="failed", as_of="2026-06-30"
    )

    assert gateway._fallback_to_cache(
        "000001.SZ", snapshot, visible_time_fields=("report_date",)
    ) is snapshot


def test_financial_growth_uses_same_reporting_period_year_over_year() -> None:
    from core.research.value_chain.fundamentals import compute_financial_quality

    result = compute_financial_quality([
        {"period": "20250331", "revenue": 100.0, "net_profit": 10.0},
        {"period": "20251231", "revenue": 500.0, "net_profit": 50.0},
        {"period": "20260331", "revenue": 130.0, "net_profit": 15.0},
    ])

    assert result.metrics["revenue_growth"].value == 0.3
    assert result.metrics["net_profit_growth"].value == 0.5


def test_causal_context_separates_verified_edges_from_theme_exploration(tmp_path: Path) -> None:
    from core.research.value_chain.causal_context import ResearchCausalContext

    db_path = tmp_path / "research_control.db"
    _write_research_control_db(db_path)
    context = ResearchCausalContext(db_path).for_company(
        symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
    )

    assert [edge["claim_id"] for edge in context["verified_edges"]] == ["causal"]
    assert [edge["claim_id"] for edge in context["exploration_links"]] == ["theme"]
    assert [edge["claim_id"] for edge in context["rejected_edges"]] == ["unverified"]


def test_causal_context_does_not_treat_operating_observations_as_causal_edges(tmp_path: Path) -> None:
    import sqlite3

    from core.research.value_chain.causal_context import ResearchCausalContext

    db_path = tmp_path / "research_control.db"
    _write_research_control_db(db_path)
    connection = sqlite3.connect(db_path)
    payload = {
        "claim_id": "observed", "subject_id": "000001.SZ", "subject_name": "测试公司",
        "relation": "OBSERVED_CAPACITY", "object_name": "产品甲", "research_status": "CANDIDATE",
    }
    connection.execute(
        "INSERT INTO claims VALUES (?, ?, '', ?)",
        ("observed", "CANDIDATE", json.dumps(payload, ensure_ascii=False)),
    )
    connection.execute(
        "INSERT INTO evidence VALUES (?, ?, '', ?)",
        ("ev-observed", "observed", json.dumps({
            "evidence_id": "ev-observed", "claim_id": "observed", "stance": "support",
            "quote": "原文", "source_trace_id": "src2", "published_at": "2026-06-20",
        }, ensure_ascii=False)),
    )
    connection.commit(); connection.close()

    context = ResearchCausalContext(db_path).for_company(
        symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
    )

    assert [edge["claim_id"] for edge in context["verified_edges"]] == ["causal"]
    assert [edge["claim_id"] for edge in context["observed_facts"]] == ["observed"]


def test_causal_context_skips_non_object_json_payloads(tmp_path: Path) -> None:
    import sqlite3

    from core.research.value_chain.causal_context import ResearchCausalContext

    db_path = tmp_path / "research_control.db"
    _write_research_control_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO evidence VALUES ('bad-evidence', 'causal', '', '[]')"
    )
    connection.execute(
        "INSERT INTO claims VALUES ('bad-claim', 'CANDIDATE', '', '[]')"
    )
    connection.commit()
    connection.close()

    context = ResearchCausalContext(db_path).for_company(
        symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30"
    )

    assert [edge["claim_id"] for edge in context["verified_edges"]] == ["causal"]


def test_profit_bridge_does_not_treat_stale_guidance_as_forward_forecast() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    result = build_profit_bridge(
        financial_history=[{"period": "20251231", "net_profit": 100.0}],
        earnings_forecasts=[{
            "period_end": "20251231", "net_profit_min_wan": 0.005,
            "net_profit_max_wan": 0.015, "announcement_date": "20260131",
        }],
    )

    assert result["status"] == "observed_only"
    assert result["forward_guidance"] == []
    assert len(result["stale_guidance"]) == 1
    assert "forward_forecast_missing" in result["gaps"]


def test_profit_bridge_keeps_real_forward_guidance_range() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    result = build_profit_bridge(
        financial_history=[{"period": "20251231", "net_profit": 100.0}],
        earnings_forecasts=[{
            "period_end": "20261231", "net_profit_min_wan": 0.015,
            "net_profit_max_wan": 0.02, "announcement_date": "20260701",
        }],
    )

    assert result["status"] == "partial"
    assert result["forward_guidance"][0]["net_profit_min"] == 150.0
    assert result["growth_range"] == {"min": 0.5, "max": 1.0}


def test_profit_bridge_skips_malformed_rows_and_consensus_values() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    observed = [
        None,
        *[
            {
                "period": "20251231",
                "metric": metric,
                "value": value,
                "observation_kind": "historical_disclosed",
                "eligible_as_forward_assumption": False,
            }
            for metric, value in (
                ("capacity", 100.0),
                ("production", 80.0),
                ("sales_volume", 72.0),
                ("revenue", 720.0),
            )
        ],
    ]
    result = build_profit_bridge(
        financial_history=[None, {"period": "20251231", "revenue": 720.0, "net_profit": 100.0}],
        earnings_forecasts=[None, {"period_end": "20261231", "net_profit_min_wan": "nan"}],
        observed_operating_facts=observed,
        structured_consensus={
            "status": "verified",
            "periods": {
                "2026Q4": None,
                "2027Q4": {
                    "source_count": "invalid",
                    "revenue_yuan": {"median": 900.0},
                    "net_profit_yuan": {"median": "nan"},
                },
            },
        },
    )

    assert result["market_implied_scenarios"][0]["source_count"] == 0
    assert result["market_implied_scenarios"][0]["implied_net_margin"] is None


def test_expectation_context_rejects_rows_without_real_evidence(tmp_path: Path) -> None:
    import sqlite3
    from core.research.value_chain.expectation_context import ExpectationContext

    expectation_db = tmp_path / "expectations.db"
    research_db = tmp_path / "research.db"
    connection = sqlite3.connect(expectation_db)
    connection.execute("CREATE TABLE market_expectations (expectation_id TEXT PRIMARY KEY, symbol TEXT, created_at TEXT, payload_json TEXT)")
    for expectation_id, evidence_ids in (("real", ["ev-real"]), ("demo", ["ev-missing"])):
        payload = {"expectation_id": expectation_id, "symbol": "000001.SZ", "expectation_source": "consensus", "evidence_ids": evidence_ids, "available_at": "2026-06-20"}
        connection.execute("INSERT INTO market_expectations VALUES (?, ?, '', ?)", (expectation_id, "000001.SZ", json.dumps(payload)))
    connection.commit(); connection.close()
    connection = sqlite3.connect(research_db)
    connection.execute("CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT, created_at TEXT, payload_json TEXT)")
    connection.execute("INSERT INTO evidence VALUES ('ev-real', '', '', ?)", (json.dumps({"evidence_id": "ev-real", "published_at": "2026-06-19", "quote": "source"}),))
    connection.commit(); connection.close()

    result = ExpectationContext(expectation_db, research_db).for_company("000001.SZ", "2026-06-30")

    assert [item["expectation_id"] for item in result["verified_expectations"]] == ["real"]
    assert [item["expectation_id"] for item in result["rejected_expectations"]] == ["demo"]


def test_expectation_context_skips_non_object_json_payloads(tmp_path: Path) -> None:
    import sqlite3

    from core.research.value_chain.expectation_context import ExpectationContext

    expectation_db = tmp_path / "expectations.db"
    research_db = tmp_path / "research.db"
    connection = sqlite3.connect(expectation_db)
    connection.execute(
        "CREATE TABLE market_expectations (expectation_id TEXT PRIMARY KEY, symbol TEXT, created_at TEXT, payload_json TEXT)"
    )
    connection.execute(
        "INSERT INTO market_expectations VALUES ('bad', '000001.SZ', '', '[]')"
    )
    connection.commit()
    connection.close()
    connection = sqlite3.connect(research_db)
    connection.execute(
        "CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, claim_id TEXT, created_at TEXT, payload_json TEXT)"
    )
    connection.execute("INSERT INTO evidence VALUES ('bad', '', '', '[]')")
    connection.commit()
    connection.close()

    result = ExpectationContext(expectation_db, research_db).for_company(
        "000001.SZ", "2026-06-30"
    )

    assert result["verified_expectations"] == []
    assert result["rejected_expectations"] == []
    assert result["gaps"] == ["verified_market_expectation_missing"]


def test_gap_task_planner_is_idempotent(tmp_path: Path) -> None:
    from core.research.control_plane.repository import ResearchRepository
    from core.research.value_chain.gap_tasks import GapTaskPlanner

    repository = ResearchRepository(tmp_path / "research_control.db")
    planner = GapTaskPlanner(repository)
    gaps = ["verified_causal_chain_missing", "forward_profit_bridge_missing"]

    first = planner.enqueue(symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30", gaps=gaps)
    second = planner.enqueue(symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30", gaps=gaps)

    assert len(first["created_task_ids"]) == 2
    assert second["created_task_ids"] == []
    assert len(second["existing_task_ids"]) == 2
    assert all(repository.get_task(task_id).status == "partial" for task_id in first["created_task_ids"])


def test_gap_task_planner_treats_missing_gaps_as_empty(tmp_path: Path) -> None:
    from core.research.control_plane.repository import ResearchRepository
    from core.research.value_chain.gap_tasks import GapTaskPlanner

    result = GapTaskPlanner(ResearchRepository(tmp_path / "research_control.db")).enqueue(
        symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30", gaps=None
    )

    assert result == {"created_task_ids": [], "existing_task_ids": []}


def test_gap_discovery_creates_only_grounded_candidate_claims(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from core.llm_processing.extraction_schema import LLMExtractionOutput
    from core.research.control_plane.repository import ResearchRepository
    from core.research.value_chain.gap_discovery import GapDiscoveryExecutor
    from core.research.value_chain.gap_tasks import GapTaskPlanner

    repository = ResearchRepository(tmp_path / "research_control.db")
    task_id = GapTaskPlanner(repository).enqueue(
        symbol="000001.SZ", company_name="测试公司", as_of="2026-06-30",
        gaps=["verified_causal_chain_missing"],
    )["created_task_ids"][0]
    document = SimpleNamespace(
        title="测试公司业务公告",
        content_text="000001 测试公司向客户甲供应产品甲。",
        url="https://example.com/a",
        source_type="news",
        published_at="2026-06-20",
        retrieved_at="2026-06-21T00:00:00+00:00",
        metadata={"symbol": "000001.SZ", "snippet_only": False},
    )

    class Retriever:
        def search(self, query, *, tier, limit):
            return [document]

    class Extractor:
        available = True

        def extract(self, chunk_text, source_text, metadata):
            return LLMExtractionOutput(extracted_relations=[{
                "from_entity": "测试公司", "relation_type": "SUPPLIES_TO",
                "to_entity": "客户甲", "evidence_text": "测试公司向客户甲供应产品甲",
                "confidence": "high",
            }]), []

    result = GapDiscoveryExecutor(repository, Retriever(), Extractor()).execute(
        repository.get_task(task_id)
    )

    assert result["status"] == "succeeded"
    claim = repository.get_claim(result["claim_ids"][0])
    assert claim.relation == "SUPPLIES_TO"
    assert claim.object_name == "客户甲"
    assert claim.research_status == "CANDIDATE"
    assert len(repository.list_evidence(claim.claim_id)) == 1
    assert len(repository.list_transitions(claim.claim_id)) == 1
    assert all(item["eligible_for_evidence_slot"] is False for item in repository.get_task(task_id).params["retrieval_hypotheses"])
    assert repository.get_task(task_id).progress["relation_audit"][0]["decision"] == "accepted"


def test_gap_discovery_visibility_rejects_invalid_cutoff() -> None:
    from types import SimpleNamespace

    from core.research.value_chain.gap_discovery import GapDiscoveryExecutor

    assert GapDiscoveryExecutor._visible(
        SimpleNamespace(published_at=""), "not-a-date"
    ) is False


def test_gap_discovery_retriever_prefers_first_party_documents() -> None:
    from types import SimpleNamespace
    from core.research.value_chain.gap_discovery import GapDiscoveryRetriever

    official = SimpleNamespace(url="https://official/a")
    web = SimpleNamespace(url="https://web/b")

    class FirstParty:
        def search(self, query, *, tier, limit, context):
            return SimpleNamespace(documents=[official])

    class Web:
        def search(self, query, *, tier, limit):
            return [web]

    documents = GapDiscoveryRetriever(
        FirstParty(), Web(), {"symbol": "000001.SZ"}
    ).search("query", tier="gap_discovery", limit=2)

    assert documents == [official, web]


def test_gap_discovery_retriever_calls_cninfo_provider_directly() -> None:
    from types import SimpleNamespace
    from core.research.value_chain.gap_discovery import GapDiscoveryRetriever

    official = SimpleNamespace(url="https://official/report")

    class Cninfo:
        def search(self, query, *, tier, limit, context):
            return [official]

    class FirstParty:
        cninfo_pdf_retriever = Cninfo()

        def search(self, *args, **kwargs):
            raise AssertionError("must not wrap CNINFO discovery in the aggregate timeout")

    class Web:
        def search(self, query, *, tier, limit):
            return []

    documents = GapDiscoveryRetriever(
        FirstParty(), Web(), {"discovery_terms": ["产品"]}
    ).search("query", tier="gap_discovery", limit=1)

    assert documents == [official]


def test_focused_relation_extractor_requests_exact_grounded_schema() -> None:
    from core.research.value_chain.gap_discovery import FocusedRelationExtractor

    captured = {}

    def caller(messages, **kwargs):
        captured["messages"] = messages
        return json.dumps({"extracted_relations": [{
            "from_entity": "测试公司", "relation_type": "PRODUCES",
            "to_entity": "产品甲", "evidence_text": "测试公司生产产品甲",
            "confidence": "high",
        }]}, ensure_ascii=False)

    extraction, _ = FocusedRelationExtractor(caller=caller).extract(
        "测试公司生产产品甲", "测试公司生产产品甲",
        {"symbol": "000001.SZ", "stock_name": "测试公司", "title": "年报"},
    )

    assert extraction.extracted_relations[0]["to_entity"] == "产品甲"
    assert "连续原文" in captured["messages"][0]["content"]


def test_focused_relation_extractor_falls_back_to_explicit_annual_report_sentence() -> None:
    from core.research.value_chain.gap_discovery import FocusedRelationExtractor

    source = "公司主营业务为高端光通信收发模块的研发、生产及销售，产品服务于国内外客户。"
    extraction, _ = FocusedRelationExtractor(caller=lambda *args, **kwargs: "模型未返回JSON").extract(
        source, source, {"symbol": "300308.SZ", "stock_name": "中际旭创", "title": "年度报告"},
    )

    assert {(item["relation_type"], item["to_entity"]) for item in extraction.extracted_relations} == {
        ("DEVELOPS", "高端光通信收发模块"),
        ("PRODUCES", "高端光通信收发模块"),
    }
    assert all(item["evidence_text"] in source for item in extraction.extracted_relations)


def test_focused_relation_extractor_builds_downstream_and_demand_driver_edges() -> None:
    from core.research.value_chain.gap_discovery import FocusedRelationExtractor

    source = (
        "公司主营业务为高端光通信收发模块的研发、生产及销售，产品服务于云计算数据中心、"
        "数据通信和5G无线网络等领域的国内外客户。\n"
        "报告期内，受益于终端客户对算力基础设施的强劲投入，公司产品出货较快增长。"
    )
    extraction, _ = FocusedRelationExtractor(caller=lambda *args, **kwargs: "not-json").extract(
        source, source, {"symbol": "300308.SZ", "stock_name": "中际旭创", "title": "年度报告"},
    )

    relations = {(item["relation_type"], item["to_entity"]) for item in extraction.extracted_relations}
    assert ("SUPPLIES_TO", "云计算数据中心、数据通信和5G无线网络等领域的国内外客户") in relations
    assert ("BENEFITS_FROM", "终端客户对算力基础设施的强劲投入") in relations
    assert all(item["evidence_text"] in source for item in extraction.extracted_relations)


def test_focused_relation_extractor_keeps_industry_upstream_as_ontology_edges() -> None:
    from core.research.value_chain.gap_discovery import FocusedRelationExtractor

    source = "光模块行业的上游主要是光芯片、电芯片、有源光器件和无源光器件等。"
    extraction, _ = FocusedRelationExtractor(caller=lambda *args, **kwargs: "{}").extract(
        source, source, {"symbol": "300308.SZ", "stock_name": "中际旭创", "title": "年度报告"},
    )

    assert {(item["from_entity"], item["relation_type"], item["to_entity"]) for item in extraction.extracted_relations} == {
        ("光模块", "REQUIRES", "光芯片"),
        ("光模块", "REQUIRES", "电芯片"),
        ("光模块", "REQUIRES", "有源光器件"),
        ("光模块", "REQUIRES", "无源光器件"),
    }
    assert all(item["industry_ontology"] is True for item in extraction.extracted_relations)


def test_consensus_summary_does_not_construct_pandas_frame(monkeypatch) -> None:
    import pandas as pd
    from core.research.value_chain.consensus import summarize_consensus

    def fail_dataframe(*args, **kwargs):
        raise AssertionError("consensus should not construct pandas frames")

    monkeypatch.setattr(pd, "DataFrame", fail_dataframe)

    result = summarize_consensus(
        [{"org_name": "机构甲", "report_date": "2026-07-01", "forecast_period": "2027Q4", "eps_forecast": 1.0}],
        as_of="2026-07-18",
        minimum_sources=1,
    )

    assert result["status"] == "verified"


def test_consensus_summary_uses_latest_row_per_independent_organization() -> None:
    from core.research.value_chain.consensus import summarize_consensus

    records = [
        {"org_name": "机构甲", "report_date": "2026-06-01", "forecast_period": "2027Q4", "net_profit_forecast_yuan": 100.0, "eps_forecast": 1.0},
        {"org_name": "机构甲", "report_date": "2026-07-01", "forecast_period": "2027Q4", "net_profit_forecast_yuan": 120.0, "eps_forecast": 1.2},
        {"org_name": "机构乙", "report_date": "2026-06-20", "forecast_period": "2027Q4", "net_profit_forecast_yuan": 140.0, "eps_forecast": 1.4},
        {"org_name": "机构丙", "report_date": "2026-06-25", "forecast_period": "2027Q4", "net_profit_forecast_yuan": 160.0, "eps_forecast": 1.6},
    ]

    result = summarize_consensus(records, as_of="2026-07-18")

    period = result["periods"]["2027Q4"]
    assert result["status"] == "verified"
    assert period["source_count"] == 3
    assert period["net_profit_yuan"]["median"] == 140.0
    assert len(period["lineage"]) == 3


def test_consensus_summary_rejects_blank_or_null_forecast_periods() -> None:
    from core.research.value_chain.consensus import summarize_consensus

    records = [
        {"org_name": "机构甲", "report_date": "2026-07-01", "forecast_period": None, "eps_forecast": 1.0},
        {"org_name": "机构乙", "report_date": "2026-07-01", "forecast_period": "", "eps_forecast": 1.1},
        {"org_name": "机构甲", "report_date": "2026-07-01", "forecast_period": "2027Q4", "eps_forecast": 1.2},
    ]

    result = summarize_consensus(records, as_of="2026-07-18")

    assert set(result["periods"]) == {"2027Q4"}
    assert result["status"] == "insufficient_sources"


def test_operating_table_parser_extracts_only_disclosed_historical_values() -> None:
    from core.research.value_chain.operational_facts import parse_operating_tables

    text = """
    中际旭创股份有限公司 2025 年年度报告全文
    2、公司生产经营和投资项目情况
    产品名称 本报告期 上年同期
    产能 产量 销量 营业收入 毛利率 产能 产量 销量 营业收入 毛利率
    光通\n信收\n发模\n块
    2,806\n万只 2,376\n万只 2,109\n万只 37,456,518,745.90 42.61%
    2,088\n万只 1,536\n万只 1,459\n万只 22,885,756,727.93 34.65%
    变化情况
    """

    tables = parse_operating_tables(text)

    assert len(tables) == 1
    table = tables[0]
    assert table["product_name"] == "光通信收发模块"
    assert table["current_period"] == "20251231"
    assert table["previous_period"] == "20241231"
    assert table["current"]["capacity"] == 28_060_000
    assert table["current"]["revenue"] == 37_456_518_745.90
    assert table["current"]["gross_margin"] == 0.4261
    assert table["observation_kind"] == "historical_disclosed"
    assert table["eligible_as_forward_assumption"] is False
    assert table["quote"] in text


def test_forward_plan_parser_keeps_qualitative_company_plan_out_of_numeric_inputs() -> None:
    from core.research.value_chain.operational_facts import parse_forward_plan_indicators

    text = """
    （二）2026 年度工作计划
    1、继续加大 1.6T、800G 等高端产品的交付能力和出货量，进一步巩固提升公司在海外市场的先发优势、营业收入和市场份额。
    2、持续推进公司国际化战略。
    """

    indicators = parse_forward_plan_indicators(text)

    assert len(indicators) == 1
    indicator = indicators[0]
    assert indicator["period"] == "2026"
    assert indicator["products"] == ["1.6T", "800G"]
    assert indicator["direction"] == "increase"
    assert indicator["metric"] == "delivery_and_shipment_direction"
    assert indicator["eligible_as_forward_assumption"] is False
    assert indicator["reason_ineligible"] == "magnitude_not_disclosed"
    assert indicator["quote"] in text


def test_operating_table_flattener_skips_malformed_tables() -> None:
    from core.research.value_chain.operational_facts import flatten_operating_tables

    rows = flatten_operating_tables([
        None,
        {"current": None, "previous": []},
        {"product_name": "产品", "current_period": "20251231", "current": {"capacity": 10}},
    ])

    assert rows == [{
        "product_name": "产品",
        "period": "20251231",
        "metric": "capacity",
        "value": 10,
        "unit": "pieces",
        "observation_kind": "historical_disclosed",
        "eligible_as_forward_assumption": False,
        "quote": "",
    }]


def test_profit_bridge_separates_observed_drivers_from_forward_assumptions() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    result = build_profit_bridge(
        financial_history=[{"period": "20251231", "net_profit": 100.0}],
        earnings_forecasts=[],
        observed_operating_facts=[{
            "product_name": "产品甲",
            "period": "20251231",
            "metric": "capacity",
            "value": 200.0,
            "unit": "pieces",
            "source_evidence_id": "ev-capacity",
            "observation_kind": "historical_disclosed",
            "eligible_as_forward_assumption": False,
        }],
    )

    assert result["status"] == "observed_only"
    assert len(result["observed_historical_drivers"]) == 1
    assert result["forward_driver_assumptions"] == []
    assert "forward_forecast_missing" in result["gaps"]


def test_profit_discovery_persists_grounded_history_but_keeps_forward_gap(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from core.research.control_plane.repository import ResearchRepository
    from core.research.value_chain.gap_tasks import GapTaskPlanner
    from core.research.value_chain.profit_discovery import (
        ProfitBridgeDiscoveryExecutor,
        load_observed_operating_facts,
    )

    repository = ResearchRepository(tmp_path / "research_control.db")
    task_id = GapTaskPlanner(repository).enqueue(
        symbol="300308.SZ", company_name="中际旭创", as_of="2026-07-18",
        gaps=["forward_profit_bridge_missing"],
    )["created_task_ids"][0]
    content = """
    中际旭创股份有限公司 2025 年年度报告全文
    产品名称 本报告期 上年同期
    产能 产量 销量 营业收入 毛利率 产能 产量 销量 营业收入 毛利率
    光通\n信收\n发模\n块
    2,806\n万只 2,376\n万只 2,109\n万只 37,456,518,745.90 42.61%
    2,088\n万只 1,536\n万只 1,459\n万只 22,885,756,727.93 34.65%
    """
    document = SimpleNamespace(
        title="2025年年度报告", content_text=content,
        url="https://static.cninfo.com.cn/report.pdf",
        published_at="2026-03-31", retrieved_at="2026-07-18T00:00:00+00:00",
        metadata={"symbol": "300308.SZ", "source_backend": "cninfo_pdf"},
    )

    class Retriever:
        def search(self, query, *, tier, limit):
            return [document]

    result = ProfitBridgeDiscoveryExecutor(repository, Retriever()).execute(
        repository.get_task(task_id)
    )
    facts = load_observed_operating_facts(
        repository, symbol="300308.SZ", as_of="2026-07-18"
    )

    assert result["status"] == "partial"
    assert result["observed_fact_count"] == 10
    assert result["forward_assumption_count"] == 0
    assert {item["metric"] for item in facts} == {
        "capacity", "production", "sales_volume", "revenue", "gross_margin"
    }
    assert all(item["eligible_as_forward_assumption"] is False for item in facts)
    assert repository.get_task(task_id).failure["code"] == "FORWARD_ASSUMPTIONS_STILL_MISSING"


def test_profit_discovery_loaders_skip_malformed_claim_metadata() -> None:
    from types import SimpleNamespace

    from core.research.value_chain.profit_discovery import (
        load_forward_disclosed_indicators,
        load_observed_operating_facts,
    )

    class Repository:
        def list_claims(self, *, limit):
            return [SimpleNamespace(
                claim_id="bad",
                subject_id="000001.SZ",
                research_status="SUPPORTED",
                attributes=["not-a-mapping"],
            )]

        def list_evidence(self, claim_id, *, limit):
            return [SimpleNamespace(
                evidence_id="ev-bad",
                published_at="2026-06-01",
                locator=[],
            )]

    repository = Repository()

    assert load_observed_operating_facts(
        repository, symbol="000001.SZ", as_of="2026-06-30"
    ) == []
    assert load_forward_disclosed_indicators(
        repository, symbol="000001.SZ", as_of="2026-06-30"
    ) == []


def test_profit_bridge_computes_market_implied_scenario_without_calling_it_guidance() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    observed = []
    for metric, value, unit in (
        ("capacity", 100.0, "pieces"),
        ("production", 80.0, "pieces"),
        ("sales_volume", 72.0, "pieces"),
        ("revenue", 720.0, "yuan"),
        ("gross_margin", 0.40, "ratio"),
    ):
        observed.append({
            "product_name": "产品甲", "period": "20251231", "metric": metric,
            "value": value, "unit": unit, "source_evidence_id": f"ev-{metric}",
            "observation_kind": "historical_disclosed",
            "eligible_as_forward_assumption": False,
        })
    consensus = {
        "status": "verified",
        "periods": {
            "2026Q1": {
                "source_count": 5,
                "revenue_yuan": {"median": 200.0},
                "net_profit_yuan": {"median": 40.0},
            },
            "2026Q4": {
                "source_count": 5,
                "revenue_yuan": {"median": 900.0},
                "net_profit_yuan": {"median": 180.0},
            }
        },
    }

    result = build_profit_bridge(
        financial_history=[{"period": "20251231", "revenue": 720.0, "net_profit": 100.0}],
        earnings_forecasts=[], observed_operating_facts=observed,
        structured_consensus=consensus,
    )

    scenario = result["market_implied_scenarios"][0]
    assert result["status"] == "market_implied"
    assert len(result["market_implied_scenarios"]) == 1
    assert scenario["period"] == "2026Q4"
    assert scenario["revenue_growth_vs_baseline"] == 0.25
    assert scenario["required_sales_if_flat_asp"] == 90.0
    assert scenario["required_capacity_if_flat_efficiency"] == 125.0
    assert scenario["implied_net_margin"] == 0.2
    assert scenario["is_company_guidance"] is False
    assert scenario["updates_fact_confidence"] is False
    assert scenario["historical_product_revenue_exposure"] == 1.0
    assert result["baseline"]["revenue"] == 720.0
    assert result["forward_driver_assumptions"] == []


def test_profit_bridge_does_not_default_missing_product_revenue_exposure_to_one() -> None:
    from core.research.value_chain.profit_bridge import build_profit_bridge

    observed = [
        {
            "product_name": "产品甲",
            "period": "20251231",
            "metric": metric,
            "value": value,
            "unit": unit,
            "source_evidence_id": f"ev-{metric}",
            "observation_kind": "historical_disclosed",
            "eligible_as_forward_assumption": False,
        }
        for metric, value, unit in (
            ("capacity", 100.0, "pieces"),
            ("production", 80.0, "pieces"),
            ("sales_volume", 72.0, "pieces"),
            ("revenue", 720.0, "yuan"),
        )
    ]
    consensus = {
        "status": "verified",
        "periods": {
            "2026Q4": {
                "source_count": 5,
                "revenue_yuan": {"median": 900.0},
                "net_profit_yuan": {"median": 180.0},
            },
        },
    }

    result = build_profit_bridge(
        financial_history=[{"period": "20251231", "net_profit": 100.0}],
        earnings_forecasts=[],
        observed_operating_facts=observed,
        structured_consensus=consensus,
    )

    assert result["market_implied_scenarios"] == []
    assert "historical_product_revenue_exposure_missing" in result["gaps"]


def test_formal_research_records_normalize_scalar_source_ids() -> None:
    from core.research.value_chain.records import build_formal_research_records

    facts, assumptions = build_formal_research_records(
        symbol="000001.SZ",
        industry_context={"verified_edges": [None], "observed_facts": [None]},
        profit_bridge={
            "observed_historical_drivers": [None],
            "forward_driver_assumptions": [{
                "source_type": "analyst_assumption",
                "assumption_id": "a1",
                "owner": "researcher",
                "falsification_condition": "condition",
                "interval": {"low": 1.0, "high": 2.0},
                "source_fact_ids": "fact-1",
                "source_evidence_ids": "evidence-1",
            }],
        },
        source_snapshots={"finance": {"records": [None]}},
    )

    assert facts == []
    assert assumptions[0].source_fact_ids == ["fact-1"]
    assert assumptions[0].source_evidence_ids == ["evidence-1"]


def test_valuation_context_does_not_construct_pandas_frame(monkeypatch) -> None:
    import pandas as pd
    from core.research.value_chain.valuation_context import summarize_valuation_history

    def fail_dataframe(*args, **kwargs):
        raise AssertionError("valuation history should not construct pandas frames")

    monkeypatch.setattr(pd, "DataFrame", fail_dataframe)

    result = summarize_valuation_history(
        [{"trade_date": "2026-07-03", "pe_ttm": 40.0}],
        structured_consensus={"status": "missing"},
        as_of="2026-07-03",
    )

    assert result["status"] == "ok"


def test_valuation_context_reports_history_percentiles_and_forward_multiples() -> None:
    from core.research.value_chain.valuation_context import summarize_valuation_history

    records = [
        {"trade_date": "2026-07-01", "pe_ttm": 20.0, "pb": 4.0, "ps_ttm": 5.0, "total_market_cap": 800.0},
        {"trade_date": "2026-07-02", "pe_ttm": 30.0, "pb": 6.0, "ps_ttm": 7.0, "total_market_cap": 900.0},
        {"trade_date": "2026-07-03", "pe_ttm": 40.0, "pb": 8.0, "ps_ttm": 9.0, "total_market_cap": 1_000.0},
    ]
    consensus = {
        "status": "verified",
        "periods": {
            "2027Q4": {"source_count": 5, "net_profit_yuan": {"median": 50.0}},
        },
    }

    result = summarize_valuation_history(records, structured_consensus=consensus, as_of="2026-07-03")

    assert result["status"] == "ok"
    assert result["current"]["pe_ttm"] == 40.0
    assert result["history"]["pe_ttm"]["percentile"] == 1.0
    assert result["history"]["pe_ttm"]["median"] == 30.0
    assert result["forward_multiples"][0] == {
        "period": "2027Q4", "source_count": 5, "forward_pe": 20.0,
        "model_kind": "market_cap_over_consensus_profit", "is_company_guidance": False,
    }


def test_research_assessment_quantifies_thesis_without_trade_action() -> None:
    from core.research.value_chain.research_assessment import build_research_assessment

    result = build_research_assessment(
        industry_context={"chain_status": "verified", "verified_edges": [], "ontology_edges": []},
        financial_quality={"status": "ok", "metrics": {"revenue_growth": {"value": 0.5}}},
        profit_bridge={
            "market_implied_scenarios": [{
                "period": "2027Q4", "capacity_change_required": 0.4,
                "model_kind": "market_implied_constraint_scenario",
            }],
            "forward_disclosed_indicators": [{"direction": "increase"}],
        },
        structured_consensus={"status": "verified", "periods": {}},
        valuation_context={"status": "ok", "history": {"pe_ttm": {"percentile": 0.7}}},
        technical_context={"status": "ok", "technical_state": "TREND_BROKEN"},
        degradation_reasons=["company_forward_driver_evidence_missing"],
    )

    assert result["evidence_completeness"] == 1.0
    assert result["execution_constraints"][0]["direction_consistent"] is True
    assert result["execution_constraints"][0]["magnitude_verified"] is False
    assert result["scope"] == "research_prioritization_not_trade_signal"
    assert "action" not in result
