"""Evidence Grounding 校验：验证 LLM 抽取的 quote 是否真实存在于召回文档。

工作流：
    1. 优先字符串包含匹配（支持子串匹配，O(n) 高效）
    2. 字符串不匹配时降级到 TF-IDF cosine 相似度
    3. 若 sentence-transformers 可用，可进一步使用 embedding 相似度

设计原则：
    - 字符串匹配优先，避免不必要的 embedding 计算
    - embedding 降级可选：依赖不可用时只用字符串匹配
    - quote 或 document_text 为空时直接返回 False
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def verify_quote_grounding(
    quote: str,
    document_text: str,
    similarity_threshold: float = 0.85,
) -> bool:
    """验证 quote 是否存在于文档原文中。

    优先字符串匹配（包含关系），不匹配时降级到 embedding 相似度。
    quote 或 document_text 为空时返回 False。

    Args:
        quote: 待验证的引用文本（LLM 抽取的原文片段）。
        document_text: 召回文档的全文文本。
        similarity_threshold: 相似度阈值，默认 0.85。
            字符串包含匹配直接通过；embedding 降级时需达到此阈值。

    Returns:
        True 表示 quote 可在 document_text 中找到支撑；False 表示无支撑。
    """
    # 防御性编程：空输入直接拒绝
    if not quote or not document_text:
        return False

    quote_clean = quote.strip()
    doc_clean = document_text.strip()
    if not quote_clean or not doc_clean:
        return False

    # 1. 优先：字符串包含匹配（子串匹配，O(n) 高效）
    if quote_clean in doc_clean:
        return True

    # 2. 降级：TF-IDF cosine 相似度
    # 仅在字符串不匹配时计算，避免无谓开销
    similarity = _compute_similarity(quote_clean, doc_clean)
    if similarity >= similarity_threshold:
        return True

    return False


def _compute_similarity(text_a: str, text_b: str) -> float:
    """计算两段文本的相似度，优先使用 sentence-transformers embedding。

    降级顺序：
        1. sentence-transformers（若可用）cosine 相似度
        2. sklearn TF-IDF cosine 相似度
        3. 字符 n-gram Jaccard 相似度（无依赖兜底）

    Args:
        text_a: 文本 A。
        text_b: 文本 B。

    Returns:
        相似度分数 [0.0, 1.0]。
    """
    # 2.1 尝试 sentence-transformers embedding（可选依赖）
    similarity = _embedding_similarity(text_a, text_b)
    if similarity is not None:
        return similarity

    # 2.2 尝试 sklearn TF-IDF cosine 相似度
    similarity = _tfidf_cosine_similarity(text_a, text_b)
    if similarity is not None:
        return similarity

    # 2.3 兜底：字符 bigram Jaccard 相似度（无外部依赖）
    return _char_ngram_jaccard(text_a, text_b, n=2)


def _embedding_similarity(text_a: str, text_b: str) -> float | None:
    """使用 sentence-transformers 计算 embedding cosine 相似度。

    依赖不可用时返回 None，由上层降级处理。

    Args:
        text_a: 文本 A。
        text_b: 文本 B。

    Returns:
        cosine 相似度 [0.0, 1.0]；依赖不可用时返回 None。
    """
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None

    try:
        # 使用轻量级多语言模型；模型加载失败时降级
        model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        embeddings = model.encode([text_a, text_b], normalize_embeddings=True)
        vec_a = np.array(embeddings[0])
        vec_b = np.array(embeddings[1])
        # 已归一化，点积即为 cosine 相似度
        cosine = float(np.dot(vec_a, vec_b))
        # 裁剪到 [0.0, 1.0]（cosine 可能因数值误差略超 1 或为负）
        return max(0.0, min(1.0, cosine))
    except Exception as exc:
        logger.debug("sentence-transformers embedding 计算失败，降级: %s", exc)
        return None


def _tfidf_cosine_similarity(text_a: str, text_b: str) -> float | None:
    """使用 sklearn TF-IDF 计算 cosine 相似度。

    依赖不可用时返回 None，由上层降级处理。

    Args:
        text_a: 文本 A。
        text_b: 文本 B。

    Returns:
        cosine 相似度 [0.0, 1.0]；依赖不可用时返回 None。
    """
    try:
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        return None

    try:
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))
        tfidf_matrix = vectorizer.fit_transform([text_a, text_b])
        # cosine 相似度 = dot(a, b) / (||a|| * ||b||)
        vec_a = tfidf_matrix[0]
        vec_b = tfidf_matrix[1]
        dot_product = float((vec_a.multiply(vec_b)).sum())
        norm_a = float(np.sqrt(vec_a.multiply(vec_a).sum()))
        norm_b = float(np.sqrt(vec_b.multiply(vec_b).sum()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        cosine = dot_product / (norm_a * norm_b)
        return max(0.0, min(1.0, cosine))
    except Exception as exc:
        logger.debug("TF-IDF 相似度计算失败，降级: %s", exc)
        return None


def _char_ngram_jaccard(text_a: str, text_b: str, n: int = 2) -> float:
    """字符 n-gram Jaccard 相似度（无外部依赖兜底方案）。

    Args:
        text_a: 文本 A。
        text_b: 文本 B。
        n: n-gram 长度，默认 2（bigram）。

    Returns:
        Jaccard 相似度 [0.0, 1.0]。
    """
    if len(text_a) < n or len(text_b) < n:
        # 文本过短时退化为字符集 Jaccard
        set_a = set(text_a)
        set_b = set(text_b)
    else:
        set_a = {text_a[i:i + n] for i in range(len(text_a) - n + 1)}
        set_b = {text_b[i:i + n] for i in range(len(text_b) - n + 1)}

    if not set_a or not set_b:
        return 0.0

    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def find_grounded_document(
    quote: str,
    documents: list[Any],
    similarity_threshold: float = 0.85,
) -> tuple[int, Any] | None:
    """在文档列表中找到第一个支撑 quote 的文档。

    遍历所有召回文档，按顺序调用 verify_quote_grounding 校验，
    返回第一个通过校验的文档索引与文档对象。

    Args:
        quote: 待验证的引用文本。
        documents: 召回文档列表（RetrievedChunk 或 DocumentChunk）。
        similarity_threshold: 相似度阈值。

    Returns:
        (index, document) 元组；无文档匹配时返回 None。
    """
    if not quote or not documents:
        return None

    for idx, doc in enumerate(documents):
        # 兼容 RetrievedChunk（含 chunk 属性）和 DocumentChunk（直接含 text）
        chunk = getattr(doc, "chunk", doc)
        doc_text = getattr(chunk, "text", "") or ""
        if verify_quote_grounding(quote, doc_text, similarity_threshold):
            return idx, doc

    return None
