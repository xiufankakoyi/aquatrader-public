"""小批量处理器 v2 — 支持规则引擎、本地 LLM、混合模式（含交叉验证）。

三种模式:
- rule_based: 纯规则引擎
- local_llm: 纯 LLM（不可用时降级规则引擎）
- hybrid: 先规则后 LLM，交叉验证合并（默认，LLM 可用时）

Hybrid 流程:
1. 执行 rule_based 抽取 → 规则结果集合 R
2. 执行 local_llm 语义抽取 → LLM 原始集合 L
3. 对齐与交叉验证
4. 合并去重
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from core.events.models import ExtractionResult, NormalizedEvent
from core.llm_processing.extraction_schema import LLMExtractionOutput
from core.llm_processing.grounding_validator import validate_evidence_items
from core.llm_processing.llm_cache import LLMCache
from core.llm_processing.llm_extraction_client import LLMExtractionClient
from core.llm_processing.prompt_templates import PROMPT_VERSION, build_extraction_prompt, get_system_prompt
from core.llm_processing.rule_based_extractor import RuleBasedExtractor
from core.llm_processing.text_chunker import TextChunker

logger = logging.getLogger(__name__)


@dataclass
class CrossValidationStats:
    """交叉验证统计。

    Attributes:
        cross_validated: 规则+LLM 双重确认数
        rule_only: 仅规则发现数
        llm_only: 仅 LLM 发现数
        total_evidence: 总证据数
    """

    cross_validated: int = 0
    rule_only: int = 0
    llm_only: int = 0
    total_evidence: int = 0


class SmallBatchProcessor:
    """小批量事件处理器 v2。

    支持 rule_based / local_llm / hybrid 三种模式。
    LLM 可用时默认 hybrid，否则 rule_based。

    Args:
        provider: 处理方式 rule_based / local_llm / hybrid
        llm_api_base: 本地 LLM API 地址
        cache_dir: 缓存目录
        max_llm_items: 单批最大 LLM 处理数
        max_chars_per_chunk: 单块最大字符数
    """

    def __init__(
        self,
        provider: str = "rule_based",
        llm_api_base: Optional[str] = None,
        cache_dir: str = "data/llm_cache/extractions",
        max_llm_items: int = 5,
        max_chars_per_chunk: int = 2000,
    ) -> None:
        self.provider = provider
        self.rule_extractor = RuleBasedExtractor()
        self._chunker = TextChunker()
        self.llm_cache = LLMCache(cache_dir)
        self.max_llm_items = max_llm_items
        self.max_chars_per_chunk = max_chars_per_chunk

        # LLM 配置优先级: 显式参数 > config/secrets.py > 环境变量
        self.llm_api_base = llm_api_base or self._load_llm_api_base()
        self.llm_model_name = self._load_llm_model_name()
        self._llm_available = bool(self.llm_api_base)

        # LLM 抽取客户端
        self._llm_client = LLMExtractionClient(
            api_base=self.llm_api_base,
            model_name=self.llm_model_name,
        )

        # 自动选择模式
        if self.provider == "hybrid" and not self._llm_available:
            self.provider = "rule_based"
            logger.info("LLM 不可用，hybrid 模式降级为 rule_based")

        # 交叉验证统计
        self._cv_stats = CrossValidationStats()

    @staticmethod
    def _load_llm_api_base() -> str:
        """从 config/secrets.py 加载 LLM API 地址。

        优先级: LOCAL_LLM_API_BASE > LLM_API_BASE > 环境变量。

        Returns:
            LLM API 地址字符串
        """
        try:
            from config.secrets import Secrets
            return Secrets.LOCAL_LLM_API_BASE or Secrets.LLM_API_BASE or os.environ.get("LOCAL_LLM_API_BASE", "")
        except ImportError:
            return os.environ.get("LOCAL_LLM_API_BASE", "")

    @staticmethod
    def _load_llm_model_name() -> str:
        """从 config/secrets.py 加载 LLM 模型名称。

        Returns:
            LLM 模型名称字符串
        """
        try:
            from config.secrets import Secrets
            return getattr(Secrets, 'LOCAL_LLM_MODEL_NAME', '') or Secrets.LLM_MODEL_NAME or os.environ.get("LOCAL_LLM_MODEL", "default")
        except ImportError:
            return os.environ.get("LOCAL_LLM_MODEL", "default")

    @property
    def cv_stats(self) -> CrossValidationStats:
        """获取交叉验证统计。"""
        return self._cv_stats

    @property
    def llm_stats(self) -> Dict[str, int]:
        """获取 LLM 调用统计。"""
        return {
            "llm_call_count": self._llm_client.stats.llm_call_count,
            "success": self._llm_client.stats.success,
            "failure": self._llm_client.stats.failure,
            "json_repair": self._llm_client.stats.json_repair,
            "compliance_drop": self._llm_client.stats.compliance_drop,
            "grounding_drop": self._llm_client.stats.grounding_drop,
            "fallback_rule_based": self._llm_client.stats.fallback_rule_based,
            "cache_hit": self._llm_client.stats.cache_hit,
            "prompt_tokens_est": self._llm_client.stats.prompt_tokens_est,
            "completion_tokens_est": self._llm_client.stats.completion_tokens_est,
            "total_tokens_est": self._llm_client.stats.total_tokens_est,
        }

    def process_batch(
        self,
        events: List[NormalizedEvent],
        batch_size: int = 10,
    ) -> List[ExtractionResult]:
        """批量处理归一化事件。

        流程:
        1. 检查缓存，跳过已处理的事件
        2. 未缓存的事件按 batch_size 分批处理
        3. 根据 provider 选择规则引擎、LLM 或混合模式
        4. LLM 失败时降级到规则引擎
        5. 缓存所有结果

        Args:
            events: 归一化事件列表
            batch_size: 批量大小

        Returns:
            ExtractionResult 列表
        """
        if not events:
            return []

        # 只处理 needs_llm=True 的事件
        needs_llm_events = [e for e in events if getattr(e, "needs_llm", True)]
        if not needs_llm_events:
            return []

        results: List[ExtractionResult] = []
        uncached: List[NormalizedEvent] = []

        # 1. 检查缓存
        for event in needs_llm_events:
            cache_key = event.raw_hash or event.event_id
            if cache_key and self.llm_cache.has(
                cache_key,
                chunk_text_hash=self._compute_chunk_hash(event.content or ""),
                prompt_version=PROMPT_VERSION,
                model_name=self.llm_model_name,
            ):
                cached = self.llm_cache.get(
                    cache_key,
                    chunk_text_hash=self._compute_chunk_hash(event.content or ""),
                    prompt_version=PROMPT_VERSION,
                    model_name=self.llm_model_name,
                )
                if cached is not None:
                    results.append(cached)
                    self._llm_client.stats.cache_hit += 1
                    continue
            uncached.append(event)

        if not uncached:
            logger.info("全部 %d 条事件已缓存，跳过处理", len(events))
            return results

        logger.info("缓存命中 %d/%d，待处理 %d 条", len(results), len(needs_llm_events), len(uncached))

        # 2. 分批处理
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i : i + batch_size]
            try:
                batch_results = self._process_batch_internal(batch)
                results.extend(batch_results)
            except Exception as exc:
                logger.error("批量处理失败 (batch %d-%d): %s", i, i + len(batch), exc)

        return results

    def _process_batch_internal(self, batch: List[NormalizedEvent]) -> List[ExtractionResult]:
        """内部批量处理逻辑。

        Args:
            batch: 一批事件

        Returns:
            抽取结果列表
        """
        if self.provider == "hybrid" and self._llm_available:
            results = self._process_hybrid(batch)
        elif self.provider == "local_llm" and self._llm_available:
            results = self._process_with_llm(batch)
            if not results:
                logger.warning("LLM 处理全部失败，降级到规则引擎")
                results = self._process_with_rules(batch)
        else:
            results = self._process_with_rules(batch)

        # 缓存所有结果
        for result in results:
            cache_key = result.event_id or ""
            if cache_key:
                self.llm_cache.set(
                    cache_key,
                    result,
                    chunk_text_hash="",
                    prompt_version=PROMPT_VERSION,
                    model_name=self.llm_model_name,
                )

        return results

    def _process_with_rules(self, events: List[NormalizedEvent]) -> List[ExtractionResult]:
        """使用规则引擎处理事件。

        长文本（>max_chars_per_chunk）先切块分别抽取再合并结果。

        Args:
            events: 事件列表

        Returns:
            抽取结果列表
        """
        results: List[ExtractionResult] = []
        for event in events:
            try:
                content = event.content or ""
                title = event.title or ""
                symbol = event.symbol or ""
                stock_name = event.stock_name or ""

                if len(content) > self.max_chars_per_chunk:
                    chunks = self._chunker.chunk(
                        text=content,
                        title=title,
                        symbol=symbol,
                        chunk_size=self.max_chars_per_chunk,
                        overlap=200,
                    )
                    chunk_results = []
                    for chunk in chunks:
                        r = self.rule_extractor.extract(
                            title=title,
                            content=chunk["text"],
                            symbol=symbol,
                            stock_name=stock_name,
                        )
                        chunk_results.append(r)
                    result = self._merge_chunk_results(chunk_results, event)
                    results.append(result)
                else:
                    result = self.rule_extractor.extract(
                        title=title,
                        content=content,
                        symbol=symbol,
                        stock_name=stock_name,
                    )
                    result.event_id = event.event_id
                    result.document_id = getattr(event, "raw_event_id", "")
                    result.doc_type = event.entity_type or ""
                    result.processor = "rule_based"
                    results.append(result)
            except Exception as exc:
                logger.warning("规则引擎处理失败 (symbol=%s): %s", event.symbol, exc)
                results.append(ExtractionResult(
                    event_id=event.event_id,
                    document_id=getattr(event, "raw_event_id", ""),
                    symbol=event.symbol,
                    stock_name=event.stock_name,
                    processor="rule_based",
                ))
        return results

    def _process_with_llm(self, events: List[NormalizedEvent]) -> List[ExtractionResult]:
        """使用本地 LLM 处理事件。

        通过 LLMExtractionClient 调用，含合规扫描和锚定校验。

        Args:
            events: 事件列表

        Returns:
            抽取结果列表
        """
        results: List[ExtractionResult] = []

        for event in events:
            try:
                content = event.content or ""
                title = event.title or ""

                # 长文本切块
                if len(content) > self.max_chars_per_chunk:
                    chunks = self._chunker.chunk(
                        text=content,
                        title=title,
                        symbol=event.symbol or "",
                        chunk_size=self.max_chars_per_chunk,
                        overlap=200,
                    )
                else:
                    chunks = [{"text": content, "chunk_index": 0, "total_chunks": 1}]

                all_evidence_items: List[Dict] = []
                all_themes: List[str] = []
                all_risks: List[str] = []
                all_catalysts: List[str] = []
                all_moat_claims: List[str] = []
                all_financial_signals: List[str] = []
                summary_parts: List[str] = []
                llm_success = False

                for chunk in chunks:
                    metadata = {
                        "symbol": event.symbol or "",
                        "stock_name": event.stock_name or "",
                        "title": title,
                        "chunk_id": str(chunk.get("chunk_index", 0)),
                    }

                    extraction, grounding_results = self._llm_client.extract(
                        chunk_text=chunk["text"],
                        source_text=content,
                        metadata=metadata,
                    )

                    if extraction is not None:
                        llm_success = True
                        all_themes.extend(extraction.themes)
                        all_risks.extend(extraction.risks)
                        all_catalysts.extend(extraction.catalysts)
                        all_moat_claims.extend(extraction.moat_claims)
                        all_financial_signals.extend(extraction.financial_signals)
                        all_evidence_items.extend(extraction.evidence_items)
                        if extraction.summary:
                            summary_parts.append(extraction.summary)

                if llm_success:
                    # 为 evidence_items 添加交叉验证标记
                    for item in all_evidence_items:
                        item["cross_validation_status"] = "llm_only"
                        item["review_status"] = "pending_human_review"

                    result = ExtractionResult(
                        event_id=event.event_id,
                        document_id=getattr(event, "raw_event_id", ""),
                        symbol=event.symbol,
                        stock_name=event.stock_name,
                        doc_type=event.entity_type or "",
                        summary=" ".join(summary_parts)[:300],
                        themes=list(dict.fromkeys(all_themes)),
                        risks=list(dict.fromkeys(all_risks)),
                        catalysts=list(dict.fromkeys(all_catalysts)),
                        moat_claims=list(dict.fromkeys(all_moat_claims)),
                        financial_signals=list(dict.fromkeys(all_financial_signals)),
                        evidence_items=all_evidence_items,
                        sentiment="neutral",
                        confidence="medium",
                        processor="local_llm",
                    )
                    results.append(result)
                else:
                    # LLM 失败，降级规则引擎
                    rule_result = self.rule_extractor.extract(
                        title=title,
                        content=content,
                        symbol=event.symbol or "",
                        stock_name=event.stock_name or "",
                    )
                    rule_result.event_id = event.event_id
                    rule_result.doc_type = event.entity_type or ""
                    rule_result.processor = "rule_based_fallback"
                    results.append(rule_result)

            except Exception as exc:
                logger.warning("LLM 处理异常 (symbol=%s): %s", event.symbol, exc)
                try:
                    rule_result = self.rule_extractor.extract(
                        title=event.title,
                        content=event.content,
                        symbol=event.symbol,
                        stock_name=event.stock_name,
                    )
                    rule_result.event_id = event.event_id
                    rule_result.processor = "rule_based_fallback"
                    results.append(rule_result)
                except Exception:
                    results.append(ExtractionResult(
                        event_id=event.event_id,
                        symbol=event.symbol,
                        stock_name=event.stock_name,
                        processor="rule_based_fallback",
                    ))

        return results

    def _process_hybrid(self, events: List[NormalizedEvent]) -> List[ExtractionResult]:
        """混合模式处理：先规则后 LLM，交叉验证合并。

        流程:
        1. 执行 rule_based 抽取 → R
        2. 执行 local_llm 语义抽取 → L
        3. 交叉验证对齐
        4. 合并去重

        Args:
            events: 事件列表

        Returns:
            抽取结果列表
        """
        # 1. 规则引擎抽取
        rule_results = self._process_with_rules(events)

        # 2. LLM 抽取
        llm_results = self._process_with_llm(events)

        # 3. 交叉验证合并
        merged_results = []
        for rule_r, llm_r in zip(rule_results, llm_results):
            merged = self._cross_validate_and_merge(rule_r, llm_r)
            merged_results.append(merged)

        return merged_results

    def _cross_validate_and_merge(
        self,
        rule_result: ExtractionResult,
        llm_result: ExtractionResult,
    ) -> ExtractionResult:
        """交叉验证并合并规则与 LLM 结果。

        对齐逻辑:
        - L 中每条证据在 R 中找语义相同的（相同 evidence_type + 实体/主题一致）
        - 找到 → cross_validated，confidence 取较高者，evidence_text 优先规则原文
        - 未找到 → llm_only，confidence 强制 low
        - R 中有而 L 无 → rule_only，保持原置信度

        Args:
            rule_result: 规则引擎结果
            llm_result: LLM 结果

        Returns:
            合并后的 ExtractionResult
        """
        merged_items: List[Dict[str, Any]] = []
        matched_rule_indices: Set[int] = set()

        # 构建 R 的索引
        rule_items = rule_result.evidence_items or []

        # 对 L 中每条证据找 R 中的匹配
        for llm_item in llm_result.evidence_items or []:
            if not isinstance(llm_item, dict):
                continue

            llm_type = llm_item.get("category", "")
            llm_label = llm_item.get("label", "")
            llm_ev_text = llm_item.get("evidence_text", "")

            matched = False
            for ri, rule_item in enumerate(rule_items):
                if ri in matched_rule_indices:
                    continue
                if not isinstance(rule_item, dict):
                    continue

                rule_type = rule_item.get("category", "")
                rule_label = rule_item.get("label", "")

                # 相同 evidence_type + 标签/主题一致
                if llm_type == rule_type and (
                    llm_label == rule_label
                    or llm_label in rule_label
                    or rule_label in llm_label
                ):
                    # cross_validated
                    merged_items.append({
                        **llm_item,
                        "cross_validation_status": "cross_validated",
                        "confidence": max(
                            self._confidence_to_float(llm_item.get("confidence", "low")),
                            self._confidence_to_float(rule_item.get("confidence", "low")),
                        ),
                        "evidence_text": rule_item.get("evidence_text", llm_ev_text),
                    })
                    matched_rule_indices.add(ri)
                    matched = True
                    self._cv_stats.cross_validated += 1
                    break

            if not matched:
                # llm_only
                merged_items.append({
                    **llm_item,
                    "cross_validation_status": "llm_only",
                    "confidence": 0.2,  # 强制 low
                    "review_status": "pending_human_review",
                })
                self._cv_stats.llm_only += 1

        # R 中有而 L 无的
        for ri, rule_item in enumerate(rule_items):
            if ri in matched_rule_indices:
                continue
            if not isinstance(rule_item, dict):
                continue
            merged_items.append({
                **rule_item,
                "cross_validation_status": "rule_only",
            })
            self._cv_stats.rule_only += 1

        self._cv_stats.total_evidence += len(merged_items)

        # 合并 themes/risks/catalysts/moat_claims
        merged_themes = list(dict.fromkeys(rule_result.themes + llm_result.themes))
        merged_risks = list(dict.fromkeys(rule_result.risks + llm_result.risks))
        merged_catalysts = list(dict.fromkeys(rule_result.catalysts + llm_result.catalysts))
        merged_moat = list(dict.fromkeys(rule_result.moat_claims + llm_result.moat_claims))
        merged_financial = list(dict.fromkeys(rule_result.financial_signals + llm_result.financial_signals))

        # 合并 summary
        summary = llm_result.summary or rule_result.summary

        return ExtractionResult(
            event_id=rule_result.event_id or llm_result.event_id,
            document_id=rule_result.document_id or llm_result.document_id,
            symbol=rule_result.symbol or llm_result.symbol,
            stock_name=rule_result.stock_name or llm_result.stock_name,
            doc_type=rule_result.doc_type or llm_result.doc_type,
            summary=summary[:300],
            key_points=list(dict.fromkeys(rule_result.key_points + llm_result.key_points)),
            themes=merged_themes,
            products=list(dict.fromkeys(rule_result.products + llm_result.products)),
            industries=list(dict.fromkeys(rule_result.industries + llm_result.industries)),
            catalysts=merged_catalysts,
            risks=merged_risks,
            moat_claims=merged_moat,
            financial_signals=merged_financial,
            evidence_items=merged_items,
            extracted_relations=list(dict.fromkeys(
                [str(r) for r in (rule_result.extracted_relations + llm_result.extracted_relations)]
            )),
            sentiment=llm_result.sentiment if llm_result.processor == "local_llm" else rule_result.sentiment,
            confidence=llm_result.confidence if llm_result.processor == "local_llm" else rule_result.confidence,
            processor="hybrid",
        )

    @staticmethod
    def _confidence_to_float(confidence) -> float:
        """将置信度转换为浮点数。

        Args:
            confidence: 置信度值（字符串或数字）

        Returns:
            浮点数置信度
        """
        if isinstance(confidence, (int, float)):
            return float(confidence)
        mapping = {"high": 1.0, "medium": 0.7, "low": 0.4}
        return mapping.get(str(confidence).lower(), 0.4)

    def _merge_chunk_results(
        self,
        chunk_results: List[ExtractionResult],
        event: NormalizedEvent,
    ) -> ExtractionResult:
        """合并多个切块的抽取结果。

        对 themes / risks / catalysts / moat_claims 去重合并，
        根据风险与催化剂数量加权判定整体情绪，根据证据数量判定置信度。

        Args:
            chunk_results: 各切块的抽取结果列表
            event: 原始事件（用于填充 ID 等元数据）

        Returns:
            合并后的 ExtractionResult
        """
        merged = ExtractionResult(
            event_id=event.event_id,
            document_id=getattr(event, "raw_event_id", ""),
            symbol=event.symbol,
            stock_name=event.stock_name,
            doc_type=event.entity_type or "",
            summary=chunk_results[0].summary if chunk_results else "",
            key_points=[],
            themes=[],
            products=[],
            industries=[],
            catalysts=[],
            risks=[],
            moat_claims=[],
            financial_signals=[],
            evidence_items=[],
            extracted_relations=[],
            processor="rule_based_chunked",
        )

        seen_themes: set = set()
        seen_risks: set = set()
        seen_catalysts: set = set()
        seen_moats: set = set()

        for r in chunk_results:
            for t in r.themes or []:
                if t not in seen_themes:
                    merged.themes.append(t)
                    seen_themes.add(t)
            for rk in r.risks or []:
                if rk not in seen_risks:
                    merged.risks.append(rk)
                    seen_risks.add(rk)
            for c in r.catalysts or []:
                if c not in seen_catalysts:
                    merged.catalysts.append(c)
                    seen_catalysts.add(c)
            for m in r.moat_claims or []:
                if m not in seen_moats:
                    merged.moat_claims.append(m)
                    seen_moats.add(m)
            merged.evidence_items.extend(r.evidence_items or [])
            merged.key_points.extend(r.key_points or [])

        # 根据风险与催化剂数量加权判定情绪
        risk_w = len(merged.risks) * 1.5
        cat_w = len(merged.catalysts)
        if cat_w > risk_w:
            merged.sentiment = "positive"
        elif risk_w > cat_w:
            merged.sentiment = "negative"

        # 根据证据数量判定置信度
        if len(merged.evidence_items) >= 5:
            merged.confidence = "high"
        elif len(merged.evidence_items) >= 2:
            merged.confidence = "medium"

        return merged

    @staticmethod
    def _compute_chunk_hash(text: str) -> str:
        """计算文本块的哈希值。

        Args:
            text: 文本内容

        Returns:
            哈希值字符串
        """
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
