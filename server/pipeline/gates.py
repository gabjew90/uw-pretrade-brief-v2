"""Gate evaluators — FOUR lights (directive 2026-06-12, four-lights revision).

Rigor lives in sub-criteria INSIDE each gate; the surface shows only the lights.
A gate is GREEN when every sub-criterion is met, RED when one measurably failed,
DARK when an input is unavailable (a measurable fail trumps an unknown). PERFECT iff
every gate in the active branch is GREEN. Minimalism principle (operator): a metric
that never flips the decision is noise and must not exist in the product — which is
why the skew leg (borrow-fee artifact, MPP 2022, and usually DARK at tier history)
and the short-volume confirmation (corroboration that never flips) were DELETED, and
conviction's only informative case (tape divergence on puts) lives inside no_squeeze.

Thresholds are constants HERE and only here, one citation per number. v1 NOTE: the
directive's cross-sectional conditions (90th-pct premium, GEX quartile) ship as
absolute thresholds — panel percentiles need a scan loop the platform deliberately
doesn't have ([[basic-platform]]); archive self-percentiles join the tuning pass.
"""
from __future__ import annotations

from server.models import GateResult, Provenance

# ── thresholds (citation beside each number) ──────────────────────────────────
ASK_SHARE_MIN = 0.70      # Hu 2014: opening ask-side imbalance predicts ~1-day returns
PREM_FLOOR_USD = 1_000_000  # v1 absolute stand-in for "90th pct cross-sectional" size
CONC_SHARE_MIN = 0.60     # bets bunched in <=2 near-dated expiries, not scattered
BUILD_NET_VS_HIGH_MIN = 0.90   # still building: session net within 10% of its high —
BUILD_LAST_PRINT_MAX_MIN = 90  # and the last qualifying print <=90 min old (Hu 2014's
                               # edge is ~1-day; a faded morning burst is stale)
FLIP_DIST_MIN_PCT = 0.5   # Barbon-Buraschi 2020: firmly in the negative-gamma zone
IVR_MAX = 30.0            # Hu-Jacobs 2020: long premium wants LOW IV rank
HV_IV_MIN = 1.0           # Goyal-Saretto: HV >= IV
TERM_SLOPE_MIN = 0.0      # Vasquez 2017: flat/upward front slope
SPREAD_MAX_PCT = 5.0      # v1 flat (directive's 3/5/8 liquidity tiers later)
BE_EM_MAX = 0.70          # breakeven move <= 70% of the expected move
THETA_MAX_PCT = 10.0      # per day
DELTA_LO, DELTA_LO_FUELED, DELTA_HI = 0.40, 0.35, 0.55   # 0.35 floor only when fueled
EARN_WINDOW_D = 3         # branch selector AND clean-window sub-criterion
MACRO_BLOCK_D = 1         # operator policy 2026-06-11: only a print you'd HOLD THROUGH
FTD_PCTILE_MAX = 90.0     # latest FTDs vs the ticker's own trailing year (MPP 2022)
IV_SPIKE_MAX_PCT = 20.0   # front IV +20% in 2 sessions = squeeze-adjacent
C1_RATIO_MAX = 0.9        # Milian 2023: implied move <= 0.9x the stock's usual move
C1_DTE_LO, C1_DTE_HI = 5, 15
CATALYST_MIN_QUARTERS = 4  # below this the history is fiction (ohlc/1d ~1y at tier)

# plain-English light labels + the short waiting-on names
LABELS = {
    "smart_flow": "Smart money just opened this bet",
    "dealer_fuel": "Dealers will pour fuel on the move",
    "cheap_vol": "The options are cheap for how much this moves",
    "good_entry": "You're not overpaying to get in",
    "no_squeeze": "No squeeze trap",
    "cheap_event": "The market is underpricing this report",
}
# the short names — waiting lines AND GateVM.short (locked vocabulary, uw-fixtures.js)
SHORT = {
    "smart_flow": "smart money",
    "dealer_fuel": "dealer fuel",
    "cheap_vol": "cheap options",
    "good_entry": "entry cost",
    "no_squeeze": "squeeze check",
    "cheap_event": "report price",
}
WAITING = SHORT   # legacy alias


def _gate(name: str, subs: list[tuple[str, bool | None, str]], prov=None) -> GateResult:
    """Resolve sub-criteria → one light. False trumps None (a measurable fail decides
    the gate even when another input is unknown); any None left → DARK. `missing`
    carries the unknown subs' render strings verbatim (the DARK why-payload)."""
    failed = [s for s, ok, _ in subs if ok is False]
    unknown = [s for s, ok, _ in subs if ok is None]
    state = "RED" if failed else ("DARK" if unknown else "GREEN")
    return GateResult(name=name, label=LABELS[name], state=state,
                      failed_subcriteria=failed,
                      missing=[v for s, ok, v in subs if ok is None and v],
                      values=[v for _, _, v in subs if v],
                      provenance=prov or Provenance())


def _side(direction: str) -> str:
    return "call" if direction == "calls" else "put"


# ── gate 1: smart_flow (flow dominance + concentration + OI basis) ────────────
def g_smart_flow(direction, s) -> GateResult:
    flow, pos = s.get("flow"), s.get("positioning")
    if flow is None or getattr(flow, "direction", None) is None:
        note = getattr(getattr(flow, "provenance", None), "note", "") or "no flow read"
        return _gate("smart_flow", [("flow", None, note)],
                     getattr(flow, "provenance", None))
    st = (flow.side_stats or {}).get(_side(direction)) or {}
    total = sum((flow.side_stats.get(sd) or {}).get("opening_prem", 0.0)
                for sd in ("call", "put"))
    subs: list[tuple] = []
    subs.append(("opening basis", flow.direction_basis == "opening_flow",
                 f"basis {flow.direction_basis} (needs opening flow)"))
    ask = st.get("ask_share")
    subs.append(("ask-side share", None if ask is None else ask >= ASK_SHARE_MIN,
                 "no ask/bid split" if ask is None else
                 f"ask-side {ask:.0%} of ${total/1e6:.1f}M (needs >={ASK_SHARE_MIN:.0%})"))
    subs.append(("size", total >= PREM_FLOOR_USD,
                 f"${total/1e6:.1f}M opening (needs >=${PREM_FLOOR_USD/1e6:g}M)"))
    if flow.direction == direction:
        subs.append(("lean", flow.lean_quality != "weak",
                     f"lean {flow.lean_note} {flow.lean_quality}"))
    c_share, c_dte, c_band = (st.get("top2_share"), st.get("top2_dte_ok"),
                              st.get("strike_band_ok"))
    conc_ok = (None if c_share is None or c_dte is None or c_band is None
               else (c_share >= CONC_SHARE_MIN and c_dte and c_band))
    subs.append(("concentration", conc_ok,
                 "expiry/strike read incomplete" if conc_ok is None else
                 f"top-2 expiries {c_share:.0%} near-money 5-30 DTE (needs >={CONC_SHARE_MIN:.0%})"))
    if flow.direction == direction:
        conf = getattr(pos, "confirmation", "unconfirmed") if pos else "unconfirmed"
        # building/flat/unconfirmed pass (archive-decoupled); only unwinding fails
        subs.append(("OI held", conf != "unwinding", f"OI {conf}"))
    # STILL BUILDING (intraday recency): the session's cumulative net premium must sit
    # within 10% of its high AND the side's last qualifying print must be fresh — a
    # morning burst that faded or reversed by now reads RED even if totals clear
    nvh, age = st.get("net_vs_high"), st.get("last_age_min")
    if nvh is None or age is None:
        subs.append(("still building", None, "no timed prints to read recency"))
    else:
        subs.append(("still building",
                     nvh >= BUILD_NET_VS_HIGH_MIN and age <= BUILD_LAST_PRINT_MAX_MIN,
                     f"net {nvh:.0%} of session high, last buy {age:.0f}m before the "
                     f"session's final print (needs >={BUILD_NET_VS_HIGH_MIN:.0%} and "
                     f"<={BUILD_LAST_PRINT_MAX_MIN:g}m)"))
    return _gate("smart_flow", subs, flow.provenance)


# ── gate 2: dealer_fuel (gamma sign + flip distance + headroom) ───────────────
def g_dealer_fuel(direction, s) -> GateResult:
    dg, cost = s.get("dealer_gamma"), s.get("cost")
    if dg is None or getattr(dg, "flip_status", "unavailable") == "unavailable":
        return _gate("dealer_fuel", [("dealer gamma", None, "no dealer gamma")],
                     getattr(dg, "provenance", None))
    subs: list[tuple] = []
    subs.append(("negative gamma", dg.gex_sign == "NEG",
                 f"gamma {dg.gex_sign} (needs NEG)"))   # v1: quartile deferred (no panel)
    fd = getattr(dg, "flip_pct", None)
    subs.append(("flip distance", None if fd is None else abs(fd) >= FLIP_DIST_MIN_PCT,
                 "no flip in range" if fd is None else
                 f"flip {fd:+.1f}% away (needs >={FLIP_DIST_MIN_PCT:g}%)"))
    em = getattr(cost, "expected_move_pct", None) if cost else None
    wall = (getattr(dg, "call_wall_pct", None) if direction == "calls"
            else getattr(dg, "put_wall_pct", None))
    head = None if (wall is None or not em) else abs(wall) >= em
    subs.append(("headroom", head,
                 "wall or expected move unknown" if head is None else
                 f"wall {abs(wall):.1f}% vs move {em:.1f}% (needs >=1 move)"))
    return _gate("dealer_fuel", subs, dg.provenance)


# ── gate 3: cheap_vol (IV rank + HV/IV + term slope + clean window) ───────────
def g_cheap_vol(direction, s) -> GateResult:
    v, cost = s.get("vol"), s.get("cost")
    subs: list[tuple] = []
    ivr = getattr(v, "ivr", None) if v else None
    subs.append(("IV rank", None if ivr is None else ivr < IVR_MAX,
                 "IV rank unavailable" if ivr is None else
                 f"IV rank {ivr:.0f} (needs <{IVR_MAX:g})"))
    hr = getattr(v, "hv_iv_ratio", None) if v else None
    subs.append(("HV/IV", None if hr is None else hr >= HV_IV_MIN,
                 "realized vol unavailable" if hr is None else
                 f"HV/IV {hr:.2f} (needs >={HV_IV_MIN:g})"))
    ts = getattr(v, "term_slope", None) if v else None
    subs.append(("term slope", None if ts is None else ts >= TERM_SLOPE_MIN,
                 "no term structure" if ts is None else
                 f"slope {ts:+.3f} (needs flat/upward)"))
    # clean window: earnings <=3d or a print you'd hold through (<=1d, operator policy
    # 2026-06-11). A failed calendar fetch is unknown, never a free pass.
    if cost is None or getattr(cost, "calendar_ok", True) is False:
        subs.append(("clean window", None, "calendar fetch failed"))
    else:
        dte_e, md = cost.days_to_earnings, getattr(cost, "macro_days", None)
        bad = []
        if dte_e is not None and dte_e <= EARN_WINDOW_D:
            bad.append(f"earnings {dte_e}d")
        if md is not None and md <= MACRO_BLOCK_D:
            bad.append(f"{getattr(cost, 'macro_name', 'macro')} <1d")
        subs.append(("clean window", not bad, " · ".join(bad) or "calendar clear"))
    return _gate("cheap_vol", subs, getattr(v, "provenance", None))


# ── gate 4: good_entry (the cost math, unchanged) ─────────────────────────────
def g_good_entry(direction, s, *, fueled: bool) -> GateResult:
    cost, flow = s.get("cost"), s.get("flow")
    if getattr(flow, "direction", None) != direction:
        return _gate("good_entry", [("contract", None, "no contract priced for this side")])
    if cost is None or cost.spread_pct is None:
        return _gate("good_entry", [("chain", None, "no chain to price")],
                     getattr(cost, "provenance", None))
    ct = cost.contract or {}
    d, th = ct.get("delta"), ct.get("theta_day_pct")
    be, em = cost.breakeven_move_pct, cost.expected_move_pct
    subs: list[tuple] = []
    subs.append(("spread", cost.spread_pct <= SPREAD_MAX_PCT,
                 f"spread {cost.spread_pct:.0f}% (needs <={SPREAD_MAX_PCT:g}%)"))
    be_ok = None if (be is None or not em) else be <= BE_EM_MAX * em
    subs.append(("breakeven", be_ok,
                 "breakeven/move unknown" if be_ok is None else
                 f"be {be:.1f}% vs move {em:.1f}% (needs <={BE_EM_MAX:.0%} of move)"))
    subs.append(("theta", None if th is None else th <= THETA_MAX_PCT,
                 "greeks missing" if th is None else
                 f"theta {th:.0f}%/d (needs <={THETA_MAX_PCT:g}%)"))
    lo = DELTA_LO_FUELED if fueled else DELTA_LO
    subs.append(("delta band", None if d is None else lo <= d <= DELTA_HI,
                 "greeks missing" if d is None else
                 f"delta {d:.2f} (needs {lo:g}-{DELTA_HI:g})"))
    return _gate("good_entry", subs, cost.provenance)


# ── puts veto: no_squeeze (FTD + IV spike + tape sign; SI dead at tier) ───────
def g_no_squeeze(direction, s) -> GateResult:
    sh, v, c = s.get("shorts"), s.get("vol"), s.get("conviction")
    subs: list[tuple] = []
    ftd = getattr(sh, "ftd_pctile", None) if sh else None
    subs.append(("FTDs", None if ftd is None else ftd <= FTD_PCTILE_MAX,
                 "no FTD history" if ftd is None else
                 f"FTDs {ftd:.0f}th pct of own year (needs <={FTD_PCTILE_MAX:.0f}th)"))
    spike = getattr(v, "iv_spike_pct", None) if v else None
    subs.append(("IV spike", None if spike is None else spike <= IV_SPIKE_MAX_PCT,
                 "no IV history" if spike is None else
                 f"front IV {spike:+.0f}% in 2d (needs <=+{IV_SPIKE_MAX_PCT:g}%)"))
    # the old conviction leg's ONLY informative case, absorbed here: a tape pushing UP
    # while you buy puts is hedger contamination / squeeze fuel
    cdir = getattr(c, "direction", None) if c else None
    subs.append(("tape sign", None if cdir is None else cdir == "puts",
                 "no greek-flow" if cdir is None else f"tape {cdir} (needs puts)"))
    # SI%float sub: NOT at tier (probe 2026-06-12: stale 2021 rows) — omitted, honest
    return _gate("no_squeeze", subs, getattr(sh, "provenance", None))


# ── catalyst gate: cheap_event (implied vs history + expiry capture) ──────────
def g_cheap_event(direction, s) -> GateResult:
    c, cost = s.get("catalyst"), s.get("cost")
    if c is None or c.days_to_earnings is None:
        return _gate("cheap_event", [("report", None, "no report in window")])
    subs: list[tuple] = []
    if c.quarters < CATALYST_MIN_QUARTERS or c.ratio is None:
        subs.append(("implied vs usual", None,
                     f"history {c.quarters}q (needs {CATALYST_MIN_QUARTERS}+)"))
    else:
        subs.append(("implied vs usual", c.ratio <= C1_RATIO_MAX,
                     f"implied {c.implied_move_pct:.1f}% vs usual {c.hist_move_pct:.1f}% "
                     f"(needs <={C1_RATIO_MAX:g}x)"))
    ct = (cost.contract or {}) if cost else {}
    dte, expiry = ct.get("dte"), str(ct.get("expiry") or "")
    cap = (None if not ct or c.report_date is None
           else (expiry > c.report_date and dte is not None
                 and C1_DTE_LO <= dte <= C1_DTE_HI))
    subs.append(("expiry capture", cap,
                 "no pick or report date" if cap is None else
                 f"{expiry or '?'} · {dte}d (needs first weekly after {c.report_date}, "
                 f"{C1_DTE_LO}-{C1_DTE_HI} DTE)"))
    return _gate("cheap_event", subs, c.provenance)


DRIFT_GATES = ["smart_flow", "dealer_fuel", "cheap_vol", "good_entry"]
CATALYST_GATES = ["cheap_event", "smart_flow", "good_entry"]


def evaluate(direction: str, branch: str, signals: dict) -> list[GateResult]:
    fuel = g_dealer_fuel(direction, signals)
    by_name = {
        "smart_flow": lambda: g_smart_flow(direction, signals),
        "dealer_fuel": lambda: fuel,
        "cheap_vol": lambda: g_cheap_vol(direction, signals),
        "good_entry": lambda: g_good_entry(direction, signals,
                                           fueled=fuel.state == "GREEN"),
        "cheap_event": lambda: g_cheap_event(direction, signals),
    }
    names = list(DRIFT_GATES if branch == "drift" else CATALYST_GATES)
    out = [by_name[n]() for n in names]
    if direction == "puts":
        out.append(g_no_squeeze(direction, signals))   # the hard veto, both branches
    return out
