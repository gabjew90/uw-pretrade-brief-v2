"""Tests for the parquet archive writer and read-through cache layer."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import pyarrow.parquet as pq
import pytest

from server import storage


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
    def fake_uw_fetch(ticker):
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
    def fake_uw_fetch(ticker):
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


def test_earnings_endpoint_ttl_is_6_hours_regardless_of_hot(tmp_data_dir, fresh_cache):
    """Quasi-static endpoints get long TTL."""
    assert storage._ttl_seconds(endpoint="earnings", is_hot=True) == 21600
    assert storage._ttl_seconds(endpoint="earnings", is_hot=False) == 21600


def test_fetch_swallows_uw_error_returns_exception_marker(tmp_data_dir, fresh_cache, monkeypatch):
    """When UW raises, storage returns a marker so the caller can record _failures."""
    from server.uw import UWError
    def fake_uw_fetch(ticker):
        raise UWError("503 on /api/stock/NVDA/spot-exposures/strike")
    monkeypatch.setattr("server.uw.fetch_spot_exposures_strike", fake_uw_fetch)

    result = storage.fetch_spot_exposures_strike("NVDA", is_hot=True)
    assert isinstance(result, storage.UWFailure)
    assert "503" in result.message
