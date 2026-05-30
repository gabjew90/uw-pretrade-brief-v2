"""Tests for the Tile 4 contract-picker scoring core (pure logic)."""
from __future__ import annotations
from server import tile4


# ── Gates ────────────────────────────────────────────────────────────────────

def test_event_gate_blocks_and_stands_down():
    g = tile4.evaluate_gates(iv_rank=40, event_before_expiry=True, term_front=30, term_back=28)
    assert g["event"]["state"] == "block"
    assert g["stand_down"] is True


def test_iv_rank_gate_blocks_above_threshold():
    g = tile4.evaluate_gates(iv_rank=85, event_before_expiry=False, term_front=30, term_back=28)
    assert g["iv_rank"]["state"] == "block"
    assert g["stand_down"] is True


def test_gates_all_clear_no_standdown():
    g = tile4.evaluate_gates(iv_rank=40, event_before_expiry=False, term_front=27, term_back=30)
    assert g["stand_down"] is False
    assert g["term_structure"]["state"] == "green"   # front below back = cheap


def test_term_structure_amber_when_front_rich():
    g = tile4.evaluate_gates(iv_rank=40, event_before_expiry=False, term_front=33, term_back=28)
    assert g["term_structure"]["state"] == "amber"


def test_gate_unknown_when_data_missing_warns_not_greenlight():
    g = tile4.evaluate_gates(iv_rank=None, event_before_expiry=None, term_front=None, term_back=None)
    assert g["iv_rank"]["state"] == "unknown"
    assert g["event"]["state"] == "unknown"
    assert g["stand_down"] is False        # unknown warns, does not block...
    assert g["warn"] is True               # ...but flags a warning


# ── Per-contract scoring ─────────────────────────────────────────────────────

def _ctx(**kw):
    base = dict(spot=100.0, direction="calls", flow_strikes={103.0}, oi_building={103.0},
                call_wall=110.0, put_wall=90.0, expected_move_pct=6.0, atm_iv=0.30)
    base.update(kw)
    return base


def _c(**kw):
    # breakeven = (103 + 1.5 - 100)/100 = 4.5% <= 6% expected -> Target passes
    base = dict(strike=103.0, type="call", premium=1.5, delta=0.45, theta=-0.10,
                spread_pct=3.0, iv=0.31, oi=5000)
    base.update(kw)
    return base


def test_perfect_contract_scores_six_and_eligible():
    s = tile4.score_contract(_c(), _ctx())
    assert s["score"] == 6
    assert s["eligible"] is True
    assert all(v == "pass" for v in s["checks"].values())


def test_target_fail_caps_eligibility():
    # breakeven = (103 + 8 - 100)/100 = 11% > 6% expected -> Target fail
    s = tile4.score_contract(_c(premium=8.0), _ctx())
    assert s["checks"]["target"] == "fail"
    assert s["eligible"] is False
    assert "move" in s["reason"].lower()


def test_execution_fail_caps_eligibility():
    s = tile4.score_contract(_c(spread_pct=9.0), _ctx())   # wide spread
    assert s["checks"]["execution"] == "fail"
    assert s["eligible"] is False


def test_unknown_dot_when_data_missing_never_passes():
    s = tile4.score_contract(_c(), _ctx(flow_strikes=None, expected_move_pct=None))
    assert s["checks"]["flow"] == "unknown"
    assert s["checks"]["target"] == "unknown"
    # unknown target does NOT cap eligibility (only an explicit fail does)
    assert s["eligible"] is True


def test_room_fail_at_wall():
    s = tile4.score_contract(_c(strike=110.0), _ctx())   # strike AT the call wall
    assert s["checks"]["room"] == "fail"


# ── Pick selection / tiebreaker ──────────────────────────────────────────────

def test_pick_best_prefers_eligible_high_score():
    scored = [
        {"strike": 108.0, "score": 4, "eligible": True, "checks": {"flow": "fail"}, "premium": 1.5, "delta": 0.40},
        {"strike": 105.0, "score": 6, "eligible": True, "checks": {"flow": "pass"}, "premium": 2.0, "delta": 0.45},
        {"strike": 103.0, "score": 6, "eligible": False, "checks": {"flow": "pass"}, "premium": 1.0, "delta": 0.5},
    ]
    pick = tile4.pick_best(scored)
    assert pick["strike"] == 105.0      # highest score among eligible


def test_pick_tiebreak_flow_then_cost_then_delta():
    scored = [
        {"strike": 106.0, "score": 6, "eligible": True, "checks": {"flow": "fail"}, "premium": 1.0, "delta": 0.45},
        {"strike": 105.0, "score": 6, "eligible": True, "checks": {"flow": "pass"}, "premium": 2.5, "delta": 0.45},
    ]
    assert tile4.pick_best(scored)["strike"] == 105.0   # flow-aligned beats cheaper


def test_pick_none_when_no_eligible():
    scored = [{"strike": 105.0, "score": 5, "eligible": False, "checks": {}, "premium": 2.0, "delta": 0.45}]
    assert tile4.pick_best(scored) is None


# ── Orchestration (build_tile4) ──────────────────────────────────────────────

import pytest
from server import storage


def _chain(expiry="2026-06-05"):
    def occ(k):
        return f"SPY260605C{int(k * 1000):08d}"
    return {"data": [
        {"option_symbol": occ(103), "nbbo_bid": "1.51", "nbbo_ask": "1.55",
         "implied_volatility": "0.31", "volume": "1000", "open_interest": "5000"},
        {"option_symbol": occ(108), "nbbo_bid": "0.40", "nbbo_ask": "0.44",
         "implied_volatility": "0.33", "volume": "500", "open_interest": "3000"},
    ]}


@pytest.fixture
def stub4(monkeypatch):
    monkeypatch.setattr(storage, "fetch_option_contracts", lambda t, n=500: _chain())
    monkeypatch.setattr(storage, "fetch_interpolated_iv", lambda t, h=False: {"data": [{"percentile": 0.4, "days": 7}]})
    monkeypatch.setattr(storage, "fetch_volatility", lambda t, h=False: {"data": [{"dte": 5, "volatility": 0.30}, {"dte": 30, "volatility": 0.33}]})
    monkeypatch.setattr(storage, "fetch_atm_chains", lambda t, exps: {"data": [{"straddle": 6.0}]})
    monkeypatch.setattr(storage, "fetch_greeks", lambda t, e: {"data": [
        {"strike": "103", "delta": "0.45", "theta": "-0.10"},
        {"strike": "108", "delta": "0.25", "theta": "-0.06"}]})
    monkeypatch.setattr(storage, "fetch_earnings", lambda t, h=False: {"data": []})
    monkeypatch.setattr(storage, "fetch_fda_calendar", lambda t: {"data": []})


def _ctx4(**kw):
    base = dict(spot=100.0, direction="calls", flow_strikes={103.0}, oi_building={103.0},
                call_wall=112.0, put_wall=90.0)
    base.update(kw)
    return base


def test_build_tile4_unavailable_on_chain_failure(monkeypatch):
    monkeypatch.setattr(storage, "fetch_option_contracts",
                        lambda t, n=500: storage.UWFailure(endpoint="option_contracts", ticker=t, message="429"))
    out = tile4.build_tile4("SPY", _ctx4())
    assert out["status"] == "unavailable"


def test_build_tile4_stand_down_on_high_iv_rank(stub4, monkeypatch):
    monkeypatch.setattr(storage, "fetch_interpolated_iv", lambda t, h=False: {"data": [{"percentile": 0.95, "days": 7}]})
    out = tile4.build_tile4("SPY", _ctx4())
    assert out["status"] == "stand_down"
    assert out["recommendation"] is None


def test_build_tile4_ok_scores_contracts_and_picks(stub4):
    out = tile4.build_tile4("SPY", _ctx4())
    assert out["status"] == "ok"
    assert out["direction"] == "calls"
    assert len(out["contracts"]) >= 1
    assert out["expected_move_pct"] == pytest.approx(6.0)   # 6.0 straddle / 100 spot * 100
    assert out["recommendation"] is not None
    assert out["recommendation"]["strike"] == 103.0
