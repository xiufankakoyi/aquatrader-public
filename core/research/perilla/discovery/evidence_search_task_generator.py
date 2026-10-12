"""Generate evidence search tasks for Perilla concept scouting."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Set


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


SAFE_EVIDENCE_LEVELS = {
    "HYPOTHESIS",
    "RUMOR_ONLY",
    "EARLY_SIGNAL",
    "CONCEPT_CLAIM_ONLY",
}

CHAIN_TEMPLATE_NODES: tuple[str, ...] = (
    "\u4e0a\u6e38\u6750\u6599",
    "\u6838\u5fc3\u5668\u4ef6",
    "\u8bbe\u5907",
    "\u7cfb\u7edf\u96c6\u6210",
    "\u5ba2\u6237",
    "\u74f6\u9888",
    "\u66ff\u4ee3\u8def\u7ebf",
    "\u964d\u672c\u8def\u7ebf",
    "A\u80a1\u6620\u5c04\u516c\u53f8",
)

NON_FINANCIAL_SOURCE_TYPES: tuple[str, ...] = (
    "\u62db\u8058",
    "\u4e13\u5229",
    "\u62db\u6295\u6807",
    "\u653f\u5e9c\u516c\u793a",
    "\u73af\u8bc4",
    "\u5c55\u4f1a",
    "\u4ea7\u54c1\u9875",
    "\u4f9b\u5e94\u5546\u540d\u5355",
    "\u5ba2\u6237\u8ba4\u8bc1",
    "\u6807\u51c6\u6587\u4ef6",
)

AQUATRADE_CAPABILITY_BY_SOURCE_TYPE: dict[str, str] = {
    "\u65b0\u95fb": "news",
    "\u7814\u62a5": "reports",
    "\u516c\u544a": "announcements",
    "\u8463\u79d8\u4e92\u52a8": "investor_interactions",
    "\u4e92\u52a8\u5e73\u53f0": "investor_interactions",
    "\u7efc\u5408\u68c0\u7d22": "semantic_search",
    "\u62db\u8058": "semantic_search",
    "\u4e13\u5229": "semantic_search",
    "\u62db\u6295\u6807": "semantic_search",
    "\u653f\u5e9c\u516c\u793a": "semantic_search",
    "\u73af\u8bc4": "semantic_search",
    "\u5c55\u4f1a": "semantic_search",
    "\u4ea7\u54c1\u9875": "semantic_search",
    "\u4f9b\u5e94\u5546\u540d\u5355": "semantic_search",
    "\u5ba2\u6237\u8ba4\u8bc1": "semantic_search",
    "\u6807\u51c6\u6587\u4ef6": "semantic_search",
}

# source_type → 质量阶梯，用于任务分批调度
SOURCE_TYPE_TO_TIER: dict[str, str] = {
    "公告": "high",
    "董秘互动": "high",
    "互动平台": "high",
    "研报": "high",
    "政府公示": "high",
    "标准文件": "high",
    "客户认证": "high",
    "招投标": "medium",
    "招聘": "medium",
    "专利": "medium",
    "环评": "medium",
    "产品页": "medium",
    "供应商名单": "medium",
    "新闻": "medium",
    "综合检索": "low",
    "展会": "low",
}

TIER_ORDER: tuple[str, ...] = ("high", "medium", "low")

NODE_QUERY_SUFFIXES: dict[str, tuple[str, ...]] = {
    "\u4e0a\u6e38\u6750\u6599": ("\u6750\u6599", "\u4e0a\u6e38", "\u539f\u6750\u6599"),
    "\u6838\u5fc3\u5668\u4ef6": ("\u6838\u5fc3\u5668\u4ef6", "\u82af\u7247", "\u6a21\u5757"),
    "\u8bbe\u5907": ("\u8bbe\u5907", "\u4ea7\u7ebf", "\u5de5\u827a"),
    "\u7cfb\u7edf\u96c6\u6210": ("\u7cfb\u7edf\u96c6\u6210", "\u89e3\u51b3\u65b9\u6848"),
    "\u5ba2\u6237": ("\u5ba2\u6237", "\u8ba4\u8bc1", "\u5bfc\u5165"),
    "\u74f6\u9888": ("\u74f6\u9888", "\u96be\u70b9", "\u4ea7\u80fd"),
    "\u66ff\u4ee3\u8def\u7ebf": ("\u66ff\u4ee3\u8def\u7ebf", "\u5bf9\u6bd4", "\u533a\u522b"),
    "\u964d\u672c\u8def\u7ebf": ("\u964d\u672c", "\u4f4e\u529f\u8017", "\u91cf\u4ea7"),
    "A\u80a1\u6620\u5c04\u516c\u53f8": ("A\u80a1", "\u4e0a\u5e02\u516c\u53f8", "\u4f9b\u5e94\u94fe"),
}

CONCEPT_SPECIFIC_QUERIES: dict[str, tuple[str, ...]] = {
    "CPO": (
        "CPO LPO NPO \u533a\u522b",
        "CPO \u66ff\u4ee3\u8def\u7ebf",
        "LPO \u5149\u6a21\u5757 \u4f4e\u529f\u8017",
        "NPO \u4ea4\u6362\u673a \u5149\u4e92\u8fde",
    ),
    "LPO": (
        "LPO \u5149\u6a21\u5757 \u4f4e\u529f\u8017",
        "LPO CPO \u5bf9\u6bd4",
    ),
    "NPO": (
        "NPO \u4ea4\u6362\u673a \u5149\u4e92\u8fde",
        "NPO CPO \u533a\u522b",
    ),
    "\u6570\u636e\u4e2d\u5fc3800V\u4f9b\u7535": (
        "\u6570\u636e\u4e2d\u5fc3 800V HVDC \u4f9b\u7535",
        "\u670d\u52a1\u5668 800V \u7535\u6e90 PDU",
    ),
}


class EvidenceSearchTaskGenerator:
    """Generate a first-stage task queue; it does not crawl the web yet."""

    def generate_tasks(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        research_questions: Iterable[Dict[str, Any]] | None = None,
        market_anomalies: Iterable[Dict[str, Any]] | None = None,
        min_tasks: int = 20,
    ) -> List[Dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        concepts = [item for item in (discovered_concepts or []) if isinstance(item, Mapping)]
        questions = [item for item in (research_questions or []) if isinstance(item, Mapping)]
        anomalies = [item for item in (market_anomalies or []) if isinstance(item, Mapping)]
        min_tasks = max(0, int(_finite(min_tasks)))
        for concept in concepts:
            tasks.extend(self._tasks_for_concept(concept, seen))

        for question in questions:
            for query in question.get("search_queries", []) or []:
                self._append_task(
                    tasks,
                    seen,
                    query=str(query),
                    root_theme=str(question.get("root_theme") or ""),
                    concept=self._first(question.get("related_concepts")),
                    company=self._first(question.get("related_companies")),
                    chain_node="\u5f85\u9a8c\u8bc1\u95ee\u9898",
                    evidence_source_type="\u7efc\u5408\u68c0\u7d22",
                    purpose="\u9a8c\u8bc1\u7814\u7a76\u95ee\u9898",
                    priority_score=_finite(question.get("priority_score")),
                )

        for anomaly in anomalies:
            tasks.extend(self._tasks_for_market_anomaly(anomaly, seen))

        if len(tasks) < min_tasks:
            self._pad_tasks(tasks, seen, concepts, min_tasks)

        tasks.sort(key=lambda item: item["priority_score"], reverse=True)
        return tasks

    def generate_research_tasks(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        research_questions: Iterable[Dict[str, Any]] | None = None,
        market_anomalies: Iterable[Dict[str, Any]] | None = None,
        min_tasks: int = 20,
    ) -> list[Any]:
        """兼容生成统一 ResearchTask；旧字典接口继续可用。"""
        from core.research.control_plane.contracts import ResearchTask

        legacy = self.generate_tasks(
            discovered_concepts,
            research_questions=research_questions,
            market_anomalies=market_anomalies,
            min_tasks=min_tasks,
        )
        return [
            ResearchTask(
                task_id=str(item.get("task_id") or ""),
                task_type="evidence_completion",
                trigger_source="perilla_discovery",
                priority=float(item.get("priority_score") or 0.0),
                params={
                    **item,
                    "retrieval_hypothesis": item.get("query", ""),
                    "evidence_kind": "retrieval_hypothesis",
                },
            )
            for item in legacy
        ]

    def generate_tiered_tasks(
        self,
        discovered_concepts: Iterable[Dict[str, Any]],
        research_questions: Iterable[Dict[str, Any]] | None = None,
        market_anomalies: Iterable[Dict[str, Any]] | None = None,
        min_tasks: int = 20,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """生成按质量阶梯分组的搜索任务。

        返回字典：{"high": [...], "medium": [...], "low": [...]}。
        调度方应先执行 high tier，再根据验证结果决定是否执行 medium/low。
        """
        tasks = self.generate_tasks(
            discovered_concepts,
            research_questions=research_questions,
            market_anomalies=market_anomalies,
            min_tasks=min_tasks,
        )
        return self.group_tasks_by_tier(tasks)

    @staticmethod
    def group_tasks_by_tier(tasks: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """把已有任务按证据来源质量阶梯分组。"""
        grouped: Dict[str, List[Dict[str, Any]]] = {
            tier: [] for tier in TIER_ORDER
        }
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, Mapping):
                continue
            source_type = task.get("evidence_source_type", "")
            tier = SOURCE_TYPE_TO_TIER.get(source_type, "low")
            task["search_tier"] = tier
            grouped[tier].append(task)
        return grouped

    def _tasks_for_concept(
        self,
        concept: Dict[str, Any],
        seen: Set[str],
    ) -> List[Dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        if not isinstance(concept, Mapping):
            return tasks
        name = str(concept.get("normalized_name") or concept.get("concept_name") or "").strip()
        if not name:
            return tasks
        root_theme = str(concept.get("root_theme") or "")
        source_nodes = self._known_nodes(concept)
        missing_nodes = [node for node in CHAIN_TEMPLATE_NODES if node not in source_nodes]
        priority = _finite(concept.get("concept_score"))

        for query in CONCEPT_SPECIFIC_QUERIES.get(name, ()):
            self._append_task(
                tasks,
                seen,
                query=query,
                root_theme=root_theme,
                concept=name,
                company="",
                chain_node="\u66ff\u4ee3\u8def\u7ebf",
                evidence_source_type="\u7efc\u5408\u68c0\u7d22",
                purpose="\u6982\u5ff5\u8def\u7ebf\u5bf9\u6bd4",
                priority_score=min(priority + 0.1, 1.0),
            )

        for node in missing_nodes:
            for suffix in NODE_QUERY_SUFFIXES.get(node, (node,))[:2]:
                self._append_task(
                    tasks,
                    seen,
                    query=f"{name} {suffix}",
                    root_theme=root_theme,
                    concept=name,
                    company="",
                    chain_node=node,
                    evidence_source_type="\u7efc\u5408\u68c0\u7d22",
                    purpose="\u8865\u5168\u4ea7\u4e1a\u94fe\u7a7a\u767d\u8282\u70b9",
                    priority_score=priority,
                )

        related_companies = concept.get("related_companies", [])
        related_companies = related_companies if isinstance(related_companies, (list, tuple)) else []
        for company in related_companies[:5]:
            company_name = _text(company).strip()
            if not company_name:
                continue
            for suffix in ("\u62db\u8058", "\u4e13\u5229", "\u5ba2\u6237\u8ba4\u8bc1"):
                self._append_task(
                    tasks,
                    seen,
                    query=f"{company_name} {name} {suffix}",
                    root_theme=root_theme,
                    concept=name,
                    company=company_name,
                    chain_node="A\u80a1\u6620\u5c04\u516c\u53f8",
                    evidence_source_type=suffix,
                    purpose="\u67e5\u627e\u516c\u53f8-\u6982\u5ff5\u5f31\u4fe1\u53f7",
                    priority_score=max(priority - 0.05, 0.0),
                )

        for source_type in NON_FINANCIAL_SOURCE_TYPES:
            self._append_task(
                tasks,
                seen,
                query=f"{name} {source_type}",
                root_theme=root_theme,
                concept=name,
                company="",
                chain_node="\u8bc1\u636e\u6269\u5c55",
                evidence_source_type=source_type,
                purpose="\u975e\u8d22\u7ecf\u516c\u5f00\u6e90\u68c0\u7d22",
                priority_score=max(priority - 0.1, 0.0),
            )
        return tasks

    def _tasks_for_market_anomaly(
        self,
        anomaly: Dict[str, Any],
        seen: Set[str],
    ) -> List[Dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        if not isinstance(anomaly, Mapping):
            return tasks
        company = str(anomaly.get("company_name") or anomaly.get("symbol") or "").strip()
        concept = str(anomaly.get("concept") or anomaly.get("chain_node") or "").strip()
        root_theme = str(anomaly.get("root_theme") or "")
        if company:
            self._append_task(
                tasks,
                seen,
                query=f"{company} \u5f02\u52a8 \u539f\u56e0",
                root_theme=root_theme,
                concept=concept,
                company=company,
                chain_node="\u5e02\u573a\u5f02\u52a8",
                evidence_source_type="\u65b0\u95fb",
                purpose="\u5e02\u573a\u5f02\u52a8\u53cd\u5411\u641c\u7d22",
                priority_score=_finite(anomaly.get("anomaly_score"), 0.6),
            )
        if concept:
            for suffix in ("\u4ea7\u4e1a\u94fe \u50ac\u5316", "\u6280\u672f \u65b0\u8fdb\u5c55"):
                self._append_task(
                    tasks,
                    seen,
                    query=f"{concept} {suffix}",
                    root_theme=root_theme,
                    concept=concept,
                    company=company,
                    chain_node="\u5e02\u573a\u5f02\u52a8",
                    evidence_source_type="\u65b0\u95fb",
                    purpose="\u5e02\u573a\u5f02\u52a8\u53cd\u5411\u641c\u7d22",
                    priority_score=_finite(anomaly.get("anomaly_score"), 0.6),
                )
        return tasks

    def _append_task(
        self,
        tasks: List[Dict[str, Any]],
        seen: Set[str],
        query: str,
        root_theme: str,
        concept: str,
        company: str,
        chain_node: str,
        evidence_source_type: str,
        purpose: str,
        priority_score: float,
    ) -> None:
        query = " ".join(str(query).split())
        if not query or query in seen:
            return
        seen.add(query)
        capability = AQUATRADE_CAPABILITY_BY_SOURCE_TYPE.get(
            evidence_source_type,
            "semantic_search",
        )
        tasks.append(
            {
                "task_id": f"est_{_short_hash(query)}",
                "query": query,
                "execution_backend": "AquaTradeResearchEngine",
                "aquatrade_capability": capability,
                "aquatrade_cli": self._cli_command(capability, query),
                "provider_policy": "AQUATRADE_ONLY",
                "direct_web_allowed": False,
                "root_theme": root_theme,
                "related_concept": concept,
                "related_company": company,
                "chain_node": chain_node,
                "evidence_source_type": evidence_source_type,
                "purpose": purpose,
                "priority_score": round(min(max(_finite(priority_score), 0.0), 1.0), 4),
                "status": "pending",
                "created_at": datetime.now().isoformat(),
                "evidence_level": "HYPOTHESIS",
            }
        )

    @staticmethod
    def _cli_command(capability: str, query: str) -> List[str]:
        command_by_capability = {
            "news": "news",
            "reports": "report",
            "announcements": "announce",
            "semantic_search": "search",
        }
        command = command_by_capability.get(capability, "search")
        return [
            "python",
            "scripts/research_data.py",
            command,
            "--query",
            query,
        ]

    @staticmethod
    def _known_nodes(concept: Dict[str, Any]) -> Set[str]:
        nodes: set[str] = set()
        sources = concept.get("source_items", []) if isinstance(concept, Mapping) else []
        for source in sources if isinstance(sources, (list, tuple)) else []:
            if not isinstance(source, Mapping):
                continue
            node = _text(source.get("chain_node")).strip()
            if node:
                nodes.add(node)
        return nodes

    @staticmethod
    def _first(values: Any) -> str:
        if isinstance(values, (list, tuple)) and values:
            return _text(values[0])
        return _text(values)

    def _pad_tasks(
        self,
        tasks: List[Dict[str, Any]],
        seen: Set[str],
        discovered_concepts: Iterable[Dict[str, Any]],
        min_tasks: int,
    ) -> None:
        concepts = [
            concept for concept in (discovered_concepts or [])
            if isinstance(concept, Mapping)
        ]
        if not concepts:
            return
        idx = 0
        while len(tasks) < min_tasks and idx < min_tasks * 3:
            concept = concepts[idx % len(concepts)]
            name = _text(concept.get("normalized_name")) or _text(concept.get("concept_name"))
            root_theme = _text(concept.get("root_theme"))
            source_type = NON_FINANCIAL_SOURCE_TYPES[idx % len(NON_FINANCIAL_SOURCE_TYPES)]
            self._append_task(
                tasks,
                seen,
                query=f"{name} {source_type} \u8bc1\u636e",
                root_theme=root_theme,
                concept=name,
                company="",
                chain_node="\u8bc1\u636e\u6269\u5c55",
                evidence_source_type=source_type,
                purpose="\u4fdd\u5e95\u751f\u6210\u641c\u7d22\u4efb\u52a1",
                priority_score=float(concept.get("concept_score") or 0.4),
            )
            idx += 1


def _short_hash(text: str) -> str:
    return hashlib.sha1(_text(text).encode("utf-8")).hexdigest()[:12]
