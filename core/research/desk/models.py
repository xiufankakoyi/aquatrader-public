"""Virtual Research Desk 数据模型 — 统一 Agent 输出结构。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, List


VALID_DECISIONS = {"PASS", "WATCH", "BLOCK", "INSUFFICIENT_EVIDENCE", "BUY_CANDIDATE"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _mapping_list(value: Any) -> list[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


@dataclass
class AgentOutput:
    """统一 Agent 输出结构。

    Attributes:
        agent: Agent 名称
        trade_date: 交易日 YYYY-MM-DD
        input_sources: 本 Agent 读取的数据源列表
        summary: 本岗结论摘要
        evidence_ids: 引用的证据 ID 列表
        confidence: 置信度 high/medium/low
        risk_flags: 风险标记列表
        decision: 决策 PASS/WATCH/BLOCK/INSUFFICIENT_EVIDENCE
        reason: 决策理由（必填）
        details: 附加详情（可选，用于各 Agent 输出额外结构化数据）
    """

    agent: str
    trade_date: str
    input_sources: List[str] = field(default_factory=list)
    summary: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    confidence: str = "low"
    risk_flags: List[str] = field(default_factory=list)
    decision: str = "INSUFFICIENT_EVIDENCE"
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验字段合法性。"""
        self.input_sources = _string_list(self.input_sources)
        self.evidence_ids = _string_list(self.evidence_ids)
        self.risk_flags = _string_list(self.risk_flags)
        self.details = dict(self.details) if isinstance(self.details, Mapping) else {}
        if self.decision not in VALID_DECISIONS:
            raise ValueError(
                f"decision '{self.decision}' 不合法，"
                f"必须是 {VALID_DECISIONS} 之一"
            )
        if self.confidence not in VALID_CONFIDENCE:
            raise ValueError(
                f"confidence '{self.confidence}' 不合法，"
                f"必须是 {VALID_CONFIDENCE} 之一"
            )
        if not self.reason:
            raise ValueError("reason 不能为空，所有结论必须有决策理由")


@dataclass
class ThemeCandidate:
    """题材候选 — Research Agent 输出的结构化题材。

    Attributes:
        theme: 题材名称
        evidence_ids: 引用的证据 ID 列表
        evidence_count: 证据数量
        related_symbols: 关联标的代码列表
        confidence: 置信度 high/medium/low
        decision: 决策 PASS/WATCH/BLOCK/INSUFFICIENT_EVIDENCE
        reason: 决策理由
    """

    theme: str
    evidence_ids: List[str] = field(default_factory=list)
    evidence_count: int = 0
    related_symbols: List[str] = field(default_factory=list)
    confidence: str = "low"
    decision: str = "INSUFFICIENT_EVIDENCE"
    reason: str = ""

    def __post_init__(self) -> None:
        self.evidence_ids = _string_list(self.evidence_ids)
        self.related_symbols = _string_list(self.related_symbols)
        try:
            self.evidence_count = max(0, int(self.evidence_count))
        except (TypeError, ValueError, OverflowError):
            self.evidence_count = 0


@dataclass
class StockCandidate:
    """股票候选 — QuantValidation Agent 输出的结构化股票。

    Attributes:
        symbol: 股票代码
        stock_name: 股票名称
        evidence_ids: 引用的证据 ID 列表
        factor_confirmed: 因子是否确认
        signal_confirmed: 信号是否确认
        trend_confirmed: 趋势是否确认
        risk_flags: 风险标记列表
        decision: 决策 PASS/WATCH/BLOCK/INSUFFICIENT_EVIDENCE
        reason: 决策理由
    """

    symbol: str
    stock_name: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    factor_confirmed: bool = False
    signal_confirmed: bool = False
    trend_confirmed: bool = False
    risk_flags: List[str] = field(default_factory=list)
    decision: str = "INSUFFICIENT_EVIDENCE"
    reason: str = ""

    def __post_init__(self) -> None:
        self.evidence_ids = _string_list(self.evidence_ids)
        self.risk_flags = _string_list(self.risk_flags)


@dataclass
class CIOOutput(AgentOutput):
    """CIO Agent 输出结构，扩展 AgentOutput。

    Attributes:
        today_watch_directions: (v1) 今日可观察方向
        today_blocked_directions: (v1) 今日禁止方向
        today_candidate_stocks: (v1) 今日候选股票
        today_no_trade_reasons: (v1) 今日不应交易的原因
        final_output_level: (v1) 最终输出等级
        market_view: (v2) 市场观点
        theme_watchlist: (v2) 题材观察列表
        stock_watchlist: (v2) 股票观察列表
        blocked_items: (v2) 阻断项列表
        insufficient_evidence_items: (v2) 证据不足项列表
        final_decision: (v2) 最终决策
    """

    # v1 字段（向后兼容）
    today_watch_directions: List[Dict[str, Any]] = field(default_factory=list)
    today_blocked_directions: List[Dict[str, Any]] = field(default_factory=list)
    today_candidate_stocks: List[Dict[str, Any]] = field(default_factory=list)
    today_no_trade_reasons: List[str] = field(default_factory=list)
    final_output_level: str = "INSUFFICIENT_EVIDENCE"
    # v2 字段
    market_view: str = ""
    theme_watchlist: List[Dict[str, Any]] = field(default_factory=list)
    stock_watchlist: List[Dict[str, Any]] = field(default_factory=list)
    blocked_items: List[Dict[str, Any]] = field(default_factory=list)
    insufficient_evidence_items: List[Dict[str, Any]] = field(default_factory=list)
    final_decision: str = "INSUFFICIENT_EVIDENCE"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.today_watch_directions = _mapping_list(self.today_watch_directions)
        self.today_blocked_directions = _mapping_list(self.today_blocked_directions)
        self.today_candidate_stocks = _mapping_list(self.today_candidate_stocks)
        self.today_no_trade_reasons = _string_list(self.today_no_trade_reasons)
        self.theme_watchlist = _mapping_list(self.theme_watchlist)
        self.stock_watchlist = _mapping_list(self.stock_watchlist)
        self.blocked_items = _mapping_list(self.blocked_items)
        self.insufficient_evidence_items = _mapping_list(self.insufficient_evidence_items)
        if self.final_output_level not in VALID_DECISIONS:
            self.final_output_level = "INSUFFICIENT_EVIDENCE"
        if self.final_decision not in VALID_DECISIONS:
            self.final_decision = "INSUFFICIENT_EVIDENCE"
