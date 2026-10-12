"""BaiduSearchProvider - 百度AI搜索 + DuckDuckGo 降级

百度 AI Search API（千帆）：
- 接口：https://qianfan.baidubce.com/v2/ai_search/web_search
- 日限100次，超出后自动降级到 DuckDuckGo
- 支持 freshness 时间范围过滤

DuckDuckGo 降级：
- 无需 API Key
- 通过 duckduckgo_search 库实现
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import requests

from core.utils.search_throttle import SearchThrottlePool

try:
    from config.secrets import Secrets
except Exception:
    class Secrets:
        BAIDU_API_KEY = ""

logger = logging.getLogger(__name__)

# 日限计数器（进程内，每日0点重置）
_daily_count: int = 0
_daily_date: str = ""
_daily_lock = threading.Lock()

DAILY_LIMIT = 100

_ddg_throttle = SearchThrottlePool(
    max_concurrency=int(os.environ.get("SEARCH_DDG_MAX_CONCURRENCY", "3")),
    min_interval=float(os.environ.get("SEARCH_DDG_MIN_INTERVAL", "1.0")),
    failure_cooldown=float(os.environ.get("SEARCH_DDG_FAILURE_COOLDOWN", "1200")),
    burst_capacity=int(os.environ.get("SEARCH_DDG_BURST", "3")),
)


def _check_daily_limit() -> tuple[bool, int]:
    """检查日限，返回 (是否允许, 当日已用次数)。

    Returns:
        (allowed, used_count)
    """
    global _daily_count, _daily_date
    today = datetime.now().strftime("%Y-%m-%d")
    with _daily_lock:
        if today != _daily_date:
            _daily_date = today
            _daily_count = 0
        if _daily_count >= DAILY_LIMIT:
            return False, _daily_count
        return True, _daily_count


def _increment_daily_count() -> None:
    """递增日限计数。"""
    global _daily_count
    with _daily_lock:
        _daily_count += 1


class BaiduSearchProvider:
    """百度 AI 搜索 + DuckDuckGo 降级搜索。"""

    name = "baidu"

    def __init__(self) -> None:
        self._api_key: str = Secrets.BAIDU_API_KEY or os.environ.get("BAIDU_API_KEY", "")

    def is_available(self) -> bool:
        """百度 API Key 已配置且日限未超。"""
        return bool(self._api_key)

    def health(self) -> dict[str, Any]:
        """返回搜索源健康状态。"""
        allowed, used = _check_daily_limit()
        return {
            "name": self.name,
            "available": self.is_available(),
            "daily_limit": DAILY_LIMIT,
            "daily_used": used,
            "daily_remaining": DAILY_LIMIT - used,
            "fallback": "duckduckgo",
        }

    def search(
        self,
        query: str,
        count: int = 10,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        """搜索网页，百度优先，超出日限降级到 DuckDuckGo。

        Args:
            query: 搜索关键词
            count: 返回结果数（1-50）
            freshness: 时间范围过滤
                - "pd": 过去24小时
                - "pw": 过去7天
                - "pm": 过去31天
                - "py": 过去365天
                - "YYYY-MM-DDtoYYYY-MM-DD": 自定义日期范围

        Returns:
            搜索结果列表 [{title, url, snippet, source, time}]
        """
        allowed, used = _check_daily_limit()
        if allowed and self._api_key:
            try:
                results = self._baidu_search(query, count, freshness)
                _increment_daily_count()
                logger.info("百度搜索成功 (query=%s, 日已用=%d/%d)", query[:20], used + 1, DAILY_LIMIT)
                return results
            except Exception as exc:
                logger.warning("百度搜索失败，降级到 DuckDuckGo: %s", exc)

        # 降级到 DuckDuckGo
        logger.info("降级到 DuckDuckGo (query=%s, 日已用=%d/%d)", query[:20], used, DAILY_LIMIT)
        return _ddg_throttle.run(self._duckduckgo_search, query, count)

    def _baidu_search(
        self,
        query: str,
        count: int = 10,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        """百度 AI Search API 搜索。

        Args:
            query: 搜索关键词
            count: 返回结果数
            freshness: 时间范围过滤

        Returns:
            搜索结果列表
        """
        count = max(1, min(50, count))

        # 构建时间过滤
        search_filter: dict[str, Any] = {}
        if freshness:
            current_time = datetime.now()
            end_date = (current_time + timedelta(days=1)).strftime("%Y-%m-%d")
            date_pattern = r"\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}"

            if freshness in ("pd", "pw", "pm", "py"):
                offsets = {"pd": 1, "pw": 6, "pm": 30, "py": 364}
                start_date = (current_time - timedelta(days=offsets[freshness])).strftime("%Y-%m-%d")
                search_filter = {"range": {"page_time": {"gte": start_date, "lt": end_date}}}
            elif re.match(date_pattern, freshness):
                parts = freshness.split("to")
                search_filter = {"range": {"page_time": {"gte": parts[0], "lt": parts[1]}}}

        request_body = {
            "messages": [{"content": query, "role": "user"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": count}],
            "search_filter": search_filter,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "X-Appbuilder-From": "openclaw",
        }

        resp = requests.post(
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
            json=request_body,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "code" in data:
            raise RuntimeError(f"百度搜索 API 错误: {data.get('message', 'unknown')}")

        references = data.get("references", [])
        results: list[dict[str, Any]] = []
        for ref in references:
            results.append({
                "title": str(ref.get("title", "")),
                "url": str(ref.get("url", "")),
                "snippet": str(ref.get("content", ref.get("snippet", ""))),
                "source": "baidu",
                "time": str(ref.get("page_time", ref.get("publish_time", ""))),
            })

        return results

    def _duckduckgo_search(
        self,
        query: str,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """DuckDuckGo 搜索降级方案。

        Args:
            query: 搜索关键词
            count: 返回结果数

        Returns:
            搜索结果列表
        """
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[no-redef]
            except ImportError:
                logger.warning("ddgs/duckduckgo_search 均未安装，无法降级搜索")
                return []

        try:
            results: list[dict[str, Any]] = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=count):
                    results.append({
                        "title": str(r.get("title", "")),
                        "url": str(r.get("href", "")),
                        "snippet": str(r.get("body", "")),
                        "source": "duckduckgo",
                        "time": "",
                    })
            return results
        except Exception as exc:
            logger.warning("DuckDuckGo 搜索失败: %s", exc)
            _ddg_throttle.penalize(
                float(os.environ.get("SEARCH_DDG_FAILURE_COOLDOWN", "1200"))
            )
            return []
