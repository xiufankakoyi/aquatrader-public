"""研究证据边界 smoke tests。

覆盖架构审查中的四个高风险点：生成式假设不得入槽、转载不得重复计权、
分层检索不得伪造名称命中、反证必须进入统一评估。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader
from core.research_graph.candidate_graph import CandidateNode
from core.research_graph.evidence_activator import ActivationResult, EvidenceActivator
from core.research_graph.tiered_evidence_searcher import TierConfig, TieredEvidenceSearcher


def test_generated_retrieval_hypothesis_cannot_enter_evidence_slots() -> None:
    activator = EvidenceActivator.__new__(EvidenceActivator)

    result = activator.activate(
        evidence_text="模型推测某公司可能供货 HBM",
        evidence_id="hypothesis-1",
        evidence_kind="retrieval_hypothesis",
    )

    assert result.matched_count == 0
    assert result.matched_node_ids == []


def test_bayesian_upgrader_counts_one_lineage_once() -> None:
    upgrader = BayesianStatusUpgrader()
    base = {
        "stance": "support",
        "quality": 1.5,
        "match_score": 1.0,
        "source_lineage_id": "announcement:001",
    }
    one = upgrader.decide("DISCOVERED", [{**base, "evidence_id": "a"}], "support", 1.5)
    reposts = upgrader.decide(
        "DISCOVERED",
        [
            {**base, "evidence_id": "a", "source_url": "https://first.example/a"},
            {**base, "evidence_id": "b", "source_url": "https://repost.example/b"},
            {**base, "evidence_id": "c", "source_url": "https://repost2.example/c"},
        ],
        "support",
        1.5,
    )

    assert reposts.effective_support == one.effective_support
    assert reposts.sample_size == one.sample_size


@dataclass
class _Document:
    document_id: str
    content_text: str
    url: str
    source_type: str = "search_result"
    title: str = ""
    published_at: str = "2026-07-01"
    metadata: dict = field(default_factory=dict)


class _RecordingActivator:
    def __init__(self, matched_id: str) -> None:
        self.matched_id = matched_id
        self.calls: list[dict] = []

    def activate(self, **kwargs) -> ActivationResult:
        self.calls.append(kwargs)
        # 模拟真实 EvidenceActivator：原文没有候选名时不匹配。
        matched = self.matched_id if "HBM材料" in kwargs["evidence_text"] else ""
        return ActivationResult(matched_node_ids=[matched] if matched else [])


class _Store:
    def __init__(self, node: CandidateNode) -> None:
        self.node = node

    def get_node(self, candidate_id: str) -> CandidateNode | None:
        return self.node if candidate_id == self.node.candidate_id else None


class _Discovery:
    def __init__(self, docs: list[_Document]) -> None:
        self.docs = docs

    def search_many(self, queries: list[str]) -> list[_Document]:
        """匹配真实 MultiEngineSearcher.search_many 契约，返回扁平 list。"""
        return list(self.docs)


def _searcher_for(doc: _Document) -> tuple[TieredEvidenceSearcher, _RecordingActivator, CandidateNode]:
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    searcher = TieredEvidenceSearcher(
        search_discovery=_Discovery([doc]),
        candidate_store=_Store(node),
        evidence_activator=activator,
        tiers=[TierConfig("high", ("official",), ("公告",), "official", max_queries=1)],
        max_total_documents=2,
    )
    return searcher, activator, node


def test_tiered_search_does_not_inject_candidate_name() -> None:
    doc = _Document("d1", "这是一篇与候选主题无关的普通公告。", "https://example.com/a")
    searcher, activator, node = _searcher_for(doc)

    result = searcher.search_for_node(node)

    assert activator.calls[0]["evidence_text"] == doc.content_text
    assert result.documents_activated == 0


def test_tiered_search_routes_explicit_denial_as_counter_evidence() -> None:
    doc = _Document(
        "d2",
        "公司公告澄清：并未向客户供货 HBM材料，相关传闻不属实。",
        "https://example.com/denial",
        metadata={"source_lineage_id": "official-denial-1"},
    )
    searcher, activator, node = _searcher_for(doc)

    searcher.search_for_node(node)

    call = activator.calls[0]
    assert call["stance"] == "counter"
    assert call["source_lineage_id"] == "official-denial-1"
    assert call["published_at"] == "2026-07-01"

