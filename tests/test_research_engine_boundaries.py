from __future__ import annotations

import math

import pytest

from core.research.engine.models import Entity, EntityRegistry, EvidenceGraph, MoatDetector, Relation
from core.research.engine.single_stock_research import SingleStockResearch


def test_graph_models_normalize_bad_fields() -> None:
    entity = Entity(entity_name="测试", aliases="invalid", symbols=["000001", 1])
    relation = Relation(
        source_entity_id=entity.entity_id,
        target_entity_id="target",
        evidence_ids="invalid",
        weight=math.nan,
    )
    registry = EntityRegistry()
    registry.add(entity)
    graph = EvidenceGraph(registry)
    graph.add_relation(relation)

    assert entity.aliases == []
    assert entity.symbols == ["000001"]
    assert relation.evidence_ids == []
    assert relation.weight == 0.0
    assert registry.find(None) == []
    assert graph.get_neighbors(entity.entity_id, max_depth="invalid") == {entity.entity_id: 0}
    with pytest.raises(TypeError):
        registry.add(None)


def test_moat_detector_handles_non_text_inputs() -> None:
    detector = MoatDetector()

    assert detector.detect_from_text(None)["moat_score"] == 0.0
    result = detector.detect_from_extraction("invalid", ["risk"], symbol="000001")

    assert result["raw_moat_claims"] == []
    assert result["raw_risks"] == ["risk"]


def test_single_stock_helpers_skip_bad_kline_values(tmp_path) -> None:
    summary = SingleStockResearch._summarize_kline(
        [None, {"close": "bad"}, {"close": 0}, {"close": 10}]
    )

    assert summary["latest_close"] == 10.0
    assert "return_5d" not in summary

    researcher = SingleStockResearch()
    researcher._report_dir = tmp_path
    result = researcher.export_report("../000001", {"symbol": "000001"})

    assert result["symbol"] == "000001"
    assert (tmp_path / "000001_research_latest.json").exists()
