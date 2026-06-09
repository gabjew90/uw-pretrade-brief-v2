"""Stage 5 — PRESENT → view model → dumb frontend.

Per element `{label, surface, meaning, logic, detail, provenance, tone}`. `surface` is the
glance, `meaning` is a terse NUMBER readout (the data behind the word), `logic` is the RULE
(how the word is decided from that data), `detail` is the tap payload. Values are formatted
HERE so the frontend computes NOTHING. A market `regime` header + a `verdict_logic` line
expose how the market read and the overall call are reached. Numbers speak: no em-dashes,
no semicolons.
"""
from __future__ import annotations

from server.models import Element, Regime, Signal, Verdict, ViewModel


def _money(v) -> str:
    if v is None:
        return "n/a"
    a, sign = abs(v), ("-" if v < 0 else "")
    return (f"{sign}${a/1e9:.1f}B" if a >= 1e9 else f"{sign}${a/1e6:.1f}M" if a >= 1e6
            else f"{sign}${a/1e3:.0f}K" if a >= 1e3 else f"{sign}${a:.0f}")


def _num(v) -> str:
    if v is None:
        return "n/a"
    a, sign = abs(v), ("-" if v < 0 else "")
    return (f"{sign}{a/1e9:.1f}B" if a >= 1e9 else f"{sign}{a/1e6:.1f}M" if a >= 1e6
            else f"{sign}{a/1e3:.0f}K" if a >= 1e3 else f"{sign}{a:.0f}")


def _pct(v, dp=1, sign=False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{dp}f}%" if sign else f"{v:.{dp}f}%"


def _unavail(key: str, label: str, sig, meaning: str = "", logic: str = "") -> Element:
    note = sig.provenance.note if sig is not None else "not computed"
    return Element(key=key, label=label, surface=None, meaning=meaning, logic=logic,
                   tone="unavailable", detail={"why": note or "unavailable"},
                   provenance=sig.provenance if sig is not None else None)


def _direction_el(flow) -> Element:
    if getattr(flow, "direction", None) is None:
        return _unavail("direction", "Where's the money betting?", flow, "no opening flow",
                        "side with more opening premium wins")
    basis = "opening flow" if flow.direction_basis == "opening_flow" else "total flow"
    return Element(key="direction", label="Where's the money betting?",
                   surface=flow.direction.upper(),
                   meaning=f"{_money(flow.call_prem)} call · {_money(flow.put_prem)} put",
                   logic="side with more opening premium wins",
                   detail={"Call premium": _money(flow.call_prem),
                           "Put premium": _money(flow.put_prem), "Read from": basis},
                   tone="neutral", provenance=flow.provenance)


def _conviction_el(c) -> Element:
    if c is None or getattr(c, "direction", None) is None:
        return _unavail("conviction", "Does the live tape agree?", c, "no greek-flow",
                        "sign of the session net delta")
    return Element(key="conviction", label="Does the live tape agree?",
                   surface=c.direction.upper(),
                   meaning=f"{_num(c.dir_delta)} net delta, {c.accumulation}",
                   logic="sign of the session net delta (calls if positive)",
                   detail={"Net directional delta": f"{c.dir_delta:,.0f}",
                           "Path this session": c.accumulation, "One-way %": f"{c.efficiency:.0%}"},
                   tone="neutral", provenance=c.provenance)


def _positioning_el(p) -> Element:
    if p is None or p.confirmation == "unconfirmed":
        el = _unavail("positioning", "Is the bet being held?", p, "no settled OI yet",
                      "growth in OI at the bet's strikes")
        if p is not None:
            el.surface = "NOT YET"
        return el
    surf = {"building": "GROWING", "unwinding": "SHRINKING", "flat": "FLAT"}.get(
        p.confirmation, p.confirmation.upper())
    tone = {"building": "positive", "unwinding": "cautionary"}.get(p.confirmation, "neutral")
    return Element(key="positioning", label="Is the bet being held?", surface=surf,
                   meaning=f"{_pct(p.oi_trend_pct, 0, sign=True)} OI at the bet's strikes",
                   logic="OI up = held, down = closing (vs prior settled session)",
                   detail={"OI change (settled)": _pct(p.oi_trend_pct, 1, sign=True),
                           "Side": p.side, "Strikes watched": p.cluster_strikes},
                   tone=tone, provenance=p.provenance)


def _structural_el(dg) -> Element:
    if dg is None or dg.flip_status == "unavailable":
        return _unavail("structural", "Will moves extend or fade?", dg, "no dealer gamma",
                        "negative dealer gamma extends moves, positive pins them")
    trend = dg.gex_sign == "NEG"
    flip = f"{_pct(dg.flip_pct, sign=True)} from price" if dg.flip_status == "ok" else "no flip in range"
    return Element(key="structural", label="Will moves extend or fade?",
                   surface="EXTEND" if trend else "FADE",
                   meaning=f"dealer gamma {_money(dg.agg_b * 1e9)}",
                   logic="negative gamma extends moves, positive pins them",
                   detail={"Dealer gamma": _money(dg.agg_b * 1e9), "Gamma flip": flip,
                           "Call wall (resistance)": _pct(dg.call_wall_pct, sign=True),
                           "Put wall (support)": _pct(-dg.put_wall_pct, sign=True)},
                   tone="neutral" if trend else "cautionary", provenance=dg.provenance)


def _skew_el(s) -> Element:
    if s is None or s.lean == "unavailable":
        return _unavail("skew", "Which way is fear priced?", s, "no risk reversal",
                        "risk-reversal sign vs your side (oppose caps the verdict)")
    surf = {"call_skew": "CALLS BID", "put_skew": "PUTS BID", "neutral": "BALANCED"}.get(
        s.lean, s.lean.upper())
    return Element(key="skew", label="Which way is fear priced?", surface=surf,
                   meaning=f"25Δ RR {s.rr25:+.3f}" if s.rr25 is not None else "n/a",
                   logic="positive = calls bid, negative = puts bid, near zero = balanced",
                   detail={"25Δ risk reversal": f"{s.rr25:+.3f}" if s.rr25 is not None else "n/a",
                           "Positive": "calls bid", "Negative": "puts bid"},
                   tone="neutral", provenance=s.provenance)


def _cost_el(c) -> Element:
    if c is None:
        return _unavail("cost", "Is it worth the cost?", c, "no cost read",
                        "blocks on event, earnings, or spread bigger than the move")
    surf = {"ok": "WORTH IT", "caution": "PRICEY", "block": "PASS"}[c.guard]
    tone = {"ok": "positive", "caution": "cautionary", "block": "negative"}[c.guard]
    d: dict = {"The call": c.reason}
    if c.contract:
        ct = c.contract
        d["Contract you'd buy"] = f"{ct['strike']:g} {ct['type']} · {ct['dte']}d · ${ct['ask']:.2f}"
    if c.spread_pct is not None:
        d["Round-trip spread"] = f"{c.spread_pct:.0f}% of premium"
    if c.breakeven_move_pct is not None:
        d["Move to break even"] = _pct(c.breakeven_move_pct, 2)
    if c.expected_move_pct is not None:
        d["Move being priced"] = _pct(c.expected_move_pct, 2)
    if c.ivr is not None:
        d["IV rank"] = f"{c.ivr:.0f}/100"
    if c.front_iv is not None and c.back_iv is not None:
        d["Near vs far IV"] = f"{c.front_iv:.0%} · {c.back_iv:.0%}" + (" inverted" if c.term_inverted else "")
    if c.days_to_earnings is not None:
        d["Days to earnings"] = c.days_to_earnings
    return Element(key="cost", label="Is it worth the cost?", surface=surf, meaning=c.reason,
                   logic="PASS on event, earnings, or spread bigger than the move",
                   detail=d, tone=tone, provenance=c.provenance)


def _regime_header(r: Regime) -> Element:
    """Market-wide posture shown as a header — with the DATA VARIABLES behind the word."""
    tone = {"Favorable": "positive", "Stand down": "cautionary"}.get(r.posture, "neutral")
    gamma = (f"{r.gamma_sign} ({'trend' if r.gamma_sign == 'NEG' else 'pinned'})"
             if r.gamma_status == "ok" else "n/a")
    vol = f"IV {r.vol_iv:.0%}" if r.vol_iv is not None else "n/a"
    tide = r.tide_lean or "neutral"
    event = r.event_line or "none in 5d"
    return Element(key="regime", label="Market backdrop (every ticker)", surface=r.posture,
                   meaning=f"gamma {gamma} · {vol} · tide {tide} · event {event}",
                   logic="neg gamma favors direction, pos pins, macro event vetoes",
                   detail={"SPY index gamma": gamma, "Macro event in 5d": event,
                           "SPY vol": vol, "Tape tide": tide},
                   tone=tone, provenance=r.provenance)


_BUILDERS = [
    ("flow", _direction_el), ("conviction", _conviction_el), ("positioning", _positioning_el),
    ("dealer_gamma", _structural_el), ("skew", _skew_el), ("cost", _cost_el),
]

_VERDICT_LOGIC = ("Stand down if cost says PASS or OI is red. Favorable needs opening-flow "
                  "direction, OI not shrinking, trend gamma, no skew/tape conflict, cost clear. "
                  "Anything else is Mixed.")


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None, regime: Regime | None = None) -> ViewModel:
    """Assemble the renderable view model. Verdict forwarded VERBATIM. Conflicting legs are
    tinted cautionary. The market `regime` (if computed) becomes a header with its data
    variables; `verdict_logic` states how the overall call is reached from the gates."""
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
            key="flow_truncation", label="Partial flow window", surface="partial",
            meaning="feed hit its 500 cap, oldest alerts missing", tone="cautionary",
            detail={"note": "flow-alerts hit the 500 page cap, older alerts not included"},
            provenance=flow.provenance))

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     regime=_regime_header(regime) if regime is not None else None,
                     verdict_logic=_VERDICT_LOGIC, elements=elements, verdict=verdict)
