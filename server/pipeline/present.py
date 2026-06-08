"""Stage 5 — PRESENT → view model → dumb frontend.

Build a `ViewModel` from signals + verdict: per element a `{surface, detail, provenance,
label, tone}`. The novice "glance on top, evidence on tap" pattern is a view-model property
here — `surface` is the glance, `detail` is the tap — NOT render-layer logic. The frontend
renders this and computes nothing (the one rule).

Surface COPY is deferred to the operator's tile-surface brief; the field STRUCTURE
(surface/detail/tone/provenance) is fixed here. Elements are emitted in a stable declared
order. An `unavailable` signal still gets an element (tone=unavailable) — never omitted, so
a tile degrades visibly rather than silently disappearing.
"""
from __future__ import annotations

from server.models import Element, Quality, Signal, Verdict, ViewModel


def _unavail(key: str, label: str, sig) -> Element:
    note = sig.provenance.note if sig is not None else "not computed"
    return Element(key=key, label=label, surface=None, tone="unavailable",
                   detail={"reason": note or "unavailable"},
                   provenance=sig.provenance if sig is not None else None)


def _direction_el(flow) -> Element:
    if getattr(flow, "direction", None) is None:
        return _unavail("direction", "Direction", flow)
    return Element(key="direction", label="Direction", surface=flow.direction.upper(),
                   detail={"basis": flow.direction_basis, "call_premium": flow.call_prem,
                           "put_premium": flow.put_prem},
                   tone="neutral", provenance=flow.provenance)


def _conviction_el(c) -> Element:
    if c is None or getattr(c, "direction", None) is None:
        return _unavail("conviction", "Greek-flow conviction", c)
    return Element(key="conviction", label="Greek-flow conviction", surface=c.direction.upper(),
                   detail={"dir_delta": c.dir_delta, "accumulation": c.accumulation,
                           "efficiency": c.efficiency},
                   tone="neutral", provenance=c.provenance)


def _positioning_el(p) -> Element:
    if p is None or p.confirmation == "unconfirmed":
        el = _unavail("positioning", "OI confirmation", p)
        if p is not None:
            el.surface, el.detail = "UNCONFIRMED", {"note": "no settled OI history yet — not blocking"}
        return el
    tone = {"building": "positive", "unwinding": "cautionary"}.get(p.confirmation, "neutral")
    return Element(key="positioning", label="OI confirmation", surface=p.confirmation.upper(),
                   detail={"oi_trend_pct": p.oi_trend_pct, "side": p.side,
                           "cluster_strikes": p.cluster_strikes},
                   tone=tone, provenance=p.provenance)


def _structural_el(dg) -> Element:
    if dg is None or dg.flip_status == "unavailable":
        return _unavail("structural", "Dealer gamma", dg)
    return Element(key="structural", label="Dealer gamma",
                   surface=("TREND" if dg.gex_sign == "NEG" else "PINNED"),
                   detail={"gex_sign": dg.gex_sign, "agg_gamma_bn": round(dg.agg_b, 2),
                           "flip_pct": round(dg.flip_pct, 2), "flip_status": dg.flip_status,
                           "call_wall_pct": round(dg.call_wall_pct, 2),
                           "put_wall_pct": round(dg.put_wall_pct, 2)},
                   tone=("neutral" if dg.gex_sign == "NEG" else "cautionary"),
                   provenance=dg.provenance)


def _skew_el(s) -> Element:
    if s is None or s.lean == "unavailable":
        return _unavail("skew", "Skew (25Δ RR)", s)
    return Element(key="skew", label="Skew (25Δ RR)", surface=s.lean.replace("_", " ").upper(),
                   detail={"rr25": s.rr25}, tone="neutral", provenance=s.provenance)


def _cost_el(c) -> Element:
    if c is None:
        return _unavail("cost", "Cost guard", c)
    tone = {"ok": "positive", "caution": "cautionary", "block": "negative"}[c.guard]
    return Element(key="cost", label="Cost guard", surface=c.guard.upper(),
                   detail={"ivr": c.ivr, "reason": c.reason,
                           "days_to_earnings": c.days_to_earnings,
                           "event_within_hold": c.event_within_hold},
                   tone=tone, provenance=c.provenance)


def _regime_el(r) -> Element:
    if r is None:
        return _unavail("regime", "Market regime", r)
    tone = {"Favorable": "positive", "Stand down": "cautionary"}.get(r.posture, "neutral")
    return Element(key="regime", label="Market regime", surface=r.posture,
                   detail={"headline": r.headline, "vol": r.vol_line, "event": r.event_line,
                           "tide": r.tide_badge, "event_within_hold": r.event_within_hold},
                   tone=tone, provenance=r.provenance)


_BUILDERS = [
    ("flow", _direction_el), ("conviction", _conviction_el), ("positioning", _positioning_el),
    ("dealer_gamma", _structural_el), ("skew", _skew_el), ("cost", _cost_el), ("regime", _regime_el),
]


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None) -> ViewModel:
    """Assemble the renderable view model. Verdict is forwarded VERBATIM (never re-derived).
    Elements whose signal is named in `verdict.conflict_legs` are tinted cautionary so the
    disagreement is visible at the element level, not just the headline."""
    elements: list[Element] = []
    for name, build in _BUILDERS:
        if name in signals:
            elements.append(build(signals[name]))

    # conflict tone propagation (conflict_legs uses element keys conviction/skew/structural)
    conflicting = set(verdict.conflict_legs or [])
    for el in elements:
        if el.key in conflicting and el.tone != "unavailable":
            el.tone = "cautionary"

    flow = signals.get("flow")
    if getattr(flow, "truncated", False):
        elements.append(Element(
            key="flow_truncation", label="Flow window truncated", surface="partial",
            tone="cautionary",
            detail={"note": "flow-alerts hit the 500 page cap; older alerts not included"},
            provenance=flow.provenance))

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     elements=elements, verdict=verdict)
