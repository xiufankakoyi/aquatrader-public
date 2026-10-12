from __future__ import annotations

from core.research_graph.gap_analyzer import (
    GapAnalyzer,
    get_evidence_quality,
    get_evidence_source_type,
    get_evidence_stance,
    get_evidence_time,
)
from core.research_graph.models import Evidence
from core.research_graph.neuron.models import ResearchNeuron
from core.research_graph.schema import EvidenceLevel


def test_canonical_evidence_metadata_stance_routes_to_counter() -> None:
    analyzer = GapAnalyzer()
    neuron = ResearchNeuron(
        neuron_id="n1",
        node_id="node_1",
        node_type="MATERIAL",
        canonical_name="电镀液",
    )
    support = Evidence(
        evidence_id="ev_support",
        source_type="announcement",
        source_title="公告",
        source_id="src_support",
        quote="公告披露支持事实",
        evidence_level=EvidenceLevel.PROVEN.value,
        metadata={"stance": "support"},
    )
    counter = Evidence(
        evidence_id="ev_counter",
        source_type="announcement",
        source_title="澄清公告",
        source_id="src_counter",
        quote="公告披露反证事实",
        evidence_level=EvidenceLevel.DISPROVEN.value,
        metadata={"stance": "counter"},
    )

    score, _ = analyzer.analyze_neuron(neuron, [support, counter], "2026-07-03T00:00:00+00:00")

    assert get_evidence_stance(counter) == "counter"
    assert score.consistency == 0.0
    assert "证据存在矛盾" in score.uncertainty_sources


def test_canonical_source_type_and_evidence_level_drive_quality() -> None:
    ev = Evidence(
        evidence_id="ev_quality",
        source_type="announcement",
        source_title="公告",
        source_id="src_quality",
        quote="公告披露可追溯事实",
        evidence_level=EvidenceLevel.LIKELY.value,
    )

    assert get_evidence_source_type(ev) == "announcement"
    assert get_evidence_quality(ev, {"announcement": 1.0}) == 1.0


def test_publish_date_participates_in_freshness() -> None:
    analyzer = GapAnalyzer()
    neuron = ResearchNeuron(
        neuron_id="n1",
        node_id="node_1",
        node_type="MATERIAL",
        canonical_name="电镀液",
    )
    ev = Evidence(
        evidence_id="ev_fresh",
        source_type="announcement",
        source_title="公告",
        source_id="src_fresh",
        publish_date="2026-07-02T00:00:00+00:00",
        quote="公告披露可追溯事实",
        evidence_level=EvidenceLevel.PROVEN.value,
    )

    score, _ = analyzer.analyze_neuron(neuron, [ev], "2026-07-03T00:00:00+00:00")

    assert get_evidence_time(ev) == "2026-07-02T00:00:00+00:00"
    assert score.freshness > 0.8
