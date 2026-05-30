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
                        lambda limit=100: {"data": [
                            {"ticker": t, "underlying_price": "100.0",
                             "type": "call", "strike": "100", "total_premium": "1000",
                             "total_ask_side_prem": "800", "total_bid_side_prem": "200",
                             "has_sweep": False, "has_singleleg": True,
                             "has_multileg": False, "total_size": 10,
                             "expiry": "2026-06-06", "option_chain": f"{t}260606C00100000",
                             "created_at": "2026-05-28T14:30:00Z",
                             "all_opening_trades": True, "volume_oi_ratio": "0.3",
                             "volume": 2442, "open_interest": 7913}
                            for t in ["NVDA", "TSLA", "AMD", "PLTR", "AMC",
                                      "AAPL", "GOOGL", "MSFT", "META", "NFLX",
                                      "AMZN", "F", "BAC", "WMT", "JPM"]]})
    monkeypatch.setattr(uw, "fetch_spot_exposures_strike",
                        lambda t, date=None, min_strike=None, max_strike=None:
                        {"data": [{"strike": 100.0,
                                   "call_gamma_oi": 1.0,
                                   "put_gamma_oi": -0.5,  # UW pre-signs puts negative
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
    monkeypatch.setattr(uw, "fetch_ohlc",
                        lambda t, candle_size="5m": {"data": [
                            {"start_time": "2026-05-28T14:30:00Z",
                             "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5,
                             "volume": 12345}]})


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


async def test_refresh_snapshot_skips_unconsumed_endpoints(stub_uw, fresh_storage_state,
                                                           tmp_data_dir):
    """Budget guard: endpoints no tile consumes must NOT be fetched every cycle.
    Each cycle previously fired ~5 ingest-only calls per ticker (×15) plus 4
    cross-ticker ones, helping blow UW Basic's 120/min · 40k/day budget → the
    429 cascade behind the 2026-05-29 outage. storage writes a parquet partition
    only on a real UW call, so absence of the partition proves no call was made."""
    await snapshot.refresh_snapshot()
    unconsumed = [
        "group_flow", "lit_flow_recent", "seasonality_market", "seasonality_ticker",
        "net_flow_expiry", "option_contracts", "max_pain", "net_prem_ticks",
    ]
    for endpoint in unconsumed:
        hits = list(tmp_data_dir.glob(f"raw/endpoint={endpoint}/**/*.parquet"))
        assert not hits, f"{endpoint} was fetched but no tile consumes it: {hits}"


async def test_consumed_endpoints_still_fetched(stub_uw, fresh_storage_state, tmp_data_dir):
    """Companion guard: the trim must not drop endpoints tiles DO consume."""
    await snapshot.refresh_snapshot()
    for endpoint in ("spot_exposures_strike", "oi_per_strike", "volatility_term_structure",
                     "darkpool", "ohlc"):
        hits = list(tmp_data_dir.glob(f"raw/endpoint={endpoint}/**/*.parquet"))
        assert hits, f"{endpoint} feeds a tile but was not fetched"


def _gex_payload(strikes_gamma, spot=100.0):
    """spot-exposures/strike shape: net gamma per strike from call/put_gamma_oi."""
    return {"data": [{"strike": k, "call_gamma_oi": g, "put_gamma_oi": 0.0,
                      "spot_price": spot} for k, g in strikes_gamma]}


def test_extract_gex_flip_at_sign_crossing_not_lowest_strike():
    """Flip = strike where cumulative net γ crosses zero. With realistic signed
    data (puts negative below spot, calls positive above) it lands near the
    crossing — NOT defaulted to the lowest strike (the all-positive bug)."""
    spot = 100.0
    rows = []
    for k in (90, 95, 100, 105, 110):
        call, put = (1e6, -1e8) if k < 100 else (1e8, -1e6)  # puts dominate below, calls above
        rows.append({"strike": k, "call_gamma_oi": call, "put_gamma_oi": put})
    flip_pct, *_ = snapshot._extract_gex({"data": rows}, spot)
    flip_strike = spot * (1 + flip_pct / 100)
    assert 95 <= flip_strike <= 105, f"flip {flip_strike} should be near the crossing, not the low strike"


def test_extract_gex_status_unavailable_on_failure_or_empty():
    from server import storage
    fail = storage.UWFailure(endpoint="spot_exposures_strike", ticker="X", message="boom")
    assert snapshot._extract_gex(fail, 100.0)[5] == "unavailable"
    assert snapshot._extract_gex({"data": []}, 100.0)[5] == "unavailable"


def test_extract_gex_no_flip_when_gamma_never_crosses_zero():
    """SOXX/EWY case: γ doesn't cross zero within the window → status 'no_flip'
    and flip is NOT defaulted to the lowest strike (the -20% band-edge garbage)."""
    spot = 100.0
    # net γ negative at every strike → cumulative never crosses zero
    rows = [{"strike": float(k), "call_gamma_oi": 1e6, "put_gamma_oi": -1e8}
            for k in (80, 90, 100, 110, 120)]
    flip_pct, _, _, _, _, status = snapshot._extract_gex({"data": rows}, spot)
    assert status == "no_flip"
    assert abs(flip_pct) < 1.0, f"flip {flip_pct}% must not be the band edge"


def test_extract_gex_flip_is_crossing_nearest_spot():
    """With multiple cumulative sign-changes, the flip is the crossing nearest
    spot, not the first one scanning from the bottom."""
    spot = 100.0
    rows = [
        {"strike": 80, "call_gamma_oi": 0, "put_gamma_oi": -1e8},   # cum -1e8
        {"strike": 85, "call_gamma_oi": 2e8, "put_gamma_oi": 0},    # cum +1e8  (cross @85, far)
        {"strike": 95, "call_gamma_oi": 0, "put_gamma_oi": -3e8},   # cum -2e8  (cross @95, near)
        {"strike": 105, "call_gamma_oi": 4e8, "put_gamma_oi": 0},   # cum +2e8  (cross @105, near)
    ]
    flip_pct, _, _, _, _, status = snapshot._extract_gex({"data": rows}, spot)
    flip_strike = spot * (1 + flip_pct / 100)
    assert status == "ok"
    assert flip_strike in (95.0, 105.0), f"flip {flip_strike} should be the nearest crossing to spot"


def test_extract_gex_walls_from_call_and_put_gamma_not_atm():
    """Call wall = strike with max call γ ABOVE spot; put wall = max |put γ|
    BELOW spot. Using max|net γ| collapsed both to ATM (net magnitude peaks
    at-the-money) — the +0.1%/-0.1% bug."""
    spot = 100.0
    rows = [
        {"strike": 95,  "call_gamma_oi": 1e6, "put_gamma_oi": -9e8},  # put wall (below)
        {"strike": 99,  "call_gamma_oi": 5e8, "put_gamma_oi": -5e8},  # ATM, big |net|
        {"strike": 101, "call_gamma_oi": 5e8, "put_gamma_oi": -5e8},  # ATM, big |net|
        {"strike": 108, "call_gamma_oi": 9e8, "put_gamma_oi": -1e6},  # call wall (above)
    ]
    _, wu, wd, *_ = snapshot._extract_gex({"data": rows}, spot)
    call_wall = spot * (1 + wu / 100)
    put_wall = spot * (1 - wd / 100)
    assert round(call_wall) == 108, f"call wall {call_wall} should be the call-γ peak, not ATM"
    assert round(put_wall) == 95, f"put wall {put_wall} should be the put-γ peak, not ATM"


def test_build_tile3_extracts_net_gamma_per_strike():
    spot = 100.0
    data = _gex_payload([(98.0, 1.0), (100.0, 3.0), (102.0, -2.0)], spot)
    t3 = snapshot._build_tile3(data, spot)
    by_strike = {s.strike: s.net_gamma for s in t3.strikes}
    assert by_strike == {98.0: 1.0, 100.0: 3.0, 102.0: -2.0}
    assert [s.strike for s in t3.strikes] == [98.0, 100.0, 102.0]  # sorted ascending


def test_build_tile3_windows_to_near_spot_and_caps():
    spot = 100.0
    # strikes from 50..150; only those within +/-8% (92..108) should survive
    data = _gex_payload([(float(k), 1.0) for k in range(50, 151, 1)], spot)
    t3 = snapshot._build_tile3(data, spot)
    assert t3.strikes, "should keep near-spot strikes"
    assert all(92.0 <= s.strike <= 108.0 for s in t3.strikes)
    assert len(t3.strikes) <= 25


def test_build_tile3_empty_on_failure_or_no_spot():
    from server import storage
    fail = storage.UWFailure(endpoint="spot_exposures_strike", ticker="X", message="boom")
    assert snapshot._build_tile3(fail, 100.0).strikes == []
    assert snapshot._build_tile3(_gex_payload([(100.0, 1.0)]), 0.0).strikes == []


async def test_refresh_fetches_spot_exposures_with_near_spot_band(stub_uw, fresh_storage_state,
                                                                  tmp_data_dir, monkeypatch):
    """The row build must request spot-exposures around the flow spot (stub spot
    = 100), so the gamma strikes actually bracket spot."""
    from server import uw
    captured = []
    orig = uw.fetch_spot_exposures_strike
    def spy(t, date=None, min_strike=None, max_strike=None):
        captured.append((min_strike, max_strike))
        return orig(t, date=date, min_strike=min_strike, max_strike=max_strike)
    monkeypatch.setattr(uw, "fetch_spot_exposures_strike", spy)
    await snapshot.refresh_snapshot()
    banded = [(lo, hi) for lo, hi in captured if lo is not None and hi is not None]
    assert banded, "spot-exposures must be fetched with a min/max strike band"
    lo, hi = banded[0]
    assert lo < 100 < hi, f"band {lo}-{hi} should bracket the flow spot 100"


async def test_refresh_snapshot_rows_carry_tile3(stub_uw, fresh_storage_state, tmp_data_dir):
    snap = await snapshot.refresh_snapshot()
    assert snap.rows[0].tile3.strikes, "rows must carry a populated tile3 ladder"
    # stub spot-exposures: call_gamma_oi 1.0 - put_gamma_oi 0.5 = 0.5 net gamma
    assert snap.rows[0].tile3.strikes[0].net_gamma == 0.5


def test_nearest_delta_matches_within_tolerance():
    """Flow strikes rarely match the greek-exposure grid exactly; nearest
    within tolerance should match, beyond tolerance should return 0."""
    grid = {720.0: 1000.0, 725.0: 2000.0, 730.0: 3000.0}
    # 729 is within 1.5% of 730 → matches 730
    assert snapshot._nearest_delta(grid, 729.0) == 3000.0
    # exact hit
    assert snapshot._nearest_delta(grid, 725.0) == 2000.0
    # 600 is far from any grid strike (>1.5%) → 0
    assert snapshot._nearest_delta(grid, 600.0) == 0.0
    # empty grid → 0
    assert snapshot._nearest_delta({}, 725.0) == 0.0


def test_median_robust_to_outlier():
    assert snapshot._median([0.3, 0.4, 0.5, 385.0]) == 0.45
    assert snapshot._median([1.0]) == 1.0
    assert snapshot._median([]) == 0.0


@pytest.mark.xfail(reason="Tracked-universe archive pass disabled in 0959d8b — "
                          "will re-enable as decoupled background task in v0.3", strict=False)
async def test_refresh_snapshot_archive_pass_covers_tracked_universe(stub_uw, fresh_storage_state,
                                                                       tmp_data_dir, monkeypatch):
    """Pinned ticker (not in hot_15) is also fetched for archive."""
    monkeypatch.setenv("TICKER_PIN_LIST", "PINNED_ONLY")
    await snapshot.refresh_snapshot()
    pinned_partition = list(tmp_data_dir.glob("raw/endpoint=spot_exposures_strike/dt=*/ticker=PINNED_ONLY"))
    assert pinned_partition, "pinned ticker must be archived"
