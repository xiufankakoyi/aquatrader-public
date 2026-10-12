from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from core.quant.theme.contracts import ThemeMember, ThemePacket
from core.research.control_plane.contracts import Claim
from core.research.control_plane.repository import ResearchRepository
from core.research.control_plane.theme_review import ThemeReviewProcessor


def test_theme_review_drops_nested_objects_missing_required_fields() -> None:
    packet = ThemePacket(
        theme_id="theme-boundary",
        theme_name="AI PC",
        catalysts=[SimpleNamespace(source="source", title="title")],
        members=[SimpleNamespace(symbol="000001")],
        negative_risk_score=9,
    )

    result = ThemeReviewProcessor().review(packet)

    assert result.enhanced_packet.catalysts == []
    assert result.enhanced_packet.members == []
    assert result.enhanced_packet.negative_risk_score == 1.0


def test_structured_membership_parses_false_boolean_text(tmp_path) -> None:
    repository = ResearchRepository(tmp_path / "research.db")
    processor = ThemeReviewProcessor(repository=repository)
    packet = ThemePacket(theme_id="theme-boundary", theme_name="AI PC")
    member = ThemeMember(
        symbol="000001",
        membership_evidence=[{
            "provider": "WENCAI",
            "raw_response_hash": "hash-1",
            "fetched_at": "2026-07-31T00:00:00+00:00",
            "members_truncated": "false",
        }],
    )
    claim = Claim(claim_id="claim-boundary")

    processor._ingest_structured_membership(
        packet, member, claim, as_of="2026-07-31T00:00:00+00:00"
    )

    with sqlite3.connect(repository.db_path) as connection:
        payload = connection.execute(
            "SELECT payload_json FROM source_traces"
        ).fetchone()[0]
    assert json.loads(payload)["metadata"]["members_truncated"] is False
