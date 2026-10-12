"""ResearchCandidate 契约序列化边界回归测试。"""

from __future__ import annotations

from core.research.candidate.contracts import ResearchCandidate


def test_candidate_to_dict_degrades_malformed_score_breakdowns() -> None:
    candidate = ResearchCandidate(
        industry_core_score=None,  # type: ignore[arg-type]
        company_exposure_score=None,  # type: ignore[arg-type]
        catalyst_score=None,  # type: ignore[arg-type]
        research_score=None,  # type: ignore[arg-type]
        alpha_score=None,  # type: ignore[arg-type]
        core_score=None,  # type: ignore[arg-type]
    )

    payload = candidate.to_dict()

    for key in (
        "industry_core_score",
        "company_exposure_score",
        "catalyst_score",
        "research_score",
        "alpha_score",
        "core_score",
    ):
        assert payload[key]["score"]["low"] == 0.0
        assert payload[key]["score"]["high"] == 0.0
