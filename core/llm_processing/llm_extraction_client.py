"""LLM 抽取客户端 — 封装 LLM API 调用、JSON 修复、合规扫描。

调用本地 LLM 服务进行结构化信息抽取，包含完整的错误处理和降级逻辑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.llm_processing.extraction_schema import LLMExtractionOutput
from core.llm_processing.grounding_validator import validate_evidence_items
from core.llm_processing.prompt_templates import (
    PROMPT_VERSION,
    build_extraction_prompt,
    check_compliance,
    check_compliance_for_summary,
    get_system_prompt,
)
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

logger = logging.getLogger(__name__)


@dataclass
class LLMCallStats:
    """LLM 调用统计。

    Attributes:
        llm_call_count: 总调用次数
        success: 成功次数
        failure: 失败次数
        json_repair: JSON 修复次数
        compliance_drop: 合规丢弃次数
        grounding_drop: 锚定丢弃次数
        fallback_rule_based: 降级到规则引擎次数
        cache_hit: 缓存命中次数
    """

    llm_call_count: int = 0
    success: int = 0
    failure: int = 0
    json_repair: int = 0
    compliance_drop: int = 0
    grounding_drop: int = 0
    fallback_rule_based: int = 0
    cache_hit: int = 0
    prompt_tokens_est: int = 0
    completion_tokens_est: int = 0
    total_tokens_est: int = 0


class LLMExtractionClient:
    """LLM 抽取客户端。

    封装 LLM API 调用、JSON 修复、合规扫描、锚定校验的完整流程。

    Args:
        api_base: OpenAI 兼容 API 地址
        model_name: 模型名称
        timeout: 超时秒数
    """

    def __init__(
        self,
        api_base: str = "",
        model_name: str = "",
        timeout: int = 60,
    ) -> None:
        self.api_base = api_base or self._load_api_base()
        self.model_name = model_name or self._load_model_name()
        self.api_key = self._load_api_key()
        self.timeout = timeout
        self.stats = LLMCallStats()
        self.providers = load_llm_providers(
            self.api_base,
            self.api_key,
            self.model_name,
        )
        self._available = bool(self.providers)

    @staticmethod
    def _load_api_base() -> str:
        """从 config/secrets.py 加载 API 地址。

        Returns:
            API 地址字符串
        """
        try:
            from config.secrets import Secrets
            return Secrets.LOCAL_LLM_API_BASE or Secrets.LLM_API_BASE or ""
        except ImportError:
            return os.environ.get("LOCAL_LLM_API_BASE", "")

    @staticmethod
    def _load_model_name() -> str:
        """从 config/secrets.py 加载模型名称。

        Returns:
            模型名称字符串
        """
        try:
            from config.secrets import Secrets
            return getattr(Secrets, 'LOCAL_LLM_MODEL_NAME', '') or Secrets.LLM_MODEL_NAME or "default"
        except ImportError:
            return os.environ.get("LOCAL_LLM_MODEL", "default")

    @staticmethod
    def _load_api_key() -> str:
        """从 config/secrets.py 加载 API Key。

        Returns:
            API Key 字符串
        """
        try:
            from config.secrets import Secrets
            return getattr(Secrets, 'LOCAL_LLM_API_KEY', '') or Secrets.LLM_API_KEY or ""
        except ImportError:
            return os.environ.get("LOCAL_LLM_API_KEY", "")

    @property
    def available(self) -> bool:
        """LLM 是否可用。"""
        return self._available

    def extract(
        self,
        chunk_text: str,
        source_text: str,
        metadata: Dict[str, str],
    ) -> Tuple[Optional[LLMExtractionOutput], List[Dict]]:
        """对单个文本块执行 LLM 抽取 + 合规扫描 + 锚定校验。

        Args:
            chunk_text: 待抽取的文本块
            source_text: 完整原文（用于锚定校验）
            metadata: 元数据

        Returns:
            (抽取结果, 锚定校验结果列表)，LLM 失败时返回 (None, [])
        """
        if not self._available:
            return None, []

        self.stats.llm_call_count += 1

        # 调用 LLM
        raw_output = self._call_api(chunk_text, metadata)
        if raw_output is None:
            self.stats.failure += 1
            self.stats.fallback_rule_based += 1
            return None, []

        # JSON 修复
        parsed = self._parse_json(raw_output)
        if parsed is None:
            self.stats.failure += 1
            self.stats.fallback_rule_based += 1
            return None, []

        # 合规扫描：仅检查建议性短语（如"建议买入"）
        # 注意: "增持""减持"等券商评级词出现在摘录中是正常的，不应拦截
        output_text = json.dumps(parsed, ensure_ascii=False)
        if not check_compliance(output_text):
            self.stats.compliance_drop += 1
            self.stats.fallback_rule_based += 1
            logger.warning("LLM 输出命中合规禁用词，整条丢弃")
            return None, []

        # 构建 LLMExtractionOutput
        extraction = LLMExtractionOutput.from_parsed_json(parsed)

        # 锚定校验
        validated_items, grounding_results = validate_evidence_items(
            extraction.evidence_items, source_text
        )
        drop_count = sum(1 for gr in grounding_results if gr.status == "drop")
        self.stats.grounding_drop += drop_count

        extraction.evidence_items = validated_items
        self.stats.success += 1

        return extraction, grounding_results

    def _call_api(self, chunk_text: str, metadata: Dict[str, str]) -> Optional[str]:
        """调用 LLM API。

        Args:
            chunk_text: 文本块
            metadata: 元数据

        Returns:
            LLM 原始输出文本，失败返回 None
        """
        try:
            import requests

            system_prompt = get_system_prompt()
            user_prompt = build_extraction_prompt(chunk_text, metadata)

            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            body: Dict[str, Any] = {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
            }

            # 尝试带 response_format
            try:
                body["response_format"] = {"type": "json_object"}
                resp = requests.post(
                    f"{self.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except Exception:
                # 不支持 response_format，重试不带
                body.pop("response_format", None)
                resp = requests.post(
                    f"{self.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )

            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        except ImportError:
            logger.warning("requests 库未安装，无法调用 LLM")
            return None
        except Exception as exc:
            logger.warning("LLM API 调用失败: %s", exc)
            return None

    def _call_api(self, chunk_text: str, metadata: Dict[str, str]) -> Optional[str]:
        """Call configured providers with fallback and rough token accounting."""
        try:
            import requests
        except ImportError:
            logger.warning("requests is not installed; cannot call LLM")
            return None

        system_prompt = get_system_prompt()
        user_prompt = build_extraction_prompt(chunk_text, metadata)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        prompt_tokens = estimate_messages_tokens(messages)
        try:
            check_llm_budget_or_raise(prompt_tokens, 0, source="LLMExtractionClient._call_api")
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return None

        for provider in self.providers:
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
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    body.pop("response_format", None)
                    resp = requests.post(
                        f"{provider.api_base}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self.timeout,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = str(data["choices"][0]["message"]["content"])
                completion_tokens = estimate_tokens(content)
                self.stats.prompt_tokens_est += prompt_tokens
                self.stats.completion_tokens_est += completion_tokens
                self.stats.total_tokens_est = self.stats.prompt_tokens_est + self.stats.completion_tokens_est
                record_llm_usage(
                    source="LLMExtractionClient._call_api",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=completion_tokens,
                    success=True,
                )
                return content
            except Exception as exc:
                self.stats.prompt_tokens_est += prompt_tokens
                self.stats.total_tokens_est = self.stats.prompt_tokens_est + self.stats.completion_tokens_est
                record_llm_usage(
                    source="LLMExtractionClient._call_api",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                logger.warning("LLM API provider failed (%s): %s", provider.name, exc)
        return None

    @staticmethod
    def _parse_json(content: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON，含修复逻辑。

        步骤: 剥离 Markdown code fence → 提取 JSON → 缺失字段补空。

        Args:
            content: LLM 返回的原始文本

        Returns:
            解析后的字典或 None
        """
        text = content.strip()

        # 剥离 Markdown code fence
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        # 尝试直接解析
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # 提取第一个 JSON 对象
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        logger.warning("无法解析 LLM 返回的 JSON")
        return None

    @staticmethod
    def compute_chunk_hash(chunk_text: str) -> str:
        """计算文本块的哈希值，用于缓存 key。

        Args:
            chunk_text: 文本块

        Returns:
            哈希值字符串
        """
        return hashlib.md5(chunk_text.encode("utf-8")).hexdigest()[:16]
