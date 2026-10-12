"""QuantSignal 信号生成器。

将 QuantCandidate + SectorAssessment + ScreeningResult 合成为
最终 QuantSignal 输出。仅 WATCH/READY/REJECTED/EXPIRED 四态，
不输出买卖和仓位。

P0-3 修复：degraded 或 sector_phase=UNKNOWN 时不得 READY。
P0-4 修复：筛选结果按 candidate_id 查找，避免同证券多候选覆盖。
P0-1 修复：准入门禁未通过的候选直接 REJECTED。

主题控制面扩展：generate_from_themes 消费 ThemePacket 与
ThemeScreeningResult，不重新查询板块成分。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from core.quant.contracts import (
    QuantCandidate,
    QuantSignal,
    RejectReason,
    ScreeningResult,
    SectorAssessment,
    SectorPhase,
    SignalStatus,
)
from core.quant.theme.contracts import ThemePacket
from core.quant.theme.daily_screener import (
    STATUS_EXPIRED,
    STATUS_READY,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    STATUS_WATCH,
    TECH_ACCELERATION_RISK,
    TECH_TREND_BROKEN,
    ThemeScreeningResult,
)

logger = logging.getLogger(__name__)

# 策略版本（审计追溯）
STRATEGY_VERSION = "quant_phase1_v2"

# 板块阶段对日线筛选的影响权重
PHASE_WEIGHTS: dict[SectorPhase, float] = {
    SectorPhase.UNKNOWN: 0.0,        # 未知：不参与
    SectorPhase.ICE: 0.5,            # 冰点：观察为主
    SectorPhase.RECOVERY: 0.7,       # 复苏：适度放宽
    SectorPhase.STARTUP: 1.0,        # 启动：正常筛选
    SectorPhase.ACCELERATION: 1.2,   # 加速：提高要求
    SectorPhase.CLIMAX: 0.3,         # 高潮：大幅收紧
    SectorPhase.DIVERGENCE: 0.4,     # 分歧：收紧
    SectorPhase.RETREAT: 0.2,        # 撤退：极严
}

# 板块阶段拒绝映射（某些阶段直接拒绝）
PHASE_REJECT: set[SectorPhase] = {
    SectorPhase.RETREAT,   # 撤退阶段不接受新候选
    SectorPhase.UNKNOWN,   # 板块数据缺失，不接受
}


class SignalGenerator:
    """QuantSignal 信号生成器。

    输入：QuantCandidate + SectorAssessment + ScreeningResult
    输出：QuantSignal（WATCH/READY/REJECTED/EXPIRED）
    """

    def __init__(self) -> None:
        """初始化信号生成器。"""
        pass

    def generate(
        self,
        candidate: QuantCandidate,
        sector_assessment: SectorAssessment | None,
        screening_result: ScreeningResult | None,
        current_date: str,
    ) -> QuantSignal:
        """生成 QuantSignal。

        Args:
            candidate: QUANT 候选。
            sector_assessment: 板块评估结果（可 None，安全降级为 UNKNOWN）。
            screening_result: 日线筛选结果（可 None，标记为 WATCH）。
            current_date: 当前日期。

        Returns:
            QuantSignal 实例。
        """
        signal_id = f"sig-{uuid.uuid4().hex[:12]}"
        # P0-3：板块评估缺失时用 UNKNOWN 而非 ICE
        sector_phase = (
            sector_assessment.phase if sector_assessment is not None
            else SectorPhase.UNKNOWN
        )
        research_score = candidate.research_score

        decision_trace: list[str] = []
        reject_reasons: list[RejectReason] = []
        degraded = candidate.degraded

        # 0. Research 准入门禁检查（P0-1）
        if not candidate.admission_passed:
            decision_trace.append(
                f"Research 准入门禁未通过: thesis_id={candidate.thesis_id}"
            )
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.REJECTED,
                reject_reasons=[RejectReason.RESEARCH_GATE_FAIL],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

        # 1. 过期检查
        if candidate.is_expired(current_date):
            decision_trace.append(f"候选已过期: expires_at={candidate.expires_at} > {current_date}")
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.EXPIRED,
                reject_reasons=[RejectReason.EXPIRED],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

        # 2. PIT 违规检查（P0-2：available_at 缺失也视为违规）
        if not candidate.is_pit_valid(current_date):
            decision_trace.append(
                f"PIT 违规: available_at={candidate.available_at!r} > {current_date}"
            )
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.REJECTED,
                reject_reasons=[RejectReason.PIT_VIOLATION],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

        # 3. 板块阶段拒绝检查（P0-3：UNKNOWN 也拒绝）
        if sector_phase in PHASE_REJECT:
            if sector_phase == SectorPhase.UNKNOWN:
                decision_trace.append("板块数据缺失(UNKNOWN)，拒绝")
                reject_reason = RejectReason.SECTOR_UNKNOWN
            else:
                decision_trace.append(f"板块阶段拒绝: {sector_phase.value}")
                reject_reason = RejectReason.SECTOR_REJECT
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.REJECTED,
                reject_reasons=[reject_reason],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

        # 4. 日线筛选结果检查
        if screening_result is None:
            # 无筛选结果 → WATCH（待筛选）
            decision_trace.append("无筛选结果，进入 WATCH 池")
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=None,
                status=SignalStatus.WATCH,
                reject_reasons=[],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

        # 5. 筛选结果决定 READY/REJECTED
        if screening_result.degraded:
            degraded = True

        # P0-3：candidate.degraded 也阻止 READY（不只是 screening_result.degraded）
        if candidate.degraded:
            degraded = True
            # candidate 降级时不得 READY，即使筛选全过
            if screening_result.all_pass:
                decision_trace.append("候选自身降级，拒绝 READY")
                return self._build_signal(
                    signal_id=signal_id,
                    candidate=candidate,
                    sector_phase=sector_phase,
                    research_score=research_score,
                    screening_result=screening_result,
                    status=SignalStatus.REJECTED,
                    reject_reasons=[RejectReason.DEGRADED],
                    current_date=current_date,
                    degraded=degraded,
                    decision_trace=decision_trace,
                )

        # P0-3：使用 ready_eligible 而非 all_pass
        # ready_eligible 检查 all_pass + not degraded + sector_phase != UNKNOWN
        if screening_result.ready_eligible:
            decision_trace.append("全部筛选通过且未降级，进入 READY 池")
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.READY,
                reject_reasons=[],
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )
        else:
            reject_reasons = list(screening_result.reject_reasons)
            # P0-3：如果 all_pass 但 degraded，补充 DEGRADED 原因
            if screening_result.all_pass and screening_result.degraded:
                reject_reasons.append(RejectReason.DEGRADED)
                decision_trace.append("筛选全过但降级，拒绝 READY")
            elif screening_result.all_pass and sector_phase == SectorPhase.UNKNOWN:
                reject_reasons.append(RejectReason.SECTOR_UNKNOWN)
                decision_trace.append("筛选全过但板块 UNKNOWN，拒绝 READY")
            else:
                decision_trace.append(f"筛选未通过: {[r.value for r in reject_reasons]}")
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=sector_phase,
                research_score=research_score,
                screening_result=screening_result,
                status=SignalStatus.REJECTED,
                reject_reasons=reject_reasons,
                current_date=current_date,
                degraded=degraded,
                decision_trace=decision_trace,
            )

    def _build_signal(
        self,
        *,
        signal_id: str,
        candidate: QuantCandidate,
        sector_phase: SectorPhase,
        research_score: float,
        screening_result: ScreeningResult | None,
        status: SignalStatus,
        reject_reasons: list[RejectReason],
        current_date: str,
        degraded: bool,
        decision_trace: list[str],
    ) -> QuantSignal:
        """构建 QuantSignal，传播追溯字段。

        Args:
            signal_id: 信号 ID。
            candidate: 候选对象。
            sector_phase: 板块阶段。
            research_score: 研究分数。
            screening_result: 筛选结果。
            status: 信号状态。
            reject_reasons: 拒绝原因。
            current_date: 当前日期。
            degraded: 是否降级。
            decision_trace: 决策追踪。

        Returns:
            QuantSignal 实例。
        """
        return QuantSignal(
            signal_id=signal_id,
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            sector_id=candidate.sector_id,
            as_of=current_date,
            status=status,
            sector_phase=sector_phase,
            research_score=research_score,
            screening_result=screening_result,
            reject_reasons=reject_reasons,
            created_at=current_date,
            expires_at=candidate.expires_at,
            degraded=degraded,
            decision_trace=decision_trace,
            # 追溯字段传播
            thesis_id=candidate.thesis_id,
            claim_id=candidate.claim_id,
            company_id=candidate.company_id,
            mapping_confidence=candidate.mapping_confidence,
            evidence_as_of=candidate.evidence_as_of,
            strategy_version=STRATEGY_VERSION,
        )

    def generate_batch(
        self,
        candidates: list[QuantCandidate],
        sector_assessments: dict[str, SectorAssessment],
        screening_results: dict[str, ScreeningResult],
        current_date: str,
    ) -> list[QuantSignal]:
        """批量生成 QuantSignal。

        P0-4 修复：筛选结果严格按 candidate_id 查找，不回退到 symbol，
        避免同证券多候选数据串联。

        Args:
            candidates: 候选列表。
            sector_assessments: {sector_id: SectorAssessment} 映射。
            screening_results: {candidate_id: ScreeningResult} 映射。
            current_date: 当前日期。

        Returns:
            QuantSignal 列表。
        """
        signals: list[QuantSignal] = []
        for candidate in candidates:
            # Pipeline 已通过 symbol 反查更新 candidate.sector_id 为正确板块名
            sector_assessment = sector_assessments.get(candidate.sector_id)
            # 严格按 candidate_id 查找，不回退到 symbol
            screening_result = screening_results.get(candidate.candidate_id)
            signal = self.generate(
                candidate, sector_assessment, screening_result, current_date
            )
            signals.append(signal)
        return signals

    # ------------------------------------------------------------------ #
    # 主题控制面入口
    # ------------------------------------------------------------------ #

    def generate_from_themes(
        self,
        candidates: list[QuantCandidate],
        theme_packets: list[ThemePacket],
        screening_results: list[ThemeScreeningResult],
        current_date: str = "",
    ) -> list[QuantSignal]:
        """从 ThemePacket 和筛选结果生成信号。

        与 generate_batch 的差异：
        - 不重新查询板块成分（不依赖 SectorAssessment）
        - 消费 ThemePacket 的 source_snapshot_id 与 theme_run_id
        - 每个候选携带 theme_id/theme_run_id
        - 阻断性负面信息 → REJECTED 信号

        状态映射（ThemeScreeningResult.status → SignalStatus）：
        - READY → READY
        - WATCH → WATCH
        - REJECTED → REJECTED
        - EXPIRED → EXPIRED
        - UNKNOWN → WATCH（降级标记，待数据补全后重判）

        Args:
            candidates: 候选列表（携带 theme_id/theme_run_id）。
            theme_packets: ThemePacket 列表（用于 source_snapshot_id 透传）。
            screening_results: ThemeScreeningResult 列表。
            current_date: 当前日期。

        Returns:
            QuantSignal 列表，与 candidates 一一对应。
        """
        # 按 candidate_id 索引 ThemeScreeningResult
        screening_map: dict[str, ThemeScreeningResult] = {
            r.candidate_id: r for r in screening_results if r.candidate_id
        }
        # 按 theme_id 索引 ThemePacket
        packet_map: dict[str, ThemePacket] = {}
        for packet in theme_packets:
            if packet.theme_id and packet.theme_id not in packet_map:
                packet_map[packet.theme_id] = packet

        signals: list[QuantSignal] = []
        for candidate in candidates:
            packet = packet_map.get(candidate.theme_id)
            screening = screening_map.get(candidate.candidate_id)
            signal = self._generate_theme_signal(
                candidate, packet, screening, current_date
            )
            signals.append(signal)
        return signals

    def _generate_theme_signal(
        self,
        candidate: QuantCandidate,
        packet: ThemePacket | None,
        screening: ThemeScreeningResult | None,
        current_date: str,
    ) -> QuantSignal:
        """生成单个主题候选的 QuantSignal。

        Args:
            candidate: 候选实例。
            packet: 关联的 ThemePacket（可 None，标记降级）。
            screening: 关联的筛选结果（可 None，标记 UNKNOWN）。
            current_date: 当前日期。

        Returns:
            QuantSignal 实例。
        """
        signal_id = f"sig-{uuid.uuid4().hex[:12]}"
        decision_trace: list[str] = []
        reject_reasons: list[RejectReason] = []
        degraded = candidate.degraded

        if not candidate.admission_passed:
            decision_trace.append("Research 准入门禁未通过")
            return self._build_signal(
                signal_id=signal_id,
                candidate=candidate,
                sector_phase=SectorPhase.UNKNOWN,
                research_score=candidate.research_score,
                screening_result=None,
                status=SignalStatus.REJECTED,
                reject_reasons=[RejectReason.RESEARCH_GATE_FAIL],
                current_date=current_date,
                degraded=True,
                decision_trace=decision_trace,
            )

        # 状态映射：ThemeScreeningResult.status → SignalStatus
        status = SignalStatus.WATCH
        if screening is not None:
            theme_status = screening.status
            if theme_status == STATUS_READY:
                status = SignalStatus.READY
                decision_trace.append("主题筛选 READY")
            elif theme_status == STATUS_WATCH:
                status = SignalStatus.WATCH
                decision_trace.append("主题筛选 WATCH")
            elif theme_status == STATUS_REJECTED:
                status = SignalStatus.REJECTED
                # 趋势破坏或加速赶顶 → 对应 reject_reason
                if screening.technical_state == TECH_TREND_BROKEN:
                    reject_reasons.append(RejectReason.TREND_FAIL)
                elif screening.technical_state == TECH_ACCELERATION_RISK:
                    reject_reasons.append(RejectReason.ACCELERATION_FAIL)
                else:
                    reject_reasons.append(RejectReason.DEGRADED)
                decision_trace.append(
                    f"主题筛选 REJECTED: technical_state={screening.technical_state}"
                )
            elif theme_status == STATUS_EXPIRED:
                status = SignalStatus.EXPIRED
                reject_reasons.append(RejectReason.EXPIRED)
                decision_trace.append("主题候选 EXPIRED")
            elif theme_status == STATUS_UNKNOWN:
                # UNKNOWN 降级到 WATCH，等待数据补齐
                status = SignalStatus.WATCH
                degraded = True
                decision_trace.append("主题筛选 UNKNOWN，降级为 WATCH")
            if screening.degraded:
                degraded = True

        # 阻断性负面信息（packet.negative_risk_score ≥ 0.7）强制 REJECTED
        if packet is not None and packet.negative_risk_score >= 0.7:
            status = SignalStatus.REJECTED
            reject_reasons = [RejectReason.DEGRADED]
            degraded = True
            decision_trace.append(
                f"阻断性负面风险: negative_risk_score={packet.negative_risk_score}"
            )

        # 消费 source_snapshot_id：写入 decision_trace 供审计回溯
        if packet is not None and packet.source_snapshot_id:
            decision_trace.append(
                f"source_snapshot_id={packet.source_snapshot_id}"
            )

        # 用 SectorPhase.UNKNOWN 标记（主题流程不依赖板块阶段）
        return self._build_signal(
            signal_id=signal_id,
            candidate=candidate,
            sector_phase=SectorPhase.UNKNOWN,
            research_score=candidate.research_score,
            screening_result=None,
            status=status,
            reject_reasons=reject_reasons,
            current_date=current_date,
            degraded=degraded,
            decision_trace=decision_trace,
        )
