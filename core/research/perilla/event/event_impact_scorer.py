"""Event impact scorer.

Scores the potential market/ontology impact of an event claim based on
source credibility, entity centrality, contradiction status, and evidence depth.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from core.research.perilla.event.event_graph_schema import EventClaim


_SOURCE_CREDIBILITY: dict[str, float] = {
    "official_announcement": 1.0,
    "exchange_filing": 0.95,
    "regulatory": 0.9,
    "broker_report": 0.75,
    "financial_media": 0.7,
    "industry_media": 0.6,
    "social_media": 0.3,
    "web_search": 0.35,
    "active_learning": 0.3,
    "unknown": 0.4,
}


def _bounded_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed)) if math.isfinite(parsed) else default


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


class EventImpactScorer:
    """Scores the impact of event claims.

    Impact score combines:
      - Source credibility (weight 0.3)
      - Entity linkage depth (linked nodes + edges, weight 0.25)
      - Contradiction penalty (weight 0.15)
      - Promotion candidate bonus (weight 0.15)
      - Base confidence (weight 0.15)
    """

    def __init__(self, source_credibility: dict[str, float] | None = None) -> None:
        """初始化。

        Args:
            source_credibility: 自定义来源可信度映射，None 使用默认值。
        """
        self._source_cred = dict(source_credibility) if isinstance(source_credibility, Mapping) else dict(_SOURCE_CREDIBILITY)

    def score(self, event_claim: EventClaim) -> float:
        """Return an impact score for an event claim.

        Args:
            event_claim: Event claim to score.

        Returns:
            Impact score between 0 and 1.
        """
        base = _bounded_float(_field(event_claim, "confidence"))

        src_type = str(_field(event_claim, "source_type") or "unknown").lower()
        src_cred = _bounded_float(self._source_cred.get(src_type, self._source_cred.get("unknown", 0.4)), 0.4)

        node_count = len(_items(_field(event_claim, "linked_node_ids")))
        edge_count = len(_items(_field(event_claim, "linked_edge_keys")))
        linkage = min(1.0, (node_count + edge_count) / 6.0)

        contradiction_penalty = 0.0
        if bool(_field(event_claim, "contradiction_candidate")):
            contradiction_penalty = 0.3

        promotion_bonus = 0.15 if bool(_field(event_claim, "promotion_candidate")) else 0.0

        freshness_weight = _bounded_float(_field(event_claim, "freshness_score")) * 0.5

        impact = (
            src_cred * 0.30
            + linkage * 0.25
            + base * 0.15
            + (1.0 - contradiction_penalty) * 0.15
            + promotion_bonus
            + freshness_weight * 0.15
        )

        return round(max(0.0, min(1.0, impact)), 4)
