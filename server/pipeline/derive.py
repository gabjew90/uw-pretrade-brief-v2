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

from server.models import Flow, Provenance, Signal
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

    def _prem(side: str, opening_only: bool) -> float:
        return sum(float(a.total_premium or 0.0) for a in alerts
                   if a.type == side and (_opening(a) or not opening_only))

    open_call, open_put = _prem("call", True), _prem("put", True)
    if open_call or open_put:
        return Flow(direction="calls" if open_call >= open_put else "puts",
                    direction_basis="opening_flow",
                    call_prem=open_call, put_prem=open_put, provenance=src)

    tot_call, tot_put = _prem("call", False), _prem("put", False)
    if tot_call or tot_put:
        return Flow(direction="calls" if tot_call >= tot_put else "puts",
                    direction_basis="total_flow",
                    call_prem=tot_call, put_prem=tot_put, provenance=src)

    return Flow(direction=None, direction_basis="unavailable",
                provenance=prov.unavailable("zero premium on both sides"))
