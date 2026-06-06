"""Pure verdict synthesis for the deep-dive (signal-honesty Plan 3). NO I/O —
callers pass already-fetched data in. Derives 25-delta risk-reversal skew from
greeks, then assembles the 3-leg verdict (Positioning / Structural / Skew) +
Cost guard + signal_conflict + overall + action.

Skew is asymmetric by design: opposition is the load-bearing caution; agreement
is mild corroboration (never a peer green). Positioning collapses Flow+OI and
only reaches green on the OPENING-flow basis (the weaker total_flow caps at
yellow), so a Favorable verdict can't rest on the fallback."""
from __future__ import annotations

_SKEW_THR = 0.02          # IV points: |RR25| below this is noise, not a signal
_DELTA_TOL = 0.10         # how far from ±0.25 a strike may be to count as the 25d leg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive_rr25(greeks_rows: list[dict], tol: float = _DELTA_TOL) -> float | None:
    """25-delta risk reversal = IV at the strike nearest +0.25 call_delta minus IV
    at the strike nearest -0.25 put_delta. None when the chain has no strike within
    `tol` of ±0.25 on either side (too sparse / too short-dated). Sign by
    construction: >0 call-skew (bullish lean), <0 put-skew (defensive)."""
    best_c = best_p = None   # (delta_distance, iv)
    for r in (greeks_rows or []):
        cd, cv = _f(r.get("call_delta")), _f(r.get("call_volatility"))
        pd, pv = _f(r.get("put_delta")), _f(r.get("put_volatility"))
        if cd is not None and cv is not None:
            d = abs(cd - 0.25)
            if d <= tol and (best_c is None or d < best_c[0]):
                best_c = (d, cv)
        if pd is not None and pv is not None:
            d = abs(pd - (-0.25))
            if d <= tol and (best_p is None or d < best_p[0]):
                best_p = (d, pv)
    if best_c is None or best_p is None:
        return None
    return best_c[1] - best_p[1]


def extract_vendor_rr(payload) -> float | None:
    """Latest 25Δ risk reversal from UW's historical-risk-reversal-skew payload,
    SIGN-CORRECTED to this module's convention. UW's `risk_reversal` is put_IV −
    call_IV (positive = put-skew); skew_state expects positive = call-skew, so we
    NEGATE. (Pinned by cross-check: vendor +0.051 ≈ −(derived call−put −0.060) for
    SPY @ the same expiry.) None on empty/failure. Rows are date-ordered; take the
    last (most recent)."""
    if not isinstance(payload, dict):
        return None
    rows = [r for r in (payload.get("data") or []) if isinstance(r, dict)]
    if not rows:
        return None
    v = rows[-1].get("risk_reversal")
    try:
        return -float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def skew_state(rr25: float | None, direction: str, *, thr: float = _SKEW_THR) -> str:
    """agree | oppose | neutral | unavailable. calls want RR>0 (call-skew); puts
    want RR<0 (put-skew). Asymmetric oppose-veto handled by the caller (agreement
    is subordinate corroboration, never a peer green)."""
    if rr25 is None:
        return "unavailable"
    if abs(rr25) < thr:
        return "neutral"
    bullish = rr25 > 0
    if direction == "calls":
        return "agree" if bullish else "oppose"
    return "oppose" if bullish else "agree"   # puts


def positioning_leg(direction_basis: str, flow_gate: str, oi_confirmation: str) -> str:
    """green / yellow / red. Collapses Flow+OI into the predictive core. Green
    requires the OPENING basis (not the weaker total_flow fallback) so a Favorable
    verdict can't rest on it; archive-decoupled (green even when OI 'unconfirmed');
    'unwinding' caps green→yellow."""
    if direction_basis == "gamma_fallback" or flow_gate == "red":
        return "red"
    if direction_basis == "total_flow":
        return "yellow"                       # weaker basis — caps below Favorable
    # opening_flow:
    if flow_gate == "green" and oi_confirmation != "unwinding":
        return "green"
    return "yellow"


def compute_verdict(*, direction: str, direction_basis: str, flow_gate: str,
                    structural_gate: str, oi_confirmation: str,
                    rr25: float | None, cost_gate: str) -> dict:
    """Assemble the 3-leg verdict + cost guard + signal_conflict + overall + action.
    Skew `agree` never upgrades to Favorable on its own; signal_conflict (structural
    red or skew oppose vs a directional positioning) caps overall to at most Mixed
    and never flips the side (positioning owns the side)."""
    positioning = positioning_leg(direction_basis, flow_gate, oi_confirmation)
    skew = skew_state(rr25, direction)
    cost_guard = {"green": "ok", "yellow": "caution", "red": "block"}.get(cost_gate, "caution")

    conflict_legs: list[str] = []
    if positioning in ("green", "yellow"):           # only conflicts when we have a side
        if structural_gate == "red":
            conflict_legs.append("structural")
        if skew == "oppose":
            conflict_legs.append("skew")
    signal_conflict = bool(conflict_legs)

    if positioning == "red" or cost_guard == "block":
        overall = "Stand down"
    elif positioning == "green" and not signal_conflict and cost_guard == "ok" and skew != "oppose":
        overall = "Favorable"
    else:
        overall = "Mixed"

    if overall == "Favorable":
        action = "Worth acting on — the rare one"
    elif overall == "Stand down":
        action = "Stand down"
    elif signal_conflict:
        action = "Skip — signals disagree"
    else:
        action = "Wait — not compelling"

    return {
        "positioning": positioning, "structural": structural_gate, "skew": skew,
        "cost_guard": cost_guard, "signal_conflict": signal_conflict,
        "conflict_legs": conflict_legs, "overall": overall, "action": action,
        "rr25": rr25,
    }
