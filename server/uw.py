"""Thin REST wrapper for Unusual Whales (Basic tier).

Endpoint paths verified against the OpenAPI spec at
https://api.unusualwhales.com/api/openapi (2026-05-24). All methods raise
UWError on non-2xx so per-ticker callers can isolate failures.

Tier (Basic): 120 req/min, 40k req/day, 30-day lookback, personal-use-only.
"""
from __future__ import annotations
import os
from typing import Any
import requests

from server import budget

BASE = "https://api.unusualwhales.com"
TIMEOUT_S = 5


class UWError(RuntimeError):
    """Raised when a UW API call fails (network, auth, or non-2xx)."""


def _get_key() -> str:
    key = os.environ.get("UW_API_KEY")
    if not key:
        raise UWError("UW_API_KEY not set (env var)")
    return key


_429_RETRY_DELAYS_S = (1, 2)  # exponential backoff: up to ~3s total (semaphore prevents bursts)


def _get(path: str, params: dict | None = None) -> Any:
    """GET wrapper with retry-on-429 (exponential backoff) so heavy concurrent
    historical-fetch bursts don't shed sample days when UW Basic's burst
    window is briefly saturated."""
    import time as _time
    url = f"{BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_get_key()}",
        "UW-CLIENT-API-ID": "100001",  # required per UW skill.md anti-hallucination protocol
    }
    last_status = None
    last_429_hint = ""
    for attempt, delay in enumerate((0,) + _429_RETRY_DELAYS_S):
        if delay:
            _time.sleep(delay)
        try:
            budget.record_call()  # count every HTTP attempt — retries consume quota too
            r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            raise UWError(f"network error calling {path}: {e}") from e
        if r.status_code == 429:
            last_status = 429
            # Capture the budget headers so an outage tells us per-minute vs
            # daily exhaustion (and how long to back off) instead of guessing.
            last_429_hint = _rate_limit_hint(r.headers)
            continue  # retry after next delay
        if not r.ok:
            raise UWError(f"{r.status_code} on {path}: {r.text[:200]}")
        return r.json()
    # All retries exhausted
    suffix = f" ({last_429_hint})" if last_429_hint else ""
    raise UWError(f"429 rate limited on {path} after {len(_429_RETRY_DELAYS_S)} retries{suffix}")


def _rate_limit_hint(headers) -> str:
    """Summarize whatever rate-limit headers UW returned on a 429 (Retry-After,
    X-RateLimit-Remaining/Limit/Reset, RateLimit-*). Empty string if none — UW
    Basic doesn't document these, so this is best-effort diagnostics."""
    keys = ("Retry-After", "X-RateLimit-Remaining", "X-RateLimit-Limit",
            "X-RateLimit-Reset", "RateLimit-Remaining", "RateLimit-Limit",
            "RateLimit-Reset")
    parts = [f"{k}={headers[k]}" for k in keys if headers.get(k) is not None]
    return "; ".join(parts)


# ---------- Endpoint methods ----------

def fetch_spot_exposures_strike(ticker: str, date: str | None = None,
                                min_strike: float | None = None,
                                max_strike: float | None = None) -> dict:
    """Per-strike Spot GEX — dealer-positioning gamma per strike. Drives
    pinning + gamma squeeze detection. Returns directional fields
    (call_gamma_oi, put_gamma_oi, *_ask, *_bid).

    `date` (ISO YYYY-MM-DD): historical snapshot if provided; latest otherwise.
    `min_strike`/`max_strike`: REQUIRED in practice — without them UW returns the
    lowest strikes in the chain (far below spot for high-priced names), which
    breaks the gamma flip and wall computation. Pass a window around spot.

    `limit` is sent at the max (500): UW's effective default returns only ~50
    strikes (the lowest in the chain / window), so without it even a min/max
    window comes back as the lowest 50 — still below spot for wide chains."""
    params: dict = {"limit": 500}
    if date:
        params["date"] = date
    if min_strike is not None:
        params["min_strike"] = min_strike
    if max_strike is not None:
        params["max_strike"] = max_strike
    return _get(f"/api/stock/{ticker}/spot-exposures/strike", params=params)


def fetch_oi_strike(ticker: str, date: str | None = None) -> dict:
    """Per-strike open interest, calls + puts split."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/oi-per-strike", params=params)


def fetch_flow_alerts(ticker: str | None = None, limit: int = 50) -> dict:
    """Recent flow alerts. With `ticker`: filtered to that ticker. Without:
    cross-ticker hot today. Per-ticker /api/stock/{t}/flow-alerts is
    DEPRECATED; this single endpoint serves both uses."""
    params: dict = {"limit": limit}
    if ticker:
        params["ticker_symbol"] = ticker
    return _get("/api/option-trades/flow-alerts", params=params)


def fetch_greek_exposure_expiry(ticker: str, date: str | None = None) -> dict:
    """Per-EXPIRY aggregated greek exposure: call/put × charm/delta/gex/vanna +
    dte + expiry. Drives Tile 3 Phase 2's per-expiry regime headline + the
    charm/vanna drift gauges, and supplies the available-expiry list."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/greek-exposure/expiry", params=params)


def fetch_spot_exposures_expiry_strike(ticker: str, expirations: list[str],
                                       min_strike: float | None = None,
                                       max_strike: float | None = None,
                                       date: str | None = None) -> dict:
    """Per-strike spot/greek exposure for SPECIFIC expiries (each greek as
    _oi/_vol/_ask/_bid). `expirations` is REQUIRED. Like spot-exposures/strike,
    pass a near-spot min/max window + limit or you get the lowest strikes."""
    params: dict = {"expirations[]": expirations, "limit": 500}
    if min_strike is not None:
        params["min_strike"] = min_strike
    if max_strike is not None:
        params["max_strike"] = max_strike
    if date:
        params["date"] = date
    return _get(f"/api/stock/{ticker}/spot-exposures/expiry-strike", params=params)


def fetch_volatility(ticker: str, date: str | None = None) -> dict:
    """IV term structure for the ticker (avg ATM call+put IV per expiry)."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/volatility/term-structure", params=params)


def fetch_max_pain(ticker: str, date: str | None = None) -> dict:
    """Max pain across expirations for the ticker."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/max-pain", params=params)


def fetch_net_prem_ticks(ticker: str, date: str | None = None) -> dict:
    """Minute-by-minute net premium ticks for the ticker on a given day.
    Daily totals = the last tick (cumulative). Used for historical net-premium
    percentile context."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/net-prem-ticks", params=params)


def fetch_darkpool(ticker: str, limit: int = 50) -> dict:
    """Recent dark pool prints for the ticker. Used to corroborate options
    flow (alignment = stronger conviction signal)."""
    return _get(f"/api/darkpool/{ticker}", params={"limit": limit})


def fetch_earnings(ticker: str) -> dict:
    """Earnings history + upcoming date for the ticker. Used as context for
    vol-regime 'event_driven' detection."""
    return _get(f"/api/stock/{ticker}/earnings")


def fetch_interpolated_iv(ticker: str, date: str | None = None) -> dict:
    """Interpolated IV with percentile/rank data. The source for IV rank
    when it's needed in key_numbers (volatility/term-structure doesn't
    include IV rank)."""
    params = {"date": date} if date else None
    return _get(f"/api/stock/{ticker}/interpolated-iv", params=params)


def fetch_option_contracts(ticker: str, limit: int = 500) -> dict:
    """All option contracts for the ticker. Each row carries bid/ask/IV/
    volume/OI, with OCC-encoded option_symbol for parsing strike/expiry."""
    return _get(f"/api/stock/{ticker}/option-contracts", params={"limit": limit})


def fetch_market_tide(date: str | None = None) -> dict:
    """Aggregated cross-market net-flow tide. Returns time-series of net
    call/put premium across the entire market for context. Used as a
    fallback sector-tide when per-sector data isn't available."""
    params = {"date": date} if date else None
    return _get("/api/market/market-tide", params=params)


def fetch_sector_tide(sector: str, date: str | None = None) -> dict:
    """Per-sector net-flow tide. `sector` is UW's slug (e.g., 'technology',
    'healthcare', 'semiconductors'). Falls back to market_tide if 404."""
    params = {"date": date} if date else None
    return _get(f"/api/market/{sector}/sector-tide", params=params)


def fetch_news_headlines(ticker: str | None = None, limit: int = 10) -> dict:
    """Recent news headlines. With `ticker`: filtered. Without: cross-ticker
    most-recent. Used for Tile 5's news count + headline list."""
    params: dict = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return _get("/api/news/headlines", params=params)


def fetch_ticker_info(ticker: str) -> dict:
    """Basic ticker metadata including sector classification. Used to map
    ticker → sector slug for fetch_sector_tide."""
    return _get(f"/api/stock/{ticker}/info")


def fetch_group_flow(group: str = "sp500") -> dict:
    """Aggregate per-Greek flow for an index/sector group. `group` slugs
    verified via probe: sp500, airline, bank, basic-materials, …"""
    return _get(f"/api/group-flow/{group}/greek-flow")


def fetch_net_flow_expiry(ticker: str | None = None) -> dict:
    """Net premium flow grouped by expiry — front-week vs 30-45 DTE vs back
    months. Qualifies flow alerts as speculation vs institutional positioning."""
    params = {"ticker_symbol": ticker} if ticker else None
    return _get("/api/net-flow/expiry", params=params)


def fetch_lit_flow_recent() -> dict:
    """Recent exchange-displayed (lit) flow — complement to /api/darkpool/{t}.
    Cross-ticker, parallel to flow-alerts but distinguishes lit vs dark venue."""
    return _get("/api/lit-flow/recent")


def fetch_option_contract_history(symbol: str) -> dict:
    """Per-contract historical bid/ask/volume/IV — drilldown used by Tile 6's
    execution-quality strip once a contract is selected. `symbol` is the OCC
    standard (e.g. SPY260605C00750000)."""
    return _get(f"/api/option-contract/{symbol}/historic")


def fetch_seasonality_market() -> dict:
    """Market-level seasonality (calendar-weighted historical drift). Quasi-
    static — refresh once per day."""
    return _get("/api/seasonality/market")


def fetch_seasonality_ticker(ticker: str) -> dict:
    """Per-ticker seasonality. Quasi-static — refresh once per day."""
    return _get(f"/api/seasonality/{ticker}/monthly")


def fetch_ohlc(ticker: str, candle_size: str = "5m") -> dict:
    """OHLC candles — feeds Tile 1's underlying price line. `candle_size` per
    UW: 1m / 5m / 15m / 1h / 1d (1m+5m are intraday). Returns most-recent bars."""
    return _get(f"/api/stock/{ticker}/ohlc/{candle_size}")


# ---------- Shape normalizers ----------

def _unwrap(payload):
    """UW wraps most endpoint responses in {'data': [...]}. Normalize."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def gex_records(payload) -> list[dict]:
    """Return list of {strike: float, gamma: float, raw: dict} sorted by strike.

    UW's /spot-exposures/strike returns per-strike directional gamma. Per UW
    docs: net dealer gamma per strike = call_gamma_oi - put_gamma_oi (OI-based,
    standing positions). Falls back to summed directional fields, then to
    single-field aggregates.
    """
    rows = _unwrap(payload)
    out = []
    for r in rows:
        strike = r.get("strike") or r.get("strike_price")
        if strike is None:
            continue
        strike = float(strike)

        call_oi = r.get("call_gamma_oi")
        put_oi = r.get("put_gamma_oi")
        gamma = None
        if call_oi is not None or put_oi is not None:
            # UW pre-signs put_gamma_oi negative, so net dealer gamma is the
            # signed SUM, not a difference. Subtracting double-negated puts and
            # made every strike positive (broke the gamma-flip zero-crossing).
            gamma = float(call_oi or 0) + float(put_oi or 0)
        else:
            ca = r.get("call_gamma_ask"); cb = r.get("call_gamma_bid")
            pa = r.get("put_gamma_ask");  pb = r.get("put_gamma_bid")
            if any(v is not None for v in (ca, cb, pa, pb)):
                gamma = sum(float(v or 0) for v in (ca, cb, pa, pb))
            else:
                for k in ("gamma_dollars", "net_gamma", "gex", "gamma"):
                    if r.get(k) is not None:
                        gamma = float(r[k])
                        break
                if gamma is None:
                    cg = r.get("call_gamma")
                    pg = r.get("put_gamma")
                    if cg is not None or pg is not None:
                        # Same signed-sum convention as call_gamma_oi/put_gamma_oi.
                        gamma = float(cg or 0) + float(pg or 0)
        if gamma is None:
            continue
        out.append({"strike": strike, "gamma": gamma, "raw": r})
    return sorted(out, key=lambda x: x["strike"])


def oi_records(payload) -> list[dict]:
    """Return list of {strike, call_oi, put_oi} sorted by strike."""
    rows = _unwrap(payload)
    out = []
    for r in rows:
        strike = r.get("strike") or r.get("strike_price")
        if strike is None:
            continue
        call_oi = int(r.get("call_oi") or r.get("calls_oi") or r.get("call_open_interest") or 0)
        put_oi = int(r.get("put_oi") or r.get("puts_oi") or r.get("put_open_interest") or 0)
        out.append({"strike": float(strike), "call_oi": call_oi, "put_oi": put_oi})
    return sorted(out, key=lambda x: x["strike"])


def flow_records(payload) -> list[dict]:
    """Return list of {side, premium_usd, ts, raw} from flow-alerts payload.

    UW flow_alerts row shape (verified): type='call'|'put', total_premium=str,
    created_at=ISO timestamp. Other useful fields kept in 'raw' for downstream.
    """
    rows = _unwrap(payload)
    out = []
    for r in rows:
        side = (r.get("type") or r.get("option_type") or "").lower()
        side = "call" if side.startswith("c") else "put" if side.startswith("p") else side
        prem = r.get("total_premium") or r.get("premium") or r.get("premium_usd")
        ts = r.get("created_at") or r.get("executed_at") or r.get("timestamp") or r.get("ts")
        out.append({"side": side, "premium_usd": float(prem or 0), "ts": ts, "raw": r})
    return out


def hot_tickers(payload, limit: int = 15) -> list[str]:
    """Return list of unique tickers from a flow-alerts payload."""
    rows = _unwrap(payload)
    seen: list[str] = []
    for r in rows:
        t = r.get("ticker") or r.get("ticker_symbol") or r.get("symbol") or r.get("underlying")
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    return seen


def term_structure(payload) -> list[dict]:
    """Return list of {dte: int, iv: float} sorted by dte.

    UW volatility/term-structure row shape: {dte, volatility, expiry, ...}.
    The 'volatility' field name is what UW uses; we normalize it to 'iv'.
    """
    p = _unwrap(payload)
    if isinstance(p, list):
        rows = p
    elif isinstance(p, dict):
        rows = p.get("term_structure") or p.get("expiries") or p.get("series") or []
    else:
        rows = []
    out = []
    for r in rows:
        dte = r.get("dte") or r.get("days_to_expiry")
        iv = r.get("volatility") or r.get("iv") or r.get("atm_iv") or r.get("implied_volatility")
        if dte is None or iv is None:
            continue
        out.append({"dte": int(dte), "iv": float(iv)})
    return sorted(out, key=lambda x: x["dte"])


# ---------- Scalar extractors ----------

def extract_spot(*payloads) -> float | None:
    """Find current/last spot price from any of the given payloads.

    Order of preference per payload: dict roots (spot/underlying_price/...),
    then list[0] row. Pass any combination of flow_alerts, max_pain, etc. —
    we'll find the first usable value.
    """
    for payload in payloads:
        if payload is None:
            continue
        p = _unwrap(payload)
        if isinstance(p, dict):
            for k in ("spot", "underlying_price", "last_price", "price", "close"):
                if p.get(k) is not None:
                    return float(p[k])
        if isinstance(p, list) and p:
            row = p[0] if isinstance(p[0], dict) else None
            if row:
                for k in ("underlying_price", "close", "spot", "last_price"):
                    if row.get(k) is not None:
                        return float(row[k])
    return None


def extract_iv_rank(volatility_payload, interpolated_iv_payload=None) -> float | None:
    """IV rank (0-100 scale). Prefers /interpolated-iv (has 'percentile' per
    DTE row); picks the front-week row (smallest 'days'). Falls back to root
    fields on the volatility payload."""
    if interpolated_iv_payload is not None:
        rows = _unwrap(interpolated_iv_payload)
        if isinstance(rows, list) and rows:
            try:
                front = min(rows, key=lambda r: r.get("days", 999))
                pct = front.get("percentile") or front.get("iv_rank") or front.get("rank")
                if pct is not None:
                    v = float(pct)
                    return v * 100 if v <= 1.0 else v
            except (ValueError, TypeError):
                pass
    p = _unwrap(volatility_payload)
    if isinstance(p, dict):
        for k in ("iv_rank", "ivr", "iv_percentile"):
            if p.get(k) is not None:
                v = float(p[k])
                return v * 100 if v <= 1.0 else v
    return None


def darkpool_records(payload) -> list[dict]:
    """Return list of {ts, price, size, premium, side, raw} from dark pool payload.

    Side inferred from price vs NBBO midpoint:
        price > midpoint → "buy" (lifting offer, buyer-initiated)
        price < midpoint → "sell" (hitting bid, seller-initiated)
        otherwise        → "neutral"
    Standard tick-rule classification.
    """
    rows = _unwrap(payload)
    out = []
    for r in rows:
        try:
            price = float(r.get("price") or 0)
        except (TypeError, ValueError):
            continue
        try:
            bid = float(r.get("nbbo_bid") or 0)
            ask = float(r.get("nbbo_ask") or 0)
        except (TypeError, ValueError):
            bid = ask = 0
        side = "neutral"
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            if price > mid:
                side = "buy"
            elif price < mid:
                side = "sell"
        out.append({
            "ts": r.get("executed_at"),
            "price": price,
            "size": int(r.get("size") or 0),
            "premium": float(r.get("premium") or 0),
            "side": side,
            "raw": r,
        })
    return out


def darkpool_net_premium(records: list[dict]) -> float:
    """Net dollar premium across dark pool prints: buys minus sells."""
    net = 0.0
    for r in records:
        if r["side"] == "buy":
            net += r["premium"]
        elif r["side"] == "sell":
            net -= r["premium"]
    return net


def next_earnings(payload) -> str | None:
    """Pull next upcoming earnings ISO date from the earnings payload, or
    None if there's nothing scheduled (e.g. ETF, or earnings already past)."""
    import datetime as _dt
    rows = _unwrap(payload)
    if not isinstance(rows, list) or not rows:
        return None
    today = _dt.date.today()
    candidates: list[tuple[_dt.date, str]] = []
    for r in rows:
        for k in ("expected_date", "report_date", "earnings_date", "date"):
            d = r.get(k)
            if not d:
                continue
            try:
                dt = _dt.date.fromisoformat(str(d)[:10])
                if dt >= today:
                    candidates.append((dt, str(d)))
            except ValueError:
                pass
            break
    if candidates:
        candidates.sort()
        return candidates[0][1]
    return None


# ---------- Option contracts (for the "contract picker" in the pinned card) ----------

import re as _re_options
_OPTION_SYMBOL_RE = _re_options.compile(
    r"^(?P<sym>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<type>[PC])(?P<strike>\d{8})$"
)


def parse_option_symbol(symbol: str) -> dict | None:
    """Parse an OCC-encoded symbol like 'SPY260522C00748000' into its parts.
    Returns {underlying, expiry (ISO), type ('call'|'put'), strike (float)} or None."""
    if not symbol:
        return None
    m = _OPTION_SYMBOL_RE.match(symbol)
    if not m:
        return None
    return {
        "underlying": m["sym"],
        "expiry": f"20{m['yy']}-{m['mm']}-{m['dd']}",
        "type": "call" if m["type"] == "C" else "put",
        "strike": int(m["strike"]) / 1000,
    }


def contract_records(payload) -> list[dict]:
    """Normalize option-contracts payload into {symbol, expiry, type, strike,
    bid, ask, iv, volume, oi} dicts."""
    rows = _unwrap(payload)
    out = []
    for r in rows:
        sym = r.get("option_symbol")
        parsed = parse_option_symbol(sym) if sym else None
        if not parsed:
            continue
        try:
            out.append({
                "symbol": sym,
                "expiry": parsed["expiry"],
                "type": parsed["type"],
                "strike": parsed["strike"],
                "bid": float(r.get("nbbo_bid") or 0),
                "ask": float(r.get("nbbo_ask") or 0),
                "iv": float(r.get("implied_volatility") or 0),
                "volume": int(r.get("volume") or 0),
                "oi": int(r.get("open_interest") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def contracts_near_focus(records: list[dict], focus_strike: float,
                         n_strikes: int = 5) -> list[dict]:
    """Filter records to the nearest-future expiry, then to the n_strikes
    immediately above and below `focus_strike` (both calls and puts).

    Returns sorted by (strike, type)."""
    import datetime as _dt
    today = _dt.date.today()
    future: list[tuple[_dt.date, dict]] = []
    for r in records:
        try:
            d = _dt.date.fromisoformat(r["expiry"])
            if d >= today:
                future.append((d, r))
        except (ValueError, KeyError):
            pass
    if not future:
        return []
    nearest = min(d for d, _ in future)
    front_week = [r for d, r in future if d == nearest]
    strikes = sorted(set(r["strike"] for r in front_week))
    if not strikes:
        return []
    closest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - focus_strike))
    start = max(0, closest_idx - n_strikes)
    end = min(len(strikes), closest_idx + n_strikes + 1)
    target = set(strikes[start:end])
    return sorted(
        [r for r in front_week if r["strike"] in target],
        key=lambda r: (r["strike"], r["type"]),
    )


def max_pain_value(payload) -> float | None:
    """Pull the FRONT-WEEK max-pain strike. UW returns a list of per-expiry
    rows sorted by expiry; index 0 is nearest expiry — the one that matters
    for weekly options."""
    p = _unwrap(payload)
    if isinstance(p, list) and p:
        first = p[0] if isinstance(p[0], dict) else None
        if first:
            mp = first.get("max_pain") or first.get("strike")
            if mp is not None:
                return float(mp)
    if isinstance(p, dict):
        for k in ("max_pain", "max_pain_strike", "strike"):
            if p.get(k) is not None:
                return float(p[k])
    return None
