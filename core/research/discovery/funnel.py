"""P2.2 基本面发现漏斗：6 维度独立排序 + Pareto frontier。

按 P2 计划 §4 Task P2.2：
- 输出 Pareto frontier 或多维分组，不强制压成一个总分
- 每家公司返回入选、排除和待补数据原因
- 维度权重只能通过版本化 policy 配置，不写死在页面
- 未经验证的权重只能 shadow，不得成为正式筛选门禁

六个独立排序维度：
1. quality：ROIC、现金兑现、杠杆、利润质量
2. growth：收入/利润增长、增量 ROIC、产品驱动持续性
3. expectation：共识修正、分歧和独立预测差异
4. valuation_burden：增长需要消化的估值负担
5. industry_timing：供需、资本开支、技术和政策阶段
6. evidence_quality：来源、PIT、独立性和完整度

设计要点：
- 每个维度独立评分（0-1），不合并成总分
- 缺数据维度 score=None，不参与排序，避免缺数据公司被中性值抬高
- Pareto frontier 在指定维度上找非劣解
- shadow policy 只产生 shadow 评分，不产生 include/exclude 门禁
- active policy 才产生 include/exclude/pending_data 决策
- 相同输入 + 相同 policy → 相同结果（确定性）
- 每个评分可追溯到字段名、字段值、公式

硬门禁（AGENTS.md）：
- 不输出交易动作建议（含开/平/调仓及盈亏控制等）
- 字段缺失降级处理，不崩溃
- 概念映射以本地证据为准
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from .contracts import (
    DATA_AVAILABILITY_AVAILABLE,
    DATA_AVAILABILITY_MISSING,
    DATA_AVAILABILITY_NOT_APPLICABLE,
    DATA_AVAILABILITY_NOT_COMPUTABLE,
    DATA_AVAILABILITY_PENDING,
    FIELD_GROUP_CONSENSUS_REVISIONS,
    FIELD_GROUP_EVIDENCE_QUALITY,
    FIELD_GROUP_FINANCIAL_QUALITY,
    FIELD_GROUP_GROWTH,
    FIELD_GROUP_INDUSTRY_TIMING,
    FIELD_GROUP_VALUATION,
    UniverseMember,
    UniverseSnapshot,
    _normalize_as_of,
    utc_now,
)
from .policies import (
    DIMENSION_EVIDENCE_QUALITY,
    DIMENSION_EXPECTATION,
    DIMENSION_GROWTH,
    DIMENSION_INDUSTRY_TIMING,
    DIMENSION_NAMES,
    DIMENSION_QUALITY,
    DIMENSION_VALUATION_BURDEN,
    DimensionPolicy,
    FunnelPolicy,
    POLICY_STATUS_ACTIVE,
    POLICY_STATUS_SHADOW,
)


# ---------------------------------------------------------------------------
# 入选状态
# ---------------------------------------------------------------------------

# 入选状态（P2.2 §110：每家公司返回入选、排除和待补数据原因）
STATUS_INCLUDE: str = "include"            # 入选（高信念）
STATUS_EXCLUDE: str = "exclude"            # 排除（门禁不通过）
STATUS_PENDING_DATA: str = "pending_data"  # 待补数据（缺关键字段）
STATUS_SHADOW_INCLUDE: str = "shadow_include"  # shadow 入选（未经验证）
STATUS_SHADOW_EXCLUDE: str = "shadow_exclude"  # shadow 排除（未经验证）

# 字段组 → 维度映射
_FIELD_GROUP_TO_DIMENSION: dict[str, str] = {
    FIELD_GROUP_FINANCIAL_QUALITY: DIMENSION_QUALITY,
    FIELD_GROUP_GROWTH: DIMENSION_GROWTH,
    FIELD_GROUP_CONSENSUS_REVISIONS: DIMENSION_EXPECTATION,
    FIELD_GROUP_VALUATION: DIMENSION_VALUATION_BURDEN,
    FIELD_GROUP_INDUSTRY_TIMING: DIMENSION_INDUSTRY_TIMING,
    FIELD_GROUP_EVIDENCE_QUALITY: DIMENSION_EVIDENCE_QUALITY,
}


# ---------------------------------------------------------------------------
# 单维度评分结果
# ---------------------------------------------------------------------------


@dataclass
class DimensionScore:
    """单维度评分结果。

    每个评分可追溯到字段名、字段值、公式，符合 P2.2 验收点 1
    「结果可解释到字段和公式」。

    Attributes:
        dimension: 维度名（DIMENSION_NAMES 之一）
        score: 归一化得分 [0, 1]；None 表示缺数据
        is_missing: 是否缺数据（score is None 时为 True）
        missing_reason: 缺数据原因（如 "no_field_available" / "below_min_coverage"）
        field_scores: 单字段得分列表（每个含 name/value/normalized_score/weight）
        formula: 评分公式（人可读）
        used_field_count: 使用的字段数
        total_field_count: 该维度应使用的总字段数
        coverage_ratio: 字段覆盖率 = used / total
        note: 备注
    """

    dimension: str = ""
    score: float | None = None
    is_missing: bool = False
    missing_reason: str = ""
    field_scores: list[dict[str, Any]] = field(default_factory=list)
    formula: str = ""
    used_field_count: int = 0
    total_field_count: int = 0
    coverage_ratio: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "dimension": self.dimension,
            "score": self.score,
            "is_missing": self.is_missing,
            "missing_reason": self.missing_reason,
            "field_scores": list(self.field_scores),
            "formula": self.formula,
            "used_field_count": self.used_field_count,
            "total_field_count": self.total_field_count,
            "coverage_ratio": self.coverage_ratio,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 单公司漏斗结果
# ---------------------------------------------------------------------------


@dataclass
class CompanyFunnelResult:
    """单公司漏斗结果。

    P2.2 §110 要求「每家公司返回入选、排除和待补数据原因」。

    Attributes:
        symbol: 公司代码
        company_name: 公司名称
        as_of: 截面时点
        status: 入选状态（include/exclude/pending_data/shadow_*）
        dimension_scores: 6 维度评分 {dimension: DimensionScore}
        is_pareto_optimal: 是否在 Pareto frontier 上
        pareto_dominated_by: 被哪些 symbol 支配（空列表表示非劣或未评估）
        inclusion_reasons: 入选原因列表
        exclusion_reasons: 排除原因列表
        pending_data_reasons: 待补数据原因列表
        policy_id: 使用的 Policy ID
        policy_version: 使用的 Policy 版本
        policy_status: 使用的 Policy 状态（shadow/active）
        config_hash: 配置 hash（用于复现性校验）
        evaluated_at: 评估时间（UTC ISO）
        note: 备注
    """

    symbol: str = ""
    company_name: str = ""
    as_of: str = ""
    status: str = ""
    dimension_scores: dict[str, DimensionScore] = field(default_factory=dict)
    is_pareto_optimal: bool = False
    pareto_dominated_by: list[str] = field(default_factory=list)
    inclusion_reasons: list[str] = field(default_factory=list)
    exclusion_reasons: list[str] = field(default_factory=list)
    pending_data_reasons: list[str] = field(default_factory=list)
    policy_id: str = ""
    policy_version: str = ""
    policy_status: str = ""
    config_hash: str = ""
    evaluated_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "as_of": self.as_of,
            "status": self.status,
            "dimension_scores": {k: v.to_dict() for k, v in self.dimension_scores.items()},
            "is_pareto_optimal": self.is_pareto_optimal,
            "pareto_dominated_by": list(self.pareto_dominated_by),
            "inclusion_reasons": list(self.inclusion_reasons),
            "exclusion_reasons": list(self.exclusion_reasons),
            "pending_data_reasons": list(self.pending_data_reasons),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_status": self.policy_status,
            "config_hash": self.config_hash,
            "evaluated_at": self.evaluated_at,
            "note": self.note,
        }

    def get_score(self, dimension: str) -> float | None:
        """获取某维度得分。

        Args:
            dimension: 维度名

        Returns:
            得分；维度不存在或缺数据返回 None
        """
        ds = self.dimension_scores.get(dimension)
        if ds is None or ds.is_missing:
            return None
        return ds.score

    def get_available_dimension_count(self) -> int:
        """获取已评分（非缺数据）维度数。"""
        return sum(
            1 for ds in self.dimension_scores.values()
            if not ds.is_missing and ds.score is not None
        )

    def get_total_dimension_count(self) -> int:
        """获取总维度数（6）。"""
        return len(DIMENSION_NAMES)


# ---------------------------------------------------------------------------
# 漏斗整体输出
# ---------------------------------------------------------------------------


@dataclass
class FunnelOutput:
    """漏斗整体输出。

    P2.2 §109 要求「输出 Pareto frontier 或多维分组，不强制压成一个总分」。

    Attributes:
        funnel_id: 漏斗运行唯一 ID
        snapshot_id: 输入截面 ID
        as_of: 截面时点
        policy_id: 使用的 Policy ID
        policy_version: 使用的 Policy 版本
        policy_status: 使用的 Policy 状态
        config_hash: 配置 hash
        results: 所有公司结果列表
        pareto_frontier: Pareto 非劣公司 symbol 列表
        high_conviction_symbols: 高信念入选公司列表
        research_pending_symbols: 待补数据公司列表
        excluded_symbols: 被排除公司列表
        shadow_symbols: shadow 入选公司列表（未经验证）
        grouping: 分组字典 {group_name: [symbols]}
        evaluated_at: 评估时间
    """

    funnel_id: str = ""
    snapshot_id: str = ""
    as_of: str = ""
    policy_id: str = ""
    policy_version: str = ""
    policy_status: str = ""
    config_hash: str = ""
    results: list[CompanyFunnelResult] = field(default_factory=list)
    pareto_frontier: list[str] = field(default_factory=list)
    high_conviction_symbols: list[str] = field(default_factory=list)
    research_pending_symbols: list[str] = field(default_factory=list)
    excluded_symbols: list[str] = field(default_factory=list)
    shadow_symbols: list[str] = field(default_factory=list)
    grouping: dict[str, list[str]] = field(default_factory=dict)
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "funnel_id": self.funnel_id,
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_status": self.policy_status,
            "config_hash": self.config_hash,
            "results": [r.to_dict() for r in self.results],
            "pareto_frontier": list(self.pareto_frontier),
            "high_conviction_symbols": list(self.high_conviction_symbols),
            "research_pending_symbols": list(self.research_pending_symbols),
            "excluded_symbols": list(self.excluded_symbols),
            "shadow_symbols": list(self.shadow_symbols),
            "grouping": {k: list(v) for k, v in self.grouping.items()},
            "evaluated_at": self.evaluated_at,
        }

    def get_result(self, symbol: str) -> CompanyFunnelResult | None:
        """获取单公司结果。

        Args:
            symbol: 公司代码

        Returns:
            CompanyFunnelResult；不存在返回 None
        """
        for r in self.results:
            if r.symbol == symbol:
                return r
        return None


# ---------------------------------------------------------------------------
# 评分归一化工具
# ---------------------------------------------------------------------------


def _min_max_normalize(
    value: float,
    field_min: float,
    field_max: float,
    higher_is_better: bool = True,
) -> float:
    """Min-Max 归一化到 [0, 1]。

    Args:
        value: 原始值
        field_min: 字段最小值
        field_max: 字段最大值
        higher_is_better: True 表示越大越好；False 表示越小越好（如 PE）

    Returns:
        归一化得分 [0, 1]
    """
    try:
        value = float(value)
        field_min = float(field_min)
        field_max = float(field_max)
    except (OverflowError, TypeError, ValueError):
        return 0.5
    if not all(isfinite(item) for item in (value, field_min, field_max)):
        return 0.5
    if field_max <= field_min:
        return 0.5
    raw = (value - field_min) / (field_max - field_min)
    raw = max(0.0, min(1.0, raw))
    return raw if higher_is_better else (1.0 - raw)


# 字段归一化参考表（默认区间，用于 shadow 评分）
_FIELD_NORMALIZATION: dict[str, dict[str, Any]] = {
    # quality
    "roic": {"min": 0.0, "max": 0.25, "higher_is_better": True},
    "roe": {"min": 0.0, "max": 0.25, "higher_is_better": True},
    "ocf_to_net_income": {"min": 0.0, "max": 1.5, "higher_is_better": True},
    "debt_to_equity": {"min": 0.0, "max": 2.0, "higher_is_better": False},
    "gross_margin": {"min": 0.0, "max": 0.6, "higher_is_better": True},
    "net_margin": {"min": 0.0, "max": 0.3, "higher_is_better": True},
    "current_ratio": {"min": 0.5, "max": 3.0, "higher_is_better": True},
    "interest_coverage": {"min": 0.0, "max": 20.0, "higher_is_better": True},
    # growth
    "revenue_growth_yoy": {"min": -0.2, "max": 0.5, "higher_is_better": True},
    "net_profit_growth_yoy": {"min": -0.3, "max": 0.8, "higher_is_better": True},
    "incremental_roic": {"min": 0.0, "max": 0.2, "higher_is_better": True},
    "eps_growth_yoy": {"min": -0.3, "max": 0.8, "higher_is_better": True},
    # expectation
    "consensus_revision_direction": {"min": -1.0, "max": 1.0, "higher_is_better": True},
    "consensus_dispersion": {"min": 0.0, "max": 0.3, "higher_is_better": False},
    "independent_forecast_gap": {"min": -0.3, "max": 0.3, "higher_is_better": True},
    # valuation_burden（越低越好 → 反向）
    "pe_to_growth": {"min": 0.0, "max": 3.0, "higher_is_better": False},
    "ev_ebitda_to_growth": {"min": 0.0, "max": 3.0, "higher_is_better": False},
    "pb_to_roe": {"min": 0.0, "max": 0.3, "higher_is_better": False},
    # industry_timing
    "lifecycle_stage_score": {"min": 0.0, "max": 1.0, "higher_is_better": True},
    "supply_demand_balance": {"min": -1.0, "max": 1.0, "higher_is_better": True},
    "policy_tailwind": {"min": -1.0, "max": 1.0, "higher_is_better": True},
    # evidence_quality
    "source_count": {"min": 0.0, "max": 5.0, "higher_is_better": True},
    "pit_compliance": {"min": 0.0, "max": 1.0, "higher_is_better": True},
    "independence_score": {"min": 0.0, "max": 1.0, "higher_is_better": True},
    "completeness_score": {"min": 0.0, "max": 1.0, "higher_is_better": True},
}


def _get_normalization(field_name: str) -> dict[str, Any]:
    """获取字段归一化参数。

    Args:
        field_name: 字段名

    Returns:
        归一化参数字典；未注册字段返回默认（0-1，越大越好）
    """
    return _FIELD_NORMALIZATION.get(
        field_name,
        {"min": 0.0, "max": 1.0, "higher_is_better": True},
    )


# ---------------------------------------------------------------------------
# 漏斗主类
# ---------------------------------------------------------------------------


class FundamentalFunnel:
    """基本面发现漏斗：6 维度独立评分 + Pareto frontier。

    工作流程：
    1. 输入：UniverseSnapshot + FunnelPolicy
    2. 对每个 member 评估 6 维度得分
    3. 计算每个 member 的入选状态（include/exclude/pending_data/shadow_*）
    4. 计算 Pareto frontier
    5. 多维分组（high_conviction / research_pending / excluded / shadow）
    6. 输出 FunnelOutput

    不变量：
    - 相同输入 + 相同 policy → 相同输出（确定性）
    - shadow policy 不产生 include/exclude 门禁
    - 缺数据维度 score=None，不参与排序，避免被中性值抬高
    - 每个评分可追溯到字段名、字段值、公式

    硬门禁：
    - 不输出交易动作建议（含开/平/调仓及盈亏控制等，AGENTS.md）
    - 字段缺失降级处理，不崩溃（AGENTS.md）
    """

    def __init__(self, policy: FunnelPolicy) -> None:
        """初始化漏斗。

        Args:
            policy: 评分 Policy（含 6 维度配置）

        Raises:
            ValueError: 当 policy 校验失败
        """
        errors = policy.validate()
        if errors:
            raise ValueError(f"Policy 校验失败: {errors}")
        self.policy = policy
        if not policy.config_hash:
            policy.config_hash = policy.compute_config_hash()

    def evaluate(self, member: UniverseMember) -> CompanyFunnelResult:
        """评估单公司。

        Args:
            member: 截面成员（含 9 字段组）

        Returns:
            CompanyFunnelResult
        """
        cutoff = _normalize_as_of(member.as_of)
        result = CompanyFunnelResult(
            symbol=member.symbol,
            company_name=member.company_name,
            as_of=cutoff,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            policy_status=self.policy.status,
            config_hash=self.policy.config_hash,
            evaluated_at=utc_now(),
        )

        # 1. 计算 6 维度得分
        for dim_name in DIMENSION_NAMES:
            dim_policy = self.policy.get_dimension(dim_name)
            if dim_policy is None:
                # 缺维度 policy，标记为缺数据
                result.dimension_scores[dim_name] = DimensionScore(
                    dimension=dim_name,
                    score=None,
                    is_missing=True,
                    missing_reason="dimension_policy_missing",
                    total_field_count=0,
                )
                continue
            score = self._compute_dimension_score(member, dim_name, dim_policy)
            result.dimension_scores[dim_name] = score

        # 2. 判定入选状态
        self._classify_company(result)

        return result

    def evaluate_snapshot(self, snapshot: UniverseSnapshot) -> FunnelOutput:
        """评估整个截面。

        Args:
            snapshot: 输入截面

        Returns:
            FunnelOutput
        """
        cutoff = _normalize_as_of(snapshot.as_of)
        results: list[CompanyFunnelResult] = []
        for member in snapshot.members:
            try:
                result = self.evaluate(member)
            except Exception as exc:  # noqa: BLE001
                # AGENTS.md：缺数据降级，不崩溃
                result = CompanyFunnelResult(
                    symbol=member.symbol,
                    company_name=member.company_name,
                    as_of=cutoff,
                    status=STATUS_PENDING_DATA,
                    policy_id=self.policy.policy_id,
                    policy_version=self.policy.version,
                    policy_status=self.policy.status,
                    config_hash=self.policy.config_hash,
                    evaluated_at=utc_now(),
                    note=f"evaluate_error: {type(exc).__name__}: {exc}",
                )
                result.pending_data_reasons.append(
                    f"evaluate_exception: {type(exc).__name__}"
                )
            results.append(result)

        # 计算 Pareto frontier
        pareto_frontier, dominated_map = self._compute_pareto_frontier(results)
        for r in results:
            r.is_pareto_optimal = r.symbol in pareto_frontier
            r.pareto_dominated_by = dominated_map.get(r.symbol, [])

        # 多维分组
        grouping = self._group_results(results)
        funnel_id = self._compute_funnel_id(snapshot.snapshot_id, cutoff)

        return FunnelOutput(
            funnel_id=funnel_id,
            snapshot_id=snapshot.snapshot_id,
            as_of=cutoff,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            policy_status=self.policy.status,
            config_hash=self.policy.config_hash,
            results=results,
            pareto_frontier=sorted(pareto_frontier),
            high_conviction_symbols=sorted(grouping["high_conviction"]),
            research_pending_symbols=sorted(grouping["research_pending"]),
            excluded_symbols=sorted(grouping["excluded"]),
            shadow_symbols=sorted(grouping["shadow"]),
            grouping={
                "high_conviction": sorted(grouping["high_conviction"]),
                "research_pending": sorted(grouping["research_pending"]),
                "excluded": sorted(grouping["excluded"]),
                "shadow": sorted(grouping["shadow"]),
            },
            evaluated_at=utc_now(),
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _compute_dimension_score(
        self,
        member: UniverseMember,
        dim_name: str,
        dim_policy: DimensionPolicy,
    ) -> DimensionScore:
        """计算单维度得分。

        Args:
            member: 截面成员
            dim_name: 维度名
            dim_policy: 维度 Policy

        Returns:
            DimensionScore
        """
        score = DimensionScore(
            dimension=dim_name,
            formula=dim_policy.score_formula,
            total_field_count=(
                len(dim_policy.weight_map)
                if hasattr(dim_policy.weight_map, "__len__") else 0
            ),
        )

        # 找到该维度对应的字段组
        field_group_name = self._dimension_to_field_group(dim_name)
        field_groups = getattr(member, "field_groups", {}) or {}
        field_group = field_groups.get(field_group_name) if hasattr(field_groups, "get") else None
        if field_group is None:
            score.is_missing = True
            score.missing_reason = "field_group_missing"
            return score

        # 收集该维度使用的字段（按 weight_map）
        field_scores: list[dict[str, Any]] = []
        used_count = 0
        weighted_sum = 0.0
        weight_total = 0.0

        weight_map = dim_policy.weight_map if hasattr(dim_policy.weight_map, "items") else {}
        for field_name, raw_weight in weight_map.items():
            try:
                weight = float(raw_weight)
            except (OverflowError, TypeError, ValueError):
                weight = 0.0
            if not isfinite(weight) or weight <= 0:
                continue
            snap_field = None
            fields = getattr(field_group, "fields", []) or []
            for f in fields:
                if f.name == field_name:
                    snap_field = f
                    break

            if snap_field is None:
                # 字段不存在 → 记为 missing
                field_scores.append({
                    "name": field_name,
                    "value": None,
                    "normalized_score": None,
                    "weight": weight,
                    "status": "missing",
                    "available_at": "",
                })
                continue

            field_value = snap_field.value
            try:
                numeric_value = float(field_value)
            except (OverflowError, TypeError, ValueError):
                numeric_value = None
            if (
                snap_field.status != DATA_AVAILABILITY_AVAILABLE
                or numeric_value is None
                or not isfinite(numeric_value)
            ):
                # 字段存在但不可用
                field_scores.append({
                    "name": field_name,
                    "value": field_value,
                    "normalized_score": None,
                    "weight": weight,
                    "status": snap_field.status,
                    "available_at": getattr(snap_field, "available_at", ""),
                })
                continue

            # 归一化
            norm_params = _get_normalization(field_name)
            normalized = _min_max_normalize(
                numeric_value,
                norm_params["min"],
                norm_params["max"],
                norm_params["higher_is_better"],
            )
            field_scores.append({
                "name": field_name,
                "value": numeric_value,
                "normalized_score": normalized,
                "weight": weight,
                "status": snap_field.status,
                "available_at": getattr(snap_field, "available_at", ""),
                "source_refs": list(getattr(snap_field, "source_refs", []) or []),
            })
            weighted_sum += normalized * weight
            weight_total += weight
            used_count += 1

        score.field_scores = field_scores
        score.used_field_count = used_count
        score.coverage_ratio = (
            used_count / score.total_field_count if score.total_field_count > 0 else 0.0
        )

        # 缺数据处理
        if used_count == 0:
            score.is_missing = True
            score.missing_reason = "no_field_available"
            return score

        # 低于最小覆盖率（50%）视为缺数据
        if score.coverage_ratio < 0.5:
            score.is_missing = True
            score.missing_reason = "below_min_coverage"
            score.note = f"coverage_ratio={score.coverage_ratio:.2f} < 0.5"
            return score

        # 按 missing_strategy 处理
        if dim_policy.missing_strategy == "drop" and used_count < score.total_field_count:
            score.is_missing = True
            score.missing_reason = "drop_strategy_triggered"
            return score

        if dim_policy.missing_strategy == "penalize":
            # 缺失字段按 0 计入
            penalty_weight = sum(
                w for fs in field_scores if fs["normalized_score"] is None
                for w in [fs["weight"]]
            )
            weight_total += penalty_weight

        if dim_policy.missing_strategy == "neutral":
            # 缺失字段按 neutral_fill 计入
            neutral_weight = 0.0
            for fs in field_scores:
                if fs["normalized_score"] is None:
                    weighted_sum += dim_policy.neutral_fill * fs["weight"]
                    neutral_weight += fs["weight"]
            weight_total += neutral_weight

        # exclude 策略：仅按已用字段归一化（默认）
        score.score = weighted_sum / weight_total if weight_total > 0 else None
        if score.score is not None and not isfinite(score.score):
            score.score = None
        if score.score is None:
            score.is_missing = True
            score.missing_reason = "weight_total_zero"

        return score

    def _dimension_to_field_group(self, dim_name: str) -> str:
        """维度名映射到字段组名。

        Args:
            dim_name: 维度名

        Returns:
            字段组名
        """
        mapping = {
            DIMENSION_QUALITY: FIELD_GROUP_FINANCIAL_QUALITY,
            DIMENSION_GROWTH: FIELD_GROUP_GROWTH,
            DIMENSION_EXPECTATION: FIELD_GROUP_CONSENSUS_REVISIONS,
            DIMENSION_VALUATION_BURDEN: FIELD_GROUP_VALUATION,
            DIMENSION_INDUSTRY_TIMING: FIELD_GROUP_INDUSTRY_TIMING,
            DIMENSION_EVIDENCE_QUALITY: FIELD_GROUP_EVIDENCE_QUALITY,
        }
        return mapping.get(dim_name, "")

    def _classify_company(self, result: CompanyFunnelResult) -> None:
        """判定公司入选状态。

        shadow policy 只产生 shadow_* 状态；
        active policy 才产生 include/exclude/pending_data 状态。

        Args:
            result: 待分类的公司结果（in-place 修改）
        """
        # 统计维度状态
        total_dim = len(DIMENSION_NAMES)
        available_dim = result.get_available_dimension_count()
        coverage = available_dim / total_dim if total_dim > 0 else 0.0

        # 检查门禁阈值（仅 active policy 才生效）
        is_gating = self.policy.is_gating()
        gating_failures: list[str] = []
        if is_gating:
            for dim_name in DIMENSION_NAMES:
                dp = self.policy.get_dimension(dim_name)
                if dp is None:
                    continue
                ds = result.dimension_scores.get(dim_name)
                if ds is None or ds.is_missing or ds.score is None:
                    continue
                if ds.score < dp.threshold_min:
                    gating_failures.append(
                        f"{dim_name}: score={ds.score:.3f} < threshold_min={dp.threshold_min}"
                    )
                if ds.score > dp.threshold_max:
                    gating_failures.append(
                        f"{dim_name}: score={ds.score:.3f} > threshold_max={dp.threshold_max}"
                    )

        # 缺数据公司 → pending_data（不被中性值抬高）
        if coverage < self.policy.high_conviction_threshold:
            result.status = STATUS_PENDING_DATA
            result.pending_data_reasons.append(
                f"dimension_coverage={coverage:.2f} < "
                f"high_conviction_threshold={self.policy.high_conviction_threshold}"
            )
            # 列出缺数据的维度
            for dim_name in DIMENSION_NAMES:
                ds = result.dimension_scores.get(dim_name)
                if ds is not None and ds.is_missing:
                    result.pending_data_reasons.append(
                        f"{dim_name}: {ds.missing_reason}"
                    )
            return

        # 门禁不通过 → exclude（仅 active）
        if is_gating and gating_failures:
            result.status = STATUS_EXCLUDE
            result.exclusion_reasons.extend(gating_failures)
            return

        # 高信念入选
        if is_gating:
            result.status = STATUS_INCLUDE
            result.inclusion_reasons.append(
                f"dimension_coverage={coverage:.2f} >= "
                f"threshold={self.policy.high_conviction_threshold}"
            )
            result.inclusion_reasons.append(
                "all dimension scores within gating thresholds"
            )
        else:
            # shadow policy → shadow_include（不产生正式门禁）
            result.status = STATUS_SHADOW_INCLUDE
            result.inclusion_reasons.append(
                f"shadow_policy: dimension_coverage={coverage:.2f} >= "
                f"threshold={self.policy.high_conviction_threshold}"
            )
            result.inclusion_reasons.append(
                "shadow_policy_does_not_produce_gating_decision"
            )

    def _compute_pareto_frontier(
        self,
        results: list[CompanyFunnelResult],
    ) -> tuple[set[str], dict[str, list[str]]]:
        """计算 Pareto frontier。

        在 policy.pareto_dimensions 指定的维度上找非劣解。
        公司 A 支配公司 B 当且仅当：A 在所有指定维度的得分都 >= B，
        且至少有一个维度严格 >。

        缺数据的维度不参与比较（视为不可比较，双方都不支配）。

        Args:
            results: 所有公司结果

        Returns:
            (pareto_symbols, dominated_map) 二元组
            - pareto_symbols: Pareto 非劣公司 symbol 集合
            - dominated_map: {symbol: [dominated_by_symbols]}
        """
        pareto_dims = self.policy.pareto_dimensions
        # 仅参与比较的公司：至少有一个维度有得分
        comparable = [
            r for r in results
            if any(
                not r.dimension_scores.get(d, DimensionScore(is_missing=True)).is_missing
                for d in pareto_dims
            )
        ]

        pareto_set: set[str] = set()
        dominated_map: dict[str, list[str]] = {r.symbol: [] for r in comparable}

        for r_a in comparable:
            dominated = False
            for r_b in comparable:
                if r_a.symbol == r_b.symbol:
                    continue
                # 检查 r_b 是否支配 r_a
                if self._dominates(r_b, r_a, pareto_dims):
                    dominated_map[r_a.symbol].append(r_b.symbol)
                    dominated = True
                    # 不 break：记录所有支配者
            if not dominated:
                pareto_set.add(r_a.symbol)

        return pareto_set, dominated_map

    def _dominates(
        self,
        r_a: CompanyFunnelResult,
        r_b: CompanyFunnelResult,
        dims: tuple[str, ...],
    ) -> bool:
        """检查 r_a 是否支配 r_b。

        r_a 支配 r_b 当且仅当：
        - 在所有可比维度上 r_a 得分 >= r_b
        - 至少有一个可比维度 r_a 得分 > r_b
        - 双方至少有一个可比维度

        缺数据的维度视为不可比，跳过。

        Args:
            r_a: 公司 A
            r_b: 公司 B
            dims: 参与维度

        Returns:
            True 表示 r_a 支配 r_b
        """
        any_ge = False
        any_gt = False
        any_comparable = False

        for d in dims:
            score_a = r_a.get_score(d)
            score_b = r_b.get_score(d)
            if score_a is None or score_b is None:
                # 任一缺数据 → 该维度不可比
                continue
            any_comparable = True
            if score_a > score_b:
                any_gt = True
                any_ge = True
            elif score_a == score_b:
                any_ge = True
            else:
                # r_a < r_b → r_a 不能支配 r_b
                return False

        # 必须至少有一个可比维度，且至少有一个严格大于
        return any_comparable and any_gt and any_ge

    def _group_results(
        self,
        results: list[CompanyFunnelResult],
    ) -> dict[str, list[str]]:
        """多维分组。

        分组规则：
        - high_conviction: status=include
        - shadow: status=shadow_include 或 shadow_exclude
        - research_pending: status=pending_data
        - excluded: status=exclude

        Args:
            results: 所有公司结果

        Returns:
            {group_name: [symbols]} 字典
        """
        grouping: dict[str, list[str]] = {
            "high_conviction": [],
            "shadow": [],
            "research_pending": [],
            "excluded": [],
        }
        for r in results:
            if r.status == STATUS_INCLUDE:
                grouping["high_conviction"].append(r.symbol)
            elif r.status in (STATUS_SHADOW_INCLUDE, STATUS_SHADOW_EXCLUDE):
                grouping["shadow"].append(r.symbol)
            elif r.status == STATUS_PENDING_DATA:
                grouping["research_pending"].append(r.symbol)
            elif r.status == STATUS_EXCLUDE:
                grouping["excluded"].append(r.symbol)
        return grouping

    def _compute_funnel_id(self, snapshot_id: str, as_of: str) -> str:
        """计算确定性 funnel_id。

        格式：funnel_<as_of>_<policy_id>_<policy_version>_<hash_short>

        Args:
            snapshot_id: 截面 ID
            as_of: 截面时点

        Returns:
            funnel_id 字符串
        """
        content = (
            f"{snapshot_id}|{as_of}|{self.policy.policy_id}|"
            f"{self.policy.version}|{self.policy.config_hash}"
        )
        hash_short = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        return f"funnel_{as_of}_{self.policy.policy_id}_{hash_short}"


__all__ = [
    # 状态常量
    "STATUS_INCLUDE",
    "STATUS_EXCLUDE",
    "STATUS_PENDING_DATA",
    "STATUS_SHADOW_INCLUDE",
    "STATUS_SHADOW_EXCLUDE",
    # 数据类
    "DimensionScore",
    "CompanyFunnelResult",
    "FunnelOutput",
    # 主类
    "FundamentalFunnel",
    # 工具
    "_min_max_normalize",
]
