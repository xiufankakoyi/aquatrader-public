"""Research 证据过匹配与反证归因 P0 修复验收测试。

覆盖：
1. P0-1 反例：主题根词/任意中文二字仅用于召回，不得直接写证据或升级
   - "AIPC关键材料" 不得命中仅含 "AIPC电脑新品发布" 的文本
   - "AIPC制造工艺" 不得命中仅含 "AIPC市场销量增长" 的文本
2. P0-2 反证归因：
   - counter_search_executed / counter_zero_results / counter_search_completed 三态分离
   - support/counter 结果保留 query_id、purpose、evaluation_status、evaluated_stance
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.research_graph.candidate_graph import CandidateNode
from core.research_graph.evidence_activator import ActivationResult, EvidenceActivator
from core.research_graph.tiered_evidence_searcher import (
    TierConfig,
    TieredEvidenceSearcher,
)


# ===========================================================================
# P0-1 反例测试：主题根词/任意中文二字仅用于召回，不得直接写证据或升级
# ===========================================================================


def test_aipc_material_not_matched_by_aipc_pc_launch_text() -> None:
    """AIPC关键材料节点不得命中仅含"AIPC电脑新品发布"的文本。

    根因：旧 _substring_match_score 对任意英文 token（如 "AIPC"）返回 0.65，
    超过 NAME_MATCH_THRESHOLD(0.5)，导致仅含主题根词的无关文本被误判为证据。
    修复后该路径恒返回 0.0，匹配只依赖精确子串与字符覆盖率。
    """
    activator = EvidenceActivator.__new__(EvidenceActivator)
    node = CandidateNode(candidate_id="n_mat", name="AIPC关键材料", node_type="MATERIAL")

    score = activator._match_node(node, "AIPC电脑新品发布")

    assert score < EvidenceActivator.NAME_MATCH_THRESHOLD, (
        f"AIPC关键材料不得命中仅含 AIPC电脑新品发布 的文本，实际 score={score}"
    )


def test_aipc_manufacturing_not_matched_by_aipc_sales_text() -> None:
    """AIPC制造工艺节点不得命中仅含"AIPC市场销量增长"的文本。"""
    activator = EvidenceActivator.__new__(EvidenceActivator)
    node = CandidateNode(candidate_id="n_mfg", name="AIPC制造工艺", node_type="PROCESS")

    score = activator._match_node(node, "AIPC市场销量增长")

    assert score < EvidenceActivator.NAME_MATCH_THRESHOLD, (
        f"AIPC制造工艺不得命中仅含 AIPC市场销量增长 的文本，实际 score={score}"
    )


def test_substring_match_score_always_returns_zero() -> None:
    """_substring_match_score 恒返回 0.0，主题根词不得贡献匹配分。"""
    # 英文主题根词
    assert EvidenceActivator._substring_match_score("AIPC", "aipc电脑新品发布") == 0.0
    # 中文二字
    assert EvidenceActivator._substring_match_score("关键材料", "aipc关键材料") == 0.0
    # 混合
    assert EvidenceActivator._substring_match_score("AIPC关键材料", "aipc关键材料发布") == 0.0


def test_char_coverage_requires_continuous_chinese_substring() -> None:
    """_char_coverage_score 要求中文字符作为连续子串出现，不得拼凑。

    根因：旧实现逐字符检查 ``c in text``，长文中散落在各处的汉字
    会被拼凑为"全覆盖"并给出 0.85，导致普通长文成为伪证据。
    修复后要求中文序列作为连续子串出现。
    """
    # 反例：分散字符不得命中
    # "关键" 在前半句，"材料" 在后半句，不连续
    score = EvidenceActivator._char_coverage_score(
        "AIPC关键材料", "aipc电脑关键技术创新，采用新型材料"
    )
    assert score == 0.0, (
        "分散出现的汉字不得拼凑为全覆盖：'关键'和'材料'不连续"
    )

    # 反例：中文字符部分出现
    score = EvidenceActivator._char_coverage_score(
        "AIPC关键材料", "aipc关键技术创新"
    )
    assert score == 0.0, "'材料' 未出现，不得命中"

    # 正例：中文序列完整连续出现
    score = EvidenceActivator._char_coverage_score(
        "AIPC关键材料", "中际旭创aipc关键材料量产供货"
    )
    assert score == 0.85, "完整连续子串出现应返回 0.85"

    # 正例：英文 token 缺失时不命中
    score = EvidenceActivator._char_coverage_score(
        "AIPC关键材料", "中际旭创关键材料量产供货"
    )
    assert score == 0.0, "英文 token 'aipc' 缺失，不得命中"

    # 反例：长文中多个节点名分散出现，不得互相命中
    long_text = (
        "aipc行业报告：关键技术创新推动产业升级，"
        "制造工艺持续优化，应用场景不断扩展，"
        "新型材料研发取得突破，生产设备国产化加速"
    )
    # "AIPC关键材料" 的中文序列 "关键材料" 在 long_text 中不连续出现
    assert EvidenceActivator._char_coverage_score("AIPC关键材料", long_text) == 0.0
    # "AIPC制造工艺" 的中文序列 "制造工艺" 在 long_text 中连续出现 → 0.85
    assert EvidenceActivator._char_coverage_score("AIPC制造工艺", long_text) == 0.85


def test_positive_control_exact_substring_still_matches() -> None:
    """正向对照：文本包含节点完整名称时仍应命中（精确子串 1.0）。"""
    activator = EvidenceActivator.__new__(EvidenceActivator)
    node = CandidateNode(candidate_id="n_pos", name="AIPC关键材料", node_type="MATERIAL")

    score = activator._match_node(node, "某公司公告披露AIPC关键材料已量产供货")

    assert score >= EvidenceActivator.NAME_MATCH_THRESHOLD
    assert score == 1.0  # 精确子串命中


def test_candidate_evidence_can_never_mint_terminal_status(tmp_path) -> None:
    """候选图可到 SUPPORTED，但不能绕过 ClaimPolicy 进入 ACTIVE/TRUSTED。"""
    from core.research_graph.candidate_graph import CandidateStore

    store = CandidateStore(tmp_path / "terminal-gate")
    store.add_node(CandidateNode(
        candidate_id="terminal-gate-node",
        name="HBM材料",
        node_type="MATERIAL",
        status="SUPPORTED",
    ))
    activator = EvidenceActivator(store)
    for idx in range(12):
        activator.activate(
            evidence_text="HBM材料公告确认量产供货",
            evidence_id=f"terminal-{idx}",
            source_type="announcement",
            stance="support",
            source_lineage_id=f"lineage-{idx}",
            query_id=f"q-{idx}",
            query_purpose="support",
        )
    saved = store.get_node("terminal-gate-node")
    assert saved is not None
    assert saved.status == "SUPPORTED"


def test_direct_promotion_requires_claim_policy_audit(tmp_path) -> None:
    """兼容 promote API 不得在缺少 Claim/Transition 时直接写 ACTIVE。"""
    from core.research_graph.candidate_graph import CandidateStore

    store = CandidateStore(tmp_path / "promotion-gate")
    store.add_node(CandidateNode(
        candidate_id="promotion-node",
        name="HBM材料",
        node_type="MATERIAL",
        status="SUPPORTED",
    ))
    result = store.promote_candidate("promotion-node")
    assert result["promoted"] is False
    assert result["status"] == "claim_policy_required"
    assert store.get_node("promotion-node").status == "SUPPORTED"


# ===========================================================================
# P0-2 反证归因测试：TieredEvidenceSearcher 集成
# ===========================================================================


@dataclass
class _Document:
    """测试用文档，兼容 perilla SourceDocument 接口。"""

    document_id: str
    content_text: str
    url: str
    source_type: str = "search_result"
    title: str = ""
    published_at: str = "2026-07-01"
    metadata: dict = field(default_factory=dict)


class _RecordingActivator:
    """记录所有 activate 调用，便于断言反证归因字段。

    匹配规则与 test_research_evidence_boundary.py 保持一致：
    原文含候选名才算命中，避免伪造命中。
    """

    def __init__(self, matched_id: str) -> None:
        self.matched_id = matched_id
        self.calls: list[dict] = []

    def activate(self, **kwargs) -> ActivationResult:
        self.calls.append(kwargs)
        matched = self.matched_id if "HBM材料" in kwargs["evidence_text"] else ""
        return ActivationResult(matched_node_ids=[matched] if matched else [])


class _Store:
    """候选存储 mock，始终返回同一节点。"""

    def __init__(self, node: CandidateNode) -> None:
        self.node = node

    def get_node(self, candidate_id: str) -> CandidateNode | None:
        return self.node if candidate_id == self.node.candidate_id else None


class _PurposeAwareDiscovery:
    """根据查询内容区分 support/counter，返回不同文档集。

    counter_pattern="否认 澄清 未供货 终止"，反证查询包含这些关键词。
    """

    def __init__(
        self,
        support_docs: list[_Document] | None = None,
        counter_docs: list[_Document] | None = None,
    ) -> None:
        self.support_docs = support_docs or []
        self.counter_docs = counter_docs or []
        self.call_history: list[tuple[list[str], str]] = []

    def search_many(self, queries: list[str]) -> list[_Document]:
        """返回扁平 list，匹配真实 MultiEngineSearcher 契约。

        根据查询是否含反证模式词决定返回 support_docs 还是 counter_docs。
        """
        combined = " ".join(queries)
        is_counter = any(
            kw in combined for kw in ("否认", "澄清", "未供货", "终止")
        )
        tag = "counter" if is_counter else "support"
        self.call_history.append((list(queries), tag))
        return list(self.counter_docs) if is_counter else list(self.support_docs)


def _build_searcher(
    discovery: _PurposeAwareDiscovery,
    activator: _RecordingActivator,
    node: CandidateNode,
) -> TieredEvidenceSearcher:
    """构造单 tier 的 TieredEvidenceSearcher。

    max_queries=2：1 条 support + 1 条 counter，保证两类查询都执行。
    """
    return TieredEvidenceSearcher(
        search_discovery=discovery,
        candidate_store=_Store(node),
        evidence_activator=activator,
        tiers=[TierConfig("high", ("official",), ("公告",), "official", max_queries=2)],
        max_total_documents=5,
    )


def test_counter_search_executed_when_counter_queries_run() -> None:
    """反证查询实际执行时 counter_search_executed 必须为 True。"""
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    # support 有命中，counter 有命中
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[_Document("d2", "公司澄清未供货HBM材料", "https://example.com/c1")],
    )
    searcher = _build_searcher(discovery, activator, node)

    result = searcher.search_for_node(node)

    assert result.counter_search_executed is True
    assert result.counter_search_completed is True
    assert result.counter_zero_results is False


def test_counter_zero_results_distinguished_from_not_executed() -> None:
    """反证查询执行但零命中，counter_zero_results=True，不得与未执行混淆。

    未执行：counter_search_executed=False, counter_zero_results=False
    零命中：counter_search_executed=True,  counter_zero_results=True
    完成：  counter_search_executed=True,  counter_search_completed=True（零命中视为已评估）
    """
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[],  # 反证零命中
    )
    searcher = _build_searcher(discovery, activator, node)

    result = searcher.search_for_node(node)

    assert result.counter_search_executed is True
    assert result.counter_zero_results is True
    # 零命中视为"已评估无反证"，completed 仍成立
    assert result.counter_search_completed is True


def test_counter_search_not_executed_when_no_counter_queries() -> None:
    """无反证查询时三态均为 False（安全降级，不与零命中混淆）。

    通过 max_queries=1 且构造只有 support 的 tier，验证未执行路径。
    """
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[],
    )
    # 模拟无反证查询：拦截 _build_queries 只返回 support
    searcher = _build_searcher(discovery, activator, node)
    searcher._build_queries = lambda n, t: [  # type: ignore[method-assign]
        (f"{n.name} 公告", "support"),
        (f"{n.name} 财报", "support"),
    ]

    result = searcher.search_for_node(node)

    assert result.counter_search_executed is False
    assert result.counter_zero_results is False
    assert result.counter_search_completed is False


def test_query_id_and_purpose_preserved_in_activator_calls() -> None:
    """support/counter 文档送入 activator 时必须携带 query_id 和 purpose。

    验收：support/counter 结果保留 query_id、purpose、evaluation_status、evaluated_stance。
    """
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[_Document("d2", "公司澄清未供货HBM材料", "https://example.com/c1")],
    )
    searcher = _build_searcher(discovery, activator, node)

    searcher.search_for_node(node)

    assert len(activator.calls) >= 2, "至少应激活 support 和 counter 各一条"

    support_calls = [c for c in activator.calls if c.get("query_purpose") == "support"]
    counter_calls = [c for c in activator.calls if c.get("query_purpose") == "counter"]

    assert len(support_calls) >= 1, "必须保留 support 归因"
    assert len(counter_calls) >= 1, "必须保留 counter 归因"

    for call in support_calls + counter_calls:
        assert call.get("query_id"), "query_id 不得为空"
        assert call.get("query_purpose") in {"support", "counter"}
        # evaluation_status 和 evaluated_stance 在 evidence_activator 内部写入 slot，
        # 不作为 activate() 入参；这里验证 activator 收到 stance（用于 evaluated_stance）
        assert call.get("stance") in {"support", "counter", "neutral"}


def test_support_and_counter_query_ids_are_distinct() -> None:
    """support 与 counter 的 query_id 必须不同，避免归因混淆。"""
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[_Document("d2", "公司澄清未供货HBM材料", "https://example.com/c1")],
    )
    searcher = _build_searcher(discovery, activator, node)

    searcher.search_for_node(node)

    support_ids = {c["query_id"] for c in activator.calls if c.get("query_purpose") == "support"}
    counter_ids = {c["query_id"] for c in activator.calls if c.get("query_purpose") == "counter"}

    assert support_ids, "support 应有 query_id"
    assert counter_ids, "counter 应有 query_id"
    assert support_ids.isdisjoint(counter_ids), "support 与 counter 的 query_id 不得重叠"


# ===========================================================================
# P0 第二轮修复：长文散字、边匹配、字典返回、反证评估、URL 去重、counter_terms
# ===========================================================================


def test_char_coverage_rejects_scattered_chars_in_long_text() -> None:
    """P0-1a: 长文中散落汉字不得被 _char_coverage_score 拼凑为 0.85 命中。

    场景："AIPC关键材料" 的四个汉字 "关键材料" 散落在长文不同位置，
    旧实现逐字符检查会返回 0.85；新实现要求连续子串，返回 0.0。
    """
    activator = EvidenceActivator.__new__(EvidenceActivator)
    node = CandidateNode(candidate_id="n_scatter", name="AIPC关键材料", node_type="MATERIAL")
    # "关"在句首、"键"在中间、"材料"在句尾，但不连续
    long_text = (
        "AIPC关注行业趋势，但普通键盘厂商也在布局，"
        "不涉及高端材料供应链，纯属市场炒作"
    )

    score = activator._match_node(node, long_text)

    assert score < EvidenceActivator.NAME_MATCH_THRESHOLD, (
        f"散落汉字不得命中，实际 score={score}"
    )


def test_char_coverage_accepts_contiguous_chinese_substring() -> None:
    """P0-1a 正向对照：中文字符连续出现时仍应命中。"""
    activator = EvidenceActivator.__new__(EvidenceActivator)
    node = CandidateNode(candidate_id="n_contig", name="AIPC关键材料", node_type="MATERIAL")

    score = activator._match_node(node, "某研报指出AIPC关键材料已实现量产突破")

    assert score >= EvidenceActivator.NAME_MATCH_THRESHOLD


def test_edge_requires_both_endpoints_matched() -> None:
    """P0-1b: 候选边两端节点都命中才附加证据，单端命中不得附加。

    用 EvidenceActivator 的真实 activate() 测试边匹配逻辑。
    需要构造一个含边的 candidate_store mock。
    """
    from core.research_graph.candidate_graph import CandidateEdge

    class _EdgeStore:
        """同时持有两个节点和一条边的 store mock。"""

        def __init__(self) -> None:
            self.node_a = CandidateNode(candidate_id="na", name="HBM材料", node_type="MATERIAL")
            self.node_b = CandidateNode(candidate_id="nb", name="封装测试", node_type="PROCESS")
            self.edge = CandidateEdge(
                candidate_id="e1",
                subject_id="na",
                object_id="nb",
                relation="依赖",
            )
            self._saved_nodes: dict[str, CandidateNode] = {}
            self._saved_edges: dict[str, CandidateEdge] = {}

        def list_candidates(self, view: str = "exploration") -> list[CandidateNode]:
            return [self.node_a, self.node_b]

        def get_node(self, cid: str) -> CandidateNode | None:
            return self._saved_nodes.get(cid) or (
                self.node_a if cid == "na" else self.node_b if cid == "nb" else None
            )

        def list_edges(self, view: str = "exploration") -> list[CandidateEdge]:
            return [self.edge]

        def update_nodes_batch(self, updates: list[tuple[str, dict]]) -> None:
            for cid, data in updates:
                node = self.get_node(cid)
                if node:
                    for k, v in data.items():
                        setattr(node, k, v)
                    self._saved_nodes[cid] = node

        def update_edges_batch(self, updates: list[tuple[str, dict]]) -> None:
            for cid, data in updates:
                if cid == "e1":
                    for k, v in data.items():
                        setattr(self.edge, k, v)

    store = _EdgeStore()
    # 不初始化 repository（会触发 DB）；直接手动设置必要属性
    activator = EvidenceActivator.__new__(EvidenceActivator)
    activator.candidate_store = store
    activator._bayesian_upgrader = __import__(
        "core.research_graph.bayesian_status_upgrader",
        fromlist=["BayesianStatusUpgrader"],
    ).BayesianStatusUpgrader()
    activator._source_quality_weights = {"announcement": 1.5, "unknown": 0.5}
    activator._on_candidate_transition = None
    from core.research_graph.keyword_match_index import KeywordMatchIndex
    activator._keyword_index = KeywordMatchIndex()
    activator._keyword_index.build([store.node_a, store.node_b])
    activator._keyword_warmed = True

    # 文本只含"HBM材料"（命中 node_a），不含"封装测试"（不命中 node_b）
    result = activator.activate(
        evidence_text="公司公告HBM材料量产",
        evidence_id="ev_single",
        source_type="announcement",
    )

    # 边不应被附加证据
    assert "e1" not in result.matched_edge_ids, (
        "单端命中不得附加边证据，matched_edge_ids 不应包含 e1"
    )


class _DictReturningDiscovery:
    """模拟真实 SearchDiscovery.search_many() 返回 Dict[str, List] 的行为。"""

    def __init__(
        self,
        support_docs: list[_Document] | None = None,
        counter_docs: list[_Document] | None = None,
    ) -> None:
        self.support_docs = support_docs or []
        self.counter_docs = counter_docs or []

    def search_many(self, queries: list[str]) -> dict[str, list[_Document]]:
        """返回 Dict[str, List[SourceDocument]]，匹配 perilla SearchDiscovery 契约。"""
        combined = " ".join(queries)
        is_counter = any(
            kw in combined for kw in ("否认", "澄清", "未供货", "终止")
        )
        docs = self.counter_docs if is_counter else self.support_docs
        return {q: list(docs) for q in queries}


def test_search_many_dict_return_is_handled() -> None:
    """P0-2a: SearchDiscovery.search_many() 返回字典时不得崩溃，文档应正确激活。"""
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    discovery = _DictReturningDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", "https://example.com/s1")],
        counter_docs=[_Document("d2", "公司澄清未供货HBM材料", "https://example.com/c1")],
    )
    searcher = _build_searcher(discovery, activator, node)

    result = searcher.search_for_node(node)

    # 不应崩溃，且应正确识别 counter 查询已执行
    assert result.counter_search_executed is True
    assert len(activator.calls) >= 1, "字典返回也应激活文档"


def test_same_url_support_and_counter_both_preserved() -> None:
    """P0-2c: 同一 URL 的文档在 support 和 counter 中都应保留，不得丢失 counter 归因。"""
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    activator = _RecordingActivator(node.candidate_id)
    # 同一 URL 同时出现在 support 和 counter 中
    same_url = "https://example.com/dual"
    discovery = _PurposeAwareDiscovery(
        support_docs=[_Document("d1", "公司公告HBM材料量产", same_url)],
        counter_docs=[_Document("d2", "公司澄清未供货HBM材料", same_url)],
    )
    searcher = _build_searcher(discovery, activator, node)

    searcher.search_for_node(node)

    support_calls = [c for c in activator.calls if c.get("query_purpose") == "support"]
    counter_calls = [c for c in activator.calls if c.get("query_purpose") == "counter"]

    assert len(support_calls) >= 1, "同一 URL 的 support 归因应保留"
    assert len(counter_calls) >= 1, "同一 URL 的 counter 归因不得丢失"


def test_build_tiered_queries_uses_custom_counter_terms() -> None:
    """P0-2d: build_tiered_queries 接受 counter_terms 并生成自定义反证查询。"""
    from core.research_graph.tiered_evidence_searcher import build_tiered_queries

    queries = build_tiered_queries(
        ["HBM材料"],
        tier_name="high",
        query_suffixes=("公告",),
        max_queries=2,
        counter_terms=["收入占比极低", "不属实"],
    )

    counter_queries = [q for q, p in queries if p == "counter"]
    assert len(counter_queries) == 1
    # 反证查询应包含自定义 counter_terms，而非默认的"否认 澄清 未供货 终止"
    assert "收入占比极低" in counter_queries[0]
    assert "不属实" in counter_queries[0]
    assert "否认" not in counter_queries[0], "不得回退到默认 counter_pattern"


def test_build_tiered_queries_falls_back_to_default_counter_pattern() -> None:
    """P0-2d: counter_terms 为空时回退到默认 counter_pattern。"""
    from core.research_graph.tiered_evidence_searcher import build_tiered_queries

    queries = build_tiered_queries(
        ["HBM材料"],
        tier_name="high",
        query_suffixes=("公告",),
        max_queries=2,
        counter_terms=None,
    )

    counter_queries = [q for q, p in queries if p == "counter"]
    assert len(counter_queries) == 1
    assert "否认" in counter_queries[0]


# ===========================================================================
# P0 第三轮修复：相同 evidence_id 双归因 + 边关系 grounding
# ===========================================================================


class _InMemoryNodeStore:
    """纯内存 CandidateStore mock，支持节点读写。

    用于验证真实 EvidenceActivator 的 evidence_slots 最终状态，
    而非仅验证 activate() 被调用。
    """

    def __init__(self, nodes: list[CandidateNode]) -> None:
        self._nodes: dict[str, CandidateNode] = {n.candidate_id: n for n in nodes}

    def list_candidates(self, view: str = "exploration") -> list[CandidateNode]:
        return list(self._nodes.values())

    def get_node(self, cid: str) -> CandidateNode | None:
        return self._nodes.get(cid)

    def update_nodes_batch(self, updates: list[tuple[str, dict]]) -> None:
        for cid, data in updates:
            node = self._nodes.get(cid)
            if node:
                for k, v in data.items():
                    setattr(node, k, v)

    def list_edges(self, view: str = "exploration") -> list:
        return []

    def update_edges_batch(self, updates: list[tuple[str, dict]]) -> None:
        pass


def _make_real_activator(store: _InMemoryNodeStore) -> EvidenceActivator:
    """构造真实 EvidenceActivator，绕过 repository 初始化。"""
    from core.research_graph.bayesian_status_upgrader import BayesianStatusUpgrader
    from core.research_graph.candidate_schema import SOURCE_QUALITY_BOOST
    from core.research_graph.keyword_match_index import KeywordMatchIndex

    activator = EvidenceActivator.__new__(EvidenceActivator)
    activator.candidate_store = store
    activator._bayesian_upgrader = BayesianStatusUpgrader()
    activator._source_quality_weights = dict(SOURCE_QUALITY_BOOST)
    activator._on_candidate_transition = None
    activator._keyword_index = KeywordMatchIndex()
    activator._keyword_index.build(store.list_candidates())
    activator._keyword_warmed = True
    return activator


def test_same_evidence_id_support_and_counter_both_preserved_in_slots() -> None:
    """P0-3a: 同一 evidence_id 的 support 和 counter 都应保留在 evidence_slots。

    真实链路中同一 URL 可能生成相同 evidence_id。旧实现按 evidence_id
    去重，第二次（counter）slot 被丢弃，反证归因丢失。新实现按
    (evidence_id, purpose) 去重，两条 slot 都保留。
    """
    node = CandidateNode(candidate_id="n1", name="HBM材料", node_type="MATERIAL")
    store = _InMemoryNodeStore([node])
    activator = _make_real_activator(store)

    # 同一 evidence_id，先 support 后 counter
    same_evidence_id = "ev-dual-001"
    activator.activate(
        evidence_text="公司公告HBM材料量产供货",
        evidence_id=same_evidence_id,
        source_type="announcement",
        stance="support",
        query_id="q-support-1",
        query_purpose="support",
    )
    activator.activate(
        evidence_text="公司澄清未供货HBM材料",
        evidence_id=same_evidence_id,
        source_type="announcement",
        stance="counter",
        query_id="q-counter-1",
        query_purpose="counter",
    )

    # 验证最终 evidence_slots 保留双归因
    updated_node = store.get_node("n1")
    assert updated_node is not None
    slots = updated_node.evidence_slots
    purposes = {s.get("purpose") for s in slots}
    assert "support" in purposes, "support slot 必须保留"
    assert "counter" in purposes, "counter slot 必须保留（同 evidence_id 不同 purpose）"

    # 验证两条 slot 的 query_id 和 evaluated_stance 正确
    support_slot = next(s for s in slots if s.get("purpose") == "support")
    counter_slot = next(s for s in slots if s.get("purpose") == "counter")
    assert support_slot["query_id"] == "q-support-1"
    assert support_slot["evaluated_stance"] == "support"
    assert counter_slot["query_id"] == "q-counter-1"
    assert counter_slot["evaluated_stance"] == "counter"


def test_edge_relation_grounded_rejects_co_occurrence_without_relation() -> None:
    """P0-3b: 文本同时提到两端实体但未表达关系语义时，边不得附加证据。

    场景：edge(HBM材料, 封装测试, relation="依赖")，
    文本含"HBM材料"和"封装测试"但无"依赖"及其同义词，不应给边附证据。
    """
    from core.research_graph.candidate_graph import CandidateEdge

    class _EdgeStore(_InMemoryNodeStore):
        def __init__(self) -> None:
            self.node_a = CandidateNode(
                candidate_id="na", name="HBM材料", node_type="MATERIAL"
            )
            self.node_b = CandidateNode(
                candidate_id="nb", name="封装测试", node_type="PROCESS"
            )
            self.edge = CandidateEdge(
                candidate_id="e1",
                subject_id="na",
                object_id="nb",
                relation="依赖",
            )
            super().__init__([self.node_a, self.node_b])

        def list_edges(self, view: str = "exploration") -> list:
            return [self.edge]

        def update_edges_batch(self, updates: list[tuple[str, dict]]) -> None:
            for cid, data in updates:
                if cid == "e1":
                    for k, v in data.items():
                        setattr(self.edge, k, v)

    store = _EdgeStore()
    activator = _make_real_activator(store)

    # 文本同时含"HBM材料"和"封装测试"，但不含"依赖"
    result = activator.activate(
        evidence_text="今日HBM材料价格波动，封装测试产能也受到影响",
        evidence_id="ev-cooccur-1",
        source_type="news",
        stance="support",
    )

    # 边不应被附加证据（未表达"依赖"关系语义）
    assert "e1" not in result.matched_edge_ids, (
        "文本同时提到两端实体但未表达关系语义时，边不得附加证据"
    )


def test_edge_relation_grounded_accepts_explicit_relation() -> None:
    """P0-3b 正向对照：文本含 relation 关键词时边应附加证据。"""
    from core.research_graph.candidate_graph import CandidateEdge

    class _EdgeStore(_InMemoryNodeStore):
        def __init__(self) -> None:
            self.node_a = CandidateNode(
                candidate_id="na", name="HBM材料", node_type="MATERIAL"
            )
            self.node_b = CandidateNode(
                candidate_id="nb", name="封装测试", node_type="PROCESS"
            )
            self.edge = CandidateEdge(
                candidate_id="e1",
                subject_id="na",
                object_id="nb",
                relation="依赖",
            )
            super().__init__([self.node_a, self.node_b])

        def list_edges(self, view: str = "exploration") -> list:
            return [self.edge]

        def update_edges_batch(self, updates: list[tuple[str, dict]]) -> None:
            for cid, data in updates:
                if cid == "e1":
                    for k, v in data.items():
                        setattr(self.edge, k, v)

    store = _EdgeStore()
    activator = _make_real_activator(store)

    # 文本含"HBM材料"、"封装测试"和"依赖"
    result = activator.activate(
        evidence_text="HBM材料量产依赖封装测试产能配合",
        evidence_id="ev-relation-1",
        source_type="announcement",
        stance="support",
    )

    # 边应被附加证据（表达"依赖"关系语义）
    assert "e1" in result.matched_edge_ids, (
        "文本表达关系语义时，边应附加证据"
    )


def test_edge_relation_empty_degrades_safely() -> None:
    """P0-3b 安全降级：relation 为空时不阻断边匹配（向后兼容）。"""
    from core.research_graph.candidate_graph import CandidateEdge

    class _EdgeStore(_InMemoryNodeStore):
        def __init__(self) -> None:
            self.node_a = CandidateNode(
                candidate_id="na", name="HBM材料", node_type="MATERIAL"
            )
            self.node_b = CandidateNode(
                candidate_id="nb", name="封装测试", node_type="PROCESS"
            )
            self.edge = CandidateEdge(
                candidate_id="e1",
                subject_id="na",
                object_id="nb",
                relation="",  # 空_relation
            )
            super().__init__([self.node_a, self.node_b])

        def list_edges(self, view: str = "exploration") -> list:
            return [self.edge]

        def update_edges_batch(self, updates: list[tuple[str, dict]]) -> None:
            for cid, data in updates:
                if cid == "e1":
                    for k, v in data.items():
                        setattr(self.edge, k, v)

    store = _EdgeStore()
    activator = _make_real_activator(store)

    # 文本同时含两端实体，relation 为空
    result = activator.activate(
        evidence_text="HBM材料与封装测试同行业关注",
        evidence_id="ev-empty-rel-1",
        source_type="news",
        stance="support",
    )

    # relation 为空时安全降级，边应被附加（保持向后兼容）
    assert "e1" in result.matched_edge_ids, (
        "relation 为空时应安全降级，不阻断边匹配"
    )
