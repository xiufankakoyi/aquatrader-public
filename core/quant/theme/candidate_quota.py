"""主题候选分桶配额生成器。

按 ThemePacket.theme_type 分桶生成最多 15 个 QuantCandidate，每桶内部按
对应价值分数降序排序，并施加桶级配额与总数上限。

设计原则：
- DRY：桶配置、排序键映射、字段透传统一收口在常量映射表中
- KISS：不做 symbol 去重（每个 theme_id 对应一个候选），由 signal_generator
  在最终输出阶段去重；同一 symbol 多个候选时 candidate_id 自带 theme_id 区分
- 防御性：theme_id 为空的 packet 跳过；core_members 为空的主题在桶截断前剔除
- 不输出投资建议

修复说明（2026-07-24）：
- 旧逻辑：桶内按分数排序 → 取前 N 个 → 再生成候选。若前 N 个主题恰好
  core_members 为空，整桶贡献 0 候选，即使排名靠后有 validated core_members
  的主题也被截掉。
- 新逻辑：桶截断前先剔除 core_members 为空的主题；剩余主题按
  (-validated_core_count, -score, theme_id) 排序，validated core members
  数量多的主题优先占用配额。reversal_score/news_score 等市场分数仅作
  同等 validated 数量下的次级排序键，避免"超跌但无 validated 成员"的主题
  挤掉"有 validated 成员"的主题。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from core.quant.contracts import QuantCandidate
from core.quant.theme.contracts import ThemePacket

logger = logging.getLogger(__name__)


# 通过 Research 门禁的 claim 状态集合，与 _build_candidates_from_packet 保持一致
_VALIDATED_CLAIM_STATUS: frozenset[str] = frozenset({"ACTIVE", "TRUSTED"})


@dataclass
class BucketDiagnostic:
    """单桶配额诊断信息，用于结构化漏斗端点展示"为什么没有候选"。

    Attributes:
        bucket_name: 桶名（theme_type）。
        quota: 桶配额上限。
        total_themes: 该桶收到的主题总数。
        eligible_themes: 有 core_members 的主题数（参与配额竞争）。
        dropped_no_core: 因 core_members 为空被剔除的主题数。
        selected: 最终被配额选中的主题数。
        dropped_theme_ids: 被剔除的 theme_id 列表（最多展示前 20 个）。
    """

    bucket_name: str
    quota: int
    total_themes: int
    eligible_themes: int
    dropped_no_core: int
    selected: int
    dropped_theme_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "bucket_name": self.bucket_name,
            "quota": self.quota,
            "total_themes": self.total_themes,
            "eligible_themes": self.eligible_themes,
            "dropped_no_core": self.dropped_no_core,
            "selected": self.selected,
            "dropped_theme_ids": self.dropped_theme_ids,
        }


@dataclass
class CandidateFunnelResult:
    """候选生成漏斗结果，含候选列表与逐桶诊断。

    Attributes:
        candidates: 最终生成的 QuantCandidate 列表。
        bucket_diagnostics: 各桶的配额诊断信息。
        total_themes: 输入主题总数。
        total_eligible: 有 core_members 的主题总数。
        total_dropped: 因 core_members 为空被剔除的主题总数。
    """

    candidates: list[QuantCandidate] = field(default_factory=list)
    bucket_diagnostics: list[BucketDiagnostic] = field(default_factory=list)
    total_themes: int = 0
    total_eligible: int = 0
    total_dropped: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（不含 candidates，candidates 由调用方单独序列化）。"""
        return {
            "total_themes": self.total_themes,
            "total_eligible": self.total_eligible,
            "total_dropped": self.total_dropped,
            "bucket_diagnostics": [d.to_dict() for d in self.bucket_diagnostics],
        }


class CandidateQuotaGenerator:
    """按主题类型分桶生成候选，每桶内部按价值排序。"""

    QUOTA_CONFIG: dict[str, int] = {
        "news_persistence": 6,     # 新闻持续主题
        "ice_reversal": 3,         # 冰点反转主题
        "research_discovery": 2,   # Research 新主题
        "legacy_reentry": 4,       # 历史再发现
    }

    TOTAL_MAX: int = 15

    # 桶 → 排序键映射，缺失分数时回退到 0.0
    _SORT_KEY: dict[str, Callable[[ThemePacket], float]] = {
        "news_persistence": lambda p: float(p.news_score or 0.0),
        "ice_reversal": lambda p: float(p.reversal_score or 0.0),
        "research_discovery": lambda p: float(p.persistence_score or 0.0),
        "legacy_reentry": lambda p: float(p.persistence_score or 0.0),
    }

    # market_role → company_role_score 映射，用于下游打分（此处仅声明用途）
    MARKET_ROLE_SCORES: dict[str, float] = {
        "core": 1.0,
        "leader": 0.8,
        "secondary": 0.5,
        "diffusion": 0.3,
    }

    def __init__(self, *, enforce_research_gate: bool = False) -> None:
        # False preserves the legacy adapter contract for existing callers.
        # Production routes/runtime pass True and never bypass Claim gating.
        self.enforce_research_gate = bool(enforce_research_gate)

    def generate(
        self,
        theme_packets: list[ThemePacket],
        research_candidates: list[QuantCandidate] | None = None,
    ) -> list[QuantCandidate]:
        """从 ThemePacket 列表生成 QuantCandidate 列表。

        流程：
        1. 按 theme_type 分桶（未配置配额的类型归入 research_discovery 桶以容错）
        2. 桶截断前剔除 core_members 为空的主题（避免占用配额却贡献 0 候选）
        3. 每桶按 (-validated_core_count, -score, theme_id) 排序，
           validated core members 多的主题优先占用配额
        4. 每桶取前 N 个（QUOTA_CONFIG 限制）
        5. 不去重 symbol，candidate_id = "{symbol}_{theme_id[:12]}" 保证唯一
        6. 总数限制 TOTAL_MAX=15（按桶优先级顺序截断）

        Args:
            theme_packets: ThemePacket 列表。
            research_candidates: 预留扩展入口（当前不参与合并，由调用方直接拼接）。

        Returns:
            最多 TOTAL_MAX 个 QuantCandidate 实例。
        """
        return self.generate_with_funnel(theme_packets, research_candidates).candidates

    def generate_with_funnel(
        self,
        theme_packets: list[ThemePacket],
        research_candidates: list[QuantCandidate] | None = None,
    ) -> CandidateFunnelResult:
        """生成候选并返回结构化漏斗诊断。

        与 generate() 相同的流程，额外返回各桶的配额竞争诊断信息，
        用于工作台"为什么没有候选"结构化漏斗端点。

        Args:
            theme_packets: ThemePacket 列表。
            research_candidates: 预留扩展入口（当前不参与合并）。

        Returns:
            CandidateFunnelResult，含 candidates 与 bucket_diagnostics。
        """
        # research_candidates 当前不参与合并（KISS），仅在签名中保留以兼容未来扩展
        _ = research_candidates

        if not theme_packets:
            return CandidateFunnelResult()

        # 1. 分桶
        buckets: dict[str, list[ThemePacket]] = {key: [] for key in self.QUOTA_CONFIG}
        for packet in theme_packets:
            if not packet.theme_id:
                logger.debug("跳过无 theme_id 的 packet: %s", packet.theme_name)
                continue
            theme_type = packet.theme_type or ""
            bucket_name = theme_type if theme_type in self.QUOTA_CONFIG else "research_discovery"
            buckets[bucket_name].append(packet)

        # 2. 桶内剔除 core_members 为空的主题 + 排序 + 配额截断
        bucket_candidates: list[QuantCandidate] = []
        diagnostics: list[BucketDiagnostic] = []
        total_themes = 0
        total_eligible = 0
        total_dropped = 0

        # 按 QUOTA_CONFIG 中的桶顺序遍历，确保截断行为稳定
        for bucket_name, quota in self.QUOTA_CONFIG.items():
            packets = buckets.get(bucket_name, [])
            if not packets:
                continue

            total_themes += len(packets)
            sort_key = self._SORT_KEY.get(bucket_name, lambda p: 0.0)

            # 关键修复：桶截断前先剔除 core_members 为空的主题
            eligible = [p for p in packets if p.core_members]
            dropped = [p for p in packets if not p.core_members]
            total_eligible += len(eligible)
            total_dropped += len(dropped)

            # 排序：(-validated_core_count, -score, theme_id)
            # validated core members 多的优先；同数量时按市场分数降序；
            # 仍相同时按 theme_id 升序保证稳定
            sorted_packets = sorted(
                eligible,
                key=lambda p: (
                    -self._count_validated_core_members(p),
                    -sort_key(p),
                    p.theme_id,
                ),
            )
            selected_packets = sorted_packets[:quota]
            for packet in selected_packets:
                bucket_candidates.extend(self._build_candidates_from_packet(packet))

            diagnostics.append(BucketDiagnostic(
                bucket_name=bucket_name,
                quota=quota,
                total_themes=len(packets),
                eligible_themes=len(eligible),
                dropped_no_core=len(dropped),
                selected=len(selected_packets),
                dropped_theme_ids=[p.theme_id for p in dropped[:20]],
            ))

        # 3. 总数限制
        final_candidates = bucket_candidates[: self.TOTAL_MAX]

        return CandidateFunnelResult(
            candidates=final_candidates,
            bucket_diagnostics=diagnostics,
            total_themes=total_themes,
            total_eligible=total_eligible,
            total_dropped=total_dropped,
        )

    @staticmethod
    def _count_validated_core_members(packet: ThemePacket) -> int:
        """统计通过 Research 门禁的 core_members 数量。

        判定标准（与 _build_candidates_from_packet 的 strict_missing 对齐）：
        - research_claim_status ∈ {ACTIVE, TRUSTED}
        - 或 research_validated_at 非空（兼容历史数据）

        Args:
            packet: ThemePacket 实例。

        Returns:
            通过验证的 core_members 数量。
        """
        count = 0
        for member in packet.core_members:
            status = str(member.research_claim_status or "").upper()
            if status in _VALIDATED_CLAIM_STATUS or member.research_validated_at:
                count += 1
        return count

    def _build_candidates_from_packet(
        self, packet: ThemePacket
    ) -> list[QuantCandidate]:
        """从单个 ThemePacket 的 core_members 生成 QuantCandidate 列表。

        规则：
        - 每个 core_member 生成一个 QuantCandidate
        - candidate_id = "{symbol}_{theme_id[:12]}"
        - sector_id 取 theme_name（主题名作为板块标识）
        - 透传 theme_id / theme_run_id / theme_type / theme_phase

        Args:
            packet: ThemePacket 实例。

        Returns:
            QuantCandidate 列表；core_members 为空或 member.symbol 缺失时返回空列表。
        """
        if not packet.core_members:
            return []

        # theme_id 短哈希片段，截断到 12 字符以控制 candidate_id 长度
        theme_id_short = packet.theme_id[:12]
        candidates: list[QuantCandidate] = []
        for member in packet.core_members:
            if not member.symbol:
                continue
            candidate_id = f"{member.symbol}_{theme_id_short}"
            # research_score 取 membership_confidence，作为下游 final_score 计算输入
            research_score = float(member.membership_confidence or 0.0)
            claim_id = member.research_claim_ids[0] if member.research_claim_ids else ""
            claim_status = str(member.research_claim_status or "")
            strict_missing = self.enforce_research_gate and not (
                claim_id
                and claim_status.upper() in {"ACTIVE", "TRUSTED"}
                and float(member.mapping_confidence or 0.0) >= 0.6
                and member.evidence_as_of
                and member.company_id
            )
            # 透传 packet 级降级（含问财 members_truncated、concept_sources_unavailable_news_only 等）
            # 到 QuantCandidate.degraded_fields，让下游 gate / 工作台能直接看到降级原因。
            packet_degraded_fields = (
                list(packet.degradation_reasons) if packet.degraded else []
            )
            candidate_degraded_fields: list[str] = []
            if strict_missing:
                candidate_degraded_fields.append("research_claim_trace")
            for reason in packet_degraded_fields:
                if reason not in candidate_degraded_fields:
                    candidate_degraded_fields.append(reason)

            # 候选 degraded 只基于自身关键字段缺失（strict_missing），
            # 不继承 packet 级降级。原因：packet 级降级（如 members_truncated、
            # theme_market_history_missing）反映的是主题聚合阶段的完整性问题，
            # 不代表保留成员的追溯字段（claim_id/claim_status/mapping_confidence
            # 等）有问题。gate.py 的 P0-5 契约要求"关键追溯字段缺失"才 fail-closed，
            # packet 级降级原因仍通过 degraded_fields 透传给下游可见。
            candidate = QuantCandidate(
                candidate_id=candidate_id,
                symbol=member.symbol,
                sector_id=packet.theme_name,
                as_of=packet.as_of,
                available_at=packet.as_of or packet.detected_at,
                expires_at=packet.expires_at,
                research_score=research_score,
                thesis_id=claim_id,
                thesis_status=(
                    "active" if claim_status.upper() in {"ACTIVE", "TRUSTED"} else ""
                ),
                claim_id=claim_id,
                claim_status=claim_status,
                company_id=member.company_id,
                mapping_confidence=float(member.mapping_confidence or 0.0),
                evidence_as_of=member.evidence_as_of,
                source_trace=list(member.membership_sources),
                # 主题追溯字段
                theme_id=packet.theme_id,
                theme_run_id=packet.theme_run_id,
                theme_type=packet.theme_type,
                theme_phase=packet.phase,
                strategy_version=packet.strategy_version,
                degraded=strict_missing,
                degraded_fields=candidate_degraded_fields,
                admission_passed=not self.enforce_research_gate,
            )
            candidates.append(candidate)
        return candidates
