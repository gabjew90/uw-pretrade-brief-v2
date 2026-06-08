"""Stage 4 — DECIDE → verdict, in exactly ONE place.

Consumes the name→Signal map from Derive BY NAME, applies the funnel, and emits a
`Verdict` with named reasons + the list of signals it actually used. Because inputs
are named, a computed-but-unused signal is a visible gap (assert signals_used vs
available in tests), not a silent strand. This is the only module allowed to produce
a verdict — the frontend never re-derives it (the one rule).

Stub: the funnel logic is written per instructions. The contract is fixed now.
"""
from __future__ import annotations

from server.models import Signal, Verdict


def decide(signals: dict[str, Signal]) -> Verdict:
    """Apply the decision funnel to named signals. Stub returns an empty verdict that
    records which signals were available, so the wiring is observable before the
    funnel exists."""
    return Verdict(action="(undecided — funnel not yet wired)",
                   reasons=[], signals_used=sorted(signals.keys()))
