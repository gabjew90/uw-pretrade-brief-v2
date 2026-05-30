"""Tests for the one-shot historical OI backfill."""
from __future__ import annotations
from datetime import date, datetime, timezone
import pytest

from server import backfill, storage, budget


@pytest.fixture(autouse=True)
def _isolate(tmp_data_dir, monkeypatch):
    storage._cache._store.clear()
    budget.reset()
    yield
    storage._cache._store.clear()
    budget.reset()


# Friday noon UTC — gives a clean run of weekday history behind it.
NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


def _oi_payload(_ticker, date=None):
    return {"data": [{"strike": 100, "call_oi": 600, "put_oi": 400},
                     {"strike": 105, "call_oi": 200, "put_oi": 300}]}


def test_trading_days_excludes_weekends_and_holidays():
    days = backfill._trading_days(NOW, max_days=10)
    isos = [d.isoformat() for d in days]
    assert "2026-05-30" not in isos and "2026-05-31" not in isos  # weekend
    assert "2026-05-25" not in isos                               # Memorial Day
    assert "2026-05-29" not in isos                               # today excluded
    assert "2026-05-28" in isos                                   # yesterday (Thu)
    assert all(date.fromisoformat(i).weekday() < 5 for i in isos)


def test_probe_unsupported_aborts_before_full_pass(monkeypatch):
    """If even the nearest historical date returns no usable OI, report
    unsupported and write nothing."""
    monkeypatch.setattr(backfill.uw, "fetch_oi_strike",
                        lambda t, date=None: {"data": []})
    report = backfill.backfill_oi_history(["SPY", "QQQ"], max_days=7, now=NOW)
    assert report["probe"] == "unsupported"
    assert report["rows_written"] == 0
    assert list(storage._data_dir().rglob("*.parquet")) == []


def test_backfill_writes_history_into_correct_dt_partitions(monkeypatch):
    calls = []
    monkeypatch.setattr(backfill.uw, "fetch_oi_strike",
                        lambda t, date=None: calls.append((t, date)) or _oi_payload(t, date))
    report = backfill.backfill_oi_history(["SPY"], max_days=5, now=NOW)

    assert report["probe"] == "ok"
    assert report["rows_written"] >= 1
    # read_oi_history must see the backfilled sessions, oldest→newest, real dates
    sessions = storage.read_oi_history("SPY", 5)
    assert len(sessions) == report["rows_written"]
    dates = [s["date"] for s in sessions]
    assert "2026-05-28" in dates          # a real historical trading day
    assert "2026-05-29" not in dates      # never backfills today
    assert dates == sorted(dates)         # oldest → newest


def test_backfill_is_idempotent_skips_existing_partitions(monkeypatch):
    # Pre-seed one (ticker, day) partition as if already captured live.
    storage.write_response(
        endpoint="oi_per_strike", ticker="SPY", params=None,
        response=_oi_payload("SPY"), status_code=200, latency_ms=10,
        fetched_at=datetime(2026, 5, 28, 20, 0, tzinfo=timezone.utc),
    )
    calls = []
    monkeypatch.setattr(backfill.uw, "fetch_oi_strike",
                        lambda t, date=None: calls.append((t, date)) or _oi_payload(t, date))
    report = backfill.backfill_oi_history(["SPY"], max_days=5, now=NOW)

    assert ("SPY", "2026-05-28") not in calls, "existing day must be skipped, not re-fetched"
    assert report["skipped_existing"] >= 1


def test_backfill_stops_when_over_budget(monkeypatch):
    monkeypatch.setattr(budget, "over_soft_budget", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(backfill.uw, "fetch_oi_strike",
                        lambda t, date=None: calls.append((t, date)) or _oi_payload(t, date))
    report = backfill.backfill_oi_history(["SPY"], max_days=5, now=NOW)

    assert report["stopped_at_budget"] is True
    assert calls == [], "no UW calls once over the soft budget"
