"""控制面统一契约边界回归测试。"""
from __future__ import annotations

from core.research.control_plane.contracts import (
    CalibrationRun,
    DocumentVersion,
    EvidenceRecord,
    FeedbackEvent,
    PolicyVersion,
    ReviewItem,
)


def test_boolean_text_is_not_treated_as_truthy():
    document = DocumentVersion(content_available="false")
    evidence = EvidenceRecord(is_first_party="false")

    assert document.content_available is False
    assert evidence.is_first_party is False


def test_key_control_plane_strings_degrade_to_safe_defaults():
    review = ReviewItem(review_id=None, claim_id=None, status=None)
    feedback = FeedbackEvent(feedback_id=None, domain=None, target_id=None)
    calibration = CalibrationRun(calibration_id=None, domain=None, status=None)
    policy = PolicyVersion(policy_version=None, domain=None, status=None)

    assert review.claim_id == ""
    assert review.status == "pending"
    assert feedback.domain == "knowledge"
    assert feedback.target_id == ""
    assert calibration.domain == "source_quality"
    assert calibration.status == "shadow"
    assert policy.domain == "research"
    assert policy.status == "draft"
