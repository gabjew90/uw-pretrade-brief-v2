"""Stage 5 — PRESENT → view model → dumb frontend.

Build a `ViewModel` from signals + verdict: per element `{label, surface, meaning, detail,
provenance, tone}`. The novice "glance on top, evidence on tap" pattern is a view-model
property here — `surface` is the glance, `meaning` is the one-line plain-English read,
`detail` is the tap payload. Values are formatted HERE (dollars, percents, plain keys) so
the frontend renders display-ready strings and computes NOTHING (the one rule).

This is the legibility layer: labels are plain questions, detail keys are words not field
names, and every number carries a unit. Copy is a first pass — operator tile briefs steer
the exact wording. Elements are emitted in a stable declared order; an `unavailable` signal
still gets an element (tone=unavailable), never omitted.
"""
from __future__ import annotations

from server.models import Element, Signal, Verdict, ViewModel


def _money(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    return (f"${a/1e9:.1f}B" if a >= 1e9 else f"${a/1e6:.1f}M" if a >= 1e6
            else f"${a/1e3:.0f}K" if a >= 1e3 else f"${a:.0f}")


def _pct(v, dp=1) -> str:
    return "—" if v is None else f"{v:.{dp}f}%"


def _unavail(key: str, label: str, sig, meaning: str = "") -> Element:
    note = sig.provenance.note if sig is not None else "not computed"
    return Element(key=key, label=label, surface=None, meaning=meaning, tone="unavailable",
                   detail={"why": note or "unavailable"},
                   provenance=sig.provenance if sig is not None else None)


def _direction_el(flow) -> Element:
    if getattr(flow, "direction", None) is None:
        return _unavail("direction", "Where's the money betting?", flow,
                        "No opening flow to read a side from.")
    basis = "opening flow" if flow.direction_basis == "opening_flow" else "total flow (weaker signal)"
    return Element(key="direction", label="Where's the money betting?",
                   surface=flow.direction.upper(),
                   meaning=f"The side options buyers paid up for today (read from {basis}).",
                   detail={"Call premium": _money(flow.call_prem),
                           "Put premium": _money(flow.put_prem), "Read from": basis},
                   tone="neutral", provenance=flow.provenance)


def _conviction_el(c) -> Element:
    if c is None or getattr(c, "direction", None) is None:
        return _unavail("conviction", "Does the live tape agree?", c,
                        "Greek-flow read unavailable this session.")
    return Element(key="conviction", label="Does the live tape agree?",
                   surface=c.direction.upper(),
                   meaning="Which way the directional delta is actually being traded right now.",
                   detail={"Net directional delta": f"{c.dir_delta:,.0f}",
                           "Path this session": c.accumulation,
                           "How clean (one-way)": f"{c.efficiency:.0%}"},
                   tone="neutral", provenance=c.provenance)


def _positioning_el(p) -> Element:
    if p is None or p.confirmation == "unconfirmed":
        el = _unavail("positioning", "Is the bet being held?", p,
                      "No settled open-interest history yet — not counted against the trade.")
        if p is not None:
            el.surface = "NOT YET CONFIRMED"
        return el
    surf = {"building": "GROWING (held)", "unwinding": "SHRINKING (closing)",
            "flat": "FLAT"}.get(p.confirmation, p.confirmation.upper())
    tone = {"building": "positive", "unwinding": "cautionary"}.get(p.confirmation, "neutral")
    return Element(key="positioning", label="Is the bet being held?", surface=surf,
                   meaning="Whether open interest at the bet's strikes is growing (conviction) or shrinking (closing out).",
                   detail={"OI change (settled)": _pct(p.oi_trend_pct), "Side": p.side,
                           "Strikes watched": p.cluster_strikes},
                   tone=tone, provenance=p.provenance)


def _structural_el(dg) -> Element:
    if dg is None or dg.flip_status == "unavailable":
        return _unavail("structural", "Will moves extend or fade?", dg,
                        "Dealer-gamma read unavailable.")
    trend = dg.gex_sign == "NEG"
    flip = (_pct(dg.flip_pct) + " from price") if dg.flip_status == "ok" else "none in range"
    return Element(key="structural", label="Will moves extend or fade?",
                   surface="EXTEND (trend)" if trend else "FADE (pinned)",
                   meaning="Dealer gamma: negative = moves tend to extend (helps a directional bet); positive = pinned/chop.",
                   detail={"Dealer gamma": f"${dg.agg_b:.1f}B ({dg.gex_sign})",
                           "Gamma flip": flip,
                           "Call wall (resistance)": "+" + _pct(dg.call_wall_pct),
                           "Put wall (support)": "−" + _pct(dg.put_wall_pct)},
                   tone="neutral" if trend else "cautionary", provenance=dg.provenance)


def _skew_el(s) -> Element:
    if s is None or s.lean == "unavailable":
        return _unavail("skew", "Which way is fear priced?", s,
                        "Risk-reversal skew unavailable.")
    surf = {"call_skew": "CALLS BID UP", "put_skew": "PUTS BID UP (defensive)",
            "neutral": "BALANCED"}.get(s.lean, s.lean.upper())
    return Element(key="skew", label="Which way is fear priced?", surface=surf,
                   meaning="The options vol-surface lean — which side is paying up (25-delta risk reversal).",
                   detail={"25Δ risk reversal": f"{s.rr25:+.3f}" if s.rr25 is not None else "—",
                           "How to read it": "positive = calls bid up, negative = puts bid up"},
                   tone="neutral", provenance=s.provenance)


def _cost_el(c) -> Element:
    if c is None:
        return _unavail("cost", "Is it worth the cost?", c, "Cost read unavailable.")
    surf = {"ok": "WORTH IT", "caution": "PRICEY", "block": "PASS"}[c.guard]
    tone = {"ok": "positive", "caution": "cautionary", "block": "negative"}[c.guard]
    d: dict = {"The call": c.reason}
    if c.contract:
        ct = c.contract
        d["Contract you'd buy"] = f"{ct['strike']:g} {ct['type']} · {ct['dte']}d out · ${ct['ask']:.2f}"
    if c.spread_pct is not None:
        d["Round-trip spread"] = _pct(c.spread_pct, 0) + " of premium"
    if c.breakeven_move_pct is not None:
        d["Move to break even"] = _pct(c.breakeven_move_pct, 2)
    if c.expected_move_pct is not None:
        d["Move being priced"] = _pct(c.expected_move_pct, 2)
    if c.ivr is not None:
        d["IV rank"] = f"{c.ivr:.0f} / 100"
    if c.front_iv is not None and c.back_iv is not None:
        d["Near vs far IV"] = f"{c.front_iv:.0%} vs {c.back_iv:.0%}" + (" (inverted)" if c.term_inverted else "")
    if c.days_to_earnings is not None:
        d["Days to earnings"] = c.days_to_earnings
    return Element(key="cost", label="Is it worth the cost?", surface=surf,
                   meaning="Whether the contract you'd actually buy can clear its round-trip cost and the move being priced.",
                   detail=d, tone=tone, provenance=c.provenance)


_BUILDERS = [
    ("flow", _direction_el), ("conviction", _conviction_el), ("positioning", _positioning_el),
    ("dealer_gamma", _structural_el), ("skew", _skew_el), ("cost", _cost_el),
]
# NB: no regime tile — regime is market-wide, not per-ticker evidence (the macro-event veto
# it carried is shown as data in the Cost tile).


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None) -> ViewModel:
    """Assemble the renderable view model. Verdict is forwarded VERBATIM (never re-derived).
    Elements named in `verdict.conflict_legs` are tinted cautionary so a disagreement is
    visible at the element level, not just the headline."""
    elements: list[Element] = []
    for name, build in _BUILDERS:
        if name in signals:
            elements.append(build(signals[name]))

    conflicting = set(verdict.conflict_legs or [])
    for el in elements:
        if el.key in conflicting and el.tone != "unavailable":
            el.tone = "cautionary"

    flow = signals.get("flow")
    if getattr(flow, "truncated", False):
        elements.append(Element(
            key="flow_truncation", label="Heads up: partial flow window", surface="partial",
            meaning="Today's flow feed hit its size cap, so the oldest alerts aren't included.",
            tone="cautionary",
            detail={"note": "flow-alerts hit the 500 page cap; older alerts not included"},
            provenance=flow.provenance))

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     elements=elements, verdict=verdict)
