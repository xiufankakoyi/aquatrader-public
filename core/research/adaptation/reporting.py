"""Build traceable four-domain reports from PIT-safe, out-of-sample inputs."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, TYPE_CHECKING
from collections.abc import Mapping

from .contracts import DomainFeedback, FourDomainReport, PortfolioDiagnostics
from .engine import grouped_returns, pearson_ic, rank_ic

if TYPE_CHECKING:
    from core.paper_trading.execution_model import ExecutionModel


class ExecutionAttributionAdapter:
    """Read actual output from the existing execution layer; never simulate fills."""

    def __init__(self, execution_model: "ExecutionModel") -> None:
        self.execution_model = execution_model

    @staticmethod
    def summarize(execution_result: dict[str, Any], actual_costs: dict[str, float]) -> dict[str, float]:
        # Fees, slippage, limit/suspension/volume rejection must be supplied by the
        # existing portfolio/execution stack. This adapter only attributes them.
        costs = actual_costs if isinstance(actual_costs, Mapping) else {}
        summary: dict[str, float] = {}
        for key, value in costs.items():
            try:
                summary[key] = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
        result = execution_result if isinstance(execution_result, Mapping) else {}
        summary["filled_buy_count"] = float(len(result.get("buys", [])) if isinstance(result.get("buys", []), (list, tuple)) else 0)
        summary["filled_sell_count"] = float(len(result.get("sells", [])) if isinstance(result.get("sells", []), (list, tuple)) else 0)
        summary["skipped_count"] = float(len(result.get("skipped", [])) if isinstance(result.get("skipped", []), (list, tuple)) else 0)
        return summary


class FourDomainReportBuilder:
    def build(
        self, *, as_of: str, signals: list[float], outcomes: list[float],
        diagnostics: PortfolioDiagnostics, feedback: Iterable[DomainFeedback],
        pit_blocked_count: int, cost_attribution: dict[str, float],
        walk_forward_windows: list[dict[str, str]],
    ) -> FourDomainReport:
        stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0})
        for item in feedback if feedback is not None else []:
            if not isinstance(item, DomainFeedback):
                continue
            stats[item.domain]["count"] += 1.0
            metrics = item.metrics if isinstance(item.metrics, Mapping) else {}
            for key, value in metrics.items():
                try:
                    stats[item.domain][key] = stats[item.domain].get(key, 0.0) + float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
        return FourDomainReport(
            as_of=as_of, pit_blocked_count=int(pit_blocked_count),
            ic=pearson_ic(signals, outcomes), rank_ic=rank_ic(signals, outcomes),
            grouped_returns=grouped_returns(signals, outcomes), diagnostics=diagnostics,
            cost_attribution=dict(cost_attribution) if isinstance(cost_attribution, Mapping) else {}, feedback_stats=dict(stats),
            walk_forward_windows=list(walk_forward_windows) if isinstance(walk_forward_windows, (list, tuple)) else [],
        )
