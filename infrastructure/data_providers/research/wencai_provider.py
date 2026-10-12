"""
问财 (iWencai) 数据提供者。

使用 iWencai OpenAPI 进行自然语言语义搜索选股。
需要 IWENCAI_API_KEY 配置（通过 config.setting 或环境变量）。

认证方式：Authorization: Bearer {IWENCAI_API_KEY}
API 端点：https://openapi.iwencai.com/v1/query2data

请求必须携带 X-Claw-* 系列请求头，否则会返回 "Skill 版本过低" 错误。
参考：hithink-macro-query skill SKILL.md
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import requests

from infrastructure.data_providers.base import BaseMarketDataProvider

logger = logging.getLogger(__name__)

# 问财 OpenAPI 要求的 Skill 标识
_SKILL_ID = "hithink-macro-query"
_SKILL_VERSION = "1.0.0"


class WencaiProvider(BaseMarketDataProvider):
    """问财数据提供者，提供自然语言语义搜索选股。

    调用 iWencai /v1/query2data 端点，携带 X-Claw-* 请求头。
    """

    name = "wencai"

    def __init__(self) -> None:
        self._api_key: str = ""
        self._base_url: str = "https://openapi.iwencai.com"
        try:
            from config.setting import Setting
            self._api_key = Setting.IWENCAI_API_KEY or ""
            self._base_url = Setting.IWENCAI_BASE_URL or self._base_url
        except Exception:
            pass

    def is_available(self) -> bool:
        """检查 IWENCAI_API_KEY 是否已配置。"""
        return bool(self._api_key)

    def health(self) -> dict[str, Any]:
        """返回健康状态。"""
        return {
            "name": self.name,
            "available": self.is_available(),
            "last_error": "",
            "capabilities": ["semantic_search"],
        }

    # ----------------------------------------------------------
    # 语义搜索
    # ----------------------------------------------------------

    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        """通过 iWencai OpenAPI 进行自然语言语义搜索选股。

        Args:
            query: 自然语言查询，如 "人形机器人 丝杠"、"连续涨停"
            count: 返回条数

        Returns:
            {source, query, results, columns, code_count}
        """
        if not self.is_available():
            return {
                "source": self.name,
                "query": query,
                "results": [],
                "error": "未配置 IWENCAI_API_KEY",
            }

        try:
            url = f"{self._base_url}/v1/query2data"
            trace_id = secrets.token_hex(32)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "X-Claw-Call-Type": "normal",
                "X-Claw-Skill-Id": _SKILL_ID,
                "X-Claw-Skill-Version": _SKILL_VERSION,
                "X-Claw-Plugin-Id": "none",
                "X-Claw-Plugin-Version": "none",
                "X-Claw-Trace-Id": trace_id,
            }
            payload = {
                "query": query,
                "page": "1",
                "limit": str(count),
                "is_cache": "1",
                "expand_index": "true",
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 401:
                return {
                    "source": self.name,
                    "query": query,
                    "results": [],
                    "error": f"认证失败: {resp.text[:200]}",
                }
            if resp.status_code != 200:
                return {
                    "source": self.name,
                    "query": query,
                    "results": [],
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }

            data = resp.json()
            datas = data.get("datas", [])
            code_count = data.get("code_count", 0)
            chunks_info = data.get("chunks_info", {})

            columns = list(datas[0].keys())[:15] if datas else []

            return {
                "source": self.name,
                "query": query,
                "results": datas[:count],
                "columns": columns,
                "code_count": code_count,
                "chunks_info": chunks_info,
            }
        except Exception as exc:
            logger.warning("问财 semantic_search 失败: %s", exc)
            return {"source": self.name, "query": query, "results": [], "error": str(exc)}
