"""Tests for the snapshot pipeline orchestrator."""
from __future__ import annotations
from datetime import datetime, timezone
import pytest

from server import snapshot, storage, universe


@pytest.fixture
def fresh_storage_state():
    storage._cache._store.clear()
    yield
    storage._cache._store.clear()


@pytest.fixture
def stub_uw(monkeypatch):
    """Patch UW client layer (not storage layer), so storage._through runs and
    writes parquet naturally."""
    from server import uw
    monkeypatch.setattr(uw, "fetch_flow_alerts",
                        lambda limit=100: {"data": [{"ticker": t} for t in
                                                     ["NVDA", "TSLA", "AMD", "PLTR", "AMC",
                                                      "AAPL", "GOOGL", "MSFT", "META", "NFLX",
                                                      "AMZN", "F", "BAC", "WMT", "JPM"]]})
    monkeypatch.setattr(uw, "fetch_spot_exposures_strike",
                        lambda t: {"data": [{"strike": 100.0,
                                              "call_gamma_oi": 1.0,
                                              "put_gamma_oi": 0.5,
                                              "spot_price": 100.0}]})
    monkeypatch.setattr(uw, "fetch_oi_strike",
                        lambda t: {"data": [{"strike": 100,
                                              "open_interest": 1000,
                                              "prev_open_interest": 800}]})
    monkeypatch.setattr(uw, "fetch_volatility",
                        lambda t: {"data": [{"dte": 7, "iv": 0.4},
                                             {"dte": 30, "iv": 0.45}]})
    monkeypatch.setattr(uw, "fetch_interpolated_iv",
                        lambda t: {"data": [{"iv_rank": 42}]})
    monkeypatch.setattr(uw, "fetch_darkpool",
                        lambda t: {"data": [{"price": 100, "size": 1000}]})
    monkeypatch.setattr(uw, "fetch_earnings",
                        lambda t: {"data": [{"report_date": "2026-07-15"}]})


async def test_refresh_snapshot_happy_path_assembles_15_rows(stub_uw, fresh_storage_state, tmp_data_dir):
    snap = await snapshot.refresh_snapshot()
    assert len(snap.rows) == 15
    assert snap.rows[0].ticker == "NVDA"


async def test_refresh_snapshot_appends_to_jsonl(stub_uw, fresh_storage_state, tmp_data_dir):
    await snapshot.refresh_snapshot()
    jsonl = (tmp_data_dir / "snapshots.jsonl").read_text().strip().splitlines()
    assert len(jsonl) == 1


async def test_refresh_snapshot_per_endpoint_failure_partial_row(stub_uw, fresh_storage_state,
                                                                  tmp_data_dir, monkeypatch):
    """One endpoint failing for one ticker = partial row, _failures populated."""
    from server import uw
    original = uw.fetch_darkpool
    def patched(t):
        if t == "NVDA":
            raise uw.UWError("503 on /api/darkpool/NVDA")
        return original(t)
    monkeypatch.setattr(uw, "fetch_darkpool", patched)

    snap = await snapshot.refresh_snapshot()
    nvda = next(r for r in snap.rows if r.ticker == "NVDA")
    assert "darkpool" in nvda._failures


async def test_refresh_snapshot_updates_sticky_state(stub_uw, fresh_storage_state, tmp_data_dir):
    await snapshot.refresh_snapshot()
    sticky = storage.load_sticky()
    assert "NVDA" in sticky and "TSLA" in sticky


async def test_refresh_snapshot_archive_pass_covers_tracked_universe(stub_uw, fresh_storage_state,
                                                                       tmp_data_dir, monkeypatch):
    """Pinned ticker (not in hot_15) is also fetched for archive."""
    monkeypatch.setenv("TICKER_PIN_LIST", "PINNED_ONLY")
    await snapshot.refresh_snapshot()
    pinned_partition = list(tmp_data_dir.glob("raw/endpoint=spot_exposures_strike/dt=*/ticker=PINNED_ONLY"))
    assert pinned_partition, "pinned ticker must be archived"
