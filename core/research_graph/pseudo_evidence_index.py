"""检索假设向量索引：为 CandidateNode 的检索假设构建可增量更新的向量索引。

方案6核心组件（已改造）：
- 使用轻量中文句子嵌入模型 BAAI/bge-small-zh-v1.5（约80MB，CPU友好）
- 每个候选节点的检索假设编码为向量，存入 FAISS 索引
- 在线证据匹配时，用证据文本向量检索最相似的检索假设，返回对应候选节点
- 检索假设仅用于查询构造，**禁止**作为真实证据写入 evidence_slots

设计原则：
- 模型懒加载：首次调用时才加载 sentence-transformers 和 FAISS
- 增量更新：支持 add_node/add_nodes 动态加入新候选节点，不重建索引
- 内容哈希缓存：同一文本只编码一次，避免重复编码
- 零 LLM：在线检索阶段不调用任何 LLM
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import numpy as np

from core.research_graph.retrieval_hypothesis import read_hypotheses

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_MATCH_THRESHOLD = 0.5


class PseudoEvidenceIndex:
    """基于检索假设的向量索引（兼容旧 pseudo_evidence 字段只读读取）。"""

    def __init__(
        self,
        embed_model: str = DEFAULT_EMBED_MODEL,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> None:
        """初始化索引（不立即加载模型）。

        Args:
            embed_model: 句子嵌入模型名。
            match_threshold: 视为命中的最小余弦相似度。
        """
        self.embed_model_name = embed_model
        self.match_threshold = match_threshold
        self._model: Any | None = None
        self._index: Any | None = None
        # 每条检索假设对应一个候选节点
        self._entries: list[dict[str, Any]] = []
        # text_hash -> embedding 缓存，避免同一文本重复编码
        self._embedding_cache: dict[str, np.ndarray] = {}
        # candidate_id -> set(text_hash)，用于快速判断节点是否已加入
        self._candidate_hashes: dict[str, set[str]] = {}

    def _load_model(self) -> Any:
        """懒加载嵌入模型。"""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading pseudo-evidence embedding model: %s", self.embed_model_name)
            self._model = SentenceTransformer(self.embed_model_name)
        return self._model

    @staticmethod
    def _text_hash(text: str) -> str:
        """计算文本的内容哈希。"""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _collect_hypotheses(self, node: Any) -> list[tuple[str, str]]:
        """从节点 attributes 收集检索假设文本与哈希。

        优先读取 retrieval_hypotheses，兼容旧 pseudo_evidence。
        """
        texts = read_hypotheses(node.attributes)
        return [(text, self._text_hash(text)) for text in texts if text]

    def add_node(self, node: Any) -> int:
        """把单个候选节点的检索假设增量加入索引。

        Args:
            node: CandidateNode 实例，要求 attributes 中包含 retrieval_hypotheses 列表。

        Returns:
            新加入索引的检索假设条数。
        """
        return self.add_nodes([node])

    def add_nodes(self, nodes: list[Any]) -> int:
        """批量增量加入多个节点，仅编码新增文本，避免全量重编码。

        Args:
            nodes: CandidateNode 实例列表。

        Returns:
            新加入索引的检索假设条数。
        """
        if not nodes:
            return 0

        # 收集所有待编码的检索假设文本，利用内容哈希去重
        texts_to_encode: list[str] = []
        encode_hashes: list[str] = []
        # (candidate_id, node_name, text_hash) 用于后续写入 entries
        pending_entries: list[tuple[str, str, str]] = []

        for node in nodes:
            candidate_id = node.candidate_id
            existing_hashes = self._candidate_hashes.setdefault(candidate_id, set())
            for text, text_hash in self._collect_hypotheses(node):
                if text_hash in existing_hashes:
                    continue
                existing_hashes.add(text_hash)
                pending_entries.append((candidate_id, node.name, text_hash))
                if text_hash not in self._embedding_cache:
                    texts_to_encode.append(text)
                    encode_hashes.append(text_hash)

        if not pending_entries:
            return 0

        # 仅对未缓存的文本进行编码
        if texts_to_encode:
            try:
                model = self._load_model()
            except ImportError:
                # 无 sentence_transformers 时降级：
                # 记录 entries 元数据但不编码向量，search 返回空。
                for candidate_id, node_name, text_hash in pending_entries:
                    self._entries.append({
                        "candidate_id": candidate_id,
                        "node_name": node_name,
                        "text_hash": text_hash,
                        "embedding_idx": -1,
                    })
                logger.warning(
                    "sentence_transformers 未安装，PseudoEvidenceIndex 降级为"
                    "仅记录元数据，search 将返回空结果。"
                    "真实流水线使用 KeywordMatchIndex 不受影响。",
                )
                return len(pending_entries)

            embeddings = model.encode(
                texts_to_encode,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=min(64, len(texts_to_encode)),
            )
            for text_hash, embedding in zip(encode_hashes, embeddings):
                self._embedding_cache[text_hash] = embedding

        # 按 entries 顺序取出对应 embedding 并入索引
        start_idx = len(self._entries)
        matrix: list[np.ndarray] = []
        for i, (candidate_id, node_name, text_hash) in enumerate(pending_entries):
            self._entries.append({
                "candidate_id": candidate_id,
                "node_name": node_name,
                "text_hash": text_hash,
                "embedding_idx": start_idx + i,
            })
            matrix.append(self._embedding_cache[text_hash])

        if self._index is None:
            import faiss

            dim = matrix[0].shape[0]
            self._index = faiss.IndexFlatIP(dim)

        self._index.add(np.asarray(matrix, dtype=np.float32))
        return len(pending_entries)

    def search(
        self,
        text: str,
        top_k: int = 10,
        score_threshold: float | None = None,
    ) -> list[tuple[str, float]]:
        """用文本检索最相关的候选节点。

        Args:
            text: 待匹配的证据文本。
            top_k: 返回 Top-K 候选节点。
            score_threshold: 可选相似度阈值过滤。

        Returns:
            列表，元素为 (candidate_id, score)。同一节点可能出现多次（来自不同检索假设），
            调用方应按 candidate_id 聚合取最大分数。
        """
        if self._index is None or not self._entries:
            return []

        threshold = score_threshold if score_threshold is not None else self.match_threshold
        model = self._load_model()
        embedding = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
        distances, indices = self._index.search(np.asarray(embedding, dtype=np.float32), top_k)

        results: list[tuple[str, float]] = []
        for rank_idx, doc_idx in enumerate(indices[0]):
            score = float(distances[0][rank_idx])
            if score < threshold:
                continue
            entry = self._entries[int(doc_idx)]
            results.append((entry["candidate_id"], score))
        return results

    def is_empty(self) -> bool:
        """索引是否为空。"""
        return self._index is None or self._index.ntotal == 0

    def stats(self) -> dict[str, Any]:
        """返回索引统计（用于性能对比）。"""
        return {
            "entries": len(self._entries),
            "unique_text_hashes": len(self._embedding_cache),
            "indexed_nodes": len(self._candidate_hashes),
            "model_loaded": self._model is not None,
        }
