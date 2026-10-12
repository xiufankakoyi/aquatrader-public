from __future__ import annotations

import threading

from core.notifications.models import NotificationPayload
from core.notifications.notification_service import NotificationService


def _service(channels):
    service = NotificationService.__new__(NotificationService)
    service._channels = channels
    service._lock = threading.Lock()
    return service


def _payload() -> NotificationPayload:
    return NotificationPayload(
        event_type="daily_brief_ready",
        title="测试报告",
        summary="测试摘要",
        priority="medium",
        links=[],
        data={"delivery_required_any": ["openclaw", "pushplus"]},
    )


def test_local_log_and_feishu_do_not_mask_failed_wechat_delivery() -> None:
    service = _service({
        "local_log": lambda payload: True,
        "openclaw": lambda payload: False,
        "feishu": lambda payload: True,
        "pushplus": lambda payload: False,
    })

    assert service.notify(_payload()) is False


def test_one_successful_wechat_primary_channel_completes_delivery() -> None:
    service = _service({
        "local_log": lambda payload: True,
        "openclaw": lambda payload: False,
        "pushplus": lambda payload: True,
    })

    assert service.notify(_payload()) is True
