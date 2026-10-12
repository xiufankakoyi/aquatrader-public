"""阶段5 联合 shadow 特征计算模块。

新增 8 个 shadow 特征，用于 walk-forward 样本外验证，不修改生产状态
（不修改 READY/WATCH/REJECTED，不修改生产总分，不输出交易动作）。

设计原则：
- 依赖反转（DIP）：通过 Provider 注入外部数据，本模块不直接依赖
  core.research.*，遵守 theme 包"禁止反向依赖 core.research"的约束。
- 防御性编程：缺数据返回 0.0 并标记 degraded，不抛异常。
- PIT 安全：所有 compute 只读取 as_of 时点已可见的数据，不读取未来数据。
- 只持久化特征值 / as_of / feature_version / policy_version。

8 个 shadow 特征：
1. earnings_revision       盈利预测修正（一致预期变化方向）
2. expectation_gap         预期差距（市场预期 vs 研究/实际）
3. eps_impact              EPS 影响度（盈利测算贡献）
4. theme_phase             主题阶段（SectorAssessment.phase 映射）
5. technical_state         技术状态（daily_screener 标签映射）
6. phase_technical_interaction  阶段×技术交互（交叉特征）
7. core_identity           核心身份（ThemeMember.market_role 映射）
8. research_evidence_quality   研究证据质量（grounding_status 统计）
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .contracts import ThemeMember, ThemePacket

logger = logging.getLogger(__name__)


# ======================================================================
# 版本标识
# ======================================================================

FEATURE_VERSION: str = "shadow_features_v1"
POLICY_VERSION: str = "policy_shadow_v1"


# ======================================================================
# Provider 类型别名（返回纯字典/原语，避免耦合 core.research）
# ======================================================================

# (symbol, as_of) -> 一致预期修正历史，每条含 {value_mid, as_of, revision_of}
# 返回空列表表示无数据
ExpectationRevisionProvider = Callable[[str, str], list[dict[str, Any]]]

# (symbol, as_of) -> 预期差距字典 {relative_gap_mid, status, available_at}
# 返回 None 表示无数据
ExpectationGapProvider = Callable[[str, str], dict[str, Any] | None]

# (symbol, as_of) -> 盈利影响字典 {eps_impact_mid, contribution_mid, status, available_at}
# 返回 None 表示无数据
EarningsImpactProvider = Callable[[str, str], dict[str, Any] | None]

# (symbol, as_of) -> 技术状态标签字符串（如 MA10_RETEST / TREND_BROKEN）
# 返回空串表示无数据
TechnicalStateProvider = Callable[[str, str], str]


# ======================================================================
# 结果对象
# ======================================================================

@dataclass
class ShadowFeatureResult:
    """单个 shadow 特征计算结果。

    Attributes:
        value: 特征值 [0, 1]，缺数据时为 0.0。
        degraded: 是否降级（数据缺失或异常）。
        reason: 降级原因，非降级时为空串。
    """

    value: float = 0.0
    degraded: bool = False
    reason: str = ""

    @classmethod
    def ok(cls, value: float) -> "ShadowFeatureResult":
        """构造正常结果，自动裁剪到 [0, 1]。"""
        return cls(value=_clamp01(value), degraded=False, reason="")

    @classmethod
    def degraded_zero(cls, reason: str) -> "ShadowFeatureResult":
        """构造降级结果（值 0.0 + 标记降级）。"""
        return cls(value=0.0, degraded=True, reason=reason)


def _clamp01(value: float) -> float:
    """将浮点数裁剪到 [0, 1]。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN 检测
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ======================================================================
# Provider 上下文（依赖注入容器）
# ======================================================================

@dataclass
class ShadowFeatureContext:
    """shadow 特征计算的 Provider 上下文（依赖注入）。

    通过 Provider 注入外部研究层数据，避免 theme 包反向依赖 core.research。
    所有 Provider 均为可选；缺失时对应特征降级返回 0.0。

    Attributes:
        expectation_revision_provider: 盈利预测修正历史 Provider。
        expectation_gap_provider: 预期差距 Provider。
        earnings_impact_provider: 盈利影响 Provider。
        technical_state_provider: 技术状态标签 Provider。
    """

    expectation_revision_provider: ExpectationRevisionProvider | None = None
    expectation_gap_provider: ExpectationGapProvider | None = None
    earnings_impact_provider: EarningsImpactProvider | None = None
    technical_state_provider: TechnicalStateProvider | None = None


# ======================================================================
# 映射常量表
# ======================================================================

# SectorPhase 字符串 -> 0-1 分数（主题阶段强度）
# 反映阶段对主题正面性的贡献：加速 > 启动 > 高潮 > 复苏 > 分歧 > 撤退 > 冰点 > 未知
PHASE_SCORE_MAP: dict[str, float] = {
    "acceleration": 0.90,
    "startup": 0.65,
    "climax": 0.70,
    "recovery": 0.35,
    "divergence": 0.45,
    "retreat": 0.20,
    "ice": 0.15,
    "unknown": 0.0,
}

# daily_screener technical_state 标签 -> 0-1 分数（技术状态强度）
# 反映技术面对价格的支撑程度
TECHNICAL_STATE_SCORE_MAP: dict[str, float] = {
    "REACCELERATION": 0.90,
    "MA5_HOLD": 0.80,
    "MA10_RETEST": 0.70,
    "TREND_PULLBACK": 0.60,
    "MA20_RETEST": 0.50,
    "MA30_RETEST": 0.40,
    "ACCELERATION_RISK": 0.30,
    "TREND_BROKEN": 0.10,
}

# ThemeMember.market_role -> 0-1 分数（核心身份强度）
MARKET_ROLE_SCORE_MAP: dict[str, float] = {
    "core": 1.0,
    "leader": 0.8,
    "secondary": 0.5,
    "diffusion": 0.3,
}

# grounding_status -> 权重（研究证据质量）
GROUNDING_WEIGHT_MAP: dict[str, float] = {
    "grounded": 1.0,
    "unverified": 0.5,
    "conflicted": 0.0,
}

# 预期差距缩放因子：relative_gap_mid 乘以此因子后映射到 0.5 ± delta
EXPECTATION_GAP_SCALE: float = 1.0

# 预期修正历史为空时的中性分
REVISION_NEUTRAL_SCORE: float = 0.5


# ======================================================================
# 辅助函数
# ======================================================================

def _find_member(symbol: str, packet: ThemePacket) -> ThemeMember | None:
    """从 ThemePacket 中查找指定 symbol 的成员。

    优先搜索 core_members，回退到 members 列表。

    Args:
        symbol: 证券代码。
        packet: 主题包。

    Returns:
        匹配的 ThemeMember；未找到返回 None。
    """
    for member in packet.core_members:
        if member.symbol == symbol:
            return member
    for member in packet.members:
        if member.symbol == symbol:
            return member
    return None


def _is_future(as_of: str, data_as_of: str) -> bool:
    """PIT 安全检查：判断 data_as_of 是否晚于 as_of（未来数据）。

    Args:
        as_of: 请求的 PIT 时点（YYYYMMDD 或 ISO）。
        data_as_of: 数据可用时点。

    Returns:
        True 表示 data_as_of 严格晚于 as_of（属于未来数据，不可见）。
    """
    normalized_request = str(as_of or "").replace("-", "").replace(":", "").replace("T", "")[:8]
    normalized_data = str(data_as_of or "").replace("-", "").replace(":", "").replace("T", "")[:8]
    if not normalized_request or not normalized_data:
        return False
    return normalized_data > normalized_request


# ======================================================================
# 8 个 shadow 特征计算器
# ======================================================================

class EarningsRevisionFeature:
    """盈利预测修正特征（一致预期变化方向）。

    从 expectations 模块的一致预期修正历史读取方向信号。
    上修 → 高分；下修 → 低分；无修正或无数据 → 降级 0.0。
    """

    NAME: str = "earnings_revision"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算盈利预测修正特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包（本特征不使用，保留接口一致性）。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del packet  # 本特征不依赖 packet
        if context is None or context.expectation_revision_provider is None:
            return ShadowFeatureResult.degraded_zero("expectation_revision_provider_missing")

        try:
            revisions = context.expectation_revision_provider(symbol, as_of)
        except Exception as exc:  # noqa: BLE001 — 防御性：Provider 异常降级
            logger.warning("earnings_revision Provider 异常: %s", exc)
            return ShadowFeatureResult.degraded_zero("provider_error")

        if not revisions:
            return ShadowFeatureResult.degraded_zero("no_revision_history")

        # 过滤掉 PIT 之后的未来数据
        visible = [
            r for r in revisions
            if not _is_future(as_of, str(r.get("as_of", "")))
        ]
        if not visible:
            return ShadowFeatureResult.degraded_zero("no_visible_revision")

        # 取最新两个版本比较方向
        sorted_revisions = sorted(
            visible,
            key=lambda r: str(r.get("as_of", "")),
        )
        if len(sorted_revisions) < 2:
            # 只有一个版本，无法判断修正方向，中性偏降级
            return ShadowFeatureResult.degraded_zero("insufficient_revision_pairs")

        latest = _safe_float(sorted_revisions[-1].get("value_mid"))
        previous = _safe_float(sorted_revisions[-2].get("value_mid"))
        if latest is None or previous is None:
            return ShadowFeatureResult.degraded_zero("revision_value_missing")

        if previous == 0:
            # 前值为零时按方向判定：上调为正
            score = 0.8 if latest > 0 else (0.2 if latest < 0 else REVISION_NEUTRAL_SCORE)
            return ShadowFeatureResult.ok(score)

        delta_ratio = (latest - previous) / abs(previous)
        # delta_ratio > 0 上修 → 高分；< 0 下修 → 低分
        score = REVISION_NEUTRAL_SCORE + delta_ratio
        return ShadowFeatureResult.ok(score)


class ExpectationGapFeature:
    """预期差距特征（市场预期 vs 研究/实际）。

    从 expectations 模块的 ExpectationGap.relative_gap 读取。
    正向差距（研究 > 预期，潜在上行）→ 高分；负向差距 → 低分。
    """

    NAME: str = "expectation_gap"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算预期差距特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包（本特征不使用）。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del packet
        if context is None or context.expectation_gap_provider is None:
            return ShadowFeatureResult.degraded_zero("expectation_gap_provider_missing")

        try:
            gap_data = context.expectation_gap_provider(symbol, as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("expectation_gap Provider 异常: %s", exc)
            return ShadowFeatureResult.degraded_zero("provider_error")

        if not gap_data:
            return ShadowFeatureResult.degraded_zero("no_expectation_gap")

        # PIT 检查：数据可用时点不得晚于 as_of
        available_at = str(gap_data.get("available_at", ""))
        if _is_future(as_of, available_at):
            return ShadowFeatureResult.degraded_zero("gap_data_in_future")

        status = str(gap_data.get("status", ""))
        if status in ("insufficient_data", "expectation_data_missing"):
            return ShadowFeatureResult.degraded_zero(f"gap_status_{status}")

        relative_gap_mid = _safe_float(gap_data.get("relative_gap_mid"))
        if relative_gap_mid is None:
            return ShadowFeatureResult.degraded_zero("relative_gap_missing")

        # 正向差距 → 高分；负向差距 → 低分
        score = 0.5 + relative_gap_mid * EXPECTATION_GAP_SCALE
        return ShadowFeatureResult.ok(score)


class EpsImpactFeature:
    """EPS 影响度特征（盈利测算对 EPS 的贡献）。

    从 earnings 模块的 EarningsImpact 读取 eps_impact 和 contribution_to_existing_profit。
    贡献占比越高 → 分数越高。
    """

    NAME: str = "eps_impact"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算 EPS 影响度特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包（本特征不使用）。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del packet
        if context is None or context.earnings_impact_provider is None:
            return ShadowFeatureResult.degraded_zero("earnings_impact_provider_missing")

        try:
            impact_data = context.earnings_impact_provider(symbol, as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("eps_impact Provider 异常: %s", exc)
            return ShadowFeatureResult.degraded_zero("provider_error")

        if not impact_data:
            return ShadowFeatureResult.degraded_zero("no_earnings_impact")

        # PIT 检查
        available_at = str(impact_data.get("available_at", ""))
        if _is_future(as_of, available_at):
            return ShadowFeatureResult.degraded_zero("impact_data_in_future")

        status = str(impact_data.get("status", ""))
        if status in ("insufficient_data",):
            return ShadowFeatureResult.degraded_zero(f"impact_status_{status}")

        # 优先使用 contribution_to_existing_profit（0-1 比例，直接映射）
        contribution_mid = _safe_float(impact_data.get("contribution_mid"))
        if contribution_mid is not None:
            # contribution 已是 0-1 比例，正值越大分数越高
            return ShadowFeatureResult.ok(contribution_mid)

        # 回退：用 eps_impact 归一化（按绝对值缩放，1% 影响 → 满分）
        eps_impact_mid = _safe_float(impact_data.get("eps_impact_mid"))
        if eps_impact_mid is None:
            return ShadowFeatureResult.degraded_zero("eps_impact_missing")

        score = min(abs(eps_impact_mid) / 0.01, 1.0)
        return ShadowFeatureResult.ok(score)


class ThemePhaseFeature:
    """主题阶段特征（SectorAssessment.phase 映射）。

    从 ThemePacket.phase 读取阶段字符串，映射为 0-1 分数。
    """

    NAME: str = "theme_phase"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算主题阶段特征值。

        Args:
            symbol: 证券代码（本特征不使用）。
            packet: 主题包。
            as_of: PIT 时点（本特征不使用）。
            context: Provider 上下文（本特征不使用）。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del symbol, as_of, context
        phase = str(packet.phase or "").lower().strip()
        if not phase or phase == "unknown":
            return ShadowFeatureResult.degraded_zero("phase_unknown")
        score = PHASE_SCORE_MAP.get(phase)
        if score is None:
            return ShadowFeatureResult.degraded_zero(f"phase_unmapped_{phase}")
        return ShadowFeatureResult.ok(score)


class TechnicalStateFeature:
    """技术状态特征（daily_screener technical_state 标签映射）。

    从 Provider 获取技术状态标签，映射为 0-1 分数。
    """

    NAME: str = "technical_state"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算技术状态特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包（本特征不使用）。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del packet
        if context is None or context.technical_state_provider is None:
            return ShadowFeatureResult.degraded_zero("technical_state_provider_missing")

        try:
            tech_label = context.technical_state_provider(symbol, as_of)
        except Exception as exc:  # noqa: BLE001
            logger.warning("technical_state Provider 异常: %s", exc)
            return ShadowFeatureResult.degraded_zero("provider_error")

        tech_label = str(tech_label or "").upper().strip()
        if not tech_label:
            return ShadowFeatureResult.degraded_zero("technical_state_empty")
        score = TECHNICAL_STATE_SCORE_MAP.get(tech_label)
        if score is None:
            return ShadowFeatureResult.degraded_zero(f"technical_state_unmapped_{tech_label}")
        return ShadowFeatureResult.ok(score)


class PhaseTechnicalInteractionFeature:
    """阶段×技术交互特征（theme_phase × technical_state 交叉特征）。

    组合主题阶段与技术状态，反映"基本面+技术面"共振程度。
    值 = theme_phase_score × technical_state_score ∈ [0, 1]。
    """

    NAME: str = "phase_technical_interaction"

    def __init__(self) -> None:
        """初始化，复用 theme_phase 和 technical_state 计算器。"""
        self._phase_feature = ThemePhaseFeature()
        self._tech_feature = TechnicalStateFeature()

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算阶段×技术交互特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        phase_result = self._phase_feature.compute(
            symbol=symbol, packet=packet, as_of=as_of, context=context,
        )
        tech_result = self._tech_feature.compute(
            symbol=symbol, packet=packet, as_of=as_of, context=context,
        )
        # 任一降级时交互特征也降级
        reasons = []
        if phase_result.degraded:
            reasons.append(f"phase:{phase_result.reason}")
        if tech_result.degraded:
            reasons.append(f"tech:{tech_result.reason}")
        if reasons:
            return ShadowFeatureResult.degraded_zero(";".join(reasons))
        # 交叉特征 = 两者乘积
        return ShadowFeatureResult.ok(phase_result.value * tech_result.value)


class CoreIdentityFeature:
    """核心身份特征（ThemeMember.market_role 映射）。

    从 packet 成员中查找 symbol 的 market_role，映射为 0-1 分数。
    core=1.0 / leader=0.8 / secondary=0.5 / diffusion=0.3。
    """

    NAME: str = "core_identity"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算核心身份特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包。
            as_of: PIT 时点（本特征不使用）。
            context: Provider 上下文（本特征不使用）。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del as_of, context
        member = _find_member(symbol, packet)
        if member is None:
            return ShadowFeatureResult.degraded_zero("member_not_found")
        role = str(member.market_role or "").lower().strip()
        if not role:
            return ShadowFeatureResult.degraded_zero("market_role_empty")
        score = MARKET_ROLE_SCORE_MAP.get(role)
        if score is None:
            return ShadowFeatureResult.degraded_zero(f"role_unmapped_{role}")
        return ShadowFeatureResult.ok(score)


class ResearchEvidenceQualityFeature:
    """研究证据质量特征（MembershipEvidence grounding_status 统计）。

    从 packet 成员的 membership_evidence 字典列表中统计 grounding_status，
    grounded=1.0 / unverified=0.5 / conflicted=0.0，取加权平均。
    """

    NAME: str = "research_evidence_quality"

    def compute(
        self,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> ShadowFeatureResult:
        """计算研究证据质量特征值。

        Args:
            symbol: 证券代码。
            packet: 主题包。
            as_of: PIT 时点（本特征不使用）。
            context: Provider 上下文（本特征不使用）。

        Returns:
            ShadowFeatureResult，value ∈ [0, 1]。
        """
        del as_of, context
        member = _find_member(symbol, packet)
        if member is None:
            return ShadowFeatureResult.degraded_zero("member_not_found")

        evidence_list = member.membership_evidence or []
        if not evidence_list:
            return ShadowFeatureResult.degraded_zero("no_membership_evidence")

        # PIT 过滤：排除 valid_from 晚于 as_of 的证据（防御性，evidence 已嵌入 packet
        # 时通常已 PIT 安全，此处二次校验）
        total_weight = 0.0
        count = 0
        for evidence in evidence_list:
            if not isinstance(evidence, dict):
                continue
            grounding = str(evidence.get("grounding_status", "")).lower().strip()
            weight = GROUNDING_WEIGHT_MAP.get(grounding)
            if weight is None:
                # 未知 grounding_status 按未验证处理
                weight = GROUNDING_WEIGHT_MAP["unverified"]
            total_weight += weight
            count += 1

        if count == 0:
            return ShadowFeatureResult.degraded_zero("no_valid_evidence_entry")
        return ShadowFeatureResult.ok(total_weight / count)


def _safe_float(value: Any) -> float | None:
    """安全转浮点，失败返回 None。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


# ======================================================================
# ShadowFeatureSet 聚合
# ======================================================================

# shadow 特征名顺序（用于有序输出）
SHADOW_FEATURE_NAMES: tuple[str, ...] = (
    "earnings_revision",
    "expectation_gap",
    "eps_impact",
    "theme_phase",
    "technical_state",
    "phase_technical_interaction",
    "core_identity",
    "research_evidence_quality",
)


@dataclass
class ShadowFeatureSet:
    """shadow 特征聚合集，输出 dict + feature_version + policy_version。

    不修改生产状态（READY/WATCH/REJECTED 不变），不修改生产总分，
    不输出交易动作。只持久化特征值 / as_of / feature_version / policy_version。

    Attributes:
        as_of: PIT 时点。
        symbol: 证券代码。
        features: {feature_name: float} 特征值字典。
        degraded: 是否整体降级。
        degraded_features: 降级特征名列表。
        degradation_reasons: {feature_name: reason} 降级原因。
        feature_version: 特征版本标识。
        policy_version: 策略版本标识。
    """

    as_of: str = ""
    symbol: str = ""
    features: dict[str, float] = field(default_factory=dict)
    degraded: bool = False
    degraded_features: list[str] = field(default_factory=list)
    degradation_reasons: dict[str, str] = field(default_factory=dict)
    feature_version: str = FEATURE_VERSION
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（只包含可持久化字段）。"""
        return {
            "as_of": self.as_of,
            "symbol": self.symbol,
            "features": dict(self.features),
            "degraded": self.degraded,
            "degraded_features": list(self.degraded_features),
            "degradation_reasons": dict(self.degradation_reasons),
            "feature_version": self.feature_version,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShadowFeatureSet":
        """从字典反序列化，忽略未知字段。"""
        allowed = {
            "as_of", "symbol", "features", "degraded",
            "degraded_features", "degradation_reasons",
            "feature_version", "policy_version",
        }
        kwargs = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**kwargs)

    @classmethod
    def compute_all(
        cls,
        *,
        symbol: str,
        packet: ThemePacket,
        as_of: str,
        context: ShadowFeatureContext | None = None,
    ) -> "ShadowFeatureSet":
        """计算全部 8 个 shadow 特征并聚合。

        Args:
            symbol: 证券代码。
            packet: 主题包。
            as_of: PIT 时点。
            context: Provider 上下文。

        Returns:
            ShadowFeatureSet 实例。
        """
        calculators = _build_calculators()
        features: dict[str, float] = {}
        degraded_features: list[str] = []
        degradation_reasons: dict[str, str] = {}

        for name, calculator in calculators:
            try:
                result = calculator.compute(
                    symbol=symbol, packet=packet, as_of=as_of, context=context,
                )
            except Exception as exc:  # noqa: BLE001 — 防御性：任何异常降级
                logger.warning("shadow 特征 %s 计算异常: %s", name, exc)
                result = ShadowFeatureResult.degraded_zero("compute_exception")
            features[name] = result.value
            if result.degraded:
                degraded_features.append(name)
                degradation_reasons[name] = result.reason

        return cls(
            as_of=as_of,
            symbol=symbol,
            features=features,
            degraded=bool(degraded_features),
            degraded_features=degraded_features,
            degradation_reasons=degradation_reasons,
            feature_version=FEATURE_VERSION,
            policy_version=POLICY_VERSION,
        )


def _build_calculators() -> list[tuple[str, Any]]:
    """构造 8 个 shadow 特征计算器实例列表。

    Returns:
        [(feature_name, calculator_instance), ...] 按固定顺序。
    """
    return [
        (EarningsRevisionFeature.NAME, EarningsRevisionFeature()),
        (ExpectationGapFeature.NAME, ExpectationGapFeature()),
        (EpsImpactFeature.NAME, EpsImpactFeature()),
        (ThemePhaseFeature.NAME, ThemePhaseFeature()),
        (TechnicalStateFeature.NAME, TechnicalStateFeature()),
        (PhaseTechnicalInteractionFeature.NAME, PhaseTechnicalInteractionFeature()),
        (CoreIdentityFeature.NAME, CoreIdentityFeature()),
        (ResearchEvidenceQualityFeature.NAME, ResearchEvidenceQualityFeature()),
    ]


# ======================================================================
# Task 5.3: CoreIdentityObservation 核心股身份版本化
# ======================================================================

@dataclass
class CoreIdentityObservation:
    """核心股身份观察记录（版本化 shadow 观测）。

    身份衰退规则先作为 shadow 特征（不进入生产），龙虎榜只作为市场特征
    （不作为事实 Evidence）。

    Attributes:
        identity_as_of: 身份观察时点（YYYYMMDD 或 ISO）。
        symbol: 证券代码。
        theme_id: 关联主题 ID。
        market_role: 市场角色（core/leader/secondary/diffusion）。
        evidence_ids: 支撑该身份的证据 ID 列表。
        source_providers: 证据来源提供方列表。
        valid_from: 身份生效起始时间。
        valid_until: 身份失效时间（UNKNOWN 时为空串）。
        feature_version: 特征版本标识。
        policy_version: 策略版本标识。
    """

    identity_as_of: str = ""
    symbol: str = ""
    theme_id: str = ""
    market_role: str = "secondary"
    evidence_ids: list[str] = field(default_factory=list)
    source_providers: list[str] = field(default_factory=list)
    valid_from: str = ""
    valid_until: str = ""
    feature_version: str = FEATURE_VERSION
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoreIdentityObservation":
        """从字典反序列化，忽略未知字段。"""
        allowed = {
            "identity_as_of", "symbol", "theme_id", "market_role",
            "evidence_ids", "source_providers", "valid_from", "valid_until",
            "feature_version", "policy_version",
        }
        kwargs = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**kwargs)

    @classmethod
    def from_member(
        cls,
        member: ThemeMember,
        theme_id: str,
        as_of: str,
    ) -> "CoreIdentityObservation":
        """从 ThemeMember 构造 CoreIdentityObservation。

        提取 market_role、证据 ID 和来源提供方，生成版本化身份观察记录。
        龙虎榜等市场特征不作为事实 Evidence，不进入 evidence_ids。

        Args:
            member: 主题成员。
            theme_id: 关联主题 ID。
            as_of: 观察时点。

        Returns:
            CoreIdentityObservation 实例。
        """
        evidence_ids: list[str] = []
        source_providers: list[str] = []
        for evidence in member.membership_evidence or []:
            if not isinstance(evidence, dict):
                continue
            # 只采集有可定位材料支撑的事实证据，排除龙虎榜等市场特征
            source_type = str(evidence.get("source_type", "")).lower()
            if source_type in ("dragon_tiger", "market_data", "kline"):
                # 龙虎榜/K线/成交数据不作为事实 Evidence
                continue
            evidence_id = str(evidence.get("evidence_id", ""))
            if evidence_id:
                evidence_ids.append(evidence_id)
            provider = str(evidence.get("provider", ""))
            if provider and provider not in source_providers:
                source_providers.append(provider)

        return cls(
            identity_as_of=as_of,
            symbol=member.symbol,
            theme_id=theme_id,
            market_role=str(member.market_role or "secondary").lower(),
            evidence_ids=evidence_ids,
            source_providers=source_providers,
            valid_from=str(member.first_seen_at or ""),
            valid_until=str(member.last_confirmed_at or ""),
            feature_version=FEATURE_VERSION,
            policy_version=POLICY_VERSION,
        )

    def is_expired(self, as_of: str) -> bool:
        """判断身份是否已过期（valid_until 早于 as_of）。

        Args:
            as_of: 检查时点。

        Returns:
            True 表示已过期；valid_until 为空（UNKNOWN）时返回 False。
        """
        if not self.valid_until:
            return False
        normalized_check = str(as_of or "").replace("-", "").replace(":", "").replace("T", "")[:8]
        normalized_until = str(self.valid_until).replace("-", "").replace(":", "").replace("T", "")[:8]
        if not normalized_check or not normalized_until:
            return False
        return normalized_until < normalized_check
