"""Tile 4 — Contract Picker & Final Gate.

Scores realistic weekly contracts (in the Tiles 1-3 direction) on six checks,
enforces hard event/IV-rank gates, and surfaces ONE pick - or "stand down".

Pure scoring/gate logic (heavily tested) + orchestration (build_tile4) that
reuses Tiles 1-3 outputs and fetches only the new vol/greeks/event data on
demand. Honest-degrade rule: missing data -> "unknown" (never a fabricated
pass); an un-evaluable hard gate warns, never greenlights.
"""
from __future__ import annotations
import datetime as _dt
import logging

from server import storage, uw

log = logging.getLogger(__name__)

_IV_RANK_BLOCK = 80.0
_SPREAD_MAX_PCT = 5.0
_SKEW_PUMP = 1.10        # iv > atm_iv * this -> skew-pumped
_DELTA_LO, _DELTA_HI = 0.35, 0.55
_THETA_MAX_FRAC = 0.15   # |theta|/premium per day tolerated
_ROOM_MIN_PCT = 1.0      # clear distance to the directional wall


# ── Gates ────────────────────────────────────────────────────────────────────

def evaluate_gates(*, iv_rank, event_before_expiry, term_front, term_back) -> dict:
    """Veto layer. event/iv_rank can hard-block (-> stand down); term_structure is
    advisory (amber/green). None inputs -> 'unknown' (warn, never greenlight)."""
    event = ("unknown" if event_before_expiry is None
             else "block" if event_before_expiry else "ok")
    if iv_rank is None:
        ivr = "unknown"
    elif iv_rank > _IV_RANK_BLOCK:
        ivr = "block"
    else:
        ivr = "ok"
    if term_front is None or term_back is None:
        term = "unknown"
    else:
        term = "amber" if term_front > term_back else "green"
    stand_down = event == "block" or ivr == "block"
    warn = "unknown" in (event, ivr, term)
    reason = ""
    if event == "block":
        reason = "Event (earnings/FDA) before expiry — don't buy premium into it."
    elif ivr == "block":
        reason = f"IV rank {iv_rank:.0f} > {_IV_RANK_BLOCK:.0f} — rich vs its own history, crush risk."
    return {
        "event": {"state": event},
        "iv_rank": {"state": ivr, "value": iv_rank},
        "term_structure": {"state": term, "front": term_front, "back": term_back},
        "stand_down": stand_down,
        "warn": warn,
        "reason": reason,
    }


# ── Per-contract scoring ─────────────────────────────────────────────────────

def _breakeven_move_pct(c: dict, spot: float, calls: bool) -> float:
    be = (c["strike"] + c["premium"]) if calls else (c["strike"] - c["premium"])
    return ((be - spot) if calls else (spot - be)) / spot * 100


def score_contract(c: dict, ctx: dict) -> dict:
    """Six checks (Flow.Campaign.Room.Target.Execution.Greeks) -> pass/fail/unknown.
    Target & Execution are hard-fail caps: an explicit fail makes the contract
    ineligible to be the pick (a wide spread / unrealistic target should sink it
    regardless of the other four)."""
    spot = ctx["spot"]
    calls = ctx.get("direction", "calls") == "calls"
    checks: dict[str, str] = {}

    fs = ctx.get("flow_strikes")
    checks["flow"] = "unknown" if fs is None else ("pass" if c["strike"] in fs else "fail")

    ob = ctx.get("oi_building")
    checks["campaign"] = "unknown" if ob is None else ("pass" if c["strike"] in ob else "fail")

    wall = ctx.get("call_wall") if calls else ctx.get("put_wall")
    if wall is None:
        checks["room"] = "unknown"
    else:
        room_pct = ((wall - c["strike"]) if calls else (c["strike"] - wall)) / spot * 100
        checks["room"] = "pass" if room_pct >= _ROOM_MIN_PCT else "fail"

    em = ctx.get("expected_move_pct")
    be_move = None
    if em is None or c.get("premium") is None:
        checks["target"] = "unknown"
    else:
        be_move = _breakeven_move_pct(c, spot, calls)
        checks["target"] = "pass" if be_move <= em else "fail"

    spread, iv, atm_iv = c.get("spread_pct"), c.get("iv"), ctx.get("atm_iv")
    if spread is None or atm_iv is None or iv is None:
        checks["execution"] = "unknown"
    else:
        checks["execution"] = "pass" if (spread <= _SPREAD_MAX_PCT and iv <= atm_iv * _SKEW_PUMP) else "fail"

    delta = c.get("delta")
    if delta is None:
        checks["greeks"] = "unknown"
    else:
        ok_delta = _DELTA_LO <= abs(delta) <= _DELTA_HI
        theta = c.get("theta")
        ok_theta = theta is None or (c.get("premium") and abs(theta) / c["premium"] <= _THETA_MAX_FRAC)
        checks["greeks"] = "pass" if (ok_delta and ok_theta) else "fail"

    eligible = checks["target"] != "fail" and checks["execution"] != "fail"
    score = sum(1 for v in checks.values() if v == "pass")
    reason = ""
    if checks["target"] == "fail":
        reason = f"Needs a {be_move:.1f}% move — bigger than the week's expected {em:.1f}%."
    elif checks["execution"] == "fail":
        reason = "Wide spread or skew-pumped IV — cost eats the edge."
    elif checks["room"] == "fail":
        reason = "Little room to the wall in your direction."
    return {
        "strike": c["strike"], "type": c.get("type"), "premium": c.get("premium"),
        "delta": delta, "spread_pct": spread, "iv": iv, "oi": c.get("oi"),
        "be_move_pct": be_move, "checks": checks, "score": score,
        "eligible": eligible, "reason": reason,
    }


def pick_best(scored: list[dict]) -> dict | None:
    """Best eligible contract: highest score, then flow-aligned, then cheaper,
    then delta nearest 0.45. None when nothing is eligible."""
    eligible = [s for s in scored if s.get("eligible")]
    if not eligible:
        return None

    def key(s):
        flow_rank = 0 if (s.get("checks", {}).get("flow") == "pass") else 1
        return (-s.get("score", 0), flow_rank, s.get("premium") or 1e9,
                abs((s.get("delta") or 0) - 0.45))
    return sorted(eligible, key=key)[0]


# ── Orchestration ────────────────────────────────────────────────────────────

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _event_before(expiry: str, *payloads) -> bool | None:
    """True if any earnings/FDA date in the payloads falls on/before `expiry`.
    None when no usable date data at all (-> gate 'unknown')."""
    try:
        exp = _dt.date.fromisoformat(expiry)
    except (TypeError, ValueError):
        return None
    today = _dt.date.today()
    seen_any = False
    for p in payloads:
        rows = p.get("data") if isinstance(p, dict) else None
        for r in (rows or []):
            for k in ("report_date", "start_date", "date", "expected_date",
                      "target_date", "announced_date"):
                d = r.get(k) if isinstance(r, dict) else None
                if not d:
                    continue
                try:
                    dd = _dt.date.fromisoformat(str(d)[:10])
                except ValueError:
                    continue
                seen_any = True
                if today <= dd <= exp:
                    return True
                break
    return False if seen_any else None


def _expected_move_pct(atm_payload, spot: float) -> float | None:
    """Expected move % ≈ the ATM straddle. UW's atm-chains returns per-ATM-
    contract rows (call + put) with bid/ask + option_symbol — NO straddle field —
    so we derive it: ATM call mid + ATM put mid, picking the strikes nearest spot
    on each side. (Verified against live TSLA payload 2026-06-01.)"""
    if not isinstance(atm_payload, dict) or spot <= 0:
        return None
    best_call = best_put = None  # (|strike-spot|, mid)
    for r in (atm_payload.get("data") or []):
        parsed = uw.parse_option_symbol(r.get("option_symbol"))
        if not parsed:
            continue
        bid, ask = _f(r.get("bid")), _f(r.get("ask"))
        mid = ((bid + ask) / 2) if (bid is not None and ask is not None) else (ask or bid)
        if not mid:
            continue
        dist = abs(parsed["strike"] - spot)
        if parsed["type"] == "call" and (best_call is None or dist < best_call[0]):
            best_call = (dist, mid)
        elif parsed["type"] == "put" and (best_put is None or dist < best_put[0]):
            best_put = (dist, mid)
    if best_call and best_put:
        straddle = best_call[1] + best_put[1]
        return straddle / spot * 100 if straddle > 0 else None
    return None


def build_tile4(ticker: str, ctx: dict) -> dict:
    """Assemble the contract-picker payload. ctx carries the reused Tiles 1-3
    outputs (spot, direction, flow_strikes, oi_building, call_wall, put_wall).

    NOTE: UW field-shape parsing here (atm straddle, greeks-by-strike) is
    validated only against stubs so far; confirm against live data."""
    spot = ctx.get("spot") or 0.0
    direction = ctx.get("direction", "calls")
    calls = direction == "calls"
    if spot <= 0:
        return {"status": "unavailable", "ticker": ticker, "reason": "no spot"}

    chain_raw = storage.fetch_option_contracts(ticker, 500)
    if isinstance(chain_raw, storage.UWFailure):
        return {"status": "unavailable", "ticker": ticker, "reason": f"chain: {chain_raw.message}"}
    records = uw.contract_records(chain_raw)
    side = "call" if calls else "put"
    today = _dt.date.today()

    def _dte(e):
        try:
            return (_dt.date.fromisoformat(e) - today).days
        except (TypeError, ValueError):
            return -9999
    fut = [r for r in records if r.get("type") == side and _dte(r.get("expiry")) >= 0]
    if not fut:
        return {"status": "unavailable", "ticker": ticker, "reason": "no future contracts in direction"}
    expiry = min((r["expiry"] for r in fut), key=lambda e: _dte(e))

    def _ok(x):
        return x if isinstance(x, dict) else None
    ivr_p = _ok(storage.fetch_interpolated_iv(ticker, True))
    vol_p = _ok(storage.fetch_volatility(ticker, True))
    atm_p = _ok(storage.fetch_atm_chains(ticker, [expiry]))
    greeks_p = _ok(storage.fetch_greeks(ticker, expiry))
    earn_p = _ok(storage.fetch_earnings(ticker, False))
    fda_p = _ok(storage.fetch_fda_calendar(ticker))

    ivr = uw.extract_iv_rank(vol_p, ivr_p)
    term = uw.term_structure(vol_p)
    term_front = term[0]["iv"] * 100 if term else None
    term_back = term[-1]["iv"] * 100 if len(term) > 1 else None
    atm_iv = term[0]["iv"] if term else None
    expected_move_pct = _expected_move_pct(atm_p, spot)
    event_before = _event_before(expiry, earn_p or {}, fda_p or {})

    gates = evaluate_gates(iv_rank=ivr, event_before_expiry=event_before,
                           term_front=term_front, term_back=term_back)
    term_curve = [{"dte": t["dte"], "iv": round(t["iv"] * 100, 1)} for t in term]

    if gates["stand_down"]:
        return {"status": "stand_down", "ticker": ticker, "direction": direction,
                "expiry": expiry, "gates": gates, "term_curve": term_curve,
                "recommendation": None, "reason": gates["reason"]}

    greeks_by_strike: dict[float, dict] = {}
    for r in (greeks_p.get("data") if greeks_p else []) or []:
        k = _f(r.get("strike"))
        if k is not None:
            greeks_by_strike[k] = r

    chain = sorted((r for r in fut if r["expiry"] == expiry), key=lambda r: r["strike"])
    cand = [r for r in chain if (r["strike"] >= spot if calls else r["strike"] <= spot)]
    cand = (cand[:8] if calls else cand[-8:]) or chain[:8]
    sctx = {"spot": spot, "direction": direction,
            "flow_strikes": ctx.get("flow_strikes"), "oi_building": ctx.get("oi_building"),
            "call_wall": ctx.get("call_wall"), "put_wall": ctx.get("put_wall"),
            "expected_move_pct": expected_move_pct, "atm_iv": atm_iv}
    scored = []
    for r in cand:
        bid, ask = r.get("bid") or 0, r.get("ask") or 0
        mid = (bid + ask) / 2 or ask or bid
        spread_pct = ((ask - bid) / mid * 100) if mid else None
        gk = greeks_by_strike.get(r["strike"], {})
        # UW greeks rows carry SEPARATE call_/put_ columns (not flat delta/theta);
        # select the leg matching the trade direction. (Verified vs live payload.)
        dpx = "call" if calls else "put"
        delta = _f(gk.get(f"{dpx}_delta", gk.get("delta")))
        theta = _f(gk.get(f"{dpx}_theta", gk.get("theta")))
        gk_iv = _f(gk.get(f"{dpx}_volatility", gk.get("iv")))
        scored.append(score_contract({
            "strike": r["strike"], "type": r["type"], "premium": ask or mid or None,
            "delta": delta, "theta": theta,
            "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
            "iv": (r.get("iv") or None) or gk_iv, "oi": r.get("oi"),
        }, sctx))

    best = pick_best(scored)
    scored = [{**s, "pick": bool(best and s["strike"] == best["strike"])} for s in scored]
    if best:
        best = {**best, "pick": True}

    return {
        "status": "ok", "ticker": ticker, "direction": direction, "expiry": expiry,
        "gates": gates, "term_curve": term_curve, "expected_move_pct": expected_move_pct,
        "contracts": scored, "recommendation": best,
    }
