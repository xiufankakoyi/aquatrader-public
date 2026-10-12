"""SearXNG-based search discovery for Perilla Discovery Engine.

Provides web search capabilities via a self-hosted SearXNG instance,
with query template expansion, URL deduplication, and credibility-based
ranking of search results.

容错策略（解决 SearXNG 不稳定的问题）:
- 启动时探测 SearXNG 健康状态（/healthz）
- 每次请求失败时记录失败计数，连续失败 N 次后自动降级到 DuckDuckGo 直连
- 降级后定期探测 SearXNG 恢复，恢复后切回主搜索
- DDG fallback 仅作为离线降级，max_concurrency=1，min_interval=2.0s
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

import requests
import yaml

from core.utils.search_throttle import SearchThrottlePool

from .models import CREDIBILITY_HINTS, SourceDocument, new_id, now_iso

logger = logging.getLogger(__name__)

# Default path for query templates relative to project root
_DEFAULT_TEMPLATES_PATH = Path("data/config/perilla_query_templates.yaml")

# Credibility ordering for ranking
_CREDIBILITY_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}

# SearXNG 健康检查与降级相关常量
_SEARXNG_HEALTH_CHECK_TIMEOUT = 5
_SEARXNG_MAX_CONSECUTIVE_FAILURES = 3  # 连续失败 N 次后降级到 DDG
_SEARXNG_RECOVERY_PROBE_INTERVAL = 300  # 降级后每 5 分钟探测一次 SearXNG 是否恢复

# DuckDuckGo HTML fallback endpoint（无 API key，仅作离线降级）
_DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_DDG_FALLBACK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AquaTrade-PerillaDiscovery/1.0"


class SearchDiscovery:
    """Search and discover web content via SearXNG.

    Expands concept names into structured queries using configurable
    templates, searches via the SearXNG JSON API, and returns
    deduplicated, ranked :class:`SourceDocument` instances.

    Args:
        searxng_base_url: Base URL of the SearXNG instance (e.g. ``http://localhost:8080``).
        max_results: Maximum number of results per search query.
        timeout: HTTP request timeout in seconds.
        enabled: Whether the search functionality is active.
    """

    def __init__(
        self,
        searxng_base_url: str = "",
        max_results: int = 10,
        timeout: int = 30,
        enabled: bool = True,
        engines: str = "",
        max_concurrency: int | None = None,
        min_interval: float | None = None,
        throttle_pool: SearchThrottlePool | None = None,
    ) -> None:
        self._base_url = searxng_base_url.rstrip("/") if searxng_base_url else ""
        self._max_results = max_results
        self._timeout = timeout
        self._enabled = enabled
        self._engines = engines.strip()
        self._max_concurrency = max(
            1,
            int(
                max_concurrency
                if max_concurrency is not None
                else os.getenv("SEARCH_SEARXNG_MAX_CONCURRENCY", "4")
            ),
        )
        interval = (
            min_interval
            if min_interval is not None
            else float(os.getenv("SEARCH_SEARXNG_MIN_INTERVAL", "0.05"))
        )
        self._throttle = throttle_pool or SearchThrottlePool(
            max_concurrency=self._max_concurrency,
            min_interval=interval,
            failure_cooldown=2.0,
        )
        self._templates: List[str] = self._load_templates()
        # SearXNG 健康状态跟踪
        self._consecutive_failures: int = 0
        self._degraded: bool = False
        self._last_probe_time: float = 0.0
        self._degraded_since: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build_queries_for_concept(self, concept_name: str) -> List[str]:
        """Expand a concept name into a list of search queries.

        Loads query templates from the YAML config file and substitutes
        the ``{topic}`` placeholder with *concept_name*.

        Args:
            concept_name: The concept / topic keyword to search for.

        Returns:
            A list of query strings ready for :meth:`search`.
        """
        if not concept_name or not concept_name.strip():
            logger.warning("Empty concept_name provided, returning no queries")
            return []

        queries: List[str] = []
        for tpl in self._templates:
            try:
                query = tpl.replace("{topic}", concept_name)
                queries.append(query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to expand template '%s': %s", tpl, exc)

        return queries

    def search(self, query: str) -> List[SourceDocument]:
        """Execute a search query via the SearXNG JSON API.

        降级策略：
        1. 优先 SearXNG；连续失败 N 次后切到 DDG 直连
        2. 降级后每 5 分钟探测 SearXNG 是否恢复
        3. DDG 失败时返回空列表，不中断主流程

        Args:
            query: The search query string.

        Returns:
            A list of :class:`SourceDocument` instances.
        """
        if not self._enabled or not self._base_url:
            logger.warning("SearXNG is not available, returning empty results")
            return []

        if not query or not query.strip():
            return []

        # 检查是否需要降级或探测恢复
        self._maybe_update_degraded_state()

        if self._degraded:
            # 降级到 DDG
            return self._throttle.run(self._search_via_ddg, query)

        return self._throttle.run(self._search_unthrottled, query)

    def search_many(self, queries: List[str]) -> Dict[str, List[SourceDocument]]:
        """Search independent queries concurrently through the shared pool."""
        unique_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not unique_queries:
            return {}

        results: Dict[str, List[SourceDocument]] = {query: [] for query in unique_queries}
        worker_count = min(self._max_concurrency, len(unique_queries))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(self.search, query): query for query in unique_queries}
            for future in as_completed(futures):
                query = futures[future]
                try:
                    results[query] = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SearXNG concurrent search failed for query='%s': %s", query, exc)
        return results

    def throttle_stats(self) -> dict:
        """Return process-local throttle counters."""
        return self._throttle.snapshot()

    def _maybe_update_degraded_state(self) -> None:
        """根据失败计数和恢复探测更新降级状态。

        策略：
        - 连续失败次数 >= _SEARXNG_MAX_CONSECUTIVE_FAILURES → 进入降级
        - 降级后每 _SEARXNG_RECOVERY_PROBE_INTERVAL 秒探测一次 SearXNG 是否恢复
        - 探测成功则退出降级，重置失败计数
        """
        now = time.time()
        if not self._degraded:
            if self._consecutive_failures >= _SEARXNG_MAX_CONSECUTIVE_FAILURES:
                self._degraded = True
                self._degraded_since = now
                self._last_probe_time = now
                logger.warning(
                    "SearXNG degraded after %d consecutive failures, falling back to DDG",
                    self._consecutive_failures,
                )
            return

        # 已降级：检查是否到探测时间
        if now - self._last_probe_time < _SEARXNG_RECOVERY_PROBE_INTERVAL:
            return
        self._last_probe_time = now
        # 探测 SearXNG 是否恢复
        if self._probe_searxng_health():
            logger.info(
                "SearXNG recovered after %.0fs, exiting degraded mode",
                now - self._degraded_since,
            )
            self._degraded = False
            self._consecutive_failures = 0
            self._degraded_since = 0.0

    def _probe_searxng_health(self) -> bool:
        """探测 SearXNG 健康状态（轻量 /healthz 请求）。"""
        if not self._base_url:
            return False
        try:
            response = requests.get(
                f"{self._base_url}/healthz",
                timeout=_SEARXNG_HEALTH_CHECK_TIMEOUT,
                headers={"User-Agent": "AquaTrade-PerillaDiscovery/1.0"},
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def _search_unthrottled(self, query: str) -> List[SourceDocument]:
        url = f"{self._base_url}/search"
        params = {"q": query, "format": "json"}
        if self._engines:
            params["engines"] = self._engines

        try:
            response = requests.get(
                url,
                params=params,
                timeout=self._timeout,
                headers={"User-Agent": "AquaTrade-PerillaDiscovery/1.0"},
            )
            if response.status_code in {403, 429}:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    cooldown = max(2.0, float(retry_after))
                except (TypeError, ValueError):
                    cooldown = 5.0
                self._throttle.penalize(cooldown)
            if response.status_code >= 500:
                # 5xx 视为 SearXNG 后端不可用，记录失败计数
                self._consecutive_failures += 1
                logger.warning(
                    "SearXNG %d (consecutive_failures=%d): query='%s'",
                    response.status_code, self._consecutive_failures, query,
                )
                return []
            response.raise_for_status()
            data = response.json()
            if not data.get("results") and data.get("unresponsive_engines"):
                self._throttle.penalize(5.0)
            # 成功响应：重置失败计数
            self._consecutive_failures = 0
        except requests.RequestException as exc:
            self._consecutive_failures += 1
            logger.warning(
                "SearXNG request failed (consecutive=%d) for query='%s': %s",
                self._consecutive_failures, query, exc,
            )
            return []
        except (ValueError, KeyError) as exc:
            self._consecutive_failures += 1
            logger.warning(
                "SearXNG returned invalid JSON (consecutive=%d) for query='%s': %s",
                self._consecutive_failures, query, exc,
            )
            return []

        results: List[SourceDocument] = []
        items = data.get("results", [])[: self._max_results]

        for item in items:
            try:
                item_url = str(item.get("url", "") or "")
                if "duckduckgo.com/l/" in item_url:
                    item_url = self._extract_ddg_actual_url(item_url)
                doc = SourceDocument(
                    document_id=new_id("sd_"),
                    source_type="search_result",
                    url=item_url,
                    title=item.get("title", ""),
                    site_name=item.get("engine", ""),
                    published_at=item.get("publishedDate") or None,
                    fetched_at=now_iso(),
                    content_text=item.get("content", ""),
                    credibility_hint=self._infer_credibility(item),
                    metadata={
                        "query": query,
                        "engines": item.get("engines", []),
                        "unresponsive_engines": data.get("unresponsive_engines", []),
                        "score": item.get("score", 0),
                        "category": item.get("category", ""),
                        "source_backend": "searxng",
                    },
                )
                results.append(doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse SearXNG result item: %s", exc)

        return results

    def _search_via_ddg(self, query: str) -> List[SourceDocument]:
        """DuckDuckGo HTML fallback：当 SearXNG 不可用时使用。

        策略：
        - 直接请求 DDG HTML 端点
        - 解析 HTML 中的结果链接和摘要（轻量正则解析，避免引入 BeautifulSoup 依赖）
        - max_results 限制为 5（DDG 反爬严格，避免大量请求）
        - 失败时返回空列表
        """
        try:
            response = requests.post(
                _DDG_HTML_ENDPOINT,
                data={"q": query},
                timeout=self._timeout,
                headers={
                    "User-Agent": _DDG_FALLBACK_USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("DDG fallback failed for query='%s': %s", query, exc)
            return []

        html = response.text
        if not html:
            return []

        # 轻量正则解析 DDG HTML 结果
        # DDG html.duckduckgo.com 的结果链接格式：
        # <a class="result__a" href="...">标题</a>
        # <a class="result__snippet" href="...">摘要</a>
        results: List[SourceDocument] = []
        # 匹配结果块
        result_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        for match in result_pattern.finditer(html):
            if len(results) >= min(self._max_results, 5):
                break
            url_raw = match.group(1)
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()
            # DDG 链接可能是 redirect 形式：//duckduckgo.com/l/?uddg=<encoded_url>
            actual_url = self._extract_ddg_actual_url(url_raw)
            if not actual_url or not title:
                continue
            try:
                doc = SourceDocument(
                    document_id=new_id("sd_ddg_"),
                    source_type="search_result",
                    url=actual_url,
                    title=title,
                    site_name="duckduckgo",
                    published_at=None,
                    fetched_at=now_iso(),
                    content_text=snippet,
                    credibility_hint="medium",  # DDG 结果默认中等可信
                    metadata={
                        "query": query,
                        "engines": ["duckduckgo"],
                        "source_backend": "ddg_fallback",
                    },
                )
                results.append(doc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to parse DDG result item: %s", exc)

        if not results:
            logger.info("DDG fallback returned 0 results for query='%s'", query)
        return results

    @staticmethod
    def _extract_ddg_actual_url(url_raw: str) -> str:
        """从 DDG redirect 链接中提取实际 URL。"""
        if not url_raw:
            return ""
        # DDG 链接形如 //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com&rut=...
        if "uddg=" in url_raw:
            try:
                from urllib.parse import parse_qs, urlparse
                # 补全协议
                if url_raw.startswith("//"):
                    url_raw = "https:" + url_raw
                parsed = urlparse(url_raw)
                qs = parse_qs(parsed.query)
                actual = qs.get("uddg", [""])[0]
                return actual
            except Exception:
                return url_raw
        # 已经是实际 URL
        if url_raw.startswith("//"):
            return "https:" + url_raw
        return url_raw

    def dedupe_urls(self, results: List[SourceDocument]) -> List[SourceDocument]:
        """Remove duplicate SourceDocuments by URL (case-insensitive).

        Keeps the first occurrence of each URL.

        Args:
            results: List of :class:`SourceDocument` to deduplicate.

        Returns:
            A deduplicated list preserving the original order.
        """
        seen: set[str] = set()
        deduped: List[SourceDocument] = []

        for doc in results:
            key = doc.url.lower() if doc.url else ""
            if key in seen:
                continue
            seen.add(key)
            deduped.append(doc)

        return deduped

    def rank_results(self, results: List[SourceDocument]) -> List[SourceDocument]:
        """Sort results by credibility hint and publication recency.

        Primary sort: credibility (high > medium > low > unknown).
        Secondary sort: published_at (most recent first).

        Args:
            results: List of :class:`SourceDocument` to rank.

        Returns:
            A new sorted list.
        """

        def _sort_key(doc: SourceDocument) -> tuple:
            cred_order = _CREDIBILITY_ORDER.get(doc.credibility_hint, 3)
            # Negate presence of date: items with dates sort before those without
            has_date = 0 if doc.published_at else 1
            # Negate date string so that more recent dates sort first
            date_val = doc.published_at or ""
            return (cred_order, has_date, date_val)

        return sorted(results, key=_sort_key)

    def is_available(self) -> bool:
        """Check if SearXNG is configured and reachable.

        Returns:
            ``True`` if the base URL is set and the health endpoint responds.
        """
        if not self._enabled or not self._base_url:
            return False

        try:
            response = requests.get(
                f"{self._base_url}/healthz",
                timeout=5,
                headers={"User-Agent": "AquaTrade-PerillaDiscovery/1.0"},
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def is_degraded(self) -> bool:
        """返回当前是否处于降级模式（使用 DDG fallback）。"""
        return self._degraded

    def health_snapshot(self) -> dict:
        """返回健康状态快照，用于监控/日志。"""
        return {
            "enabled": self._enabled,
            "base_url": self._base_url,
            "degraded": self._degraded,
            "consecutive_failures": self._consecutive_failures,
            "degraded_since": self._degraded_since,
            "last_probe_time": self._last_probe_time,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _load_templates() -> List[str]:
        """Load query templates from the YAML config file.

        Returns:
            A list of template strings.  Falls back to a minimal default
            set if the file cannot be loaded.
        """
        try:
            with open(_DEFAULT_TEMPLATES_PATH, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            templates = data.get("templates", [])
            if templates:
                return templates
        except FileNotFoundError:
            logger.warning("Query templates file not found: %s", _DEFAULT_TEMPLATES_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load query templates: %s", exc)

        # Minimal fallback templates
        return [
            "{topic} 供应链",
            "{topic} 产业链",
            "{topic} 供应商",
        ]

    @staticmethod
    def _infer_credibility(item: dict) -> str:
        """Infer a credibility hint from a SearXNG result item.

        Uses the engine source and score to estimate credibility.

        Args:
            item: A single result dict from the SearXNG JSON response.

        Returns:
            A credibility hint string from :data:`CREDIBILITY_HINTS`.
        """
        engines = item.get("engines", [])
        score = item.get("score", 0)

        # Academic / official engines tend to be more credible
        high_cred_engines = {"google scholar", "duckduckgo", "wikipedia"}
        if any(e.lower() in high_cred_engines for e in engines):
            return "high"

        if score and float(score) > 5.0:
            return "medium"

        return "low"
