"""On-demand Tile 3 detail (Phase 2): per-expiry gamma map with OI/Vol toggle and
charm/vanna drift. Built when a ticker is selected — NOT in the per-cycle
snapshot — so the heavy spot-exposures/expiry-strike + greek-exposure/expiry
calls don't touch the UW budget for all 15 tickers every cycle.

Reuses server.gex.compute_levels for flip/walls/regime (same math as the
snapshot's lightweight Tile 3), fed by the corrected near-spot strike window.
"""
from __future__ import annotations
import logging

from server import gex, storage

log = logging.getLogger(__name__)

_N_EXPIRIES = 3          # this-wk, next-wk, +1
_TAGS = ("this wk", "next wk", "+2 wk")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _rungs(rows: list[dict], oi: bool) -> list[dict]:
    """Build gamma rungs from spot-exposures/expiry-strike rows, selecting the
    _oi or _vol columns. net = call + put (UW pre-signs puts negative)."""
    suffix = "oi" if oi else "vol"
    out = []
    for r in rows:
        k = _f(r.get("strike"))
        if k <= 0:
            continue
        cg = _f(r.get(f"call_gamma_{suffix}"))
        pg = _f(r.get(f"put_gamma_{suffix}"))
        out.append({"strike": k, "call_gamma": cg, "put_gamma": pg, "net": cg + pg})
    out.sort(key=lambda r: r["strike"])
    return out


def _label(expiry: str) -> str:
    # "2026-06-05" -> "Jun 05"
    import calendar
    try:
        _, m, d = expiry.split("-")
        return f"{calendar.month_abbr[int(m)]} {int(d):02d}"
    except (ValueError, IndexError):
        return expiry


def build_tile3_detail(ticker: str, flow_spot: float, direction: str) -> dict:
    """Assemble the per-expiry Tile 3 payload. Returns {"status":"unavailable"}
    when greek-exposure data is missing for the ticker."""
    if flow_spot <= 0:
        return {"status": "unavailable", "ticker": ticker}
    ge = storage.fetch_greek_exposure_expiry(ticker)
    if isinstance(ge, storage.UWFailure):
        return {"status": "unavailable", "ticker": ticker}
    ge_rows = ge.get("data") if isinstance(ge, dict) else None
    if not ge_rows:
        return {"status": "unavailable", "ticker": ticker}

    # One charm/vanna row per expiry (first wins), sorted by dte; keep the front.
    per_expiry: dict[str, dict] = {}
    for r in ge_rows:
        e = r.get("expiry")
        if e and e not in per_expiry:
            per_expiry[e] = r
    chosen = sorted(per_expiry.values(), key=lambda r: _f(r.get("dte")))[:_N_EXPIRIES]
    expiries = [r["expiry"] for r in chosen]
    if not expiries:
        return {"status": "unavailable", "ticker": ticker}

    gex_min, gex_max = round(flow_spot * 0.80), round(flow_spot * 1.20)
    es = storage.fetch_spot_exposures_expiry_strike(ticker, expiries, gex_min, gex_max)
    es_rows = es.get("data") if isinstance(es, dict) else []
    by_expiry: dict[str, list] = {}
    for r in (es_rows or []):
        by_expiry.setdefault(r.get("expiry"), []).append(r)

    drift_available = False
    views: dict[str, dict] = {}
    for i, ge_row in enumerate(chosen):
        e = ge_row["expiry"]
        rows = by_expiry.get(e, [])
        oi_rungs = _rungs(rows, oi=True)
        vol_rungs = _rungs(rows, oi=False)
        # Levels (flip/walls/regime) from the OI ladder — the standing structure.
        lv = gex.compute_levels(oi_rungs, flow_spot) if oi_rungs else None
        charm = _f(ge_row.get("call_charm")) + _f(ge_row.get("put_charm"))
        vanna = _f(ge_row.get("call_vanna")) + _f(ge_row.get("put_vanna"))
        if charm or vanna:
            drift_available = True
        views[e] = {
            "label": _label(e),
            "tag": _TAGS[i] if i < len(_TAGS) else f"+{i} wk",
            "dte": int(_f(ge_row.get("dte"))),
            "regime": ("long" if (lv and lv["gex_sign"] == "POS") else "short") if lv else "short",
            "flip_pct": lv["flip_pct"] if lv else 0.0,
            "flip_status": lv["flip_status"] if lv else "no_flip",
            "call_wall": round(flow_spot * (1 + lv["call_wall_pct"] / 100), 2) if lv else None,
            "put_wall": round(flow_spot * (1 - lv["put_wall_pct"] / 100), 2) if lv else None,
            "charm": {"dir": "up" if charm > 0 else "down" if charm < 0 else "flat", "value": charm},
            "vanna": {"dir": "up_if_iv_rises" if vanna > 0 else "up_if_iv_falls" if vanna < 0 else "flat",
                      "value": vanna},
            "oi": oi_rungs,
            "vol": vol_rungs,
        }

    return {
        "status": "ok",
        "ticker": ticker,
        "spot": flow_spot,
        "direction": direction,
        "default_expiry": expiries[0],
        "drift_available": drift_available,
        "views": views,
    }
