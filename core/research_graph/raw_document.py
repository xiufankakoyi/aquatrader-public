"""统一原始文档表示。

消除 evidence_miner.SourceDocument 和 knowledge_loop_engine dict 之间的表示不一致，
所有文档流转统一使用 RawDocument。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawDocument:
    """统一原始文档表示。

    字段取 evidence_miner.SourceDocument 和 knowledge_loop_engine dict 的并集，
    命名以 evidence_miner.SourceDocument 为基准（已有 searchable_text 方法）。

    Attributes:
        document_id: 文档唯一 ID。
        source_type: 来源类型（如 official/news/research_report）。
        title: 文档标题。
        content: 文档正文内容。
        publish_date: 发布日期（YYYY-MM-DD 或 ISO 格式）。
        url: 原文 URL。
        source_id: 来源 ID。
        topic: 研究主题。
        summary: 摘要。
        related_companies: 相关公司列表。
        related_themes: 相关主题列表。
        collected_at: 采集时间。
        raw_hash: 原始内容哈希（用于去重）。
        source_payload: 原始来源载荷（保留原始响应数据）。
        metadata: 额外元数据。
    """

    document_id: str
    source_type: str
    title: str
    content: str
    publish_date: str = ""
    url: str = ""
    source_id: str = ""
    topic: str = ""
    summary: str = ""
    related_companies: list[str] = field(default_factory=list)
    related_themes: list[str] = field(default_factory=list)
    collected_at: str = ""
    raw_hash: str = ""
    source_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def searchable_text(self) -> str:
        """返回可用于文本匹配的拼接文本。

        将 title、summary、content 拼接，用于证据匹配和关键词检索。
        """
        parts = [self.title, self.summary, self.content]
        return " ".join(p for p in parts if p).strip()
