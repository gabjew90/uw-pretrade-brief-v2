"""Stage 3 — DERIVE → signals, as PURE functions.

Each signal is a pure function: canonical records in, a typed `Signal` out, NO I/O
(no fetch, no clock-via-now, no storage). Time comes in as an argument (from the
clock) so the function stays deterministic and golden-testable. This is the layer that
catches sign inversions and gamma-direction bugs before they ship.

Signals (first-class entities in server.models): direction(Flow), conviction,
positioning(Positioning), dealer_gamma(DealerGamma), skew(Skew), cost(Cost),
Each registered here as `derive_<name>(canon, *, asof) -> Signal`. (Market regime is NOT a
signal — it is two raw inputs assembled in the orchestrator, not a posture.)

Stub: the pure functions + their golden tests are written per instructions. The
registry + purity contract are fixed now (lint/test should assert no I/O imports here).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from server.models import (Conviction, Cost, DealerGamma, Flow, Positioning, Provenance,
                            Signal, Skew)
from server.services import provenance as prov

# Skew is measured as CHANGE vs the ticker's own recent baseline (RR rides a structurally
# negative level on most names, so a fixed level gate is permanently bearish). _SKEW_DELTA_THR
# is the move-vs-normal that counts as a lean. Operator-tunable.
_SKEW_DELTA_THR = 0.01
_SKEW_MIN_PRIORS = 3      # need >=3 prior days to form a baseline, else unavailable
_SKEW_BASELINE_N = 10     # baseline = mean of the LAST N priors ("its own recent normal" —
                          # the monthly series runs months deep; a quarter-mean is not "recent")

# Cost gate thresholds (v2 GATE_THRESHOLDS; operator-deferred). IV rank bands + the
# don't-buy-premium-into-a-known-vol-event rule.
_IVR_GREEN_MAX = 60      # ivr <= 60 → ok (premium not rich)
_IVR_YELLOW_MAX = 80     # 60 < ivr <= 80 → caution
_EARNINGS_DAYS_MIN = 7   # earnings inside 7 days → block
_IVR_TARGET_DTE = 30     # IV-rank horizon (standard 30d); operator-deferred
# Spread severity is COST-RELATIVE-TO-MOVE (continuous, caution band), not a 15% cliff.
# block only the genuinely-dead; warn the borderline. All operator-deferred.
_SPREAD_HARD_PCT = 25.0    # spread this wide (% of premium) is dead regardless of move
_SPREAD_REL_FLOOR = 10.0   # a relative (burden) block still needs a meaningful absolute spread
_SPREAD_CAUTION_PCT = 10.0  # spread above this is at least worth a caution
_BURDEN_BLOCK = 2.0        # spread/priced-move ≥ this ⇒ friction ≫ the move you're playing for
_BURDEN_CAUTION = 1.0      # spread comparable to the move ⇒ caution
_COST_MIN_DTE = 2        # weekly-DTE floor: skip 0–1 DTE (event/expiry gamma) when a real
_COST_NEAR_DTE = 14      # weekly exists — the contract you'd actually hold. Operator-tunable.
# Contract-guidance checks (v2 tile4 keepers; operator-tunable):
_DELTA_LO, _DELTA_HI = 0.35, 0.55   # the sane naked-weekly band: below = lottery ticket,
                                     # above = mostly intrinsic (paying for stock exposure)
_THETA_MAX_DAY_PCT = 15.0            # |theta|/premium per day above this bleeds too fast
_N_CANDIDATES = 5                    # nearby alternatives shown with their numbers
_TERM_FRONT_DTE = 5      # term-structure overpay: front (weekly) vs back (~30d) IV
_TERM_BACK_DTE = 30
_TERM_INVERT_PTS = 0.03  # front − back above this (3 vol pts) = inverted → overpaying near

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


_ET = ZoneInfo("America/New_York")


def _alert_session(a) -> str:
    """The ET trading date this alert belongs to ('' if unparseable)."""
    try:
        t = datetime.fromisoformat((a.created_at or "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(_ET).date().isoformat()
    except (TypeError, ValueError):
        return ""


def session_alerts(alerts) -> list:
    """Filter a flow-alerts pull to its NEWEST session (ET date). The 500-cap pull is the
    most-recent TAIL and can span multiple sessions (our golden capture runs Fri 16:53 ET →
    Mon 11:02 ET), so summing the whole pull mixes the prior session's flow into today's
    direction. Pure: the session is the max ET date present in the data itself (pre-market,
    that is naturally the last completed session). Unparseable timestamps drop; if none
    parse, the original list is returned (shape problem, not a window problem)."""
    dated = [(_alert_session(a), a) for a in (alerts or [])]
    latest = max((d for d, _ in dated if d), default="")
    if not latest:
        return list(alerts or [])
    return [a for d, a in dated if d == latest]


def _side_prem(alerts, side: str, opening_only: bool) -> float:
    return sum(float(a.total_premium or 0.0) for a in (alerts or [])
               if a.type == side and (_opening(a) or not opening_only))


def flow_side(alerts) -> tuple[str | None, str]:
    """THE single side-picker (used by both derive_direction and the orchestrator's canon
    assembly, so the verdict side, the cost contract, and the OI cluster are always the same
    side). Reads ONLY the newest session in the pull (session_alerts). OPENING flow leads
    (Ge-Lin-Pearson: opening bets predict, closing don't); falls back to TOTAL signed flow.
    Returns (side|None, basis) with side in {'call','put'}."""
    alerts = session_alerts(alerts)
    oc, op = _side_prem(alerts, "call", True), _side_prem(alerts, "put", True)
    if oc or op:
        return ("call" if oc >= op else "put"), "opening_flow"
    tc, tp = _side_prem(alerts, "call", False), _side_prem(alerts, "put", False)
    if tc or tp:
        return ("call" if tc >= tp else "put"), "total_flow"
    return None, "unavailable"


# The side's own quality bar (reviewer 2026-06-11): the Pan-Poteshman edge lives in the
# EXTREME of signed flow, not its sign. A picked side must dominate by ratio AND clear an
# absolute floor, else lean_quality="weak" → decide caps at Mixed ("weak evidence,
# unopposed" must never read Favorable). The flat floor is v1 — per-name scaling
# (percentile vs the ticker's own history) joins the threshold-tuning pass once the
# archive has ~20 sessions.
_LEAN_DOMINANCE = 2.0       # winner premium must be >= 2x the loser's
_LEAN_FLOOR_USD = 500_000.0  # and >= this in absolute opening dollars


def _lean_quality(win: float, lose: float) -> tuple[float | None, str, str]:
    """(lean_ratio, lean_quality, lean_note) for a picked side's premium split."""
    ratio = round(win / lose, 1) if lose > 0 else None
    if win < _LEAN_FLOOR_USD:
        return ratio, "weak", f"thin ${win/1e3:.0f}K"
    if ratio is not None and ratio < _LEAN_DOMINANCE:
        return ratio, "weak", f"{ratio:g}:1"
    return ratio, "qualified", f"{ratio:g}:1" if ratio is not None else "one-sided"


@register("flow")
def derive_direction(canon: dict, *, asof: str | None = None) -> Flow:
    """Pick the call/put side via the shared `flow_side` (OPENING leads, TOTAL fallback),
    reading ONLY the newest session in the pull (the 500-cap tail can span sessions — the
    prior day's flow must not contaminate today's read). NO gamma fallback; with no flow at
    all the signal is `unavailable` — never a guessed side. The picked side then meets its
    own bar (`_lean_quality`). PURE. Ported from `e1d6c5e:server/gates.py::derive_direction`."""
    raw = canon.get("flow_alerts") or []
    if not raw:
        return Flow(direction=None, direction_basis="unavailable",
                    provenance=prov.unavailable(canon.get("flow_error") or "no flow alerts"))
    truncated = any(getattr(a, "truncated", False) for a in raw)
    alerts = session_alerts(raw)                    # newest session only
    side, basis = flow_side(alerts)
    if side is None:
        return Flow(direction=None, direction_basis="unavailable", truncated=truncated,
                    provenance=prov.unavailable("zero premium on both sides"))
    opening_only = basis == "opening_flow"
    call_prem = _side_prem(alerts, "call", opening_only)
    put_prem = _side_prem(alerts, "put", opening_only)
    # chart rows: top strikes by premium PER SIDE (per-side selection — a dominant side
    # must not bury the other; the v2 SPY "no flow" lesson)
    by_ks: dict[tuple, float] = {}
    for a in alerts:
        if a.strike is not None and (_opening(a) or not opening_only):
            by_ks[(a.strike, a.type)] = by_ks.get((a.strike, a.type), 0.0) + float(a.total_premium or 0.0)
    def _top(sd):
        ks = sorted((k for k in by_ks if k[1] == sd), key=lambda k: by_ks[k], reverse=True)[:5]
        return [{"strike": k[0], "side": sd, "premium": round(by_ks[k])} for k in ks]
    win, lose = (call_prem, put_prem) if side == "call" else (put_prem, call_prem)
    lean_ratio, lean_q, lean_note = _lean_quality(win, lose)
    return Flow(direction="calls" if side == "call" else "puts", direction_basis=basis,
                truncated=truncated, call_prem=call_prem, put_prem=put_prem,
                lean_ratio=lean_ratio, lean_quality=lean_q, lean_note=lean_note,
                top_strikes=_top("call") + _top("put"),
                provenance=prov.derived(alerts[0].provenance))


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


def _session_share_volume(bars) -> float | None:
    """Today's regular-hours stock share volume from the 15m OHLC bars (newest ET session,
    market_time 'r' — the same filter the price axis uses). The yardstick for the delta
    net: dir_delta is share-equivalent pressure, so |net| / shares-traded says whether the
    lean is big FOR THIS TICKER TODAY, which a raw cross-ticker number can't."""
    reg = [b for b in (bars or []) if b.market_time == "r"]
    if not reg:
        return None
    def et_date(b):
        t = datetime.fromisoformat(b.start_time.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(_ET).date()
    newest = max(et_date(b) for b in reg)
    vol = sum((b.volume or 0) for b in reg if et_date(b) == newest)
    return float(vol) or None


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
    # chart: the cumulative curve, evenly downsampled to <=48 points (keeps the last)
    n = len(cum)
    idxs = list(range(n)) if n <= 48 else \
        sorted({min(n - 1, int(i * n / 48)) for i in range(48)} | {n - 1})
    cum_series = [{"t": points[i].timestamp[11:16], "v": round(cum[i])} for i in idxs]
    if dir_net == 0:
        return Conviction(direction=None, dir_delta=0.0, total_delta=round(total_net),
                          accumulation=state, efficiency=eff, cum_series=cum_series,
                          provenance=prov.unavailable("dir_delta net is zero (no lean)"))
    share_vol = _session_share_volume(canon.get("ohlc"))
    return Conviction(direction="calls" if dir_net > 0 else "puts",
                      dir_delta=round(dir_net), total_delta=round(total_net),
                      accumulation=state, efficiency=eff, cum_series=cum_series,
                      share_volume=share_vol,
                      vol_ratio=round(abs(dir_net) / share_vol, 4) if share_vol else None,
                      provenance=src)


# The strike band must extend at least this far BOTH sides of spot, or the gamma read is
# structurally one-sided: agg/gex_sign skew toward the covered side, the "wall" is just the
# band edge, and the flip can't be seen (the v2 "band-edge garbage" bug, re-found in review).
_GAMMA_MIN_BAND_PCT = 2.0


@register("dealer_gamma")
def derive_dealer_gamma(canon: dict, *, asof: str | None = None) -> DealerGamma:
    """Dealer-gamma structural levels from per-strike OI gamma. NET per strike is the
    SIGNED SUM call_gamma_oi + put_gamma_oi (UW pre-signs the put negative — summing, not
    subtracting, is what fixed the 2026-05-30 flip bug). flip = cumulative-net zero-crossing
    NEAREST spot; walls = max call-γ above / max |put-γ| below spot (kept separate — net
    peaks ATM). GUARDS the band: if the strikes don't cover ±_GAMMA_MIN_BAND_PCT of spot the
    whole read is unavailable (a one-sided band inverts the sign and fakes walls); a missing
    side never fabricates a wall (None, not spot±5%). PURE. Ported from
    `e1d6c5e:server/gex.py::compute_levels`. A GUARD, not a direction (decide spec)."""
    rungs = canon.get("gamma_strikes") or []
    if not rungs:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot-exposures data"))
    spot = canon.get("spot") or next((r.price for r in rungs if r.price), 0.0) or 0.0
    if spot <= 0:
        return DealerGamma(flip_status="unavailable",
                           provenance=prov.unavailable("no spot price in spot-exposures"))
    rs = sorted(rungs, key=lambda r: r.strike)

    above_pct = (rs[-1].strike - spot) / spot * 100
    below_pct = (spot - rs[0].strike) / spot * 100
    if above_pct < _GAMMA_MIN_BAND_PCT or below_pct < _GAMMA_MIN_BAND_PCT:
        return DealerGamma(flip_status="unavailable", provenance=prov.unavailable(
            f"strike band one-sided ({below_pct:+.1f}%/{above_pct:+.1f}% of spot) — "
            "gamma sign/flip/walls not trustworthy"))
    src: Provenance = prov.derived(rungs[0].provenance)

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
    agg = sum(r.call_gamma_oi + r.put_gamma_oi for r in rs) / 1e9

    # chart: the per-strike net-gamma ladder within ±10% of spot, <=48 rungs (the v2
    # Tile-3 ladder's data, $bn per strike)
    near = [r for r in rs if abs(r.strike - spot) / spot <= 0.10]
    step = max(1, len(near) // 48)
    ladder = [{"strike": r.strike,
               "net_b": round((r.call_gamma_oi + r.put_gamma_oi) / 1e9, 3)}
              for r in near[::step]]

    return DealerGamma(
        gex_sign="POS" if agg >= 0 else "NEG",
        flip_pct=(flip - spot) / spot * 100, flip_status=flip_status,
        call_wall_pct=(cwall.strike - spot) / spot * 100 if cwall else None,
        put_wall_pct=(spot - pwall.strike) / spot * 100 if pwall else None,
        agg_b=agg, ladder=ladder, provenance=src)


@register("skew")
def derive_skew(canon: dict, *, asof: str | None = None) -> Skew:
    """25Δ risk-reversal lean, measured as today's RR vs its own trailing baseline (NOT a
    fixed level — RR rides a structurally negative baseline on most names, which would make
    the leg permanently bearish). SIGN-CORRECTED: vendor risk_reversal = put_IV − call_IV;
    we NEGATE to call−put (>0 = calls bid). delta = today − mean(prior days). delta > +THR =
    call_skew (calls richer than usual), < −THR = put_skew, else neutral. Needs ≥3 prior days
    else unavailable. The raw LEAN only; agree/oppose-vs-direction is decide's job. PURE.
    Ported sign-correction from `e1d6c5e:server/verdict.py::extract_vendor_rr`."""
    pts = canon.get("skew_rr") or []
    if len(pts) < _SKEW_MIN_PRIORS + 1:
        return Skew(lean="unavailable", provenance=prov.unavailable("insufficient RR history"))
    ordered = sorted(pts, key=lambda p: p.date)      # oldest → newest
    latest, priors = ordered[-1], ordered[:-1][-_SKEW_BASELINE_N:]
    rr_today = -latest.risk_reversal                 # vendor (put−call) → call−put
    baseline = -sum(p.risk_reversal for p in priors) / len(priors)   # call−put
    delta = rr_today - baseline
    if abs(delta) < _SKEW_DELTA_THR:
        lean = "neutral"
    else:
        lean = "call_skew" if delta > 0 else "put_skew"
    series = [{"date": p.date, "rr": round(-p.risk_reversal, 4)} for p in ordered]  # call−put
    return Skew(rr25=round(rr_today, 4), rr_baseline=round(baseline, 4), rr_delta=round(delta, 4),
                lean=lean, series=series, provenance=prov.derived(latest.provenance))


@register("cost")
def derive_cost(canon: dict, *, asof: str | None = None) -> Cost:
    """Non-directional cost/risk guard. Earnings inside the hold window BLOCKS (gap risk +
    IV crush on the exact contract); a macro event inside it only FLAGS (caution with name
    and days — prints are weekly-cadence, so blocking would PASS most weeks); otherwise the
    IV rank (interpolated-iv `percentile`, at ~30d) bands ok/caution/block. Missing IV with
    no event → `caution` (conservative, never a silent ok). PURE. `days_to_earnings`,
    `event_within_hold` and `macro_event` are inputs (computed upstream from earnings +
    clock/regime). Ported from `e1d6c5e:server/gates.py::_cost_gate`."""
    iv = canon.get("iv_term") or []
    dte_e = canon.get("days_to_earnings")
    event = bool(canon.get("event_within_hold"))
    contracts = canon.get("option_contracts") or []
    side = canon.get("flow_side")
    spot = canon.get("spot") or 0.0
    asof_d = date.fromisoformat(asof) if asof else None

    # ALWAYS compute the supporting data (IV rank) even when something blocks — the tile
    # shows what backs the read, not a null (operator: compute the variables even if blocked).
    ivr = None
    ivsrc = prov.unavailable("no interpolated-iv")
    if iv:
        row = min(iv, key=lambda p: abs((p.days or 0) - _IVR_TARGET_DTE))
        # a vendor-null percentile stays None ("IV rank n/a"), never 0 ("cheapest ever")
        ivr = round(row.percentile * 100, 1) if row.percentile is not None else None
        ivsrc = prov.derived(row.provenance)

    # Tradeability: on the realistic contract you'd actually buy (near-the-money, near-dated,
    # flow side), the round-trip SPREAD and whether the priced EXPECTED MOVE can reach
    # BREAKEVEN. This is the load-bearing gate — the edge is smaller than the round-trip cost.
    spread_pct = be_pct = em_pct = None
    contract_d = None
    candidates: list[dict] = []
    greeks = canon.get("greeks") or []
    pick = _pick_contract(contracts, side, spot, asof_d)
    if pick:
        contract_d = _contract_metrics(pick, side, spot, asof_d, iv, greeks)
        spread_pct, be_pct, em_pct = (contract_d.get("spread_pct"),
                                      contract_d.get("breakeven_move_pct"),
                                      contract_d.get("expected_move_pct"))
        candidates = _candidate_metrics(contracts, pick, side, spot, asof_d, iv, greeks)
    tradeable = spread_pct is not None and be_pct is not None and em_pct is not None
    front_iv, back_iv, inverted = _term_overpay(canon.get("term_structure"))

    def _ge(x, t):
        return x is not None and x >= t

    # Spread severity = cost RELATIVE TO the move you're playing for (continuous), so a wide
    # spread against a big move is fine but a modest spread against a tiny move is dead.
    burden = (spread_pct / em_pct) if (spread_pct is not None and em_pct) else None
    spread_block = _ge(spread_pct, _SPREAD_HARD_PCT) or (
        burden is not None and burden >= _BURDEN_BLOCK and _ge(spread_pct, _SPREAD_REL_FLOOR))
    spread_caution = _ge(spread_pct, _SPREAD_CAUTION_PCT) or (
        burden is not None and burden >= _BURDEN_CAUTION)

    # ── EV-KILLERS → Stand down: expected-value-negative no matter how right the direction.
    # Reasons are number-led and terse (let the numbers speak), never prose.
    if dte_e is not None and dte_e < _EARNINGS_DAYS_MIN:
        guard, reason = "block", f"earnings {dte_e}d out"
    elif spread_block:
        mv = f" vs {em_pct:.1f}% move" if em_pct is not None else ""
        guard, reason = "block", f"spread {spread_pct:.0f}%{mv}"
    elif not tradeable:
        # can't confirm the spread/move gate → never a confident green
        guard, reason = "caution", "no chain to price the trade"
    else:
        # ── DEGRADERS → caution (cap to Mixed + flag): a correct, large, imminent move can
        # still win through these. Collect every applicable warning, number-led.
        flags: list[str] = []
        # macro prints are weekly-cadence (CPI/PPI/NFP/FOMC) — a hard block would PASS most
        # weeks by construction. Operator policy 2026-06-11: inform, never halt; you can
        # plan the exit around a named date. Earnings (above) stays the hard event block.
        if event:
            flags.append(f"macro {canon.get('macro_event') or 'event in 5d'}")
        if em_pct < be_pct:
            flags.append(f"move {em_pct:.1f}% < breakeven {be_pct:.1f}%")
        if _ge(ivr, _IVR_YELLOW_MAX):
            flags.append(f"IV rank {ivr:.0f} rich")
        elif _ge(ivr, _IVR_GREEN_MAX):
            flags.append(f"IV rank {ivr:.0f}")
        if spread_caution:
            flags.append(f"spread {spread_pct:.0f}%")
        if inverted:
            flags.append(f"term inverted {front_iv:.0%}/{back_iv:.0%}")
        if ivr is None:
            flags.append("IV rank n/a")
        # contract-quality degraders (v2 tile4 keepers): outside the delta band or
        # bleeding too fast a right direction still tends to lose
        d = (contract_d or {}).get("delta")
        if d is not None and d < _DELTA_LO:
            flags.append(f"delta {d:.2f} lottery-ish")
        elif d is not None and d > _DELTA_HI:
            flags.append(f"delta {d:.2f} mostly intrinsic")
        th = (contract_d or {}).get("theta_day_pct")
        if th is not None and th > _THETA_MAX_DAY_PCT:
            flags.append(f"theta {th:.0f}%/day")
        # a FAILED calendar fetch is unknown, not an all-clear (review SEVERE #2)
        if canon.get("event_calendar_ok") is False:
            flags.append("macro calendar n/a")
        if canon.get("earnings_calendar_ok") is False:
            flags.append("earnings dates n/a")
        if flags:
            guard, reason = "caution", " · ".join(flags)
        else:
            guard, reason = "ok", f"spread {spread_pct:.0f}%, move {em_pct:.1f}% > be {be_pct:.1f}%"

    src = prov.derived(ivsrc, pick.provenance) if pick else ivsrc
    term_curve = [{"dte": t.dte, "iv": round(t.volatility, 4)}
                  for t in (canon.get("term_structure") or [])
                  if t.volatility is not None and t.dte <= 90]
    return Cost(guard=guard, ivr=ivr, days_to_earnings=dte_e, event_within_hold=event,
                spread_pct=spread_pct, breakeven_move_pct=be_pct, expected_move_pct=em_pct,
                contract=contract_d, candidates=candidates, front_iv=front_iv,
                back_iv=back_iv, term_inverted=inverted, term_curve=term_curve,
                reason=reason, provenance=src)


def _pick_contract(contracts, side, spot, asof_d):
    """The realistic WEEKLY you'd actually buy: flow side, DTE in [2, 14] (skip 0–1 DTE
    expiry/event noise when a real weekly exists), soonest expiry, strike nearest spot.
    None if un-evaluable."""
    if side not in ("call", "put") or not spot or spot <= 0 or asof_d is None:
        return None
    cands = []
    for c in contracts:
        if c.type != side:
            continue
        try:
            dte = (date.fromisoformat(c.expiry) - asof_d).days
        except (TypeError, ValueError):
            continue
        if dte < 0:
            continue
        cands.append((dte, abs(c.strike - spot), c))
    if not cands:
        return None
    weekly = [t for t in cands if _COST_MIN_DTE <= t[0] <= _COST_NEAR_DTE]
    near = [t for t in cands if t[0] <= _COST_NEAR_DTE]
    pool = weekly or near or cands            # prefer a real weekly; fall back gracefully
    pool.sort(key=lambda t: (t[0], t[1]))     # soonest expiry, then nearest the money
    return pool[0][2]


def _term_overpay(term_points):
    """(front_iv, back_iv, inverted): front = IV nearest _TERM_FRONT_DTE (the weekly), back =
    nearest _TERM_BACK_DTE (~30d). Inverted when front exceeds back by _TERM_INVERT_PTS —
    near-dated vol is pumped, so you're overpaying for the weekly you'd buy. Ported from
    `e1d6c5e:server/insights.py` term-structure read."""
    pts = [t for t in (term_points or []) if t.volatility is not None]
    if len(pts) < 2:
        return None, None, False
    front = min(pts, key=lambda t: abs(t.dte - _TERM_FRONT_DTE)).volatility
    back = min(pts, key=lambda t: abs(t.dte - _TERM_BACK_DTE)).volatility
    return round(front, 4), round(back, 4), (front - back) > _TERM_INVERT_PTS


def _breakeven_move_pct(c, spot: float, premium: float) -> float:
    """% move in the underlying needed to break even at expiry (premium baked in).
    Ported from `e1d6c5e:server/tile4.py::_breakeven_move_pct`."""
    if c.type == "call":
        return (c.strike + premium - spot) / spot * 100
    return (spot - (c.strike - premium)) / spot * 100


def _expected_move_pct(iv_term, dte: int):
    """The move % the options are pricing for ~`dte`, from interpolated-iv's
    implied_move_perc (already a fraction). None if absent."""
    if not iv_term:
        return None
    row = min(iv_term, key=lambda p: abs((p.days or 0) - dte))
    return None if row.implied_move_perc is None else row.implied_move_perc * 100


def _greeks_leg(greeks, strike: float, side: str) -> tuple[float | None, float | None]:
    """(delta, theta) for the side's leg at `strike` — greeks rows carry SEPARATE
    call_/put_ columns (v2 live-confirmed). None when the strike isn't in the sheet."""
    for g in greeks or []:
        if g.strike == strike:
            if side == "call":
                return g.call_delta, g.call_theta
            return g.put_delta, g.put_theta
    return None, None


def _contract_metrics(c, side: str, spot: float, asof_d, iv_term, greeks) -> dict:
    """Everything an amateur needs to judge ONE contract: quote, spread, breakeven vs the
    priced move, delta (P(profit)-ish), and theta drag (%premium bled per day)."""
    mid = (c.bid + c.ask) / 2
    prem = c.ask or mid
    dte_c = (date.fromisoformat(c.expiry) - asof_d).days
    delta, theta = _greeks_leg(greeks, c.strike, side)
    em = _expected_move_pct(iv_term, dte_c)
    return {
        "type": c.type, "strike": c.strike, "expiry": c.expiry, "dte": dte_c,
        "bid": c.bid, "ask": c.ask,
        "spread_pct": round((c.ask - c.bid) / mid * 100, 1) if mid > 0 else None,
        "breakeven_move_pct": round(_breakeven_move_pct(c, spot, prem), 2) if prem > 0 else None,
        "expected_move_pct": round(em, 2) if em is not None else None,
        "delta": round(abs(delta), 2) if delta is not None else None,
        "theta_day_pct": round(abs(theta) / prem * 100, 1) if (theta is not None and prem > 0) else None,
        "volume": c.volume, "open_interest": c.open_interest,
    }


def _candidate_metrics(contracts, pick, side: str, spot: float, asof_d, iv_term, greeks) -> list[dict]:
    """The pick's nearest-the-money same-expiry neighbours with the same metrics — so the
    strike CHOICE is informed, not just the gate. Ranked by closeness to the money."""
    sibs = [c for c in contracts or []
            if c.type == side and c.expiry == pick.expiry and c.strike != pick.strike]
    sibs.sort(key=lambda c: abs(c.strike - spot))
    return [_contract_metrics(c, side, spot, asof_d, iv_term, greeks)
            for c in sibs[:_N_CANDIDATES]]


_POS_WINDOW = 5   # settled sessions the build/unwind trend is read over (operator-tunable)


@register("positioning")
def derive_positioning(canon: dict, *, asof: str | None = None) -> Positioning:
    """OI confirmation of the flow's bet: does OI on the EXACT cluster contracts the flow
    hit GROW (building) or shrink (unwinding)? Sourced from per-contract daily history
    (option-contract/{id}/historic — the contract's whole life, no ~7-day ceiling), summed
    across the cluster per date, trended over the last _POS_WINDOW sessions. Contract-level
    is the most faithful read of tile2-confirmation-principle (pooled per-strike OI mixes
    expiries). `unconfirmed` when history is missing/insufficient — and unconfirmed NEVER
    blocks (archive-decoupled). NOT a direction. PURE.

    Inputs (orchestrator-assembled): `flow_side`, `flow_strikes` (the cluster),
    `contract_oi` = list of per-contract `list[ContractOIBar]` (oldest→newest each)."""
    side = canon.get("flow_side")
    strikes = set(canon.get("flow_strikes") or [])
    per_contract = [bars for bars in (canon.get("contract_oi") or []) if bars]
    if side not in ("call", "put") or not per_contract:
        return Positioning(confirmation="unconfirmed", side=(side if side in ("call", "put") else ""),
                           cluster_strikes=sorted(strikes),
                           provenance=prov.unavailable("OI history unconfirmed (archive-decoupled)"))

    # COMMON-COVERAGE window only: a weekly listed mid-window has no early bars, so a raw
    # sum fakes "building" from contract BIRTHS (live-caught: 6.9K -> 117K was listings,
    # not accumulation). Sum only dates on/after the latest first-date across contracts.
    start = max(min(b.date for b in bars) for bars in per_contract)
    by_date: dict[str, int] = {}
    for bars in per_contract:
        for b in bars:
            if b.date >= start:
                by_date[b.date] = by_date.get(b.date, 0) + b.open_interest
    window = sorted(by_date)[-_POS_WINDOW:]
    if len(window) < 2:
        return Positioning(confirmation="unconfirmed", side=side, cluster_strikes=sorted(strikes),
                           provenance=prov.unavailable("OI history unconfirmed (archive-decoupled)"))
    first, last = by_date[window[0]], by_date[window[-1]]
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
    src = prov.derived(*[bars[0].provenance for bars in per_contract])
    # chart: summed cluster OI per day, last 30 sessions (the bar-chart series)
    oi_series = [{"date": d, "oi": by_date[d]} for d in sorted(by_date)[-30:]]
    return Positioning(confirmation=conf, oi_trend_pct=round(trend, 1), side=side,
                       cluster_strikes=sorted(strikes), oi_series=oi_series, provenance=src)


# Macro events crossable within a weekly hold window (1–5d); a high-impact one inside it
# FLAGS new premium buying as caution (feeds Cost.event_within_hold + macro_event) — never
# a block, by operator policy: the prints are weekly-cadence, a veto would halt most weeks.
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


def next_macro_event(events, now):
    """The next high-impact macro event inside the hold window as (name, days), or None. The
    ONE surviving piece of market-regime logic (review Fix 5): it feeds Cost.event_within_hold
    and the muted Market-now line. No posture, no Signal."""
    nxt = _next_high_impact_event(events, now)
    if nxt is None:
        return None
    _, ev, days = nxt
    return (ev.get("event") or (ev.get("type") or "event").upper()), days
