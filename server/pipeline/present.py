"""Stage 5 — PRESENT → view model → dumb frontend.

Build a `ViewModel` from signals + verdict: per element a `{surface, detail,
provenance, label}`. The novice "glance on top, evidence on tap" pattern is a
view-model property here — `surface` is the glance, `detail` is the tap — NOT
render-layer logic. The frontend renders this and computes nothing (the one rule).

Stub: the per-element builders are written per instructions. The contract is fixed.
"""
from __future__ import annotations

from server.models import Element, Signal, Verdict, ViewModel


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None) -> ViewModel:
    """Assemble the renderable view model. Stub emits one element per signal (label +
    provenance) plus the verdict, so the end-to-end shape is exercisable before the
    real surfaces/labels are written."""
    elements = [
        Element(key=name, label=name.replace("_", " ").title(),
                surface=None, detail=None, provenance=sig.provenance)
        for name, sig in sorted(signals.items())
    ]
    return ViewModel(ticker=ticker, as_of=as_of, elements=elements, verdict=verdict)
