"""冰点反转检测器。

在主题进入冰点（ICE）阶段后，通过多重条件判断是否出现反转迹象，
连续确认后将主题升级为 REVERSAL_CANDIDATE。

核心原则：不能因为跌得多就输出，必须满足多重条件。
"""
from __future__ import annotations

import copy
import logging

from .contracts import ThemePacket

logger = logging.getLogger(__name__)


class IceReversalDetector:
    """冰点反转检测器。

    对 ThemePacket 进行冰点反转检测。主题候选满足冰点反转的条件（全部 AND）：
    - 历史位置处于低分位（reversal_score < 0.3）
    - 相对强度不再创新低（breadth_score > 0.4）
    - 核心成员率先企稳（core_members 中至少一个 market_role in {"core","leader"}）
    - 没有 blocking 级负面信息（negative_risk_score < 0.7）

    连续确认机制：至少连续两个采样周期返回 ICE 后，第三次才可能升级为
    REVERSAL_CANDIDATE。上涨家数/MA20 上方比例连续改善通过 _ice_history
    队列中的连续 ICE 记录体现；成交从极度萎缩温和恢复由外部填充到
    reversal_score 中体现，避免重复设计。
    """

    # 反转条件阈值
    REVERSAL_SCORE_MAX = 0.3  # reversal_score 严格小于此值视为历史低分位
    BREADTH_SCORE_MIN = 0.4  # breadth_score 严格大于此值视为广度企稳
    NEGATIVE_RISK_MAX = 0.7  # negative_risk_score 严格小于此值视为无 blocking
    CORE_ROLES = ("core", "leader")  # 核心成员角色集合
    REQUIRED_CONSECUTIVE_ICE = 2  # 升级所需的连续 ICE 次数

    def __init__(
        self,
        history_limit: int = 5,
        *,
        repository: object | None = None,
        require_research_validated_core: bool = False,
    ) -> None:
        """初始化冰点反转检测器。

        Args:
            history_limit: 每个 theme_id 的历史队列长度上限。

        Raises:
            ValueError: history_limit 小于所需的连续 ICE 次数。
        """
        if history_limit < self.REQUIRED_CONSECUTIVE_ICE:
            raise ValueError(
                f"history_limit must be >= {self.REQUIRED_CONSECUTIVE_ICE}, "
                f"got {history_limit}"
            )
        self._history_limit = history_limit
        self._repository = repository
        self._require_research_validated_core = bool(require_research_validated_core)
        # theme_id -> [(as_of, phase), ...]
        self._ice_history: dict[str, list[tuple[str, str]]] = {}

    def detect(
        self, packet: ThemePacket, *, as_of: str = ""
    ) -> ThemePacket | None:
        """对单个 ThemePacket 进行冰点反转检测。

        返回规则：
        - 未达反转条件且无历史 ICE 记录：返回 None（首次进入，需观察）
        - 已有 ICE 历史，本次仍不满足反转条件：返回 phase="ICE" 的 packet（保持观察）
        - 满足反转条件且 ICE 历史连续 >= 2 次：返回 phase="REVERSAL_CANDIDATE" 的 packet
        - 满足反转条件但 ICE 历史不足 2 次：返回 phase="ICE" 的 packet（继续观察）

        Args:
            packet: 待检测的主题包（不会被修改）。
            as_of: 检测时间戳，为空时使用 packet.as_of。

        Returns:
            检测后的 ThemePacket（phase 为 ICE 或 REVERSAL_CANDIDATE），
            或 None（首次进入且未达反转条件时）。
        """
        if not packet.theme_id:
            logger.warning("detect received packet without theme_id, skip")
            return None

        # 防御性：深拷贝，不修改入参
        new_packet = copy.deepcopy(packet)
        timestamp = as_of or new_packet.as_of or ""

        # 判断反转条件
        meets_reversal = self._meets_reversal_conditions(new_packet)

        # 获取历史（判断时不含本次记录）
        history = self._load_history(new_packet.theme_id, exclude_as_of=timestamp)
        has_enough_ice = self._has_consecutive_ice(
            history, self.REQUIRED_CONSECUTIVE_ICE
        )

        # 决定 phase 与返回值
        if meets_reversal and has_enough_ice:
            phase = "REVERSAL_CANDIDATE"
            result = new_packet
        elif meets_reversal:
            # 满足反转条件但 ICE 历史不足 2 次，继续观察
            phase = "ICE"
            result = new_packet
        else:
            # 不满足反转条件
            phase = "ICE"
            result = new_packet if history else None

        # 更新 packet 的 phase 和 theme_type
        new_packet.phase = phase
        new_packet.theme_type = "ice_reversal"

        # 记录历史（无论结果如何都追加）
        self._append_history(new_packet.theme_id, timestamp, phase)

        return result

    def detect_many(
        self, packets: list[ThemePacket], *, as_of: str = ""
    ) -> list[ThemePacket]:
        """批量检测。

        Args:
            packets: 待检测的主题包列表。
            as_of: 检测时间戳。

        Returns:
            所有 phase 为 ICE 或 REVERSAL_CANDIDATE 的 packet（不返回 None）。
        """
        results: list[ThemePacket] = []
        for packet in packets:
            detected = self.detect(packet, as_of=as_of)
            if detected is not None:
                results.append(detected)
        return results

    def reset_history(self, theme_id: str | None = None) -> None:
        """重置历史记录。

        Args:
            theme_id: 指定主题 ID。None 时重置全部。
        """
        if theme_id is None:
            self._ice_history.clear()
        else:
            self._ice_history.pop(theme_id, None)

    def _meets_reversal_conditions(self, packet: ThemePacket) -> bool:
        """判断是否满足冰点反转条件（全部 AND）。

        Args:
            packet: 主题包。

        Returns:
            是否满足全部反转条件。
        """
        # 历史位置处于低分位
        if packet.reversal_score >= self.REVERSAL_SCORE_MAX:
            return False
        # 相对强度不再创新低（广度企稳）
        if packet.breadth_score <= self.BREADTH_SCORE_MIN:
            return False
        # 核心成员率先企稳
        has_core_or_leader = any(
            m.market_role in self.CORE_ROLES
            and (
                not self._require_research_validated_core
                or (
                    bool(m.research_claim_ids)
                    and str(m.research_claim_status).upper() in {"ACTIVE", "TRUSTED"}
                )
            )
            for m in packet.core_members
        )
        if not has_core_or_leader:
            return False
        # 没有 blocking 级负面信息
        if packet.negative_risk_score >= self.NEGATIVE_RISK_MAX:
            return False
        return True

    def _has_consecutive_ice(
        self, history: list[tuple[str, str]], required: int
    ) -> bool:
        """判断历史队列最后 N 条是否都是 ICE。

        Args:
            history: 历史队列。
            required: 需要的连续 ICE 次数。

        Returns:
            是否满足连续 ICE。
        """
        if len(history) < required:
            return False
        last_n = history[-required:]
        return all(phase == "ICE" for _, phase in last_n)

    def _append_history(
        self, theme_id: str, as_of: str, phase: str
    ) -> None:
        """追加历史记录，超出上限时丢弃最旧的。

        Args:
            theme_id: 主题 ID。
            as_of: 时间戳。
            phase: 阶段。
        """
        if theme_id not in self._ice_history:
            self._ice_history[theme_id] = []
        queue = self._ice_history[theme_id]
        if queue and queue[-1][0] == as_of:
            queue[-1] = (as_of, phase)
            return
        queue.append((as_of, phase))
        if len(queue) > self._history_limit:
            queue.pop(0)

    def _load_history(
        self, theme_id: str, *, exclude_as_of: str
    ) -> list[tuple[str, str]]:
        """Load distinct prior observations from memory and repository."""
        combined = list(self._ice_history.get(theme_id, []))
        if self._repository is not None:
            try:
                for packet in self._repository.list_versions(theme_id):
                    observed = str(packet.as_of or packet.detected_at or "")
                    phase = str(packet.phase or "")
                    if observed and phase:
                        combined.append((observed, phase))
            except Exception as exc:
                logger.warning("load persisted reversal history failed: %s", exc)
        distinct: dict[str, str] = {}
        for observed, phase in combined:
            if observed and observed != exclude_as_of:
                distinct[observed] = phase
        return sorted(distinct.items(), key=lambda item: item[0])[-self._history_limit:]
