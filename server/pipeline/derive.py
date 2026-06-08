"""Stage 3 — DERIVE → signals, as PURE functions.

Each signal is a pure function: canonical records in, a typed `Signal` out, NO I/O
(no fetch, no clock-via-now, no storage). Time comes in as an argument (from the
clock) so the function stays deterministic and golden-testable. This is the layer that
catches sign inversions and gamma-direction bugs before they ship.

Signals (first-class entities in server.models): direction(Flow), conviction,
positioning(Positioning), dealer_gamma(DealerGamma), skew(Skew), cost(Cost),
regime(Regime). Each registered here as `derive_<name>(canon, *, asof) -> Signal`.

Stub: the pure functions + their golden tests are written per instructions. The
registry + purity contract are fixed now (lint/test should assert no I/O imports here).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                            Regime, Signal, Skew)
from server.services import provenance as prov

# 25Δ RR magnitude below which skew is "neutral" (no lean). v2 default; operator-deferred
# pending Phase-2 RR-magnitude review (SPY rides ~0.03–0.05, so 0.02 is a low gate).
_SKEW_THR = 0.02

# Cost gate thresholds (v2 GATE_THRESHOLDS; operator-deferred). IV rank bands + the
# don't-buy-premium-into-a-known-vol-event rule.
_IVR_GREEN_MAX = 60      # ivr <= 60 → ok (premium not rich)
_IVR_YELLOW_MAX = 80     # 60 < ivr <= 80 → caution
_EARNINGS_DAYS_MIN = 7   # earnings inside 7 days → block
_IVR_TARGET_DTE = 30     # IV-rank horizon (standard 30d); operator-deferred

# OI cluster trend bands (first→last settled session); operator-deferred.
_OI_BUILD_PCT = 5.0      # cluster OI up >= +5% → building (corroborates the flow)
_OI_UNWIND_PCT = -5.0    # cluster OI down <= -5% → unwinding (the 'buying' was closing)

REGISTRY: dict[str, Callable[..., Signal]] = {}


def register(name: str):
    def deco(fn: Callable[..., Signal]):
        REGISTRY[name] = fn
        return fn
    return deco


def derive_all(canon: dict, *, asof: str | None = None) -> dict[str, Signal]:
    """Run every registered signal function over the canonical inputs. Returns a
    name→Signal map (consumed BY NAME in Decide). A signal whose inputs are missing
    returns a Signal with quality=unavailable, never a fabricated value."""
    return {name: fn(canon, asof=asof) for name, fn in REGISTRY.items()}


def _opening(a) -> bool:
    """Opening intensity: today's volume exceeded prior OI (volume_oi_ratio > 1) = net-new
    positioning. Phase-2 live-confirmed this is THE opening proxy — UW's all_opening_trades
    flag is ~always False on Basic tier and cannot carry the signal."""
    try:
        return float(a.volume_oi_ratio or 0.0) > 1.0
    except (TypeError, ValueError):
        return False


@register("flow")
def derive_direction(canon: dict, *, asof: str | None = None) -> Flow:
    """Pick the call/put side. OPENING flow leads (Ge-Lin-Pearson: opening bets predict,
    closing bets don't); fall back to total signed flow (Pan-Poteshman). NO gamma fallback
    in the walking skeleton (that needs DealerGamma — Phase 4); with no flow at all, the
    signal is `unavailable` — never a guessed side. PURE: canonical in → Signal out, no I/O.
    Ported from `e1d6c5e:server/gates.py::derive_direction`."""
    alerts = canon.get("flow_alerts") or []
    if not alerts:
        return Flow(direction=None, direction_basis="unavailable",
                    provenance=prov.unavailable("no flow alerts"))
    src: Provenance = prov.derived(alerts[0].provenance)
    truncated = any(getattr(a, "truncated", False) for a in alerts)

    def _prem(side: str, opening_only: bool) -> float:
        return sum(float(a.total_premium or 0.0) for a in alerts
                   if a.type == side and (_opening(a) or not opening_only))

    open_call, open_put = _prem("call", True), _prem("put", True)
    if open_call or open_put:
        return Flow(direction="calls" if open_call >= open_put else "puts",
                    direction_basis="opening_flow", truncated=truncated,
                    call_prem=open_call, put_prem=open_put, provenance=src)

    tot_call, tot_put = _prem("call", False), _prem("put", False)
    if tot_call or tot_put:
        return Flow(direction="calls" if tot_call >= tot_put else "puts",
                    direction_basis="total_flow", truncated=truncated,
                    call_prem=tot_call, put_prem=tot_put, provenance=src)

    return Flow(direction=None, direction_basis="unavailable", truncated=truncated,
                provenance=prov.unavailable("zero premium on both sides"))


def _accumulation_read(cumsum: list[float]) -> tuple[str, float]:
    """Path read on a cumsum curve. efficiency = |final| / max(|cumsum|) (1.0 = clean
    one-way; «1 = spiked & reverted). Zero-crossing aware. Ported from
    `e1d6c5e:server/greek_flow.py::accumulation_read`."""
    if not cumsum:
        return "flat", 0.0
    final = cumsum[-1]
    peak = max(cumsum, key=abs)
    peak_abs = abs(peak)
    if peak_abs == 0:
        return "flat", 0.0
    eff = abs(final) / peak_abs
    crossed = (peak > 0 and final < 0) or (peak < 0 and final > 0)
    if crossed:
        state = "reversed"
    elif eff >= 0.7:
        state = "building"
    elif eff <= 0.4:
        state = "fading"
    else:
        state = "choppy"
    return state, round(eff, 2)


@register("conviction")
def derive_conviction(canon: dict, *, asof: str | None = None) -> Conviction:
    """Greek-flow directional conviction from the per-minute `dir_delta_flow` series.
    PER-MINUTE → session figure is sum, curve is cumsum (never a last tick). Sign PINNED
    (Phase 2 finding (a)): positive net = calls/bullish, negative = puts/bearish. A
    degenerate (empty/flat/all-equal) series is `unavailable` — never a silent 'flat'.
    PURE. Ported from `e1d6c5e:server/greek_flow.py::build_composite`. SAME flow family as
    Flow → in the funnel this only matters on DIVERGENCE (signal-honesty §Confluence)."""
    points = canon.get("greek_flow") or []
    dseries = [p.dir_delta_flow for p in points]
    if len(dseries) < 2 or max(dseries) == min(dseries):    # degenerate
        return Conviction(direction=None,
                          provenance=prov.unavailable("greek-flow empty or flat"))
    src: Provenance = prov.derived(points[0].provenance)
    dir_net = sum(dseries)
    total_net = sum((p.total_delta_flow or 0.0) for p in points)
    cum, run = [], 0.0
    for v in dseries:
        run += v
        cum.append(run)
    state, eff = _accumulation_read(cum)
    if dir_net == 0:
        return Conviction(direction=None, dir_delta=0.0, total_delta=round(total_net),
                          accumulation=state, efficiency=eff,
                          provenance=prov.unavailable("dir_delta net is zero (no lean)"))
    return Conviction(direction="calls" if dir_net > 0 else "puts",
                      dir_delta=round(dir_net), total_delta=round(total_net),
                      accumulation=state, efficiency=eff, provenance=src)


@register("dealer_gamma")
def derive_dealer_gamma(canon: dict, *, asof: str | None = None) -> DealerGamma:
    """Dealer-gamma structural levels from per-strike OI gamma. NET per strike is the
    SIGNED SUM call_gamma_oi + put_gamma_oi (UW pre-signs the put negative — summing, not
    subtracting, is what fixed the 2026-05-30 flip bug). flip = cumulative-net zero-crossing
    NEAREST spot; walls = max call-γ above / max |put-γ| below spot (kept separate — net
    peaks ATM). PURE. Ported from `e1d6c5e:server/gex.py::compute_levels`. A GUARD, not a
    direction (decide spec)."""
    rungs = canon.get("gamma_strikes") or []
    if not rungs:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot-exposures data"))
    spot = next((r.price for r in rungs if r.price), 0.0) or 0.0
    if spot <= 0:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot price in spot-exposures"))
    src: Provenance = prov.derived(rungs[0].provenance)
    rs = sorted(rungs, key=lambda r: r.strike)

    cum, crossings, prev = 0.0, [], None
    for r in rs:
        cum += r.call_gamma_oi + r.put_gamma_oi        # signed sum (put pre-signed negative)
        sign = 1 if cum >= 0 else -1
        if prev is not None and sign != prev:
            crossings.append(r.strike)
        prev = sign
    if crossings:
        flip, flip_status = min(crossings, key=lambda k: abs(k - spot)), "ok"
    else:
        flip, flip_status = spot, "no_flip"

    above = [r for r in rs if r.strike > spot]
    below = [r for r in rs if r.strike < spot]
    cwall = max(above, key=lambda r: r.call_gamma_oi, default=None)
    pwall = max(below, key=lambda r: abs(r.put_gamma_oi), default=None)
    cwall_k = cwall.strike if cwall else spot * 1.05
    pwall_k = pwall.strike if pwall else spot * 0.95
    agg = sum(r.call_gamma_oi + r.put_gamma_oi for r in rs) / 1e9

    return DealerGamma(
        gex_sign="POS" if agg >= 0 else "NEG",
        flip_pct=(flip - spot) / spot * 100, flip_status=flip_status,
        call_wall_pct=(cwall_k - spot) / spot * 100,
        put_wall_pct=(spot - pwall_k) / spot * 100,
        agg_b=agg, provenance=src)


@register("skew")
def derive_skew(canon: dict, *, asof: str | None = None) -> Skew:
    """25Δ risk-reversal lean from the latest historical-RR row. SIGN-CORRECTED: vendor
    risk_reversal = put_IV − call_IV (positive = put-skew); we NEGATE to call−put (>0 =
    call-skew/bullish). |rr| < _SKEW_THR → neutral. The raw LEAN only; agree/oppose-vs-
    direction is decide's job (asymmetric oppose-veto). PURE. Ported from
    `e1d6c5e:server/verdict.py::extract_vendor_rr` + `skew_state`."""
    pts = canon.get("skew_rr") or []
    if not pts:
        return Skew(lean="unavailable", provenance=prov.unavailable("no RR-skew data"))
    latest = max(pts, key=lambda p: p.date)          # date-ordered; most recent wins
    rr = -latest.risk_reversal                       # vendor (put−call) → call−put convention
    if abs(rr) < _SKEW_THR:
        lean = "neutral"
    else:
        lean = "call_skew" if rr > 0 else "put_skew"
    return Skew(rr25=rr, lean=lean, provenance=prov.derived(latest.provenance))


@register("cost")
def derive_cost(canon: dict, *, asof: str | None = None) -> Cost:
    """Non-directional cost/risk guard. Earnings or a macro event inside the hold window
    BLOCKS (don't buy premium into a known vol event) regardless of IV; otherwise the IV
    rank (interpolated-iv `percentile`, at ~30d) bands ok/caution/block. Missing IV with no
    event → `caution` (conservative, never a silent ok). PURE. `days_to_earnings` and
    `event_within_hold` are inputs (computed upstream from earnings + clock/regime).
    Ported from `e1d6c5e:server/gates.py::_cost_gate`."""
    iv = canon.get("iv_term") or []
    dte = canon.get("days_to_earnings")
    event = bool(canon.get("event_within_hold"))

    # ALWAYS compute the supporting data (IV rank) even when an event/earnings will block —
    # the tile must show what backs the read, not a null (operator: compute the variables
    # even if blocked).
    ivr = None
    src = prov.unavailable("no interpolated-iv")
    if iv:
        row = min(iv, key=lambda p: abs((p.days or 0) - _IVR_TARGET_DTE))
        ivr = round((row.percentile or 0.0) * 100, 1)
        src = prov.derived(row.provenance)

    # event/earnings veto wins over the IV bands, but ivr stays populated above.
    if dte is not None and dte < _EARNINGS_DAYS_MIN:
        guard, reason = "block", f"earnings in {dte}d — don't buy premium into it"
    elif event:
        guard, reason = "block", "macro event inside the hold window"
    elif ivr is None:
        guard, reason = "caution", "IV rank unavailable — proceeding cautiously"
    elif ivr <= _IVR_GREEN_MAX:
        guard, reason = "ok", f"IV rank {ivr:.0f} — premium not rich"
    elif ivr <= _IVR_YELLOW_MAX:
        guard, reason = "caution", f"IV rank {ivr:.0f} — elevated premium"
    else:
        guard, reason = "block", f"IV rank {ivr:.0f} — premium rich, poor cost/move"
    return Cost(guard=guard, ivr=ivr, days_to_earnings=dte, event_within_hold=event,
                reason=reason, provenance=src)


@register("positioning")
def derive_positioning(canon: dict, *, asof: str | None = None) -> Positioning:
    """OI confirmation of the flow's bet: does the FLOW-SIDE near-dated strike cluster's OI
    GROW (building) or shrink (unwinding) across settled sessions? Anchored to the flow
    side + near-dated cluster (tile2-confirmation-principle — aggregate call+put OI is the
    trap). `unconfirmed` when history is missing/insufficient — and unconfirmed NEVER blocks
    (archive-decoupled). NOT a direction (confirms the existing side). PURE. Combines with
    Flow via positioning_leg in decide.

    Inputs (orchestrator-assembled): `flow_side` ('call'/'put'), `flow_strikes` (the
    near-dated cluster), `oi_sessions` (list of settled sessions oldest→newest, each a
    list[OISnapshot])."""
    side = canon.get("flow_side")
    strikes = set(canon.get("flow_strikes") or [])
    sessions = canon.get("oi_sessions") or []
    if side not in ("call", "put") or not strikes or len(sessions) < 2:
        return Positioning(confirmation="unconfirmed", side=(side if side in ("call", "put") else ""),
                           cluster_strikes=sorted(strikes),
                           provenance=prov.unavailable("OI history unconfirmed (archive-decoupled)"))

    def cluster_oi(snaps) -> int:
        pick = (lambda s: s.call_oi) if side == "call" else (lambda s: s.put_oi)
        return sum(pick(s) for s in snaps if s.strike in strikes)

    first, last = cluster_oi(sessions[0]), cluster_oi(sessions[-1])
    if first <= 0:
        return Positioning(confirmation="unconfirmed", side=side, cluster_strikes=sorted(strikes),
                           provenance=prov.unavailable("no prior OI at the flow cluster"))
    trend = (last - first) / first * 100
    if trend >= _OI_BUILD_PCT:
        conf = "building"
    elif trend <= _OI_UNWIND_PCT:
        conf = "unwinding"
    else:
        conf = "flat"
    provs = [s.provenance for snaps in sessions for s in snaps if snaps]
    src = prov.derived(*provs) if provs else prov.derived()
    return Positioning(confirmation=conf, oi_trend_pct=round(trend, 1), side=side,
                       cluster_strikes=sorted(strikes), provenance=src)


# Macro events crossable within a weekly hold window (1–5d); a high-impact one inside it
# vetoes new premium buying (feeds Cost.event_within_hold too).
_HOLD_DAYS = 5
_HIGH_IMPACT = ("fomc", "cpi", "consumer price", "nonfarm", "payroll", "jobs report",
                "employment situation", "pce", "fed rate", "interest rate decision", "ppi")


def _parse_time(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_high_impact(ev: dict) -> bool:
    if (ev.get("type") or "").lower() == "fomc":
        return True
    return any(k in (ev.get("event") or "").lower() for k in _HIGH_IMPACT)


def _next_high_impact_event(events, now):
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


# NOT registered in the per-ticker pipeline: regime is MARKET-WIDE, not per-ticker evidence
# (operator: "the other tiles show what data supported the verdict; market regime is just a
# bunch of words"). Its one decision-relevant datum — a macro event in the hold window —
# routes through Cost (visible, data-backed). Kept as a library fn for a future market
# HEADER (computed once), not a per-ticker tile.
def derive_regime(canon: dict, *, asof: str | None = None) -> Regime:
    """Market-wide posture (Favorable/Mixed/Stand down) — NEVER a direction. NEG index
    gamma = trend (favorable for directional weeklies); POS = pin/chop (stand down); a
    high-impact macro event inside the hold window vetoes. PURE — `now` is injected via
    canon (no datetime.now()). Ported from `e1d6c5e:server/market_regime.py`.

    Input `regime` dict (orchestrator-assembled, market-wide): {gamma:{sign,status},
    vol:{iv,rv,trend}, events:[...], tide:{lean}, opex:bool, now:datetime}."""
    r = canon.get("regime") or {}
    now = r.get("now")
    if not isinstance(now, datetime):
        return Regime(posture="Mixed", headline="Market regime inputs unavailable.",
                      provenance=prov.unavailable("no regime inputs"))

    gamma = r.get("gamma") or {}
    sign, status = gamma.get("sign"), gamma.get("status")
    if status != "ok" or sign not in ("POS", "NEG"):
        headline, base = "Index gamma unavailable — market regime unclear.", "Mixed"
    elif sign == "NEG":
        headline, base = "Trend regime — moves likely to extend (favorable for directional weeklies).", "Favorable"
    else:
        headline, base = "Pinned / chop regime — moves likely to fade (hard for weeklies).", "Stand down"

    nxt = _next_high_impact_event(r.get("events"), now)
    event_within_hold = nxt is not None
    event_line = event_severity = None
    if nxt is not None:
        _, ev, days = nxt
        name = ev.get("event") or (ev.get("type") or "event").upper()
        if days <= 1:
            event_line, event_severity = f"{name} within ~1d — don't initiate weeklies into it.", "veto"
        else:
            event_line, event_severity = f"{name} in ~{int(round(days))}d — a weekly opened now will likely cross it.", "warn"

    vol = r.get("vol") or {}
    iv, rv, trend = vol.get("iv"), vol.get("rv"), vol.get("trend")
    if iv is None:
        vol_line, vol_cheap, crush_risk = "Vol environment unavailable.", False, False
    else:
        vol_cheap = (rv is None or iv <= rv) and iv <= 0.22
        crush_risk = (iv > 0.25) and (trend == "falling")
        vol_line = ("Options are cheap to own — calm vol." if vol_cheap else
                    "Vol elevated and falling — IV-crush risk on what you buy." if crush_risk else
                    "Vol middling — neither tailwind nor clear warning.")

    lean = (r.get("tide") or {}).get("lean", "neutral")
    tide_badge = {"bull": "tape flow leaning risk-on", "bear": "tape flow leaning risk-off"}.get(
        lean, "tape flow neutral")
    tide_hostile = lean == "bear"

    if event_severity == "veto":
        posture = "Stand down"
    else:
        posture = base
        if posture == "Stand down" and vol_cheap and not tide_hostile:
            posture = "Mixed"
        elif posture == "Favorable" and (crush_risk or tide_hostile or event_severity == "warn"):
            posture = "Mixed"

    return Regime(posture=posture, headline=headline, vol_line=vol_line,
                  event_line=event_line, event_severity=event_severity,
                  event_within_hold=event_within_hold, tide_badge=tide_badge,
                  opex=bool(r.get("opex")), provenance=prov.derived())
