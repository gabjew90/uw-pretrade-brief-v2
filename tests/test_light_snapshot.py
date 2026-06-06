import pytest

from server import snapshot as snap_mod


@pytest.mark.asyncio
async def test_build_light_snapshot_flow_only(monkeypatch):
    flow_payload = {"data": [
        {"ticker": "AAA", "type": "call", "total_premium": 900000,
         "volume_oi_ratio": 2.0, "all_opening_trades": False, "underlying_price": 100},
        {"ticker": "BBB", "type": "put", "total_premium": 500000,
         "volume_oi_ratio": 2.0, "all_opening_trades": False, "underlying_price": 50},
    ]}
    monkeypatch.setattr(snap_mod.storage, "fetch_flow_alerts", lambda *a, **k: flow_payload)

    heavy_called = []
    for name in ("fetch_spot_exposures_strike", "fetch_oi_strike", "fetch_volatility",
                 "fetch_interpolated_iv", "fetch_darkpool", "fetch_earnings",
                 "fetch_ticker_info", "fetch_news_headlines", "fetch_ohlc",
                 "read_oi_history"):
        monkeypatch.setattr(snap_mod.storage, name,
                            lambda *a, _n=name, **k: heavy_called.append(_n))

    snap = await snap_mod.build_light_snapshot()
    assert heavy_called == [], f"light build must not fetch per-ticker endpoints: {heavy_called}"
    assert len(snap.rows) >= 1
    tickers = {r.ticker for r in snap.rows}
    assert {"AAA", "BBB"} <= tickers
    for r in snap.rows:
        assert r.is_light is True
        assert r.gates.flow in ("green", "yellow", "red")
        # opening flow present (voi>1) → real side, opening_flow basis
        assert r.direction in ("calls", "puts")
        assert r.direction_basis in ("opening_flow", "total_flow", "gamma_fallback")
