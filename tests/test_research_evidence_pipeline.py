"""AquaResearch 事实与证据主流程集成测试。

覆盖：
1. BayesianStatusUpgrader 边界行为与来源质量单一计权
2. ResearchOrchestrator 统一调度、缓存、停止条件与状态迁移审计
3. SearchCache 幂等与版本隔离
4. 伪证据/检索假设的阻断与索引增量性能
5. Point-in-time 证据可见性与有效期
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.research.control_plane.contracts import (
    Claim, ClaimStatus, EvidenceRecord, ResearchTask, TaskBudget, TaskStatus,
)
from core.research.control_plane.orchestrator import ResearchOrchestrator, SearchTier
from core.research.control_plane.policies import ClaimPolicy, evidence_is_current, evidence_visible_as_of
from core.research.control_plane.adapters import adapt_candidate
from core.research.control_plane.repository import ResearchRepository
from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader
from core.research_graph.candidate_graph import CandidateNode, CandidateStore
from core.research_graph.candidate_schema import CandidateStatus
from core.research_graph.evidence_activator import EvidenceActivator
from core.research_graph.pseudo_evidence_index import PseudoEvidenceIndex
from core.research_graph.retrieval_hypothesis import BLOCKED_EVIDENCE_KINDS, is_blocked_evidence_kind
from core.research_graph.search_cache import SearchCache

try:
    import sentence_transformers  # noqa: F401
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    _HAS_SENTENCE_TRANSFORMERS = False

_requires_sentence_transformers = pytest.mark.skipif(
    not _HAS_SENTENCE_TRANSFORMERS,
    reason="sentence_transformers 未安装；PseudoEvidenceIndex 为孤立组件，真实流水线使用 KeywordMatchIndex 不依赖它",
)


@pytest.fixture
def repo(tmp_path: Path) -> ResearchRepository:
    """返回基于临时目录的研究仓库。"""
    return ResearchRepository(tmp_path / "research.db")


# ---------------------------------------------------------------------------
# BayesianStatusUpgrader 边界测试
# ---------------------------------------------------------------------------

def test_blocked_evidence_kinds_are_hardcoded() -> None:
    """被禁止的生成式假设类型必须全局一致且不可配置。"""
    for kind in ("pseudo_evidence", "retrieval_hypothesis", "query_prototype", "generated"):
        assert is_blocked_evidence_kind(kind)
        assert kind in BLOCKED_EVIDENCE_KINDS


def test_blocked_evidence_kind_does_not_affect_posterior() -> None:
    """检索假设不应影响 Beta 后验，也不得触发升级。"""
    upgrader = BayesianStatusUpgrader()
    support = {"stance": "support", "quality": 1.5, "match_score": 1.0, "evidence_id": "real"}
    hypothesis = {
        "stance": "support", "quality": 1.5, "match_score": 1.0,
        "evidence_id": "hyp", "evidence_kind": "retrieval_hypothesis",
    }
    real_only = upgrader.decide("DISCOVERED", [support], "support", 1.5)
    with_hypothesis = upgrader.decide("DISCOVERED", [support, hypothesis], "support", 1.5)
    assert real_only.effective_support == with_hypothesis.effective_support
    assert real_only.new_status == with_hypothesis.new_status


def test_source_quality_is_weighted_once_in_slot_weight() -> None:
    """_slot_weight 中 source_discount 是 quality 的唯一入口，不得重复乘 quality。"""
    upgrader = BayesianStatusUpgrader(source_discount_kappa=0.5)
    slot = {"stance": "support", "quality": 1.5, "match_score": 1.0, "evidence_id": "a"}
    weight = upgrader._slot_weight(slot)
    # source_discount = 1.5 / (1.5 + 0.5) = 0.75；不应再额外乘 1.5
    expected = 0.75 * 1.0
    assert abs(weight - expected) < 1e-6


def test_repost_lineage_collapses_to_one_observation() -> None:
    """同一来源的转载/重复材料在贝叶斯更新中只计一次。"""
    upgrader = BayesianStatusUpgrader()
    base = {"stance": "support", "quality": 1.5, "match_score": 1.0, "source_lineage_id": "lineage-x"}
    single = upgrader.decide("DISCOVERED", [{**base, "evidence_id": "a"}], "support", 1.5)
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
    assert reposts.effective_support == single.effective_support
    assert reposts.sample_size == single.sample_size


def test_counter_evidence_penalizes_posterior() -> None:
    """反证应增加 beta，降低后验均值。"""
    upgrader = BayesianStatusUpgrader()
    support = {"stance": "support", "quality": 1.5, "match_score": 1.0, "evidence_id": "s1"}
    counter = {"stance": "counter", "quality": 1.5, "match_score": 1.0, "evidence_id": "c1"}
    only_support = upgrader.decide("CANDIDATE", [support], "support", 1.5)
    with_counter = upgrader.decide("CANDIDATE", [support, counter], "counter", 1.5)
    assert with_counter.posterior_mean < only_support.posterior_mean


def test_strong_counter_demotes_to_rejected() -> None:
    """强反证（counter≥3 且 support=0）必须降级为 REJECTED。"""
    upgrader = BayesianStatusUpgrader()
    counters = [
        {"stance": "counter", "quality": 1.0, "match_score": 1.0, "evidence_id": f"c{i}"}
        for i in range(3)
    ]
    result = upgrader.decide("CANDIDATE", counters, "counter", 1.0)
    assert result.new_status == CandidateStatus.REJECTED.value


# ---------------------------------------------------------------------------
# SearchCache 幂等与版本隔离测试
# ---------------------------------------------------------------------------

def test_adapt_candidate_preserves_importance_and_terms() -> None:
    """adapt_candidate 应把候选节点的 importance/support_terms/counter_terms 写入 Claim。"""
    node = CandidateNode(
        candidate_id="cn_x",
        name="HBM材料",
        node_type="MATERIAL",
        attributes={
            "importance": "high",
            "support_terms": ["HBM", "材料"],
            "counter_terms": ["否认", "澄清"],
        },
    )
    claim = adapt_candidate(node)
    assert claim.attributes.get("importance") == "high"
    assert claim.support_terms == ["HBM", "材料"]
    assert claim.counter_terms == ["否认", "澄清"]


def test_search_cache_is_idempotent_under_same_key() -> None:
    """相同候选、层级、查询、版本应只标记一次搜索。"""
    cache = SearchCache(cache_dir=None, ttl_days=1, strategy_version="v1")
    cache.mark_tier_searched("c1", "high", "query-abc", content_version="v2")
    assert cache.is_tier_searched("c1", "high", "query-abc", content_version="v2")
    # 不同版本应视为未搜索
    assert not cache.is_tier_searched("c1", "high", "query-abc", content_version="v3")
    # 不同 scope 应隔离
    assert not cache.is_tier_searched("c1", "high", "query-abc", scope="other", content_version="v2")


def test_search_cache_persists_and_reloads_idempotently(tmp_path: Path) -> None:
    """缓存持久化后重新加载应保持幂等，且不丢失版本信息。"""
    cache_dir = str(tmp_path / "cache")
    cache = SearchCache(cache_dir=cache_dir, ttl_days=1, strategy_version="v1")
    cache.mark_tier_searched("c1", "medium", "q", content_version="v2")

    cache2 = SearchCache(cache_dir=cache_dir, ttl_days=1, strategy_version="v1")
    assert cache2.is_tier_searched("c1", "medium", "q", content_version="v2")
    # 重新标记不应产生重复条目
    cache2.mark_tier_searched("c1", "medium", "q", content_version="v2")
    assert len(cache2._memory) == 1


def test_search_cache_strategy_version_isolates_results() -> None:
    """不同 strategy_version 必须产生不同的缓存键。"""
    cache_v1 = SearchCache(strategy_version="v1")
    cache_v2 = SearchCache(strategy_version="v2")
    cache_v1.mark_tier_searched("c1", "high", "q")
    assert cache_v1.is_tier_searched("c1", "high", "q")
    assert not cache_v2.is_tier_searched("c1", "high", "q")


def test_search_cache_ttl_hours_expires_faster() -> None:
    """ttl_hours 应使缓存更快过期。"""
    cache = SearchCache(ttl_hours=1)
    cache.mark_tier_searched("c1", "high", "q")
    assert cache.is_tier_searched("c1", "high", "q")
    # 把 cached_at 改成 2 小时前，模拟过期
    key = cache._make_key("c1", "high", "q")
    cache._memory[key]["cached_at"] = (
        datetime.now() - timedelta(hours=2)
    ).isoformat()
    assert not cache.is_tier_searched("c1", "high", "q")


def test_search_cache_ttl_hours_takes_precedence_over_days() -> None:
    """显式传入 ttl_hours 时，应覆盖 ttl_days。"""
    cache = SearchCache(ttl_days=7, ttl_hours=2)
    assert cache.ttl_hours == 2
    assert cache.snapshot()["ttl_hours"] == 2


# ---------------------------------------------------------------------------
# Point-in-time 测试
# ---------------------------------------------------------------------------

def test_future_evidence_blocked_in_historical_mode() -> None:
    """历史模式下，未来证据不可见。"""
    future = EvidenceRecord(
        published_at="2030-01-01T00:00:00+00:00",
        retrieved_at="2030-01-01T00:00:00+00:00",
        stance="support",
        quote="quote",
        grounding_status="exact",
    )
    assert not evidence_visible_as_of(future, "2029-01-01T00:00:00+00:00", historical=True)


def test_missing_pit_is_blocked_in_historical_mode_but_allowed_in_realtime() -> None:
    """缺少 PIT 字段的证据在历史模式应被排除，实时模式允许但需 UI 标记。"""
    missing = EvidenceRecord(published_at="", retrieved_at="", stance="support", quote="quote", grounding_status="exact")
    assert not evidence_visible_as_of(missing, "2029-01-01T00:00:00+00:00", historical=True)
    assert evidence_visible_as_of(missing, "2029-01-01T00:00:00+00:00", historical=False)


def test_expired_evidence_is_not_current() -> None:
    """valid_to 早于 as_of 的证据应判定为过期。"""
    expired = EvidenceRecord(
        published_at="2020-01-01T00:00:00+00:00",
        retrieved_at="2020-01-01T00:00:00+00:00",
        valid_from="2020-01-01T00:00:00+00:00",
        valid_to="2021-01-01T00:00:00+00:00",
        stance="support",
        quote="quote",
        grounding_status="exact",
    )
    assert not evidence_is_current(expired, "2022-01-01T00:00:00+00:00")


def test_claim_policy_respects_as_of_cutoff() -> None:
    """ClaimPolicy.evaluate 的 as_of 参数应过滤未来证据。"""
    claim = Claim(claim_id="c1", counter_search_completed=True)
    past = EvidenceRecord(
        claim_id="c1", lineage_id="l1", stance="support", quote="q",
        source_quality=1.2, match_score=0.9, grounding_status="exact",
        published_at="2024-01-01T00:00:00+00:00", retrieved_at="2024-01-02T00:00:00+00:00",
        metadata={"slots": ["identity", "relationship"]},
    )
    future = EvidenceRecord(
        claim_id="c1", lineage_id="l2", stance="support", quote="q",
        source_quality=1.2, match_score=0.9, grounding_status="exact",
        published_at="2026-01-01T00:00:00+00:00", retrieved_at="2026-01-02T00:00:00+00:00",
        metadata={"slots": ["identity", "relationship"]},
    )
    result = ClaimPolicy().evaluate(claim, [past, future], as_of="2025-01-01T00:00:00+00:00", historical=True)
    assert result.independent_sources == 1
    assert result.target_status == ClaimStatus.CANDIDATE.value


# ---------------------------------------------------------------------------
# 检索假设索引增量性能测试
# ---------------------------------------------------------------------------

@_requires_sentence_transformers
def test_pseudo_evidence_index_incremental_encoding(tmp_path: Path) -> None:
    """重复 add_nodes 不应重复编码已有文本。"""
    index = PseudoEvidenceIndex()
    node = CandidateNode(
        candidate_id="cn_a",
        name="TSV应用场景",
        node_type="APPLICATION",
        attributes={"retrieval_hypotheses": [
            {"query_prototype": "硅通孔技术广泛应用于三维堆叠存储器封装中。", "evidence_kind": "retrieval_hypothesis"},
            {"query_prototype": "高带宽内存通过垂直导电微孔实现芯片层间互连。", "evidence_kind": "retrieval_hypothesis"},
        ]},
    )

    added1 = index.add_node(node)
    stats1 = index.stats()
    added2 = index.add_node(node)
    stats2 = index.stats()

    assert added1 == 2
    assert added2 == 0
    assert stats1["entries"] == stats2["entries"]
    assert stats1["unique_text_hashes"] == stats2["unique_text_hashes"]


@_requires_sentence_transformers
def test_pseudo_evidence_index_content_hash_dedupes_across_nodes() -> None:
    """不同节点若检索假设文本相同，应共享 embedding 缓存。"""
    index = PseudoEvidenceIndex()
    shared_text = "硅通孔技术广泛应用于先进封装。"
    node_a = CandidateNode(
        candidate_id="cn_a", name="A", node_type="THEME",
        attributes={"retrieval_hypotheses": [{"query_prototype": shared_text, "evidence_kind": "retrieval_hypothesis"}]},
    )
    node_b = CandidateNode(
        candidate_id="cn_b", name="B", node_type="THEME",
        attributes={"retrieval_hypotheses": [{"query_prototype": shared_text, "evidence_kind": "retrieval_hypothesis"}]},
    )
    index.add_nodes([node_a, node_b])
    stats = index.stats()
    assert stats["entries"] == 2
    assert stats["unique_text_hashes"] == 1


def test_pseudo_evidence_index_is_empty_before_search() -> None:
    """未添加节点时，搜索应返回空结果且不触发模型加载。"""
    index = PseudoEvidenceIndex()
    assert index.is_empty()
    assert index.search("任意查询") == []
    assert not index.stats()["model_loaded"]


def test_pseudo_evidence_index_degrades_without_sentence_transformers() -> None:
    """无 sentence_transformers 时，PseudoEvidenceIndex 应明确降级而非崩溃。

    降级路径：
    - add_node 不编码向量，只记录条目元数据
    - search 返回空列表（无法做语义匹配）
    - stats 标记 model_loaded=False
    - 不抛异常
    """
    index = PseudoEvidenceIndex()
    node = CandidateNode(
        candidate_id="cn_degrade",
        name="测试节点",
        node_type="UNKNOWN",
        attributes={"retrieval_hypotheses": ["检索假设文本"]},
    )

    if _HAS_SENTENCE_TRANSFORMERS:
        # 有依赖时正常编码，此测试验证的是无依赖降级路径，
        # 在有依赖的环境中只验证接口不崩溃。
        index.add_node(node)
        assert not index.is_empty()
        return

    # 无依赖时的降级路径
    index.add_node(node)
    # 不应崩溃，但 search 返回空（无法做语义匹配）
    results = index.search("检索假设文本", top_k=1, score_threshold=0.3)
    assert results == []
    assert not index.stats()["model_loaded"]


@_requires_sentence_transformers
def test_pseudo_evidence_index_legacy_field_readonly(tmp_path: Path) -> None:
    """旧 pseudo_evidence 字段应只读，索引仍可工作。"""
    index = PseudoEvidenceIndex()
    node = CandidateNode(
        candidate_id="cn_legacy",
        name="Legacy",
        node_type="UNKNOWN",
        attributes={"pseudo_evidence": ["旧字段检索假设片段。"]},
    )
    index.add_node(node)
    results = index.search("旧字段检索假设片段", top_k=1, score_threshold=0.3)
    assert any(cid == "cn_legacy" for cid, _ in results)


# ---------------------------------------------------------------------------
# Orchestrator 集成测试
# ---------------------------------------------------------------------------

@dataclass
class _Doc:
    text: str
    tier: str = "high"


class _InMemoryRetriever:
    def __init__(self, docs: list[_Doc]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, str]] = []

    def search(self, query: str, *, tier: str, limit: int) -> list[dict[str, str]]:
        self.calls.append((query, tier))
        return [{"text": d.text} for d in self.docs if d.tier == tier][:limit]


class _StaticExtractor:
    def __init__(self, evidence_per_doc: int = 1) -> None:
        self.evidence_per_doc = evidence_per_doc

    def extract(self, document: dict[str, str], claim: Claim, *, query_purpose: str) -> list[EvidenceRecord]:
        if query_purpose == "counter":
            return []
        return [
            EvidenceRecord(
                claim_id=claim.claim_id,
                quote=document["text"][:20],
                stance="support",
                source_quality=1.2,
                match_score=0.9,
                grounding_status="exact",
                is_first_party="high" in document["text"],
                published_at="2025-01-01T00:00:00+00:00",
                retrieved_at="2025-01-02T00:00:00+00:00",
                metadata={"slots": ["identity", "relationship"]},
            )
            for _ in range(self.evidence_per_doc)
        ]


def test_orchestrator_stops_after_sufficiency_without_lower_tiers(repo: ResearchRepository) -> None:
    """高质量层已满足充分性时，不应再进入中/低质量层。"""
    claim = Claim(claim_id="c1", subject_name="测试公司", relation="供应", object_name="产品")
    repo.save_claim(claim)
    task = ResearchTask(target_claim_id="c1", budget=TaskBudget(max_requests=10, max_documents=10).to_dict())
    repo.save_task(task)

    retriever = _InMemoryRetriever([
        _Doc("high 第一方公告证据", tier="high"),
        _Doc("high 第二方公告证据", tier="high"),
        _Doc("medium 新闻证据", tier="medium"),
    ])
    orchestrator = ResearchOrchestrator(
        repo, retriever, _StaticExtractor(),
        tiers=(SearchTier("high", 3), SearchTier("medium", 3), SearchTier("low", 3)),
    )
    summary = orchestrator.execute(task)

    # 应至少执行 high 层，且不应进入 low 层
    assert "high" in summary.tiers_executed
    assert "low" not in summary.tiers_executed
    # 所有结果均经过统一评估
    assert summary.sufficiency.get("sufficient") is True
    assert summary.status in {TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL.value}
    # 状态迁移审计记录完整
    transitions = repo.list_transitions("c1")
    assert transitions
    last = transitions[-1]
    assert last.from_status and last.to_status
    assert isinstance(last.evidence_ids, list)
    assert last.decision_score is not None
    assert last.threshold_version
    assert last.strategy_version
    assert last.changed_at
    assert last.reason_codes


def test_partial_task_persists_evidence_insufficiency_reason(repo: ResearchRepository) -> None:
    claim = Claim(
        claim_id="insufficient",
        subject_name="测试公司",
        relation="涉及",
        object_name="测试概念",
    )
    repo.save_claim(claim)
    task = ResearchTask(
        target_claim_id=claim.claim_id,
        budget=TaskBudget(max_requests=3, max_documents=3).to_dict(),
    )
    repo.save_task(task)
    orchestrator = ResearchOrchestrator(
        repo,
        _InMemoryRetriever([]),
        _StaticExtractor(),
        tiers=(SearchTier("high", 1),),
    )

    summary = orchestrator.execute(task)
    persisted = repo.get_task(task.task_id)

    assert summary.status == TaskStatus.PARTIAL.value
    assert persisted is not None
    assert persisted.failure["code"] == "EVIDENCE_INSUFFICIENT"
    assert persisted.failure["reason_codes"]
    assert persisted.failure["stopped_reason_code"] == summary.stopped_reason_code


class _InMemoryCache:
    """模拟支持 dict key 的通用缓存。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    def _key(key: Any) -> str:
        if isinstance(key, dict):
            return json.dumps(key, ensure_ascii=False, sort_keys=True)
        return str(key)

    def get(self, key: Any) -> Any | None:
        return self._data.get(self._key(key))

    def set(self, key: Any, value: Any) -> None:
        self._data[self._key(key)] = value


def test_orchestrator_uses_cache_and_avoids_duplicate_search(repo: ResearchRepository) -> None:
    """同一任务再次执行时应命中缓存，减少实际检索调用。"""
    claim = Claim(claim_id="c2", subject_name="测试公司", relation="供应", object_name="产品")
    repo.save_claim(claim)
    task = ResearchTask(target_claim_id="c2", budget=TaskBudget(max_requests=10, max_documents=10).to_dict())
    repo.save_task(task)

    retriever = _InMemoryRetriever([_Doc("high 证据", tier="high")])
    cache = _InMemoryCache()
    orchestrator = ResearchOrchestrator(
        repo, retriever, _StaticExtractor(),
        tiers=(SearchTier("high", 2),),
        cache=cache,
    )
    orchestrator.execute(task)
    first_call_count = len(retriever.calls)
    assert first_call_count > 0

    retriever.calls.clear()
    task2 = ResearchTask(target_claim_id="c2", budget=TaskBudget(max_requests=10, max_documents=10).to_dict())
    repo.save_task(task2)
    orchestrator.execute(task2)
    assert len(retriever.calls) == 0


def test_search_cache_adapter_maps_orchestrator_payload(tmp_path: Path) -> None:
    """SearchCacheAdapter 能把 Orchestrator 的 payload dict 映射到 SearchCache tier 语义。"""
    from core.research_graph.search_cache import SearchCache, SearchCacheAdapter

    search_cache = SearchCache(cache_dir=str(tmp_path / "orch_cache"), strategy_version="v1")
    adapter = SearchCacheAdapter(search_cache)

    payload = {
        "candidate": "sub-1", "claim": "claim-1", "query": "测试 公告",
        "purpose": "support", "tier": "high", "content_version": "v2",
    }
    assert adapter.get(payload) is None
    adapter.set(payload, ["doc1", "doc2"])
    assert adapter.get(payload) is True
    # 不同 tier 应隔离
    payload_medium = {**payload, "tier": "medium"}
    assert adapter.get(payload_medium) is None


def test_orchestrator_counter_search_flag_is_set(repo: ResearchRepository) -> None:
    """Orchestrator 应执行反证查询并标记 counter_search_completed / counter_evidence_evaluated。"""
    claim = Claim(claim_id="c3", subject_name="测试公司", relation="供应", object_name="产品")
    repo.save_claim(claim)
    task = ResearchTask(target_claim_id="c3", budget=TaskBudget(max_requests=10, max_documents=10).to_dict())
    repo.save_task(task)

    retriever = _InMemoryRetriever([_Doc("high 证据", tier="high")])
    orchestrator = ResearchOrchestrator(
        repo, retriever, _StaticExtractor(),
        tiers=(SearchTier("high", 2),),
    )
    orchestrator.execute(task)

    updated = repo.get_claim("c3")
    assert updated is not None
    assert updated.counter_search_completed is True
    assert updated.counter_evidence_evaluated is True
    assert any("否认" in q or "澄清" in q for q, _ in retriever.calls)


# ---------------------------------------------------------------------------
# EvidenceActivator 与检索假设阻断测试
# ---------------------------------------------------------------------------

def test_activator_blocks_all_generated_evidence_kinds() -> None:
    """所有生成式 evidence_kind 均不得进入 evidence_slots 或触发升级。"""
    for kind in ("pseudo_evidence", "retrieval_hypothesis", "query_prototype", "generated"):
        activator = EvidenceActivator.__new__(EvidenceActivator)
        result = activator.activate(
            evidence_text="包含候选名 HBM材料 的文本",
            evidence_id=f"ev-{kind}",
            evidence_kind=kind,
        )
        assert result.matched_count == 0
        assert result.matched_node_ids == []
        assert not result.upgraded_nodes


def test_activator_preserves_pit_fields_in_slots(tmp_path: Path) -> None:
    """EvidenceActivator 应将 published_at/retrieved_at 写入 evidence_slots。"""
    store = CandidateStore(db_path=str(tmp_path / "candidate.lance"))
    node = CandidateNode(candidate_id="cn_pit", name="HBM材料", node_type="MATERIAL", status=CandidateStatus.DISCOVERED.value)
    store.add_node(node)
    activator = EvidenceActivator(candidate_store=store)
    activator.activate(
        evidence_text="HBM材料供应链紧张。",
        evidence_id="doc_pit",
        source_type="official",
        published_at="2025-06-01T00:00:00+00:00",
        retrieved_at="2025-06-02T00:00:00+00:00",
    )
    updated = store.get_node("cn_pit")
    assert updated is not None
    assert len(updated.evidence_slots) == 1
    slot = updated.evidence_slots[0]
    assert slot["published_at"] == "2025-06-01T00:00:00+00:00"
    assert slot["retrieved_at"] == "2025-06-02T00:00:00+00:00"
