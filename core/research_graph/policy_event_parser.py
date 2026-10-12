"""政策/事件冲击证据解析器(Step 2)。

把政策发布、宏观数据披露、监管公告等来源文本解析为带 event_date 与
causal_status 的规范 Evidence,使其可被 EvidenceLedger 与 GraphVerifier
按"政策冲击"语义统一处理。

设计原则(KISS):
- 只做规则解析,不引入额外依赖。
- 输入:GrowthMiningEngine 产出的 source_row(dict) + 原始文本。
- 输出:Evidence 实例列表(支持同一文档包含多个事件)。
- 兼容性:source_type 不是政策类时返回空列表,主流程不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.research_graph.models import Evidence, utc_now
from core.research_graph.schema import CausalStatus, EvidenceLevel


# 政策/事件类 source_type 白名单。命中其一即视为事件证据候选。
POLICY_SOURCE_TYPES: frozenset[str] = frozenset({
    "policy_release",
    "fomc_statement",
    "fomc_minutes",
    "central_bank_speech",
    "regulatory_announcement",
    "macro_data_release",
})

# 政策事件关键词 → shock_type 映射。用于在 quote 命中关键词时打标。
_SHOCK_KEYWORD_MAP: list[tuple[str, str]] = [
    ("加息", "rate_hike"),
    ("降息", "rate_cut"),
    ("上调", "rate_hike"),
    ("下调", "rate_cut"),
    ("降准", "rrr_cut"),
    ("提高", "tighten"),
    ("收紧", "tighten"),
    ("宽松", "ease"),
    ("扩表", "balance_sheet_expand"),
    ("缩表", "balance_sheet_shrink"),
    ("surprise", "surprise"),
    ("超预期", "surprise"),
    ("不及预期", "negative_surprise"),
]

# 日期正则:支持 YYYY-MM-DD、YYYY/MM/DD、YYYY年MM月DD日。
_DATE_PATTERNS = [
    (re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"), lambda m: f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"),
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"), lambda m: f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"),
]

# 标题中常见事件关键词,用于回退匹配。
_TITLE_KEYWORDS = ["声明", "纪要", "公告", "决议", "讲话", "披露", "发布", "调整", "修订"]


@dataclass
class PolicyEventExtraction:
    """政策事件抽取中间结果。"""

    event_date: str
    shock_type: str
    quote: str
    summary: str
    span_start: int
    span_end: int


def is_policy_source(source_row: dict[str, Any]) -> bool:
    """判断 source_row 是否为政策事件类来源。

    Args:
        source_row: GrowthMiningEngine._persist_source 产出的字典。

    Returns:
        True 表示应交给 policy_event_parser 处理。
    """
    source_type = str(source_row.get("provider_type") or "").lower()
    meta = source_row.get("metadata") or {}
    content_origin = str(meta.get("content_origin") or "").lower()
    if source_type in POLICY_SOURCE_TYPES:
        return True
    if content_origin in POLICY_SOURCE_TYPES:
        return True
    # 标题命中政策关键词也视为候选。
    title = str(source_row.get("title") or "")
    return any(kw in title for kw in _TITLE_KEYWORDS)


def _extract_event_date(text: str, source_row: dict[str, Any]) -> str:
    """从正文或 source_row 元数据中提取事件日期。

    优先级:metadata.event_date > retrieved_at 中的日期 > 正文首个日期。
    """
    meta = source_row.get("metadata") or {}
    explicit = str(meta.get("event_date") or "").strip()
    if explicit:
        return explicit
    retrieved_at = str(source_row.get("retrieved_at") or "")
    if retrieved_at:
        for pattern, formatter in _DATE_PATTERNS:
            m = pattern.search(retrieved_at)
            if m:
                return formatter(m)
    for pattern, formatter in _DATE_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return formatter(m)
    return ""


def _extract_shock_type(quote: str, source_type: str) -> str:
    """根据 quote 命中关键词推断 shock_type,未命中时返回 source_type 兜底。"""
    for kw, shock in _SHOCK_KEYWORD_MAP:
        if kw in (quote or ""):
            return shock
    return source_type


def _scan_policy_events(text: str, source_row: dict[str, Any]) -> list[PolicyEventExtraction]:
    """扫描文本,抽取政策事件片段。

    策略(KISS):
    - 找到包含日期的段落作为事件锚点。
    - 以日期前后 240 字符为 quote 窗口。
    - 同一日期只取首个匹配,避免重复。

    Args:
        text: 原始正文。
        source_row: source_row dict。

    Returns:
        PolicyEventExtraction 列表(可能为空)。
    """
    if not text:
        return []
    extractions: list[PolicyEventExtraction] = []
    seen_dates: set[str] = set()
    for pattern, formatter in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            date_str = formatter(m)
            if date_str in seen_dates:
                continue
            start = max(0, m.start() - 160)
            end = min(len(text), m.end() + 280)
            quote = text[start:end]
            shock_type = _extract_shock_type(quote, str(source_row.get("provider_type") or "policy_release"))
            extractions.append(PolicyEventExtraction(
                event_date=date_str,
                shock_type=shock_type,
                quote=quote,
                summary=f"政策事件 {date_str} {shock_type}",
                span_start=start,
                span_end=end,
            ))
            seen_dates.add(date_str)
    if not extractions:
        # 退化:整个文本作为单个事件片段。
        event_date = _extract_event_date(text, source_row) or datetime.utcnow().strftime("%Y-%m-%d")
        quote = text[:480]
        shock_type = _extract_shock_type(quote, str(source_row.get("provider_type") or "policy_release"))
        extractions.append(PolicyEventExtraction(
            event_date=event_date,
            shock_type=shock_type,
            quote=quote,
            summary=f"政策事件 {event_date} {shock_type}",
            span_start=0,
            span_end=min(len(text), 480),
        ))
    return extractions


def parse_policy_event(source_row: dict[str, Any], text: str) -> list[Evidence]:
    """把政策类 source_row 解析为规范 Evidence 列表。

    Args:
        source_row: GrowthMiningEngine._persist_source 产出的字典。
        text: 该 source 对应的原始正文。

    Returns:
        Evidence 列表;source_row 非政策类时返回空列表。
    """
    if not is_policy_source(source_row):
        return []
    source_type = str(source_row.get("provider_type") or "policy_release").lower()
    if source_type not in POLICY_SOURCE_TYPES:
        source_type = "policy_release"
    extractions = _scan_policy_events(text, source_row)
    evidence_list: list[Evidence] = []
    for idx, ext in enumerate(extractions):
        evidence_id = f"pev_{source_row.get('source_id', 'src')}_{idx}"
        evidence_list.append(Evidence(
            evidence_id=evidence_id,
            source_type=source_type,
            source_title=str(source_row.get("title") or "policy_event"),
            source_id=str(source_row.get("source_id") or ""),
            document_id=str(source_row.get("document_id") or ""),
            source_url=str(source_row.get("url") or ""),
            publish_date=ext.event_date,
            quote=ext.quote,
            summary=ext.summary,
            evidence_level=EvidenceLevel.LIKELY.value,
            extracted_by="policy_event_parser",
            verified_by="rule_verifier",
            event_date=ext.event_date,
            # 政策事件证据先标注为 OBSERVATIONAL_ASSOCIATION,
            # 后续时序因果服务(Step 8)再升级为 TEMPORAL_PRECEDENCE/QUASI_EXPERIMENTAL_EFFECT。
            causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
            metadata={
                "shock_type": ext.shock_type,
                "span_start": ext.span_start,
                "span_end": ext.span_end,
                "content_origin": "policy_event_parser",
                "parsed_at": utc_now(),
            },
        ))
    return evidence_list


def evidence_from_policy_source(source_row: dict[str, Any], text: str) -> list[Evidence]:
    """对外别名,供 environment / growth_mining 调用。"""
    return parse_policy_event(source_row, text)
