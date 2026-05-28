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

from server import uw
from server.cache import TTLCache

log = logging.getLogger(__name__)


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

_QUASI_STATIC_ENDPOINTS = {"earnings", "ticker_info", "max_pain"}
_MEDIUM_ENDPOINTS = {
    "volatility_term_structure", "interpolated_iv",
    "sector_tide", "market_tide", "option_contracts", "net_prem_ticks",
}
_NEWS_ENDPOINTS = {"news_headlines"}

# UW Basic tier = 120 req/min = 2 calls/sec sustained. With ThreadPoolExecutor
# at 8 workers, no throttle means up to 8 calls in parallel completing in
# <1s each → 8-16 calls/sec = WAY past the per-second budget.
# Setting semaphore to 2 caps real throughput to ~2-4 calls/sec, comfortable
# within UW's per-minute quota. Yes, this slows the pipeline a lot — that's
# the point. Better to be slow than 429'd permanently.
import threading
_UW_CONCURRENCY_LIMIT = 2
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
) -> Any | None:
    """Look for the most recent matching row in the parquet archive whose
    fetched_at is within `max_age_seconds` of now. Returns the deserialized
    response if a fresh row exists, else None.

    Scans today's hour-partition files newest-first; also peeks at the most
    recent file from yesterday's partition if today's holds nothing fresh
    (covers the midnight rollover case for long-TTL endpoints like earnings).
    """
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(seconds=max_age_seconds)
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
                return json.loads(response_str)
            except Exception as e:
                log.warning("parquet read failed: %s/%s @ %s: %s",
                            endpoint, ticker, path.name, e)
                continue
    return None


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
    cached = _cache.get(key)
    if cached is not None:
        return cached

    # 2. Parquet cache (persistent, survives restarts)
    parquet_hit = _read_latest_from_parquet(endpoint, ticker, params, max_age_seconds=ttl)
    if parquet_hit is not None:
        _cache.set(key, parquet_hit, ttl_seconds=ttl)
        return parquet_hit

    # 3. UW (only when nothing fresh exists)
    started_at = time.monotonic()
    try:
        with _uw_call_gate:
            response = uw_call()
    except uw.UWError as e:
        return UWFailure(endpoint=endpoint, ticker=ticker, message=str(e))
    latency_ms = int((time.monotonic() - started_at) * 1000)
    write_response(
        endpoint=endpoint, ticker=ticker, params=params, response=response,
        status_code=200, latency_ms=latency_ms,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    _cache.set(key, response, ttl_seconds=ttl)
    return response


# ── Public per-endpoint fetch wrappers ────────────────────────────────────────
# Mirror the names in server/uw.py so the snapshot pipeline can swap
# `uw.fetch_*` for `storage.fetch_*` mechanically.

def fetch_spot_exposures_strike(ticker: str, is_hot: bool = False):
    return _through("spot_exposures_strike", ticker, None, is_hot,
                    lambda: uw.fetch_spot_exposures_strike(ticker))


def fetch_oi_strike(ticker: str, is_hot: bool = False, date: str | None = None):
    """Fetch OI per strike. `date` is ISO YYYY-MM-DD (None = today's snapshot).
    Cache key includes the date so today's and yesterday's snapshots don't collide."""
    params = {"date": date} if date else None
    return _through("oi_per_strike", ticker, params, is_hot,
                    lambda: uw.fetch_oi_strike(ticker, date=date))


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


def fetch_flow_alerts(limit: int = 100):
    # Cross-ticker endpoint; treated as "hot" (60s TTL) since hot-list computation
    # is on the critical path every snapshot.
    return _through("flow_alerts", None, {"limit": limit}, True,
                    lambda: uw.fetch_flow_alerts(limit=limit))


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
