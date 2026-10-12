"""P2.4 Top-K 主动证据检索调度器。

按 P2 计划 §4 Task P2.4：
- 轻量筛选后才执行深检索（基于 P2.2 漏斗与 P2.3 雷达候选）
- 优先补阻断盈利模型的证据，而不是泛搜更多新闻
- 查询必须包含目标变量、公司、产品、期间和首选来源
- 先第一方，再可信行业/券商，再公开网页
- 预算、并发、缓存、去重、超时和停止条件显式化
- 闲时预算不超过总预算 20%

来源优先级（P2.4 §172）：
    first_party        公司公告、年报、投关页、交易所披露
    industry_broker    行业协会、第三方研究机构、券商研报
    public_web         公开网页、新闻媒体、社交平台

调度流程：
    1. select_topk()：轻量筛选 Top-K 候选（不调用任何外部检索）
       - 来源 1：P2.2 FunnelOutput.pareto_frontier / high_conviction_symbols
       - 来源 2：P2.3 ChangeRadar 中 status=retrieval_hypothesis 的事件
       - 来源 3：P2.x GapTaskPlanner 中 model_blocking=True 的缺口
    2. plan_query()：为每个候选构造 SearchQuery（含目标变量/公司/产品/期间/首选来源）
    3. execute_search()：按预算/并发/超时执行检索；失败只产生 gap，不生成事实
    4. recover()：基于 ResearchTask.checkpoint 幂等恢复
    5. stats()：统计命中率、有效输入转化率、单次成本

硬门禁（AGENTS.md + P2.4）：
- Top-K 外公司不发生无界深搜（验收点 1）
- 搜索失败只产生 gap，不生成事实（验收点 3）
- 任务重启可恢复且幂等（验收点 4）
- 不输出投资建议
- 字段缺失降级处理
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

from core.research.control_plane.contracts import ResearchTask, TaskBudget, TaskStatus
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.budget_manager import GlobalBudgetManager
from core.research.value_chain.gap_tasks import GapTaskPlanner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 来源优先级（P2.4 §172：先第一方，再可信行业/券商，再公开网页）
# ---------------------------------------------------------------------------

SOURCE_TIER_FIRST_PARTY: str = "first_party"
SOURCE_TIER_INDUSTRY_BROKER: str = "industry_broker"
SOURCE_TIER_PUBLIC_WEB: str = "public_web"

SOURCE_ORDER: tuple[str, ...] = (
    SOURCE_TIER_FIRST_PARTY,
    SOURCE_TIER_INDUSTRY_BROKER,
    SOURCE_TIER_PUBLIC_WEB,
)

SOURCE_TIER_LABELS: dict[str, str] = {
    SOURCE_TIER_FIRST_PARTY: "公司公告/年报/投关页/交易所披露",
    SOURCE_TIER_INDUSTRY_BROKER: "行业协会/第三方研究/券商研报",
    SOURCE_TIER_PUBLIC_WEB: "公开网页/新闻媒体/社交平台",
}


# ---------------------------------------------------------------------------
# 候选来源（用于 Top-K 选择）
# ---------------------------------------------------------------------------

CANDIDATE_SOURCE_FUNNEL_PARETO: str = "funnel_pareto"
CANDIDATE_SOURCE_FUNNEL_HIGH_CONV: str = "funnel_high_conviction"
CANDIDATE_SOURCE_CHANGE_RADAR: str = "change_radar_retrieval_hypothesis"
CANDIDATE_SOURCE_GAP_MODEL_BLOCKING: str = "gap_model_blocking"


# ---------------------------------------------------------------------------
# 任务状态（与 ResearchTask.status 区分；此处是调度器内部状态）
# ---------------------------------------------------------------------------

SCHEDULER_STATUS_PENDING: str = "pending"
SCHEDULER_STATUS_RESERVED: str = "reserved"
SCHEDULER_STATUS_SEARCHING: str = "searching"
SCHEDULER_STATUS_SUCCEEDED: str = "succeeded"
SCHEDULER_STATUS_FAILED: str = "failed"
SCHEDULER_STATUS_GAP: str = "gap"  # 搜索失败只产生 gap
SCHEDULER_STATUS_CANCELED: str = "canceled"

SCHEDULER_TERMINAL_STATUSES: tuple[str, ...] = (
    SCHEDULER_STATUS_SUCCEEDED,
    SCHEDULER_STATUS_FAILED,
    SCHEDULER_STATUS_GAP,
    SCHEDULER_STATUS_CANCELED,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_IDLE_BUDGET_RATIO: float = 0.2  # P2.4 §174：闲时预算不超过总预算 20%
DEFAULT_TOP_K: int = 20
DEFAULT_CONCURRENCY: int = 4
DEFAULT_TIMEOUT_SECONDS: float = 30.0
DEFAULT_CACHE_TTL_SECONDS: float = 300.0


def _safe_text(value: Any, default: str = "") -> str:
    """将外部字段降级为稳定文本，避免边界输入击穿调度器。"""
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _safe_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """解析有限浮点数，并将非法值夹到安全范围。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = float(default)
    if not isfinite(parsed):
        parsed = float(default)
    if minimum is not None:
        parsed = max(float(minimum), parsed)
    if maximum is not None:
        parsed = min(float(maximum), parsed)
    return parsed


def _safe_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    """解析整数；不接受 NaN/无穷和不可解析字符串。"""
    try:
        if isinstance(value, str) and not value.strip():
            raise ValueError
        parsed_float = float(value)
        if not isfinite(parsed_float):
            raise ValueError
        parsed = int(parsed_float)
    except (TypeError, ValueError, OverflowError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    return parsed


def _items(value: Any) -> tuple[Any, ...]:
    """安全地读取可迭代输入；字符串和映射按单个对象处理。"""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    try:
        return tuple(value)
    except Exception:
        return ()


def _field_value(item: Any, name: str, default: Any = "") -> Any:
    """同时支持 dataclass/普通对象和映射输入。"""
    if isinstance(item, Mapping):
        return item.get(name, default)
    try:
        return getattr(item, name, default)
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """解析常见布尔表示，避免 ``bool('false')`` 的反直觉结果。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "否", ""}:
            return False
    if value is None:
        return default
    return bool(value)


def _utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass
class TopKCandidate:
    """Top-K 候选公司。

    由 select_topk() 产生，是轻量筛选阶段的输出，不触发任何外部检索。

    Attributes:
        symbol: 公司代码
        company_name: 公司名
        as_of: PIT 时点
        rank: 候选排名（1-based）
        candidate_source: 候选来源（CANDIDATE_SOURCE_* 之一）
        reason: 入选理由（可解释到字段/事件/gap）
        target_variable: 目标变量（如 "asp" / "收入增速" / "毛利率"）
        product: 相关产品/业务（可空）
        period: 期间（YYYY-MM 或 YYYY-Qn）
        preferred_source: 首选来源（SOURCE_TIER_* 之一）
        priority: 优先级 [0, 1]，越高越优先
        metadata: 附加元数据
    """

    symbol: str = ""
    company_name: str = ""
    as_of: str = ""
    rank: int = 0
    candidate_source: str = ""
    reason: str = ""
    target_variable: str = ""
    product: str = ""
    period: str = ""
    preferred_source: str = SOURCE_TIER_FIRST_PARTY
    priority: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": _safe_text(self.symbol),
            "company_name": _safe_text(self.company_name),
            "as_of": _safe_text(self.as_of),
            "rank": _safe_int(self.rank, 0, minimum=0),
            "candidate_source": _safe_text(self.candidate_source),
            "reason": _safe_text(self.reason),
            "target_variable": _safe_text(self.target_variable),
            "product": _safe_text(self.product),
            "period": _safe_text(self.period),
            "preferred_source": _safe_text(self.preferred_source),
            "priority": _safe_float(self.priority, 0.0, minimum=0.0, maximum=1.0),
            "metadata": dict(self.metadata) if isinstance(self.metadata, Mapping) else {},
        }


@dataclass
class SearchQuery:
    """检索查询。

    按验收点（P2.4 §171）：「查询必须包含目标变量、公司、产品、期间和首选来源」。
    任何必填字段缺失则视为非法查询，调度器拒绝执行。

    Attributes:
        query_id: 查询唯一 ID
        candidate: 关联的 Top-K 候选
        target_variable: 目标变量（必填）
        company: 公司名（必填）
        product: 产品/业务（必填，可填 "整体业务" 兜底）
        period: 期间（必填）
        preferred_source: 首选来源（必填，SOURCE_TIER_* 之一）
        source_tiers: 来源层级顺序（默认 SOURCE_ORDER）
        keywords: 关键词列表
        required_evidence_kind: 期望证据类型（quantitative / qualitative / guidance）
        timeout_seconds: 超时秒数
        max_documents: 单次最大文档数
        dedup_key: 去重键（避免相同查询重复执行）
    """

    query_id: str = field(default_factory=lambda: _new_id("sq"))
    target_variable: str = ""
    company: str = ""
    product: str = ""
    period: str = ""
    preferred_source: str = SOURCE_TIER_FIRST_PARTY
    source_tiers: tuple[str, ...] = SOURCE_ORDER
    keywords: list[str] = field(default_factory=list)
    required_evidence_kind: str = "quantitative"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_documents: int = 10
    dedup_key: str = ""

    def validate(self) -> list[str]:
        """校验必填字段。

        Returns:
            错误信息列表；空列表表示合法
        """
        errors: list[str] = []
        if not _safe_text(self.target_variable):
            errors.append("target_variable 缺失（查询必须包含目标变量）")
        if not _safe_text(self.company):
            errors.append("company 缺失（查询必须包含公司）")
        if not _safe_text(self.product):
            errors.append("product 缺失（查询必须包含产品）")
        if not _safe_text(self.period):
            errors.append("period 缺失（查询必须包含期间）")
        preferred_source = _safe_text(self.preferred_source)
        if not preferred_source:
            errors.append("preferred_source 缺失（查询必须包含首选来源）")
        elif preferred_source not in SOURCE_ORDER:
            errors.append(
                f"非法 preferred_source: {preferred_source}; "
                f"允许值: {SOURCE_ORDER}"
            )
        timeout = _safe_float(self.timeout_seconds, float("nan"))
        if not isfinite(timeout) or timeout < 0:
            errors.append("timeout_seconds 必须是非负有限数")
        try:
            documents = float(self.max_documents)
            valid_documents = isfinite(documents) and documents >= 0
        except (TypeError, ValueError, OverflowError):
            valid_documents = False
        if not valid_documents:
            errors.append("max_documents 必须是非负有限数")
        return errors

    def compute_dedup_key(self) -> str:
        """计算去重键。

        相同 (target_variable, company, product, period, preferred_source) 视为重复查询。

        Returns:
            去重键字符串
        """
        content = "|".join(
            (
                _safe_text(self.target_variable),
                _safe_text(self.company),
                _safe_text(self.product),
                _safe_text(self.period),
                _safe_text(self.preferred_source),
            )
        )
        return f"sq_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"


@dataclass
class SearchHit:
    """单条检索命中。

    Attributes:
        hit_id: 命中唯一 ID
        source_tier: 来源层级
        source_ref: 来源引用（url / claim_id / doc_id）
        title: 标题
        snippet: 摘要
        published_at: 发布时间
        retrieved_at: 检索时间
        confidence: 置信度 [0, 1]
        is_independent: 是否为独立来源
        cost: 单条成本（美元）
        metadata: 附加元数据
    """

    hit_id: str = field(default_factory=lambda: _new_id("hit"))
    source_tier: str = ""
    source_ref: str = ""
    title: str = ""
    snippet: str = ""
    published_at: str = ""
    retrieved_at: str = field(default_factory=_utc_now)
    confidence: float = 0.0
    is_independent: bool = True
    cost: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """单次检索结果。

    验收点 3「搜索失败只产生 gap，不生成事实」：
    - status=failed/gap 时不返回 SearchHit（hits 为空）
    - status=succeeded 时 hits 至少 1 条
    - gap_description 仅在 status=gap 时填充

    Attributes:
        query_id: 关联的查询 ID
        status: 调度状态（SCHEDULER_STATUS_* 之一）
        hits: 命中列表
        gap_description: 缺口描述（status=gap 时填充）
        failure_reason: 失败原因（status=failed 时填充）
        consumed_budget: 实际消耗预算
        elapsed_seconds: 耗时
        cached: 是否命中缓存
        started_at: 开始时间
        finished_at: 结束时间
        reservation_id: 预算预留 ID（用于审计）
    """

    query_id: str = ""
    status: str = SCHEDULER_STATUS_PENDING
    hits: list[SearchHit] = field(default_factory=list)
    gap_description: str = ""
    failure_reason: str = ""
    consumed_budget: TaskBudget = field(default_factory=TaskBudget)
    elapsed_seconds: float = 0.0
    cached: bool = False
    started_at: str = ""
    finished_at: str = ""
    reservation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        serialized_hits: list[dict[str, Any]] = []
        for hit in _items(self.hits):
            if isinstance(hit, SearchHit):
                serialized_hits.append(deepcopy(hit.__dict__))
            elif isinstance(hit, Mapping):
                serialized_hits.append(dict(hit))
        return {
            "query_id": _safe_text(self.query_id),
            "status": _safe_text(self.status, SCHEDULER_STATUS_PENDING),
            "hits": serialized_hits,
            "gap_description": _safe_text(self.gap_description),
            "failure_reason": _safe_text(self.failure_reason),
            "consumed_budget": _coerce_budget(self.consumed_budget).to_dict(),
            "elapsed_seconds": _safe_float(self.elapsed_seconds, 0.0, minimum=0.0),
            "cached": bool(self.cached),
            "started_at": _safe_text(self.started_at),
            "finished_at": _safe_text(self.finished_at),
            "reservation_id": _safe_text(self.reservation_id),
        }


def _coerce_budget(value: Any) -> TaskBudget:
    """将 executor 的预算回传统一为经过边界清洗的 TaskBudget。"""
    if isinstance(value, TaskBudget):
        return TaskBudget.from_dict(value.to_dict())
    if isinstance(value, Mapping):
        return TaskBudget.from_dict(dict(value))
    return TaskBudget()


def _coerce_hit(value: Any) -> SearchHit | None:
    """只接纳可序列化的命中，坏命中直接丢弃。"""
    if isinstance(value, SearchHit):
        try:
            hit = deepcopy(value)
        except Exception:
            return None
    elif isinstance(value, Mapping):
        allowed = {
            "hit_id",
            "source_tier",
            "source_ref",
            "title",
            "snippet",
            "published_at",
            "retrieved_at",
            "confidence",
            "is_independent",
            "cost",
            "metadata",
        }
        try:
            hit = SearchHit(**{key: value[key] for key in allowed if key in value})
        except (TypeError, ValueError):
            return None
    else:
        return None

    return replace(
        hit,
        hit_id=_safe_text(hit.hit_id, _new_id("hit")),
        source_tier=_safe_text(hit.source_tier),
        source_ref=_safe_text(hit.source_ref),
        title=_safe_text(hit.title),
        snippet=_safe_text(hit.snippet),
        published_at=_safe_text(hit.published_at),
        retrieved_at=_safe_text(hit.retrieved_at, _utc_now()),
        confidence=_safe_float(hit.confidence, 0.0, minimum=0.0, maximum=1.0),
        is_independent=_safe_bool(hit.is_independent, True),
        cost=_safe_float(hit.cost, 0.0, minimum=0.0),
        metadata=dict(hit.metadata) if isinstance(hit.metadata, Mapping) else {},
    )


def _normalise_result(value: Any, query_id: str) -> SearchResult:
    """清洗 executor 输出，确保失败状态永远不会携带事实命中。"""
    if isinstance(value, SearchResult):
        try:
            result = deepcopy(value)
        except Exception:
            result = SearchResult(query_id=query_id)
    elif isinstance(value, Mapping):
        result = SearchResult(
            query_id=_safe_text(value.get("query_id"), query_id),
            status=_safe_text(value.get("status"), SCHEDULER_STATUS_GAP),
            hits=list(_items(value.get("hits"))),
            gap_description=_safe_text(value.get("gap_description")),
            failure_reason=_safe_text(value.get("failure_reason")),
            consumed_budget=_coerce_budget(value.get("consumed_budget")),
            elapsed_seconds=_safe_float(
                value.get("elapsed_seconds"), 0.0, minimum=0.0
            ),
            cached=_safe_bool(value.get("cached")),
            started_at=_safe_text(value.get("started_at")),
            finished_at=_safe_text(value.get("finished_at")),
            reservation_id=_safe_text(value.get("reservation_id")),
        )
    else:
        return SearchResult(
            query_id=query_id,
            status=SCHEDULER_STATUS_GAP,
            gap_description="检索器未返回有效结果",
        )

    result.query_id = _safe_text(result.query_id, query_id)
    status = _safe_text(result.status, SCHEDULER_STATUS_GAP).lower()
    result.consumed_budget = _coerce_budget(result.consumed_budget)
    result.elapsed_seconds = _safe_float(result.elapsed_seconds, 0.0, minimum=0.0)
    result.hits = [
        hit
        for raw_hit in _items(result.hits)
        if (hit := _coerce_hit(raw_hit)) is not None
    ]

    if status == SCHEDULER_STATUS_SUCCEEDED and result.hits:
        result.status = status
        result.gap_description = ""
        result.failure_reason = ""
        return result

    # 失败/取消/未知状态统一清空 hits，防止把不可信返回写成事实。
    result.hits = []
    if status == SCHEDULER_STATUS_SUCCEEDED:
        result.status = SCHEDULER_STATUS_GAP
        result.gap_description = "检索器声明成功但未返回有效命中"
        result.failure_reason = ""
    elif status == SCHEDULER_STATUS_FAILED:
        result.status = status
        result.failure_reason = _safe_text(result.failure_reason, "检索失败")
        result.gap_description = ""
    elif status == SCHEDULER_STATUS_CANCELED:
        result.status = status
        result.failure_reason = _safe_text(result.failure_reason, "检索已取消")
        result.gap_description = ""
    else:
        result.status = SCHEDULER_STATUS_GAP
        result.gap_description = _safe_text(
            result.gap_description, "检索未产生有效证据"
        )
        result.failure_reason = ""
    return result


@dataclass
class SchedulerStats:
    """调度器统计指标。

    验收点 2「证据命中率、有效输入转化率和单次成本可统计」：
    - hit_rate = total_hits / total_queries
    - conversion_rate = effective_inputs / total_hits
    - avg_cost_per_query = total_cost / total_queries
    - avg_cost_per_hit = total_cost / total_hits

    Attributes:
        total_candidates: Top-K 候选总数
        total_queries: 实际执行查询数
        total_hits: 命中总数
        effective_inputs: 有效输入数（被 P1 模型采纳的命中）
        total_cost: 总成本（美元）
        total_documents: 总文档数
        total_tokens: 总 token 数
        gap_count: gap 数（搜索失败转 gap）
        cache_hit_count: 缓存命中数
        denied_by_budget: 因预算不足被拒数
        denied_by_topk: 因 Top-K 外被拒数
        recovered_tasks: 恢复任务数
    """

    total_candidates: int = 0
    total_queries: int = 0
    total_hits: int = 0
    effective_inputs: int = 0
    total_cost: float = 0.0
    total_documents: int = 0
    total_tokens: int = 0
    gap_count: int = 0
    cache_hit_count: int = 0
    denied_by_budget: int = 0
    denied_by_topk: int = 0
    recovered_tasks: int = 0

    @property
    def hit_rate(self) -> float:
        """命中率 = 命中数 / 查询数。"""
        queries = _safe_int(self.total_queries, 0, minimum=0)
        hits = _safe_int(self.total_hits, 0, minimum=0)
        return hits / queries if queries > 0 else 0.0

    @property
    def conversion_rate(self) -> float:
        """有效输入转化率 = 有效输入数 / 命中数。"""
        hits = _safe_int(self.total_hits, 0, minimum=0)
        effective = _safe_int(self.effective_inputs, 0, minimum=0)
        return effective / hits if hits > 0 else 0.0

    @property
    def avg_cost_per_query(self) -> float:
        """单次查询平均成本。"""
        queries = _safe_int(self.total_queries, 0, minimum=0)
        cost = _safe_float(self.total_cost, 0.0, minimum=0.0)
        return cost / queries if queries > 0 else 0.0

    @property
    def avg_cost_per_hit(self) -> float:
        """单条命中平均成本。"""
        hits = _safe_int(self.total_hits, 0, minimum=0)
        cost = _safe_float(self.total_cost, 0.0, minimum=0.0)
        return cost / hits if hits > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "total_candidates": _safe_int(self.total_candidates, 0, minimum=0),
            "total_queries": _safe_int(self.total_queries, 0, minimum=0),
            "total_hits": _safe_int(self.total_hits, 0, minimum=0),
            "effective_inputs": _safe_int(self.effective_inputs, 0, minimum=0),
            "total_cost": _safe_float(self.total_cost, 0.0, minimum=0.0),
            "total_documents": _safe_int(self.total_documents, 0, minimum=0),
            "total_tokens": _safe_int(self.total_tokens, 0, minimum=0),
            "gap_count": _safe_int(self.gap_count, 0, minimum=0),
            "cache_hit_count": _safe_int(self.cache_hit_count, 0, minimum=0),
            "denied_by_budget": _safe_int(self.denied_by_budget, 0, minimum=0),
            "denied_by_topk": _safe_int(self.denied_by_topk, 0, minimum=0),
            "recovered_tasks": _safe_int(self.recovered_tasks, 0, minimum=0),
            "hit_rate": self.hit_rate,
            "conversion_rate": self.conversion_rate,
            "avg_cost_per_query": self.avg_cost_per_query,
            "avg_cost_per_hit": self.avg_cost_per_hit,
        }


# ---------------------------------------------------------------------------
# 检索器协议（外部实现注入）
# ---------------------------------------------------------------------------


class SearchExecutor:
    """检索器接口协议。

    实际检索由外部注入（如 perilla 检索器、web 搜索器）。
    调度器只负责：候选筛选、查询构造、预算管理、超时/并发/缓存/去重、状态恢复。
    """

    def execute(
        self,
        query: SearchQuery,
        budget: TaskBudget,
        timeout_seconds: float,
    ) -> SearchResult:
        """执行检索。

        Args:
            query: 已校验的检索查询
            budget: 已预留的预算
            timeout_seconds: 超时秒数

        Returns:
            SearchResult；status=succeeded 时 hits 至少 1 条；
            status=gap 时 gap_description 必填
        """
        raise NotImplementedError


class _NullSearchExecutor(SearchExecutor):
    """默认空检索器，用于单元测试与默认降级。

    不调用任何外部 API；返回 status=gap 的结果，并写入 gap_description。
    符合验收点 3「搜索失败只产生 gap，不生成事实」。
    """

    def execute(
        self,
        query: SearchQuery,
        budget: TaskBudget,
        timeout_seconds: float,
    ) -> SearchResult:
        return SearchResult(
            query_id=query.query_id,
            status=SCHEDULER_STATUS_GAP,
            gap_description=(
                f"未配置检索器；查询 {query.target_variable} "
                f"({query.company} / {query.product} / {query.period}) "
                f"转为 gap"
            ),
            consumed_budget=TaskBudget(),
            elapsed_seconds=0.0,
            started_at=_utc_now(),
            finished_at=_utc_now(),
        )


# ---------------------------------------------------------------------------
# Top-K 调度器主类
# ---------------------------------------------------------------------------


class TopKResearchScheduler:
    """Top-K 主动证据检索调度器。

    工作流程：
        1. select_topk()：轻量筛选 Top-K 候选（不触发任何外部检索）
        2. plan_query()：为候选构造 SearchQuery
        3. execute_search()：按预算/超时执行检索
        4. recover()：基于 ResearchTask.checkpoint 幂等恢复
        5. stats()：统计命中率/转化率/单次成本

    硬门禁：
    - Top-K 外公司不发生无界深搜（验收点 1）
    - 搜索失败只产生 gap，不生成事实（验收点 3）
    - 任务重启可恢复且幂等（验收点 4）
    - 闲时预算不超过总预算 20%
    - 不输出投资建议
    """

    def __init__(
        self,
        *,
        budget_manager: GlobalBudgetManager | None = None,
        gap_planner: GapTaskPlanner | None = None,
        repository: ResearchRepository | None = None,
        executor: SearchExecutor | None = None,
        top_k: int = DEFAULT_TOP_K,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        idle_budget_ratio: float = MAX_IDLE_BUDGET_RATIO,
    ) -> None:
        """初始化调度器。

        Args:
            budget_manager: 全局预算管理器
            gap_planner: 缺口任务规划器
            repository: 研究仓储
            executor: 检索器（None 则使用空检索器）
            top_k: Top-K 上限
            concurrency: 最大并发数
            timeout_seconds: 单次查询默认超时
            cache_ttl_seconds: 查询缓存 TTL
            idle_budget_ratio: 闲时预算比例上限
        """
        self.repository = repository or ResearchRepository()
        self.budget_manager = budget_manager or GlobalBudgetManager(repository=self.repository)
        self.gap_planner = gap_planner or GapTaskPlanner(repository=self.repository)
        self.executor = executor or _NullSearchExecutor()
        self.top_k = _safe_int(top_k, DEFAULT_TOP_K, minimum=1)
        self.concurrency = _safe_int(concurrency, DEFAULT_CONCURRENCY, minimum=1)
        self.timeout_seconds = _safe_float(
            timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=0.0
        )
        self.cache_ttl_seconds = _safe_float(
            cache_ttl_seconds, DEFAULT_CACHE_TTL_SECONDS, minimum=0.0
        )
        # 防御性：闲时预算比例必须在 (0, 1] 且不超过 MAX_IDLE_BUDGET_RATIO
        self.idle_budget_ratio = max(
            0.0,
            min(
                _safe_float(idle_budget_ratio, MAX_IDLE_BUDGET_RATIO),
                MAX_IDLE_BUDGET_RATIO,
            ),
        )

        # 内部状态（线程安全）
        self._lock = threading.RLock()
        self._stats = SchedulerStats()
        self._query_cache: dict[str, tuple[float, SearchResult]] = {}
        self._dedup_keys: set[str] = set()
        self._inflight_keys: set[str] = set()
        self._topk_symbols: set[str] = set()  # 当前批 Top-K 公司集合（验收点 1）
        self._active_reservations: dict[str, str] = {}  # query_id → reservation_id

    # ------------------------------------------------------------------
    # 验收点 1：Top-K 选择（轻量筛选，不触发外部检索）
    # ------------------------------------------------------------------

    def select_topk(
        self,
        *,
        funnel_output: Any | None = None,
        change_radar: Any | None = None,
        as_of: str = "",
        k: int | None = None,
    ) -> list[TopKCandidate]:
        """轻量筛选 Top-K 候选。

        不调用任何外部检索；仅基于 P2.2 漏斗、P2.3 雷达和缺口任务产生候选。

        Args:
            funnel_output: P2.2 FunnelOutput（可空）
            change_radar: P2.3 ChangeRadar（可空）
            as_of: PIT 时点
            k: Top-K 上限（None 则使用 self.top_k）

        Returns:
            Top-K 候选列表（按 priority 降序，长度 <= k）
        """
        top_k = _safe_int(self.top_k if k is None else k, self.top_k, minimum=1)
        candidates: list[TopKCandidate] = []

        # 来源 1：P2.2 漏斗 Pareto frontier
        if funnel_output is not None:
            try:
                candidates.extend(
                    self._candidates_from_funnel(funnel_output, _safe_text(as_of))
                )
            except Exception as exc:
                logger.warning("从 Funnel 读取候选失败: %s", exc)

        # 来源 2：P2.3 雷达 retrieval_hypothesis 事件
        if change_radar is not None:
            try:
                candidates.extend(
                    self._candidates_from_change_radar(change_radar, _safe_text(as_of))
                )
            except Exception as exc:
                logger.warning("从 ChangeRadar 读取候选失败: %s", exc)

        # 来源 3：缺口任务（model_blocking 优先）
        try:
            candidates.extend(self._candidates_from_gaps(_safe_text(as_of)))
        except Exception as exc:
            logger.warning("从缺口任务读取候选失败: %s", exc)

        # 去重（同 symbol 取 priority 最高）
        deduped = self._dedup_candidates(candidates)

        # 按 priority 降序排序，取 Top-K
        deduped.sort(key=lambda c: c.priority, reverse=True)
        top = deduped[:top_k]

        # 重新分配 rank
        for idx, cand in enumerate(top, start=1):
            cand.rank = idx

        # 记录 Top-K 公司集合（验收点 1：Top-K 外公司不发生无界深搜）
        with self._lock:
            self._topk_symbols = {c.symbol for c in top if c.symbol}
            self._stats.total_candidates += len(top)

        return top

    @staticmethod
    def _dedup_candidates(
        candidates: list[TopKCandidate],
    ) -> list[TopKCandidate]:
        """同 symbol 取 priority 最高候选。"""
        seen: dict[str, TopKCandidate] = {}
        for cand in candidates:
            if not isinstance(cand, TopKCandidate):
                continue
            cand.symbol = _safe_text(cand.symbol)
            cand.company_name = _safe_text(cand.company_name)
            cand.priority = _safe_float(cand.priority, 0.0, minimum=0.0, maximum=1.0)
            key = cand.symbol or cand.company_name
            if not key:
                continue
            existing = seen.get(key)
            if existing is None or cand.priority > existing.priority:
                seen[key] = cand
        return list(seen.values())

    def _candidates_from_funnel(
        self,
        funnel_output: Any,
        as_of: str,
    ) -> list[TopKCandidate]:
        """从 P2.2 漏斗输出抽取候选。

        Pareto frontier 公司优先级 0.9；high_conviction_symbols 优先级 0.8；
        research_pending_symbols 优先级 0.5（待补数据）。
        """
        candidates: list[TopKCandidate] = []
        results = _items(_field_value(funnel_output, "results", ()))
        result_by_symbol = {
            _safe_text(_field_value(result, "symbol")): result
            for result in results
            if _safe_text(_field_value(result, "symbol"))
        }

        # Pareto frontier
        pareto = tuple(
            symbol
            for raw_symbol in _items(_field_value(funnel_output, "pareto_frontier", ()))
            if (symbol := _safe_text(raw_symbol))
        )
        pareto_set = set(pareto)
        for symbol in pareto:
            result = result_by_symbol.get(symbol)
            company_name = _safe_text(_field_value(result, "company_name")) if result else ""
            candidates.append(
                TopKCandidate(
                    symbol=symbol,
                    company_name=company_name,
                    as_of=as_of,
                    candidate_source=CANDIDATE_SOURCE_FUNNEL_PARETO,
                    reason="P2.2 Pareto frontier 非劣公司",
                    target_variable="fundamental_frontier",
                    product="整体业务",
                    period=as_of,
                    preferred_source=SOURCE_TIER_FIRST_PARTY,
                    priority=0.9,
                )
            )

        # High conviction
        high_conv = tuple(
            symbol
            for raw_symbol in _items(
                _field_value(funnel_output, "high_conviction_symbols", ())
            )
            if (symbol := _safe_text(raw_symbol))
        )
        high_conv_set = set(high_conv)
        for symbol in high_conv:
            if symbol in pareto_set:
                continue
            result = result_by_symbol.get(symbol)
            company_name = _safe_text(_field_value(result, "company_name")) if result else ""
            candidates.append(
                TopKCandidate(
                    symbol=symbol,
                    company_name=company_name,
                    as_of=as_of,
                    candidate_source=CANDIDATE_SOURCE_FUNNEL_HIGH_CONV,
                    reason="P2.2 高信念入选",
                    target_variable="fundamental_high_conviction",
                    product="整体业务",
                    period=as_of,
                    preferred_source=SOURCE_TIER_FIRST_PARTY,
                    priority=0.8,
                )
            )

        # Research pending（待补数据，优先级低）
        pending = tuple(
            symbol
            for raw_symbol in _items(
                _field_value(funnel_output, "research_pending_symbols", ())
            )
            if (symbol := _safe_text(raw_symbol))
        )
        for symbol in pending:
            if symbol in pareto_set or symbol in high_conv_set:
                continue
            result = result_by_symbol.get(symbol)
            company_name = _safe_text(_field_value(result, "company_name")) if result else ""
            candidates.append(
                TopKCandidate(
                    symbol=symbol,
                    company_name=company_name,
                    as_of=as_of,
                    candidate_source=CANDIDATE_SOURCE_FUNNEL_HIGH_CONV,
                    reason="P2.2 待补数据公司",
                    target_variable="data_completion",
                    product="整体业务",
                    period=as_of,
                    preferred_source=SOURCE_TIER_FIRST_PARTY,
                    priority=0.5,
                )
            )
        return candidates

    def _candidates_from_change_radar(
        self,
        change_radar: Any,
        as_of: str,
    ) -> list[TopKCandidate]:
        """从 P2.3 雷达抽取候选。

        仅选取 status=retrieval_hypothesis 的事件（待补证据升级）。
        优先级 0.85（高于待补数据，低于 Pareto frontier）。
        """
        candidates: list[TopKCandidate] = []
        # 雷达查询方法：list_events_by_status
        list_method = getattr(change_radar, "list_events_by_status", None)
        if list_method is None:
            # 降级：尝试 list_events
            list_method = getattr(change_radar, "list_events", None)
        if list_method is None:
            return candidates

        try:
            events = list_method("retrieval_hypothesis") if list_method else []
        except Exception as exc:
            logger.warning("从 ChangeRadar 读取 retrieval_hypothesis 失败: %s", exc)
            return candidates

        for event in _items(events):
            symbol = _safe_text(
                _field_value(event, "symbol")
                or _field_value(event, "entity_name")
            )
            if not symbol:
                continue
            target_variable = self._infer_target_variable_from_event(event)
            candidates.append(
                TopKCandidate(
                    symbol=symbol,
                    company_name=_safe_text(_field_value(event, "entity_name")),
                    as_of=as_of or _safe_text(_field_value(event, "observed_at")),
                    candidate_source=CANDIDATE_SOURCE_CHANGE_RADAR,
                    reason=(
                        f"P2.3 雷达事件 {_safe_text(_field_value(event, 'event_type'))} "
                        f"待补证据升级"
                    ),
                    target_variable=target_variable,
                    product=_safe_text(_field_value(event, "entity_name"), "整体业务"),
                    period=_safe_text(_field_value(event, "observed_at"), as_of),
                    preferred_source=SOURCE_TIER_INDUSTRY_BROKER,
                    priority=0.85,
                    metadata={"event_id": _safe_text(_field_value(event, "event_id"))},
                )
            )
        return candidates

    @staticmethod
    def _infer_target_variable_from_event(event: Any) -> str:
        """从事件类型推断目标变量（不输出交易建议，仅描述性）。"""
        event_type = _safe_text(_field_value(event, "event_type"))
        mapping = {
            "product_price_cost": "asp_or_cost",
            "order_tender": "order_or_revenue",
            "capacity_utilization_inventory": "capacity_or_inventory",
            "hiring_patent_tech": "rd_or_headcount",
            "capex_expansion": "capex",
            "policy_subsidy": "policy_subsidy",
            "management_guidance": "guidance",
            "consensus_revision": "consensus_revision",
        }
        return mapping.get(event_type, "event_driver")

    def _candidates_from_gaps(self, as_of: str) -> list[TopKCandidate]:
        """从缺口任务抽取候选。

        model_blocking=True 的缺口优先级 0.88（高于一般 Pareto frontier）；
        其他缺口优先级 0.6。
        """
        candidates: list[TopKCandidate] = []
        try:
            tasks = self.repository.list_tasks(limit=500)
        except Exception as exc:
            logger.warning("从 ResearchRepository 读取任务失败: %s", exc)
            return candidates

        for task in tasks or []:
            params = _field_value(task, "params", {}) or {}
            if not isinstance(params, Mapping):
                continue
            # 仅取 idle_learning 模式
            if params.get("research_mode") != "idle_learning":
                continue
            symbol = str(params.get("target_entity", "") or "")
            if not symbol:
                continue
            gap = str(params.get("gap", "") or "")
            company_name = str(params.get("company_name", "") or "")
            model_blocking = _safe_bool(params.get("model_blocking", False))
            priority = 0.88 if model_blocking else 0.6
            # 阻断盈利模型的证据优先（P2.4 §170）
            target_variable = self._gap_to_target_variable(gap)
            candidates.append(
                TopKCandidate(
                    symbol=symbol,
                    company_name=company_name,
                    as_of=str(params.get("as_of", as_of) or as_of),
                    candidate_source=CANDIDATE_SOURCE_GAP_MODEL_BLOCKING,
                    reason=(
                        f"缺口 {gap}（model_blocking={model_blocking}）"
                    ),
                    target_variable=target_variable,
                    product="整体业务",
                    period=str(params.get("as_of", as_of) or as_of),
                    preferred_source=SOURCE_TIER_FIRST_PARTY,
                    priority=priority,
                    metadata={
                        "task_id": _safe_text(_field_value(task, "task_id")),
                        "gap": gap,
                    },
                )
            )
        return candidates

    @staticmethod
    def _gap_to_target_variable(gap: str) -> str:
        """将缺口类型映射为目标变量。"""
        mapping = {
            "verified_causal_chain_missing": "causal_chain",
            "causal_chain_depth_insufficient": "causal_chain_depth",
            "forward_profit_bridge_missing": "forward_profit_driver",
            "company_forward_driver_evidence_missing": "forward_driver",
            "verified_market_expectation_missing": "market_expectation",
        }
        return mapping.get(gap, "gap_completion")

    # ------------------------------------------------------------------
    # 验收点 2：查询构造（必填字段校验）
    # ------------------------------------------------------------------

    def plan_query(
        self,
        candidate: TopKCandidate,
        *,
        keywords: list[str] | None = None,
        required_evidence_kind: str = "quantitative",
        timeout_seconds: float | None = None,
        max_documents: int = 10,
    ) -> SearchQuery:
        """为候选构造 SearchQuery。

        按验收点（P2.4 §171）：查询必须包含目标变量、公司、产品、期间和首选来源。
        若 candidate 字段缺失，则降级为兜底值（不抛异常，AGENTS.md 字段缺失降级）。

        Args:
            candidate: Top-K 候选
            keywords: 关键词列表（可空）
            required_evidence_kind: 期望证据类型
            timeout_seconds: 超时秒数
            max_documents: 单次最大文档数

        Returns:
            SearchQuery（未校验）
        """
        target_variable = _safe_text(
            _field_value(candidate, "target_variable"), "unknown"
        )
        symbol = _safe_text(_field_value(candidate, "symbol"))
        company_name = _safe_text(_field_value(candidate, "company_name"))
        product = _safe_text(_field_value(candidate, "product"), "整体业务")
        period = _safe_text(
            _field_value(candidate, "period")
            or _field_value(candidate, "as_of"),
            "unknown",
        )
        preferred_source = _safe_text(
            _field_value(candidate, "preferred_source"), SOURCE_TIER_FIRST_PARTY
        )
        keyword_values = tuple(
            text for raw in _items(keywords) if (text := _safe_text(raw))
        )
        query = SearchQuery(
            target_variable=target_variable,
            company=company_name or symbol or "unknown",
            product=product,
            period=period,
            preferred_source=preferred_source,
            source_tiers=SOURCE_ORDER,
            keywords=list(keyword_values),
            required_evidence_kind=_safe_text(required_evidence_kind, "quantitative"),
            timeout_seconds=(
                self.timeout_seconds
                if timeout_seconds is None
                else _safe_float(timeout_seconds, self.timeout_seconds, minimum=0.0)
            ),
            max_documents=_safe_int(max_documents, 10, minimum=0),
        )
        query.dedup_key = query.compute_dedup_key()
        return query

    # ------------------------------------------------------------------
    # 验收点 1 + 2 + 3：执行检索（Top-K 校验 + 预算 + 缓存 + 去重 + 失败转 gap）
    # ------------------------------------------------------------------

    def execute_search(
        self,
        candidate: TopKCandidate,
        query: SearchQuery,
        *,
        task_id: str = "",
        attempt_id: str = "",
        idle_mode: bool = False,
    ) -> SearchResult:
        """执行单次检索。

        流程：
        1. 验收点 1：校验 candidate.symbol 是否在 Top-K 集合内
        2. 校验 query 必填字段
        3. 去重检查（dedup_key）
        4. 缓存检查（cache_ttl_seconds）
        5. 预算预留（含闲时预算上限校验）
        6. 调用 executor.execute（超时控制）
        7. 释放未用预算
        8. 更新统计

        Args:
            candidate: Top-K 候选
            query: 检索查询
            task_id: 任务 ID（用于幂等）
            attempt_id: attempt ID
            idle_mode: 是否闲时模式（应用 20% 上限）

        Returns:
            SearchResult；失败/gap 时不返回 hits
        """
        if not isinstance(candidate, TopKCandidate) or not isinstance(query, SearchQuery):
            return SearchResult(
                status=SCHEDULER_STATUS_FAILED,
                failure_reason="candidate/query 类型无效",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )

        # 验收点 1：Top-K 外公司不发生无界深搜
        if not self._is_within_topk(candidate):
            with self._lock:
                self._stats.denied_by_topk += 1
            return SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_CANCELED,
                failure_reason=(
                    f"Top-K 外公司 {candidate.symbol} 不允许深搜 "
                    f"（验收点 1：Top-K 外公司不发生无界深搜）"
                ),
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )

        # 校验必填字段
        errors = query.validate()
        if errors:
            return SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_FAILED,
                failure_reason="查询必填字段缺失: " + "; ".join(errors),
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )

        # 外部构造的 query 可能没有去重键，或带有过期/错误的键；统一重算。
        computed_dedup_key = query.compute_dedup_key()
        if query.dedup_key != computed_dedup_key:
            query.dedup_key = computed_dedup_key

        # 去重检查（同 dedup_key 不重复执行）
        with self._lock:
            if query.dedup_key and query.dedup_key in self._dedup_keys:
                self._stats.cache_hit_count += 1
                return SearchResult(
                    query_id=query.query_id,
                    status=SCHEDULER_STATUS_GAP,
                    gap_description=f"重复查询（dedup_key={query.dedup_key}）",
                    cached=True,
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            if query.dedup_key and query.dedup_key in self._inflight_keys:
                return SearchResult(
                    query_id=query.query_id,
                    status=SCHEDULER_STATUS_GAP,
                    gap_description=f"查询正在执行（dedup_key={query.dedup_key}）",
                    cached=True,
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            # 缓存检查
            cached = self._lookup_cache(query.dedup_key)
            if cached is not None:
                self._stats.cache_hit_count += 1
                cached.cached = True
                return cached
            if query.dedup_key:
                self._inflight_keys.add(query.dedup_key)

        # 预算预留
        requested = self._estimate_budget(query)
        if idle_mode and not self._is_idle_budget_within_cap(requested):
            with self._lock:
                self._inflight_keys.discard(query.dedup_key)
            with self._lock:
                self._stats.denied_by_budget += 1
            return SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_CANCELED,
                failure_reason=(
                    f"闲时预算超出 {self.idle_budget_ratio:.0%} 上限 "
                    f"（P2.4 §174）"
                ),
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )

        action_id = f"topk_{query.dedup_key}"
        try:
            reservation = self.budget_manager.reserve(
                requested,
                task_id=task_id or f"topk_{candidate.symbol}",
                attempt_id=attempt_id or "1",
                action_id=action_id,
            )
        except Exception as exc:
            with self._lock:
                self._inflight_keys.discard(query.dedup_key)
            logger.warning("预留预算失败: %s", exc)
            return SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_GAP,
                gap_description=f"预算服务异常: {exc}",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )
        if not reservation.granted:
            with self._lock:
                self._inflight_keys.discard(query.dedup_key)
            with self._lock:
                self._stats.denied_by_budget += 1
            return SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_CANCELED,
                failure_reason=f"预算不足: {reservation.reason_code}",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )

        # 执行检索
        started_at = _utc_now()
        start_ts = time.monotonic()
        try:
            result = self._execute_with_timeout(query, reservation.allocated)
        except TimeoutError as exc:
            result = SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_GAP,
                gap_description=f"检索超时: {exc}",
                started_at=started_at,
                finished_at=_utc_now(),
            )
        except Exception as exc:
            logger.exception("检索执行异常: %s", exc)
            result = SearchResult(
                query_id=query.query_id,
                status=SCHEDULER_STATUS_GAP,
                gap_description=f"检索异常: {exc}",
                started_at=started_at,
                finished_at=_utc_now(),
            )
        result = _normalise_result(result, query.query_id)
        result.elapsed_seconds = max(
            result.elapsed_seconds, time.monotonic() - start_ts
        )
        result.reservation_id = reservation.reservation_id

        # 释放未用预算
        consumed = _coerce_budget(result.consumed_budget)
        try:
            self.budget_manager.consume(reservation.reservation_id, consumed)
        except Exception as exc:
            logger.warning("记录预算消耗失败: %s", exc)
        finally:
            try:
                self.budget_manager.release(reservation_id=reservation.reservation_id)
            except Exception as exc:
                logger.warning("释放预算失败: %s", exc)

        # 更新统计与缓存
        with self._lock:
            if query.dedup_key:
                self._dedup_keys.add(query.dedup_key)
            self._stats.total_queries += 1
            if result.status == SCHEDULER_STATUS_SUCCEEDED:
                self._stats.total_hits += len(result.hits)
                self._stats.total_cost += sum(
                    _safe_float(getattr(h, "cost", 0.0), 0.0, minimum=0.0)
                    for h in result.hits
                )
                self._stats.total_documents += int(
                    getattr(consumed, "max_documents", 0) or 0
                )
                self._stats.total_tokens += int(
                    getattr(consumed, "max_tokens", 0) or 0
                )
            elif result.status == SCHEDULER_STATUS_GAP:
                self._stats.gap_count += 1
            self._store_cache(query.dedup_key, result)
            self._inflight_keys.discard(query.dedup_key)

        # 若传入 task_id，保存 checkpoint 以支持后续 recover（验收点 4）
        if task_id:
            self._save_checkpoint(task_id, candidate, query, result)
        return result

    def _is_within_topk(self, candidate: TopKCandidate) -> bool:
        """检查候选是否在当前 Top-K 集合内。

        验收点 1：Top-K 外公司不发生无界深搜。
        若 Top-K 集合为空（select_topk 未调用），允许执行（兼容首次启动）。
        """
        with self._lock:
            if not self._topk_symbols:
                return True
            symbol = _safe_text(_field_value(candidate, "symbol"))
            return bool(symbol) and symbol in self._topk_symbols

    def _lookup_cache(self, dedup_key: str) -> SearchResult | None:
        """查询缓存。"""
        if not dedup_key:
            return None
        entry = self._query_cache.get(dedup_key)
        if entry is None:
            return None
        ts, result = entry
        if time.monotonic() - ts >= self.cache_ttl_seconds:
            self._query_cache.pop(dedup_key, None)
            return None
        try:
            return deepcopy(result)
        except Exception:
            self._query_cache.pop(dedup_key, None)
            return None

    def _store_cache(self, dedup_key: str, result: SearchResult) -> None:
        """写入缓存。"""
        if not dedup_key or result.status != SCHEDULER_STATUS_SUCCEEDED:
            return
        try:
            self._query_cache[dedup_key] = (time.monotonic(), deepcopy(result))
        except Exception:
            logger.warning("写入查询缓存失败，忽略本次缓存")

    @staticmethod
    def _estimate_budget(query: SearchQuery) -> TaskBudget:
        """估算单次查询所需预算。"""
        return TaskBudget(
            max_requests=10,
            max_seconds=_safe_float(query.timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=0.0),
            max_tokens=10_000,
            max_cost=10.0,
            max_documents=_safe_int(query.max_documents, 10, minimum=0),
        )

    def _is_idle_budget_within_cap(self, requested: TaskBudget) -> bool:
        """检查闲时预算是否在 20% 上限内。

        若当日已消耗预算 * idle_budget_ratio + requested 不超过 daily_total * idle_budget_ratio，
        则允许；否则拒绝。
        """
        try:
            snapshot = self.budget_manager.snapshot()
        except Exception as exc:
            logger.warning("读取预算快照失败，闲时检查放行: %s", exc)
            return True

        total = _coerce_budget(_field_value(snapshot, "total", {}))
        consumed = _coerce_budget(_field_value(snapshot, "consumed", {}))
        # 闲时预算上限 = total * idle_budget_ratio
        idle_cap_requests = int(total.max_requests * self.idle_budget_ratio)
        idle_cap_cost = total.max_cost * self.idle_budget_ratio
        idle_cap_tokens = int(total.max_tokens * self.idle_budget_ratio)
        idle_cap_documents = int(total.max_documents * self.idle_budget_ratio)
        idle_cap_seconds = total.max_seconds * self.idle_budget_ratio

        # 已消耗的"闲时部分"按比例估算
        # 简化：闲时预算上限 - 已消耗预算 >= requested
        # 否则视为超限
        remaining_idle_requests = max(
            0, idle_cap_requests - consumed.max_requests
        )
        remaining_idle_cost = max(0.0, idle_cap_cost - consumed.max_cost)
        remaining_idle_tokens = max(0, idle_cap_tokens - consumed.max_tokens)
        remaining_idle_documents = max(
            0, idle_cap_documents - consumed.max_documents
        )
        remaining_idle_seconds = max(
            0.0, idle_cap_seconds - consumed.max_seconds
        )

        return (
            remaining_idle_requests >= requested.max_requests
            and remaining_idle_cost >= requested.max_cost
            and remaining_idle_tokens >= requested.max_tokens
            and remaining_idle_documents >= requested.max_documents
            and remaining_idle_seconds >= requested.max_seconds
        )

    def _execute_with_timeout(
        self,
        query: SearchQuery,
        budget: TaskBudget,
    ) -> SearchResult:
        """执行检索（带超时）。

        单线程实现：实际超时由 executor 内部实现；调度器仅做时间记录。
        若 executor 不响应超时，调度器记录耗时但不强制中断（避免数据损坏）。
        """
        return self.executor.execute(
            query=query,
            budget=budget,
            timeout_seconds=query.timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 验收点 4：任务重启可恢复且幂等
    # ------------------------------------------------------------------

    def recover(
        self,
        task_id: str,
        *,
        candidate: TopKCandidate | None = None,
        query: SearchQuery | None = None,
    ) -> SearchResult:
        """幂等恢复任务。

        基于 ResearchTask.checkpoint 恢复：
        - 若 task 已存在且 status=succeeded，返回已记录结果
        - 若 task 已存在且 status=running/partial，从 checkpoint 续跑
        - 若 task 不存在，新建并执行

        幂等性保证：
        - task_id 由 (symbol, gap, as_of) 哈希生成（与 GapTaskPlanner._task_id 一致）
        - 相同 task_id 多次调用只执行一次检索

        Args:
            task_id: 任务 ID
            candidate: Top-K 候选（新建任务时必填）
            query: 检索查询（新建任务时必填）

        Returns:
            SearchResult
        """
        task_id = _safe_text(task_id)
        if not task_id:
            return SearchResult(
                status=SCHEDULER_STATUS_FAILED,
                failure_reason="task_id 缺失",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )
        with self._lock:
            self._stats.recovered_tasks += 1

        try:
            existing = self.repository.get_task(task_id)
        except Exception as exc:
            logger.warning("读取恢复任务失败: %s", exc)
            return SearchResult(
                status=SCHEDULER_STATUS_FAILED,
                failure_reason=f"读取恢复任务异常: {exc}",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )
        if existing is None:
            # 新建任务
            if candidate is None or query is None:
                return SearchResult(
                    status=SCHEDULER_STATUS_FAILED,
                    failure_reason=(
                        "新建恢复任务需要 candidate 与 query 参数"
                    ),
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            return self._create_and_execute(task_id, candidate, query)

        # 已存在：检查是否已完成
        checkpoint = _field_value(existing, "checkpoint", {}) or {}
        if not isinstance(checkpoint, Mapping):
            return SearchResult(
                status=SCHEDULER_STATUS_FAILED,
                failure_reason=f"task {task_id} 的 checkpoint 格式无效",
                started_at=_utc_now(),
                finished_at=_utc_now(),
            )
        last_status = _safe_text(checkpoint.get("scheduler_status")).lower()
        if last_status == SCHEDULER_STATUS_SUCCEEDED:
            # 幂等返回
            cached_result = checkpoint.get("last_result", {})
            if not isinstance(cached_result, Mapping):
                return SearchResult(
                    status=SCHEDULER_STATUS_FAILED,
                    failure_reason=f"task {task_id} 的结果 checkpoint 格式无效",
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            restored = _normalise_result(
                cached_result, _safe_text(_field_value(query, "query_id"))
            )
            if restored.status != SCHEDULER_STATUS_SUCCEEDED:
                return SearchResult(
                    status=SCHEDULER_STATUS_FAILED,
                    failure_reason=f"task {task_id} 的成功 checkpoint 缺少有效命中",
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            restored.cached = True
            return restored

        # 续跑：执行剩余检索
        if candidate is None or query is None:
            # 从 checkpoint 恢复 candidate/query
            cand_payload = checkpoint.get("candidate", {})
            query_payload = checkpoint.get("query", {})
            if not isinstance(cand_payload, Mapping) or not isinstance(query_payload, Mapping):
                return SearchResult(
                    status=SCHEDULER_STATUS_FAILED,
                    failure_reason=(
                        f"无法从 checkpoint 恢复 task {task_id}：缺 candidate/query"
                    ),
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )
            try:
                candidate = TopKCandidate(**dict(cand_payload))
                query = SearchQuery(**dict(query_payload))
            except (TypeError, ValueError) as exc:
                return SearchResult(
                    status=SCHEDULER_STATUS_FAILED,
                    failure_reason=f"无法解析 task {task_id} 的 checkpoint：{exc}",
                    started_at=_utc_now(),
                    finished_at=_utc_now(),
                )

        # 续跑使用新的 attempt_id
        attempt_id = _new_id("recover")
        result = self.execute_search(
            candidate,
            query,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        # 写回 checkpoint
        self._save_checkpoint(task_id, candidate, query, result)
        return result

    @staticmethod
    def _query_payload(query: SearchQuery) -> dict[str, Any]:
        """保存恢复所需的完整查询参数，避免续跑悄悄丢失边界配置。"""
        return {
            "query_id": _safe_text(query.query_id),
            "target_variable": _safe_text(query.target_variable),
            "company": _safe_text(query.company),
            "product": _safe_text(query.product),
            "period": _safe_text(query.period),
            "preferred_source": _safe_text(query.preferred_source),
            "source_tiers": tuple(_safe_text(tier) for tier in _items(query.source_tiers)),
            "keywords": [
                text for raw in _items(query.keywords) if (text := _safe_text(raw))
            ],
            "required_evidence_kind": _safe_text(
                query.required_evidence_kind, "quantitative"
            ),
            "timeout_seconds": _safe_float(
                query.timeout_seconds, DEFAULT_TIMEOUT_SECONDS, minimum=0.0
            ),
            "max_documents": _safe_int(query.max_documents, 10, minimum=0),
            "dedup_key": _safe_text(query.dedup_key),
        }

    def _create_and_execute(
        self,
        task_id: str,
        candidate: TopKCandidate,
        query: SearchQuery,
    ) -> SearchResult:
        """新建并执行任务。"""
        task = ResearchTask(
            task_id=task_id,
            task_type="topk_evidence_search",
            trigger_source="topk_research_scheduler",
            priority=_safe_float(candidate.priority, 0.0, minimum=0.0, maximum=1.0),
            status=TaskStatus.RUNNING.value,
            params={
                "research_mode": "idle_learning",
                "target_entity": candidate.symbol,
                "company_name": candidate.company_name,
                "as_of": candidate.as_of,
                "candidate_source": candidate.candidate_source,
                "target_variable": candidate.target_variable,
                "preferred_source": candidate.preferred_source,
                "required_dispatcher": "topk_research_scheduler",
            },
            checkpoint={
                "candidate": candidate.to_dict(),
                "query": self._query_payload(query),
            },
        )
        try:
            self.repository.save_task(task)
        except Exception as exc:
            logger.warning("保存任务失败: %s", exc)

        result = self.execute_search(
            candidate,
            query,
            task_id=task_id,
            attempt_id="1",
        )
        self._save_checkpoint(task_id, candidate, query, result)
        return result

    def _save_checkpoint(
        self,
        task_id: str,
        candidate: TopKCandidate,
        query: SearchQuery,
        result: SearchResult,
    ) -> None:
        """保存 checkpoint 到 ResearchTask。

        若 task 不存在，则自动新建（供 execute_search 传 task_id 时使用）。
        """
        try:
            task = self.repository.get_task(task_id)
        except Exception as exc:
            logger.warning("读取 checkpoint 任务失败: %s", exc)
            return
        if task is None:
            # 自动新建任务（用于 execute_search 传 task_id 但未预先创建场景）
            task = ResearchTask(
                task_id=task_id,
                task_type="topk_evidence_search",
                trigger_source="topk_research_scheduler",
                priority=_safe_float(candidate.priority, 0.0, minimum=0.0, maximum=1.0),
                status=TaskStatus.RUNNING.value,
                params={
                    "research_mode": "idle_learning",
                    "target_entity": candidate.symbol,
                    "company_name": candidate.company_name,
                    "as_of": candidate.as_of,
                    "candidate_source": candidate.candidate_source,
                    "target_variable": candidate.target_variable,
                    "preferred_source": candidate.preferred_source,
                    "required_dispatcher": "topk_research_scheduler",
                },
            )
        task.checkpoint = {
            "candidate": candidate.to_dict(),
            "query": self._query_payload(query),
            "scheduler_status": _safe_text(result.status, SCHEDULER_STATUS_FAILED),
            "last_result": result.to_dict(),
            "updated_at": _utc_now(),
        }
        task.status = (
            TaskStatus.SUCCEEDED.value
            if result.status == SCHEDULER_STATUS_SUCCEEDED
            else TaskStatus.PARTIAL.value
            if result.status == SCHEDULER_STATUS_GAP
            else TaskStatus.FAILED.value
            if result.status == SCHEDULER_STATUS_FAILED
            else task.status
        )
        try:
            self.repository.save_task(task)
        except Exception as exc:
            logger.warning("保存 checkpoint 失败: %s", exc)

    # ------------------------------------------------------------------
    # 验收点 2：统计与审计
    # ------------------------------------------------------------------

    def stats(self) -> SchedulerStats:
        """返回调度器统计快照。"""
        with self._lock:
            return SchedulerStats(
                total_candidates=self._stats.total_candidates,
                total_queries=self._stats.total_queries,
                total_hits=self._stats.total_hits,
                effective_inputs=self._stats.effective_inputs,
                total_cost=self._stats.total_cost,
                total_documents=self._stats.total_documents,
                total_tokens=self._stats.total_tokens,
                gap_count=self._stats.gap_count,
                cache_hit_count=self._stats.cache_hit_count,
                denied_by_budget=self._stats.denied_by_budget,
                denied_by_topk=self._stats.denied_by_topk,
                recovered_tasks=self._stats.recovered_tasks,
            )

    def record_effective_input(self, count: int = 1) -> None:
        """记录有效输入数（被 P1 模型采纳的命中）。

        由 P1 模型反馈调用；用于计算 conversion_rate。

        Args:
            count: 有效输入数
        """
        with self._lock:
            self._stats.effective_inputs += _safe_int(count, 0, minimum=0)

    def clear_cache(self) -> None:
        """清空查询缓存。"""
        with self._lock:
            self._query_cache.clear()
            self._dedup_keys.clear()


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------


def build_default_scheduler(
    *,
    repository: ResearchRepository | None = None,
    executor: SearchExecutor | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> TopKResearchScheduler:
    """构建默认调度器。

    Args:
        repository: 研究仓储（None 则新建）
        executor: 检索器（None 则使用空检索器）
        top_k: Top-K 上限

    Returns:
        TopKResearchScheduler 实例
    """
    repository = repository or ResearchRepository()
    return TopKResearchScheduler(
        budget_manager=GlobalBudgetManager(repository=repository),
        gap_planner=GapTaskPlanner(repository=repository),
        repository=repository,
        executor=executor,
        top_k=top_k,
    )
