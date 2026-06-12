"""Stage 4 — DECIDE → the strict-conjunction verdict, in exactly ONE place.

Directive 2026-06-12: the question changed from "is the evidence favorable, on
balance?" to "are ALL conditions for a perfect long call/put present simultaneously,
right now?" The honest answer is binary: PERFECT or NOT NOW — n/N. 'Mixed' and
'Favorable' are BANNED words (they emitted a daily shrug carrying no per-glance
information).

Both directions are evaluated every cycle. Branch: CATALYST when an earnings report
falls inside the hold window (<= 3 trading days), else DRIFT. Gate thresholds and
evaluators live in `gates.py`, one citation beside each constant. DARK (input
unavailable) counts as not-green — conservatism preserved — but renders gray.
The old hard cases fold in: cost-block and flow-unavailable are just RED/DARK gates,
named FIRST in the waiting-on line (put squeeze RED also names first).

`Verdict.caps` (non-green gate names of the best direction) keeps the backtest's
gate-binding histogram contract: "is PERFECT reachable, and which gate binds most?"
is answered from replay data, not vibes.

Hold-window honesty: NEG gamma helps moves extend, but excess short-horizon momentum
reverts over days (Baltussen et al.) — time stops are drift 3 days, puts 2, catalyst
exit on report day.
"""
from __future__ import annotations

from server.models import DirectionCall, GateResult, Provenance, Signal, Verdict
from server.pipeline.gates import EARN_WINDOW_D, evaluate

# the gates whose failure means "this isn't tradeable at all" — named first
_BLOCKING_FIRST = ("no_squeeze", "flow_dominance", "cost")


def _waiting_on(gates: list[GateResult]) -> str:
    bad = [g for g in gates if g.state != "GREEN"]
    bad.sort(key=lambda g: (g.state != "RED" or g.name not in _BLOCKING_FIRST,
                            g.name not in _BLOCKING_FIRST))
    names = [g.label for g in bad]
    if len(names) > 3:
        names = names[:3] + [f"+{len(names) - 3} more"]
    return ", ".join(names)


def _direction_call(direction: str, branch: str, signals: dict) -> DirectionCall:
    gates = evaluate(direction, branch, signals)
    green = sum(1 for g in gates if g.state == "GREEN")
    state = "PERFECT" if green == len(gates) else "NOT NOW"
    return DirectionCall(direction=direction, state=state, green=green,
                         total=len(gates), branch=branch, gates=gates,
                         waiting_on=_waiting_on(gates) if state == "NOT NOW" else "")


def decide(signals: dict[str, Signal]) -> Verdict:
    cat = signals.get("catalyst")
    dte_e = getattr(cat, "days_to_earnings", None) if cat else None
    if dte_e is None:
        cost = signals.get("cost")
        dte_e = getattr(cost, "days_to_earnings", None) if cost else None
    branch = "catalyst" if (dte_e is not None and dte_e <= EARN_WINDOW_D) else "drift"

    calls = _direction_call("calls", branch, signals)
    puts = _direction_call("puts", branch, signals)

    # best direction: PERFECT wins; else more green; tie -> the flow side
    flow = signals.get("flow")
    flow_dir = getattr(flow, "direction", None) if flow else None
    if calls.state == "PERFECT":
        best = calls
    elif puts.state == "PERFECT":
        best = puts
    elif calls.green != puts.green:
        best = calls if calls.green > puts.green else puts
    else:
        best = puts if flow_dir == "puts" else calls

    if best.state == "PERFECT":
        action = f"PERFECT — {best.direction.upper()}"
    else:
        action = f"NOT NOW — {best.green}/{best.total}"

    used = [n for n in ("flow", "positioning", "conviction", "skew", "dealer_gamma",
                        "cost", "vol", "shorts", "catalyst") if signals.get(n) is not None]
    reasons = [best.waiting_on] if best.waiting_on else []
    caps = [g.name for g in best.gates if g.state != "GREEN"]
    consumed = [signals[n].provenance for n in used]
    return Verdict(action=action, overall=best.state, direction=best.direction,
                   branch=branch, calls=calls, puts=puts, reasons=reasons,
                   signals_used=used, caps=caps,
                   provenance=Provenance.worst(*consumed) if consumed else Provenance())
