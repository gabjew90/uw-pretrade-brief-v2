"""Stage 3 — DERIVE → signals, as PURE functions.

Each signal is a pure function: canonical records in, a typed `Signal` out, NO I/O
(no fetch, no clock-via-now, no storage). Time comes in as an argument (from the
clock) so the function stays deterministic and golden-testable. This is the layer that
catches sign inversions and gamma-direction bugs before they ship.

Signals (first-class entities in server.models): direction(Flow), conviction,
positioning(Positioning), dealer_gamma(DealerGamma), skew(Skew), cost(Cost),
regime(Regime). Each registered here as `derive_<name>(canon, *, asof) -> Signal`.

Stub: the pure functions + their golden tests are written per instructions. The
registry + purity contract are fixed now (lint/test should assert no I/O imports here).
"""
from __future__ import annotations

from typing import Callable

from server.models import Conviction, DealerGamma, Flow, Provenance, Signal
from server.services import provenance as prov

REGISTRY: dict[str, Callable[..., Signal]] = {}


def register(name: str):
    def deco(fn: Callable[..., Signal]):
        REGISTRY[name] = fn
        return fn
    return deco


def derive_all(canon: dict, *, asof: str | None = None) -> dict[str, Signal]:
    """Run every registered signal function over the canonical inputs. Returns a
    name→Signal map (consumed BY NAME in Decide). A signal whose inputs are missing
    returns a Signal with quality=unavailable, never a fabricated value."""
    return {name: fn(canon, asof=asof) for name, fn in REGISTRY.items()}


def _opening(a) -> bool:
    """Opening intensity: today's volume exceeded prior OI (volume_oi_ratio > 1) = net-new
    positioning. Phase-2 live-confirmed this is THE opening proxy — UW's all_opening_trades
    flag is ~always False on Basic tier and cannot carry the signal."""
    try:
        return float(a.volume_oi_ratio or 0.0) > 1.0
    except (TypeError, ValueError):
        return False


@register("flow")
def derive_direction(canon: dict, *, asof: str | None = None) -> Flow:
    """Pick the call/put side. OPENING flow leads (Ge-Lin-Pearson: opening bets predict,
    closing bets don't); fall back to total signed flow (Pan-Poteshman). NO gamma fallback
    in the walking skeleton (that needs DealerGamma — Phase 4); with no flow at all, the
    signal is `unavailable` — never a guessed side. PURE: canonical in → Signal out, no I/O.
    Ported from `e1d6c5e:server/gates.py::derive_direction`."""
    alerts = canon.get("flow_alerts") or []
    if not alerts:
        return Flow(direction=None, direction_basis="unavailable",
                    provenance=prov.unavailable("no flow alerts"))
    src: Provenance = prov.derived(alerts[0].provenance)
    truncated = any(getattr(a, "truncated", False) for a in alerts)

    def _prem(side: str, opening_only: bool) -> float:
        return sum(float(a.total_premium or 0.0) for a in alerts
                   if a.type == side and (_opening(a) or not opening_only))

    open_call, open_put = _prem("call", True), _prem("put", True)
    if open_call or open_put:
        return Flow(direction="calls" if open_call >= open_put else "puts",
                    direction_basis="opening_flow", truncated=truncated,
                    call_prem=open_call, put_prem=open_put, provenance=src)

    tot_call, tot_put = _prem("call", False), _prem("put", False)
    if tot_call or tot_put:
        return Flow(direction="calls" if tot_call >= tot_put else "puts",
                    direction_basis="total_flow", truncated=truncated,
                    call_prem=tot_call, put_prem=tot_put, provenance=src)

    return Flow(direction=None, direction_basis="unavailable", truncated=truncated,
                provenance=prov.unavailable("zero premium on both sides"))


def _accumulation_read(cumsum: list[float]) -> tuple[str, float]:
    """Path read on a cumsum curve. efficiency = |final| / max(|cumsum|) (1.0 = clean
    one-way; «1 = spiked & reverted). Zero-crossing aware. Ported from
    `e1d6c5e:server/greek_flow.py::accumulation_read`."""
    if not cumsum:
        return "flat", 0.0
    final = cumsum[-1]
    peak = max(cumsum, key=abs)
    peak_abs = abs(peak)
    if peak_abs == 0:
        return "flat", 0.0
    eff = abs(final) / peak_abs
    crossed = (peak > 0 and final < 0) or (peak < 0 and final > 0)
    if crossed:
        state = "reversed"
    elif eff >= 0.7:
        state = "building"
    elif eff <= 0.4:
        state = "fading"
    else:
        state = "choppy"
    return state, round(eff, 2)


@register("conviction")
def derive_conviction(canon: dict, *, asof: str | None = None) -> Conviction:
    """Greek-flow directional conviction from the per-minute `dir_delta_flow` series.
    PER-MINUTE → session figure is sum, curve is cumsum (never a last tick). Sign PINNED
    (Phase 2 finding (a)): positive net = calls/bullish, negative = puts/bearish. A
    degenerate (empty/flat/all-equal) series is `unavailable` — never a silent 'flat'.
    PURE. Ported from `e1d6c5e:server/greek_flow.py::build_composite`. SAME flow family as
    Flow → in the funnel this only matters on DIVERGENCE (signal-honesty §Confluence)."""
    points = canon.get("greek_flow") or []
    dseries = [p.dir_delta_flow for p in points]
    if len(dseries) < 2 or max(dseries) == min(dseries):    # degenerate
        return Conviction(direction=None,
                          provenance=prov.unavailable("greek-flow empty or flat"))
    src: Provenance = prov.derived(points[0].provenance)
    dir_net = sum(dseries)
    total_net = sum((p.total_delta_flow or 0.0) for p in points)
    cum, run = [], 0.0
    for v in dseries:
        run += v
        cum.append(run)
    state, eff = _accumulation_read(cum)
    if dir_net == 0:
        return Conviction(direction=None, dir_delta=0.0, total_delta=round(total_net),
                          accumulation=state, efficiency=eff,
                          provenance=prov.unavailable("dir_delta net is zero (no lean)"))
    return Conviction(direction="calls" if dir_net > 0 else "puts",
                      dir_delta=round(dir_net), total_delta=round(total_net),
                      accumulation=state, efficiency=eff, provenance=src)


@register("dealer_gamma")
def derive_dealer_gamma(canon: dict, *, asof: str | None = None) -> DealerGamma:
    """Dealer-gamma structural levels from per-strike OI gamma. NET per strike is the
    SIGNED SUM call_gamma_oi + put_gamma_oi (UW pre-signs the put negative — summing, not
    subtracting, is what fixed the 2026-05-30 flip bug). flip = cumulative-net zero-crossing
    NEAREST spot; walls = max call-γ above / max |put-γ| below spot (kept separate — net
    peaks ATM). PURE. Ported from `e1d6c5e:server/gex.py::compute_levels`. A GUARD, not a
    direction (decide spec)."""
    rungs = canon.get("gamma_strikes") or []
    if not rungs:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot-exposures data"))
    spot = next((r.price for r in rungs if r.price), 0.0) or 0.0
    if spot <= 0:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot price in spot-exposures"))
    src: Provenance = prov.derived(rungs[0].provenance)
    rs = sorted(rungs, key=lambda r: r.strike)

    cum, crossings, prev = 0.0, [], None
    for r in rs:
        cum += r.call_gamma_oi + r.put_gamma_oi        # signed sum (put pre-signed negative)
        sign = 1 if cum >= 0 else -1
        if prev is not None and sign != prev:
            crossings.append(r.strike)
        prev = sign
    if crossings:
        flip, flip_status = min(crossings, key=lambda k: abs(k - spot)), "ok"
    else:
        flip, flip_status = spot, "no_flip"

    above = [r for r in rs if r.strike > spot]
    below = [r for r in rs if r.strike < spot]
    cwall = max(above, key=lambda r: r.call_gamma_oi, default=None)
    pwall = max(below, key=lambda r: abs(r.put_gamma_oi), default=None)
    cwall_k = cwall.strike if cwall else spot * 1.05
    pwall_k = pwall.strike if pwall else spot * 0.95
    agg = sum(r.call_gamma_oi + r.put_gamma_oi for r in rs) / 1e9

    return DealerGamma(
        gex_sign="POS" if agg >= 0 else "NEG",
        flip_pct=(flip - spot) / spot * 100, flip_status=flip_status,
        call_wall_pct=(cwall_k - spot) / spot * 100,
        put_wall_pct=(spot - pwall_k) / spot * 100,
        agg_b=agg, provenance=src)
