"""Storage layer: read-through cache in front of UW + parquet archive writer.

Layout on disk:
  $DATA_DIR/
    raw/
      endpoint=<name>/
        dt=YYYY-MM-DD/
          ticker=<TICKER>/        ← absent for cross-ticker endpoints
            part-HHMM.parquet     ← one file per hour, append-mode
    snapshots.jsonl
    sticky.json

Per-call write is best-effort: any I/O failure is logged and swallowed so the
dashboard continues to render — losing archive coverage is preferable to
crashing the snapshot pipeline.
"""
from __future__ import annotations
import contextlib
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from server import budget, freshness, uw
from server.cache import TTLCache

log = logging.getLogger(__name__)

# Load-bearing rule: live UW ingestion happens ONLY in the background snapshot
# loop; the production REQUEST PATH serves cached/parquet data only. Request
# handlers wrap their work in `with storage.cached_only():` so a cache miss
# returns a UWFailure instead of firing a synchronous UW call mid-request.
_CACHED_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("uw_cached_only", default=False)


@contextlib.contextmanager
def cached_only():
    """Within this block, `_through` never calls UW — a cache/parquet miss yields
    a UWFailure('cached-only'). Use on the request path; the loop omits it."""
    token = _CACHED_ONLY.set(True)
    try:
        yield
    finally:
        _CACHED_ONLY.reset(token)


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _partition_path(endpoint: str, ticker: str | None, fetched_at: datetime) -> Path:
    """Build the parquet partition path for this (endpoint, ticker, hour)."""
    dt = fetched_at.strftime("%Y-%m-%d")
    hhmm = fetched_at.strftime("%H") + "00"  # bucket to the hour
    parts = [_data_dir(), "raw", f"endpoint={endpoint}", f"dt={dt}"]
    if ticker:
        parts.append(f"ticker={ticker}")
    parts.append(f"part-{hhmm}.parquet")
    return Path(*parts)


def _record_schema() -> pa.Schema:
    # ticker is NOT written as a column — it is encoded in the partition path
    # (ticker=<TICKER>/) so pyarrow's Hive-partition inference doesn't conflict.
    return pa.schema([
        ("fetched_at", pa.timestamp("us", tz="UTC")),
        ("params_json", pa.string()),
        ("status_code", pa.int32()),
        ("latency_ms", pa.int32()),
        ("response", pa.string()),  # JSON-serialized; schema-on-read via duckdb
    ])


def write_response(
    endpoint: str,
    ticker: str | None,
    params: dict | None,
    response: Any,
    status_code: int,
    latency_ms: int,
    fetched_at: datetime,
) -> bool:
    """Append one row to the appropriate parquet partition.

    Returns True on success, False on any I/O failure (logged, not raised).
    """
    try:
        path = _partition_path(endpoint, ticker, fetched_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "fetched_at": [fetched_at],
            # ticker omitted: encoded in the partition path (ticker=<TICKER>/)
            "params_json": [json.dumps(params or {}, sort_keys=True)],
            "status_code": [status_code],
            "latency_ms": [latency_ms],
            "response": [json.dumps(response, default=str)],
        }
        new_table = pa.table(row, schema=_record_schema())
        if path.exists():
            existing = pq.ParquetFile(path).read()
            new_table = pa.concat_tables([existing, new_table])
        pq.write_table(new_table, path, compression="zstd", use_dictionary=False)
        return True
    except Exception as e:
        log.error("storage write failed: endpoint=%s ticker=%s err=%s", endpoint, ticker, e)
        return False


# ── Module-level singleton — process-local. One per server. ───────────────────
_cache = TTLCache()


# TTL tiers — extended now that parquet persists across restarts.
# A cold container reads fresh parquet rows instead of re-hitting UW.
_TTL_HOT_SECONDS = 60          # spot/OI/darkpool — dashboard freshness
_TTL_SLEEPER_SECONDS = 300     # 5min — non-hot tracked tickers
_TTL_QUASI_STATIC_SECONDS = 86400  # 24h — earnings, ticker_info, max_pain, prev-day OI
_TTL_MEDIUM_SECONDS = 300      # 5min — volatility, IV rank, sector tide, market tide, option_contracts, net_prem_ticks
_TTL_NEWS_SECONDS = 900        # 15min — news headlines

_QUASI_STATIC_ENDPOINTS = {
    "earnings", "ticker_info", "max_pain",
    "seasonality_market", "seasonality_ticker",
    "fda_calendar", "economic_calendar",
}
_MEDIUM_ENDPOINTS = {
    "volatility_term_structure", "interpolated_iv",
    "sector_tide", "market_tide", "option_contracts", "net_prem_ticks",
    "net_flow_expiry", "option_contract_history",
    # Tile 4
    "greeks", "atm_chains", "risk_reversal_skew", "realized_vol",
    # Tile 3-detail (loop-prewarmed) — 5-min TTL so the per-cycle loop reuses the
    # cache instead of re-fetching these heavy endpoints every 120s. The cost
    # lever for the added budget is TTL, not ticker count.
    "greek_exposure_expiry", "spot_exposures_expiry_strike",
}
_NEWS_ENDPOINTS = {"news_headlines"}
# group_flow + lit_flow_recent fall through to the hot/sleeper default (is_hot
# arg controls TTL); both wrappers above pass is_hot=True → 60s.

# UW Basic tier = 120 req/min, 40k req/day. With ThreadPoolExecutor at 8 workers
# and no throttle, up to 8 calls run in parallel → bursts far past any budget.
# This semaphore bounds CONCURRENCY to 1 — it does NOT enforce a per-minute rate:
# with sub-360ms responses a single serial caller still does >2.7/sec (~166/min
# was observed during the 2026-05-30 backfill, with no 429s). The binding
# constraint behind the 2026-05-29 outage was the DAILY cap, addressed by the
# per-cycle call-volume cuts + budget.over_soft_budget guard, not by this gate.
# If per-minute 429s ever appear (watch /health calls_1m + Retry-After logs), the
# correct fix is a token-bucket rate limiter here/in uw._get, not a smaller value.
import threading
_UW_CONCURRENCY_LIMIT = 1
_uw_call_gate = threading.BoundedSemaphore(_UW_CONCURRENCY_LIMIT)


def _ttl_seconds(endpoint: str, is_hot: bool) -> int:
    """Per-endpoint TTL. Some endpoints change rarely (24h: earnings, ticker
    sector); others are dashboard-critical and stay short (60s). News + sector
    tide are in the middle (5-15min)."""
    if endpoint in _QUASI_STATIC_ENDPOINTS:
        return _TTL_QUASI_STATIC_SECONDS
    if endpoint in _NEWS_ENDPOINTS:
        return _TTL_NEWS_SECONDS
    if endpoint in _MEDIUM_ENDPOINTS:
        return _TTL_MEDIUM_SECONDS
    return _TTL_HOT_SECONDS if is_hot else _TTL_SLEEPER_SECONDS


@dataclass
class UWFailure:
    """Sentinel returned by storage.fetch_* when the upstream UW call raised.
    Callers (snapshot pipeline) detect this and append to row._failures[]."""
    endpoint: str
    ticker: str | None
    message: str


def _make_key(endpoint: str, ticker: str | None, params: dict | None) -> tuple:
    return (endpoint, ticker, json.dumps(params or {}, sort_keys=True))


def _read_latest_from_parquet(
    endpoint: str, ticker: str | None, params: dict | None, max_age_seconds: float
) -> tuple[Any, Any]:
    """Look for the most recent matching row in the parquet archive whose
    fetched_at is within `max_age_seconds` of now. Returns a
    (response, fetched_at) tuple if a fresh row exists, else (None, None).

    Scans today's hour-partition files newest-first; also peeks at the most
    recent file from yesterday's partition if today's holds nothing fresh
    (covers the midnight rollover case for long-TTL endpoints like earnings).
    """
    now = datetime.now(tz=timezone.utc)
    # max_age_seconds=None → no freshness limit (replay/cached-only reads any
    # archived row regardless of age).
    cutoff = (datetime(1970, 1, 1, tzinfo=timezone.utc) if max_age_seconds is None
              else now - timedelta(seconds=max_age_seconds))
    params_key = json.dumps(params or {}, sort_keys=True)

    def _candidate_dirs() -> list[Path]:
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        dirs: list[Path] = []
        for dt in (today, yesterday):
            parts = [_data_dir(), "raw", f"endpoint={endpoint}", f"dt={dt}"]
            if ticker:
                parts.append(f"ticker={ticker}")
            d = Path(*parts)
            if d.is_dir():
                dirs.append(d)
        return dirs

    for d in _candidate_dirs():
        # Newest hour-file first (lexicographic sort on "part-HHMM.parquet"
        # matches chronological order within a day, so reverse=True puts the
        # most recent hour first).
        files = sorted(d.glob("part-*.parquet"), reverse=True)
        for path in files[:2]:  # current + previous hour cover all per-hour TTLs
            try:
                table = pq.read_table(path)
                if table.num_rows == 0:
                    continue
                mask_params = pc.equal(table["params_json"], params_key)
                # fetched_at is timestamp("us", tz="UTC"); compare against cutoff.
                cutoff_pa = pa.scalar(cutoff, type=pa.timestamp("us", tz="UTC"))
                mask_time = pc.greater_equal(table["fetched_at"], cutoff_pa)
                filtered = table.filter(pc.and_(mask_params, mask_time))
                if filtered.num_rows == 0:
                    continue
                # Take the latest row by fetched_at
                latest_idx = pc.sort_indices(
                    filtered.select(["fetched_at"]),
                    sort_keys=[("fetched_at", "descending")],
                )[0].as_py()
                response_str = filtered["response"][latest_idx].as_py()
                fetched_at = filtered["fetched_at"][latest_idx].as_py()
                # pyarrow returns a tz-aware datetime for timestamp("us", tz="UTC")
                return json.loads(response_str), fetched_at
            except Exception as e:
                log.warning("parquet read failed: %s/%s @ %s: %s",
                            endpoint, ticker, path.name, e)
                continue
    return None, None


def read_oi_history(ticker: str, n_sessions: int = 5) -> list[dict]:
    """Read up to `n_sessions` of daily OI-per-strike snapshots for `ticker`
    from our own parquet archive (endpoint=oi_per_strike). Tier-independent —
    sources history from the days we've already archived, not from UW ?date=
    lookups (which the Basic tier may not honor).

    Returns oldest→newest: [{"date": "YYYY-MM-DD",
                             "strikes": {strike_float: total_oi_int, ...}}].
    Each session uses the latest hour-file's latest no-date-param row for that
    day (the live "today" snapshot captured during that session)."""
    base = _data_dir() / "raw" / "endpoint=oi_per_strike"
    if not base.is_dir():
        return []
    # dt= dirs, newest first
    dt_dirs = sorted(
        [d for d in base.glob("dt=*") if (d / f"ticker={ticker}").is_dir()],
        key=lambda d: d.name, reverse=True,
    )[:n_sessions]
    sessions: list[dict] = []
    for d in dt_dirs:
        date_str = d.name.replace("dt=", "")
        tdir = d / f"ticker={ticker}"
        files = sorted(tdir.glob("part-*.parquet"), reverse=True)
        strikes: dict[float, int] = {}
        for path in files:
            try:
                table = pq.read_table(path)
                if table.num_rows == 0:
                    continue
                # Prefer the no-date-param row (the live today snapshot).
                mask = pc.equal(table["params_json"], "{}")
                filtered = table.filter(mask)
                src = filtered if filtered.num_rows else table
                latest_idx = pc.sort_indices(
                    src.select(["fetched_at"]),
                    sort_keys=[("fetched_at", "descending")],
                )[0].as_py()
                payload = json.loads(src["response"][latest_idx].as_py())
                rows = payload.get("data") if isinstance(payload, dict) else payload
                for r in (rows or []):
                    try:
                        k = float(r.get("strike") or 0)
                        oi = int(r.get("call_oi") or 0) + int(r.get("put_oi") or 0)
                        if k > 0:
                            strikes[k] = oi
                    except (TypeError, ValueError):
                        continue
                if strikes:
                    break  # got this day's snapshot from the newest file
            except Exception as e:
                log.warning("oi-history read failed %s @ %s: %s", ticker, path.name, e)
                continue
        if strikes:
            sessions.append({"date": date_str, "strikes": strikes})
    sessions.reverse()  # oldest → newest
    return sessions


def _through(endpoint: str, ticker: str | None, params: dict | None, is_hot: bool,
             uw_call):
    """Read-through cache with parquet persistence:
      1. RAM cache hit               → return cached
      2. parquet hit within TTL      → hydrate RAM cache, return parquet row
      3. miss everywhere             → call UW
         a. success → write parquet, cache, return response
         b. UWError → return UWFailure (not cached)

    The parquet check makes the cache persistent across container restarts and
    deduplicates UW calls across processes/redeploys. Without it, every cold
    start hammers UW for the full snapshot universe.
    """
    key = _make_key(endpoint, ticker, params)
    ttl = _ttl_seconds(endpoint, is_hot)

    # 1. RAM cache (fastest path)
    cached, cached_observed_at = _cache.get(key)
    if cached is not None:
        freshness.record(endpoint, cached_observed_at, "cache")
        return cached

    # 2. Parquet cache (persistent, survives restarts). In cached-only (replay)
    # mode we ignore the TTL — replay reads captured archives that are hours/days
    # old, and freshness is irrelevant when there's no live UW to fall back to.
    replay = _CACHED_ONLY.get()
    max_age = None if replay else ttl
    parquet_hit, parquet_fetched_at = _read_latest_from_parquet(
        endpoint, ticker, params, max_age_seconds=max_age)
    if parquet_hit is not None:
        _cache.set(key, parquet_hit, ttl_seconds=ttl, observed_at=parquet_fetched_at)
        # A within-TTL hit is "cache"; an aged hit served only because we're in
        # replay/cached-only is "archive" (honestly old).
        freshness.record(endpoint, parquet_fetched_at, "archive" if replay else "cache")
        return parquet_hit

    # 2a. Cached-only mode (request path): never call UW mid-request. A miss here
    # means the loop hasn't warmed this yet → caller renders warming/unavailable.
    if _CACHED_ONLY.get():
        return UWFailure(endpoint=endpoint, ticker=ticker,
                         message="cached-only: not yet warmed by the snapshot loop")

    # 2b. Soft budget guard: once today's UW calls cross the soft cap, shed every
    # NON-critical endpoint here — before hitting UW — so we glide to a stop on
    # last-good data instead of slamming a 429 wall. flow_alerts is whitelisted:
    # it's the core read (hot list / health), so it survives longest.
    if endpoint != "flow_alerts" and budget.over_soft_budget():
        return UWFailure(endpoint=endpoint, ticker=ticker,
                         message="budget guard: daily soft cap reached")

    # 3. UW (only when nothing fresh exists)
    started_at = time.monotonic()
    try:
        with _uw_call_gate:
            response = uw_call()
    except uw.UWError as e:
        return UWFailure(endpoint=endpoint, ticker=ticker, message=str(e))
    latency_ms = int((time.monotonic() - started_at) * 1000)
    now = datetime.now(tz=timezone.utc)
    write_response(
        endpoint=endpoint, ticker=ticker, params=params, response=response,
        status_code=200, latency_ms=latency_ms, fetched_at=now,
    )
    _cache.set(key, response, ttl_seconds=ttl, observed_at=now)
    freshness.record(endpoint, now, "live")
    return response


# ── Public per-endpoint fetch wrappers ────────────────────────────────────────
# Mirror the names in server/uw.py so the snapshot pipeline can swap
# `uw.fetch_*` for `storage.fetch_*` mechanically.

def fetch_spot_exposures_strike(ticker: str, is_hot: bool = False,
                                min_strike: float | None = None,
                                max_strike: float | None = None):
    """Per-strike spot GEX. min/max_strike bracket the near-spot window — without
    them UW returns the lowest strikes in the chain, far below spot."""
    params: dict = {}
    if min_strike is not None:
        params["min_strike"] = min_strike
    if max_strike is not None:
        params["max_strike"] = max_strike
    return _through("spot_exposures_strike", ticker, params or None, is_hot,
                    lambda: uw.fetch_spot_exposures_strike(
                        ticker, min_strike=min_strike, max_strike=max_strike))


def fetch_oi_strike(ticker: str, is_hot: bool = False, date: str | None = None):
    """Fetch OI per strike. `date` is ISO YYYY-MM-DD (None = today's snapshot).
    Cache key includes the date so today's and yesterday's snapshots don't collide."""
    params = {"date": date} if date else None
    return _through("oi_per_strike", ticker, params, is_hot,
                    lambda: uw.fetch_oi_strike(ticker, date=date))


def fetch_greeks(ticker: str, expiry: str):
    """Per-contract greeks for an expiry (Tile 4)."""
    return _through("greeks", ticker, {"expiry": expiry}, False,
                    lambda: uw.fetch_greeks(ticker, expiry))


def fetch_atm_chains(ticker: str, expirations: list[str]):
    """ATM straddle / expected move per expiry (Tile 4)."""
    return _through("atm_chains", ticker, {"expirations": list(expirations)}, False,
                    lambda: uw.fetch_atm_chains(ticker, expirations))


def fetch_risk_reversal_skew(ticker: str, expiry: str, delta: int = 25):
    """25-delta risk-reversal skew for an expiry (Tile 4 Execution)."""
    return _through("risk_reversal_skew", ticker, {"expiry": expiry, "delta": delta}, False,
                    lambda: uw.fetch_risk_reversal_skew(ticker, expiry, delta))


def fetch_realized_vol(ticker: str):
    """Realized volatility (Tile 4 RV-vs-IV context)."""
    return _through("realized_vol", ticker, None, False,
                    lambda: uw.fetch_realized_vol(ticker))


def fetch_stock_state(ticker: str):
    """Last stock price/volume (Tile 4 spot reference). Hot TTL."""
    return _through("stock_state", ticker, None, True,
                    lambda: uw.fetch_stock_state(ticker))


def fetch_fda_calendar(ticker: str):
    """FDA calendar for a ticker (Tile 4 event gate). Quasi-static."""
    return _through("fda_calendar", ticker, {"ticker": ticker}, False,
                    lambda: uw.fetch_fda_calendar(ticker))


def fetch_greek_exposure_expiry(ticker: str, is_hot: bool = True):
    """Per-expiry greek exposure (charm/vanna/gex + dte). Hot TTL — drives the
    on-demand Tile 3 detail; fresh per ticker-select."""
    return _through("greek_exposure_expiry", ticker, None, is_hot,
                    lambda: uw.fetch_greek_exposure_expiry(ticker))


def fetch_spot_exposures_expiry_strike(ticker: str, expirations: list[str],
                                       min_strike: float | None = None,
                                       max_strike: float | None = None,
                                       is_hot: bool = True):
    """Per-strike spot exposures for specific expiries (OI/Vol toggle source).
    expirations + near-spot window go into the cache key + the UW call."""
    params: dict = {"expirations": list(expirations)}
    if min_strike is not None:
        params["min_strike"] = min_strike
    if max_strike is not None:
        params["max_strike"] = max_strike
    return _through("spot_exposures_expiry_strike", ticker, params, is_hot,
                    lambda: uw.fetch_spot_exposures_expiry_strike(
                        ticker, expirations, min_strike=min_strike, max_strike=max_strike))


def fetch_volatility(ticker: str, is_hot: bool = False):
    return _through("volatility_term_structure", ticker, None, is_hot,
                    lambda: uw.fetch_volatility(ticker))


def fetch_interpolated_iv(ticker: str, is_hot: bool = False):
    return _through("interpolated_iv", ticker, None, is_hot,
                    lambda: uw.fetch_interpolated_iv(ticker))


def fetch_darkpool(ticker: str, is_hot: bool = False):
    return _through("darkpool", ticker, None, is_hot,
                    lambda: uw.fetch_darkpool(ticker))


def fetch_earnings(ticker: str, is_hot: bool = False):
    return _through("earnings", ticker, None, is_hot,
                    lambda: uw.fetch_earnings(ticker))


def fetch_flow_alerts(limit: int = 100, ticker: str | None = None):
    # Cross-ticker endpoint; treated as "hot" (60s TTL) since hot-list computation
    # is on the critical path every snapshot. With `ticker`, filtered to that
    # symbol — used by the on-demand search-any-ticker lookup.
    params = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return _through("flow_alerts", ticker, params, True,
                    lambda: uw.fetch_flow_alerts(ticker=ticker, limit=limit))


def fetch_market_tide():
    """Cross-market net-flow tide. Cached at sleeper TTL (5min) — market-level
    flow doesn't change minute-by-minute."""
    return _through("market_tide", None, None, False,
                    lambda: uw.fetch_market_tide())


def fetch_sector_tide(sector: str):
    """Per-sector tide. Sleeper TTL (5min)."""
    return _through("sector_tide", sector, None, False,
                    lambda: uw.fetch_sector_tide(sector))


def fetch_news_headlines(ticker: str | None = None, limit: int = 10):
    """News headlines, optionally per-ticker. Sleeper TTL (5min)."""
    params = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    return _through("news_headlines", ticker, params, False,
                    lambda: uw.fetch_news_headlines(ticker=ticker, limit=limit))


def fetch_ticker_info(ticker: str):
    """Ticker metadata (sector etc). Quasi-static — 24h TTL."""
    return _through("ticker_info", ticker, None, False,
                    lambda: uw.fetch_ticker_info(ticker))


def fetch_option_contracts(ticker: str, limit: int = 500):
    """All option contracts for ticker (bid/ask/IV/vol/OI). Replaces Tile 6
    synthetic chain. 5-min TTL — chain prices move intraday but not by the second."""
    return _through("option_contracts", ticker, {"limit": limit}, False,
                    lambda: uw.fetch_option_contracts(ticker, limit=limit))


def fetch_max_pain(ticker: str, date: str | None = None):
    """Max-pain strike per expiry. 24h TTL — moves slowly day-to-day."""
    params = {"date": date} if date else None
    return _through("max_pain", ticker, params, False,
                    lambda: uw.fetch_max_pain(ticker, date=date))


def fetch_net_prem_ticks(ticker: str, date: str | None = None):
    """Minute-by-minute net premium ticks. 5-min TTL."""
    params = {"date": date} if date else None
    return _through("net_prem_ticks", ticker, params, False,
                    lambda: uw.fetch_net_prem_ticks(ticker, date=date))


def fetch_group_flow(group: str = "sp500"):
    """Aggregate per-Greek flow for an index/sector group. Hot TTL (60s) for
    sp500 so sector-rotation regime is fresh per cycle."""
    return _through("group_flow", group, None, True,
                    lambda: uw.fetch_group_flow(group))


def fetch_net_flow_expiry(ticker: str | None = None):
    """Net premium flow grouped by expiry. Medium TTL (5min). Per-ticker when
    `ticker` set; cross-market aggregate otherwise."""
    params = {"ticker_symbol": ticker} if ticker else None
    return _through("net_flow_expiry", ticker, params, False,
                    lambda: uw.fetch_net_flow_expiry(ticker))


def fetch_lit_flow_recent():
    """Recent lit-exchange flow (complement to darkpool). Hot TTL (60s) so we
    catch lit/dark divergence in near-real-time."""
    return _through("lit_flow_recent", None, None, True,
                    lambda: uw.fetch_lit_flow_recent())


def fetch_option_contract_history(symbol: str):
    """Per-contract historic bid/ask/vol/IV. Medium TTL (5min). Lazy-fetched
    on contract selection — not in the per-cycle snapshot fan-out."""
    return _through("option_contract_history", symbol, None, False,
                    lambda: uw.fetch_option_contract_history(symbol))


def fetch_economic_calendar():
    """Economic calendar (FOMC/CPI/jobs/etc.). Quasi-static (24h TTL) —
    event list changes daily at most; stable within a day."""
    return _through("economic_calendar", None, None, False,
                    lambda: uw.fetch_economic_calendar())


def fetch_seasonality_market():
    """Market-level seasonality. Quasi-static (24h TTL)."""
    return _through("seasonality_market", None, None, False,
                    lambda: uw.fetch_seasonality_market())


def fetch_seasonality_ticker(ticker: str):
    """Per-ticker monthly seasonality. Quasi-static (24h TTL)."""
    return _through("seasonality_ticker", ticker, None, False,
                    lambda: uw.fetch_seasonality_ticker(ticker))


def fetch_ohlc(ticker: str, candle_size: str = "5m", is_hot: bool = True):
    """OHLC candles for Tile 1's price line. Hot TTL (60s) so the line tracks
    intraday price moves within one polling cycle of the dashboard."""
    return _through("ohlc", ticker, {"candle_size": candle_size}, is_hot,
                    lambda: uw.fetch_ohlc(ticker, candle_size=candle_size))


# ── Snapshot JSONL appender ───────────────────────────────────────────────────

def _snapshots_path() -> Path:
    return _data_dir() / "snapshots.jsonl"


def _sticky_path() -> Path:
    return _data_dir() / "sticky.json"


def append_snapshot(snapshot: dict) -> bool:
    """Append one snapshot as a JSON line. Best-effort I/O."""
    try:
        path = _snapshots_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
        return True
    except Exception as e:
        log.error("snapshot append failed: %s", e)
        return False


def _view_path(view: str, ticker: str) -> Path:
    safe = "".join(c for c in ticker.upper() if c.isalnum() or c in "._-")
    return _data_dir() / "views" / view / f"{safe}.json"


def save_view(view: str, ticker: str, payload: dict) -> bool:
    """Persist a COMPLETE on-demand view (tile3/tile4/lookup) as one unit, so the
    atomic fallback can replay a single coherent moment instead of reassembling
    each endpoint's most-recent archive row (which mixes moments). Best-effort."""
    try:
        p = _view_path(view, ticker)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception as e:
        log.warning("save_view %s/%s failed: %s", view, ticker, e)
        return False


def read_view(view: str, ticker: str) -> dict | None:
    """Read the last complete persisted view, or None. Best-effort."""
    p = _view_path(view, ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("read_view %s/%s failed: %s", view, ticker, e)
        return None


def read_last_snapshot() -> dict | None:
    """Return the most recent persisted snapshot that HAS rows, as a dict (the
    same shape append_snapshot stored, i.e. Snapshot.model_dump). Used to seed
    the in-memory cache on cold-boot so a fresh container (off-hours, or while UW
    is rate-limited) serves last-good data instead of a blank dashboard. Returns
    None if the file is missing or every line is empty. Best-effort — never
    raises."""
    path = _snapshots_path()
    if not path.exists():
        return None
    try:
        last_good = None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(snap, dict) and snap.get("rows"):
                    last_good = snap   # keep the latest non-empty
        return last_good
    except Exception as e:
        log.warning("read_last_snapshot failed: %s", e)
        return None


def load_sticky() -> dict[str, str]:
    """Read sticky.json (ticker → ISO timestamp of last hot_15 appearance).

    Returns {} on missing or corrupt file, never raises."""
    path = _sticky_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("sticky.json corrupt; treating as empty: %s", e)
        return {}


def save_sticky(state: dict[str, str]) -> bool:
    """Persist sticky state atomically via tmp-file rename."""
    path = _sticky_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error("sticky.json save failed: %s", e)
        return False
