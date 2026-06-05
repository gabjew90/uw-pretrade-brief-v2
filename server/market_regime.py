"""Pure market-regime synthesis for the header. NO I/O — callers fetch the
inputs and pass them in. Produces a structured, plain-English regime read that
is a REGIME read (trend vs chop, calm vs fearful, event vs clear) and NEVER a
market-direction call. Posture mirrors the per-ticker verdict at the market
level: Favorable / Mixed / Stand down, willing to stand down on chop days."""
from __future__ import annotations
from datetime import datetime, timezone

_HOLD_DAYS = 5  # weeklies held 1-5d: an event within this window is crossable

_HIGH_IMPACT = ("fomc", "cpi", "consumer price", "nonfarm", "payroll",
                "jobs report", "employment situation", "pce", "fed rate",
                "interest rate decision", "ppi")


def _parse_time(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_high_impact(ev: dict) -> bool:
    if (ev.get("type") or "").lower() == "fomc":
        return True
    name = (ev.get("event") or "").lower()
    return any(k in name for k in _HIGH_IMPACT)


def _next_event(events, now):
    best = None
    for ev in events or []:
        if not _is_high_impact(ev):
            continue
        t = _parse_time(ev.get("time"))
        if t is None or t <= now:
            continue
        days = (t - now).total_seconds() / 86400.0
        if days > _HOLD_DAYS:
            continue
        if best is None or t < best[0]:
            best = (t, ev, days)
    return best


def compute_market_regime(*, gamma: dict, vol: dict, events: list,
                          tide: dict, opex: bool, now: datetime) -> dict:
    sign = (gamma or {}).get("sign")
    status = (gamma or {}).get("status")

    if status != "ok" or sign not in ("POS", "NEG"):
        headline = "Index gamma unavailable — market regime unclear."
        base = "Mixed"
    elif sign == "NEG":
        headline = "Trend regime — moves likely to extend (favorable for directional weeklies)."
        base = "Favorable"
    else:
        headline = "Pinned / chop regime — moves likely to fade (premium decays; hard for weeklies)."
        base = "Stand down"

    nxt = _next_event(events, now)
    event_within_hold = nxt is not None
    if nxt is None:
        event = {"line": None, "severity": None, "days": None}
    else:
        t, ev, days = nxt
        name = ev.get("event") or (ev.get("type") or "event").upper()
        if days <= 1:
            event = {"line": f"{name} within ~1d — don't initiate weeklies into it.",
                     "severity": "veto", "days": round(days, 1)}
        else:
            event = {"line": f"{name} in ~{int(round(days))}d — any weekly you open now will likely cross it.",
                     "severity": "warn", "days": round(days, 1)}

    iv, rv, trend = (vol or {}).get("iv"), (vol or {}).get("rv"), (vol or {}).get("trend")
    if iv is None:
        vol_line = "Vol environment unavailable."
        vol_cheap, crush_risk = False, False
    else:
        cheap = (rv is None or iv <= rv) and iv <= 0.22
        vol_cheap = cheap
        crush_risk = (iv > 0.25) and (trend == "falling")
        if cheap:
            vol_line = "Options are cheap to own — calm vol."
        elif crush_risk:
            vol_line = "Vol elevated and falling — IV-crush risk on anything you buy."
        else:
            vol_line = "Vol middling — neither a tailwind nor a clear warning."

    lean = (tide or {}).get("lean", "neutral")
    tide_badge = {"bull": "tape flow leaning risk-on", "bear": "tape flow leaning risk-off"}.get(
        lean, "tape flow neutral")
    tide_hostile = lean == "bear"

    if event.get("severity") == "veto":
        posture = "Stand down"
    else:
        posture = base
        if posture == "Stand down" and vol_cheap and not tide_hostile:
            posture = "Mixed"
        elif posture == "Favorable" and (crush_risk or tide_hostile or event.get("severity") == "warn"):
            posture = "Mixed"

    return {
        "headline": headline,
        "event": event,
        "vol": vol_line,
        "tide_badge": tide_badge,
        "opex": bool(opex),
        "posture": posture,
        "event_within_hold": event_within_hold,
    }
