"""Gate computation: v2 thresholds + history-aware signature for v0.2 evolution."""
from __future__ import annotations
import pytest

from server import gates
from server.history import TickerHistory


def _row(**overrides):
    base = {
        "ticker": "TEST",
        "direction": "calls",
        "spot": 100.0,
        "flow_rank_cross": 10,
        "oi": {"strikes": [{"strike": 100, "pct": 25}]},
        "flip_dist_pct": 1.0,
        "wall_up_dist_pct": 5.0,
        "wall_dn_dist_pct": 5.0,
        "ivr": 40,
        "days_to_earnings": 30,
    }
    base.update(overrides)
    return base


def test_flow_gate_green_when_rank_in_top_15_cross_pct():
    assert gates._flow_gate(_row(flow_rank_cross=10), history=None) == "green"


def test_flow_gate_yellow_in_top_35_cross_pct():
    assert gates._flow_gate(_row(flow_rank_cross=30), history=None) == "yellow"


def test_flow_gate_red_beyond_top_35():
    assert gates._flow_gate(_row(flow_rank_cross=50), history=None) == "red"


def test_flow_gate_boundary_at_exactly_15_is_green():
    assert gates._flow_gate(_row(flow_rank_cross=15), history=None) == "green"


def test_oi_gate_green_when_any_strike_above_20_pct():
    row = _row(oi={"strikes": [{"strike": 100, "pct": 5}, {"strike": 105, "pct": 25}]})
    assert gates._oi_gate(row, history=None) == "green"


def test_oi_gate_red_when_all_strikes_closing():
    row = _row(oi={"strikes": [{"strike": 100, "pct": -10}, {"strike": 105, "pct": -8}]})
    assert gates._oi_gate(row, history=None) == "red"


def test_oi_gate_yellow_between_5_and_20():
    row = _row(oi={"strikes": [{"strike": 100, "pct": 12}]})
    assert gates._oi_gate(row, history=None) == "yellow"


def test_oi_gate_handles_empty_strikes_as_yellow_placeholder():
    assert gates._oi_gate(_row(oi={"strikes": []}), history=None) == "yellow"


def test_structural_gate_green_when_flip_close_and_wall_clear():
    row = _row(flip_dist_pct=1.0, wall_up_dist_pct=3.0, wall_dn_dist_pct=3.0)
    assert gates._structural_gate(row) == "green"


def test_structural_gate_red_when_wall_within_one_pct_of_trade_direction():
    row = _row(direction="calls", flip_dist_pct=1.0, wall_up_dist_pct=0.5)
    assert gates._structural_gate(row) == "red"


def test_cost_gate_green_when_ivr_under_60_and_no_near_earnings():
    assert gates._cost_gate(_row(ivr=50, days_to_earnings=30)) == "green"


def test_cost_gate_red_when_earnings_within_7_days():
    assert gates._cost_gate(_row(ivr=30, days_to_earnings=3)) == "red"


def test_cost_gate_yellow_when_ivr_between_60_and_80():
    assert gates._cost_gate(_row(ivr=70, days_to_earnings=30)) == "yellow"


def test_compute_gates_accepts_history_none():
    result = gates.compute_gates(_row())
    assert set(result.keys()) == {"flow", "oi", "structural", "cost"}


def test_compute_gates_accepts_history_object_but_falls_back_in_v2():
    """v2: TickerHistory exists but gates ignore it (days_available not consulted)."""
    history = TickerHistory("TEST")
    result = gates.compute_gates(_row(), history=history)
    assert result["flow"] in {"green", "yellow", "red"}


def test_compute_gate_method_returns_v2_methods():
    result = gates.compute_gate_method(_row(), history=None)
    assert result == {
        "flow": "cross_sectional",
        "oi": "absolute",
        "structural": "absolute",
        "cost": "percentile",
    }


# ---------- derive_direction tests ----------
from types import SimpleNamespace


def _fa(type_, premium, opening):
    # mimics a FlowAlert: .type, .total_premium, .all_opening_trades
    return SimpleNamespace(type=type_, total_premium=premium, all_opening_trades=opening)


def test_direction_opening_flow_leads_calls():
    alerts = [_fa("call", 900_000, True), _fa("put", 100_000, True),
              _fa("put", 5_000_000, False)]  # huge CLOSING put must NOT flip the side
    d, basis = gates.derive_direction(alerts, gex_sign="POS", flip_pct=-1.0)
    assert d == "calls" and basis == "opening_flow"


def test_direction_opening_flow_leads_puts():
    alerts = [_fa("put", 800_000, True), _fa("call", 200_000, True)]
    d, basis = gates.derive_direction(alerts, gex_sign="NEG", flip_pct=1.0)
    assert d == "puts" and basis == "opening_flow"


def test_direction_falls_back_to_total_flow_when_no_opening():
    alerts = [_fa("call", 300_000, False), _fa("put", 900_000, False)]  # none opening
    d, basis = gates.derive_direction(alerts, gex_sign="NEG", flip_pct=1.0)
    assert d == "puts" and basis == "total_flow"


def test_direction_falls_back_to_gamma_when_no_flow():
    d, basis = gates.derive_direction([], gex_sign="NEG", flip_pct=-1.0)
    assert d == "calls" and basis == "gamma_fallback"          # NEG -> calls (old rule)
    d2, basis2 = gates.derive_direction([], gex_sign="POS", flip_pct=-1.0)
    assert d2 == "puts" and basis2 == "gamma_fallback"


def test_direction_opening_tie_breaks_to_calls_but_basis_is_opening():
    alerts = [_fa("call", 500_000, True), _fa("put", 500_000, True)]
    d, basis = gates.derive_direction(alerts, gex_sign="POS", flip_pct=-1.0)
    assert d == "calls" and basis == "opening_flow"   # tie -> calls, still opening-based
