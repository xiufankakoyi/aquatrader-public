"""Agent 讨论协议 — 让五岗像研究员开会一样讨论、反驳、权衡。

解决差距 4:Virtual Research Desk 像"五个 Excel 表拼接",不像"五个研究员开会"。

每个 Agent 对每个 candidate 提出 AgentArgument(支持/反对/观望),
AgentDebate 汇总后识别共识与分歧,CIOBalancer 基于显式规则生成最终决策和推理链。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from math import isfinite
from typing import Any


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


@dataclass
class AgentArgument:
    """单个 Agent 对某个候选对象的论点。"""

    agent: str
    target_type: str  # theme / stock
    target_key: str  # theme 名称或 symbol
    stance: str  # support / oppose / neutral
    claim: str  # 论点陈述
    confidence: str  # high / medium / low
    evidence_ids: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.evidence_ids = _string_list(self.evidence_ids)
        self.risk_flags = _string_list(self.risk_flags)
        self.rebuttal_to = self.rebuttal_to if isinstance(self.rebuttal_to, str) else ""
    rebuttal_to: str = ""  # 反驳哪个 agent 的论点(可选)

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "agent": self.agent,
            "target_type": self.target_type,
            "target_key": self.target_key,
            "stance": self.stance,
            "claim": self.claim,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "risk_flags": list(self.risk_flags),
            "rebuttal_to": self.rebuttal_to,
        }


@dataclass
class DebateResult:
    """Agent 讨论结果。"""

    target_type: str
    target_key: str
    consensus_decision: str = "INSUFFICIENT_EVIDENCE"  # BUY_CANDIDATE / WATCH / BLOCK / INSUFFICIENT_EVIDENCE
    consensus_confidence: str = "low"
    support_score: float = 0.0
    oppose_score: float = 0.0
    dominant_risks: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    reasoning_chain: list[str] = field(default_factory=list)
    arguments: list[AgentArgument] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.support_score = _safe_float(self.support_score)
        self.oppose_score = _safe_float(self.oppose_score)
        self.dominant_risks = _string_list(self.dominant_risks)
        self.disagreements = _string_list(self.disagreements)
        self.reasoning_chain = _string_list(self.reasoning_chain)
        self.arguments = [item for item in self.arguments if isinstance(item, AgentArgument)]

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "target_type": self.target_type,
            "target_key": self.target_key,
            "consensus_decision": self.consensus_decision,
            "consensus_confidence": self.consensus_confidence,
            "support_score": round(self.support_score, 4),
            "oppose_score": round(self.oppose_score, 4),
            "dominant_risks": list(self.dominant_risks),
            "disagreements": list(self.disagreements),
            "reasoning_chain": list(self.reasoning_chain),
            "arguments": [a.to_dict() for a in self.arguments],
        }


class AgentDebate:
    """Agent 讨论器。

    收集每个 agent 对 theme/stock candidate 的 argument,
    按权重计算 support/oppose 分数,识别分歧点和主导风险。
    """

    _AGENT_WEIGHTS = {
        "DataOpsAgent": 1.0,
        "ResearchAgent": 1.2,
        "QuantValidationAgent": 1.1,
        "RiskAgent": 1.3,
    }

    _CONFIDENCE_SCORE = {"high": 1.0, "medium": 0.6, "low": 0.3}

    def __init__(self, agent_weights: dict[str, float] | None = None) -> None:
        """初始化讨论器。

        Args:
            agent_weights: 可选的 agent 权重覆盖。
        """
        raw_weights = agent_weights if isinstance(agent_weights, Mapping) else self._AGENT_WEIGHTS
        self._weights = {
            str(agent): max(0.0, _safe_float(weight, 1.0))
            for agent, weight in raw_weights.items()
            if isinstance(agent, str) and agent
        }

    def debate(
        self,
        target_type: str,
        target_key: str,
        arguments: list[AgentArgument],
    ) -> DebateResult:
        """对单个候选对象执行讨论。

        Args:
            target_type: theme / stock。
            target_key: 候选对象标识。
            arguments: 各 agent 的论点列表。

        Returns:
            DebateResult。
        """
        arguments = [item for item in arguments if isinstance(item, AgentArgument)]
        result = DebateResult(
            target_type=target_type,
            target_key=target_key,
            arguments=arguments,
        )

        support_score = 0.0
        oppose_score = 0.0
        risk_counter: dict[str, int] = {}
        stance_by_agent: dict[str, str] = {}

        for arg in arguments:
            weight = self._weights.get(arg.agent, 1.0)
            conf = self._CONFIDENCE_SCORE.get(arg.confidence, 0.3)
            score = weight * conf

            if arg.stance == "support":
                support_score += score
            elif arg.stance == "oppose":
                oppose_score += score

            stance_by_agent[arg.agent] = arg.stance

            for risk in _string_list(arg.risk_flags):
                risk_counter[risk] = risk_counter.get(risk, 0) + 1

        result.support_score = support_score
        result.oppose_score = oppose_score
        result.dominant_risks = [
            risk for risk, _ in sorted(risk_counter.items(), key=lambda x: x[1], reverse=True)[:3]
        ]

        # 识别分歧:Research 支持但 Quant 反对等
        research_stance = stance_by_agent.get("ResearchAgent", "neutral")
        quant_stance = stance_by_agent.get("QuantValidationAgent", "neutral")
        risk_stance = stance_by_agent.get("RiskAgent", "neutral")
        dataops_stance = stance_by_agent.get("DataOpsAgent", "neutral")

        if research_stance == "support" and quant_stance == "oppose":
            result.disagreements.append("行研看好但量化认为赔率不足")
        if research_stance == "support" and risk_stance == "oppose":
            result.disagreements.append("行研看好但风控标记重要风险")
        if dataops_stance == "oppose":
            result.disagreements.append("数据岗认为数据条件不满足")

        # 生成推理链
        result.reasoning_chain = self._build_reasoning_chain(result, stance_by_agent)

        # 调用 CIO 决策器
        balancer = CIOBalancer()
        result.consensus_decision, result.consensus_confidence = balancer.decide(
            result, stance_by_agent
        )

        return result

    @staticmethod
    def _build_reasoning_chain(
        result: DebateResult,
        stance_by_agent: dict[str, str],
    ) -> list[str]:
        """生成讨论的显式推理链。"""
        chain = [
            f"讨论对象:{result.target_type}/{result.target_key}",
            f"支持分数:{result.support_score:.2f},反对分数:{result.oppose_score:.2f}",
        ]
        for agent, stance in stance_by_agent.items():
            chain.append(f"{agent}立场:{stance}")
        if result.dominant_risks:
            chain.append(f"主导风险:{', '.join(result.dominant_risks)}")
        if result.disagreements:
            chain.append(f"关键分歧:{'; '.join(result.disagreements)}")
        return chain


class CIOBalancer:
    """CIO 权衡决策器 — 基于讨论结果做最终决策。

    规则:
      - DataOps 或 Risk 反对 -> BLOCK
      - Research 支持 + Quant 反对 -> WATCH(等待更好赔率)
      - Research 支持 + Quant 支持 + 无重要风险 -> BUY_CANDIDATE
      - 证据不足或全部 neutral -> INSUFFICIENT_EVIDENCE
    """

    def decide(
        self,
        debate: DebateResult,
        stance_by_agent: dict[str, str],
    ) -> tuple[str, str]:
        """基于讨论结果输出最终决策和置信度。

        Args:
            debate: DebateResult。
            stance_by_agent: agent -> stance 映射。

        Returns:
            (decision, confidence)
        """
        research = stance_by_agent.get("ResearchAgent", "neutral")
        quant = stance_by_agent.get("QuantValidationAgent", "neutral")
        risk = stance_by_agent.get("RiskAgent", "neutral")
        dataops = stance_by_agent.get("DataOpsAgent", "neutral")

        # 硬阻断
        if dataops == "oppose" or risk == "oppose":
            if debate is not None:
                confidence = "low" if debate.support_score < debate.oppose_score else "medium"
            else:
                confidence = "medium"
            return "BLOCK", confidence

        # 量化反对行研 -> 观望
        if research == "support" and quant == "oppose":
            return "WATCH", "medium"

        # 双支持且无硬风险 -> 候选
        if research == "support" and quant == "support":
            return "BUY_CANDIDATE", "high"

        # 行研支持但量化 neutral
        if research == "support" and quant == "neutral":
            return "WATCH", "medium"

        # 无明显支持
        if research == "neutral" and quant == "neutral":
            return "INSUFFICIENT_EVIDENCE", "low"

        # 默认观望
        return "WATCH", "medium"


def build_arguments_from_agent_outputs(
    dataops_output: Any,
    research_output: Any,
    quant_output: Any,
    risk_output: Any,
    theme_candidates: list[dict[str, Any]] | None = None,
    stock_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, list[AgentArgument]]:
    """从现有 AgentOutput 构造 AgentArgument 字典。

    按 target_key 分组,便于后续 debate。

    Args:
        dataops_output: DataOpsAgent 输出。
        research_output: ResearchAgent 输出。
        quant_output: QuantValidationAgent 输出。
        risk_output: RiskAgent 输出。
        theme_candidates: theme 候选列表。
        stock_candidates: stock 候选列表。

    Returns:
        {target_key: [AgentArgument, ...]}。
    """
    args_by_target: dict[str, list[AgentArgument]] = {}
    theme_candidates = theme_candidates if isinstance(theme_candidates, list) else []
    stock_candidates = stock_candidates if isinstance(stock_candidates, list) else []

    def add_arg(arg: AgentArgument) -> None:
        args_by_target.setdefault(arg.target_key, []).append(arg)

    def stance_from_decision(decision: str) -> str:
        if decision in ("PASS", "BUY_CANDIDATE"):
            return "support"
        if decision in ("BLOCK",):
            return "oppose"
        return "neutral"

    # DataOps:全局立场,对每个 candidate 都表达
    dataops_decision = getattr(dataops_output, "decision", "WATCH")
    dataops_confidence = getattr(dataops_output, "confidence", "low")
    dataops_risks = _string_list(getattr(dataops_output, "risk_flags", []))
    dataops_reason = getattr(dataops_output, "reason", "")

    for cand in theme_candidates:
        if not isinstance(cand, Mapping):
            continue
        key = cand.get("name", "") or cand.get("theme", "")
        if not key:
            continue
        add_arg(
            AgentArgument(
                agent="DataOpsAgent",
                target_type="theme",
                target_key=key,
                stance=stance_from_decision(dataops_decision),
                claim=dataops_reason or "数据岗立场",
                confidence=dataops_confidence,
                risk_flags=dataops_risks,
            )
        )

    # Research:从 details.themes 构造
    r_details = getattr(research_output, "details", {}) or {}
    r_details = r_details if isinstance(r_details, Mapping) else {}
    for theme in r_details.get("themes", []):
        if not isinstance(theme, Mapping):
            continue
        key = theme.get("name", "")
        if not key:
            continue
        add_arg(
            AgentArgument(
                agent="ResearchAgent",
                target_type="theme",
                target_key=key,
                stance="support",
                claim=f"行研认为 {key} 有 {theme.get('strength', 'medium')} 强度证据支撑",
                confidence=theme.get("confidence", "medium"),
                evidence_ids=[theme.get("evidence_id", "")] if theme.get("evidence_id") else [],
            )
        )

    # Quant:从 details.stock_candidates 构造
    q_details = getattr(quant_output, "details", {}) or {}
    q_details = q_details if isinstance(q_details, Mapping) else {}
    for stock in q_details.get("stock_candidates", []):
        if not isinstance(stock, Mapping):
            continue
        symbol = stock.get("symbol", "")
        if not symbol:
            continue
        confirmed = (
            stock.get("factor_confirmed", False)
            or stock.get("signal_confirmed", False)
            or stock.get("trend_confirmed", False)
        )
        stance = "support" if confirmed else "oppose"
        add_arg(
            AgentArgument(
                agent="QuantValidationAgent",
                target_type="stock",
                target_key=symbol,
                stance=stance,
                claim=stock.get("reason", "") or f"量化验证{'通过' if confirmed else '未通过'}",
                confidence=stock.get("confidence", "medium"),
                evidence_ids=_string_list(stock.get("evidence_ids")),
                risk_flags=_string_list(stock.get("risk_flags")),
            )
        )

    # Risk:全局 + 对 stock candidate
    risk_decision = getattr(risk_output, "decision", "WATCH")
    risk_confidence = getattr(risk_output, "confidence", "low")
    risk_reason = getattr(risk_output, "reason", "")
    risk_flags = _string_list(getattr(risk_output, "risk_flags", []))

    for cand in stock_candidates:
        if not isinstance(cand, Mapping):
            continue
        symbol = cand.get("symbol", "")
        if not symbol:
            continue
        add_arg(
            AgentArgument(
                agent="RiskAgent",
                target_type="stock",
                target_key=symbol,
                stance=stance_from_decision(risk_decision),
                claim=risk_reason or "风控岗立场",
                confidence=risk_confidence,
                risk_flags=risk_flags,
            )
        )

    return args_by_target
