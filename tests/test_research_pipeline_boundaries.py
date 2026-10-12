from __future__ import annotations

from pathlib import Path

import pytest


def test_quant_flow_uses_project_root_not_core_directory():
    from core.research.pipeline import quant_flow_pipeline as module

    assert module.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_quant_flow_ignores_malformed_health_report(monkeypatch, tmp_path):
    from core.research.pipeline import quant_flow_pipeline as module

    report_path = tmp_path / "data_health_latest.json"
    report_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(module, "REPORT_DIR", tmp_path)

    assert module._safe_get_lancedb_latest_from_health() == ""


def test_quant_flow_reads_lancedb_through_fast_market_query(monkeypatch):
    import polars as pl

    from core.research.pipeline import quant_flow_pipeline as module

    class QueryStub:
        def _read_table(self, table_name, **kwargs):
            assert table_name == "index_daily"
            return pl.DataFrame({"trade_date": ["2026-07-31"]})

    monkeypatch.setattr(
        "data_svc.query.fast_market_query.get_fast_market_query",
        lambda: QueryStub(),
    )

    frame = module._read_lancedb_table("index_daily")

    assert frame.height == 1
    assert frame["trade_date"].to_list() == ["2026-07-31"]


def test_quant_flow_signal_serialization_degrades_bad_numeric_fields():
    from core.research.pipeline.quant_flow_pipeline import QuantFlowPipeline

    result = QuantFlowPipeline._signal_to_dict({
        "stock_code": "000001.SZ",
        "signal_strength": "not-a-number",
        "price_at_signal": float("nan"),
    })

    assert result["stock_code"] == "000001.SZ"
    assert result["signal_strength"] == 0.0
    assert result["price_at_signal"] == 0.0


def test_gateway_pipeline_uses_project_root():
    from core.research.pipeline import gateway_ingestion_pipeline as module

    assert Path(module._PROJECT_ROOT) == Path(__file__).resolve().parents[1]


def test_gateway_evidence_pack_handles_mapping_records_and_safe_filenames(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from core.research.pipeline import gateway_ingestion_pipeline as module

    monkeypatch.setattr(module, "_PROJECT_ROOT", str(tmp_path))
    evidence = {
        "symbol": "../bad",
        "evidence_type": "report",
        "provider": {"unhashable": True},
        "quality_strength": ["bad"],
        "cross_validation_status": "rule_only",
    }

    pack = module._generate_evidence_pack(
        SimpleNamespace(evidences=[evidence]),
        "../bad",
        [],
        [],
        [],
    )
    module._write_evidence_pack("../bad", pack)

    assert pack["data_source_stats"] == {"unknown": 1}
    assert pack["report_evidence"] == [evidence]
    assert (tmp_path / "data" / "reports" / "single_stock_evidence" / ".._bad_evidence_pack.json").exists()


def test_autonomous_perilla_pipeline_uses_project_root():
    from core.research.pipeline import autonomous_perilla_pipeline as module

    assert Path(module._PROJECT_ROOT) == Path(__file__).resolve().parents[1]


def test_autonomous_perilla_pipeline_rejects_blank_topic():
    from core.research.pipeline.autonomous_perilla_pipeline import run_pipeline

    with pytest.raises(SystemExit):
        run_pipeline(["--mode", "topic", "--topic", "   "])


def test_virtual_research_desk_pipeline_uses_project_root():
    from core.research.pipeline import virtual_research_desk_pipeline as module

    assert module.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_research_data_engine_pipeline_uses_project_root():
    from core.research.pipeline import research_data_engine_pipeline as module

    assert module.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_research_data_engine_normalizes_pipeline_inputs_and_serialization():
    from core.research.pipeline.research_data_engine_pipeline import (
        ResearchDataEnginePipeline,
        _safe_symbols,
        _safe_trade_date,
    )

    assert _safe_symbols([" 000001.SZ ", None, "000001.SZ", ""]) == ["000001.SZ"]
    assert len(_safe_trade_date("not-a-date")) == 10
    assert ResearchDataEnginePipeline._make_serializable({"x": (1, {2})})["x"] == [1, [2]]


def test_research_data_engine_optional_stages_do_not_fail_whole_run(monkeypatch):
    from core.research.pipeline import research_data_engine_pipeline as module

    pipeline = object.__new__(module.ResearchDataEnginePipeline)
    pipeline._step_results = {"stale": {"status": "error"}}
    pipeline.report_dir = Path(".")
    monkeypatch.setattr(pipeline, "_step_probe_providers", lambda: {"status": "ok"})
    monkeypatch.setattr(
        pipeline,
        "_step_ingestion",
        lambda _trade_date, _symbols: {"status": "ok"},
    )
    monkeypatch.setattr(pipeline, "_step_event_bus_publish", lambda _result: {"status": "ok"})
    monkeypatch.setattr(pipeline, "_step_extraction", lambda _result: {"status": "ok"})
    monkeypatch.setattr(
        pipeline,
        "_step_evidence_update",
        lambda _ingestion, _extraction, _trade_date: {"status": "ok"},
    )
    monkeypatch.setattr(pipeline, "_step_entity_build", lambda _result: {"status": "ok"})
    monkeypatch.setattr(pipeline, "_step_graph_build", lambda _result: {"status": "ok"})
    monkeypatch.setattr(pipeline, "_step_quantflow", lambda _trade_date: {"status": "error"})
    monkeypatch.setattr(pipeline, "_step_validation", lambda _symbols, _trade_date: {"status": "ok"})
    monkeypatch.setattr(pipeline, "_step_vrd", lambda _trade_date: {"status": "error"})
    monkeypatch.setattr(
        pipeline,
        "_step_final_brief",
        lambda _results, _trade_date: {"status": "warning"},
    )
    monkeypatch.setattr(pipeline, "export_report", lambda _result: None)

    result = pipeline.run(trade_date="2026-07-31", symbols=["000001.SZ"])

    assert result["success"] is True
    assert result["steps"]["quantflow"]["status"] == "error"
    assert result["steps"]["vrd"]["status"] == "error"


def test_single_stock_pipeline_uses_project_root():
    from core.research.pipeline import single_stock_research_pipeline as module

    assert module.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_single_stock_pipeline_bounds_cli_values():
    from core.research.pipeline.single_stock_research_pipeline import (
        _safe_days,
        _safe_float,
        _safe_symbol,
    )

    assert _safe_symbol(" 000001.SZ ") == "000001.SZ"
    assert _safe_symbol(None) == ""
    assert _safe_days(-1) == 1
    assert _safe_days(float("inf")) == 365
    assert _safe_float("bad") == 0.0


def test_entity_resolver_skips_malformed_local_records(tmp_path):
    (tmp_path / "a_share_stocks.json").write_text(
        '[{"name": "好公司", "code": "600000.SH"}, null, "bad"]',
        encoding="utf-8",
    )
    (tmp_path / "non_a_share_companies.json").write_text(
        '[null, {"name": "海外公司", "aliases": ["海外"]}]',
        encoding="utf-8",
    )
    (tmp_path / "company_aliases.json").write_text("[]", encoding="utf-8")
    (tmp_path / "material_equipment_terms.json").write_text(
        '[null, {"name": "光刻胶", "category": "material"}]',
        encoding="utf-8",
    )
    (tmp_path / "generic_denylist.json").write_text(
        '["泛概念"]', encoding="utf-8"
    )

    from core.research.perilla.shared.identity.entity_resolver import EntityResolver

    resolver = EntityResolver(entity_universe_dir=tmp_path)
    assert resolver.resolve("好公司").stock_code == "600000.SH"
    assert resolver.resolve("海外").canonical_name == "海外公司"
    assert resolver.resolve("光刻胶").entity_type == "material"


def test_perilla_now_iso_is_timezone_aware_utc():
    from core.research.perilla.shared.common.temporal import now_iso

    assert now_iso().endswith("+00:00")


def test_ontology_versioning_skips_malformed_lines(tmp_path):
    path = tmp_path / "ontology_versions.jsonl"
    path.write_text(
        "not-json\n"
        '{"description": "missing version"}\n'
        '{"version": "1.0.0", "description": "valid"}\n',
        encoding="utf-8",
    )
    from core.research.perilla.shared.ontology.ontology_versioning import OntologyVersioning

    versioning = OntologyVersioning(str(path))
    assert [item.version for item in versioning.list_versions()] == ["1.0.0"]


def test_schema_proposal_from_dict_ignores_malformed_fields():
    from core.research.perilla.shared.ontology.schema_proposal import SchemaProposal

    proposal = SchemaProposal.from_dict({
        "proposed_relations": "not-a-list",
        "promotion_score": "not-a-number",
    })
    assert proposal.proposed_relations == []
    assert proposal.promotion_score == 0.0


def test_ontology_registry_degrades_invalid_top_level_data(tmp_path):
    from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

    registry = OntologyRegistry(data=[])
    assert registry.version == "1.0.0"

    path = tmp_path / "registry.json"
    path.write_text("[]", encoding="utf-8")
    loaded = OntologyRegistry.load(str(path))
    assert loaded.version == "1.0.0"


def test_ontology_registry_saves_to_current_directory(tmp_path, monkeypatch):
    from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

    monkeypatch.chdir(tmp_path)
    OntologyRegistry.default().save("registry.json")
    assert (tmp_path / "registry.json").exists()


def test_evidence_evolution_normalizes_malformed_input_files(tmp_path):
    (tmp_path / "research_hypotheses_latest.json").write_text(
        '{"not": "a-list"}', encoding="utf-8"
    )
    (tmp_path / "evidence_items_latest.json").write_text(
        '[null, {"document_id": "doc-1"}]', encoding="utf-8"
    )
    from core.research.perilla.evidence.evidence_evolution_tracker import load_inputs

    loaded = load_inputs(tmp_path)
    assert loaded["research_hypotheses"] == []
    assert loaded["evidence_items"] == [{"document_id": "doc-1"}]


def test_evidence_evolution_handles_unhashable_ids_and_numeric_boundaries():
    from core.research.perilla.evidence.evidence_evolution_tracker import (
        build_evidence_timeline,
        build_post_mortem_stub,
        update_belief_state,
    )

    hypotheses = [{
        "hypothesis_id": ["h-1"],
        "hypothesis_text": "demo",
        "evidence_document_ids": ["doc-1"],
        "source_nodes": ["node-1"],
    }]
    timeline = build_evidence_timeline(
        hypotheses,
        [{"document_id": "doc-1", "quote": "evidence"}],
        [{"source_node": "node-1", "target_node": "node-2"}],
        [{"related_node": "node-1"}],
        [{"hypothesis_id": ["h-1"], "skepticism_score": "bad"}],
    )
    assert timeline
    beliefs = update_belief_state(
        hypotheses,
        timeline,
        [{"hypothesis_id": ["h-1"], "skepticism_score": "bad"}],
        [{"symbol": "000001.SZ"}],
        use_llm=False,
    )
    assert beliefs[0]["hypothesis_id"] == "['h-1']"
    assert 0 <= beliefs[0]["posterior_probability"] <= 1
    post_mortems = build_post_mortem_stub(hypotheses, beliefs, review_horizon_days=10**100)
    assert post_mortems[0]["future_review_date"]


def test_source_registry_normalizes_malformed_entries(tmp_path):
    from core.research.perilla.evidence.source_registry import SourceEntry, SourceRegistry

    entry = SourceEntry(
        source_id=["source-1"],
        name=None,
        source_type=["manual"],
        base_url=None,
        enabled="true",
        requires_login="yes",
        tags=["tag-a", ["bad"]],
        rate_limit_seconds=float("inf"),
    )
    registry = SourceRegistry()
    registry.add_source(entry)
    assert len(registry) == 1
    assert registry.get_all_sources()[0].source_id == "['source-1']"
    assert registry.get_all_sources()[0].enabled
    assert registry.filter_by_tags([[], "tag-a"])[0] is entry
    assert len(SourceRegistry.load_from_yaml(tmp_path)) == 0
    assert len(SourceRegistry.load_from_yaml(None)) == 0


def test_raw_evidence_store_skips_bad_rows_and_writes_atomically(tmp_path):
    from core.research.perilla.evidence.evidence_schema import RawEvidenceRecord
    from core.research.perilla.evidence.raw_evidence_store import RawEvidenceStore

    persist_file = tmp_path / "raw_evidence.jsonl"
    persist_file.write_text(
        "not-json\n"
        '{"evidence_id": "e-1", "source_document_id": "d-1", '
        '"source_type": "announcement", "evidence_text": "valid"}\n',
        encoding="utf-8",
    )
    store = RawEvidenceStore(tmp_path, auto_persist=True)
    assert store.count() == 1

    store.save_many([
        None,
        RawEvidenceRecord("e-2", "d-2", "news", "second"),
    ])

    assert store.count() == 2
    assert store.get("e-2") is not None


def test_bayesian_calibrator_degrades_malformed_model_inputs(tmp_path):
    import numpy as np

    from core.research.perilla.alpha.bayesian_weight_calibrator import (
        BayesianWeightCalibrator,
        CalibrationResult,
    )

    result = CalibrationResult.from_dict({
        "feature_names": "not-a-list",
        "posteriors": [None, {"name": "quality", "posterior_mean": "bad"}],
    })
    assert result.feature_names == []
    assert len(result.posteriors) == 1
    assert result.posteriors[0].posterior_mean == 0.0

    calibrator = BayesianWeightCalibrator(model_path=tmp_path / "model.json")
    vector = calibrator._get_feature_vector(
        {"quality": float("nan"), "coverage": "bad"},
        ["quality", "coverage"],
    )
    assert vector.tolist() == [0.0, 0.0]
    assert calibrator._compute_auc_simple(
        np.array([1, 0, 1, 0]),
        np.array([0.9, 0.1, 0.8, 0.2]),
    ) == 1.0


def test_frontier_score_path_candidate_sets_probabilistic_adjusted_score():
    from types import SimpleNamespace

    from core.research.perilla.alpha.flow_path_pruner import PrunedPath
    from core.research.perilla.alpha.frontier_scorer import FrontierScorer
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate

    kb = SimpleNamespace(nodes={}, edges={}, claims={}, ontology_registry=SimpleNamespace())
    scorer = FrontierScorer(kb, use_cache=False, research_repository=SimpleNamespace(
        get_active_policy=lambda _name: None,
    ))
    candidate = PathCandidate(
        path_id="p-1",
        nodes=["root", "leaf"],
        root_entity="root",
        target_entity="leaf",
    )

    result = scorer.score_path_candidate(PrunedPath(candidate, flow_score=0.5))

    assert "event_adjusted_score" in result
    assert result["event_adjusted_score"] == 0.2
    assert result["final_score"] == 0.16


def test_sag_knowledge_base_skips_malformed_persisted_records(tmp_path):
    import json

    from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

    storage_path = tmp_path / "sag.json"
    storage_path.write_text(json.dumps({
        "nodes": [None, {"name": "valid", "source_count": "bad"}],
        "edges": [{"head": "missing"}],
        "claims": [{"claim_id": "missing"}],
        "event_edges": [None],
        "event_claims": [{"event_id": "missing"}],
    }), encoding="utf-8")

    kb = SAGKnowledgeBase(storage_path=storage_path)

    assert kb.has_node("valid")
    assert kb.node_count() == 1
    assert kb.edge_count() == 0
    assert kb.claim_count() == 0


def test_event_graph_schema_normalizes_malformed_numeric_and_collection_fields():
    from core.research.perilla.event.event_graph_schema import EventClaim, EventEdge

    claim = EventClaim.from_dict({
        "event_id": "event-1",
        "event_type": "announcement",
        "confidence": "bad",
        "linked_node_ids": "bad",
        "raw_payload": [],
    })
    edge = EventEdge.from_dict({
        "subject": "company",
        "event_type": "announcement",
        "object": "capacity",
        "confidence": "bad",
        "source_weights": [],
        "source_count": "bad",
    })

    assert claim.confidence == 0.0
    assert claim.linked_node_ids == []
    assert claim.raw_payload == {}
    assert edge.confidence == 0.0
    assert edge.source_weights == {}
    assert edge.source_count == 0

    bounded_edge = EventEdge.from_dict({
        "subject": "company",
        "event_type": "announcement",
        "object": "capacity",
        "status": [],
        "source_count": 10**100,
    })
    assert bounded_edge.status == "active"
    assert bounded_edge.source_count == 1_000_000_000


def test_dynamic_event_scoring_clamps_malformed_controls():
    import math
    from types import SimpleNamespace

    from core.research.perilla.event.dynamic_event_propagation import (
        EventAwarePPRRanker,
        EventImpactScorer,
    )
    from core.research.perilla.event.event_graph_schema import EventClaim

    kb = SimpleNamespace(
        nodes={"root": object()},
        edges={},
        has_node=lambda _name: True,
        get_neighbors=lambda *_args, **_kwargs: [],
    )
    assert EventAwarePPRRanker(kb).rank(
        "root", [], max_iter=-1, damping=float("nan"), tol=-1
    ) == {"root": 1.0}

    signal = EventImpactScorer().score_event_claim(EventClaim(
        event_id="e-1",
        event_type="other",
        confidence=float("nan"),
    ))
    assert math.isfinite(signal.impact_score)


def test_event_impact_scorer_handles_nonfinite_claim_values():
    from core.research.perilla.event.event_graph_schema import EventClaim
    from core.research.perilla.event.event_impact_scorer import EventImpactScorer

    score = EventImpactScorer(source_credibility=[]).score(EventClaim(
        event_id="event-2",
        event_type="other",
        confidence=float("nan"),
        freshness_score=float("inf"),
    ))

    assert 0.0 <= score <= 1.0


def test_temporal_event_scorer_rejects_invalid_half_life_overrides():
    from core.research.perilla.event.event_graph_schema import EventClaim
    from core.research.perilla.event.temporal_event_scorer import TemporalEventScorer

    scorer = TemporalEventScorer({"other": 0, "bad": "nope", "valid": 10})
    score = scorer.score(EventClaim(
        event_id="event-3",
        event_type="other",
        confidence=float("nan"),
        source_time="2026-01-01",
        freshness_score=float("inf"),
    ))

    assert 0.05 <= score <= 1.0


def test_cross_theme_surprise_miner_degrades_malformed_inputs():
    from types import SimpleNamespace

    from core.research.perilla.event.cross_theme_surprise_miner import CrossThemeSurpriseMiner

    resolver = SimpleNamespace(
        _material_equipment_terms={"source": None},
        resolve=lambda _name: SimpleNamespace(entity_type="material"),
    )
    miner = CrossThemeSurpriseMiner(
        entity_resolver=resolver,
        confidence_threshold=float("nan"),
        min_surprise_strength=float("inf"),
        max_candidates=-1,
    )

    assert miner.mine(None, None, synapses="bad") == []


def test_concept_signal_loader_skips_malformed_scores_and_rows(tmp_path):
    from core.research.perilla.discovery.concept_signal_loader import ConceptSignalLoader

    loader = ConceptSignalLoader(project_root=tmp_path, output_dir="reports")
    written = loader.write_discovered_concepts([None, {"concept_name": "valid", "concept_score": 0.8}])
    assert written.exists()
    assert [signal.name for signal in loader.to_concept_signals([
        {"concept_name": "valid", "concept_score": float("nan")},
        {"concept_name": "kept", "concept_score": 0.8, "related_terms": "bad"},
    ])] == ["kept"]


def test_change_monitor_degrades_non_mapping_webhooks():
    from core.research.perilla.discovery.change_monitor import ChangeMonitor

    monitor = ChangeMonitor()
    assert monitor.parse_webhook([]) is None
    document = monitor.parse_webhook({"url": 123, "diff": 456})
    assert document is not None
    assert document.url == "123"
    assert document.content_text == "456"


def test_research_inbox_skips_malformed_persisted_rows(tmp_path):
    import json

    from core.research.perilla.discovery.research_inbox import ResearchInbox

    (tmp_path / "inbox_items.json").write_text(json.dumps({
        "bad": None,
        "valid": {"item_id": "item-1", "company_name": "company"},
    }), encoding="utf-8")
    (tmp_path / "changelog.json").write_text(json.dumps([
        None,
        {"change_id": "change-1", "item_id": "item-1"},
    ]), encoding="utf-8")

    inbox = ResearchInbox(str(tmp_path))

    assert [item.item_id for item in inbox.get_items()] == ["item-1"]
    assert [change.change_id for change in inbox.get_changelog()] == ["change-1"]


def test_html_extractor_sanitizes_cache_document_ids(tmp_path):
    from core.research.perilla.discovery.html_text_extractor import (
        ExtractedContent,
        HTMLTextExtractor,
    )

    result = HTMLTextExtractor(tmp_path).save_extracted(
        ExtractedContent(main_text="safe", success=True),
        "../../outside",
    )

    assert Path(result.extracted_text_path).parent == tmp_path.resolve()
    assert (tmp_path / Path(result.extracted_text_path).name).read_text(encoding="utf-8") == "safe"


def test_html_extractor_normalizes_inputs_and_preserves_raw_html(tmp_path):
    from core.research.perilla.discovery.html_text_extractor import HTMLTextExtractor

    html = "<html><title>Example</title><body><p>Hello</p></body></html>"
    extractor = HTMLTextExtractor(tmp_path)

    result = extractor.extract_from_html(html, url=123)
    assert result.success is True
    assert result.raw_html == html
    assert "url" not in result.metadata

    saved = extractor.save_extracted(result, "doc-1")
    assert Path(saved.raw_html_path).read_text(encoding="utf-8") == html
    assert extractor.extract_from_html(None).error == "Invalid HTML input"
    assert extractor.extract_from_url(None).error == "Invalid URL provided"
    assert extractor.extract_from_url("http://[invalid").error == "Invalid URL provided"
    assert extractor.extract_from_url("ftp://example.com").error == "Unsupported URL scheme"


def test_adversarial_agents_skip_malformed_records_and_bound_scores():
    from core.research.perilla.discovery.adversarial_research_agents import (
        EvidenceReviewAgent,
        PortfolioTranslator,
        SkepticAgent,
    )

    hypotheses = [
        None,
        {
            "hypothesis_id": "hyp-1",
            "evidence_quotes": None,
            "evidence_document_ids": ["doc-1", []],
            "source_nodes": [None, 123, "GB200"],
            "crowding_score": "not-a-number",
            "evidence_score": float("nan"),
            "novelty_score": float("inf"),
        },
        "malformed",
    ]

    reviews = SkepticAgent(use_llm=False).review(hypotheses)
    scores = EvidenceReviewAgent().review(hypotheses)
    translations = PortfolioTranslator(use_llm=False).translate(
        hypotheses,
        [None, {"hypothesis_id": "hyp-1", "skepticism_score": float("inf")}],
    )

    assert reviews
    assert len(scores) == 1
    assert 0.0 <= scores[0]["skepticism_score"] <= 1.0
    assert len(translations) == 1
    assert translations[0]["watchlist_action"] in {
        "ignore", "monitor", "research_required", "watchlist_candidate",
    }


def test_autonomous_perilla_engine_normalizes_limits_and_tasks(tmp_path):
    from core.research.perilla.discovery.autonomous_perilla_engine import (
        AutonomousPerillaEngine,
    )

    engine = AutonomousPerillaEngine(
        lookback_days=-1,
        max_concepts="bad",
        max_sources_per_concept=float("nan"),
        max_candidates=-3,
        topic=123,
        output_dir=tmp_path,
        mock_web=True,
    )

    assert engine._lookback_days == 0
    assert engine._max_concepts == 5
    assert engine._max_sources_per_concept == 10
    assert engine._max_candidates == 0
    assert engine._topic is None
    assert engine._classify_pending_tasks_by_tier([None, object()]) == {
        "high": [], "medium": [], "low": [],
    }


def test_autonomous_perilla_engine_deduplicates_search_queries(tmp_path):
    from types import SimpleNamespace

    from core.research.perilla.discovery.autonomous_perilla_engine import (
        AutonomousPerillaEngine,
    )

    calls = []

    class SearchStub:
        def build_queries_for_concept(self, _name):
            return ["same query", "same query"]

        def search(self, query):
            calls.append(query)
            return []

        def dedupe_urls(self, documents):
            return documents

    engine = AutonomousPerillaEngine(output_dir=tmp_path, mock_web=True)
    engine._search = SearchStub()

    assert engine._step_search_discovery([
        SimpleNamespace(name="first"),
        SimpleNamespace(name="second"),
    ]) == []
    assert calls == ["same query"]


def test_autonomous_perilla_engine_normalizes_manual_signal_payloads(tmp_path):
    import json

    from core.research.perilla.discovery.autonomous_perilla_engine import (
        AutonomousPerillaEngine,
        _safe_filename_component,
    )

    signal_file = tmp_path / "signals.json"
    signal_file.write_text(json.dumps({
        "sources": [None, {
            "title": 123,
            "content": None,
            "source_type": None,
            "published_at": 456,
        }],
    }), encoding="utf-8")

    engine = AutonomousPerillaEngine(
        manual_signal_file=signal_file,
        output_dir=tmp_path,
        mock_web=True,
    )
    docs, early_signals = engine._load_manual_signals()

    assert any(doc.title == "123" and doc.content_text == "" for doc in docs)
    assert isinstance(early_signals, list)
    assert _safe_filename_component("../../危险/概念") == "危险_概念"


def test_autonomous_perilla_engine_skips_malformed_documents():
    from core.research.perilla.discovery.autonomous_perilla_engine import (
        AutonomousPerillaEngine,
    )
    from core.research.perilla.discovery.models import SourceDocument

    engine = AutonomousPerillaEngine(mock_web=True)
    assert engine._compute_source_stats([None, SourceDocument(source_type="news")]) == {
        "news": 1, "total": 1,
    }
    assert engine._compute_source_stats(None) == {"total": 0}
    assert engine._step_focused_crawl(None) == []


def test_hypothesis_engine_skips_malformed_inputs_and_nonfinite_scores(tmp_path):
    import json

    from core.research.perilla.discovery.hypothesis_engine import (
        _clamp01,
        generate_hypotheses,
        initialize_lifecycle,
        load_inputs,
    )

    assert _clamp01(float("nan"), default=0.25) == 0.25
    assert _clamp01(float("inf"), default=0.25) == 0.25
    assert generate_hypotheses({
        "candidate_concepts": [None, "bad"],
        "causal_chains": None,
        "missing_edges": [None],
        "concept_scores": [None],
    }, use_llm=False) == []
    assert initialize_lifecycle([None, "bad"]) == []

    (tmp_path / "candidate_concepts_latest.json").write_text("null", encoding="utf-8")
    inputs = load_inputs(tmp_path)
    assert inputs["candidate_concepts"] == []


def test_hypothesis_manager_normalizes_evidence_and_nested_inputs():
    from core.research.perilla.discovery.hypothesis_manager import HypothesisManager

    manager = HypothesisManager()
    hypothesis = manager.register_hypothesis(
        hypothesis_type="chain_node_hypothesis",
        reason=123,
        based_on_evidence_ids=None,
        missing_evidence=None,
        verification_queries=None,
    )
    assert hypothesis.reason == "123"
    assert hypothesis.based_on_evidence_ids == []
    assert hypothesis.missing_evidence == []
    assert hypothesis.verification_queries == []

    manager.verify_hypothesis(hypothesis.hypothesis_id, None)
    assert manager._infer_missing_from_six_questions(None)
    assert manager.from_chain_and_bottlenecks(None, None, None) == []


def test_industry_chain_builder_skips_malformed_graph_records(tmp_path):
    import json

    from core.research.perilla.discovery.industry_chain_graph_builder import (
        build_allowed_edges,
        build_nodes,
        detect_missing_edges,
        load_inputs,
    )

    nodes = build_nodes([None, {
        "normalized_name": 123,
        "term_type": None,
        "evidence_document_ids": None,
        "final_score": "not-a-number",
    }])
    assert nodes[0]["node_name"] == "123"
    assert nodes[0]["evidence_document_ids"] == []
    assert build_allowed_edges(None, [None], None) == []
    assert detect_missing_edges([None], None) == []

    (tmp_path / "candidate_concepts_latest.json").write_text("null", encoding="utf-8")
    assert load_inputs(tmp_path)["candidate_concepts"] == []


def test_search_task_compiler_bounds_missing_edges_and_priority(tmp_path):
    import json

    from core.research.perilla.discovery.search_task_compiler import (
        compile_search_tasks,
        load_missing_edges,
    )

    tasks = compile_search_tasks(
        [None, {
            "source_node": " GB200 ",
            "target_hint": " PCB ",
            "priority_score": float("nan"),
        }]
    )
    assert tasks
    assert all(task["priority_score"] == 0.5 for task in tasks)

    (tmp_path / "missing_edges_latest.json").write_text(
        json.dumps({"not": "a list"}), encoding="utf-8"
    )
    assert load_missing_edges(tmp_path) == []


def test_julang_report_provider_bounds_inputs_and_claim_failures(tmp_path, monkeypatch):
    from core.research.perilla.discovery.julang_report_provider import (
        JulangReportProvider,
    )

    provider = JulangReportProvider()
    assert provider.search_reports(None) == []
    assert provider.fetch_report_text(None) == ""
    assert provider._search_cache_key("query", 0)

    provider.report_dir = tmp_path
    cache_path = provider._report_cache_path(tmp_path, "bad-report")
    cache_path.write_text("cached", encoding="utf-8")
    assert provider.fetch_report_text("bad-report") == "cached"

    class Builder:
        def extract_claims_from_text(self, **_kwargs):
            return [None, {
                "head": "A", "relation": "related_to", "tail": "B",
                "evidence": "e", "source_title": "t", "timestamp": "",
                "confidence": 0.5,
            }]

    class KnowledgeBase:
        def add_claim(self, **_kwargs):
            return True

    monkeypatch.setattr(provider, "is_available", lambda: True)
    monkeypatch.setattr(provider, "search_reports", lambda _query, count=5: [
        None, {"report_id": "r1", "title": 123, "date": None},
    ])
    monkeypatch.setattr(provider, "fetch_report_text", lambda _report_id: "report text")

    assert provider.ingest_to_kb(KnowledgeBase(), "query", "root", Builder()) == 1


def test_knowledge_loop_engine_normalizes_limits_and_records(tmp_path):
    from core.research.perilla.discovery.knowledge_loop_engine import (
        PerillaKnowledgeLoopEngine,
    )

    engine = PerillaKnowledgeLoopEngine(
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_execute_tasks=-1,
        max_llm_terms=float("nan"),
        drill_down_depth=-2,
    )

    assert engine.max_execute_tasks == 0
    assert engine.max_llm_terms == 0
    assert engine.drill_down_depth == 0
    assert engine._extract_raw_terms([None, object()]) == []
    assert engine._build_candidate_concepts([None, "bad"]) == []
    assert engine._classify_tasks_by_tier(None) == {
        "high": [], "medium": [], "low": [],
    }


def test_concept_discovery_normalizes_nonfinite_configuration():
    from core.research.perilla.discovery.concept_discovery_engine import (
        ConceptDiscoveryEngine,
        _avg,
        _to_float,
    )

    engine = ConceptDiscoveryEngine(lookback_days=-1, min_score=float("nan"))

    assert engine.lookback_days == 0
    assert engine.min_score == 0.0
    assert _to_float(float("inf")) == 0.0
    assert _avg([1.0, float("nan"), "bad"]) == 1.0


def test_concept_radar_safe_float_rejects_nonfinite_values():
    from core.research.perilla.discovery.concept_radar import ConceptRadar

    assert ConceptRadar._safe_float(float("nan"), 0.25) == 0.25
    assert ConceptRadar._safe_float("inf", 0.25) == 0.25


def test_market_resonance_degrades_malformed_inputs():
    from core.research.perilla.discovery.market_resonance_engine import (
        compute_market_resonance,
        generate_trigger_signals,
    )

    assert compute_market_resonance(
        [None, {"hypothesis_id": "h-1", "source_nodes": "bad"}],
        [None, {"hypothesis_id": "h-1"}],
        [None, {"name": "move"}],
    )[0]["matched_market_items"] == []
    signal = generate_trigger_signals(
        [None, {"hypothesis_id": "h-1"}],
        [None, {"hypothesis_id": "h-1", "posterior_probability": "bad"}],
        [None, {"hypothesis_id": "h-1", "market_attention_score": float("nan")}],
    )[0]
    assert signal["trigger_type"] in {"evidence_upgrade", "needs_manual_review"}


def test_source_registry_normalizes_malformed_entry_fields():
    from core.research.perilla.evidence.source_registry import SourceEntry, SourceRegistry

    registry = SourceRegistry()
    registry.load_from_dict({
        "sources": [
            None,
            {"source_id": "source-1", "tags": "official", "rate_limit_seconds": "bad"},
        ],
    })
    entry = registry.get_all_sources()[0]

    assert entry.tags == ["official"]
    assert entry.rate_limit_seconds == 60
    assert SourceEntry("source-2", "", "manual", "", tags=["official"]).source_id == "source-2"


def test_outcome_tracker_bounds_inputs_and_skips_malformed_json(tmp_path):
    import json
    import sqlite3

    from core.research.perilla.alpha.outcome_tracker import OutcomeTracker

    tracker = OutcomeTracker(tmp_path / "outcomes.db")
    record_id = tracker.record_promotion(
        "path-1", "root", "target", None, None, float("nan"), metadata=[]
    )
    assert tracker.get_labeled_outcomes(limit=-1) == []

    with sqlite3.connect(tmp_path / "outcomes.db") as conn:
        conn.execute(
            "UPDATE alpha_outcomes SET path_json=?, features_json=?, metadata_json=?, outcome_label=? WHERE record_id=?",
            ("[]", json.dumps([]), json.dumps([]), "success", record_id),
        )
        conn.commit()

    assert tracker.get_labeled_outcomes(limit=10) == []
    assert tracker.get_pending_resolutions(days_elapsed="bad") == []


def test_weight_registry_does_not_mutate_defaults_or_expose_invalid_values(tmp_path):
    from core.research.perilla.alpha.weight_registry import WeightRegistry

    config = tmp_path / "weights.yaml"
    config.write_text(
        "claim_scoring:\n  positive_weights:\n    source_weight: 0.1\n"
        "source_weights:\n  default: bad\n",
        encoding="utf-8",
    )

    custom = WeightRegistry(config)
    assert custom.get_claim_weights()["source_weight"] == 0.1
    assert custom.get_source_weight("unknown") == 0.5
    custom.get_claim_weights()["source_weight"] = 0.9

    fallback = WeightRegistry()
    assert fallback.get_claim_weights()["source_weight"] == 0.3


def test_graph_cache_bounds_sizes_and_recovers_malformed_wal(tmp_path):
    from core.research.perilla.alpha.graph_cache import (
        AdjacencyIndex,
        NodeDepthCache,
        NodeScoreCache,
        WALWriter,
    )

    depth = NodeDepthCache(-1)
    depth.set("root", "node", 1)
    assert depth.stats()["size"] == 0

    scores = NodeScoreCache(ttl_seconds=-1, max_size=1)
    scores.set("root", "node", {"score": 1})
    cached = scores.get("root", "node")
    assert cached is None
    scores = NodeScoreCache(ttl_seconds=300, max_size=1)
    scores.set("root", "node", {"score": 1})
    scores.get("root", "node")["score"] = 0
    assert scores.get("root", "node")["score"] == 1

    index = AdjacencyIndex()
    index.rebuild({("head", "rel", "tail"): {}, ("bad",): {}})
    assert index.stats()["indexed_edges"] == 1

    wal = WALWriter(tmp_path / "state.json")
    wal._wal_path.write_text(
        "not-json\n"
        '{"op": "add_claim", "data": null}\n'
        '{"op": "add_claim", "data": {"claim": {"claim_id": "c-1"}}}\n',
        encoding="utf-8",
    )
    recovered = wal.recover({"nodes": [None], "edges": [], "claims": []})
    assert [claim["claim_id"] for claim in recovered["claims"]] == ["c-1"]


def test_probabilistic_aggregator_degrades_nonfinite_inputs():
    import math
    import numpy as np

    from core.research.perilla.alpha.probabilistic_aggregator import (
        IsotonicCalibrator,
        apply_multiplicative_penalties,
        apply_vetoes,
        compute_temporal_weight,
        hawkes_excitation,
        noisy_or_aggregate,
        score_node_probabilistic,
    )

    assert math.isfinite(noisy_or_aggregate(None, None, prior=float("nan")))
    assert apply_multiplicative_penalties(float("nan"), None, None) == 0.0
    assert apply_vetoes(0.5, {"text": "false"}) == 0.5
    assert compute_temporal_weight(float("nan"), 10) == 1.0
    assert math.isfinite(hawkes_excitation([1.0, float("nan")], float("nan")))

    calibrator = IsotonicCalibrator().fit(np.array([float("nan"), 0.5]), np.array([1.0, 0.0]))
    assert math.isfinite(float(calibrator.transform(0.5)))
    mismatched = IsotonicCalibrator().fit(np.array([0.1, 0.2]), np.array([1.0]))
    assert mismatched._fitted is False
    restored = IsotonicCalibrator.from_dict({"fitted": True, "thresholds": "bad"})
    assert restored.transform(float("nan")) == 0.0

    result = score_node_probabilistic(None, None, None, None, None, prior=float("inf"))
    assert 0.0 <= result["final_score"] <= 1.0


def test_temporal_kernel_degrades_malformed_events_and_configuration():
    import math
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from core.research.perilla.alpha.temporal_kernel import EventImpact, TemporalKernel

    registry = SimpleNamespace(get_temporal_config=lambda: {
        "half_lives": {"bad": {"half_life_days": "nope"}},
        "hawkes": {
            "self_excitation_alpha": float("nan"),
            "decay_beta": "bad",
            "max_excitation": -1,
        },
    })
    kernel = TemporalKernel(registry)

    assert kernel.get_half_life("bad") == 30.0
    assert kernel.temporal_weight("not-a-date") == 0.1
    scores = kernel.compute_aggregate_temporal_score([
        None,
        EventImpact("news", "not-a-date", float("nan")),
    ])
    assert scores["event_count"] == 0
    assert all(math.isfinite(float(value)) for value in scores.values())
    overlay = kernel.compute_event_overlay_for_path(None)
    assert overlay["event_boost_score"] == 0.0
    assert math.isfinite(kernel.temporal_weight(
        "2026-01-01T00:00:00+08:00",
        reference_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
    ))


def test_path_discovery_schema_serializes_malformed_runtime_fields():
    from core.research.perilla.alpha.path_discovery_schema import (
        AlphaPathResult,
        PathCandidate,
        PathEvidenceBundle,
        PathQualityReport,
        PathVerificationResult,
        PromotionDecision,
    )

    candidate = PathCandidate(
        path_id=None,
        nodes=None,
        edges=[("a", "rel", "b"), None],
        base_score=float("nan"),
    )
    assert candidate.to_dict()["nodes"] == []
    assert candidate.to_dict()["edges"] == [["a", "rel", "b"]]
    bundle = PathEvidenceBundle(
        path_candidate=None,
        stable_claims=[None],
        event_claims=[None],
        evidence_chunks=[None],
    )
    assert bundle.to_dict()["stable_claims"] == []
    assert PromotionDecision(path_id=None).to_dict()["path_id"] == ""
    assert PathVerificationResult(path_id=None, validity_score=float("inf")).to_dict()["validity_score"] == 0.0
    assert PathQualityReport(path_id=None, final_quality_score=float("nan")).to_dict()["final_quality_score"] == 0.0
    assert AlphaPathResult(path_id=None, final_score=float("inf")).to_dict()["final_score"] == 0.0


def test_graph_schema_bounds_claim_scores_and_axis_inputs():
    import math

    from core.research.perilla.alpha.graph_schema import (
        ClaimScoreComponents,
        apply_source_floor,
        get_source_weight,
        infer_axis,
    )

    assert math.isfinite(get_source_weight(None))
    assert apply_source_floor(float("nan"), float("nan")) == 0.0
    assert apply_source_floor(float("inf"), 1.0) == 0.6
    assert 0.0 <= ClaimScoreComponents(
        source_weight="bad",
        extractor_confidence=float("nan"),
        evidence_clarity=float("inf"),
    ).compute(use_registry=False) <= 1.0
    assert infer_axis(None) == {"material_axis"}


def test_dual_retriever_bounds_query_limits_and_rank_inputs():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.dual_retriever import (
        EvidenceChunkRetriever,
        EventSignalRetriever,
        PathEvidenceBundle,
        RetrievalFusionRanker,
        StablePathRetriever,
    )

    resolver = SimpleNamespace(resolve=lambda _value: None)
    retriever = StablePathRetriever(SimpleNamespace(), resolver)
    assert retriever.link_entities(None) == []
    assert retriever.retrieve(None, max_depth=-1, max_paths=-1) == []
    assert retriever.retrieve("alpha", max_depth=float("inf"), max_paths=float("inf"))

    graph = SimpleNamespace(
        nodes={},
        get_neighbors=lambda _name, direction="out": [
            SimpleNamespace(head="a", tail="b", confidence=0.5),
            SimpleNamespace(head="a", tail="c", confidence=0.5),
        ] if direction == "out" else [],
    )
    paths = StablePathRetriever(graph, resolver).retrieve("", root_entity="a", max_depth=1)
    assert {item["path_id"] for item in paths} == {"a->b", "a->c"}

    assert EventSignalRetriever(SimpleNamespace(event_edges=[])).retrieve_for_nodes(["a"]) == []

    evidence = EvidenceChunkRetriever(SimpleNamespace(claims={
        "c1": {"claim_id": "c1", "evidence": "alpha evidence", "source_type": "公告"},
    })).retrieve(
        [{"claim_ids": ["c1"], "head": "a", "relation": "supports", "tail": "b"}],
        [],
    )
    assert evidence[0]["claim_id"] == "c1"

    bundle = PathEvidenceBundle(
        path=None,
        path_id=None,
        evidence_chunks=[None],
        stable_path_score=float("nan"),
        event_edges=[None],
        event_claims=[None],
    )
    ranked = RetrievalFusionRanker().rank([None, bundle])
    assert ranked == [bundle]
    assert math.isfinite(bundle.fused_score)
    assert bundle.to_dict()["path"] == []

    malformed_score = PathEvidenceBundle(path=[], path_id="bad", stable_path_score="bad")
    assert RetrievalFusionRanker().rank([malformed_score]) == [malformed_score]
    assert "stable=0.00" in malformed_score.reason


def test_llm_path_verifier_degrades_malformed_prompts_and_results():
    import math

    from core.research.perilla.alpha.llm_path_verifier import (
        LLMPathVerifier,
        VerificationPromptBuilder,
        VerificationResultParser,
    )
    from core.research.perilla.alpha.path_discovery_schema import PathEvidenceBundle

    bundle = PathEvidenceBundle(
        path_candidate=None,
        evidence_chunks=[None],
        missing_evidence=[None],
        contradiction_signals=[None],
        coverage_score=float("nan"),
    )
    system_prompt, user_prompt = VerificationPromptBuilder().build(bundle, query=None)
    assert system_prompt and user_prompt
    failed = VerificationResultParser().parse("[]", None)
    assert failed.is_valid is False
    result = LLMPathVerifier().verify(bundle, query=None)
    assert math.isfinite(result.validity_score)


def test_llm_path_verifier_normalizes_malformed_edge_keys():
    from core.research.perilla.alpha.llm_path_verifier import LLMPathVerifier
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate, PathEvidenceBundle

    candidate = PathCandidate(path_id="p", edges=[("a", "r", ["nested"])])
    bundle = PathEvidenceBundle(
        path_candidate=candidate,
        coverage_score=float("inf"),
        evidence_chunks=[{"edge_key": ["a", "r", ["nested"]], "confidence": float("nan")}],
    )
    result = LLMPathVerifier().verify(bundle)
    assert result.unsupported_edges == []
    assert result.weak_edges == [("a", "r", "['nested']")]


def test_flow_path_pruner_bounds_malformed_candidates_and_limits():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.flow_path_pruner import (
        FlowPathPruner,
        PathDiversitySelector,
        PathFlowScorer,
        PrunedPath,
    )
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate

    kb = SimpleNamespace(edges={}, claims={})
    scorer = PathFlowScorer(kb)
    assert scorer.score(None, query=None, axis_weights=None)[0] == 0.0
    assert math.isfinite(PrunedPath(None, flow_score=float("nan")).to_dict()["flow_score"])
    selector = PathDiversitySelector(kb, min_axes=-1, max_same_path_type=-1, max_same_target=-1)
    assert selector.select(None, top_k=-1) == []
    assert FlowPathPruner(kb, min_flow_score=float("nan")).prune(None, query=None, top_k=-1) == []
    candidate = PathCandidate(path_id="p", nodes=["a"], axes=["material_axis"])
    assert PathDiversitySelector(kb).select([PrunedPath(candidate, flow_score=0.5)], top_k=1)

    malformed = PathCandidate(
        path_id="malformed",
        nodes=None,
        edges=[["unhashable"]],
        axes=[{}],
        hop_count="2",
    )
    flow_score, components = scorer.score(malformed, query=None, axis_weights=None)
    assert math.isfinite(flow_score)
    assert components["length_penalty"] == 1.0
    assert PathDiversitySelector(kb).select(
        [PrunedPath(malformed, flow_score=0.5)], top_k=1
    )
    assert FlowPathPruner._path_signature([1, None, "a"])


def test_claim_quality_scorer_degrades_malformed_claims_and_graphs():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.claim_quality_scorer import ClaimQualityScorer

    claim = SimpleNamespace(
        evidence=None,
        head=None,
        tail=None,
        relation=None,
        source_type=None,
        timestamp=None,
        confidence=float("nan"),
        extractor_type=None,
    )
    scorer = ClaimQualityScorer()
    assert scorer.evidence_clarity(claim) == 0.0
    assert scorer.relation_clarity(claim) == 0.0
    assert scorer.text_quality_score(None) == 0.0
    assert math.isfinite(scorer.recency_score(None))
    components = scorer.compute_components(claim)
    assert 0.0 <= components.compute(use_registry=False) <= 1.0
    graph = SimpleNamespace(nodes={}, claims={}, get_neighbors=lambda *_args, **_kwargs: None)
    assert scorer.noise_penalty(claim, graph) == 0.0


def test_claim_quality_scorer_handles_mapping_claims_and_graphs():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.claim_quality_scorer import ClaimQualityScorer

    claim = {
        "evidence": "root 使用材料 leaf，2025年数据 10%",
        "head": "root",
        "tail": "leaf",
        "relation": "made_of",
        "source_type": "公告",
        "confidence": float("inf"),
        "extractor_type": "rule",
        "timestamp": "bad",
    }
    edge = {"tail": "leaf", "relation": "made_of", "claim_ids": ["c1"]}
    kb = SimpleNamespace(
        nodes={"leaf": {"node_type": "material", "metadata": {"resolver_confidence": "bad"}}},
        claims={"c1": claim},
        get_neighbors=lambda _name, direction="out": [edge] if direction == "out" else [],
    )
    scorer = ClaimQualityScorer(kb)
    assert math.isfinite(scorer.evidence_clarity(claim))
    assert math.isfinite(scorer.relation_clarity(claim))
    assert math.isfinite(scorer.noise_penalty(claim))
    assert math.isfinite(scorer.compute_components(claim).compute(use_registry=False))


def test_structure_navigator_bounds_graph_inputs_and_path_limits():
    from types import SimpleNamespace

    from core.research.perilla.alpha.structure_navigator import StructureNavigator

    kb = SimpleNamespace(nodes={}, get_neighbors=lambda *_args, **_kwargs: None)
    navigator = StructureNavigator(kb)
    assert navigator.is_sparse(None) is True
    assert navigator.classify_axes(None)
    assert navigator.generate_path_candidates(None, max_depth=-1, max_paths=-1) == []


def test_structure_navigator_handles_mapping_nodes_and_edges():
    from types import SimpleNamespace

    from core.research.perilla.alpha.structure_navigator import StructureNavigator

    edge = {
        "head": "a",
        "relation": "supports",
        "tail": "b",
        "confidence": "bad",
        "source_weights": {"公告": float("inf")},
    }
    kb = SimpleNamespace(
        nodes={
            "a": {"axes": ["material_axis", None], "node_type": "process"},
            "b": {"axes": None, "node_type": "equipment"},
        },
        get_neighbors=lambda name, direction="out": [edge] if name == "a" and direction == "out" else [],
    )
    navigator = StructureNavigator(kb)
    assert navigator.classify_axes("a") == ["material_axis"]
    assert navigator.is_sparse("a") is True
    candidates = navigator.generate_path_candidates(
        "a", max_depth=float("inf"), max_paths=float("inf")
    )
    assert candidates and candidates[0].nodes == ["a", "b"]
    assert StructureNavigator._candidate_path_id([1, None, "a"])


def test_path_confidence_propagator_bounds_numeric_overlays():
    import math

    from core.research.perilla.alpha.path_confidence_propagator import (
        PathConfidencePropagator,
        PathRecord,
    )

    result = PathConfidencePropagator.compute_path_confidence(
        [float("nan"), "bad", 0.8],
        has_conflict="yes",
        has_gap=1,
        event_overlay={
            "event_boost_score": float("inf"),
            "event_risk_score": "bad",
            "freshness_score": float("nan"),
            "contradiction_pressure": "bad",
        },
    )
    assert 0.0 <= result <= 1.0
    record = PathRecord(path_id=None, path=None, final_confidence=float("nan"))
    assert math.isfinite(record.to_dict()["final_confidence"])


def test_path_confidence_propagator_degrades_malformed_path_records():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.path_confidence_propagator import PathConfidencePropagator
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate, PathEvidenceBundle

    edge = SimpleNamespace(confidence=float("inf"), conflict_status="none")
    kb = SimpleNamespace(edges={("a", "r", "b"): edge}, claims={})
    candidate = PathCandidate(
        path_id="p",
        nodes=["a", "b"],
        edges=[("a", "r", "b")],
        hop_count="bad",
    )
    bundle = PathEvidenceBundle(
        path_candidate=candidate,
        stable_claims=[{"claim_score": float("nan"), "confidence": "bad"}],
        contradiction_signals=None,
    )
    record = PathConfidencePropagator(kb).propagate_path(
        candidate,
        bundle=bundle,
        event_overlay={"event_boost_score": float("inf"), "event_risk_score": "bad"},
    )
    assert math.isfinite(record.final_confidence)
    assert all(math.isfinite(item["value"]) for item in record.to_dict()["propagation_trace"])


def test_frontier_scorer_bounds_malformed_path_inputs():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.flow_path_pruner import PrunedPath
    from core.research.perilla.alpha.frontier_scorer import FrontierScorer
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate

    scorer = FrontierScorer(
        SimpleNamespace(nodes={}, edges={}, claims={}, ontology_registry=SimpleNamespace()),
        use_cache=False,
        research_repository=SimpleNamespace(get_active_policy=lambda _name: None),
    )
    assert scorer.score_path(None, None, {
        "event_boost_score": float("nan"), "event_risk_score": "bad"
    })["path_alpha_score"] == 0.0
    assert scorer.score_path_candidate(None)["final_score"] == 0.0
    result = scorer.score_path_candidate(PrunedPath(
        PathCandidate(path_id="p", nodes=["root", "leaf"], edges=None),
        flow_score=float("nan"),
        component_scores=None,
    ))
    assert math.isfinite(result["final_score"])


def test_frontier_scorer_handles_mapping_graphs_and_runtime_boundaries():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.frontier_scorer import FrontierScorer

    def _raise(_root):
        raise ValueError("bad event payload")

    resolver = SimpleNamespace(
        resolve=lambda _name: None,
        is_company_position_valid=lambda *_args: True,
    )
    edge = {
        "head": "root",
        "tail": "leaf",
        "relation": "supports",
        "confidence": "bad",
        "source_weights": {"公告": float("inf")},
        "claim_ids": [["unhashable"]],
    }
    kb = SimpleNamespace(
        nodes={"root": {"node_type": "material", "source_count": "bad", "axes": [None, {}]}},
        edges={("root", "supports", "leaf"): edge},
        claims={},
        ontology_registry=SimpleNamespace(is_valid_node_type=lambda _value: True),
        get_neighbors=lambda _name, direction="out": [edge] if direction == "out" else [],
    )
    scorer = FrontierScorer(
        kb,
        entity_resolver=resolver,
        event_engine=SimpleNamespace(propagate_for_root=_raise),
        use_cache=False,
        research_repository=SimpleNamespace(get_active_policy=lambda _name: None),
    )

    node_result = scorer.score_node("root", "root")
    assert math.isfinite(node_result["total"])
    paths = scorer.enumerate_paths("root", max_depth=float("inf"), max_paths=float("inf"))
    assert paths
    assert all(math.isfinite(item["path_alpha_score"]) for item in paths)
    adjusted = scorer.apply_temporal_decay(None, event_time=float("inf"))
    assert 0.0 <= adjusted["freshness_score"] <= 1.0
    assert scorer.rank_pruned_paths([None, None])


def test_path_quality_evaluator_bounds_malformed_payloads():
    import math

    from core.research.perilla.alpha.path_discovery_schema import (
        PathCandidate,
        PathEvidenceBundle,
        PathVerificationResult,
    )
    from core.research.perilla.alpha.path_quality_evaluator import PathQualityEvaluator

    candidate = PathCandidate(
        path_id="p",
        nodes=None,
        edges=None,
        relations=None,
        axes=None,
        node_types=None,
        hop_count="bad",
    )
    report = PathQualityEvaluator().evaluate_path_candidate(
        candidate,
        bundle=PathEvidenceBundle(
            path_candidate=candidate,
            evidence_chunks=None,
            source_summary=None,
            contradiction_signals=None,
            coverage_score=float("nan"),
        ),
        verification=PathVerificationResult(
            path_id="p",
            validity_score="bad",
            evidence_strength=float("nan"),
            supported_edges=None,
            weak_edges=None,
            unsupported_edges=None,
            contradictions=None,
        ),
        event_overlay={"freshness_score": "bad"},
    )
    assert 0.0 <= report.final_quality_score <= 1.0
    assert all(math.isfinite(value) for value in (
        report.structural_score,
        report.evidence_score,
        report.source_score,
        report.consistency_score,
        report.freshness_score,
        report.bottleneck_score,
        report.diversity_score,
    ))


def test_path_quality_evaluator_handles_mapping_graphs_and_limits():
    import math
    from types import SimpleNamespace

    from core.research.perilla.alpha.path_quality_evaluator import (
        PathQualityEvaluator,
        PathQualityMetrics,
    )
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate

    resolver = SimpleNamespace(
        resolve=lambda _name: SimpleNamespace(entity_type="generic_term", resolver_confidence="bad"),
        is_company=lambda _name: False,
        is_company_position_valid=lambda *_args: True,
    )
    edge = {
        "head": "root",
        "relation": "supports",
        "tail": "leaf",
        "claim_ids": ["c1"],
        "conflict_status": "none",
    }
    kb = SimpleNamespace(
        nodes={
            "root": {"node_type": "generic", "axes": [{"bad": True}], "metadata": None},
            "leaf": {"node_type": "material", "axes": ["material_axis"], "metadata": {"noise_score": float("inf")}},
        },
        edges={("root", "supports", "leaf"): edge},
        claims={"c1": {"source_type": "公告", "status": "trusted"}},
        get_neighbors=lambda name, direction="out": [edge] if name == "root" and direction == "out" else [],
    )
    evaluator = PathQualityEvaluator(entity_resolver=resolver)
    candidate = PathCandidate(
        path_id="p",
        nodes=["root", "leaf"],
        axes=[{"bad": True}],
        hop_count=float("inf"),
    )
    report = evaluator.evaluate_path_candidate(candidate)
    assert math.isfinite(report.final_quality_score)
    legacy = evaluator.evaluate_path(kb, ["root", "leaf"])
    assert math.isfinite(legacy["noise_node_ratio"])
    metrics = evaluator.evaluate_root(
        kb,
        "root",
        paths=[{"path": ["root", "leaf"]}],
        max_depth=float("inf"),
        max_paths=float("inf"),
    )
    assert metrics.path_count == 1
    metrics.unresolved_conflict_count = float("inf")
    assert math.isfinite(metrics.to_dict()["unresolved_conflict_count"])
    assert PathQualityMetrics().to_dict()["path_count"] == 0


def test_report_graph_builder_skips_malformed_documents(monkeypatch):
    from core.research.perilla.alpha.report_graph_builder import ReportGraphBuilder

    builder = ReportGraphBuilder(llm_fallback=False)
    monkeypatch.setattr(builder, "_llm_available", lambda: False)
    assert builder._clean_llm_json(None) == ""
    assert builder.extract_claims_from_text(None, None, None) == []
    assert builder.ingest_text_documents([None, "bad", {}, {"text": None}]) == 0


def test_report_graph_builder_degrades_when_entity_resolver_misses():
    from types import SimpleNamespace

    from core.research.perilla.alpha.report_graph_builder import ReportGraphBuilder

    builder = ReportGraphBuilder(llm_fallback=False)
    builder.entity_resolver = SimpleNamespace(resolve=lambda _name: None)

    assert builder._infer_node_type("光芯片", "made_of", "tail") == "material"


def test_claim_promotion_engine_rejects_malformed_path_evidence():
    from types import SimpleNamespace

    from core.research.perilla.alpha.claim_promotion_engine import ClaimPromotionEngine
    from core.research.perilla.alpha.path_discovery_schema import (
        PathCandidate,
        PathEvidenceBundle,
        PathQualityReport,
        PathVerificationResult,
    )
    from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry

    engine = ClaimPromotionEngine.__new__(ClaimPromotionEngine)
    engine.ontology_registry = OntologyRegistry.default()
    engine.kb = SimpleNamespace()
    result = engine.promote_from_path_evidence(
        PathEvidenceBundle(
            path_candidate=PathCandidate(path_id="p", edges=None, node_types=None),
            stable_claims=None,
            coverage_score=float("nan"),
            contradiction_signals=None,
        ),
        PathVerificationResult(
            path_id="p",
            evidence_strength="bad",
            supported_edges=None,
            weak_edges=None,
            unsupported_edges=None,
            contradictions=None,
        ),
        PathQualityReport(
            path_id="p",
            quality_label="PROMOTION_READY_PATH",
            consistency_score=0.5,
            final_quality_score=float("nan"),
        ),
    )
    assert result.promotable_claims == []
    assert "below threshold" in result.evidence_check_result["reason"]


def test_claim_promotion_engine_degrades_invalid_event_and_edge_inputs():
    from core.research.perilla.alpha.claim_promotion_engine import ClaimPromotionEngine

    engine = ClaimPromotionEngine.__new__(ClaimPromotionEngine)
    assert engine.process_promotion_candidates([None]) == [{
        "event_claim_id": "",
        "promoted": False,
        "stable_claim_id": None,
        "reason": "invalid event claim",
    }]
    assert engine.update_edge_and_paths(None) == {
        "edge_key": None,
        "confidence": 0.0,
        "affected_path_ids": [],
    }


def test_contradiction_detector_skips_malformed_path_payloads():
    from types import SimpleNamespace

    from core.research.perilla.alpha.contradiction_detector import ContradictionDetector
    from core.research.perilla.alpha.path_discovery_schema import PathCandidate, PathEvidenceBundle

    detector = ContradictionDetector.__new__(ContradictionDetector)
    detector.kb = SimpleNamespace(edges={}, claims={}, event_claims={}, event_edges={})
    detector.entity_resolver = SimpleNamespace()
    assert detector.detect_for_claim(None) == []
    assert detector.detect_path_contradictions(PathEvidenceBundle(
        path_candidate=PathCandidate(path_id="p", edges=None),
        event_claims=None,
        evidence_chunks=None,
    )) == []
    assert detector.detect_ontology_violation(PathCandidate(path_id="p", edges=None)) is None


def test_contradiction_detector_handles_mapping_edges_and_resolver_misses():
    from types import SimpleNamespace

    from core.research.perilla.alpha.contradiction_detector import ContradictionDetector
    from core.research.perilla.alpha.sag_knowledge_base import Claim

    def make_claim(claim_id, tail):
        return Claim(
            claim_id=claim_id,
            head="product",
            relation="supplied_by",
            tail=tail,
            evidence="official evidence",
            source_type="official_announcement",
            source_title="source",
            timestamp="2025-01-01",
            confidence=0.9,
        )

    first = make_claim("c1", "supplier-a")
    second = make_claim("c2", "supplier-b")
    detector = ContradictionDetector.__new__(ContradictionDetector)
    detector.kb = SimpleNamespace(
        edges={("product", "supplied_by", "supplier-a"): {"claim_ids": ["c1", "c2"]}},
        claims={"c1": first, "c2": second},
    )
    detector.entity_resolver = SimpleNamespace(resolve=lambda _value: None)

    assert detector._candidate_others(first) == [second]
    assert detector._is_entity_conflict(first, second) is False


def test_contradiction_detector_resolves_group_without_score_shadowing():
    from types import SimpleNamespace

    from core.research.perilla.alpha.contradiction_detector import ContradictionDetector
    from core.research.perilla.alpha.sag_knowledge_base import Claim

    def make_claim(claim_id, score):
        return Claim(
            claim_id=claim_id,
            head="a",
            relation="contains",
            tail="b",
            evidence="evidence",
            source_type="official_announcement",
            source_title="source",
            timestamp="2025-01-01",
            confidence=0.9,
            claim_score=score,
            contradiction_group_id="group-1",
        )

    winner = make_claim("winner", 0.9)
    loser = make_claim("loser", 0.1)

    def update_claim_status(*, claim_id, new_status, **_kwargs):
        {"winner": winner, "loser": loser}[claim_id].status = new_status

    detector = ContradictionDetector.__new__(ContradictionDetector)
    detector.kb = SimpleNamespace(
        claims={"winner": winner, "loser": loser},
        update_claim_status=update_claim_status,
    )

    result = detector.resolve_conflict_group("group-1")

    assert result["resolved"] is True
    assert result["winner_claim_id"] == "winner"
    assert result["changed_claim_ids"] == ["loser"]


def test_alpha_discovery_engine_bounds_path_helpers():
    from types import SimpleNamespace

    from core.research.perilla.alpha.alpha_discovery_engine import AlphaDiscoveryEngine

    engine = AlphaDiscoveryEngine.__new__(AlphaDiscoveryEngine)
    engine.entity_resolver = SimpleNamespace(is_company=lambda _value: False)
    assert engine._extract_evidence_gaps(None) == []
    assert engine._identify_axes([None, {"node_scores": [None, {"axes": None}]}]) == []
    result = engine._extract_evidence_gaps({
        "path": ["root", "leaf"],
        "path_confidence": float("nan"),
        "path_alpha_score": "bad",
        "evidence_gaps": [None, {"head": "root", "tail": "leaf", "missing": None}],
    })
    assert len(result) == 1
    assert 0.0 <= result[0].priority <= 1.0
    assert engine._map_event_overlays([None], None) == {}


def test_alpha_discovery_engine_handles_mapping_edges_and_bounded_config():
    from types import SimpleNamespace

    from core.research.perilla.alpha.alpha_discovery_engine import AlphaDiscoveryEngine

    engine = AlphaDiscoveryEngine.__new__(AlphaDiscoveryEngine)
    engine.kb = SimpleNamespace(
        claims={"c1": SimpleNamespace(status="trusted")},
        edges={
            ("a", "contains", "b"): {
                "claim_ids": ["c1"],
                "conflict_status": "none",
            },
            ("a", "contains", "c"): {
                "claim_ids": [],
                "conflict_status": "conflicted",
            },
        },
    )

    assert engine._get_trusted_edges() == [{
        "claim_ids": ["c1"],
        "conflict_status": "none",
    }]
    assert engine._get_conflicted_edges() == [{
        "claim_ids": [],
        "conflict_status": "conflicted",
    }]

    from core.research.perilla.alpha.alpha_discovery_engine import _bounded_int

    assert _bounded_int(float("inf"), 10, 1000) == 10
    assert _bounded_int(10**100, 10, 1000) == 1000


def test_alpha_gap_feedback_emitter_bounds_malformed_gaps():
    from core.research.perilla.bridge.alpha_gap_feedback_emitter import AlphaGapFeedbackEmitter

    emitter = AlphaGapFeedbackEmitter()
    assert emitter.emit_from_path_gap(None) == []
    assert emitter.emit_from_evidence_gap({"path": None}) is None
    task = emitter.emit_from_evidence_gap({
        "gap_id": "g1",
        "path": ["root", "leaf"],
        "missing_entity": "leaf",
        "priority": float("nan"),
    })
    assert task is not None
    assert task.priority == 0.5


def test_promotion_candidate_builder_bounds_malformed_event_edges():
    from types import SimpleNamespace

    from core.research.perilla.bridge.promotion_candidate_builder import PromotionCandidateBuilder

    builder = PromotionCandidateBuilder.__new__(PromotionCandidateBuilder)
    builder.kb = SimpleNamespace(
        event_edges={
            "edge": {
                "confidence": "bad",
                "source_count": "bad",
            },
        },
    )
    builder.min_edge_confidence = 0.55
    builder.min_source_count = 2
    assert builder.build_candidates(max_candidates=None) == []


def test_perilla_sag_bridge_degrades_malformed_alpha_results():
    from core.research.perilla.bridge.perilla_sag_bridge import PerillaSAGBridge

    bridge = PerillaSAGBridge.__new__(PerillaSAGBridge)
    bridge.pending_search_tasks = []
    bridge.gap_emitter = None
    assert bridge.emit_search_tasks_from_alpha_result(None) == []
    assert bridge.get_pending_search_tasks() == []


def test_evidence_grader_bounds_helper_inputs():
    from core.research.perilla.evidence.evidence_grader import EvidenceGrader

    grader = EvidenceGrader()
    assert grader._score_source_count(None) == 0.0
    assert grader._score_market_relevance("a_share") == 1.0
    assert grader._score_evidence_recency([None, "bad-date"]) == 0.3


def test_evidence_binder_degrades_malformed_records():
    from core.research.perilla.evidence.evidence_binder import EvidenceBinder

    binder = EvidenceBinder(entity_aliases=[])
    assert binder.resolve_entity(None) == ("", 0.0)
    assert binder.match_relation(None) == ("UNKNOWN", 0.0)
    assert binder.bind_to_edge(None, None, None, None)["binding_confidence"] == 0.0


def test_evidence_probe_degrades_malformed_path_payloads():
    from types import SimpleNamespace

    from core.research.perilla.evidence.evidence_probe import EvidenceProbe

    probe = EvidenceProbe.__new__(EvidenceProbe)
    probe.kb = SimpleNamespace(edges={}, claims={}, event_claims={})
    probe.entity_resolver = SimpleNamespace(
        is_generic_term=lambda _value: False,
        is_company_position_valid=lambda *_args: True,
    )
    probe.graph_builder = SimpleNamespace()

    assert probe._search_queries(None)
    bundle = probe.bind_path_evidence(None)
    assert bundle.coverage_score == 0.0


def test_bayesian_calibrator_bounds_malformed_model_and_features():
    from types import SimpleNamespace

    from core.research.perilla.alpha.bayesian_weight_calibrator import (
        BayesianWeightCalibrator,
        CalibrationResult,
    )

    result = CalibrationResult.from_dict({
        "feature_names": ["score"],
        "posteriors": [None, {"name": "score", "posterior_mean": float("nan")}],
    })
    calibrator = BayesianWeightCalibrator.__new__(BayesianWeightCalibrator)
    calibrator._result = result
    calibrator.registry = SimpleNamespace(
        get_node_weights=lambda: None,
        get_bayesian_config=lambda: None,
    )
    probability = calibrator.predict_success_probability(None)
    assert 0.0 <= probability <= 1.0


def test_sag_runtime_boundaries_reject_invalid_writes(tmp_path):
    from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

    storage_path = tmp_path / "runtime-sag.json"
    kb = SAGKnowledgeBase(storage_path=storage_path)
    assert kb.add_claim(None, "made_of", "ceramic_part", "evidence", "web_search", "title") is None
    assert kb.add_event_claim(None, "subject", "object", None, "evidence", "web_search", "title") is None
    claim = kb.add_claim(
        "alpha_device", "made_of", "ceramic_part", "evidence", "web_search", "title"
    )
    assert claim is not None
    claim.claim_score = float("nan")
    assert kb.save() is False
    assert kb.get_subgraph("alpha_device", max_depth="bad", min_confidence=float("nan"))["root"] == "alpha_device"

    updated = kb.update_claim_status(
        claim.claim_id,
        "supported",
        "boundary test",
    )
    assert updated is claim
    assert updated.status_history[-1]["new_status"] == "supported"
    assert not hasattr(updated, "last_updated")


def test_bottleneck_miner_skips_malformed_nodes_and_bounds_probability():
    from types import SimpleNamespace

    from core.research.perilla.discovery.bottleneck_miner import BottleneckMiner

    miner = BottleneckMiner()
    chain = SimpleNamespace(chain_id="chain", concept_id="concept", nodes=[None])
    assert miner.mine_bottlenecks(chain, [None], [None]) == []
    assert 0.0 <= miner._compute_perilla_probability(None, "bad") <= 1.0


def test_chain_constructor_bounds_malformed_payloads():
    from core.research.perilla.discovery.chain_constructor import DynamicChainConstructor

    constructor = DynamicChainConstructor.__new__(DynamicChainConstructor)
    result = constructor._parse_chain_response(
        '{"nodes": [null, {"name": null, "layer": "material"}, '
        '{"name": "材料", "layer": "material"}], "edges": null}',
        None,
        None,
    )
    assert result is not None
    assert [node.name for node in result.nodes] == ["材料"]
    assert result.edges == []
    assert constructor._extract_json(None) is None
    assert constructor._build_chain_prompt(None, None, [None], [None])
    assert constructor.construct_chain(None, None, None, None).nodes


def test_change_monitor_rejects_invalid_paths_and_payload_shapes():
    from core.research.perilla.discovery.change_monitor import ChangeMonitor

    monitor = ChangeMonitor()
    assert monitor.import_changes(None) == []
    assert monitor.parse_webhook(None) is None
    assert monitor.parse_webhook({"url": "https://example.com", "diff": {}}) is not None


def test_change_monitor_handles_non_utf8_exports_and_normalizes_fields(tmp_path):
    from core.research.perilla.discovery.change_monitor import ChangeMonitor

    path = tmp_path / "changes.json"
    path.write_bytes(b"\xff\xfe")
    assert ChangeMonitor().import_changes(str(path)) == []

    document = ChangeMonitor().parse_webhook(
        {
            "url": " https://example.com ",
            "title": " title ",
            "last_changed": 123,
            "diff": {"changed": True},
        }
    )
    assert document is not None
    assert document.url == "https://example.com"
    assert document.title == "title"
    assert document.published_at == "123"
    assert document.content_text == "{'changed': True}"


def test_company_mapper_skips_malformed_inputs():
    from types import SimpleNamespace

    from core.research.perilla.discovery.company_mapper import CompanyMapper
    from core.research.perilla.discovery.models import SupplyChainNode

    mapper = CompanyMapper()
    chain = SimpleNamespace(
        chain_id="chain",
        nodes=[SupplyChainNode(node_id="node", name="材料", layer="material"), None],
    )
    assert mapper.map_companies(chain, [None], [None], [None], None) == []
    assert mapper._resolve_company_code(None) == (None, "UNKNOWN")


def test_concept_claim_tagger_skips_malformed_candidates_and_documents():
    from core.research.perilla.discovery.concept_claim_tagger import ConceptClaimTagger

    tagger = ConceptClaimTagger()
    assert tagger.tag_concept_claims(None, None) == []
    assert tagger.tag_concept_claims([None], [None]) == [None]
    assert tagger._has_supply_evidence(None, [None]) is False
    assert tagger._filter_docs_for_company("company", [None]) == []


def test_concept_claim_tagger_accepts_mapping_boundaries():
    from core.research.perilla.discovery.concept_claim_tagger import ConceptClaimTagger

    candidate = {
        "company_name": "公司A",
        "evidence_level": "INSUFFICIENT_EVIDENCE",
        "needs_human_review": False,
    }
    docs = [
        {
            "content_text": "公司A仅出现在概念页面。",
            "source_type": "concept_page",
        }
    ]
    result = ConceptClaimTagger().tag_concept_claims([candidate], docs)
    assert result[0]["evidence_level"] == "CONCEPT_CLAIM_ONLY"
    assert result[0]["needs_human_review"] is True


def test_concept_clusterer_skips_malformed_signals_and_documents():
    from core.research.perilla.discovery.concept_clusterer import ConceptClusterer

    clusterer = ConceptClusterer()
    assert clusterer.cluster_concepts(None) == []
    assert clusterer.cluster_concepts([None]) == []
    assert clusterer.identify_demand_anchors(None, None) == []
    assert clusterer._merge_group("AIPC", set(), []) is None


def test_concept_clusterer_normalizes_mapping_signals_and_lowercase_anchors():
    from core.research.perilla.discovery.concept_clusterer import ConceptClusterer

    clusterer = ConceptClusterer()
    merged = clusterer.cluster_concepts(
        [
            {"name": "AI PC", "aliases": []},
            {"name": "AIPC", "aliases": []},
        ]
    )
    assert len(merged) == 1
    assert merged[0].name == "AIPC"

    anchors = clusterer.identify_demand_anchors(
        {"name": "ai芯片", "concept_id": "c1", "confidence": "bad"},
        [{"document_id": "doc1"}],
    )
    assert {anchor.anchor_type for anchor in anchors} >= {
        "application_scenario",
        "technology_route",
    }
    assert all(anchor.confidence == "low" for anchor in anchors)


def test_concept_discovery_engine_bounds_malformed_inputs():
    from core.research.perilla.discovery.concept_discovery_engine import ConceptDiscoveryEngine

    engine = ConceptDiscoveryEngine.__new__(ConceptDiscoveryEngine)
    engine.lookback_days = 30
    engine.min_score = 0.2
    assert engine._extract_candidate_terms(None) == []
    assert engine._item_text(None) == ""
    assert engine._candidate_text(None) == ""
    assert engine._evidence_strength(None) == 0.0
    assert engine._format_company(None) == ""
    assert all(value == 0 for value in engine._branch_counts([None], set()).values())
    assert engine._concept_evidence_level(float("nan"), "bad") == "HYPOTHESIS"


def test_concept_llm_explainer_bounds_malformed_terms(tmp_path):
    import pytest

    from core.research.perilla.discovery.concept_llm_explainer import ConceptLLMExplainer

    explainer = ConceptLLMExplainer.__new__(ConceptLLMExplainer)
    explainer.providers = [object()]
    explainer.model_name = "model"
    explainer.cache_dir = tmp_path
    explainer._cache_enabled = True
    assert explainer.explain(None) is None
    assert explainer._user_prompt(None)
    with pytest.raises(ValueError):
        explainer._parse_json(None)
    assert explainer._validate({}, None) is None


def test_concept_radar_skips_malformed_source_rows(monkeypatch, tmp_path):
    from core.research.perilla.discovery.concept_radar import ConceptRadar

    radar = ConceptRadar(project_root=str(tmp_path))
    monkeypatch.setattr(radar, "_load_gateway_themes", lambda: [None])
    monkeypatch.setattr(radar, "_load_local_evidence", lambda _cutoff: [None])
    monkeypatch.setattr(radar, "_load_recent_titles", lambda _cutoff: [None])
    monkeypatch.setattr(radar, "_load_gateway_search", lambda _topic: [None])
    monkeypatch.setattr(radar, "_load_manual_signals", lambda: [None])
    assert radar.auto_discover_concepts(None) == []
    assert radar._normalize_name(None) == ""
    assert radar._safe_float("bad") == 0.0
    assert radar._dedup_key(None) == ""


def test_concept_scout_engine_skips_malformed_market_and_report_rows(tmp_path):
    from core.research.perilla.discovery.concept_scout_engine import ConceptScoutEngine

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "market_anomalies_latest.json").write_text(
        '[null, {"symbol": "000001"}]', encoding="utf-8"
    )
    engine = ConceptScoutEngine(project_root=str(tmp_path), output_dir="reports")
    assert engine._load_market_records() == [{"symbol": "000001"}]
    path = engine._write_discovery_items_md(
        [None, {"concept_score": "bad"}], [None, {}]
    )
    assert path.exists()


def test_concept_scout_engine_deduplicates_identical_market_rows(tmp_path):
    from core.research.perilla.discovery.concept_scout_engine import ConceptScoutEngine

    (tmp_path / "data").mkdir()
    payload = '[{"symbol": "000001", "score": 1}]'
    (tmp_path / "data" / "market_anomalies_latest.json").write_text(
        payload, encoding="utf-8"
    )
    (tmp_path / "data" / "reports").mkdir()
    (tmp_path / "data" / "reports" / "market_anomalies_latest.json").write_text(
        payload, encoding="utf-8"
    )
    engine = ConceptScoutEngine(project_root=str(tmp_path), output_dir="reports")
    assert engine._load_market_records() == [{"symbol": "000001", "score": 1}]


def test_concept_radar_keeps_display_name_after_deduplication(monkeypatch, tmp_path):
    from core.research.perilla.discovery.concept_radar import ConceptRadar

    radar = ConceptRadar(project_root=str(tmp_path))
    monkeypatch.setattr(
        radar,
        "_load_gateway_themes",
        lambda: [{"name": "AI 芯片", "heat": 1.0}],
    )
    monkeypatch.setattr(radar, "_load_local_evidence", lambda _cutoff: [])
    monkeypatch.setattr(radar, "_load_recent_titles", lambda _cutoff: [])
    monkeypatch.setattr(radar, "_load_gateway_search", lambda _topic: [])
    monkeypatch.setattr(radar, "_load_manual_signals", lambda: [])

    signals = radar.auto_discover_concepts()
    assert [signal.name for signal in signals] == ["AI 芯片"]


def test_concept_signal_loader_bounds_malformed_rows(tmp_path):
    from core.research.perilla.discovery.concept_signal_loader import ConceptSignalLoader

    loader = ConceptSignalLoader(project_root=tmp_path, output_dir="reports")
    assert loader.to_concept_signals(None) == []
    assert loader.to_concept_signals([None, {"concept_score": "bad"}]) == []
    assert loader._read_json(tmp_path / "missing.json") is None
    assert loader._write_json_list(loader.output_dir / "rows.json", [None, {"ok": True}]).exists()


def test_concept_taxonomy_bounds_malformed_terms_and_context():
    from core.research.perilla.discovery.concept_taxonomy import ConceptTaxonomy

    taxonomy = ConceptTaxonomy()
    assert taxonomy.normalize(None, None) == ""
    assert taxonomy.root_theme_for(None, None) == ""
    assert taxonomy.parent_concept_for(None, None) == ""
    assert taxonomy._contains_any(None, None) is False
    assert ConceptTaxonomy(None).themes == ()


def test_concept_taxonomy_rejects_non_text_terms():
    from core.research.perilla.discovery.concept_taxonomy import ConceptTaxonomy

    taxonomy = ConceptTaxonomy()
    assert taxonomy.normalize(123, None) == ""
    assert taxonomy.is_known_term(123) is False


def test_research_question_engine_bounds_iterables_and_scores():
    import math

    from core.research.perilla.discovery.research_question_engine import ResearchQuestionEngine

    engine = ResearchQuestionEngine()
    assert engine.generate_questions(None) == []
    questions = engine.generate_questions(
        (item for item in [
            None,
            {
                "normalized_name": "AI PC",
                "related_companies": "公司A",
                "concept_score": float("nan"),
                "source_items": None,
            },
        ]),
        weak_signals=[
            {
                "company_name": "公司A",
                "concept_name": "AI PC",
                "priority_score": float("inf"),
            }
        ],
        max_questions="bad",
    )
    assert questions
    assert all(math.isfinite(question["priority_score"]) for question in questions)
    assert any(question["related_companies"] == ["公司A"] for question in questions)


def test_weak_signal_tracker_handles_generators_and_negative_moves():
    from core.research.perilla.discovery.weak_signal_tracker import WeakSignalTracker

    tracker = WeakSignalTracker()
    signals = tracker.build_signals(
        (item for item in [{
            "concept_name": "AI PC",
            "related_companies": "公司A",
            "concept_score": float("nan"),
        }]),
        company_candidates=None,
        inbox_items=None,
    )
    assert len(signals) == 1
    assert signals[0]["company_name"] == "公司A"
    assert signals[0]["priority_score"] == 0.0

    anomalies = tracker.identify_market_anomalies(
        [{"symbol": "000001", "company_name": "公司A", "change_pct": -8.0}]
    )
    assert len(anomalies) == 1
    assert anomalies[0]["anomaly_score"] == 0.8


def test_drill_down_engine_bounds_malformed_runtime_inputs(monkeypatch, tmp_path):
    from core.research.perilla.discovery.drill_down_engine import DrillDownEngine

    for key in (
        "DRILL_LLM_BATCH_SIZE", "DRILL_EXTRACTION_CONFIDENCE_THRESHOLD",
        "DRILL_IMPORTANT_MAX_DEPTH", "SEARCH_DDG_MAX_CONCURRENCY",
        "SEARCH_DDG_MIN_INTERVAL", "SEARCH_DDG_FAILURE_COOLDOWN", "SEARCH_DDG_BURST",
    ):
        monkeypatch.setenv(key, "bad")
    engine = DrillDownEngine(atomic_terms_path=tmp_path / "missing.json")
    assert engine.is_atomic(None) is False
    assert engine._merge_docs([None]) == []
    assert engine._extraction_score([None]) == 0.0
    assert engine._rule_extract(None, [None]) == []
    assert engine._llm_extract_batch(None) == {}
    assert DrillDownEngine.render_tree(None) == "?"


def test_evidence_search_task_generator_skips_malformed_inputs():
    from core.research.perilla.discovery.evidence_search_task_generator import (
        EvidenceSearchTaskGenerator,
    )

    generator = EvidenceSearchTaskGenerator()
    assert generator.generate_tasks(None, None, None, min_tasks="bad") == []
    assert generator._tasks_for_concept(None, set()) == []
    assert generator._tasks_for_market_anomaly(None, set()) == []
    assert generator.group_tasks_by_tier(None) == {"high": [], "medium": [], "low": []}


def test_focused_crawler_bounds_malformed_urls(tmp_path):
    from core.research.perilla.discovery.focused_crawler import FocusedCrawler

    crawler = FocusedCrawler(cache_dir=str(tmp_path), timeout="bad")
    assert crawler.crawl_url(None) is None
    assert crawler.crawl_url(123) is None
    assert crawler.crawl_urls([None, 123]) == []
    assert crawler.is_url_cached(None) is False
    assert crawler._url_to_hash(None) == ""
    assert crawler._infer_source_type(None) == "official_site"
    assert crawler._infer_credibility(None) == "unknown"


def test_knowledge_loop_source_ingestion_skips_malformed_rows(monkeypatch, tmp_path):
    from core.research.perilla.discovery import knowledge_loop_engine as module
    from core.research.perilla.discovery.knowledge_loop_engine import (
        PerillaKnowledgeLoopEngine,
    )
    from infrastructure.data_providers import provider_registry

    class Loader:
        inbox_path = tmp_path / "inbox.json"

        @staticmethod
        def load_inbox_items():
            return [None, {"main_dish": 123, "company_name": "测试公司"}]

        @staticmethod
        def load_title_items():
            return [None, {"title": 456, "summary": object()}]

    class Registry:
        def get_announcements(self, **_kwargs):
            return [None, {"title": 789, "digest": object()}]

        def get_research_reports(self, **_kwargs):
            return [None]

        def get_periodic_reports(self, **_kwargs):
            return None

        def get_investor_interactions(self, **_kwargs):
            return [None, {"question": 1, "answer": 2}]

        def get_hot_themes(self):
            return {"industry": [None, {"name": 3}], "concept": "bad"}

    monkeypatch.setattr(provider_registry, "ProviderRegistry", Registry)
    for name in (
        "_fetch_sina_news", "_fetch_eastmoney_news", "_fetch_ths_headlines",
        "_fetch_tushare_news", "_fetch_tushare_announcements",
        "_fetch_tushare_reports", "_fetch_eastmoney_guba",
    ):
        monkeypatch.setattr(module, name, lambda: [None, {"title": 1}])

    engine = PerillaKnowledgeLoopEngine(
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        use_llm_explanations=False,
    )
    engine.loader = Loader()
    documents = engine._build_raw_documents()

    assert documents
    assert all(document.title is not None for document in documents)
    assert all(isinstance(document.source_payload, dict) for document in documents)


def test_knowledge_loop_response_helpers_bound_external_payloads():
    from core.research.perilla.discovery.knowledge_loop_engine import (
        _frame_records,
        _nested_records,
        _response_mapping,
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Frame:
        def to_dict(self, **_kwargs):
            return [None, {"title": "ok"}]

    assert _response_mapping(Response([])) == {}
    assert _nested_records({"data": {"list": [None, {"ok": True}]}}, "data", "list") == [{"ok": True}]
    assert _frame_records(Frame()) == [{"title": "ok"}]


def test_knowledge_loop_search_batch_filters_malformed_tasks(monkeypatch, tmp_path):
    from core.research.perilla.discovery.knowledge_loop_engine import (
        PerillaKnowledgeLoopEngine,
    )
    from infrastructure.data_providers import provider_registry

    class Registry:
        def semantic_search(self, **_kwargs):
            return {"results": [None, {"unexpected": object()}], "source": "stub"}

    monkeypatch.setattr(provider_registry, "ProviderRegistry", Registry)
    engine = PerillaKnowledgeLoopEngine(
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        use_llm_explanations=False,
    )

    runs, documents = engine._execute_search_tasks_batch(
        [None, {"task_id": "task-1", "query": None, "aquatrade_capability": "semantic_search"}]
    )

    assert runs[0]["task_id"] == "task-1"
    assert runs[0]["event_count"] == 1
    assert len(documents) == 1


def test_knowledge_loop_candidate_and_output_boundaries(tmp_path):
    from core.research.perilla.discovery.knowledge_loop_engine import (
        PerillaKnowledgeLoopEngine,
    )

    engine = PerillaKnowledgeLoopEngine(
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        use_llm_explanations=False,
    )
    engine.loader.load_concept_signals = lambda: [None, {"name": None, "aliases": object()}]

    assert engine._build_evidence_edges([None, {"normalized_name": None}], [None]) == []
    assert engine._build_search_tasks([None], [None]) == []
    assert engine._select_cold_new_term([None]) == {}
    assert engine._enrich_candidate_concepts_with_llm([None], [None]) == ([], [])
    assert engine._append_task([], set(), None, None, None, None, float("nan"), None) is None
    outputs = engine._write_outputs({}, [], raw_documents=[None])
    assert "raw_documents" in outputs


def test_market_scope_tagger_normalizes_and_bounds_market_inputs():
    from core.research.perilla.discovery.market_scope_tagger import MarketScopeTagger
    from core.research.perilla.discovery.models import CompanyCandidate

    tagger = MarketScopeTagger()
    assert tagger.tag_market(CompanyCandidate(company_name="A", market=" us ")) == ("US", None)
    market, warning = tagger.tag_market(CompanyCandidate(company_name="B", market=123))
    assert market == "UNKNOWN"
    assert warning
    assert tagger.tag_market(None)[0] == "UNKNOWN"
    assert tagger.tag_market({"company_name": "C", "market": " hk "})[0] == "HK"
    assert tagger.is_core_market(" us ") is True
    assert tagger.is_core_market(None) is False


def test_review_queue_bounds_mapping_items_and_probabilities(tmp_path):
    from core.research.perilla.discovery.review_queue import ReviewQueue

    queue_manager = ReviewQueue()
    assert queue_manager.build_queue([None], None) == []
    queue = queue_manager.build_queue(
        [{
            "item_id": "item-1",
            "classification": "CONCEPT_CLAIM_ONLY",
            "evidence_level": "HYPOTHESIS",
            "perilla_probability": float("nan"),
            "source_urls": "https://example.com",
            "company_name": "公司A",
            "concept": "AI PC",
        }],
        None,
    )
    assert queue
    assert queue[0].related_sources == ["https://example.com"]
    exported = queue_manager.export_queue(queue, str(tmp_path))
    assert exported.endswith("review_queue_latest.json")


def test_perilla_classifier_bounds_malformed_batch_inputs():
    from core.research.perilla.discovery.models import CompanyCandidate, DynamicSupplyChain
    from core.research.perilla.discovery.perilla_classifier import PerillaClassifier

    classifier = PerillaClassifier()
    items = classifier.classify([None, CompanyCandidate(company_name="测试")], [None], DynamicSupplyChain())

    assert len(items) == 2
    assert all(item.review_status == "pending_human_review" for item in items)
    assert classifier.classify(None, None, None) == []
    assert classifier._compute_perilla_probability_for_item(
        CompanyCandidate(), type("Bottleneck", (), {"perilla_probability": float("nan")})()
    ) == 0.0


def test_perilla_leaf_screener_bounds_inputs_and_removes_duplicate_llm_path(monkeypatch):
    from core.research.perilla.discovery.perilla_leaf_screener import PerillaLeafScreener

    screener = PerillaLeafScreener.__new__(PerillaLeafScreener)
    screener._llm_available = False
    screener._search_method = "none"
    screener._search = None
    screener._ddgs = None
    monkeypatch.setattr(screener, "_web_search_bing", lambda _query: "")

    assert screener._extract_json(None) is None
    assert screener._extract_json("[1, 2]") is None
    assert screener._extract_json('{"ok": true}') == {"ok": True}
    assert screener._filter_evidence_links([None, "https://example.com", "https://example.com", "https://baike.baidu.com/x"]) == ["https://example.com"]
    assert screener._normalize_market(None, None) == "OTHER"
    assert screener._web_search(None) == ""
    assert screener.step0_define_main_dish(None) == ""
    assert screener.step1_drill_down_chain(None, None) == []
    assert screener.step3_scoring_card(None) == (0, True)


def test_perilla_leaf_screener_bounds_mapping_and_red_team_inputs():
    from core.research.perilla.discovery.perilla_leaf_screener import (
        PerillaLeafScreener,
        PerillaLeafCandidate,
    )

    screener = PerillaLeafScreener.__new__(PerillaLeafScreener)
    screener._llm_available = False
    screener._search_available = False
    screener._verification_cache = {}
    scores = {"total_score": "bad", "pricing_vetoed": None, "scores": None}

    assert screener._step4_fallback(None, None, None)
    candidates = screener._parse_tickers_to_candidates(
        [None, {"company_name": 123, "symbol": None, "stress_test_passed": "false"}],
        None,
        None,
        scores,
        None,
    )
    assert len(candidates) == 1
    assert isinstance(candidates[0], PerillaLeafCandidate)
    assert screener.step5_red_team([None]) == []
    assert screener._step5_fallback(None) == []


def test_perilla_leaf_screener_empty_report_and_run_do_not_create_zero_worker_pool(monkeypatch):
    from core.research.perilla.discovery.perilla_leaf_screener import PerillaLeafScreener

    screener = PerillaLeafScreener.__new__(PerillaLeafScreener)
    screener._llm_available = False
    screener._search_available = False
    screener._progress_callback = None
    screener._verification_cache = {}
    screener._progress = lambda _message: None
    monkeypatch.setattr(screener, "step0_define_main_dish", lambda _topic: "main")
    monkeypatch.setattr(screener, "step1_drill_down_chain", lambda _main, _topic: [{"layer": "terminal", "is_perilla_candidate": False}])

    report = screener.run("topic", mode="unexpected")
    assert report.candidates == []
    assert screener.step6_generate_report(None, None, None, [None]).candidates == []
    assert screener._summarize_chain_nodes(None) == "产业链为空"


def test_perilla_leaf_report_renderer_bounds_malformed_report_fields():
    from core.research.perilla.discovery.perilla_leaf_screener import (
        PerillaLeafReport,
        _format_scores,
        render_perilla_leaf_report_md,
    )

    assert render_perilla_leaf_report_md(None) == ""
    report = PerillaLeafReport(
        topic=123,
        candidates=[None],
        top_picks=[None],
        eliminated=[None],
        disclaimer=None,
    )
    rendered = render_perilla_leaf_report_md(report)
    assert "紫苏叶选股法报告" in rendered
    assert _format_scores(None) == "必配性=0，价值占比=0，供给格局=0，扩产速度=0，切换成本=0，定价状态=0"


def test_active_learning_planner_bounds_inputs_and_storage_paths(tmp_path):
    from core.research.alpha.active_learning_planner import ActiveLearningPlanner

    planner = ActiveLearningPlanner(tmp_path)
    assert planner.plan_from_gaps(None) == []
    assert planner.plan_from_alpha_result(None) == []
    assert planner.load_plans("missing.json") == []
    planner.save_plans([None], "plans.json")
    assert planner.load_plans("plans.json") == []
    with pytest.raises(ValueError):
        planner.save_plans([], "../escape.json")
