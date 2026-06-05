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

    def _fac(ok, value, note):
        return {"ok": ok, "value": value, "note": note}

    def _money(v):
        a = abs(v)
        return (f"${a/1e9:.1f}B" if a >= 1e9 else f"${a/1e6:.1f}M" if a >= 1e6
                else f"${a/1e3:.0f}K" if a >= 1e3 else f"${a:.0f}")

    factors: dict[str, dict] = {}
    k = c["strike"]

    # ── Tier 1: Thesis (Flow · Campaign · Room) ──────────────────────────────
    # Flow: prefer the per-strike premium MAP (real $); fall back to the yes/no set.
    fpm = ctx.get("flow_premium_by_strike")
    fs = ctx.get("flow_strikes")
    if fpm is not None:
        prem = fpm.get(k, 0.0)
        if prem > 0:
            factors["flow"] = _fac(True, _money(prem), f"smart-money {_money(prem)} at this strike")
        else:
            factors["flow"] = _fac(False, "—", "no flow at this strike")
    elif fs is None:
        factors["flow"] = _fac(None, "—", "flow data unavailable")
    else:
        hit = k in fs
        factors["flow"] = _fac(hit, "✓" if hit else "—",
                               "smart-money flow hit this strike" if hit else "no flow at this strike")

    # Campaign: prefer the per-strike OI MAP (Δ% + trend); fall back to the set.
    obm = ctx.get("oi_by_strike")
    ob = ctx.get("oi_building")
    if obm is not None:
        info = obm.get(k)
        if info and info.get("trend") == "building":
            d = info.get("delta_pct")
            factors["campaign"] = _fac(True, f"+{d:.0f}%" if d is not None else "▲",
                                       f"OI building (+{d:.0f}% 5d)" if d is not None else "OI building here")
        elif info and info.get("trend") == "unwinding":
            d = info.get("delta_pct")
            factors["campaign"] = _fac(False, f"{d:.0f}%" if d is not None else "▼",
                                       f"OI unwinding ({d:.0f}% 5d)" if d is not None else "OI unwinding")
        else:
            factors["campaign"] = _fac(False, "flat", "OI flat / not building here")
    elif ob is None:
        factors["campaign"] = _fac(None, "—", "OI trend unavailable")
    else:
        bld = k in ob
        factors["campaign"] = _fac(bld, "✓" if bld else "—",
                                   "OI building here" if bld else "OI flat / not building here")

    wall = ctx.get("call_wall") if calls else ctx.get("put_wall")
    if wall is None:
        factors["room"] = _fac(None, "—", "wall data unavailable")
    else:
        room_pct = ((wall - k) if calls else (k - wall)) / spot * 100
        if room_pct >= _ROOM_MIN_PCT:
            factors["room"] = _fac(True, f"{room_pct:.1f}%", f"{room_pct:.1f}% to the {'call' if calls else 'put'} wall")
        else:
            factors["room"] = _fac(False, f"{room_pct:.1f}%", f"{room_pct:.1f}% — at/over the wall")

    # ── Tier 2: Realism (Target) ─────────────────────────────────────────────
    em = ctx.get("expected_move_pct")
    be_move = None
    if em is None or c.get("premium") is None:
        factors["target"] = _fac(None, "—", "expected move unavailable")
    else:
        be_move = _breakeven_move_pct(c, spot, calls)
        ok = be_move <= em
        factors["target"] = _fac(ok, f"{be_move:.1f}/{em:.1f}",
                                 (f"needs {be_move:.1f}% vs {em:.1f}% expected" if ok
                                  else f"needs {be_move:.1f}% — bigger than {em:.1f}% expected"))

    # ── Tier 3: Cost / risk (Execution · Greeks) ─────────────────────────────
    spread, iv, atm_iv = c.get("spread_pct"), c.get("iv"), ctx.get("atm_iv")
    if spread is None or atm_iv is None or iv is None:
        factors["execution"] = _fac(None, "—", "quote/IV unavailable")
    else:
        skew_ok = iv <= atm_iv * _SKEW_PUMP
        sval = f"{spread:.0f}%"
        if spread <= _SPREAD_MAX_PCT and skew_ok:
            factors["execution"] = _fac(True, sval, f"spread {spread:.0f}%, IV fair vs ATM")
        elif spread > _SPREAD_MAX_PCT:
            factors["execution"] = _fac(False, sval, f"spread {spread:.0f}% — bleeds edge")
        else:
            factors["execution"] = _fac(False, sval + "·skew", "IV skew-pumped vs ATM")

    delta = c.get("delta")
    if delta is None:
        factors["greeks"] = _fac(None, "—", "Δ unavailable")
    else:
        ok_delta = _DELTA_LO <= abs(delta) <= _DELTA_HI
        theta = c.get("theta")
        ok_theta = theta is None or (c.get("premium") and abs(theta) / c["premium"] <= _THETA_MAX_FRAC)
        dval = f"Δ{abs(delta):.2f}"
        if ok_delta and ok_theta:
            factors["greeks"] = _fac(True, dval, f"Δ{abs(delta):.2f}, θ tolerable")
        elif not ok_delta:
            factors["greeks"] = _fac(False, dval, f"Δ{abs(delta):.2f} — outside {_DELTA_LO:g}-{_DELTA_HI:g}")
        else:
            factors["greeks"] = _fac(False, dval, f"Δ{abs(delta):.2f}, θ decay too heavy")

    def _pts(*names):
        return sum(1 for n in names if factors[n]["ok"] is True)
    tier1 = _pts("flow", "campaign", "room")
    tier2 = _pts("target")
    tier3 = _pts("execution", "greeks")
    return {
        "strike": c["strike"], "type": c.get("type"), "premium": c.get("premium"),
        "delta": delta, "spread_pct": spread, "iv": iv, "oi": c.get("oi"),
        "be_move_pct": be_move,
        "factors": factors, "tier1": tier1, "tier2": tier2, "tier3": tier3,
        "score": tier1 + tier2 + tier3,
    }


def rank_contracts(scored: list[dict]) -> list[dict]:
    """Rank all contracts best→worst by tier (Thesis → Realism → Cost), then the
    flow→cost→delta tiebreak within a tier. NO veto: a wide-spread / unrealistic
    contract ranks LOWER but is never dropped. Adds a 1-based `rank` to each."""
    def key(s):
        flow_ok = (s.get("factors", {}).get("flow", {}) or {}).get("ok") is True
        return (-s.get("tier1", 0), -s.get("tier2", 0), -s.get("tier3", 0),
                0 if flow_ok else 1, s.get("premium") or 1e9,
                abs((s.get("delta") or 0) - 0.45))
    ordered = sorted(scored, key=key)
    for i, s in enumerate(ordered, 1):
        s["rank"] = i
    return ordered


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


_HOLD_DAYS = 3  # rough weekly-hold horizon for the theta-drag context estimate

def _cost_check(top: dict | None, expected_move_pct: float | None) -> dict:
    """Can the expected move clear the trade's cost? Dimensionally sound: compare
    the expected UNDERLYING move to the top contract's breakeven move
    (be_move_pct already bakes in the premium paid). theta_drag_pct/spread_pct are
    premium-% cost CONTEXT (the bleed), never mixed into the underlying-% compare.
    covers=None when either input is missing (honest unknown)."""
    be = (top or {}).get("be_move_pct")
    spread = (top or {}).get("spread_pct")
    theta = (top or {}).get("theta")
    prem = (top or {}).get("ask") or (top or {}).get("mid")
    theta_drag = (abs(theta) * _HOLD_DAYS / prem * 100.0
                  if (theta is not None and prem) else None)
    covers = None
    if expected_move_pct is not None and be is not None:
        covers = expected_move_pct >= be
    return {
        "expected_move_pct": expected_move_pct,
        "breakeven_move_pct": be,
        "theta_drag_pct": round(theta_drag, 1) if theta_drag is not None else None,
        "spread_pct": spread,
        "hold_days": _HOLD_DAYS,
        "covers": covers,
    }


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

    # NOTE: stand-down no longer short-circuits to an empty result. The picker is
    # a ranked COMPARISON tool, not a decider — we build and show the contracts
    # even when a gate trips, surfacing the caution via status + reason. The
    # operator chooses (the frontend renders a STAND DOWN banner above the table).

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
        iv = (r.get("iv") or None) or gk_iv
        row = score_contract({
            "strike": r["strike"], "type": r["type"], "premium": ask or mid or None,
            "delta": delta, "theta": theta,
            "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
            "iv": iv, "oi": r.get("oi"),
        }, sctx)
        # Raw chain quote (display): so the row reads as a real contract you'd take
        # to a broker, alongside the why-it-ranks factors.
        row.update({
            "expiry": expiry, "theta": theta,
            "bid": round(bid, 2), "ask": round(ask, 2),
            "last": round(r.get("last") or 0, 2), "mid": round(mid, 2),
            "iv": round(iv, 4) if iv is not None else None,
            "volume": r.get("volume"), "oi": r.get("oi"),
        })
        scored.append(row)

    ranked = rank_contracts(scored)
    out = {
        "status": "stand_down" if gates["stand_down"] else "ok",
        "ticker": ticker, "direction": direction, "expiry": expiry,
        "gates": gates, "term_curve": term_curve, "expected_move_pct": expected_move_pct,
        "ranked": ranked, "top": ranked[0] if ranked else None,
        "cost_check": _cost_check(ranked[0] if ranked else None, expected_move_pct),
    }
    if gates["stand_down"]:
        out["reason"] = gates["reason"]   # surfaced in the frontend STAND DOWN banner
    return out
