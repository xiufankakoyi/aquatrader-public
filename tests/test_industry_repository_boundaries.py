"""产业仓储坏数据边界回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.research.industry.contracts import IndustryProfile
from core.research.industry.repository import IndustryRepository, profile_from_dict


def test_profile_from_dict_drops_null_scalars_and_bad_points() -> None:
    profile = profile_from_dict(
        {
            "industry_name": None,
            "code_system": None,
            "price_cost_curve": {
                "price_points": ["bad", ["2026-Q1", "not-a-number"]],
            },
            "competitive_landscape": {
                "member_of_theme_excluded": "false",
            },
        }
    )

    assert profile.industry_name == ""
    assert profile.code_system == "sw"
    assert profile.price_cost_curve is not None
    assert profile.price_cost_curve.price_points == []
    assert profile.competitive_landscape is not None
    assert profile.competitive_landscape.member_of_theme_excluded is False


def test_repository_limit_and_corrupt_schema_metadata_degrade(tmp_path: Path) -> None:
    repo = IndustryRepository(tmp_path / "industry.db")

    assert repo._list("industry_economics", limit=float("inf")) == []

    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "UPDATE industry_schema_meta SET value=? WHERE key='schema_version'",
            ("broken",),
        )

    assert repo.stats()["schema_version"] == 0


def test_private_sql_identifier_is_whitelisted(tmp_path: Path) -> None:
    repo = IndustryRepository(tmp_path / "industry.db")

    with pytest.raises(ValueError):
        repo._list("industry_economics; DROP TABLE industry_economics")


def test_save_economics_uses_contract_boundary_serialization(tmp_path: Path) -> None:
    repo = IndustryRepository(tmp_path / "industry.db")
    profile = IndustryProfile(demand_drivers=[None])  # type: ignore[list-item]

    repo.save_economics("600000.SH", "801031", "2026-06-30", profile)

    payload = repo.list_economics(company_symbol="600000.SH")[0]
    assert payload["demand_drivers"] == []
