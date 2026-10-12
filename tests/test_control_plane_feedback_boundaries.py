"""FeedbackController 防循环边界回归测试。"""
from __future__ import annotations

from core.research.control_plane.contracts import FeedbackEvent
from core.research.control_plane.feedback import FeedbackController
from core.research.control_plane.repository import ResearchRepository


def test_event_trace_id_is_used_for_depth_control(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    controller = FeedbackController(repository)

    results = []
    for index in range(FeedbackController.MAX_FEEDBACK_DEPTH + 1):
        results.append(controller.record(
            FeedbackEvent(
                domain="knowledge",
                event_type=f"event_{index}",
                target_id=f"claim_{index}",
                trace_id="trace_from_event",
            ),
            generate_task=True,
        ))

    assert results[-1].metrics.get("depth_capped") is True


def test_deduped_event_is_not_added_to_feedback_samples(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    controller = FeedbackController(repository)
    event = FeedbackEvent(domain="knowledge", event_type="fact", target_id="claim_1")

    controller.record(event, generate_task=False)
    duplicate = controller.record(
        FeedbackEvent(domain="knowledge", event_type="fact", target_id="claim_1"),
        generate_task=False,
    )

    assert duplicate.metrics.get("deduped") is True
    assert len(repository.list_feedback("knowledge", limit=100)) == 1


def test_record_degrades_mutated_metrics_field(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    controller = FeedbackController(repository)
    event = FeedbackEvent(domain="knowledge", event_type="fact", target_id="claim_1")
    event.metrics = None

    stored = controller.record(event, generate_task=False)

    assert stored.metrics == {}
