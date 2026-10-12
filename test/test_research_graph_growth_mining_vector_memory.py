from __future__ import annotations

from core.research_graph.growth_mining import GrowthMiningEngine, GrowthMiningRequest
from core.research_graph.vector_memory import DocumentChunk, DocumentChunkStore


def test_growth_mining_collects_vector_memory_candidates(tmp_path) -> None:
    vector_path = tmp_path / "chunks.lance"
    store = DocumentChunkStore(db_path=vector_path)
    store.upsert_chunks([
        DocumentChunk(
            chunk_id="chunk_plating",
            document_id="doc_plating",
            source_id="src_plating",
            source_type="announcement",
            topic="HBM",
            text="电镀液 HBM 先进封装 TSV 铜电镀填充 客户验证",
        )
    ])
    engine = GrowthMiningEngine(
        GrowthMiningRequest(
            topic="HBM",
            node="电镀液",
            run_root=tmp_path / "runs",
            vector_memory_path=vector_path,
        )
    )

    failed_sources: list[dict] = []
    candidates = engine._collect_vector_memory_candidates(failed_sources)

    assert failed_sources == []
    assert candidates
    assert candidates[0].provider_name == "vector_memory"
    assert candidates[0].provider_type == "local_vector_memory"
    assert candidates[0].metadata["content_origin"] == "vector_memory_chunk"


def test_growth_mining_writes_persisted_source_back_to_vector_memory(tmp_path) -> None:
    vector_path = tmp_path / "chunks.lance"
    engine = GrowthMiningEngine(
        GrowthMiningRequest(
            topic="HBM",
            node="电镀液",
            run_root=tmp_path / "runs",
            vector_memory_path=vector_path,
        )
    )
    source = {
        "source_id": "src_new",
        "document_id": "doc_new",
        "provider_type": "announcement",
        "provider_name": "fixture",
        "query_or_request": "电镀液 HBM",
        "retrieved_at": "2026-07-03T00:00:00+00:00",
        "content_hash": "sha256:test",
        "normalized_document_path": str(tmp_path / "doc.txt"),
    }
    (tmp_path / "doc.txt").write_text("电镀液 HBM 先进封装 TSV 铜电镀填充", encoding="utf-8")

    engine._upsert_vector_memory_source(source)

    results = DocumentChunkStore(db_path=vector_path).search("HBM 铜电镀填充", limit=1)
    assert results
    assert results[0].chunk.document_id == "doc_new"
