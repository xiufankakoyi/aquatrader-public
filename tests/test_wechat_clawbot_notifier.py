from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

from core.notifications.wechat_clawbot_notifier import WeChatClawBotNotifier
from core.notifications.models import NotificationPayload


def test_openclaw_cli_uses_resolved_windows_command() -> None:
    notifier = WeChatClawBotNotifier()
    notifier._weixin_target = "personal@im.wechat"
    notifier._weixin_account = "account-im-bot"

    with (
        patch(
            "core.notifications.wechat_clawbot_notifier.shutil.which",
            return_value=r"C:/Users/USER\AppData\Roaming\npm\openclaw.cmd",
        ),
        patch(
            "core.notifications.wechat_clawbot_notifier.subprocess.run",
            return_value=CompletedProcess([], 0, stdout='{"ok": true}', stderr=""),
        ) as run,
    ):
        assert notifier._send_via_openclaw_cli("测试") is True

    command = run.call_args.args[0]
    assert command[0].lower().endswith("openclaw.cmd")


def test_clawbot_prefers_grounded_short_recap() -> None:
    notifier = WeChatClawBotNotifier()
    payload = NotificationPayload(
        event_type="daily_brief_ready",
        title="完整标题不应进入短报",
        summary="完整摘要不应进入短报",
        priority="medium",
        data={"clawbot_short_text": "2026/7/16复盘\n今日大盘：偏强"},
    )

    assert notifier._format_short_text(payload) == "2026/7/16复盘\n今日大盘：偏强"
