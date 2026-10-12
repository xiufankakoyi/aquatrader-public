from __future__ import annotations

from pathlib import Path

from core.research_graph.vector_memory import (
    DocumentChunk,
    DocumentChunkStore,
    HybridRetriever,
    LocalHashEmbeddingProvider,
)


def test_document_chunk_store_upserts_and_searches_similar_chunks(tmp_path: Path) -> None:
    store = DocumentChunkStore(db_path=tmp_path / "chunks.lance")
    store.upsert_chunks(
        [
            DocumentChunk(
                chunk_id="chunk_hbm",
                document_id="doc_hbm",
                source_id="src_hbm",
                source_type="announcement",
                topic="HBM",
                text="HBM3E high bandwidth memory uses advanced packaging materials.",
                quote="HBM3E high bandwidth memory",
                quote_span=[0, 27],
                metadata={"source": "fixture"},
            ),
            DocumentChunk(
                chunk_id="chunk_cpo",
                document_id="doc_cpo",
                source_id="src_cpo",
                source_type="news",
                topic="CPO",
                text="Co-packaged optics uses silicon photonics and optical engines.",
            ),
        ]
    )

    results = store.search("HBM3E bandwidth memory packaging", limit=2)

    assert [item.chunk.chunk_id for item in results][0] == "chunk_hbm"
    assert results[0].chunk.quote == "HBM3E high bandwidth memory"
    assert results[0].chunk.quote_span == [0, 27]
    assert results[0].chunk.metadata["source"] == "fixture"


def test_hybrid_retriever_delegates_to_local_chunk_store(tmp_path: Path) -> None:
    store = DocumentChunkStore(db_path=tmp_path / "chunks.lance")
    store.upsert_chunks(
        [
            DocumentChunk(
                chunk_id="chunk_material",
                document_id="doc_material",
                text="Temporary bonding adhesive material for advanced packaging.",
            ),
            DocumentChunk(
                chunk_id="chunk_other",
                document_id="doc_other",
                text="Optical connector demand in data centers.",
            ),
        ]
    )
    retriever = HybridRetriever(store)

    results = retriever.retrieve("bonding adhesive packaging", limit=1)

    assert len(results) == 1
    assert results[0].chunk.chunk_id == "chunk_material"
    assert results[0].score > 0


def test_document_chunk_missing_optional_fields_degrades_to_empty_values(tmp_path: Path) -> None:
    store = DocumentChunkStore(db_path=tmp_path / "chunks.lance")
    store.upsert_chunks(
        [
            DocumentChunk(
                chunk_id="chunk_minimal",
                document_id="doc_minimal",
                text="Minimal local evidence chunk.",
            )
        ]
    )

    result = store.search("minimal evidence", limit=1)[0].chunk

    assert result.source_id == ""
    assert result.source_type == ""
    assert result.quote == ""
    assert result.quote_span == []
    assert result.metadata == {}


def test_local_hash_embedding_provider_is_deterministic_and_local() -> None:
    provider = LocalHashEmbeddingProvider(dimension=32)

    first = provider.embed("HBM local vector memory")
    second = provider.embed("HBM local vector memory")

    assert first == second
    assert len(first) == 32
    assert any(value > 0 for value in first)
