"""present elements tests (Phase 4 integration C) — one element per signal, unavailable
never omitted, conflict tone propagated, verdict forwarded verbatim.
"""
from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                           Quality, Skew)
from server.pipeline.decide import decide
from server.pipeline.present import present


def test_market_line_is_data_not_a_posture():
    """Market context is a muted data line (gamma/IV/tide/event) — NO posture word, not a
    second verdict (Fix 5)."""
    market = {"gamma_sign": "NEG", "iv": 0.20, "tide": "bear", "event_line": "CPI <1d",
              "event_within_hold": True, "as_of": "2026-06-09T15:00:00Z"}
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs), market=market)
    assert vm.regime is not None
    assert vm.regime.surface is None                                  # no posture word
    assert "gamma NEG" in vm.regime.meaning and "IV 20%" in vm.regime.meaning
    assert "CPI" in vm.regime.meaning and "bear" in vm.regime.meaning
    assert vm.regime.provenance.source.value == "live"
    assert vm.verdict_logic                                            # how the call is made


def test_every_tile_has_logic():
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs))
    assert all(e.logic for e in vm.elements if e.key != "flow_truncation")


def _full_signals():
    return {
        "flow": Flow(direction="calls", direction_basis="opening_flow", call_prem=1e6, put_prem=1e5),
        "conviction": Conviction(direction="calls", dir_delta=100, accumulation="building"),
        "positioning": Positioning(confirmation="building", side="call", oi_trend_pct=20.0),
        "dealer_gamma": DealerGamma(gex_sign="NEG", flip_status="ok", agg_b=-2.0),
        "skew": Skew(rr25=-0.05, lean="put_skew"),       # opposes calls → conflict
        "cost": Cost(guard="ok", ivr=40),
    }


def test_one_element_per_signal_plus_keys():
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs))
    keys = {e.key for e in vm.elements}
    assert {"direction", "conviction", "positioning", "structural", "skew", "cost"} <= keys
    assert "regime" not in keys                          # market-wide, not a per-ticker tile


def test_unavailable_signal_emits_element_not_omitted():
    sigs = _full_signals()
    sigs["skew"] = Skew(lean="unavailable", provenance=Provenance(quality=Quality.UNAVAILABLE, note="no RR"))
    vm = present("SPY", sigs, decide(sigs))
    skew_el = next(e for e in vm.elements if e.key == "skew")
    assert skew_el.surface is None
    assert skew_el.tone == "unavailable"
    assert "why" in skew_el.detail


def test_conflict_leg_element_tinted_cautionary():
    """skew opposes calls → it's in conflict_legs → its element tone becomes cautionary."""
    sigs = _full_signals()                              # skew put_skew opposes calls
    v = decide(sigs)
    assert "skew" in v.conflict_legs
    vm = present("SPY", sigs, v)
    skew_el = next(e for e in vm.elements if e.key == "skew")
    assert skew_el.tone == "cautionary"


def test_verdict_forwarded_verbatim():
    sigs = _full_signals()
    v = decide(sigs)
    vm = present("SPY", sigs, v)
    assert vm.verdict is v                              # same object, not re-derived


def test_cost_tone_tracks_guard():
    sigs = _full_signals()
    sigs["cost"] = Cost(guard="block", reason="earnings in 2d")
    vm = present("SPY", sigs, decide(sigs))
    cost_el = next(e for e in vm.elements if e.key == "cost")
    assert cost_el.surface == "PASS" and cost_el.tone == "negative"
