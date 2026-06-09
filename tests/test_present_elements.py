"""present elements tests (Phase 4 integration C) — one element per signal, unavailable
never omitted, conflict tone propagated, verdict forwarded verbatim.
"""
from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                           Quality, Regime, Skew)
from server.pipeline.decide import decide
from server.pipeline.present import present


def test_regime_header_shows_data_variables_and_logic():
    """The regime header exposes the DATA behind the posture word, plus the rule."""
    regime = Regime(posture="Stand down", gamma_sign="POS", gamma_status="ok",
                    vol_iv=0.17, tide_lean="bear", event_line="CPI 2d", event_severity="warn")
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs), regime=regime)
    assert vm.regime is not None
    assert vm.regime.surface == "Stand down"
    assert "POS" in vm.regime.meaning and "17%" in vm.regime.meaning   # data variables shown
    assert vm.regime.logic                                            # the rule is stated
    assert vm.regime.detail["SPY index gamma"].startswith("POS")
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
