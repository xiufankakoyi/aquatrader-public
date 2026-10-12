from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os

import requests


def load_feishu_webhook() -> str | None:
    webhook = os.getenv("FEISHU_WEBHOOK")
    if webhook:
        return webhook
    try:
        from config.config import Config

        return getattr(Config, "FEISHU_WEBHOOK", None) or None
    except Exception:
        return None


def push_scan_to_feishu(message: str, run_dir: str | Path, webhook_url: str | None = None) -> dict[str, Any]:
    webhook = webhook_url or load_feishu_webhook()
    status = {
        "enabled": True,
        "configured": bool(webhook),
        "success": False,
        "reason": "",
    }
    if not webhook:
        status["reason"] = "FEISHU_WEBHOOK not configured"
        _write_status(run_dir, status)
        return status

    body = {
        "msg_type": "text",
        "content": {
            "text": message,
        },
    }
    try:
        response = requests.post(webhook, json=body, timeout=15)
        status["http_status"] = response.status_code
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = {"text": response.text[:500]}
        status["response"] = response_payload
        status["success"] = response.status_code == 200 and (
            response_payload.get("StatusCode") == 0 or response_payload.get("code") == 0
        )
        if not status["success"]:
            status["reason"] = f"feishu webhook rejected response: {response_payload}"
    except Exception as exc:  # noqa: BLE001
        status["reason"] = f"feishu request failed: {exc}"

    _write_status(run_dir, status)
    return status


def _write_status(run_dir: str | Path, status: dict[str, Any]) -> None:
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "feishu_push.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
