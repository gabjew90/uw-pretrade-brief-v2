"""Stage 5 — PRESENT → view model → dumb frontend.

Per element `{label, surface, meaning, logic, detail, provenance, tone}`. `surface` is the
glance, `meaning` is a terse NUMBER readout (the data behind the word), `logic` is the RULE
(how the word is decided from that data), `detail` is the tap payload. Values are formatted
HERE so the frontend computes NOTHING. A market `regime` header + a `verdict_logic` line
expose how the market read and the overall call are reached. Numbers speak: no em-dashes,
no semicolons.
"""
from __future__ import annotations

from server.models import Element, Signal, Verdict, ViewModel
from server.services import provenance as prov


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


def _fmt_alert(r: dict) -> str:
    """One alert as a terse receipt: '10:42 · 580 put 06-13 · $1.2M · ask-side sweep · 4.1x OI'."""
    bits = []
    if r.get("time"):
        bits.append(r["time"])
    contract = f"{r['strike']:g} {r['type']}" if r.get("strike") is not None else r["type"]
    if r.get("expiry"):
        contract += f" {str(r['expiry'])[5:]}"
    bits.append(contract)
    bits.append(_money(r.get("premium", 0)))
    if r.get("aggressor"):
        bits.append(r["aggressor"] + (" sweep" if r.get("sweep") else ""))
    if r.get("voi") is not None:
        bits.append(f"{r['voi']:.1f}x OI")
    return " · ".join(bits)


def _direction_el(flow) -> Element:
    if getattr(flow, "direction", None) is None:
        return _unavail("direction", "Where's the money betting?", flow, "no opening flow",
                        "side with more opening premium wins")
    basis = "opening flow" if flow.direction_basis == "opening_flow" else "total flow"
    weak = flow.lean_quality == "weak"
    meaning = f"{_money(flow.call_prem)} call · {_money(flow.put_prem)} put"
    if weak:
        meaning += f" · weak lean ({flow.lean_note})"
    # the receipts: the actual biggest bets behind the read, not just the totals
    bets = {f"Top bet {i}": _fmt_alert(r) for i, r in enumerate(flow.top_alerts, 1)}
    return Element(key="direction", label="Where's the money betting?",
                   surface=flow.direction.upper(),
                   meaning=meaning,
                   logic="side with more opening premium wins; needs 2:1 dominance and "
                         "$500K+ to count as qualified, else it caps the call at Mixed",
                   detail={"Call premium": _money(flow.call_prem),
                           "Put premium": _money(flow.put_prem), "Read from": basis,
                           **bets,
                           "Lean (needs 2:1 + $500K)": f"{flow.lean_note} {flow.lean_quality}",
                           "Why it matters": "a real bet reads like 5:1 — millions on one "
                           "side, little on the other. 1.3:1 is a coin flip no matter how "
                           "big the dollars. And even a clean one-sided read points to a "
                           "small move (typically under 1% over a few days, often less "
                           "than your contract needs to break even) — which is why the "
                           "other tiles must also agree before anything is Favorable"},
                   series={"kind": "strike_bars", "points": flow.top_strikes},
                   tone="cautionary" if weak else "neutral", provenance=flow.provenance)


def _timeline_el(flow) -> Element | None:
    """'When did the money arrive?' — the intraday arrival of the bet premium (display-
    only, second tile from the flow signal like cost→contract). The totals erase timing:
    an early lean the day kept confirming reads differently from a last-hour pile-in.
    With a truncated pull the early session may simply be missing — honesty over pattern."""
    if not getattr(flow, "flow_series", None):
        return None
    late = flow.late_pct
    first_t, last_t = flow.flow_series[0]["t"], flow.flow_series[-1]["t"]
    # the cap flag alone doesn't mean lost coverage — pagination may still have reached
    # the open. Partial only when the window demonstrably misses the morning.
    partial = bool(flow.truncated) and first_t > "09:45"
    if partial:
        surf = "PARTIAL VIEW"
    elif late is not None and late >= 50:
        surf = "BACK-LOADED"
    elif late is not None and late <= 15:
        surf = "MOSTLY EARLY"
    else:
        surf = "SPREAD OUT"
    meaning = (f"{late:.0f}% of opening premium after 14:00 ET" if late is not None
               else "arrival timing unavailable")
    if partial:
        meaning += f" · window starts {first_t} ET, morning missing"
    return Element(key="flow_timeline", label="When did the money arrive?", surface=surf,
                   meaning=meaning,
                   logic="cumulative opening premium by side through the session",
                   detail={"First alert": f"{first_t} ET", "Last alert": f"{last_t} ET",
                           "After 14:00 ET": f"{late:.0f}%" if late is not None else "n/a",
                           "Why it matters": "a last-hour pile-in is a bet on tomorrow "
                           "morning; an early lean the day kept confirming is steadier "
                           "evidence. If the window is partial, a 'late' pattern can be a "
                           "fetch artifact, not a real one"},
                   series={"kind": "two_line", "points": flow.flow_series},
                   tone="cautionary" if partial else "neutral",
                   provenance=flow.provenance)


def _conviction_el(c) -> Element:
    if c is None or getattr(c, "direction", None) is None:
        return _unavail("conviction", "Does the live tape agree?", c, "no greek-flow",
                        "sign of the session net delta")
    # the % means nothing without a scale word: 12.6% is heavy pressure, 0.1% is noise
    size = ""
    if c.vol_ratio is not None:
        word = ("dominant" if c.vol_ratio >= 0.15 else "heavy" if c.vol_ratio >= 0.05
                else "noticeable" if c.vol_ratio >= 0.01 else "noise")
        pct = f"{c.vol_ratio:.1%}" if c.vol_ratio >= 0.001 else "<0.1%"
        size = f" · {pct} of share volume = {word}"
    detail = {"Net directional delta": f"{c.dir_delta:,.0f}",
              "Path this session": c.accumulation, "One-way %": f"{c.efficiency:.0%}"}
    if c.vol_ratio is not None:
        detail["Vs shares traded"] = (f"{pct} of today's {_num(c.share_volume)} shares "
                                      f"({word}: under 1% = noise, 5%+ = heavy, "
                                      "15%+ = dominant)")
    detail["Why it matters"] = ("this is the same money as the flow read a different way, "
                                "so agreement adds nothing — but the tape pushing AGAINST "
                                "the flow means the bet is contested")
    return Element(key="conviction", label="Does the live tape agree?",
                   surface=c.direction.upper(),
                   meaning=f"{_num(c.dir_delta)} net delta, {c.accumulation}{size}",
                   logic="sign of the session net delta (calls if positive), "
                         "sized against the day's share volume",
                   detail=detail,
                   series={"kind": "line", "points": c.cum_series},
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
    # Absolute contracts carry the meaning. Weekly strikes are often only days old, so a
    # percent against the near-zero birth base reads "+36692%" — true and useless.
    n = p.window_sessions or 0
    if p.oi_start is not None and p.oi_end is not None:
        span = f"{_num(p.oi_start)} → {_num(p.oi_end)} contracts over {n} sessions"
        if p.oi_trend_pct > 300:
            meaning = f"{span} (fresh strikes, built from near zero)"
            change = f"{span} — % vs the tiny day-one base isn't meaningful"
        else:
            meaning = f"{_pct(p.oi_trend_pct, 0, sign=True)} OI ({span})"
            change = f"{_pct(p.oi_trend_pct, 1, sign=True)} ({span})"
    else:
        meaning = f"{_pct(p.oi_trend_pct, 0, sign=True)} OI at the bet's strikes"
        change = _pct(p.oi_trend_pct, 1, sign=True)
    return Element(key="positioning", label="Is the bet being held?", surface=surf,
                   meaning=meaning,
                   logic="OI up = held, down = closing (across recent settled sessions)",
                   detail={"OI change (settled)": change,
                           "Side": p.side, "Strikes watched": p.cluster_strikes,
                           "Why it matters": "open interest counts positions still alive — "
                           "if it grows, yesterday's buyers kept the bet overnight; if it "
                           "shrinks, what looked like buying was closing. On days-old "
                           "weeklies read the contract counts, not the percent"},
                   series={"kind": "bars", "points": p.oi_series},
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
                           "Put wall (support)": _pct(-dg.put_wall_pct, sign=True)
                                                 if dg.put_wall_pct is not None else "n/a",
                           "Why it matters": "a directional weekly needs the move to RUN — "
                           "negative gamma means dealer hedging amplifies it, positive "
                           "smothers it. Caution: extended moves tend to revert within "
                           "days, so plan a 1-3 day hold, not the week"},
                   series={"kind": "ladder", "points": dg.ladder},
                   tone="neutral" if trend else "cautionary", provenance=dg.provenance)


def _skew_el(s) -> Element:
    if s is None or s.lean == "unavailable":
        # visible badge, not a silent cap: a name with no RR baseline (newly watched /
        # thin options history) structurally can't reach Favorable — say so
        return _unavail("skew", "Which way is fear priced?", s,
                        "no baseline yet — needs 3+ prior sessions of risk-reversal "
                        "history, so this name can't reach Favorable today",
                        "risk-reversal change vs its own normal (oppose caps the verdict)")
    surf = {"call_skew": "CALLS BID", "put_skew": "PUTS BID", "neutral": "BALANCED"}.get(
        s.lean, s.lean.upper())
    rr = f"{s.rr25:+.3f}" if s.rr25 is not None else "n/a"
    base = f"{s.rr_baseline:+.3f}" if s.rr_baseline is not None else "n/a"
    return Element(key="skew", label="Which way is fear priced?", surface=surf,
                   meaning=f"RR {rr} vs {base} normal",
                   logic="today's risk reversal vs its own recent normal, calls bid = richer than usual",
                   detail={"Today (25Δ RR)": rr, "Recent normal": base,
                           "Change vs normal": f"{s.rr_delta:+.3f}" if s.rr_delta is not None else "n/a",
                           "Why it matters": "the one check from an independent data "
                           "source: what protection actually costs. Fear leaning against "
                           "your side means someone is paying real money to disagree"},
                   series={"kind": "line", "points": s.series},
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
    d["Why it matters"] = ("the costs are certain, the move isn't: a wide spread can eat "
                           "10%+ round trip and a weekly bleeds theta every day — that's "
                           "why cost alone can kill an otherwise good setup")
    return Element(key="cost", label="Is it worth the cost?", surface=surf, meaning=c.reason,
                   logic="PASS on event, earnings, or spread bigger than the move",
                   detail=d, series={"kind": "line", "points": c.term_curve},
                   tone=tone, provenance=c.provenance)


def _market_el(m: dict) -> Element:
    """The 'Market today' tile — raw market context (SPY gamma sign, SPY IV, tide, next
    macro event), shaped like every other tile (label, surface, meaning, how, tap detail)
    but with DATA on the surface, never a posture/verdict word (only THE CALL gets one).
    A FAILED calendar/tide fetch reads n/a, never an implied all-clear."""
    gs = m.get("gamma_sign")
    gamma = f"{gs} ({'trend' if gs == 'NEG' else 'pinned'})" if gs else "n/a"
    vol = f"{m['iv']:.0%}" if m.get("iv") is not None else "n/a"
    tide = m.get("tide") or "n/a"
    if m.get("event_line"):
        event = m["event_line"]
    elif m.get("events_known"):
        event = "none in 5d"
    else:
        event = "n/a"
    return Element(key="regime", label="What's the market backdrop?",
                   surface=f"CPI/FOMC: {event}" if event not in ("none in 5d", "n/a")
                           else ("NO MACRO EVENT 5D" if event == "none in 5d" else "EVENTS N/A"),
                   meaning=f"SPY gamma {gamma} · IV {vol} · tide {tide}",
                   logic="same for every ticker. a macro event inside 5d flags Cost (caution), only earnings block",
                   detail={"SPY index gamma": gamma, "SPY IV (near-term)": vol,
                           "Tape tide": tide, "Next macro event (5d)": event},
                   provenance=prov.live(m.get("as_of")))


def _fmt_candidate(c: dict) -> str:
    """One contract as a terse number row: '745 put · 2d · $4.68 · Δ0.45 · spread 1% · θ8%/d'."""
    bits = [f"{c['strike']:g} {c['type']} · {c['dte']}d · ${c['ask']:.2f}"]
    if c.get("delta") is not None:
        bits.append(f"Δ{c['delta']:.2f}")
    if c.get("spread_pct") is not None:
        bits.append(f"spread {c['spread_pct']:.0f}%")
    if c.get("theta_day_pct") is not None:
        bits.append(f"θ{c['theta_day_pct']:.0f}%/d")
    if c.get("breakeven_move_pct") is not None:
        bits.append(f"be {c['breakeven_move_pct']:.2f}%")
    return " · ".join(bits)


def _contract_el(c) -> Element:
    """'Which contract?' — the realistic pick with its numbers, plus the nearest
    alternatives so the strike CHOICE is informed (guidance, not a verdict leg)."""
    if c is None or not c.contract:
        return _unavail("contract", "Which contract?", c, "no chain to pick from",
                        "flow side, 2-14 days out, strike nearest the price")
    ct = c.contract
    d: dict = {"The pick": _fmt_candidate(ct)}
    for i, alt in enumerate(c.candidates, 1):
        d[f"Alt {i}"] = _fmt_candidate(alt)
    d["Sane delta band"] = "0.35 to 0.55 (below = lottery ticket, above = mostly intrinsic)"
    if ct.get("ask"):
        d["Max loss"] = (f"${ct['ask'] * 100:,.0f} per contract. weeklies routinely expire "
                         "worthless, size so a full loss is fine")
    return Element(key="contract", label="Which contract?",
                   surface=f"{ct['strike']:g} {ct['type'].upper()} · {ct['dte']}d",
                   meaning=_fmt_candidate(ct),
                   logic="flow side, 2-14 days out, nearest the money. delta and theta from the greeks sheet",
                   detail=d, tone="neutral", provenance=c.provenance)


_BUILDERS = [
    ("flow", _direction_el), ("conviction", _conviction_el), ("positioning", _positioning_el),
    ("dealer_gamma", _structural_el), ("skew", _skew_el), ("cost", _cost_el),
]

_VERDICT_LOGIC = ("PERFECT only when every gate is green at once — the conjunction is "
                  "the model, there are no weights and no balance-of-evidence. Anything "
                  "less is NOT NOW with the count. A gray gate is an unknown and counts "
                  "as not-green. Time stops: drift 3 days, puts 2, catalyst exits on the "
                  "report day before the IV crush.")


def _time_stop(verdict: Verdict) -> str:
    if verdict.branch == "catalyst":
        return "exit on the report day"
    return "time-stop 2 days" if verdict.direction == "puts" else "time-stop 3 days"


def _next_step(verdict: Verdict, cost) -> str:
    """The 'so what do I do' line. PERFECT: act, with the time stop. NOT NOW: what's
    missing, blocking gate first — doing nothing is the default state of this product."""
    if verdict.overall == "PERFECT":
        return (f"All gates green for {verdict.direction}. The ticket below is the "
                f"trade; size so a full loss is fine, {_time_stop(verdict)}.")
    waiting = verdict.reasons[0] if verdict.reasons else "data"
    return f"Waiting on: {waiting}."


def _numbers(verdict: Verdict, cost) -> list[str]:
    """The ONLY numerals on the default render (directive §3): shown when PERFECT or
    within 2 gates of it. Spread, breakeven-vs-move, the time stop, and the ticket."""
    best = verdict.calls if verdict.direction == "calls" else verdict.puts
    if best is None or (best.state != "PERFECT" and best.green < best.total - 2):
        return []
    out: list[str] = []
    if cost is not None and cost.spread_pct is not None:
        out.append(f"spread {cost.spread_pct:.0f}% of premium")
    if (cost is not None and cost.breakeven_move_pct is not None
            and cost.expected_move_pct is not None):
        out.append(f"needs {cost.breakeven_move_pct:.1f}% — market expects "
                   f"{cost.expected_move_pct:.1f}%")
    out.append(_time_stop(verdict))
    ct = (cost.contract or {}) if cost else {}
    if ct.get("ask"):
        out.append(f"{ct['strike']:g} {ct['type']} {ct['dte']}d ${ct['ask']:.2f} · "
                   f"max loss ${ct['ask'] * 100:,.0f}")
    return out[:4]


def _price_el(candles: list[dict], provenance) -> Element:
    """The session's regular-hours 15m candles — price CONTEXT for the chart UI (walls,
    flip, and breakeven overlay on this axis). Not a signal; never feeds the verdict."""
    last, first = candles[-1], candles[0]
    chg = (last["c"] - first["o"]) / first["o"] * 100 if first["o"] else 0.0
    return Element(key="price", label="Today's tape", surface=f"${last['c']:g}",
                   meaning=f"{chg:+.2f}% session · {len(candles)} 15m bars",
                   logic="price context only. the call never comes from price",
                   detail={"Open": f"${first['o']:g}", "Last": f"${last['c']:g}",
                           "High": f"${max(c['h'] for c in candles):g}",
                           "Low": f"${min(c['l'] for c in candles):g}"},
                   series={"kind": "candles", "points": candles},
                   tone="neutral", provenance=provenance)


def present(ticker: str, signals: dict[str, Signal], verdict: Verdict,
            *, as_of: str | None = None, market: dict | None = None,
            candles: list[dict] | None = None) -> ViewModel:
    """Assemble the renderable view model. Verdict forwarded VERBATIM. The default render
    is lights-only (verdict blocks + gate dots + <=4 numbers); `elements` are the why-
    panel content — everything deleted from the default lives behind ONE disclosure."""
    elements: list[Element] = []
    if candles:
        flow = signals.get("flow")
        elements.append(_price_el(candles, flow.provenance if flow else None))
    for name, build in _BUILDERS:
        if name in signals:
            elements.append(build(signals[name]))
            if name == "flow":                  # one signal, two tiles: the side + its timing
                tl = _timeline_el(signals[name])
                if tl is not None:
                    elements.append(tl)
            if name == "cost":                  # one signal, two tiles: the gate + the pick
                elements.append(_contract_el(signals[name]))

    flow = signals.get("flow")
    if getattr(flow, "truncated", False):
        elements.append(Element(
            key="flow_truncation", label="Partial flow window", surface="partial",
            meaning="feed hit its 500 cap, oldest alerts missing", tone="cautionary",
            detail={"note": "flow-alerts hit the 500 page cap, older alerts not included"},
            provenance=flow.provenance))

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     regime=_market_el(market) if market else None,
                     verdict_logic=_VERDICT_LOGIC,
                     next_step=_next_step(verdict, signals.get("cost")),
                     numbers=_numbers(verdict, signals.get("cost")),
                     elements=elements, verdict=verdict)
