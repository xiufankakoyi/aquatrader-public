from __future__ import annotations

from core.research.control_plane.validity_config import (
    compute_valid_until,
    is_validity_configurable,
)


def test_validity_config_rejects_invalid_declared_date() -> None:
    assert compute_valid_until(
        source_declared_validity="not-a-date",
        claim_type="news",
        published_at="2026-07-31T00:00:00+00:00",
    ) == ""


def test_validity_config_normalizes_custom_keys_and_naive_publish_time() -> None:
    assert compute_valid_until(
        claim_type=" CUSTOM_FACT ",
        published_at="2026-07-31T00:00:00",
        custom_config={"claim_types": {"custom_fact": 2}},
    ) == "2026-08-02T00:00:00+00:00"


def test_validity_config_rejects_non_finite_or_negative_days() -> None:
    assert compute_valid_until(
        claim_type="custom_fact",
        published_at="2026-07-31T00:00:00+00:00",
        custom_config={"claim_types": {"custom_fact": float("nan")}},
    ) == ""
    assert is_validity_configurable(" NEWS ", "") is False
