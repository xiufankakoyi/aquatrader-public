"""Perilla Discovery Engine - 公司映射模块.

将供应链节点（尤其是瓶颈节点）与具体上市公司关联，
解析证券代码，判定证据等级，生成 CompanyCandidate。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    EVIDENCE_LEVELS,
    CompanyCandidate,
    ConceptSignal,
    DynamicSupplyChain,
    EarlySignal,
    SourceDocument,
    new_id,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _nodes(chain: Any) -> list[Any]:
    return [
        node
        for node in _items(getattr(chain, "nodes", None))
        if _text(getattr(node, "node_id", ""))
    ]

# A 股代码前缀 → 市场
_A_SHARE_PREFIXES: Dict[str, str] = {
    "600": "SH", "601": "SH", "603": "SH", "605": "SH", "688": "SH",
    "000": "SZ", "002": "SZ", "003": "SZ", "300": "SZ", "301": "SZ",
}

# 高可信度来源类型（官方渠道）
_HIGH_CREDIBILITY_SOURCES: frozenset[str] = frozenset({
    "announcement", "official_site",
})

# 中等可信度来源类型（多家报道可升级）
_MEDIUM_CREDIBILITY_SOURCES: frozenset[str] = frozenset({
    "news", "report", "local_evidence",
})

# 低可信度来源类型
_LOW_CREDIBILITY_SOURCES: frozenset[str] = frozenset({
    "forum", "social", "rumor",
})

# 概念/板块来源类型
_CONCEPT_SOURCES: frozenset[str] = frozenset({
    "concept_page", "index_page", "sector_page",
})

# 早期线索来源类型
_EARLY_SOURCES: frozenset[str] = frozenset({
    "recruitment", "expo", "tender",
})

# 公司名称提取正则：匹配中文公司名（2-20字，以"公司"/"集团"/"科技"等结尾）
_COMPANY_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"[\u4e00-\u9fff]{2,18}"
    r"(?:公司|集团|科技|技术|电子|半导体|材料|装备|股份|有限)"
)

# 证券代码提取正则
_STOCK_CODE_PATTERN: re.Pattern[str] = re.compile(
    r"(?<!\d)(\d{6})(?!\d)"
)

# 上下文窗口：提取公司名时，取节点名称前后多少字符
_CONTEXT_WINDOW: int = 80


class CompanyMapper:
    """公司映射器 - 将供应链节点映射到具体上市公司.

    从来源文档和早期信号中提取公司提及，解析证券代码，
    判定证据等级，生成 CompanyCandidate 列表。
    """

    def map_companies(
        self,
        chain: DynamicSupplyChain,
        bottleneck_hypotheses: List["BottleneckHypothesis"],
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
        concept_signal: ConceptSignal,
    ) -> List[CompanyCandidate]:
        """将供应链节点映射到公司，生成候选列表.

        优先处理瓶颈节点，对所有节点提取公司提及并映射。

        Args:
            chain: 动态供应链实例。
            bottleneck_hypotheses: 瓶颈假设列表。
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。
            concept_signal: 概念信号实例。

        Returns:
            公司候选列表，去重后的 CompanyCandidate。
        """
        nodes = _nodes(chain)
        if not chain or not nodes:
            logger.warning("供应链为空或无节点，跳过公司映射")
            return []

        bottleneck_hypotheses = _items(bottleneck_hypotheses)
        source_docs = _items(source_docs)
        early_signals = _items(early_signals)

        # 构建节点 → 瓶颈假设 ID 的映射
        node_to_bh: Dict[str, str] = {}
        for bh in bottleneck_hypotheses:
            node_id = _text(getattr(bh, "node_id", ""))
            if node_id:
                node_to_bh[node_id] = _text(getattr(bh, "hypothesis_id", ""))

        # 去重：同一公司 + 同一节点只保留一个候选
        seen_keys: set[str] = set()
        candidates: List[CompanyCandidate] = []

        # 优先处理瓶颈节点
        bottleneck_nodes = [
            n for n in nodes if _text(getattr(n, "node_id", "")) in node_to_bh
        ]
        other_nodes = [
            n for n in nodes if _text(getattr(n, "node_id", "")) not in node_to_bh
        ]

        for node in bottleneck_nodes + other_nodes:
            try:
                mentions = self._extract_company_mentions(
                    source_docs, early_signals, _text(getattr(node, "name", ""))
                )
            except Exception:
                logger.exception(
                    "提取节点 %s(%s) 的公司提及失败",
                    _text(getattr(node, "node_id", "")),
                    _text(getattr(node, "name", "")),
                )
                continue

            for mention in mentions:
                company_name = _text(mention.get("company_name"))
                node_id = _text(getattr(node, "node_id", ""))
                if not company_name or not node_id:
                    continue
                dedup_key = f"{company_name}|{node_id}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                symbol, market = self._resolve_company_code(company_name)

                # 收集该映射的来源信息
                mapping_sources = [_text(mention.get("context"))]
                source_types = [_text(mention.get("source_type"))]
                mention_evidence_ids = mention.get("evidence_ids", [])

                # 检查是否有供应链直接证据
                has_supply_evidence = self._check_supply_evidence(
                    company_name, _text(getattr(node, "name", "")), source_docs
                )

                evidence_level, needs_review = self._determine_evidence_level(
                    mapping_sources, source_types, has_supply_evidence
                )

                market_warning = self._tag_market_scope(market)

                candidate = CompanyCandidate(
                    candidate_id=new_id("cc_"),
                    company_name=company_name,
                    symbol=symbol,
                    market=market,
                    market_warning=market_warning,
                    concept_id=_text(getattr(concept_signal, "concept_id", "")),
                    chain_node_id=node_id,
                    bottleneck_hypothesis_id=node_to_bh.get(node_id, ""),
                    mapping_sources=[s for s in mapping_sources if s],
                    evidence_level=evidence_level,
                    evidence_ids=[
                        item for item in _items(mention_evidence_ids) if isinstance(item, str)
                    ],
                    claim_summary=_text(mention.get("context"))[:200],
                    needs_human_review=needs_review,
                )
                candidates.append(candidate)

        # 补充早期信号中 anchor_companies 的映射
        candidates = self._add_anchor_companies(
            early_signals, chain, node_to_bh, concept_signal,
            source_docs, candidates, seen_keys,
        )

        # 补充：直接从 EarlySignal 的 mentioned_companies 提取公司
        # 手动信号文件中的公司可能不与任何特定节点名匹配，
        # 但它们属于该概念的供应链，应映射到最相关的节点
        candidates = self._add_signal_companies(
            early_signals, chain, node_to_bh, concept_signal,
            source_docs, candidates, seen_keys,
        )

        logger.info(
            "供应链 %s 映射到 %d 家公司候选",
            _text(getattr(chain, "chain_id", "")), len(candidates),
        )
        return candidates

    def _extract_company_mentions(
        self,
        source_docs: List[SourceDocument],
        early_signals: List[EarlySignal],
        node_name: str,
    ) -> List[dict]:
        """从来源文档和早期信号中提取与节点相关的公司提及.

        在文档文本和信号声明中搜索节点名称附近的公司名，
        返回公司提及列表。

        Args:
            source_docs: 来源文档列表。
            early_signals: 早期信号列表。
            node_name: 节点名称。

        Returns:
            公司提及列表，每项包含 company_name, context, source_type, evidence_ids。
        """
        mentions: List[dict] = []
        seen_companies: set[str] = set()

        # 从来源文档中提取
        node_name = _text(node_name)
        if not node_name:
            return []

        for doc in _items(source_docs):
            text = _text(getattr(doc, "content_text", ""))
            if not text or node_name not in text:
                continue

            # 在节点名称附近搜索公司名
            for match in _COMPANY_NAME_PATTERN.finditer(text):
                company_name = match.group()
                if company_name == node_name:
                    continue
                if company_name in seen_companies:
                    continue

                # 检查公司名与节点名是否在同一上下文窗口
                company_pos = match.start()
                node_positions = [
                    m.start() for m in re.finditer(re.escape(node_name), text)
                ]
                in_context = any(
                    abs(company_pos - np) < _CONTEXT_WINDOW
                    for np in node_positions
                )
                if not in_context:
                    continue

                seen_companies.add(company_name)

                # 提取上下文片段
                start = max(0, company_pos - 30)
                end = min(len(text), company_pos + len(company_name) + 30)
                context = text[start:end].replace("\n", " ").strip()

                mentions.append({
                    "company_name": company_name,
                    "context": context,
                    "source_type": _text(getattr(doc, "source_type", "")),
                    "evidence_ids": [
                        _text(getattr(doc, "document_id", ""))
                    ] if _text(getattr(doc, "document_id", "")) else [],
                })

        # 从早期信号中提取（宽松匹配：不要求节点名在文本中）
        for es in _items(early_signals):
            claim_text = _text(getattr(es, "claim", ""))
            evidence_text = _text(getattr(es, "evidence_text", ""))

            # 宽松匹配：信号 claim/evidence 包含节点名，或者信号没有特定节点关联
            node_mentioned = (
                node_name in claim_text
                or node_name in evidence_text
                or not claim_text  # 无特定 claim 的信号视为通用信号
            )

            if not node_mentioned:
                continue

            # 从 mentioned_companies 和 anchor_companies 获取公司名
            companies = list(_items(getattr(es, "mentioned_companies", None)))
            companies += list(_items(getattr(es, "anchor_companies", None)))
            for company_name in companies:
                company_name = _text(company_name).strip()
                if not company_name:
                    continue
                if company_name in seen_companies or company_name == node_name:
                    continue
                seen_companies.add(company_name)

                context = claim_text[:200] if claim_text else evidence_text[:200]
                mentions.append({
                    "company_name": company_name,
                    "context": context,
                    "source_type": _text(getattr(es, "signal_type", "")),
                    "evidence_ids": [
                        _text(getattr(es, "source_document_id", ""))
                    ] if _text(getattr(es, "source_document_id", "")) else [],
                })

        return mentions

    def _resolve_company_code(self, company_name: str) -> Tuple[Optional[str], str]:
        """解析公司名称中的证券代码，推断所属市场.

        识别规则：
        - A 股：6 位纯数字，根据前缀判断 SH/SZ/BJ
        - 港股：5 位数字 → HK
        - 美股：1-5 位大写字母 → US
        - 无法识别 → UNKNOWN

        Args:
            company_name: 公司名称（可能包含证券代码）。

        Returns:
            (symbol, market) 元组：证券代码和所属市场。
        """
        company_name = _text(company_name)
        if not company_name:
            return None, "UNKNOWN"

        # 尝试提取 6 位数字代码（A 股）
        code_match = _STOCK_CODE_PATTERN.search(company_name)
        if code_match:
            code = code_match.group(1)

            # A 股 SH/SZ 判断
            for prefix, market in _A_SHARE_PREFIXES.items():
                if code.startswith(prefix):
                    return f"{code}.{market}", "A_SHARE"

            # 北交所：8 或 4 开头
            if code.startswith("8") or code.startswith("4"):
                return f"{code}.BJ", "A_SHARE"

            # 6 位数字但不匹配已知前缀，默认 A 股
            return code, "A_SHARE"

        # 港股：5 位数字
        hk_pattern = re.compile(r"(?<!\d)(\d{5})(?!\d)")
        hk_match = hk_pattern.search(company_name)
        if hk_match:
            return f"{hk_match.group(1)}.HK", "HK"

        # 美股：1-5 位大写字母
        us_pattern = re.compile(r"(?<![A-Z])([A-Z]{1,5})(?![A-Z])")
        us_match = us_pattern.search(company_name)
        if us_match:
            return us_match.group(1), "US"

        # 无法识别
        return None, "UNKNOWN"

    def _determine_evidence_level(
        self,
        mapping_sources: List[str],
        source_types: List[str],
        has_supply_evidence: bool,
    ) -> Tuple[str, bool]:
        """判定公司映射的证据等级.

        根据来源类型和供应链直接证据，判定证据等级和是否需要人工审核。

        Args:
            mapping_sources: 映射来源上下文列表。
            source_types: 来源类型列表。
            has_supply_evidence: 是否有供应链直接证据。

        Returns:
            (evidence_level, needs_human_review) 元组。
        """
        source_type_set = {
            _text(source_type) for source_type in _items(source_types) if _text(source_type)
        }

        # 官方公告/官网 → 确认供应商
        if source_type_set & _HIGH_CREDIBILITY_SOURCES:
            return "CONFIRMED_SUPPLIER", False

        # 有供应链直接证据 → 确认供应商
        if has_supply_evidence:
            return "CONFIRMED_SUPPLIER", False

        # 多家新闻/报告 → 可能供应商
        medium_sources = source_type_set & _MEDIUM_CREDIBILITY_SOURCES
        if len(medium_sources) >= 2:
            return "PROBABLE_SUPPLIER", False
        if len(medium_sources) >= 1:
            return "PROBABLE_SUPPLIER", True

        # 概念页/指数页 → 仅概念声明
        if source_type_set & _CONCEPT_SOURCES:
            return "CONCEPT_CLAIM_ONLY", True

        # 招聘/展会/招标 → 早期信号
        if source_type_set & _EARLY_SOURCES:
            return "EARLY_SIGNAL", True

        # 论坛/社交 → 传闻
        if source_type_set & _LOW_CREDIBILITY_SOURCES:
            return "RUMOR_ONLY", True

        # 无外部证据 → 假设
        return "HYPOTHESIS", True

    def _tag_market_scope(self, market: str) -> Optional[str]:
        """根据市场类型生成风险提示.

        A 股和美股为核心市场，无需提示；其他市场标注风险提示。

        Args:
            market: 市场标识，如 "A_SHARE", "HK", "US" 等。

        Returns:
            风险提示字符串，核心市场返回 None。
        """
        market = _text(market)
        market_warnings: Dict[str, Optional[str]] = {
            "A_SHARE": None,
            "US": None,
            "HK": "港股市场，非A股核心标的",
            "JP": "日股市场，非A股核心标的",
            "TW": "台股市场，非A股核心标的",
            "KR": "韩股市场，非A股核心标的",
            "EU": "欧股市场，非A股核心标的",
            "OTHER": "非核心市场标的，需人工确认",
            "UNKNOWN": "非核心市场标的，需人工确认",
        }
        return market_warnings.get(market, "非核心市场标的，需人工确认")

    def _check_supply_evidence(
        self,
        company_name: str,
        node_name: str,
        source_docs: List[SourceDocument],
    ) -> bool:
        """检查是否存在供应链直接证据.

        在来源文档中搜索公司名与节点名同时出现的供应链关系描述。

        Args:
            company_name: 公司名称。
            node_name: 节点名称。
            source_docs: 来源文档列表。

        Returns:
            是否存在供应链直接证据。
        """
        company_name = _text(company_name)
        node_name = _text(node_name)
        if not company_name or not node_name:
            return False

        supply_keywords = re.compile(
            r"供应商|供货|提供.*产品|供应.*材料|供应.*设备|供应.*组件"
            r"|核心供应商|主要供应商|独家供应|一级供应商|合格供应商"
        )

        for doc in _items(source_docs):
            text = _text(getattr(doc, "content_text", ""))
            if company_name not in text or node_name not in text:
                continue
            # 检查公司名和节点名是否在同一上下文窗口且包含供应关系关键词
            for match in re.finditer(re.escape(company_name), text):
                start = max(0, match.start() - _CONTEXT_WINDOW)
                end = min(len(text), match.end() + _CONTEXT_WINDOW)
                context = text[start:end]
                if node_name in context and supply_keywords.search(context):
                    return True

        return False

    def _add_anchor_companies(
        self,
        early_signals: List[EarlySignal],
        chain: DynamicSupplyChain,
        node_to_bh: Dict[str, str],
        concept_signal: ConceptSignal,
        source_docs: List[SourceDocument],
        existing_candidates: List[CompanyCandidate],
        seen_keys: set[str],
    ) -> List[CompanyCandidate]:
        """将早期信号中的 anchor_companies 补充映射到链上节点.

        Args:
            early_signals: 早期信号列表。
            chain: 动态供应链。
            node_to_bh: 节点 → 瓶颈假设 ID 映射。
            concept_signal: 概念信号。
            source_docs: 来源文档列表。
            existing_candidates: 已有候选列表。
            seen_keys: 去重键集合。

        Returns:
            补充后的候选列表。
        """
        candidates = list(_items(existing_candidates))
        nodes = _nodes(chain)

        for es in _items(early_signals):
            anchor_companies = [
                _text(item).strip()
                for item in _items(getattr(es, "anchor_companies", None))
                if _text(item).strip()
            ]
            if not anchor_companies:
                continue

            # 尝试将 anchor_company 映射到最相关的节点
            claim = _text(getattr(es, "claim", ""))
            signal_type = _text(getattr(es, "signal_type", ""))
            source_document_id = _text(getattr(es, "source_document_id", ""))
            for company_name in anchor_companies:
                # 寻找最相关的节点：信号声明中提及的节点名
                best_node = self._find_relevant_node(
                    company_name, claim, nodes
                )
                if best_node is None:
                    continue

                best_node_id = _text(getattr(best_node, "node_id", ""))
                if not best_node_id:
                    continue
                dedup_key = f"{company_name}|{best_node_id}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                symbol, market = self._resolve_company_code(company_name)
                has_supply = self._check_supply_evidence(
                    company_name, _text(getattr(best_node, "name", "")), source_docs
                )
                evidence_level, needs_review = self._determine_evidence_level(
                    [claim[:200]] if claim else [], [signal_type], has_supply
                )
                market_warning = self._tag_market_scope(market)

                candidate = CompanyCandidate(
                    candidate_id=new_id("cc_"),
                    company_name=company_name,
                    symbol=symbol,
                    market=market,
                    market_warning=market_warning,
                    concept_id=_text(getattr(concept_signal, "concept_id", "")),
                    chain_node_id=best_node_id,
                    bottleneck_hypothesis_id=node_to_bh.get(best_node_id, ""),
                    mapping_sources=[claim[:200]] if claim else [],
                    evidence_level=evidence_level,
                    evidence_ids=[source_document_id] if source_document_id else [],
                    claim_summary=claim[:200] if claim else "",
                    needs_human_review=needs_review,
                )
                candidates.append(candidate)

        return candidates

    def _find_relevant_node(
        self,
        company_name: str,
        claim_text: str,
        nodes: List,
    ) -> Optional:
        """在信号声明中寻找与公司最相关的供应链节点.

        匹配策略：
        1. 精确匹配：节点名出现在 claim_text 中
        2. 关键词匹配：节点名的核心词（去掉概念前缀）出现在 claim_text 中
        3. 模糊匹配：claim_text 中的关键词与节点描述匹配

        Args:
            company_name: 公司名称。
            claim_text: 信号声明文本。
            nodes: 供应链节点列表。

        Returns:
            最相关的节点，若无匹配返回 None。
        """
        claim_text = _text(claim_text)
        if not claim_text:
            return None

        nodes = [node for node in _items(nodes) if _text(getattr(node, "node_id", ""))]

        # 1. 精确匹配
        for node in nodes:
            node_name = _text(getattr(node, "name", ""))
            if node_name and node_name in claim_text:
                return node

        # 2. 关键词匹配：从节点名中提取核心词
        # 例如 "AIPC核心材料" → "材料", "散热模组" → "散热"
        for node in nodes:
            # 去掉概念前缀，提取核心关键词
            core_words = self._extract_node_keywords(getattr(node, "name", ""))
            for word in core_words:
                if word and len(word) >= 2 and word in claim_text:
                    return node

        # 3. 模糊匹配：节点描述中的关键词
        for node in nodes:
            description = _text(getattr(node, "description", ""))
            if description and description in claim_text:
                return node

        return None

    @staticmethod
    def _extract_node_keywords(node_name: str) -> List[str]:
        """从节点名中提取核心关键词.

        例如 "AIPC核心材料" → ["材料", "核心材料"]
        "散热模组" → ["散热", "模组", "散热模组"]
        """
        # 去掉常见前缀
        prefixes = ["AIPC", "AI"]
        name = _text(node_name)
        if not name:
            return []
        for p in prefixes:
            if name.startswith(p):
                name = name[len(p):]

        # 去掉连接词
        for connector in ["核心", "关键", "重要", "主要"]:
            name = name.replace(connector, "")

        # 拆分为 2-4 字的子串
        keywords = []
        if len(name) >= 2:
            keywords.append(name)
            # 尝试 2 字窗口
            for i in range(len(name) - 1):
                w = name[i:i+2]
                if any('\u4e00' <= c <= '\u9fff' for c in w):
                    keywords.append(w)

        return keywords

    def _add_signal_companies(
        self,
        early_signals: List[EarlySignal],
        chain: DynamicSupplyChain,
        node_to_bh: Dict[str, str],
        concept_signal: ConceptSignal,
        source_docs: List[SourceDocument],
        existing_candidates: List[CompanyCandidate],
        seen_keys: set[str],
    ) -> List[CompanyCandidate]:
        """从 EarlySignal 的 mentioned_companies 中提取公司并映射到最相关节点.

        手动信号文件中的公司可能与特定节点名不匹配，
        但它们属于该概念的供应链。本方法尝试将公司映射到
        最相关的节点（通过 claim/evidence_text 匹配），
        若无匹配则映射到第一个非 terminal 节点。

        Args:
            early_signals: 早期信号列表。
            chain: 动态供应链。
            node_to_bh: 节点 → 瓶颈假设 ID 映射。
            concept_signal: 概念信号。
            source_docs: 来源文档列表。
            existing_candidates: 已有候选列表。
            seen_keys: 去重键集合。

        Returns:
            补充后的候选列表。
        """
        candidates = list(_items(existing_candidates))
        nodes = _nodes(chain)

        # 找到第一个非 terminal 节点作为默认映射目标
        default_node = None
        for n in nodes:
            if _text(getattr(n, "layer", "")) not in ("terminal",):
                default_node = n
                break
        if default_node is None and nodes:
            default_node = nodes[0]

        for es in _items(early_signals):
            all_companies = list(_items(getattr(es, "mentioned_companies", None)))
            all_companies += list(_items(getattr(es, "anchor_companies", None)))
            if not all_companies:
                continue

            for company_name in all_companies:
                company_name = _text(company_name).strip()
                if not company_name:
                    continue
                # 尝试找到最相关的节点
                best_node = self._find_relevant_node(
                    company_name,
                    _text(getattr(es, "claim", ""))
                    + " "
                    + _text(getattr(es, "evidence_text", "")),
                    nodes,
                )
                if best_node is None:
                    # 尝试通过 evidence_text 中的关键词匹配
                    evidence_text = _text(getattr(es, "evidence_text", ""))
                    for node in nodes:
                        if _text(getattr(node, "name", "")) in evidence_text:
                            best_node = node
                            break

                if best_node is None:
                    best_node = default_node

                if best_node is None:
                    continue

                node_id = _text(getattr(best_node, "node_id", ""))
                if not node_id:
                    continue
                dedup_key = f"{company_name}|{node_id}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                symbol, market = self._resolve_company_code(company_name)
                has_supply = self._check_supply_evidence(
                    company_name, _text(getattr(best_node, "name", "")), source_docs
                )
                claim = _text(getattr(es, "claim", ""))
                signal_type = _text(getattr(es, "signal_type", ""))
                source_document_id = _text(getattr(es, "source_document_id", ""))
                evidence_level, needs_review = self._determine_evidence_level(
                    [claim[:200]] if claim else [],
                    [signal_type],
                    has_supply,
                )
                market_warning = self._tag_market_scope(market)

                candidate = CompanyCandidate(
                    candidate_id=new_id("cc_"),
                    company_name=company_name,
                    symbol=symbol,
                    market=market,
                    market_warning=market_warning,
                    concept_id=_text(getattr(concept_signal, "concept_id", "")),
                    chain_node_id=node_id,
                    bottleneck_hypothesis_id=node_to_bh.get(node_id, ""),
                    mapping_sources=[claim[:200]] if claim else [],
                    evidence_level=evidence_level,
                    evidence_ids=[source_document_id] if source_document_id else [],
                    claim_summary=claim[:200] if claim else "",
                    needs_human_review=needs_review,
                )
                candidates.append(candidate)

        return candidates
