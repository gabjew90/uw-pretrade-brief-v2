"""Tests for the shared gamma-levels math (server/gex.py)."""
from __future__ import annotations
from server import gex


def _rung(strike, call_g, put_g):
    return {"strike": float(strike), "call_gamma": call_g, "put_gamma": put_g,
            "net": call_g + put_g}


def test_flip_is_nearest_spot_crossing():
    spot = 100.0
    rungs = [_rung(80, 0, -1e8), _rung(85, 2e8, 0), _rung(95, 0, -3e8), _rung(105, 4e8, 0)]
    lv = gex.compute_levels(rungs, spot)
    flip_strike = spot * (1 + lv["flip_pct"] / 100)
    assert lv["flip_status"] == "ok"
    assert flip_strike in (95.0, 105.0)


def test_no_flip_when_no_crossing():
    spot = 100.0
    rungs = [_rung(k, 1e6, -1e8) for k in (80, 90, 100, 110, 120)]  # net always negative
    lv = gex.compute_levels(rungs, spot)
    assert lv["flip_status"] == "no_flip"
    assert abs(lv["flip_pct"]) < 1.0


def test_walls_from_call_and_put_gamma_not_atm():
    spot = 100.0
    rungs = [_rung(95, 1e6, -9e8), _rung(99, 5e8, -5e8), _rung(101, 5e8, -5e8), _rung(108, 9e8, -1e6)]
    lv = gex.compute_levels(rungs, spot)
    assert round(spot * (1 + lv["call_wall_pct"] / 100)) == 108   # max call-γ above
    assert round(spot * (1 - lv["put_wall_pct"] / 100)) == 95     # max |put-γ| below


def test_sign_from_aggregate_net():
    spot = 100.0
    assert gex.compute_levels([_rung(100, 1e8, -1e6)], spot)["gex_sign"] == "POS"
    assert gex.compute_levels([_rung(100, 1e6, -1e8)], spot)["gex_sign"] == "NEG"
