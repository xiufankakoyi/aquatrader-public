from __future__ import annotations

import pytest

from core.research.control_plane.contracts import SourceTrace
from core.research.control_plane.governance import SourceGovernance


@pytest.mark.parametrize("status", ["unavailable", "offline", "error", "failed", "disabled"])
def test_unavailable_source_is_blocked(status: str) -> None:
    decision = SourceGovernance().evaluate(SourceTrace(availability_status=status))

    assert decision.allowed is False
    assert decision.reliability_prior == 0.0


def test_source_type_is_case_insensitive() -> None:
    decision = SourceGovernance().evaluate(
        SourceTrace(source_type="Official", availability_status="ok")
    )

    assert decision.allowed is True
    assert decision.reliability_prior == 0.85
