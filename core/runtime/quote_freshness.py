"""Intraday quote freshness checker.

Validates that the realtime quote used for monitoring carries a recent
exchange-side timestamp and refers to the expected trade date. Different
market sessions have different freshness expectations:

    - MORNING / AFTERNOON:        strict (default 15s stale threshold)
    - CALL_AUCTION:                tolerant (snapshot may lag the call window)
    - LUNCH_BREAK / POST_MARKET:  lenient (last snapshot from prior session)
    - PRE_MARKET:                  only date validation matters
    - CLOSED:                      no freshness expectation

This module only emits freshness verdicts; it never produces trading signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.runtime.market_session import MarketSession, MARKET_TZ

# 时段默认的「最大可接受延迟」（秒）。0 表示不限制延迟（仅做日期校验）。
DEFAULT_MAX_STALE_SECONDS: dict[MarketSession, int] = {
    MarketSession.MORNING: 15,
    MarketSession.AFTERNOON: 15,
    MarketSession.CALL_AUCTION: 60,
    MarketSession.LUNCH_BREAK: 60 * 60,        # 上一交易段最后一笔快照可保留到午休结束
    MarketSession.POST_MARKET: 60 * 30,        # 收盘后允许稍长的延迟
    MarketSession.PRE_MARKET: 0,               # 盘前无实时撮合，不限制延迟
    MarketSession.CLOSED: 0,                    # 休市不检查延迟
}


@dataclass
class FreshnessPolicy:
    """Per-session freshness policy.

    Attributes:
        max_stale_seconds: 按时段映射的最大可接受延迟（秒）。键不存在时
            表示「不限制延迟」，仅做日期校验。
        exchange_timestamp_required: 是否强制要求交易所时间戳。为 False 时
            允许缺失时间戳返回 ``UNKNOWN`` 而非 ``STALE``。
    """

    max_stale_seconds: dict[MarketSession, int] = field(
        default_factory=lambda: dict(DEFAULT_MAX_STALE_SECONDS)
    )
    exchange_timestamp_required: bool = True

    def threshold_for(self, session: MarketSession) -> int | None:
        """返回指定时段的最大可接受延迟；返回 None 表示不限制。"""
        return self.max_stale_seconds.get(session)


@dataclass
class FreshnessResult:
    """Quote freshness check verdict."""

    is_fresh: bool
    staleness_seconds: float
    reason: str
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_fresh": self.is_fresh,
            "staleness_seconds": round(self.staleness_seconds, 2),
            "reason": self.reason,
            "degraded": self.degraded,
        }


class QuoteFreshnessChecker:
    """Check realtime quote freshness against the active market session."""

    def __init__(self, policy: FreshnessPolicy | None = None) -> None:
        self.policy = policy or FreshnessPolicy()

    def check(
        self,
        *,
        exchange_timestamp: datetime | str | None,
        as_of_date: str | None,
        session: MarketSession,
        now: datetime,
    ) -> FreshnessResult:
        """Check quote freshness.

        Args:
            exchange_timestamp: 交易所侧的时间戳。支持 datetime 或字符串，
                字符串要求可被 ``datetime.fromisoformat`` 解析（或形如
                ``YYYYMMDDHHMMSS`` 的紧凑格式）。``None`` 表示缺失。
            as_of_date: 行情对应的交易日期（``YYYY-MM-DD``）。``None`` 表示缺失。
            session: 当前 ``MarketSession``。
            now: 校验当前时间（带时区更佳；朴素 datetime 视为市场本地时间）。

        Returns:
            FreshnessResult: 包含 ``is_fresh``、``staleness_seconds``、
            ``reason``、``degraded``。
        """
        market_now = self._to_market_datetime(now)

        # 1) 交易日期校验：as_of_date 必须与当前交易日一致
        date_result = self._check_trade_date(as_of_date, market_now)
        if date_result is not None:
            return date_result

        # 2) 休市 / 盘前不要求时间戳新鲜度
        if session == MarketSession.CLOSED:
            return FreshnessResult(
                is_fresh=True,
                staleness_seconds=0.0,
                reason="closed_session_no_freshness_required",
            )
        if session == MarketSession.PRE_MARKET:
            return FreshnessResult(
                is_fresh=True,
                staleness_seconds=0.0,
                reason="pre_market_no_realtime_required",
            )

        # 3) 时间戳缺失
        if exchange_timestamp is None or exchange_timestamp == "":
            if self.policy.exchange_timestamp_required:
                return FreshnessResult(
                    is_fresh=False,
                    staleness_seconds=0.0,
                    reason="exchange_timestamp_missing",
                    degraded=True,
                )
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=0.0,
                reason="exchange_timestamp_unknown",
                degraded=True,
            )

        parsed_ts = self._parse_timestamp(exchange_timestamp)
        if parsed_ts is None:
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=0.0,
                reason="exchange_timestamp_unparseable",
                degraded=True,
            )
        parsed_ts = self._to_market_datetime(parsed_ts)
        staleness = (market_now - parsed_ts).total_seconds()
        if staleness < 0:
            # 交易所时间戳超前于 now（时钟漂移或测试边界），按 0 处理
            staleness = 0.0

        threshold = self.policy.threshold_for(session)
        # 午休 / 收盘后：允许保留上一交易段的最后快照
        if session in {MarketSession.LUNCH_BREAK, MarketSession.POST_MARKET}:
            return self._evaluate_session_with_tolerance(
                staleness, threshold, session, parsed_ts, market_now
            )

        # 集合竞价：允许稍长的快照延迟
        if session == MarketSession.CALL_AUCTION:
            return self._evaluate_session_with_tolerance(
                staleness, threshold, session, parsed_ts, market_now
            )

        # 连续交易段：严格判定
        if threshold is None or threshold <= 0:
            return FreshnessResult(
                is_fresh=True,
                staleness_seconds=staleness,
                reason="no_threshold_for_session",
            )
        if staleness > threshold:
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=staleness,
                reason=f"stale_quote_{staleness:.0f}s_gt_{threshold}s",
                degraded=True,
            )
        return FreshnessResult(
            is_fresh=True,
            staleness_seconds=staleness,
            reason="fresh",
        )

    def _check_trade_date(
        self, as_of_date: str | None, market_now: datetime
    ) -> FreshnessResult | None:
        """校验行情所属交易日期与当前日期是否一致。

        Returns:
            FreshnessResult 如果日期不一致或缺失则返回降级结果；None 表示
            日期校验通过，继续后续时间戳检查。
        """
        today_text = market_now.date().isoformat()
        if not as_of_date:
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=0.0,
                reason="as_of_date_missing",
                degraded=True,
            )
        normalized = self._normalize_date_text(str(as_of_date))
        if normalized != today_text:
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=0.0,
                reason=f"as_of_date_mismatch:{normalized}_vs_{today_text}",
                degraded=True,
            )
        return None

    def _evaluate_session_with_tolerance(
        self,
        staleness: float,
        threshold: int | None,
        session: MarketSession,
        parsed_ts: datetime,
        market_now: datetime,
    ) -> FreshnessResult:
        """对午休 / 集合竞价 / 收盘后等容忍时段的延迟结果进行评估。"""
        if threshold is None or threshold <= 0:
            return FreshnessResult(
                is_fresh=True,
                staleness_seconds=staleness,
                reason=f"tolerant_session_{session.value}",
            )
        if staleness > threshold:
            return FreshnessResult(
                is_fresh=False,
                staleness_seconds=staleness,
                reason=f"stale_quote_{staleness:.0f}s_gt_{threshold}s_{session.value}",
                degraded=True,
            )
        return FreshnessResult(
            is_fresh=True,
            staleness_seconds=staleness,
            reason=f"fresh_{session.value}",
        )

    @staticmethod
    def _to_market_datetime(value: datetime) -> datetime:
        """归一化 datetime 到 Asia/Shanghai。"""
        if value.tzinfo is None:
            return value.replace(tzinfo=MARKET_TZ)
        return value.astimezone(MARKET_TZ)

    @staticmethod
    def _parse_timestamp(value: datetime | str) -> datetime | None:
        """解析多种格式的时间戳。"""
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        # 兼容紧凑格式：YYYYMMDDHHMMSS
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 14 and "-" not in text and ":" not in text:
            try:
                return datetime(
                    int(digits[:4]), int(digits[4:6]), int(digits[6:8]),
                    int(digits[8:10]), int(digits[10:12]), int(digits[12:14]),
                )
            except ValueError:
                return None
        # 兼容 YYYYMMDD（仅日期）
        if len(digits) == 8 and "-" not in text:
            try:
                return datetime(
                    int(digits[:4]), int(digits[4:6]), int(digits[6:8])
                )
            except ValueError:
                return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _normalize_date_text(text: str) -> str:
        """把多种日期格式归一化为 ``YYYY-MM-DD``。"""
        cleaned = text.strip()
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if len(digits) == 8 and "-" not in cleaned:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        # 取前 10 个字符兼容 ``YYYY-MM-DD`` 前缀
        return cleaned[:10]
