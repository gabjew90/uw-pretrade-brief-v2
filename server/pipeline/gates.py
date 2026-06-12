"""Gate evaluators for the strict-conjunction verdict (directive 2026-06-12).

Each gate compares signal fields to a cited constant and returns a GateResult:
GREEN (met), RED (measurably not met), DARK (input unavailable — never fabricated).
PERFECT iff every gate in the active branch is GREEN; DARK counts as not-green
(conservatism preserved) but renders gray.

Gates COMPARE, they never compute — every number is derived upstream (pure derive).
Thresholds live HERE and only here, one citation beside each. v1 NOTE: the directive's
cross-sectional conditions (90th-pct premium, GEX quartile, 70th-pct short ratio) ship
as absolute thresholds — panel percentiles need a scan loop the platform deliberately
doesn't have ([[basic-platform]]); archive self-percentiles join the tuning pass.
"""
from __future__ import annotations

from server.models import GateResult, Provenance

# ── thresholds (citation beside each number) ──────────────────────────────────
ASK_SHARE_MIN = 0.70      # Hu 2014: opening ask-side imbalance predicts ~1-day returns
PREM_FLOOR_USD = 1_000_000  # v1 absolute stand-in for "90th pct cross-sectional" size
CONC_SHARE_MIN = 0.60     # directive G2: bets bunched, not scattered
CONC_DTE_LO, CONC_DTE_HI = 5, 30
FLIP_DIST_MIN_PCT = 0.5   # Barbon-Buraschi 2020: firmly in the negative-gamma zone
IVR_MAX = 30.0            # Hu-Jacobs 2020 / Goyal-Saretto: long premium wants LOW IV
HV_IV_MIN = 1.0           # ...and HV >= IV
TERM_SLOPE_MIN = 0.0      # Vasquez 2017: flat/upward front slope
RR_SHIFT_MIN = 0.01       # An-Ang-Bali-Cakici 2014: skew CHANGE, not level
SKEW_BASELINE_MIN = 3     # below this the baseline is fiction -> DARK
SPREAD_MAX_PCT = 5.0      # v1 flat (directive's 3/5/8 liquidity tiers later)
BE_EM_MAX = 0.70          # breakeven move <= 70% of the expected move
THETA_MAX_PCT = 10.0      # per day
DELTA_LO, DELTA_LO_FUELED, DELTA_HI = 0.40, 0.35, 0.55   # 0.35 floor only when G4 GREEN
EARN_WINDOW_D = 3         # branch selector: earnings inside -> catalyst branch
MACRO_BLOCK_D = 1         # operator policy 2026-06-11: only a print you'd HOLD THROUGH
                          # reds the window; later prints are displayed, never gating
SHORT_RATIO_MIN = 0.50    # v1 absolute stand-in for "70th pct cross-sectional"
FTD_PCTILE_MAX = 90.0     # latest FTDs vs the ticker's own trailing year
IV_SPIKE_MAX_PCT = 20.0   # front IV +20% in 2 sessions = squeeze-adjacent
C1_RATIO_MAX = 0.9        # Milian 2023: implied move <= 0.9x historical earnings move
C3_DTE_LO, C3_DTE_HI = 5, 15
CATALYST_MIN_QUARTERS = 4  # below this C1 is DARK (ohlc/1d gives ~1y at tier)

# plain-English light labels (directive §3)
LABELS = {
    "flow_dominance": "Big money just opened bets this way",
    "flow_concentration": "The bets are bunched near here, expiring soon",
    "oi_basis": "The position is being held, not closed",
    "dealer_fuel": "Dealers will add fuel to the move",
    "headroom": "Room to run before the next wall",
    "cheap_vol": "The options are cheap for how much this moves",
    "term_slope": "No event premium waiting to deflate",
    "skew_shift": "Pricing just tilted this way",
    "cost": "You're not overpaying to get in",
    "clean_window": "Nothing on the calendar mid-trade",
    "tape_agrees": "The live tape agrees",
    "short_pressure": "Short sellers are pressing",
    "no_squeeze": "No squeeze trap",
    "cheap_implied_move": "The market is underpricing this report",
    "expiry_capture": "The expiry captures the report",
    "not_crowded": "The crowd isn't already in",
}


def _g(name, state, value="", threshold="", prov=None) -> GateResult:
    return GateResult(name=name, label=LABELS[name], state=state, value=value,
                      threshold=threshold, provenance=prov or Provenance())


def _side(direction: str) -> str:
    return "call" if direction == "calls" else "put"


# ── drift gates ───────────────────────────────────────────────────────────────
def g_flow_dominance(direction, s) -> GateResult:
    flow = s.get("flow")
    if flow is None or getattr(flow, "direction", None) is None:
        return _g("flow_dominance", "DARK", "no flow read")
    st = (flow.side_stats or {}).get(_side(direction)) or {}
    prov = flow.provenance
    if flow.direction_basis != "opening_flow":
        return _g("flow_dominance", "RED", f"basis {flow.direction_basis}",
                  "opening flow required", prov)
    ask_share, prem = st.get("ask_share"), st.get("opening_prem", 0.0)
    total = (flow.side_stats.get("call", {}).get("opening_prem", 0.0)
             + flow.side_stats.get("put", {}).get("opening_prem", 0.0))
    if ask_share is None:
        return _g("flow_dominance", "DARK", "no ask/bid split", "", prov)
    ok = (ask_share >= ASK_SHARE_MIN and total >= PREM_FLOOR_USD
          and getattr(flow, "lean_quality", "n/a") != "weak"
          and getattr(flow, "direction", None) == direction)
    val = f"ask-side {ask_share:.0%} of ${total/1e6:.1f}M"
    return _g("flow_dominance", "GREEN" if ok else "RED", val,
              f">= {ASK_SHARE_MIN:.0%} and >= ${PREM_FLOOR_USD/1e6:g}M, lean qualified", prov)


def g_flow_concentration(direction, s) -> GateResult:
    flow = s.get("flow")
    st = (getattr(flow, "side_stats", None) or {}).get(_side(direction)) or {}
    if (not st or st.get("top2_share") is None or st.get("top2_dte_ok") is None
            or st.get("strike_band_ok") is None):
        return _g("flow_concentration", "DARK", "expiry/strike read incomplete",
                  prov=getattr(flow, "provenance", None))
    ok = (st["top2_share"] >= CONC_SHARE_MIN and st["top2_dte_ok"]
          and st["strike_band_ok"])
    return _g("flow_concentration", "GREEN" if ok else "RED",
              f"top-2 expiries {st['top2_share']:.0%}",
              f">= {CONC_SHARE_MIN:.0%}, {CONC_DTE_LO}-{CONC_DTE_HI} DTE, near the money",
              flow.provenance)


def g_oi_basis(direction, s) -> GateResult:
    p, flow = s.get("positioning"), s.get("flow")
    flow_dir = getattr(flow, "direction", None)
    if flow_dir != direction:
        return _g("oi_basis", "DARK", "no OI cluster for this side")
    conf = getattr(p, "confirmation", "unconfirmed") if p else "unconfirmed"
    # building/flat/unconfirmed pass (archive-decoupled, unchanged); unwinding is RED
    state = "RED" if conf == "unwinding" else "GREEN"
    return _g("oi_basis", state, conf, "not unwinding",
              getattr(p, "provenance", None))


def g_dealer_fuel(direction, s) -> GateResult:
    dg = s.get("dealer_gamma")
    if dg is None or getattr(dg, "flip_status", "unavailable") == "unavailable":
        return _g("dealer_fuel", "DARK", "no dealer gamma")
    if dg.gex_sign != "NEG":
        return _g("dealer_fuel", "RED", f"gamma {dg.gex_sign}", "negative", dg.provenance)
    fd = getattr(dg, "flip_pct", None)
    if fd is None:
        return _g("dealer_fuel", "DARK", "no flip in range", prov=dg.provenance)
    ok = abs(fd) >= FLIP_DIST_MIN_PCT
    return _g("dealer_fuel", "GREEN" if ok else "RED",
              f"NEG, flip {fd:+.1f}% away", f">= {FLIP_DIST_MIN_PCT}% from flip",
              dg.provenance)


def g_headroom(direction, s) -> GateResult:
    dg, cost = s.get("dealer_gamma"), s.get("cost")
    em = getattr(cost, "expected_move_pct", None) if cost else None
    wall = (getattr(dg, "call_wall_pct", None) if direction == "calls"
            else getattr(dg, "put_wall_pct", None)) if dg else None
    if wall is None or em is None or not em:
        return _g("headroom", "DARK", "wall or expected move unknown",
                  prov=getattr(dg, "provenance", None))
    ok = abs(wall) >= em
    return _g("headroom", "GREEN" if ok else "RED",
              f"wall {abs(wall):.1f}% vs move {em:.1f}%", ">= 1 expected move",
              dg.provenance)


def g_cheap_vol(direction, s) -> GateResult:
    v = s.get("vol")
    if v is None or v.ivr is None or v.hv_iv_ratio is None:
        return _g("cheap_vol", "DARK", "IV rank or HV unavailable",
                  prov=getattr(v, "provenance", None))
    ok = v.ivr < IVR_MAX and v.hv_iv_ratio >= HV_IV_MIN
    return _g("cheap_vol", "GREEN" if ok else "RED",
              f"IV rank {v.ivr:.0f}, HV/IV {v.hv_iv_ratio:.2f}",
              f"rank < {IVR_MAX:g} and HV/IV >= {HV_IV_MIN:g}", v.provenance)


def g_term_slope(direction, s) -> GateResult:
    v = s.get("vol")
    if v is None or v.term_slope is None:
        return _g("term_slope", "DARK", "no term structure",
                  prov=getattr(v, "provenance", None))
    ok = v.term_slope >= TERM_SLOPE_MIN
    return _g("term_slope", "GREEN" if ok else "RED",
              f"slope {v.term_slope:+.3f}", "flat or upward", v.provenance)


def g_skew_shift(direction, s) -> GateResult:
    sk = s.get("skew")
    if (sk is None or getattr(sk, "lean", "unavailable") == "unavailable"
            or sk.rr_delta is None):
        return _g("skew_shift", "DARK", "no baseline yet (needs 3+ sessions)",
                  prov=getattr(sk, "provenance", None))
    need = RR_SHIFT_MIN if direction == "calls" else -RR_SHIFT_MIN
    ok = sk.rr_delta >= need if direction == "calls" else sk.rr_delta <= need
    return _g("skew_shift", "GREEN" if ok else "RED",
              f"RR change {sk.rr_delta:+.3f}", f"{'>=' if direction == 'calls' else '<='} {need:+.2f}",
              sk.provenance)


def g_cost(direction, s, *, fueled: bool) -> GateResult:
    cost, flow = s.get("cost"), s.get("flow")
    if getattr(flow, "direction", None) != direction:
        return _g("cost", "DARK", "no contract priced for this side")
    if cost is None or cost.spread_pct is None:
        return _g("cost", "DARK", "no chain to price",
                  prov=getattr(cost, "provenance", None))
    ct = cost.contract or {}
    d, th = ct.get("delta"), ct.get("theta_day_pct")
    be, em = cost.breakeven_move_pct, cost.expected_move_pct
    checks, vals = [], []
    checks.append(cost.spread_pct <= SPREAD_MAX_PCT)
    vals.append(f"spread {cost.spread_pct:.0f}%")
    if be is not None and em:
        checks.append(be <= BE_EM_MAX * em)
        vals.append(f"be {be:.1f}% vs move {em:.1f}%")
    else:
        return _g("cost", "DARK", "breakeven/move unknown", prov=cost.provenance)
    if th is not None:
        checks.append(th <= THETA_MAX_PCT)
        vals.append(f"theta {th:.0f}%/d")
    if d is not None:
        lo = DELTA_LO_FUELED if fueled else DELTA_LO
        checks.append(lo <= d <= DELTA_HI)
        vals.append(f"delta {d:.2f}")
    if th is None or d is None:
        return _g("cost", "DARK", "greeks missing for the pick", prov=cost.provenance)
    return _g("cost", "GREEN" if all(checks) else "RED", " · ".join(vals),
              f"spread<= {SPREAD_MAX_PCT:g}%, be<= {BE_EM_MAX:.0%} of move, "
              f"theta<= {THETA_MAX_PCT:g}%/d, delta in band", cost.provenance)


def g_clean_window(direction, s) -> GateResult:
    cost = s.get("cost")
    if cost is None:
        return _g("clean_window", "DARK", "no calendar read")
    dte_e = cost.days_to_earnings
    macro_d = getattr(cost, "macro_days", None)
    if getattr(cost, "calendar_ok", True) is False:
        return _g("clean_window", "DARK", "calendar fetch failed", prov=cost.provenance)
    bad = []
    if dte_e is not None and dte_e <= EARN_WINDOW_D:
        bad.append(f"earnings {dte_e}d")
    if macro_d is not None and macro_d <= MACRO_BLOCK_D:
        bad.append(f"{getattr(cost, 'macro_name', 'macro')} <1d")
    return _g("clean_window", "RED" if bad else "GREEN",
              " · ".join(bad) or "clear",
              f"no earnings <= {EARN_WINDOW_D}d, no print <= {MACRO_BLOCK_D}d held through",
              cost.provenance)


# ── put-only gates ────────────────────────────────────────────────────────────
def g_tape_agrees(direction, s) -> GateResult:
    c = s.get("conviction")
    cdir = getattr(c, "direction", None) if c else None
    if cdir is None:
        return _g("tape_agrees", "DARK", "no greek-flow",
                  prov=getattr(c, "provenance", None))
    ok = cdir == direction
    return _g("tape_agrees", "GREEN" if ok else "RED", f"tape {cdir}",
              f"tape {direction}", c.provenance)


def g_short_pressure(direction, s) -> GateResult:
    sh = s.get("shorts")
    if sh is None or sh.ratio_latest is None:
        return _g("short_pressure", "DARK", "no short-volume data",
                  prov=getattr(sh, "provenance", None))
    ok = sh.ratio_latest >= SHORT_RATIO_MIN and bool(sh.rising)
    return _g("short_pressure", "GREEN" if ok else "RED",
              f"short ratio {sh.ratio_latest:.0%}{' rising' if sh.rising else ''}",
              f">= {SHORT_RATIO_MIN:.0%} and rising", sh.provenance)


def g_no_squeeze(direction, s) -> GateResult:
    sh, v = s.get("shorts"), s.get("vol")
    ftd = getattr(sh, "ftd_pctile", None) if sh else None
    spike = getattr(v, "iv_spike_pct", None) if v else None
    if ftd is None and spike is None:
        return _g("no_squeeze", "DARK", "no FTD or IV history")
    bad = []
    if ftd is not None and ftd > FTD_PCTILE_MAX:
        bad.append(f"FTDs {ftd:.0f}th pct")
    if spike is not None and spike > IV_SPIKE_MAX_PCT:
        bad.append(f"front IV +{spike:.0f}% in 2d")
    # SI%float not at tier (probe 2026-06-12: stale 2021 rows) — FTD + IV legs only
    return _g("no_squeeze", "RED" if bad else "GREEN",
              " · ".join(bad) or "no squeeze marks",
              f"FTDs <= {FTD_PCTILE_MAX:.0f}th pct, IV spike <= {IV_SPIKE_MAX_PCT:g}%",
              getattr(sh, "provenance", None) or Provenance())


# ── catalyst gates ────────────────────────────────────────────────────────────
def g_cheap_implied_move(direction, s) -> GateResult:
    c = s.get("catalyst")
    if (c is None or c.ratio is None or c.quarters < CATALYST_MIN_QUARTERS):
        return _g("cheap_implied_move", "DARK",
                  f"history {getattr(c, 'quarters', 0)}q (needs {CATALYST_MIN_QUARTERS}+)",
                  prov=getattr(c, "provenance", None))
    ok = c.ratio <= C1_RATIO_MAX
    return _g("cheap_implied_move", "GREEN" if ok else "RED",
              f"implied {c.implied_move_pct:.1f}% vs usual {c.hist_move_pct:.1f}%",
              f"<= {C1_RATIO_MAX:g}x the usual move", c.provenance)


def g_expiry_capture(direction, s) -> GateResult:
    cost, cat = s.get("cost"), s.get("catalyst")
    ct = (cost.contract or {}) if cost else {}
    if not ct or cat is None or cat.report_date is None:
        return _g("expiry_capture", "DARK", "no pick or report date")
    dte, expiry = ct.get("dte"), str(ct.get("expiry") or "")
    ok = (expiry > cat.report_date and dte is not None
          and C3_DTE_LO <= dte <= C3_DTE_HI)
    return _g("expiry_capture", "GREEN" if ok else "RED",
              f"{expiry or '?'} · {dte}d", f"first expiry after {cat.report_date}, "
              f"{C3_DTE_LO}-{C3_DTE_HI} DTE", cost.provenance)


def g_not_crowded(direction, s) -> GateResult:
    # panel-wide small-lot share is unobservable without a scan loop — born DARK v1
    return _g("not_crowded", "DARK", "crowding not observable at tier")


DRIFT_GATES = [g_flow_dominance, g_flow_concentration, g_oi_basis, g_dealer_fuel,
               g_headroom, g_cheap_vol, g_term_slope, g_skew_shift, g_cost,
               g_clean_window]
PUT_EXTRA_DRIFT = [g_tape_agrees, g_short_pressure, g_no_squeeze]
CATALYST_GATES = [g_cheap_implied_move, g_flow_dominance, g_expiry_capture,
                  g_not_crowded, g_cost]
PUT_EXTRA_CATALYST = [g_no_squeeze]


def evaluate(direction: str, branch: str, signals: dict) -> list[GateResult]:
    fuel = g_dealer_fuel(direction, signals)
    fueled = fuel.state == "GREEN"
    out = []
    fns = list(DRIFT_GATES if branch == "drift" else CATALYST_GATES)
    if direction == "puts":
        fns += PUT_EXTRA_DRIFT if branch == "drift" else PUT_EXTRA_CATALYST
    for fn in fns:
        if fn is g_dealer_fuel:
            out.append(fuel)
        elif fn is g_cost:
            out.append(g_cost(direction, signals, fueled=fueled))
        else:
            out.append(fn(direction, signals))
    return out
