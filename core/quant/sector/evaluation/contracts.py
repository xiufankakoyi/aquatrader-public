"""板块轮动评价框架的数据契约。

将旧 ICE/反转/加速规则的固定阈值抽象为 PolicyVersion，旧规则降级为
feature/shadow，未通过 walk-forward 样本外验证前不得进入生产评分。

核心原则：
- PolicyVersion 分离参数与治理元数据（feature_version/policy_version/
  code_version/test_run_id/approved_by/activation_criteria）
- SectorFeatureRecord 持久化每日 PIT 板块特征
- EvaluationLabel 只在 evaluation 层生成，不回写事实层
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    """返回当前 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:16]}"


# 旧规则版本标识（标记 feature_version 和 policy_version）
# 旧规则指 sector_engine.py 的七阶段状态机、reversal_detector.py 的冰点反转、
# daily_screener.py 的均线筛选阈值。这些阈值未经 PIT 样本外验证，
# 默认为 shadow，需通过 walk-forward 评价和人工审批后才可进入生产评分。
LEGACY_SECTOR_ENGINE_FEATURE_VERSION: str = "sector_engine_v1_legacy"
LEGACY_SECTOR_ENGINE_POLICY_VERSION: str = "policy_sector_legacy_shadow_v1"

LEGACY_REVERSAL_DETECTOR_FEATURE_VERSION: str = "ice_reversal_v1_legacy"
LEGACY_REVERSAL_DETECTOR_POLICY_VERSION: str = "policy_reversal_legacy_shadow_v1"

LEGACY_DAILY_SCREENER_FEATURE_VERSION: str = "daily_screener_v1_legacy"
LEGACY_DAILY_SCREENER_POLICY_VERSION: str = "policy_screener_legacy_shadow_v1"

# 基准对比的 5 种基准名称（spec 阶段 2 要求）
BASELINE_CURRENT_THEME_RULE: str = "current_theme_rule"
BASELINE_SIMPLE_RELATIVE_STRENGTH: str = "simple_relative_strength"
BASELINE_SIMPLE_REVERSAL: str = "simple_reversal"
BASELINE_RANDOM_EQUAL_WEIGHT: str = "random_equal_weight"
BASELINE_NO_RESEARCH_ABLATION: str = "no_research_ablation"
BASELINE_WITH_RESEARCH_GATE: str = "with_research_gate"

BASELINE_NAMES: tuple[str, ...] = (
    BASELINE_CURRENT_THEME_RULE,
    BASELINE_SIMPLE_RELATIVE_STRENGTH,
    BASELINE_SIMPLE_REVERSAL,
    BASELINE_RANDOM_EQUAL_WEIGHT,
    BASELINE_NO_RESEARCH_ABLATION,
    BASELINE_WITH_RESEARCH_GATE,
)


@dataclass
class PolicyVersion:
    """策略版本契约（分离参数与治理元数据）。

    旧规则的固定阈值（如 DEFAULT_RALLY_THRESHOLD=1.5、REVERSAL_SCORE_MAX=0.3、
    READY_SCORE_THRESHOLD=0.6）通过 PolicyVersion 配置化，默认 status="shadow"，
    未通过 walk-forward 验证和人工审批前不得进入生产评分。

    P2.5/P2.6 新增字段：
    - activation_criteria_directions: 每个 criterion 的方向语义
      (higher_is_better/lower_is_better/range)，缺失默认 higher_is_better
    - required_metrics: 激活时 TestRunRecord.metrics 中必须包含的指标键，
      缺失任意一个则拒绝激活（防止用部分指标通过冒充整体通过）
    - data_as_of: 策略依赖的数据时点（YYYYMMDD），激活时必须与
      TestRunRecord.data_as_of 匹配（防止用过期数据激活新策略）

    Attributes:
        policy_version_id: 策略版本 ID（自动生成）。
        feature_version: 特征版本标识（如 sector_engine_v1_legacy）。
        policy_version: 策略版本标识（如 policy_sector_legacy_shadow_v1）。
        parameters: 策略参数（阈值/权重/天数等），与治理元数据分离。
        code_version: 代码版本（git commit hash 或语义版本）。
        test_run_id: 关联的 TestRunRecord ID，激活前必须服务端验证通过。
        approved_by: 审批人，空串表示未审批。
        activation_criteria: 激活条件（最低样本量/IC 阈值/置信区间等）。
        status: 状态（shadow / approved / active / rolled_back）。
        previous_version: 前一版本 ID，用于精确回滚。
        created_at: 创建时间。
        activated_at: 激活时间，空串表示未激活。
    """

    policy_version_id: str = field(default_factory=lambda: new_id("pv"))
    feature_version: str = ""
    policy_version: str = ""
    parameters: dict[str, float] = field(default_factory=dict)
    code_version: str = ""
    test_run_id: str = ""
    approved_by: str = ""
    activation_criteria: dict[str, float] = field(default_factory=dict)
    status: str = "shadow"  # shadow / approved / active / rolled_back
    previous_version: str = ""
    created_at: str = field(default_factory=utc_now)
    activated_at: str = ""
    # P2.6: 方向语义。值为 "higher_is_better" / "lower_is_better" / "range"。
    # 缺失键时默认 "higher_is_better"（向后兼容旧 PolicyVersion）。
    activation_criteria_directions: dict[str, str] = field(default_factory=dict)
    # P2.6: 必填指标键列表。激活时 TestRunRecord.metrics 必须包含全部键，
    # 缺失任意一个则拒绝激活（防止用部分指标通过冒充整体通过）。
    required_metrics: list[str] = field(default_factory=list)
    # P2.5: 数据时点（YYYYMMDD）。激活时核对 TestRunRecord.data_as_of 必须匹配。
    data_as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyVersion:
        """从字典反序列化，忽略未知字段。"""
        allowed = {
            "policy_version_id", "feature_version", "policy_version",
            "parameters", "code_version", "test_run_id", "approved_by",
            "activation_criteria", "status", "previous_version",
            "created_at", "activated_at",
            "activation_criteria_directions", "required_metrics", "data_as_of",
        }
        kwargs = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**kwargs)


@dataclass
class SectorFeatureRecord:
    """每日 PIT 板块特征记录。

    持久化每日板块的 phase/分数等特征，as_of 严格 PIT（不得写入未来日期）。
    未来表现标签不写入此表，只在 EvaluationLabel 中生成。

    Attributes:
        record_id: 记录 ID（自动生成）。
        as_of: PIT 时点（YYYYMMDD 或 ISO 日期）。
        sector_id: 板块 ID。
        sector_name: 板块名称。
        phase: 板块阶段（ice/recovery/startup/acceleration/climax/
            divergence/retreat/unknown）。
        phase_confidence: 阶段置信度 [0, 1]。
        momentum_score: 动量分数 [0, 1]。
        breadth_score: 广度分数 [0, 1]。
        turnover_score: 换手分数 [0, 1]。
        dispersion_score: 分歧分数 [0, 1]。
        feature_version: 特征版本（如 sector_engine_v1_legacy）。
        policy_version: 策略版本（如 policy_sector_legacy_shadow_v1）。
        degraded: 是否降级。
        degraded_fields: 降级字段列表。
        extra: 额外特征（如相对强度/扩散率/核心成员贡献集中度等）。
    """

    record_id: str = field(default_factory=lambda: new_id("sfr"))
    as_of: str = ""
    sector_id: str = ""
    sector_name: str = ""
    phase: str = "unknown"
    phase_confidence: float = 0.0
    momentum_score: float = 0.0
    breadth_score: float = 0.0
    turnover_score: float = 0.0
    dispersion_score: float = 0.0
    feature_version: str = ""
    policy_version: str = ""
    degraded: bool = False
    degraded_fields: list[str] = field(default_factory=list)
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)


@dataclass
class EvaluationLabel:
    """未来表现标签（只在 evaluation 层生成）。

    标签在事实发生后生成（如 5 日后才能生成 forward_return_5d），
    不回写事实层（不修改 SectorFeatureRecord 或 SectorAssessment）。
    标签与 Alpha 反馈分域保存，不修改 Claim 事实置信度。

    Attributes:
        label_id: 标签 ID（自动生成）。
        as_of: PIT 时点（被评价的日期）。
        sector_id: 板块 ID。
        horizon_days: 预测周期（如 5/10/20 日）。
        forward_return: 前瞻收益率（百分比）。
        forward_max_drawdown: 前瞻最大回撤（百分比，负数）。
        forward_volatility: 前瞻波动率（百分比）。
        benchmark_return: 基准收益率（百分比）。
        excess_return: 超额收益率（百分比）。
        label_version: 标签版本。
        generated_at: 标签生成时间（事实发生后）。
    """

    label_id: str = field(default_factory=lambda: new_id("evl"))
    as_of: str = ""
    sector_id: str = ""
    horizon_days: int = 5
    forward_return: float = 0.0
    forward_max_drawdown: float = 0.0
    forward_volatility: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    label_version: str = "evaluation_v1"
    generated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)


def make_legacy_sector_engine_policy() -> PolicyVersion:
    """构造旧七阶段板块引擎的 shadow PolicyVersion。

    旧规则的固定阈值通过 PolicyVersion 配置化，默认 status="shadow"，
    未通过 walk-forward 验证和人工审批前不得进入生产评分。

    Returns:
        status="shadow" 的 PolicyVersion 实例。
    """
    return PolicyVersion(
        feature_version=LEGACY_SECTOR_ENGINE_FEATURE_VERSION,
        policy_version=LEGACY_SECTOR_ENGINE_POLICY_VERSION,
        parameters={
            "history_days": 20.0,
            "rally_threshold": 1.5,
            "volume_spike_ratio": 1.5,
            "min_stocks_per_sector": 5.0,
        },
        activation_criteria={
            "min_sample_size": 60.0,
            "min_ic": 0.03,
            "min_sharpe": 0.5,
        },
        status="shadow",
    )


def make_legacy_reversal_detector_policy() -> PolicyVersion:
    """构造旧冰点反转检测器的 shadow PolicyVersion。

    Returns:
        status="shadow" 的 PolicyVersion 实例。
    """
    return PolicyVersion(
        feature_version=LEGACY_REVERSAL_DETECTOR_FEATURE_VERSION,
        policy_version=LEGACY_REVERSAL_DETECTOR_POLICY_VERSION,
        parameters={
            "reversal_score_max": 0.3,
            "breadth_score_min": 0.4,
            "negative_risk_max": 0.7,
            "required_consecutive_ice": 2.0,
        },
        activation_criteria={
            "min_sample_size": 40.0,
            "min_precision": 0.5,
            "min_recall": 0.3,
        },
        status="shadow",
    )


def make_legacy_daily_screener_policy() -> PolicyVersion:
    """构造旧主题日线筛选器的 shadow PolicyVersion。

    Returns:
        status="shadow" 的 PolicyVersion 实例。
    """
    return PolicyVersion(
        feature_version=LEGACY_DAILY_SCREENER_FEATURE_VERSION,
        policy_version=LEGACY_DAILY_SCREENER_POLICY_VERSION,
        parameters={
            "blocking_negative_threshold": 0.7,
            "warning_negative_threshold": 0.4,
            "crowding_block_threshold": 0.7,
            "trend_quality_threshold": 0.7,
            "ready_score_threshold": 0.6,
            "watch_score_threshold": 0.4,
            "acceleration_return_threshold": 0.15,
            "pullback_healthy_threshold": 0.05,
            "liquidity_turnover_threshold": 5e7,
        },
        activation_criteria={
            "min_sample_size": 100.0,
            "min_ready_precision": 0.5,
            "min_ic": 0.05,
        },
        status="shadow",
    )
