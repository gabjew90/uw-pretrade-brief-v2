"""Stage 5 — PRESENT → view model → dumb frontend.

Per element `{label, surface, meaning, logic, detail, provenance, tone}`. `surface` is the
glance, `meaning` is a terse NUMBER readout (the data behind the word), `logic` is the RULE
(how the word is decided from that data), `detail` is the tap payload. Values are formatted
HERE so the frontend computes NOTHING. A market `regime` header + a `verdict_logic` line
expose how the market read and the overall call are reached. Numbers speak: no em-dashes,
no semicolons.
"""
from __future__ import annotations

from datetime import date

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


# skew tile DELETED with the skew leg (four-lights directive: a metric that never
# flips the decision must not exist in the product; MPP 2022 borrow-fee artifact)
_BUILDERS = [
    ("flow", _direction_el), ("conviction", _conviction_el), ("positioning", _positioning_el),
    ("dealer_gamma", _structural_el), ("cost", _cost_el),
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
    one gate short. Spread, breakeven-vs-move, the time stop, and the ticket."""
    best = verdict.calls if verdict.direction == "calls" else verdict.puts
    if best is None or (best.state != "PERFECT" and best.green < best.total - 1):
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


# ════════════════════════════════════════════════════════════════════════════
# The v3 frontend contract (Present Contract Extensions, 2026-06-12): DirectionVM /
# GateVM / FlowStripVM / WhyVM. Every string authored HERE — the client's only math
# is pixel geometry. Threshold constants that feed visuals are emitted from gates.py
# so the frontend can never hardcode-drift them.
from server.pipeline.gates import (ASK_SHARE_MIN, BE_EM_MAX, BUILD_NET_VS_HIGH_MIN,
                                   IVR_MAX, SHORT)

_BLOCK_FIRST = ("no_squeeze", "smart_flow", "good_entry")


def _prov_note(p) -> str:
    """'live · as of 12:42 UTC' — subtext-only (never on the default render)."""
    m = {"live": "live", "cache": "cached", "archive": "archive", "derived": "derived"}
    src = m.get(getattr(getattr(p, "source", None), "value", ""), "derived")
    as_of = getattr(p, "as_of", None)
    return f"{src} · as of {as_of[11:16]} UTC" if as_of else f"{src} · no as_of"


def _ampm(hhmm: str) -> str:
    """'09:33' -> '9:33a' (display only)."""
    try:
        h, m = int(hhmm[:2]), hhmm[3:5]
    except (TypeError, ValueError):
        return hhmm
    return f"{h % 12 or 12}:{m}{'a' if h < 12 else 'p'}"


def _daily_vol(v) -> float | None:
    """Annualized vol -> %/day (display convention of the cheap_vol visual)."""
    return round(v / (252 ** 0.5) * 100, 1) if v is not None else None


def _flow_strip(direction: str, flow) -> dict | None:
    """FlowStripVM: net cumulative premium sign-adjusted to the card's direction, the
    side's top prints as dots, anchors authored here. buildFrac emits the gates.py
    still-building constant so the shaded zone can't drift."""
    if flow is None or not getattr(flow, "flow_series", None):
        return None
    side = "call" if direction == "calls" else "put"
    sgn = 1 if direction == "calls" else -1
    st = (flow.side_stats or {}).get(side) or {}
    age = st.get("last_age_min")
    # minutes are DATA-anchored (vs the session's newest print, replay-deterministic and
    # meaningful for the evening/pre-market read) — the label says so honestly instead
    # of implying wall-clock "now" ("last buy 0m ago" at midnight was a lie)
    end_t = _ampm(flow.flow_series[-1]["t"])
    if age is None:
        end_note = f"thru {end_t}"
    elif age < 1:
        end_note = f"thru {end_t} · bought into the last print"
    else:
        end_note = f"thru {end_t} · last buy {age:.0f}m before that"
    return {"pts": [float((p["call"] - p["put"]) * sgn) for p in flow.flow_series],
            "alerts": (flow.flow_marks or {}).get(side, []),
            "total": _money(st.get("opening_prem", 0)),
            "startNote": _ampm(flow.flow_series[0]["t"]),
            "endNote": end_note,
            "buildFrac": BUILD_NET_VS_HIGH_MIN}


def _subtext(g) -> str:
    return " · ".join(list(g.values) + [_prov_note(g.provenance)])


def _why(g, direction: str, signals: dict) -> dict:
    """WhyVM: one micro-visual per gate, marker-vs-pass-zone, value-anchored. DARK
    chart-gates carry missing[] and draw nothing. no_squeeze is a checklist only."""
    other = "puts" if direction == "calls" else "calls"
    flow, dg, cost, v = (signals.get("flow"), signals.get("dealer_gamma"),
                         signals.get("cost"), signals.get("vol"))
    sgn = "+" if direction == "calls" else "−"

    if g.name == "no_squeeze":
        sh, c = signals.get("shorts"), signals.get("conviction")
        ftd = getattr(sh, "ftd_pctile", None) if sh else None
        spike = getattr(v, "iv_spike_pct", None) if v else None
        cdir = getattr(c, "direction", None) if c else None
        items = [
            ([None, "Delivery failures — no FTD history"] if ftd is None else
             [ftd <= 90, "No delivery failures piling up" if ftd <= 90 else
              f"Delivery failures piling up — {ftd:.0f}th pct of its own year"]),
            ([None, "Panic premium — no IV history"] if spike is None else
             [spike <= 20, "No panic premium in the last 2 days" if spike <= 20 else
              f"Panic premium — option prices {spike:+.0f}% in 2 days"]),
            ([None, "Tape direction — no greek-flow"] if cdir is None else
             [cdir == "puts", "The tape is pushing down too" if cdir == "puts" else
              "The tape is pushing up against this bet"]),
        ]
        caption = {"GREEN": "no trap conditions on the short side",
                   "RED": "betting on a drop into squeeze conditions is how puts get "
                          "eaten — this is a hard veto",
                   "DARK": "unreadable this cycle — counted against the verdict, "
                           "never guessed"}[g.state]
        return {"caption": caption, "subtext": _subtext(g), "items": items}

    if g.state == "DARK":
        return {"kind": None, "caption": None, "subtext": _prov_note(g.provenance),
                "missing": list(g.missing) or ["input unavailable"]}

    if g.name == "smart_flow":
        st = (getattr(flow, "side_stats", None) or {}).get(
            "call" if direction == "calls" else "put") or {}
        ost = (getattr(flow, "side_stats", None) or {}).get(
            "call" if direction == "puts" else "put") or {}
        total = (st.get("opening_prem", 0) or 0) + (ost.get("opening_prem", 0) or 0)
        left = round(st.get("opening_prem", 0) / total * 100) if total else 0
        if g.state == "GREEN":
            caption = f"{st.get('n_prints', 0)} qualifying {direction} prints since the open"
        elif getattr(flow, "direction", None) not in (direction, None):
            caption = "the money is on the other side today"
        else:
            caption = "a lean, but it fails its own bar — see the checks"
        return {"kind": "tug", "caption": caption, "subtext": _subtext(g),
                "data": {"leftPct": left,
                         "leftLabel": f"{_money(st.get('opening_prem', 0))} {direction} ({left}%)",
                         "rightLabel": f"{_money(ost.get('opening_prem', 0))} {other}",
                         "threshPct": round(ASK_SHARE_MIN * 100),
                         "threshLabel": f"{ASK_SHARE_MIN:.0%} needed"}}

    if g.name == "dealer_fuel":
        flip = getattr(dg, "flip_pct", None) if dg else None
        wall = (getattr(dg, "call_wall_pct", None) if direction == "calls"
                else getattr(dg, "put_wall_pct", None)) if dg else None
        wsgn = 1 if direction == "calls" else -1
        em = getattr(cost, "expected_move_pct", None) if cost else None
        spot_p = getattr(dg, "spot", None) if dg else None
        room = (f"{abs(wall) / em:.1f} expected moves of room"
                if g.state == "GREEN" and wall is not None and em
                else "fuel is on the other side today" if g.state == "RED"
                else "")
        caption = ("market makers amplify the move from here" if g.state == "GREEN"
                   else "dealers would resist this move, not fuel it")
        if spot_p:
            # REAL price space: dollar labels, dollar geometry, gamma rungs as bars
            flip_p = spot_p * (1 + (flip or 0.0) / 100)
            wall_p = spot_p * (1 + wsgn * abs(wall) / 100) if wall is not None else spot_p
            data = {"spot": spot_p, "flip": round(flip_p, 2), "wall": round(wall_p, 2),
                    "spotLabel": f"${spot_p:,.2f}", "spotNote": "you are here",
                    "flipLabel": f"${flip_p:,.0f}",
                    "flipNote": f"fuel off {'below' if direction == 'calls' else 'above'}",
                    "wallLabel": f"${wall_p:,.0f}" if wall is not None else "n/a",
                    "wallNote": "ceiling" if direction == "calls" else "floor",
                    "roomLabel": room,
                    # the per-strike net-gamma rungs (signed $bn) — drawn as bars
                    "bars": [{"x": r["strike"], "v": r["net_b"]}
                             for r in (getattr(dg, "ladder", None) or [])]}
        else:
            data = {"spot": 0.0, "flip": flip if flip is not None else 0.0,
                    "wall": wsgn * abs(wall) if wall is not None else 0.0,
                    "spotLabel": "price", "spotNote": "you are here",
                    "flipLabel": f"{flip:+.1f}%" if flip is not None else "n/a",
                    "flipNote": "fuel off below" if direction == "calls" else "fuel off above",
                    "wallLabel": f"{wsgn * abs(wall):+.1f}%" if wall is not None else "n/a",
                    "wallNote": "ceiling" if direction == "calls" else "floor",
                    "roomLabel": room}
        return {"kind": "ladder", "caption": caption, "subtext": _subtext(g),
                "data": data}

    if g.name == "cheap_vol":
        hv_d, iv_d = _daily_vol(getattr(v, "hv", None)), _daily_vol(getattr(v, "iv_front", None))
        ivr = getattr(v, "ivr", None)
        caption = ("movement costs less than it's been delivering · calendar clear "
                   "through your hold" if g.state == "GREEN" else
                   f"charging {iv_d}%/day of movement, delivering {hv_d}% — you'd pay "
                   "for motion that isn't happening" if hv_d and iv_d else
                   "the options are rich for the movement on offer")
        return {"kind": "cheap_vol", "caption": caption, "subtext": _subtext(g),
                "data": {"actual": hv_d or 0.0, "charged": iv_d or 0.0,
                         "ivRank": ivr if ivr is not None else 0.0,
                         "actualTitle": "how much it actually moves (recent)",
                         "actualLabel": f"{hv_d}%/day" if hv_d else "n/a",
                         "chargedTitle": "what the options charge (this week)",
                         "chargedLabel": f"{iv_d}%/day" if iv_d else "n/a",
                         "rankTitle": "option price vs its past year",
                         "rankLabel": f"{ivr:.0f}/100" if ivr is not None else "n/a",
                         "leftAnchor": "cheapest ←", "rightAnchor": "→ priciest",
                         "rankPassMax": IVR_MAX}}

    if g.name == "good_entry":
        be = getattr(cost, "breakeven_move_pct", None) if cost else None
        em = getattr(cost, "expected_move_pct", None) if cost else None
        spread = getattr(cost, "spread_pct", None) if cost else None
        caption = ("the entry toll is already counted in your breakeven"
                   if g.state == "GREEN" else "the entry costs more than the edge")
        return {"kind": "runway", "caption": caption, "subtext": _subtext(g),
                "data": {"needPct": be or 0.0, "expectPct": em or 0.0,
                         "tollPct": spread or 0.0, "passFrac": BE_EM_MAX,
                         "needLabel": f"{sgn}{be:.1f}%" if be is not None else "n/a",
                         "needNote": "break even", "zeroLabel": "0%",
                         "expectLabel": f"{sgn}{em:.1f}% expected" if em is not None else "n/a"}}

    if g.name == "cheap_event":
        cat = signals.get("catalyst")
        moves = list(getattr(cat, "moves", None) or [])
        implied = getattr(cat, "implied_move_pct", None)
        avg = getattr(cat, "hist_move_pct", None)
        caption = (f"market charges ±{implied:.1f}% · the stock's own history says "
                   f"reports move it {avg:.1f}% on average"
                   if implied is not None and avg is not None else None)
        return {"kind": "dot_strip", "caption": caption, "subtext": _subtext(g),
                "data": {"moves": moves, "implied": implied or 0.0, "avg": avg or 0.0,
                         "impliedLabel": f"±{implied:.1f}%" if implied is not None else "n/a",
                         "impliedNote": "price of this report",
                         "avgLabel": f"avg {avg:.1f}%" if avg is not None else "n/a",
                         "dotsNote": f"● last {len(moves)} report moves"}}

    return {"kind": None, "caption": None, "subtext": _subtext(g), "missing": []}


def _gate_vm(g, direction: str, signals: dict) -> dict:
    vm = {"name": g.name, "state": g.state.lower(), "label": g.label,
          "short": SHORT.get(g.name, g.name), "why": _why(g, direction, signals)}
    if g.name == "smart_flow":
        vm["flow"] = _flow_strip(direction, signals.get("flow"))
    return vm


def _waiting_line(gates) -> str:
    bad = [g for g in gates if g.state != "GREEN"]
    # no_squeeze RED is the hard veto — absolutely first; then other blocking REDs
    bad.sort(key=lambda g: (not (g.name == "no_squeeze" and g.state == "RED"),
                            not (g.state == "RED" and g.name in _BLOCK_FIRST),
                            g.name not in _BLOCK_FIRST))
    return "Waiting on: " + ", ".join(SHORT.get(g.name, g.name) for g in bad)


def _tag(verdict: Verdict, signals: dict) -> str | None:
    if verdict.branch != "catalyst":
        return None
    cat = signals.get("catalyst")
    rd = getattr(cat, "report_date", None) if cat else None
    if rd:
        try:
            return f"EARNINGS {date.fromisoformat(rd).strftime('%a').upper()}"
        except ValueError:
            pass
    return "EARNINGS SOON"


def _number_pairs(dc, verdict: Verdict, cost) -> list | None:
    """Labeled pairs, ≤4, only at PERFECT or one gate short — decided HERE, never
    client-side."""
    if dc.state != "PERFECT" and dc.green < dc.total - 1:
        return None
    sgn = "+" if dc.direction == "calls" else "−"
    out = []
    if cost is not None and cost.spread_pct is not None:
        out.append(["Entry toll", f"{cost.spread_pct:.1f}% of ticket"])
    if (cost is not None and cost.breakeven_move_pct is not None
            and cost.expected_move_pct is not None):
        out.append(["Needs vs expects",
                    f"{sgn}{cost.breakeven_move_pct:.1f}% vs {sgn}{cost.expected_move_pct:.1f}%"])
    out.append(["Time stop", "exit on report day" if verdict.branch == "catalyst"
                else ("2 days" if dc.direction == "puts" else "3 days")])
    ct = (cost.contract or {}) if cost else {}
    if ct.get("ask"):
        cp = "C" if ct.get("type") == "call" else "P"
        exp = str(ct.get("expiry") or "")[5:].replace("-", "/")
        out.append(["Contract / max loss",
                    f"${ct['strike']:g}{cp} {exp} · ${ct['ask'] * 100:,.0f}"])
    return out[:4] if out else None


def _direction_vm(ticker: str, dc, verdict: Verdict, signals: dict) -> dict:
    return {"ticker": ticker, "direction": dc.direction.upper(),
            "tag": _tag(verdict, signals), "branch": dc.branch, "state": dc.state,
            "green": dc.green, "total": dc.total,
            "waiting": _waiting_line(dc.gates) if dc.state == "NOT NOW" else None,
            "gates": [_gate_vm(g, dc.direction, signals) for g in dc.gates],
            "numbers": (_number_pairs(dc, verdict, signals.get("cost"))
                        if getattr(signals.get("flow"), "direction", None) == dc.direction
                        else None)}


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

    # the ONLY default-render chart: the session's cumulative net opening premium,
    # sign-adjusted so "up" = building toward the best direction (its shape —
    # building / fading / reversed — is the still-building signal a dot can't carry)
    spark: list[float] = []
    spark_state = "DARK"
    best = verdict.calls if verdict.direction == "calls" else verdict.puts
    if flow is not None and getattr(flow, "flow_series", None):
        sgn = 1 if verdict.direction == "calls" else -1
        spark = [float((p["call"] - p["put"]) * sgn) for p in flow.flow_series]
    if best is not None:
        spark_state = next((g.state for g in best.gates if g.name == "smart_flow"),
                           "DARK")

    # the ONE why-panel chart: spot / flip / wall / breakeven on a single % axis
    dg, cost = signals.get("dealer_gamma"), signals.get("cost")
    ladder: list[dict] = [{"label": "price", "pct": 0.0}]
    if dg is not None and getattr(dg, "flip_status", "") == "ok" and dg.flip_pct is not None:
        ladder.append({"label": "gamma flip", "pct": round(dg.flip_pct, 2)})
    wall = (getattr(dg, "call_wall_pct", None) if verdict.direction == "calls"
            else getattr(dg, "put_wall_pct", None)) if dg else None
    if wall is not None:
        sign = 1 if verdict.direction == "calls" else -1
        ladder.append({"label": "wall", "pct": round(sign * abs(wall), 2)})
    be = getattr(cost, "breakeven_move_pct", None) if cost else None
    if be is not None:
        sign = 1 if verdict.direction == "calls" else -1
        ladder.append({"label": "breakeven", "pct": round(sign * be, 2)})

    asofs = sorted(s.provenance.as_of for s in signals.values() if s.provenance.as_of)
    return ViewModel(ticker=ticker, as_of=asofs[0] if asofs else as_of,
                     regime=_market_el(market) if market else None,
                     verdict_logic=_VERDICT_LOGIC,
                     next_step=_next_step(verdict, signals.get("cost")),
                     numbers=_numbers(verdict, signals.get("cost")),
                     spark=spark, spark_state=spark_state,
                     why_ladder=ladder if len(ladder) > 1 else [],
                     elements=elements, verdict=verdict,
                     best=verdict.direction or "calls",
                     calls=(_direction_vm(ticker, verdict.calls, verdict, signals)
                            if verdict.calls else None),
                     puts=(_direction_vm(ticker, verdict.puts, verdict, signals)
                           if verdict.puts else None))
