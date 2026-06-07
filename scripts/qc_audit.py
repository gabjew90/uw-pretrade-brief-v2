"""One-off QC audit: fetch every grid ticker's /api/lookup and run a sanity
battery across all tiles, flagging structural issues (mismatched labels vs data,
NaN/Infinity, dead/degenerate states, cross-tile inconsistencies). REPLAY-safe.
Not a pytest — run against a running replay server. Delete after use."""
from __future__ import annotations
import json, math, sys, urllib.request

BASE = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def bad_num(x):
    return isinstance(x, float) and (math.isnan(x) or math.isinf(x))


def scan_bad(obj, path=""):
    """Recursively find NaN/Infinity and 'undefined'/'NaN' strings."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += scan_bad(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            out += scan_bad(v, f"{path}[{i}]")
    elif bad_num(obj):
        out.append(f"{path}={obj}")
    elif isinstance(obj, str) and obj.strip().lower() in ("nan", "undefined", "infinity", "none"):
        out.append(f"{path}={obj!r}")
    return out


def audit(t):
    issues = []
    try:
        d = get(f"/api/lookup/{t}")
    except Exception as e:
        return [f"LOOKUP FAILED: {e}"]
    # universal bad-number scan
    for b in scan_bad(d):
        issues.append(f"bad value {b}")

    # ── Tile 2 ────────────────────────────────────────────────
    t2 = d.get("tile2") or {}
    strikes = t2.get("strikes") or []
    for side in ("call", "put"):
        ss = [s for s in strikes if s.get("side") == side]
        in_agg = [s for s in ss if s.get("in_aggregate")]
        is_ctx = t2.get(f"{side}_is_context")
        conf = t2.get(f"{side}_confirmation")
        sess = t2.get(f"{side}_sessions") or []
        # context must imply unconfirmed (no bet to confirm)
        if is_ctx and conf != "unconfirmed":
            issues.append(f"T2 {side}: is_context but confirmation={conf} (should be unconfirmed)")
        # a confirmed (building/unwinding) side must have an aggregate series
        if conf in ("building", "unwinding", "flat") and ss and not sess:
            issues.append(f"T2 {side}: conf={conf} but {side}_sessions empty")
        # aggregate strikes must be a subset that actually exists
        if ss and not in_agg and not is_ctx and conf != "unconfirmed":
            issues.append(f"T2 {side}: conf={conf} but NO in_aggregate strikes")
        # trend % sanity vs sessions
        if len(sess) >= 2 and sess[0].get("oi"):
            calc = round((sess[-1]["oi"] - sess[0]["oi"]) / sess[0]["oi"] * 100, 1)
            shown = t2.get(f"{side}_oi_trend_pct")
            if abs(calc - shown) > 0.5:
                issues.append(f"T2 {side}: trend {shown}% != recomputed {calc}% from sessions")
    # flow_side must be one of the sides that has flow (or "")
    fs = t2.get("flow_side")
    if fs and not any(s.get("side") == fs and not s.get("is_context") for s in strikes):
        issues.append(f"T2 flow_side={fs} but no non-context {fs} strikes")
    # settlement: forming must have settles_on; settled must not
    if t2.get("settlement_mode") == "forming" and not t2.get("settles_on"):
        issues.append("T2 forming but no settles_on")
    if t2.get("settlement_mode") == "settled" and t2.get("settles_on"):
        issues.append("T2 settled but settles_on set")
    # sessions_available vs bars
    sa = t2.get("sessions_available", 0)
    if sa > 5:
        issues.append(f"T2 sessions_available={sa} (>5, cap broken)")

    # ── Tile 3 / GEX ──────────────────────────────────────────
    status = d.get("gex_status")
    if status not in ("ok", "unavailable", "no_flip", None):
        issues.append(f"T3 gex_status unexpected: {status}")
    if status == "ok":
        for f in ("flip_dist_pct", "wall_up_dist_pct", "wall_dn_dist_pct"):
            v = d.get(f)
            if v is not None and abs(v) > 100:
                issues.append(f"T3 {f}={v} (>100% from spot, suspicious)")
        if d.get("gex_sign") not in ("POS", "NEG", None):
            issues.append(f"T3 gex_sign odd: {d.get('gex_sign')}")

    # ── gates ─────────────────────────────────────────────────
    gates = d.get("gates") or {}
    for g, v in gates.items():
        if v not in ("green", "yellow", "red", "gray", None):
            issues.append(f"gate {g}={v} (not a color)")

    # ── verdict ───────────────────────────────────────────────
    v = d.get("verdict")
    if v:
        if not v.get("action"):
            issues.append("verdict present but no action headline")

    # ── direction / spot ──────────────────────────────────────
    # spot=0 alone is a (usually replay-incomplete) data gap that degrades
    # gracefully — Tile 3 shows "unavailable". It's only a BUG if gex_status=="ok",
    # because then Tile 3 computes flip/wall = spot*(1+d%) = $0 strikes.
    if not d.get("spot") and status == "ok":
        issues.append("spot=0 WITH gex_status=ok -> Tile 3 would render $0 strikes")
    if d.get("direction") not in ("calls", "puts", None, ""):
        issues.append(f"direction odd: {d.get('direction')}")
    return issues


def main():
    snap = get("/snapshot.json")
    tickers = [r["ticker"] for r in (snap.get("rows") or [])]
    print(f"auditing {len(tickers)} tickers: {tickers}\n")
    total = 0
    for t in tickers:
        iss = audit(t)
        flag = "OK" if not iss else f"{len(iss)} ISSUE(S)"
        print(f"-- {t}: {flag}")
        for i in iss:
            print(f"     * {i}")
        total += len(iss)
    print(f"\nTOTAL ISSUES: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
