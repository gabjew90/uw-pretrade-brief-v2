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
                        lambda t, date=None: {"data": [{"strike": 100,
                                                         "call_oi": 600,
                                                         "put_oi": 400}]})
    monkeypatch.setattr(uw, "fetch_volatility",
                        lambda t: {"data": [{"dte": 7, "iv": 0.4},
                                             {"dte": 30, "iv": 0.45}]})
    monkeypatch.setattr(uw, "fetch_interpolated_iv",
                        lambda t: {"data": [{"iv_rank": 42}]})
    monkeypatch.setattr(uw, "fetch_darkpool",
                        lambda t: {"data": [{"price": 100, "size": 1000}]})
    monkeypatch.setattr(uw, "fetch_earnings",
                        lambda t: {"data": [{"report_date": "2026-07-15"}]})
    monkeypatch.setattr(uw, "fetch_ticker_info",
                        lambda t: {"data": [{"sector": "Technology"}]})
    monkeypatch.setattr(uw, "fetch_news_headlines",
                        lambda ticker=None, limit=10: {"data": [
                            {"headline": "Test news", "source": "AP",
                             "published_at": "2026-05-28T10:30:00Z"}]})
    monkeypatch.setattr(uw, "fetch_market_tide",
                        lambda date=None: {"data": [{"tide": 0.3}]})
    monkeypatch.setattr(uw, "fetch_sector_tide",
                        lambda sector, date=None: {"data": [{"tide": 0.25}]})
    monkeypatch.setattr(uw, "fetch_option_contracts",
                        lambda t, limit=500: {"data": [{"option_symbol": "X", "bid": 1, "ask": 2}]})
    monkeypatch.setattr(uw, "fetch_max_pain",
                        lambda t, date=None: {"data": [{"max_pain": 100}]})
    monkeypatch.setattr(uw, "fetch_net_prem_ticks",
                        lambda t, date=None: {"data": [{"net_call_premium": 0, "net_put_premium": 0}]})
    monkeypatch.setattr(uw, "fetch_group_flow",
                        lambda group="sp500": {"data": [{"greek": "delta", "net_flow": 1.0}]})
    monkeypatch.setattr(uw, "fetch_net_flow_expiry",
                        lambda ticker=None: {"data": [{"expiry": "2026-06-06", "net_premium": 0}]})
    monkeypatch.setattr(uw, "fetch_lit_flow_recent",
                        lambda: {"data": [{"ticker": "NVDA", "premium": 100}]})
    monkeypatch.setattr(uw, "fetch_option_contract_history",
                        lambda symbol: {"data": [{"date": "2026-05-27", "bid": 1.0, "ask": 1.2}]})
    monkeypatch.setattr(uw, "fetch_seasonality_market",
                        lambda: {"data": [{"month": "May", "avg_return": 0.012}]})
    monkeypatch.setattr(uw, "fetch_seasonality_ticker",
                        lambda t: {"data": [{"month": "May", "avg_return": 0.018}]})


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


@pytest.mark.xfail(reason="Tracked-universe archive pass disabled in 0959d8b — "
                          "will re-enable as decoupled background task in v0.3", strict=False)
async def test_refresh_snapshot_archive_pass_covers_tracked_universe(stub_uw, fresh_storage_state,
                                                                       tmp_data_dir, monkeypatch):
    """Pinned ticker (not in hot_15) is also fetched for archive."""
    monkeypatch.setenv("TICKER_PIN_LIST", "PINNED_ONLY")
    await snapshot.refresh_snapshot()
    pinned_partition = list(tmp_data_dir.glob("raw/endpoint=spot_exposures_strike/dt=*/ticker=PINNED_ONLY"))
    assert pinned_partition, "pinned ticker must be archived"
