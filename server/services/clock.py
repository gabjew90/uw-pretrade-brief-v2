"""Market clock / session service — the SINGLE source of truth for time.

Every stage asks the clock; nothing re-derives session/settlement state from
`datetime.now()`. This is deliberately the one place that knows: trading days,
holidays, half-days, the current session phase, and the **settlement cadence per data
type** (flow is live intraday; OI settles ~9:15am ET the NEXT trading day). v2's
forming/settled, UTC-vs-ET, weekend-handling and flow-as-"session" bugs were all a
missing clock.

`now` is injectable everywhere so tests are deterministic (v2's lesson).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# US full-closure holidays (NYSE/Nasdaq). Extend yearly. Half-days are not full
# closures — handled separately via _HALF_DAYS.
_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
# Early-close (1:00pm ET) sessions.
_HALF_DAYS = {
    "2026-11-27", "2026-12-24", "2027-11-26", "2027-12-23",
}

_OPEN = time(9, 30)
_CLOSE = time(16, 0)
_HALF_CLOSE = time(13, 0)
_PREMARKET = time(4, 0)
_AFTERHOURS_END = time(20, 0)
_OI_PUBLISH = time(9, 15)   # prior session's OI settles ~9:15am ET next trading day


class Phase(str, Enum):
    PREMARKET = "premarket"      # 04:00–09:30 ET on a trading day
    OPEN = "open"                # regular hours
    AFTERHOURS = "afterhours"    # close–20:00 ET
    CLOSED = "closed"            # overnight / weekend / holiday


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _HOLIDAYS


def is_half_day(d: date) -> bool:
    return d.isoformat() in _HALF_DAYS


def _close_time(d: date) -> time:
    return _HALF_CLOSE if is_half_day(d) else _CLOSE


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _et(now: datetime | None) -> datetime:
    return (now or datetime.now(tz=ET)).astimezone(ET)


def phase(now: datetime | None = None) -> Phase:
    n = _et(now)
    if not is_trading_day(n.date()):
        return Phase.CLOSED
    t = n.time()
    if t < _PREMARKET:
        return Phase.CLOSED
    if t < _OPEN:
        return Phase.PREMARKET
    if t <= _close_time(n.date()):
        return Phase.OPEN
    if t <= _AFTERHOURS_END:
        return Phase.AFTERHOURS
    return Phase.CLOSED


def market_is_open(now: datetime | None = None) -> bool:
    return phase(now) == Phase.OPEN


def session_date(now: datetime | None = None) -> date:
    """The trading day whose flow we're reading: today if a trading day, else the most
    recent trading day (so weekend/holiday reads anchor to the last real session)."""
    d = _et(now).date()
    return d if is_trading_day(d) else prev_trading_day(d)


def oi_settled_through(now: datetime | None = None) -> date:
    """The most recent trading session whose OI has PUBLISHED (settled). A session's
    OI publishes ~9:15am ET the next trading day; so 'settled through' is the latest
    session `s` for which now >= 9:15am ET on next_trading_day(s)."""
    n = _et(now)
    cand = session_date(now)
    while n < datetime.combine(next_trading_day(cand), _OI_PUBLISH, tzinfo=ET):
        cand = prev_trading_day(cand)
    return cand


def is_forming(sess: date, now: datetime | None = None) -> bool:
    """True when `sess`'s OI has NOT yet published (still 'forming'). Only a trading
    session can be forming — a weekend/holiday is never a forming session."""
    if not is_trading_day(sess):
        return False
    return sess > oi_settled_through(now)
