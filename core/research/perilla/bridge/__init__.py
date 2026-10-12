"""Bridge layer connecting Perilla discovery/event/evidence to SAG/Alpha."""
from __future__ import annotations

from core.research.perilla.bridge.alpha_gap_feedback_emitter import (
    AlphaGapFeedbackEmitter,
)
from core.research.perilla.bridge.models import EventClaimDraft, PerillaSearchTask
from core.research.perilla.bridge.perilla_sag_bridge import (
    EventClaimIngestor,
    PerillaSAGBridge,
    PerillaSignalNormalizer,
)
from core.research.perilla.bridge.promotion_candidate_builder import (
    PromotionCandidateBuilder,
)

__all__ = [
    "AlphaGapFeedbackEmitter",
    "EventClaimDraft",
    "EventClaimIngestor",
    "PerillaSAGBridge",
    "PerillaSearchTask",
    "PerillaSignalNormalizer",
    "PromotionCandidateBuilder",
]
