"""P1 Step 10:Cross-Encoder 模型重排序(检索层精排)。

报告层 5(检索层)短期优先:
- 报告要求:BM25 + Dense + Late-Interaction(ColBERT)+ Cross-Encoder 精排
- 用户决策:全部做完,保持懒加载降级策略

接口:
    CrossEncoderReranker
    - 默认模型:BAAI/bge-reranker-base
    - 降级策略:sentence-transformers 缺失时用规则排序(关键词匹配度 + 文档长度)

集成点:
    DualRetriever.retrieve() 在 evidentiality_scorer 后加 cross-encoder 精排

依赖:sentence-transformers.CrossEncoder(懒加载,缺失时降级)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _try_import_cross_encoder() -> Any | None:
    """懒加载 sentence-transformers CrossEncoder,缺失返回 None。"""
    try:
        from sentence_transformers import CrossEncoder
        return CrossEncoder
    except Exception as exc:
        logger.debug(
            "sentence-transformers unavailable, "
            "CrossEncoderReranker 将降级为规则排序: %s",
            exc,
        )
        return None


class CrossEncoderReranker:
    """Cross-Encoder 模型重排序器。

    报告推荐:ColBERT / Cross-Encoder 用于零样本鲁棒性最优的精排。
    本实现用 BAAI/bge-reranker-base(中文友好,轻量)。

    降级:
    - sentence-transformers 缺失时用规则排序:
      关键词匹配度(Jaccard 相似度)+ 文档长度归一化
    - model_name 无效时同样降级
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        *,
        max_length: int = 512,
    ) -> None:
        """初始化 Cross-Encoder 重排序器。

        Args:
            model_name: HuggingFace 模型名,默认 BAAI/bge-reranker-base。
            max_length: 最大序列长度,默认 512。
        """
        self.model_name = model_name
        self.max_length = max_length
        self._cross_encoder_cls = _try_import_cross_encoder()
        self._model: Any = None
        self._available: bool | None = None
        # 尝试加载模型(可能因网络/磁盘失败,失败时降级)
        if self._cross_encoder_cls is not None:
            try:
                self._model = self._cross_encoder_cls(
                    model_name, max_length=max_length
                )
                self._available = True
            except Exception as exc:
                logger.warning(
                    "CrossEncoder 模型加载失败,降级为规则排序: %s", exc
                )
                self._model = None
                self._available = False
        else:
            self._available = False

    def is_available(self) -> bool:
        """返回 Cross-Encoder 模型是否可用。"""
        return bool(self._available and self._model is not None)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, float]]:
        """对文档列表做 Cross-Encoder 精排。

        Args:
            query: 查询字符串。
            documents: 候选文档列表。
            top_k: 返回前 K 项,默认 5。

        Returns:
            [(original_idx, score), ...] 按分数降序。
            模型不可用时降级为规则排序。
        """
        if not documents:
            return []

        if self.is_available():
            return self._rerank_with_model(query, documents, top_k)
        return self._rerank_with_rules(query, documents, top_k)

    def _rerank_with_model(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """用 Cross-Encoder 模型打分精排。"""
        try:
            # 构造 (query, doc) pairs
            pairs = [(query, doc) for doc in documents]
            scores = self._model.predict(pairs)
            # 按分数降序排序
            indexed = list(enumerate(float(s) for s in scores))
            indexed.sort(key=lambda x: x[1], reverse=True)
            return indexed[:top_k]
        except Exception as exc:
            logger.warning(
                "Cross-Encoder 模型推理失败,降级为规则排序: %s", exc
            )
            return self._rerank_with_rules(query, documents, top_k)

    def _rerank_with_rules(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """规则降级排序:关键词匹配度(Jaccard)+ 文档长度归一化。

        策略:
        1. 对 query 和 doc 做字符级 bigram Jaccard 相似度
        2. 加上关键词命中数(中文按字切分)
        3. 归一化到 [0, 1] 区间
        """
        query_chars = set(query)
        query_bigrams = self._char_bigrams(query)

        scored: list[tuple[int, float]] = []
        for idx, doc in enumerate(documents):
            # 1. Jaccard 相似度(bigram)
            doc_bigrams = self._char_bigrams(doc)
            if query_bigrams and doc_bigrams:
                intersection = len(query_bigrams & doc_bigrams)
                union = len(query_bigrams | doc_bigrams)
                jaccard = intersection / union if union > 0 else 0.0
            else:
                jaccard = 0.0

            # 2. 关键词命中数(query 字符在 doc 中出现的比例)
            doc_chars = set(doc)
            char_hits = len(query_chars & doc_chars) / max(
                len(query_chars), 1
            )

            # 3. 综合分数:Jaccard * 0.6 + char_hits * 0.4
            score = jaccard * 0.6 + char_hits * 0.4
            scored.append((idx, float(score)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _char_bigrams(text: str) -> set[str]:
        """构造字符级 bigram 集合(中文友好)。

        Args:
            text: 输入文本。

        Returns:
            bigram 字符串集合,如 {"货币", "币政", "政策"} for "货币政策"。
        """
        if len(text) < 2:
            return {text} if text else set()
        return {text[i : i + 2] for i in range(len(text) - 1)}
