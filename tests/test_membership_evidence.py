"""MembershipEvidence 召回与事实确认分离测试。

覆盖：
- 无事实证据时候选只进入探索视图（membership_status 保持 recall_only）
- 有事实证据时候选状态升级为 grounded
- 过期证据不被确认
- grounded/unverified/conflicted 三态确认规则
- counter/neutral 不确认候选
- SQLite 持久化与查询
- 手动过期证据
- 序列化往返
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.theme_membership.confirmator import MembershipConfirmator
from core.research.theme_membership.contracts import (
    MEMBERSHIP_STATUS_GROUNDED,
    MEMBERSHIP_STATUS_RECALL_ONLY,
    MembershipEvidence,
    ThemeMemberCandidate,
)
from core.research.theme_membership.repository import MembershipEvidenceStore


# ---------- 辅助函数 ----------

def _make_evidence(
    symbol: str = "600000.SH",
    theme_id: str = "theme_optical",
    grounding_status: str = "grounded",
    valid_until: str = "",
    valid_from: str = "2025-01-01T00:00:00+00:00",
    source_type: str = "announcement",
    provider: str = "cninfo",
    stance: str = "support",
    confidence: float = 0.9,
) -> MembershipEvidence:
    """构造完整的事实证据。"""
    return MembershipEvidence(
        symbol=symbol,
        company_id=f"company_{symbol}",
        theme_id=theme_id,
        stance=stance,
        source_type=source_type,
        provider=provider,
        source_url="https://example.com/doc/123",
        document_id="doc_123",
        document_version_id="docv_123_v1",
        quote="公司是国内领先的光学元件厂商",
        locator={"field": "主营业务", "offset": 120},
        published_at="2025-01-15T00:00:00+00:00",
        retrieved_at="2025-01-20T00:00:00+00:00",
        valid_from=valid_from,
        valid_until=valid_until,
        lineage_id="lineage_001",
        grounding_status=grounding_status,
        policy_version="policy_v1",
        confidence=confidence,
    )


def _make_candidate(
    symbol: str = "600000.SH",
    theme_id: str = "theme_optical",
    source_provider: str = "wencai",
) -> ThemeMemberCandidate:
    """构造召回候选。"""
    return ThemeMemberCandidate(
        symbol=symbol,
        theme_id=theme_id,
        source_provider=source_provider,
        source_board_name="光学元件概念",
        fetched_at="2025-01-25T00:00:00+00:00",
        truncation_info={"total": 50, "returned": 50},
    )


# ---------- 契约序列化测试 ----------

class TestContractsSerialization:
    def test_membership_evidence_to_dict_roundtrip(self):
        """MembershipEvidence 序列化往返。"""
        evidence = _make_evidence()
        d = evidence.to_dict()
        assert d["symbol"] == "600000.SH"
        assert d["grounding_status"] == "grounded"
        assert d["locator"] == {"field": "主营业务", "offset": 120}
        restored = MembershipEvidence.from_dict(d)
        assert restored.symbol == evidence.symbol
        assert restored.theme_id == evidence.theme_id
        assert restored.grounding_status == evidence.grounding_status
        assert restored.locator == evidence.locator
        assert restored.confidence == evidence.confidence

    def test_membership_evidence_has_evidence_id(self):
        """MembershipEvidence 自动生成 evidence_id。"""
        evidence = _make_evidence()
        assert evidence.evidence_id != ""

    def test_theme_member_candidate_to_dict_roundtrip(self):
        """ThemeMemberCandidate 序列化往返。"""
        candidate = _make_candidate()
        d = candidate.to_dict()
        assert d["membership_status"] == MEMBERSHIP_STATUS_RECALL_ONLY
        assert d["source_provider"] == "wencai"
        restored = ThemeMemberCandidate.from_dict(d)
        assert restored.symbol == candidate.symbol
        assert restored.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY
        assert restored.truncation_info == candidate.truncation_info

    def test_theme_member_candidate_default_status_is_recall_only(self):
        """ThemeMemberCandidate 默认 membership_status 为 recall_only。"""
        candidate = ThemeMemberCandidate(
            symbol="600000.SH",
            theme_id="theme_optical",
            source_provider="wencai",
            source_board_name="光学",
            fetched_at="2025-01-25T00:00:00+00:00",
            truncation_info={},
        )
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY


# ---------- 持久化存储测试 ----------

class TestMembershipEvidenceStore:
    @pytest.fixture
    def store(self, tmp_path):
        return MembershipEvidenceStore(tmp_path / "membership_evidence.db")

    def test_migration_idempotent(self, store):
        """迁移幂等。"""
        store.migrate()
        store.migrate()
        with sqlite3.connect(store.db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "membership_evidence" in tables

    def test_save_and_get_evidence(self, store):
        """保存并查询证据。"""
        evidence = _make_evidence()
        assert store.save_evidence(evidence) is True
        got = store.get_evidence(evidence.symbol, evidence.theme_id)
        assert len(got) == 1
        assert got[0].symbol == evidence.symbol
        assert got[0].grounding_status == "grounded"
        assert got[0].locator == {"field": "主营业务", "offset": 120}

    def test_get_evidence_empty(self, store):
        """无证据时返回空列表。"""
        got = store.get_evidence("999999.SH", "theme_nonexistent")
        assert got == []

    def test_get_evidence_multiple(self, store):
        """同一公司同一主题可有多条证据。"""
        e1 = _make_evidence()
        e2 = _make_evidence(source_type="qa", provider="sse_irm")
        store.save_evidence(e1)
        store.save_evidence(e2)
        got = store.get_evidence("600000.SH", "theme_optical")
        assert len(got) == 2

    def test_get_confirmed_members(self, store):
        """查询主题的已确认成员。"""
        store.save_evidence(_make_evidence(symbol="600000.SH", theme_id="theme_optical"))
        store.save_evidence(_make_evidence(symbol="000001.SZ", theme_id="theme_optical"))
        # unverified 不进入已确认
        store.save_evidence(
            _make_evidence(symbol="600001.SH", theme_id="theme_optical", grounding_status="unverified")
        )
        members = store.get_confirmed_members("theme_optical")
        assert set(members) == {"600000.SH", "000001.SZ"}

    def test_get_confirmed_members_excludes_expired(self, store):
        """已过期证据不进入已确认成员。"""
        store.save_evidence(_make_evidence(symbol="600000.SH", valid_until="2024-01-01T00:00:00+00:00"))
        store.save_evidence(_make_evidence(symbol="000001.SZ", valid_until=""))
        members = store.get_confirmed_members("theme_optical")
        assert "000001.SZ" in members
        assert "600000.SH" not in members

    def test_get_confirmed_members_excludes_counter_stance(self, store):
        """counter 立场的证据不确认成员。"""
        store.save_evidence(_make_evidence(symbol="600000.SH", stance="counter"))
        store.save_evidence(_make_evidence(symbol="000001.SZ", stance="support"))
        members = store.get_confirmed_members("theme_optical")
        assert "000001.SZ" in members
        assert "600000.SH" not in members

    def test_get_confirmed_members_excludes_neutral_stance(self, store):
        """neutral 立场的证据不确认成员。"""
        store.save_evidence(_make_evidence(symbol="600000.SH", stance="neutral"))
        members = store.get_confirmed_members("theme_optical")
        assert "600000.SH" not in members

    def test_is_member_confirmed_true(self, store):
        """有 grounded 且未过期证据时确认为成员。"""
        store.save_evidence(_make_evidence(symbol="600000.SH"))
        assert store.is_member_confirmed("600000.SH", "theme_optical") is True

    def test_is_member_confirmed_false_no_evidence(self, store):
        """无证据时不确认为成员。"""
        assert store.is_member_confirmed("999999.SH", "theme_optical") is False

    def test_is_member_confirmed_false_unverified(self, store):
        """unverified 证据不确认。"""
        store.save_evidence(
            _make_evidence(symbol="600000.SH", grounding_status="unverified")
        )
        assert store.is_member_confirmed("600000.SH", "theme_optical") is False

    def test_is_member_confirmed_false_expired(self, store):
        """已过期证据不确认。"""
        store.save_evidence(
            _make_evidence(symbol="600000.SH", valid_until="2024-01-01T00:00:00+00:00")
        )
        assert store.is_member_confirmed("600000.SH", "theme_optical") is False

    def test_is_member_confirmed_false_counter(self, store):
        """counter 立场证据不确认成员。"""
        store.save_evidence(
            _make_evidence(symbol="600000.SH", stance="counter")
        )
        assert store.is_member_confirmed("600000.SH", "theme_optical") is False

    def test_is_member_confirmed_unknown_valid_until(self, store):
        """valid_until 为空（UNKNOWN）时视为未过期。"""
        store.save_evidence(_make_evidence(symbol="600000.SH", valid_until=""))
        assert store.is_member_confirmed("600000.SH", "theme_optical") is True

    def test_expire_evidence(self, store):
        """手动过期证据。"""
        evidence = _make_evidence(symbol="600000.SH")
        store.save_evidence(evidence)
        assert store.is_member_confirmed("600000.SH", "theme_optical") is True
        assert store.expire_evidence(evidence.evidence_id, "2025-06-01T00:00:00+00:00") is True
        assert store.is_member_confirmed("600000.SH", "theme_optical") is False

    def test_expire_evidence_not_found(self, store):
        """过期不存在的证据返回 False。"""
        assert store.expire_evidence("nonexistent_id", "2025-06-01T00:00:00+00:00") is False

    def test_indexes_exist(self, store):
        """联合索引和 valid_until 索引存在。"""
        with sqlite3.connect(store.db_path) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='membership_evidence'"
                ).fetchall()
            }
        assert any("symbol" in idx and "theme_id" in idx for idx in indexes)
        assert any("valid_until" in idx for idx in indexes)


# ---------- 强校验测试 ----------

class TestStrongValidation:
    """P1.3：grounded support 证据保存前强校验。"""

    @pytest.fixture
    def store(self, tmp_path):
        return MembershipEvidenceStore(tmp_path / "membership_evidence.db")

    def test_reject_empty_symbol(self, store):
        """空 symbol 拒绝保存。"""
        evidence = _make_evidence(symbol="")
        assert store.save_evidence(evidence) is False

    def test_reject_empty_theme_id(self, store):
        """空 theme_id 拒绝保存。"""
        evidence = _make_evidence(theme_id="")
        assert store.save_evidence(evidence) is False

    def test_reject_grounded_support_without_quote(self, store):
        """grounded support 证据无 quote 拒绝保存。"""
        evidence = _make_evidence()
        evidence.quote = ""
        assert store.save_evidence(evidence) is False

    def test_reject_grounded_support_without_document(self, store):
        """grounded support 证据无 document_id/document_version_id 拒绝保存。"""
        evidence = _make_evidence()
        evidence.document_id = ""
        evidence.document_version_id = ""
        assert store.save_evidence(evidence) is False

    def test_reject_grounded_support_without_locator(self, store):
        """grounded support 证据无 locator 拒绝保存。"""
        evidence = _make_evidence()
        evidence.locator = {}
        assert store.save_evidence(evidence) is False

    def test_reject_grounded_support_without_source(self, store):
        """grounded support 证据无 source_url 和 provider 拒绝保存。"""
        evidence = _make_evidence()
        evidence.source_url = ""
        evidence.provider = ""
        assert store.save_evidence(evidence) is False

    def test_reject_grounded_support_without_retrieved_at(self, store):
        """grounded support 证据无 retrieved_at 拒绝保存。"""
        evidence = _make_evidence()
        evidence.retrieved_at = ""
        assert store.save_evidence(evidence) is False

    def test_accept_counter_without_full_locator(self, store):
        """counter 证据不要求 grounded support 的完整字段（可保存但不确认成员）。"""
        evidence = _make_evidence(stance="counter")
        evidence.locator = {}
        assert store.save_evidence(evidence) is True

    def test_accept_unverified_without_full_locator(self, store):
        """unverified 证据不要求 grounded support 的完整字段（可保存但不确认成员）。"""
        evidence = _make_evidence(grounding_status="unverified")
        evidence.locator = {}
        assert store.save_evidence(evidence) is True


# ---------- 确认器测试 ----------

class TestMembershipConfirmator:
    @pytest.fixture
    def store(self, tmp_path):
        return MembershipEvidenceStore(tmp_path / "membership_evidence.db")

    @pytest.fixture
    def confirmator(self, store):
        return MembershipConfirmator(store)

    def test_candidate_without_evidence_stays_in_exploration(self, store, confirmator):
        """无事实证据时候选只进入探索视图（membership_status 保持 recall_only）。"""
        candidate = _make_candidate()
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_candidate_with_evidence_status_grounded(self, store, confirmator):
        """有事实证据时候选 membership_status 升级为 grounded。"""
        candidate = _make_candidate()
        store.save_evidence(_make_evidence(symbol="600000.SH", theme_id="theme_optical"))
        confirmed = confirmator.filter_confirmed([candidate])
        assert len(confirmed) == 1
        assert confirmed[0].membership_status == MEMBERSHIP_STATUS_GROUNDED

    def test_expired_evidence_not_confirmed(self, store, confirmator):
        """过期证据不被确认。"""
        candidate = _make_candidate()
        store.save_evidence(
            _make_evidence(symbol="600000.SH", valid_until="2024-01-01T00:00:00+00:00")
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_unverified_evidence_not_confirmed(self, store, confirmator):
        """unverified 证据不确认候选。"""
        candidate = _make_candidate()
        store.save_evidence(
            _make_evidence(symbol="600000.SH", grounding_status="unverified")
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []

    def test_conflicted_evidence_not_confirmed(self, store, confirmator):
        """conflicted 证据不确认候选。"""
        candidate = _make_candidate()
        store.save_evidence(
            _make_evidence(symbol="600000.SH", grounding_status="conflicted")
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []

    def test_counter_evidence_not_confirmed(self, store, confirmator):
        """counter 立场证据不确认候选。"""
        candidate = _make_candidate()
        store.save_evidence(
            _make_evidence(symbol="600000.SH", stance="counter")
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []

    def test_neutral_evidence_not_confirmed(self, store, confirmator):
        """neutral 立场证据不确认候选。"""
        candidate = _make_candidate()
        store.save_evidence(
            _make_evidence(symbol="600000.SH", stance="neutral")
        )
        confirmed = confirmator.filter_confirmed([candidate])
        assert confirmed == []

    def test_confirm_candidate_with_matching_evidence(self, store, confirmator):
        """confirm_candidate 用匹配的 grounded support 证据确认候选。"""
        candidate = _make_candidate()
        evidence = _make_evidence(symbol="600000.SH", theme_id="theme_optical")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is True
        assert candidate.membership_status == MEMBERSHIP_STATUS_GROUNDED

    def test_confirm_candidate_with_mismatched_symbol(self, store, confirmator):
        """symbol 不匹配时不确认。"""
        candidate = _make_candidate(symbol="600000.SH")
        evidence = _make_evidence(symbol="000001.SZ")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_mismatched_theme(self, store, confirmator):
        """theme_id 不匹配时不确认。"""
        candidate = _make_candidate(theme_id="theme_optical")
        evidence = _make_evidence(theme_id="theme_other")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_unverified_evidence(self, confirmator):
        """unverified 证据不确认候选。"""
        candidate = _make_candidate()
        evidence = _make_evidence(grounding_status="unverified")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_expired_evidence(self, confirmator):
        """过期证据不确认候选。"""
        candidate = _make_candidate()
        evidence = _make_evidence(valid_until="2024-01-01T00:00:00+00:00")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_counter_evidence(self, confirmator):
        """counter 立场证据不确认候选。"""
        candidate = _make_candidate()
        evidence = _make_evidence(stance="counter")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_neutral_evidence(self, confirmator):
        """neutral 立场证据不确认候选。"""
        candidate = _make_candidate()
        evidence = _make_evidence(stance="neutral")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is False
        assert candidate.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY

    def test_confirm_candidate_with_unknown_valid_until(self, confirmator):
        """valid_until 为空（UNKNOWN）时视为未过期，可确认。"""
        candidate = _make_candidate()
        evidence = _make_evidence(valid_until="")
        result = confirmator.confirm_candidate(candidate, evidence)
        assert result is True
        assert candidate.membership_status == MEMBERSHIP_STATUS_GROUNDED

    def test_filter_confirmed_mixed(self, store, confirmator):
        """混合候选：部分确认、部分未确认。"""
        store.save_evidence(_make_evidence(symbol="600000.SH"))
        store.save_evidence(_make_evidence(symbol="000001.SZ"))
        candidates = [
            _make_candidate(symbol="600000.SH"),  # 有证据
            _make_candidate(symbol="000001.SZ"),  # 有证据
            _make_candidate(symbol="600001.SH"),  # 无证据
        ]
        confirmed = confirmator.filter_confirmed(candidates)
        confirmed_symbols = {c.symbol for c in confirmed}
        assert confirmed_symbols == {"600000.SH", "000001.SZ"}
        # 未确认的保持 recall_only
        unconfirmed = next(c for c in candidates if c.symbol == "600001.SH")
        assert unconfirmed.membership_status == MEMBERSHIP_STATUS_RECALL_ONLY
        # 已确认的升级为 grounded
        confirmed_first = next(c for c in candidates if c.symbol == "600000.SH")
        assert confirmed_first.membership_status == MEMBERSHIP_STATUS_GROUNDED

    def test_filter_confirmed_preserves_shadow_confidence(self, store, confirmator):
        """确认只修改 membership_status，不修改 confidence（shadow 分数）。"""
        candidate = _make_candidate()
        candidate.membership_confidence = 0.8
        store.save_evidence(_make_evidence(symbol="600000.SH"))
        confirmed = confirmator.filter_confirmed([candidate])
        assert len(confirmed) == 1
        # confidence 字段保持不变（shadow 分数）
        assert confirmed[0].membership_confidence == 0.8
        # 但 membership_status 已升级
        assert confirmed[0].membership_status == MEMBERSHIP_STATUS_GROUNDED
