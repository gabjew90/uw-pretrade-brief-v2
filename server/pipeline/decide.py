"""Stage 4 — DECIDE → verdict, in exactly ONE place.

Consumes the name→Signal map from Derive BY NAME, applies the funnel, emits a `Verdict`
with named reasons + the signals it used. The only module allowed to produce a verdict;
the frontend never re-derives it (the one rule).

The funnel is ported from `e1d6c5e:server/verdict.py::compute_verdict` + `positioning_leg`
+ `skew_state`, with the v3 combination STRUCTURE locked by the decide spec:

- **Flow + OI are ONE family** → collapsed by `_positioning_leg` (the side + its strength).
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
"""
from __future__ import annotations

from server.models import Provenance, Signal, Verdict

_CORE = ("flow",)


def _positioning_leg(flow, positioning) -> str:
    """Collapse Flow + OI into green/yellow/red (the side's strength). Green requires the
    OPENING basis; total_flow caps at yellow; OI unwinding caps green→yellow; OI building
    or flat/unconfirmed leaves opening flow standing (green). `unconfirmed` NEVER blocks
    (archive-decoupled). Ported from `e1d6c5e:verdict.py::positioning_leg`."""
    basis = getattr(flow, "direction_basis", "unavailable")
    if basis == "gamma_fallback":
        return "yellow"                       # gamma-only side is weak (skeleton has none yet)
    if basis == "total_flow":
        return "yellow"                       # weaker basis — caps below Favorable
    if basis != "opening_flow":
        return "red"
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
                       reasons=[f"flow unavailable: {note}"],
                       signals_used=["flow"] if flow is not None else [],
                       provenance=flow.provenance if flow else Provenance())
    direction = flow.direction

    positioning = signals.get("positioning")
    if positioning is not None:
        used.append("positioning")
    positioning_color = _positioning_leg(flow, positioning)
    if flow.direction_basis == "opening_flow":
        reasons.append(f"opening flow favors {direction}")
    else:
        reasons.append(f"flow favors {direction} on the weaker {flow.direction_basis} basis")
    conf = getattr(positioning, "confirmation", "unconfirmed") if positioning else "unconfirmed"
    if conf == "building":
        reasons.append("OI is building at the flow strikes (corroborates)")
    elif conf == "unwinding":
        reasons.append("OI is unwinding — the buying looks like closing (caps the read)")
    elif conf == "unconfirmed":
        reasons.append("OI history unconfirmed — flow stands on its own (not blocking)")

    conflict_legs: list[str] = []

    # ── conviction: SAME flow family → divergence-only caution, no agreement bonus ──
    conviction = signals.get("conviction")
    if conviction is not None:
        used.append("conviction")
        cdir = getattr(conviction, "direction", None)
        if cdir is None:
            reasons.append("greek-flow conviction unavailable (partial coverage)")
        elif cdir != direction:
            conflict_legs.append("conviction")
            reasons.append(f"greek-flow conviction diverges ({cdir} vs {direction}) — caution")
        # agreement: deliberately NO reason/bonus — concordant same-family adds nothing

    # ── skew: orthogonal, asymmetric oppose-veto ──────────────────────────────
    skew = signals.get("skew")
    if skew is not None:
        used.append("skew")
    skew_st = _skew_state(skew, direction)
    if skew_st == "oppose" and positioning_color in ("green", "yellow"):
        conflict_legs.append("skew")
        reasons.append(f"skew opposes {direction} (vol surface leans the other way)")
    elif skew_st == "unavailable":
        reasons.append("skew unavailable (partial coverage)")
    # agree is subordinate — never an upgrade, never a peer green (no reason added)

    # ── structural guard (dealer gamma) ───────────────────────────────────────
    dealer_gamma = signals.get("dealer_gamma")
    if dealer_gamma is not None:
        used.append("dealer_gamma")
    structural = _structural(dealer_gamma)
    if structural == "yellow":
        reasons.append("POS dealer gamma — pinning regime resists directional weeklies (caps)")
    elif structural == "unavailable":
        reasons.append("dealer gamma unavailable (partial coverage)")

    # ── cost guard ────────────────────────────────────────────────────────────
    cost = signals.get("cost")
    if cost is not None:
        used.append("cost")
    cost_guard = getattr(cost, "guard", "caution") if cost else "caution"
    if cost is not None and getattr(cost, "reason", ""):
        reasons.append(f"cost: {cost.reason}")

    signal_conflict = bool(conflict_legs)

    # ── resolution ────────────────────────────────────────────────────────────
    # (Market regime is NOT a per-ticker leg — its only decision-relevant datum, a macro
    # event in the hold window, routes through Cost above. The verdict rests on the
    # ticker's own evidence.)
    if positioning_color == "red" or cost_guard == "block":
        overall = "Stand down"
    elif (positioning_color == "green" and not signal_conflict and cost_guard == "ok"
          and skew_st != "oppose" and structural == "green"):
        overall = "Favorable"
    else:
        overall = "Mixed"

    if overall == "Favorable":
        action = f"Favorable — lean {direction}"
    elif overall == "Stand down":
        action = "Stand down"
    elif signal_conflict:
        action = f"Mixed — signals disagree ({', '.join(conflict_legs)})"
    else:
        action = f"Mixed — {direction} not compelling"

    consumed = [signals[n].provenance for n in used if signals.get(n) is not None]
    return Verdict(action=action, overall=overall, direction=direction, reasons=reasons,
                   signals_used=used, signal_conflict=signal_conflict,
                   conflict_legs=conflict_legs,
                   provenance=Provenance.worst(*consumed) if consumed else Provenance())
