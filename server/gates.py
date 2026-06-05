"""Gate computation. v2 uses cross-sectional/absolute thresholds; v0.2 will
swap to percentile-based for tickers with ≥30 days of archive history (spec §8b).

The (row, history=None) signature is forward-compatible: v0.2 percentile
variants slot in alongside the existing fallback functions."""
from __future__ import annotations
from typing import Literal

from server.history import TickerHistory


GATE_THRESHOLDS = {
    "flow_rank_green_cross_pct": 15,
    "flow_rank_yellow_cross_pct": 35,
    "oi_green_pct": 20,
    "oi_yellow_pct": 5,
    "oi_red_pct": -5,
    "gate3_flip_dist_pct": 1.5,
    "gate3_wall_dist_pct": 1.0,
    "ivr_green_max": 60,
    "ivr_yellow_max": 80,
    "earnings_days_min": 7,
}

Color = Literal["green", "yellow", "red"]
Method = Literal["cross_sectional", "absolute", "percentile"]


def compute_gates(row: dict, history: TickerHistory | None = None) -> dict[str, Color]:
    return {
        "flow":       _flow_gate(row, history),
        "oi":         _oi_gate(row, history),
        "structural": _structural_gate(row),
        "cost":       _cost_gate(row),
    }


def compute_gate_method(row: dict, history: TickerHistory | None = None) -> dict[str, Method]:
    flow_method: Method = "percentile" if _can_use_percentile(history, "flow") else "cross_sectional"
    oi_method: Method = "percentile" if _can_use_percentile(history, "oi") else "absolute"
    return {
        "flow": flow_method,
        "oi": oi_method,
        "structural": "absolute",
        "cost": "percentile",
    }


def _can_use_percentile(history: TickerHistory | None, gate: str) -> bool:
    if history is None:
        return False
    try:
        return history.days_available(gate) >= 30
    except NotImplementedError:
        return False


def _flow_gate(row: dict, history: TickerHistory | None) -> Color:
    if _can_use_percentile(history, "flow"):
        return _flow_gate_percentile(row, history)
    return _flow_gate_cross_sectional(row)


def _flow_gate_cross_sectional(row: dict) -> Color:
    pct = row.get("flow_rank_cross", 100)
    if pct <= GATE_THRESHOLDS["flow_rank_green_cross_pct"]:
        return "green"
    if pct <= GATE_THRESHOLDS["flow_rank_yellow_cross_pct"]:
        return "yellow"
    return "red"


def _flow_gate_percentile(row: dict, history: TickerHistory) -> Color:
    today_score = row.get("flow_composite_score", 0.0)
    pct = history.percentile(today_score, window_days=60)
    if pct >= 85: return "green"
    if pct >= 65: return "yellow"
    return "red"


def _oi_gate(row: dict, history: TickerHistory | None) -> Color:
    if _can_use_percentile(history, "oi"):
        return _oi_gate_percentile(row, history)
    return _oi_gate_absolute(row)


def _oi_gate_absolute(row: dict) -> Color:
    strikes = row.get("oi", {}).get("strikes", [])
    if not strikes:
        return "yellow"
    max_pct = max(s.get("pct", 0) for s in strikes)
    min_pct = min(s.get("pct", 0) for s in strikes)
    if max_pct >= GATE_THRESHOLDS["oi_green_pct"]:
        return "green"
    if min_pct <= GATE_THRESHOLDS["oi_red_pct"]:
        return "red"
    if max_pct >= GATE_THRESHOLDS["oi_yellow_pct"]:
        return "yellow"
    return "yellow"


def _oi_gate_percentile(row: dict, history: TickerHistory) -> Color:
    strikes = row.get("oi", {}).get("strikes", [])
    if not strikes:
        return "yellow"
    today_top = max(abs(s.get("pct", 0)) for s in strikes)
    pct = history.oi_change_percentile(today_top, window_days=60)
    if pct >= 90: return "green"
    if pct >= 70: return "yellow"
    return "red"


def _structural_gate(row: dict) -> Color:
    flip = abs(row.get("flip_dist_pct", 99))
    direction = row.get("direction", "calls")
    dir_wall_pct = (row.get("wall_up_dist_pct", 99) if direction == "calls"
                    else row.get("wall_dn_dist_pct", 99))
    if dir_wall_pct <= GATE_THRESHOLDS["gate3_wall_dist_pct"]:
        return "red"
    if flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"]:
        color: Color = "green"
    elif flip <= GATE_THRESHOLDS["gate3_flip_dist_pct"] * 2:
        color = "yellow"
    else:
        return "red"
    # Regime cap: positive dealer gamma (POS = long γ → chop/pin, premium-killing)
    # can't justify a GREEN directional-weekly structural read. Cap at yellow.
    if color == "green" and row.get("gex_sign") == "POS":
        return "yellow"
    return color


def _cost_gate(row: dict) -> Color:
    ivr = row.get("ivr", 100)
    days = row.get("days_to_earnings", 99)
    if days is not None and days < GATE_THRESHOLDS["earnings_days_min"]:
        return "red"
    if ivr <= GATE_THRESHOLDS["ivr_green_max"]:
        return "green"
    if ivr <= GATE_THRESHOLDS["ivr_yellow_max"]:
        return "yellow"
    return "red"


def derive_direction(flow_alerts, gex_sign: str, flip_pct: float) -> tuple[str, str]:
    """Pick the call/put side. OPENING flow leads (Ge-Lin-Pearson: opening bets
    predict, closing bets don't); fall back to total signed flow (Pan-Poteshman),
    then to the legacy gamma rule. Returns (direction, direction_basis) where
    basis is 'opening_flow' | 'total_flow' | 'gamma_fallback'. Pure; reads
    attributes tolerantly so it works on FlowAlert objects or dicts."""
    def _prem(want_type, opening_only):
        out = 0.0
        for x in flow_alerts or []:
            typ = getattr(x, "type", None) if not isinstance(x, dict) else x.get("type")
            opn = getattr(x, "all_opening_trades", False) if not isinstance(x, dict) else x.get("all_opening_trades", False)
            prem = getattr(x, "total_premium", 0.0) if not isinstance(x, dict) else x.get("total_premium", 0.0)
            if typ == want_type and (opn or not opening_only):
                out += float(prem or 0.0)
        return out

    open_call, open_put = _prem("call", True), _prem("put", True)
    if open_call or open_put:
        return ("calls" if open_call >= open_put else "puts", "opening_flow")

    tot_call, tot_put = _prem("call", False), _prem("put", False)
    if tot_call or tot_put:
        return ("calls" if tot_call >= tot_put else "puts", "total_flow")

    # legacy gamma rule, last resort, never silent (basis says so)
    if gex_sign == "NEG" or flip_pct > 0:
        return ("calls", "gamma_fallback")
    return ("puts", "gamma_fallback")
