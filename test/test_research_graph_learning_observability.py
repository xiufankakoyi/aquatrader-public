from __future__ import annotations

import json
from pathlib import Path

from core.research_graph.learning_observability import LearningObservabilityReporter
from core.research_graph.models import Evidence, HyperEdge, HyperNode
from core.research_graph.neuron.environment import ResearchGraphEnvironment
from core.research_graph.neuron.models import ResearchNeuron
from core.research_graph.neuron.state_store import NeuronStateStore
from core.research_graph.schema import EdgeStatus
from core.research_graph.store import GraphStore


def test_learning_observability_empty_state_outputs_zero_metrics() -> None:
    snapshot = LearningObservabilityReporter().build_snapshot()

    metrics = snapshot["learning_metrics"]
    assert metrics["canonical_evidence_count"] == 0
    assert metrics["active_edge_count"] == 0
    assert metrics["conflicted_edge_count"] == 0
    assert metrics["avg_confidence"] == 0.0
    assert metrics["avg_uncertainty"] == 0.0
    assert metrics["breakthrough_score"] == 0.0
    assert snapshot["topic_mastery_profile"]["topics"] == []


def test_learning_observability_aggregates_graph_and_neuron_metrics() -> None:
    store = GraphStore()
    store.nodes["n1"] = HyperNode(node_id="n1", type="MATERIAL", name="HBM material")
    store.nodes["n2"] = HyperNode(node_id="n2", type="PROCESS", name="HBM process")
    store.edges["e_active"] = HyperEdge(
        edge_id="e_active",
        relation="REQUIRES",
        subjects=["n1"],
        objects=["n2"],
        status=EdgeStatus.ACTIVE.value,
    )
    store.edges["e_conflict"] = HyperEdge(
        edge_id="e_conflict",
        relation="CONTRADICTED_BY",
        subjects=["n1"],
        objects=["n2"],
        status=EdgeStatus.CONFLICTED.value,
    )
    store.evidence["ev1"] = Evidence(
        evidence_id="ev1",
        source_type="announcement",
        source_title="公告",
        source_id="src1",
        quote="公告披露了可追溯事实",
    )
    store.evidence["ev2"] = Evidence(
        evidence_id="ev2",
        source_type="news",
        source_title="新闻",
        source_id="src2",
        quote="新闻披露了补充事实",
        extracted_by="llm_extractor",
        metadata={"llm_extracted": True, "extraction_trace_path": "trace.json"},
    )
    neurons = [
        ResearchNeuron(
            neuron_id="rn1",
            node_id="n1",
            node_type="MATERIAL",
            canonical_name="HBM material",
            root_theme="HBM",
            confidence=0.8,
            uncertainty=0.2,
            support_count=2,
            counter_count=1,
            source_diversity=0.5,
            novelty=0.75,
        ),
        ResearchNeuron(
            neuron_id="rn2",
            node_id="n2",
            node_type="PROCESS",
            canonical_name="HBM process",
            root_theme="HBM",
            confidence=0.4,
            uncertainty=0.6,
            support_count=1,
            counter_count=0,
        ),
    ]
    delta_report = {
        "significant_deltas": [
            {
                "neuron_id": "rn1",
                "confidence_delta": 0.4,
                "new_evidence_ids": ["ev1", "ev2"],
            }
        ]
    }

    snapshot = LearningObservabilityReporter(
        graph_store=store,
        neurons=neurons,
        delta_report=delta_report,
        duplicate_count=1,
        llm_rejected_count=1,
    ).build_snapshot()

    metrics = snapshot["learning_metrics"]
    assert metrics["canonical_evidence_count"] == 2
    assert metrics["active_edge_count"] == 1
    assert metrics["conflicted_edge_count"] == 1
    assert metrics["avg_confidence"] == 0.6
    assert metrics["avg_uncertainty"] == 0.4
    assert metrics["new_evidence_delta"] == 2
    assert metrics["confidence_delta"] == 0.4
    assert metrics["duplicate_ratio"] == 0.3333
    assert metrics["llm_rejected_ratio"] == 0.5
    assert metrics["breakthrough_score"] > 0
    assert snapshot["topic_mastery_profile"]["totals"]["topic_count"] == 2


def test_learning_observability_writes_four_artifacts(tmp_path: Path) -> None:
    reporter = LearningObservabilityReporter()

    artifacts = reporter.write_artifacts(tmp_path)

    assert set(artifacts) == {
        "learning_metrics",
        "learning_curve_latest",
        "topic_mastery_profile",
        "breakthrough_report",
    }
    for path in artifacts.values():
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))
    history = tmp_path / "learning_curve_history.jsonl"
    assert history.exists()
    row = json.loads(history.read_text(encoding="utf-8").splitlines()[0])
    assert row["canonical_evidence_count"] == 0
    assert "breakthrough_score" in row


def test_learning_observability_appends_history_points(tmp_path: Path) -> None:
    reporter = LearningObservabilityReporter(tick_count=12)

    first = reporter.append_history(tmp_path)
    reporter.append_history(tmp_path)

    rows = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["tick_count"] == 12


def test_environment_exports_learning_observability(tmp_path: Path) -> None:
    store = GraphStore()
    state_store = NeuronStateStore(db_path=tmp_path / "state.lance")
    state_store.save_neuron(
        ResearchNeuron(
            neuron_id="rn1",
            node_id="n1",
            node_type="MATERIAL",
            canonical_name="HBM material",
            confidence=0.7,
            uncertainty=0.3,
        )
    )
    env = ResearchGraphEnvironment(state_store=state_store, graph_store=store)

    result = env.export_learning_observability(tmp_path / "learning")

    assert result["learning_metrics"]["avg_confidence"] == 0.7
    assert Path(result["history_path"]).exists()
    for path in result["artifacts"].values():
        assert Path(path).exists()
