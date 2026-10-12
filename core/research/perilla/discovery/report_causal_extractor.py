"""P1 研报因果链抽取 — hybrid 模式（LLM 优先 + 规则降级）。

从 raw_documents_latest.json 抽取结构化观点，输出 4 个 JSON：
- report_claims_latest.json
- causal_chains_latest.json
- evidence_items_latest.json
- failure_conditions_latest.json

hybrid 模式：
- LLM 可用时优先用 LLMExtractionClient + P1 Prompt 抽取 claim/causal_chain
- LLM 失败时降级到信号词规则
- LLM 输出经 check_compliance + 锚定校验
- LLM 调用走 LLMCache 缓存（data/llm_cache/extractions/）

字段语义约束（遵循 spec.md 全局约束表）：
- confidence（claim/chain）：low / medium，禁止 high
- evidence_level（evidence_items）：hypothesis / weak_signal / probable（仅 official）
- probable 仅表示来源强，不表示关系已验证
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.llm_processing.llm_cache import LLMCache
from core.llm_processing.llm_extraction_client import LLMExtractionClient
from core.llm_processing.prompt_templates import (
    P1_PROMPT_VERSION,
    build_p1_extraction_prompt,
    check_compliance,
    get_p1_system_prompt,
)
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    record_llm_usage,
)

logger = logging.getLogger(__name__)

# 默认目录
_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_OUTPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_CACHE_DIR = Path("data/llm_cache/extractions")

# 信号词列表（规则降级用）
_SIGNAL_WORDS = [
    "带动", "推动", "受益", "利好", "有望", "预计", "可能", "建议关注",
    "催化", "验证", "导入", "扩产", "涨价", "国产替代", "对日替代",
]

# 高置信度信号词（confidence = medium）
_HIGH_CONFIDENCE_SIGNALS = ["公告", "披露", "已", "确认", "签订", "客户认证"]

# 低置信度信号词（confidence = low）
_LOW_CONFIDENCE_SIGNALS = ["预计", "有望", "可能", "建议关注"]

# 因果连接词
_CAUSE_CONNECTORS = ["因为", "由于", "受益于", "随着", "在"]
_EFFECT_CONNECTORS = ["带动", "推动", "提升", "利好"]

# 产品平台关键词（用于 failure_condition 生成）
_PRODUCT_PLATFORMS = ["GB200", "H100", "H200", "B200", "NVL72", "AI服务器", "AI芯片"]

# 材料关键词（用于 failure_condition 生成）
_MATERIAL_KEYWORDS = [
    "材料", "树脂", "铜箔", "衬底", "PTFE", "磷化铟",
    "电子布", "玻纤布", "基材",
]

# 国产替代关键词
_SUBSTITUTION_KEYWORDS = ["国产替代", "对日替代"]

# source_type 映射到 evidence_type
_SOURCE_TYPE_MAP = {
    "公告": "official",
    "年报": "official",
    "公司官网": "official",
    "研报": "report",
    "新闻": "news",
    "公众号": "weak_text",
    "社区": "weak_text",
}

# evidence_level 映射（probable 仅表示来源强，不表示关系已验证）
_EVIDENCE_LEVEL_MAP = {
    "official": "probable",
    "report": "weak_signal",
    "news": "weak_signal",
    "weak_text": "hypothesis",
    "unknown": "hypothesis",
}

# risk_type 枚举
_RISK_TYPES = {"demand_risk", "evidence_risk", "substitution_risk", "valuation_risk", "unknown"}


# =============================================================================
# P1 LLM 抽取器（复用 LLMExtractionClient 配置 + P1 Prompt + LLMCache）
# =============================================================================


class _P1LLMExtractor:
    """P1 LLM 抽取器 — 复用 LLMExtractionClient 配置，使用 P1 Prompt。

    不算新建 LLM 客户端，因为复用 LLMExtractionClient 的：
    - api_base / model_name / api_key / timeout 配置
    - _parse_json 方法
    - available 属性
    - stats 统计

    新增的是 P1 Prompt 和 LLMCache 集成。

    Args:
        client: LLMExtractionClient 实例
        cache: LLMCache 实例
    """

    def __init__(
        self,
        client: LLMExtractionClient,
        cache: Optional[LLMCache] = None,
    ) -> None:
        self.client = client
        self.cache = cache or LLMCache(str(_DEFAULT_CACHE_DIR))
        # 内存缓存：避免 extract_report_claims 和 extract_causal_chains 重复调用 LLM
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        """LLM 是否可用。"""
        return self.client.available

    def extract(self, document: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Dict]]:
        """对单个文档执行 P1 LLM 抽取。

        Args:
            document: raw_document 字典

        Returns:
            (parsed_result, grounding_results)
            parsed_result 含 claims 和 causal_chains 两个列表
            LLM 失败时返回 (None, [])
        """
        doc_id = str(document.get("document_id", ""))
        if doc_id and doc_id in self._memory_cache:
            self.client.stats.cache_hit += 1
            return self._memory_cache[doc_id], []

        if not self.available:
            return None, []

        self.client.stats.llm_call_count += 1

        chunk_text = self._build_chunk_text(document)
        source_text = chunk_text
        metadata = {
            "title": str(document.get("title", "")),
            "source_type": str(document.get("source_type", "")),
            "document_id": doc_id,
        }

        # 计算缓存 key（复用 LLMCache 的 key 计算逻辑）
        raw_hash = str(document.get("raw_hash", ""))
        chunk_text_hash = LLMExtractionClient.compute_chunk_hash(chunk_text)
        cache_key = LLMCache._compute_cache_key(
            raw_hash, chunk_text_hash, P1_PROMPT_VERSION, self.client.model_name
        )
        cache_file = self.cache._cache_path(cache_key)

        # 检查持久化缓存
        if cache_file.exists():
            try:
                cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached_data.get("prompt_version") == P1_PROMPT_VERSION:
                    self.client.stats.cache_hit += 1
                    self._memory_cache[doc_id] = cached_data.get("parsed_result", {})
                    return cached_data.get("parsed_result", {}), []
            except Exception:
                pass

        # 调用 LLM API（用 P1 Prompt）
        raw_output = self._call_p1_api(chunk_text, metadata)
        if raw_output is None:
            self.client.stats.failure += 1
            self.client.stats.fallback_rule_based += 1
            return None, []

        # JSON 解析（复用 LLMExtractionClient._parse_json）
        parsed = self.client._parse_json(raw_output)
        if parsed is None:
            self.client.stats.failure += 1
            self.client.stats.fallback_rule_based += 1
            return None, []

        # 合规扫描
        output_text = json.dumps(parsed, ensure_ascii=False)
        if not check_compliance(output_text):
            self.client.stats.compliance_drop += 1
            self.client.stats.fallback_rule_based += 1
            logger.warning("P1 LLM 输出命中合规禁用词，整条丢弃")
            return None, []

        # 锚定校验（对 claims 和 causal_chains 的 evidence_quote）
        parsed, grounding_results = self._validate_grounding(parsed, source_text)

        # 写入持久化缓存
        try:
            cache_data = {
                "raw_hash": raw_hash,
                "chunk_text_hash": chunk_text_hash,
                "prompt_version": P1_PROMPT_VERSION,
                "model_name": self.client.model_name,
                "parsed_result": parsed,
            }
            cache_file.write_text(
                json.dumps(cache_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("P1 缓存写入失败: %s", exc)

        # 写入内存缓存
        self._memory_cache[doc_id] = parsed

        self.client.stats.success += 1
        return parsed, grounding_results

    def _call_p1_api(self, chunk_text: str, metadata: Dict[str, str]) -> Optional[str]:
        """用 P1 Prompt 调用 LLM API。

        复用 LLMExtractionClient 的 api_base/model_name/api_key/timeout 配置。

        Args:
            chunk_text: 文本块
            metadata: 元数据

        Returns:
            LLM 原始输出文本，失败返回 None
        """
        try:
            import requests

            system_prompt = get_p1_system_prompt()
            user_prompt = build_p1_extraction_prompt(chunk_text, metadata)

            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if self.client.api_key:
                headers["Authorization"] = f"Bearer {self.client.api_key}"

            body: Dict[str, Any] = {
                "model": self.client.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
            }

            try:
                body["response_format"] = {"type": "json_object"}
                resp = requests.post(
                    f"{self.client.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.client.timeout,
                )
            except Exception:
                body.pop("response_format", None)
                resp = requests.post(
                    f"{self.client.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.client.timeout,
                )

            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        except ImportError:
            logger.warning("requests 库未安装，无法调用 LLM")
            return None
        except Exception as exc:
            logger.warning("P1 LLM API 调用失败: %s", exc)
            return None

    def _call_p1_api(self, chunk_text: str, metadata: Dict[str, str]) -> Optional[str]:
        """Call P1 prompt through all configured providers with rough token accounting."""
        try:
            import requests
        except ImportError:
            logger.warning("requests is not installed; cannot call P1 LLM")
            return None

        system_prompt = get_p1_system_prompt()
        user_prompt = build_p1_extraction_prompt(chunk_text, metadata)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        prompt_tokens = estimate_messages_tokens(messages)
        try:
            check_llm_budget_or_raise(prompt_tokens, 0, source="P1LLMExtractor._call_p1_api")
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return None

        providers = getattr(self.client, "providers", []) or []
        for provider in providers:
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if provider.api_key:
                headers["Authorization"] = f"Bearer {provider.api_key}"
            body: Dict[str, Any] = {
                "model": provider.model_name,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            try:
                resp = requests.post(
                    f"{provider.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.client.timeout,
                )
                if resp.status_code >= 400:
                    body.pop("response_format", None)
                    resp = requests.post(
                        f"{provider.api_base}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self.client.timeout,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = str(data["choices"][0]["message"]["content"])
                completion_tokens = estimate_tokens(content)
                self.client.stats.prompt_tokens_est += prompt_tokens
                self.client.stats.completion_tokens_est += completion_tokens
                self.client.stats.total_tokens_est = (
                    self.client.stats.prompt_tokens_est + self.client.stats.completion_tokens_est
                )
                record_llm_usage(
                    source="P1LLMExtractor._call_p1_api",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=completion_tokens,
                    success=True,
                )
                return content
            except Exception as exc:
                self.client.stats.prompt_tokens_est += prompt_tokens
                self.client.stats.total_tokens_est = (
                    self.client.stats.prompt_tokens_est + self.client.stats.completion_tokens_est
                )
                record_llm_usage(
                    source="P1LLMExtractor._call_p1_api",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                logger.warning("P1 LLM provider failed (%s): %s", provider.name, exc)
        return None

    def _validate_grounding(
        self,
        parsed: Dict[str, Any],
        source_text: str,
    ) -> Tuple[Dict[str, Any], List[Dict]]:
        """对 claims 和 causal_chains 的 evidence_quote 做锚定校验。

        丢弃无法锚定的条目。

        Args:
            parsed: LLM 解析后的 JSON
            source_text: 原文

        Returns:
            (校验后的 parsed, grounding_results)
        """
        from core.llm_processing.grounding_validator import validate_evidence_items

        grounding_results: List[Dict] = []

        # 校验 claims
        valid_claims: List[Dict[str, Any]] = []
        for claim in parsed.get("claims", []):
            quote = str(claim.get("evidence_quote", ""))
            if not quote:
                continue
            item = {"evidence_text": quote, "source_chunk_offset": ""}
            validated, results = validate_evidence_items([item], source_text)
            for r in results:
                grounding_results.append({
                    "status": r.status,
                    "grounding_type": r.grounding_type,
                    "reason": r.reason,
                })
            if validated:
                claim["evidence_quote"] = validated[0].get("evidence_text", quote)
                valid_claims.append(claim)
            else:
                self.client.stats.grounding_drop += 1
        parsed["claims"] = valid_claims

        # 校验 causal_chains
        valid_chains: List[Dict[str, Any]] = []
        for chain in parsed.get("causal_chains", []):
            quote = str(chain.get("evidence_quote", ""))
            if not quote:
                continue
            item = {"evidence_text": quote, "source_chunk_offset": ""}
            validated, results = validate_evidence_items([item], source_text)
            for r in results:
                grounding_results.append({
                    "status": r.status,
                    "grounding_type": r.grounding_type,
                    "reason": r.reason,
                })
            if validated:
                chain["evidence_quote"] = validated[0].get("evidence_text", quote)
                valid_chains.append(chain)
            else:
                self.client.stats.grounding_drop += 1
        parsed["causal_chains"] = valid_chains

        return parsed, grounding_results

    @staticmethod
    def _build_chunk_text(document: Dict[str, Any]) -> str:
        """从 document 构建待抽取的文本块。

        Args:
            document: raw_document 字典

        Returns:
            拼接后的文本块
        """
        parts = [
            str(document.get("title", "")),
            str(document.get("summary", "")),
            str(document.get("content_text", "")),
        ]
        return " ".join(p for p in parts if p)


# =============================================================================
# 公开函数
# =============================================================================


def load_raw_documents(input_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读取 raw_documents_latest.json。

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/

    Returns:
        raw_document 字典列表，文件缺失时返回空列表
    """
    path = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    file_path = path / "raw_documents_latest.json"
    if not file_path.exists():
        logger.warning("raw_documents_latest.json 不存在: %s", file_path)
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        logger.warning("raw_documents_latest.json 格式异常，期望 list")
        return []
    except Exception as exc:
        logger.error("加载 raw_documents_latest.json 失败: %s", exc)
        return []


def extract_report_claims(
    documents: List[Dict[str, Any]],
    llm_extractor: Optional[_P1LLMExtractor] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """hybrid 模式抽取 claim。

    LLM 可用时优先用 LLM 抽取，失败时降级到信号词规则。
    confidence 由 LLM 输出（经合规校验），LLM 不可用时用规则分级（low/medium，禁止 high）。

    Args:
        documents: raw_document 字典列表
        llm_extractor: P1 LLM 抽取器，None 时纯规则模式

    Returns:
        (claims, stats) - claims 列表和 LLM 调用统计
    """
    claims: List[Dict[str, Any]] = []
    stats = {
        "llm_call_count": 0,
        "llm_success": 0,
        "llm_failure": 0,
        "llm_fallback": 0,
        "cache_hit": 0,
        "compliance_drop": 0,
    }

    for doc in documents:
        llm_used = False
        if llm_extractor and llm_extractor.available:
            parsed, _ = llm_extractor.extract(doc)
            if parsed and parsed.get("claims"):
                for claim_data in parsed["claims"]:
                    claim = _build_claim_from_llm(claim_data, doc)
                    if claim:
                        claims.append(claim)
                llm_used = True
                stats["llm_success"] += 1

        if not llm_used:
            rule_claims = _extract_claims_by_rules(doc)
            claims.extend(rule_claims)
            if llm_extractor and llm_extractor.available:
                stats["llm_fallback"] += 1

    if llm_extractor:
        client_stats = llm_extractor.client.stats
        stats["llm_call_count"] = client_stats.llm_call_count
        stats["cache_hit"] = client_stats.cache_hit
        stats["compliance_drop"] = client_stats.compliance_drop

    return claims, stats


def extract_causal_chains(
    claims: List[Dict[str, Any]],
    documents: List[Dict[str, Any]],
    llm_extractor: Optional[_P1LLMExtractor] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """hybrid 模式拆解 cause/mechanism/effect。

    LLM 可用时优先用 LLM 拆解，失败时降级到因果连接词规则。
    无法抽取时字段写"未明确"，禁止编造。

    Args:
        claims: claim 字典列表
        documents: raw_document 字典列表
        llm_extractor: P1 LLM 抽取器，None 时纯规则模式

    Returns:
        (causal_chains, stats) - causal_chain 列表和 LLM 调用统计
    """
    chains: List[Dict[str, Any]] = []
    stats = {"llm_success": 0, "llm_fallback": 0}

    doc_map: Dict[str, Dict[str, Any]] = {
        str(d.get("document_id", "")): d for d in documents
    }

    for claim in claims:
        doc_id = str(claim.get("document_id", ""))
        doc = doc_map.get(doc_id, {})
        llm_used = False

        if llm_extractor and llm_extractor.available and doc:
            parsed, _ = llm_extractor.extract(doc)
            if parsed and parsed.get("causal_chains"):
                for chain_data in parsed["causal_chains"]:
                    chain = _build_chain_from_llm(chain_data, claim, doc)
                    if chain:
                        chains.append(chain)
                llm_used = True
                stats["llm_success"] += 1

        if not llm_used:
            rule_chain = _extract_causal_chain_by_rules(claim, doc)
            if rule_chain:
                chains.append(rule_chain)
                if llm_extractor and llm_extractor.available:
                    stats["llm_fallback"] += 1

    return chains, stats


def build_evidence_items(
    claims: List[Dict[str, Any]],
    causal_chains: List[Dict[str, Any]],
    documents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按 source_type 映射 evidence_type 和 evidence_level。

    evidence_level 映射规则：
    - official（公告/年报/官网）→ probable（仅表示来源强，不表示关系已验证）
    - report（研报）/ news（新闻）→ weak_signal
    - weak_text（公众号/社区）/ unknown → hypothesis

    Args:
        claims: claim 字典列表
        causal_chains: causal_chain 字典列表
        documents: raw_document 字典列表

    Returns:
        evidence_item 字典列表
    """
    doc_map: Dict[str, Dict[str, Any]] = {
        str(d.get("document_id", "")): d for d in documents
    }
    items: List[Dict[str, Any]] = []

    # 从 claim 生成 evidence_item
    for claim in claims:
        doc_id = str(claim.get("document_id", ""))
        doc = doc_map.get(doc_id, {})
        source_type_raw = str(doc.get("source_type", "") or claim.get("source_type", ""))
        evidence_type = _map_evidence_type(source_type_raw)
        evidence_level = _EVIDENCE_LEVEL_MAP.get(evidence_type, "hypothesis")

        items.append({
            "evidence_id": f"evi_{_short_hash(claim.get('claim_id', ''))}",
            "document_id": doc_id,
            "claim_id": str(claim.get("claim_id", "")),
            "chain_id": "",
            "quote": str(claim.get("evidence_quote", "")),
            "source_type": source_type_raw,
            "evidence_type": evidence_type,
            "evidence_level": evidence_level,
            "reason": f"claim 抽取自 {source_type_raw or '未知来源'}",
        })

    # 从 causal_chain 生成 evidence_item
    for chain in causal_chains:
        doc_id = str(chain.get("document_id", ""))
        doc = doc_map.get(doc_id, {})
        source_type_raw = str(doc.get("source_type", ""))
        evidence_type = _map_evidence_type(source_type_raw)
        evidence_level = _EVIDENCE_LEVEL_MAP.get(evidence_type, "hypothesis")

        items.append({
            "evidence_id": f"evi_{_short_hash(chain.get('chain_id', ''))}",
            "document_id": doc_id,
            "claim_id": str(chain.get("claim_id", "")),
            "chain_id": str(chain.get("chain_id", "")),
            "quote": str(chain.get("evidence_quote", "")),
            "source_type": source_type_raw,
            "evidence_type": evidence_type,
            "evidence_level": evidence_level,
            "reason": f"causal_chain 抽取自 {source_type_raw or '未知来源'}",
        })

    return items


def generate_failure_conditions(
    claims: List[Dict[str, Any]],
    causal_chains: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按三类规则生成 failure_condition。

    规则：
    1. cause 包含产品平台（GB200/H100/AI服务器 等）→ demand_risk
    2. beneficiary_node 属于材料类 → evidence_risk
    3. claim_text 包含国产替代/对日替代 → substitution_risk
    4. 其他 → unknown

    Args:
        claims: claim 字典列表
        causal_chains: causal_chain 字典列表

    Returns:
        failure_condition 字典列表
    """
    conditions: List[Dict[str, Any]] = []
    chain_by_claim: Dict[str, Dict[str, Any]] = {
        str(c.get("claim_id", "")): c for c in causal_chains
    }

    for claim in claims:
        claim_id = str(claim.get("claim_id", ""))
        claim_text = str(claim.get("claim_text", ""))
        chain = chain_by_claim.get(claim_id, {})

        failure_condition, risk_type = _generate_single_failure_condition(claim, chain)
        severity = "medium" if risk_type == "unknown" else "high"

        conditions.append({
            "failure_id": f"fc_{_short_hash(claim_id)}",
            "claim_id": claim_id,
            "chain_id": str(chain.get("chain_id", "")),
            "failure_condition": failure_condition,
            "risk_type": risk_type,
            "severity": severity,
        })

    return conditions


def run_report_causal_extractor(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """串联 P1 全流程，输出 4 个 JSON，返回 dict summary。

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/
        output_dir: 输出目录，默认为 data/reports/perilla_discovery/
        use_llm: 是否启用 LLM（False 时纯规则模式）

    Returns:
        dict summary，含 module / success / outputs / counts / llm_stats
    """
    in_dir = Path(input_dir) if input_dir else _DEFAULT_INPUT_DIR
    out_dir = Path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 加载文档
        documents = load_raw_documents(in_dir)
        if not documents:
            logger.warning("P1 无 raw_documents，输出空 JSON")

        # 初始化 LLM 抽取器（hybrid 模式）
        llm_extractor: Optional[_P1LLMExtractor] = None
        if use_llm:
            try:
                client = LLMExtractionClient()
                cache = LLMCache(str(_DEFAULT_CACHE_DIR))
                llm_extractor = _P1LLMExtractor(client, cache)
                if not llm_extractor.available:
                    logger.info("P1 LLM 不可用，降级为纯规则模式")
                    llm_extractor = None
            except Exception as exc:
                logger.warning("P1 LLM 初始化失败，降级为纯规则模式: %s", exc)
                llm_extractor = None

        # 抽取 claims
        claims, claims_stats = extract_report_claims(documents, llm_extractor)

        # 抽取 causal_chains
        chains, chains_stats = extract_causal_chains(claims, documents, llm_extractor)

        # 构建 evidence_items
        evidence_items = build_evidence_items(claims, chains, documents)

        # 生成 failure_conditions
        failure_conditions = generate_failure_conditions(claims, chains)

        # 写入 4 个 JSON
        outputs: List[str] = []
        file_map = {
            "report_claims_latest.json": claims,
            "causal_chains_latest.json": chains,
            "evidence_items_latest.json": evidence_items,
            "failure_conditions_latest.json": failure_conditions,
        }
        for filename, data in file_map.items():
            file_path = out_dir / filename
            file_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            outputs.append(str(file_path))

        # 汇总 LLM 统计
        llm_stats = {
            "claims": claims_stats,
            "chains": chains_stats,
        }
        if llm_extractor:
            llm_stats["client_stats"] = {
                "llm_call_count": llm_extractor.client.stats.llm_call_count,
                "success": llm_extractor.client.stats.success,
                "failure": llm_extractor.client.stats.failure,
                "fallback_rule_based": llm_extractor.client.stats.fallback_rule_based,
                "cache_hit": llm_extractor.client.stats.cache_hit,
                "compliance_drop": llm_extractor.client.stats.compliance_drop,
                "grounding_drop": llm_extractor.client.stats.grounding_drop,
                "prompt_tokens_est": llm_extractor.client.stats.prompt_tokens_est,
                "completion_tokens_est": llm_extractor.client.stats.completion_tokens_est,
                "total_tokens_est": llm_extractor.client.stats.total_tokens_est,
            }

        return {
            "module": "report_causal_extractor",
            "success": True,
            "outputs": outputs,
            "counts": {
                "raw_documents": len(documents),
                "report_claims": len(claims),
                "causal_chains": len(chains),
                "evidence_items": len(evidence_items),
                "failure_conditions": len(failure_conditions),
            },
            "llm_stats": llm_stats,
        }
    except Exception as exc:
        logger.error("run_report_causal_extractor 失败: %s", exc, exc_info=True)
        return {
            "module": "report_causal_extractor",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }


# =============================================================================
# 内部辅助函数
# =============================================================================


def _build_claim_from_llm(claim_data: Dict[str, Any], document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 LLM 输出构建 claim 字典。

    Args:
        claim_data: LLM 输出的单条 claim
        document: 关联的 raw_document

    Returns:
        claim 字典，或 None（数据无效时）
    """
    claim_text = str(claim_data.get("claim_text", "")).strip()
    if not claim_text:
        return None

    confidence = str(claim_data.get("confidence", "low")).strip().lower()
    # 禁止 high，降级为 medium
    if confidence not in ("low", "medium"):
        confidence = "low" if confidence == "high" else "low"

    return {
        "claim_id": f"claim_{_short_hash(claim_text + str(document.get('document_id', '')))}",
        "document_id": str(document.get("document_id", "")),
        "title": str(document.get("title", "")),
        "claim_text": claim_text,
        "mentioned_terms": [str(t) for t in claim_data.get("mentioned_terms", []) if t],
        "mentioned_companies": [str(c) for c in claim_data.get("mentioned_companies", []) if c],
        "signal_words": [str(s) for s in claim_data.get("signal_words", []) if s],
        "source_type": str(document.get("source_type", "")),
        "published_at": str(document.get("published_at", "")),
        "evidence_quote": str(claim_data.get("evidence_quote", "")),
        "confidence": confidence,
    }


def _build_chain_from_llm(
    chain_data: Dict[str, Any],
    claim: Dict[str, Any],
    document: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """从 LLM 输出构建 causal_chain 字典。

    Args:
        chain_data: LLM 输出的单条 causal_chain
        claim: 关联的 claim
        document: 关联的 raw_document

    Returns:
        causal_chain 字典，或 None（数据无效时）
    """
    cause = str(chain_data.get("cause", "未明确")).strip() or "未明确"
    effect = str(chain_data.get("effect", "未明确")).strip() or "未明确"
    if cause == "未明确" and effect == "未明确":
        return None

    confidence = str(chain_data.get("confidence", "low")).strip().lower()
    if confidence not in ("low", "medium"):
        confidence = "low"

    chain_id = f"chain_{_short_hash(cause + effect + str(claim.get('claim_id', '')))}"
    return {
        "chain_id": chain_id,
        "claim_id": str(claim.get("claim_id", "")),
        "document_id": str(document.get("document_id", "")),
        "cause": cause,
        "mechanism": str(chain_data.get("mechanism", "未明确")).strip() or "未明确",
        "effect": effect,
        "beneficiary_node": str(chain_data.get("beneficiary_node", "未明确")).strip() or "未明确",
        "evidence_quote": str(chain_data.get("evidence_quote", "")),
        "confidence": confidence,
        "status": "pending",
    }


def _extract_claims_by_rules(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """规则降级：用信号词从文档中抽取 claim。

    Args:
        document: raw_document 字典

    Returns:
        claim 字典列表
    """
    title = str(document.get("title", ""))
    summary = str(document.get("summary", ""))
    content = str(document.get("content_text", ""))
    text = " ".join(p for p in [title, summary, content] if p)

    if not text.strip():
        return []

    # 检测信号词
    found_signals = [w for w in _SIGNAL_WORDS if w in text]
    if not found_signals:
        return []

    # confidence 分级
    if any(s in text for s in _HIGH_CONFIDENCE_SIGNALS):
        confidence = "medium"
    elif any(s in text for s in _LOW_CONFIDENCE_SIGNALS):
        confidence = "low"
    else:
        confidence = "low"

    # 抽取 evidence_quote（信号词周围的文本）
    evidence_quote = _extract_quote_around_signal(text, found_signals[0])

    # 抽取 mentioned_terms（简单匹配大写缩写和技术术语）
    mentioned_terms = _extract_terms_from_text(text)

    # 抽取 mentioned_companies
    mentioned_companies = list(document.get("related_companies", []))

    claim_text = summary or title or evidence_quote or text[:100]
    claim_id = f"claim_{_short_hash(claim_text + str(document.get('document_id', '')))}"

    return [{
        "claim_id": claim_id,
        "document_id": str(document.get("document_id", "")),
        "title": title,
        "claim_text": claim_text,
        "mentioned_terms": mentioned_terms,
        "mentioned_companies": mentioned_companies,
        "signal_words": found_signals,
        "source_type": str(document.get("source_type", "")),
        "published_at": str(document.get("published_at", "")),
        "evidence_quote": evidence_quote,
        "confidence": confidence,
    }]


def _extract_causal_chain_by_rules(
    claim: Dict[str, Any],
    document: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """规则降级：用因果连接词从 claim/document 中抽取 causal_chain。

    Args:
        claim: claim 字典
        document: 关联的 raw_document

    Returns:
        causal_chain 字典，或 None（无法抽取时）
    """
    text = " ".join(p for p in [
        str(claim.get("claim_text", "")),
        str(document.get("summary", "")),
        str(document.get("content_text", "")),
    ] if p)

    if not text.strip():
        return None

    # 尝试抽取 cause
    cause = "未明确"
    for connector in _CAUSE_CONNECTORS:
        idx = text.find(connector)
        if idx >= 0:
            # 取连接词后的 4-20 字作为 cause
            start = idx + len(connector)
            end = min(len(text), start + 20)
            candidate = text[start:end].strip()
            # 截断到第一个标点
            for punct in ["，", "。", "；", ",", ".", ";"]:
                punct_idx = candidate.find(punct)
                if punct_idx > 2:
                    candidate = candidate[:punct_idx]
                    break
            if len(candidate) >= 2:
                cause = candidate
                break

    # 尝试抽取 effect
    # 对于"带动/推动/提升/利好"等动词，effect 通常在动词前面（如"PCB 需求提升"中"PCB 需求"是 effect）
    effect = "未明确"
    for connector in _EFFECT_CONNECTORS:
        idx = text.find(connector)
        if idx >= 0:
            # 取连接词前面的文本作为 effect
            start = max(0, idx - 20)
            candidate = text[start:idx].strip()
            # 去掉末尾的修饰词（有望/预计/可能/将/正在）
            for mod in ["有望", "预计", "可能", "将", "正在"]:
                if candidate.endswith(mod):
                    candidate = candidate[:-len(mod)].strip()
            # 截断到最近的标点（从后往前找）
            for punct in ["，", "。", "；", ",", ".", ";"]:
                punct_idx = candidate.rfind(punct)
                if punct_idx >= 0:
                    candidate = candidate[punct_idx + len(punct):].strip()
            if len(candidate) >= 2:
                effect = candidate
                break

    # 如果都未明确，返回 None
    if cause == "未明确" and effect == "未明确":
        return None

    confidence = str(claim.get("confidence", "low"))
    if confidence not in ("low", "medium"):
        confidence = "low"

    chain_id = f"chain_{_short_hash(cause + effect + str(claim.get('claim_id', '')))}"
    return {
        "chain_id": chain_id,
        "claim_id": str(claim.get("claim_id", "")),
        "document_id": str(document.get("document_id", "")),
        "cause": cause,
        "mechanism": "未明确",
        "effect": effect,
        "beneficiary_node": "未明确",
        "evidence_quote": str(claim.get("evidence_quote", "")),
        "confidence": confidence,
        "status": "pending",
    }


def _map_evidence_type(source_type: str) -> str:
    """按 source_type 映射 evidence_type。

    Args:
        source_type: 原始来源类型

    Returns:
        evidence_type：official / report / news / weak_text / unknown
    """
    source_type = str(source_type or "").strip()
    for keyword, evidence_type in _SOURCE_TYPE_MAP.items():
        if keyword in source_type:
            return evidence_type
    return "unknown"


def _generate_single_failure_condition(
    claim: Dict[str, Any],
    chain: Dict[str, Any],
) -> Tuple[str, str]:
    """为单个 claim/chain 生成 failure_condition。

    Args:
        claim: claim 字典
        chain: 关联的 causal_chain 字典

    Returns:
        (failure_condition, risk_type)
    """
    claim_text = str(claim.get("claim_text", ""))
    cause = str(chain.get("cause", ""))
    beneficiary = str(chain.get("beneficiary_node", ""))

    # 规则 3：国产替代
    if any(kw in claim_text for kw in _SUBSTITUTION_KEYWORDS):
        return (
            "如果缺少客户导入、认证进展、进口替代订单或公告证据，则该替代逻辑不能升级。",
            "substitution_risk",
        )

    # 规则 1：产品平台
    if any(pp in cause or pp in claim_text for pp in _PRODUCT_PLATFORMS):
        return (
            "如果相关产品放量不及预期，或其对下游环节的价值量拉动未兑现，则该观点失效。",
            "demand_risk",
        )

    # 规则 2：材料类
    if any(mk in beneficiary or mk in claim_text for mk in _MATERIAL_KEYWORDS):
        return (
            "如果该材料仅为概念关联，缺少订单、客户认证、价格上涨或产能利用率提升证据，则该观点失效。",
            "evidence_risk",
        )

    # 默认
    return (
        "如果缺少公告、官网、客户认证或订单证据，则该观点不能升级。",
        "unknown",
    )


def _extract_quote_around_signal(text: str, signal: str, window: int = 60) -> str:
    """提取信号词周围的文本作为 evidence_quote。

    Args:
        text: 原文
        signal: 信号词
        window: 窗口大小

    Returns:
        信号词周围的文本片段
    """
    idx = text.find(signal)
    if idx < 0:
        return text[:100] if text else ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(signal) + window)
    return text[start:end].strip()


def _extract_terms_from_text(text: str) -> List[str]:
    """从文本中抽取术语（简单匹配大写缩写）。

    Args:
        text: 原文

    Returns:
        术语列表
    """
    # 匹配 2-10 位大写字母+数字组合
    terms = set(re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", text))
    # 匹配常见技术术语
    tech_terms = re.findall(
        r"[\u4e00-\u9fff]{2,8}(?:模块|芯片|引擎|组件|电源|连接器|控制器|封装|材料|设备|衬底|铜箔|树脂)",
        text,
    )
    terms.update(tech_terms)
    return sorted(terms)


def _short_hash(text: str) -> str:
    """计算短哈希。

    Args:
        text: 原文

    Returns:
        12 位哈希字符串
    """
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]
