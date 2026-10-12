"""Unified MarketSession state machine and price-anomaly detector.

This module is the single source of truth for A-share intraday session
classification. It never emits orders, positions, entry prices, exits, profit
targets or stop losses -- it only classifies the wall-clock time into one of
seven canonical sessions.

Session timeline (Asia/Shanghai):
    00:00-09:00  CLOSED
    09:00-09:15  PRE_MARKET
    09:15-09:25  CALL_AUCTION  (opening call auction)
    09:25-09:30  PRE_MARKET   (transition between call auction and continuous)
    09:30-11:30  MORNING
    11:30-13:00  LUNCH_BREAK
    13:00-14:57  AFTERNOON
    14:57-15:00  CALL_AUCTION (closing call auction)
    15:00-15:30  POST_MARKET
    15:30-23:59  CLOSED
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("Asia/Shanghai")


class MarketSession(str, Enum):
    """Canonical intraday sessions for the A-share market."""

    CLOSED = "closed"            # 休市（非交易日 / 09:00 前 / 15:30 后）
    PRE_MARKET = "pre_market"    # 盘前
    CALL_AUCTION = "call_auction" # 集合竞价（开盘或收盘）
    MORNING = "morning"          # 上午连续交易
    LUNCH_BREAK = "lunch_break"  # 午休
    AFTERNOON = "afternoon"      # 下午连续交易
    POST_MARKET = "post_market"  # 收盘后


# 按时间顺序排列的「正常交易日」会话序列，用于 next_session 推导。
_SESSION_ORDER: list[MarketSession] = [
    MarketSession.PRE_MARKET,
    MarketSession.CALL_AUCTION,  # opening call auction
    MarketSession.MORNING,
    MarketSession.LUNCH_BREAK,
    MarketSession.AFTERNOON,
    MarketSession.CALL_AUCTION,  # closing call auction
    MarketSession.POST_MARKET,
    MarketSession.CLOSED,
]


def _to_market_datetime(now: datetime) -> datetime:
    """把任意 datetime 归一化为 Asia/Shanghai 时区。

    朴素 datetime 视为市场本地时间；带时区的 datetime 转换到市场时区。
    """
    if now.tzinfo is None:
        return now.replace(tzinfo=MARKET_TZ)
    return now.astimezone(MARKET_TZ)


def _as_time(value: datetime) -> time:
    """取出市场时区下的 time 部分。"""
    return _to_market_datetime(value).time()


class MarketSessionStateMachine:
    """A-share intraday session classifier.

    Args:
        trading_calendar: 可选交易日历。支持以下三种形式：
            - ``None``：降级为周一到周五判断（不区分节假日）。
            - ``Callable[[date], bool]``：传入日期返回是否交易日。
            - ``Iterable[str]``：交易日字符串集合，格式 ``YYYY-MM-DD``。
    """

    def __init__(
        self,
        trading_calendar: Callable[[date], bool] | Iterable[str] | None = None,
    ) -> None:
        self._calendar_func = self._normalize_calendar(trading_calendar)

    @staticmethod
    def _normalize_calendar(
        calendar: Callable[[date], bool] | Iterable[str] | None,
    ) -> Callable[[date], bool]:
        """把不同形式的交易日历归一化为统一可调用对象。"""
        if calendar is None:
            return lambda candidate: candidate.weekday() < 5
        if callable(calendar):
            return calendar
        # 视为可迭代字符串集合，预先转成 set 加速查找
        try:
            open_dates = {str(item) for item in calendar}
        except TypeError as exc:
            raise TypeError(
                "trading_calendar must be None, callable, or iterable of date strings"
            ) from exc

        def _lookup(candidate: date) -> bool:
            return candidate.isoformat() in open_dates

        return _lookup

    def is_trading_day(self, target: date | datetime) -> bool:
        """判断某天是否为交易日。"""
        day = target.date() if isinstance(target, datetime) else target
        try:
            return bool(self._calendar_func(day))
        except Exception as exc:
            logger.warning("trading calendar lookup failed: %s; fallback weekday", exc)
            return day.weekday() < 5

    def current_session(self, now: datetime) -> MarketSession:
        """根据当前时间返回所属交易时段。

        - 非交易日返回 ``CLOSED``。
        - 交易日按时间窗口返回具体时段。
        """
        market_now = _to_market_datetime(now)
        if not self.is_trading_day(market_now):
            return MarketSession.CLOSED
        current_time = market_now.time()
        return self._session_for_time(current_time)

    @staticmethod
    def _session_for_time(current_time: time) -> MarketSession:
        """根据市场本地时间确定交易时段。"""
        if current_time < time(9, 0):
            return MarketSession.CLOSED
        if current_time < time(9, 15):
            return MarketSession.PRE_MARKET
        if current_time < time(9, 25):
            return MarketSession.CALL_AUCTION  # 开盘集合竞价
        if current_time < time(9, 30):
            return MarketSession.PRE_MARKET  # 集合竞价后到连续竞价前的过渡
        if current_time < time(11, 30):
            return MarketSession.MORNING
        if current_time < time(13, 0):
            return MarketSession.LUNCH_BREAK
        if current_time < time(14, 57):
            return MarketSession.AFTERNOON
        if current_time < time(15, 0):
            return MarketSession.CALL_AUCTION  # 收盘集合竞价
        if current_time < time(15, 30):
            return MarketSession.POST_MARKET
        return MarketSession.CLOSED

    def is_trading_time(self, now: datetime) -> bool:
        """是否处于实际撮合时段（上午/下午/集合竞价）。"""
        return self.current_session(now) in {
            MarketSession.MORNING,
            MarketSession.AFTERNOON,
            MarketSession.CALL_AUCTION,
        }

    def next_session(self, now: datetime) -> MarketSession:
        """返回下一时段。

        若当日已全部走完（``POST_MARKET`` 之后），下一时段为下一交易日的
        ``PRE_MARKET``；若下一日非交易日，继续向后找最近交易日。
        """
        current = self.current_session(now)
        market_now = _to_market_datetime(now)
        if current == MarketSession.CLOSED:
            # 在休市窗口内，下一时段取决于现在的时间：
            # - 当前 <= POST_MARKET 结束（15:30），且当前是交易日 → POST_MARKET
            # - 否则找下一交易日的 PRE_MARKET
            today_closed = market_now.time() >= time(15, 30)
            if (
                self.is_trading_day(market_now)
                and market_now.time() >= time(15, 0)
                and not today_closed
            ):
                return MarketSession.POST_MARKET
            return MarketSession.PRE_MARKET

        current_index = _SESSION_ORDER.index(current)
        # CALL_AUCTION 在序列里出现两次，需要区分开盘 / 收盘场景
        if current == MarketSession.CALL_AUCTION:
            current_index = 1 if market_now.time() < time(12, 0) else 5
        next_index = current_index + 1
        return _SESSION_ORDER[next_index] if next_index < len(_SESSION_ORDER) else MarketSession.CLOSED


@dataclass
class AnomalyResult:
    """Price-anomaly detection result.

    仅为增量研究假设，不直接升级 Claim 优先级，也不进入证据槽/后验。
    """

    symbol: str
    anomaly_type: str           # surge / plunge
    pct_chg: float
    hypothesis_text: str
    created_at: str
    candidate_id: str = ""
    query_prototype: str = ""

    def to_retrieval_hypothesis(self, hypothesis_id: str) -> Any:
        """转换为 RetrievalHypothesis 数据结构。

        遵循 ``core.research_graph.retrieval_hypothesis`` 的契约：生成的假设
        ``eligible_for_evidence_slot=False``、``eligible_for_posterior=False``，
        仅用于构造检索查询原型，不会写入证据槽。
        """
        from core.research_graph.retrieval_hypothesis import RetrievalHypothesis

        return RetrievalHypothesis(
            hypothesis_id=hypothesis_id,
            candidate_id=self.candidate_id,
            query_prototype=self.query_prototype or self.hypothesis_text,
            reason=self.hypothesis_text,
            generated_by="price_anomaly_detector",
        )


class PriceAnomalyDetector:
    """Detect intraday price anomalies and emit RetrievalHypothesis records.

    依据 AGENTS.md：仅产出研究任务/检索假设，不会输出投资建议，也不会直接
    升级 Claim 优先级。阈值：
        - 涨幅 >= 7% 触发 ``surge`` 假设
        - 跌幅 <= -7% 触发 ``plunge`` 假设
    """

    SURGE_THRESHOLD: float = 7.0
    PLUNGE_THRESHOLD: float = -7.0

    def __init__(self, surge_threshold: float | None = None, plunge_threshold: float | None = None) -> None:
        """允许通过参数覆盖阈值，便于测试。"""
        if surge_threshold is not None:
            self.SURGE_THRESHOLD = float(surge_threshold)
        if plunge_threshold is not None:
            self.PLUNGE_THRESHOLD = float(plunge_threshold)

    def detect(
        self,
        *,
        symbol: str,
        pct_chg: float,
        session: MarketSession,
        now: datetime | None = None,
    ) -> AnomalyResult | None:
        """检测价格异动，触发阈值时返回 AnomalyResult。

        Args:
            symbol: 证券代码。
            pct_chg: 涨跌幅（百分比，例如 8.5 表示 +8.5%）。
            session: 当前交易时段，仅在实际撮合时段才检测。
            now: 触发时间，默认取当前市场时间。

        Returns:
            AnomalyResult 或 None（未触发阈值或非交易时段）。
        """
        if not symbol:
            return None
        if session not in {
            MarketSession.MORNING,
            MarketSession.AFTERNOON,
            MarketSession.CALL_AUCTION,
        }:
            return None
        try:
            change = float(pct_chg)
        except (TypeError, ValueError):
            return None

        anomaly_type = ""
        if change >= self.SURGE_THRESHOLD:
            anomaly_type = "surge"
        elif change <= self.PLUNGE_THRESHOLD:
            anomaly_type = "plunge"
        else:
            return None

        market_now = _to_market_datetime(now) if now is not None else datetime.now(MARKET_TZ)
        hypothesis_text = self._build_hypothesis_text(symbol, change, anomaly_type)
        return AnomalyResult(
            symbol=str(symbol),
            anomaly_type=anomaly_type,
            pct_chg=round(change, 4),
            hypothesis_text=hypothesis_text,
            created_at=market_now.isoformat(timespec="seconds"),
            candidate_id=f"price_anomaly_{symbol}",
            query_prototype=hypothesis_text,
        )

    def _build_hypothesis_text(self, symbol: str, pct_chg: float, anomaly_type: str) -> str:
        """构造检索假设的查询原型文本（仅作为查询输入，不进入证据槽）。"""
        direction = "涨幅" if anomaly_type == "surge" else "跌幅"
        return f"{symbol} 盘中{direction} {abs(pct_chg):.2f}%，触发价格异动检索假设。"
