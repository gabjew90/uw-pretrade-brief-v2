"""Stage 5 — PRESENT → view model → dumb frontend.

Build a `ViewModel` from signals + verdict: per element a `{surface, detail, provenance,
label, tone}`. The novice "glance on top, evidence on tap" pattern is a view-model
property here — `surface` is the glance, `detail` is the tap — NOT render-layer logic.
The frontend renders this and computes nothing (the one rule).

**Phase 3 walking skeleton:** only the `flow` direction element (+ a truncation warning
element when the pull hit the page cap). Surface COPY is deferred to the operator's
tile-surface brief; the field STRUCTURE (surface/detail/tone/provenance) is fixed here.
More elements land per signal in Phase 4.
"""
from __future__ import annotations

from server.models import Element, Signal, Verdict, ViewModel


def _direction_element(flow) -> Element:
    if getattr(flow, "direction", None) is None:
        return Element(key="direction", label="Direction", surface=None,
                       tone="unavailable",
                       detail={"reason": flow.provenance.note or "no directional flow"},
                       provenance=flow.provenance)
    return Element(
        key="direction", label="Direction",
        surface=flow.direction.upper(),                 # glance: "CALLS" / "PUTS"
        detail={                                        # tap: the evidence behind it
            "basis": flow.direction_basis,
            "call_premium": flow.call_prem,
            "put_premium": flow.put_prem,
        },
        tone="neutral",   # a side is not good/bad; sentiment lives in the verdict (deferred)
        provenance=flow.provenance,
    )


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None) -> ViewModel:
    """Assemble the renderable view model. Verdict is forwarded VERBATIM (never
    re-derived). `as_of` is the oldest provenance across signals."""
    elements: list[Element] = []

    flow = signals.get("flow")
    if flow is not None:
        elements.append(_direction_element(flow))
        # the one case where a derived FLAG (not a gate) drives its own element: the
        # flow-alerts page-cap truncation, surfaced honestly so Tile 1 can warn.
        if getattr(flow, "truncated", False):
            elements.append(Element(
                key="flow_truncation", label="Flow window truncated", surface="partial",
                tone="cautionary",
                detail={"note": "flow-alerts hit the 500 page cap; older alerts not "
                                "included — paginate via older_than for the full window"},
                provenance=flow.provenance))

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     elements=elements, verdict=verdict)
