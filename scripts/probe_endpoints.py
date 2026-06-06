"""End-to-end UW data-health probe. Hits every endpoint the dashboard depends on
with a real ticker and reports per-endpoint: HTTP outcome, row count, whether the
KEYS THE PARSERS READ are present, and VALUE-SANITY invariants (the layer that
catches semantic/sign bugs a shape check misses — e.g. an index that isn't
put-skewed, IV out of range, a 'fetched but always empty' endpoint).

Why this exists: UW failures degrade *silently* (404 / empty → 'unavailable' →
fallback), so a broken pull looks identical to 'no data right now'. This makes it
loud. Run on demand (esp. after any UW-touching change) with the key injected:

    railway run python scripts/probe_endpoints.py            # uses Railway's UW_API_KEY
    # or locally:  UW_API_KEY=... python scripts/probe_endpoints.py [TICKER]

Exit code is non-zero if any endpoint ERRORed (404/exception) or any sanity
invariant failed — so it can gate automation.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root on path

# Probe calls UW directly (raw), bypassing the cache, so it tests the real network
# path + current payload shapes — not whatever happens to be archived.
os.environ.setdefault("DATA_DIR", "./_probe_data")
from server import uw  # noqa: E402

TICKER = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()


def _rows(payload):
    if isinstance(payload, dict):
        d = payload.get("data", payload)
        return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
    return payload if isinstance(payload, list) else []


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _near_expiries():
    """A near-term and a ~30d expiry that actually exist for TICKER, from
    greek-exposure-expiry; falls back to computed Fridays."""
    try:
        rows = _rows(uw.fetch_greek_exposure_expiry(TICKER))
        exps = sorted({r.get("expiry") for r in rows if r.get("expiry")})
        if exps:
            near = exps[0]
            d0 = datetime.now(tz=timezone.utc).date()
            far = next((e for e in exps
                        if (datetime.fromisoformat(e).date() - d0).days >= 25), exps[-1])
            return near, far
    except Exception:
        pass
    today = datetime.now(tz=timezone.utc).date()
    nf = today + timedelta(days=(4 - today.weekday()) % 7)
    return nf.isoformat(), (nf + timedelta(days=28)).isoformat()


# ---- value-sanity invariants (return a warning string, or None if OK) ----
def _sane_flow(rows):
    if not rows:
        return None
    opening = sum(1 for r in rows if r.get("all_opening_trades"))
    if opening == 0:
        return "all_opening_trades=False for ALL rows (opening proxy dead — direction uses volume_oi_ratio)"
    return None


def _sane_realized_vol(rows):
    if not rows:
        return None
    iv = _f(rows[-1].get("implied_volatility"))
    if iv is None or not (0.03 <= iv <= 3.0):
        return f"latest implied_volatility out of sane range: {rows[-1].get('implied_volatility')}"
    settled = [r for r in rows if _f(r.get("realized_volatility")) is not None]
    if not settled:
        return "no settled realized_volatility in the series"
    return None


def _sane_rr(rows):
    if not rows:
        return None
    rr = _f(rows[-1].get("risk_reversal"))
    if rr is None:
        return "latest risk_reversal missing/non-numeric"
    # vendor risk_reversal = put_IV - call_IV (positive = put-skew). An index (SPY/
    # QQQ/IWM) is structurally put-skewed → expect positive. Flag the sign-flip risk.
    if TICKER in ("SPY", "QQQ", "IWM", "DIA") and rr <= 0:
        return f"index {TICKER} risk_reversal={rr} not put-skewed (sign convention may have changed!)"
    return None


def _sane_interp_iv(rows):
    if not rows:
        return None
    if _f(rows[0].get("volatility")) is None:
        return "no 'volatility' key (parser reads 'volatility'/'percentile')"
    return None


def _sane_greeks(rows):
    if not rows:
        return None
    need = ("call_delta", "put_delta", "call_volatility", "put_volatility")
    miss = [k for k in need if rows[0].get(k) is None]
    return f"greeks missing parser keys: {miss}" if miss else None


near, far = _near_expiries()

# (label, callable, keys-the-parser-reads, sanity_fn)
PROBES = [
    ("flow_alerts",                 lambda: uw.fetch_flow_alerts(100),
     ["ticker", "type", "total_premium", "all_opening_trades", "volume_oi_ratio"], _sane_flow),
    ("stock_state",                 lambda: uw.fetch_stock_state(TICKER), [], None),
    ("spot_exposures_strike",       lambda: uw.fetch_spot_exposures_strike(TICKER), ["strike"], None),
    ("oi_per_strike",               lambda: uw.fetch_oi_strike(TICKER), ["strike", "call_oi", "put_oi"], None),
    ("volatility_term_structure",   lambda: uw.fetch_volatility(TICKER), ["expiry", "volatility"], None),
    ("interpolated_iv",             lambda: uw.fetch_interpolated_iv(TICKER),
     ["days", "volatility", "percentile"], _sane_interp_iv),
    ("realized_vol",                lambda: uw.fetch_realized_vol(TICKER),
     ["implied_volatility", "realized_volatility"], _sane_realized_vol),
    ("darkpool",                    lambda: uw.fetch_darkpool(TICKER, 50), [], None),
    ("earnings",                    lambda: uw.fetch_earnings(TICKER), [], None),
    ("ticker_info",                 lambda: uw.fetch_ticker_info(TICKER), [], None),
    ("news_headlines",              lambda: uw.fetch_news_headlines(TICKER, 10), [], None),
    ("ohlc_5m",                     lambda: uw.fetch_ohlc(TICKER, "5m"), [], None),
    ("greek_exposure_expiry",       lambda: uw.fetch_greek_exposure_expiry(TICKER), ["expiry", "dte"], None),
    ("spot_exposures_expiry_strike", lambda: uw.fetch_spot_exposures_expiry_strike(TICKER, [near], None, None),
     ["strike"], None),
    ("greeks(~30d)",                lambda: uw.fetch_greeks(TICKER, far),
     ["call_delta", "put_delta", "call_volatility", "put_volatility"], _sane_greeks),
    ("atm_chains",                  lambda: uw.fetch_atm_chains(TICKER, [near]), [], None),
    ("risk_reversal_skew(~30d)",    lambda: uw.fetch_risk_reversal_skew(TICKER, far, 25),
     ["date", "risk_reversal"], _sane_rr),
    ("option_contracts",            lambda: uw.fetch_option_contracts(TICKER, 500), [], None),
    ("economic_calendar",           lambda: uw.fetch_economic_calendar(), ["event", "time", "type"], None),
    ("market_tide",                 lambda: uw.fetch_market_tide(), [], None),
]

print(f"=== UW data-health probe — ticker={TICKER}  expiries: near={near} ~30d={far} ===")
errors = warns = 0
for label, call, keys, sanity in PROBES:
    try:
        payload = call()
        rows = _rows(payload)
        if not rows:
            print(f"  EMPTY  {label:32} (200 but no rows)")
            continue
        missing = [k for k in keys if rows[0].get(k) is None]
        warn = sanity(rows) if sanity else None
        tag = "OK   "
        notes = []
        if missing:
            notes.append(f"MISSING KEYS {missing}")
            warns += 1
            tag = "WARN "
        if warn:
            notes.append(f"SANITY: {warn}")
            warns += 1
            tag = "WARN "
        print(f"  {tag}{label:32} n={len(rows):<4} {'· '.join(notes)}")
    except Exception as e:
        errors += 1
        print(f"  ERROR  {label:32} {str(e)[:110]}")

print(f"=== done: {errors} error(s), {warns} warning(s) ===")
sys.exit(1 if (errors or warns) else 0)
