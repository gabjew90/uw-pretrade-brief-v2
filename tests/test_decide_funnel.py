"""decide tests — the FOUR-lights strict-conjunction verdict (directive 2026-06-12).

PERFECT iff every gate in the active branch is GREEN; else NOT NOW — n/N. Rigor lives
in sub-criteria INSIDE each gate; a measurable fail (RED) trumps an unknown (DARK).
Calls run 4 gates, puts add the no_squeeze hard veto (5). 'Mixed' and 'Favorable' are
BANNED. caps = non-green gate names (backtest histogram contract).
"""
from server.models import (Catalyst, Conviction, Cost, DealerGamma, Flow, Provenance,
                           Quality, Shorts, Vol)
from server.pipeline.decide import decide
from server.pipeline.gates import WAITING


def _stats(prem_c=5e6, prem_p=5e5, ask_c=0.8, ask_p=0.05, nvh=0.98, age=10.0):
    base = {"top2_share": 0.7, "top2_dte_ok": True, "strike_band_ok": True,
            "opening_share": 0.9, "net_vs_high": nvh, "last_age_min": age}
    return {"call": {**base, "opening_prem": prem_c, "ask_share": ask_c},
            "put": {**base, "opening_prem": prem_p, "ask_share": ask_p}}


def _all_green(direction="calls"):
    """A signal map where every gate of `direction` is GREEN (drift branch)."""
    side = "calls" if direction == "calls" else "puts"
    stats = _stats() if side == "calls" else _stats(prem_c=5e5, prem_p=5e6,
                                                    ask_c=0.05, ask_p=0.8)
    # one dict shared by contract + contracts[side] so tests that mutate cost.contract[...]
    # still drive the per-direction good_entry gate
    ctype = "call" if side == "calls" else "put"
    c_metrics = {"type": ctype, "strike": 600, "expiry": "2026-06-19", "dte": 7, "ask": 2.5,
                 "delta": 0.45, "theta_day_pct": 6.0, "spread_pct": 3.0,
                 "breakeven_move_pct": 0.8, "expected_move_pct": 1.5}
    return {
        "flow": Flow(direction=side, direction_basis="opening_flow",
                     call_prem=5e6 if side == "calls" else 5e5,
                     put_prem=5e5 if side == "calls" else 5e6,
                     lean_quality="qualified", side_stats=stats,
                     provenance=Provenance()),
        "conviction": Conviction(direction=side, dir_delta=100 if side == "calls" else -100),
        "dealer_gamma": DealerGamma(gex_sign="NEG", flip_status="ok", flip_pct=-1.2,
                                    call_wall_pct=2.5, put_wall_pct=2.5),
        "cost": Cost(guard="ok", ivr=25, spread_pct=3.0, breakeven_move_pct=0.8,
                     expected_move_pct=1.5, calendar_ok=True,
                     contract=c_metrics, contracts={ctype: c_metrics}),
        "vol": Vol(ivr=25, hv=0.22, iv_front=0.20, hv_iv_ratio=1.1, term_slope=0.01,
                   iv_spike_pct=2.0),
        "shorts": Shorts(ftd_latest=100, ftd_pctile=40.0),
    }


def _sigs(direction="calls", **over):
    s = _all_green(direction)
    s.update(over)
    return s


def _gate(v, direction, name):
    d = v.calls if direction == "calls" else v.puts
    return next(g for g in d.gates if g.name == name)


# ── PERFECT iff every gate GREEN; counts are 4 (calls) / 5 (puts) ─────────────
def test_all_green_calls_is_perfect_four_gates():
    v = decide(_sigs("calls"))
    assert v.overall == "PERFECT"
    assert v.action == "PERFECT — CALLS"
    assert v.calls.green == v.calls.total == 4
    assert [g.name for g in v.calls.gates] == ["smart_flow", "dealer_fuel",
                                               "cheap_vol", "good_entry"]
    assert v.caps == []


def test_all_green_puts_is_perfect_five_gates_with_veto():
    v = decide(_sigs("puts"))
    assert v.puts.state == "PERFECT"
    assert v.puts.total == 5
    assert v.puts.gates[-1].name == "no_squeeze"
    assert v.action == "PERFECT — PUTS"


def test_one_failed_subcriterion_reds_the_gate():
    s = _sigs("calls")
    s["vol"].ivr = 85                                   # one sub inside cheap_vol
    v = decide(s)
    assert v.action == "NOT NOW — 3/4"
    g = _gate(v, "calls", "cheap_vol")
    assert g.state == "RED" and "IV rank" in g.failed_subcriteria
    assert v.calls.waiting_on == WAITING["cheap_vol"]   # short name, not the label


def test_red_trumps_dark_inside_a_gate():
    """A measurable fail decides the gate even when another sub-input is unknown."""
    s = _sigs("calls")
    s["vol"].ivr = 85                                   # measurable fail
    s["vol"].hv_iv_ratio = None                         # unknown
    assert _gate(decide(s), "calls", "cheap_vol").state == "RED"


def test_one_dark_gate_blocks_perfect_and_is_named():
    s = _sigs("calls")
    s["vol"] = Vol(provenance=Provenance(quality=Quality.UNAVAILABLE))
    v = decide(s)
    assert v.overall == "NOT NOW"
    assert "cheap_vol" in v.caps
    assert _gate(v, "calls", "cheap_vol").state == "DARK"


def test_banned_words_never_appear():
    for sigmap in (_sigs("calls"), {"flow": Flow(direction=None)}, {}):
        v = decide(dict(sigmap))
        text = v.action + " " + " ".join(v.reasons)
        assert "Mixed" not in text and "Favorable" not in text
        assert v.overall in ("PERFECT", "NOT NOW")


# ── both directions; the puts veto names first ────────────────────────────────
def test_both_directions_always_evaluated():
    v = decide(_sigs("calls"))
    assert v.calls is not None and v.puts is not None
    assert v.puts.state == "NOT NOW"
    assert _gate(v, "puts", "smart_flow").state == "RED"   # flow leans calls


def test_put_squeeze_red_is_named_first():
    s = _sigs("puts")
    s["shorts"] = Shorts(ftd_latest=9999, ftd_pctile=99.0)   # FTDs extreme
    s["vol"].ivr = 85                                        # another gate also red
    v = decide(s)
    assert v.puts.waiting_on.startswith(WAITING["no_squeeze"])


def test_tape_divergence_lives_inside_no_squeeze():
    """The old conviction leg's only informative case: tape pushing UP while buying
    puts = hedger contamination → the squeeze veto reds."""
    s = _sigs("puts")
    s["conviction"] = Conviction(direction="calls", dir_delta=500)
    g = _gate(decide(s), "puts", "no_squeeze")
    assert g.state == "RED" and "tape sign" in g.failed_subcriteria


def test_mirror_direction_entry_is_dark_not_fabricated():
    v = decide(_sigs("calls"))
    assert _gate(v, "puts", "good_entry").state == "DARK"


# ── the old hard cases fold into NOT NOW, blocking gate first ─────────────────
def test_no_flow_is_not_now_with_flow_first():
    v = decide({"flow": Flow(direction=None, direction_basis="unavailable",
                             provenance=Provenance(quality=Quality.UNAVAILABLE,
                                                   note="no flow alerts"))})
    assert v.overall == "NOT NOW"
    assert v.reasons and WAITING["smart_flow"] in v.reasons[0]


# ── branch switch (acceptance #2) ─────────────────────────────────────────────
def test_earnings_inside_window_switches_to_catalyst_branch():
    s = _sigs("calls")
    s["catalyst"] = Catalyst(days_to_earnings=2, report_date="2026-06-17",
                             implied_move_pct=4.0, hist_move_pct=6.0, quarters=4,
                             ratio=0.67)
    v = decide(s)
    assert v.branch == "catalyst"
    assert [g.name for g in v.calls.gates] == ["cheap_event", "smart_flow", "good_entry"]
    # cheap_vol (and its clean-window sub) is drift-only
    s["cost"].contract["dte"] = 7                       # 5-15 DTE: expiry capture green
    v2 = decide(s)
    assert _gate(v2, "calls", "cheap_event").state == "GREEN"


def test_catalyst_below_min_quarters_is_dark():
    s = _sigs("calls")
    s["catalyst"] = Catalyst(days_to_earnings=2, report_date="2026-06-17",
                             implied_move_pct=4.0, hist_move_pct=6.0, quarters=2,
                             ratio=0.67)
    g = _gate(decide(s), "calls", "cheap_event")
    assert g.state == "DARK"                            # 2 quarters of history is fiction


# ── sub-criteria worth locking ────────────────────────────────────────────────
def test_faded_morning_burst_reds_smart_flow():
    """STILL BUILDING: a burst that reversed (net 60% of its session high) reads RED
    even when the daily totals clear every other bar — Hu's edge is ~1-day, staleness
    kills it."""
    s = _sigs("calls")
    s["flow"].side_stats = _stats(nvh=0.60, age=10.0)
    g = _gate(decide(s), "calls", "smart_flow")
    assert g.state == "RED" and "still building" in g.failed_subcriteria


def test_stale_last_print_reds_smart_flow():
    s = _sigs("calls")
    s["flow"].side_stats = _stats(nvh=0.98, age=240.0)   # 4h since the last print
    g = _gate(decide(s), "calls", "smart_flow")
    assert g.state == "RED" and "still building" in g.failed_subcriteria


def test_rolling_dominated_side_reds_smart_flow_fresh_money():
    """Explicit same-day vol/OI enforcement: a side whose opening flow is a MINORITY of its
    own activity (mostly rolling/closing) fails 'fresh money' even with a qualifying opening
    sliver — closes the contamination hole the opening pool alone left open."""
    s = _sigs("calls")
    s["flow"].side_stats["call"]["opening_share"] = 0.18    # 18% opening, 82% rolling
    g = _gate(decide(s), "calls", "smart_flow")
    assert g.state == "RED" and "fresh money" in g.failed_subcriteria


def test_weak_lean_reds_smart_flow():
    s = _sigs("calls")
    s["flow"].lean_quality = "weak"
    g = _gate(decide(s), "calls", "smart_flow")
    assert g.state == "RED" and "lean" in g.failed_subcriteria


def test_macro_print_only_reds_window_when_held_through():
    """Operator policy 2026-06-11 preserved inside cheap_vol's clean-window sub."""
    s = _sigs("calls")
    s["cost"].macro_days, s["cost"].macro_name = 3.0, "CPI"
    assert _gate(decide(s), "calls", "cheap_vol").state == "GREEN"
    s["cost"].macro_days = 0.5
    g = _gate(decide(s), "calls", "cheap_vol")
    assert g.state == "RED" and "clean window" in g.failed_subcriteria


def test_failed_calendar_darkens_cheap_vol_never_green():
    s = _sigs("calls")
    s["cost"].calendar_ok = False
    assert _gate(decide(s), "calls", "cheap_vol").state == "DARK"


def test_delta_floor_relaxes_only_when_fueled():
    s = _sigs("calls")
    s["cost"].contracts["call"]["delta"] = 0.37    # the per-direction pick the gate reads
    assert _gate(decide(s), "calls", "good_entry").state == "GREEN"   # fueled: floor 0.35
    s["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok", flip_pct=1.0,
                                    call_wall_pct=2.5, put_wall_pct=2.5)
    g = _gate(decide(s), "calls", "good_entry")
    assert g.state == "RED" and "delta band" in g.failed_subcriteria  # unfueled: 0.40


def test_caps_lists_every_non_green_gate():
    s = _sigs("calls")
    s["vol"] = Vol(ivr=85, hv=0.1, iv_front=0.20, hv_iv_ratio=0.5, term_slope=-0.02)
    s["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok", flip_pct=1.0)
    v = decide(s)
    assert {"cheap_vol", "dealer_fuel"} <= set(v.caps)
