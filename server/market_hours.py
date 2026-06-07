"""US equity market-hours gate for the snapshot loop.

Options flow / OI / gamma / darkpool data only changes during market hours, so
running the 120s loop 24/7 burns ~80% of the UW daily budget on a closed,
unchanging market (and was pushing us past the 40k/day cap → 429 cascades).

Gate window: weekdays 9:00am–4:30pm ET (regular hours 9:30–16:00 plus a 30-min
buffer each side for the opening-auction setup and closing prints). Weekends
and full-closure holidays are skipped. Half-days are left at normal RTH — the
extra coverage is negligible.

ET conversion uses zoneinfo (tzdata is a pinned dependency so this resolves
inside the slim container).
"""
from __future__ import annotations
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_GATE_OPEN = time(9, 0)     # 30 min before the 9:30 open
_GATE_CLOSE = time(16, 30)  # 30 min after the 16:00 close

# US full-closure holidays (NYSE/Nasdaq). Half-days are not gated tighter.
_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027 (so the gate keeps working into next year without a redeploy)
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_trading_day(d: date) -> bool:
    """True when `d` is a weekday and not a full-closure holiday. Shared
    trading-calendar source for the snapshot gate and the OI backfill (which
    only attempts dates the market was actually open)."""
    if d.weekday() >= 5:                  # Sat=5, Sun=6
        return False
    return d.isoformat() not in _HOLIDAYS


def next_trading_day(d: date) -> date:
    """The first trading day strictly after `d` (skips weekends + holidays)."""
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    """The most recent trading day strictly before `d` (skips weekends + holidays)."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def market_is_open(now_utc: datetime | None = None) -> bool:
    """True when US equity markets are open (within the buffered gate window),
    False on weekends, holidays, and outside the daily window."""
    now = (now_utc or datetime.now(tz=timezone.utc)).astimezone(_ET)
    if now.weekday() >= 5:                       # Sat=5, Sun=6
        return False
    if now.date().isoformat() in _HOLIDAYS:
        return False
    return _GATE_OPEN <= now.time() <= _GATE_CLOSE
