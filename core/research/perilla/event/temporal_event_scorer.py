"""Temporal event scorer.

Adjusts event confidence based on recency with exponential decay curves.
Different source types have different half-lives:
  - Official announcements/filings: 30 days (slow decay)
  - Financial media: 14 days
  - Social media/web_search: 3 days (fast decay)
  - Default: 7 days
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from core.research.perilla.event.event_graph_schema import EventClaim


_HALF_LIFE_DAYS: dict[str, float] = {
    "official_announcement": 30.0,
    "exchange_filing": 30.0,
    "regulatory": 30.0,
    "broker_report": 21.0,
    "financial_media": 14.0,
    "industry_media": 10.0,
    "social_media": 3.0,
    "web_search": 5.0,
    "active_learning": 5.0,
    "unknown": 7.0,
}

_DEFAULT_HALF_LIFE = 7.0
_MAX_AGE_DAYS = 90.0
_MIN_SCORE = 0.05


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


class TemporalEventScorer:
    """Scores event claims with exponential temporal decay."""

    def __init__(self, half_life_overrides: dict[str, float] | None = None) -> None:
        """初始化。

        Args:
            half_life_overrides: 自定义半衰期覆盖（天）。
        """
        self._half_lives = dict(_HALF_LIFE_DAYS)
        if isinstance(half_life_overrides, Mapping):
            for source_type, value in half_life_overrides.items():
                half_life = _finite_float(value)
                if half_life > 0:
                    self._half_lives[str(source_type)] = half_life

    def score(self, event_claim: EventClaim) -> float:
        """Return a temporally-adjusted score for an event claim.

        使用指数衰减：score = confidence * exp(-ln(2) * age_days / half_life)
        - 无 source_time 时直接返回 confidence * freshness_score
        - 超过 90 天的事件分数压缩到最低值
        - 事件状态为 deprecated/resolved 时额外降权

        Args:
            event_claim: Event claim to score.

        Returns:
            Temporally-adjusted score between 0 and 1.
        """
        base_confidence = max(0.0, min(1.0, _finite_float(_field(event_claim, "confidence"))))
        source_time = _text(_field(event_claim, "source_time")) or _text(_field(event_claim, "time"))

        if not source_time:
            return round(
                max(_MIN_SCORE, base_confidence * max(_finite_float(_field(event_claim, "freshness_score")), 0.1)),
                4,
            )

        age_days = self._parse_age_days(source_time)
        if age_days is None:
            return round(
                max(_MIN_SCORE, base_confidence * max(_finite_float(_field(event_claim, "freshness_score")), 0.1)),
                4,
            )

        src_type = _text(_field(event_claim, "source_type"), "unknown").lower()
        half_life = max(1e-9, _finite_float(self._half_lives.get(src_type), _DEFAULT_HALF_LIFE))

        if age_days >= _MAX_AGE_DAYS:
            decay_factor = _MIN_SCORE
        else:
            decay_factor = math.exp(-math.log(2) * age_days / half_life)
            decay_factor = max(_MIN_SCORE, decay_factor)

        freshness_score = _finite_float(_field(event_claim, "freshness_score"))
        if freshness_score > 0:
            decay_factor = decay_factor * 0.7 + min(1.0, freshness_score) * 0.3

        status = _text(_field(event_claim, "status")).lower()
        if status in ("deprecated", "resolved", "expired"):
            decay_factor *= 0.5

        if bool(_field(event_claim, "contradiction_candidate")):
            decay_factor *= 0.8

        return round(max(_MIN_SCORE, min(1.0, base_confidence * decay_factor)), 4)

    @staticmethod
    def _parse_age_days(time_str: str) -> float | None:
        """从时间字符串解析距今天数。

        支持 ISO 格式（2024-01-15 / 2024-01-15T10:30:00）和季度格式（2024Q1）。

        Returns:
            天数，解析失败返回 None。
        """
        time_str = _text(time_str)
        if not time_str:
            return None
        try:
            time_str = time_str.strip()
            if "Q" in time_str:
                year_part = time_str[:4]
                q_part = time_str[5:6] if len(time_str) > 5 else "1"
                try:
                    year = int(year_part)
                    quarter = int(q_part)
                    month = (quarter - 1) * 3 + 1
                    dt = datetime(year, month, 1)
                except (ValueError, IndexError):
                    return None
            elif "T" in time_str:
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00").split("+")[0])
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
            elif len(time_str) == 10:
                dt = datetime.strptime(time_str, "%Y-%m-%d")
            else:
                return None

            return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
        except (OverflowError, TypeError, ValueError):
            return None
