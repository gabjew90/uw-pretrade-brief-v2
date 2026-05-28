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
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
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


# TTL tiers per §3a/§3b of the spec
_TTL_HOT_SECONDS = 60         # hot_15 tickers
_TTL_SLEEPER_SECONDS = 300    # non-hot tracked tickers (5 min)
_TTL_QUASI_STATIC_SECONDS = 21600  # earnings (6h)
_QUASI_STATIC_ENDPOINTS = {"earnings"}


def _ttl_seconds(endpoint: str, is_hot: bool) -> int:
    if endpoint in _QUASI_STATIC_ENDPOINTS:
        return _TTL_QUASI_STATIC_SECONDS
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


def _through(endpoint: str, ticker: str | None, params: dict | None, is_hot: bool,
             uw_call):
    """Generic read-through path:
      1. cache hit  → return cached
      2. cache miss → call UW
         a. on success → write parquet, cache, return response
         b. on UWError → return UWFailure (not cached)
    """
    from datetime import timezone   # local import to avoid name shadowing
    key = _make_key(endpoint, ticker, params)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    started_at = time.monotonic()
    try:
        response = uw_call()
    except uw.UWError as e:
        return UWFailure(endpoint=endpoint, ticker=ticker, message=str(e))
    latency_ms = int((time.monotonic() - started_at) * 1000)
    write_response(
        endpoint=endpoint, ticker=ticker, params=params, response=response,
        status_code=200, latency_ms=latency_ms,
        fetched_at=datetime.now(tz=timezone.utc),
    )
    _cache.set(key, response, ttl_seconds=_ttl_seconds(endpoint, is_hot))
    return response


# ── Public per-endpoint fetch wrappers ────────────────────────────────────────
# Mirror the names in server/uw.py so the snapshot pipeline can swap
# `uw.fetch_*` for `storage.fetch_*` mechanically.

def fetch_spot_exposures_strike(ticker: str, is_hot: bool = False):
    return _through("spot_exposures_strike", ticker, None, is_hot,
                    lambda: uw.fetch_spot_exposures_strike(ticker))


def fetch_oi_strike(ticker: str, is_hot: bool = False):
    return _through("oi_per_strike", ticker, None, is_hot,
                    lambda: uw.fetch_oi_strike(ticker))


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
