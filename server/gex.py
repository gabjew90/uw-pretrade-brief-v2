"""Shared gamma-exposure level math: gamma flip, call/put walls, regime sign.

Operates on a list of "rungs" — one per strike — each a dict with:
  strike, net (call_gamma + put_gamma, signed), call_gamma, put_gamma.

Centralizes the logic the 2026-05-30 debugging fixed, so the snapshot path
(_extract_gex) and the on-demand Tile 3 detail share ONE implementation:
  - flip   = cumulative-net zero-crossing NEAREST spot ("no_flip" if none);
  - walls  = max call-γ strike above spot / max |put-γ| strike below spot
             (NOT max|net| — net peaks at-the-money);
  - regime = sign of aggregate net γ.
"""
from __future__ import annotations


def compute_levels(rungs: list[dict], spot: float) -> dict:
    """rungs need 'strike' + 'net'; 'call_gamma'/'put_gamma' improve the walls.
    Returns flip_pct, flip_status, call_wall_pct, put_wall_pct, gex_sign, agg_b.
    Assumes rungs is non-empty and spot > 0 (callers handle 'unavailable')."""
    rs = sorted(rungs, key=lambda r: r["strike"])

    # Flip: nearest-spot cumulative zero-crossing.
    cum = 0.0
    crossings: list[float] = []
    prev_sign = None
    for r in rs:
        cum += r["net"]
        sign = 1 if cum >= 0 else -1
        if prev_sign is not None and sign != prev_sign:
            crossings.append(r["strike"])
        prev_sign = sign
    if crossings:
        flip = min(crossings, key=lambda k: abs(k - spot))
        flip_status = "ok"
    else:
        flip = spot
        flip_status = "no_flip"

    # Walls: call γ above spot / |put γ| below spot, kept separate (net magnitude
    # peaks ATM and would collapse both onto spot).
    def _cg(r):
        return float(r.get("call_gamma", 0) or 0)

    def _pg(r):
        return abs(float(r.get("put_gamma", 0) or 0))

    above = [r for r in rs if r["strike"] > spot]
    below = [r for r in rs if r["strike"] < spot]
    cwall = max(above, key=_cg, default=None)
    pwall = max(below, key=_pg, default=None)
    cwall_k = cwall["strike"] if cwall else spot * 1.05
    pwall_k = pwall["strike"] if pwall else spot * 0.95

    agg = sum(r["net"] for r in rs) / 1e9
    return {
        "flip_pct": (flip - spot) / spot * 100,
        "flip_status": flip_status,
        "call_wall_pct": (cwall_k - spot) / spot * 100,
        "put_wall_pct": (spot - pwall_k) / spot * 100,
        "gex_sign": "POS" if agg >= 0 else "NEG",
        "agg_b": agg,
    }
