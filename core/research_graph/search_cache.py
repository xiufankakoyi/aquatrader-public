"""搜索去重缓存：避免同一节点在短期内被重复搜索。

设计思路：
- 缓存粒度为 (candidate_id, tier_name, date)
- 同一节点同一天内，同一 tier 只搜索一次
- 支持内存缓存和文件持久化（JSON 行格式）
- 缓存只记录"已搜索"元信息，不缓存原始文档（避免数据膨胀）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class SearchCacheAdapter:
    """把 SearchCache 包装成 ResearchOrchestrator 可注入的 get/set 适配器。

    Orchestrator 的缓存 key 是一个包含 candidate/claim/query/purpose/tier 等字段的
    payload dict。本适配器负责把它映射到 SearchCache 的 tier 语义：
    - candidate_id 取自 payload["candidate"] 或 payload["claim"]
    - tier_name 取自 payload["tier"]
    - query_key 由 query + purpose 哈希生成
    - scope 固定为 "orchestrator"
    - content_version 取自 payload["content_version"]
    """

    def __init__(self, search_cache: SearchCache) -> None:
        """初始化适配器。

        Args:
            search_cache: 被包装的 SearchCache 实例。
        """
        self.search_cache = search_cache

    @staticmethod
    def _extract_key_fields(payload: dict[str, Any]) -> tuple[str, str, str, str]:
        """从 Orchestrator payload 提取 SearchCache 参数。"""
        candidate_id = str(payload.get("candidate") or payload.get("claim") or "unknown")
        tier_name = str(payload.get("tier") or "default")
        content_version = str(payload.get("content_version") or "")
        query = str(payload.get("query") or "")
        purpose = str(payload.get("purpose") or "")
        query_key = hashlib.sha1(
            f"{query}|{purpose}".encode("utf-8")
        ).hexdigest()[:16]
        return candidate_id, tier_name, query_key, content_version

    def get(self, key: Any) -> Any | None:
        """检查当前 tier 是否已在有效期内搜索过。

        Args:
            key: Orchestrator 传入的 payload dict（或其 sha256 哈希字符串）。
                 若传入字符串则无法还原 tier 语义，返回 None。

        Returns:
            已搜索过返回 True（占位值），未搜索返回 None。
        """
        if not isinstance(key, dict):
            return None
        candidate_id, tier_name, query_key, content_version = self._extract_key_fields(key)
        if self.search_cache.is_tier_searched(
            candidate_id, tier_name, query_key,
            scope="orchestrator", content_version=content_version,
        ):
            return True
        return None

    def set(self, key: Any, value: Any) -> None:
        """标记当前 tier 已搜索。

        Args:
            key: Orchestrator 传入的 payload dict。
            value: 搜索结果，仅用于 metadata 记录条数。
        """
        if not isinstance(key, dict):
            return
        candidate_id, tier_name, query_key, content_version = self._extract_key_fields(key)
        metadata = None
        if isinstance(value, list):
            metadata = {"documents_found": len(value)}
        self.search_cache.mark_tier_searched(
            candidate_id, tier_name, query_key,
            metadata=metadata,
            scope="orchestrator", content_version=content_version,
        )


class SearchCache:
    """轻量级搜索去重缓存。"""

    def __init__(
        self,
        cache_dir: str | None = None,
        ttl_days: int = 1,
        ttl_hours: int | None = None,
        strategy_version: str = "default",
    ) -> None:
        """初始化搜索缓存。

        Args:
            cache_dir: 缓存目录；为 None 时使用系统临时目录。
            ttl_days: 缓存有效期（天）；当 ttl_hours 为 None 时生效。
            ttl_hours: 缓存有效期（小时）；优先级高于 ttl_days，适用于高频研究任务。
            strategy_version: 策略版本，纳入 key 以隔离不同策略/版本的搜索结果。
        """
        self.ttl_days = max(1, int(ttl_days))
        self.ttl_hours = max(1, int(ttl_hours)) if ttl_hours is not None else self.ttl_days * 24
        self.strategy_version = strategy_version
        self._cache_dir = Path(cache_dir or os.path.join(tempfile.gettempdir(), "aquatrade_search_cache"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "search_cache.jsonl"
        self._memory: dict[str, dict[str, Any]] = {}
        self._load()

    def is_tier_searched(
        self,
        candidate_id: str,
        tier_name: str,
        query_key: str = "",
        scope: str = "default",
        content_version: str = "",
    ) -> bool:
        """判断某个 tier 是否已在有效期内搜索过。

        Args:
            candidate_id: 候选节点 ID。
            tier_name: 质量阶梯名称（high/medium/low）。
            query_key: 查询指纹，用于区分不同查询。
            scope: 缓存作用域，用于隔离不同调用方（默认 "default"）。
            content_version: 内容版本/策略版本，纳入 key 以隔离不同版本结果。
        """
        key = self._make_key(candidate_id, tier_name, query_key, scope, content_version)
        entry = self._memory.get(key)
        if entry is None:
            return False
        cached_at = datetime.fromisoformat(entry["cached_at"])
        if datetime.now() - cached_at > timedelta(hours=self.ttl_hours):
            self._memory.pop(key, None)
            return False
        return True

    def mark_tier_searched(
        self,
        candidate_id: str,
        tier_name: str,
        query_key: str = "",
        metadata: dict[str, Any] | None = None,
        scope: str = "default",
        content_version: str = "",
    ) -> None:
        """标记某个 tier 已搜索。

        Args:
            candidate_id: 候选节点 ID。
            tier_name: 质量阶梯名称（high/medium/low）。
            query_key: 查询指纹，用于区分不同查询。
            metadata: 附加元信息（如 queries、documents_found）。
            scope: 缓存作用域，用于隔离不同调用方（默认 "default"）。
            content_version: 内容版本/策略版本，纳入 key 以隔离不同版本结果。
        """
        key = self._make_key(candidate_id, tier_name, query_key, scope, content_version)
        self._memory[key] = {
            "candidate_id": candidate_id,
            "tier_name": tier_name,
            "query_key": query_key,
            "scope": scope,
            "content_version": content_version,
            "strategy_version": self.strategy_version,
            "cached_at": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        """返回当前缓存快照（用于调试）。"""
        return {
            "cache_dir": str(self._cache_dir),
            "ttl_days": self.ttl_days,
            "ttl_hours": self.ttl_hours,
            "entries": len(self._memory),
        }

    def clear(self) -> None:
        """清空缓存。"""
        self._memory.clear()
        if self._cache_file.exists():
            self._cache_file.unlink()

    def _make_key(
        self,
        candidate_id: str,
        tier_name: str,
        query_key: str,
        scope: str = "default",
        content_version: str = "",
    ) -> str:
        """生成缓存 key（使用当前实例的 strategy_version）。"""
        return self._make_key_with_strategy(
            candidate_id, tier_name, query_key, scope, content_version, self.strategy_version
        )

    @staticmethod
    def _make_key_with_strategy(
        candidate_id: str,
        tier_name: str,
        query_key: str,
        scope: str,
        content_version: str,
        strategy_version: str,
    ) -> str:
        """生成缓存 key（显式指定 strategy_version，用于持久化加载）。"""
        today = datetime.now().strftime("%Y%m%d")
        raw = f"{scope}|{candidate_id}|{tier_name}|{query_key}|{content_version}|{strategy_version}|{today}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _load(self) -> None:
        """从磁盘加载缓存。"""
        if not self._cache_file.exists():
            return
        try:
            with self._cache_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # 加载时必须使用条目自身的 strategy_version 重建 key，
                        # 否则切换 strategy_version 会导致旧条目被错误复用。
                        saved_strategy = entry.get("strategy_version", self.strategy_version)
                        key = self._make_key_with_strategy(
                            entry.get("candidate_id", ""),
                            entry.get("tier_name", ""),
                            entry.get("query_key", ""),
                            entry.get("scope", "default"),
                            entry.get("content_version", ""),
                            saved_strategy,
                        )
                        cached_at = datetime.fromisoformat(entry["cached_at"])
                        if datetime.now() - cached_at <= timedelta(hours=self.ttl_hours):
                            self._memory[key] = entry
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass

    def _persist(self) -> None:
        """持久化缓存到磁盘。"""
        try:
            with self._cache_file.open("w", encoding="utf-8") as fh:
                for entry in self._memory.values():
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
