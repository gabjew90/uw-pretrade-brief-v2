"""Stage 4 — DECIDE → verdict, in exactly ONE place.

Consumes the name→Signal map from Derive BY NAME, applies the funnel, emits a `Verdict`
with named reasons + the signals it used. The only module allowed to produce a verdict;
the frontend never re-derives it (the one rule).

The funnel is ported from `e1d6c5e:server/verdict.py::compute_verdict` + `positioning_leg`
+ `skew_state`, with the v3 combination STRUCTURE locked by the decide spec:

- **Flow + OI are ONE family** → collapsed by `_positioning_leg` (the side + its strength).
- **The side meets its OWN bar** (reviewer 2026-06-11): a lean below 2:1 dominance or the
  premium floor is "weak" → caps at Mixed. Favorable must never mean "weak evidence,
  unopposed" — that is exactly the false confidence the rest of the funnel prevents.
- **Conviction (greek-flow) is the SAME flow family** → agreement adds NOTHING; only
  DIVERGENCE vs the flow side is informative and acts as a caution (no agreement bonus).
- **Skew is the orthogonal leg** → asymmetric: opposition vetoes/caps; agreement is
  subordinate corroboration, never a peer green, never an upgrade.
- **Dealer-gamma (structural) and Cost are GUARDS, not direction** → POS gamma caps below
  Favorable; cost block → Stand down. They never add a green.
- **Regime is advisory** → Stand down posture caps below Favorable; never a direction.
- General rule: **divergence vetoes; agreement never promotes.**

Honest-degrade is GATED as behavior: an unavailable CORE input (flow) → Stand down with
the reason NAMED; unavailable non-core inputs take the conservative path AND are named.

Hold-window honesty: NEG gamma helps moves extend, but excess short-horizon momentum tends
to REVERT over the following days (Baltussen et al.) — so the product's guidance leans
1–3 day holds with a time stop, never riding the week.
"""
from __future__ import annotations

from server.models import Provenance, Signal, Verdict

_CORE = ("flow",)


def _positioning_leg(flow, positioning) -> str:
    """Collapse Flow + OI into green/yellow (the side's strength). Green requires the
    OPENING basis AND the lean meeting its own bar (dominance + floor — the Pan-Poteshman
    edge is in the EXTREME of signed flow, not its sign); total_flow caps at yellow; a weak
    lean caps at yellow; OI unwinding caps green→yellow; OI building or flat/unconfirmed
    leaves opening flow standing (green). `unconfirmed` NEVER blocks (archive-decoupled).
    There is deliberately no 'red' path: an unavailable basis implies direction None, which
    the core gate already turned into Stand down (dead branch deleted, reviewer 2026-06-11).
    Ported from `e1d6c5e:verdict.py::positioning_leg`."""
    basis = getattr(flow, "direction_basis", "unavailable")
    if basis != "opening_flow":
        return "yellow"                       # total_flow / gamma_fallback — weaker basis
    if getattr(flow, "lean_quality", "n/a") == "weak":
        return "yellow"                       # side picked, but on coin-flip evidence
    conf = getattr(positioning, "confirmation", "unconfirmed") if positioning else "unconfirmed"
    if conf == "unwinding":
        return "yellow"                       # the 'buying' was closing → cap down
    return "green"                            # building / flat / unconfirmed → flow stands


def _skew_state(skew, direction) -> str:
    """agree | oppose | neutral | unavailable, given the flow side. calls want call-skew,
    puts want put-skew. The asymmetry (oppose vetoes, agree subordinate) is applied in the
    funnel below, not here. Ported from `e1d6c5e:verdict.py::skew_state`."""
    lean = getattr(skew, "lean", "unavailable") if skew else "unavailable"
    if lean == "unavailable":
        return "unavailable"
    if lean == "neutral":
        return "neutral"
    bullish = lean == "call_skew"
    if direction == "calls":
        return "agree" if bullish else "oppose"
    return "oppose" if bullish else "agree"   # puts


def _structural(dealer_gamma) -> str:
    """green | yellow | unavailable. NEG index gamma = trend (moves extend → green); POS =
    pin/chop (resists directional weeklies → caps at yellow, signal-honesty Plan 2). A GUARD,
    never a direction. (A hard 'red' wall-proximity rule is operator-deferred.)"""
    if dealer_gamma is None or getattr(dealer_gamma, "flip_status", "unavailable") == "unavailable":
        return "unavailable"
    return "green" if dealer_gamma.gex_sign == "NEG" else "yellow"


def decide(signals: dict[str, Signal]) -> Verdict:
    flow = signals.get("flow")
    used: list[str] = ["flow"]
    reasons: list[str] = []

    # ── honest-degrade: unavailable CORE input (flow) → Stand down, named ──────
    if flow is None or getattr(flow, "direction", None) is None:
        note = flow.provenance.note if flow else "no flow signal"
        return Verdict(action="Stand down", overall="Stand down",
                       reasons=[f"flow n/a: {note}"], caps=["flow n/a"],
                       signals_used=["flow"] if flow is not None else [],
                       provenance=flow.provenance if flow else Provenance())
    direction = flow.direction
    lean_weak = getattr(flow, "lean_quality", "n/a") == "weak"

    positioning = signals.get("positioning")
    if positioning is not None:
        used.append("positioning")
    positioning_color = _positioning_leg(flow, positioning)
    if flow.direction_basis == "opening_flow":
        reasons.append(f"opening flow {direction}")
    else:
        reasons.append(f"{direction} on {flow.direction_basis} flow")
    if lean_weak:
        reasons.append(f"flow lean weak ({flow.lean_note})")
    conf = getattr(positioning, "confirmation", "unconfirmed") if positioning else "unconfirmed"
    if conf == "building":
        reasons.append("OI building")
    elif conf == "unwinding":
        reasons.append("OI unwinding")
    elif conf == "unconfirmed":
        reasons.append("OI unconfirmed")

    conflict_legs: list[str] = []

    # ── conviction: SAME flow family → divergence-only caution, no agreement bonus ──
    conviction = signals.get("conviction")
    if conviction is not None:
        used.append("conviction")
        cdir = getattr(conviction, "direction", None)
        if cdir is None:
            reasons.append("tape n/a")
        elif cdir != direction:
            conflict_legs.append("conviction")
            reasons.append(f"tape {cdir} vs flow {direction}")
        # agreement: deliberately NO reason/bonus — concordant same-family adds nothing

    # ── skew: orthogonal, asymmetric oppose-veto ──────────────────────────────
    skew = signals.get("skew")
    if skew is not None:
        used.append("skew")
    skew_st = _skew_state(skew, direction)
    if skew_st == "oppose" and positioning_color in ("green", "yellow"):
        conflict_legs.append("skew")
        reasons.append(f"skew opposes {direction}")
    elif skew_st == "unavailable":
        reasons.append("skew n/a")
    # agree is subordinate — never an upgrade, never a peer green (no reason added)

    # ── structural guard (dealer gamma) ───────────────────────────────────────
    dealer_gamma = signals.get("dealer_gamma")
    if dealer_gamma is not None:
        used.append("dealer_gamma")
    structural = _structural(dealer_gamma)
    if structural == "yellow":
        reasons.append("gamma pinned")
    elif structural == "unavailable":
        reasons.append("gamma n/a")

    # ── cost guard ────────────────────────────────────────────────────────────
    cost = signals.get("cost")
    if cost is not None:
        used.append("cost")
    cost_guard = getattr(cost, "guard", "caution") if cost else "caution"
    if cost is not None and getattr(cost, "reason", ""):
        reasons.append(f"cost: {cost.reason}")

    signal_conflict = bool(conflict_legs)

    # ── resolution ────────────────────────────────────────────────────────────
    # `caps` names EVERY gate that blocked Favorable (not just the first) — the backtest
    # aggregates them into the gate-binding histogram, so "is Favorable reachable at all,
    # and which gate binds most?" is answered from data, not vibes (reviewer 2026-06-11).
    # (Market regime is NOT a per-ticker leg — its only decision-relevant datum, a macro
    # event in the hold window, routes through Cost above. The verdict rests on the
    # ticker's own evidence.)
    caps: list[str] = []
    if flow.direction_basis != "opening_flow":
        caps.append(f"basis {flow.direction_basis}")
    if lean_weak:
        caps.append("weak lean")
    conf_p = getattr(positioning, "confirmation", "unconfirmed") if positioning else "unconfirmed"
    if conf_p == "unwinding":
        caps.append("OI unwinding")
    if "conviction" in conflict_legs:
        caps.append("tape diverges")
    if skew_st == "oppose":
        caps.append("skew opposes")
    elif skew_st == "unavailable":
        # skew dark caps at Mixed (like structural): with the orthogonal leg unreadable,
        # the divergence check can't run, so the rare green is not certifiable. Neutral
        # skew (readable, no lean) still permits Favorable.
        caps.append("skew dark")
    if structural == "yellow":
        caps.append("gamma pinned")
    elif structural == "unavailable":
        caps.append("gamma dark")
    if cost_guard == "block":
        caps.append("cost block")
    elif cost_guard != "ok":
        caps.append("cost flags")

    if cost_guard == "block":
        overall = "Stand down"
    elif not caps:
        overall = "Favorable"
    else:
        overall = "Mixed"

    if overall == "Favorable":
        action = f"Favorable {direction}"
    elif overall == "Stand down":
        action = "Stand down"
    elif signal_conflict:
        action = f"Mixed, {', '.join(conflict_legs)} disagree"
    else:
        action = f"Mixed, {direction} weak"

    consumed = [signals[n].provenance for n in used if signals.get(n) is not None]
    return Verdict(action=action, overall=overall, direction=direction, reasons=reasons,
                   signals_used=used, signal_conflict=signal_conflict,
                   conflict_legs=conflict_legs, caps=caps,
                   provenance=Provenance.worst(*consumed) if consumed else Provenance())
