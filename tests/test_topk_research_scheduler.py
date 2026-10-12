"""P2.4 Top-K 主动证据检索调度器测试。

按 P2 计划 §4 Task P2.4 验收：
- Top-K 外公司不发生无界深搜（验收点 1）
- 证据命中率、有效输入转化率和单次成本可统计（验收点 2）
- 搜索失败只产生 gap，不生成事实（验收点 3）
- 任务重启后可恢复且幂等（验收点 4）

测试组织：
- TestSourceTierConstants：来源层级常量
- TestCandidateSourceConstants：候选来源常量
- TestSchedulerStatus：调度器状态常量
- TestTopKCandidate：TopKCandidate 数据类
- TestSearchQuery：SearchQuery 必填字段校验与去重键
- TestSearchHit：SearchHit 数据类
- TestSearchResult：SearchResult 状态契约
- TestSchedulerStats：统计指标计算
- TestNullSearchExecutor：默认空检索器返回 gap
- TestSelectTopK：轻量筛选（验收点 1 前置）
- TestSelectTopKFromFunnel：从 P2.2 漏斗抽取候选
- TestSelectTopKFromChangeRadar：从 P2.3 雷达抽取候选
- TestSelectTopKFromGaps：从缺口任务抽取候选
- TestPlanQuery：查询构造（含字段缺失降级）
- TestExecuteSearchTopKGuard：验收点 1 Top-K 外公司拒绝
- TestExecuteSearchBudgetGuard：预算预留与闲时上限
- TestExecuteSearchDedup：去重与缓存
- TestExecuteSearchFailureToGap：验收点 3 失败转 gap
- TestExecuteSearchStats：验收点 2 统计可观察
- TestRecoverIdempotent：验收点 4 幂等恢复
- TestRecoverResume：验收点 4 续跑
- TestGapTaskPlannerModelBlocking：gap_tasks model_blocking 标记
- TestBudgetManagerIdleCap：budget_manager 闲时上限 API
- TestAcceptancePoint1：验收点 1 综合
- TestAcceptancePoint2：验收点 2 综合
- TestAcceptancePoint3：验收点 3 综合
- TestAcceptancePoint4：验收点 4 综合
- TestGraceful：字段缺失降级（AGENTS.md）
- TestNoTradeAdvice：AGENTS.md 合规性
- TestEndToEndIntegration：端到端集成
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.research.control_plane.budget_manager import GlobalBudgetManager
from core.research.control_plane.contracts import ResearchTask, TaskBudget, TaskStatus
from core.research.control_plane.repository import ResearchRepository
from core.research.discovery.research_scheduler import (
    CANDIDATE_SOURCE_CHANGE_RADAR,
    CANDIDATE_SOURCE_FUNNEL_HIGH_CONV,
    CANDIDATE_SOURCE_FUNNEL_PARETO,
    CANDIDATE_SOURCE_GAP_MODEL_BLOCKING,
    DEFAULT_TOP_K,
    MAX_IDLE_BUDGET_RATIO,
    SCHEDULER_STATUS_CANCELED,
    SCHEDULER_STATUS_FAILED,
    SCHEDULER_STATUS_GAP,
    SCHEDULER_STATUS_PENDING,
    SCHEDULER_STATUS_SUCCEEDED,
    SCHEDULER_TERMINAL_STATUSES,
    SOURCE_ORDER,
    SOURCE_TIER_FIRST_PARTY,
    SOURCE_TIER_INDUSTRY_BROKER,
    SOURCE_TIER_PUBLIC_WEB,
    SearchExecutor,
    SearchHit,
    SearchQuery,
    SearchResult,
    SchedulerStats,
    TopKCandidate,
    TopKResearchScheduler,
    _NullSearchExecutor,
    build_default_scheduler,
)
from core.research.value_chain.gap_tasks import (
    MODEL_BLOCKING_GAPS,
    GapTaskPlanner,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repository(tmp_path) -> ResearchRepository:
    """临时 SQLite 仓储。"""
    return ResearchRepository(db_path=str(tmp_path / "test_research.db"))


@pytest.fixture
def budget_manager(tmp_repository) -> GlobalBudgetManager:
    """绑定临时仓储的全局预算管理器。"""
    return GlobalBudgetManager(repository=tmp_repository)


@pytest.fixture
def gap_planner(tmp_repository) -> GapTaskPlanner:
    """绑定临时仓储的缺口任务规划器。"""
    return GapTaskPlanner(repository=tmp_repository)


@pytest.fixture
def scheduler(tmp_repository, budget_manager, gap_planner) -> TopKResearchScheduler:
    """默认调度器（使用 _NullSearchExecutor）。"""
    return TopKResearchScheduler(
        budget_manager=budget_manager,
        gap_planner=gap_planner,
        repository=tmp_repository,
    )


def _make_candidate(
    symbol: str = "600519",
    company_name: str = "贵州茅台",
    as_of: str = "2024-06-30",
    target_variable: str = "asp",
    product: str = "茅台酒",
    preferred_source: str = SOURCE_TIER_FIRST_PARTY,
    priority: float = 0.9,
    candidate_source: str = CANDIDATE_SOURCE_FUNNEL_PARETO,
) -> TopKCandidate:
    """构造测试用 Top-K 候选。"""
    return TopKCandidate(
        symbol=symbol,
        company_name=company_name,
        as_of=as_of,
        target_variable=target_variable,
        product=product,
        period=as_of,
        preferred_source=preferred_source,
        priority=priority,
        candidate_source=candidate_source,
    )


def _make_query(
    target_variable: str = "asp",
    company: str = "贵州茅台",
    product: str = "茅台酒",
    period: str = "2024-Q2",
    preferred_source: str = SOURCE_TIER_FIRST_PARTY,
) -> SearchQuery:
    """构造测试用查询。"""
    q = SearchQuery(
        target_variable=target_variable,
        company=company,
        product=product,
        period=period,
        preferred_source=preferred_source,
    )
    q.dedup_key = q.compute_dedup_key()
    return q


class _StubExecutor(SearchExecutor):
    """可控的检索器 stub。"""

    def __init__(
        self,
        hits: list[SearchHit] | None = None,
        status: str = SCHEDULER_STATUS_SUCCEEDED,
        gap_description: str = "",
        raise_exc: Exception | None = None,
        consumed_budget: TaskBudget | None = None,
    ) -> None:
        self.hits = hits or []
        self.status = status
        self.gap_description = gap_description
        self.raise_exc = raise_exc
        self.consumed_budget = consumed_budget or TaskBudget()
        self.calls: list[SearchQuery] = []

    def execute(
        self,
        query: SearchQuery,
        budget: TaskBudget,
        timeout_seconds: float,
    ) -> SearchResult:
        self.calls.append(query)
        if self.raise_exc is not None:
            raise self.raise_exc
        return SearchResult(
            query_id=query.query_id,
            status=self.status,
            hits=list(self.hits),
            gap_description=self.gap_description,
            consumed_budget=self.consumed_budget,
            elapsed_seconds=0.01,
            started_at="2024-01-01T00:00:00+00:00",
            finished_at="2024-01-01T00:00:01+00:00",
        )


# ---------------------------------------------------------------------------
# 常量测试
# ---------------------------------------------------------------------------


class TestSourceTierConstants:
    """来源层级常量。"""

    def test_source_order(self) -> None:
        """来源顺序：first_party → industry_broker → public_web。"""
        assert SOURCE_ORDER == (
            SOURCE_TIER_FIRST_PARTY,
            SOURCE_TIER_INDUSTRY_BROKER,
            SOURCE_TIER_PUBLIC_WEB,
        )

    def test_first_party_first(self) -> None:
        """第一方来源必须排在第一位。"""
        assert SOURCE_ORDER[0] == SOURCE_TIER_FIRST_PARTY

    def test_public_web_last(self) -> None:
        """公开网页必须排在最后。"""
        assert SOURCE_ORDER[-1] == SOURCE_TIER_PUBLIC_WEB


class TestCandidateSourceConstants:
    """候选来源常量。"""

    def test_candidate_sources_distinct(self) -> None:
        """4 个候选来源互不重复。"""
        sources = {
            CANDIDATE_SOURCE_FUNNEL_PARETO,
            CANDIDATE_SOURCE_FUNNEL_HIGH_CONV,
            CANDIDATE_SOURCE_CHANGE_RADAR,
            CANDIDATE_SOURCE_GAP_MODEL_BLOCKING,
        }
        assert len(sources) == 4

    def test_candidate_sources_nonempty(self) -> None:
        """候选来源常量非空字符串。"""
        for s in (
            CANDIDATE_SOURCE_FUNNEL_PARETO,
            CANDIDATE_SOURCE_FUNNEL_HIGH_CONV,
            CANDIDATE_SOURCE_CHANGE_RADAR,
            CANDIDATE_SOURCE_GAP_MODEL_BLOCKING,
        ):
            assert isinstance(s, str)
            assert s


class TestSchedulerStatus:
    """调度器状态常量。"""

    def test_terminal_statuses(self) -> None:
        """终态包含 succeeded/failed/gap/canceled。"""
        assert SCHEDULER_STATUS_SUCCEEDED in SCHEDULER_TERMINAL_STATUSES
        assert SCHEDULER_STATUS_FAILED in SCHEDULER_TERMINAL_STATUSES
        assert SCHEDULER_STATUS_GAP in SCHEDULER_TERMINAL_STATUSES
        assert SCHEDULER_STATUS_CANCELED in SCHEDULER_TERMINAL_STATUSES

    def test_pending_not_terminal(self) -> None:
        """pending 不是终态。"""
        assert SCHEDULER_STATUS_PENDING not in SCHEDULER_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# 数据契约测试
# ---------------------------------------------------------------------------


class TestTopKCandidate:
    """TopKCandidate 数据类。"""

    def test_to_dict_roundtrip(self) -> None:
        """序列化与字段保留。"""
        cand = _make_candidate()
        d = cand.to_dict()
        assert d["symbol"] == "600519"
        assert d["company_name"] == "贵州茅台"
        assert d["preferred_source"] == SOURCE_TIER_FIRST_PARTY
        assert d["priority"] == pytest.approx(0.9)

    def test_default_preferred_source(self) -> None:
        """默认 preferred_source 为 first_party。"""
        cand = TopKCandidate(symbol="000001")
        assert cand.preferred_source == SOURCE_TIER_FIRST_PARTY


class TestSearchQuery:
    """SearchQuery 必填字段校验与去重键。"""

    def test_validate_all_present(self) -> None:
        """所有字段齐全时校验通过。"""
        q = _make_query()
        assert q.validate() == []

    def test_validate_missing_target_variable(self) -> None:
        """缺失 target_variable 校验失败。"""
        q = _make_query(target_variable="")
        errors = q.validate()
        assert any("target_variable" in e for e in errors)

    def test_validate_missing_company(self) -> None:
        """缺失 company 校验失败。"""
        q = _make_query(company="")
        errors = q.validate()
        assert any("company" in e for e in errors)

    def test_validate_missing_product(self) -> None:
        """缺失 product 校验失败。"""
        q = _make_query(product="")
        errors = q.validate()
        assert any("product" in e for e in errors)

    def test_validate_missing_period(self) -> None:
        """缺失 period 校验失败。"""
        q = _make_query(period="")
        errors = q.validate()
        assert any("period" in e for e in errors)

    def test_validate_missing_preferred_source(self) -> None:
        """缺失 preferred_source 校验失败。"""
        q = _make_query(preferred_source="")
        errors = q.validate()
        assert any("preferred_source" in e for e in errors)

    def test_validate_invalid_preferred_source(self) -> None:
        """非法 preferred_source 校验失败。"""
        q = _make_query(preferred_source="dark_web")
        errors = q.validate()
        assert any("非法 preferred_source" in e for e in errors)

    def test_dedup_key_stable(self) -> None:
        """相同查询去重键相同。"""
        q1 = _make_query()
        q2 = _make_query()
        assert q1.compute_dedup_key() == q2.compute_dedup_key()

    def test_dedup_key_differs_when_target_variable_differs(self) -> None:
        """不同 target_variable 去重键不同。"""
        q1 = _make_query(target_variable="asp")
        q2 = _make_query(target_variable="margin")
        assert q1.compute_dedup_key() != q2.compute_dedup_key()


class TestSearchHit:
    """SearchHit 数据类。"""

    def test_default_hit_id_generated(self) -> None:
        """hit_id 默认生成。"""
        h = SearchHit()
        assert h.hit_id.startswith("hit_")


class TestSearchResult:
    """SearchResult 状态契约。"""

    def test_default_status_pending(self) -> None:
        """默认状态为 pending。"""
        r = SearchResult()
        assert r.status == SCHEDULER_STATUS_PENDING

    def test_gap_no_hits(self) -> None:
        """gap 状态不返回 hits（验收点 3）。"""
        r = SearchResult(status=SCHEDULER_STATUS_GAP, gap_description="无数据")
        assert r.hits == []
        assert r.gap_description


class TestSchedulerBoundaryNormalization:
    """调度器边界输入和 executor 非规范返回的降级契约。"""

    def test_invalid_scheduler_options_use_safe_defaults(self, tmp_repository) -> None:
        """非法构造参数不应在启动阶段击穿。"""
        sched = TopKResearchScheduler(
            repository=tmp_repository,
            top_k="bad",
            concurrency=float("nan"),
            timeout_seconds=float("inf"),
            cache_ttl_seconds=-1,
            idle_budget_ratio=float("nan"),
        )
        assert sched.top_k == DEFAULT_TOP_K
        assert sched.concurrency >= 1
        assert sched.timeout_seconds == pytest.approx(30.0)
        assert sched.cache_ttl_seconds == 0.0
        assert 0.0 <= sched.idle_budget_ratio <= MAX_IDLE_BUDGET_RATIO

    def test_non_string_query_fields_validate_without_crashing(self) -> None:
        """不可信字段类型应返回校验错误，而不是抛 AttributeError。"""
        query = SearchQuery(
            target_variable=None,  # type: ignore[arg-type]
            company=123,  # type: ignore[arg-type]
            product=None,  # type: ignore[arg-type]
            period=[],  # type: ignore[arg-type]
            preferred_source=None,  # type: ignore[arg-type]
            timeout_seconds="bad",  # type: ignore[arg-type]
            max_documents="bad",  # type: ignore[arg-type]
        )
        errors = query.validate()
        assert any("target_variable" in error for error in errors)
        assert any("timeout_seconds" in error for error in errors)
        assert any("max_documents" in error for error in errors)

    def test_executor_success_without_valid_hits_becomes_gap(self, tmp_repository) -> None:
        """executor 声称成功但没有有效命中时不能生成事实。"""
        sched = TopKResearchScheduler(
            budget_manager=GlobalBudgetManager(repository=tmp_repository),
            gap_planner=GapTaskPlanner(repository=tmp_repository),
            repository=tmp_repository,
            executor=_StubExecutor(hits=[object()]),
        )
        sched._topk_symbols = {"600519"}
        result = sched.execute_search(_make_candidate(), _make_query())
        assert result.status == SCHEDULER_STATUS_GAP
        assert result.hits == []
        assert sched.stats().gap_count == 1

    def test_failed_executor_hits_are_discarded(self, tmp_repository) -> None:
        """失败状态即使携带误传 hits，也必须清空。"""
        sched = TopKResearchScheduler(
            budget_manager=GlobalBudgetManager(repository=tmp_repository),
            gap_planner=GapTaskPlanner(repository=tmp_repository),
            repository=tmp_repository,
            executor=_StubExecutor(
                status=SCHEDULER_STATUS_FAILED,
                hits=[SearchHit(cost=float("nan"))],
            ),
        )
        sched._topk_symbols = {"600519"}
        result = sched.execute_search(_make_candidate(), _make_query())
        assert result.status == SCHEDULER_STATUS_FAILED
        assert result.hits == []
        assert sched.stats().total_hits == 0

    def test_cache_hit_is_a_copy(self, tmp_repository) -> None:
        """调用方修改缓存命中不应污染后续查询。"""
        executor = _StubExecutor(hits=[SearchHit(title="原始标题")])
        sched = TopKResearchScheduler(
            budget_manager=GlobalBudgetManager(repository=tmp_repository),
            gap_planner=GapTaskPlanner(repository=tmp_repository),
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        sched.execute_search(_make_candidate(), _make_query())
        sched._dedup_keys.clear()
        cached = sched.execute_search(_make_candidate(), _make_query())
        cached.hits[0].title = "调用方修改"
        sched._dedup_keys.clear()
        cached_again = sched.execute_search(_make_candidate(), _make_query())
        assert cached_again.hits[0].title == "原始标题"

    def test_malformed_success_checkpoint_degrades(self, tmp_repository) -> None:
        """损坏 checkpoint 不应在恢复时抛出异常或伪造命中。"""
        task = ResearchTask(
            task_id="malformed_checkpoint",
            task_type="topk_evidence_search",
            checkpoint={
                "scheduler_status": SCHEDULER_STATUS_SUCCEEDED,
                "last_result": {
                    "status": SCHEDULER_STATUS_SUCCEEDED,
                    "hits": "not-a-hit-list",
                },
            },
        )
        tmp_repository.save_task(task)
        sched = TopKResearchScheduler(repository=tmp_repository)
        result = sched.recover(task.task_id)
        assert result.status == SCHEDULER_STATUS_FAILED
        assert result.hits == []


class TestSchedulerStats:
    """统计指标计算。"""

    def test_hit_rate_zero_queries(self) -> None:
        """0 查询时命中率为 0。"""
        s = SchedulerStats()
        assert s.hit_rate == 0.0

    def test_hit_rate_nonzero(self) -> None:
        """命中率 = 命中数 / 查询数。"""
        s = SchedulerStats(total_queries=10, total_hits=7)
        assert s.hit_rate == pytest.approx(0.7)

    def test_conversion_rate_zero_hits(self) -> None:
        """0 命中时转化率为 0。"""
        s = SchedulerStats()
        assert s.conversion_rate == 0.0

    def test_conversion_rate_nonzero(self) -> None:
        """转化率 = 有效输入数 / 命中数。"""
        s = SchedulerStats(total_hits=10, effective_inputs=4)
        assert s.conversion_rate == pytest.approx(0.4)

    def test_avg_cost_per_query(self) -> None:
        """单次查询平均成本。"""
        s = SchedulerStats(total_queries=10, total_cost=12.5)
        assert s.avg_cost_per_query == pytest.approx(1.25)

    def test_avg_cost_per_hit(self) -> None:
        """单条命中平均成本。"""
        s = SchedulerStats(total_hits=5, total_cost=12.5)
        assert s.avg_cost_per_hit == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# 默认空检索器测试
# ---------------------------------------------------------------------------


class TestNullSearchExecutor:
    """默认空检索器返回 gap（验收点 3）。"""

    def test_returns_gap_status(self) -> None:
        """未配置检索器返回 gap。"""
        executor = _NullSearchExecutor()
        q = _make_query()
        result = executor.execute(q, TaskBudget(), 30.0)
        assert result.status == SCHEDULER_STATUS_GAP
        assert result.gap_description
        assert result.hits == []


# ---------------------------------------------------------------------------
# 验收点 1：Top-K 选择（轻量筛选，不触发外部检索）
# ---------------------------------------------------------------------------


class TestSelectTopK:
    """Top-K 轻量筛选。"""

    def test_empty_inputs_returns_empty(self, scheduler) -> None:
        """空输入返回空候选列表。"""
        candidates = scheduler.select_topk(as_of="2024-06-30")
        assert candidates == []

    def test_default_top_k(self, scheduler) -> None:
        """默认 top_k = DEFAULT_TOP_K。"""
        assert scheduler.top_k == DEFAULT_TOP_K

    def test_top_k_limit(self, scheduler) -> None:
        """Top-K 上限生效。"""
        # 构造 30 个候选
        funnel = MagicMock()
        funnel.pareto_frontier = [f"S{i:03d}" for i in range(30)]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = scheduler.select_topk(
            funnel_output=funnel, as_of="2024-06-30", k=5
        )
        assert len(candidates) == 5

    def test_rank_assigned(self, scheduler) -> None:
        """候选被赋予 1-based rank。"""
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001", "S002", "S003"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = scheduler.select_topk(
            funnel_output=funnel, as_of="2024-06-30"
        )
        ranks = [c.rank for c in candidates]
        assert ranks == [1, 2, 3]

    def test_topk_symbols_recorded(self, scheduler) -> None:
        """Top-K 公司集合被记录（验收点 1）。"""
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001", "S002"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        assert scheduler._topk_symbols == {"S001", "S002"}


class TestSelectTopKFromFunnel:
    """从 P2.2 漏斗抽取候选。"""

    def test_pareto_priority_0_9(self, scheduler) -> None:
        """Pareto frontier 优先级 0.9。"""
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        assert candidates[0].priority == pytest.approx(0.9)
        assert candidates[0].candidate_source == CANDIDATE_SOURCE_FUNNEL_PARETO

    def test_high_conviction_priority_0_8(self, scheduler) -> None:
        """high_conviction 优先级 0.8。"""
        funnel = MagicMock()
        funnel.pareto_frontier = []
        funnel.high_conviction_symbols = ["S001"]
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        assert candidates[0].priority == pytest.approx(0.8)

    def test_pending_priority_0_5(self, scheduler) -> None:
        """research_pending 优先级 0.5。"""
        funnel = MagicMock()
        funnel.pareto_frontier = []
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = ["S001"]
        funnel.results = []
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        assert candidates[0].priority == pytest.approx(0.5)

    def test_no_duplicate_across_categories(self, scheduler) -> None:
        """同公司出现在多个类别只保留最高优先级。"""
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001"]
        funnel.high_conviction_symbols = ["S001", "S002"]
        funnel.research_pending_symbols = ["S001", "S003"]
        funnel.results = []
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        symbols = [c.symbol for c in candidates]
        assert symbols.count("S001") == 1
        # S001 应来自 Pareto（priority 0.9）
        s001 = next(c for c in candidates if c.symbol == "S001")
        assert s001.priority == pytest.approx(0.9)


class TestSelectTopKFromChangeRadar:
    """从 P2.3 雷达抽取候选。"""

    def test_retrieval_hypothesis_priority_0_85(self, scheduler) -> None:
        """retrieval_hypothesis 事件优先级 0.85。"""
        radar = MagicMock()
        event = MagicMock()
        event.symbol = "S001"
        event.entity_name = "S001"
        event.event_type = "product_price_cost"
        event.observed_at = "2024-06-30"
        event.event_id = "cevt_test"
        radar.list_events_by_status = MagicMock(return_value=[event])
        candidates = scheduler.select_topk(change_radar=radar, as_of="2024-06-30")
        assert len(candidates) == 1
        assert candidates[0].priority == pytest.approx(0.85)
        assert candidates[0].candidate_source == CANDIDATE_SOURCE_CHANGE_RADAR

    def test_radar_exception_degrades_gracefully(self, scheduler) -> None:
        """雷达查询异常降级为空列表。"""
        radar = MagicMock()
        radar.list_events_by_status = MagicMock(side_effect=RuntimeError("boom"))
        candidates = scheduler.select_topk(change_radar=radar, as_of="2024-06-30")
        assert candidates == []

    def test_fallback_to_list_events(self, scheduler) -> None:
        """无 list_events_by_status 时回退到 list_events。"""
        radar = MagicMock()
        radar.list_events_by_status = None  # type: ignore[assignment]
        event = MagicMock()
        event.symbol = "S001"
        event.entity_name = "S001"
        event.event_type = "consensus_revision"
        event.observed_at = "2024-06-30"
        event.event_id = "cevt_test"
        radar.list_events = MagicMock(return_value=[event])
        candidates = scheduler.select_topk(change_radar=radar, as_of="2024-06-30")
        assert len(candidates) == 1


class TestSelectTopKFromGaps:
    """从缺口任务抽取候选。"""

    def test_model_blocking_priority_0_88(
        self, scheduler, gap_planner
    ) -> None:
        """model_blocking=True 的缺口优先级 0.88。"""
        gap_planner.enqueue(
            symbol="S001",
            company_name="公司A",
            as_of="2024-06-30",
            gaps=["forward_profit_bridge_missing"],
        )
        candidates = scheduler.select_topk(as_of="2024-06-30")
        assert len(candidates) == 1
        assert candidates[0].priority == pytest.approx(0.88)
        assert candidates[0].candidate_source == CANDIDATE_SOURCE_GAP_MODEL_BLOCKING

    def test_non_blocking_priority_0_6(
        self, scheduler, gap_planner
    ) -> None:
        """非阻断缺口优先级 0.6。"""
        gap_planner.enqueue(
            symbol="S002",
            company_name="公司B",
            as_of="2024-06-30",
            gaps=["causal_chain_depth_insufficient"],
        )
        candidates = scheduler.select_topk(as_of="2024-06-30")
        assert len(candidates) == 1
        assert candidates[0].priority == pytest.approx(0.6)

    def test_repository_exception_degrades_gracefully(self, scheduler) -> None:
        """仓储查询异常降级为空列表。"""
        scheduler.repository.list_tasks = MagicMock(  # type: ignore[assignment]
            side_effect=RuntimeError("boom")
        )
        candidates = scheduler.select_topk(as_of="2024-06-30")
        assert candidates == []


# ---------------------------------------------------------------------------
# 验收点 2：查询构造
# ---------------------------------------------------------------------------


class TestPlanQuery:
    """查询构造。"""

    def test_query_includes_all_required_fields(self, scheduler) -> None:
        """查询包含目标变量/公司/产品/期间/首选来源。"""
        cand = _make_candidate()
        q = scheduler.plan_query(cand)
        assert q.target_variable
        assert q.company
        assert q.product
        assert q.period
        assert q.preferred_source in SOURCE_ORDER

    def test_query_dedup_key_computed(self, scheduler) -> None:
        """查询去重键已计算。"""
        cand = _make_candidate()
        q = scheduler.plan_query(cand)
        assert q.dedup_key
        assert q.dedup_key.startswith("sq_")

    def test_query_field_degradation(self, scheduler) -> None:
        """字段缺失降级为 unknown 兜底（AGENTS.md）。"""
        cand = TopKCandidate(symbol="S001")
        q = scheduler.plan_query(cand)
        assert q.target_variable == "unknown"
        assert q.company == "S001"  # fallback to symbol
        assert q.product == "整体业务"


# ---------------------------------------------------------------------------
# 验收点 1：Top-K 外公司不发生无界深搜
# ---------------------------------------------------------------------------


class TestExecuteSearchTopKGuard:
    """验收点 1：Top-K 外公司拒绝深搜。"""

    def test_outside_topk_rejected(self, scheduler) -> None:
        """Top-K 外公司深搜被拒绝。"""
        # 设置 Top-K 集合
        scheduler._topk_symbols = {"S001", "S002"}
        cand = _make_candidate(symbol="S999")
        q = _make_query()
        result = scheduler.execute_search(cand, q)
        assert result.status == SCHEDULER_STATUS_CANCELED
        assert "Top-K 外公司" in result.failure_reason
        assert scheduler.stats().denied_by_topk == 1

    def test_inside_topk_allowed(self, scheduler) -> None:
        """Top-K 内公司允许执行。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate(symbol="600519")
        q = _make_query()
        result = scheduler.execute_search(cand, q)
        # NullExecutor 返回 gap
        assert result.status == SCHEDULER_STATUS_GAP
        assert scheduler.stats().denied_by_topk == 0

    def test_empty_topk_allows_all(self, scheduler) -> None:
        """Top-K 集合为空时允许执行（兼容首次启动）。"""
        assert scheduler._topk_symbols == set()
        cand = _make_candidate(symbol="ANY")
        q = _make_query()
        result = scheduler.execute_search(cand, q)
        # 不应因 Top-K 被拒
        assert result.status != SCHEDULER_STATUS_CANCELED or "Top-K" not in result.failure_reason


# ---------------------------------------------------------------------------
# 预算与闲时上限测试
# ---------------------------------------------------------------------------


class TestExecuteSearchBudgetGuard:
    """预算预留与闲时上限。"""

    def test_budget_reserved_and_released(self, scheduler) -> None:
        """执行后预算被释放。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        scheduler.execute_search(cand, q)
        # 预算被释放后 reserved 应接近 0
        snapshot = scheduler.budget_manager.snapshot()
        assert snapshot.active_tasks == 0

    def test_idle_mode_within_cap(self, tmp_repository) -> None:
        """闲时模式预算在 20% 上限内允许。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=_StubExecutor(hits=[SearchHit()]),
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        result = sched.execute_search(cand, q, idle_mode=True)
        # 闲时预算足够，应允许执行
        assert result.status == SCHEDULER_STATUS_SUCCEEDED

    def test_idle_mode_exceeds_cap(self, tmp_repository) -> None:
        """闲时预算超限时拒绝。

        预算消耗必须通过公共 API 完成，因为持久化模式下 snapshot()
        从 SQLite 读取，直接修改 _consumed 不会生效。
        """
        bm = GlobalBudgetManager(repository=tmp_repository)
        # 通过 allocate 公共 API 预先消耗 90% 预算（reserve + consume）
        consumed_90pct = TaskBudget(
            max_requests=int(bm.daily_total.max_requests * 0.9),
            max_seconds=bm.daily_total.max_seconds * 0.9,
            max_tokens=int(bm.daily_total.max_tokens * 0.9),
            max_cost=bm.daily_total.max_cost * 0.9,
            max_documents=int(bm.daily_total.max_documents * 0.9),
        )
        alloc_result = bm.allocate(consumed_90pct)
        assert alloc_result.granted, "预留 90% 预算应成功"
        gp = GapTaskPlanner(repository=tmp_repository)
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        result = sched.execute_search(cand, q, idle_mode=True)
        # 闲时预算已被消耗 90%，远超 20% 上限
        assert result.status == SCHEDULER_STATUS_CANCELED
        assert "闲时预算" in result.failure_reason
        assert sched.stats().denied_by_budget == 1


# ---------------------------------------------------------------------------
# 去重与缓存测试
# ---------------------------------------------------------------------------


class TestExecuteSearchDedup:
    """去重与缓存。"""

    def test_same_dedup_key_second_call_returns_gap(
        self, tmp_repository
    ) -> None:
        """相同 dedup_key 第二次调用直接返回 gap。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        # 第一次执行
        r1 = sched.execute_search(cand, q)
        assert r1.status == SCHEDULER_STATUS_SUCCEEDED
        # 第二次相同 dedup_key
        q2 = _make_query()
        r2 = sched.execute_search(cand, q2)
        assert r2.status == SCHEDULER_STATUS_GAP
        assert "重复查询" in r2.gap_description
        # executor 只被调用一次
        assert len(executor.calls) == 1

    def test_cache_returns_cached_result(self, tmp_repository) -> None:
        """缓存命中返回缓存结果。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
            cache_ttl_seconds=60.0,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        # 第一次执行
        r1 = sched.execute_search(cand, q)
        assert r1.status == SCHEDULER_STATUS_SUCCEEDED
        # 清除 dedup_keys 让缓存生效
        sched._dedup_keys.clear()
        # 第二次应命中缓存
        q2 = _make_query()
        # 重置 query_id 让其与第一次不同
        q2.query_id = "sq_diff"
        r2 = sched.execute_search(cand, q2)
        assert r2.cached is True


# ---------------------------------------------------------------------------
# 验收点 3：搜索失败只产生 gap，不生成事实
# ---------------------------------------------------------------------------


class TestExecuteSearchFailureToGap:
    """验收点 3：失败转 gap。"""

    def test_executor_exception_to_gap(self, tmp_repository) -> None:
        """executor 抛异常转为 gap。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(raise_exc=RuntimeError("network error"))
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        result = sched.execute_search(cand, q)
        assert result.status == SCHEDULER_STATUS_GAP
        assert "检索异常" in result.gap_description
        assert result.hits == []

    def test_null_executor_returns_gap(self, scheduler) -> None:
        """默认空检索器返回 gap（无 hits）。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        result = scheduler.execute_search(cand, q)
        assert result.status == SCHEDULER_STATUS_GAP
        assert result.hits == []
        assert result.gap_description

    def test_invalid_query_returns_failed_not_gap(self, scheduler) -> None:
        """非法查询返回 failed（不是 gap）。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = SearchQuery()  # 全部字段缺失
        result = scheduler.execute_search(cand, q)
        assert result.status == SCHEDULER_STATUS_FAILED
        assert "必填字段缺失" in result.failure_reason

    def test_gap_does_not_become_fact(self, tmp_repository) -> None:
        """gap 不应产生 SearchHit。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(
            status=SCHEDULER_STATUS_GAP, gap_description="no data"
        )
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        result = sched.execute_search(cand, q)
        assert result.status == SCHEDULER_STATUS_GAP
        assert result.hits == []


# ---------------------------------------------------------------------------
# 验收点 2：统计可观察
# ---------------------------------------------------------------------------


class TestExecuteSearchStats:
    """验收点 2：统计可观察。"""

    def test_stats_initial_zero(self, scheduler) -> None:
        """初始统计为 0。"""
        stats = scheduler.stats()
        assert stats.total_queries == 0
        assert stats.total_hits == 0
        assert stats.hit_rate == 0.0

    def test_stats_after_successful_search(self, tmp_repository) -> None:
        """成功检索后统计累加。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(
            hits=[
                SearchHit(cost=0.5),
                SearchHit(cost=0.3),
            ]
        )
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        sched.execute_search(cand, q)
        stats = sched.stats()
        assert stats.total_queries == 1
        assert stats.total_hits == 2
        assert stats.total_cost == pytest.approx(0.8)
        assert stats.hit_rate == pytest.approx(2.0)

    def test_stats_after_gap_search(self, scheduler) -> None:
        """gap 检索累加 gap_count。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        scheduler.execute_search(cand, q)
        stats = scheduler.stats()
        assert stats.total_queries == 1
        assert stats.gap_count == 1
        assert stats.total_hits == 0

    def test_conversion_rate_via_record_effective_input(
        self, tmp_repository
    ) -> None:
        """通过 record_effective_input 记录有效输入。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit(), SearchHit(), SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        sched.execute_search(cand, q)
        sched.record_effective_input(2)
        stats = sched.stats()
        assert stats.total_hits == 3
        assert stats.effective_inputs == 2
        assert stats.conversion_rate == pytest.approx(2 / 3)

    def test_stats_to_dict_includes_derived_metrics(
        self, tmp_repository
    ) -> None:
        """to_dict 包含命中率/转化率/单次成本。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit(cost=1.0)])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        sched.execute_search(cand, q)
        stats_dict = sched.stats().to_dict()
        assert "hit_rate" in stats_dict
        assert "conversion_rate" in stats_dict
        assert "avg_cost_per_query" in stats_dict
        assert "avg_cost_per_hit" in stats_dict


# ---------------------------------------------------------------------------
# 验收点 4：任务重启可恢复且幂等
# ---------------------------------------------------------------------------


class TestRecoverIdempotent:
    """验收点 4：幂等恢复。"""

    def test_recover_new_task_executes_once(self, tmp_repository) -> None:
        """新建恢复任务只执行一次。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        task_id = "rt_recover_test_001"
        # 第一次调用：新建任务
        r1 = sched.recover(task_id, candidate=cand, query=q)
        assert r1.status == SCHEDULER_STATUS_SUCCEEDED
        assert len(executor.calls) == 1
        # 第二次调用相同 task_id：应从 checkpoint 幂等返回
        r2 = sched.recover(task_id, candidate=cand, query=q)
        assert r2.status == SCHEDULER_STATUS_SUCCEEDED
        # executor 仍然只被调用一次
        assert len(executor.calls) == 1

    def test_recover_completed_task_returns_cached(self, tmp_repository) -> None:
        """已完成任务的恢复返回缓存结果。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        task_id = "rt_recover_test_002"
        # 第一次：执行
        sched.recover(task_id, candidate=cand, query=q)
        # 第二次：从 checkpoint 恢复
        r2 = sched.recover(task_id)
        assert r2.status == SCHEDULER_STATUS_SUCCEEDED
        assert r2.cached is True

    def test_recover_no_params_no_existing_fails(self, scheduler) -> None:
        """无参数且无现有任务时失败。"""
        result = scheduler.recover("nonexistent_task_id")
        assert result.status == SCHEDULER_STATUS_FAILED
        assert "candidate" in result.failure_reason

    def test_recover_stats_increments(self, tmp_repository) -> None:
        """recover 调用累加 recovered_tasks 统计。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        sched.recover("rt_recover_test_003", candidate=cand, query=q)
        assert sched.stats().recovered_tasks == 1


class TestRecoverResume:
    """验收点 4：续跑。"""

    def test_recover_from_partial_checkpoint_resumes(
        self, tmp_repository
    ) -> None:
        """从 partial 状态续跑。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        # 预先创建 partial 任务
        task_id = "rt_recover_resume_001"
        task = ResearchTask(
            task_id=task_id,
            task_type="topk_evidence_search",
            status=TaskStatus.PARTIAL.value,
            checkpoint={
                "candidate": cand.to_dict(),
                "query": {
                    "query_id": q.query_id,
                    "target_variable": q.target_variable,
                    "company": q.company,
                    "product": q.product,
                    "period": q.period,
                    "preferred_source": q.preferred_source,
                    "dedup_key": q.dedup_key,
                },
                "scheduler_status": SCHEDULER_STATUS_GAP,
            },
        )
        tmp_repository.save_task(task)
        # 续跑
        # 清除 dedup_keys 避免被去重
        sched._dedup_keys.clear()
        result = sched.recover(task_id)
        assert result.status == SCHEDULER_STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# gap_tasks model_blocking 测试
# ---------------------------------------------------------------------------


class TestGapTaskPlannerModelBlocking:
    """gap_tasks.py model_blocking 标记。"""

    def test_model_blocking_gaps_set(self) -> None:
        """MODEL_BLOCKING_GAPS 包含 3 个阻断缺口。"""
        assert "verified_causal_chain_missing" in MODEL_BLOCKING_GAPS
        assert "forward_profit_bridge_missing" in MODEL_BLOCKING_GAPS
        assert "company_forward_driver_evidence_missing" in MODEL_BLOCKING_GAPS

    def test_is_model_blocking_true_for_blocking_gap(self) -> None:
        """阻断缺口返回 True。"""
        assert GapTaskPlanner.is_model_blocking("forward_profit_bridge_missing")
        assert GapTaskPlanner.is_model_blocking("verified_causal_chain_missing")
        assert GapTaskPlanner.is_model_blocking(
            "company_forward_driver_evidence_missing"
        )

    def test_is_model_blocking_false_for_non_blocking_gap(self) -> None:
        """非阻断缺口返回 False。"""
        assert not GapTaskPlanner.is_model_blocking(
            "causal_chain_depth_insufficient"
        )
        assert not GapTaskPlanner.is_model_blocking(
            "verified_market_expectation_missing"
        )

    def test_enqueue_marks_model_blocking(
        self, tmp_repository, gap_planner
    ) -> None:
        """enqueue 为阻断缺口标记 model_blocking=True。"""
        gap_planner.enqueue(
            symbol="S001",
            company_name="公司A",
            as_of="2024-06-30",
            gaps=["forward_profit_bridge_missing"],
        )
        tasks = tmp_repository.list_tasks(limit=10)
        assert len(tasks) == 1
        assert tasks[0].params["model_blocking"] is True

    def test_enqueue_marks_non_blocking(
        self, tmp_repository, gap_planner
    ) -> None:
        """enqueue 为非阻断缺口标记 model_blocking=False。"""
        gap_planner.enqueue(
            symbol="S002",
            company_name="公司B",
            as_of="2024-06-30",
            gaps=["causal_chain_depth_insufficient"],
        )
        tasks = tmp_repository.list_tasks(limit=10)
        assert len(tasks) == 1
        assert tasks[0].params["model_blocking"] is False


# ---------------------------------------------------------------------------
# budget_manager 闲时上限 API 测试
# ---------------------------------------------------------------------------


class TestBudgetManagerIdleCap:
    """budget_manager.py 闲时上限 API。"""

    def test_default_idle_budget_ratio(self, budget_manager) -> None:
        """默认闲时预算比例为 0.2。"""
        assert budget_manager.DEFAULT_IDLE_BUDGET_RATIO == 0.2

    def test_idle_budget_cap_default(self, budget_manager) -> None:
        """默认闲时上限为总预算的 20%。"""
        cap = budget_manager.idle_budget_cap()
        assert cap.max_requests == int(budget_manager.daily_total.max_requests * 0.2)
        assert cap.max_cost == pytest.approx(
            budget_manager.daily_total.max_cost * 0.2
        )

    def test_idle_budget_cap_custom_ratio(self, budget_manager) -> None:
        """自定义比例生效。"""
        cap = budget_manager.idle_budget_cap(0.1)
        assert cap.max_requests == int(
            budget_manager.daily_total.max_requests * 0.1
        )

    def test_idle_budget_cap_ratio_clamped(self, budget_manager) -> None:
        """比例夹紧到 [0, 1]。"""
        cap_neg = budget_manager.idle_budget_cap(-1.0)
        assert cap_neg.max_requests == 0
        cap_big = budget_manager.idle_budget_cap(2.0)
        # 比例 > 1 被夹紧到 1，即等于总预算
        assert cap_big.max_requests == budget_manager.daily_total.max_requests

    def test_idle_budget_remaining_initial(self, budget_manager) -> None:
        """初始闲时剩余等于上限。"""
        cap = budget_manager.idle_budget_cap()
        remaining = budget_manager.idle_budget_remaining()
        assert remaining.max_requests == cap.max_requests

    def test_is_idle_budget_within_cap_true(self, budget_manager) -> None:
        """小请求在闲时上限内。"""
        small = TaskBudget(max_requests=10, max_cost=1.0)
        assert budget_manager.is_idle_budget_within_cap(small)

    def test_is_idle_budget_within_cap_false(self, budget_manager) -> None:
        """超出闲时上限的请求返回 False。"""
        # 模拟已消耗预算
        budget_manager._consumed = TaskBudget(
            max_requests=int(budget_manager.daily_total.max_requests * 0.5),
            max_seconds=budget_manager.daily_total.max_seconds * 0.5,
            max_tokens=int(budget_manager.daily_total.max_tokens * 0.5),
            max_cost=budget_manager.daily_total.max_cost * 0.5,
            max_documents=int(budget_manager.daily_total.max_documents * 0.5),
        )
        # 闲时上限已被 50% 消耗 > 20% 上限
        big = TaskBudget(max_requests=500, max_cost=200.0)
        assert not budget_manager.is_idle_budget_within_cap(big)


# ---------------------------------------------------------------------------
# 验收点综合测试
# ---------------------------------------------------------------------------


class TestAcceptancePoint1:
    """验收点 1 综合：Top-K 外公司不发生无界深搜。"""

    def test_topk_filter_before_deep_search(self, tmp_repository) -> None:
        """综合：先 Top-K 筛选，再执行深搜。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        # 构造漏斗：5 家公司，top_k=3
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001", "S002", "S003", "S004", "S005"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = sched.select_topk(funnel_output=funnel, as_of="2024-06-30", k=3)
        assert len(candidates) == 3
        # Top-K 外公司深搜应被拒
        outside_cand = _make_candidate(symbol="S999")
        q = _make_query()
        r = sched.execute_search(outside_cand, q)
        assert r.status == SCHEDULER_STATUS_CANCELED
        assert "Top-K 外公司" in r.failure_reason
        assert len(executor.calls) == 0  # executor 未被调用

    def test_topk_inside_company_allowed(self, tmp_repository) -> None:
        """综合：Top-K 内公司允许深搜。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        sched.select_topk(funnel_output=funnel, as_of="2024-06-30")
        cand = _make_candidate(symbol="S001")
        q = _make_query()
        r = sched.execute_search(cand, q)
        assert r.status == SCHEDULER_STATUS_SUCCEEDED
        assert len(executor.calls) == 1

    def test_no_unbounded_search_without_topk(self, tmp_repository) -> None:
        """综合：未调用 select_topk 时仍可执行（兼容首次启动）。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        # 不调用 select_topk，直接执行
        cand = _make_candidate()
        q = _make_query()
        r = sched.execute_search(cand, q)
        # 不应因 Top-K 被拒
        assert r.status in (SCHEDULER_STATUS_SUCCEEDED, SCHEDULER_STATUS_GAP)


class TestAcceptancePoint2:
    """验收点 2 综合：证据命中率、有效输入转化率和单次成本可统计。"""

    def test_full_stats_flow(self, tmp_repository) -> None:
        """综合：5 次查询，3 次成功，2 次 gap，统计正确。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        # executor 前 3 次成功，后 2 次失败
        executor = _StubExecutor(hits=[SearchHit(cost=1.0)])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"S001", "S002", "S003", "S004", "S005"}

        # 3 次成功
        for i in range(3):
            cand = _make_candidate(symbol=f"S00{i+1}")
            q = _make_query(company=f"公司{i}", target_variable=f"var{i}")
            sched.execute_search(cand, q)

        # 2 次 gap（模拟失败）
        executor.status = SCHEDULER_STATUS_GAP
        executor.gap_description = "no data"
        executor.hits = []
        for i in range(2):
            cand = _make_candidate(symbol=f"S00{i+4}")
            q = _make_query(company=f"公司Gap{i}", target_variable=f"gapvar{i}")
            sched.execute_search(cand, q)

        stats = sched.stats()
        assert stats.total_queries == 5
        assert stats.total_hits == 3  # 3 次成功，每次 1 个 hit
        assert stats.gap_count == 2
        assert stats.total_cost == pytest.approx(3.0)
        assert stats.hit_rate == pytest.approx(3 / 5)
        assert stats.avg_cost_per_query == pytest.approx(3.0 / 5)
        assert stats.avg_cost_per_hit == pytest.approx(1.0)


class TestAcceptancePoint3:
    """验收点 3 综合：搜索失败只产生 gap，不生成事实。"""

    def test_failure_never_produces_hits(self, tmp_repository) -> None:
        """综合：所有失败场景均不产生 hits。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(
            status=SCHEDULER_STATUS_GAP, gap_description="模拟失败"
        )
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        r = sched.execute_search(cand, q)
        assert r.status == SCHEDULER_STATUS_GAP
        assert r.hits == []
        assert r.gap_description

    def test_exception_produces_gap_not_fact(self, tmp_repository) -> None:
        """综合：异常产生 gap 而非事实。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(raise_exc=ValueError("boom"))
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        r = sched.execute_search(cand, q)
        assert r.status == SCHEDULER_STATUS_GAP
        assert r.hits == []

    def test_topk_outside_canceled_no_hits(self, tmp_repository) -> None:
        """综合：Top-K 外公司被取消时不产生 hits。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"S001"}
        cand = _make_candidate(symbol="S999")
        q = _make_query()
        r = sched.execute_search(cand, q)
        assert r.status == SCHEDULER_STATUS_CANCELED
        assert r.hits == []
        assert len(executor.calls) == 0


class TestAcceptancePoint4:
    """验收点 4 综合：任务重启后可恢复且幂等。"""

    def test_recover_idempotent_multiple_calls(self, tmp_repository) -> None:
        """综合：多次调用 recover 同一 task_id 只执行一次。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        task_id = "rt_acceptance4_001"

        # 多次调用
        r1 = sched.recover(task_id, candidate=cand, query=q)
        r2 = sched.recover(task_id)
        r3 = sched.recover(task_id)

        assert r1.status == SCHEDULER_STATUS_SUCCEEDED
        assert r2.status == SCHEDULER_STATUS_SUCCEEDED
        assert r3.status == SCHEDULER_STATUS_SUCCEEDED
        assert r2.cached is True
        assert r3.cached is True
        # executor 只调用一次
        assert len(executor.calls) == 1

    def test_recover_after_restart(self, tmp_repository) -> None:
        """综合：模拟进程重启后恢复任务。"""
        bm1 = GlobalBudgetManager(repository=tmp_repository)
        gp1 = GapTaskPlanner(repository=tmp_repository)
        executor1 = _StubExecutor(hits=[SearchHit()])
        sched1 = TopKResearchScheduler(
            budget_manager=bm1,
            gap_planner=gp1,
            repository=tmp_repository,
            executor=executor1,
        )
        sched1._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        task_id = "rt_acceptance4_002"
        # 第一次执行
        sched1.recover(task_id, candidate=cand, query=q)

        # 模拟重启：新建调度器（同一仓储）
        bm2 = GlobalBudgetManager(repository=tmp_repository)
        gp2 = GapTaskPlanner(repository=tmp_repository)
        executor2 = _StubExecutor(hits=[SearchHit()])
        sched2 = TopKResearchScheduler(
            budget_manager=bm2,
            gap_planner=gp2,
            repository=tmp_repository,
            executor=executor2,
        )
        sched2._topk_symbols = {"600519"}
        # 重启后调用 recover
        r = sched2.recover(task_id)
        assert r.status == SCHEDULER_STATUS_SUCCEEDED
        assert r.cached is True
        # 第二个 executor 不应被调用（从 checkpoint 恢复）
        assert len(executor2.calls) == 0


# ---------------------------------------------------------------------------
# AGENTS.md 合规性测试
# ---------------------------------------------------------------------------


class TestGraceful:
    """字段缺失降级处理（AGENTS.md）。"""

    def test_funnel_missing_fields_no_crash(self, scheduler) -> None:
        """漏斗字段缺失不崩溃。"""
        funnel = MagicMock()
        funnel.pareto_frontier = None  # type: ignore[assignment]
        funnel.high_conviction_symbols = None  # type: ignore[assignment]
        funnel.research_pending_symbols = None  # type: ignore[assignment]
        funnel.results = None  # type: ignore[assignment]
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        assert candidates == []

    def test_change_radar_missing_methods_no_crash(self, scheduler) -> None:
        """雷达无查询方法时不崩溃。"""
        radar = object()  # 无 list_events_by_status / list_events
        candidates = scheduler.select_topk(change_radar=radar, as_of="2024-06-30")
        assert candidates == []

    def test_repository_failure_degrades(self, scheduler) -> None:
        """仓储查询失败降级。"""
        scheduler.repository.list_tasks = MagicMock(  # type: ignore[assignment]
            side_effect=RuntimeError("db error")
        )
        candidates = scheduler.select_topk(as_of="2024-06-30")
        assert candidates == []

    def test_budget_snapshot_failure_degrades(self, tmp_repository) -> None:
        """预算快照失败时闲时检查放行。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        bm.snapshot = MagicMock(side_effect=RuntimeError("db error"))  # type: ignore[assignment]
        gp = GapTaskPlanner(repository=tmp_repository)
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
        )
        # 应放行（不阻塞）
        assert sched._is_idle_budget_within_cap(TaskBudget(max_requests=1)) is True


class TestNoTradeAdvice:
    """AGENTS.md 合规性：不输出投资建议。"""

    FORBIDDEN_KEYWORDS = (
        "买入",
        "卖出",
        "建仓",
        "清仓",
        "加仓",
        "减仓",
        "止盈",
        "止损",
        "target_price",
        "price_target",
    )

    def _check_no_advice(self, text: str) -> None:
        """检查文本不含投资建议关键词。"""
        for kw in self.FORBIDDEN_KEYWORDS:
            assert kw not in text, f"发现禁用关键词 {kw}: {text}"

    def test_source_tier_labels_no_advice(self) -> None:
        """来源层级标签不含投资建议。"""
        from core.research.discovery.research_scheduler import SOURCE_TIER_LABELS

        for label in SOURCE_TIER_LABELS.values():
            self._check_no_advice(label)

    def test_candidate_reason_no_advice(self, scheduler) -> None:
        """候选理由不含投资建议。"""
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = scheduler.select_topk(funnel_output=funnel, as_of="2024-06-30")
        for c in candidates:
            self._check_no_advice(c.reason)

    def test_gap_description_no_advice(self, scheduler) -> None:
        """gap 描述不含投资建议。"""
        scheduler._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        r = scheduler.execute_search(cand, q)
        if r.gap_description:
            self._check_no_advice(r.gap_description)

    def test_failure_reason_no_advice(self, scheduler) -> None:
        """失败原因不含投资建议。"""
        scheduler._topk_symbols = {"S001"}
        cand = _make_candidate(symbol="S999")
        q = _make_query()
        r = scheduler.execute_search(cand, q)
        self._check_no_advice(r.failure_reason)


# ---------------------------------------------------------------------------
# 端到端集成测试
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端集成。"""

    def test_full_pipeline(self, tmp_repository) -> None:
        """完整流水线：漏斗 → Top-K → 查询 → 检索 → 统计。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit(cost=0.5)])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        # 1. 漏斗输出
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001", "S002"]
        funnel.high_conviction_symbols = ["S003"]
        funnel.research_pending_symbols = ["S004"]
        funnel.results = []
        # 2. Top-K 选择
        candidates = sched.select_topk(funnel_output=funnel, as_of="2024-06-30", k=3)
        assert len(candidates) == 3
        # 3. 为每个候选构造查询并执行
        for cand in candidates:
            q = sched.plan_query(cand)
            r = sched.execute_search(cand, q)
            assert r.status in (SCHEDULER_STATUS_SUCCEEDED, SCHEDULER_STATUS_GAP)
        # 4. 统计
        stats = sched.stats()
        assert stats.total_queries == 3
        assert stats.total_hits == 3  # 3 次成功，每次 1 个 hit
        assert stats.total_cost == pytest.approx(1.5)

    def test_pipeline_with_gaps_and_recovery(self, tmp_repository) -> None:
        """完整流水线：含 gap 与恢复。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit(cost=0.3)])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        funnel = MagicMock()
        funnel.pareto_frontier = ["S001"]
        funnel.high_conviction_symbols = []
        funnel.research_pending_symbols = []
        funnel.results = []
        candidates = sched.select_topk(funnel_output=funnel, as_of="2024-06-30")
        cand = candidates[0]
        q = sched.plan_query(cand)
        # 第一次执行
        r1 = sched.execute_search(cand, q, task_id="rt_e2e_001")
        assert r1.status == SCHEDULER_STATUS_SUCCEEDED
        # 通过 recover 恢复（应从 checkpoint 返回）
        r2 = sched.recover("rt_e2e_001")
        assert r2.status == SCHEDULER_STATUS_SUCCEEDED
        assert r2.cached is True

    def test_build_default_scheduler(self, tmp_repository) -> None:
        """build_default_scheduler 工厂函数。"""
        sched = build_default_scheduler(repository=tmp_repository)
        assert isinstance(sched, TopKResearchScheduler)
        assert sched.executor is not None
        # 默认空检索器返回 gap
        q = _make_query()
        r = sched.executor.execute(q, TaskBudget(), 30.0)
        assert r.status == SCHEDULER_STATUS_GAP

    def test_clear_cache(self, tmp_repository) -> None:
        """clear_cache 清空缓存与 dedup_keys。"""
        bm = GlobalBudgetManager(repository=tmp_repository)
        gp = GapTaskPlanner(repository=tmp_repository)
        executor = _StubExecutor(hits=[SearchHit()])
        sched = TopKResearchScheduler(
            budget_manager=bm,
            gap_planner=gp,
            repository=tmp_repository,
            executor=executor,
        )
        sched._topk_symbols = {"600519"}
        cand = _make_candidate()
        q = _make_query()
        sched.execute_search(cand, q)
        assert sched._dedup_keys
        assert sched._query_cache
        sched.clear_cache()
        assert not sched._dedup_keys
        assert not sched._query_cache
