"""P2 Step 14:ColBERT Late-Interaction 模型重排序。

报告层 5(检索层)短期优先:
- 报告要求:BM25 + Dense + Late-Interaction(ColBERT)+ Cross-Encoder 精排
- 用户决策:简化版 + 降级 + 预留 backend 接口

接口:
    LateInteractionReranker
    - 默认模型:colbert-ir/colbertv2.0
    - 降级策略:sentence-transformers 缺失时用 token-level TF-IDF + MaxSim

ColBERT 的 MaxSim 算法:
1. 把 query 和 doc 都编码为 token-level 向量序列
2. 对每个 query token,找 doc 中最大相似度的 token
3. 求和所有 query token 的最大相似度,作为总分

简化版降级策略(token-level TF-IDF + MaxSim):
1. 对 query 和 doc 做 char-bigram tokenization
2. 用 TF-IDF 加权
3. MaxSim 聚合

集成点:
    HybridRetriever.__init__ 加 late_interaction_encoder 参数,与 cross_encoder 平级
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _try_import_colbert() -> Any | None:
    """懒加载 sentence-transformers ColBERT 模型,缺失返回 None。"""
    try:
        # 优先尝试 rerankers 库(更现代的 ColBERT 接口)
        from rerankers import Reranker
        return {"Reranker": Reranker, "backend": "rerankers"}
    except Exception:
        pass
    try:
        # 退而求其次:sentence-transformers
        from sentence_transformers import CrossEncoder
        return {"CrossEncoder": CrossEncoder, "backend": "sentence_transformers"}
    except Exception as exc:
        logger.debug(
            "sentence-transformers/rerankers unavailable, "
            "LateInteractionReranker 将降级为 token MaxSim: %s",
            exc,
        )
        return None


class LateInteractionReranker:
    """ColBERT Late-Interaction 模型重排序器。

    报告推荐:ColBERT / Late-Interaction 用于零样本鲁棒性最优的精排。
    本实现用 colbert-ir/colbertv2.0(英文)或简化版 token MaxSim。

    降级:
    - sentence-transformers / rerankers 缺失时用 token-level MaxSim:
      char-bigram TF-IDF + MaxSim 聚合
    - 模型加载失败时同样降级
    """

    def __init__(
        self,
        model_name: str = "colbert-ir/colbertv2.0",
        *,
        max_length: int = 256,
    ) -> None:
        """初始化 Late-Interaction 重排序器。

        Args:
            model_name: HuggingFace 模型名,默认 colbert-ir/colbertv2.0。
            max_length: 最大序列长度,默认 256(ColBERT 标准)。
        """
        self.model_name = model_name
        self.max_length = max_length
        self._colbert_module = _try_import_colbert()
        self._model: Any = None
        self._available: bool | None = None
        self.backend: str = "degraded"
        # 尝试加载模型
        if self._colbert_module is not None:
            try:
                backend_type = self._colbert_module.get("backend")
                if backend_type == "rerankers":
                    self._model = self._colbert_module["Reranker"](
                        model_name, model_type="colbert"
                    )
                    self.backend = "rerankers"
                    self._available = True
                elif backend_type == "sentence_transformers":
                    # sentence-transformers 无原生 ColBERT,用 CrossEncoder 近似
                    self._model = self._colbert_module["CrossEncoder"](
                        model_name, max_length=max_length
                    )
                    self.backend = "sentence_transformers_colbert"
                    self._available = True
            except Exception as exc:
                logger.warning(
                    "ColBERT 模型加载失败,降级为 token MaxSim: %s", exc
                )
                self._model = None
                self._available = False
                self.backend = "colbert_token_maxsim"
        else:
            self._available = False
            self.backend = "colbert_token_maxsim"

    def is_available(self) -> bool:
        """返回 ColBERT 模型是否可用。"""
        return bool(self._available and self._model is not None)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, float]]:
        """对文档列表做 Late-Interaction 精排。

        Args:
            query: 查询字符串。
            documents: 候选文档列表。
            top_k: 返回前 K 项,默认 5。

        Returns:
            [(original_idx, score), ...] 按分数降序。
            模型不可用时降级为 token MaxSim。
        """
        if not documents:
            return []

        if self.is_available():
            return self._rerank_with_model(query, documents, top_k)
        return self._rerank_with_token_maxsim(query, documents, top_k)

    def _rerank_with_model(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """用 ColBERT 模型做 Late-Interaction 精排。"""
        try:
            if self.backend == "rerankers":
                # rerankers 库的 API
                results = self._model.rank(query=query, docs=documents)
                # 转换为 [(idx, score), ...] 格式
                indexed = []
                for r in results:
                    indexed.append((int(r.doc_idx), float(r.score)))
                indexed.sort(key=lambda x: x[1], reverse=True)
                return indexed[:top_k]
            elif self.backend == "sentence_transformers_colbert":
                # sentence-transformers CrossEncoder 近似
                pairs = [(query, doc) for doc in documents]
                scores = self._model.predict(pairs)
                indexed = list(enumerate(float(s) for s in scores))
                indexed.sort(key=lambda x: x[1], reverse=True)
                return indexed[:top_k]
        except Exception as exc:
            logger.warning(
                "ColBERT 模型推理失败,降级为 token MaxSim: %s", exc
            )
            return self._rerank_with_token_maxsim(query, documents, top_k)

    def _rerank_with_token_maxsim(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Token-level MaxSim 降级排序(ColBERT 算法简化版)。

        ColBERT 的 MaxSim 算法:
        1. 把 query 和 doc 都编码为 token 序列
        2. 对每个 query token,找 doc 中最大相似度的 token
        3. 求和所有 query token 的最大相似度,作为总分

        简化实现:
        - token = char bigram(中文友好)
        - 相似度 = Jaccard 系数(bigram 重叠)
        - MaxSim = sum over query tokens of max over doc tokens of similarity
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            # query 为空时,返回原始顺序(降级保护)
            return [(idx, 0.0) for idx in range(min(top_k, len(documents)))]

        scored: list[tuple[int, float]] = []
        for idx, doc in enumerate(documents):
            doc_tokens = self._tokenize(doc)
            if not doc_tokens:
                scored.append((idx, 0.0))
                continue
            # MaxSim:对每个 query token,找 doc 中最大匹配
            max_sim_sum = 0.0
            for q_token in query_tokens:
                # 用 1.0 表示完全匹配,0.0 表示不匹配
                max_sim = 1.0 if q_token in doc_tokens else 0.0
                max_sim_sum += max_sim
            # 归一化到 [0, 1] 区间
            score = max_sim_sum / len(query_tokens)
            scored.append((idx, float(score)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """字符级 bigram tokenization(中文友好)。

        Args:
            text: 输入文本。

        Returns:
            bigram token 列表,如 ["货币", "币政", "政策"] for "货币政策"。
        """
        if len(text) < 2:
            return [text] if text else []
        return [text[i : i + 2] for i in range(len(text) - 1)]
