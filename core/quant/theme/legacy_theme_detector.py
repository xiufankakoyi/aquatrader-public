"""历史主题再发现检测器。

当一个历史主题重新出现价值时，将其标记为 legacy_reentry 类型。
判定条件（全部 AND）：
1. 历史主题曾有持续性（news_score >= 阈值 或 phase 曾经为 STARTUP/TREND）
2. 核心产业逻辑未被否定（历史 negative_risk_score < 阈值）
3. 核心公司映射仍有效（core_members 中至少一个 last_confirmed_at 在最近 N 天内）
4. 当前无阻断性负面信息（current negative_risk_score < 阈值）
5. 核心成员出现趋势内回撤或重新转强（technical_state 在允许集合内）
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone

from .contracts import ThemePacket, generate_theme_run_id
from .repository import ThemeRepository

logger = logging.getLogger(__name__)


class LegacyThemeDetector:
    """历史主题再发现检测器。

    通过 ThemeRepository 读取历史 ThemePacket，结合当前 packet 判断
    是否构成历史主题再发现（legacy_reentry）。
    """

    # 历史持续性：phase 集合
    _PERSISTENT_PHASES = frozenset({"STARTUP", "TREND"})

    # 允许的 technical_state 集合（趋势内回撤或重新转强）
    _ALLOWED_TECHNICAL_STATES = frozenset({
        "MA10_RETEST",
        "MA20_RETEST",
        "TREND_PULLBACK",
        "REACCELERATION",
    })

    # scan_legacy_themes 构造当前 packet 时使用的默认 technical_state
    _DEFAULT_SCAN_TECHNICAL_STATE = "TREND_PULLBACK"

    def __init__(
        self,
        repository: ThemeRepository,
        lookback_days: int = 90,
        min_history_persistence: float = 0.5,
        blocking_negative_threshold: float = 0.7,
        require_current_market_state: bool = False,
    ) -> None:
        """初始化检测器。

        Args:
            repository: ThemeRepository 实例，用于读取历史主题。
            lookback_days: 核心成员 last_confirmed_at 的回看天数。
            min_history_persistence: 历史持续性最小 news_score 阈值。
            blocking_negative_threshold: 阻断性负面风险分数阈值。
        """
        self._repository = repository
        self._lookback_days = lookback_days
        self._min_history_persistence = min_history_persistence
        self._blocking_negative_threshold = blocking_negative_threshold
        # Compatibility default preserves legacy tests/callers.  Production
        # wiring sets this True so a stored packet can never manufacture a
        # current pullback state.
        self._require_current_market_state = bool(require_current_market_state)

    def detect(self, current_packet: ThemePacket, *, as_of: str = "") -> ThemePacket | None:
        """对当前 ThemePacket 检测是否为历史主题再发现。

        Args:
            current_packet: 当前检测到的主题包（不会被修改）。
            as_of: 检测时间戳（ISO 格式），空串则使用当前 UTC 时间。

        Returns:
            满足条件时返回更新后的 ThemePacket（深拷贝，theme_type="legacy_reentry"，
            phase="LEGACY_REENTRY"，persistence_score 更新为历史 news_score）；
            否则返回 None。
        """
        if not current_packet.theme_id:
            return None

        as_of_dt = self._parse_as_of(as_of)

        # 读取历史版本（防御性：异常时返回空列表）
        historical_packets = self._load_historical_packets(
            current_packet.theme_id,
            exclude_run_id=current_packet.theme_run_id,
        )
        if not historical_packets:
            return None

        # 条件 1 & 2: 历史曾有持续性 且 核心逻辑未被否定
        reference_history = self._find_reference_history(historical_packets)
        if reference_history is None:
            return None

        # 条件 3: 核心公司映射仍有效
        if not self._has_valid_core_member(current_packet, reference_history, as_of_dt):
            return None

        # 条件 4: 当前无阻断性负面信息
        if current_packet.negative_risk_score >= self._blocking_negative_threshold:
            return None

        # 条件 5: 核心成员技术状态在允许集合内
        technical_state = current_packet.source_status.get("technical_state", "")
        if technical_state not in self._ALLOWED_TECHNICAL_STATES:
            return None

        # 全部条件满足，构建再发现 packet（深拷贝，不修改入参）
        reentry_packet = copy.deepcopy(current_packet)
        reentry_packet.theme_type = "legacy_reentry"
        reentry_packet.phase = "LEGACY_REENTRY"
        reentry_packet.persistence_score = reference_history.news_score
        return reentry_packet

    def detect_many(
        self, current_packets: list[ThemePacket], *, as_of: str = ""
    ) -> list[ThemePacket]:
        """批量检测历史主题再发现。

        Args:
            current_packets: 当前检测到的主题包列表。
            as_of: 检测时间戳。

        Returns:
            通过条件的 legacy_reentry ThemePacket 列表。
        """
        results: list[ThemePacket] = []
        for packet in current_packets:
            try:
                reentry = self.detect(packet, as_of=as_of)
            except Exception as exc:  # noqa: BLE001 — 防御性：单包失败不影响其他包
                logger.warning(
                    "detect_many failed for theme_id=%s: %s",
                    packet.theme_id,
                    exc,
                )
                continue
            if reentry is not None:
                results.append(reentry)
        return results

    def scan_legacy_themes(
        self,
        *,
        as_of: str = "",
        recent_days: int = 30,
        max_themes: int = 10,
    ) -> list[ThemePacket]:
        """主动扫描历史主题，寻找可能再发现的候选。

        从 repository 读取最近 recent_days 天内的历史 ThemePacket，
        对每个历史主题构造当前 packet（成员沿用历史 + technical_state 默认
        TREND_PULLBACK），调用 detect 判断是否为再发现。

        Args:
            as_of: 检测时间戳。
            recent_days: 历史回看天数。
            max_themes: 最多返回的主题数。

        Returns:
            再发现候选 ThemePacket 列表。
        """
        as_of_dt = self._parse_as_of(as_of)
        cutoff = as_of_dt - timedelta(days=recent_days)

        # 读取最近的历史 packets（防御性：异常时返回空列表）
        try:
            recent_packets = self._repository.list_recent_packets(limit=200)
        except Exception as exc:  # noqa: BLE001 — 防御性：读取异常返回空列表
            logger.warning("scan_legacy_themes read recent failed: %s", exc)
            return []

        # 按 theme_id 去重（list_recent_packets 已按 as_of DESC，保留最新）
        latest_by_theme: dict[str, ThemePacket] = {}
        for packet in recent_packets:
            if not packet.theme_id:
                continue
            packet_dt = self._parse_as_of(packet.as_of)
            if packet_dt < cutoff:
                continue
            if packet.theme_id in latest_by_theme:
                continue
            latest_by_theme[packet.theme_id] = packet

        # 构造当前 packet 并检测
        as_of_str = as_of if as_of else as_of_dt.isoformat()
        results: list[ThemePacket] = []
        for history_packet in latest_by_theme.values():
            current = self._build_scan_current_packet(history_packet, as_of_str)
            try:
                reentry = self.detect(current, as_of=as_of)
            except Exception as exc:  # noqa: BLE001 — 防御性：单主题失败不影响其他
                logger.warning(
                    "scan_legacy_themes detect failed for theme_id=%s: %s",
                    history_packet.theme_id,
                    exc,
                )
                continue
            if reentry is not None:
                results.append(reentry)
                if len(results) >= max_themes:
                    break
        return results

    def _load_historical_packets(
        self, theme_id: str, *, exclude_run_id: str = ""
    ) -> list[ThemePacket]:
        """从 repository 加载历史 ThemePacket。

        Args:
            theme_id: 主题 ID。
            exclude_run_id: 需排除的 theme_run_id（避免当前 packet 干扰）。

        Returns:
            历史 ThemePacket 列表，读取异常时返回空列表。
        """
        try:
            versions = self._repository.list_versions(theme_id)
        except Exception as exc:  # noqa: BLE001 — 防御性：读取异常不抛出
            logger.warning(
                "Load historical packets failed for theme_id=%s: %s",
                theme_id,
                exc,
            )
            return []
        if exclude_run_id:
            return [p for p in versions if p.theme_run_id != exclude_run_id]
        return versions

    def _find_reference_history(
        self, historical_packets: list[ThemePacket]
    ) -> ThemePacket | None:
        """找到满足历史持续性与未被否定条件的历史 packet。

        条件：
        - 历史曾有持续性：news_score >= min_history_persistence 或 phase in {STARTUP, TREND}
        - 核心逻辑未被否定：negative_risk_score < blocking_negative_threshold

        Args:
            historical_packets: 历史 ThemePacket 列表。

        Returns:
            满足条件且 news_score 最高的历史 packet；无满足时返回 None。
        """
        candidates: list[ThemePacket] = []
        for packet in historical_packets:
            had_persistence = (
                packet.news_score >= self._min_history_persistence
                or packet.phase in self._PERSISTENT_PHASES
            )
            if not had_persistence:
                continue
            if packet.negative_risk_score >= self._blocking_negative_threshold:
                continue
            candidates.append(packet)

        if not candidates:
            return None
        # 取 news_score 最高的作为历史持续性参考
        return max(candidates, key=lambda p: p.news_score)

    def _has_valid_core_member(
        self,
        current_packet: ThemePacket,
        reference_history: ThemePacket,
        as_of_dt: datetime,
    ) -> bool:
        """检查核心公司映射仍有效。

        core_members 中至少一个 last_confirmed_at 在最近 lookback_days 天内。
        优先使用当前 packet 的 core_members，为空时回退到历史 packet。

        Args:
            current_packet: 当前 ThemePacket。
            reference_history: 参考历史 ThemePacket。
            as_of_dt: 检测时间。

        Returns:
            核心成员仍有效返回 True，否则 False。
        """
        core_members = current_packet.core_members
        if not core_members:
            core_members = reference_history.core_members
        if not core_members:
            return False

        cutoff = as_of_dt - timedelta(days=self._lookback_days)
        for member in core_members:
            if not member.last_confirmed_at:
                continue
            confirmed_dt = self._parse_as_of(member.last_confirmed_at)
            if confirmed_dt >= cutoff:
                return True
        return False

    def _build_scan_current_packet(
        self, history_packet: ThemePacket, as_of_str: str
    ) -> ThemePacket:
        """为 scan_legacy_themes 构造当前 packet。

        成员沿用历史，technical_state 默认 TREND_PULLBACK，as_of 更新为当前。

        Args:
            history_packet: 历史 ThemePacket。
            as_of_str: 当前检测时间戳。

        Returns:
            构造的当前 ThemePacket（深拷贝，不修改历史 packet）。
        """
        current = copy.deepcopy(history_packet)
        # Production must obtain the state from current PIT bars.  The legacy
        # synthetic default is retained only behind an explicit compatibility
        # switch for older callers.
        technical_state = (
            "" if self._require_current_market_state
            else self._DEFAULT_SCAN_TECHNICAL_STATE
        )
        current.source_status = {
            **current.source_status,
            "technical_state": technical_state,
        }
        if self._require_current_market_state:
            current.degraded = True
            current.degradation_reasons.append(
                "legacy_requires_current_market_revalidation"
            )
        current.as_of = as_of_str
        # 生成新的 theme_run_id，避免与历史 packet 冲突（detect 会按 run_id 排除当前包）
        current.theme_run_id = generate_theme_run_id(history_packet.theme_id, as_of_str)
        return current

    @staticmethod
    def _parse_as_of(as_of: str) -> datetime:
        """解析时间戳字符串为 UTC datetime。

        支持带 Z 后缀的 ISO 格式；空串返回当前 UTC 时间；
        解析失败时回退到当前 UTC 时间并记录警告。

        Args:
            as_of: 时间戳字符串。

        Returns:
            时区感知的 UTC datetime。
        """
        if not as_of:
            return datetime.now(timezone.utc)
        # datetime.fromisoformat 在 3.11 之前不支持 "Z" 后缀，统一替换
        cleaned = as_of.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            logger.warning("Failed to parse as_of=%s, using now", as_of)
            return datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
