from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research_graph import (
    CorpusEvidenceMiner,
    EvidenceCorpusIndex,
    SourceDocument,
    claim_from_candidate,
    summarize_mining_results,
)
from core.research_graph.evidence_corpus import read_jsonl
from scripts.run_overnight_evidence_corpus import build_run


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """Build a deterministic corpus instead of relying on a developer-local run."""
    return build_run(tmp_path, "overnight_evidence_miner_service")


def test_corpus_index_loads_overnight_raw_evidence_with_source_traceability(run_dir: Path) -> None:
    index = EvidenceCorpusIndex.from_overnight_run(run_dir)

    assert len(index.documents) == 12
    assert all(document.source_id for document in index.documents)
    assert all(document.document_id for document in index.documents)
    assert all(document.url for document in index.documents)


def test_corpus_evidence_miner_retrieves_and_mines_candidate_claim(run_dir: Path) -> None:
    claim_row = read_jsonl(run_dir / "candidate_claims.jsonl")[1]
    claim = claim_from_candidate(claim_row)
    miner = CorpusEvidenceMiner.from_overnight_run(run_dir)

    result = miner.mine_claim(claim, limit=3)

    assert result.claim_id == claim_row["claim_id"]
    assert result.retrieved_documents
    assert result.support_evidence
    assert result.should_update_graph is True
    assert result.suggested_confidence in {"LIKELY", "PROVEN", "CONFLICTED"}
    for evidence in result.support_evidence + result.counter_evidence:
        assert evidence.source_id
        assert evidence.document_id
        assert evidence.metadata["claim_id"] == claim.claim_id
        assert evidence.metadata["source_id"] == evidence.source_id
    assert all("ACTIVE" not in str(item.to_dict()) for item in result.support_evidence)


def test_corpus_retrieval_prioritizes_topic_and_specific_terms() -> None:
    index = EvidenceCorpusIndex([
        SourceDocument(
            document_id="doc_hbm",
            source_id="src_hbm",
            source_type="technical_doc",
            title="HBM3E architecture",
            content="Blackwell requires HBM3E high bandwidth memory subsystem.",
            topic="HBM",
        ),
        SourceDocument(
            document_id="doc_cpo",
            source_id="src_cpo",
            source_type="technical_doc",
            title="CPO optical engine",
            content="CPO uses optical engines and silicon photonics.",
            topic="CPO",
        ),
    ])
    claim = claim_from_candidate({
        "claim_id": "claim_hbm",
        "topic": "HBM",
        "subject": {"name": "Blackwell", "type": "PRODUCT"},
        "relation": "REQUIRES",
        "object": {"name": "HBM3E", "type": "PRODUCT"},
        "path_hint": ["HBM", "Blackwell", "HBM3E"],
        "reason": "Blackwell requires HBM3E",
    })

    retrieved = index.retrieve(claim, limit=2)

    assert [item.document.document_id for item in retrieved] == ["doc_hbm"]
    assert "source_quality_hint" in retrieved[0].reasons
    assert {"Blackwell", "HBM3E"}.issubset(set(retrieved[0].matched_terms))


def test_batch_mining_summary_is_traceable_and_not_stock_recommendation(run_dir: Path) -> None:
    rows = read_jsonl(run_dir / "candidate_claims.jsonl")[:4]
    claims = [claim_from_candidate(row) for row in rows]
    miner = CorpusEvidenceMiner.from_overnight_run(run_dir)

    results = miner.mine_many(claims, limit=3)
    summary = summarize_mining_results(results)

    assert summary["claims"] == 4
    assert summary["claims_with_retrieved_documents"] == 4
    assert summary["claims_with_support_evidence"] >= 3
    assert summary["untraceable_evidence"] == []
    assert summary["non_candidate_updates"] == []
    forbidden = [
        "买" + "入",
        "卖" + "出",
        "仓" + "位",
        "止" + "盈",
        "止" + "损",
        "推荐" + "股票",
        "目标" + "价",
    ]
    assert not any(term in str(summary) for term in forbidden)
