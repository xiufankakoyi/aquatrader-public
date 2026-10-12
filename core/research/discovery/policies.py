"""P2.2 基本面发现漏斗的版本化 Policy 配置。

按 P2 计划 §4 Task P2.2 要求：
- 维度权重只能通过版本化 policy 配置，不写死在页面
- 未经验证的权重只能 shadow，不得成为正式筛选门禁
- 每家公司返回入选、排除和待补数据原因
- 结果可解释到字段和公式

六个独立排序维度（P2.2 §98-105）：
1. quality：ROIC、现金兑现、杠杆、利润质量
2. growth：收入/利润增长、增量 ROIC、产品驱动持续性
3. expectation：共识修正、分歧和独立预测差异
4. valuation_burden：增长需要消化的估值负担
5. industry_timing：供需、资本开支、技术和政策阶段
6. evidence_quality：来源、PIT、独立性和完整度

Policy 状态机：
    draft → shadow → active → deprecated

硬门禁：
- status=shadow 的 policy 只能产生 shadow 评分，不得作为正式筛选门禁
- status=active 的 policy 才能产生 include/exclude 决策
- 同一 (policy_id, version) 不可覆盖
- Policy 必须含 policy_id / version / status / created_at / created_by
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _safe_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


# ---------------------------------------------------------------------------
# 维度名称常量（与 P2.2 §98-105 对齐）
# ---------------------------------------------------------------------------

DIMENSION_QUALITY: str = "quality"
DIMENSION_GROWTH: str = "growth"
DIMENSION_EXPECTATION: str = "expectation"
DIMENSION_VALUATION_BURDEN: str = "valuation_burden"
DIMENSION_INDUSTRY_TIMING: str = "industry_timing"
DIMENSION_EVIDENCE_QUALITY: str = "evidence_quality"

DIMENSION_NAMES: tuple[str, ...] = (
    DIMENSION_QUALITY,
    DIMENSION_GROWTH,
    DIMENSION_EXPECTATION,
    DIMENSION_VALUATION_BURDEN,
    DIMENSION_INDUSTRY_TIMING,
    DIMENSION_EVIDENCE_QUALITY,
)


# ---------------------------------------------------------------------------
# Policy 状态
# ---------------------------------------------------------------------------

POLICY_STATUS_DRAFT: str = "draft"
POLICY_STATUS_SHADOW: str = "shadow"
POLICY_STATUS_ACTIVE: str = "active"
POLICY_STATUS_DEPRECATED: str = "deprecated"

POLICY_STATUSES: tuple[str, ...] = (
    POLICY_STATUS_DRAFT,
    POLICY_STATUS_SHADOW,
    POLICY_STATUS_ACTIVE,
    POLICY_STATUS_DEPRECATED,
)

# 仅 active 状态可作为正式筛选门禁
GATING_STATUSES: tuple[str, ...] = (POLICY_STATUS_ACTIVE,)


def utc_now() -> str:
    """返回当前 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 单维度 Policy
# ---------------------------------------------------------------------------


@dataclass
class DimensionPolicy:
    """单维度 Policy：字段权重、阈值与门禁配置。

    设计要点：
    - 字段权重通过 weight_map 显式配置，不写死在代码
    - 阈值（threshold_min/max）用于门禁；shadow 状态不强制
    - 缺数据策略（missing_strategy）控制缺数据公司处理：
      * "neutral"：缺数据维度得中性分 0.5（默认）
      * "penalize"：缺数据维度得 0.0
      * "drop"：缺数据维度直接排除
      * "exclude"：缺数据公司在该维度不可比较，进入 pending_data
    - neutral_fill 仅在 missing_strategy="neutral" 时使用

    Attributes:
        dimension: 维度名（DIMENSION_NAMES 之一）
        weight_map: 字段名 → 权重（归一化由 funnel 处理）
        threshold_min: 门禁下界（< 此值标记 exclude，shadow 状态仅记录）
        threshold_max: 门禁上界（> 此值标记 exclude）
        missing_strategy: 缺数据策略
        neutral_fill: 中性填充值（默认 0.5）
        score_formula: 评分公式描述（人可读）
        description: 维度描述
    """

    dimension: str = ""
    weight_map: dict[str, float] = field(default_factory=dict)
    threshold_min: float = 0.0
    threshold_max: float = 1.0
    missing_strategy: str = "exclude"
    neutral_fill: float = 0.5
    score_formula: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "dimension": self.dimension,
            "weight_map": dict(self.weight_map),
            "threshold_min": self.threshold_min,
            "threshold_max": self.threshold_max,
            "missing_strategy": self.missing_strategy,
            "neutral_fill": self.neutral_fill,
            "score_formula": self.score_formula,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DimensionPolicy":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        raw_weights = data.get("weight_map", {}) or {}
        if not isinstance(raw_weights, Mapping):
            raw_weights = {}
        return cls(
            dimension=str(data.get("dimension", "")),
            weight_map={str(k): _safe_float(v, 0.0) for k, v in raw_weights.items()},
            threshold_min=_safe_float(data.get("threshold_min", 0.0), 0.0),
            threshold_max=_safe_float(data.get("threshold_max", 1.0), 1.0),
            missing_strategy=str(data.get("missing_strategy", "exclude")),
            neutral_fill=_safe_float(data.get("neutral_fill", 0.5), 0.5),
            score_formula=str(data.get("score_formula", "")),
            description=str(data.get("description", "")),
        )

    def validate(self) -> list[str]:
        """校验 DimensionPolicy 合法性。

        Returns:
            错误消息列表；空列表表示合法
        """
        errors: list[str] = []
        if self.dimension not in DIMENSION_NAMES:
            errors.append(
                f"非法维度名: {self.dimension}; 允许值: {DIMENSION_NAMES}"
            )
        if self.missing_strategy not in ("neutral", "penalize", "drop", "exclude"):
            errors.append(
                f"非法 missing_strategy: {self.missing_strategy}; "
                f"允许值: neutral/penalize/drop/exclude"
            )
        if not (0.0 <= self.neutral_fill <= 1.0):
            errors.append(f"neutral_fill 必须在 [0, 1]，当前: {self.neutral_fill}")
        if self.threshold_min > self.threshold_max:
            errors.append(
                f"threshold_min({self.threshold_min}) 不能大于 "
                f"threshold_max({self.threshold_max})"
            )
        # 权重必须非负
        for fname, w in self.weight_map.items():
            if w < 0:
                errors.append(f"字段 {fname} 权重为负: {w}")
        return errors


# ---------------------------------------------------------------------------
# 完整 Funnel Policy（6 维度集合）
# ---------------------------------------------------------------------------


@dataclass
class FunnelPolicy:
    """完整 Funnel Policy：6 维度配置 + 版本元数据。

    硬门禁：
    - policy_id + version 唯一标识一个 policy
    - status=shadow 时只能产生 shadow 评分，不能作为正式门禁
    - status=active 时才能产生 include/exclude 决策
    - status=deprecated 时仅保留历史，不应再被使用

    Attributes:
        policy_id: Policy 唯一标识（如 "default_funnel_policy"）
        version: Policy 版本号（如 "v1.0.0"）
        status: Policy 状态（draft/shadow/active/deprecated）
        dimensions: 6 维度配置 {dimension: DimensionPolicy}
        pareto_dimensions: 参与 Pareto frontier 计算的维度列表
        high_conviction_threshold: 高信念入选的维度覆盖率阈值（默认 0.8）
        created_at: 创建时间（UTC ISO）
        created_by: 创建者
        description: Policy 描述
        config_hash: 配置 hash（自动计算，用于复现性校验）
    """

    policy_id: str = ""
    version: str = ""
    status: str = POLICY_STATUS_DRAFT
    dimensions: dict[str, DimensionPolicy] = field(default_factory=dict)
    pareto_dimensions: tuple[str, ...] = DIMENSION_NAMES
    high_conviction_threshold: float = 0.8
    created_at: str = ""
    created_by: str = ""
    description: str = ""
    config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "status": self.status,
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "pareto_dimensions": list(self.pareto_dimensions),
            "high_conviction_threshold": self.high_conviction_threshold,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "description": self.description,
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FunnelPolicy":
        """从字典反序列化。"""
        data = data if isinstance(data, Mapping) else {}
        dims_raw = data.get("dimensions", {}) or {}
        if not isinstance(dims_raw, Mapping):
            dims_raw = {}
        return cls(
            policy_id=str(data.get("policy_id", "")),
            version=str(data.get("version", "")),
            status=str(data.get("status", POLICY_STATUS_DRAFT)),
            dimensions={
                str(k): DimensionPolicy.from_dict(v)
                for k, v in dims_raw.items()
                if isinstance(v, Mapping)
            },
            pareto_dimensions=tuple(
                item for item in (data.get("pareto_dimensions", DIMENSION_NAMES) or [])
                if isinstance(item, str)
            ),
            high_conviction_threshold=_safe_float(
                data.get("high_conviction_threshold", 0.8), 0.8
            ),
            created_at=str(data.get("created_at", "")),
            created_by=str(data.get("created_by", "")),
            description=str(data.get("description", "")),
            config_hash=str(data.get("config_hash", "")),
        )

    def validate(self) -> list[str]:
        """校验 FunnelPolicy 合法性。

        Returns:
            错误消息列表；空列表表示合法
        """
        errors: list[str] = []
        if not self.policy_id:
            errors.append("policy_id 不能为空")
        if not self.version:
            errors.append("version 不能为空")
        if self.status not in POLICY_STATUSES:
            errors.append(
                f"非法 status: {self.status}; 允许值: {POLICY_STATUSES}"
            )
        # 校验每个维度
        for dim_name, dim_policy in self.dimensions.items():
            if dim_name not in DIMENSION_NAMES:
                errors.append(f"非法维度名: {dim_name}")
                continue
            if dim_policy.dimension != dim_name:
                errors.append(
                    f"维度 {dim_name} 的 DimensionPolicy.dimension "
                    f"不匹配: {dim_policy.dimension}"
                )
            errors.extend(dim_policy.validate())
        # Pareto 维度必须都在 6 个维度内
        for dim in self.pareto_dimensions:
            if dim not in DIMENSION_NAMES:
                errors.append(f"pareto_dimensions 含非法维度: {dim}")
        # 高信念阈值合理
        if not (0.0 <= self.high_conviction_threshold <= 1.0):
            errors.append(
                f"high_conviction_threshold 必须在 [0, 1]，"
                f"当前: {self.high_conviction_threshold}"
            )
        return errors

    def compute_config_hash(self) -> str:
        """计算配置 hash（用于复现性校验）。

        Returns:
            形如 "pol_<sha256[:12]>" 的 hash 字符串
        """
        parts: list[str] = [
            f"policy_id={self.policy_id}",
            f"version={self.version}",
            f"status={self.status}",
            f"pareto_dimensions={','.join(self.pareto_dimensions)}",
            f"high_conviction_threshold={self.high_conviction_threshold}",
        ]
        for dim_name in DIMENSION_NAMES:
            dp = self.dimensions.get(dim_name)
            if dp is None:
                parts.append(f"{dim_name}=None")
            else:
                parts.append(
                    f"{dim_name}={dp.missing_strategy}|{dp.neutral_fill}|"
                    f"{dp.threshold_min}|{dp.threshold_max}|"
                    f"{','.join(f'{k}:{v}' for k, v in sorted(dp.weight_map.items()))}"
                )
        content = "|".join(parts)
        return f"pol_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"

    def is_gating(self) -> bool:
        """是否可作为正式筛选门禁（仅 active 状态）。"""
        return self.status in GATING_STATUSES

    def is_shadow(self) -> bool:
        """是否为 shadow 状态（仅记录评分，不产生门禁决策）。"""
        return self.status == POLICY_STATUS_SHADOW

    def get_dimension(self, name: str) -> DimensionPolicy | None:
        """获取单维度 Policy。

        Args:
            name: 维度名

        Returns:
            DimensionPolicy；不存在返回 None
        """
        return self.dimensions.get(name)


# ---------------------------------------------------------------------------
# 默认 Policy（shadow 版本，未经验证）
# ---------------------------------------------------------------------------


def build_default_shadow_policy() -> FunnelPolicy:
    """构建默认 shadow Policy（未经验证，仅用于 shadow 评分）。

    按 P2.2 要求「未经验证的权重只能 shadow，不得成为正式筛选门禁」，
    默认 Policy 状态为 shadow，不能作为正式筛选门禁。
    所有维度采用 missing_strategy="exclude"（缺数据公司不抬高中性值）。

    Returns:
        FunnelPolicy 实例（status=shadow）
    """
    policy = FunnelPolicy(
        policy_id="default_funnel_policy",
        version="v0.1.0-shadow",
        status=POLICY_STATUS_SHADOW,
        pareto_dimensions=DIMENSION_NAMES,
        high_conviction_threshold=0.8,
        created_at=utc_now(),
        created_by="system",
        description=(
            "默认 shadow Policy：未经验证的初始权重，仅用于 shadow 评分，"
            "不产生正式 include/exclude 门禁决策。"
            "缺数据维度采用 exclude 策略，避免缺数据公司被中性值抬高。"
        ),
        dimensions={
            DIMENSION_QUALITY: DimensionPolicy(
                dimension=DIMENSION_QUALITY,
                weight_map={
                    "roic": 0.3,
                    "ocf_to_net_income": 0.2,
                    "debt_to_equity": 0.2,
                    "gross_margin": 0.15,
                    "net_margin": 0.15,
                },
                threshold_min=0.3,
                threshold_max=1.0,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="weighted_sum(roic*0.3 + ocf_to_net_income*0.2 + ...)",
                description="ROIC、现金兑现、杠杆、利润质量",
            ),
            DIMENSION_GROWTH: DimensionPolicy(
                dimension=DIMENSION_GROWTH,
                weight_map={
                    "revenue_growth_yoy": 0.3,
                    "net_profit_growth_yoy": 0.3,
                    "incremental_roic": 0.2,
                    "eps_growth_yoy": 0.2,
                },
                threshold_min=0.3,
                threshold_max=1.0,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="weighted_sum(growth metrics)",
                description="收入/利润增长、增量 ROIC、产品驱动持续性",
            ),
            DIMENSION_EXPECTATION: DimensionPolicy(
                dimension=DIMENSION_EXPECTATION,
                weight_map={
                    "consensus_revision_direction": 0.4,
                    "consensus_dispersion": 0.3,
                    "independent_forecast_gap": 0.3,
                },
                threshold_min=0.3,
                threshold_max=1.0,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="weighted_sum(expectation metrics)",
                description="共识修正、分歧和独立预测差异",
            ),
            DIMENSION_VALUATION_BURDEN: DimensionPolicy(
                dimension=DIMENSION_VALUATION_BURDEN,
                weight_map={
                    "pe_to_growth": 0.4,
                    "ev_ebitda_to_growth": 0.3,
                    "pb_to_roe": 0.3,
                },
                threshold_min=0.0,
                threshold_max=0.7,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="1 - normalized(pe_to_growth, ev_ebitda_to_growth, pb_to_roe)",
                description="增长需要消化的估值负担（越低越好）",
            ),
            DIMENSION_INDUSTRY_TIMING: DimensionPolicy(
                dimension=DIMENSION_INDUSTRY_TIMING,
                weight_map={
                    "lifecycle_stage_score": 0.4,
                    "supply_demand_balance": 0.3,
                    "policy_tailwind": 0.3,
                },
                threshold_min=0.3,
                threshold_max=1.0,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="weighted_sum(industry timing metrics)",
                description="供需、资本开支、技术和政策阶段",
            ),
            DIMENSION_EVIDENCE_QUALITY: DimensionPolicy(
                dimension=DIMENSION_EVIDENCE_QUALITY,
                weight_map={
                    "source_count": 0.3,
                    "pit_compliance": 0.3,
                    "independence_score": 0.2,
                    "completeness_score": 0.2,
                },
                threshold_min=0.3,
                threshold_max=1.0,
                missing_strategy="exclude",
                neutral_fill=0.5,
                score_formula="weighted_sum(evidence quality metrics)",
                description="来源、PIT、独立性和完整度",
            ),
        },
    )
    policy.config_hash = policy.compute_config_hash()
    return policy


def build_empty_active_policy() -> FunnelPolicy:
    """构建空的 active Policy（无字段权重，仅供测试）。

    实际生产中 active Policy 需经验证后由人工配置激活，
    本函数仅作为测试用例与 schema 校验的占位。

    Returns:
        FunnelPolicy 实例（status=active，但 weight_map 全部为空）
    """
    policy = FunnelPolicy(
        policy_id="empty_active_policy",
        version="v0.0.1-active",
        status=POLICY_STATUS_ACTIVE,
        pareto_dimensions=DIMENSION_NAMES,
        high_conviction_threshold=0.8,
        created_at=utc_now(),
        created_by="system",
        description="空 active Policy（测试用），无字段权重，不产生实际门禁。",
        dimensions={
            dim: DimensionPolicy(dimension=dim, missing_strategy="exclude")
            for dim in DIMENSION_NAMES
        },
    )
    policy.config_hash = policy.compute_config_hash()
    return policy


# ---------------------------------------------------------------------------
# Policy 仓储（内存版，版本化）
# ---------------------------------------------------------------------------


class PolicyRepository:
    """Policy 仓储：内存版版本化存储。

    不变量：
    - 同 (policy_id, version) 不可覆盖
    - 仅返回最新版本（按 version 字符串排序）
    - shadow 状态 policy 不影响 active 状态 policy

    生产场景应替换为 SQLite 仓储（同 P1.5/P2.1 风格）。
    """

    def __init__(self) -> None:
        """初始化空仓储。"""
        self._policies: dict[tuple[str, str], FunnelPolicy] = {}

    def save(self, policy: FunnelPolicy) -> None:
        """保存 policy。

        Args:
            policy: 待保存的 FunnelPolicy

        Raises:
            ValueError: 当 (policy_id, version) 已存在
        """
        key = (policy.policy_id, policy.version)
        if key in self._policies:
            raise ValueError(
                f"Policy 已存在: policy_id={policy.policy_id}, "
                f"version={policy.version}（不可覆盖）"
            )
        # 保存前重新计算 config_hash 确保一致
        if not policy.config_hash:
            policy.config_hash = policy.compute_config_hash()
        self._policies[key] = policy

    def get(
        self,
        policy_id: str,
        version: str | None = None,
    ) -> FunnelPolicy | None:
        """获取 policy。

        Args:
            policy_id: Policy ID
            version: 版本号；None 表示最新版本

        Returns:
            FunnelPolicy；不存在返回 None
        """
        if version is not None:
            return self._policies.get((policy_id, version))
        # 取最新版本
        candidates = [
            (v, p) for (pid, v), p in self._policies.items() if pid == policy_id
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def list_policies(
        self,
        status: str | None = None,
    ) -> list[FunnelPolicy]:
        """列出所有 policy。

        Args:
            status: 状态过滤；None 表示所有状态

        Returns:
            FunnelPolicy 列表
        """
        if status is None:
            return list(self._policies.values())
        return [p for p in self._policies.values() if p.status == status]

    def list_active(self) -> list[FunnelPolicy]:
        """列出所有 active 状态 policy。"""
        return self.list_policies(status=POLICY_STATUS_ACTIVE)

    def list_shadow(self) -> list[FunnelPolicy]:
        """列出所有 shadow 状态 policy。"""
        return self.list_policies(status=POLICY_STATUS_SHADOW)


__all__ = [
    # 常量
    "DIMENSION_QUALITY",
    "DIMENSION_GROWTH",
    "DIMENSION_EXPECTATION",
    "DIMENSION_VALUATION_BURDEN",
    "DIMENSION_INDUSTRY_TIMING",
    "DIMENSION_EVIDENCE_QUALITY",
    "DIMENSION_NAMES",
    "POLICY_STATUS_DRAFT",
    "POLICY_STATUS_SHADOW",
    "POLICY_STATUS_ACTIVE",
    "POLICY_STATUS_DEPRECATED",
    "POLICY_STATUSES",
    "GATING_STATUSES",
    # 数据类
    "DimensionPolicy",
    "FunnelPolicy",
    # 工厂
    "build_default_shadow_policy",
    "build_empty_active_policy",
    # 仓储
    "PolicyRepository",
    # 工具
    "utc_now",
]
