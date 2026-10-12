from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import lancedb
import numpy as np
import pyarrow as pa

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    dimension: int

    def embed(self, text: str) -> list[float]:
        ...


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    text: str
    source_id: str = ""
    source_type: str = ""
    topic: str = ""
    quote: str = ""
    quote_span: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DocumentChunk":
        return cls(
            chunk_id=str(payload.get("chunk_id", "")),
            document_id=str(payload.get("document_id", "")),
            text=str(payload.get("text", "")),
            source_id=str(payload.get("source_id", "")),
            source_type=str(payload.get("source_type", "")),
            topic=str(payload.get("topic", "")),
            quote=str(payload.get("quote", "")),
            quote_span=list(payload.get("quote_span") or []),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    score: float
    vector_score: float
    keyword_score: float
    source_authority_score: float

    def to_dict(self) -> dict[str, Any]:
        payload = self.chunk.to_dict()
        payload.update(
            {
                "score": round(float(self.score), 4),
                "vector_score": round(float(self.vector_score), 4),
                "keyword_score": round(float(self.keyword_score), 4),
                "source_authority_score": round(float(self.source_authority_score), 4),
            }
        )
        return payload


class LocalHashEmbeddingProvider:
    """Deterministic local embedding provider with no network dependency."""

    def __init__(self, dimension: int = 128) -> None:
        self.dimension = int(dimension)

    def embed(self, text: str) -> list[float]:
        try:
            from sklearn.feature_extraction.text import HashingVectorizer

            vectorizer = HashingVectorizer(
                analyzer="char_wb",
                ngram_range=(2, 4),
                n_features=self.dimension,
                alternate_sign=False,
                norm=None,
            )
            values = vectorizer.transform([text or ""]).toarray()[0].astype(np.float32)
        except Exception:
            values = self._fallback_embed(text)
        norm = float(np.linalg.norm(values))
        if norm > 0:
            values = values / norm
        return values.astype(np.float32).tolist()

    def _fallback_embed(self, text: str) -> np.ndarray:
        values = np.zeros(self.dimension, dtype=np.float32)
        normalized = (text or "").lower()
        grams = set()
        for size in (2, 3, 4):
            grams.update(
                normalized[index : index + size]
                for index in range(max(0, len(normalized) - size + 1))
            )
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            values[index] += 1.0
        return values


class DocumentChunkStore:
    """LanceDB-backed local vector memory for research document chunks."""

    def __init__(
        self,
        db_path: str | Path = "data/vector_store/research_chunks.lance",
        *,
        table_name: str = "chunks",
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        self._ensure_table()

    @property
    def schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("chunk_id", pa.string()),
                pa.field("document_id", pa.string()),
                pa.field("source_id", pa.string()),
                pa.field("source_type", pa.string()),
                pa.field("topic", pa.string()),
                pa.field("text", pa.string()),
                pa.field("quote", pa.string()),
                pa.field("quote_span", pa.string()),
                pa.field("metadata", pa.string()),
                pa.field(
                    "embedding",
                    pa.list_(pa.float32(), self.embedding_provider.dimension),
                ),
            ]
        )

    def upsert_chunks(self, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return
        table = self.db.open_table(self.table_name)
        records = [self._record_from_chunk(chunk) for chunk in chunks]
        for record in records:
            try:
                table.delete(f"chunk_id = '{record['chunk_id']}'")
            except Exception:
                pass
        table.add(pa.table({key: [row[key] for row in records] for key in records[0]}, schema=self.schema))

    def search(self, query: str, limit: int = 10) -> list[RetrievedChunk]:
        query_embedding = np.asarray(self.embedding_provider.embed(query), dtype=np.float32)
        query_terms = _terms(query)
        rows = self.db.open_table(self.table_name).search().to_list()
        retrieved: list[RetrievedChunk] = []
        for row in rows:
            chunk = self._chunk_from_record(row)
            vector = np.asarray(row.get("embedding") or [], dtype=np.float32)
            vector_score = _cosine(query_embedding, vector)
            keyword_score = _keyword_score(query_terms, chunk)
            source_authority_score = _source_authority_score(chunk.source_type)
            score = (
                0.55 * vector_score
                + 0.35 * keyword_score
                + 0.10 * source_authority_score
            )
            if score > 0:
                retrieved.append(
                    RetrievedChunk(
                        chunk=chunk,
                        score=score,
                        vector_score=vector_score,
                        keyword_score=keyword_score,
                        source_authority_score=source_authority_score,
                    )
                )
        retrieved.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return retrieved[:limit]

    def _ensure_table(self) -> None:
        existing = self.db.list_tables().tables
        if self.table_name not in existing:
            empty = pa.table({field.name: [] for field in self.schema}, schema=self.schema)
            self.db.create_table(self.table_name, empty)

    def _record_from_chunk(self, chunk: DocumentChunk) -> dict[str, Any]:
        return {
            "chunk_id": str(chunk.chunk_id),
            "document_id": str(chunk.document_id),
            "source_id": str(chunk.source_id),
            "source_type": str(chunk.source_type),
            "topic": str(chunk.topic),
            "text": str(chunk.text),
            "quote": str(chunk.quote),
            "quote_span": json.dumps(list(chunk.quote_span or []), ensure_ascii=False),
            "metadata": json.dumps(dict(chunk.metadata or {}), ensure_ascii=False),
            "embedding": self.embedding_provider.embed(chunk.text),
        }

    def _chunk_from_record(self, row: dict[str, Any]) -> DocumentChunk:
        try:
            quote_span = json.loads(row.get("quote_span") or "[]")
        except Exception:
            quote_span = []
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except Exception:
            metadata = {}
        return DocumentChunk(
            chunk_id=str(row.get("chunk_id", "")),
            document_id=str(row.get("document_id", "")),
            source_id=str(row.get("source_id", "")),
            source_type=str(row.get("source_type", "")),
            topic=str(row.get("topic", "")),
            text=str(row.get("text", "")),
            quote=str(row.get("quote", "")),
            quote_span=list(quote_span or []),
            metadata=dict(metadata or {}),
        )


class HybridRetriever:
    """Hybrid keyword/vector retriever over the local chunk store."""

    def __init__(
        self,
        chunk_store: DocumentChunkStore,
        *,
        cross_encoder: Any | None = None,
        late_interaction_encoder: Any | None = None,
    ) -> None:
        """初始化 Hybrid 检索器。

        Args:
            chunk_store: 文档 chunk 存储。
            cross_encoder: 可选的 Cross-Encoder 重排序器(P1 Step 10)。
                注入时在 evidentiality_scorer 后做 cross-encoder 精排;
                为 None 时跳过精排,保持原有行为。
            late_interaction_encoder: 可选的 Late-Interaction 重排序器(P2 Step 14)。
                注入时在 cross-encoder 后做 ColBERT-style token-level MaxSim 精排,
                提升零样本鲁棒性;为 None 时跳过,保持原有行为。
        """
        self.chunk_store = chunk_store
        self.cross_encoder = cross_encoder
        self.late_interaction_encoder = late_interaction_encoder

    def retrieve(
        self,
        query: str,
        limit: int = 10,
        *,
        hypothesis: str | None = None,
        stance: str = "support",
        event_date: str = "",
        min_evidentiality: float = 0.0,
    ) -> list[RetrievedChunk]:
        """检索 chunk,可选按证据性重排 + Cross-Encoder 精排。

        Args:
            query: 检索关键词。
            limit: 返回上限。
            hypothesis: 当前主张文本。提供时启用证据性重排(Step 6)。
            stance: 期望立场 support/counter。
            event_date: 事件日期,用于时间窗口打分。
            min_evidentiality: 证据性最低总分阈值,低于则过滤。
        """
        chunks = self.chunk_store.search(query, limit=limit * 2 if hypothesis else limit)
        if not hypothesis:
            return chunks[:limit]
        from core.research_graph.evidentiality_scorer import rerank_chunks

        scored = rerank_chunks(
            chunks,
            hypothesis,
            stance=stance,
            event_date=event_date,
            min_total=min_evidentiality,
        )
        result_chunks = [chunk for chunk, _ in scored[:limit]]

        # P1 Step 10:Cross-Encoder 精排(可选)
        # 在 evidentiality_scorer 之后对 top 候选做 cross-encoder 精排,
        # 提升零样本鲁棒性。cross_encoder 为 None 时跳过,保持原有行为。
        if self.cross_encoder is not None and result_chunks:
            documents = [
                getattr(chunk, "text", "") or getattr(chunk, "content", "")
                for chunk in result_chunks
            ]
            try:
                reranked = self.cross_encoder.rerank(
                    query if not hypothesis else hypothesis,
                    documents,
                    top_k=limit,
                )
                # 按 cross-encoder 排序重排 chunks
                result_chunks = [
                    result_chunks[idx] for idx, _ in reranked
                ]
            except Exception as exc:
                logger.warning(
                    "Cross-Encoder rerank 失败,保持原排序: %s", exc
                )

        # P2 Step 14:Late-Interaction 精排(可选)
        # 在 cross-encoder 后做 ColBERT-style token-level MaxSim 精排,
        # 零样本鲁棒性优于单点 Cross-Encoder。late_interaction_encoder 为 None 时跳过。
        if self.late_interaction_encoder is not None and result_chunks:
            documents = [
                getattr(chunk, "text", "") or getattr(chunk, "content", "")
                for chunk in result_chunks
            ]
            try:
                reranked = self.late_interaction_encoder.rerank(
                    query if not hypothesis else hypothesis,
                    documents,
                    top_k=limit,
                )
                result_chunks = [
                    result_chunks[idx] for idx, _ in reranked
                ]
            except Exception as exc:
                logger.warning(
                    "Late-Interaction rerank 失败,保持原排序: %s", exc
                )

        return result_chunks


class VectorMemoryFingerprintStore:
    """LanceDB 持久化的 SimHash 指纹存储(Step 4)。

    独立表存储 (fingerprint, evidence_id),供 EvidenceNoveltyGate 跨运行去重。
    与 chunks 表分离,避免破坏现有 schema。

    SimHash 指纹是无符号 64 位整数,但 lancedb 仅支持 int64。
    因此存储前用补码转为有符号 int64,读取时还原为无符号。
    """

    FINGERPRINTS_SCHEMA = pa.schema([
        pa.field("fingerprint", pa.int64()),
        pa.field("evidence_id", pa.string()),
    ])

    def __init__(
        self,
        db_path: str | Path = "data/vector_store/research_chunks.lance",
        *,
        table_name: str = "evidence_fingerprints",
    ) -> None:
        """初始化,与 DocumentChunkStore 共享同一 lancedb 连接。

        Args:
            db_path: lancedb 路径,默认与 DocumentChunkStore 一致。
            table_name: 指纹表名。
        """
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        self._ensure_table()

    @staticmethod
    def _to_signed(fingerprint: int) -> int:
        """无符号 64 位指纹转有符号 int64。"""
        fp = int(fingerprint) & ((1 << 64) - 1)
        return fp if fp < (1 << 63) else fp - (1 << 64)

    @staticmethod
    def _to_unsigned(fingerprint: int) -> int:
        """有符号 int64 还原为无符号 64 位指纹。"""
        fp = int(fingerprint)
        return fp if fp >= 0 else fp + (1 << 64)

    def _ensure_table(self) -> None:
        existing = self.db.list_tables().tables
        if self.table_name not in existing:
            empty = pa.table(
                {f.name: [] for f in self.FINGERPRINTS_SCHEMA},
                schema=self.FINGERPRINTS_SCHEMA,
            )
            self.db.create_table(self.table_name, empty)

    def has_fingerprint(self, fingerprint: int) -> bool:
        """查询指纹是否已存在(精确匹配)。

        Args:
            fingerprint: 64 位 SimHash 指纹(无符号)。

        Returns:
            True 表示已存在相同指纹。
        """
        signed_fp = self._to_signed(fingerprint)
        table = self.db.open_table(self.table_name)
        rows = table.search().where(f"fingerprint = {signed_fp}").limit(1).to_list()
        return bool(rows)

    def add_fingerprint(self, fingerprint: int, evidence_id: str) -> None:
        """追加一条指纹记录。

        Args:
            fingerprint: 64 位 SimHash 指纹(无符号)。
            evidence_id: 对应证据 ID。
        """
        signed_fp = self._to_signed(fingerprint)
        table = self.db.open_table(self.table_name)
        arrow_table = pa.table(
            {
                "fingerprint": pa.array([signed_fp], pa.int64()),
                "evidence_id": pa.array([str(evidence_id)], pa.string()),
            },
            schema=self.FINGERPRINTS_SCHEMA,
        )
        table.add(arrow_table)

    def all_fingerprints(self) -> list[tuple[int, str]]:
        """返回全部已登记指纹(供冷启动加载到内存,返回无符号指纹)。"""
        table = self.db.open_table(self.table_name)
        rows = table.search().to_list()
        return [(self._to_unsigned(r["fingerprint"]), str(r.get("evidence_id", ""))) for r in rows]


def _terms(text: str) -> set[str]:
    normalized = (text or "").lower()
    return {part for part in normalized.replace("/", " ").replace("-", " ").split() if part}


def _keyword_score(query_terms: set[str], chunk: DocumentChunk) -> float:
    if not query_terms:
        return 0.0
    haystack = " ".join(
        [chunk.text, chunk.topic, chunk.source_type, chunk.source_id, chunk.document_id]
    ).lower()
    hits = sum(1 for term in query_terms if term in haystack)
    return min(1.0, hits / len(query_terms))


def _source_authority_score(source_type: str) -> float:
    return {
        "announcement": 1.0,
        "annual_report": 1.0,
        "financial_report": 1.0,
        "official": 0.9,
        "research_report": 0.8,
        "news": 0.6,
    }.get((source_type or "").lower(), 0.4)


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    if left.size == 0 or right.size == 0 or left.shape != right.shape:
        return 0.0
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(left, right) / denom)))
