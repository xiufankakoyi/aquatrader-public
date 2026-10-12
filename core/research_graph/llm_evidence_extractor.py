"""介入点 1:LLM 证据抽取增强(替代纯正则 anchor window)。

架构:本地高召回预筛 → LLM 精排/抽取 → quote 回原文校验。

四阶段:
- Stage 0: 本地高召回预筛,关键词 + 别名 + 主题打分,低阈值过滤,零 token 消耗
- Stage 1: 本地 chunk 召回,按句子切分,保留命中关键词的句子及上下文窗口
- Stage 2: LLM 精排,对每个 chunk 判断 support/counter/neutral/noise
- Stage 3: LLM 四元组抽取,输出 (subject, object, relationship, quote) + confidence
- 后处理: quote 回原文校验,过滤幻觉,转 Evidence 对象

设计原则:
- 第一道闸门是本地规则,极高召回,零 token 消耗
- LLM 只做精排和抽取,处理的文本量已压缩到很小
- quote 强制回原文校验,保证 evidence-bound
- 输出标准 Evidence 对象,完全兼容现有神经元网络
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.research_graph.models import Evidence
from core.research_graph.schema import EvidenceLevel

logger = logging.getLogger(__name__)


# 关键词库(分正证、反证,可迭代扩充)
POSITIVE_KEYWORDS: list[str] = [
    "生产", "供应", "客户", "认证", "送样", "建设", "投产", "扩产",
    "年产", "募投", "投资", "参股", "收购", "子公司", "项目",
    "产能", "吨", "万片", "亿元", "良率", "量产", "交付",
    "原材料", "供应商", "下游", "上游", "工艺", "制造",
]
NEGATIVE_KEYWORDS: list[str] = [
    "暂无", "无相关", "未涉及", "未形成收入", "无计划", "剥离", "终止", "否认",
    "无进一步开发", "尚未", "不在经营范围", "暂停",
]


@dataclass
class EvidenceCandidate:
    """Stage 3 产出的四元组。"""

    subject: str
    subject_type: str
    relationship: str
    object: str
    object_type: str
    stance: str
    quote: str
    sentence_ids: list[int]
    confidence: float
    reason: str = ""


@dataclass
class ChunkRef:
    """Stage 1 产出的 chunk 引用。"""

    chunk_id: str
    source_id: str
    document_id: str
    sentence_ids: list[int]
    chunk_text: str
    span_start: int
    span_end: int


def stage0_high_recall_filter(
    documents: list[dict[str, Any]],
    root_theme: str,
    node_aliases: list[str],
) -> list[dict[str, Any]]:
    """Stage 0: 本地高召回预筛,零 token 消耗。

    保留可能含证据的文章,关键词 + 别名 + 主题打分,低阈值过滤。

    Args:
        documents: 文章列表,每篇含 title / snippet / full_text / url / source_type。
        root_theme: 根主题(如 HBM)。
        node_aliases: 节点别名(canonical_name + aliases)。

    Returns:
        过滤后的文章列表(按 score 降序),每篇追加 _filter_score 字段。
    """
    alias_set = {alias.lower() for alias in node_aliases if alias}
    theme_lower = root_theme.lower() if root_theme else ""

    kept: list[dict[str, Any]] = []
    for doc in documents:
        text_snippet = (
            str(doc.get("title", "")) + " "
            + str(doc.get("snippet", "")) + " "
            + str(doc.get("full_text", ""))[:500]
        ).lower()

        score = 0
        # 正证词命中(+2)
        for kw in POSITIVE_KEYWORDS:
            if kw in text_snippet:
                score += 2
        # 反证词命中(+2,与正证同等重要,保留反证)
        for kw in NEGATIVE_KEYWORDS:
            if kw in text_snippet:
                score += 2
        # 节点别名命中(+3)
        for alias in alias_set:
            if alias and alias in text_snippet:
                score += 3
        # 主题词命中(+1)
        if theme_lower and theme_lower in text_snippet:
            score += 1

        # source_type 权重:公司公告/年报权重翻倍
        source_weight = 1.0
        if doc.get("source_type") in ("announcement", "annual_report",
                                       "semiannual_report", "quarterly_report"):
            source_weight = 2.0
        score *= source_weight

        # 低阈值(1 分)确保高召回
        if score >= 1:
            doc["_filter_score"] = score
            kept.append(doc)

    kept.sort(key=lambda d: d.get("_filter_score", 0), reverse=True)
    return kept


def stage1_local_chunking(
    doc: dict[str, Any],
    positive_kws: list[str] | None = None,
    negative_kws: list[str] | None = None,
    window: int = 2,
) -> list[ChunkRef]:
    """Stage 1: 本地 chunk 召回,按句子切分保留命中关键词的句子及上下文窗口。

    Args:
        doc: 单篇文章,含 doc_id / full_text 等字段。
        positive_kws: 正证关键词,默认用 POSITIVE_KEYWORDS。
        negative_kws: 反证关键词,默认用 NEGATIVE_KEYWORDS。
        window: 命中句前后各取 window 句,默认 2。

    Returns:
        ChunkRef 列表(已合并重叠窗口)。
    """
    positive_kws = positive_kws or POSITIVE_KEYWORDS
    negative_kws = negative_kws or NEGATIVE_KEYWORDS
    full_text = str(doc.get("full_text", ""))
    if not full_text.strip():
        return []

    # 按句号/分号/换行切分,保留索引
    sentences = re.split(r"(?<=[。；;!\n])", full_text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return []

    # 标记每个句子是否命中关键词
    hit = [False] * len(sentences)
    for i, sent in enumerate(sentences):
        sent_lower = sent.lower()
        if any(kw in sent_lower for kw in positive_kws) or \
           any(kw in sent_lower for kw in negative_kws):
            hit[i] = True

    chunks: list[ChunkRef] = []
    used: set[tuple[int, ...]] = set()
    doc_id = str(doc.get("doc_id", doc.get("document_id", "")))
    source_id = str(doc.get("source_id", ""))
    for i, is_hit in enumerate(hit):
        if not is_hit:
            continue
        start = max(0, i - window)
        end = min(len(sentences), i + window + 1)
        sent_ids = list(range(start, end))
        key = tuple(sent_ids)
        if key in used:
            continue
        used.add(key)
        chunk_text = " ".join([sentences[j] for j in sent_ids])
        span_start = full_text.find(sentences[start])
        span_end = full_text.find(sentences[end - 1]) + len(sentences[end - 1]) \
            if span_start >= 0 else -1
        if span_start < 0:
            continue
        chunks.append(ChunkRef(
            chunk_id=f"{doc_id}_chunk_{len(chunks)}",
            source_id=source_id,
            document_id=doc_id,
            sentence_ids=sent_ids,
            chunk_text=chunk_text,
            span_start=span_start,
            span_end=span_end,
        ))
    return chunks


def stage2_llm_rerank_chunks(
    chunks: list[ChunkRef],
    llm_providers: Any = None,
    trace_logger: Any = None,
) -> list[tuple[ChunkRef, str, str]]:
    """Stage 2: LLM 精排,批量对多个 chunk 判断 support/counter/neutral/noise。

    优化:批量化,一次 LLM 调用处理多个 chunk(每批 10 个),
    避免单 chunk 单调用的串行延迟(推理模型每次 2.5s)。
    LLM 失败时保守保留(标记 unknown),避免丢失潜在证据。
    JSON 解析用 robust_json_parse + 1 次重试(RETRY_PROMPT)。

    Args:
        chunks: Stage 1 产出的 ChunkRef 列表。
        llm_providers: LLM provider 列表,None 时从 config 加载。
        trace_logger: ExtractionTraceLogger 实例,用于记录追踪与拒绝。

    Returns:
        (chunk, label, reason) 元组列表,只保留 support/counter/unknown。
    """
    import uuid
    from core.research_graph.json_parse_utils import (
        ParseFailure, RETRY_PROMPT, ExtractionTrace, robust_json_parse,
    )
    from core.utils.llm_usage import call_llm_with_budget

    if not chunks:
        return []

    system_prompt = (
        "你是一个供应链证据分类器。请对给定的多个文本片段进行分类。\n"
        "输出 JSON 数组,每个元素对应输入的片段:\n"
        "[{\"id\": 片段序号, \"label\": \"support|counter|neutral|noise\", \"reason\": \"一句话理由\"}]\n"
        "- support: 文本表明某公司确实生产、供应、投资、认证了某产品/材料/工艺\n"
        "- counter: 文本否认或澄清相关业务(如\"暂未涉及\"\"无相关业务\")\n"
        "- neutral: 无关或非供应链内容\n"
        "- noise: 完全无法理解的片段"
    )

    kept: list[tuple[ChunkRef, str, str]] = []
    # 批处理,每批 20 个 chunk(增大 batch 减少 LLM 调用次数,降低总耗时)
    BATCH_SIZE = 20
    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        # 构造批量输入
        lines = []
        for i, chunk in enumerate(batch):
            lines.append(f"[{i}] {chunk.chunk_text}")
        user_prompt = "请对以下片段分类:\n" + "\n".join(lines)

        trace_id = f"trace_{uuid.uuid4().hex[:10]}"
        parse_status = "success"
        retry_count = 0

        try:
            content = call_llm_with_budget(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                source="evidence_extractor.stage2_rerank",
                providers=llm_providers,
                temperature=0.0,
                max_completion_tokens=1024,
                response_format_json=True,
                timeout=30,
                allow_no_provider=True,
                tier="flash",
            )
            if not content:
                # 无 provider,保守保留全部
                for chunk in batch:
                    kept.append((chunk, "unknown", "no_llm_provider"))
                if trace_logger:
                    trace_logger.record_trace(ExtractionTrace(
                        trace_id=trace_id, chunk_id=f"batch_{batch_start}",
                        parse_status="failed", parse_error_type="no_llm_provider",
                    ))
                continue

            # robust_json_parse 解析
            result = robust_json_parse(content, f"batch_{batch_start}")
            if isinstance(result, ParseFailure):
                # 1 次重试
                retry_count = 1
                retry_content = call_llm_with_budget(
                    messages=[
                        {"role": "system", "content": RETRY_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    source="evidence_extractor.stage2_rerank_retry",
                    providers=llm_providers,
                    temperature=0.0,
                    max_completion_tokens=1024,
                    timeout=30,
                    allow_no_provider=True,
                    tier="flash",
                )
                if retry_content:
                    result = robust_json_parse(retry_content, f"batch_{batch_start}")

            if isinstance(result, ParseFailure):
                # 重试仍失败,保守保留全部
                parse_status = "failed"
                for chunk in batch:
                    kept.append((chunk, "unknown", f"json_parse_failed: {result.error_type}"))
                if trace_logger:
                    trace_logger.record_rejected(
                        chunk_id=f"batch_{batch_start}", source_id="",
                        document_id="", stage="stage2_llm_rerank",
                        failure=result, retry_count=retry_count,
                    )
            else:
                # 解析成功:按 id 映射回 chunk
                if retry_count > 0:
                    parse_status = "retry_success"
                # 构建 id → label 映射
                label_map: dict[int, tuple[str, str]] = {}
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, dict):
                            idx = item.get("id")
                            if isinstance(idx, int):
                                label_map[idx] = (
                                    str(item.get("label", "noise")).lower(),
                                    str(item.get("reason", "")),
                                )
                for i, chunk in enumerate(batch):
                    label, reason = label_map.get(i, ("noise", "未分类"))
                    if label in ("support", "counter"):
                        kept.append((chunk, label, reason))
        except Exception as exc:
            logger.warning("stage2 rerank failed for batch %d: %s", batch_start, exc)
            parse_status = "failed"
            for chunk in batch:
                kept.append((chunk, "unknown", f"llm_failed: {exc}"))

        if trace_logger:
            trace_logger.record_trace(ExtractionTrace(
                trace_id=trace_id, chunk_id=f"batch_{batch_start}",
                parse_status=parse_status, retry_count=retry_count,
            ))
    return kept


def stage3_llm_extract(
    reranked: list[tuple[ChunkRef, str, str]],
    node_name: str,
    llm_providers: Any = None,
    trace_logger: Any = None,
) -> list[EvidenceCandidate]:
    """Stage 3: LLM 四元组抽取,批量处理多个 chunk。

    优化:批量化,一次 LLM 调用处理多个 chunk(每批 5 个),
    避免单 chunk 单调用的串行延迟。
    要求返回 quote 和 sentence_ids,保证可定位回原文。
    JSON 解析用 robust_json_parse + 1 次重试(RETRY_PROMPT)。

    Args:
        reranked: Stage 2 产出的 (chunk, label, reason) 列表。
        node_name: 当前神经元 canonical_name,作为抽取锚点。
        llm_providers: LLM provider 列表。
        trace_logger: ExtractionTraceLogger 实例,用于记录追踪与拒绝。

    Returns:
        EvidenceCandidate 列表。
    """
    import uuid
    from core.research_graph.json_parse_utils import (
        ParseFailure, RETRY_PROMPT, ExtractionTrace, robust_json_parse,
    )
    from core.utils.llm_usage import call_llm_with_budget

    if not reranked:
        return []

    system_prompt = (
        "你是供应链关系抽取器。从给定的多个文本片段中抽取 (公司, 产品/材料/工艺, 关系) 三元组。\n"
        "输出 JSON 数组,每项必须包含:\n"
        "{\n"
        "  \"chunk_id\": 片段序号,\n"
        "  \"subject\": \"公司全称\",\n"
        "  \"subject_type\": \"COMPANY\",\n"
        "  \"relationship\": \"BUILDS_CAPACITY_FOR | SUPPLIES | OWNS | PRODUCES | CERTIFIES | DENIES | MENTIONS | PARTNERS_WITH\",\n"
        "  \"object\": \"产品/材料/工艺名称\",\n"
        "  \"object_type\": \"MATERIAL | PROCESS | EQUIPMENT | PRODUCT\",\n"
        "  \"stance\": \"support | counter\",\n"
        "  \"quote\": \"原文中支持该关系的完整句子(不得改写!)\",\n"
        "  \"confidence\": 0.0-1.0,\n"
        "  \"reason\": \"简短解释为什么是这个关系\"\n"
        "}\n"
        "规则:\n"
        "1. quote 必须从文本中原样复制,不得修改\n"
        "2. 如果涉及否定(如\"暂未涉及XX业务\"),关系用 DENIES,stance=counter\n"
        "3. 仅抽取文本明确支持的关系,不确定的不要输出\n"
        "4. 不输出任何投资建议(买入/卖出/仓位/止盈/止损)"
    )

    all_candidates: list[EvidenceCandidate] = []
    # 批处理,每批 5 个 chunk(stage3 JSON 输出较长,5 个四元组约 750 tokens,
    # 留足余量避免截断;stage2 已批量压缩到 1 批,stage3 2 批可接受)
    BATCH_SIZE = 5
    for batch_start in range(0, len(reranked), BATCH_SIZE):
        batch = reranked[batch_start:batch_start + BATCH_SIZE]
        lines = []
        for i, (chunk, _, _) in enumerate(batch):
            lines.append(f"[片段{i}] {chunk.chunk_text}")
        user_prompt = (
            f"节点名称: {node_name}\n"
            "请从以下片段抽取关系:\n" + "\n".join(lines)
        )

        trace_id = f"trace_{uuid.uuid4().hex[:10]}"
        parse_status = "success"
        retry_count = 0
        accepted = 0

        try:
            content = call_llm_with_budget(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                source="evidence_extractor.stage3_extract",
                providers=llm_providers,
                temperature=0.0,
                max_completion_tokens=1024,
                response_format_json=True,
                timeout=40,
                allow_no_provider=True,
                tier="flash",
            )
            if not content:
                continue

            # robust_json_parse 解析
            result = robust_json_parse(content, f"batch_{batch_start}")
            if isinstance(result, ParseFailure):
                # 1 次重试
                retry_count = 1
                retry_content = call_llm_with_budget(
                    messages=[
                        {"role": "system", "content": RETRY_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    source="evidence_extractor.stage3_extract_retry",
                    providers=llm_providers,
                    temperature=0.0,
                    max_completion_tokens=1024,
                    timeout=40,
                    allow_no_provider=True,
                    tier="flash",
                )
                if retry_content:
                    result = robust_json_parse(retry_content, f"batch_{batch_start}")

            if isinstance(result, ParseFailure):
                parse_status = "failed"
                if trace_logger:
                    trace_logger.record_rejected(
                        chunk_id=f"batch_{batch_start}", source_id="",
                        document_id="", stage="stage3_llm_extract",
                        failure=result, retry_count=retry_count,
                    )
                continue

            if retry_count > 0:
                parse_status = "retry_success"
            items = result if isinstance(result, list) else result.get("items", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                quote = str(item.get("quote", "")).strip()
                if not quote:
                    continue
                cand = EvidenceCandidate(
                    subject=str(item.get("subject", "")).strip(),
                    subject_type=str(item.get("subject_type", "COMPANY")).strip(),
                    relationship=str(item.get("relationship", "MENTIONS")).strip(),
                    object=str(item.get("object", "")).strip(),
                    object_type=str(item.get("object_type", "MATERIAL")).strip(),
                    stance=str(item.get("stance", "support")).strip().lower(),
                    quote=quote,
                    sentence_ids=[],
                    confidence=float(item.get("confidence", 0.5)),
                    reason=str(item.get("reason", "")).strip(),
                )
                # stance 合法性校验
                if cand.stance not in ("support", "counter"):
                    cand.stance = "support"
                all_candidates.append(cand)
                accepted += 1
        except Exception as exc:
            logger.warning("stage3 extract failed for batch %d: %s", batch_start, exc)
            parse_status = "failed"

        if trace_logger:
            trace_logger.record_trace(ExtractionTrace(
                trace_id=trace_id, chunk_id=f"batch_{batch_start}",
                parse_status=parse_status, retry_count=retry_count,
                accepted_count=accepted,
                rejected_count=1 if parse_status == "failed" else 0,
            ))
    return all_candidates


def validate_and_build_evidence(
    candidates: list[EvidenceCandidate],
    docs_by_id: dict[str, dict[str, Any]],
    source_type_default: str = "search_snippet",
) -> list[Evidence]:
    """后处理:quote 回原文校验,过滤幻觉,转 Evidence 对象。

    Args:
        candidates: Stage 3 产出的 EvidenceCandidate 列表。
        docs_by_id: doc_id / document_id → doc 字典,用于回原文校验。
        source_type_default: 默认 source_type。

    Returns:
        通过校验的 Evidence 列表。
    """
    valid: list[Evidence] = []
    for cand in candidates:
        # 校验 quote 在原文中出现(过滤幻觉)
        doc = None
        for d in docs_by_id.values():
            full_text = str(d.get("full_text", ""))
            if cand.quote and cand.quote in full_text:
                doc = d
                break
        if doc is None:
            # quote 不在原文中,幻觉,丢弃
            logger.info(" hallucinated quote dropped: %s", cand.quote[:60])
            continue

        full_text = str(doc.get("full_text", ""))
        span_start = full_text.find(cand.quote)
        span_end = span_start + len(cand.quote)

        # 构造 Evidence 对象
        source_type = str(doc.get("source_type", source_type_default))
        evidence_id = f"ev_llm_{uuid.uuid4().hex[:12]}"
        metadata = {
            "stance": cand.stance,
            "subject": cand.subject,
            "subject_type": cand.subject_type,
            "relationship": cand.relationship,
            "object": cand.object,
            "object_type": cand.object_type,
            "confidence": cand.confidence,
            "sentence_ids": cand.sentence_ids,
            "span_start": span_start,
            "span_end": span_end,
            "extraction_method": "llm_quad",
            "reason": cand.reason,
        }
        # supports / contradicts 用 doc 关联的 node_id
        node_ids = doc.get("node_ids", [])
        ev = Evidence(
            evidence_id=evidence_id,
            source_type=source_type,
            source_title=str(doc.get("title", "")),
            source_id=str(doc.get("source_id", "")),
            document_id=str(doc.get("doc_id", doc.get("document_id", ""))),
            source_url=str(doc.get("url", "")),
            publish_date=str(doc.get("publish_date", "")),
            quote=cand.quote,
            summary=f"{cand.subject} {cand.relationship} {cand.object}",
            evidence_level=(
                EvidenceLevel.LIKELY.value
                if source_type in {"announcement", "annual_report", "semiannual_report", "quarterly_report"}
                and cand.confidence >= 0.7
                else EvidenceLevel.SPECULATIVE.value
            ),
            supports=node_ids if cand.stance == "support" else [],
            contradicts=node_ids if cand.stance == "counter" else [],
            freshness="current",
            extracted_by="llm_extractor",
            verified_by="quote_match",
            metadata=metadata,
        )
        valid.append(ev)
    return valid


def enhanced_evidence_extraction(
    search_results: list[dict[str, Any]],
    root_theme: str,
    node_aliases: list[str],
    node_name: str,
    llm_providers: Any = None,
    source_type_default: str = "search_snippet",
    trace_logger: Any = None,
) -> list[Evidence]:
    """介入点 1 主入口:本地高召回预筛 → LLM 精排/抽取 → quote 回原文校验。

    替代 GrowthMiningEngine._extract_evidence 的纯正则 anchor window,
    产出标准 Evidence 对象,完全兼容现有神经元网络。

    Args:
        search_results: 搜索文章列表,每篇含 title / snippet / full_text / url / source_type / doc_id。
        root_theme: 根主题(如 HBM)。
        node_aliases: 节点别名(canonical_name + aliases)。
        node_name: 当前神经元 canonical_name,作为抽取锚点。
        llm_providers: LLM provider 列表,None 时从 config 加载(tier="flash")。
        source_type_default: 默认 source_type。
        trace_logger: ExtractionTraceLogger 实例,记录追踪与拒绝。

    Returns:
        通过校验的 Evidence 列表(已过滤幻觉)。
    """
    # Stage 0: 本地高召回预筛
    docs = stage0_high_recall_filter(search_results, root_theme, node_aliases)
    if not docs:
        return []

    # Stage 1: 本地 chunk 召回
    all_chunks: list[ChunkRef] = []
    for doc in docs:
        chunks = stage1_local_chunking(doc)
        all_chunks.extend(chunks)
    if not all_chunks:
        return []

    # Stage 2: LLM 精排(带 trace_logger)
    reranked = stage2_llm_rerank_chunks(
        all_chunks, llm_providers=llm_providers, trace_logger=trace_logger,
    )
    if not reranked:
        return []

    # Stage 3: LLM 四元组抽取(带 trace_logger)
    candidates = stage3_llm_extract(
        reranked, node_name, llm_providers=llm_providers, trace_logger=trace_logger,
    )
    if not candidates:
        return []

    # 后处理:quote 回原文校验 + 转 Evidence
    docs_by_id: dict[str, dict[str, Any]] = {}
    for d in docs:
        doc_id = str(d.get("doc_id", d.get("document_id", "")))
        if doc_id:
            docs_by_id[doc_id] = d
    return validate_and_build_evidence(
        candidates, docs_by_id, source_type_default=source_type_default,
    )
