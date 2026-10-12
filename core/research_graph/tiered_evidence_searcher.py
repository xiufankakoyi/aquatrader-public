"""分层证据搜索引擎：按质量阶梯分批拉取单个候选节点的证据。

职责边界：
- 本类只负责“为一个候选节点执行 tiered 搜索”和“把文档送入 EvidenceActivator”
- 节点级状态评估使用 BayesianStatusUpgrader，仅作为独立搜索时的早停启发
- Claim 级充分性判断（关键槽覆盖、独立来源、反证搜索完成度、冲突解决等）
  由 ResearchOrchestrator 中的 ClaimPolicy 统一处理，不在本模块实现

设计思路：
- 把证据来源按可信度分为高/中/低三个阶梯
- 每轮只搜索当前阶梯指定的来源类型/查询后缀
- 每轮结束后把结果送入 EvidenceActivator 升级候选状态
- 若候选已升级到目标状态，或预算耗尽，则停止，不再进入更低质量阶梯
"""
from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from typing import Any

from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader
from core.research_graph.candidate_graph import CandidateNode
from core.research_graph.candidate_schema import (
    SOURCE_QUALITY_BOOST,
    CandidateStatus,
)
from core.research_graph.models import utc_now
from core.research_graph.search_cache import SearchCache


@dataclass
class TierConfig:
    """单个质量阶梯的配置。"""

    name: str
    source_types: tuple[str, ...]
    query_suffixes: tuple[str, ...]
    default_source_type: str
    max_results_per_query: int = 5
    max_queries: int = 5


@dataclass
class TieredSearchResult:
    """分层搜索结果摘要。"""

    candidate_id: str
    target_status: str
    final_status: str
    tiers_executed: list[str] = field(default_factory=list)
    documents_found: int = 0
    documents_activated: int = 0
    stopped_reason: str = ""
    status_trajectory: list[tuple[str, str]] = field(default_factory=list)
    budget_estimate: dict[str, Any] = field(default_factory=dict)
    counter_search_completed: bool = False
    counter_search_executed: bool = False
    counter_zero_results: bool = False
    independent_sources: int = 0
    query_audit: list[dict[str, Any]] = field(default_factory=list)


def build_tiered_queries(
    terms: list[str],
    *,
    tier_name: str,
    query_suffixes: tuple[str, ...],
    max_queries: int = 5,
    counter_pattern: str = "否认 澄清 未供货 终止",
    counter_terms: list[str] | None = None,
) -> list[tuple[str, str]]:
    """统一构造某质量层的支持/反证查询列表。

    这是 TieredEvidenceSearcher 与 ResearchOrchestrator 共享的查询构造逻辑，
    避免两套控制循环各自维护 suffix 与反证模式。

    Args:
        terms: 查询主体词列表（节点名、别名、claim 的 subject/object 等）。
        tier_name: 质量层名称，仅用于日志/元信息。
        query_suffixes: 该层后缀元组。
        max_queries: 该层最大查询条数；保留 1 条给反证查询。
        counter_pattern: 反证查询模板后缀（默认值）。
        counter_terms: 来自 Claim 的自定义反证词；非空时覆盖 counter_pattern，
            使 Claim 级反证配置生效。

    Returns:
        列表元素为 (query_text, purpose)，purpose ∈ {"support", "counter"}。
    """
    terms = [t for t in terms if t]
    if not terms:
        return []

    queries: list[tuple[str, str]] = []
    support_limit = max(0, max_queries - 1)
    count = 0
    for term in terms:
        for suffix in query_suffixes:
            if count >= support_limit:
                break
            queries.append((f"{term} {suffix}", "support"))
            count += 1
        if count >= support_limit:
            break

    # 反证查询后缀：优先使用 Claim 的 counter_terms，否则回退到默认 counter_pattern
    if counter_terms:
        counter_suffix = " ".join(counter_terms)
    else:
        counter_suffix = counter_pattern
    for term in terms[:1]:
        queries.append((f"{term} {counter_suffix}", "counter"))
    return queries


# 默认三层配置：高/中/低质量来源
DEFAULT_TIER_CONFIGS: list[TierConfig] = [
    TierConfig(
        name="high",
        source_types=("announcement", "official", "financial_report", "local_cache"),
        query_suffixes=("公告", "财报", "官方", "年报", "半年报"),
        default_source_type="announcement",
        max_results_per_query=5,
        max_queries=5,
    ),
    TierConfig(
        name="medium",
        source_types=("news", "crawler", "api"),
        query_suffixes=("新闻", "报道", "供应链", "最新消息"),
        default_source_type="news",
        max_results_per_query=5,
        max_queries=4,
    ),
    TierConfig(
        name="low",
        source_types=("blog", "forum", "unknown"),
        query_suffixes=("论坛", "博客", "讨论", "分析"),
        default_source_type="forum",
        max_results_per_query=3,
        max_queries=3,
    ),
]


class TieredEvidenceSearcher:
    """按质量阶梯分批搜索证据，并动态决定是否进入下一阶梯。"""

    CACHE_POLICY_VERSION = "tiered-evidence-v2"
    _COUNTER_PATTERNS = (
        r"否认|澄清.{0,12}(?:不属实|未|没有)|并未|从未|不再|终止|取消",
        r"未(?:供货|合作|签订|量产|进入|通过|获得)|不存在.{0,10}(?:合作|供应)",
    )

    def __init__(
        self,
        search_discovery: Any,
        candidate_store: Any,
        evidence_activator: Any,
        *,
        tiers: list[TierConfig] | None = None,
        bayesian_upgrader: BayesianStatusUpgrader | None = None,
        target_status: str = CandidateStatus.ACTIVE.value,
        max_total_documents: int = 30,
        search_cache: SearchCache | None = None,
        counter_terms: list[str] | None = None,
    ) -> None:
        """初始化分层搜索引擎。

        Args:
            search_discovery: 搜索后端实例（如 SearchDiscovery）。
            candidate_store: CandidateStore 实例，用于读取/更新候选状态。
            evidence_activator: EvidenceActivator 实例，用于激活证据。
            tiers: 自定义质量阶梯配置；为 None 时使用默认三层。
            bayesian_upgrader: 自定义贝叶斯升级器；为 None 时使用默认。
            target_status: 搜索期望达到的目标状态，默认 ACTIVE。
            max_total_documents: 整个搜索过程最多处理的文档数。
            search_cache: 搜索去重缓存；为 None 时不启用缓存。
            counter_terms: 自定义反证查询词；为 None 时使用默认 counter_pattern。
        """
        self.search_discovery = search_discovery
        self.candidate_store = candidate_store
        self.evidence_activator = evidence_activator
        self.tiers = tiers or list(DEFAULT_TIER_CONFIGS)
        self.bayesian_upgrader = bayesian_upgrader or BayesianStatusUpgrader()
        self.target_status = target_status
        self.max_total_documents = max(max_total_documents, 1)
        self.search_cache = search_cache
        self._counter_terms = list(counter_terms) if counter_terms else None

    def search_for_node(self, node: CandidateNode) -> TieredSearchResult:
        """为单个候选节点执行分层搜索，直到达标或阶梯耗尽。

        Args:
            node: 候选节点。

        Returns:
            TieredSearchResult，包含执行了哪些阶梯、最终状态、停止原因。
        """
        result = TieredSearchResult(
            candidate_id=node.candidate_id,
            target_status=self.target_status,
            final_status=node.status,
            status_trajectory=[(utc_now(), node.status)],
        )
        total_documents = 0

        for tier in self.tiers:
            if total_documents >= self.max_total_documents:
                result.stopped_reason = f"达到总文档上限 {self.max_total_documents}"
                break

            # 1. 构建并执行当前阶梯的查询（保留 purpose 区分 support/counter）
            typed_queries = self._build_queries(node, tier)
            if not typed_queries:
                continue
            query_key = self._cache_query_key(
                [q for q, _ in typed_queries], tier,
            )
            content_version = self.CACHE_POLICY_VERSION
            if self.search_cache is not None and self.search_cache.is_tier_searched(
                node.candidate_id, tier.name, query_key, content_version=content_version
            ):
                result.tiers_executed.append(f"{tier.name}(cached)")
                continue

            # 分组执行：support 查询和 counter 查询分别调用 search_many，
            # 才能独立确认反证查询是否实际执行并返回了结果。
            support_queries = [q for q, p in typed_queries if p == "support"]
            counter_queries = [q for q, p in typed_queries if p == "counter"]

            support_docs: list[Any] = []
            counter_docs: list[Any] = []
            if support_queries:
                support_docs = self._search_many_flat(support_queries)
            if counter_queries:
                counter_docs = self._search_many_flat(counter_queries)

            # 反证归因（P0 修复）：
            # - counter_search_executed：反证查询已实际执行（与“未执行”可区分）
            # - counter_zero_results：反证查询执行但零命中（不得与“未执行”混淆）
            # - counter_search_completed：反证查询已执行且结果完成评估（激活后置位）
            counter_executed = bool(counter_queries)
            counter_zero = counter_executed and len(counter_docs) == 0
            result.counter_search_executed = result.counter_search_executed or counter_executed
            if counter_executed and counter_zero:
                result.counter_zero_results = True

            # 保留 purpose/query_id：禁止在合并 support/counter 时丢失归因。
            support_query_id = self._query_id(tier.name, "support", support_queries)
            counter_query_id = self._query_id(tier.name, "counter", counter_queries)
            tagged_results: list[tuple[Any, str, str]] = []
            for doc in support_docs:
                if doc:
                    tagged_results.append((doc, "support", support_query_id))
            for doc in counter_docs:
                if doc:
                    tagged_results.append((doc, "counter", counter_query_id))

            documents = self._flatten_and_dedupe_tagged(
                tagged_results, tier, limit_per_query=tier.max_results_per_query
            )

            result.tiers_executed.append(tier.name)
            result.documents_found += len(documents)
            if self.search_cache is not None:
                self.search_cache.mark_tier_searched(
                    node.candidate_id,
                    tier.name,
                    query_key,
                    metadata={"queries": [q for q, _ in typed_queries], "documents_found": len(documents)},
                    content_version=content_version,
                )

            # 2. 把搜索结果作为证据激活，每处理一条后实时检查是否达标
            activated = 0
            counter_evaluated = False
            for doc, purpose, query_id in documents:
                if total_documents >= self.max_total_documents:
                    result.stopped_reason = f"达到总文档上限 {self.max_total_documents}"
                    break
                # 反证文档送入激活即视为“已评估”（无论是否命中节点）。
                if purpose == "counter":
                    counter_evaluated = True
                if not self._activate_document(node, doc, tier, query_id=query_id, query_purpose=purpose):
                    continue

                activated += 1
                total_documents += 1

                # 实时刷新节点状态，一旦达标立即停止当前 tier 及后续 tier
                refreshed = self.candidate_store.get_node(node.candidate_id)
                if refreshed is None:
                    result.stopped_reason = "候选节点在搜索过程中丢失"
                    break
                node = refreshed
                result.final_status = node.status

                reached, independent = self._node_status_reached(node)
                result.independent_sources = independent
                if reached:
                    result.stopped_reason = (
                        f"节点级贝叶斯状态已达到目标 {self.target_status}"
                    )
                    break

            # 反证查询已执行且结果完成评估（激活即评估）后，才标记完成。
            # 零命中也视为“已评估无反证”，通过 counter_zero_results 与未执行区分。
            if counter_executed and (counter_evaluated or counter_zero):
                result.counter_search_completed = True

            audit_entry = {
                "tier": tier.name,
                "support_query_id": support_query_id,
                "counter_query_id": counter_query_id,
                "counter_search_executed": counter_executed,
                "counter_zero_results": counter_zero,
                "counter_results": len(counter_docs),
                "counter_results_evaluated": counter_evaluated or counter_zero,
                "counter_search_completed": bool(
                    counter_executed and (counter_evaluated or counter_zero)
                ),
                "evaluated_at": utc_now(),
            }
            result.query_audit.append(audit_entry)
            # Persist the receipt separately from evidence stance. A counter
            # document is not itself proof that counter-search completed.
            refreshed_for_audit = self.candidate_store.get_node(node.candidate_id)
            if refreshed_for_audit is not None and hasattr(self.candidate_store, "update_node"):
                attrs = dict(refreshed_for_audit.attributes or {})
                history = list(attrs.get("query_audit") or [])
                history.append(audit_entry)
                attrs["query_audit"] = history[-100:]
                attrs["counter_search_executed"] = result.counter_search_executed
                attrs["counter_zero_results"] = result.counter_zero_results
                attrs["counter_search_completed"] = result.counter_search_completed
                self.candidate_store.update_node(node.candidate_id, attributes=attrs)
                node = self.candidate_store.get_node(node.candidate_id) or node

            result.documents_activated += activated
            result.status_trajectory.append((utc_now(), node.status))

            # 3. 预算估计：判断还需要多少证据才能到目标状态
            budget = self._estimate_remaining_budget(node)
            result.budget_estimate[tier.name] = budget

            # 4. 停止条件检查（循环已因达标 break 时，stopped_reason 已设置）
            if result.stopped_reason:
                break

            # 当前层无匹配时仍应进入下一层，避免高质量来源暂不可用导致流程断路。

        else:
            if not result.stopped_reason:
                result.stopped_reason = "所有质量阶梯已耗尽仍未达标"

        return result

    def _build_queries(self, node: CandidateNode, tier: TierConfig) -> list[tuple[str, str]]:
        """构造当前 tier 的查询列表，保留 purpose 信息。

        Returns:
            list[tuple[str, str]]，元素为 (query_text, purpose)，
            purpose ∈ {"support", "counter"}。
        """
        terms = [node.name, *node.aliases]
        queries = build_tiered_queries(
            terms,
            tier_name=tier.name,
            query_suffixes=tier.query_suffixes,
            max_queries=tier.max_queries,
            counter_terms=self._counter_terms,
        )
        return queries

    def _search_many_flat(self, queries: list[str]) -> list[Any]:
        """调用 search_many 并把返回值统一转为扁平 list。

        兼容两种后端契约：
        - ``MultiEngineSearcher.search_many`` → ``list[SourceDocument]``
        - ``SearchDiscovery.search_many`` → ``Dict[str, List[SourceDocument]]``

        Args:
            queries: 查询列表。

        Returns:
            扁平文档列表。
        """
        if not queries:
            return []
        raw = self.search_discovery.search_many(queries)
        if isinstance(raw, dict):
            flat: list[Any] = []
            for doc_list in raw.values():
                if isinstance(doc_list, list):
                    flat.extend(doc_list)
            return flat
        if isinstance(raw, list):
            return raw
        return []

    def _node_status_reached(self, node: CandidateNode) -> tuple[bool, int]:
        """节点级贝叶斯状态评估。

        注意：本方法仅用于 TieredEvidenceSearcher 作为独立搜索工具时的早停启发；
        Claim 级充分性判断（独立来源、反证搜索完成度、冲突解决等）必须由
        ResearchOrchestrator 中的 ClaimPolicy 统一处理。
        """
        decision = self.bayesian_upgrader.decide(
            node.status,
            node.evidence_slots,
            stance="support",
            source_quality=SOURCE_QUALITY_BOOST.get("announcement", 1.5),
        )
        order = {
            CandidateStatus.DISCOVERED.value: 0,
            CandidateStatus.CANDIDATE.value: 1,
            CandidateStatus.SUPPORTED.value: 2,
            CandidateStatus.ACTIVE.value: 3,
            CandidateStatus.TRUSTED.value: 4,
        }
        reached = order.get(decision.new_status, -1) >= order.get(self.target_status, -1)

        # 独立来源计数：按血缘折叠，仅用于日志/预算估计
        strongest: dict[str, dict[str, Any]] = {}
        for slot in node.evidence_slots:
            if slot.get("evidence_kind") in {
                "generated", "pseudo_evidence", "retrieval_hypothesis", "query_prototype"
            }:
                continue
            lineage = str(
                slot.get("source_lineage_id")
                or slot.get("correlation_group")
                or slot.get("source_url")
                or slot.get("evidence_id")
                or ""
            )
            current = strongest.get(lineage)
            if current is None or float(slot.get("quality", 0)) * float(slot.get("match_score", 0)) > float(current.get("quality", 0)) * float(current.get("match_score", 0)):
                strongest[lineage] = slot
        return reached, len(strongest)

    def _cache_query_key(self, queries: list[str], tier: TierConfig) -> str:
        backend = type(self.search_discovery).__name__
        payload = {
            "queries": sorted(queries),
            "backend": backend,
            "tier": tier.name,
            "policy": self.CACHE_POLICY_VERSION,
        }
        return hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]

    def _infer_source_type(self, document: Any, tier: TierConfig) -> str:
        """基于 URL/标题/SearXNG engine 推断文档的 source_type。

        优先级：URL 域名 > 标题关键词 > SearXNG engine > tier 默认值。
        """
        url = (getattr(document, "url", "") or "").lower()
        title = (getattr(document, "title", "") or "").lower()
        text = (getattr(document, "content_text", "") or "")[:200].lower()
        metadata = getattr(document, "metadata", {}) or {}
        engines = metadata.get("engines", [])
        if isinstance(engines, str):
            engines = [engines]
        engines = [str(e).lower() for e in engines]

        # 1. URL 域名规则
        url_rules: list[tuple[str, str]] = [
            (r"cninfo\.com\.cn|sse\.com\.cn|szse\.cn|hkexnews\.hk", "announcement"),
            (r"xinhuanet\.com|people\.com\.cn|news\.cn|chinadaily\.com\.cn", "official"),
            (r"jiemian\.com|36kr\.com|cls\.cn|caijing\.com\.cn|sina\.com\.cn|sohu\.com", "news"),
            (r"xueqiu\.com|guba\.eastmoney\.com|tieba\.baidu\.com|zhihu\.com", "forum"),
            (r"github\.com|pypi\.org|npmjs\.com", "api"),
        ]
        for pattern, inferred in url_rules:
            if re.search(pattern, url):
                return inferred

        # 2. 标题关键词规则
        title_text = title + " " + text
        keyword_rules: list[tuple[tuple[str, ...], str]] = [
            (("公告", "公示", "年报", "半年报", "财报", "业绩快报"), "announcement"),
            (("研报", "研究报告", "券商", "投行"), "financial_report"),
            (("论坛", "讨论", "股吧", "帖子"), "forum"),
            (("博客", "专栏", "自媒体"), "blog"),
            (("新闻", "报道", "快讯"), "news"),
        ]
        for keywords, inferred in keyword_rules:
            if any(kw in title_text for kw in keywords):
                return inferred

        # 3. SearXNG engine 规则
        engine_rules: dict[str, str] = {
            "bing news": "news",
            "google news": "news",
            "baidu": "news",
            "duckduckgo": "news",
        }
        for engine in engines:
            for key, inferred in engine_rules.items():
                if key in engine:
                    return inferred

        # 4. fallback 到 tier 默认值
        return tier.default_source_type

    def _flatten_and_dedupe(
        self,
        search_results: list[Any],
        tier: TierConfig,
        limit_per_query: int,
    ) -> list[Any]:
        """把 search_many 的扁平结果去重、截断。

        MultiEngineSearcher.search_many 已在内部按查询维度去重，
        返回扁平 list[SourceDocument]。此方法只做 URL 去重和 tier 级截断。
        """
        seen_urls: set[str] = set()
        documents: list[Any] = []
        for doc in search_results:
            if not doc:
                continue
            url = getattr(doc, "url", "") or ""
            if url and url.lower() in seen_urls:
                continue
            if url:
                seen_urls.add(url.lower())
            # 基于 URL/标题/SearXNG engine 推断更准确的 source_type
            inferred_source_type = self._infer_source_type(doc, tier)
            if hasattr(doc, "source_type"):
                doc.source_type = inferred_source_type
            documents.append(doc)
            if len(documents) >= limit_per_query * 3:
                break
        return documents

    def _flatten_and_dedupe_tagged(
        self,
        tagged_results: list[tuple[Any, str, str]],
        tier: TierConfig,
        limit_per_query: int,
    ) -> list[tuple[Any, str, str]]:
        """去重、截断，同时保留每条文档的 purpose 与 query_id 归因。

        P0 修复：去重 key 从 url 改为 (url, purpose)，这样同一 URL 可以
        同时作为 support 和 counter 各保留一条，不会丢失 counter 归因。
        同一 URL 同一 purpose 出现多次时仍只保留第一条。
        """
        seen_keys: set[tuple[str, str]] = set()
        documents: list[tuple[Any, str, str]] = []
        # Safety first: reserve the bounded evaluation budget for counter
        # results before support results. Otherwise a large support result set
        # can consume the cap and make counter_search look executed but never
        # evaluated.
        ordered_results = sorted(
            tagged_results,
            key=lambda item: 0 if item[1] == "counter" else 1,
        )
        for doc, purpose, query_id in ordered_results:
            if not doc:
                continue
            url = getattr(doc, "url", "") or ""
            dedupe_key = (url.lower(), purpose)
            if url and dedupe_key in seen_keys:
                continue
            if url:
                seen_keys.add(dedupe_key)
            inferred_source_type = self._infer_source_type(doc, tier)
            if hasattr(doc, "source_type"):
                doc.source_type = inferred_source_type
            documents.append((doc, purpose, query_id))
            if len(documents) >= limit_per_query * 3:
                break
        return documents

    @staticmethod
    def _query_id(tier_name: str, purpose: str, queries: list[str]) -> str:
        """为一批查询生成稳定 query_id，用于反证归因追溯。"""
        if not queries:
            return ""
        payload = {"tier": tier_name, "purpose": purpose, "queries": sorted(queries)}
        digest = hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        return f"{tier_name}:{purpose}:{digest}"

    def _activate_document(
        self,
        node: CandidateNode,
        document: Any,
        tier: TierConfig,
        *,
        query_id: str = "",
        query_purpose: str = "",
    ) -> bool:
        """把单个文档作为证据送入 EvidenceActivator。

        拼接 title + content 作为匹配文本，因为搜索结果 snippet
        通常很短，节点名更可能出现在 title 中。

        Returns:
            是否成功激活（文档内容与节点相关）。
        """
        # 统一获取文本：兼容 RawDocument(content) 和 perilla SourceDocument(content_text)
        content = getattr(document, "content_text", "") or getattr(document, "content", "")
        title = getattr(document, "title", "") or ""
        # 拼接 title + content 提高匹配命中率
        text = f"{title} {content}".strip() if title else content
        if not text:
            return False

        doc_id = getattr(document, "document_id", "") or str(id(document))
        source_type = getattr(document, "source_type", tier.default_source_type)

        # 必须使用原文做相关性匹配，禁止把候选名拼入文本制造命中。
        evidence_text = text
        url = getattr(document, "url", "") or ""
        metadata = getattr(document, "metadata", {}) or {}
        published_at = getattr(document, "published_at", "") or ""
        retrieved_at = getattr(document, "retrieved_at", "") or utc_now()
        lineage_id = self._source_lineage_id(url, text, metadata)
        stance = self._infer_stance(text)
        activation_result = self.evidence_activator.activate(
            evidence_text=evidence_text,
            evidence_id=doc_id,
            source_type=source_type,
            stance=stance,
            source_url=url,
            source_lineage_id=lineage_id,
            published_at=str(published_at),
            retrieved_at=str(retrieved_at),
            evidence_kind="document",
            query_id=query_id,
            query_purpose=query_purpose,
        )
        return node.candidate_id in activation_result.matched_node_ids

    @classmethod
    def _infer_stance(cls, text: str) -> str:
        """保守识别明确反证；无法判断的材料保持中性，避免默认加分。"""
        normalized = re.sub(r"\s+", "", text or "")
        if any(re.search(pattern, normalized) for pattern in cls._COUNTER_PATTERNS):
            return "counter"
        support_markers = (
            "公告", "确认", "签署", "签订", "供货", "合作", "量产",
            "通过验证", "中标", "收入", "客户", "供应商",
        )
        if any(marker in normalized for marker in support_markers):
            return "support"
        return "neutral"

    @staticmethod
    def _source_lineage_id(url: str, text: str, metadata: dict[str, Any]) -> str:
        """生成保守的来源血缘键，优先使用上游提供的原始来源标识。"""
        explicit = (
            metadata.get("source_lineage_id")
            or metadata.get("original_source_id")
            or metadata.get("canonical_url")
        )
        if explicit:
            return str(explicit)
        if url:
            parts = urlsplit(url)
            canonical = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        else:
            canonical = ""
        content_fingerprint = hashlib.sha1(
            re.sub(r"\s+", "", text or "").encode("utf-8")
        ).hexdigest()[:16]
        # 完全相同的转载摘要即使 URL 不同也归并；非重复文本保留 URL 身份。
        return f"content:{content_fingerprint}" if text else f"url:{canonical}"

    def _estimate_remaining_budget(self, node: CandidateNode) -> dict[str, Any]:
        """估算当前节点到目标状态还需要多少高质量证据。"""
        decision = self.bayesian_upgrader.decide(
            node.status,
            node.evidence_slots,
            stance="support",
            source_quality=SOURCE_QUALITY_BOOST.get("announcement", 1.5),
        )
        return {
            "current_status": node.status,
            "target_status": self.target_status,
            "decision_score": round(
                0.7 * decision.posterior_mean + 0.3 * decision.credible_lower, 4
            ),
            "threshold": self.bayesian_upgrader.confidence_thresholds.get(
                node.status, 0.75
            ),
            "sample_size": round(decision.sample_size, 2),
            "remaining_gap": round(
                max(
                    0.0,
                    self.bayesian_upgrader.confidence_thresholds.get(node.status, 0.75)
                    - (0.7 * decision.posterior_mean + 0.3 * decision.credible_lower),
                ),
                4,
            ),
        }

    @staticmethod
    def _status_reached(current: str, target: str) -> bool:
        """判断当前状态是否已达到或超过目标状态。"""
        order = {
            CandidateStatus.DISCOVERED.value: 0,
            CandidateStatus.CANDIDATE.value: 1,
            CandidateStatus.SUPPORTED.value: 2,
            CandidateStatus.ACTIVE.value: 3,
            CandidateStatus.TRUSTED.value: 4,
        }
        return order.get(current, -1) >= order.get(target, -1)
