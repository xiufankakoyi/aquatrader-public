"""增强型多引擎搜索封装。

设计原则:
  1. SearXNG 优先: 生产环境仍优先走自托管 SearXNG（低延迟、多上游降级）。
  2. 多引擎聚合 fallback: SearXNG 不可用时，聚合 DuckDuckGo + Bing HTML，
     用 rank_bm25 对合并结果重排。
  3. 智能正文抓取: 不盲目抓取所有链接，而是根据摘要/标题的信息密度评分，
     只抓取最有潜力的 top 链接，避免噪声冲淡覆盖。
  4. 本地缓存: 按 query 缓存搜索结果（默认 TTL 1 小时），减少重复请求。
  5. 规则过滤: 集成 perilla 低质量 URL 黑名单和标题噪声过滤。

输出:
  与 evidence_miner.SourceDocument 同构的文档列表。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from core.research_graph.evidence_miner import SourceDocument

logger = logging.getLogger(__name__)

# 低质量 URL/标题 过滤规则
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
_LOW_QUALITY_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I)
    for p in [r"^[^\u4e00-\u9fff]{0,2}广告", r"推广链接", r"优惠券", r"招聘", r"股吧"]
]


def _is_low_quality(url: str, title: str = "") -> bool:
    """判断结果是否为低质量站点。"""
    if any(p.search(url) for p in _LOW_QUALITY_URL_PATTERNS):
        return True
    if any(p.search(title) for p in _LOW_QUALITY_TITLE_PATTERNS):
        return True
    return False


@dataclass
class SearchConfig:
    """搜索配置。"""

    searxng_base_url: str = ""
    searxng_engines: str = "bing,baidu kaifa,duckduckgo"
    use_searxng: bool = True
    max_results: int = 8
    bm25_top_k: int = 20
    fetch_article: bool = True
    max_article_fetches: int = 3
    min_title_length: int = 4
    min_snippet_length: int = 12
    cache_ttl_seconds: int = 3600
    ddg_throttle_seconds: float = 0.6
    bing_throttle_seconds: float = 0.3
    request_timeout: int = 15


class MultiEngineSearcher:
    """增强型多引擎搜索封装。

    Args:
        topic: 研究主题，填充 SourceDocument.topic。
        config: SearchConfig 实例。
        cache_dir: 本地缓存目录；None 表示禁用缓存。
    """

    def __init__(
        self,
        topic: str,
        config: SearchConfig | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        self.topic = topic
        self.config = config or SearchConfig()
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._searxng: Any = None
        self._searxng_available: bool = False
        self._ddgs: Any | None = None
        self._bm25_class: Any | None = None
        self._bm25_available: bool = False
        self._init_searxng()
        self._init_ddgs()
        self._init_bm25()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _init_searxng(self) -> None:
        """初始化 SearXNG 客户端，支持配置读取与自动端口探测。"""
        if not self.config.use_searxng:
            logger.debug("MultiEngineSearcher: SearXNG 被显式禁用")
            return

        if not self.config.searxng_base_url:
            try:
                from config.secrets import Secrets
                self.config.searxng_base_url = getattr(Secrets, "SEARXNG_BASE_URL", "")
                self.config.searxng_engines = getattr(Secrets, "SEARXNG_ENGINES", self.config.searxng_engines)
            except Exception as exc:
                logger.debug("读取 Secrets SearXNG 配置失败: %s", exc)

        if not self.config.searxng_base_url:
            try:
                from config.setting import Setting
                self.config.searxng_base_url = getattr(Setting, "SEARXNG_BASE_URL", "")
            except Exception as exc:
                logger.debug("读取 Setting SearXNG 配置失败: %s", exc)

        # 自动探测可用 SearXNG 端口(配置端口未运行时，或配置端口不可达时)
        if not self.config.searxng_base_url:
            discovered = self._discover_searxng_url()
            if discovered:
                self.config.searxng_base_url = discovered
            else:
                logger.warning("MultiEngineSearcher: 未探测到可用 SearXNG 端口")
                return
        elif not self._is_searxng_reachable(self.config.searxng_base_url):
            logger.warning(
                "MultiEngineSearcher: 配置的 SearXNG %s 不可达，尝试自动探测",
                self.config.searxng_base_url,
            )
            discovered = self._discover_searxng_url()
            if discovered:
                self.config.searxng_base_url = discovered
            else:
                logger.warning("MultiEngineSearcher: 未探测到可用 SearXNG 端口")
                return

        try:
            from core.research.perilla.discovery.search_discovery import SearchDiscovery
            self._searxng = SearchDiscovery(
                searxng_base_url=self.config.searxng_base_url,
                max_results=self.config.max_results,
                engines=self.config.searxng_engines,
                timeout=self.config.request_timeout,
            )
            self._searxng_available = self._searxng.is_available()
            if self._searxng_available:
                logger.info("MultiEngineSearcher: SearXNG 可用 %s", self.config.searxng_base_url)
        except Exception as exc:
            logger.warning("MultiEngineSearcher: SearXNG 初始化失败: %s", exc)

    @staticmethod
    def _is_searxng_reachable(base_url: str, timeout: int = 3) -> bool:
        """检查 SearXNG 根路径是否可达。"""
        if not base_url:
            return False
        try:
            import requests
            resp = requests.get(
                f"{base_url.rstrip('/')}/",
                timeout=timeout,
                headers={"User-Agent": "AquaTrade-MultiEngineSearcher/1.0"},
            )
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _discover_searxng_url() -> str:
        """自动探测本地常见 SearXNG 端口。"""
        candidates = ["http://127.0.0.1:8888", "http://127.0.0.1:8895", "http://127.0.0.1:8080"]
        for url in candidates:
            if MultiEngineSearcher._is_searxng_reachable(url):
                logger.info("MultiEngineSearcher: 自动探测到 SearXNG %s", url)
                return url
        return ""

    def _init_ddgs(self) -> None:
        """初始化 DuckDuckGo 客户端。"""
        try:
            from ddgs import DDGS
            self._ddgs = DDGS()
            logger.info("MultiEngineSearcher: DDGS 已初始化")
        except Exception as exc:
            logger.warning("MultiEngineSearcher: DDGS 初始化失败: %s", exc)
            self._ddgs = None

    def _init_bm25(self) -> None:
        """初始化 BM25。"""
        try:
            import rank_bm25
            self._bm25_class = rank_bm25.BM25Okapi
            self._bm25_available = True
        except Exception as exc:
            logger.debug("rank_bm25 不可用: %s", exc)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    @property
    def backend(self) -> str:
        """当前实际使用的后端名称。"""
        if self._searxng_available:
            return "searxng"
        if self._ddgs is not None:
            return "ddg+bing"
        return "bing_html"

    @property
    def available_backends(self) -> list[str]:
        """当前可用的后端列表，用于诊断降级状态。"""
        backends: list[str] = []
        if self._searxng_available:
            backends.append("searxng")
        if self._ddgs is not None:
            backends.append("ddgs")
        # Bing HTML 仅依赖 requests + bs4，通常可用
        backends.append("bing_html")
        return backends

    def search(self, query: str) -> list[SourceDocument]:
        """单次搜索，返回 SourceDocument 列表。

        所有后端都失败时记录 warning，便于运维诊断。
        """
        if not query or not query.strip():
            return []

        cached = self._cache_get(query)
        if cached is not None:
            logger.debug("命中本地搜索缓存: %s", query)
            return cached

        docs: list[SourceDocument] = []

        # 1. SearXNG
        if self._searxng_available and self._searxng:
            try:
                perilla_docs = self._searxng.search(query)
                if perilla_docs:
                    docs = [self._convert_perilla_doc(d, query) for d in perilla_docs]
            except Exception as exc:
                logger.debug("SearXNG 搜索失败 '%s': %s", query, exc)

        # 2. fallback 聚合
        if not docs:
            docs = self._search_fallback(query)

        # 3. 所有后端均无结果时记录 warning
        if not docs:
            logger.warning(
                "搜索 query='%s' 所有后端均无结果，可用后端: %s",
                query, self.available_backends,
            )

        # 4. 智能正文抓取
        if self.config.fetch_article and docs:
            docs = self._fetch_articles_for_top_docs(query, docs)

        self._cache_set(query, docs)
        return docs

    def search_many(self, queries: list[str]) -> list[SourceDocument]:
        """批量搜索并去重。"""
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
        return all_docs

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _search_fallback(self, query: str) -> list[SourceDocument]:
        """DDG + Bing HTML 聚合，BM25 重排。

        DDG 不可用时降级到仅 Bing HTML；两者均失败时返回空列表。
        """
        raw: list[dict[str, str]] = []

        # DDG
        if self._ddgs is not None:
            try:
                results = self._ddgs.text(
                    query,
                    region="cn-zh",
                    safesearch="off",
                    max_results=self.config.bm25_top_k,
                )
                if results:
                    raw.extend(results)
            except Exception as exc:
                logger.debug("DDG 搜索失败 '%s': %s", query, exc)
            time.sleep(self.config.ddg_throttle_seconds)
        else:
            logger.debug("DDGS 不可用，仅使用 Bing HTML 后端: query='%s'", query)

        # Bing HTML
        try:
            bing_results = self._search_bing_html(query)
            raw.extend(bing_results)
            time.sleep(self.config.bing_throttle_seconds)
        except Exception as exc:
            logger.debug("Bing HTML 搜索失败 '%s': %s", query, exc)

        # 过滤 + 去重 + BM25 重排
        filtered = [item for item in raw if self._is_quality_result(item)]
        ranked = self._rerank_bm25(query, dedupe_results(filtered))

        return [
            SourceDocument(
                document_id=f"me_{hash(item.get('href') or item.get('url', '')) & 0xFFFFFFFF:08x}",
                source_type=self._infer_source_type(item.get("href") or item.get("url", "")),
                title=item.get("title", ""),
                content=item.get("body", ""),
                publish_date="",
                url=item.get("href") or item.get("url", ""),
                source_id=item.get("source", "unknown"),
                topic=self.topic,
                summary=(item.get("body") or "")[:200],
                metadata={
                    "query": query,
                    "credibility_hint": "unknown",
                    "mentioned_chain_nodes": [],
                    "mentioned_entities": [],
                    "possible_relation_types": [],
                    "evidence_strength_hint": "SPECULATIVE",
                    "search_backend": "multi_engine_fallback",
                },
            )
            for item in ranked[: self.config.max_results]
        ]

    def _search_bing_html(self, query: str) -> list[dict[str, str]]:
        import requests
        from bs4 import BeautifulSoup

        url = f"https://cn.bing.com/search?q={quote_plus(query)}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=self.config.request_timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        docs: list[dict[str, str]] = []
        for result in soup.select("#b_results > li.b_algo")[: self.config.bm25_top_k]:
            title_el = result.select_one("h2 a")
            snippet_el = result.select_one(".b_caption p") or result.select_one("p")
            title = title_el.get_text(strip=True) if title_el else ""
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            href = title_el.get("href", "") if title_el else ""
            if title or snippet:
                docs.append({"title": title, "href": href, "body": snippet, "source": "bing_html"})
        return docs

    def _convert_perilla_doc(self, doc: Any, query: str) -> SourceDocument:
        """perilla SourceDocument → evidence_miner SourceDocument。"""
        source_type = self._infer_source_type(doc.url or "")
        return SourceDocument(
            document_id=doc.document_id or f"sx_{hash(doc.url) & 0xFFFFFFFF:08x}",
            source_type=source_type,
            title=doc.title or "",
            content=doc.content_text or "",
            publish_date=doc.published_at or "",
            url=doc.url or "",
            source_id=doc.site_name or "searxng",
            topic=self.topic,
            summary=(doc.content_text or "")[:200],
            metadata={
                "query": query,
                "credibility_hint": doc.credibility_hint,
                "mentioned_chain_nodes": [],
                "mentioned_entities": [],
                "possible_relation_types": [],
                "evidence_strength_hint": "SPECULATIVE",
                "search_backend": "searxng",
            },
        )

    def _is_quality_result(self, item: dict[str, str]) -> bool:
        url = item.get("href") or item.get("url") or ""
        title = item.get("title", "")
        body = item.get("body", "")
        if not url.startswith(("http://", "https://")):
            return False
        if len(title) < self.config.min_title_length or len(body) < self.config.min_snippet_length:
            return False
        if _is_low_quality(url, title):
            return False
        return True

    def _rerank_bm25(self, query: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
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

    def _fetch_articles_for_top_docs(self, query: str, docs: list[SourceDocument]) -> list[SourceDocument]:
        """基于信息密度评分，只抓取最有潜力的 top 文档。"""
        if not docs:
            return docs

        scored = [(d, self._info_density_score(query, d)) for d in docs]
        scored.sort(key=lambda x: x[1], reverse=True)

        fetched = 0
        for doc, score in scored:
            if fetched >= self.config.max_article_fetches:
                break
            if score < 0.3:
                continue
            article = fetch_article_text(doc.url, timeout=self.config.request_timeout)
            if article:
                doc.content = f"{doc.content}\n{article}"[:3000]
                doc.summary = doc.content[:200]
                doc.metadata["article_fetched"] = True
                fetched += 1
        logger.debug("query '%s': 抓取 %d/%d 篇正文", query, fetched, len(docs))
        return docs

    def _info_density_score(self, query: str, doc: SourceDocument) -> float:
        """信息密度评分：标题/摘要包含多少 query 核心词。"""
        query_terms = set(self._tokenize(query))
        text = f"{doc.title} {doc.content[:300]}"
        text_terms = set(self._tokenize(text))
        if not query_terms:
            return 0.0
        overlap = len(query_terms & text_terms) / len(query_terms)
        # 长摘要通常信息量大
        length_bonus = min(1.0, len(doc.content) / 500) * 0.2
        return min(1.0, overlap + length_bonus)

    @staticmethod
    def _infer_source_type(url: str) -> str:
        """根据 URL 推断来源类型,让高置信来源优先进入状态升级通道。"""
        if not url:
            return "search_result"
        url_lower = url.lower()

        # 官方/监管/交易所
        official_markers = [
            "gov.cn", "sec.gov", "csrc.gov.cn", "sse.com.cn", "szse.cn",
            "bse.cn", "hkexnews.hk", "cninfo.com.cn", "stcn.com", "cs.com.cn",
        ]
        if any(marker in url_lower for marker in official_markers):
            return "official"

        # 公司官网/投资者关系
        if any(marker in url_lower for marker in ["ir.", "investor.", "investors.", "about."]):
            return "company_website"

        # 技术文档/开发者资源
        technical_markers = [
            "arxiv.org", "github.com", "docs.", "developer.", "api.",
            "ieee.org", "acm.org", "researchgate.net", "patents.",
        ]
        if any(marker in url_lower for marker in technical_markers):
            return "technical_doc"

        # 研究报告/财报 PDF
        if any(marker in url_lower for marker in ["report", "research", ".pdf"]):
            return "research_report"

        return "search_result"

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
        tokens.extend(re.findall(r"[\u4e00-\u9fff]", text))
        return tokens

    # ------------------------------------------------------------------
    # 本地缓存
    # ------------------------------------------------------------------
    def _cache_get(self, query: str) -> list[SourceDocument] | None:
        if not self._cache_dir:
            return None
        path = self._cache_path(query)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) > self.config.cache_ttl_seconds:
                return None
            return [SourceDocument(**item) for item in data.get("docs", [])]
        except Exception:
            return None

    def _cache_set(self, query: str, docs: list[SourceDocument]) -> None:
        if not self._cache_dir:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(query)
        payload = {
            "ts": time.time(),
            "docs": [self._doc_to_dict(d) for d in docs],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _cache_path(self, query: str) -> Path:
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "_", query).strip("_")[:80]
        return self._cache_dir / f"search_cache_{key}.json"

    @staticmethod
    def _doc_to_dict(doc: SourceDocument) -> dict[str, Any]:
        return {
            "document_id": doc.document_id,
            "source_type": doc.source_type,
            "title": doc.title,
            "content": doc.content,
            "publish_date": doc.publish_date,
            "url": doc.url,
            "source_id": doc.source_id,
            "topic": doc.topic,
            "summary": doc.summary,
            "metadata": doc.metadata,
        }


def fetch_article_text(url: str, timeout: int = 12) -> str:
    """抓取网页正文。"""
    import requests
    try:
        from readability import Document
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
            timeout=timeout,
        )
        resp.raise_for_status()
        doc = Document(resp.text)
        text = re.sub(r"<[^>]+>", "", doc.summary())
        return re.sub(r"\s+", " ", text).strip()[:6000]
    except Exception:
        try:
            from bs4 import BeautifulSoup
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
                timeout=timeout,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))[:6000]
        except Exception:
            return ""


def dedupe_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    """按 URL 去重。"""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in results:
        key = (item.get("href") or item.get("url", "")).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def main() -> None:
    """命令行快速测试。"""
    import argparse

    parser = argparse.ArgumentParser(description="测试增强型多引擎搜索")
    parser.add_argument("--topic", default="HBM")
    parser.add_argument("--query", default="HBM 产业链 供应商")
    parser.add_argument("--max", type=int, default=8)
    parser.add_argument("--fetch", action="store_true", default=True)
    args = parser.parse_args()

    config = SearchConfig(max_results=args.max, fetch_article=args.fetch)
    searcher = MultiEngineSearcher(topic=args.topic, config=config, cache_dir="data/search_cache")
    docs = searcher.search(args.query)
    print(json.dumps([{"title": d.title, "url": d.url, "backend": d.metadata.get("search_backend"), "len": len(d.content)} for d in docs], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
