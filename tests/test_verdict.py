import json
from pathlib import Path

from server import verdict

_GREEKS = Path(__file__).parent / "fixtures" / "golden" / "greeks.json"


# ---------- derive_rr25 ----------
def test_derive_rr25_exact_from_synthetic_rows():
    rows = [
        {"call_delta": "0.50", "put_delta": "-0.50", "call_volatility": "0.30", "put_volatility": "0.40"},
        {"call_delta": "0.26", "put_delta": "-0.24", "call_volatility": "0.18", "put_volatility": "0.23"},  # nearest 25d
        {"call_delta": "0.10", "put_delta": "-0.90", "call_volatility": "0.15", "put_volatility": "0.50"},
    ]
    # RR25 = call_vol(0.26 leg) - put_vol(-0.24 leg) = 0.18 - 0.23 = -0.05
    assert abs(verdict.derive_rr25(rows) - (-0.05)) < 1e-9


def test_derive_rr25_none_when_no_strike_near_25d():
    rows = [{"call_delta": "0.95", "put_delta": "-0.95", "call_volatility": "0.2", "put_volatility": "0.2"}]
    assert verdict.derive_rr25(rows, tol=0.10) is None


def test_derive_rr25_runs_on_real_greeks_payload():
    rows = json.loads(_GREEKS.read_text(encoding="utf-8")).get("data")
    rr = verdict.derive_rr25(rows)
    assert rr is None or isinstance(rr, float)   # real-shape smoke: no crash on live columns


# ---------- skew_state ----------
def test_skew_state_calls():
    assert verdict.skew_state(0.05, "calls") == "agree"
    assert verdict.skew_state(-0.05, "calls") == "oppose"
    assert verdict.skew_state(0.001, "calls") == "neutral"
    assert verdict.skew_state(None, "calls") == "unavailable"


def test_skew_state_puts_mirrored():
    assert verdict.skew_state(-0.05, "puts") == "agree"
    assert verdict.skew_state(0.05, "puts") == "oppose"


# ---------- positioning_leg ----------
def test_positioning_green_only_on_opening_flow_even_without_archive():
    assert verdict.positioning_leg("opening_flow", "green", "unconfirmed") == "green"


def test_positioning_total_flow_caps_at_yellow():
    assert verdict.positioning_leg("total_flow", "green", "building") == "yellow"


def test_positioning_unwinding_caps_green_to_yellow():
    assert verdict.positioning_leg("opening_flow", "green", "unwinding") == "yellow"


def test_positioning_gamma_fallback_is_red():
    assert verdict.positioning_leg("gamma_fallback", "green", "building") == "red"


def test_positioning_weak_flow_yellow():
    assert verdict.positioning_leg("opening_flow", "yellow", "building") == "yellow"


# ---------- compute_verdict ----------
def _v(**kw):
    base = dict(direction="calls", direction_basis="opening_flow", flow_gate="green",
                structural_gate="green", oi_confirmation="building", rr25=0.05, cost_gate="green")
    base.update(kw)
    return verdict.compute_verdict(**base)


def test_compute_verdict_favorable_happy_path():
    v = _v()
    assert v["positioning"] == "green" and v["overall"] == "Favorable"
    assert v["action"].startswith("Worth acting on")
    assert v["signal_conflict"] is False


def test_compute_verdict_skew_oppose_conflicts_and_caps():
    v = _v(rr25=-0.05)                       # put-skew vs long calls → oppose
    assert v["skew"] == "oppose"
    assert v["signal_conflict"] is True and "skew" in v["conflict_legs"]
    assert v["overall"] == "Mixed" and v["action"] == "Skip — signals disagree"


def test_compute_verdict_skew_agree_never_favorable_alone():
    v = _v(direction_basis="total_flow", rr25=0.05)
    assert v["skew"] == "agree" and v["overall"] != "Favorable"


def test_compute_verdict_cost_block_stand_down():
    v = _v(cost_gate="red")
    assert v["cost_guard"] == "block" and v["overall"] == "Stand down"
    assert v["action"] == "Stand down"


def test_compute_verdict_structural_red_conflict():
    v = _v(structural_gate="red")
    assert v["signal_conflict"] is True and "structural" in v["conflict_legs"]
    assert v["overall"] == "Mixed"
