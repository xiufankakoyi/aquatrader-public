"""Phase 6 acceptance tests: market session awareness and intraday runtime.

Simulates 5 consecutive trading days of operation to validate:
- MarketSessionStateMachine covering all 7 sessions and cross-day transitions
- QuoteFreshnessChecker scenarios (fresh/stale/missing/wrong-date/compact-ts)
- MonitorScheduler per-session intervals and bounded backoff persistence
- PriceAnomalyDetector creating RetrievalHypothesis without upgrading Claim priority
- PushDedupStore deduplication and failure retry semantics

These tests follow the project rule: tests live only in ``tests/`` and never
emit buy/sell/position/stop-loss advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.runtime.market_session import (
    AnomalyResult,
    MarketSession,
    MarketSessionStateMachine,
    PriceAnomalyDetector,
    MARKET_TZ,
)
from core.runtime.monitor_schedule import (
    DEFAULT_SESSION_INTERVALS,
    MonitorSchedule,
    MonitorScheduler,
)
from core.runtime.push_dedup import PushDedupStore
from core.runtime.quote_freshness import (
    FreshnessPolicy,
    QuoteFreshnessChecker,
)


# Five consecutive trading days (Mon 2026-07-13 .. Fri 2026-07-17).
# Using an explicit set keeps the test deterministic and independent of the
# weekday fallback.
TRADING_DAYS = [
    "2026-07-13",
    "2026-07-14",
    "2026-07-15",
    "2026-07-16",
    "2026-07-17",
]
# Weekend right after the trading window, used to validate non-trading-day logic.
WEEKEND_DAYS = ["2026-07-18", "2026-07-19"]


def _trading_calendar() -> set[str]:
    """Return the explicit trading-day set used by the simulated calendar."""
    return set(TRADING_DAYS)


def _at(day: str, hour: int, minute: int, second: int = 0) -> datetime:
    """Build a timezone-aware market datetime for the given components."""
    year = int(day[:4])
    month = int(day[5:7])
    day_of_month = int(day[8:10])
    return datetime(year, month, day_of_month, hour, minute, second, tzinfo=MARKET_TZ)


# ---------------------------------------------------------------------------
# Task 6.1: MarketSessionStateMachine
# ---------------------------------------------------------------------------


@pytest.fixture
def state_machine() -> MarketSessionStateMachine:
    """State machine backed by an explicit trading-day set."""
    return MarketSessionStateMachine(trading_calendar=_trading_calendar())


class TestMarketSessionStateMachine:
    """Validate all 7 sessions, cross-day transitions, and weekend handling."""

    def test_all_seven_sessions_classified_correctly(self, state_machine: MarketSessionStateMachine) -> None:
        """All 7 MarketSession values are reachable on a single trading day."""
        day = TRADING_DAYS[0]
        cases = [
            (_at(day, 8, 30), MarketSession.CLOSED),
            (_at(day, 9, 5), MarketSession.PRE_MARKET),
            (_at(day, 9, 20), MarketSession.CALL_AUCTION),   # opening call auction
            (_at(day, 9, 27), MarketSession.PRE_MARKET),     # transition gap
            (_at(day, 9, 45), MarketSession.MORNING),
            (_at(day, 12, 0), MarketSession.LUNCH_BREAK),
            (_at(day, 13, 30), MarketSession.AFTERNOON),
            (_at(day, 14, 58), MarketSession.CALL_AUCTION),  # closing call auction
            (_at(day, 15, 10), MarketSession.POST_MARKET),
            (_at(day, 16, 0), MarketSession.CLOSED),
        ]
        for now, expected in cases:
            actual = state_machine.current_session(now)
            assert actual == expected, (
                f"at {now.time()} expected {expected.value}, got {actual.value}"
            )

    def test_all_seven_session_enum_values_are_distinct(self) -> None:
        """Sanity check: there are exactly 7 distinct MarketSession values."""
        values = {session.value for session in MarketSession}
        assert values == {
            "closed", "pre_market", "call_auction", "morning",
            "lunch_break", "afternoon", "post_market",
        }
        assert len(MarketSession) == 7

    def test_weekend_returns_closed(self, state_machine: MarketSessionStateMachine) -> None:
        """Weekend (non-trading day) returns CLOSED regardless of time."""
        for day in WEEKEND_DAYS:
            assert state_machine.current_session(_at(day, 10, 0)) == MarketSession.CLOSED
            assert state_machine.current_session(_at(day, 14, 0)) == MarketSession.CLOSED

    def test_is_trading_time_only_for_matching_sessions(self, state_machine: MarketSessionStateMachine) -> None:
        """is_trading_time returns True only for MORNING / AFTERNOON / CALL_AUCTION."""
        day = TRADING_DAYS[0]
        trading_cases = [
            _at(day, 9, 20),    # CALL_AUCTION
            _at(day, 10, 0),    # MORNING
            _at(day, 13, 30),   # AFTERNOON
            _at(day, 14, 58),   # CALL_AUCTION
        ]
        non_trading_cases = [
            _at(day, 8, 30),    # CLOSED
            _at(day, 9, 5),     # PRE_MARKET
            _at(day, 12, 0),    # LUNCH_BREAK
            _at(day, 15, 10),   # POST_MARKET
            _at(day, 16, 0),    # CLOSED
        ]
        for now in trading_cases:
            assert state_machine.is_trading_time(now) is True, f"expected trading at {now.time()}"
        for now in non_trading_cases:
            assert state_machine.is_trading_time(now) is False, f"expected non-trading at {now.time()}"

    def test_next_session_full_day_cycle_on_trading_day(self, state_machine: MarketSessionStateMachine) -> None:
        """Validate the full intraday session transition chain on a trading day."""
        day = TRADING_DAYS[0]
        # CLOSED (before 09:00) -> PRE_MARKET
        assert state_machine.next_session(_at(day, 8, 30)) == MarketSession.PRE_MARKET
        # PRE_MARKET -> CALL_AUCTION (opening)
        assert state_machine.next_session(_at(day, 9, 5)) == MarketSession.CALL_AUCTION
        # CALL_AUCTION (opening, before 12:00) -> MORNING
        assert state_machine.next_session(_at(day, 9, 20)) == MarketSession.MORNING
        # MORNING -> LUNCH_BREAK
        assert state_machine.next_session(_at(day, 10, 0)) == MarketSession.LUNCH_BREAK
        # LUNCH_BREAK -> AFTERNOON
        assert state_machine.next_session(_at(day, 12, 0)) == MarketSession.AFTERNOON
        # AFTERNOON -> CALL_AUCTION (closing)
        assert state_machine.next_session(_at(day, 13, 30)) == MarketSession.CALL_AUCTION
        # CALL_AUCTION (closing, after 12:00) -> POST_MARKET
        assert state_machine.next_session(_at(day, 14, 58)) == MarketSession.POST_MARKET
        # POST_MARKET -> CLOSED
        assert state_machine.next_session(_at(day, 15, 10)) == MarketSession.CLOSED
        # CLOSED (after 15:30) -> PRE_MARKET (next trading day)
        assert state_machine.next_session(_at(day, 16, 0)) == MarketSession.PRE_MARKET

    def test_cross_day_transition_from_post_market_to_next_day_pre_market(
        self, state_machine: MarketSessionStateMachine
    ) -> None:
        """POST_MARKET of day N transitions to CLOSED, then to PRE_MARKET of day N+1."""
        end_of_day_1 = _at(TRADING_DAYS[0], 15, 29)
        assert state_machine.current_session(end_of_day_1) == MarketSession.POST_MARKET
        # Late evening same day: CLOSED
        late_evening = _at(TRADING_DAYS[0], 23, 0)
        assert state_machine.current_session(late_evening) == MarketSession.CLOSED
        # Next morning (trading day 2): PRE_MARKET
        next_morning = _at(TRADING_DAYS[1], 9, 5)
        assert state_machine.current_session(next_morning) == MarketSession.PRE_MARKET

    def test_naive_datetime_treated_as_market_time(self, state_machine: MarketSessionStateMachine) -> None:
        """Naive datetime (no tzinfo) is interpreted as Asia/Shanghai."""
        day = TRADING_DAYS[0]
        naive = datetime(2026, 7, 13, 10, 0, 0)  # no tzinfo
        assert state_machine.current_session(naive) == MarketSession.MORNING

    def test_calendar_accepts_callable(self) -> None:
        """Callable calendar form is supported and used for trading-day check."""
        closed_dates = {"2026-07-15"}  # holiday in the middle
        calendar_set = set(TRADING_DAYS) - closed_dates

        def is_open(target) -> bool:
            return target.isoformat() in calendar_set

        machine = MarketSessionStateMachine(trading_calendar=is_open)
        assert machine.is_trading_day(datetime(2026, 7, 13).date()) is True
        assert machine.is_trading_day(datetime(2026, 7, 15).date()) is False
        # 2026-07-15 is now a holiday -> CLOSED even at 10:00
        assert machine.current_session(_at("2026-07-15", 10, 0)) == MarketSession.CLOSED

    def test_default_calendar_falls_back_to_weekday(self) -> None:
        """Without a calendar, the machine uses the Mon-Fri weekday fallback."""
        machine = MarketSessionStateMachine()
        # 2026-07-18 is Saturday
        assert machine.is_trading_day(datetime(2026, 7, 18).date()) is False
        # 2026-07-13 is Monday
        assert machine.is_trading_day(datetime(2026, 7, 13).date()) is True


# ---------------------------------------------------------------------------
# Task 6.6 (5-day simulation): walk through 5 consecutive trading days
# ---------------------------------------------------------------------------


class TestFiveTradingDaySimulation:
    """Simulate 5 consecutive trading days of session transitions."""

    def test_full_five_day_session_cycle(self) -> None:
        """Walk every session boundary across 5 trading days."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        expected_session_sequence = [
            MarketSession.CLOSED,
            MarketSession.PRE_MARKET,
            MarketSession.CALL_AUCTION,
            MarketSession.MORNING,
            MarketSession.LUNCH_BREAK,
            MarketSession.AFTERNOON,
            MarketSession.CALL_AUCTION,
            MarketSession.POST_MARKET,
            MarketSession.CLOSED,
        ]
        for day in TRADING_DAYS:
            daily_sessions = [
                machine.current_session(_at(day, 8, 30)),
                machine.current_session(_at(day, 9, 5)),
                machine.current_session(_at(day, 9, 20)),
                machine.current_session(_at(day, 10, 0)),
                machine.current_session(_at(day, 12, 0)),
                machine.current_session(_at(day, 13, 30)),
                machine.current_session(_at(day, 14, 58)),
                machine.current_session(_at(day, 15, 10)),
                machine.current_session(_at(day, 16, 0)),
            ]
            assert daily_sessions == expected_session_sequence, (
                f"day {day} session sequence mismatch: "
                f"{[s.value for s in daily_sessions]}"
            )

    def test_weekend_after_five_trading_days_is_closed(self) -> None:
        """Weekend immediately after the 5-day window is fully CLOSED."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        for weekend_day in WEEKEND_DAYS:
            for hour in (8, 10, 12, 14, 16):
                assert machine.current_session(_at(weekend_day, hour, 0)) == MarketSession.CLOSED


# ---------------------------------------------------------------------------
# Task 6.2: QuoteFreshnessChecker
# ---------------------------------------------------------------------------


class TestQuoteFreshnessChecker:
    """Cover fresh / stale / missing / wrong-date / lunch-break / closed cases."""

    @pytest.fixture
    def checker(self) -> QuoteFreshnessChecker:
        return QuoteFreshnessChecker()

    def test_fresh_quote_in_morning(self, checker: QuoteFreshnessChecker) -> None:
        """Sub-threshold staleness in MORNING returns is_fresh=True."""
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        exchange_ts = now - timedelta(seconds=5)
        result = checker.check(
            exchange_timestamp=exchange_ts,
            as_of_date=day,
            session=MarketSession.MORNING,
            now=now,
        )
        assert result.is_fresh is True
        assert result.staleness_seconds == pytest.approx(5.0, abs=0.5)
        assert result.degraded is False

    def test_stale_quote_in_morning(self, checker: QuoteFreshnessChecker) -> None:
        """Staleness above the 15s MORNING threshold returns is_fresh=False."""
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        exchange_ts = now - timedelta(seconds=30)
        result = checker.check(
            exchange_timestamp=exchange_ts,
            as_of_date=day,
            session=MarketSession.MORNING,
            now=now,
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert "stale_quote" in result.reason

    def test_missing_exchange_timestamp_required(self) -> None:
        """Missing timestamp with required=True -> degraded False is_fresh."""
        checker = QuoteFreshnessChecker(policy=FreshnessPolicy(exchange_timestamp_required=True))
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=None,
            as_of_date=day,
            session=MarketSession.MORNING,
            now=_at(day, 10, 0),
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert result.reason == "exchange_timestamp_missing"

    def test_missing_exchange_timestamp_unknown_when_not_required(self) -> None:
        """Missing timestamp with required=False -> degraded True, reason=unknown."""
        checker = QuoteFreshnessChecker(policy=FreshnessPolicy(exchange_timestamp_required=False))
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=None,
            as_of_date=day,
            session=MarketSession.MORNING,
            now=_at(day, 10, 0),
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert result.reason == "exchange_timestamp_unknown"

    def test_wrong_as_of_date_returns_degraded(self, checker: QuoteFreshnessChecker) -> None:
        """as_of_date mismatch is detected before timestamp checks."""
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=_at(day, 9, 55),
            as_of_date="2026-07-10",  # previous trade date
            session=MarketSession.MORNING,
            now=_at(day, 10, 0),
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert result.reason.startswith("as_of_date_mismatch")

    def test_missing_as_of_date(self, checker: QuoteFreshnessChecker) -> None:
        """Missing as_of_date triggers degraded result."""
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=_at(day, 9, 55),
            as_of_date=None,
            session=MarketSession.MORNING,
            now=_at(day, 10, 0),
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert result.reason == "as_of_date_missing"

    def test_compact_timestamp_format_parsed(self, checker: QuoteFreshnessChecker) -> None:
        """Compact YYYYMMDDHHMMSS timestamps are parsed correctly."""
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        # 10:00:00 - 09:59:55 = 5s, within the 15s MORNING threshold -> fresh
        result = checker.check(
            exchange_timestamp="20260713095955",
            as_of_date=day,
            session=MarketSession.MORNING,
            now=now,
        )
        assert result.is_fresh is True
        assert result.staleness_seconds == pytest.approx(5.0, abs=0.5)

    def test_lunch_break_tolerates_old_snapshot(self, checker: QuoteFreshnessChecker) -> None:
        """LUNCH_BREAK uses a much larger threshold (1h by default)."""
        day = TRADING_DAYS[0]
        now = _at(day, 12, 30, 0)
        # 12:30:00 - 11:31:00 = 59min = 3540s, within the 3600s LUNCH_BREAK threshold
        # but far beyond the 15s MORNING threshold -- this is the tolerance behavior.
        exchange_ts = _at(day, 11, 31, 0)
        result = checker.check(
            exchange_timestamp=exchange_ts,
            as_of_date=day,
            session=MarketSession.LUNCH_BREAK,
            now=now,
        )
        assert result.is_fresh is True
        assert result.staleness_seconds > 1800

    def test_call_auction_uses_tolerant_threshold(self, checker: QuoteFreshnessChecker) -> None:
        """CALL_AUCTION allows up to 60s by default."""
        day = TRADING_DAYS[0]
        now = _at(day, 9, 20, 0)
        # 45s stale - within 60s CALL_AUCTION threshold
        fresh_result = checker.check(
            exchange_timestamp=now - timedelta(seconds=45),
            as_of_date=day,
            session=MarketSession.CALL_AUCTION,
            now=now,
        )
        assert fresh_result.is_fresh is True
        # 90s stale - exceeds 60s threshold
        stale_result = checker.check(
            exchange_timestamp=now - timedelta(seconds=90),
            as_of_date=day,
            session=MarketSession.CALL_AUCTION,
            now=now,
        )
        assert stale_result.is_fresh is False
        assert stale_result.degraded is True

    def test_closed_session_skips_freshness(self, checker: QuoteFreshnessChecker) -> None:
        """CLOSED session returns is_fresh=True regardless of timestamp."""
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=None,
            as_of_date=day,
            session=MarketSession.CLOSED,
            now=_at(day, 16, 0),
        )
        assert result.is_fresh is True
        assert result.reason == "closed_session_no_freshness_required"

    def test_pre_market_skips_realtime_check(self, checker: QuoteFreshnessChecker) -> None:
        """PRE_MARKET returns is_fresh=True without timestamp checks."""
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp=None,
            as_of_date=day,
            session=MarketSession.PRE_MARKET,
            now=_at(day, 9, 5),
        )
        assert result.is_fresh is True
        assert result.reason == "pre_market_no_realtime_required"

    def test_unparseable_timestamp_returns_degraded(self, checker: QuoteFreshnessChecker) -> None:
        """Unparseable timestamp string returns degraded result."""
        day = TRADING_DAYS[0]
        result = checker.check(
            exchange_timestamp="not-a-timestamp",
            as_of_date=day,
            session=MarketSession.MORNING,
            now=_at(day, 10, 0),
        )
        assert result.is_fresh is False
        assert result.degraded is True
        assert result.reason == "exchange_timestamp_unparseable"

    def test_future_exchange_timestamp_treated_as_zero_staleness(self, checker: QuoteFreshnessChecker) -> None:
        """Exchange timestamp slightly ahead of now is clamped to 0 staleness."""
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        future_ts = now + timedelta(seconds=3)
        result = checker.check(
            exchange_timestamp=future_ts,
            as_of_date=day,
            session=MarketSession.MORNING,
            now=now,
        )
        assert result.is_fresh is True
        assert result.staleness_seconds == 0.0

    def test_custom_policy_overrides_threshold(self) -> None:
        """Custom FreshnessPolicy max_stale_seconds overrides defaults."""
        custom_policy = FreshnessPolicy(
            max_stale_seconds={
                MarketSession.MORNING: 5,  # tighten to 5s
            },
        )
        checker = QuoteFreshnessChecker(policy=custom_policy)
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        # 10s stale: exceeds custom 5s threshold
        result = checker.check(
            exchange_timestamp=now - timedelta(seconds=10),
            as_of_date=day,
            session=MarketSession.MORNING,
            now=now,
        )
        assert result.is_fresh is False
        assert result.degraded is True


# ---------------------------------------------------------------------------
# Task 6.3: MonitorScheduler
# ---------------------------------------------------------------------------


class TestMonitorScheduler:
    """Validate per-session intervals, should_run, backoff, and persistence."""

    def test_next_interval_returns_default_per_session(self) -> None:
        """next_interval returns DEFAULT_SESSION_INTERVALS for each session."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        day = TRADING_DAYS[0]
        cases = [
            (_at(day, 10, 0), DEFAULT_SESSION_INTERVALS[MarketSession.MORNING]),
            (_at(day, 12, 30), DEFAULT_SESSION_INTERVALS[MarketSession.LUNCH_BREAK]),
            (_at(day, 13, 30), DEFAULT_SESSION_INTERVALS[MarketSession.AFTERNOON]),
            (_at(day, 9, 5), DEFAULT_SESSION_INTERVALS[MarketSession.PRE_MARKET]),
            (_at(day, 15, 10), DEFAULT_SESSION_INTERVALS[MarketSession.POST_MARKET]),
            (_at(day, 16, 0), DEFAULT_SESSION_INTERVALS[MarketSession.CLOSED]),
            (_at(day, 9, 20), DEFAULT_SESSION_INTERVALS[MarketSession.CALL_AUCTION]),
        ]
        for now, expected_interval in cases:
            assert scheduler.next_interval(now) == expected_interval, (
                f"at {now.time()} expected {expected_interval}s"
            )

    def test_should_run_true_when_no_last_run(self) -> None:
        """should_run returns True when there is no prior run."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        assert scheduler.should_run(_at(TRADING_DAYS[0], 10, 0)) is True

    def test_should_run_false_within_interval(self) -> None:
        """should_run returns False when elapsed < session interval."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        day = TRADING_DAYS[0]
        last_run = _at(day, 10, 0, 0)
        # MORNING interval is 300s; only 60s elapsed
        now = _at(day, 10, 1, 0)
        assert scheduler.should_run(now, last_run=last_run) is False

    def test_should_run_true_after_interval_elapsed(self) -> None:
        """should_run returns True when elapsed >= session interval."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        day = TRADING_DAYS[0]
        last_run = _at(day, 10, 0, 0)
        # 6 minutes later > 300s MORNING interval
        now = _at(day, 10, 6, 0)
        assert scheduler.should_run(now, last_run=last_run) is True

    def test_record_failure_increases_backoff_bounded(self) -> None:
        """record_failure increases backoff exponentially, capped at 1800s."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(
            state_machine=machine,
            base_backoff_seconds=60,
            max_backoff_seconds=1800,
        )
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        scheduler.record_failure(now=now)
        # 1 failure: 60 * 2^0 = 60s, but session interval (300) > 60 -> max(60, 300) = 300
        assert scheduler.next_interval(now) == 300
        scheduler.record_failure(now=now)
        # 2 failures: 60 * 2^1 = 120s, max(120, 300) = 300
        assert scheduler.next_interval(now) == 300
        scheduler.record_failure(now=now)
        scheduler.record_failure(now=now)
        scheduler.record_failure(now=now)
        # 5 failures: 60 * 2^4 = 960s, max(960, 300) = 960
        assert scheduler.next_interval(now) == 960
        # Trigger enough failures to hit the cap
        for _ in range(10):
            scheduler.record_failure(now=now)
        # 15+ failures: 60 * 2^14 = huge, capped at 1800
        assert scheduler.next_interval(now) == 1800

    def test_record_success_resets_backoff(self) -> None:
        """record_success clears failure_count and backoff."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        for _ in range(3):
            scheduler.record_failure(now=now)
        assert scheduler.failure_count == 3
        scheduler.record_success(now=now)
        assert scheduler.failure_count == 0
        # Back to MORNING interval
        assert scheduler.next_interval(now) == DEFAULT_SESSION_INTERVALS[MarketSession.MORNING]

    def test_persistence_restores_state_across_restart(self, tmp_path) -> None:
        """Restart restores last_run and failure_count from disk."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        state_path = tmp_path / "scheduler_state.json"
        day = TRADING_DAYS[0]
        now = _at(day, 10, 0, 0)
        scheduler = MonitorScheduler(
            state_machine=machine,
            state_path=state_path,
        )
        for _ in range(2):
            scheduler.record_failure(now=now)
        # Re-instantiate with same state_path: should restore failure_count
        scheduler2 = MonitorScheduler(
            state_machine=machine,
            state_path=state_path,
        )
        assert scheduler2.failure_count == 2
        assert scheduler2.last_run is not None

    def test_cross_day_forces_run(self, tmp_path) -> None:
        """should_run returns True when trade date changed even if interval not elapsed."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(state_machine=machine)
        # Record a run on day 1 at 10:00
        scheduler.record_success(now=_at(TRADING_DAYS[0], 10, 0))
        # Next day morning, only a few minutes after midnight
        next_day_morning = _at(TRADING_DAYS[1], 8, 30)
        assert scheduler.should_run(next_day_morning) is True

    def test_custom_schedule_overrides_intervals(self) -> None:
        """Custom MonitorSchedule.session_intervals overrides defaults."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        custom = MonitorSchedule(
            session_intervals={MarketSession.MORNING: 120},
        )
        scheduler = MonitorScheduler(state_machine=machine, schedule=custom)
        # MORNING uses custom 120s; LUNCH_BREAK falls back to default 1800s
        assert scheduler.next_interval(_at(TRADING_DAYS[0], 10, 0)) == 120
        assert scheduler.next_interval(_at(TRADING_DAYS[0], 12, 30)) == 1800


# ---------------------------------------------------------------------------
# Task 6.4: PriceAnomalyDetector
# ---------------------------------------------------------------------------


class TestPriceAnomalyDetector:
    """Validate hypothesis creation, threshold boundaries, and session gating."""

    @pytest.fixture
    def detector(self) -> PriceAnomalyDetector:
        return PriceAnomalyDetector()

    def test_surge_creates_hypothesis(self, detector: PriceAnomalyDetector) -> None:
        """pct_chg >= 7% triggers a surge RetrievalHypothesis."""
        result = detector.detect(
            symbol="600584.SH",
            pct_chg=8.5,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is not None
        assert result.anomaly_type == "surge"
        assert result.symbol == "600584.SH"
        assert result.pct_chg == 8.5
        assert "涨幅" in result.hypothesis_text

    def test_plunge_creates_hypothesis(self, detector: PriceAnomalyDetector) -> None:
        """pct_chg <= -7% triggers a plunge RetrievalHypothesis."""
        result = detector.detect(
            symbol="000021.SZ",
            pct_chg=-9.0,
            session=MarketSession.AFTERNOON,
            now=_at(TRADING_DAYS[0], 13, 30),
        )
        assert result is not None
        assert result.anomaly_type == "plunge"
        assert result.pct_chg == -9.0
        assert "跌幅" in result.hypothesis_text

    def test_below_threshold_returns_none(self, detector: PriceAnomalyDetector) -> None:
        """pct_chg in (-7, 7) returns None."""
        for change in (6.9, -6.9, 0.0, 5.0, -5.0):
            result = detector.detect(
                symbol="600584.SH",
                pct_chg=change,
                session=MarketSession.MORNING,
                now=_at(TRADING_DAYS[0], 10, 0),
            )
            assert result is None, f"pct_chg={change} should not trigger"

    def test_threshold_boundary_inclusive(self, detector: PriceAnomalyDetector) -> None:
        """Exactly +7.0% and -7.0% trigger hypotheses (inclusive boundary)."""
        surge = detector.detect(
            symbol="A.SH",
            pct_chg=7.0,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert surge is not None and surge.anomaly_type == "surge"
        plunge = detector.detect(
            symbol="B.SH",
            pct_chg=-7.0,
            session=MarketSession.AFTERNOON,
            now=_at(TRADING_DAYS[0], 13, 30),
        )
        assert plunge is not None and plunge.anomaly_type == "plunge"

    def test_non_trading_session_returns_none(self, detector: PriceAnomalyDetector) -> None:
        """Detection is suppressed outside of trading sessions."""
        day = TRADING_DAYS[0]
        for session in (MarketSession.CLOSED, MarketSession.PRE_MARKET,
                        MarketSession.LUNCH_BREAK, MarketSession.POST_MARKET):
            result = detector.detect(
                symbol="600584.SH",
                pct_chg=10.0,
                session=session,
                now=_at(day, 12, 0),
            )
            assert result is None, f"session={session.value} should not trigger"

    def test_call_auction_triggers_hypothesis(self, detector: PriceAnomalyDetector) -> None:
        """CALL_AUCTION is a trading session and triggers hypotheses."""
        result = detector.detect(
            symbol="600584.SH",
            pct_chg=8.0,
            session=MarketSession.CALL_AUCTION,
            now=_at(TRADING_DAYS[0], 9, 20),
        )
        assert result is not None
        assert result.anomaly_type == "surge"

    def test_invalid_pct_chg_returns_none(self, detector: PriceAnomalyDetector) -> None:
        """Non-numeric pct_chg returns None instead of raising."""
        result = detector.detect(
            symbol="600584.SH",
            pct_chg="not-a-number",  # type: ignore[arg-type]
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is None

    def test_empty_symbol_returns_none(self, detector: PriceAnomalyDetector) -> None:
        """Empty symbol returns None."""
        result = detector.detect(
            symbol="",
            pct_chg=10.0,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is None

    def test_to_retrieval_hypothesis_blocked_from_evidence_slots(self, detector: PriceAnomalyDetector) -> None:
        """Generated RetrievalHypothesis cannot enter evidence slots or posterior."""
        from core.research_graph.retrieval_hypothesis import RetrievalHypothesis

        result = detector.detect(
            symbol="600584.SH",
            pct_chg=8.5,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is not None
        hypothesis = result.to_retrieval_hypothesis(hypothesis_id="hyp_test_1")
        assert isinstance(hypothesis, RetrievalHypothesis)
        # Per project contract: hypotheses never enter evidence slots / posterior
        assert hypothesis.eligible_for_evidence_slot is False
        assert hypothesis.eligible_for_posterior is False
        assert hypothesis.evidence_kind == "retrieval_hypothesis"
        assert hypothesis.generated_by == "price_anomaly_detector"

    def test_does_not_directly_upgrade_claim_priority(self, detector: PriceAnomalyDetector) -> None:
        """AnomalyResult carries no Claim priority field (it's a hypothesis only)."""
        result = detector.detect(
            symbol="600584.SH",
            pct_chg=10.0,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is not None
        # The result must NOT carry any priority/trading-relevance upgrade field
        forbidden_fields = {"priority", "trading_relevance", "evidence_confidence"}
        assert not (forbidden_fields & set(result.__dict__.keys()))

    def test_custom_thresholds_override_defaults(self) -> None:
        """Constructor accepts custom surge/plunge thresholds."""
        detector = PriceAnomalyDetector(
            surge_threshold=5.0,
            plunge_threshold=-5.0,
        )
        result = detector.detect(
            symbol="X.SH",
            pct_chg=5.5,
            session=MarketSession.MORNING,
            now=_at(TRADING_DAYS[0], 10, 0),
        )
        assert result is not None
        assert result.anomaly_type == "surge"


# ---------------------------------------------------------------------------
# Task 6.5: PushDedupStore
# ---------------------------------------------------------------------------


class TestPushDedupStore:
    """Validate dedup, failure retry, persistence across restart, and edge cases."""

    @pytest.fixture
    def store(self, tmp_path) -> PushDedupStore:
        return PushDedupStore(db_path=tmp_path / "push_dedup.db")

    def test_should_push_true_initially(self, store: PushDedupStore) -> None:
        """First-time push for a meaningful state change is allowed."""
        assert store.should_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        ) is True

    def test_record_push_blocks_subsequent_duplicates(self, store: PushDedupStore) -> None:
        """After record_push, the same key is blocked from re-push."""
        kwargs = dict(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        store.record_push(**kwargs)
        assert store.should_push(**kwargs) is False

    def test_different_entity_id_pushes_independently(self, store: PushDedupStore) -> None:
        """Different entity_id produces a different dedup key."""
        store.record_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        assert store.should_push(
            date="2026-07-13",
            entity_id="claim_2",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        ) is True

    def test_different_policy_version_pushes_again(self, store: PushDedupStore) -> None:
        """Changing policy_version creates a new dedup bucket."""
        store.record_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        # Same date/entity/state but new policy_version: should push
        assert store.should_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v2",
        ) is True

    def test_different_date_pushes_again(self, store: PushDedupStore) -> None:
        """Same key on a different trade date is allowed (per-day dedup)."""
        store.record_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        assert store.should_push(
            date="2026-07-14",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        ) is True

    def test_non_meaningful_state_change_blocked(self, store: PushDedupStore) -> None:
        """Non-meaningful state changes (UNCHANGED / NO_CHANGE) are never pushed."""
        for state_change in ("UNCHANGED", "NO_CHANGE", "NONE", "", "null"):
            assert store.should_push(
                date="2026-07-13",
                entity_id="claim_1",
                state_change=state_change,
                policy_version="push_policy_v1",
            ) is False

    def test_record_failure_creates_retryable_entry(self, store: PushDedupStore) -> None:
        """record_failure stores the failure and list_failures returns it."""
        store.record_failure(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
            error="network_timeout",
        )
        failures = store.list_failures(limit=10)
        assert len(failures) == 1
        assert failures[0]["entity_id"] == "claim_1"
        assert failures[0]["error"] == "network_timeout"
        assert failures[0]["attempt_count"] == 1

    def test_repeated_failure_increments_attempt_count(self, store: PushDedupStore) -> None:
        """Repeated record_failure for the same key increments attempt_count."""
        kwargs = dict(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
            error="network_timeout",
        )
        store.record_failure(**kwargs)
        store.record_failure(**kwargs)
        store.record_failure(**kwargs)
        failures = store.list_failures(limit=10)
        assert len(failures) == 1
        assert failures[0]["attempt_count"] == 3

    def test_record_push_clears_failure_entry(self, store: PushDedupStore) -> None:
        """A successful record_push clears the corresponding failure entry."""
        kwargs = dict(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        store.record_failure(**{**kwargs, "error": "network_timeout"})
        store.record_push(**kwargs)
        assert store.list_failures(limit=10) == []
        # And the push itself is now deduped
        assert store.should_push(**kwargs) is False

    def test_persistence_survives_restart(self, tmp_path) -> None:
        """Restart with the same db_path preserves pushed records and failures."""
        db_path = tmp_path / "push_dedup.db"
        store1 = PushDedupStore(db_path=db_path)
        store1.record_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        store1.record_failure(
            date="2026-07-13",
            entity_id="claim_2",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
            error="timeout",
        )
        # New instance pointing to the same DB
        store2 = PushDedupStore(db_path=db_path)
        # Pushed record is still deduped
        assert store2.should_push(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        ) is False
        # Failed entry is still listed
        failures = store2.list_failures(limit=10)
        assert len(failures) == 1
        assert failures[0]["entity_id"] == "claim_2"

    def test_purge_for_date_resets_dedup(self, store: PushDedupStore) -> None:
        """purge_for_date clears all records for the given date."""
        kwargs = dict(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
        )
        store.record_push(**kwargs)
        deleted = store.purge_for_date("2026-07-13")
        assert deleted >= 1
        assert store.should_push(**kwargs) is True

    def test_clear_failures_after_successful_retry(self, store: PushDedupStore) -> None:
        """clear_failures removes a specific failure entry by dedup_key."""
        key = store.record_failure(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
            error="timeout",
        )
        assert len(store.list_failures(limit=10)) == 1
        store.clear_failures(key)
        assert store.list_failures(limit=10) == []

    def test_payload_roundtrip_through_failure(self, store: PushDedupStore) -> None:
        """Payload attached to a failure is recoverable via list_failures."""
        payload = {"theme_id": "T1", "snapshot": {"price": 12.34}}
        store.record_failure(
            date="2026-07-13",
            entity_id="claim_1",
            state_change="READY_TO_WATCH",
            policy_version="push_policy_v1",
            error="timeout",
            payload=payload,
        )
        failures = store.list_failures(limit=10)
        assert failures[0]["payload"] == payload


# ---------------------------------------------------------------------------
# Cross-cutting: 5-day simulation with all subsystems together
# ---------------------------------------------------------------------------


class TestFiveDayIntegratedSimulation:
    """Exercise all phase-6 subsystems across 5 trading days end-to-end."""

    def test_full_intraday_pipeline_each_day(self, tmp_path) -> None:
        """For each of 5 trading days, walk through sessions and exercise
        freshness, scheduler, anomaly detector, and push dedup."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(
            state_machine=machine,
            state_path=tmp_path / "scheduler.json",
        )
        freshness = QuoteFreshnessChecker()
        detector = PriceAnomalyDetector()
        dedup = PushDedupStore(db_path=tmp_path / "push_dedup.db")

        for day in TRADING_DAYS:
            # Morning: fresh quote + no anomaly + push allowed
            morning = _at(day, 10, 0, 0)
            assert machine.current_session(morning) == MarketSession.MORNING
            assert scheduler.should_run(morning) is True
            fresh_result = freshness.check(
                exchange_timestamp=morning - timedelta(seconds=5),
                as_of_date=day,
                session=MarketSession.MORNING,
                now=morning,
            )
            assert fresh_result.is_fresh is True
            scheduler.record_success(now=morning)
            # No anomaly below threshold
            assert detector.detect(
                symbol="600584.SH", pct_chg=3.0,
                session=MarketSession.MORNING, now=morning,
            ) is None
            # Push is allowed first time per day
            push_kwargs = dict(
                date=day, entity_id="claim_morning",
                state_change="READY_TO_WATCH", policy_version="push_policy_v1",
            )
            assert dedup.should_push(**push_kwargs) is True
            dedup.record_push(**push_kwargs)
            # Same push within same day is deduped
            assert dedup.should_push(**push_kwargs) is False

            # Midday anomaly: surge triggers hypothesis
            surge_time = _at(day, 11, 0, 0)
            anomaly = detector.detect(
                symbol="600584.SH", pct_chg=8.0,
                session=MarketSession.MORNING, now=surge_time,
            )
            assert anomaly is not None and anomaly.anomaly_type == "surge"

            # Lunch break: stale quote tolerated
            lunch = _at(day, 12, 30, 0)
            assert machine.current_session(lunch) == MarketSession.LUNCH_BREAK
            lunch_freshness = freshness.check(
                exchange_timestamp=_at(day, 11, 31, 0),  # 3540s stale, within 3600s threshold
                as_of_date=day,
                session=MarketSession.LUNCH_BREAK,
                now=lunch,
            )
            assert lunch_freshness.is_fresh is True

            # Afternoon: scheduler interval respected
            afternoon = _at(day, 13, 30, 0)
            assert machine.current_session(afternoon) == MarketSession.AFTERNOON
            assert scheduler.next_interval(afternoon) == DEFAULT_SESSION_INTERVALS[MarketSession.AFTERNOON]

            # Closing call auction: triggers hypothesis at boundary
            closing = _at(day, 14, 58, 0)
            assert machine.current_session(closing) == MarketSession.CALL_AUCTION
            closing_anomaly = detector.detect(
                symbol="000021.SZ", pct_chg=-8.5,
                session=MarketSession.CALL_AUCTION, now=closing,
            )
            assert closing_anomaly is not None
            assert closing_anomaly.anomaly_type == "plunge"

            # Post market: scheduler interval returns 600s
            post = _at(day, 15, 10, 0)
            assert machine.current_session(post) == MarketSession.POST_MARKET
            assert scheduler.next_interval(post) == DEFAULT_SESSION_INTERVALS[MarketSession.POST_MARKET]

            # End of day: CLOSED
            closed = _at(day, 16, 0, 0)
            assert machine.current_session(closed) == MarketSession.CLOSED
            assert scheduler.next_interval(closed) == DEFAULT_SESSION_INTERVALS[MarketSession.CLOSED]

    def test_failure_backoff_recovery_within_day(self, tmp_path) -> None:
        """Simulate 3 consecutive failures followed by recovery on the same day."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        scheduler = MonitorScheduler(
            state_machine=machine,
            state_path=tmp_path / "scheduler_backoff.json",
            base_backoff_seconds=60,
            max_backoff_seconds=1800,
        )
        day = TRADING_DAYS[0]
        # Three consecutive failures
        for _ in range(3):
            scheduler.record_failure(now=_at(day, 10, 0, 0))
        assert scheduler.failure_count == 3
        # Backoff is bounded and at least the MORNING interval (300s)
        backoff = scheduler.next_interval(_at(day, 10, 0, 0))
        assert 300 <= backoff <= 1800
        # Recovery resets backoff
        scheduler.record_success(now=_at(day, 10, 30, 0))
        assert scheduler.failure_count == 0
        assert scheduler.next_interval(_at(day, 10, 30, 0)) == 300

    def test_push_dedup_per_day_reset_across_five_days(self, tmp_path) -> None:
        """Per-day dedup means the same push is allowed once per day across 5 days."""
        store = PushDedupStore(db_path=tmp_path / "push_dedup.db")
        for day in TRADING_DAYS:
            kwargs = dict(
                date=day,
                entity_id="claim_steady",
                state_change="READY_TO_WATCH",
                policy_version="push_policy_v1",
            )
            # First push of the day is allowed
            assert store.should_push(**kwargs) is True
            store.record_push(**kwargs)
            # Same-day duplicate is blocked
            assert store.should_push(**kwargs) is False
        # After 5 days, dedup DB has 5 distinct records (one per day)
        # Verify by attempting purge on each date
        for day in TRADING_DAYS:
            deleted = store.purge_for_date(day)
            assert deleted >= 1, f"expected at least 1 record purged for {day}"

    def test_persisted_state_recovers_after_crash(self, tmp_path) -> None:
        """Simulate a crash mid-day; the restarted scheduler resumes state."""
        machine = MarketSessionStateMachine(trading_calendar=_trading_calendar())
        state_path = tmp_path / "crashed_scheduler.json"
        day = TRADING_DAYS[0]
        scheduler = MonitorScheduler(
            state_machine=machine,
            state_path=state_path,
        )
        # Record a run in the morning
        scheduler.record_success(now=_at(day, 10, 0, 0))
        # Simulate crash: discard the in-memory object
        del scheduler
        # Re-instantiate with the same state path: should restore last_run
        scheduler2 = MonitorScheduler(
            state_machine=machine,
            state_path=state_path,
        )
        assert scheduler2.last_run is not None
        assert scheduler2.failure_count == 0
        # 4 minutes later (240s < 300s MORNING interval) -> should not run yet
        four_min_later = _at(day, 10, 4, 0)
        assert scheduler2.should_run(four_min_later) is False
        # 6 minutes later (360s >= 300s) -> interval elapsed, should run
        six_min_later = _at(day, 10, 6, 0)
        assert scheduler2.should_run(six_min_later) is True
