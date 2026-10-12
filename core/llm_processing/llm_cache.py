"""LLM 抽取缓存 v2 — 按 raw_hash + chunk_text_hash + prompt_version 缓存。

prompt_version 或 model_name 变化 → cache miss；
chunk_text_hash 保证分块逻辑变更后缓存失效。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, Optional

from core.events.models import ExtractionResult

logger = logging.getLogger(__name__)


class LLMCache:
    """按 raw_hash + chunk_text_hash + prompt_version 缓存抽取结果。

    缓存文件存储在 cache_dir 下，每个 cache_key 对应一个 JSON 文件。
    prompt_version 或 model_name 变化时缓存自动失效。

    Args:
        cache_dir: 缓存目录路径
    """

    def __init__(self, cache_dir: str = "data/llm_cache/extractions") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        raw_hash: str,
        chunk_text_hash: str = "",
        prompt_version: str = "",
        model_name: str = "",
    ) -> Optional[ExtractionResult]:
        """获取缓存中的抽取结果。

        Args:
            raw_hash: 原始事件哈希
            chunk_text_hash: 文本块哈希（v2 新增）
            prompt_version: Prompt 版本号（v2 新增）
            model_name: 模型名称（v2 新增）

        Returns:
            ExtractionResult 或 None（未缓存或版本不匹配时）
        """
        cache_key = self._compute_cache_key(raw_hash, chunk_text_hash, prompt_version, model_name)
        cache_file = self._cache_path(cache_key)
        if not cache_file.exists():
            return None

        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))

            # v2: 校验 prompt_version 和 model_name
            if prompt_version and data.get("prompt_version") != prompt_version:
                return None
            if model_name and data.get("model_name") != model_name:
                return None

            return self._deserialize(data)
        except Exception as exc:
            logger.warning("读取缓存失败 (key=%s): %s", cache_key[:8], exc)
            return None

    def set(
        self,
        raw_hash: str,
        result: ExtractionResult,
        chunk_text_hash: str = "",
        prompt_version: str = "",
        model_name: str = "",
        raw_llm_output: str = "",
        grounding_result: Optional[Dict] = None,
    ) -> None:
        """缓存一个抽取结果。

        Args:
            raw_hash: 原始事件哈希
            result: 抽取结果
            chunk_text_hash: 文本块哈希
            prompt_version: Prompt 版本号
            model_name: 模型名称
            raw_llm_output: LLM 原始输出
            grounding_result: 锚定校验结果
        """
        cache_key = self._compute_cache_key(raw_hash, chunk_text_hash, prompt_version, model_name)
        cache_file = self._cache_path(cache_key)
        try:
            data = asdict(result)
            # v2 扩展字段
            data["_raw_hash"] = raw_hash
            data["_cache_key"] = cache_key
            data["input_hash"] = raw_hash
            data["chunk_text_hash"] = chunk_text_hash
            data["prompt_version"] = prompt_version
            data["model_name"] = model_name
            data["processor"] = result.processor
            if raw_llm_output:
                data["raw_llm_output"] = raw_llm_output
            if grounding_result:
                data["grounding_result"] = grounding_result
            cache_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("写入缓存失败 (key=%s): %s", cache_key[:8], exc)

    def has(
        self,
        raw_hash: str,
        chunk_text_hash: str = "",
        prompt_version: str = "",
        model_name: str = "",
    ) -> bool:
        """检查是否已缓存且版本匹配。

        Args:
            raw_hash: 原始事件哈希
            chunk_text_hash: 文本块哈希
            prompt_version: Prompt 版本号
            model_name: 模型名称

        Returns:
            是否已缓存
        """
        return self.get(raw_hash, chunk_text_hash, prompt_version, model_name) is not None

    @staticmethod
    def _compute_cache_key(
        raw_hash: str,
        chunk_text_hash: str,
        prompt_version: str,
        model_name: str,
    ) -> str:
        """计算缓存 key。

        Key = MD5(raw_hash + chunk_text_hash + prompt_version + model_name)，
        保证任一维度变化都会产生不同的 key。

        Args:
            raw_hash: 原始事件哈希
            chunk_text_hash: 文本块哈希
            prompt_version: Prompt 版本号
            model_name: 模型名称

        Returns:
            缓存 key 字符串
        """
        combined = f"{raw_hash}|{chunk_text_hash}|{prompt_version}|{model_name}"
        return hashlib.md5(combined.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_key: str) -> Path:
        """获取缓存文件路径。

        使用 key 前 2 位作为子目录，避免单目录文件过多。

        Args:
            cache_key: 缓存 key

        Returns:
            缓存文件路径
        """
        subdir = self.cache_dir / cache_key[:2]
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir / f"{cache_key}.json"

    @staticmethod
    def _deserialize(data: dict) -> ExtractionResult:
        """将字典反序列化为 ExtractionResult。

        只传入 ExtractionResult 已知字段，忽略多余字段。

        Args:
            data: 序列化的字典

        Returns:
            ExtractionResult 实例
        """
        known_fields = {f.name for f in fields(ExtractionResult)}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return ExtractionResult(**filtered)
