"""Tests for the market-hours gate. Times constructed in UTC; the gate converts
to America/New_York. 2026-05-29 is a Friday (EDT, UTC-4)."""
from __future__ import annotations
from datetime import datetime, timezone

from server import market_hours


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_open_during_rth_weekday():
    # 14:00 UTC = 10:00 EDT on a Friday → open
    assert market_hours.market_is_open(_utc(2026, 5, 29, 14, 0)) is True


def test_is_trading_day_weekday_not_holiday():
    from datetime import date
    assert market_hours.is_trading_day(date(2026, 5, 29)) is True   # Friday


def test_is_trading_day_weekend_false():
    from datetime import date
    assert market_hours.is_trading_day(date(2026, 5, 30)) is False  # Saturday
    assert market_hours.is_trading_day(date(2026, 5, 31)) is False  # Sunday


def test_is_trading_day_holiday_false():
    from datetime import date
    assert market_hours.is_trading_day(date(2026, 5, 25)) is False  # Memorial Day


def test_open_at_buffer_edges():
    # 13:00 UTC = 09:00 EDT (gate opens) → open
    assert market_hours.market_is_open(_utc(2026, 5, 29, 13, 0)) is True
    # 20:30 UTC = 16:30 EDT (gate closes) → open (inclusive)
    assert market_hours.market_is_open(_utc(2026, 5, 29, 20, 30)) is True


def test_closed_before_gate_open():
    # 12:30 UTC = 08:30 EDT → before 09:00 gate → closed
    assert market_hours.market_is_open(_utc(2026, 5, 29, 12, 30)) is False


def test_closed_after_gate_close():
    # 21:00 UTC = 17:00 EDT → after 16:30 gate → closed
    assert market_hours.market_is_open(_utc(2026, 5, 29, 21, 0)) is False


def test_closed_overnight():
    # 04:00 UTC = 00:00 EDT → closed
    assert market_hours.market_is_open(_utc(2026, 5, 29, 4, 0)) is False


def test_closed_weekend():
    # 2026-05-30 is Saturday, 2026-05-31 Sunday — closed even mid-day
    assert market_hours.market_is_open(_utc(2026, 5, 30, 15, 0)) is False
    assert market_hours.market_is_open(_utc(2026, 5, 31, 15, 0)) is False


def test_closed_on_holiday():
    # 2026-05-25 is Memorial Day (Monday) — closed even during RTH
    assert market_hours.market_is_open(_utc(2026, 5, 25, 15, 0)) is False


def test_open_day_after_holiday():
    # 2026-05-26 Tuesday RTH → open
    assert market_hours.market_is_open(_utc(2026, 5, 26, 15, 0)) is True
