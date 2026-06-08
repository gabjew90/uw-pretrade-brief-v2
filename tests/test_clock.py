"""Clock service tests — deterministic via injected `now` (v2's lesson). This is the
one scaffold module with real logic, so it gets real tests."""
from datetime import datetime
from zoneinfo import ZoneInfo

from server.services import clock

ET = ZoneInfo("America/New_York")


def test_trading_day_weekend_and_holiday():
    assert clock.is_trading_day(datetime(2026, 6, 5).date())      # Fri
    assert not clock.is_trading_day(datetime(2026, 6, 6).date())  # Sat
    assert not clock.is_trading_day(datetime(2026, 6, 19).date()) # Juneteenth (holiday)


def test_next_prev_trading_day_skip_weekend():
    assert clock.next_trading_day(datetime(2026, 6, 5).date()).isoformat() == "2026-06-08"  # Fri→Mon
    assert clock.prev_trading_day(datetime(2026, 6, 8).date()).isoformat() == "2026-06-05"  # Mon→Fri


def test_phase_open_premarket_closed():
    assert clock.phase(datetime(2026, 6, 5, 11, 0, tzinfo=ET)) == clock.Phase.OPEN
    assert clock.phase(datetime(2026, 6, 5, 8, 0, tzinfo=ET)) == clock.Phase.PREMARKET
    assert clock.phase(datetime(2026, 6, 6, 11, 0, tzinfo=ET)) == clock.Phase.CLOSED  # Sat


def test_half_day_closes_early():
    # 2026-11-27 is a half day (1pm close): 2pm ET is afterhours, not open.
    assert clock.phase(datetime(2026, 11, 27, 14, 0, tzinfo=ET)) == clock.Phase.AFTERHOURS


def test_session_date_anchors_to_last_trading_day_on_weekend():
    assert clock.session_date(datetime(2026, 6, 7, 12, 0, tzinfo=ET)).isoformat() == "2026-06-05"


def test_oi_settlement_and_forming():
    # Sunday: Friday's OI settled Sat? No — publishes next TRADING day (Mon 9:15).
    sun = datetime(2026, 6, 7, 12, 0, tzinfo=ET)
    fri = datetime(2026, 6, 5).date()
    assert clock.is_forming(fri, now=sun)                          # Fri still forming on Sun
    assert clock.oi_settled_through(now=sun) < fri                  # settled through < Fri
    # Tuesday after Monday's 9:15 publish: Friday is settled.
    tue = datetime(2026, 6, 9, 12, 0, tzinfo=ET)
    assert not clock.is_forming(fri, now=tue)
