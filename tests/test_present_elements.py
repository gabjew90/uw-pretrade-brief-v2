"""present elements tests (Phase 4 integration C) — one element per signal, unavailable
never omitted, the lights-only default contract, verdict forwarded verbatim.
"""
from pathlib import Path

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
    assert {"direction", "conviction", "positioning", "structural", "cost"} <= keys
    assert "skew" not in keys       # skew leg DELETED (four-lights; MPP 2022 artifact)
    assert "regime" not in keys                          # market-wide, not a per-ticker tile


def test_unavailable_signal_emits_element_not_omitted():
    sigs = _full_signals()
    sigs["dealer_gamma"] = DealerGamma(flip_status="unavailable",
                                       provenance=Provenance(quality=Quality.UNAVAILABLE,
                                                             note="no gamma"))
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "structural")
    assert el.surface is None
    assert el.tone == "unavailable"
    assert "why" in el.detail


def test_default_render_contract_lights_only():
    """Directive acceptance #4: the default render carries at most 4 numbers and the
    verdict vocabulary is exactly PERFECT / NOT NOW — n/N. Everything else (provenance,
    how-lines, tiles) lives in the why-panel (vm.elements)."""
    sigs = _full_signals()
    vm = present("SPY", sigs, decide(sigs))
    assert len(vm.numbers) <= 4
    assert vm.verdict.overall in ("PERFECT", "NOT NOW")
    assert vm.verdict.action.startswith(("PERFECT", "NOT NOW"))
    assert "Mixed" not in vm.next_step and "Favorable" not in vm.next_step
    assert len(vm.verdict.calls.gates) <= 5 and len(vm.verdict.puts.gates) <= 5


def test_spark_is_net_premium_sign_adjusted_to_best_direction():
    """The one default-render chart: cumulative net opening premium, 'up' = building
    toward the best direction; colored by smart_flow's state."""
    sigs = _full_signals()
    sigs["flow"].flow_series = [{"t": "09:35", "call": 100, "put": 0},
                                {"t": "10:00", "call": 300, "put": 50}]
    vm = present("SPY", sigs, decide(sigs))
    sgn = 1 if vm.verdict.direction == "calls" else -1
    assert vm.spark == [100.0 * sgn, 250.0 * sgn]
    assert vm.spark_state in ("GREEN", "RED", "DARK")


def test_why_ladder_marks_price_flip_wall_breakeven():
    sigs = _full_signals()
    sigs["dealer_gamma"] = DealerGamma(gex_sign="NEG", flip_status="ok", flip_pct=-1.2,
                                       call_wall_pct=2.5, put_wall_pct=2.0)
    sigs["cost"] = Cost(guard="ok", ivr=40, breakeven_move_pct=0.9)
    vm = present("SPY", sigs, decide(sigs))
    labels = {m["label"]: m["pct"] for m in vm.why_ladder}
    assert labels["price"] == 0.0 and "gamma flip" in labels
    assert "wall" in labels and "breakeven" in labels


# (the old index.html one-svg parser test is superseded: the default-render chart
# budget is enforced at the ViewModel layer by tests/test_present_snapshot.py and
# in-page by static/js/uw-contract-tests.js)


def test_series_carry_chart_data_for_the_future_ui():
    """Every evidence tile must carry its chart-ready series (the raw data the future
    UI will draw): OI per day, cum-delta curve, gamma ladder, RR series, term curve,
    top strikes. Summaries alone are not enough to build the frontend on."""
    from server.pipeline.derive import derive_positioning
    from server.models import ContractOIBar
    sigs = _full_signals()
    bars = [ContractOIBar(date=f"2026-06-{d:02d}", open_interest=1000 + d * 100)
            for d in range(1, 7)]
    sigs["positioning"] = derive_positioning(
        {"flow_side": "call", "flow_strikes": [600.0], "contract_oi": [bars]})
    vm = present("SPY", sigs, decide(sigs))
    pos = next(e for e in vm.elements if e.key == "positioning")
    assert pos.series["kind"] == "bars"
    assert [p["oi"] for p in pos.series["points"]] == [1100, 1200, 1300, 1400, 1500, 1600]
    assert all(e.series is not None for e in vm.elements
               if e.key in ("direction", "conviction", "positioning", "structural", "skew", "cost"))


def test_direction_detail_lists_the_top_bets_as_receipts():
    sigs = _full_signals()
    sigs["flow"] = Flow(direction="calls", direction_basis="opening_flow",
                        call_prem=2e6, put_prem=1e5, lean_quality="qualified",
                        top_alerts=[{"time": "10:42", "type": "put", "strike": 580.0,
                                     "expiry": "2026-06-13", "premium": 1_200_000,
                                     "aggressor": "ask-side", "voi": 4.1, "sweep": True}])
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "direction")
    assert el.detail["Top bet 1"] == "10:42 · 580 put 06-13 · $1.2M · ask-side sweep · 4.1x OI"


def test_flow_timeline_is_a_second_tile_with_arrival_read():
    sigs = _full_signals()
    sigs["flow"] = Flow(direction="calls", direction_basis="opening_flow",
                        call_prem=2e6, put_prem=1e5, late_pct=62.0,
                        flow_series=[{"t": "09:35", "call": 100, "put": 0},
                                     {"t": "15:30", "call": 2_000_000, "put": 100_000}])
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "flow_timeline")
    assert el.surface == "BACK-LOADED"
    assert "62% of opening premium after 14:00 ET" in el.meaning
    assert el.series == {"kind": "two_line", "points": sigs["flow"].flow_series}
    # truncated pull: honesty over pattern — but ONLY when the window misses the morning
    sigs["flow"].truncated = True
    sigs["flow"].flow_series[0]["t"] = "11:02"          # demonstrably missing the open
    vm2 = present("SPY", sigs, decide(sigs))
    el2 = next(e for e in vm2.elements if e.key == "flow_timeline")
    assert el2.surface == "PARTIAL VIEW" and "window starts 11:02 ET" in el2.meaning
    # cap hit but pagination still reached the open -> full coverage, no false caveat
    sigs["flow"].flow_series[0]["t"] = "09:35"
    vm2b = present("SPY", sigs, decide(sigs))
    el2b = next(e for e in vm2b.elements if e.key == "flow_timeline")
    assert el2b.surface == "BACK-LOADED" and "missing" not in el2b.meaning
    # and no series -> no tile
    sigs["flow"].flow_series = []
    vm3 = present("SPY", sigs, decide(sigs))
    assert all(e.key != "flow_timeline" for e in vm3.elements)


def test_conviction_meaning_sized_against_share_volume():
    sigs = _full_signals()
    sigs["conviction"] = Conviction(direction="calls", dir_delta=350, accumulation="building",
                                    share_volume=10000.0, vol_ratio=0.035)
    vm = present("SPY", sigs, decide(sigs))
    el = next(e for e in vm.elements if e.key == "conviction")
    assert "3.5% of share volume = noticeable" in el.meaning   # number + scale word
    assert el.detail["Vs shares traded"].startswith("3.5% of today's 10K shares")
    # and without the yardstick the meaning stays size-free (no fake 0%)
    vm2 = present("SPY", _full_signals(), decide(_full_signals()))
    el2 = next(e for e in vm2.elements if e.key == "conviction")
    assert "share volume" not in el2.meaning


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
    assert el.detail["Max loss"].startswith("$468 per contract")   # premium = max loss, stated


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
