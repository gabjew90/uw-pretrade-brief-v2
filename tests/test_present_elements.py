"""present elements tests (Phase 4 integration C) — one element per signal, unavailable
never omitted, conflict tone propagated, verdict forwarded verbatim.
"""
from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                           Quality, Skew)
from server.pipeline.decide import decide
from server.pipeline.present import present


def test_market_tile_is_data_not_a_posture():
    """Market context is a real TILE (label, surface, meaning, logic, tap detail) like the
    others — but its surface is DATA (the event), never a posture/verdict word."""
    market = {"gamma_sign": "NEG", "iv": 0.20, "tide": "bear", "event_line": "CPI <1d",
              "event_within_hold": True, "events_known": True,
              "as_of": "2026-06-09T15:00:00Z"}
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs), market=market)
    r = vm.regime
    assert r is not None
    assert r.surface == "CPI/FOMC: CPI <1d"                            # data, not a posture
    assert r.surface not in ("Favorable", "Mixed", "Stand down")
    assert "SPY gamma NEG" in r.meaning and "IV 20%" in r.meaning and "bear" in r.meaning
    assert r.logic and r.detail["Next macro event (5d)"] == "CPI <1d"  # tap variables
    assert r.provenance.source.value == "live"
    assert vm.verdict_logic


def test_market_tile_failed_calendar_is_na_not_all_clear():
    """A FAILED calendar/tide fetch must read n/a — never 'no event in 5d' (SEVERE #2)."""
    market = {"gamma_sign": None, "iv": None, "tide": None, "event_line": None,
              "event_within_hold": False, "events_known": False,
              "as_of": "2026-06-09T15:00:00Z"}
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs), market=market)
    assert vm.regime.surface == "EVENTS N/A"
    assert vm.regime.detail["Next macro event (5d)"] == "n/a"
    assert "no event" not in str(vm.regime.detail)
    assert vm.regime.detail["Tape tide"] == "n/a"


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


def test_contract_tile_shows_pick_and_alternatives():
    sigs = _full_signals()
    sigs["cost"] = Cost(guard="ok", ivr=40,
                        contract={"type": "call", "strike": 600.0, "expiry": "2026-06-12",
                                  "dte": 3, "bid": 4.5, "ask": 4.68, "spread_pct": 3.9,
                                  "breakeven_move_pct": 1.1, "expected_move_pct": 1.5,
                                  "delta": 0.45, "theta_day_pct": 8.0,
                                  "volume": 100, "open_interest": 1000},
                        candidates=[{"type": "call", "strike": 605.0, "expiry": "2026-06-12",
                                     "dte": 3, "bid": 2.0, "ask": 2.1, "spread_pct": 4.8,
                                     "breakeven_move_pct": 1.6, "expected_move_pct": 1.5,
                                     "delta": 0.30, "theta_day_pct": 11.0,
                                     "volume": 50, "open_interest": 500}])
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "contract")
    assert el.surface == "600 CALL · 3d"
    assert "Δ0.45" in el.meaning and "spread 4%" in el.meaning
    assert "Alt 1" in el.detail and "605" in el.detail["Alt 1"]


def test_contract_tile_unavailable_without_chain():
    sigs = _full_signals()
    sigs["cost"] = Cost(guard="caution", reason="no chain to price the trade")
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "contract")
    assert el.tone == "unavailable"


def test_cost_tone_tracks_guard():
    sigs = _full_signals()
    sigs["cost"] = Cost(guard="block", reason="earnings in 2d")
    vm = present("SPY", sigs, decide(sigs))
    cost_el = next(e for e in vm.elements if e.key == "cost")
    assert cost_el.surface == "PASS" and cost_el.tone == "negative"
