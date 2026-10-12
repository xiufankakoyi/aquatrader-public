"""EvidenceProbe：根据证据缺口选择数据源补证据。

策略：
- 缺供应商证据 → 公告、年报、官网、问财、东财
- 缺技术瓶颈证据 → 研报、白皮书、专利、标准
- 缺市场低关注/估值证据 → 行情、财务、市值、涨幅、研报覆盖、概念热度
- 缺早期信号 → 新闻、展会、会议纪要、互动易

每次补到的新 claim 必须写回 SAG。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    RelationType,
    SourceType,
    StructuralAxis,
)
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.event.event_graph_schema import EventClaim
from core.research.perilla.alpha.path_discovery_schema import PathCandidate, PathEvidenceBundle
from core.research.perilla.alpha.report_graph_builder import ReportGraphBuilder
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase

logger = logging.getLogger(__name__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _score(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(0.0, min(1.0, number))


class EvidenceProbe:
    """证据探测器。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase,
        graph_builder: ReportGraphBuilder | None = None,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库
            graph_builder: ReportGraphBuilder，None 则新建
            entity_resolver: 实体解析器，None 则自动新建
        """
        self.kb = kb
        self.graph_builder = graph_builder or ReportGraphBuilder(kb=kb)
        self.entity_resolver = entity_resolver or EntityResolver()

    def _search_queries(self, gap: dict[str, Any]) -> list[dict[str, Any]]:
        """根据缺口类型生成搜索查询和数据源偏好。"""
        gap = _mapping(gap)
        head = _text(gap.get("head"))
        tail = _text(gap.get("tail"))
        relation = _text(gap.get("relation"))
        missing = _text(gap.get("missing"))

        queries: list[dict[str, Any]] = []

        if missing == "多源证据" or "供应商" in missing:
            # 供应商证据
            for q in [
                f"{tail} 供应商",
                f"{tail} 主要客户",
                f"{head} {tail} 供应关系",
            ]:
                queries.append({
                    "query": q,
                    "source_type": SourceType.WENCAI.value,
                    "source_title": f"问财搜索: {q}",
                })

        if relation in (
            RelationType.BOTTLENECK_OF.value,
            RelationType.CONSTRAINED_BY.value,
            RelationType.IMPORT_DEPENDENT_ON.value,
        ) or "技术" in missing:
            # 技术瓶颈证据
            for q in [
                f"{tail} 技术难点",
                f"{tail} 国产替代 瓶颈",
                f"{tail} 专利 标准",
            ]:
                queries.append({
                    "query": q,
                    "source_type": SourceType.BROKER_REPORT.value,
                    "source_title": f"研报/技术搜索: {q}",
                })

        if "估值" in missing or "市场" in missing:
            # 市场/估值证据
            for q in [
                f"{tail} 市值",
                f"{tail} 估值",
                f"{tail} 研报覆盖",
            ]:
                queries.append({
                    "query": q,
                    "source_type": SourceType.MARKET_DATA.value,
                    "source_title": f"市场数据搜索: {q}",
                })

        if "早期" in missing or "信号" in missing:
            # 早期信号
            for q in [
                f"{tail} 最新进展",
                f"{tail} 互动易",
                f"{tail} 展会",
            ]:
                queries.append({
                    "query": q,
                    "source_type": SourceType.NEWS.value,
                    "source_title": f"新闻搜索: {q}",
                })

        if not queries:
            # 默认兜底
            queries.append({
                "query": f"{head} {tail} {relation}",
                "source_type": SourceType.WEB_SEARCH.value,
                "source_title": f"网页搜索: {head} {tail}",
            })

        return queries

    def _fetch_search_results(self, query: str) -> list[dict[str, Any]]:
        """执行搜索并返回文本结果列表。"""
        from infrastructure.data_providers.provider_registry import ProviderRegistry

        registry = ProviderRegistry()
        results: list[dict[str, Any]] = []

        try:
            resp = _mapping(registry.semantic_search(query=query, count=5))
            source = _text(resp.get("source"), "unknown")
            for item in _items(resp.get("results")):
                if isinstance(item, Mapping):
                    text = f"{_text(item.get('title'))} {_text(item.get('snippet', item.get('body')))}".strip()
                else:
                    text = str(item)
                if text:
                    results.append({
                        "text": text,
                        "source": source,
                        "raw": item,
                    })
        except Exception as e:
            logger.warning("EvidenceProbe 搜索失败 (%s): %s", query, e)

        return results

    def probe_gap(self, gap: dict[str, Any]) -> list[dict[str, Any]]:
        """针对一个证据缺口补证据。

        Args:
            gap: {"head", "tail", "relation", "missing"}

        Returns:
            新增 claim dict 列表
        """
        gap = _mapping(gap)
        head = _text(gap.get("head"))
        tail = _text(gap.get("tail"))
        relation = _text(gap.get("relation"))

        # 跳过泛概念节点，避免浪费查询
        for name in (head, tail):
            try:
                generic = bool(name and self.entity_resolver.is_generic_term(name))
            except (AttributeError, TypeError, ValueError):
                generic = False
            if generic:
                logger.debug("EvidenceProbe 跳过泛概念节点: %s", name)
                return []

        # 若为公司节点但位置非法，优先纠正查询方向
        try:
            position_valid = self.entity_resolver.is_company_position_valid(head, relation, tail)
        except (AttributeError, TypeError, ValueError):
            position_valid = True
        if not position_valid:
            logger.debug("EvidenceProbe 发现公司节点位置非法: %s %s %s", head, relation, tail)

        queries = self._search_queries(gap)
        new_claims: list[dict[str, Any]] = []

        for q in _items(queries)[:2]:  # 每个缺口最多查2个查询
            q = _mapping(q)
            results = self._fetch_search_results(q["query"])
            documents = [
                {
                    "text": r["text"],
                    "source_type": q["source_type"],
                    "source_title": q["source_title"],
                }
                for r in _items(results)[:3]  # 每个查询最多3条结果
                if isinstance(r, Mapping)
            ]
            if documents:
                try:
                    claims = self.graph_builder.extract_claims_from_text(
                        text="\n".join(_text(d.get("text")) for d in documents),
                        source_type=_text(q.get("source_type")),
                        source_title=_text(q.get("source_title")),
                        root_concept=head,
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("EvidenceProbe claim 抽取失败", exc_info=True)
                    claims = []
                for c in _items(claims):
                    c = _mapping(c)
                    if _text(c.get("head")) == head or _text(c.get("tail")) == tail:
                        try:
                            added = self.kb.add_claim(
                                head=c.get("head"), relation=c.get("relation"), tail=c.get("tail"),
                                evidence=c.get("evidence"), source_type=c.get("source_type"),
                                source_title=c.get("source_title"), timestamp=c.get("timestamp"),
                                confidence=c.get("confidence"), head_type=c.get("head_type"),
                                tail_type=c.get("tail_type"),
                                metadata={"structure_axis": c.get("structure_axis")},
                            )
                        except (AttributeError, KeyError, TypeError, ValueError):
                            added = None
                        if added:
                            new_claims.append(dict(c))

        related_event_ids = [_text(event_id) for event_id in _items(gap.get("related_event_claim_ids")) if _text(event_id)]
        if related_event_ids:
            for c in new_claims:
                existing = c.setdefault("related_event_claim_ids", [])
                for event_id in related_event_ids:
                    if event_id not in existing:
                        existing.append(event_id)

        if new_claims:
            if self.kb.save():
                logger.info("EvidenceProbe 为缺口 %s 补充 %d 条 claim", gap, len(new_claims))
            else:
                logger.error("EvidenceProbe 补充 %d 条 claim 后持久化失败: %s", len(new_claims), gap)

        return new_claims

    def probe_node(self, node_name: str, axes: list[str] | None = None) -> list[dict[str, Any]]:
        """针对一个节点沿其结构轴补证据。

        Args:
            node_name: 节点名
            axes: 结构轴列表

        Returns:
            新增 claim dict 列表
        """
        node_name = _text(node_name)
        axes = [_text(axis) for axis in _items(axes) if _text(axis)]

        # 跳过泛概念节点
        try:
            is_generic = bool(self.entity_resolver.is_generic_term(node_name))
        except (AttributeError, TypeError, ValueError):
            is_generic = False
        if is_generic:
            logger.debug("EvidenceProbe 跳过泛概念节点: %s", node_name)
            return []

        try:
            resolved = self.entity_resolver.resolve(node_name)
            is_company = _text(_field(resolved, "entity_type")) == "company"
        except (AttributeError, KeyError, TypeError, ValueError):
            is_company = False

        queries: list[dict[str, Any]] = []

        # 若节点为公司，优先查询供应商/客户关系
        if is_company:
            queries.append({
                "query": f"{node_name} 供应商 客户",
                "source_type": SourceType.WENCAI.value,
                "source_title": f"问财: {node_name} 供应商客户",
            })
            queries.append({
                "query": f"{node_name} 年报 供应商",
                "source_type": SourceType.ANNUAL_REPORT.value,
                "source_title": f"年报: {node_name} 供应商",
            })

        if StructuralAxis.SUPPLIER.value in axes or StructuralAxis.DOMESTIC_SUBSTITUTION.value in axes:
            queries.append({
                "query": f"{node_name} 供应商 国产替代",
                "source_type": SourceType.WENCAI.value,
                "source_title": f"问财: {node_name} 供应商",
            })

        if StructuralAxis.MATERIAL.value in axes:
            queries.append({
                "query": f"{node_name} 材料 上游",
                "source_type": SourceType.BROKER_REPORT.value,
                "source_title": f"研报: {node_name} 材料",
            })

        if StructuralAxis.EQUIPMENT.value in axes:
            queries.append({
                "query": f"{node_name} 设备 厂商",
                "source_type": SourceType.BROKER_REPORT.value,
                "source_title": f"研报: {node_name} 设备",
            })

        if StructuralAxis.PROCESS.value in axes:
            queries.append({
                "query": f"{node_name} 工艺 难点",
                "source_type": SourceType.BROKER_REPORT.value,
                "source_title": f"研报: {node_name} 工艺",
            })

        if StructuralAxis.VALUATION.value in axes:
            queries.append({
                "query": f"{node_name} 估值 市值",
                "source_type": SourceType.MARKET_DATA.value,
                "source_title": f"市场数据: {node_name} 估值",
            })

        if not queries:
            queries.append({
                "query": f"{node_name} 产业链",
                "source_type": SourceType.WEB_SEARCH.value,
                "source_title": f"网页搜索: {node_name}",
            })

        new_claims: list[dict[str, Any]] = []
        for q in _items(queries)[:2]:
            q = _mapping(q)
            results = self._fetch_search_results(q["query"])
            documents = [
                {"text": r.get("text"), "source_type": q.get("source_type"), "source_title": q.get("source_title")}
                for r in _items(results)[:3]
                if isinstance(r, Mapping)
            ]
            if documents:
                try:
                    claims = self.graph_builder.extract_claims_from_text(
                        text="\n".join(_text(d.get("text")) for d in documents),
                        source_type=_text(q.get("source_type")),
                        source_title=_text(q.get("source_title")),
                        root_concept=node_name,
                    )
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("EvidenceProbe 节点 claim 抽取失败", exc_info=True)
                    claims = []
                for c in _items(claims):
                    c = _mapping(c)
                    try:
                        added = self.kb.add_claim(
                            head=c.get("head"), relation=c.get("relation"), tail=c.get("tail"),
                            evidence=c.get("evidence"), source_type=c.get("source_type"),
                            source_title=c.get("source_title"), timestamp=c.get("timestamp"),
                            confidence=c.get("confidence"), head_type=c.get("head_type"),
                            tail_type=c.get("tail_type"),
                            metadata={"structure_axis": c.get("structure_axis")},
                        )
                    except (AttributeError, KeyError, TypeError, ValueError):
                        added = None
                    if added:
                        new_claims.append(dict(c))

        if new_claims:
            if self.kb.save():
                logger.info("EvidenceProbe 为节点 %s 补充 %d 条 claim", node_name, len(new_claims))
            else:
                logger.error("EvidenceProbe 为节点 %s 补充 %d 条 claim 后持久化失败", node_name, len(new_claims))

        return new_claims

    # ------------------------------------------------------------------
    # Event-driven evidence gaps
    # ------------------------------------------------------------------

    @staticmethod
    def _sources_for_event_type(event_type: str) -> list[str]:
        """根据事件类型推荐首选数据源。"""
        mapping: dict[str, list[str]] = {
            "price_increase": ["公告", "年报", "研报", "新闻"],
            "capacity_expansion": ["公告", "年报", "官网", "新闻"],
            "customer_certified": ["公告", "官网", "新闻"],
            "order_disclosed": ["公告", "年报", "官网"],
            "product_launched": ["公告", "官网", "新闻", "展会"],
            "supply_shortage": ["研报", "新闻", "公告"],
            "domestic_substitution_progress": ["研报", "公告", "新闻", "专利"],
            "policy_signal": ["公告", "新闻"],
        }
        return mapping.get(event_type, ["公告", "研报", "新闻"])

    def probe_event_driven_paths(
        self,
        paths: list[dict[str, Any]],
        root: str,
    ) -> list[dict[str, Any]]:
        """探测路径上的事件驱动证据缺口。

        Args:
            paths: 路径数据列表，每项包含 "path"、可选 "edges"、
                   可选 "related_event_claim_ids"。
            root: 根节点名（预留，当前未参与过滤）。

        Returns:
            事件驱动的证据缺口 dict 列表，按 (head, tail, relation) 去重。
        """
        gaps: dict[tuple[str, str, str], dict[str, Any]] = {}

        for path_data in _items(paths):
            path_data = _mapping(path_data)
            path = [_text(node) for node in _items(path_data.get("path")) if _text(node)]
            related_ids: set[str] = {_text(event_id) for event_id in _items(path_data.get("related_event_claim_ids")) if _text(event_id)}

            if not related_ids:
                for edge_key in _items(path_data.get("edges")):
                    try:
                        related_ids.update(
                            _text(_field(c, "event_id")) for c in self.kb.get_event_claims_for_edge(edge_key)
                            if _text(_field(c, "event_id"))
                        )
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue
                for node in path:
                    try:
                        related_ids.update(
                            _text(_field(c, "event_id")) for c in self.kb.get_event_claims_for_node(node)
                            if _text(_field(c, "event_id"))
                        )
                    except (AttributeError, KeyError, TypeError, ValueError):
                        continue

            for event_id in related_ids:
                event_claims = getattr(self.kb, "event_claims", {})
                claim = event_claims.get(event_id) if isinstance(event_claims, dict) else None
                if not isinstance(claim, EventClaim):
                    continue

                key = (claim.subject, claim.object, claim.event_type)
                if key in gaps:
                    if claim.event_id not in gaps[key]["related_event_claim_ids"]:
                        gaps[key]["related_event_claim_ids"].append(claim.event_id)
                    continue

                gaps[key] = {
                    "head": claim.subject,
                    "tail": claim.object,
                    "relation": claim.event_type,
                    "missing": "事件驱动证据",
                    "related_event_claim_ids": [claim.event_id],
                    "recommended_sources": self._sources_for_event_type(claim.event_type),
                    "priority": "high" if claim.confidence > 0.7 else "medium",
                }

        return list(gaps.values())

    # ------------------------------------------------------------------
    # Path-based evidence binding
    # ------------------------------------------------------------------

    def bind_path_evidence(
        self,
        candidate: PathCandidate,
        include_event_claims: bool = False,
    ) -> PathEvidenceBundle:
        """将 PathCandidate 与稳定图 claim、事件 claim、原始证据片段绑定。

        Args:
            candidate: 路径候选。
            include_event_claims: 是否同时绑定事件图 claim（避免循环依赖，默认 False）。

        Returns:
            PathEvidenceBundle。
        """
        stable_claims: list[Any] = []
        event_claims: list[Any] = []
        evidence_chunks: list[dict[str, Any]] = []
        source_summary: dict[str, int] = {}
        missing_evidence: list[dict[str, Any]] = []
        contradiction_signals: list[dict[str, Any]] = []

        if not isinstance(candidate, PathCandidate):
            candidate = PathCandidate(path_id="")
        edge_keys = [
            tuple(_text(part) for part in edge_key)
            for edge_key in _items(candidate.edges)
            if isinstance(edge_key, (list, tuple)) and len(edge_key) == 3
        ]
        covered_edges = 0
        for edge_key in edge_keys:
            head, relation, tail = edge_key
            edge = self.kb.edges.get(edge_key)
            if edge is None:
                missing_evidence.append({
                    "edge_key": list(edge_key),
                    "head": head,
                    "relation": relation,
                    "tail": tail,
                    "reason": "stable_edge_missing",
                    "preferred_sources": self._preferred_sources_for_relation(relation),
                })
                continue

            edge_claims = [self.kb.claims[cid] for cid in _items(edge.claim_ids) if cid in self.kb.claims]
            if not edge_claims:
                missing_evidence.append({
                    "edge_key": list(edge_key),
                    "head": head,
                    "relation": relation,
                    "tail": tail,
                    "reason": "no_claims_on_edge",
                    "preferred_sources": self._preferred_sources_for_relation(relation),
                })
                continue

            covered_edges += 1
            stable_claims.extend(edge_claims)

            for claim in edge_claims:
                evidence_chunks.append({
                    "claim_id": _text(claim.claim_id),
                    "edge_key": list(edge_key),
                    "source_type": _text(claim.source_type),
                    "source_title": _text(claim.source_title),
                    "evidence": _text(claim.evidence),
                    "confidence": _score(claim.confidence),
                })
                source_type = _text(claim.source_type)
                source_summary[source_type] = source_summary.get(source_type, 0) + 1

                # 冲突信号
                if _text(claim.conflict_status, "none") != "none":
                    contradiction_signals.append({
                        "claim_id": claim.claim_id,
                        "edge_key": list(edge_key),
                        "conflict_status": claim.conflict_status,
                    })

            # 多源/可信 claim 检查
            source_types = {_text(c.source_type) for c in edge_claims}
            trusted_count = sum(
                1 for c in edge_claims if _text(c.status) in ("trusted", "supported")
            )
            if len(source_types) < 2:
                missing_evidence.append({
                    "edge_key": list(edge_key),
                    "head": head,
                    "relation": relation,
                    "tail": tail,
                    "reason": "single_source",
                    "preferred_sources": self._preferred_sources_for_relation(relation),
                })
            if trusted_count == 0:
                missing_evidence.append({
                    "edge_key": list(edge_key),
                    "head": head,
                    "relation": relation,
                    "tail": tail,
                    "reason": "no_trusted_or_supported_claim",
                    "preferred_sources": self._preferred_sources_for_relation(relation),
                })

        # 弱边：边置信度低于 0.5
        weak_edges: list[tuple[str, str, str]] = []
        for edge_key in edge_keys:
            edge = self.kb.edges.get(edge_key)
            if edge and _score(edge.confidence) < 0.5:
                weak_edges.append(edge_key)
                missing_evidence.append({
                    "edge_key": list(edge_key),
                    "head": edge_key[0],
                    "relation": edge_key[1],
                    "tail": edge_key[2],
                    "reason": "low_confidence_edge",
                    "preferred_sources": self._preferred_sources_for_relation(edge_key[1]),
                })

        coverage_score = covered_edges / max(1, len(edge_keys))

        return PathEvidenceBundle(
            path_candidate=candidate,
            stable_claims=stable_claims,
            event_claims=event_claims,
            evidence_chunks=evidence_chunks,
            source_summary=source_summary,
            missing_evidence=missing_evidence,
            contradiction_signals=contradiction_signals,
            coverage_score=round(coverage_score, 3),
        )

    @staticmethod
    def _preferred_sources_for_relation(relation: str) -> list[str]:
        """根据关系类型推荐首选数据源。"""
        if relation in ("supplied_by", "import_dependent_on"):
            return ["annual_report", "announcement", "official_website", "wencai"]
        if relation in ("bottleneck_of", "constrained_by"):
            return ["broker_report", "industry_whitepaper", "patent", "standard"]
        if relation == "certified_by":
            return ["announcement", "official_website", "standard"]
        if relation in ("made_of", "contains"):
            return ["broker_report", "industry_whitepaper", "patent"]
        if relation == "substitute_for":
            return ["broker_report", "news", "patent"]
        return ["broker_report", "web_search"]
