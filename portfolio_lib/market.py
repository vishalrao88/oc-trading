"""Market status, timezone-aware times, and trading-day calculations.

This module is the SINGLE SOURCE OF TRUTH for "what time is it" and "is the
market open" across the portfolio dashboard. All other modules call into this
one rather than computing times independently. This is what prevents the
"wrong date sometimes" bug that came from multiple modules computing 'today'
in different timezones.

Design principle: every function that depends on the current time accepts a
`current` parameter (defaulting to `now()`). This makes the module trivially
testable with frozen times — no monkey-patching, no freezegun, just pass in
the time you want to test.

US equity market sessions (NYSE):
    Pre-market:    04:00 - 09:30 ET
    Regular:       09:30 - 16:00 ET
    Post-market:   16:00 - 20:00 ET
    Closed:        20:00 - 04:00 ET, weekends, holidays
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Denver")
UTC = ZoneInfo("UTC")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PRE_MARKET_OPEN = time(4, 0)
POST_MARKET_CLOSE = time(20, 0)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Session = Literal["pre", "regular", "post", "closed"]


@dataclass(frozen=True)
class MarketStatus:
    """Snapshot of market state at a given moment. All datetimes are
    timezone-aware (ET)."""

    session: Session
    is_regular_hours: bool
    current_time_et: datetime
    next_open: datetime   # next regular-hours open (after now)
    next_close: datetime  # corresponding close for the next/current session

    @property
    def is_open(self) -> bool:
        """True only during regular trading hours."""
        return self.is_regular_hours


# ---------------------------------------------------------------------------
# Calendar (cached at module level so we only instantiate once)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _nyse():
    return mcal.get_calendar("NYSE")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def now() -> datetime:
    """Current time in ET. Single source of truth for 'what time is it'."""
    return datetime.now(tz=ET)


def _to_et(current: datetime) -> datetime:
    """Normalize an input datetime to ET. Rejects naive datetimes loudly —
    we never want to guess what timezone the caller meant."""
    if current.tzinfo is None:
        raise ValueError(
            f"market functions require timezone-aware datetimes, got naive: {current!r}"
        )
    return current.astimezone(ET)


def is_trading_day(d: date) -> bool:
    """True if d is a US equity market trading day."""
    schedule = _nyse().schedule(start_date=d, end_date=d)
    return not schedule.empty


def previous_trading_day(d: date) -> date:
    """Most recent trading day strictly before d."""
    # 10 days back covers any plausible holiday weekend (longest is ~4 days)
    schedule = _nyse().schedule(
        start_date=d - timedelta(days=10),
        end_date=d - timedelta(days=1),
    )
    if schedule.empty:
        raise ValueError(f"No trading day found in 10 days before {d}")
    return schedule.index[-1].date()


def most_recent_trading_day(current: datetime | None = None) -> date:
    """The most recent trading day where the regular session has started.

    If today is a trading day and we're at or past 09:30 ET, returns today.
    Otherwise looks backward. Used for finding the relevant prior close.
    """
    if current is None:
        current = now()
    current_et = _to_et(current)
    today = current_et.date()
    if is_trading_day(today) and current_et.time() >= REGULAR_OPEN:
        return today
    return previous_trading_day(today)


def market_status(current: datetime | None = None) -> MarketStatus:
    """Compute the market status for the given moment (defaults to now())."""
    if current is None:
        current = now()
    current_et = _to_et(current)
    today = current_et.date()

    if is_trading_day(today):
        t = current_et.time()
        if REGULAR_OPEN <= t < REGULAR_CLOSE:
            session: Session = "regular"
        elif PRE_MARKET_OPEN <= t < REGULAR_OPEN:
            session = "pre"
        elif REGULAR_CLOSE <= t < POST_MARKET_CLOSE:
            session = "post"
        else:
            session = "closed"
    else:
        session = "closed"

    next_open_dt, next_close_dt = _next_open_and_close(current_et)

    return MarketStatus(
        session=session,
        is_regular_hours=(session == "regular"),
        current_time_et=current_et,
        next_open=next_open_dt,
        next_close=next_close_dt,
    )


def _next_open_and_close(current_et: datetime) -> tuple[datetime, datetime]:
    """Compute the next regular-hours open and its corresponding close.

    Behavior:
      - If currently before today's open (or today isn't a trading day):
        returns today's open/close, or the next trading day's open/close.
      - If currently in regular hours: returns next session's open and
        today's close (the close of the session we're in).
      - If currently after today's close: returns next trading day's
        open/close.
    """
    today = current_et.date()
    # 14 days ahead handles any plausible holiday-extended weekend
    schedule = _nyse().schedule(
        start_date=today,
        end_date=today + timedelta(days=14),
    )
    if schedule.empty:
        raise ValueError(f"No trading days in 14 days from {today}")

    sessions = [
        (
            row.market_open.tz_convert(ET).to_pydatetime(),
            row.market_close.tz_convert(ET).to_pydatetime(),
        )
        for row in schedule.itertuples()
    ]

    for i, (open_dt, close_dt) in enumerate(sessions):
        if current_et < open_dt:
            # Haven't reached this session's open yet
            return open_dt, close_dt
        if open_dt <= current_et < close_dt:
            # Currently in this session — next open is next session, close is today's
            if i + 1 < len(sessions):
                next_open = sessions[i + 1][0]
            else:
                next_open = open_dt  # degenerate; shouldn't happen with 14-day window
            return next_open, close_dt
        # else: past this session's close, keep looking

    # Past close of every session in the window — fall back to last
    return sessions[-1]


def news_since_for(current: datetime | None = None) -> datetime:
    """Compute the news cutoff timestamp per SKILL.md rules.

    The cutoff means: "include news published at or after this time, because
    it could move the stock at the next session open."

    Rules:
      - Regular hours: today's 09:30 ET (capture today's session news)
      - Post-market on a trading day: today's 09:30 ET (same window)
      - Pre-market on a trading day: previous trading day's 16:00 ET close
      - Overnight on a trading day (00:00-04:00 or after 20:00): see below
      - Weekend or holiday: most recent trading day's 16:00 ET close
    """
    if current is None:
        current = now()
    current_et = _to_et(current)
    today = current_et.date()
    t = current_et.time()

    if not is_trading_day(today):
        # Weekend or holiday: most recent trading day's close
        most_recent = most_recent_trading_day(current_et)
        return datetime.combine(most_recent, REGULAR_CLOSE, tzinfo=ET)

    # Today is a trading day
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        # Regular hours
        return datetime.combine(today, REGULAR_OPEN, tzinfo=ET)
    if REGULAR_CLOSE <= t < POST_MARKET_CLOSE:
        # Post-market
        return datetime.combine(today, REGULAR_OPEN, tzinfo=ET)
    if PRE_MARKET_OPEN <= t < REGULAR_OPEN:
        # Pre-market — previous trading day's close
        prev = previous_trading_day(today)
        return datetime.combine(prev, REGULAR_CLOSE, tzinfo=ET)
    # Overnight: either before today's pre-market or after today's post-market
    if t < PRE_MARKET_OPEN:
        # Before today's pre-market opens — previous trading day's close
        prev = previous_trading_day(today)
        return datetime.combine(prev, REGULAR_CLOSE, tzinfo=ET)
    # After today's post-market close
    return datetime.combine(today, REGULAR_CLOSE, tzinfo=ET)
