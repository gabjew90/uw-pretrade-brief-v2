"""Tests for the on-demand Tile 3 detail builder."""
from __future__ import annotations
import pytest

from server import tile3_detail, storage


GE = {"data": [
    {"expiry": "2026-06-05", "dte": 2, "call_charm": "1e8", "put_charm": "-5e7",
     "call_vanna": "2e9", "put_vanna": "1e9"},
    {"expiry": "2026-06-12", "dte": 9, "call_charm": "5e7", "put_charm": "-2e7",
     "call_vanna": "1e9", "put_vanna": "5e8"},
]}
ES = {"data": [
    {"expiry": "2026-06-05", "strike": "95", "call_gamma_oi": "1e6", "put_gamma_oi": "-9e8",
     "call_gamma_vol": "2e6", "put_gamma_vol": "-1e8"},
    {"expiry": "2026-06-05", "strike": "105", "call_gamma_oi": "9e8", "put_gamma_oi": "-1e6",
     "call_gamma_vol": "5e8", "put_gamma_vol": "-2e6"},
    {"expiry": "2026-06-12", "strike": "100", "call_gamma_oi": "3e8", "put_gamma_oi": "-3e8",
     "call_gamma_vol": "1e8", "put_gamma_vol": "-1e8"},
]}


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(storage, "fetch_greek_exposure_expiry", lambda t: GE)
    monkeypatch.setattr(storage, "fetch_spot_exposures_expiry_strike",
                        lambda t, expirations, min_strike=None, max_strike=None: ES)
    monkeypatch.setattr(storage, "fetch_max_pain", lambda t: {"data": []})


def test_unavailable_when_greek_exposure_empty(monkeypatch):
    monkeypatch.setattr(storage, "fetch_greek_exposure_expiry", lambda t: {"data": []})
    out = tile3_detail.build_tile3_detail("SPY", 100.0, "calls")
    assert out["status"] == "unavailable"


def test_unavailable_on_uw_failure(monkeypatch):
    fail = storage.UWFailure(endpoint="greek_exposure_expiry", ticker="SPY", message="boom")
    monkeypatch.setattr(storage, "fetch_greek_exposure_expiry", lambda t: fail)
    assert tile3_detail.build_tile3_detail("SPY", 100.0, "calls")["status"] == "unavailable"


def test_builds_views_keyed_by_expiry(stub):
    out = tile3_detail.build_tile3_detail("SPY", 100.0, "calls")
    assert out["status"] == "ok"
    assert set(out["views"]) == {"2026-06-05", "2026-06-12"}
    assert out["default_expiry"] == "2026-06-05"   # nearest dte
    assert out["direction"] == "calls"


def test_oi_and_vol_ladders_use_their_columns(stub):
    v = tile3_detail.build_tile3_detail("SPY", 100.0, "calls")["views"]["2026-06-05"]
    oi_by_strike = {r["strike"]: r for r in v["oi"]}
    vol_by_strike = {r["strike"]: r for r in v["vol"]}
    assert oi_by_strike[95.0]["put_gamma"] == pytest.approx(-9e8)   # _oi column
    assert vol_by_strike[95.0]["put_gamma"] == pytest.approx(-1e8)  # _vol column
    assert oi_by_strike[105.0]["net"] == pytest.approx(9e8 + -1e6)


def test_levels_and_regime_from_oi(stub):
    v = tile3_detail.build_tile3_detail("SPY", 100.0, "calls")["views"]["2026-06-05"]
    # net: 95 -> ~-9e8 (puts), 105 -> ~+9e8 (calls) → aggregate ~0, walls separated
    assert v["regime"] in ("long", "short")
    assert round(v["call_wall"]) == 105   # max call-γ above spot
    assert round(v["put_wall"]) == 95     # max |put-γ| below spot


def test_drift_available_reflects_charm_vanna(stub, monkeypatch):
    assert tile3_detail.build_tile3_detail("SPY", 100.0, "calls")["drift_available"] is True
    zero = {"data": [{"expiry": "2026-06-05", "dte": 2, "call_charm": "0", "put_charm": "0",
                      "call_vanna": "0", "put_vanna": "0"}]}
    monkeypatch.setattr(storage, "fetch_greek_exposure_expiry", lambda t: zero)
    assert tile3_detail.build_tile3_detail("SPY", 100.0, "calls")["drift_available"] is False
