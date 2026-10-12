"""高性价比关键词倒排索引缓存。

设计目标:
- 用更少内存、更快匹配速度替代全量遍历候选节点。
- 按中文单字/英文 token 构建倒排索引,避免依赖外部分词库。
- 支持增量更新,候选节点增删时无需重建整个索引。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from core.research_graph.candidate_graph import CandidateNode
from core.research_graph.candidate_schema import is_exploration_visible

logger = logging.getLogger(__name__)

# 简单停用词集合,避免过多无意义 token
_STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "和", "是", "在", "有", "被", "为", "对", "与", "及", "等", "或",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
})


class KeywordMatchIndex:
    """候选节点关键词倒排索引。

    索引结构:
      token -> set(candidate_id)

    查询流程:
      1. 从证据文本提取 token
      2. 取 token 命中的 candidate_id 并集作为候选集(通常 <5% 总节点)
      3. 对候选集执行精细匹配(char coverage / bigram / 伪证据等)
    """

    def __init__(self) -> None:
        """初始化空索引。"""
        self._index: dict[str, set[str]] = defaultdict(set)
        self._node_keywords: dict[str, set[str]] = {}

    @staticmethod
    def extract_tokens(text: str) -> set[str]:
        """从文本中提取索引 token。

        规则:
        - 中文:单字 token + 连续中文字符的 bigram(2 字组合)
        - 英文/数字:长度 >=2 的 token
        - 过滤停用词和纯数字
        """
        if not text:
            return set()

        tokens: set[str] = set()
        lowered = text.lower()

        # 中文单字 + bigram
        chinese = re.findall(r"[\u4e00-\u9fff]+", lowered)
        for seq in chinese:
            for ch in seq:
                if ch not in _STOPWORDS:
                    tokens.add(ch)
            for i in range(len(seq) - 1):
                bigram = seq[i : i + 2]
                if bigram not in _STOPWORDS:
                    tokens.add(bigram)

        # 英文/数字 token
        en_tokens = re.findall(r"[a-z0-9]+", lowered)
        for tok in en_tokens:
            if len(tok) >= 2 and not tok.isdigit() and tok not in _STOPWORDS:
                tokens.add(tok)

        return tokens

    def add_node(self, node: CandidateNode) -> None:
        """向索引添加/更新单个候选节点。"""
        if not is_exploration_visible(node.status):
            self.remove_node(node.candidate_id)
            return

        # 收集节点所有文本来源
        texts = [node.name, node.description]
        texts.extend(node.aliases)
        if node.attributes and isinstance(node.attributes, dict):
            texts.extend(str(v) for v in node.attributes.values() if isinstance(v, (str, int, float)))

        keywords: set[str] = set()
        for text in texts:
            keywords.update(self.extract_tokens(str(text)))

        # 移除旧索引
        self.remove_node(node.candidate_id)

        self._node_keywords[node.candidate_id] = keywords
        for kw in keywords:
            self._index[kw].add(node.candidate_id)

    def remove_node(self, candidate_id: str) -> None:
        """从索引移除节点。"""
        old_keywords = self._node_keywords.pop(candidate_id, set())
        for kw in old_keywords:
            self._index[kw].discard(candidate_id)
            if not self._index[kw]:
                del self._index[kw]

    def build(self, nodes: list[CandidateNode]) -> None:
        """从候选节点列表批量构建索引。"""
        self._index.clear()
        self._node_keywords.clear()
        for node in nodes:
            self.add_node(node)
        logger.debug("KeywordMatchIndex: 已索引 %d 个节点, %d 个 token", len(self._node_keywords), len(self._index))

    def query(self, text: str) -> set[str]:
        """根据文本查询可能相关的 candidate_id 集合。"""
        tokens = self.extract_tokens(text)
        if not tokens:
            return set()

        result: set[str] | None = None
        for token in tokens:
            hits = self._index.get(token)
            if not hits:
                continue
            if result is None:
                result = set(hits)
            else:
                result |= hits

        return result if result is not None else set()

    def query_ranked(self, text: str, top_k: int = 0) -> list[tuple[str, int]]:
        """返回按命中 token 数排序的候选节点。

        Args:
            text: 查询文本。
            top_k: 返回前 K 个,0 表示全部。

        Returns:
            (candidate_id, hit_count) 列表,按命中数降序。
        """
        tokens = self.extract_tokens(text)
        if not tokens:
            return []

        scores: dict[str, int] = defaultdict(int)
        for token in tokens:
            for cid in self._index.get(token, set()):
                scores[cid] += 1

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        if top_k > 0:
            ranked = ranked[:top_k]
        return ranked

    def is_empty(self) -> bool:
        """索引是否为空。"""
        return len(self._node_keywords) == 0

    def stats(self) -> dict[str, Any]:
        """返回索引统计信息。"""
        return {
            "nodes": len(self._node_keywords),
            "tokens": len(self._index),
            "avg_keywords_per_node": (
                sum(len(v) for v in self._node_keywords.values()) / len(self._node_keywords)
                if self._node_keywords else 0.0
            ),
        }
