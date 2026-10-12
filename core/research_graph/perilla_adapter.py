"""PerillaKnowledgeLoopEngine → EvidenceLedger / GraphStore 适配器。

打通 Perilla 的 LLM 抽取产出(raw_documents / candidate_concepts / evidence_edges)
与 research_graph 的 Evidence 体系,使 neuron engine 能消费 Perilla 的 LLM 产出。

ID 约定(统一为 node_id,方案 C):
  - Evidence.supports / contradicts 存 node_id(即 HyperNode.node_id)
  - EvidenceIndex.lookup 期望 node_id,可直接匹配
  - 冷启动时 neuron_id == node_id,后续通过 EvidenceIndex 反查

evidence_level 映射(report_causal_extractor → research_graph):
  - probable → LIKELY
  - weak_signal → SPECULATIVE
  - hypothesis → SPECULATIVE
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from core.research_graph.evidence_ledger import EvidenceLedger
from core.research_graph.models import Evidence, HyperEdge, HyperNode
from core.research_graph.schema import EdgeStatus, EvidenceLevel, NodeSource
from core.research_graph.store import GraphStore

logger = logging.getLogger(__name__)


# ── evidence_level 映射表 ──
_EVIDENCE_LEVEL_MAP: dict[str, str] = {
    "probable": EvidenceLevel.LIKELY.value,
    "weak_signal": EvidenceLevel.SPECULATIVE.value,
    "hypothesis": EvidenceLevel.SPECULATIVE.value,
    "official": EvidenceLevel.LIKELY.value,
    "report": EvidenceLevel.SPECULATIVE.value,
    "news": EvidenceLevel.SPECULATIVE.value,
    "weak_text": EvidenceLevel.SPECULATIVE.value,
    "unknown": EvidenceLevel.SPECULATIVE.value,
}


# ── source_type 归一化(Perilla source_type → research_graph evidence_weight key)──
# 与 NeuronPolicyConfig.evidence_weight 对齐,避免走 0.3 fallback
_SOURCE_TYPE_NORMALIZE: dict[str, str] = {
    # Perilla 产出的 source_type → evidence_weight key
    "announcement": "announcement",
    "annual_report": "annual_report",
    "semiannual_report": "semiannual_report",
    "quarterly_report": "quarterly_report",
    "periodic_report": "periodic_report",
    "research_report": "research_report",
    "industry_report": "industry_report",
    "investor_interaction": "investor_interaction",
    "news": "news",
    "online_news": "news",
    "live_feed": "news",
    "inbox": "local_structured_seed",
    "local_title": "local_structured_seed",
    "local_documents": "local_structured_seed",
    "hot_theme": "search_snippet",
    "semantic_search": "search_snippet",
    "search_result": "search_snippet",
    "weak_text": "forum_social",
}


def _normalize_source_type(raw: str) -> str:
    """把 Perilla 产出的 source_type 归一化到 evidence_weight key。

    Args:
        raw: Perilla raw_documents/evidence_items 里的 source_type 字符串。

    Returns:
        归一化后的 source_type,未命中映射表则原样返回(走 0.3 fallback)。
    """
    if not raw:
        return "search_snippet"
    return _SOURCE_TYPE_NORMALIZE.get(raw, raw)


def _make_evidence_id(document_id: str, quote: str, stance: str) -> str:
    """生成稳定的 evidence_id。

    基于 (document_id + quote 前 80 字 + stance) 的 sha1 前 16 位,
    确保同一文档同一引文同一立场只生成一个 evidence_id(GraphStore 按 ID 覆盖去重)。

    Args:
        document_id: 文档 ID。
        quote: 证据引文。
        stance: support / counter / neutral。

    Returns:
        形如 ev_perilla_<sha1[:16]> 的 evidence_id。
    """
    import hashlib
    raw = f"{document_id}|{quote[:80]}|{stance}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"ev_perilla_{h}"


def ingest_perilla_result(
    graph_store: GraphStore,
    loop_result: Any,
    *,
    operator: str = "perilla_adapter",
    default_stance: str = "support",
    evidence_ledger: EvidenceLedger | None = None,
) -> dict[str, int]:
    """把 PerillaKnowledgeLoopEngine 的 LoopRunResult 写入 GraphStore。

    流程:
      1. 从 loop_result.raw_documents 构造 HyperNode(若不存在)
      2. 从 loop_result.candidate_concepts 抽取 node_id + evidence
      3. 从 loop_result.evidence_edges 构造 HyperEdge(若不存在)
      4. 把 evidence 写入 GraphStore.evidence,supports/contradicts 存 node_id

    Args:
        graph_store: 目标 GraphStore。
        loop_result: PerillaKnowledgeLoopEngine.run() 的返回值(LoopRunResult dataclass)。
        operator: mutation log 中的操作者标识。
        default_stance: 无明确立场信息时默认的 stance(support / counter)。

    Returns:
        统计字典:{"nodes_added": N, "edges_added": N, "evidence_added": N,
                  "evidence_skipped": N}
    """
    stats = {"nodes_added": 0, "edges_added": 0, "evidence_added": 0, "evidence_skipped": 0}
    ledger = evidence_ledger or EvidenceLedger(graph_store)

    # 提取字段(LoopRunResult dataclass)
    raw_documents: list[dict[str, Any]] = getattr(loop_result, "raw_documents", []) or []
    candidate_concepts: list[dict[str, Any]] = getattr(loop_result, "candidate_concepts", []) or []
    evidence_edges: list[dict[str, Any]] = getattr(loop_result, "evidence_edges", []) or []

    # 1. 构造 doc_by_id 索引
    doc_by_id: dict[str, dict[str, Any]] = {}
    for doc in raw_documents:
        did = str(doc.get("document_id", ""))
        if did:
            doc_by_id[did] = doc

    # 2. 从 candidate_concepts 抽取 node + evidence
    # candidate_concepts 字段:normalized_name / term_type / root_theme /
    #                       evidence_document_ids / evidence_quotes / related_companies
    for concept in candidate_concepts:
        name = str(concept.get("normalized_name", "")).strip()
        if not name:
            continue
        node_id = f"node_{name}"  # 稳定 node_id
        node_type = str(concept.get("term_type", "CONCEPT")).upper()

        # 2a. 构造 HyperNode(若不存在)
        if node_id not in graph_store.nodes:
            node = HyperNode(
                node_id=node_id,
                name=name,
                type=node_type,
                aliases=[],
                source=NodeSource.SEED_GRAPH.value,
                attributes={"root_theme": str(concept.get("root_theme", ""))},
            )
            graph_store.add_node(node, operator=operator)
            stats["nodes_added"] += 1

        # 2b. 为该 concept 的 evidence_documents 创建 Evidence
        ev_doc_ids = concept.get("evidence_document_ids", []) or []
        ev_quotes = concept.get("evidence_quotes", []) or []
        related_companies = concept.get("related_companies", []) or []

        for idx, doc_id in enumerate(ev_doc_ids[:5]):  # 限制每个 concept 最多 5 条
            doc = doc_by_id.get(str(doc_id))
            if not doc:
                stats["evidence_skipped"] += 1
                continue
            quote = ev_quotes[idx] if idx < len(ev_quotes) else str(doc.get("title", ""))
            evidence = _build_evidence_from_doc(
                doc=doc,
                quote=quote,
                node_ids=[node_id] + [f"node_{c}" for c in related_companies if c],
                stance=default_stance,
            )
            if evidence is None:
                stats["evidence_skipped"] += 1
                continue
            # 写入 GraphStore(按 evidence_id 去重覆盖)
            if evidence.evidence_id in graph_store.evidence:
                stats["evidence_skipped"] += 1
                continue
            try:
                ledger.add(evidence, operator=operator)
                stats["evidence_added"] += 1
            except ValueError:
                stats["evidence_skipped"] += 1

    # 3. 从 evidence_edges 构造 HyperEdge + 关联 evidence
    for edge in evidence_edges:
        source_node = str(edge.get("source_node", "")).strip()
        target_node = str(edge.get("target_node", "")).strip()
        relation = str(edge.get("relation", "co_occurs_with"))
        if not source_node or not target_node:
            continue
        source_node_id = f"node_{source_node}"
        target_node_id = f"node_{target_node}"
        edge_id = str(edge.get("edge_id", f"edge_{source_node_id}_{relation}_{target_node_id}"))

        # 确保端点 node 存在
        for nid, ntype in [(source_node_id, "CONCEPT"), (target_node_id, "CONCEPT")]:
            if nid not in graph_store.nodes:
                graph_store.add_node(HyperNode(
                    node_id=nid, name=nid.replace("node_", ""),
                    type=ntype, aliases=[],
                    source=NodeSource.SEED_GRAPH.value,
                ), operator=operator)
                stats["nodes_added"] += 1

        # 构造 HyperEdge(若不存在)
        if edge_id not in graph_store.edges:
            hyper_edge = HyperEdge(
                edge_id=edge_id,
                subjects=[source_node_id],
                relation=relation,
                objects=[target_node_id],
                confidence=0.3 if edge.get("confidence_level") == "hypothesis" else 0.5,
                status=EdgeStatus.UNVERIFIED_SEED.value,
            )
            graph_store.add_edge(hyper_edge, operator=operator)
            stats["edges_added"] += 1

        # 为该 edge 的 evidence_documents 创建 Evidence 并 attach
        ev_doc_ids = edge.get("evidence_document_ids", []) or []
        ev_quotes = edge.get("evidence_quotes", []) or []
        for idx, doc_id in enumerate(ev_doc_ids[:5]):
            doc = doc_by_id.get(str(doc_id))
            if not doc:
                continue
            quote = ev_quotes[idx] if idx < len(ev_quotes) else str(doc.get("title", ""))
            evidence = _build_evidence_from_doc(
                doc=doc, quote=quote,
                node_ids=[source_node_id, target_node_id],
                stance=default_stance,
            )
            if evidence is None:
                continue
            if evidence.evidence_id in graph_store.evidence:
                graph_store.attach_evidence(
                    edge_id, evidence.evidence_id, as_counter=False, operator=operator)
                continue
            try:
                ledger.support_edge(edge_id, evidence, operator=operator)
                stats["evidence_added"] += 1
            except ValueError:
                stats["evidence_skipped"] += 1

    logger.info(
        "perilla_adapter: ingested nodes=%d edges=%d evidence=%d skipped=%d",
        stats["nodes_added"], stats["edges_added"],
        stats["evidence_added"], stats["evidence_skipped"],
    )
    return stats


def _build_evidence_from_doc(
    doc: dict[str, Any],
    quote: str,
    node_ids: list[str],
    stance: str,
) -> Evidence | None:
    """从 Perilla raw_document dict 构造 Evidence 对象。

    Args:
        doc: raw_documents 中的 dict,字段含 document_id / source_type / title /
             content_text / url / published_at / related_companies。
        quote: 证据引文(优先用传入的,空则用 title)。
        node_ids: 关联的 node_id 列表,写入 supports(stance=support)或 contradicts。
        stance: support / counter / neutral。

    Returns:
        Evidence 实例,或 None(doc 缺关键字段时)。
    """
    document_id = str(doc.get("document_id", ""))
    if not document_id:
        return None
    quote = (quote or "").strip() or str(doc.get("title", ""))
    if not quote:
        return None

    raw_source_type = str(doc.get("source_type", ""))
    normalized_source_type = _normalize_source_type(raw_source_type)
    title = str(doc.get("title", ""))

    evidence_id = _make_evidence_id(document_id, quote, stance)
    supports = list(node_ids) if stance == "support" else []
    contradicts = list(node_ids) if stance == "counter" else []

    return Evidence(
        evidence_id=evidence_id,
        source_type=normalized_source_type,
        source_title=title,
        source_id=str(doc.get("source_id", "")),
        document_id=document_id,
        source_url=str(doc.get("url", "")),
        publish_date=str(doc.get("published_at", "")),
        quote=quote,
        summary=str(doc.get("summary", ""))[:200],
        evidence_level=EvidenceLevel.SPECULATIVE.value,
        supports=supports,
        contradicts=contradicts,
        extracted_by="perilla_adapter",
        verified_by="rule_verifier",
        metadata={
            "stance": stance,
            "raw_source_type": raw_source_type,
            "related_companies": doc.get("related_companies", []),
            "related_themes": doc.get("related_themes", []),
        },
    )
