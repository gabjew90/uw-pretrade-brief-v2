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

from server.models import Signal

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
