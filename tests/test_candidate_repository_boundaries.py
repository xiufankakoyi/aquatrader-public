"""CandidateRepository 持久化边界回归测试。"""
from __future__ import annotations

import pytest

from core.research.candidate.repository import CandidateRepository


def test_save_normalizes_text_boundaries_and_query_whitespace(tmp_path):
    repo = CandidateRepository(tmp_path / "candidates.db")

    repo.save_candidate({
        "candidate_id": " candidate_1 ",
        "symbol": None,
        "created_at": None,
    })

    assert repo.get_candidate(" candidate_1 ") == {
        "candidate_id": " candidate_1 ",
        "symbol": None,
        "created_at": None,
    }
    assert repo.get_candidate("candidate_1") == {
        "candidate_id": " candidate_1 ",
        "symbol": None,
        "created_at": None,
    }
    assert repo.list_candidates(symbol="  ") == [
        {"candidate_id": " candidate_1 ", "symbol": None, "created_at": None}
    ]


def test_non_callable_to_dict_is_rejected_with_clear_type_error(tmp_path):
    repo = CandidateRepository(tmp_path / "candidates.db")

    with pytest.raises(TypeError, match="candidate payload"):
        repo.save_candidate(type("Candidate", (), {"to_dict": 1})())


def test_invalid_utf8_payload_is_skipped(tmp_path):
    repo = CandidateRepository(tmp_path / "candidates.db")
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO research_candidates(candidate_id,symbol,created_at,payload_json) VALUES(?,?,?,?)",
            ("bad", "", "", sqlite_blob()),
        )

    assert repo.get_candidate("bad") is None
    assert repo.list_candidates() == []


def sqlite_blob():
    """返回无法按 UTF-8 解码的 SQLite BLOB。"""
    return b"\xff"
