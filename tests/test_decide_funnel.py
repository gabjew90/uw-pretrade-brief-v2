"""decide tests — the strict-conjunction verdict (directive 2026-06-12).

PERFECT iff every gate in the active branch is GREEN; anything else NOT NOW — n/N.
DARK counts as not-green but is distinct from RED. Both directions every cycle.
'Mixed' and 'Favorable' are BANNED words. caps = non-green gate names (backtest
histogram contract).
"""
from server.models import (Catalyst, Conviction, Cost, DealerGamma, Flow, Provenance,
                           Quality, Shorts, Skew, Vol)
from server.pipeline.decide import decide
from server.pipeline.gates import LABELS


def _stats(prem_c=5e6, prem_p=5e5, ask_c=0.8, ask_p=0.05):
    return {"call": {"opening_prem": prem_c, "ask_share": ask_c, "top2_share": 0.7,
                     "top2_dte_ok": True, "strike_band_ok": True},
            "put": {"opening_prem": prem_p, "ask_share": ask_p, "top2_share": 0.7,
                    "top2_dte_ok": True, "strike_band_ok": True}}


def _all_green(direction="calls"):
    """A signal map where every CALLS drift gate is GREEN (puts mirror by flipping)."""
    side = "calls" if direction == "calls" else "puts"
    stats = _stats() if side == "calls" else _stats(prem_c=5e5, prem_p=5e6,
                                                    ask_c=0.05, ask_p=0.8)
    return {
        "flow": Flow(direction=side, direction_basis="opening_flow",
                     call_prem=5e6 if side == "calls" else 5e5,
                     put_prem=5e5 if side == "calls" else 5e6,
                     lean_quality="qualified", side_stats=stats,
                     provenance=Provenance()),
        "positioning": None,                     # unconfirmed passes (archive-decoupled)
        "conviction": Conviction(direction=side, dir_delta=100 if side == "calls" else -100),
        "dealer_gamma": DealerGamma(gex_sign="NEG", flip_status="ok", flip_pct=-1.2,
                                    call_wall_pct=2.5, put_wall_pct=2.5),
        "skew": Skew(rr25=0.0, rr_baseline=0.0,
                     rr_delta=0.02 if side == "calls" else -0.02, lean="neutral"),
        "cost": Cost(guard="ok", ivr=25, spread_pct=3.0, breakeven_move_pct=0.8,
                     expected_move_pct=1.5, calendar_ok=True,
                     contract={"type": "call" if side == "calls" else "put", "strike": 600,
                               "expiry": "2026-06-19", "dte": 7, "ask": 2.5,
                               "delta": 0.45, "theta_day_pct": 6.0}),
        "vol": Vol(ivr=25, hv=0.22, iv_front=0.20, hv_iv_ratio=1.1, term_slope=0.01,
                   iv_spike_pct=2.0),
        "shorts": Shorts(ratio_latest=0.55, ratio_prev=0.50, rising=True, ftd_pctile=40.0),
    }


def _sigs(direction="calls", **over):
    s = {k: v for k, v in _all_green(direction).items() if v is not None}
    s.update(over)
    return s


# ── PERFECT iff every gate GREEN ──────────────────────────────────────────────
def test_all_green_calls_is_perfect():
    v = decide(_sigs("calls"))
    assert v.overall == "PERFECT"
    assert v.action == "PERFECT — CALLS"
    assert v.calls.state == "PERFECT" and v.calls.green == v.calls.total == 10
    assert v.caps == []


def test_all_green_puts_is_perfect_with_extra_gates():
    v = decide(_sigs("puts"))
    assert v.puts.state == "PERFECT"
    assert v.puts.total == 13                    # drift 10 + P1/P2/P3
    assert v.action == "PERFECT — PUTS"


def test_one_red_gate_is_not_now_with_count():
    s = _sigs("calls")
    s["vol"] = Vol(ivr=85, hv=0.22, iv_front=0.20, hv_iv_ratio=1.1, term_slope=0.01)
    v = decide(s)
    assert v.overall == "NOT NOW"
    assert v.action == "NOT NOW — 9/10"
    assert v.caps == ["cheap_vol"]
    assert LABELS["cheap_vol"] in v.calls.waiting_on


def test_one_dark_gate_blocks_perfect_and_is_named():
    """DARK counts as not-green (conservatism) — an unknown can never certify PERFECT."""
    s = _sigs("calls")
    s["skew"] = Skew(lean="unavailable",
                     provenance=Provenance(quality=Quality.UNAVAILABLE))
    v = decide(s)
    assert v.overall == "NOT NOW"
    assert "skew_shift" in v.caps
    sk = next(g for g in v.calls.gates if g.name == "skew_shift")
    assert sk.state == "DARK"


def test_banned_words_never_appear():
    for sigmap in (_sigs("calls"), {"flow": Flow(direction=None)}, {}):
        v = decide(dict(sigmap))
        text = v.action + " " + " ".join(v.reasons)
        assert "Mixed" not in text and "Favorable" not in text
        assert v.overall in ("PERFECT", "NOT NOW")


# ── both directions, every cycle ──────────────────────────────────────────────
def test_both_directions_always_evaluated():
    v = decide(_sigs("calls"))
    assert v.calls is not None and v.puts is not None
    assert v.puts.state == "NOT NOW"             # flow leans calls → puts dominance RED
    dom = next(g for g in v.puts.gates if g.name == "flow_dominance")
    assert dom.state == "RED"


def test_mirror_direction_cost_is_dark_not_fabricated():
    """No contract is priced for the non-flow side — its cost gate is DARK, never a
    guessed GREEN."""
    v = decide(_sigs("calls"))
    cost_p = next(g for g in v.puts.gates if g.name == "cost")
    assert cost_p.state == "DARK"


# ── the old hard cases fold into NOT NOW, blocking gate named first ───────────
def test_no_flow_is_not_now_with_flow_gate_first():
    v = decide({"flow": Flow(direction=None, direction_basis="unavailable",
                             provenance=Provenance(quality=Quality.UNAVAILABLE,
                                                   note="no flow alerts"))})
    assert v.overall == "NOT NOW"
    assert v.reasons and v.reasons[0].startswith(LABELS["flow_dominance"])


def test_put_squeeze_red_is_named_first():
    s = _sigs("puts")
    s["shorts"] = Shorts(ratio_latest=0.55, ratio_prev=0.50, rising=True,
                         ftd_pctile=99.0)       # FTDs extreme → squeeze trap RED
    v = decide(s)
    assert v.puts.state == "NOT NOW"
    assert v.puts.waiting_on.startswith(LABELS["no_squeeze"])


# ── branch switch (acceptance #2) ─────────────────────────────────────────────
def test_earnings_inside_window_switches_to_catalyst_branch():
    s = _sigs("calls")
    s["catalyst"] = Catalyst(days_to_earnings=2, report_date="2026-06-17",
                             implied_move_pct=4.0, hist_move_pct=6.0, quarters=4,
                             ratio=0.67)
    v = decide(s)
    assert v.branch == "catalyst"
    names = [g.name for g in v.calls.gates]
    assert "cheap_implied_move" in names and "expiry_capture" in names
    assert "clean_window" not in names           # G10 is drift-only


def test_catalyst_cheap_move_gate_dark_below_min_quarters():
    s = _sigs("calls")
    s["catalyst"] = Catalyst(days_to_earnings=2, report_date="2026-06-17",
                             implied_move_pct=4.0, hist_move_pct=6.0, quarters=2,
                             ratio=0.67)
    v = decide(s)
    c1 = next(g for g in v.calls.gates if g.name == "cheap_implied_move")
    assert c1.state == "DARK"                    # 2 quarters of history is fiction


# ── specific gate semantics worth locking ─────────────────────────────────────
def test_weak_lean_reds_dominance():
    s = _sigs("calls")
    s["flow"].lean_quality = "weak"
    v = decide(s)
    assert next(g for g in v.calls.gates if g.name == "flow_dominance").state == "RED"


def test_macro_print_only_reds_window_when_held_through():
    """Operator policy 2026-06-11 preserved: a CPI 3 days out does NOT red the window
    (you can exit before it); a print <=1 day out does (you'd hold through it)."""
    s = _sigs("calls")
    s["cost"].macro_days, s["cost"].macro_name = 3.0, "CPI"
    assert next(g for g in decide(s).calls.gates
                if g.name == "clean_window").state == "GREEN"
    s["cost"].macro_days = 0.5
    assert next(g for g in decide(s).calls.gates
                if g.name == "clean_window").state == "RED"


def test_failed_calendar_darkens_window_never_green():
    s = _sigs("calls")
    s["cost"].calendar_ok = False
    assert next(g for g in decide(s).calls.gates
                if g.name == "clean_window").state == "DARK"


def test_pos_gamma_reds_fuel_and_relaxed_delta_floor_only_when_fueled():
    s = _sigs("calls")
    s["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok", flip_pct=1.0,
                                    call_wall_pct=2.5, put_wall_pct=2.5)
    v = decide(s)
    assert next(g for g in v.calls.gates if g.name == "dealer_fuel").state == "RED"
    # delta 0.37 passes only with fuel: without it the floor is 0.40 → cost RED too
    s["cost"].contract["delta"] = 0.37
    v2 = decide(s)
    assert next(g for g in v2.calls.gates if g.name == "cost").state == "RED"
    s["dealer_gamma"] = DealerGamma(gex_sign="NEG", flip_status="ok", flip_pct=-1.2,
                                    call_wall_pct=2.5, put_wall_pct=2.5)
    v3 = decide(s)
    assert next(g for g in v3.calls.gates if g.name == "cost").state == "GREEN"


def test_caps_lists_every_non_green_gate():
    s = _sigs("calls")
    s["vol"] = Vol(ivr=85, hv=0.1, iv_front=0.20, hv_iv_ratio=0.5, term_slope=-0.02)
    s["dealer_gamma"] = DealerGamma(gex_sign="POS", flip_status="ok", flip_pct=1.0)
    v = decide(s)
    assert {"cheap_vol", "term_slope", "dealer_fuel"} <= set(v.caps)


def test_skew_shift_is_directional():
    s = _sigs("calls")
    s["skew"] = Skew(rr25=0.0, rr_baseline=0.0, rr_delta=-0.02, lean="put_skew")
    v = decide(s)
    assert next(g for g in v.calls.gates if g.name == "skew_shift").state == "RED"
    assert next(g for g in v.puts.gates if g.name == "skew_shift").state == "GREEN"
