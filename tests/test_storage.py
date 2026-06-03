"""Tests for the parquet archive writer and read-through cache layer."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pyarrow.parquet as pq
import pytest

from server import storage


def test_view_store_persists_and_reads_complete_view(tmp_data_dir):
    """A complete on-demand view (tile3/tile4/lookup) is persisted as ONE unit so
    the atomic fallback replays a coherent moment, not per-endpoint reassembly."""
    payload = {"status": "ok", "ticker": "NVDA", "ranked": [{"strike": 100}]}
    storage.save_view("tile4", "NVDA", payload)
    got = storage.read_view("tile4", "NVDA")
    assert got == payload


def test_read_view_none_when_absent(tmp_data_dir):
    assert storage.read_view("tile4", "ZZZZ") is None


def test_view_store_keys_isolate_view_and_ticker(tmp_data_dir):
    storage.save_view("tile3", "NVDA", {"v": 3})
    storage.save_view("tile4", "NVDA", {"v": 4})
    assert storage.read_view("tile3", "NVDA") == {"v": 3}
    assert storage.read_view("tile4", "NVDA") == {"v": 4}


def test_read_last_snapshot_returns_last_good_line(tmp_data_dir):
    """Boot-seed: read the most recent snapshot WITH rows from snapshots.jsonl,
    so a cold-boot can serve last-good data instead of blank."""
    storage.append_snapshot({"fetched_at": "2026-06-01T14:00:00Z", "rows": [{"ticker": "AAA"}]})
    storage.append_snapshot({"fetched_at": "2026-06-01T14:02:00Z", "rows": [{"ticker": "BBB"}]})
    storage.append_snapshot({"fetched_at": "2026-06-01T14:04:00Z", "rows": []})  # later empty
    snap = storage.read_last_snapshot()
    assert snap is not None
    assert snap["rows"][0]["ticker"] == "BBB"   # last NON-EMPTY, not the trailing empty one


def test_read_last_snapshot_none_when_no_file(tmp_data_dir):
    assert storage.read_last_snapshot() is None


def test_read_last_snapshot_none_when_all_empty(tmp_data_dir):
    storage.append_snapshot({"fetched_at": "x", "rows": []})
    assert storage.read_last_snapshot() is None


def test_cached_only_serves_stale_parquet_for_replay(tmp_data_dir, monkeypatch):
    """In cached-only (replay) mode, a parquet row OLDER than its TTL still counts
    as a hit — replay reads captured archives that are hours/days old. Without
    this, every replay read misses on freshness and returns unavailable."""
    from datetime import datetime, timedelta, timezone
    from server import uw
    storage._cache._store.clear()
    # Write a greeks row stamped 6 hours ago (well beyond the 5-min medium TTL).
    old = datetime.now(tz=timezone.utc) - timedelta(hours=6)
    storage.write_response(endpoint="greeks", ticker="NVDA", params={"expiry": "2026-06-06"},
                           response={"data": [{"strike": 100, "call_delta": "0.5"}]},
                           status_code=200, latency_ms=1, fetched_at=old)
    monkeypatch.setattr(uw, "fetch_greeks", lambda t, expiry: {"data": [{"LIVE": True}]})
    storage._cache._store.clear()
    with storage.cached_only():
        result = storage.fetch_greeks("NVDA", "2026-06-06")
    assert result == {"data": [{"strike": 100, "call_delta": "0.5"}]}   # the stale archived row, not LIVE


def test_cached_only_mode_never_calls_uw(tmp_data_dir, monkeypatch):
    """In cached-only mode (request path) a cache/parquet miss returns UWFailure
    WITHOUT calling UW — enforces 'live-ingest in loop, request path cached-only'."""
    from server import uw
    storage._cache._store.clear()
    called = []
    monkeypatch.setattr(uw, "fetch_volatility", lambda t, date=None: called.append(t) or {"data": []})
    with storage.cached_only():
        result = storage.fetch_volatility("SPY", is_hot=True)
    assert isinstance(result, storage.UWFailure)
    assert "cached-only" in result.message.lower()
    assert called == [], "request path must not hit UW"


def test_cached_only_still_serves_warm_cache(tmp_data_dir, monkeypatch):
    """Cached-only still returns a warm RAM/parquet hit — it only blocks the live
    UW call, not cache reads."""
    from server import uw
    storage._cache._store.clear()
    monkeypatch.setattr(uw, "fetch_volatility", lambda t, date=None: {"data": [{"warm": True}]})
    storage.fetch_volatility("SPY", is_hot=True)            # loop path warms cache
    with storage.cached_only():
        result = storage.fetch_volatility("SPY", is_hot=True)
    assert result == {"data": [{"warm": True}]}


def test_loop_path_still_calls_uw_by_default(tmp_data_dir, monkeypatch):
    from server import uw
    storage._cache._store.clear()
    called = []
    monkeypatch.setattr(uw, "fetch_volatility", lambda t, date=None: called.append(t) or {"data": []})
    storage.fetch_volatility("SPY", is_hot=True)            # no cached_only → live fetch
    assert called == ["SPY"]


def test_expiry_strike_passes_expirations_and_window_to_uw(tmp_data_dir, monkeypatch):
    from server import uw
    storage._cache._store.clear()
    seen = {}
    monkeypatch.setattr(uw, "fetch_spot_exposures_expiry_strike",
                        lambda t, expirations, min_strike=None, max_strike=None:
                        seen.update(exp=expirations, mn=min_strike, mx=max_strike) or {"data": []})
    storage.fetch_spot_exposures_expiry_strike("SPY", ["2026-06-05"], min_strike=605, max_strike=905)
    assert seen == {"exp": ["2026-06-05"], "mn": 605, "mx": 905}


def test_spot_exposures_passes_min_max_strike_to_uw(tmp_data_dir, monkeypatch):
    """Near-spot strike window must reach UW — without min/max_strike the endpoint
    returns the lowest strikes in the chain (far below spot). Regression for the
    gamma flip/wall garbage on high-priced tickers."""
    from server import uw
    storage._cache._store.clear()
    seen = {}
    monkeypatch.setattr(uw, "fetch_spot_exposures_strike",
                        lambda t, date=None, min_strike=None, max_strike=None:
                        seen.update(min=min_strike, max=max_strike) or {"data": []})
    storage.fetch_spot_exposures_strike("SPY", is_hot=True, min_strike=605, max_strike=905)
    assert seen == {"min": 605, "max": 905}


def test_budget_guard_sheds_noncritical_endpoint_without_calling_uw(tmp_data_dir, monkeypatch):
    """Over the soft budget, a non-whitelisted endpoint short-circuits to a
    UWFailure WITHOUT hitting UW — the proactive shedding that prevents a 429
    cascade. Regression guard for the 2026-05-29 outage."""
    from server import uw, budget
    storage._cache._store.clear()
    monkeypatch.setattr(budget, "over_soft_budget", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(uw, "fetch_oi_strike", lambda *a, **k: called.append(1) or {"data": []})

    result = storage.fetch_oi_strike("SPY", is_hot=True)
    assert isinstance(result, storage.UWFailure)
    assert "budget guard" in result.message.lower()
    assert not called, "UW must NOT be called when shedding"


def test_budget_guard_whitelists_flow_alerts(tmp_data_dir, monkeypatch):
    """flow_alerts is the core read — it must still call through even when over
    budget, so the hot list / health stay alive longest."""
    from server import uw, budget
    storage._cache._store.clear()
    monkeypatch.setattr(budget, "over_soft_budget", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(uw, "fetch_flow_alerts",
                        lambda ticker=None, limit=100: called.append(1) or {"data": [{"ticker": "SPY"}]})

    result = storage.fetch_flow_alerts(100)
    assert called, "flow_alerts must call through despite the budget guard"
    assert not isinstance(result, storage.UWFailure)


def test_write_response_creates_partition_file(tmp_data_dir: Path):
    storage.write_response(
        endpoint="spot_exposures_strike",
        ticker="NVDA",
        params={"date": "2026-05-27"},
        response={"data": [{"strike": 150.0, "gamma": 1.5}]},
        status_code=200,
        latency_ms=140,
        fetched_at=datetime(2026, 5, 27, 14, 16, 23, tzinfo=timezone.utc),
    )
    partitions = list(tmp_data_dir.rglob("*.parquet"))
    assert len(partitions) == 1
    p = partitions[0]
    # Path: /data/raw/endpoint=spot_exposures_strike/dt=2026-05-27/ticker=NVDA/part-1400.parquet
    assert "endpoint=spot_exposures_strike" in p.parts
    assert "dt=2026-05-27" in p.parts
    assert "ticker=NVDA" in p.parts
    assert p.name == "part-1400.parquet"


def test_write_response_appends_to_existing_hour_file(tmp_data_dir: Path):
    for minute in (16, 30, 45):
        storage.write_response(
            endpoint="oi_per_strike",
            ticker="TSLA",
            params=None,
            response={"data": [{"strike": 250}]},
            status_code=200,
            latency_ms=80,
            fetched_at=datetime(2026, 5, 27, 14, minute, 0, tzinfo=timezone.utc),
        )
    parts = list(tmp_data_dir.rglob("*.parquet"))
    assert len(parts) == 1, "all 3 writes in the 14:xx hour should land in one file"
    table = pq.read_table(parts[0])
    assert table.num_rows == 3


def test_write_response_no_ticker_uses_endpoint_only_partition(tmp_data_dir: Path):
    """flow-alerts is cross-ticker; no ticker= partition segment."""
    storage.write_response(
        endpoint="flow_alerts",
        ticker=None,
        params={"limit": 100},
        response={"data": [{"ticker": "NVDA"}]},
        status_code=200,
        latency_ms=200,
        fetched_at=datetime(2026, 5, 27, 14, 16, 0, tzinfo=timezone.utc),
    )
    parts = list(tmp_data_dir.rglob("*.parquet"))
    assert len(parts) == 1
    p = parts[0]
    assert "ticker=" not in "/".join(p.parts), "ticker partition absent for cross-ticker endpoint"
    assert "endpoint=flow_alerts" in p.parts
    assert "dt=2026-05-27" in p.parts


def test_write_response_records_metadata_fields(tmp_data_dir: Path):
    storage.write_response(
        endpoint="darkpool",
        ticker="GLD",
        params=None,
        response={"data": []},
        status_code=200,
        latency_ms=350,
        fetched_at=datetime(2026, 5, 27, 14, 16, 0, tzinfo=timezone.utc),
    )
    p = next(tmp_data_dir.rglob("*.parquet"))
    table = pq.read_table(p).to_pylist()
    assert len(table) == 1
    row = table[0]
    assert row["status_code"] == 200
    assert row["latency_ms"] == 350
    assert json.loads(row["response"]) == {"data": []}


def test_write_response_swallows_disk_full(tmp_data_dir: Path, monkeypatch, caplog):
    """When pyarrow raises OSError, write_response must log + return False, not raise."""
    import pyarrow.parquet as pq_mod
    def boom(*a, **kw): raise OSError("[Errno 28] No space left on device")
    monkeypatch.setattr(pq_mod, "write_table", boom)
    ok = storage.write_response(
        endpoint="spot_exposures_strike",
        ticker="NVDA",
        params=None,
        response={"data": []},
        status_code=200,
        latency_ms=100,
        fetched_at=datetime(2026, 5, 27, 14, 16, 0, tzinfo=timezone.utc),
    )
    assert ok is False
    assert "storage write failed" in caplog.text.lower()


import time
from unittest.mock import patch


@pytest.fixture
def fresh_cache():
    """Reset storage.py's module-level cache between tests."""
    storage._cache._store.clear()
    yield
    storage._cache._store.clear()


def test_fetch_calls_uw_on_first_call_and_writes_parquet(tmp_data_dir, fresh_cache, monkeypatch):
    """First call for (endpoint, ticker) hits UW and writes to parquet."""
    calls = []
    def fake_uw_fetch(ticker, min_strike=None, max_strike=None):
        calls.append(ticker)
        return {"data": [{"strike": 150}]}
    monkeypatch.setattr("server.uw.fetch_spot_exposures_strike", fake_uw_fetch)

    result = storage.fetch_spot_exposures_strike("NVDA", is_hot=True)

    assert result == {"data": [{"strike": 150}]}
    assert calls == ["NVDA"]
    assert list(tmp_data_dir.rglob("*.parquet")), "must have written parquet"


def test_fetch_returns_cache_on_second_call_within_ttl(tmp_data_dir, fresh_cache, monkeypatch):
    """Second call within TTL returns cached without re-hitting UW."""
    calls = []
    def fake_uw_fetch(ticker, min_strike=None, max_strike=None):
        calls.append(ticker)
        return {"data": [{"strike": 150}]}
    monkeypatch.setattr("server.uw.fetch_spot_exposures_strike", fake_uw_fetch)

    storage.fetch_spot_exposures_strike("NVDA", is_hot=True)  # cold
    storage.fetch_spot_exposures_strike("NVDA", is_hot=True)  # warm

    assert len(calls) == 1, "second call must hit cache"


def test_hot_ticker_ttl_is_60_seconds(tmp_data_dir, fresh_cache):
    """is_hot=True → 60s TTL."""
    assert storage._ttl_seconds(endpoint="spot_exposures_strike", is_hot=True) == 60


def test_sleeper_ticker_ttl_is_300_seconds(tmp_data_dir, fresh_cache):
    """is_hot=False → 300s (5min) TTL for dynamic endpoints."""
    assert storage._ttl_seconds(endpoint="spot_exposures_strike", is_hot=False) == 300


def test_earnings_endpoint_ttl_is_24h_regardless_of_hot(tmp_data_dir, fresh_cache):
    """Quasi-static endpoints get 24h TTL (parquet persistence makes long
    TTLs safe — see commit caeda18)."""
    assert storage._ttl_seconds(endpoint="earnings", is_hot=True) == 86400
    assert storage._ttl_seconds(endpoint="earnings", is_hot=False) == 86400


def test_fetch_swallows_uw_error_returns_exception_marker(tmp_data_dir, fresh_cache, monkeypatch):
    """When UW raises, storage returns a marker so the caller can record _failures."""
    from server.uw import UWError
    def fake_uw_fetch(ticker, min_strike=None, max_strike=None):
        raise UWError("503 on /api/stock/NVDA/spot-exposures/strike")
    monkeypatch.setattr("server.uw.fetch_spot_exposures_strike", fake_uw_fetch)

    result = storage.fetch_spot_exposures_strike("NVDA", is_hot=True)
    assert isinstance(result, storage.UWFailure)
    assert "503" in result.message


def test_append_snapshot_writes_jsonl_line(tmp_data_dir):
    snap = {"fetched_at": "2026-05-27T14:16:00Z", "rows": [{"ticker": "NVDA"}]}
    storage.append_snapshot(snap)
    p = tmp_data_dir / "snapshots.jsonl"
    assert p.exists()
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == snap


def test_append_snapshot_appends_to_existing_file(tmp_data_dir):
    storage.append_snapshot({"n": 1})
    storage.append_snapshot({"n": 2})
    storage.append_snapshot({"n": 3})
    lines = (tmp_data_dir / "snapshots.jsonl").read_text().strip().splitlines()
    assert [json.loads(l)["n"] for l in lines] == [1, 2, 3]


def test_load_sticky_returns_empty_dict_when_missing(tmp_data_dir):
    assert storage.load_sticky() == {}


def test_save_then_load_sticky_roundtrips(tmp_data_dir):
    sticky = {"NVDA": "2026-05-27T15:30:00+00:00", "AMD": "2026-05-26T11:15:00+00:00"}
    storage.save_sticky(sticky)
    assert storage.load_sticky() == sticky


def test_load_sticky_returns_empty_on_corrupt_file(tmp_data_dir, caplog):
    p = tmp_data_dir / "sticky.json"
    p.write_text("{ this is not json")
    result = storage.load_sticky()
    assert result == {}
    assert "sticky.json corrupt" in caplog.text.lower()


# ── Parquet read-through tests ────────────────────────────────────────────────


def test_parquet_read_returns_fresh_row(tmp_data_dir, fresh_cache):
    """Write a recent row; _read_latest_from_parquet returns it."""
    from datetime import datetime, timezone
    storage.write_response(
        endpoint="spot_exposures_strike", ticker="NVDA", params=None,
        response={"data": [{"strike": 100}]},
        status_code=200, latency_ms=100,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    hit, fetched_at = storage._read_latest_from_parquet(
        "spot_exposures_strike", "NVDA", None, max_age_seconds=300
    )
    assert hit == {"data": [{"strike": 100}]}
    assert fetched_at is not None


def test_parquet_read_skips_stale_rows(tmp_data_dir, fresh_cache):
    """Row older than max_age_seconds should be ignored."""
    from datetime import datetime, timedelta, timezone
    storage.write_response(
        endpoint="oi_per_strike", ticker="TSLA", params=None,
        response={"data": [{"strike": 250}]},
        status_code=200, latency_ms=80,
        fetched_at=datetime.now(tz=timezone.utc) - timedelta(minutes=30),
    )
    # Asking for max-age 5min should reject the 30-min-old row
    hit, fetched_at = storage._read_latest_from_parquet(
        "oi_per_strike", "TSLA", None, max_age_seconds=300
    )
    assert hit is None
    assert fetched_at is None


def test_through_skips_uw_when_parquet_has_fresh_row(tmp_data_dir, fresh_cache, monkeypatch):
    """The whole point: parquet hit means UW is NOT called."""
    from datetime import datetime, timezone
    storage.write_response(
        endpoint="spot_exposures_strike", ticker="NVDA", params=None,
        response={"data": [{"strike": 100, "from_parquet": True}]},
        status_code=200, latency_ms=100,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    calls = []
    def fake_uw_fetch(ticker, min_strike=None, max_strike=None):
        calls.append(ticker)
        return {"data": [{"strike": 100, "from_uw": True}]}
    monkeypatch.setattr("server.uw.fetch_spot_exposures_strike", fake_uw_fetch)

    result = storage.fetch_spot_exposures_strike("NVDA", is_hot=True)

    assert result == {"data": [{"strike": 100, "from_parquet": True}]}
    assert calls == [], "UW must not be called when parquet has a fresh row"


# ── OI history reader (Tile 2) ────────────────────────────────────────────────


def test_read_oi_history_returns_sessions_oldest_to_newest(tmp_data_dir):
    """Three days of archived oi_per_strike → 3 sessions, oldest first, with
    per-strike total OI (call_oi + put_oi)."""
    from datetime import datetime, timezone
    for day, oi in ((25, 1000), (26, 1200), (27, 1500)):
        storage.write_response(
            endpoint="oi_per_strike", ticker="NVDA", params=None,
            response={"data": [{"strike": "150", "call_oi": oi, "put_oi": 0}]},
            status_code=200, latency_ms=50,
            fetched_at=datetime(2026, 5, day, 14, 0, 0, tzinfo=timezone.utc),
        )
    sessions = storage.read_oi_history("NVDA", n_sessions=5)
    assert [s["date"] for s in sessions] == ["2026-05-25", "2026-05-26", "2026-05-27"]
    assert sessions[0]["strikes"][150.0] == 1000
    assert sessions[-1]["strikes"][150.0] == 1500


def test_read_oi_history_caps_at_n_sessions(tmp_data_dir):
    from datetime import datetime, timezone
    for day in range(20, 28):  # 8 days archived
        storage.write_response(
            endpoint="oi_per_strike", ticker="TSLA", params=None,
            response={"data": [{"strike": "250", "call_oi": 100 * day, "put_oi": 0}]},
            status_code=200, latency_ms=50,
            fetched_at=datetime(2026, 5, day, 14, 0, 0, tzinfo=timezone.utc),
        )
    sessions = storage.read_oi_history("TSLA", n_sessions=5)
    assert len(sessions) == 5, "must cap at the 5 most-recent sessions"
    assert sessions[-1]["date"] == "2026-05-27"  # newest retained


def test_read_oi_history_empty_when_no_archive(tmp_data_dir):
    assert storage.read_oi_history("NOPE", n_sessions=5) == []


# ── Freshness provenance tests ────────────────────────────────────────────────


def test_through_records_live_provenance(tmp_data_dir):
    from server import freshness
    storage._cache._store.clear()
    with freshness.collect() as fc:
        out = storage._through("realized_vol", "SPY", None, False,
                               lambda: {"data": [{"x": 1}]})
        s = fc.summary()
    assert out == {"data": [{"x": 1}]}
    assert s["n_live"] == 1 and s["data_provenance"] == "live"
    assert s["as_of"] is not None   # stamped ~now


def test_through_ram_hit_records_cache_with_original_observed_at(tmp_data_dir):
    from server import freshness
    storage._cache._store.clear()
    obs = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)
    key = storage._make_key("realized_vol", "SPY", None)
    storage._cache.set(key, {"data": [1]}, ttl_seconds=60, observed_at=obs)
    with freshness.collect() as fc:
        out = storage._through("realized_vol", "SPY", None, False, lambda: None)
        s = fc.summary()
    assert out == {"data": [1]}
    assert s["data_provenance"] == "cache"
    assert s["as_of"] == obs.isoformat()   # ORIGINAL pull time, not cache-set time


def test_through_archive_provenance_in_cached_only(tmp_data_dir):
    from datetime import timedelta
    from server import freshness
    storage._cache._store.clear()
    old = datetime.now(tz=timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    storage.write_response("realized_vol", "SPY", None, {"data": [9]},
                           status_code=200, latency_ms=1, fetched_at=old)
    storage._cache._store.clear()
    with freshness.collect() as fc, storage.cached_only():
        out = storage._through("realized_vol", "SPY", None, False, lambda: None)
        s = fc.summary()
    assert out == {"data": [9]}
    assert s["data_provenance"] == "archive"
    # as_of reflects the aged parquet row, not now
    assert s["as_of"].startswith(old.isoformat()[:16])


def test_through_within_ttl_parquet_hit_records_cache(tmp_data_dir):
    from server import freshness
    storage._cache._store.clear()
    fresh_ts = datetime.now(tz=timezone.utc)
    storage.write_response("realized_vol", "SPY", None, {"data": [7]},
                           status_code=200, latency_ms=1, fetched_at=fresh_ts)
    storage._cache._store.clear()
    with freshness.collect() as fc:
        out = storage._through("realized_vol", "SPY", None, False, lambda: None)
        s = fc.summary()
    assert out == {"data": [7]}
    # a within-TTL parquet hit (NOT cached_only) is "cache", not "archive"
    assert s["data_provenance"] == "cache"
    # as_of carries the served row's real pull time (not now / not None)
    assert s["as_of"] is not None
    assert s["as_of"].startswith(fresh_ts.isoformat()[:19])
