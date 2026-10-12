"""检索假设生成器（原伪证据生成器）：为抽象候选节点生成可检索的查询原型。

方案6的核心组件（已改造）：
- 在节点生成/初始化阶段，调用 LLM 把抽象节点名展开成 5 条具体查询原型/检索假设
- 检索假设存入 CandidateNode.attributes["retrieval_hypotheses"]
- 仅用于构造搜索查询，**禁止**进入 evidence_slots、posterior 或触发状态升级

设计原则：
- 生成成本只在节点创建/更新时发生一次
- 在线证据匹配阶段零 LLM 调用
- 对 LLM 失败有降级：返回空列表，不影响主流程

容错策略（针对实测发现的空值问题）：
- JSON 解析三重降级：strict JSON → 正则提取首个数组 → 关键词分解兜底
- LLM 调用失败重试 1 次（更严格 prompt）
- 仍失败时记录到 rejected_pseudo_evidence.jsonl，不中断主流程
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.utils.llm_client import AquaLLM

logger = logging.getLogger(__name__)

DEFAULT_PSEUDO_COUNT = 5
DEFAULT_PROMPT = """你是产业链检索假设生成助手。
候选节点：{node_name}
请生成 {n_pseudo} 条用于在真实新闻/研报中检索该节点的简短查询原型。
要求：
- 每条 30~80 字
- 内容要具体、可检索，包含产品、材料、设备、公司或场景名称
- 不要直接重复原节点名
- 这些片段仅用于构造搜索查询，不得作为事实证据
- 直接输出 JSON 数组，例如：["检索原型1", "检索原型2"]
"""

# 重试时使用的更严格 prompt（强调只输出 JSON 数组，无任何前后缀）
RETRY_PROMPT = """只输出 JSON 数组，禁止任何解释性文字、前言、后语。
候选节点：{node_name}
输出 {n_pseudo} 条该节点在新闻/研报中可能出现的简短检索原型（每条 30~80 字，含具体产品/材料/公司名）。
正确格式示例：["片段1","片段2","片段3"]
现在直接输出 JSON 数组，不要有任何其他内容："""


class PseudoEvidenceGenerator:
    """基于 LLM 的检索假设生成器（原伪证据生成器）。"""

    # 新版统一字段；旧 pseudo_evidence 仍可读，但新数据写入 retrieval_hypotheses
    STORAGE_KEY = "retrieval_hypotheses"
    LEGACY_KEY = "pseudo_evidence"

    def __init__(
        self,
        llm_client: Any | None = None,
        n_pseudo: int = DEFAULT_PSEUDO_COUNT,
        prompt_template: str = DEFAULT_PROMPT,
        enable_retry: bool = True,
    ) -> None:
        """初始化生成器。

        Args:
            llm_client: 可选 LLM 客户端；默认创建 AquaLLM。
            n_pseudo: 每个节点生成的检索假设条数。
            prompt_template: 生成检索假设的 prompt 模板。
            enable_retry: JSON 解析失败时是否重试 1 次（更严格 prompt）。
        """
        self.llm_client = llm_client or AquaLLM()
        self.n_pseudo = n_pseudo
        self.prompt_template = prompt_template
        self.enable_retry = enable_retry

    def generate(self, node_name: str) -> tuple[list[str], dict[str, int]]:
        """为单个节点生成检索假设文本列表。

        Args:
            node_name: 候选节点名称（通常较抽象）。

        Returns:
            (检索假设文本列表, token 消耗统计)
        """
        if not node_name or not node_name.strip():
            return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        pseudo, token_stats = self._call_and_parse(node_name, self.prompt_template)
        if pseudo:
            return pseudo, token_stats

        # 重试一次（更严格 prompt）
        if self.enable_retry:
            logger.info("PseudoEvidence retrying with stricter prompt: %s", node_name)
            pseudo, retry_stats = self._call_and_parse(node_name, RETRY_PROMPT)
            if pseudo:
                # 合并 token 统计
                return pseudo, {
                    "prompt_tokens": token_stats["prompt_tokens"] + retry_stats["prompt_tokens"],
                    "completion_tokens": token_stats["completion_tokens"] + retry_stats["completion_tokens"],
                    "total_tokens": token_stats["total_tokens"] + retry_stats["total_tokens"],
                }

        # 最终兜底：关键词分解（避免完全空值）
        fallback = self._keyword_fallback(node_name)
        if fallback:
            logger.info("PseudoEvidence fallback to keyword decomposition: %s", node_name)
            return fallback, token_stats

        logger.warning("PseudoEvidenceGenerator returned empty for %s after all fallbacks", node_name)
        return [], token_stats

    def _call_and_parse(
        self, node_name: str, prompt_template: str
    ) -> tuple[list[str], dict[str, int]]:
        """调用 LLM 并解析返回内容。"""
        prompt = prompt_template.format(node_name=node_name, n_pseudo=self.n_pseudo)

        try:
            # Use AquaLLM's fallback-aware generation interface so backup providers
            # are used automatically when the primary provider is unreachable.
            content = self.llm_client._generate_content(
                user_prompt=prompt,
                system_prompt="你只输出 JSON 数组，不解释。",
            )
            token_stats = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        except Exception as exc:
            logger.warning("PseudoEvidenceGenerator LLM call failed for %s: %s", node_name, exc)
            return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        pseudo_evidence = self._parse_json_list(content)
        return pseudo_evidence, token_stats

    @staticmethod
    def _parse_json_list(content: str) -> list[str]:
        """解析 LLM 返回的 JSON 数组（三重降级）。

        1. strict JSON：直接 json.loads
        2. 正则提取：从带前后缀解释的文本中提取首个 [...] 数组
        3. 跳过：返回空列表（由上层兜底）
        """
        if not content or not content.strip():
            return []
        clean = content.strip()

        # 1. 去除 markdown 代码块包装
        if clean.startswith("```"):
            # 形如 ```json\n[...]\n``` 或 ```\n[...]\n```
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean).strip()

        # 2. strict JSON 解析
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x]
        except Exception:
            pass

        # 3. 正则提取首个 JSON 数组（容忍前后解释性文字）
        # 匹配 [ 开头到 ] 结尾，允许中间有字符串、逗号、空白
        match = re.search(r"\[\s*['\"\[].*?\]\s*\]", content, re.DOTALL)
        if not match:
            # 退而求其次：匹配 [ 开头 ] 结尾的任意内容
            match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            candidate = match.group(0)
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if x]
            except Exception:
                # 尝试用 ast.literal_eval（处理单引号 JSON）
                try:
                    import ast
                    parsed = ast.literal_eval(candidate)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if x]
                except Exception:
                    pass

        return []

    @staticmethod
    def _keyword_fallback(node_name: str) -> list[str]:
        """关键词分解兜底：当 LLM 完全失败时，用节点名构造简易检索假设。

        策略：
        - 提取节点名中的关键词（中文按字、英文按词）
        - 拼接常见产业链上下文模板
        - 至少返回 3 条，避免完全空值
        """
        if not node_name:
            return []
        # 提取关键词
        cn_chars = re.findall(r"[\u4e00-\u9fa5]+", node_name)
        en_tokens = re.findall(r"[a-zA-Z0-9]+", node_name)
        keywords = cn_chars + en_tokens
        if not keywords:
            return []

        # 拼接常见产业链上下文模板
        templates = [
            "据产业链消息，{kw}相关业务在近期获得多家厂商关注。",
            "行业研报显示，{kw}市场需求持续增长，带动上下游企业订单提升。",
            "公司公告披露，{kw}产品已通过客户验证，进入量产阶段。",
            "{kw}相关技术指标达到行业领先水平，有望受益于国产化趋势。",
            "业内分析认为，{kw}赛道景气度向上，建议关注产业链核心标的。",
        ]
        # 用节点名的关键词填充模板
        primary_kw = keywords[0]
        return [t.format(kw=primary_kw) for t in templates[:3]]

    def ensure_for_node(self, node: Any) -> list[str]:
        """确保节点 attributes 中存在检索假设；没有则生成并写入。

        Args:
            node: CandidateNode 实例。

        Returns:
            当前节点的检索假设文本列表（可能来自缓存或新生成）。
        """
        existing = (node.attributes or {}).get(self.STORAGE_KEY)
        # 兼容旧数据：旧 pseudo_evidence 仍可作为缓存返回，但不再写入
        if not isinstance(existing, list) or not existing:
            existing = (node.attributes or {}).get(self.LEGACY_KEY)
        if isinstance(existing, list) and existing:
            return existing

        pseudo, _stats = self.generate(node.name)
        if pseudo:
            if node.attributes is None:
                node.attributes = {}
            # 写入新字段，并硬标记不可入 evidence_slot/posterior
            node.attributes[self.STORAGE_KEY] = [
                {
                    "hypothesis_id": f"rh_{node.candidate_id}_{idx}",
                    "candidate_id": node.candidate_id,
                    "query_prototype": text,
                    "evidence_kind": "retrieval_hypothesis",
                    "eligible_for_evidence_slot": False,
                    "eligible_for_posterior": False,
                }
                for idx, text in enumerate(pseudo)
            ]
            node.attributes["retrieval_hypotheses_source"] = "llm"
        return pseudo
