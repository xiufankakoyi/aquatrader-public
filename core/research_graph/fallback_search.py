"""无 SearXNG 时的搜索 fallback：DuckDuckGo HTML + BM25 重排序 + 规则过滤。

设计方案:
  1. 用 DuckDuckGo HTML endpoint 获取原始结果（无 API key、国内可访问）。
  2. 用 rank_bm25 对结果按 query 相关性二次排序，解决 DDG 返回顺序不一定最优的问题。
  3. 用规则过滤库剔除低质量/无关/广告站点（参考 perilla_leaf_screener）。
  4. 输出 SearchDiscovery 同构的 SourceDocument 列表，供 WebSearcher 消费。

依赖:
  pip install duckduckgo_search rank_bm25
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.evidence_miner import SourceDocument

logger = logging.getLogger(__name__)

# 低质量 URL 过滤规则（参考 perilla_leaf_screener）
_LOW_QUALITY_URL_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I)
    for p in [
        r"baike\.baidu\.com",
        r"zhidao\.baidu\.com",
        r"wenku\.baidu\.com",
        r"tieba\.baidu\.com",
        r"baike\.so\.com",
        r"baike\.sogou\.com",
        r"wikipedia\.org",
        r"zhihu\.com/question",
        r"zhuanlan\.zhihu\.com/p/\d+$",
        r"douban\.com",
        r"dxy\.com",
        r"v\.qq\.com",
        r"tencent\.com",
        r"amap\.com",
        r"news\.qq\.com/rain",
        r"sohu\.com/a/\d+",
        r"163\.com/dy/article",
        r"site\.ip138\.com",
        r"consumer\.huawei\.com",
        r"avdioes\.com",
        r"chagushici\.com",
        r"hgcha\.com/zidian",
        r"hancibao\.com/zi/",
        r"gushici\.net/\d",
        r"zidian\.gushici\.net",
    ]
]

# 标题 snippet 中常见的低质量信号
_LOW_QUALITY_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I)
    for p in [
        r"^[^\u4e00-\u9fff]{0,2}广告",
        r"推广链接",
        r"优惠券",
        r"招聘",
        r"股吧",
    ]
]


@dataclass
class FallbackSearchConfig:
    """Fallback 搜索配置。"""

    max_results_per_query: int = 8
    bm25_top_k: int = 20
    min_snippet_length: int = 12
    min_title_length: int = 4
    throttle_seconds: float = 0.8
    max_retries: int = 2
    user_region: str = "cn-zh"


class FallbackWebSearcher:
    """不依赖 SearXNG/Bing 的搜索封装：DDG + BM25 + 规则过滤。

    Args:
        topic: 研究主题，用于填充 SourceDocument.topic。
        config: FallbackSearchConfig 实例。
    """

    def __init__(self, topic: str, config: FallbackWebSearcher | None = None) -> None:
        self.topic = topic
        self.config = config or FallbackSearchConfig()
        self._ddgs: Any | None = None
        self._bm25_available: bool = False
        self._init_ddgs()
        self._init_bm25()

    def _init_ddgs(self) -> None:
        """延迟初始化 duckduckgo_search.DDGS。"""
        try:
            from ddgs import DDGS

            self._ddgs = DDGS()
            logger.info("FallbackWebSearcher: DDGS 已初始化")
        except Exception as exc:
            logger.warning("FallbackWebSearcher: DDGS 初始化失败: %s", exc)
            self._ddgs = None

    def _init_bm25(self) -> None:
        """尝试加载 rank_bm25。"""
        try:
            import rank_bm25

            self._bm25_class = rank_bm25.BM25Okapi
            self._bm25_available = True
            logger.info("FallbackWebSearcher: BM25 已初始化")
        except Exception as exc:
            logger.warning("FallbackWebSearcher: rank_bm25 不可用: %s", exc)
            self._bm25_available = False

    def search(self, query: str) -> list[SourceDocument]:
        """执行单次 fallback 搜索。

        Args:
            query: 搜索 query。

        Returns:
            SourceDocument 列表，按 BM25 重排后的 top_k 结果。
        """
        if not query or not query.strip():
            return []

        raw = self._search_ddg(query)
        if not raw:
            return []

        filtered = [item for item in raw if self._is_quality_result(item)]
        ranked = self._rerank_bm25(query, filtered)
        return [
            SourceDocument(
                document_id=f"ddg_{hash(item.get('href') or item.get('url', '')) & 0xFFFFFFFF:08x}",
                source_type="search_result",
                title=item.get("title", ""),
                content=item.get("body", ""),
                publish_date="",
                url=item.get("href") or item.get("url", ""),
                source_id="duckduckgo",
                topic=self.topic,
                summary=(item.get("body") or "")[:200],
                metadata={
                    "query": query,
                    "credibility_hint": "unknown",
                    "mentioned_chain_nodes": [],
                    "mentioned_entities": [],
                    "possible_relation_types": [],
                    "evidence_strength_hint": "SPECULATIVE",
                    "search_backend": "ddg_fallback",
                },
            )
            for item in ranked[: self.config.max_results_per_query]
        ]

    def search_many(self, queries: list[str]) -> list[SourceDocument]:
        """批量搜索并去重。"""
        import time

        seen_urls: set[str] = set()
        all_docs: list[SourceDocument] = []
        for query in queries:
            docs = self.search(query)
            for doc in docs:
                key = (doc.url or doc.document_id).lower()
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                all_docs.append(doc)
            time.sleep(self.config.throttle_seconds)
        return all_docs

    def _search_ddg(self, query: str) -> list[dict[str, str]]:
        """调用 DDGS 文本搜索，带重试。"""
        if self._ddgs is None:
            return []

        import time

        for attempt in range(self.config.max_retries + 1):
            try:
                results = self._ddgs.text(
                    query,
                    region=self.config.user_region,
                    safesearch="off",
                    max_results=self.config.bm25_top_k,
                )
                if results:
                    return results
            except Exception as exc:
                logger.debug("DDG 搜索失败 '%s' (attempt %d): %s", query, attempt + 1, exc)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.throttle_seconds * (attempt + 1))
        return []

    def _is_quality_result(self, item: dict[str, str]) -> bool:
        """规则过滤：URL 黑名单 + 长度 + 标题信号。"""
        url = item.get("href") or ""
        title = item.get("title") or ""
        body = item.get("body") or ""

        if not url.startswith(("http://", "https://")):
            return False
        if len(title) < self.config.min_title_length or len(body) < self.config.min_snippet_length:
            return False
        for pat in _LOW_QUALITY_URL_PATTERNS:
            if pat.search(url):
                return False
        for pat in _LOW_QUALITY_TITLE_PATTERNS:
            if pat.search(title):
                return False
        return True

    def _rerank_bm25(self, query: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
        """用 BM25 对结果重排序。"""
        if not self._bm25_available or len(results) <= 1:
            return results

        try:
            tokenized_corpus = [self._tokenize(f"{r.get('title', '')} {r.get('body', '')}") for r in results]
            bm25 = self._bm25_class(tokenized_corpus)
            scores = bm25.get_scores(self._tokenize(query))
            scored = sorted(zip(results, scores), key=lambda x: x[1], reverse=True)
            return [item for item, _ in scored]
        except Exception as exc:
            logger.debug("BM25 重排失败: %s", exc)
            return results

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简单中英文分词。"""
        # 英文/数字按非词字符切分
        tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
        # 中文按单字切分
        tokens.extend(re.findall(r"[\u4e00-\u9fff]", text))
        return tokens


def main() -> None:
    """命令行快速测试。"""
    import argparse

    parser = argparse.ArgumentParser(description="测试 fallback 搜索")
    parser.add_argument("--topic", default="HBM")
    parser.add_argument("--query", default="HBM 产业链 供应商")
    parser.add_argument("--max", type=int, default=8)
    args = parser.parse_args()

    config = FallbackSearchConfig(max_results_per_query=args.max)
    searcher = FallbackWebSearcher(topic=args.topic, config=config)
    docs = searcher.search(args.query)
    print(json.dumps([{"title": d.title, "url": d.url, "summary": d.summary} for d in docs], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
