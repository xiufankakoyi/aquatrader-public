"""QUANT 模块统一契约。

定义 Research→QUANT 的只读适配入口、板块轮动阶段、日线筛选结果
和 QuantSignal 输出。本阶段仅输出 WATCH/READY/REJECTED/EXPIRED，
不产生买卖指令和仓位建议。

设计原则：
- Research 推荐仅进入候选池，必须经过全量筛选才能成为 READY
- PIT（Point-in-Time）严格准入：as_of 之后的任何数据不得参与决策
- 缺字段安全降级：字段缺失时标记 DEGRADED 而非崩溃
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SectorPhase(str, Enum):
    """板块轮动七阶段 + UNKNOWN。

    每个阶段对应板块生命周期的不同位置，驱动日线筛选的权重调整。
    UNKNOWN 表示板块数据缺失，不得直接进入 READY。
    """

    UNKNOWN = "unknown"              # 未知：板块数据缺失，禁止 READY
    ICE = "ice"                      # 冰点：板块持续低迷，成交萎缩
    RECOVERY = "recovery"            # 复苏：底部放量，领涨品种出现
    STARTUP = "startup"              # 启动：板块共振，多品种齐涨
    ACCELERATION = "acceleration"    # 加速：涨幅扩大，成交激增
    CLIMAX = "climax"                # 高潮：顶部放量滞涨，分歧加大
    DIVERGENCE = "divergence"        # 分歧：品种分化，领涨品种滞涨
    RETREAT = "retreat"              # 撤退：板块回调，资金流出


class SignalStatus(str, Enum):
    """QuantSignal 四态输出。

    本阶段不输出 buy/sell/position，仅标记候选池状态。
    """

    WATCH = "watch"        # 观察池：通过板块筛选，待日线确认
    READY = "ready"        # 就绪池：通过全部筛选，可进入下一阶段
    REJECTED = "rejected"  # 拒绝：未通过某项筛选
    EXPIRED = "expired"    # 过期：超过 as_of 有效期或 PIT 违规


class RejectReason(str, Enum):
    """日线筛选拒绝原因。"""

    TREND_FAIL = "trend_fail"                 # 趋势不满足
    POSITION_FAIL = "position_fail"           # 位置不满足
    ACCELERATION_FAIL = "acceleration_fail"   # 加速赶顶（负面信号）
    LIQUIDITY_FAIL = "liquidity_fail"         # 流动性不足
    TRADABILITY_FAIL = "tradability_fail"     # 不可交易（ST/停牌/涨跌停）
    PIT_VIOLATION = "pit_violation"           # PIT 违规
    SECTOR_REJECT = "sector_reject"           # 板块阶段不支持
    SECTOR_UNKNOWN = "sector_unknown"         # 板块数据缺失
    EXPIRED = "expired"                       # 超过有效期
    MISSING_DATA = "missing_data"             # 数据缺失安全降级
    RESEARCH_GATE_FAIL = "research_gate_fail" # Research 准入门禁未通过
    DEGRADED = "degraded"                     # 关键字段降级，禁止 READY


@dataclass
class QuantCandidate:
    """QUANT 候选池统一入口。

    由 Research→QUANT 只读适配器从 ResearchCandidate 生成，
    不携带任何投研内部的 Claim/Evidence/Thesis 对象。
    所有字段均为 QUANT 独立可消费的标量或简单结构。

    追溯字段：thesis_id/claim_id/company_id/mapping_confidence/evidence_as_of
    用于审计和回溯，不参与筛选决策。
    """

    candidate_id: str = ""
    symbol: str = ""
    sector_id: str = ""
    as_of: str = ""               # PIT 时点（ISO 日期）
    available_at: str = ""        # 数据可用时间
    expires_at: str = ""          # 候选失效时间
    research_score: float = 0.0   # 研究可信度 [0, 1]
    alpha_score: float = 0.0      # Alpha 分数 [0, 1]
    core_score: float = 0.0       # 产业核心分数 [0, 1]
    evidence_confidence_low: float = 0.0   # 证据置信度下界
    evidence_confidence_high: float = 0.0  # 证据置信度上界
    valuation_risk_low: float = 0.0        # 估值风险下界
    valuation_risk_high: float = 1.0       # 估值风险上界
    catalyst_window: str = ""     # 催化剂窗口
    source_trace: list[str] = field(default_factory=list)
    degraded: bool = False        # 缺字段降级标记
    degraded_fields: list[str] = field(default_factory=list)
    # 追溯字段（P0-4 契约补齐）
    thesis_id: str = ""           # 关联投研观点 ID
    thesis_status: str = ""       # 观点状态（active/high_conviction）
    claim_id: str = ""            # 关联 Claim ID（准入门禁校验）
    claim_status: str = ""        # Claim 状态（ACTIVE/TRUSTED）
    company_id: str = ""          # 公司规范 ID
    mapping_confidence: float = 0.0  # 公司映射置信度 [0, 1]
    evidence_as_of: str = ""      # 证据时点（PIT 审计）
    strategy_version: str = ""    # 策略版本（审计）
    admission_passed: bool = False  # Research 准入门禁是否通过
    # 主题追溯字段（动态主题控制面）
    theme_id: str = ""            # 关联主题 ID
    theme_run_id: str = ""        # 关联主题运行 ID（版本链）
    theme_type: str = ""          # 主题类型（news_persistence/ice_reversal/...）
    theme_phase: str = ""         # 主题阶段（STARTUP/ACCELERATION/...）

    def is_expired(self, current_date: str) -> bool:
        """检查候选是否超过 PIT 有效期。

        Args:
            current_date: 当前日期（ISO 格式 YYYY-MM-DD）。

        Returns:
            True 表示已过期。
        """
        if not self.expires_at:
            return False
        # expires_at 可能是 ISO 8601 带时区格式（如 2027-12-31T00:00:00+00:00），
        # current_date 是纯日期（如 2026-07-11）。
        # 统一取日期部分（前 10 字符）比较，避免字符串格式不一致导致误判。
        return current_date[:10] > self.expires_at[:10]

    def is_pit_valid(self, current_date: str) -> bool:
        """检查 PIT 准入：available_at 不得晚于 current_date。

        P0-2 修复：available_at 缺失时视为 PIT 无效（不得放过未来数据）。

        Args:
            current_date: 当前日期（ISO 格式 YYYY-MM-DD）。

        Returns:
            True 表示 PIT 有效（数据已可用）。
        """
        # 无 available_at 时视为 PIT 无效：不得放过未来数据
        if not self.available_at:
            return False
        # available_at 可能是 ISO 8601 带时区格式（如 2026-07-11T12:07:51+00:00），
        # current_date 是纯日期（如 2026-07-11）。
        # 统一取日期部分（前 10 字符）比较，避免字符串格式不一致导致误判。
        return self.available_at[:10] <= current_date[:10]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，包含全部追溯与主题字段。"""
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "sector_id": self.sector_id,
            "as_of": self.as_of,
            "available_at": self.available_at,
            "expires_at": self.expires_at,
            "research_score": round(self.research_score, 4),
            "alpha_score": round(self.alpha_score, 4),
            "core_score": round(self.core_score, 4),
            "evidence_confidence_low": round(self.evidence_confidence_low, 4),
            "evidence_confidence_high": round(self.evidence_confidence_high, 4),
            "valuation_risk_low": round(self.valuation_risk_low, 4),
            "valuation_risk_high": round(self.valuation_risk_high, 4),
            "catalyst_window": self.catalyst_window,
            "source_trace": list(self.source_trace),
            "degraded": self.degraded,
            "degraded_fields": list(self.degraded_fields),
            # 追溯字段（P0-4 契约补齐）
            "thesis_id": self.thesis_id,
            "thesis_status": self.thesis_status,
            "claim_id": self.claim_id,
            "claim_status": self.claim_status,
            "company_id": self.company_id,
            "mapping_confidence": round(self.mapping_confidence, 4),
            "evidence_as_of": self.evidence_as_of,
            "strategy_version": self.strategy_version,
            "admission_passed": self.admission_passed,
            # 主题追溯字段（动态主题控制面）
            "theme_id": self.theme_id,
            "theme_run_id": self.theme_run_id,
            "theme_type": self.theme_type,
            "theme_phase": self.theme_phase,
        }


@dataclass
class SectorAssessment:
    """板块阶段评估结果。

    由七阶段板块轮动引擎产出，作为日线筛选的上下文输入。
    板块数据缺失时 phase=UNKNOWN，禁止下游 READY。

    旧规则降级为 feature/shadow：feature_version 和 policy_version 标记
    规则版本，未通过 walk-forward 样本外验证前不得进入生产评分。
    """

    sector_id: str = ""
    sector_name: str = ""
    phase: SectorPhase = SectorPhase.UNKNOWN
    phase_confidence: float = 0.0       # 阶段置信度 [0, 1]
    momentum_score: float = 0.0         # 动量分数 [0, 1]
    breadth_score: float = 0.0          # 广度分数（上涨品种占比）[0, 1]
    turnover_score: float = 0.0         # 换手分数 [0, 1]
    dispersion_score: float = 0.0       # 分歧分数 [0, 1]
    phase_history: list[tuple[str, SectorPhase]] = field(default_factory=list)
    assessed_at: str = ""
    degraded: bool = False
    degraded_fields: list[str] = field(default_factory=list)
    # 旧规则降级为 feature/shadow：标记规则版本
    feature_version: str = ""           # 特征版本（如 sector_engine_v1_legacy）
    policy_version: str = ""            # 策略版本（如 policy_sector_legacy_shadow_v1）


@dataclass
class ScreeningResult:
    """日线筛选引擎单只股票的完整筛选结果。

    P0-4：新增 screening_id 和 candidate_id，支持同一证券多候选不互相覆盖。
    """

    screening_id: str = ""          # 筛选结果唯一 ID
    candidate_id: str = ""          # 关联候选 ID（P0-4 修复覆盖问题）
    symbol: str = ""
    as_of: str = ""
    sector_phase: SectorPhase = SectorPhase.UNKNOWN
    trend_pass: bool = False
    position_pass: bool = False
    acceleration_pass: bool = False     # True = 未加速赶顶（正面）
    liquidity_pass: bool = False
    tradability_pass: bool = False
    pit_pass: bool = False
    reject_reasons: list[RejectReason] = field(default_factory=list)
    trend_score: float = 0.0
    position_score: float = 0.0
    liquidity_score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False

    @property
    def all_pass(self) -> bool:
        """是否通过全部筛选（不含 degraded 检查，由 SignalGenerator 把关）。"""
        return (
            self.trend_pass
            and self.position_pass
            and self.acceleration_pass
            and self.liquidity_pass
            and self.tradability_pass
            and self.pit_pass
        )

    @property
    def ready_eligible(self) -> bool:
        """是否具备 READY 资格：全部通过 + 未降级 + 板块非 UNKNOWN。

        P0-3 修复：degraded 或 sector_phase=UNKNOWN 时不得 READY。
        """
        if self.degraded:
            return False
        if self.sector_phase == SectorPhase.UNKNOWN:
            return False
        return self.all_pass


@dataclass
class QuantSignal:
    """QUANT 最终信号输出。

    仅 WATCH/READY/REJECTED/EXPIRED 四态，不输出买卖和仓位。
    追溯字段用于审计回溯。
    """

    signal_id: str = ""
    candidate_id: str = ""
    symbol: str = ""
    sector_id: str = ""
    as_of: str = ""
    status: SignalStatus = SignalStatus.WATCH
    sector_phase: SectorPhase = SectorPhase.UNKNOWN
    research_score: float = 0.0
    screening_result: ScreeningResult | None = None
    reject_reasons: list[RejectReason] = field(default_factory=list)
    created_at: str = ""
    expires_at: str = ""
    degraded: bool = False
    decision_trace: list[str] = field(default_factory=list)
    # 追溯字段（P0-4 契约补齐）
    thesis_id: str = ""
    claim_id: str = ""
    company_id: str = ""
    mapping_confidence: float = 0.0
    evidence_as_of: str = ""
    strategy_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "signal_id": self.signal_id,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "sector_id": self.sector_id,
            "as_of": self.as_of,
            "status": self.status.value,
            "sector_phase": self.sector_phase.value,
            "research_score": round(self.research_score, 4),
            "screening_result": (
                _screening_result_to_dict(self.screening_result)
                if self.screening_result
                else None
            ),
            "reject_reasons": [r.value for r in self.reject_reasons],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "degraded": self.degraded,
            "decision_trace": list(self.decision_trace),
            # 追溯字段
            "thesis_id": self.thesis_id,
            "claim_id": self.claim_id,
            "company_id": self.company_id,
            "mapping_confidence": round(self.mapping_confidence, 4),
            "evidence_as_of": self.evidence_as_of,
            "strategy_version": self.strategy_version,
        }


def _screening_result_to_dict(result: ScreeningResult) -> dict[str, Any]:
    """序列化 ScreeningResult。"""
    return {
        "screening_id": result.screening_id,
        "candidate_id": result.candidate_id,
        "symbol": result.symbol,
        "as_of": result.as_of,
        "sector_phase": result.sector_phase.value,
        "trend_pass": result.trend_pass,
        "position_pass": result.position_pass,
        "acceleration_pass": result.acceleration_pass,
        "liquidity_pass": result.liquidity_pass,
        "tradability_pass": result.tradability_pass,
        "pit_pass": result.pit_pass,
        "all_pass": result.all_pass,
        "ready_eligible": result.ready_eligible,
        "reject_reasons": [r.value for r in result.reject_reasons],
        "trend_score": round(result.trend_score, 4),
        "position_score": round(result.position_score, 4),
        "liquidity_score": round(result.liquidity_score, 4),
        "details": dict(result.details),
        "degraded": result.degraded,
    }
