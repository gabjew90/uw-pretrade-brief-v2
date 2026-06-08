"""Stage 4 — DECIDE → verdict, in exactly ONE place.

Consumes the name→Signal map from Derive BY NAME, applies the funnel, and emits a
`Verdict` with named reasons + the list of signals it actually used. Because inputs are
named, a computed-but-unused signal is a visible gap, not a silent strand. This is the
only module allowed to produce a verdict — the frontend never re-derives it (the one rule).

**Phase 3 walking skeleton:** a TRIVIAL single-signal verdict — the action mirrors the
`flow` direction so the boundary is proven end-to-end. The real funnel (positioning cap,
cost block, structural cap, skew oppose-veto, regime) lands in Phase 4 per the decide
spec; this is intentionally minimal and will be REPLACED, not extended ad hoc.
"""
from __future__ import annotations

from server.models import Provenance, Signal, Verdict


def decide(signals: dict[str, Signal]) -> Verdict:
    """Skeleton: read `flow` by name; action mirrors its direction. Honest-degrade is
    GATED as behavior — an unavailable flow yields `Stand down` with the reason NAMED,
    never a guessed side."""
    flow = signals.get("flow")
    if flow is None:
        return Verdict(action="Stand down", reasons=["no flow signal computed"],
                       signals_used=[], provenance=Provenance())

    direction = getattr(flow, "direction", None)
    if direction is None:
        # unavailable CORE input → cannot be Favorable; name WHY (honest-degrade gated)
        note = flow.provenance.note or "no directional flow"
        return Verdict(action="Stand down", reasons=[f"flow unavailable: {note}"],
                       signals_used=["flow"], provenance=flow.provenance)

    basis = getattr(flow, "direction_basis", "unavailable")
    if basis == "opening_flow":
        reason = f"opening flow favors {direction}"
    else:
        reason = f"total flow favors {direction} (no opening signal — weaker basis)"
    return Verdict(action=f"Lean {direction}", reasons=[reason],
                   signals_used=["flow"], provenance=flow.provenance)
