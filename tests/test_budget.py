"""Tests for the UW call meter + soft budget guard."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import pytest

from server import budget


@pytest.fixture(autouse=True)
def reset_meter():
    budget.reset()
    yield
    budget.reset()


def _t(h=12, m=0, s=0, day=15):
    return datetime(2026, 5, day, h, m, s, tzinfo=timezone.utc)


def test_daily_count_survives_restart(tmp_path, monkeypatch):
    """The daily count must persist across a cold-boot so the guard doesn't reset
    to 0 on every redeploy (which masked the real cap on 2026-06-01)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    now = _t()
    for _ in range(2500):
        budget.record_call(now=now)
    assert budget.snapshot(now=now)["calls_today"] == 2500
    # simulate a restart: clear in-memory state, reload from disk
    budget.reset()
    budget.load_persisted(now=now)
    assert budget.snapshot(now=now)["calls_today"] == 2500


def test_persisted_count_ignored_on_new_utc_day(tmp_path, monkeypatch):
    """A persisted count from yesterday must NOT carry into today."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for _ in range(100):
        budget.record_call(now=_t(day=15))
    budget.reset()
    budget.load_persisted(now=_t(day=16))   # next UTC day
    assert budget.snapshot(now=_t(day=16))["calls_today"] == 0


def test_record_increments_daily_and_minute_counts():
    now = _t()
    budget.record_call(now=now)
    budget.record_call(now=now)
    snap = budget.snapshot(now=now)
    assert snap["calls_today"] == 2
    assert snap["calls_1m"] == 2


def test_minute_window_prunes_old_calls_but_keeps_daily():
    budget.record_call(now=_t(12, 0, 0))
    budget.record_call(now=_t(12, 0, 30))
    # 90s later: the first two are outside the rolling 60s window
    snap = budget.snapshot(now=_t(12, 1, 30))
    assert snap["calls_1m"] == 0
    assert snap["calls_today"] == 2  # daily count is not pruned


def test_daily_count_resets_on_utc_midnight_rollover():
    budget.record_call(now=_t(23, 59, 0, day=15))
    budget.record_call(now=_t(23, 59, 30, day=15))
    assert budget.snapshot(now=_t(23, 59, 45, day=15))["calls_today"] == 2
    # New UTC day → daily counter resets
    assert budget.snapshot(now=_t(0, 0, 5, day=16))["calls_today"] == 0
    budget.record_call(now=_t(0, 0, 6, day=16))
    assert budget.snapshot(now=_t(0, 0, 7, day=16))["calls_today"] == 1


def test_over_soft_budget_flips_at_threshold(monkeypatch):
    monkeypatch.setenv("UW_DAILY_CAP", "100")
    monkeypatch.setenv("UW_BUDGET_SOFT_PCT", "0.9")
    now = _t()
    for _ in range(89):
        budget.record_call(now=now)
    assert budget.over_soft_budget(now=now) is False
    budget.record_call(now=now)  # 90th → at threshold
    assert budget.over_soft_budget(now=now) is True


def test_snapshot_reports_budget_pct(monkeypatch):
    monkeypatch.setenv("UW_DAILY_CAP", "200")
    now = _t()
    for _ in range(50):
        budget.record_call(now=now)
    snap = budget.snapshot(now=now)
    assert snap["budget_pct"] == 25.0
    assert snap["daily_cap"] == 200
