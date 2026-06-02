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


def test_factors_are_transparent_with_value_and_note():
    """Each factor is self-explaining: {ok, value, note} — not a bare pass/fail."""
    s = tile4.score_contract(_c(), _ctx())
    f = s["factors"]
    assert set(f) == {"flow", "campaign", "room", "target", "execution", "greeks"}
    for name, fac in f.items():
        assert set(fac) >= {"ok", "note"}, name
        assert isinstance(fac["note"], str) and fac["note"], f"{name} needs a note"
    # the note carries the actual value / why
    assert "%" in f["room"]["note"] or "wall" in f["room"]["note"].lower()
    assert f["target"]["note"]  # e.g. "needs 4.5% vs 6% expected"


def test_perfect_contract_all_factors_ok_and_tiers_full():
    s = tile4.score_contract(_c(), _ctx())
    assert all(s["factors"][k]["ok"] is True for k in s["factors"])
    assert s["tier1"] == 3 and s["tier2"] == 1 and s["tier3"] == 2   # 3 thesis, 1 realism, 2 cost
    assert s["score"] == 6


def test_no_hard_veto_wide_spread_still_scored_not_disqualified():
    """A wide spread fails Execution but the contract is NOT disqualified — it
    just loses its tier-3 points and ranks lower. No 'eligible' veto anymore."""
    s = tile4.score_contract(_c(spread_pct=40.0), _ctx())
    assert s["factors"]["execution"]["ok"] is False
    assert "eligible" not in s                 # no veto field
    assert s["tier3"] < 2                       # lost cost points
    assert s["tier1"] == 3                      # thesis intact → still ranks decently


def test_missing_data_factor_ok_is_none_never_fabricated_pass():
    s = tile4.score_contract(_c(), _ctx(flow_strikes=None, expected_move_pct=None))
    assert s["factors"]["flow"]["ok"] is None
    assert s["factors"]["target"]["ok"] is None
    assert "unavailable" in s["factors"]["flow"]["note"].lower()


def test_room_at_wall_fails_with_explanatory_note():
    s = tile4.score_contract(_c(strike=110.0), _ctx())   # AT the call wall
    assert s["factors"]["room"]["ok"] is False
    assert "wall" in s["factors"]["room"]["note"].lower()


# ── Ranking (tier1 → tier2 → tier3, no veto) ─────────────────────────────────

def _scored(**kw):
    base = dict(tier1=3, tier2=1, tier3=2, score=6, factors={"flow": {"ok": True}},
                premium=1.5, delta=0.45, strike=103.0)
    base.update(kw)
    return base


def test_rank_orders_by_tier_thesis_dominates():
    rows = [
        _scored(strike=108, tier1=1, tier2=1, tier3=2),   # weak thesis
        _scored(strike=103, tier1=3, tier2=0, tier3=0),   # strong thesis, weak realism/cost
        _scored(strike=105, tier1=2, tier2=1, tier3=2),
    ]
    ranked = tile4.rank_contracts(rows)
    assert [r["strike"] for r in ranked] == [103, 105, 108]   # tier1 dominates
    assert [r["rank"] for r in ranked] == [1, 2, 3]


def test_rank_keeps_low_quality_contract_not_dropped():
    """No veto: a wide-spread / unrealistic contract still appears, just last."""
    rows = [_scored(strike=103, tier1=3, tier2=1, tier3=2),
            _scored(strike=120, tier1=0, tier2=0, tier3=0)]   # bad on everything
    ranked = tile4.rank_contracts(rows)
    assert len(ranked) == 2                       # nothing dropped
    assert ranked[-1]["strike"] == 120


def test_rank_tiebreak_within_tier_flow_then_cost_then_delta():
    rows = [
        _scored(strike=106, tier1=3, tier2=1, tier3=2, factors={"flow": {"ok": False}}, premium=1.0, delta=0.45),
        _scored(strike=105, tier1=3, tier2=1, tier3=2, factors={"flow": {"ok": True}},  premium=2.5, delta=0.45),
    ]
    assert tile4.rank_contracts(rows)[0]["strike"] == 105   # flow-aligned wins the tie


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
    # atm-chains: real shape = per-ATM-contract rows (call + put) w/ bid/ask +
    # stock_price; NO 'straddle' field. Expected move derived = ATM call mid + put
    # mid. Spot 100: C@100 mid 3.0 + P@100 mid 3.0 → straddle 6.0 → 6%.
    monkeypatch.setattr(storage, "fetch_atm_chains", lambda t, exps: {"data": [
        {"option_symbol": "SPY260605C00100000", "bid": "2.9", "ask": "3.1", "stock_price": "100"},
        {"option_symbol": "SPY260605P00100000", "bid": "2.9", "ask": "3.1", "stock_price": "100"}]})
    # greeks: real shape = one row per strike w/ separate call_/put_ columns.
    monkeypatch.setattr(storage, "fetch_greeks", lambda t, e: {"data": [
        {"strike": "103", "call_delta": "0.45", "call_theta": "-0.10",
         "put_delta": "-0.55", "put_theta": "-0.09"},
        {"strike": "108", "call_delta": "0.25", "call_theta": "-0.06",
         "put_delta": "-0.75", "put_theta": "-0.05"}]})
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
    assert out["ranked"] == [] and out["top"] is None


def test_build_tile4_ok_ranks_contracts_with_quote_and_factors(stub4):
    out = tile4.build_tile4("SPY", _ctx4())
    assert out["status"] == "ok"
    assert out["direction"] == "calls"
    assert len(out["ranked"]) >= 1
    assert out["expected_move_pct"] == pytest.approx(6.0)   # 6.0 straddle / 100 spot * 100
    top = out["top"]
    assert top is not None and top["rank"] == 1
    # 103 is the thesis-aligned, realistic strike → ranks first
    assert top["strike"] == 103.0
    # transparent factors present + delta parsed from call_delta
    assert top["delta"] == pytest.approx(0.45)
    assert top["factors"]["greeks"]["ok"] is True
    assert top["factors"]["target"]["ok"] is True
    # raw quote carried through for display
    assert top["bid"] == 1.51 and top["ask"] == 1.55
    assert "oi" in top and "volume" in top and "iv" in top
