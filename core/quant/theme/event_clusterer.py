"""新闻事件聚类器。

从新闻/公告中聚类事件，提取事件实体与共现股票。
用于在东财/同花顺概念源失败时，通过新闻聚类独立生成临时主题。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    """新闻事件。"""
    event_id: str = ""
    title: str = ""
    summary: str = ""
    event_date: str = ""

    # 事件实体（关键词）
    keywords: list[str] = field(default_factory=list)
    theme_hints: list[str] = field(default_factory=list)

    # 共现股票（symbol 列表）
    co_occurred_symbols: list[str] = field(default_factory=list)

    # 来源信息
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)

    # 独立性判断
    is_independent: bool = False
    independent_source_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "event_id": self.event_id,
            "title": self.title,
            "summary": self.summary,
            "event_date": self.event_date,
            "keywords": self.keywords,
            "theme_hints": self.theme_hints,
            "co_occurred_symbols": self.co_occurred_symbols,
            "sources": self.sources,
            "source_urls": self.source_urls,
            "is_independent": self.is_independent,
            "independent_source_count": self.independent_source_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsEvent:
        """从字典反序列化，忽略未知字段以兼容旧数据。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class EventClusterer:
    """新闻事件聚类器。

    从新闻列表中：
    1. 提取事件关键词
    2. 识别共现股票
    3. 判断独立来源（非单一转载）
    4. 聚类相似事件
    """

    # 股票代码正则（6 位数字，支持沪深）
    SYMBOL_PATTERN = re.compile(r"\b(\d{6})\b")

    # 常见转载标记
    REPUBLISH_MARKERS = {"转载", "来源：", "原文：", "转自", "引自"}

    # 事件关键词停用词
    STOP_WORDS = {"的", "了", "在", "是", "和", "与", "或", "也", "都", "这", "那", "一个", "一种"}

    def __init__(
        self,
        stock_name_to_symbol: dict[str, str] | None = None,
        known_theme_names: list[str] | None = None,
    ) -> None:
        """初始化。

        Args:
            stock_name_to_symbol: 公司名→股票代码映射，用于从新闻正文提取股票。
        """
        self._stock_name_map = stock_name_to_symbol or {}
        self._known_theme_names = sorted(
            {str(name).strip() for name in (known_theme_names or []) if str(name).strip()},
            key=len,
            reverse=True,
        )

    def cluster_events(
        self,
        news_items: list[dict[str, Any]],
        min_co_occurrence: int = 2,
    ) -> list[NewsEvent]:
        """从新闻列表聚类事件。

        Args:
            news_items: 新闻列表，每条含 title/content/source/url/date。
            min_co_occurrence: 最小共现股票数，低于此数的事件被过滤。

        Returns:
            聚类后的 NewsEvent 列表。
        """
        if not news_items:
            return []

        # 1. 提取每条新闻的事件特征
        raw_events: list[NewsEvent] = []
        for item in news_items:
            event = self._extract_event(item)
            if event:
                raw_events.append(event)

        # 2. 聚类相似事件
        clustered = self._cluster_similar_events(raw_events)

        # 3. 过滤共现股票不足的事件
        filtered = [
            ev for ev in clustered
            if len(ev.co_occurred_symbols) >= min_co_occurrence or bool(ev.theme_hints)
        ]

        # 4. 判断独立性
        for event in filtered:
            event.is_independent = self._is_independent_source(event)

        return filtered

    def _extract_event(self, news_item: dict[str, Any]) -> NewsEvent | None:
        """从单条新闻提取事件。"""
        title = str(
            news_item.get("title")
            or news_item.get("新闻标题")
            or news_item.get("标题")
            or ""
        ).strip()
        content = str(
            news_item.get("content")
            or news_item.get("summary")
            or news_item.get("digest")
            or news_item.get("description")
            or news_item.get("摘要")
            or ""
        ).strip()
        source = str(
            news_item.get("source")
            or news_item.get("provider")
            or news_item.get("媒体")
            or news_item.get("信息来源")
            or ""
        ).strip()
        url = str(
            news_item.get("url")
            or news_item.get("link")
            or news_item.get("source_url")
            or ""
        ).strip()
        date = str(
            news_item.get("date")
            or news_item.get("published_at")
            or news_item.get("publish_date")
            or news_item.get("time")
            or news_item.get("showTime")
            or news_item.get("publish_time")
            or news_item.get("datetime")
            or news_item.get("发布时间")
            or news_item.get("发布日期")
            or ""
        ).strip()

        if url:
            match = re.search(r"https?://([^/]+)", url)
            # Domain lineage is more reliable than provider display labels
            # such as "7x24" and prevents one domain being double-counted.
            source = match.group(1).lower() if match else source

        if not title:
            return None

        # 提取关键词
        theme_hints = self._extract_theme_hints(title, content)
        keywords = list(dict.fromkeys(theme_hints + self._extract_keywords(title, content)))

        # 提取共现股票
        symbols = self._extract_symbols(title, content)

        event_id = "evt_" + hashlib.sha1(
            f"{title}|{date}".encode("utf-8")
        ).hexdigest()[:12]

        return NewsEvent(
            event_id=event_id,
            title=title,
            summary=content[:200] if content else "",
            event_date=date,
            keywords=keywords,
            theme_hints=theme_hints,
            co_occurred_symbols=list(set(symbols)),
            sources=[source] if source else [],
            source_urls=[url] if url else [],
        )

    def _extract_keywords(self, title: str, content: str) -> list[str]:
        """提取事件关键词。

        简单策略：从标题中提取名词性短语，过滤停用词。
        """
        try:
            import jieba.analyse
            words = jieba.analyse.extract_tags(
                f"{title} {content}", topK=12, withWeight=False
            )
        except Exception:
            words = re.split(r"[]\s，。、：；""''（）()[【】！!？?]+", title)
        generic = {
            "公司", "公告", "预计", "最新", "今日", "昨日", "记者", "消息",
            "同比", "上半年", "下半年", "年度", "集团", "股份", "股价",
        }
        keywords = [
            w.strip() for w in words
            if w.strip()
            and len(w.strip()) >= 2
            and w.strip() not in self.STOP_WORDS
            and w.strip() not in generic
            and w.strip() not in self._stock_name_map
        ]
        return keywords[:10]  # 最多 10 个关键词

    @staticmethod
    def _normalized_term(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fa5]+", "", str(value).lower())

    def _extract_theme_hints(self, title: str, content: str) -> list[str]:
        text = self._normalized_term(f"{title} {content}")
        hints: list[str] = []
        for name in self._known_theme_names:
            normalized = self._normalized_term(name)
            if len(normalized) >= 2 and normalized in text:
                hints.append(name)
        # New concepts often appear in news before any local registry exists
        # (for example "AI PC").  Treat these as query prototypes only; the
        # downstream Wencai exact-concept check and Research evidence gate must
        # still validate them before any company becomes core.
        raw_text = f"{title} {content[:300]}"
        trigger_terms = (
            "发布", "推出", "首次", "技术", "平台", "架构", "产业", "概念",
            "产品", "市场", "政策", "商业化", "量产", "获批",
        )
        if any(term in raw_text for term in trigger_terms):
            emerging: list[str] = []
            emerging.extend(
                match.group(0).strip()
                for match in re.finditer(
                    r"(?<![A-Za-z0-9])[A-Za-z]{2,}(?:[\s-]+[A-Za-z0-9]{2,}){0,2}(?![A-Za-z0-9])",
                    raw_text,
                )
            )
            emerging.extend(
                value.strip()
                for value in re.findall(r"[“‘\"]([^”’\"]{2,12})[”’\"]", raw_text)
            )
            blocked = {
                "CEO", "CFO", "IPO", "ETF", "A股", "公司", "记者", "证券",
                "NEWS", "HTTP", "HTTPS", "SINA", "THS",
            }
            company_names = set(self._stock_name_map)
            for candidate in emerging:
                normalized = self._normalized_term(candidate)
                if (
                    len(normalized) < 3
                    or candidate.upper() in blocked
                    or candidate in company_names
                    or any(self._normalized_term(existing) == normalized for existing in hints)
                ):
                    continue
                hints.append(candidate)
        return hints[:5]

    def _extract_symbols(self, title: str, content: str) -> list[str]:
        """从文本中提取股票代码。

        策略：
        1. 正则匹配 6 位数字股票代码
        2. 通过公司名映射表查找
        """
        symbols: list[str] = []

        # 正则匹配
        matches = self.SYMBOL_PATTERN.findall(title + " " + content)
        symbols.extend(matches)

        # 公司名映射
        full_text = title + " " + content
        for name, sym in self._stock_name_map.items():
            if name in full_text:
                symbols.append(sym)

        return list(set(symbols))

    def _cluster_similar_events(
        self, events: list[NewsEvent]
    ) -> list[NewsEvent]:
        """聚类相似事件。

        简单策略：关键词重叠率 > 50% 的事件合并。
        """
        if not events:
            return []

        clusters: list[NewsEvent] = []

        for event in events:
            merged = False
            for cluster in clusters:
                if self._events_similar(event, cluster):
                    # 合并到 cluster
                    self._merge_events(cluster, event)
                    merged = True
                    break

            if not merged:
                clusters.append(event)

        # 计算独立来源数
        for cluster in clusters:
            cluster.independent_source_count = len(set(cluster.sources))

        return clusters

    def _events_similar(self, ev1: NewsEvent, ev2: NewsEvent) -> bool:
        """判断两个事件是否相似。"""
        if not ev1.keywords or not ev2.keywords:
            return False

        # 关键词重叠率
        overlap = len(set(ev1.keywords) & set(ev2.keywords))
        min_len = min(len(ev1.keywords), len(ev2.keywords))

        return min_len > 0 and overlap / min_len > 0.5

    def _merge_events(self, base: NewsEvent, other: NewsEvent) -> None:
        """合并事件。"""
        # 合并关键词
        base.keywords = list(set(base.keywords + other.keywords))[:15]
        base.theme_hints = list(dict.fromkeys(base.theme_hints + other.theme_hints))[:5]

        # 合并共现股票
        base.co_occurred_symbols = list(set(base.co_occurred_symbols + other.co_occurred_symbols))

        # 合并来源
        base.sources = list(set(base.sources + other.sources))
        base.source_urls = list(set(base.source_urls + other.source_urls))

        # 合并日期（取最早）
        if other.event_date and (not base.event_date or other.event_date < base.event_date):
            base.event_date = other.event_date

    def _is_independent_source(self, event: NewsEvent) -> bool:
        """判断事件是否有独立来源（非单一转载）。

        规则：
        1. 来源数 >= 2
        2. 或来源中有非转载标记
        """
        return event.independent_source_count >= 2
