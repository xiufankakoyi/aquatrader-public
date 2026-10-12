"""Event models for Research Data Engine v1."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    """Raw event types from providers."""

    QUOTE = "quote"
    KLINE = "kline"
    ORDERBOOK = "orderbook"
    FINANCE_SNAPSHOT = "finance_snapshot"
    COMPANY_PROFILE = "company_profile"
    NEWS = "news"
    REPORT = "report"
    ANNOUNCEMENT = "announcement"
    FINANCIAL = "financial"
    CLS_FLASH = "cls_flash"
    GLOBAL_NEWS = "global_news"
    VALUATION = "valuation"


class ProviderStatusEnum(str, Enum):
    """数据源状态枚举"""

    OK = "ok"
    PARTIAL = "partial"
    DISABLED = "disabled"
    REQUIRES_AUTH = "requires_auth"
    FAILED = "failed"


class EventTopic(str, Enum):
    """Event bus topics."""

    MARKET_QUOTE = "market.quote"
    MARKET_KLINE = "market.kline"
    COMPANY_NEWS = "company.news"
    COMPANY_REPORT = "company.report"
    COMPANY_ANNOUNCEMENT = "company.announcement"
    COMPANY_FINANCIAL = "company.financial"
    EVIDENCE_CREATED = "evidence.created"
    GRAPH_UPDATED = "graph.updated"
    CANDIDATE_UPDATED = "candidate.updated"
    RISK_ALERT = "risk.alert"
    RAW_EVENT = "raw_event"
    NORMALIZED_EVENT = "normalized_event"
    DOCUMENT_SAVED = "document_saved"
    EXTRACTION_DONE = "extraction_done"


class EnvelopeType(str, Enum):
    """Envelope types for event bus."""

    RAW_EVENT = "raw_event"
    NORMALIZED_EVENT = "normalized_event"
    DOCUMENT_SAVED = "document_saved"
    EXTRACTION_DONE = "extraction_done"
    EVIDENCE_CREATED = "evidence_created"
    GRAPH_UPDATED = "graph_updated"
    CANDIDATE_CREATED = "candidate_created"
    RISK_ALERT = "risk_alert"


@dataclass
class RawEvent:
    """Raw event from a data provider.

    Attributes:
        event_id: 唯一事件标识
        provider: 数据提供者名称
        event_type: 事件类型
        symbol: 股票代码
        stock_name: 股票名称
        trade_date: 交易日期 YYYY-MM-DD
        event_time: 事件时间 YYYY-MM-DDTHH:mm:ss
        title: 标题
        summary: 摘要
        content: 正文内容
        source_url: 来源 URL
        file_path: 本地文件路径
        payload: 附加数据字典
        fetched_at: 抓取时间
        quality: 数据质量 ok/partial/failed
        error: 错误信息
        raw_hash: 原始数据哈希
        dedup_key: 去重键
    """

    event_id: str = ""
    provider: str = ""
    event_type: str = ""
    symbol: str = ""
    stock_name: str = ""
    trade_date: str = ""
    event_time: str = ""
    title: str = ""
    summary: str = ""
    content: str = ""
    source_url: str = ""
    file_path: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""
    quality: str = "ok"
    error: Optional[str] = None
    raw_hash: str = ""
    dedup_key: str = ""

    def __post_init__(self) -> None:
        """自动填充 event_id、fetched_at、raw_hash、dedup_key。"""
        if not self.event_id:
            self.event_id = f"raw_{uuid.uuid4().hex[:12]}"
        if not self.fetched_at:
            self.fetched_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not self.raw_hash:
            content = f"{self.provider}:{self.event_type}:{self.symbol}:{self.source_url}:{self.title}"
            self.raw_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        if not self.dedup_key:
            self.dedup_key = f"{self.provider}:{self.raw_hash}"


@dataclass
class RawDocument:
    """Document metadata for PDF/text storage.

    Attributes:
        document_id: 唯一文档标识
        event_id: 关联的 RawEvent ID
        provider: 数据提供者
        document_type: 文档类型
        symbol: 股票代码
        title: 文档标题
        source_url: 来源 URL
        file_path: 本地文件路径
        content_text: 纯文本内容
        content_available: 内容是否可用
        download_status: 下载状态 ok/failed/skipped
        parse_status: 解析状态 ok/failed/skipped
        raw_hash: 原始数据哈希
        created_at: 创建时间
    """

    document_id: str = ""
    event_id: str = ""
    provider: str = ""
    document_type: str = ""
    symbol: str = ""
    title: str = ""
    source_url: str = ""
    file_path: str = ""
    content_text: str = ""
    content_available: bool = False
    download_status: str = "skipped"
    parse_status: str = "skipped"
    raw_hash: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        """自动填充 document_id 和 created_at。"""
        if not self.document_id:
            self.document_id = f"doc_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class NormalizedEvent:
    """Normalized event after dedup and enrichment.

    Attributes:
        event_id: 唯一事件标识
        raw_event_id: 关联的 RawEvent ID
        provider: 数据提供者
        event_type: 事件类型
        entity_type: 实体类型
        entity_id: 实体标识
        symbol: 股票代码
        stock_name: 股票名称
        title: 标题
        summary: 摘要
        content: 正文内容
        event_time: 事件时间
        data_date: 数据日期 YYYY-MM-DD
        source_url: 来源 URL
        file_path: 本地文件路径
        quality: 数据质量
        needs_llm: 是否需要 LLM 处理
        raw_hash: 原始数据哈希
        dedup_key: 去重键
    """

    event_id: str = ""
    raw_event_id: str = ""
    provider: str = ""
    event_type: str = ""
    entity_type: str = ""
    entity_id: str = ""
    symbol: str = ""
    stock_name: str = ""
    title: str = ""
    summary: str = ""
    content: str = ""
    event_time: str = ""
    data_date: str = ""
    source_url: str = ""
    file_path: str = ""
    quality: str = "ok"
    needs_llm: bool = False
    raw_hash: str = ""
    dedup_key: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """自动填充 event_id。"""
        if not self.event_id:
            self.event_id = f"norm_{uuid.uuid4().hex[:12]}"


@dataclass
class ExtractionResult:
    """Result from LLM or rule-based extraction.

    Attributes:
        event_id: 关联的事件 ID
        document_id: 关联的文档 ID
        symbol: 股票代码
        stock_name: 股票名称
        doc_type: 文档类型
        summary: 提取摘要
        key_points: 关键要点列表
        themes: 题材列表
        products: 产品列表
        industries: 行业列表
        catalysts: 催化剂列表
        risks: 风险列表
        moat_claims: 护城河声明列表
        financial_signals: 财务信号列表
        sentiment: 情绪倾向 positive/neutral/negative
        confidence: 置信度 high/medium/low
        evidence_items: 证据条目列表
        extracted_relations: 提取的关系列表
        processor: 处理器类型 rule_based/local_llm
        processed_at: 处理时间
    """

    event_id: str = ""
    document_id: str = ""
    symbol: str = ""
    stock_name: str = ""
    doc_type: str = ""
    summary: str = ""
    key_points: List[str] = field(default_factory=list)
    themes: List[str] = field(default_factory=list)
    products: List[str] = field(default_factory=list)
    industries: List[str] = field(default_factory=list)
    catalysts: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    moat_claims: List[str] = field(default_factory=list)
    financial_signals: List[str] = field(default_factory=list)
    sentiment: str = "neutral"
    confidence: str = "medium"
    evidence_items: List[Dict[str, Any]] = field(default_factory=list)
    extracted_relations: List[Dict[str, Any]] = field(default_factory=list)
    processor: str = "rule_based"
    processed_at: str = ""

    def __post_init__(self) -> None:
        """自动填充 processed_at。"""
        if not self.processed_at:
            self.processed_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class EventEnvelope:
    """Envelope for event bus transport.

    Attributes:
        event_id: 唯一信封标识
        event_type: 信封类型
        topic: 事件主题
        source: 事件来源
        payload: 事件负载数据
        created_at: 创建时间
        trace_id: 追踪 ID
        parent_event_id: 父事件 ID
    """

    event_id: str = ""
    event_type: str = ""
    topic: str = ""
    source: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    trace_id: str = ""
    parent_event_id: Optional[str] = None

    def __post_init__(self) -> None:
        """自动填充 event_id、created_at、trace_id。"""
        if not self.event_id:
            self.event_id = f"env_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not self.trace_id:
            self.trace_id = f"trace_{uuid.uuid4().hex[:8]}"


@dataclass
class ProviderStatus:
    """Status report for a data provider.

    Attributes:
        provider: 提供者名称
        enabled: 是否启用
        status: 状态 ok/partial/disabled/requires_auth/failed
        capabilities: 支持的能力列表
        last_error: 最近一次错误信息
        sample_event_count: 样本事件数量
    """

    provider: str = ""
    enabled: bool = False
    status: str = "disabled"
    capabilities: List[str] = field(default_factory=list)
    last_error: str = ""
    sample_event_count: int = 0
