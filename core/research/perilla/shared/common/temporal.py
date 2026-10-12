"""Shared temporal utilities for Perilla modules.

Provides ISO timestamp helpers used by discovery models, evidence trackers,
and report builders.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """返回当前时间的 ISO 8601 格式字符串.

    Returns:
        当前 UTC 时间的 ISO 格式字符串，如 "2025-01-15T08:30:00+00:00"。
    """
    return datetime.now(timezone.utc).isoformat()
