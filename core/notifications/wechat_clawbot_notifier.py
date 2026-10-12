"""WeChat / ClawBot notification channel.

Supported delivery methods:
1. OPENCLAW_WEBHOOK: enterprise WeChat or compatible webhook.
2. OPENCLAW_WEIXIN_TARGET: OpenClaw CLI target for the openclaw-weixin channel.

The notifier returns False when neither route is configured. That avoids
mistaking local logs for a verified WeChat delivery.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

from config.logger import get_logger
from core.notifications.models import NotificationPayload

logger = get_logger(__name__)


class WeChatClawBotNotifier:
    """Send short notifications to WeChat through OpenClaw."""

    def __init__(self) -> None:
        self._webhook_url = self._load_config("OPENCLAW_WEBHOOK")
        self._weixin_target = self._load_config("OPENCLAW_WEIXIN_TARGET")
        self._weixin_account = self._load_config("OPENCLAW_WEIXIN_ACCOUNT")
        self._weixin_channel = self._load_config("OPENCLAW_WEIXIN_CHANNEL") or "openclaw-weixin"

    @property
    def configured(self) -> bool:
        return bool(self._webhook_url or self._weixin_target)

    @property
    def delivery_mode(self) -> str:
        if self._webhook_url:
            return "webhook"
        if self._weixin_target:
            return "openclaw_cli"
        return "not_configured"

    def send(self, payload: NotificationPayload) -> bool:
        try:
            message = self._format_short_text(payload)
            if self._webhook_url:
                return self._send_via_webhook(message, payload)
            if self._weixin_target:
                return self._send_via_openclaw_cli(message)
            logger.warning("WeChat/ClawBot delivery is not configured")
            return False
        except Exception as exc:
            logger.error("ClawBot notification failed: %s", exc, exc_info=True)
            return False

    def _format_short_text(self, payload: NotificationPayload) -> str:
        if isinstance(payload.data, dict):
            grounded = str(payload.data.get("clawbot_short_text") or "").strip()
            if grounded:
                return grounded[:700]
        priority_label = {"high": "!!", "medium": "!", "low": ""}.get(payload.priority, "")
        text = f"{priority_label}{payload.title}: {payload.summary}"
        for link in payload.links:
            link_text = link.get("text", "")
            link_url = link.get("url", "")
            if link_url:
                text += f"\n{link_text}: {link_url}"
                break
        return text

    def _send_via_webhook(self, message: str, payload: NotificationPayload) -> bool:
        try:
            import requests
        except ImportError:
            logger.warning("requests is not installed; cannot send ClawBot webhook")
            return False

        md_body = {
            "msgtype": "markdown",
            "markdown": {"content": f"**{payload.title}**\n> {payload.summary}"},
        }
        try:
            response = requests.post(self._webhook_url, json=md_body, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result.get("errcode") == 0 or result.get("code") == 0:
                    return True

            text_body = {"msgtype": "text", "text": {"content": message}}
            response = requests.post(self._webhook_url, json=text_body, timeout=10)
            if response.status_code == 200:
                try:
                    result = response.json()
                    return result.get("errcode", 0) == 0 or result.get("code", 0) == 0
                except Exception:
                    return True
            logger.warning("ClawBot webhook failed: status=%s", response.status_code)
            return False
        except Exception as exc:
            logger.error("ClawBot webhook request failed: %s", exc, exc_info=True)
            return False

    def _send_via_openclaw_cli(self, message: str) -> bool:
        executable = shutil.which("openclaw.cmd") or shutil.which("openclaw")
        if not executable:
            logger.warning("OpenClaw CLI executable was not found in PATH")
            return False
        command = [
            executable,
            "message",
            "send",
            "--channel",
            self._weixin_channel,
            "--target",
            self._weixin_target,
            "--message",
            message,
            "--json",
        ]
        if self._weixin_account:
            command.extend(["--account", self._weixin_account])

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if completed.returncode != 0:
            logger.warning("OpenClaw CLI send failed: %s", completed.stderr.strip())
            return False

        try:
            payload = json.loads(completed.stdout)
            return bool(payload.get("ok", True))
        except Exception:
            return True

    @staticmethod
    def _load_config(key: str) -> str:
        try:
            from config.config import Config

            value = getattr(Config, key, "")
            if value:
                return str(value)
        except Exception:
            pass
        return os.getenv(key, "")
