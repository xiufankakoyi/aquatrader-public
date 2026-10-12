from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScheduledNeuron:
    neuron_id: str
    priority: float
    uncertainty: float
    evidence_gap: float
    contradiction_pressure: float
    staleness: float
    last_confidence_delta: float
    saturation: float
    feedback_adjustment: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "neuron_id": self.neuron_id,
            "priority": round(self.priority, 4),
            "uncertainty": round(self.uncertainty, 4),
            "evidence_gap": round(self.evidence_gap, 4),
            "contradiction_pressure": round(self.contradiction_pressure, 4),
            "staleness": round(self.staleness, 4),
            "last_confidence_delta": round(self.last_confidence_delta, 4),
            "saturation": round(self.saturation, 4),
            "feedback_adjustment": round(self.feedback_adjustment, 4),
        }


class TickScheduler:
    """Information-gain scheduler for active research neurons."""

    def __init__(self) -> None:
        self._feedback_adjustments: dict[str, float] = {}
        self._feedback_log: list[dict[str, Any]] = []

    def select(
        self,
        neurons: list[Any],
        *,
        limit: int,
    ) -> list[ScheduledNeuron]:
        scored = [self.score(neuron) for neuron in neurons]
        scored.sort(key=lambda item: (-item.priority, item.neuron_id))
        return scored[: max(0, int(limit))]

    def score(self, neuron: Any) -> ScheduledNeuron:
        uncertainty = _clamp(getattr(neuron, "uncertainty", 0.0))
        evidence_gap = _clamp(getattr(neuron, "evidence_gap", 0.0))
        contradiction_pressure = _clamp(getattr(neuron, "contradiction_pressure", 0.0))
        staleness = _clamp(getattr(neuron, "staleness", 0.0))
        source_node_ref = getattr(neuron, "source_node_ref", {}) or {}
        last_confidence_delta = _clamp(
            abs(float(source_node_ref.get("last_confidence_delta", 0.0) or 0.0))
        )
        feedback_adjustment = self._feedback_adjustments.get(
            str(getattr(neuron, "neuron_id", "")),
            0.0,
        )
        support = int(getattr(neuron, "support_count", 0) or 0)
        counter = int(getattr(neuron, "counter_count", 0) or 0)
        saturation = _clamp((support + counter) / 3.0)
        priority = (
            0.30 * uncertainty
            + 0.25 * evidence_gap
            + 0.20 * contradiction_pressure
            + 0.15 * staleness
            + 0.10 * last_confidence_delta
            - 0.20 * saturation
            + feedback_adjustment
        )
        return ScheduledNeuron(
            neuron_id=str(getattr(neuron, "neuron_id", "")),
            priority=max(0.0, priority),
            uncertainty=uncertainty,
            evidence_gap=evidence_gap,
            contradiction_pressure=contradiction_pressure,
            staleness=staleness,
            last_confidence_delta=last_confidence_delta,
            saturation=saturation,
            feedback_adjustment=feedback_adjustment,
        )

    def record_feedback(
        self,
        *,
        selected_neuron_id: str,
        allocated_budget: int = 0,
        new_evidence_count: int = 0,
        accepted_evidence_count: int = 0,
        rejected_evidence_count: int = 0,
        confidence_delta: float = 0.0,
        breakthrough_score_delta: float = 0.0,
        token_cost: float = 0.0,
    ) -> dict[str, Any]:
        """Record lightweight scheduler feedback for future priority scoring.

        Step 9 增强:纳入证据接受率(accepted/new)与单位 token 信息增益,
        让调度器优先投入"单位成本信息增益高"的神经元。
        """
        # Step 9:接受率 = accepted / max(new, 1),衡量证据质量。
        total_attempted = max(int(new_evidence_count), 1)
        acceptance_rate = _clamp(accepted_evidence_count / total_attempted)
        # Step 9:单位 token 信息增益 = accepted_evidence / max(token_cost, 1)。
        # token 越多但 accepted 越少,惩罚越大。
        token_per_accept = (
            float(token_cost) / max(int(accepted_evidence_count), 1)
            if accepted_evidence_count > 0
            else float(token_cost)
        )
        reward = (
            0.04 * _clamp(new_evidence_count / 3)
            + 0.04 * _clamp(accepted_evidence_count / 3)
            + 0.04 * _clamp(abs(confidence_delta))
            + 0.04 * _clamp(breakthrough_score_delta)
            + 0.03 * acceptance_rate
            - 0.03 * _clamp(rejected_evidence_count / 3)
            - 0.02 * _clamp(token_cost / 10000)
            - 0.02 * _clamp(token_per_accept / 5000)
        )
        previous = self._feedback_adjustments.get(selected_neuron_id, 0.0)
        adjustment = _clamp(previous * 0.7 + reward, low=-0.15, high=0.15)
        self._feedback_adjustments[selected_neuron_id] = adjustment
        row = {
            "selected_neuron_id": selected_neuron_id,
            "allocated_budget": int(allocated_budget),
            "new_evidence_count": int(new_evidence_count),
            "accepted_evidence_count": int(accepted_evidence_count),
            "rejected_evidence_count": int(rejected_evidence_count),
            "confidence_delta": round(float(confidence_delta), 4),
            "breakthrough_score_delta": round(float(breakthrough_score_delta), 4),
            "token_cost": float(token_cost),
            "acceptance_rate": round(float(acceptance_rate), 4),
            "token_per_accept": round(float(token_per_accept), 4),
            "feedback_adjustment": round(adjustment, 4),
        }
        self._feedback_log.append(row)
        return row

    def feedback_log(self) -> list[dict[str, Any]]:
        return list(self._feedback_log)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value or 0.0)))
