"""新闻持续性主题检测器。

基于 6 维评分对 ThemePacket 进行新闻持续性检测，输出通过准入的主题。
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from .contracts import ThemeCatalyst, ThemeMember, ThemePacket

logger = logging.getLogger(__name__)


class NewsPersistenceDetector:
    """新闻持续性主题检测器。

    对装配好的 ThemePacket 进行新闻持续性评分与准入过滤，输出通过准入的主题。

    评分公式（6 维加权 - 惩罚）::

        news_score = 0.25×消息质量 + 0.20×独立来源 + 0.20×产业链可解释性
                   + 0.15×成分扩散 + 0.10×核心股连续性 + 0.10×成交连续性
                   - 0.5×crowding_score - 0.5×negative_risk_score

    准入条件（全部满足才输出）:
        1. 独立来源数 >= independent_source_threshold（非单一转载源）
        2. 至少一个明确事件（event_type ∈ {news/announcement/research_report/policy}）
        3. 成员数 >= min_members
        4. 消息质量 > 0（非纯标题匹配）
        5. 核心成员可被 Research 解释（有 research_claim_ids 或 market_role ∈ {core/leader}）
    """

    # 6 维权重（和为 1.0）
    WEIGHT_QUALITY = 0.25
    WEIGHT_INDEPENDENT = 0.20
    WEIGHT_CHAIN_EXPLAIN = 0.20
    WEIGHT_DIFFUSION = 0.15
    WEIGHT_CORE_CONTINUITY = 0.10
    WEIGHT_TRADING_CONTINUITY = 0.10

    # 惩罚系数
    PENALTY_CROWDING = 0.5
    PENALTY_NEGATIVE = 0.5

    # 满分阈值
    INDEPENDENT_SOURCE_FULL = 3  # 独立来源达到 3 即满分
    MEMBER_DIFFUSION_FULL = 5  # 成员数达到 5 即满分

    # 默认成交连续性（无外部行情数据时中性值，避免阻断）
    DEFAULT_TRADING_CONTINUITY = 0.5

    # 明确事件类型（非纯标题匹配）
    EXPLICIT_EVENT_TYPES = frozenset(
        {"news", "announcement", "research_report", "policy"}
    )

    # 可解释的市场角色
    EXPLAINABLE_ROLES = frozenset({"core", "leader"})

    def __init__(
        self,
        independent_source_threshold: int = 2,
        min_members: int = 2,
        core_continuity_days: int = 14,
    ) -> None:
        """初始化检测器。

        Args:
            independent_source_threshold: 独立来源数准入阈值，默认 2。
            min_members: 最小成员数准入阈值，默认 2。
            core_continuity_days: 核心股连续性窗口天数，默认 14。
        """
        self._independent_source_threshold = independent_source_threshold
        self._min_members = min_members
        self._core_continuity_days = core_continuity_days

    def detect(self, packet: ThemePacket, *, as_of: str = "") -> ThemePacket | None:
        """对单个 ThemePacket 进行新闻持续性检测。

        Args:
            packet: 待检测的主题包（不会被修改）。
            as_of: 检测基准时间（ISO 格式），空则使用当前 UTC 时间。

        Returns:
            通过准入时返回更新后的 ThemePacket（深拷贝，news_score/theme_type/phase
            已更新）；未通过准入时返回 None。
        """
        as_of_dt = self._resolve_as_of(as_of)
        dims = self._compute_dimensions(packet, as_of_dt)

        if not self._passes_admission(packet, dims):
            return None

        # 深拷贝，避免修改入参（防御性）
        new_packet = copy.deepcopy(packet)
        new_packet.news_score = self._compute_news_score(new_packet, dims)
        new_packet.theme_type = "news_persistence"
        new_packet.phase = "STARTUP"
        return new_packet

    def detect_many(
        self, packets: list[ThemePacket], *, as_of: str = ""
    ) -> list[ThemePacket]:
        """批量检测，返回通过准入的 ThemePacket 列表。

        Args:
            packets: 待检测的主题包列表。
            as_of: 检测基准时间（ISO 格式），空则使用当前 UTC 时间。

        Returns:
            通过准入的 ThemePacket 列表（顺序保留）。
        """
        results: list[ThemePacket] = []
        for packet in packets:
            detected = self.detect(packet, as_of=as_of)
            if detected is not None:
                results.append(detected)
        return results

    # ------------------------------------------------------------------ #
    # 维度计算
    # ------------------------------------------------------------------ #

    def _compute_dimensions(
        self, packet: ThemePacket, as_of_dt: datetime
    ) -> dict[str, float]:
        """计算所有评分维度。

        Args:
            packet: 主题包。
            as_of_dt: 基准时间（UTC aware）。

        Returns:
            维度分数字典，含准入判断所需的标志位。
        """
        quality = self._message_quality(packet.catalysts)
        independent_count = self._count_independent_sources(packet.catalysts)
        # 独立来源分：达到 INDEPENDENT_SOURCE_FULL 即满分
        independent_score = independent_count / max(
            independent_count, self.INDEPENDENT_SOURCE_FULL
        )
        chain_explain = self._chain_explainability(packet.core_members)
        diffusion = self._member_diffusion(packet.members)
        core_continuity = self._core_continuity(packet.core_members, as_of_dt)
        has_explicit = float(
            any(c.event_type in self.EXPLICIT_EVENT_TYPES for c in packet.catalysts)
        )
        research_explainable = float(
            any(
                m.research_claim_ids or m.market_role in self.EXPLAINABLE_ROLES
                for m in packet.core_members
            )
        )

        return {
            "quality": quality,
            "independent_count": float(independent_count),
            "independent_score": independent_score,
            "chain_explainability": chain_explain,
            "member_diffusion": diffusion,
            "core_continuity": core_continuity,
            "trading_continuity": self.DEFAULT_TRADING_CONTINUITY,
            "has_explicit_event": has_explicit,
            "research_explainable": research_explainable,
        }

    def _passes_admission(
        self, packet: ThemePacket, dims: dict[str, float]
    ) -> bool:
        """准入条件检查。

        Args:
            packet: 主题包。
            dims: 维度分数字典。

        Returns:
            通过全部准入条件返回 True，否则 False。
        """
        # 1. 不是单一转载源（独立来源数 >= 阈值）
        if dims["independent_count"] < self._independent_source_threshold:
            return False
        # 2. 至少存在一个明确事件
        if dims["has_explicit_event"] <= 0.0:
            return False
        # 3. 至少 min_members 个合理关联成员
        if len(packet.members) < self._min_members:
            return False
        # 4. 不只有标题关键词匹配（消息质量 > 0）
        if dims["quality"] <= 0.0:
            return False
        # 5. Research 可解释（核心成员有 research_claim_ids 或 market_role ∈ {core,leader}）
        if dims["research_explainable"] <= 0.0:
            return False
        return True

    def _compute_news_score(
        self, packet: ThemePacket, dims: dict[str, float]
    ) -> float:
        """计算 news_score（6 维加权 - 惩罚），限制在 [0.0, 1.0]。

        Args:
            packet: 主题包（用于读取 crowding/negative 分数）。
            dims: 维度分数字典。

        Returns:
            news_score ∈ [0.0, 1.0]。
        """
        score = (
            self.WEIGHT_QUALITY * dims["quality"]
            + self.WEIGHT_INDEPENDENT * dims["independent_score"]
            + self.WEIGHT_CHAIN_EXPLAIN * dims["chain_explainability"]
            + self.WEIGHT_DIFFUSION * dims["member_diffusion"]
            + self.WEIGHT_CORE_CONTINUITY * dims["core_continuity"]
            + self.WEIGHT_TRADING_CONTINUITY * dims["trading_continuity"]
            - self.PENALTY_CROWDING * packet.crowding_score
            - self.PENALTY_NEGATIVE * packet.negative_risk_score
        )
        return max(0.0, min(1.0, score))

    # ------------------------------------------------------------------ #
    # 单维打分
    # ------------------------------------------------------------------ #

    def _message_quality(self, catalysts: list[ThemeCatalyst]) -> float:
        """消息质量：明确事件催化剂占比。

        Args:
            catalysts: 催化剂列表。

        Returns:
            明确事件催化剂数 / 总催化剂数；空列表返回 0.0。
        """
        if not catalysts:
            return 0.0
        explicit = sum(
            1 for c in catalysts if c.event_type in self.EXPLICIT_EVENT_TYPES
        )
        return explicit / len(catalysts)

    def _count_independent_sources(self, catalysts: list[ThemeCatalyst]) -> int:
        """统计独立来源数。

        催化剂 source 可能是单来源或 "src1, src2" 合并形式，按逗号拆分去重。

        Args:
            catalysts: 催化剂列表。

        Returns:
            独立来源数。
        """
        sources: set[str] = set()
        for c in catalysts:
            if not c.source:
                continue
            for part in c.source.split(","):
                part = part.strip()
                if part:
                    sources.add(part)
        return len(sources)

    def _chain_explainability(self, core_members: list[ThemeMember]) -> float:
        """产业链可解释性：核心成员中具备 research_claim_ids 的比例。

        Args:
            core_members: 核心成员列表。

        Returns:
            具备 research_claim_ids 的成员占比；空列表返回 0.0。
        """
        if not core_members:
            return 0.0
        explained = sum(1 for m in core_members if m.research_claim_ids)
        return explained / len(core_members)

    def _member_diffusion(self, members: list[ThemeMember]) -> float:
        """成分扩散：成员数 / max(成员数, 5)。

        Args:
            members: 成员列表。

        Returns:
            扩散分数 ∈ [0.0, 1.0]；空列表返回 0.0。
        """
        count = len(members)
        return count / max(count, self.MEMBER_DIFFUSION_FULL)

    def _core_continuity(
        self, core_members: list[ThemeMember], as_of_dt: datetime
    ) -> float:
        """核心股连续性：核心成员 last_confirmed_at 在最近 N 天内的比例。

        Args:
            core_members: 核心成员列表。
            as_of_dt: 基准时间（UTC aware）。

        Returns:
            最近 N 天内确认的核心成员占比；空列表返回 0.0。
        """
        if not core_members:
            return 0.0
        recent_count = 0
        for m in core_members:
            confirmed = self._parse_datetime(m.last_confirmed_at)
            if confirmed is None:
                continue
            confirmed_utc = self._to_utc_aware(confirmed)
            # 排除未来日期，仅统计 [0, N] 天窗口内
            delta_days = (as_of_dt - confirmed_utc).total_seconds() / 86400.0
            if 0.0 <= delta_days <= self._core_continuity_days:
                recent_count += 1
        return recent_count / len(core_members)

    # ------------------------------------------------------------------ #
    # 时间工具
    # ------------------------------------------------------------------ #

    def _resolve_as_of(self, as_of: str) -> datetime:
        """解析基准时间，空或解析失败则使用当前 UTC 时间。

        Args:
            as_of: ISO 格式时间字符串。

        Returns:
            UTC aware datetime。
        """
        if as_of:
            dt = self._parse_datetime(as_of)
            if dt is not None:
                return self._to_utc_aware(dt)
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_datetime(s: str) -> datetime | None:
        """解析 ISO 格式日期字符串。

        Args:
            s: 日期字符串。

        Returns:
            datetime 对象，解析失败或空串返回 None。
        """
        if not s:
            return None
        try:
            # 兼容带 "Z" 后缀的 ISO 时间
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _to_utc_aware(dt: datetime) -> datetime:
        """转换为 UTC aware datetime。

        naive datetime 视为 UTC。

        Args:
            dt: 待转换的 datetime。

        Returns:
            UTC aware datetime。
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
