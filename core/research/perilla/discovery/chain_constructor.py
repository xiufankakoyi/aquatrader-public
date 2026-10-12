"""Perilla Discovery Engine - 动态供应链构建.

基于 LLM 或回退种子模式，围绕需求锚点构建动态供应链图谱。
LLM 模式利用大语言模型从证据中推理供应链结构，
回退模式生成最简三层链路作为兜底。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

from .models import (
    ANCHOR_TYPES,
    CHAIN_CONSTRUCTION_METHODS,
    CONFIDENCE_LEVELS,
    SUPPLY_CHAIN_LAYERS,
    ConceptSignal,
    DemandAnchor,
    DynamicSupplyChain,
    EarlySignal,
    SourceDocument,
    SupplyChainEdge,
    SupplyChainNode,
    new_id,
    now_iso,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default

# LLM 输出 JSON 中节点/边的期望字段
_REQUIRED_NODE_KEYS = {"name", "layer"}
_REQUIRED_EDGE_KEYS = {"source", "target", "relation"}


class DynamicChainConstructor:
    """动态供应链构建器 —— LLM 优先 + 种子回退.

    优先使用 LLM 从证据中推理供应链结构；若 LLM 不可用或返回
    无效结果，则回退到最简三层种子链路。

    Usage::

        constructor = DynamicChainConstructor()
        chain = constructor.construct_chain(concept, anchor, docs, signals)
    """

    def __init__(
        self,
        llm_api_base: str = "",
        llm_model_name: str = "",
    ) -> None:
        """初始化供应链构建器.

        从 config/secrets.py 防御性读取 LLM 配置，
        若配置缺失则仅支持回退种子模式。

        Args:
            llm_api_base: LLM API 地址，为空时从 Secrets 读取。
            llm_model_name: LLM 模型名称，为空时从 Secrets 读取。
        """
        self._llm_api_base = llm_api_base
        self._llm_model_name = llm_model_name
        self._llm_api_key: str = ""
        self._llm_available: bool = False
        self._llm_providers = []

        # 防御性读取 Secrets 配置
        try:
            from config.secrets import Secrets

            if not self._llm_api_base:
                self._llm_api_base = getattr(Secrets, "LLM_API_BASE", "")
            if not self._llm_model_name:
                self._llm_model_name = getattr(Secrets, "LLM_MODEL_NAME", "")
            self._llm_api_key = getattr(Secrets, "LLM_API_KEY", "")
        except ImportError:
            logger.warning("config/secrets.py 未找到，LLM 配置不可用")
        except Exception as exc:
            logger.warning("读取 LLM 配置失败: %s", exc)

        # 检查 LLM 是否可用
        if self._llm_api_base and self._llm_model_name:
            self._llm_available = True
            logger.info(
                "LLM 供应链构建已启用: base=%s, model=%s",
                self._llm_api_base,
                self._llm_model_name,
            )
        else:
            self._llm_available = False
            logger.info("LLM 不可用，将使用 fallback_seed 模式构建供应链")

        self._llm_providers = load_llm_providers(
            self._llm_api_base,
            self._llm_api_key,
            self._llm_model_name,
        )
        self._llm_available = bool(self._llm_providers)

    def construct_chain(
        self,
        concept: ConceptSignal,
        anchor: DemandAnchor,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> DynamicSupplyChain:
        """构建动态供应链.

        优先尝试 LLM 构建，失败则回退到种子模式。

        Args:
            concept: 概念信号。
            anchor: 需求锚点。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            构建完成的 DynamicSupplyChain。
        """
        # 尝试 LLM 构建
        if bool(getattr(self, "_llm_available", False)):
            chain = self._construct_with_llm(concept, anchor, source_docs, early_signals)
            if chain is not None:
                return chain
            logger.warning("LLM 构建失败，回退到 fallback_seed 模式")

        # 回退种子构建
        return self._construct_fallback_seed(concept, anchor)

    def _construct_with_llm(
        self,
        concept: ConceptSignal,
        anchor: DemandAnchor,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> Optional[DynamicSupplyChain]:
        prompt = self._build_chain_prompt(concept, anchor, source_docs, early_signals)
        if not prompt:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个供应链分析专家。根据提供的概念和证据构建供应链图谱。"
                    "严格输出 JSON，不要输出其他内容。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        raw_text = self._call_llm(messages, max_tokens=4096)
        if raw_text is None:
            return None
        return self._parse_chain_response(
            raw_text,
            _text(getattr(concept, "concept_id", "")),
            _text(getattr(anchor, "anchor_id", "")),
        )

    def _call_llm(self, messages: list[dict], max_tokens: int) -> Optional[str]:
        try:
            import openai
        except ImportError:
            logger.warning("openai package is not installed; cannot call LLM")
            return None

        prompt_tokens = estimate_messages_tokens(messages)
        try:
            check_llm_budget_or_raise(prompt_tokens, max_tokens, source="DynamicChainConstructor._call_llm")
        except RuntimeError as exc:
            logger.warning("%s", exc)
            return None

        for provider in self._llm_providers:
            try:
                client = openai.OpenAI(
                    base_url=provider.api_base,
                    api_key=provider.api_key or "dummy",
                )
                response = client.chat.completions.create(
                    model=provider.model_name,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    stream=False,
                )
                content = response.choices[0].message.content or ""
                record_llm_usage(
                    source="DynamicChainConstructor._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=estimate_tokens(content),
                    success=True,
                )
                return content
            except Exception as exc:
                record_llm_usage(
                    source="DynamicChainConstructor._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                logger.warning("LLM provider failed (%s): %s", provider.name, exc)
        return None

    def _construct_fallback_seed(
        self,
        concept: ConceptSignal,
        anchor: DemandAnchor,
    ) -> DynamicSupplyChain:
        """回退种子构建 —— 最简三层供应链.

        生成 terminal → module → component 三层链路，
        所有节点标记为假设，置信度为 low。

        Args:
            concept: 概念信号。
            anchor: 需求锚点。

        Returns:
            最简 DynamicSupplyChain。
        """
        chain_id = new_id("chain_")
        concept_name = _text(getattr(concept, "name", "")) or "未命名概念"

        # 三层节点
        terminal_node = SupplyChainNode(
            node_id=new_id("node_"),
            chain_id=chain_id,
            name=f"{concept_name}终端产品",
            layer="terminal",
            description=f"{concept_name}的终端产品层",
            is_anchor=True,
            is_potential_bottleneck=False,
            confidence="low",
            hypothesis_reason="fallback_seed 模式自动生成，未经 LLM 验证",
        )
        module_node = SupplyChainNode(
            node_id=new_id("node_"),
            chain_id=chain_id,
            name=f"{concept_name}核心模块",
            layer="module",
            description=f"{concept_name}的核心模块层",
            is_anchor=False,
            is_potential_bottleneck=True,
            confidence="low",
            hypothesis_reason="fallback_seed 模式自动生成，标记为潜在瓶颈",
        )
        component_node = SupplyChainNode(
            node_id=new_id("node_"),
            chain_id=chain_id,
            name=f"{concept_name}关键组件",
            layer="component",
            is_anchor=False,
            is_potential_bottleneck=False,
            confidence="low",
            hypothesis_reason="fallback_seed 模式自动生成，未经 LLM 验证",
        )
        material_node = SupplyChainNode(
            node_id=new_id("node_"),
            chain_id=chain_id,
            name=f"{concept_name}核心材料",
            layer="material",
            description=f"{concept_name}的核心材料层",
            is_anchor=False,
            is_potential_bottleneck=True,
            confidence="low",
            hypothesis_reason="fallback_seed 模式自动生成，标记为潜在瓶颈",
        )

        nodes = [terminal_node, module_node, component_node, material_node]

        # 边: terminal ← module ← component ← material
        edges = [
            SupplyChainEdge(
                source_node_id=module_node.node_id,
                target_node_id=terminal_node.node_id,
                relation_type="supplies",
                confidence="low",
            ),
            SupplyChainEdge(
                source_node_id=component_node.node_id,
                target_node_id=module_node.node_id,
                relation_type="supplies",
                confidence="low",
            ),
            SupplyChainEdge(
                source_node_id=material_node.node_id,
                target_node_id=component_node.node_id,
                relation_type="supplies",
                confidence="low",
            ),
        ]

        return DynamicSupplyChain(
            chain_id=chain_id,
            concept_id=_text(getattr(concept, "concept_id", "")),
            demand_anchor_id=_text(getattr(anchor, "anchor_id", "")),
            nodes=nodes,
            edges=edges,
            construction_method="fallback_seed",
            confidence="low",
        )

    def _build_chain_prompt(
        self,
        concept: ConceptSignal,
        anchor: DemandAnchor,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
    ) -> str:
        """构建 LLM 供应链推理提示词.

        包含概念信息、锚点信息、文档摘要、早期信号声明，
        以及反编造指令和合规检查。

        Args:
            concept: 概念信号。
            anchor: 需求锚点。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。

        Returns:
            完整的提示词字符串。
        """
        # 文档摘要：每篇取前 500 字符，最多 5 篇
        doc_excerpts: List[str] = []
        for doc in _items(source_docs)[:5]:
            excerpt = _text(getattr(doc, "content_text", ""))[:500]
            if excerpt:
                doc_excerpts.append(
                    f"【文档: {_text(getattr(doc, 'title', '')) or _text(getattr(doc, 'document_id', ''))}】\n{excerpt}"
                )

        # 早期信号声明
        signal_claims: List[str] = []
        for sig in _items(early_signals)[:10]:
            claim = _text(getattr(sig, "claim", ""))
            if claim:
                signal_claims.append(f"- {claim}")

        parts = [
            "## 任务",
            "根据以下概念和证据，构建该概念的供应链图谱。",
            "",
            "## 概念信息",
            f"- 名称: {_text(getattr(concept, 'name', ''))}",
            f"- 别名: {', '.join(item for item in _items(getattr(concept, 'aliases', None)) if isinstance(item, str)) or '无'}",
            f"- 热度: {_finite(getattr(concept, 'heat_score', 0.0)):.2f}",
            f"- 新颖度: {_finite(getattr(concept, 'novelty_score', 0.0)):.2f}",
            "",
            "## 需求锚点",
            f"- 名称: {_text(getattr(anchor, 'name', ''))}",
            f"- 类型: {_text(getattr(anchor, 'anchor_type', 'unknown'))}",
            f"- 描述: {_text(getattr(anchor, 'description', '')) or '无'}",
        ]

        if doc_excerpts:
            parts.append("")
            parts.append("## 来源文档摘要")
            parts.extend(doc_excerpts)

        if signal_claims:
            parts.append("")
            parts.append("## 早期信号声明")
            parts.extend(signal_claims)

        parts.extend([
            "",
            "## 输出要求",
            "严格输出以下 JSON 格式，不要输出任何其他内容：",
            "```json",
            "{",
            '  "nodes": [',
            "    {",
            '      "name": "节点名称",',
            '      "layer": "terminal|module|component|material|equipment|testing|consumable|service|software|unknown",',
            '      "description": "节点描述",',
            '      "is_anchor": true/false,',
            '      "is_potential_bottleneck": true/false,',
            '      "is_hypothesis": true/false',
            "    }",
            "  ],",
            '  "edges": [',
            "    {",
            '      "source": "源节点名称",',
            '      "target": "目标节点名称",',
            '      "relation": "upstream_of|downstream_of|depends_on|supplies|tests|enables|substitutes|unknown"',
            "    }",
            "  ]",
            "}",
            "```",
            "",
            "## 约束",
            "1. 只基于提供的证据推理，不要编造不存在的供应链环节",
            "2. 不确定的关系标记 is_hypothesis=true",
            "3. 禁止输出任何买入、卖出、仓位、止盈、止损等投资建议",
            "4. 如果证据不足以构建完整链路，只输出有依据的节点",
            "",
            "## 重要：深层展开要求",
            "必须尽量展开到 material/equipment/testing/consumable 层级。",
            "terminal/module 层通常是终端产品，不是供应链瓶颈。",
            "紫苏叶（瓶颈）环节通常出现在 material/equipment/testing/consumable 层。",
            "即使证据不足，也请基于行业常识推理出可能的深层节点，并标记 is_hypothesis=true。",
        ])

        return "\n".join(parts)

    def _parse_chain_response(
        self,
        response_text: str,
        concept_id: str,
        anchor_id: str,
    ) -> Optional[DynamicSupplyChain]:
        """解析 LLM 返回的 JSON 供应链结构.

        从响应文本中提取 JSON、验证字段、分配 ID，
        构建 DynamicSupplyChain 对象。

        Args:
            response_text: LLM 原始响应文本。
            concept_id: 关联概念 ID。
            anchor_id: 关联锚点 ID。

        Returns:
            DynamicSupplyChain 或 None（解析失败时）。
        """
        response_text = _text(response_text)
        if not response_text:
            return None

        # 提取 JSON：尝试从 markdown 代码块中提取，或直接解析
        json_str = self._extract_json(response_text)
        if not json_str:
            logger.warning("无法从 LLM 响应中提取 JSON")
            return None

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.warning("JSON 解析失败: %s", exc)
            return None

        if not isinstance(data, dict):
            logger.warning("LLM 返回的 JSON 不是对象")
            return None

        raw_nodes = data.get("nodes", [])
        raw_edges = data.get("edges", [])

        if not isinstance(raw_nodes, list) or not raw_nodes:
            logger.warning("LLM 返回的 nodes 为空或格式错误")
            return None

        if not isinstance(raw_edges, list):
            raw_edges = []

        # 验证并构建节点
        chain_id = new_id("chain_")
        name_to_id: Dict[str, str] = {}
        nodes: List[SupplyChainNode] = []
        has_hypothesis = False

        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping):
                continue
            if not _REQUIRED_NODE_KEYS.issubset(raw_node.keys()):
                logger.warning("节点缺少必要字段: %s", raw_node)
                continue

            layer = raw_node["layer"]
            node_name = _text(raw_node.get("name"))
            if not node_name:
                continue
            if not isinstance(layer, str):
                layer = "unknown"
            if layer not in SUPPLY_CHAIN_LAYERS:
                layer = "unknown"

            is_hypothesis = bool(raw_node.get("is_hypothesis", False))
            if is_hypothesis:
                has_hypothesis = True

            node_id = new_id("node_")
            name_to_id[node_name] = node_id

            nodes.append(SupplyChainNode(
                node_id=node_id,
                chain_id=chain_id,
                name=node_name,
                layer=layer,
                description=_text(raw_node.get("description")),
                is_anchor=bool(raw_node.get("is_anchor", False)),
                is_potential_bottleneck=bool(raw_node.get("is_potential_bottleneck", False)),
                confidence="low",
                hypothesis_reason="LLM 推理生成" if is_hypothesis else "",
            ))

        if not nodes:
            logger.warning("没有有效的供应链节点")
            return None

        # 验证并构建边
        edges: List[SupplyChainEdge] = []
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, Mapping):
                continue
            if not _REQUIRED_EDGE_KEYS.issubset(raw_edge.keys()):
                continue

            source_name = _text(raw_edge.get("source"))
            target_name = _text(raw_edge.get("target"))
            if not source_name or not target_name:
                continue
            source_id = name_to_id.get(source_name)
            target_id = name_to_id.get(target_name)

            if not source_id or not target_id:
                logger.warning(
                    "边的节点名称未找到对应 ID: %s -> %s",
                    source_name,
                    target_name,
                )
                continue

            relation = raw_edge.get("relation")
            if not isinstance(relation, str):
                relation = "unknown"
            if relation not in {
                "upstream_of", "downstream_of", "depends_on", "supplies",
                "tests", "enables", "substitutes", "unknown",
            }:
                relation = "unknown"

            edges.append(SupplyChainEdge(
                source_node_id=source_id,
                target_node_id=target_id,
                relation_type=relation,
                confidence="low",
            ))

        # 整体置信度：有假设节点时为 low，否则 medium
        overall_confidence = "low" if has_hypothesis else "medium"

        return DynamicSupplyChain(
            chain_id=chain_id,
            concept_id=_text(concept_id),
            demand_anchor_id=_text(anchor_id),
            nodes=nodes,
            edges=edges,
            construction_method="llm_dynamic",
            confidence=overall_confidence,
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从 LLM 响应文本中提取 JSON 字符串.

        尝试从 markdown 代码块中提取，若失败则尝试直接解析。

        Args:
            text: LLM 原始响应文本。

        Returns:
            JSON 字符串或 None。
        """
        text = _text(text)
        if not text:
            return None

        # 尝试从 ```json ... ``` 代码块中提取
        pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 尝试直接查找 JSON 对象（从第一个 { 到最后一个 }）
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            # 快速验证是否为有效 JSON
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

        return None
